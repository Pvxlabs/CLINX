#!/usr/bin/env python3
"""Provision dedicated principals and revoke exact legacy cgroups.

No database migration, service cutover, sudo escalation, or rollback engine.
`inspect` is read-only. Mutating subcommands require the administrator directly.
"""
from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import pwd
import subprocess
import time
from pathlib import Path

ROOT = Path('/var/lib/clinx-rust')
PRINCIPALS = ('clinx-authority', 'clinx-api', 'clinx-transport', 'clinx-worker')
UNITS = ('clinx-execution-owner.service', 'clinx.service', 'clinx-node-centre.service',
         'clinx-provider.service', 'clinx-tunnel.service')


def run(argv, **kw):
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=20, **kw).stdout


def durable(path, payload):
    path = Path(path)
    data = json.dumps(payload, indent=2, sort_keys=True).encode() + b'\n'
    with path.open('xb') as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def user_systemctl(user, *args):
    account = pwd.getpwnam(user)
    env = dict(os.environ, XDG_RUNTIME_DIR=f'/run/user/{account.pw_uid}',
               DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{account.pw_uid}/bus')
    argv = ['/usr/bin/systemctl', '--user', *args]
    if os.geteuid() == 0:
        argv = ['/usr/sbin/runuser', '-u', user, '--', *argv]
    elif os.getuid() != account.pw_uid:
        raise RuntimeError('LEGACY_USER_IDENTITY_MISMATCH')
    return run(argv, env=env)


def process_identity(pid):
    if not pid:
        return None
    # comm can contain spaces and parentheses; parse after the last ')'.
    stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return dict(pid=pid, start_ticks=int(stat[19]), exe=os.readlink(f'/proc/{pid}/exe'))


def inspect(user):
    units = []
    for unit in UNITS:
        values = dict(line.split('=', 1) for line in user_systemctl(user, 'show', unit,
            '-p', 'MainPID', '-p', 'ControlGroup', '-p', 'ActiveState').splitlines() if '=' in line)
        units.append(dict(unit=unit, cgroup=values['ControlGroup'], active=values['ActiveState'],
                          process=process_identity(int(values['MainPID']))))
    accounts = {}
    for name in PRINCIPALS:
        try:
            a = pwd.getpwnam(name)
            accounts[name] = dict(uid=a.pw_uid, gid=a.pw_gid, groups=os.getgrouplist(name, a.pw_gid))
        except KeyError:
            accounts[name] = None
    return dict(schema=1, host=os.uname().nodename, boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                user=user, units=units, principals=accounts,
                status='NOT_VERIFIED' if all(accounts.values()) else 'BLOCKED',
                reason='DEDICATED_PRINCIPALS_AND_PRODUCTION_CONFINEMENT_REQUIRED')


def require_root():
    if os.geteuid() != 0:
        raise RuntimeError('BLOCKED: administrator execution required; current sudo allowlist does not grant CLINX provisioning')


def provision():
    require_root()
    for group in ('clinx-ipc', 'clinx-provider-ipc'):
        try:
            grp.getgrnam(group)
        except KeyError:
            run(['/usr/sbin/groupadd', '--system', group])
    for name in PRINCIPALS:
        try:
            a = pwd.getpwnam(name)
        except KeyError:
            run(['/usr/sbin/useradd', '--system', '--no-create-home', '--user-group', '--shell', '/usr/sbin/nologin', name])
            a = pwd.getpwnam(name)
        if a.pw_uid == 0 or a.pw_shell not in ('/usr/sbin/nologin', '/bin/false'):
            raise RuntimeError('DEDICATED_PRINCIPAL_CONFLICT:' + name)
        allowed = {a.pw_gid}
        if name != 'clinx-worker':
            run(['/usr/sbin/usermod', '-a', '-G', 'clinx-ipc', name])
            allowed.add(grp.getgrnam('clinx-ipc').gr_gid)
        if name != 'clinx-authority':
            run(['/usr/sbin/usermod', '-a', '-G', 'clinx-provider-ipc', name])
            allowed.add(grp.getgrnam('clinx-provider-ipc').gr_gid)
        if set(os.getgrouplist(name, a.pw_gid)) - allowed:
            raise RuntimeError('PRINCIPAL_HAS_UNEXPECTED_GROUPS:' + name)
    if ROOT.is_symlink():
        raise RuntimeError('STATE_ROOT_IDENTITY_CONFLICT')
    ROOT.mkdir(mode=0o755, exist_ok=True)
    if ROOT.stat().st_uid != 0 or ROOT.stat().st_mode & 0o022:
        raise RuntimeError('ROOT_CONTROLLED_STATE_PARENT_REQUIRED')
    writer = pwd.getpwnam('clinx-authority')
    state = ROOT / 'authority'
    # Never chown an existing state tree, open a legacy database or reuse history.
    if not state.exists():
        state.mkdir(mode=0o700)
        os.chown(state, writer.pw_uid, writer.pw_gid)
    if state.is_symlink() or state.stat().st_uid != writer.pw_uid or state.stat().st_mode & 0o077:
        raise RuntimeError('PRIVATE_AUTHORITY_DIRECTORY_REQUIRED')
    ipc = Path('/run/clinx-rust')
    ipc_group = grp.getgrnam('clinx-ipc').gr_gid
    if not ipc.exists():
        ipc.mkdir(mode=0o750)
        os.chown(ipc, writer.pw_uid, ipc_group)
        os.chmod(ipc, 0o2750)  # Group inheritance for the socket; never FD revocation.
    if (ipc.is_symlink() or ipc.stat().st_uid != writer.pw_uid
            or ipc.stat().st_gid != ipc_group or ipc.stat().st_mode & 0o022):
        raise RuntimeError('PROTECTED_IPC_PARENT_REQUIRED')
    return dict(status='PASS', scope='PRINCIPAL_PROVISIONING_ONLY',
                production_authority='NOT_VERIFIED', state=str(state))


def writable_holders(paths):
    identities = {(p.stat().st_dev, p.stat().st_ino) for p in paths if p.exists()}
    holders = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            for fd in (proc / 'fd').iterdir():
                try:
                    stat = fd.stat()
                    if (stat.st_dev, stat.st_ino) not in identities:
                        continue
                    flags = next(line.split()[1] for line in (proc / 'fdinfo' / fd.name).read_text().splitlines()
                                 if line.startswith('flags:'))
                    if int(flags, 8) & os.O_ACCMODE:
                        holders.append(dict(pid=int(proc.name), fd=int(fd.name)))
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass
    return holders


def revoke(manifest, receipt, legacy_db):
    require_root()
    saved = json.loads(Path(manifest).read_text())
    result_path = Path(receipt).resolve(strict=True)
    result = json.loads(result_path.read_text())
    if result.get('real_provider_e2e') != 'PASS' or result.get('physical_isolation') != 'PASS' or result.get('safe_to_retire_old_control_plane') is not True:
        raise RuntimeError('QUALIFIED_PERSISTED_MIGRATION_RESULT_REQUIRED')
    with result_path.open('rb') as f:
        os.fsync(f.fileno())
    fd = os.open(result_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    os.fsync(fd)
    os.close(fd)
    current = inspect(saved['user'])
    if any(saved[k] != current[k] for k in ('host', 'boot_id', 'user', 'units')):
        raise RuntimeError('LEGACY_PROCESS_IDENTITY_CHANGED_RECAPTURE_REQUIRED')
    groups = []
    for entry in saved['units']:
        if not entry['process']:
            continue
        relative = entry['cgroup']
        if not relative.startswith('/user.slice/') or '..' in relative:
            raise RuntimeError('UNEXPECTED_LEGACY_CGROUP')
        groups.append(Path('/sys/fs/cgroup') / relative.lstrip('/'))
    pids = set()
    for group in groups:
        for procs in group.rglob('cgroup.procs'):
            pids.update(int(x) for x in procs.read_text().split())
    db = Path(legacy_db).resolve(strict=True)
    paths = [db, Path(str(db) + '-wal'), Path(str(db) + '-shm')]
    if any(h['pid'] not in pids for h in writable_holders(paths)):
        raise RuntimeError('LEGACY_WRITABLE_FD_OUTSIDE_APPROVED_CGROUPS')
    # Disable automatic restart first, then kernel cgroup.kill includes SIGSTOP
    # processes and descendants. No SIGCONT interval and no per-UID kill.
    user_systemctl(saved['user'], 'mask', '--runtime', *UNITS)
    if inspect(saved['user'])['units'] != saved['units']:
        raise RuntimeError('LEGACY_PROCESS_CHANGED_AFTER_MASK')
    for group in groups:
        (group / 'cgroup.kill').write_text('1\n')
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if all(not g.exists() or 'populated 1' not in (g / 'cgroup.events').read_text() for g in groups):
            break
        time.sleep(.05)
    else:
        raise RuntimeError('LEGACY_CGROUP_NOT_REAPED')
    user_systemctl(saved['user'], 'stop', *UNITS)
    if writable_holders(paths):
        raise RuntimeError('LEGACY_WRITABLE_FD_REMAINED')
    return dict(status='PASS', scope='EXACT_LEGACY_CGROUP_REAP',
                migration_receipt_sha256=hashlib.sha256(result_path.read_bytes()).hexdigest(),
                old_holders='REAPED', production_cutover='NOT_PERFORMED')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=('inspect', 'provision', 'revoke'))
    p.add_argument('--user', default='pvxlabs')
    p.add_argument('--output', required=True)
    p.add_argument('--manifest')
    p.add_argument('--migration-receipt')
    p.add_argument('--legacy-db')
    a = p.parse_args()
    try:
        if a.command == 'inspect':
            result = inspect(a.user)
        elif a.command == 'provision':
            result = provision()
        else:
            if not all((a.manifest, a.migration_receipt, a.legacy_db)):
                p.error('revoke requires --manifest --migration-receipt --legacy-db')
            result = revoke(a.manifest, a.migration_receipt, a.legacy_db)
    except (RuntimeError, OSError, subprocess.SubprocessError, KeyError, ValueError) as exc:
        result = dict(status='BLOCKED', reason=str(exc)[:500])
    durable(a.output, result)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
