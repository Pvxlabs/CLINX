"""Legacy qualification API for the standalone Rust Tunnel supervisor.

Normal CLI/service execution replaces this process with clinx-tunnel. The object
API supplies only injectable OS/clock IO to that same Rust lifecycle engine.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

_rust_run, _rust_popen, _PIPE = subprocess.run, subprocess.Popen, subprocess.PIPE


def _binary():
    explicit = os.environ.get('CLINX_TUNNEL_BINARY')
    node = os.environ.get('CLINX_NODE_BINARY')
    choices = [explicit, str(Path(node).with_name('clinx-tunnel')) if node else None,
               str(Path(__file__).resolve().with_name('clinx-tunnel')), shutil.which('clinx-tunnel')]
    for value in choices:
        if value and Path(value).is_absolute() and Path(value).is_file():
            return value
    raise ValueError('RUST_TUNNEL_BINARY_REQUIRED')


def _command(operation, **arguments):
    reply = json.loads(_rust_run([_binary(), '--rpc'], input=json.dumps(dict(
        operation='tunnel.' + operation, arguments=arguments), allow_nan=False) + '\n',
        capture_output=True, text=True, timeout=10, check=True).stdout)
    if 'error' in reply:
        raise ValueError(reply['error'])
    return reply['result']


def poll_sample(metrics, *, now, max_age):
    return _command('poll', metrics=metrics, now=now, max_age=max_age)


def process_start(pid):
    return _command('process_start', pid=pid)


def process_group_live(pgid):
    return _command('group_live', pgid=pgid)


def read_status(state_dir):
    return _command('status', directory=str(state_dir), now=time.time())


def probe(url_file, max_age, timeout=3):
    result = _rust_run([_binary(), 'probe', '--url-file', str(url_file), '--max-age', str(max_age),
                        '--timeout', str(timeout)], capture_output=True, text=True, timeout=timeout + 2)
    value = json.loads(result.stdout)
    if result.returncode:
        raise ValueError(value.get('error', 'PROBE_UNAVAILABLE'))
    return value


@dataclass(frozen=True)
class Policy:
    stale_after: float = 120
    startup_grace: float = 45
    interval: float = 5
    probe_timeout: float = 3
    stop_timeout: float = 5
    cooldown: float = 10
    failure_grace: float = 15
    max_restarts: int = 3
    stable_for: float = 300

    def __post_init__(self):
        _command('policy', policy=asdict(self))


class ChildStopUnconfirmed(RuntimeError):
    pass


class Supervisor:
    def __init__(self, state_dir, command, policy=Policy()):
        self.directory, self.command, self.policy = Path(state_dir).absolute(), list(command), policy
        if not command or not Path(command[0]).is_absolute():
            raise ValueError('ABSOLUTE_TUNNEL_COMMAND_REQUIRED')
        self.child, self.lock_fd, self.stop_unconfirmed = None, None, False
        self.stop, self.state = threading.Event(), {}
        self.url_file = self.directory / 'tunnel.url'
        self.identity = hashlib.sha256(json.dumps([self.command, asdict(policy)], sort_keys=True).encode()).hexdigest()
        self.process_start = process_start(os.getpid())

    def _lock(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if self.directory.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('PRIVATE_STATE_DIRECTORY_REQUIRED')
        self.lock_fd = os.open(self.directory / 'supervisor.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def acquire(self):
        self._lock()
        self.state = _command('acquire', directory=str(self.directory), identity=self.identity)

    def record(self, status, reason, **fields):
        result = _command('record', directory=str(self.directory), state=self.state, status=status,
                          reason=reason, fields=fields, updated_at=time.time(), policy=asdict(self.policy),
                          supervisor_pid=os.getpid(), supervisor_start=self.process_start)
        self.state = result['state']
        if result['changed']:
            self._event()

    def _event(self):
        print(json.dumps(dict(self.state, event='TUNNEL_RELIABILITY', task_replay=False,
                              execution_authority_changed=False, dispatch_verified=False)), flush=True)

    def sample(self):
        try:
            return probe(self.url_file, self.policy.stale_after, self.policy.probe_timeout)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return dict(fresh=False, last_poll=None, poll_age=None,
                        probe_error='PROBE_DEADLINE' if str(exc) == 'PROBE_DEADLINE' else 'PROBE_UNAVAILABLE')

    def _io(self, operation, args):
        if operation == 'monotonic': return time.monotonic()
        if operation == 'time': return time.time()
        if operation == 'lock': return self._lock()
        if operation == 'unlock':
            if self.lock_fd is not None:
                os.close(self.lock_fd)
                self.lock_fd = None
        elif operation == 'event': self._event()
        elif operation == 'stopped': return self.stop.is_set()
        elif operation == 'wait': return self.stop.wait(args)
        elif operation == 'sleep': time.sleep(args)
        elif operation == 'sample': return self.sample()
        elif operation == 'poll': return self.child.poll() is not None
        elif operation == 'spawn':
            try:
                self.child = subprocess.Popen(args['command'] + ['--health.url-file', args['url_file']], start_new_session=True)
                return self.child.pid
            except OSError:
                return None
        elif operation == 'signal':
            try: os.killpg(args['pid'], args['signal'])
            except ProcessLookupError: pass
        elif operation == 'child_wait':
            try: self.child.wait(timeout=args['timeout']); return True
            except subprocess.TimeoutExpired: return False
        elif operation == 'group_live': return process_group_live(args['pid'])
        elif operation == 'child_clear': self.child = None
        elif operation == 'terminate_override':
            if 'terminate_child' in self.__dict__:
                self.terminate_child()
                return dict(child_pid=self.child.pid if self.child else None)

    def _lifecycle(self, operation):
        config = dict(operation=operation, directory=str(self.directory), command=self.command,
                      policy=asdict(self.policy), state=self.state, identity=self.identity,
                      supervisor_pid=os.getpid(), supervisor_start=self.process_start,
                      child_pid=self.child.pid if self.child else None, stop_unconfirmed=self.stop_unconfirmed)
        config.update({key: getattr(self, key, None) for key in
                       ('launched_mono', 'launched_wall', 'healthy_since', 'healthy_start_poll')})
        with _rust_popen([_binary(), '--compat'], stdin=_PIPE, stdout=_PIPE,
                         text=True, bufsize=1) as engine:
            engine.stdin.write(json.dumps(config) + '\n'); engine.stdin.flush()
            while True:
                line = engine.stdout.readline()
                if not line: raise ValueError('RUST_TUNNEL_UNAVAILABLE')
                reply = json.loads(line)
                for key, value in reply['snapshot'].items():
                    if key != 'child_pid': setattr(self, key, value)
                if reply['kind'] == 'result':
                    if 'error' in reply:
                        if reply['error'] == 'CHILD_TERMINATION_UNCONFIRMED': raise ChildStopUnconfirmed()
                        raise ValueError(reply['error'])
                    return reply['result']
                try:
                    response = dict(result=self._io(reply['operation'], reply['arguments']))
                except (OSError, ValueError, IndexError):
                    response = dict(error='CALLBACK_IO_UNAVAILABLE')
                engine.stdin.write(json.dumps(response) + '\n'); engine.stdin.flush()

    def launch(self): return self._lifecycle('launch')
    def terminate_child(self): return self._lifecycle('terminate')
    def run(self): return self._lifecycle('run')


def main():
    os.execv(_binary(), [_binary(), *sys.argv[1:]])


if __name__ == '__main__':
    main()
