"""Refresh only the Observer when its read-only native SQLite mounts are stale.

Runs outside the Observer namespace. It stats six fixed paths, never opens history
contents or credentials, and never starts an inactive Observer or touches a Provider.
"""
from pathlib import Path
import subprocess


UNIT = "clinx-observer.service"
FILES = tuple(name + suffix for name in ("state_5.sqlite", "thread_history_1.sqlite")
              for suffix in ("", "-wal", "-shm"))


def identities(root):
    result = {}
    for name in FILES:
        try:
            stat = (root / name).stat()
            result[name] = (stat.st_dev, stat.st_ino)
        except FileNotFoundError:
            result[name] = None
    return result


def active_pid():
    result = subprocess.run(
        ["systemctl", "--user", "show", UNIT, "-p", "MainPID", "-p", "ActiveState"],
        check=True, capture_output=True, text=True, timeout=5)
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    pid = int(fields.get("MainPID", "0"))
    return pid if fields.get("ActiveState") == "active" and pid > 0 else None


def refresh_if_stale(native_root=None, proc_root=Path("/proc")):
    native_root = native_root or Path.home() / ".codex"
    pid = active_pid()
    if pid is None:
        return False
    mounted_root = proc_root / str(pid) / "root" / native_root.relative_to("/")
    before = identities(native_root)
    # Wait out creation/replacement rather than restarting on a half-created source.
    if any(before[name] is None for name in FILES if name.endswith(".sqlite")):
        return False
    mounted = identities(mounted_root)
    if before != identities(native_root) or before == mounted:
        return False
    if active_pid() != pid:
        return False
    # try-restart also honours an operator stop racing this check.
    subprocess.run(["systemctl", "--user", "try-restart", UNIT], check=True, timeout=15)
    print("Observer native history mounts refreshed", flush=True)
    return True


if __name__ == "__main__":
    refresh_if_stale()
