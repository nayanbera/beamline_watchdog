"""Run commands for soft-IOCs locally or on remote hosts over SSH."""
import functools
import getpass
import logging
import os
import re
import shlex
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

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


def ssh_env(legacy=0):
    """
    Environment for the system ssh/ssh-keygen. The watchdog runs inside a conda env (and IOC start
    scripts may export LD_LIBRARY_PATH): if /usr/bin/ssh then loads conda's libcrypto/libssl instead of
    the system ones it can fail with "error in libcrypto". So strip the library overrides.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'LD_AUDIT', 'OPENSSL_CONF', 'OPENSSL_MODULES',
                        'PYTHONHOME', 'PYTHONPATH')}
    if legacy_level(legacy):
        # RHEL/Fedora OpenSSL refuses SHA-1 signatures ("invalid digest ... error in libcrypto"), which an
        # old host's ssh-rsa host key needs. This per-process switch re-enables them for this ssh only.
        env['OPENSSL_ENABLE_SHA1_SIGNATURES'] = '1'
    return env


def _wrap(ioc, command):
    if ioc.working_dir:
        command = f'cd {shlex.quote(ioc.working_dir)} && {command}'
    return command


# Extra algorithms for old sshd versions (e.g. only ssh-rsa/ssh-dss host keys, SHA-1 key exchange)
# that current OpenSSH clients refuse by default. "+" appends to the defaults, so modern hosts still
# negotiate the strong algorithms.
@functools.lru_cache(maxsize=1)
def _pubkey_opt():
    """OpenSSH 8.5 renamed PubkeyAcceptedKeyTypes to PubkeyAcceptedAlgorithms (the old name is a
    deprecated alias); use the name this client's version knows."""
    try:
        r = subprocess.run(['ssh', '-V'], capture_output=True, text=True, timeout=5, env=ssh_env())
        m = re.search(r'OpenSSH_(\d+)\.(\d+)', r.stderr + r.stdout)
        if m and (int(m.group(1)), int(m.group(2))) >= (8, 5):
            return 'PubkeyAcceptedAlgorithms'
    except Exception:
        pass
    return 'PubkeyAcceptedKeyTypes'


def _legacy_opts(level):
    """Level 1: allow ssh-rsa (what `ssh -oHostKeyAlgorithms=+ssh-rsa -oPubkeyAcceptedAlgorithms=+ssh-rsa`
    does). Level 2 adds SHA-1 key exchange and CBC ciphers."""
    opts = ['-o', 'HostKeyAlgorithms=+ssh-rsa', '-o', f'{_pubkey_opt()}=+ssh-rsa']
    if level >= 2:
        opts += ['-o', 'KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1',
                 '-o', 'Ciphers=+aes128-cbc,aes256-cbc']
    return opts


def legacy_level(value):
    """0 = modern, 1 = allow ssh-rsa, 2 = also SHA-1 key exchange and CBC ciphers.
    Accepts ints, bools and form values ('', 'on', '1', '2')."""
    if value in (None, '', False, 0, '0'):
        return 0
    return 2 if str(value) == '2' else 1


def _ssh_argv(host, user, port, command, legacy=0):
    argv = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
            '-o', 'StrictHostKeyChecking=accept-new']
    if legacy_level(legacy):
        argv += _legacy_opts(legacy_level(legacy))
    if port:
        argv += ['-p', str(int(port))]
    return argv + [f'{user}@{host}' if user else host, '--', command]


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
        argv = _ssh_argv(ioc.host, ioc.ssh_user, ioc.ssh_port, command,
                         legacy_level(getattr(ioc, 'ssh_legacy', 0)))
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL,
                           env=ssh_env(legacy_level(getattr(ioc, 'ssh_legacy', 0))) if argv[0] == 'ssh' else None)
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


