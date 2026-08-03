from datetime import datetime
from flask import Blueprint, render_template, jsonify
from ..models import PVMonitor, CompoundRule, SystemConfig

dashboard_bp = Blueprint('dashboard', __name__)


def _site_name():
    row = SystemConfig.query.filter_by(key='site_name').first()
    return row.value if row else 'EPICS PV Watchdog'


@dashboard_bp.route('/')
def index():
    pvs = PVMonitor.query.filter_by(enabled=True).order_by(PVMonitor.pv_name).all()
    rules = CompoundRule.query.filter_by(enabled=True).order_by(CompoundRule.name).all()

    statuses = [p.status for p in pvs]
    ok_count = statuses.count('OK')
    alarm_count = statuses.count('ALARM')
    disc_count = sum(1 for s in statuses if s in ('DISCONNECTED', 'ERROR', 'UNKNOWN'))

    return render_template(
        'dashboard.html',
        pvs=pvs,
        rules=rules,
        ok_count=ok_count,
        alarm_count=alarm_count,
        disconnected_count=disc_count,
        total_count=len(pvs),
        last_updated=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC'),
        site_name=_site_name(),
    )


@dashboard_bp.route('/api/status')
def api_status():
    pvs = PVMonitor.query.filter_by(enabled=True).all()
    rules = CompoundRule.query.filter_by(enabled=True).all()

    pv_data = []
    for p in pvs:
        pv_data.append({
            'id': p.id,
            'pv_name': p.pv_name,
            'alias': p.alias or '',
            'current_value': p.current_value_str or ('N/A' if p.current_value is None else str(p.current_value)),
            'status': p.status,
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
            'last_checked': r.last_checked.strftime('%H:%M:%S UTC') if r.last_checked else 'Never',
        })

    statuses = [p['status'] for p in pv_data]
    return jsonify({
        'pvs': pv_data,
        'rules': rule_data,
        'ok_count': statuses.count('OK'),
        'alarm_count': statuses.count('ALARM'),
        'disconnected_count': sum(1 for s in statuses if s not in ('OK', 'ALARM')),
        'total_count': len(pv_data),
        'last_updated': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC'),
    })
