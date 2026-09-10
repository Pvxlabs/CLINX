"""Requested-run and terminal-outcome contracts for CLINX V2."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from ._model import (
    DomainModel,
    JsonDocument,
    json_document,
    optional_text,
    require_text,
    string_tuple,
)


@dataclasses.dataclass(frozen=True)
class TerminalOutcome(DomainModel):
    status: str
    code: str | None = None
    summary: str | None = None
    evidence_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", require_text("status", self.status))
        object.__setattr__(self, "code", optional_text("code", self.code))
        object.__setattr__(self, "summary", optional_text("summary", self.summary))
        object.__setattr__(
            self,
            "evidence_references",
            string_tuple("evidence_references", self.evidence_references),
        )


@dataclasses.dataclass(frozen=True)
class ExecutionState(DomainModel):
    lifecycle: str = "REQUESTED"
    terminal_outcome: TerminalOutcome | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "lifecycle", require_text("lifecycle", self.lifecycle))


@dataclasses.dataclass(frozen=True)
class Execution(DomainModel):
    execution_id: str
    task_id: str
    request_snapshot: JsonDocument | Mapping[str, Any]
    execution_policy: JsonDocument | Mapping[str, Any] = dataclasses.field(
        default_factory=JsonDocument.empty
    )
    route_constraints: JsonDocument | Mapping[str, Any] = dataclasses.field(
        default_factory=JsonDocument.empty
    )
    requested_at: str | None = None
    state: ExecutionState = dataclasses.field(default_factory=ExecutionState)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "execution_id", require_text("execution_id", self.execution_id)
        )
        object.__setattr__(self, "task_id", require_text("task_id", self.task_id))
        object.__setattr__(self, "request_snapshot", json_document(self.request_snapshot))
        object.__setattr__(self, "execution_policy", json_document(self.execution_policy))
        object.__setattr__(self, "route_constraints", json_document(self.route_constraints))
        object.__setattr__(
            self, "requested_at", optional_text("requested_at", self.requested_at)
        )
