"""
Background watchdog: periodically reads EPICS PVs, evaluates alarm conditions,
monitors system processes, and sends email notifications.
"""
import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psutil

logger = logging.getLogger(__name__)

try:
    import epics
    EPICS_AVAILABLE = True
    logger.info('pyepics loaded — EPICS Channel Access available')
except ImportError:
    EPICS_AVAILABLE = False
    logger.warning('pyepics not installed — PV values will read as DISCONNECTED')

_scheduler = None


def _local_now_str(cfg):
    """Return current time formatted in the configured display timezone."""
    tz_name = cfg.get('timezone') or 'UTC'
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        tz = ZoneInfo('UTC')
        tz_name = 'UTC'
    now = datetime.now(tz=ZoneInfo('UTC')).astimezone(tz)
    return now.strftime(f'%Y-%m-%d %H:%M:%S {tz_name}')


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def _pv_alarm_body(pv, value, now_str):
    return (
        f"EPICS PV Watchdog — ALARM\n"
        f"{'='*50}\n"
        f"PV Name   : {pv.pv_name}\n"
        f"Alias     : {pv.alias or 'N/A'}\n"
        f"Description: {pv.description or 'N/A'}\n\n"
        f"Current Value : {value}\n"
        f"Alarm Condition: {pv.condition_str}\n\n"
        f"Status : ALARM\n"
        f"Time   : {now_str}\n\n"
        f"This is an automated message from EPICS PV Watchdog.\n"
    )


def _pv_recovery_body(pv, value, now_str):
    return (
        f"EPICS PV Watchdog — RECOVERED\n"
        f"{'='*50}\n"
        f"PV Name : {pv.pv_name}\n"
        f"Alias   : {pv.alias or 'N/A'}\n\n"
        f"Current Value : {value}\n"
        f"Alarm Condition (no longer met): {pv.condition_str}\n\n"
        f"Status : RECOVERED\n"
        f"Time   : {now_str}\n\n"
        f"This is an automated message from EPICS PV Watchdog.\n"
    )


def _pv_disconnect_body(pv, now_str):
    return (
        f"EPICS PV Watchdog — DISCONNECTED\n"
        f"{'='*50}\n"
        f"PV Name   : {pv.pv_name}\n"
        f"Alias     : {pv.alias or 'N/A'}\n"
        f"Description: {pv.description or 'N/A'}\n\n"
        f"The PV could not be reached (Channel Access timeout).\n\n"
        f"Status : DISCONNECTED\n"
        f"Time   : {now_str}\n\n"
        f"This is an automated message from EPICS PV Watchdog.\n"
    )


def _pv_reconnect_body(pv, value, now_str):
    return (
        f"EPICS PV Watchdog — RECONNECTED\n"
        f"{'='*50}\n"
        f"PV Name : {pv.pv_name}\n"
        f"Alias   : {pv.alias or 'N/A'}\n\n"
        f"Current Value : {value}\n\n"
        f"Status : RECONNECTED\n"
        f"Time   : {now_str}\n\n"
        f"This is an automated message from EPICS PV Watchdog.\n"
    )


def _rule_alarm_body(rule, pv_status_map, now_str):
    from .models import PVMonitor
    expr = rule.get_expression()
    lines = []
    for cond in expr.get('conditions', []):
        pv = PVMonitor.query.get(cond.get('pv_id'))
        if pv:
            actual = pv_status_map.get(pv.id, 'UNKNOWN')
            expected = cond.get('expected_status', 'ALARM')
            lines.append(f"  {pv.display_name}: current={actual}, expected={expected}")

    return (
        f"EPICS PV Watchdog — RULE ALARM\n"
        f"{'='*50}\n"
        f"Rule        : {rule.name}\n"
        f"Description : {rule.description or 'N/A'}\n"
        f"Logic       : {expr.get('logic', 'AND')}\n\n"
        f"PV Conditions:\n" + '\n'.join(lines) + "\n\n"
        f"Status : ALARM\n"
        f"Time   : {now_str}\n\n"
        f"This is an automated message from EPICS PV Watchdog.\n"
    )


