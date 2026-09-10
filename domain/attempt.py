"""Concrete execution-realization contracts for CLINX V2."""

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
class AttemptState(DomainModel):
    lifecycle: str = "PENDING"
    provider_session_id: str | None = None
    worker_id: str | None = None
    assignment_epoch: int | None = None
    provider_correlation: JsonDocument | Mapping[str, Any] = dataclasses.field(
        default_factory=JsonDocument.empty
    )
    evidence_references: tuple[str, ...] = ()
    outcome: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "lifecycle", require_text("lifecycle", self.lifecycle))
        object.__setattr__(
            self,
            "provider_session_id",
            optional_text("provider_session_id", self.provider_session_id),
        )
        object.__setattr__(
            self,
            "worker_id",
            optional_text("worker_id", self.worker_id),
        )
        if self.assignment_epoch is not None and self.assignment_epoch < 0:
            raise ValueError("assignment_epoch must not be negative")
        object.__setattr__(
            self, "provider_correlation", json_document(self.provider_correlation)
        )
        object.__setattr__(
            self,
            "evidence_references",
            string_tuple("evidence_references", self.evidence_references),
        )
        object.__setattr__(self, "outcome", optional_text("outcome", self.outcome))


@dataclasses.dataclass(frozen=True)
class Attempt(DomainModel):
    attempt_id: str
    execution_id: str
    retry_index: int
    created_at: str | None = None
    state: AttemptState = dataclasses.field(default_factory=AttemptState)

    def __post_init__(self) -> None:
        object.__setattr__(self, "attempt_id", require_text("attempt_id", self.attempt_id))
        object.__setattr__(
            self, "execution_id", require_text("execution_id", self.execution_id)
        )
        if self.retry_index < 0:
            raise ValueError("retry_index must not be negative")
        object.__setattr__(self, "created_at", optional_text("created_at", self.created_at))
