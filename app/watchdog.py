"""
Background watchdog: periodically reads EPICS PVs, evaluates alarm conditions,
monitors system processes, and sends email notifications.
"""
import json
import logging
import os
from datetime import datetime

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


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def _pv_alarm_body(pv, value):
    return (
        f"EPICS PV Watchdog — ALARM\n"
        f"{'='*50}\n"
        f"PV Name   : {pv.pv_name}\n"
        f"Alias     : {pv.alias or 'N/A'}\n"
        f"Description: {pv.description or 'N/A'}\n\n"
        f"Current Value : {value}\n"
        f"Alarm Condition: {pv.condition_str}\n\n"
        f"Status : ALARM\n"
        f"Time   : {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
        f"This is an automated message from EPICS PV Watchdog.\n"
    )


def _pv_recovery_body(pv, value):
    return (
        f"EPICS PV Watchdog — RECOVERED\n"
        f"{'='*50}\n"
        f"PV Name : {pv.pv_name}\n"
        f"Alias   : {pv.alias or 'N/A'}\n\n"
        f"Current Value : {value}\n"
        f"Alarm Condition (no longer met): {pv.condition_str}\n\n"
        f"Status : RECOVERED\n"
        f"Time   : {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
        f"This is an automated message from EPICS PV Watchdog.\n"
    )


def _pv_disconnect_body(pv):
    return (
        f"EPICS PV Watchdog — DISCONNECTED\n"
        f"{'='*50}\n"
        f"PV Name   : {pv.pv_name}\n"
        f"Alias     : {pv.alias or 'N/A'}\n"
        f"Description: {pv.description or 'N/A'}\n\n"
        f"The PV could not be reached (Channel Access timeout).\n\n"
        f"Status : DISCONNECTED\n"
        f"Time   : {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
        f"This is an automated message from EPICS PV Watchdog.\n"
    )


def _pv_reconnect_body(pv, value):
    return (
        f"EPICS PV Watchdog — RECONNECTED\n"
        f"{'='*50}\n"
        f"PV Name : {pv.pv_name}\n"
        f"Alias   : {pv.alias or 'N/A'}\n\n"
        f"Current Value : {value}\n\n"
        f"Status : RECONNECTED\n"
        f"Time   : {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
        f"This is an automated message from EPICS PV Watchdog.\n"
    )


def _rule_alarm_body(rule, pv_status_map):
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
        f"Time   : {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
        f"This is an automated message from EPICS PV Watchdog.\n"
    )


def _rule_recovery_body(rule):
    return (
        f"EPICS PV Watchdog — RULE RECOVERED\n"
        f"{'='*50}\n"
        f"Rule   : {rule.name}\n\n"
        f"Status : RECOVERED\n"
        f"Time   : {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
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
    """
    import shlex
    import subprocess

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


def _proc_stopped_body(pm):
    return (
        f"EPICS PV Watchdog — PROCESS STOPPED\n"
        f"{'='*50}\n"
        f"Process     : {pm.name}\n"
        f"Description : {pm.description or 'N/A'}\n"
        f"Match Type  : {pm.match_type}\n"
        f"Match Value : {pm.match_value}\n\n"
        f"Status : STOPPED (process not found)\n"
        f"Time   : {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
        f"This is an automated message from EPICS PV Watchdog.\n"
    )


def _proc_recovery_body(pm):
    return (
        f"EPICS PV Watchdog — PROCESS RECOVERED\n"
        f"{'='*50}\n"
        f"Process : {pm.name}\n"
        f"PID     : {pm.pid}\n\n"
        f"Status : RUNNING\n"
        f"Time   : {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
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

        # --- Individual PVs ---
        pv_status_map = {}

        for pv in PVMonitor.query.filter_by(enabled=True).all():
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
                                body = _pv_disconnect_body(pv)
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

                    alarm = is_in_alarm(raw, pv.condition_op,
                                        pv.condition_value, pv.condition_value2)

                    # Reconnect notification (was disconnected, now readable)
                    if prev == 'DISCONNECTED' and pv.notify_on_disconnect and pv.email_list_id:
                        elist = EmailList.query.get(pv.email_list_id)
                        if elist:
                            body = _pv_reconnect_body(pv, raw)
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
                        pv.last_alarm = now

                        if pv.notify_flag and pv.email_list_id:
                            ni = pv.notify_interval or default_notify_interval
                            should = (prev not in ('ALARM',) or pv.last_notified is None or
                                      (now - pv.last_notified).total_seconds() >= ni)
                            if should:
                                elist = EmailList.query.get(pv.email_list_id)
                                if elist:
                                    body = _pv_alarm_body(pv, raw)
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
                                    body = _pv_recovery_body(pv, raw)
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
                pv.status = 'ERROR'
                pv_status_map[pv.id] = 'ERROR'

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
                                body = _rule_alarm_body(rule, pv_status_map)
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
                            body = _rule_recovery_body(rule)
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
                            body = _proc_recovery_body(pm)
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
                                body = _proc_stopped_body(pm)
                                ok, err = send_notification_email(
                                    elist.get_emails(),
                                    f'[WATCHDOG PROCESS STOPPED] {pm.name}',
                                    body, cfg)
                                _log_notification(db, 'PROCESS', pm.id, pm.name,
                                                  'ALARM', body, elist.get_emails(), ok, err)
                                pm.last_notified = now

            except Exception as exc:
                logger.exception('Error checking process %s: %s', pm.name, exc)
                pm.status = 'UNKNOWN'

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
    _scheduler.start()
    logger.info('Watchdog started — check interval %ds', interval)

    import atexit
    atexit.register(lambda: _scheduler.shutdown(wait=False))


def restart_watchdog(app, new_interval):
    """Call from admin settings when check_interval changes."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.reschedule_job('watchdog', trigger='interval', seconds=new_interval)
        logger.info('Watchdog rescheduled to %ds', new_interval)
