"""Shared compatibility identities; no registry, storage or state transitions."""

from __future__ import annotations

import hashlib
import os


class TaskRegistryError(RuntimeError):
    pass


class M9IntegrationError(RuntimeError):
    def __init__(self, message, *, details=None):
        super().__init__(message)
        self.details = details or {}


class BridgeError(RuntimeError):
    pass


TERMINAL_EXECUTION_STAGES = frozenset(
    {
        "CANCELLED",
        "COMPLETED",
        "FAILED",
        "IN_REVIEW",
        "BLOCKED",
        "RECOVERY_REQUIRED",
        "STOPPED",
    }
)


def worktree_key(*, host: str, cwd: str, repository_origin: str | None) -> str:
    payload = "\x1f".join(
        (
            host.strip().casefold(),
            os.path.realpath(cwd),
            (repository_origin or "").strip().casefold(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
