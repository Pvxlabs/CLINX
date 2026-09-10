"""Durable worker identity and assignment metadata for CLINX V2."""

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
class RuntimeWorkerState(DomainModel):
    lifecycle: str = "REGISTERED"
    heartbeat_at: str | None = None
    assignment_ids: tuple[str, ...] = ()
    assignment_metadata: JsonDocument | Mapping[str, Any] = dataclasses.field(
        default_factory=JsonDocument.empty
    )
    fencing_epoch: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "lifecycle", require_text("lifecycle", self.lifecycle))
        object.__setattr__(
            self, "heartbeat_at", optional_text("heartbeat_at", self.heartbeat_at)
        )
        object.__setattr__(
            self,
            "assignment_ids",
            string_tuple("assignment_ids", self.assignment_ids),
        )
        object.__setattr__(
            self, "assignment_metadata", json_document(self.assignment_metadata)
        )
        if self.fencing_epoch < 0:
            raise ValueError("fencing_epoch must not be negative")


@dataclasses.dataclass(frozen=True)
class RuntimeWorker(DomainModel):
    worker_id: str
    worker_kind: str
    host_reference: str
    capabilities: tuple[str, ...] = ()
    state: RuntimeWorkerState = dataclasses.field(default_factory=RuntimeWorkerState)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "worker_id",
            require_text("worker_id", self.worker_id),
        )
        object.__setattr__(self, "worker_kind", require_text("worker_kind", self.worker_kind))
        object.__setattr__(
            self, "host_reference", require_text("host_reference", self.host_reference)
        )
        object.__setattr__(
            self, "capabilities", string_tuple("capabilities", self.capabilities)
        )
