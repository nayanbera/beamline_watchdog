from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from flask import Blueprint, render_template, jsonify
from ..models import PVMonitor, CompoundRule, ProcessMonitor

dashboard_bp = Blueprint('dashboard', __name__)


def _display_tz():
    from ..models import SystemConfig
    row = SystemConfig.query.filter_by(key='timezone').first()
    tz_name = row.value if (row and row.value) else 'UTC'
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        return ZoneInfo('UTC')


def _fmt(dt, tz, fmt='%Y-%m-%d %H:%M:%S'):
    if dt is None:
        return 'Never'
    return dt.replace(tzinfo=ZoneInfo('UTC')).astimezone(tz).strftime(fmt)


@dashboard_bp.route('/')
def index():
    pvs = PVMonitor.query.order_by(PVMonitor.pv_name).all()
    rules = CompoundRule.query.order_by(CompoundRule.name).all()
    processes = ProcessMonitor.query.order_by(ProcessMonitor.name).all()

    enabled_statuses = [p.status for p in pvs if p.enabled]
    ok_count = enabled_statuses.count('OK')
    alarm_count = enabled_statuses.count('ALARM')
    disc_count = sum(1 for s in enabled_statuses if s in ('DISCONNECTED', 'ERROR', 'UNKNOWN'))
    disabled_count = (sum(1 for p in pvs if not p.enabled) +
                      sum(1 for r in rules if not r.enabled) +
                      sum(1 for pm in processes if not pm.enabled))

    tz = _display_tz()
    now_local = datetime.now(tz=ZoneInfo('UTC')).astimezone(tz).strftime('%Y-%m-%d %H:%M:%S %Z')

    return render_template(
        'dashboard.html',
        pvs=pvs,
        rules=rules,
        processes=processes,
        ok_count=ok_count,
        alarm_count=alarm_count,
        disconnected_count=disc_count,
        total_count=len(pvs),
        disabled_count=disabled_count,
        last_updated=now_local,
    )


@dashboard_bp.route('/api/status')
def api_status():
    pvs = PVMonitor.query.order_by(PVMonitor.pv_name).all()
    rules = CompoundRule.query.order_by(CompoundRule.name).all()
    processes = ProcessMonitor.query.order_by(ProcessMonitor.name).all()

    tz = _display_tz()

    pv_data = []
    for p in pvs:
        pv_data.append({
            'id': p.id,
            'pv_name': p.pv_name,
            'alias': p.alias or '',
            'current_value': p.current_value_str or ('N/A' if p.current_value is None else str(p.current_value)),
            'status': p.status,
            'enabled': p.enabled,
            'last_checked': _fmt(p.last_checked, tz, '%H:%M:%S %Z'),
            'last_alarm': _fmt(p.last_alarm, tz, '%Y-%m-%d %H:%M:%S'),
        })

    rule_data = []
    for r in rules:
        rule_data.append({
            'id': r.id,
            'name': r.name,
            'logic_op': r.logic_op,
            'status': r.status,
            'enabled': r.enabled,
            'last_checked': _fmt(r.last_checked, tz, '%H:%M:%S %Z'),
        })

    proc_data = []
    for pm in processes:
        proc_data.append({
            'id': pm.id,
            'name': pm.name,
            'match_value': pm.match_value,
            'status': pm.status,
            'enabled': pm.enabled,
            'pid': pm.pid,
            'last_checked': _fmt(pm.last_checked, tz, '%H:%M:%S %Z'),
            'last_stopped': _fmt(pm.last_stopped, tz, '%Y-%m-%d %H:%M:%S'),
        })

    enabled_statuses = [p['status'] for p in pv_data if p['enabled']]
    disabled_count = (sum(1 for p in pv_data if not p['enabled']) +
                      sum(1 for r in rule_data if not r['enabled']) +
                      sum(1 for pm in proc_data if not pm['enabled']))

    now_local = datetime.now(tz=ZoneInfo('UTC')).astimezone(tz).strftime('%Y-%m-%d %H:%M:%S %Z')

    return jsonify({
        'pvs': pv_data,
        'rules': rule_data,
        'processes': proc_data,
        'ok_count': enabled_statuses.count('OK'),
        'alarm_count': enabled_statuses.count('ALARM'),
        'disconnected_count': sum(1 for s in enabled_statuses if s not in ('OK', 'ALARM')),
        'total_count': len(pv_data),
        'disabled_count': disabled_count,
        'last_updated': now_local,
    })
