"""Sealed local Git routing. Task text cannot silently select another checkout."""

import re
import subprocess
from pathlib import Path

from runtime_contracts import TaskRegistryError


class WorkspaceContractError(TaskRegistryError):
    side_effect = "NONE"

    def __init__(self, message):
        self.code = message.split(":", 1)[0]
        super().__init__(message)


def workspace_contract_schema():
    """The public request and the sealed value use the same closed contract."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["cwd", "branch", "head"],
        "properties": {
            "cwd": {"type": "string", "pattern": r"^/[^\x00]*$"},
            "branch": {"type": "string", "minLength": 1, "not": {"pattern": r"[\s\x00]"}},
            "head": {"type": "string", "maxLength": 40, "pattern": "^[0-9a-f]{40}$"},
        },
        "description": (
            "Optional exact Git workspace override. Natural-language workspace mentions are task context. "
            "Supply all three strings: absolute cwd, attached branch, full lowercase 40-hex HEAD. "
            "Only the registered project or its existing linked worktrees are accepted; "
            "cwd/branch/HEAD must match Git. Omission seals the configured workspace, "
            "or inherits the sealed linked workspace on continuation."
        ),
    }


def validate_workspace_contract(requested):
    """Validate shape before routing, adoption or policy writes, including direct calls."""
    if (
        not isinstance(requested, dict)
        or set(requested) != {"cwd", "branch", "head"}
        or not all(isinstance(v, str) and v for v in requested.values())
        or not re.fullmatch(r"/[^\x00]*", requested["cwd"])
        or not re.fullmatch(r"[^\s\x00]+", requested["branch"])
        or not re.fullmatch(r"[0-9a-f]{40}", requested["head"])
    ):
        raise WorkspaceContractError("WORKSPACE_CONTRACT_INVALID")


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


# Require an instruction delimiter. A bare slash after a category label is
# ordinary prose ("工作树/Owner/Provider"), not execution authority.
_WORKSPACE_PATH = re.compile(
    r"(?:工作树|工作目录|执行目录|(?<![\w])(?:worktree|working[ .]directory|cwd)(?![\w]))"
    r"(?:\s*[:=：]\s*[`\"']?|\s+[`\"']?|[`\"'])(/[^\s`\"'，。；,;]+)",
    re.I,
)


def reject_unstructured_workspace(prompt):
    # Keep detection identical to legacy explicit-path routing.
    if _WORKSPACE_PATH.search(prompt):
        raise WorkspaceContractError(
            "WORKSPACE_CONTRACT_REQUIRED: supply cwd, branch and full head separately"
        )


def workspace_from_prompt(prompt):
    """Resolve only a delimited explicit checkout; ordinary prose is context."""
    paths = set(_WORKSPACE_PATH.findall(prompt))
    if len(paths) > 1:
        raise WorkspaceContractError('WORKSPACE_TARGET_AMBIGUOUS: multiple explicit checkouts')
    return seal_workspace(next(iter(paths)), None) if paths else None


def seal_workspace(path, branch, requested=None):
    if requested is not None:
        validate_workspace_contract(requested)
    try:
        path = Path(path).resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise WorkspaceContractError("WORKSPACE_IDENTITY_UNAVAILABLE") from exc
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
    validate_workspace_contract(requested)
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
