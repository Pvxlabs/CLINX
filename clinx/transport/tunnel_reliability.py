"""Process/HTTP compatibility IO for the Rust Tunnel recovery policy and receipts.

No task DB, Provider connection, systemctl call, RPC retry or payload journal lives here.
This is an opt-in launcher; importing it or running the existing health check is read-only.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import shutil
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path


# A standalone release contains the exact qualified Node binary beside this file.
# Capture process IO before compatibility tests inject an owned-child factory.
_rust_run = subprocess.run


def _command(operation, **arguments):
    local = Path(__file__).resolve().with_name('clinx-node')
    binary = os.environ.get('CLINX_NODE_BINARY') or (str(local) if local.is_file() else shutil.which('clinx-node'))
    if not binary or not Path(binary).is_absolute():
        raise ValueError('RUST_TUNNEL_BINARY_REQUIRED')
    request = dict(protocol='CLINX_REQUEST_PIPE_V1', mode='network', operation='tunnel.' + operation, arguments=arguments)
    result = _rust_run([binary], input=json.dumps(request, allow_nan=False) + '\n',
                       capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise ValueError('RUST_TUNNEL_UNAVAILABLE')
    reply = json.loads(result.stdout)
    if reply.get('kind') != 'result':
        raise ValueError('RUST_TUNNEL_INVALID_RESPONSE')
    if 'error' in reply:
        raise ValueError(reply['error']['code'])
    return reply['result']


def poll_sample(metrics, *, now, max_age):
    return _command('poll', metrics=metrics, now=now, max_age=max_age)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("HEALTH_REDIRECT_REFUSED")


def process_start(pid):
    return _command('process_start', pid=pid)


def process_group_live(pgid):
    return _command('group_live', pgid=pgid)


def read_status(state_dir):
    return _command('status', directory=str(state_dir), now=time.time())


def probe(url_file, max_age):
    # This helper is run in a disposable process with a parent-enforced total deadline,
    # so half-open sockets and trickling HTTP bodies cannot block the supervisor.
    url = Path(url_file).read_text().strip()
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or not parsed.port
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("HEALTH_URL_NOT_LOOPBACK")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(url.rstrip("/") + "/metrics", timeout=2) as response:
        raw = response.read(1048577)
    if len(raw) > 1048576:
        raise ValueError("METRICS_TOO_LARGE")
    return poll_sample(raw.decode(), now=time.time(), max_age=max_age)


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
        for name, value in asdict(self).items():
            if not math.isfinite(value) or value <= 0:
                raise ValueError("INVALID_POLICY_" + name)
        if type(self.max_restarts) is not int:
            raise ValueError("INVALID_RESTART_LIMIT")


class ChildStopUnconfirmed(RuntimeError):
    pass


class Supervisor:
    def __init__(self, state_dir, command, policy=Policy()):
        self.directory = Path(state_dir).absolute()
        self.command, self.policy = list(command), policy
        if not command or not Path(command[0]).is_absolute():
            raise ValueError("ABSOLUTE_TUNNEL_COMMAND_REQUIRED")
        self.child = None
        self.stop_unconfirmed = False
        self.stop = threading.Event()
        self.state = {}
        self.lock_fd = None
        self.url_file = self.directory / "tunnel.url"
        self.identity = hashlib.sha256(
            json.dumps([self.command, asdict(policy)], sort_keys=True).encode()
        ).hexdigest()
        self.process_start = process_start(os.getpid())

    def acquire(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if (
            self.directory.is_symlink()
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("PRIVATE_STATE_DIRECTORY_REQUIRED")
        self.lock_fd = os.open(
            self.directory / "supervisor.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.state = _command('acquire', directory=str(self.directory), identity=self.identity)

    def record(self, status, reason, **fields):
        result = _command('record', directory=str(self.directory), state=self.state,
                          status=status, reason=reason, updated_at=time.time(),
                          supervisor_pid=os.getpid(), supervisor_start=self.process_start,
                          policy=asdict(self.policy), fields=fields)
        self.state = result['state']
        if result['changed']:
            print(json.dumps(dict(self.state, event='TUNNEL_RELIABILITY', task_replay=False,
                                  execution_authority_changed=False, dispatch_verified=False),
                             allow_nan=False), flush=True)

    def terminate_child(self):
        if self.child is None:
            return
        # A new session per launch limits signals to this exact owned process tree.
        # Also drain descendants when the group leader has already exited.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            deadline = time.monotonic() + self.policy.stop_timeout
            try:
                os.killpg(self.child.pid, sig)
            except ProcessLookupError:
                pass
            try:
                self.child.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                continue
            try:
                while process_group_live(self.child.pid):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(0.02, remaining))
                else:
                    self.child = None
                    return
            except (OSError, ValueError, IndexError):
                # Cannot prove the group drained; never launch another consumer.
                break
        self.stop_unconfirmed = True
        self.record(
            "HOLD", "CHILD_TERMINATION_UNCONFIRMED", child_pid=self.child.pid
        )
        raise ChildStopUnconfirmed()

    def launch(self):
        result = _command('launch', state=self.state, policy=asdict(self.policy))
        self.state = result['state']
        if not result['allowed']:
            self.record('HOLD', 'RECOVERY_BUDGET_EXHAUSTED', child_pid=None)
            return False
        # Consume budget durably BEFORE spawn. External supervisor restarts do not reset it.
        self.record(
            "STARTING",
            "LAUNCH_INTENT",
            child_pid=None,
            child_start=None,
            last_poll=None,
            poll_age=None,
            poll_errors={},
            probe_error=None,
            probe_seconds=None,
            fresh=False,
            recovery_seconds=None,
        )
        self.url_file.unlink(missing_ok=True)
        self.launched_mono, self.launched_wall = time.monotonic(), time.time()
        self.healthy_since = None
        self.healthy_start_poll = None
        try:
            self.child = subprocess.Popen(
                self.command + ["--health.url-file", str(self.url_file)],
                start_new_session=True,
            )
        except OSError:
            self.record("DEGRADED", "SPAWN_FAILED", child_pid=None)
            return False
        self.record(
            "STARTING",
            "AWAITING_NEW_POLL",
            child_pid=self.child.pid,
            child_start=process_start(self.child.pid),
        )
        return True

    def sample(self):
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "probe",
                    "--url-file",
                    str(self.url_file),
                    "--max-age",
                    str(self.policy.stale_after),
                ],
                capture_output=True,
                text=True,
                timeout=self.policy.probe_timeout,
                check=True,
            )
            return json.loads(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return dict(
                fresh=False,
                last_poll=None,
                poll_age=None,
                probe_error="PROBE_DEADLINE"
                if isinstance(exc, subprocess.TimeoutExpired)
                else "PROBE_UNAVAILABLE",
            )

    def run(self):
        try:
            self.acquire()
        except BaseException:
            if self.lock_fd is not None:
                os.close(self.lock_fd)
                self.lock_fd = None
            raise
        failure_started = None
        try:
            while not self.stop.is_set():
                if self.child is None:
                    if not self.launch():
                        if self.state.get("status") == "HOLD":
                            return 78
                        if self.stop.wait(self.policy.cooldown):
                            break
                        continue
                probe_started = time.monotonic()
                exited = self.child.poll() is not None
                sample = (
                    self.sample()
                    if not exited
                    else dict(fresh=False, last_poll=None, poll_age=None)
                )
                now = time.monotonic()
                sample["probe_seconds"] = now - probe_started
                decision = _command('step', policy=asdict(self.policy), state=self.state,
                    timing=dict(healthy_since=self.healthy_since,
                                healthy_start_poll=self.healthy_start_poll,
                                failure_started=failure_started), sample=sample, now=now,
                    launched_wall=self.launched_wall, launched_mono=self.launched_mono, exited=exited)
                self.state = decision['state']
                self.healthy_since = decision['timing']['healthy_since']
                self.healthy_start_poll = decision['timing']['healthy_start_poll']
                failure_started = decision['timing']['failure_started']
                self.record(decision['status'], decision['reason'], **sample)
                if decision['action'] == 'recover':
                    self.terminate_child()
                    self.record('BACKOFF', decision['reason'], child_pid=None)
                    if self.stop.wait(self.policy.cooldown):
                        break
                    continue
                self.stop.wait(self.policy.interval)
            self.record("STOPPED", "SUPERVISOR_STOP_REQUESTED")
            return 0
        finally:
            try:
                if not self.stop_unconfirmed:
                    self.terminate_child()
                    self.record(
                        self.state.get("status", "STOPPED"),
                        self.state.get("reason", "SUPERVISOR_STOPPED"),
                        child_pid=None,
                    )
            finally:
                os.close(self.lock_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--url-file", required=True)
    p.add_argument("--max-age", type=float, required=True)
    p = sub.add_parser("status")
    p.add_argument("--state-dir", required=True)
    p = sub.add_parser("run")
    p.add_argument("--state-dir", required=True)
    for name, default in asdict(Policy()).items():
        p.add_argument(
            "--" + name.replace("_", "-"),
            type=int if name == "max_restarts" else float,
            default=default,
        )
    p.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.mode == "probe":
        print(json.dumps(probe(args.url_file, args.max_age), allow_nan=False))
        return 0
    if args.mode == "status":
        try:
            report = read_status(args.state_dir)
        except (OSError, ValueError, KeyError, TypeError):
            report = dict(
                status="UNKNOWN", currently_healthy=False, dispatch_verified=False
            )
        print(json.dumps(report, allow_nan=False))
        return 0 if report["currently_healthy"] else 1
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    runner = Supervisor(
        args.state_dir,
        command,
        Policy(**{k: getattr(args, k) for k in asdict(Policy())}),
    )
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: runner.stop.set())
    try:
        return runner.run()
    except (OSError, ValueError, ChildStopUnconfirmed) as exc:
        print(
            json.dumps(
                dict(
                    event="TUNNEL_RELIABILITY",
                    status="HOLD",
                    reason="CHILD_TERMINATION_UNCONFIRMED"
                    if isinstance(exc, ChildStopUnconfirmed)
                    else "STATE_OR_PROCESS_OWNERSHIP_UNPROVEN",
                    task_replay=False,
                )
            ),
            flush=True,
        )
        return 78


if __name__ == "__main__":
    sys.exit(main())
