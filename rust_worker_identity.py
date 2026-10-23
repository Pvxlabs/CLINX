"""Read the root-owned deployment identity; callers cannot choose a worker UID."""
import os
import pwd
import stat
from pathlib import Path


def worker_account():
    path = Path('/var/lib/clinx-rust/config/worker-user')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode):
            raise RuntimeError('ROOT_CONTROLLED_WORKER_IDENTITY_REQUIRED')
        name = stream.read(256).strip()
    if not name or not name.isascii() or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in name):
        raise RuntimeError('INVALID_WORKER_IDENTITY')
    account = pwd.getpwnam(name)
    if account.pw_uid == 0 or name in ('clinx-authority', 'clinx-api', 'clinx-transport'):
        raise RuntimeError('WORKER_AUTHORITY_IDENTITY_CONFLICT')
    return account
