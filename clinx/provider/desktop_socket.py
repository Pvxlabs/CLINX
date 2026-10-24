"""Publish a verified existing Desktop socket; never launch a Codex daemon.

The legacy logical alias preserves Rust's endpoint and historical receipt IDs.
Run as the Desktop owner after draining managed executions. Socket recreation
requires explicit re-publication; there is no discovery or Provider fallback.
"""
import argparse
import json
import os
from pathlib import Path
import pwd
import socket
import stat
import struct
import subprocess

import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from clinx.provider.native_provider import existing_client


def inspect(socket_path, witness_thread_id):
    path = Path(socket_path)
    root = Path('/tmp') / f'codex-daemon-{os.getuid()}'
    if (not path.is_absolute() or path.parent != root or path.is_symlink()
            or root.is_symlink() or root.stat().st_uid != os.getuid()
            or path.stat().st_uid != os.getuid() or not stat.S_ISSOCK(path.stat().st_mode)):
        raise RuntimeError('DESKTOP_SOCKET_IDENTITY_REQUIRED')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(3)
        peer.connect(str(path))
        pid, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid != os.getuid() or uid == 0:
        raise RuntimeError('DESKTOP_SOCKET_PEER_CONFLICT')
    before = (path.stat().st_dev, path.stat().st_ino)
    with existing_client(str(path), timeout=3) as client:
        client.initialize(client_name='clinx-desktop-socket', client_title='CLINX Desktop socket verification',
                          client_version='1')
        thread = client.thread_read(witness_thread_id)
    if thread.get('id') != witness_thread_id or before != (path.stat().st_dev, path.stat().st_ino):
        raise RuntimeError('DESKTOP_WITNESS_CONFLICT')
    return dict(socket=str(path), daemon_pid=pid, uid=uid, socket_device=before[0], socket_inode=before[1],
                witness_thread_id=witness_thread_id, witness_cwd=thread.get('cwd'), daemon_started=False)


def publish(socket_path, witness_thread_id, alias, *, apply=False):
    result = inspect(socket_path, witness_thread_id)
    alias = Path(alias)
    if (alias != Path('/run/clinx-rust-provider/provider.sock')
            or alias.parent.is_symlink() or alias.parent.stat().st_uid != os.getuid()
            or (alias.exists() and not alias.is_symlink())):
        raise RuntimeError('DESKTOP_ALIAS_IDENTITY_REQUIRED')
    result['previous_alias'] = str(alias.readlink()) if alias.is_symlink() else None
    result['alias'] = str(alias)
    result['applied'] = False
    if apply:
        # Grant only the existing API, transport and authority principals access
        # to this exact socket. No project, credential or Codex-home ACL changes.
        identities = [pwd.getpwnam('clinx-' + name).pw_uid for name in ('api', 'transport', 'authority')]
        for path, rights in ((Path(socket_path).parent, '--x'), (Path(socket_path), 'rw-')):
            subprocess.run(['/usr/bin/setfacl', '-m', ','.join(f'u:{uid}:{rights}' for uid in identities),
                            str(path)], check=True)
        # Recheck the actual peer and inode after granting access, before publish.
        verified = inspect(socket_path, witness_thread_id)
        if any(result[key] != verified[key] for key in ('daemon_pid', 'socket_device', 'socket_inode')):
            raise RuntimeError('DESKTOP_SOCKET_CHANGED')
        temporary = alias.with_name(alias.name + f'.desktop-{os.getpid()}')
        try:
            temporary.symlink_to(socket_path)
            os.replace(temporary, alias)
        finally:
            temporary.unlink(missing_ok=True)
        result['applied'] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', required=True)
    parser.add_argument('--witness-thread-id', required=True)
    parser.add_argument('--alias', default='/run/clinx-rust-provider/provider.sock')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(publish(args.socket, args.witness_thread_id, args.alias, apply=args.apply)))


if __name__ == '__main__':
    main()