def _status_cmd(ioc):
    if ioc.status_command:
        cmd = ioc.status_command
    elif ioc.procserv_port:
        # RUNNING = procServ is up AND its child (the IOC) is alive. With --noautorestart
        # procServ outlives a crashed IOC, so the listening port alone isn't enough.
        cmd = f'{_procserv_find(ioc.procserv_port)}; [ -n "$pid" ] && pgrep -P "$pid" >/dev/null'
    else:
        cmd = f'pgrep -f -- {shlex.quote(_pgrep_pattern(ioc.match_value))}'
    return cmd


def check_ioc(ioc):
    """Returns (status, detail) for one IOC: RUNNING, STOPPED or UNREACHABLE."""
    rc, out = run_on_host(ioc, _status_cmd(ioc), timeout=10)
    if rc is None or rc == 255 and ioc.host.strip().lower() not in LOCAL_HOSTS:
        return 'UNREACHABLE', (out or 'SSH connection failed')[:500]
    if rc == 0:
        return 'RUNNING', None
    return 'STOPPED', None


@functools.lru_cache(maxsize=1)
def _own_names():
    names = {socket.gethostname().lower(), socket.getfqdn().lower()}
    return names | {n.split('.')[0] for n in names}


def _runs_locally(ioc):
    """True if the IOC's host is this computer and the SSH user (if any) is the watchdog's own user."""
    host = (ioc.host or '').strip().lower()
    if host not in LOCAL_HOSTS and host not in _own_names():
        return False
    try:
        return not ioc.ssh_user or ioc.ssh_user == getpass.getuser()
    except Exception:
        return not ioc.ssh_user


