"""Run commands for soft-IOCs locally or on remote hosts over SSH."""
import logging
import re
import shlex
import subprocess

logger = logging.getLogger(__name__)

LOCAL_HOSTS = ('', 'localhost', '127.0.0.1', '::1')
_SAFE_TOKEN = re.compile(r'^[A-Za-z0-9_.:\-\[\]]+$')


def valid_host_fields(host, user):
    """Reject values that ssh could interpret as options or extra arguments."""
    if not host or not _SAFE_TOKEN.match(host) or host.startswith('-'):
        return False
    if user and (not _SAFE_TOKEN.match(user) or user.startswith('-')):
        return False
    return True


def _wrap(ioc, command):
    if ioc.working_dir:
        command = f'cd {shlex.quote(ioc.working_dir)} && {command}'
    return command


def run_on_host(ioc, command, timeout=10):
    """
    Run a shell command for `ioc` on its host.
    Returns (returncode, output). returncode is None on timeout/launch failure,
    and 255 for SSH connection failures.
    """
    command = _wrap(ioc, command)
    if (ioc.host or '').strip().lower() in LOCAL_HOSTS:
        argv = ['bash', '-c', command]
    else:
        if not valid_host_fields(ioc.host, ioc.ssh_user):
            return None, 'Invalid host or user.'
        argv = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                '-o', 'StrictHostKeyChecking=accept-new']
        if ioc.ssh_port:
            argv += ['-p', str(int(ioc.ssh_port))]
        dest = f'{ioc.ssh_user}@{ioc.host}' if ioc.ssh_user else ioc.host
        argv += [dest, '--', command]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return None, f'Timed out after {timeout} s.'
    except Exception as exc:
        return None, str(exc)


def _pgrep_pattern(value):
    """Bracket the first character so pgrep -f doesn't match its own shell command line."""
    value = value.strip()
    if value and re.match(r'[A-Za-z0-9/_]', value[0]):
        return f'[{value[0]}]{value[1:]}'
    return value


def check_ioc(ioc):
    """Returns (status, detail): status is RUNNING, STOPPED or UNREACHABLE."""
    cmd = ioc.status_command or f'pgrep -f -- {shlex.quote(_pgrep_pattern(ioc.match_value))}'
    rc, out = run_on_host(ioc, cmd, timeout=10)
    if rc is None or rc == 255 and ioc.host.strip().lower() not in LOCAL_HOSTS:
        return 'UNREACHABLE', (out or 'SSH connection failed')[:500]
    if rc == 0:
        return 'RUNNING', None
    return 'STOPPED', None


def start_ioc(ioc):
    if not ioc.start_command:
        return False, 'No start command configured.'
    # Detach so the command (and the SSH session) returns immediately
    cmd = (f'nohup sh -c {shlex.quote(ioc.start_command)} '
           f'>/dev/null 2>&1 </dev/null & echo started')
    rc, out = run_on_host(ioc, cmd, timeout=20)
    return rc == 0, out or ('Start issued.' if rc == 0 else f'Exit {rc}.')


def stop_ioc(ioc):
    cmd = ioc.stop_command or f'pkill -f -- {shlex.quote(_pgrep_pattern(ioc.match_value))}'
    rc, out = run_on_host(ioc, cmd, timeout=20)
    return rc == 0, out or ('Stop issued.' if rc == 0 else f'Exit {rc}.')
