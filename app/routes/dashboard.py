import json as _json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from flask import Blueprint, render_template, jsonify, request
from ..models import PVMonitor, CompoundRule, ProcessMonitor

logger = logging.getLogger(__name__)

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


def _group_by_category(items):
    """Return list of (category_name, items) sorted alphabetically, Uncategorized last."""
    groups = defaultdict(list)
    for item in items:
        key = item.category.name if item.category else 'Uncategorized'
        groups[key].append(item)
    result = sorted([(k, v) for k, v in groups.items() if k != 'Uncategorized'],
                    key=lambda x: x[0].lower())
    if 'Uncategorized' in groups:
        result.append(('Uncategorized', groups['Uncategorized']))
    return result


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

    from ..models import SystemConfig
    aa_row = SystemConfig.query.filter_by(key='archiver_url').first()
    archiver_configured = bool(aa_row and aa_row.value and aa_row.value.strip())

    return render_template(
        'dashboard.html',
        pv_groups=_group_by_category(pvs),
        proc_groups=_group_by_category(processes),
        rules=rules,
        ok_count=ok_count,
        alarm_count=alarm_count,
        disconnected_count=disc_count,
        total_count=len(pvs),
        disabled_count=disabled_count,
        last_updated=now_local,
        archiver_configured=archiver_configured,
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
            'category': p.category.name if p.category else '',
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
            'category': pm.category.name if pm.category else '',
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


@dashboard_bp.route('/api/pv-history')
def pv_history():
    """Proxy to Archiver Appliance getData.json — avoids browser CORS issues."""
    from ..models import SystemConfig

    pv_name = request.args.get('pv', '').strip()
    from_time = request.args.get('from', '').strip()
    to_time = request.args.get('to', '').strip()
    count = request.args.get('count', '').strip()

    if not pv_name:
        return jsonify({'error': 'pv parameter required'}), 400

    # Only serve data for PVs that are actually monitored
    if not PVMonitor.query.filter_by(pv_name=pv_name).first():
        return jsonify({'error': 'PV not in monitor list'}), 404

    row = SystemConfig.query.filter_by(key='archiver_url').first()
    aa_url = (row.value or '').strip() if row else ''
    if not aa_url:
        return jsonify({'error': 'Archiver Appliance URL not configured — go to Admin → Settings'}), 503

    aa_params = {'pv': pv_name, 'from': from_time, 'to': to_time, 'fetchLatestMetadata': 'true'}
    if count and count.isdigit() and int(count) >= 10:
        aa_params['operator'] = f'mean_{int(count)}'
    url = f"{aa_url.rstrip('/')}/retrieval/data/getData.json?{urllib.parse.urlencode(aa_params)}"

    try:
        req = urllib.request.Request(url, headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = _json.loads(resp.read())
        return jsonify(data)
    except urllib.error.URLError as e:
        reason = getattr(e, 'reason', str(e))
        logger.warning('AA fetch failed for %s: %s', pv_name, reason)
        return jsonify({'error': f'Could not reach Archiver Appliance: {reason}'}), 502
    except Exception as e:
        logger.exception('AA fetch error for %s', pv_name)
        return jsonify({'error': str(e)}), 500