def _rule_recovery_body(rule, now_str):
    return (
        f"EPICS PV Watchdog — RULE RECOVERED\n"
        f"{'='*50}\n"
        f"Rule   : {rule.name}\n\n"
        f"Status : RECOVERED\n"
        f"Time   : {now_str}\n\n"
        f"This is an automated message from EPICS PV Watchdog.\n"
    )


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------

def _find_process(match_type, match_value):
    """
    Search running processes.
    Returns (is_running: bool, pid: int|None, proc_name: str|None).

    match_type 'command': runs match_value as a shell command; exit code 0 → running.
    match_type 'ping':    sends one ICMP echo to the host in match_value; reply → running.
    """
    import shlex
    import subprocess
    import sys

    if match_type == 'ping':
        host = match_value.strip()
        if not host or host.startswith('-') or any(c.isspace() for c in host):
            logger.warning('Invalid ping host %r', match_value)
            return False, None, None
        # -W is milliseconds on macOS, seconds on Linux
        wait = '2000' if sys.platform == 'darwin' else '2'
        try:
            result = subprocess.run(
                ['ping', '-c', '1', '-W', wait, host],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0, None, host
        except Exception as exc:
            logger.warning('Ping check failed for %r: %s', host, exc)
            return False, None, None

    if match_type == 'command':
        try:
            result = subprocess.run(
                shlex.split(match_value),
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0, None, match_value
        except Exception as exc:
            logger.warning('Command check failed for %r: %s', match_value, exc)
            return False, None, None

    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if match_type == 'name':
                if proc.info['name'] and match_value.lower() in proc.info['name'].lower():
                    return True, proc.info['pid'], proc.info['name']
            elif match_type == 'cmdline':
                cmdline = ' '.join(proc.info['cmdline'] or [])
                if match_value in cmdline:
                    return True, proc.info['pid'], proc.info['name']
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return False, None, None


def _proc_stopped_body(pm, now_str):
    return (
        f"EPICS PV Watchdog — PROCESS STOPPED\n"
        f"{'='*50}\n"
        f"Process     : {pm.name}\n"
        f"Description : {pm.description or 'N/A'}\n"
        f"Match Type  : {pm.match_type}\n"
        f"Match Value : {pm.match_value}\n\n"
        f"Status : STOPPED (process not found)\n"
        f"Time   : {now_str}\n\n"
        f"This is an automated message from EPICS PV Watchdog.\n"
    )


def _proc_recovery_body(pm, now_str):
    return (
        f"EPICS PV Watchdog — PROCESS RECOVERED\n"
        f"{'='*50}\n"
        f"Process : {pm.name}\n"
        f"PID     : {pm.pid}\n\n"
        f"Status : RUNNING\n"
        f"Time   : {now_str}\n\n"
        f"This is an automated message from EPICS PV Watchdog.\n"
    )


# ---------------------------------------------------------------------------
# Core check function
# ---------------------------------------------------------------------------

def _log_notification(db, source_type, source_id, source_name,
                       event_status, body, emails, success, err):
    from .models import NotificationLog
    entry = NotificationLog(
        source_type=source_type,
        source_id=source_id,
        source_name=source_name,
        event_status=event_status,
        message=body,
        success=success,
        error_message=None if success else err,
    )
    entry.recipients = emails
    db.session.add(entry)


def check_all_pvs(app):
    """Main watchdog tick — called by APScheduler every check_interval seconds."""
    with app.app_context():
        from . import db
        from .models import PVMonitor, CompoundRule, EmailList, SystemConfig
        from .condition_eval import is_in_alarm, evaluate_compound_rule
        from .email_utils import send_notification_email

        cfg = {c.key: c.value for c in SystemConfig.query.all()}
        default_notify_interval = int(cfg.get('default_notify_interval', 3600))
        now = datetime.utcnow()
        now_str = _local_now_str(cfg)

        # --- Individual PVs ---
        pv_status_map = {}

        for pv in PVMonitor.query.all():
            try:
                if EPICS_AVAILABLE:
                    raw = epics.caget(pv.pv_name, timeout=1.0)
                else:
                    raw = None

                prev = pv.status
                pv.last_checked = now

                if raw is None:
                    pv.current_value = None
                    pv.current_value_str = 'N/A'
                    pv.pending_since = None
                    if pv.enabled:
                        pv.status = 'DISCONNECTED'
                        pv_status_map[pv.id] = 'DISCONNECTED'

                        if pv.notify_on_disconnect and pv.email_list_id:
                            ni = pv.notify_interval or default_notify_interval
                            should = (prev != 'DISCONNECTED' or
                                      pv.last_notified_disconnect is None or
                                      (now - pv.last_notified_disconnect).total_seconds() >= ni)
                            if should:
                                elist = EmailList.query.get(pv.email_list_id)
                                if elist:
                                    body = _pv_disconnect_body(pv, now_str)
                                    ok, err = send_notification_email(
                                        elist.get_emails(),
                                        f'[WATCHDOG DISCONNECTED] {pv.display_name}',
                                        body, cfg)
                                    _log_notification(db, 'PV', pv.id, pv.pv_name,
                                                      'DISCONNECTED', body, elist.get_emails(), ok, err)
                                    pv.last_notified_disconnect = now
                else:
                    try:
                        fval = float(raw)
                        pv.current_value = fval
                        pv.current_value_str = f'{fval:.6g}'
                    except (TypeError, ValueError):
                        pv.current_value = None
                        pv.current_value_str = str(raw)

                    if not pv.enabled:
                        continue  # value updated; skip alarm/notification for disabled PVs

                    alarm = is_in_alarm(raw, pv.condition_op,
                                        pv.condition_value, pv.condition_value2,
                                        prev_alarm=(prev == 'ALARM'),
                                        clear_value=pv.condition_clear_value)

                    # Time debounce: a state change must persist for
                    # alarm_delay / clear_delay seconds before it takes effect.
                    # While pending, the previous alarm/non-alarm state is held.
                    if alarm is None:
                        pv.pending_since = None
                    else:
                        was_alarm = (prev == 'ALARM')
                        delay = ((pv.alarm_delay if alarm else pv.clear_delay) or 0)
                        if alarm == was_alarm:
                            pv.pending_since = None
                        elif delay > 0:
                            if pv.pending_since is None:
                                pv.pending_since = now
                            if (now - pv.pending_since).total_seconds() < delay:
                                alarm = was_alarm  # hold previous state
                            else:
                                pv.pending_since = None
                        else:
                            pv.pending_since = None

                    # Reconnect notification (was disconnected, now readable)
                    if prev == 'DISCONNECTED' and pv.notify_on_disconnect and pv.email_list_id:
                        elist = EmailList.query.get(pv.email_list_id)
                        if elist:
                            body = _pv_reconnect_body(pv, raw, now_str)
                            ok, err = send_notification_email(
                                elist.get_emails(),
                                f'[WATCHDOG RECONNECTED] {pv.display_name}',
                                body, cfg)
                            _log_notification(db, 'PV', pv.id, pv.pv_name,
                                              'RECONNECTED', body, elist.get_emails(), ok, err)

                    if alarm is None:
                        pv.status = 'ERROR'
                        pv_status_map[pv.id] = 'ERROR'
                    elif alarm:
                        pv.status = 'ALARM'
                        pv_status_map[pv.id] = 'ALARM'
                        if prev != 'ALARM':
                            pv.last_alarm = now

                        if pv.notify_flag and pv.email_list_id:
                            ni = pv.notify_interval or default_notify_interval
                            should = (prev not in ('ALARM',) or pv.last_notified is None or
                                      (now - pv.last_notified).total_seconds() >= ni)
                            if should:
                                elist = EmailList.query.get(pv.email_list_id)
                                if elist:
                                    body = _pv_alarm_body(pv, raw, now_str)
                                    ok, err = send_notification_email(
                                        elist.get_emails(),
                                        f'[WATCHDOG ALARM] {pv.display_name}',
                                        body, cfg)
                                    _log_notification(db, 'PV', pv.id, pv.pv_name,
                                                      'ALARM', body, elist.get_emails(), ok, err)
                                    pv.last_notified = now
                    else:
                        pv_status_map[pv.id] = 'OK'
                        if prev == 'ALARM':
                            pv.status = 'OK'
                            if pv.notify_flag and pv.email_list_id:
                                elist = EmailList.query.get(pv.email_list_id)
                                if elist:
                                    body = _pv_recovery_body(pv, raw, now_str)
                                    ok, err = send_notification_email(
                                        elist.get_emails(),
                                        f'[WATCHDOG RECOVERED] {pv.display_name}',
                                        body, cfg)
                                    _log_notification(db, 'PV', pv.id, pv.pv_name,
                                                      'RECOVERED', body, elist.get_emails(), ok, err)
                        else:
                            pv.status = 'OK'

            except Exception as exc:
                logger.exception('Error checking PV %s: %s', pv.pv_name, exc)
                if pv.enabled:
                    pv.status = 'ERROR'
                    pv_status_map[pv.id] = 'ERROR'

        db.session.commit()

        # --- Auto-enable disabled PVs ---
        # PVs with auto_enable=True are checked even while disabled.
        # When the alarm condition is no longer met the PV is re-enabled automatically.
        if EPICS_AVAILABLE:
            for pv in PVMonitor.query.filter_by(enabled=False, auto_enable=True).all():
                try:
                    raw = epics.caget(pv.pv_name, timeout=1.0)
                    if raw is None:
                        continue  # still disconnected — leave disabled
                    alarm = is_in_alarm(raw, pv.condition_op,
                                        pv.condition_value, pv.condition_value2,
                                        prev_alarm=True,
                                        clear_value=pv.condition_clear_value)
                    if alarm is False:  # condition no longer met → re-enable
                        try:
                            fval = float(raw)
                            pv.current_value = fval
                            pv.current_value_str = f'{fval:.6g}'
                        except (TypeError, ValueError):
                            pv.current_value = None
                            pv.current_value_str = str(raw)
                        pv.enabled = True
                        pv.status = 'OK'
                        pv.last_checked = now
                        pv.auto_enabled_at = now
                        logger.info('Auto-enabled PV %s (value=%s)', pv.pv_name, raw)
                        body = (
                            f"EPICS PV Watchdog — AUTO-ENABLED\n"
                            f"{'='*50}\n"
                            f"PV Name : {pv.pv_name}\n"
                            f"Alias   : {pv.alias or 'N/A'}\n\n"
                            f"Current Value : {raw}\n"
                            f"Condition (no longer met): {pv.condition_str}\n\n"
                            f"The PV has been automatically re-enabled.\n"
                            f"Time : {now_str}\n"
                        )
                        _log_notification(db, 'PV', pv.id, pv.pv_name,
                                          'AUTO_ENABLED', body, [], True, None)
                except Exception as exc:
                    logger.exception('Error during auto-enable check for PV %s: %s',
                                     pv.pv_name, exc)
            db.session.commit()

        # --- Compound rules ---
        for rule in CompoundRule.query.filter_by(enabled=True).all():
            try:
                alarm = evaluate_compound_rule(rule, pv_status_map)
                rule.last_checked = now
                prev = rule.status

                if alarm is None:
                    rule.status = 'UNKNOWN'
                elif alarm:
                    rule.status = 'ALARM'
                    rule.last_alarm = now

                    if rule.notify_flag and rule.email_list_id:
                        ni = rule.notify_interval or default_notify_interval
                        should = (prev != 'ALARM' or rule.last_notified is None or
                                  (now - rule.last_notified).total_seconds() >= ni)
                        if should:
                            elist = EmailList.query.get(rule.email_list_id)
                            if elist:
                                body = _rule_alarm_body(rule, pv_status_map, now_str)
                                ok, err = send_notification_email(
                                    elist.get_emails(),
                                    f'[WATCHDOG RULE ALARM] {rule.name}',
                                    body, cfg)
                                _log_notification(db, 'RULE', rule.id, rule.name,
                                                  'ALARM', body, elist.get_emails(), ok, err)
                                rule.last_notified = now
                else:
                    rule.status = 'OK'
                    if prev == 'ALARM' and rule.notify_flag and rule.email_list_id:
                        elist = EmailList.query.get(rule.email_list_id)
                        if elist:
                            body = _rule_recovery_body(rule, now_str)
                            ok, err = send_notification_email(
                                elist.get_emails(),
                                f'[WATCHDOG RULE RECOVERED] {rule.name}',
                                body, cfg)
                            _log_notification(db, 'RULE', rule.id, rule.name,
                                              'RECOVERED', body, elist.get_emails(), ok, err)

            except Exception as exc:
                logger.exception('Error evaluating rule %s: %s', rule.name, exc)
                rule.status = 'ERROR'

        db.session.commit()

        # --- Process monitors ---
        from .models import ProcessMonitor
        for pm in ProcessMonitor.query.filter_by(enabled=True).all():
            try:
                running, pid, proc_name = _find_process(pm.match_type, pm.match_value)
                prev = pm.status
                pm.last_checked = now

                if running:
                    pm.status = 'RUNNING'
                    pm.pid = pid
                    if prev == 'STOPPED' and pm.notify_flag and pm.email_list_id:
                        elist = EmailList.query.get(pm.email_list_id)
                        if elist:
                            body = _proc_recovery_body(pm, now_str)
                            ok, err = send_notification_email(
                                elist.get_emails(),
                                f'[WATCHDOG PROCESS RUNNING] {pm.name}',
                                body, cfg)
                            _log_notification(db, 'PROCESS', pm.id, pm.name,
                                              'RECOVERED', body, elist.get_emails(), ok, err)
                else:
                    pm.status = 'STOPPED'
                    pm.pid = None
                    if prev != 'STOPPED':
                        pm.last_stopped = now
                    if pm.notify_flag and pm.email_list_id:
                        ni = pm.notify_interval or default_notify_interval
                        should = (prev != 'STOPPED' or pm.last_notified is None or
                                  (now - pm.last_notified).total_seconds() >= ni)
                        if should:
                            elist = EmailList.query.get(pm.email_list_id)
                            if elist:
                                body = _proc_stopped_body(pm, now_str)
                                ok, err = send_notification_email(
                                    elist.get_emails(),
                                    f'[WATCHDOG PROCESS STOPPED] {pm.name}',
                                    body, cfg)
                                _log_notification(db, 'PROCESS', pm.id, pm.name,
                                                  'ALARM', body, elist.get_emails(), ok, err)
                                pm.last_notified = now

                if pm.status != prev or pm.status_since is None:
                    pm.status_since = now

            except Exception as exc:
                logger.exception('Error checking process %s: %s', pm.name, exc)
                if pm.status != 'UNKNOWN':
                    pm.status_since = now
                pm.status = 'UNKNOWN'

        db.session.commit()

        # --- Auto-enable disabled process monitors ---
        # Monitors with auto_enable=True are still checked while disabled; once the
        # process is running (or the device reachable) again they are re-enabled.
        for pm in ProcessMonitor.query.filter_by(enabled=False, auto_enable=True).all():
            try:
                running, pid, _ = _find_process(pm.match_type, pm.match_value)
                if not running:
                    continue  # still down — leave disabled
                pm.enabled = True
                pm.status = 'RUNNING'
                pm.pid = pid
                pm.last_checked = now
                pm.status_since = now
                pm.auto_enabled_at = now
                logger.info('Auto-enabled process monitor %s', pm.name)
                body = (
                    f"EPICS PV Watchdog — AUTO-ENABLED\n"
                    f"{'='*50}\n"
                    f"Process     : {pm.name}\n"
                    f"Match Type  : {pm.match_type}\n"
                    f"Match Value : {pm.match_value}\n\n"
                    f"The process is running again and the monitor has been automatically re-enabled.\n"
                    f"Time : {now_str}\n"
                )
                _log_notification(db, 'PROCESS', pm.id, pm.name,
                                  'AUTO_ENABLED', body, [], True, None)
            except Exception as exc:
                logger.exception('Error during auto-enable check for process %s: %s',
                                 pm.name, exc)

        db.session.commit()


# Standard devIocStats record suffixes (iocAdminSoft / iocAdminVxWorks / iocAdminRTEMS)
_STATS_SUFFIXES = ('HEARTBEAT', 'UPTIME', 'STARTTOD', 'IOC_CPU_LOAD',
                    'MEM_USED', 'MEM_FREE', 'MEM_MAX', 'MEM_USED.EGU')

_IOC_EVENTS = {
    # event: (heading, subject tag)
    'STOPPED':      ('SOFT-IOC STOPPED',        'IOC STOPPED'),
    'UNREACHABLE':  ('IOC UNREACHABLE',         'IOC UNREACHABLE'),
    'HUNG':         ('IOC NOT RESPONDING',      'IOC HUNG'),
    'DISCONNECTED': ('IOC NOT REACHABLE (CA)',  'IOC DISCONNECTED'),
    'RECOVERED':    ('IOC RECOVERED',           'IOC RUNNING'),
    'RESTARTED':    ('IOC RESTARTED',           'IOC RESTARTED'),
}
_IOC_BAD = ('STOPPED', 'UNREACHABLE', 'HUNG', 'DISCONNECTED')


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _egu_scale(egu):
    """Bytes per unit for a devIocStats memory EGU string (default: bytes)."""
    e = str(egu or '').strip().lower()
    if e.startswith('k'):
        return 1024.0
    if e.startswith('m'):
        return 1024.0 ** 2
    if e.startswith('g'):
        return 1024.0 ** 3
    return 1.0


def _fetch_ioc_stats(iocs):
    """
    Read devIocStats PVs for every IOC that has a stats_prefix, in one parallel CA call.
    Returns {ioc.id: {suffix: value}}; values are None for PVs that did not respond.
    """
    wanted = [i for i in iocs if i.stats_prefix]
    if not wanted or not EPICS_AVAILABLE:
        return {}
    names = [f'{i.stats_prefix}:{sfx}' for i in wanted for sfx in _STATS_SUFFIXES]
    try:
        vals = epics.caget_many(names, as_string=False, as_numpy=False,
                                timeout=2.0, connection_timeout=3.0)
    except Exception as exc:
        logger.warning('devIocStats read failed: %s', exc)
        vals = [None] * len(names)
    n = len(_STATS_SUFFIXES)
    return {i.id: dict(zip(_STATS_SUFFIXES, vals[k * n:(k + 1) * n]))
            for k, i in enumerate(wanted)}


def _apply_stats(ioc, stats, now):
    """
    Update the IOC's devIocStats fields from `stats`.
    Returns (hb_state, restarted): hb_state is 'OK', 'STALE', 'DISCONNECTED', 'PENDING'
    (heartbeat missing but not yet for hb_timeout seconds) or None (no prefix). A frozen
    heartbeat only counts as STALE once it has not changed for hb_timeout seconds.
    """
    if not ioc.stats_prefix or stats is None:
        return None, False
    hb = stats.get('HEARTBEAT')
    timeout = ioc.hb_timeout or 30
    restarted = False

    if hb is None:
        ioc.hb_bad_since = ioc.hb_bad_since or now
        state = 'DISCONNECTED'
    else:
        try:
            hbf = float(hb)
        except (TypeError, ValueError):
            hbf = None
        if hbf is not None and hbf != ioc.hb_value:
            ioc.hb_value = hbf
            ioc.hb_changed_at = now
            ioc.hb_bad_since = None
            state = 'OK'
        else:
            if ioc.hb_changed_at is None:
                ioc.hb_changed_at = now
            ioc.hb_bad_since = None
            frozen = (now - ioc.hb_changed_at).total_seconds()
            state = 'STALE' if frozen >= timeout else 'OK'
        started = stats.get('STARTTOD')
        started = str(started).strip() if started not in (None, '') else None
        if started and ioc.ioc_started and started != ioc.ioc_started:
            restarted = True
            ioc.last_restart = now
        if started:
            ioc.ioc_started = started
        up = stats.get('UPTIME')
        ioc.ioc_uptime = str(up).strip() if up not in (None, '') else ioc.ioc_uptime
        ioc.ioc_cpu = _num(stats.get('IOC_CPU_LOAD'))
        scale = _egu_scale(stats.get('MEM_USED.EGU'))
        used, free, mx = (_num(stats.get(k)) for k in ('MEM_USED', 'MEM_FREE', 'MEM_MAX'))
        total = mx if mx else (used + free if used is not None and free is not None else None)
        ioc.ioc_mem_used = used * scale if used is not None else None
        ioc.ioc_mem_total = total * scale if total else None

    if state == 'DISCONNECTED' and (now - ioc.hb_bad_since).total_seconds() < timeout:
        return 'PENDING', False   # give a transient CA hiccup time to clear
    return state, restarted


def _combine_ioc_status(ioc, proc, hb_state):
    """
    Merge the process-level result `proc` ((status, detail) from SSH, or None for hard
    IOCs) with the heartbeat state into one (status, detail).
    """
    if ioc.is_hard:
        if hb_state == 'STALE':
            return 'HUNG', 'Heartbeat is frozen (IOC reachable but not running)'
        if hb_state == 'DISCONNECTED':
            return 'DISCONNECTED', 'No Channel Access response from the IOC'
        if hb_state == 'OK':
            return 'RUNNING', None
        if hb_state == 'PENDING':           # transient: keep the previous status
            return ioc.status or 'UNKNOWN', ioc.status_detail
        return 'UNKNOWN', None

    status, detail = proc
    if hb_state == 'PENDING':
        hb_state = None
    if not ioc.stats_prefix:
        return status, detail
    if status == 'STOPPED':
        return 'STOPPED', detail
    if status == 'UNREACHABLE':
        if hb_state == 'OK':
            # SSH is down but the IOC itself answers: it is running, control is unavailable
            return 'RUNNING', (f'SSH check failed ({detail}); '
                               'IOC still answers over Channel Access')
        return status, detail
    # process is RUNNING
    if hb_state == 'STALE':
        return 'HUNG', 'Process is running but the heartbeat is frozen'
    if hb_state == 'DISCONNECTED':
        return 'HUNG', 'Process is running but the IOC does not answer over Channel Access'
    return 'RUNNING', None


def _ioc_body(ioc, now_str, event):
    head = _IOC_EVENTS[event][0]
    lines = [
        f"EPICS PV Watchdog — {head}",
        '=' * 50,
        f"IOC         : {ioc.name} ({ioc.ioc_type})",
        f"Host        : {ioc.target}",
        f"Description : {ioc.description or 'N/A'}",
        '',
        f"Status : {ioc.status}{' — ' + ioc.status_detail if ioc.status_detail else ''}",
    ]
    if ioc.stats_prefix:
        lines.append(f"Uptime : {ioc.ioc_uptime or 'N/A'}   (started {ioc.ioc_started or 'N/A'})")
    lines += [f"Time   : {now_str}", '',
              'This is an automated message from EPICS PV Watchdog.', '']
    return '\n'.join(lines)


def check_iocs(app):
    """Check every IOC: SSH/process state (soft) + devIocStats heartbeat. Called by APScheduler."""
    from concurrent.futures import ThreadPoolExecutor
    with app.app_context():
        from . import db
        from .models import SoftIOC, EmailList, SystemConfig
        from .email_utils import send_notification_email
        from .ioc_utils import check_ioc

        cfg = {c.key: c.value for c in SystemConfig.query.all()}
        default_notify_interval = int(cfg.get('default_notify_interval', 3600))
        now = datetime.utcnow()
        now_str = _local_now_str(cfg)

        iocs = SoftIOC.query.filter(
            (SoftIOC.enabled == True) | (SoftIOC.auto_enable == True)).all()  # noqa: E712
        if not iocs:
            return

        def _check(ioc):
            if ioc.is_hard:
                return None
            try:
                return check_ioc(ioc)
            except Exception as exc:
                logger.exception('IOC check failed for %s', ioc.name)
                return 'UNREACHABLE', str(exc)[:500]

        soft = [i for i in iocs if not i.is_hard]
        procs = {}
        if soft:
            with ThreadPoolExecutor(max_workers=min(8, len(soft))) as pool:
                procs = dict(zip((i.id for i in soft), pool.map(_check, soft)))
        all_stats = _fetch_ioc_stats(iocs)

        for ioc in iocs:
            try:
                hb_state, restarted = _apply_stats(ioc, all_stats.get(ioc.id), now) \
                    if ioc.stats_prefix else (None, False)
                status, detail = _combine_ioc_status(ioc, procs.get(ioc.id), hb_state)

                if not ioc.enabled:
                    # Auto-enable: only re-enable once the IOC is running again
                    if status == 'RUNNING':
                        ioc.enabled = True
                        ioc.status, ioc.status_detail = status, detail
                        ioc.last_checked = ioc.status_since = ioc.auto_enabled_at = now
                        _log_notification(db, 'IOC', ioc.id, ioc.name, 'AUTO_ENABLED',
                                          f"IOC {ioc.name} is running again; monitor "
                                          f"re-enabled at {now_str}.", [], True, None)
                    continue

                prev = ioc.status
                ioc.last_checked = now
                ioc.status, ioc.status_detail = status, detail
                if status in ('STOPPED', 'HUNG', 'DISCONNECTED') and prev != status:
                    ioc.last_stopped = now

                event = None
                if status == 'RUNNING':
                    if prev in _IOC_BAD:
                        event = 'RECOVERED'
                    elif restarted:
                        event = 'RESTARTED'     # restarted between two checks
                elif status in _IOC_BAD:
                    ni = ioc.notify_interval or default_notify_interval
                    if prev != status or ioc.last_notified is None or \
                            (now - ioc.last_notified).total_seconds() >= ni:
                        event = status
                        ioc.last_notified = now

                if event and ioc.notify_flag and ioc.email_list_id:
                    elist = EmailList.query.get(ioc.email_list_id)
                    if elist:
                        body = _ioc_body(ioc, now_str, event)
                        ok, err = send_notification_email(
                            elist.get_emails(),
                            f'[WATCHDOG {_IOC_EVENTS[event][1]}] {ioc.name}', body, cfg)
                        _log_notification(db, 'IOC', ioc.id, ioc.name, event, body,
                                          elist.get_emails(), ok, err)

                if ioc.status != prev or ioc.status_since is None:
                    ioc.status_since = now
            except Exception as exc:
                logger.exception('Error processing IOC %s: %s', ioc.name, exc)

        db.session.commit()


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------

def start_watchdog(app):
    global _scheduler

    # In Flask debug mode the reloader forks a child; only start in the child.
    if app.debug and os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        return

    from apscheduler.schedulers.background import BackgroundScheduler

    with app.app_context():
        from .models import SystemConfig
        row = SystemConfig.query.filter_by(key='check_interval').first()
        interval = int(row.value) if row and row.value else 10

    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        func=check_all_pvs,
        args=[app],
        trigger='interval',
        seconds=interval,
        id='watchdog',
        replace_existing=True,
        misfire_grace_time=interval,
    )
    _scheduler.add_job(
        func=check_iocs,
        args=[app],
        trigger='interval',
        seconds=interval,
        id='ioc_watchdog',
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=interval,
    )
    _scheduler.start()
    logger.info('Watchdog started — check interval %ds', interval)

    import atexit
    atexit.register(lambda: _scheduler.shutdown(wait=False))


def restart_watchdog(app, new_interval):
    """Call from admin settings when check_interval changes."""
    global _scheduler
    if _scheduler and _scheduler.running:
        try:
            _scheduler.reschedule_job('watchdog', trigger='interval', seconds=new_interval)
            _scheduler.reschedule_job('ioc_watchdog', trigger='interval', seconds=new_interval)
            logger.info('Watchdog rescheduled to %ds', new_interval)
        except Exception as exc:
            logger.warning('reschedule_job failed (%s); watchdog keeps current interval', exc)
