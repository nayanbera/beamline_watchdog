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


# ---------------------------------------------------------------------------
# SSH key-login setup (used by the Add-IOC screen)
# ---------------------------------------------------------------------------

def _ssh_target(host, user, port):
    """Validate and return a SimpleNamespace usable with run_on_host, or (None, error)."""
    from types import SimpleNamespace
    host = (host or '').strip()
    user = (user or '').strip() or None
    if host.lower() in LOCAL_HOSTS:
        return None, 'Host is the local machine — no SSH is needed.'
    if not valid_host_fields(host, user):
        return None, 'Invalid host or SSH user.'
    try:
        port = int(port) if port not in (None, '') else None
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        return None, 'Invalid SSH port.'
    return SimpleNamespace(host=host, ssh_user=user, ssh_port=port, working_dir=None), None


def explain_ssh_error(out):
    """Turn raw ssh stderr into a short hint."""
    o = out or ''
    if 'Host key verification failed' in o:
        return 'Host key verification failed (the host key changed or is untrusted).'
    if 'Permission denied' in o:
        return 'Key login is not set up yet (permission denied).'
    if 'Could not resolve hostname' in o:
        return 'Host name could not be resolved.'
    if 'Connection refused' in o:
        return 'Connection refused — is sshd running and the port right?'
    if 'timed out' in o.lower() or 'No route to host' in o:
        return 'Host is unreachable (timed out / no route).'
    return o[:300] or 'SSH failed.'


def ssh_test(host, user=None, port=None):
    """Non-interactive key-login test. Returns (ok, message)."""
    target, err = _ssh_target(host, user, port)
    if err:
        return False, err
    rc, out = run_on_host(target, 'true', timeout=12)
    if rc == 0:
        return True, 'Key login works.'
    return False, explain_ssh_error(out)


def _ssh_dir():
    import os
    return os.path.join(os.path.expanduser('~'), '.ssh')


def ensure_public_key():
    """
    Return (pubkey_text, error) for the watchdog account's SSH key, creating an
    ed25519 key pair if none exists.
    """
    import os
    d = _ssh_dir()
    for name in ('id_ed25519', 'id_ecdsa', 'id_rsa'):
        pub = os.path.join(d, name + '.pub')
        if os.path.exists(pub) and os.path.exists(os.path.join(d, name)):
            with open(pub) as fh:
                return fh.read().strip(), None
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        key = os.path.join(d, 'id_ed25519')
        r = subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C',
                            'beamline-watchdog', '-f', key],
                           capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            return None, (r.stderr or 'ssh-keygen failed').strip()
        with open(key + '.pub') as fh:
            return fh.read().strip(), None
    except Exception as exc:
        return None, str(exc)


def install_ssh_key(host, user, port, password):
    """
    Install the watchdog's public key into the remote account's authorized_keys using a
    one-time password login, then verify key login. The password is passed to ssh through
    SSH_ASKPASS (environment of the child process only) and is never stored or logged.
    Returns (ok, message).
    """
    import os
    import stat
    import tempfile
    target, err = _ssh_target(host, user, port)
    if err:
        return False, err
    if not password:
        return False, 'Password is required to install the key.'
    pubkey, err = ensure_public_key()
    if err:
        return False, f'Could not get/create the watchdog SSH key: {err}'

    q = shlex.quote(pubkey)
    remote = ('umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && '
              f'(grep -qxF {q} ~/.ssh/authorized_keys || echo {q} >> ~/.ssh/authorized_keys) && '
              'chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys && '
              '(command -v restorecon >/dev/null 2>&1 && restorecon -R ~/.ssh || true) && '
              'echo KEY_INSTALLED')
    dest = f'{target.ssh_user}@{target.host}' if target.ssh_user else target.host
    argv = ['ssh', '-o', 'BatchMode=no', '-o', 'PubkeyAuthentication=no',
            '-o', 'PreferredAuthentications=password,keyboard-interactive',
            '-o', 'NumberOfPasswordPrompts=1', '-o', 'ConnectTimeout=8',
            '-o', 'StrictHostKeyChecking=accept-new']
    if target.ssh_port:
        argv += ['-p', str(target.ssh_port)]
    argv += [dest, '--', remote]

    fd, askpass = tempfile.mkstemp(prefix='wd-askpass-')
    try:
        with os.fdopen(fd, 'w') as fh:
            fh.write('#!/bin/sh\nprintf \'%s\\n\' "$WD_SSH_PASS"\n')
        os.chmod(askpass, stat.S_IRWXU)
        env = dict(os.environ, SSH_ASKPASS=askpass, SSH_ASKPASS_REQUIRE='force',
                   DISPLAY=os.environ.get('DISPLAY') or ':0', WD_SSH_PASS=password)
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=25, env=env,
                               stdin=subprocess.DEVNULL, start_new_session=True)
        except subprocess.TimeoutExpired:
            return False, 'Timed out while connecting.'
        except Exception as exc:
            return False, str(exc)
        finally:
            env.pop('WD_SSH_PASS', None)
    finally:
        try:
            os.unlink(askpass)
        except OSError:
            pass

    if r.returncode != 0 or 'KEY_INSTALLED' not in r.stdout:
        msg = explain_ssh_error(r.stderr)
        if 'Permission denied' in (r.stderr or ''):
            msg = 'Password login was refused (wrong password, or the account does not allow password SSH).'
        return False, msg
    ok, msg = ssh_test(target.host, target.ssh_user, target.ssh_port)
    return ok, ('Key installed and verified — key login works.' if ok
                else f'Key installed, but the verification login failed: {msg}')
