"""Run commands for soft-IOCs locally or on remote hosts over SSH."""
import logging
import re
import shlex
import subprocess
import time

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


def _procserv_find(port):
    """Shell snippet that sets $pid to the procServ instance listening on `port`."""
    pattern = f'[p]rocServ .* {int(port)}( |$)'   # [p]: don't match this shell's own command line
    return f"pid=$(pgrep -f {shlex.quote(pattern)} | head -1)"


def check_ioc(ioc):
    """Returns (status, detail): status is RUNNING, STOPPED or UNREACHABLE."""
    if ioc.status_command:
        cmd = ioc.status_command
    elif ioc.procserv_port:
        # RUNNING = procServ is up AND its child (the IOC) is alive. With --noautorestart
        # procServ outlives a crashed IOC, so the listening port alone isn't enough.
        cmd = f'{_procserv_find(ioc.procserv_port)}; [ -n "$pid" ] && pgrep -P "$pid" >/dev/null'
    else:
        cmd = f'pgrep -f -- {shlex.quote(_pgrep_pattern(ioc.match_value))}'
    rc, out = run_on_host(ioc, cmd, timeout=10)
    if rc is None or rc == 255 and ioc.host.strip().lower() not in LOCAL_HOSTS:
        return 'UNREACHABLE', (out or 'SSH connection failed')[:500]
    if rc == 0:
        return 'RUNNING', None
    return 'STOPPED', None


def start_ioc(ioc):
    """
    Run the start command detached, wait a few seconds, and report what it printed,
    whether it exited (and with what status), and the IOC's status afterwards.
    Returns (ok, message); ok is False if the command exited non-zero.
    """
    if not ioc.start_command:
        return False, 'No start command configured.'
    if ioc.procserv_port:
        # A crashed IOC leaves its procServ behind, which makes start scripts think it is
        # still running. Clear it first, but only if the IOC child is gone.
        run_on_host(ioc, f'{_procserv_find(ioc.procserv_port)}; '
                         '[ -n "$pid" ] && ! pgrep -P "$pid" >/dev/null && '
                         'kill "$pid" && sleep 1; true', timeout=15)
    inner = shlex.quote(ioc.start_command)
    cmd = (
        'LOG=$(mktemp /tmp/wd-start.XXXXXX); '
        f'nohup sh -c {inner} >"$LOG" 2>&1 </dev/null & pid=$!; '
        'sleep 3; '
        'if kill -0 $pid 2>/dev/null; then echo "[still running after 3 s]"; '
        'else wait $pid; echo "[exited with status $?]"; fi; '
        'tail -n 20 "$LOG"; rm -f "$LOG"'
    )
    rc, out = run_on_host(ioc, cmd, timeout=25)
    if rc is None or rc != 0:
        return False, out or f'Start failed (exit {rc}).'
    m = re.search(r'\[exited with status (\d+)\]', out)
    ok = not (m and m.group(1) != '0')
    time.sleep(2)   # let a daemonising start settle before re-checking
    status, detail = check_ioc(ioc)
    msg = out.replace('\n', ' | ')
    return ok, f'{msg} || status now: {status}' + (f' ({detail})' if detail else '')


def stop_ioc(ioc):
    if ioc.stop_command:
        cmd = ioc.stop_command
    elif ioc.procserv_port:
        # Killing procServ also takes down its IOC child
        cmd = (f'{_procserv_find(ioc.procserv_port)}; '
               'if [ -n "$pid" ]; then kill "$pid" && echo "Stopped procServ (pid $pid)"; '
               'else echo "procServ not running"; exit 1; fi')
    else:
        cmd = f'pkill -f -- {shlex.quote(_pgrep_pattern(ioc.match_value))}'
    rc, out = run_on_host(ioc, cmd, timeout=20)
    return rc == 0, out or ('Stop issued.' if rc == 0 else f'Exit {rc}.')