def _check_group(key, iocs):
    """Check all `iocs` of one host with a single local/SSH invocation."""
    script = '; '.join(
        f'sh -c {shlex.quote(_wrap(i, _status_cmd(i)))} >/dev/null 2>&1 </dev/null; echo "WD:{i.id}:$?"'
        for i in iocs)
    command = f'sh -c {shlex.quote(script)}'
    timeout = min(15 + len(iocs), 60)
    if key is None:                                   # this computer: no SSH needed
        argv = ['bash', '-c', command]
    else:
        argv = _ssh_argv(key[0], key[1] or None, key[2] or None, command, key[3])
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL,
                           env=ssh_env(key[3]) if key is not None else None)
        out = (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return {i.id: ('UNREACHABLE', f'Timed out after {timeout} s.') for i in iocs}
    except Exception as exc:
        return {i.id: ('UNREACHABLE', str(exc)[:500]) for i in iocs}
    codes = dict(re.findall(r'^WD:(\d+):(\d+)$', r.stdout, re.M))
    res = {}
    for i in iocs:
        if str(i.id) in codes:
            res[i.id] = ('RUNNING', None) if codes[str(i.id)] == '0' else ('STOPPED', None)
        else:
            res[i.id] = ('UNREACHABLE', (out or 'SSH connection failed')[:500])
    return res


def check_iocs_batch(iocs):
    """
    Status of many soft IOCs with one invocation per host (instead of one SSH login per IOC);
    IOCs on this computer are checked directly, without SSH.
    Returns {ioc.id: (status, detail)}.
    """
    groups, results = {}, {}
    for ioc in iocs:
        if _runs_locally(ioc):
            key = None
        elif valid_host_fields(ioc.host, ioc.ssh_user):
            key = (ioc.host.strip().lower(), ioc.ssh_user or '', ioc.ssh_port or 0,
                   legacy_level(ioc.ssh_legacy))
        else:
            results[ioc.id] = ('UNREACHABLE', 'Invalid host or user.')
            continue
        groups.setdefault(key, []).append(ioc)
    if groups:
        with ThreadPoolExecutor(max_workers=min(8, len(groups))) as pool:
            futures = [(g, pool.submit(_check_group, k, g)) for k, g in groups.items()]
            for g, f in futures:
                try:
                    results.update(f.result())
                except Exception as exc:
                    logger.exception('IOC group check failed')
                    results.update({i.id: ('UNREACHABLE', str(exc)[:500]) for i in g})
    return results


def start_ioc(ioc):
    """
    Run the start command detached, wait a few seconds, and report what it printed,
    whether it exited (and with what status), and the IOC's status afterwards.
    Returns (ok, message); ok is False if the command exited non-zero and the IOC is not RUNNING afterwards.
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
    if not ok and status == 'RUNNING':
        # Some start scripts exit non-zero after the IOC is up (e.g. a final telnet probe of the
        # procServ port that procServ closes). What matters is that the IOC is running.
        ok = True
        msg = f'IOC is running; the start command exited non-zero, which is ignored. | {msg}'
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

def _ssh_target(host, user, port, legacy=0):
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
    return SimpleNamespace(host=host, ssh_user=user, ssh_port=port, working_dir=None,
                           ssh_legacy=legacy_level(legacy)), None


def is_negotiation_error(out):
    o = out or ''
    return 'Unable to negotiate' in o or 'no matching host key type' in o or 'no matching key exchange' in o \
        or 'no matching cipher' in o


def explain_ssh_error(out):
    """Turn raw ssh stderr into a short hint."""
    o = out or ''
    if 'error in libcrypto' in o:
        return ('The system crypto library refused this host\'s algorithms (RHEL blocks SHA-1 and weak '
                'key exchange). See "Old SSH servers" in the README.')
    if is_negotiation_error(o):
        return ('This host runs an old SSH server that needs legacy algorithms '
                '(e.g. ssh-rsa). Tick "Old SSH server" and try again.')
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


def ssh_test(host, user=None, port=None, legacy=0):
    """
    Non-interactive key-login test. Returns (ok, message, legacy_level_needed). If the host
    rejects modern algorithms ("no matching host key type"), it is retried with the minimal
    legacy options (ssh-rsa), then with SHA-1 key exchange / CBC ciphers added.
    """
    level = legacy_level(legacy)
    target, err = _ssh_target(host, user, port, level)
    if err:
        return False, err, 0
    out = ''
    while True:
        rc, out = run_on_host(target, 'true', timeout=12)
        if rc == 0:
            return True, 'Key login works' + (f' (legacy SSH level {level}).' if level else '.'), level
        if not is_negotiation_error(out) or level >= 2:
            break
        level += 1                                  # try again with more legacy algorithms
        target.ssh_legacy = level
    if level > legacy_level(legacy) or (level and is_negotiation_error(out)):
        return False, ('Old SSH server detected. ' + explain_ssh_error(out)), level
    return False, explain_ssh_error(out), level


def ssh_diagnose(host, user=None, port=None, legacy=0):
    """Details for troubleshooting an SSH failure as seen from inside the watchdog process."""
    import getpass
    import shutil
    target, err = _ssh_target(host, user, port, legacy)
    if err:
        return err
    lines = []
    try:
        v = subprocess.run(['ssh', '-V'], capture_output=True, text=True, timeout=5, env=ssh_env())
        lines.append('ssh binary : %s  (%s)' % (shutil.which('ssh'), (v.stderr + v.stdout).strip()))
    except Exception as exc:
        lines.append(f'ssh binary : cannot run ssh ({exc})')
    lines.append('run as     : %s  HOME=%s' % (getpass.getuser(), os.path.expanduser('~')))
    lines.append('SHA-1 sigs : OPENSSL_ENABLE_SHA1_SIGNATURES=1 is set for ssh in legacy mode: %s'
                 % ('yes' if legacy_level(target.ssh_legacy) else 'no'))
    for k in ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'OPENSSL_CONF', 'OPENSSL_ENABLE_SHA1_SIGNATURES', 'PATH'):
        lines.append('%-11s: %s%s' % (k, os.environ.get(k, '(not set)'),
                                      '   [removed for ssh]' if k != 'PATH' and k in os.environ else ''))
    argv = _ssh_argv(target.host, target.ssh_user, target.ssh_port, 'true', target.ssh_legacy)
    argv.insert(1, '-vv')
    lines.append('command    : ' + ' '.join(shlex.quote(a) for a in argv))
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
                           env=ssh_env(target.ssh_legacy))
        tail = (r.stderr + r.stdout).strip().splitlines()[-25:]
        lines.append(f'exit code  : {r.returncode}')
        lines.append('--- last lines of ssh -vv ---')
        lines += tail
    except Exception as exc:
        lines.append(f'ssh -vv failed to run: {exc}')
    return '\n'.join(lines)


def _ssh_dir():
    import os
    return os.path.join(os.path.expanduser('~'), '.ssh')


def ensure_public_key(rsa=False):
    """
    Return (pubkey_text, error) for the watchdog account's SSH key, creating an
    ed25519 key pair if none exists. With rsa=True (old hosts that predate ed25519) the RSA
    key id_rsa is used, and created (3072 bit) if missing.
    """
    import os
    d = _ssh_dir()
    for name in (('id_rsa',) if rsa else ('id_ed25519', 'id_ecdsa', 'id_rsa')):
        pub = os.path.join(d, name + '.pub')
        if os.path.exists(pub) and os.path.exists(os.path.join(d, name)):
            with open(pub) as fh:
                return fh.read().strip(), None
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        key = os.path.join(d, 'id_rsa' if rsa else 'id_ed25519')
        r = subprocess.run(['ssh-keygen', '-q', '-t', 'rsa' if rsa else 'ed25519'] +
                           (['-b', '3072'] if rsa else []) + ['-N', '', '-C', 'beamline-watchdog', '-f', key],
                           capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL, env=ssh_env())
        if r.returncode != 0:
            return None, (r.stderr or 'ssh-keygen failed').strip()
        with open(key + '.pub') as fh:
            return fh.read().strip(), None
    except Exception as exc:
        return None, str(exc)


def _install_once(target, pubkey, password):
    """One password login that appends `pubkey` to authorized_keys. Returns (ok, stderr_or_message)."""
    import os
    import stat
    import tempfile
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
    if target.ssh_legacy:
        argv += _legacy_opts(target.ssh_legacy)
    if target.ssh_port:
        argv += ['-p', str(target.ssh_port)]
    argv += [dest, '--', remote]

    fd, askpass = tempfile.mkstemp(prefix='wd-askpass-')
    try:
        with os.fdopen(fd, 'w') as fh:
            fh.write('#!/bin/sh\nprintf \'%s\\n\' "$WD_SSH_PASS"\n')
        os.chmod(askpass, stat.S_IRWXU)
        env = dict(ssh_env(target.ssh_legacy), SSH_ASKPASS=askpass, SSH_ASKPASS_REQUIRE='force',
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
        return False, r.stderr or ''
    return True, ''


def install_ssh_key(host, user, port, password, legacy=0):
    """
    Install the watchdog's public key into the remote account's authorized_keys using a
    one-time password login, then verify key login. The password is passed to ssh through
    SSH_ASKPASS (environment of the child process only) and is never stored or logged.
    Old hosts that reject modern algorithms are retried with legacy options and an RSA key.
    Returns (ok, message, legacy_used).
    """
    target, err = _ssh_target(host, user, port, legacy)
    if err:
        return False, err, False
    if not password:
        return False, 'Password is required to install the key.', False

    def attempt(leg):
        target.ssh_legacy = leg
        pubkey, kerr = ensure_public_key(rsa=leg)
        if kerr:
            return False, f'Could not get/create the watchdog SSH key: {kerr}'
        return _install_once(target, pubkey, password)

    used = legacy_level(legacy)
    ok, out = attempt(used)
    while not ok and is_negotiation_error(out) and used < 2:
        used += 1
        ok, out = attempt(used)
    if not ok:
        msg = explain_ssh_error(out)
        if 'Permission denied' in (out or ''):
            msg = 'Password login was refused (wrong password, or the account does not allow password SSH).'
        return False, msg, used
    vok, vmsg, vleg = ssh_test(target.host, target.ssh_user, target.ssh_port, used)
    return vok, ('Key installed and verified — key login works'
                 + (' (old host: legacy algorithms + RSA key).' if used else '.') if vok
                 else f'Key installed, but the verification login failed: {vmsg}'), max(used, vleg or 0)
