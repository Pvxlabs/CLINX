"""Publish the confined Provider's existing native socket to its IPC group."""
import grp
import os
import stat
import time
from pathlib import Path

from rust_worker_identity import worker_account


def main():
    if os.getuid() != worker_account().pw_uid:
        raise SystemExit('PROVIDER_IDENTITY_REQUIRED')
    root = Path('/run/clinx-rust-provider')
    native = Path('/tmp') / f'codex-daemon-{os.getuid()}'
    if not native.samefile(root / 'native'):
        raise SystemExit('PROVIDER_NATIVE_RUNTIME_BIND_REQUIRED')
    endpoint = root / 'provider.sock'
    for _ in range(150):
        try:
            target = endpoint.resolve(strict=True)
            if native not in target.parents or not stat.S_ISSOCK(target.stat().st_mode):
                raise RuntimeError('PROVIDER_SOCKET_OUTSIDE_PRIVATE_RUNTIME')
            for path in (target, *target.parents):
                info = path.stat()
                if info.st_uid != os.getuid():
                    raise RuntimeError('PROVIDER_SOCKET_OWNER_MISMATCH')
                os.chown(path, -1, grp.getgrnam('clinx-provider-ipc').gr_gid)
                path.chmod(0o750 if path.is_dir() else 0o660)
                if path == native:
                    break
            return
        except FileNotFoundError:
            time.sleep(.1)
    raise SystemExit('PROVIDER_SOCKET_NOT_READY')


if __name__ == '__main__':
    main()
