from datetime import datetime
from flask import Blueprint, render_template, jsonify
from ..models import PVMonitor, CompoundRule, ProcessMonitor

dashboard_bp = Blueprint('dashboard', __name__)


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
        last_updated=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC'),
    )


@dashboard_bp.route('/api/status')
def api_status():
    pvs = PVMonitor.query.order_by(PVMonitor.pv_name).all()
    rules = CompoundRule.query.order_by(CompoundRule.name).all()
    processes = ProcessMonitor.query.order_by(ProcessMonitor.name).all()

    pv_data = []
    for p in pvs:
        pv_data.append({
            'id': p.id,
            'pv_name': p.pv_name,
            'alias': p.alias or '',
            'current_value': p.current_value_str or ('N/A' if p.current_value is None else str(p.current_value)),
            'status': p.status,
            'enabled': p.enabled,
            'last_checked': p.last_checked.strftime('%H:%M:%S UTC') if p.last_checked else 'Never',
            'last_alarm': p.last_alarm.strftime('%Y-%m-%d %H:%M:%S') if p.last_alarm else 'Never',
        })

    rule_data = []
    for r in rules:
        rule_data.append({
            'id': r.id,
            'name': r.name,
            'logic_op': r.logic_op,
            'status': r.status,
            'enabled': r.enabled,
            'last_checked': r.last_checked.strftime('%H:%M:%S UTC') if r.last_checked else 'Never',
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
            'last_checked': pm.last_checked.strftime('%H:%M:%S UTC') if pm.last_checked else 'Never',
            'last_stopped': pm.last_stopped.strftime('%Y-%m-%d %H:%M:%S') if pm.last_stopped else 'Never',
        })

    enabled_statuses = [p['status'] for p in pv_data if p['enabled']]
    disabled_count = (sum(1 for p in pv_data if not p['enabled']) +
                      sum(1 for r in rule_data if not r['enabled']) +
                      sum(1 for pm in proc_data if not pm['enabled']))
    return jsonify({
        'pvs': pv_data,
        'rules': rule_data,
        'processes': proc_data,
        'ok_count': enabled_statuses.count('OK'),
        'alarm_count': enabled_statuses.count('ALARM'),
        'disconnected_count': sum(1 for s in enabled_statuses if s not in ('OK', 'ALARM')),
        'total_count': len(pv_data),
        'disabled_count': disabled_count,
        'last_updated': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC'),
    })
