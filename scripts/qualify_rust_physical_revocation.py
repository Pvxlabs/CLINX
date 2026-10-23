#!/usr/bin/env python3
"""Root-run held-FD/SIGSTOP/path-alias prerequisite; never retires production.

Uses the provisioned principals and a disposable sentinel in the protected new
state directory. This is a kernel prerequisite, NOT a production revocation
receipt; production also needs exact worker inventory, confined units and reaping.
"""

import argparse
import hashlib
import json
import os
import pwd
import select
import signal
import time
import uuid
from pathlib import Path

from rust_authority_isolation import ROOT, durable, require_root


def stopped(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "T"


def execute(legacy_user):
    require_root()
    authority = pwd.getpwnam("clinx-authority")
    worker = pwd.getpwnam((ROOT / "config/worker-user").read_text().strip())
    old = pwd.getpwnam(legacy_user)
    if (authority.pw_uid == 0 or worker.pw_uid == 0 or old.pw_uid == 0
            or authority.pw_uid in (worker.pw_uid, old.pw_uid)):
        raise RuntimeError("DISTINCT_AUTHORITY_PRINCIPAL_REQUIRED")
    directory = ROOT / "authority"
    if directory.is_symlink() or directory.stat().st_uid != authority.pw_uid:
        raise RuntimeError("PROVISIONED_AUTHORITY_DIRECTORY_REQUIRED")
    nonce = uuid.uuid4().hex
    group = Path("/sys/fs/cgroup") / ("clinx-fencing-qualification-" + nonce)
    sentinel = directory / ("fencing-" + nonce)
    alias = ROOT / ("fencing-alias-" + nonce)
    group.mkdir()
    fd = os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_CLOEXEC, 0o600)
    os.fchown(fd, authority.pw_uid, authority.pw_gid)
    ready_read, ready_write = os.pipe()
    proceed_read, proceed_write = os.pipe()
    pid = None
    reaped = False
    try:
        pid = os.fork()
        if pid == 0:
            try:
                os.close(ready_read)
                os.close(proceed_write)
                os.read(proceed_read, 1)  # Parent first places us in the exact cgroup.
                os.setgroups(os.getgrouplist(old.pw_name, old.pw_gid))
                os.setgid(old.pw_gid)
                os.setuid(old.pw_uid)
                os.pwrite(fd, b"HELD_FD_WRITABLE_BEFORE_KILL", 0)
                os.fsync(fd)
                os.write(ready_write, b"1")
                os.kill(os.getpid(), signal.SIGSTOP)
                # Any resumed survivor would visibly corrupt the sentinel.
                os.pwrite(fd, b"RESUMED_OLD_WRITER", 0)
                os.fsync(fd)
                os._exit(9)
            except BaseException:
                os._exit(10)
        os.close(ready_write)
        os.close(proceed_read)
        (group / "cgroup.procs").write_text(str(pid))
        os.write(proceed_write, b"1")
        os.close(proceed_write)
        if (
            not select.select([ready_read], [], [], 5)[0]
            or os.read(ready_read, 1) != b"1"
        ):
            raise RuntimeError("HELD_FD_PROOF_FAILED")
        deadline = time.monotonic() + 5
        while not stopped(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        if not stopped(pid):
            raise RuntimeError("OLD_WORKER_NOT_STOPPED")
        before = os.pread(fd, 128, 0)
        if before != b"HELD_FD_WRITABLE_BEFORE_KILL":
            raise RuntimeError("OLD_WRITABLE_FD_NOT_DEMONSTRATED")
        (group / "cgroup.kill").write_text("1\n")
        _, status = os.waitpid(pid, 0)
        reaped = True
        if not os.WIFSIGNALED(status) or os.WTERMSIG(status) != signal.SIGKILL:
            raise RuntimeError("KERNEL_REAP_NOT_PROVEN")
        try:
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        else:
            raise RuntimeError("OLD_PID_STILL_EXISTS")
        if "populated 1" in (group / "cgroup.events").read_text():
            raise RuntimeError("CGROUP_NOT_EMPTY")
        alias.symlink_to(sentinel)
        # After reaping, the unprivileged replacement worker receives no FD and
        # cannot open either the canonical path or a substituted alias.
        check = os.fork()
        if check == 0:
            os.close(fd)
            os.setgroups(os.getgrouplist(worker.pw_name, worker.pw_gid))
            os.setgid(worker.pw_gid)
            os.setuid(worker.pw_uid)
            for path in (sentinel, alias):
                try:
                    opened = os.open(path, os.O_RDWR)
                except PermissionError:
                    continue
                else:
                    os.close(opened)
                    os._exit(11)
            os._exit(0)
        _, check_status = os.waitpid(check, 0)
        if check_status != 0 or os.pread(fd, 128, 0) != before:
            raise RuntimeError("NEW_STATE_PATH_DENIAL_FAILED")
        return dict(
            status="PASS",
            scope="KERNEL_PREREQUISITE_ONLY",
            production_revocation="NOT_VERIFIED",
            old_uid=old.pw_uid,
            worker_uid=worker.pw_uid,
            authority_uid=authority.pw_uid,
            held_fd_write_before_kill=True,
            stopped_worker_sigkill=True,
            cgroup_empty=True,
            canonical_and_alias_open_denied=True,
            sentinel_sha256=hashlib.sha256(before).hexdigest(),
        )
    finally:
        if pid is not None and not reaped:
            try:
                (group / "cgroup.kill").write_text("1\n")
                os.waitpid(pid, 0)
            except (FileNotFoundError, ChildProcessError):
                pass
        os.close(fd)
        os.close(ready_read)
        alias.unlink(missing_ok=True)
        sentinel.unlink(missing_ok=True)
        group.rmdir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-user", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Reject before touching root-owned paths, even if output is unavailable.
    require_root()
    result = execute(args.legacy_user)
    durable(args.output, result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
