"""Sealed local Git routing. Task text cannot silently select another checkout."""

import re
import subprocess
from pathlib import Path

from task_registry import TaskRegistryError


class WorkspaceContractError(TaskRegistryError):
    side_effect = "NONE"

    def __init__(self, message):
        self.code = message.split(":", 1)[0]
        super().__init__(message)


def git(path, *args):
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkspaceContractError("WORKSPACE_IDENTITY_UNAVAILABLE") from exc
    return result.stdout.strip()


def reject_unstructured_workspace(prompt):
    # Never infer authority/identity from prose. An explicit workspace instruction
    # needs the structured contract; keep ordinary mentions of worktrees usable.
    if re.search(
        r"(?:工作树|工作目录|执行目录|worktree|working.directory|cwd)\s*[:=：]?\s*[/`]",
        prompt,
        re.I,
    ):
        raise WorkspaceContractError(
            "WORKSPACE_CONTRACT_REQUIRED: supply cwd, branch and full head separately"
        )


def seal_workspace(path, branch, requested=None):
    try:
        path = Path(path).resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise WorkspaceContractError("WORKSPACE_IDENTITY_UNAVAILABLE") from exc
    if requested is not None and (
        not isinstance(requested, dict)
        or set(requested) != {"cwd", "branch", "head"}
        or not all(isinstance(v, str) and v for v in requested.values())
        or not Path(requested["cwd"]).is_absolute()
        or not re.fullmatch(r"[0-9a-f]{40}", requested["head"])
    ):
        raise WorkspaceContractError("WORKSPACE_CONTRACT_INVALID")
    actual = {
        "cwd": str(path),
        "branch": git(path, "branch", "--show-current"),
        "head": git(path, "rev-parse", "HEAD"),
    }
    if git(path, "rev-parse", "--show-toplevel") != str(path) or not actual["branch"]:
        raise WorkspaceContractError(
            "WORKSPACE_CONTRACT_MISMATCH: exact checkout and attached branch required"
        )
    if branch and branch != actual["branch"]:
        raise WorkspaceContractError("WORKSPACE_CONTRACT_MISMATCH: branch differs")
    if requested is not None and (
        str(Path(requested["cwd"]).resolve()) != actual["cwd"]
        or requested["branch"] != actual["branch"]
        or requested["head"] != actual["head"]
    ):
        raise WorkspaceContractError(
            "WORKSPACE_CONTRACT_MISMATCH: cwd, branch or HEAD differs"
        )
    return actual


def select_workspace(descriptor, requested):
    if not isinstance(requested, dict) or "cwd" not in requested:
        raise WorkspaceContractError("WORKSPACE_CONTRACT_INVALID")
    actual = seal_workspace(requested["cwd"], None, requested)
    base = descriptor.cwd.resolve(strict=True)
    path = Path(actual["cwd"])
    # A path outside configured roots is admitted only as an existing Git linked
    # worktree of the registered project, never as an arbitrary sibling clone.
    if git(base, "rev-parse", "--path-format=absolute", "--git-common-dir") != git(
        path, "rev-parse", "--path-format=absolute", "--git-common-dir"
    ):
        raise WorkspaceContractError("WORKSPACE_REPOSITORY_MISMATCH")
    registered = [
        line[9:]
        for line in git(base, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]
    if str(path) not in registered:
        raise WorkspaceContractError("WORKSPACE_NOT_REGISTERED")
    if (
        descriptor.repository_origin
        and git(path, "remote", "get-url", "origin") != descriptor.repository_origin
    ):
        raise WorkspaceContractError("WORKSPACE_ORIGIN_MISMATCH")
    return path, actual["branch"]
