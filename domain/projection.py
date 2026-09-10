"""Rebuildable, non-authoritative read-model contracts for CLINX V2."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from ._model import DomainModel, JsonDocument, json_document, optional_text, require_text


@dataclasses.dataclass(frozen=True)
class ProjectionState(DomainModel):
    lifecycle: str = "PENDING"
    source_event_id: str | None = None
    source_version: int = 0
    checkpoint: str | None = None
    data: JsonDocument | Mapping[str, Any] = dataclasses.field(
        default_factory=JsonDocument.empty
    )
    last_error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "lifecycle", require_text("lifecycle", self.lifecycle))
        object.__setattr__(
            self,
            "source_event_id",
            optional_text("source_event_id", self.source_event_id),
        )
        if self.source_version < 0:
            raise ValueError("source_version must not be negative")
        object.__setattr__(self, "checkpoint", optional_text("checkpoint", self.checkpoint))
        object.__setattr__(self, "data", json_document(self.data))
        object.__setattr__(self, "last_error", optional_text("last_error", self.last_error))


@dataclasses.dataclass(frozen=True)
class Projection(DomainModel):
    projection_id: str
    projection_type: str
    subject_type: str
    subject_id: str
    state: ProjectionState = dataclasses.field(default_factory=ProjectionState)

    def __post_init__(self) -> None:
        for name in ("projection_id", "projection_type", "subject_type", "subject_id"):
            object.__setattr__(self, name, require_text(name, getattr(self, name)))
