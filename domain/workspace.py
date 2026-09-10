"""Workspace and resource-scope contracts for CLINX V2."""

from __future__ import annotations

import dataclasses

from ._model import DomainModel, optional_text, require_text


@dataclasses.dataclass(frozen=True)
class WorkspaceState(DomainModel):
    lifecycle: str = "AVAILABLE"
    capacity: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "lifecycle", require_text("lifecycle", self.lifecycle))
        if self.capacity is not None and self.capacity < 0:
            raise ValueError("capacity must not be negative")


@dataclasses.dataclass(frozen=True)
class Workspace(DomainModel):
    workspace_id: str
    project_id: str
    host: str
    checkout: str
    resource_scope: str
    worktree: str | None = None
    runtime_namespace: str | None = None
    state: WorkspaceState = dataclasses.field(default_factory=WorkspaceState)

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_id", require_text("workspace_id", self.workspace_id))
        object.__setattr__(self, "project_id", require_text("project_id", self.project_id))
        object.__setattr__(self, "host", require_text("host", self.host))
        object.__setattr__(self, "checkout", require_text("checkout", self.checkout))
        object.__setattr__(
            self, "resource_scope", require_text("resource_scope", self.resource_scope)
        )
        object.__setattr__(self, "worktree", optional_text("worktree", self.worktree))
        object.__setattr__(
            self,
            "runtime_namespace",
            optional_text("runtime_namespace", self.runtime_namespace),
        )
