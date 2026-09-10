"""Append-only lifecycle fact contract for CLINX V2."""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Mapping
from typing import Any

from ._model import (
    DomainModel,
    DomainValidationError,
    JsonDocument,
    json_document,
    optional_text,
    require_text,
)


@dataclasses.dataclass(frozen=True)
class EventActor(DomainModel):
    actor_type: str
    actor_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "actor_type", require_text("actor_type", self.actor_type))
        object.__setattr__(self, "actor_id", require_text("actor_id", self.actor_id))


@dataclasses.dataclass(frozen=True)
class Event(DomainModel):
    event_id: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    event_type: str
    schema_version: int
    occurred_at: str
    recorded_at: str
    actor: EventActor
    idempotency_scope: str
    idempotency_key: str
    payload: JsonDocument | Mapping[str, Any] = dataclasses.field(
        default_factory=JsonDocument.empty
    )
    payload_hash: str | None = None
    causation_id: str | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "event_id",
            "aggregate_type",
            "aggregate_id",
            "event_type",
            "occurred_at",
            "recorded_at",
            "idempotency_scope",
            "idempotency_key",
        ):
            object.__setattr__(self, name, require_text(name, getattr(self, name)))
        if not isinstance(self.actor, EventActor):
            raise DomainValidationError("actor must be an EventActor")
        if self.aggregate_version < 1:
            raise ValueError("aggregate_version must be positive")
        if self.schema_version < 1:
            raise ValueError("schema_version must be positive")
        payload = json_document(self.payload)
        object.__setattr__(self, "payload", payload)
        calculated_hash = hashlib.sha256(payload.to_json().encode("utf-8")).hexdigest()
        if self.payload_hash is not None and self.payload_hash != calculated_hash:
            raise DomainValidationError("payload_hash does not match canonical payload")
        object.__setattr__(self, "payload_hash", calculated_hash)
        for name in ("causation_id", "correlation_id"):
            object.__setattr__(self, name, optional_text(name, getattr(self, name)))
