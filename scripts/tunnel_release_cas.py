"""Config-only CAS preflight and isolated rollback qualification. No service actions.

The write CLI deliberately accepts task-owned artifact fixture roots only. A future
authorized activation driver must establish drain/lifecycle gates before consuming
the same plan. Never restores a DB, state budget, profile, launcher or Owner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path


class CASRefused(ValueError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def rooted(root, name):
    relative = Path(name)
    if not relative.is_absolute() or ".." in relative.parts:
        raise CASRefused("INVALID_ABSOLUTE_PATH")
    target = root / relative.relative_to("/")
    for parent in [target, *target.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise CASRefused("SYMLINK_REFUSED: " + name)
    return target


def state(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise CASRefused("NOT_REGULAR_FILE")
    return {"sha256": sha(path.read_bytes()), "mode": stat.S_IMODE(info.st_mode)}


def fixture_root(root):
    root = Path(root).absolute()
    if root == Path("/") or not root.is_relative_to("/data/artifacts"):
        raise CASRefused("OFFLINE_FIXTURE_ROOT_REQUIRED")
    for p in [root, *root.parents]:
        if p.is_symlink():
            raise CASRefused("FIXTURE_SYMLINK_REFUSED")
    marker = root / ".clinx-p1-fixture"
    if (
        not marker.is_file()
        or marker.is_symlink()
        or marker.read_text() != "ISOLATED_CONFIG_ONLY\n"
        or root.stat().st_uid != os.getuid()
    ):
        raise CASRefused("FIXTURE_OWNERSHIP_MARKER_REQUIRED")
    return root


def check(plan, capsule, root, direction, *, guard_root=None):
    root, capsule = Path(root).absolute(), Path(capsule)
    # Fixtures may validate dependency hashes in place, without copying credentials.
    guard_root = root if guard_root is None else Path(guard_root).absolute()
    expected = "before" if direction == "forward" else "after"
    desired = "after" if direction == "forward" else "before"
    allowed = set(plan["managed_paths"])
    home = plan["home"]
    exact = {
        home + "/.config/systemd/user/clinx-tunnel.service",
        home + "/.config/systemd/user/clinx-tunnel-health.service",
        home + "/.config/systemd/user/clinx-tunnel.service.d/99-p1-reliability.conf",
    }
    if direction not in ("forward", "rollback") or allowed != exact:
        raise CASRefused("CONFIG_ONLY_SCOPE_REQUIRED")
    if set(plan["changes"]) != allowed:
        raise CASRefused("CHANGE_SET_MISMATCH")
    # Check the whole guarded unit topology and dependencies before any write.
    for directory, names in plan["directory_entries"].items():
        target = rooted(root, directory)
        actual = sorted(p.name for p in target.iterdir()) if target.exists() else []
        wanted = set(names)
        for name in allowed:
            if str(Path(name).parent) == directory:
                if plan["changes"][name][expected] is None:
                    wanted.discard(Path(name).name)
                else:
                    wanted.add(Path(name).name)
        if actual != sorted(wanted):
            raise CASRefused("DIRECTORY_DRIFT: " + directory)
    for name, guard in plan["guards"].items():
        if name in allowed:
            raise CASRefused("GUARD_CHANGE_OVERLAP")
        if state(rooted(guard_root, name)) != guard:
            raise CASRefused("DEPENDENCY_DRIFT: " + name)
    changes = []
    for name in sorted(allowed):
        entry = plan["changes"][name]
        if state(rooted(root, name)) != entry[expected]:
            raise CASRefused("STALE_STATE: " + name)
        data = None
        target_state = entry[desired]
        if target_state is not None:
            digest = target_state["sha256"]
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise CASRefused("INVALID_BLOB_DIGEST")
            data = (capsule / "blobs" / digest).read_bytes()
            if sha(data) != digest:
                raise CASRefused("BLOB_HASH_MISMATCH")
        changes.append((name, target_state, data))
    return changes


def apply_fixture(plan, capsule, root, direction, *, guard_root=None):
    root = fixture_root(root)
    changes = check(plan, capsule, root, direction, guard_root=guard_root)
    # Files are replaced atomically one at a time, not as a multi-file transaction.
    # Offline consumers only; crash/partial-state must fail the next CAS preflight.
    for name, target_state, data in changes:
        target = rooted(root, name)
        if target_state is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".p1-cas-")
        try:
            with os.fdopen(fd, "wb") as f:
                os.fchmod(f.fileno(), target_state["mode"])
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, target)
            parent_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    opposite = "rollback" if direction == "forward" else "forward"
    check(plan, capsule, root, opposite, guard_root=guard_root)
    return {"status": "PASS", "direction": direction, "scope": "OFFLINE_CONFIG_ONLY"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "fixture-apply"))
    parser.add_argument("--capsule", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--guard-root", type=Path, help="Read-only dependency root; defaults to --root"
    )
    parser.add_argument("--direction", choices=("forward", "rollback"), required=True)
    args = parser.parse_args()
    plan = json.loads((args.capsule / "plan.json").read_text())
    if args.action == "fixture-apply":
        print(
            json.dumps(
                apply_fixture(
                    plan, args.capsule, args.root, args.direction, guard_root=args.guard_root
                )
            )
        )
    else:
        check(plan, args.capsule, args.root, args.direction, guard_root=args.guard_root)
        print(json.dumps({"status": "PASS", "scope": "READ_ONLY_CAS"}))


if __name__ == "__main__":
    main()
