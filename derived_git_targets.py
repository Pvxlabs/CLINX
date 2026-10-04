"""Revalidate task-owned Git worktrees before granting or using a Git target."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess


class DerivedGitTargetError(ValueError):
    pass


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(("git", "-C", str(path), *args), capture_output=True,
                            text=True, timeout=10, env={
                                "HOME": str(Path.home()), "PATH": os.defpath,
                                "LANG": "C", "LC_ALL": "C",
                            })
    if result.returncode:
        raise DerivedGitTargetError("Git worktree identity is unavailable")
    return result.stdout.strip()


def inspect_worktree(path: str | Path, parent: str | Path, origin: str,
                     trusted_roots: tuple[Path, ...]) -> dict[str, str | int]:
    """Require a real linked worktree in the parent's repository and trusted root."""
    candidate = Path(path).expanduser().absolute()
    try:
        resolved = candidate.resolve(strict=True)
        parent_root = Path(parent).resolve(strict=True)
    except OSError as exc:
        raise DerivedGitTargetError("Git target path is unavailable") from exc
    if candidate != resolved or not resolved.is_dir():
        raise DerivedGitTargetError("Git target is a symlink or is not a directory")
    if resolved == parent_root or not trusted_roots or not any(
            resolved.is_relative_to(root.resolve(strict=True)) for root in trusted_roots):
        raise DerivedGitTargetError("Git target is outside trusted derived workspace roots")
    dot_git = resolved / ".git"
    if not dot_git.is_file() or dot_git.is_symlink():
        raise DerivedGitTargetError("Git target is not a linked worktree")
    if Path(_git(resolved, "rev-parse", "--show-toplevel")).resolve() != resolved:
        raise DerivedGitTargetError("Git target is not a worktree root")
    common = Path(_git(resolved, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    parent_common = Path(_git(parent_root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    if not common.samefile(parent_common):
        raise DerivedGitTargetError("Git target belongs to a different repository")
    git_dir = Path(_git(resolved, "rev-parse", "--path-format=absolute", "--git-dir")).resolve()
    if not git_dir.is_relative_to(common / "worktrees") or not git_dir.is_dir():
        raise DerivedGitTargetError("Git target is not a linked worktree")
    listed = _git(parent_root, "worktree", "list", "--porcelain", "-z").split("\0")
    if "worktree " + str(resolved) not in listed:
        raise DerivedGitTargetError("Git target is absent from repository worktree registry")
    branch = validate_push_state(resolved, origin)
    stat = git_dir.stat()
    return {"path": str(resolved), "common_dir": str(common), "git_dir": str(git_dir),
            "git_dir_device": stat.st_dev, "git_dir_inode": stat.st_ino,
            "branch": branch, "origin": origin}


def validate_push_state(path: str | Path, origin: str) -> str:
    """Validate the only branch and remote which the fixed Git push may write."""
    resolved = Path(path).resolve(strict=True)
    urls = _git(resolved, "remote", "get-url", "--all", "origin").splitlines()
    push_urls = _git(resolved, "remote", "get-url", "--push", "--all", "origin").splitlines()
    if urls != [origin] or push_urls != [origin]:
        raise DerivedGitTargetError("Git target origin differs from the registered repository")
    branch = _git(resolved, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) or any(
            part in branch for part in ("..", "//", "@{")) or branch.endswith(("/", ".", ".lock")):
        raise DerivedGitTargetError("Git target branch is invalid")
    protected = {"main", "master"}
    remote_head = subprocess.run(("git", "-C", str(resolved), "symbolic-ref", "--quiet", "--short",
                                  "refs/remotes/origin/HEAD"), capture_output=True, text=True)
    if remote_head.returncode == 0:
        protected.add(remote_head.stdout.strip().removeprefix("origin/"))
    configured = subprocess.run(("git", "-C", str(resolved), "config", "--get-all",
                                 "clinx.protectedBranch"), capture_output=True, text=True)
    if configured.returncode == 0:
        protected.update(configured.stdout.splitlines())
    elif configured.returncode != 1:
        raise DerivedGitTargetError("repository protected branch configuration is unavailable")
    if branch in protected:
        raise DerivedGitTargetError("Git target branch is protected")
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "rebase-merge", "rebase-apply"):
        if Path(_git(resolved, "rev-parse", "--git-path", marker)).exists():
            raise DerivedGitTargetError("Git target has an unresolved operation")
    return branch
