#!/usr/bin/env python3
"""Exercise held-FD revocation below an actual legacy service cgroup.

Only a task-owned child cgroup is killed. Existing production processes are
preserved; PASS here is explicitly not whole-service revocation or cutover.
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

from rust_authority_isolation import ROOT, durable, inspect, require_root


def execute(user):
    require_root()
    before = inspect(user)
    entry = next(e for e in before['units'] if e['unit'] == 'clinx.service')
    if not entry['process'] or entry['active'] != 'active':
        raise RuntimeError('LIVE_LEGACY_SERVICE_REQUIRED')
    parent = Path('/sys/fs/cgroup') / entry['cgroup'].lstrip('/')
    if not entry['cgroup'].startswith('/user.slice/') or not parent.is_dir():
        raise RuntimeError('EXACT_LEGACY_CGROUP_REQUIRED')
    nonce = uuid.uuid4().hex
    group = parent / ('rust-fd-qualification-' + nonce)
    sentinel = ROOT / 'authority' / ('legacy-fd-' + nonce)
    moved = sentinel.with_suffix('.original')
    alias = ROOT / ('legacy-alias-' + nonce)
    account, authority = pwd.getpwnam(user), pwd.getpwnam('clinx-authority')
    group.mkdir()
    fd = os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_CLOEXEC, 0o600)
    os.fchown(fd, authority.pw_uid, authority.pw_gid)
    read, write = os.pipe()
    proceed, allow = os.pipe()
    pid = None
    reaped = False

    def await_stopped(marker):
        if not select.select([read], [], [], 5)[0] or os.read(read, 1) != marker:
            raise RuntimeError('OLD_FD_WRITE_NOT_PROVEN')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
            if state == 'T':
                return
            time.sleep(.01)
        raise RuntimeError('OLD_WORKER_NOT_STOPPED')

    try:
        pid = os.fork()
        if pid == 0:
            try:
                os.close(read)
                os.close(allow)
                os.read(proceed, 1)
                os.setgroups(os.getgrouplist(user, account.pw_gid))
                os.setgid(account.pw_gid)
                os.setuid(account.pw_uid)
                for marker in (b'1', b'2', b'3'):
                    os.pwrite(fd, marker, 0)
                    os.fsync(fd)
                    os.write(write, marker)
                    os.kill(os.getpid(), signal.SIGSTOP)
                os.pwrite(fd, b'UNEXPECTED_SURVIVOR', 0)
                os.fsync(fd)
                os._exit(9)
            except BaseException:
                os._exit(10)
        os.close(write)
        os.close(proceed)
        (group / 'cgroup.procs').write_text(str(pid))
        os.write(allow, b'1')
        os.close(allow)
        await_stopped(b'1')
        assert os.pread(fd, 1, 0) == b'1'
        os.kill(pid, signal.SIGCONT)
        await_stopped(b'2')
        assert os.pread(fd, 1, 0) == b'2'
        sentinel.rename(moved)
        with sentinel.open('xb') as replacement:
            replacement.write(b'NEW_PATH_INODE')
            replacement.flush()
            os.fsync(replacement.fileno())
        os.chown(sentinel, authority.pw_uid, authority.pw_gid)
        sentinel.chmod(0o600)
        os.kill(pid, signal.SIGCONT)
        await_stopped(b'3')
        assert moved.read_bytes() == b'3' and sentinel.read_bytes() == b'NEW_PATH_INODE'
        identity = dict(pid=pid, cgroup=Path(f'/proc/{pid}/cgroup').read_text(),
                        uid=account.pw_uid, held_inode=os.fstat(fd).st_ino)
        (group / 'cgroup.kill').write_text('1\n')
        _, status = os.waitpid(pid, 0)
        reaped = True
        if not os.WIFSIGNALED(status) or os.WTERMSIG(status) != signal.SIGKILL:
            raise RuntimeError('KERNEL_KILL_NOT_PROVEN')
        try:
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        else:
            raise RuntimeError('REAPED_PROCESS_STILL_RESUMABLE')
        if 'populated 1' in (group / 'cgroup.events').read_text():
            raise RuntimeError('CGROUP_NOT_EMPTY')
        alias.symlink_to(ROOT / 'authority/authority.sqlite3')
        check = os.fork()
        if check == 0:
            os.close(fd)
            os.setgroups(os.getgrouplist(user, account.pw_gid))
            os.setgid(account.pw_gid)
            os.setuid(account.pw_uid)
            for path in (ROOT / 'authority/authority.sqlite3', alias):
                try:
                    opened = os.open(path, os.O_RDWR)
                except PermissionError:
                    continue
                else:
                    os.close(opened)
                    os._exit(11)
            os._exit(0)
        _, check_status = os.waitpid(check, 0)
        assert check_status == 0 and moved.read_bytes() == b'3'
        after = inspect(user)
        if before['units'] != after['units']:
            raise RuntimeError('LEGACY_SERVICE_IDENTITY_CHANGED_DURING_PROBE')
        return dict(status='PASS', scope='ACTUAL_LEGACY_SERVICE_CHILD_CGROUP_ONLY',
                    legacy_service=entry, probe=identity,
                    held_fd_writable_before_kill=True, resumed_fd_writable_before_kill=True,
                    replaced_path_did_not_revoke_old_fd=True, cgroup_killed_and_reaped=True,
                    sigcont_after_reap_denied=True, legacy_uid_new_db_and_alias_open_denied=True,
                    sentinel_sha256=hashlib.sha256(moved.read_bytes()).hexdigest(),
                    whole_legacy_service_revocation='NOT_VERIFIED', production_cutover=False,
                    unrelated_production_processes_unchanged=True)
    finally:
        if pid is not None and not reaped:
            (group / 'cgroup.kill').write_text('1\n')
            os.waitpid(pid, 0)
        os.close(fd)
        os.close(read)
        alias.unlink(missing_ok=True)
        sentinel.unlink(missing_ok=True)
        moved.unlink(missing_ok=True)
        group.rmdir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', default='pvxlabs')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError('UNIQUE_RECEIPT_REQUIRED')
    result = execute(args.user)
    durable(args.output, result)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
