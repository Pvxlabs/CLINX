"""Human intent and long-lived engineering lifecycle for CLINX V2."""

from __future__ import annotations

import dataclasses

from ._model import DomainModel, optional_text, require_text


@dataclasses.dataclass(frozen=True)
class TaskState(DomainModel):
    lifecycle: str = "OPEN"
    blocker: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "lifecycle", require_text("lifecycle", self.lifecycle))
        object.__setattr__(self, "blocker", optional_text("blocker", self.blocker))
        object.__setattr__(self, "updated_at", optional_text("updated_at", self.updated_at))


@dataclasses.dataclass(frozen=True)
class Task(DomainModel):
    task_id: str
    project_id: str
    intent: str
    objective: str
    workspace_id: str | None = None
    external_reference: str | None = None
    created_at: str | None = None
    state: TaskState = dataclasses.field(default_factory=TaskState)

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", require_text("task_id", self.task_id))
        object.__setattr__(self, "project_id", require_text("project_id", self.project_id))
        object.__setattr__(self, "intent", require_text("intent", self.intent))
        object.__setattr__(self, "objective", require_text("objective", self.objective))
        object.__setattr__(
            self, "workspace_id", optional_text("workspace_id", self.workspace_id)
        )
        object.__setattr__(
            self,
            "external_reference",
            optional_text("external_reference", self.external_reference),
        )
        object.__setattr__(self, "created_at", optional_text("created_at", self.created_at))
