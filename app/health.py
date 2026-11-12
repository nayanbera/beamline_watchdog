"""Health metrics of the computer the watchdog runs on (load, memory, swap, disks, systemd, clock)."""
import logging
import os
import shutil
import subprocess

import psutil

logger = logging.getLogger(__name__)

# key -> (label, default warn, default crit); disk rows are created per mount point
DEFAULTS = {
    'load':         ('Load per core (5 min)', 1.0, 2.0),
    'memory':       ('Memory used', 85.0, 95.0),
    'swap':         ('Swap used', 50.0, 80.0),
    'failed_units': ('Failed systemd units', 1.0, None),
    'time_sync':    ('Clock not synchronised', 1.0, None),
}
DISK_DEFAULTS = (85.0, 95.0)


def defaults_for(key):
    if key.startswith('disk:'):
        return (f'Disk {key[5:]}',) + DISK_DEFAULTS
    return DEFAULTS[key]


def level(value, warn, crit):
    if value is None:
        return 'UNKNOWN'
    if crit is not None and value >= crit:
        return 'CRIT'
    if warn is not None and value >= warn:
        return 'WARN'
    return 'OK'


def _gib(n):
    return f'{n / 1024 ** 3:.1f} GiB'


def _run(argv):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout
    except Exception:
        return None, ''


def _load():
    l1, l5, l15 = os.getloadavg()
    n = psutil.cpu_count() or 1
    return {'key': 'load', 'value': l5 / n,
            'text': f'{l5 / n:.2f}', 'detail': f'load {l1:.2f} / {l5:.2f} / {l15:.2f} on {n} cores'}


def _memory():
    vm = psutil.virtual_memory()
    return {'key': 'memory', 'value': vm.percent, 'text': f'{vm.percent:.0f}%',
            'detail': f'{_gib(vm.total - vm.available)} used of {_gib(vm.total)} ({_gib(vm.available)} available)'}


def _swap():
    sm = psutil.swap_memory()
    if not sm.total:
        return {'key': 'swap', 'value': 0.0, 'text': '0%', 'detail': 'no swap configured'}
    return {'key': 'swap', 'value': sm.percent, 'text': f'{sm.percent:.0f}%',
            'detail': f'{_gib(sm.used)} used of {_gib(sm.total)}'}


def _disks():
    out, seen = [], set()
    for p in psutil.disk_partitions(all=False):
        if p.device in seen or p.mountpoint.startswith(('/snap', '/var/lib/docker', '/run', '/System/Volumes', '/Volumes')):
            continue
        seen.add(p.device)
        try:
            u = shutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        pct = u.used / (u.used + u.free) * 100 if (u.used + u.free) else 0.0   # like df: used/(used+avail)
        out.append({'key': f'disk:{p.mountpoint}', 'value': pct, 'text': f'{pct:.0f}%',
                    'detail': f'{_gib(u.used)} used of {_gib(u.total)}, {_gib(u.free)} free ({p.device})'})
    return out


def _failed_units(ignore):
    if not shutil.which('systemctl'):
        return None
    rc, out = _run(['systemctl', '--failed', '--no-legend', '--plain', '--no-pager'])
    if rc is None:
        return None
    units = [ln.lstrip('●* ').split()[0] for ln in out.splitlines() if ln.lstrip('●* ').strip()]
    ignored = [u for u in units if u in ignore]
    units = [u for u in units if u not in ignore]
    detail = ', '.join(units) if units else 'none'
    if ignored:
        detail += f'  (ignoring: {", ".join(ignored)})'
    return {'key': 'failed_units', 'value': float(len(units)), 'text': str(len(units)), 'detail': detail}


def _time_sync():
    if not shutil.which('timedatectl'):
        return None
    rc, out = _run(['timedatectl', 'show', '-p', 'NTPSynchronized', '--value'])
    out = out.strip().lower()
    if rc != 0 or out not in ('yes', 'no'):
        return None
    synced = out == 'yes'
    return {'key': 'time_sync', 'value': 0.0 if synced else 1.0,
            'text': 'synchronised' if synced else 'NOT synchronised', 'detail': f'NTPSynchronized={out}'}


def collect(ignore_units=()):
    """List of readings: {key, value (float), text, detail}. Each metric failing alone is skipped."""
    readings = []
    for fn in (_load, _memory, _swap, _disks, lambda: _failed_units(set(ignore_units)), _time_sync):
        try:
            r = fn()
        except Exception:
            logger.exception('health metric failed')
            continue
        if r:
            readings.extend(r if isinstance(r, list) else [r])
    return readings
