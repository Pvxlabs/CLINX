"""Persistence request and result models for the shadow ledger."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from domain import Event, EventActor, JsonDocument
from domain._model import optional_text, require_text

from .errors import LedgerValidationError


def _document(value: JsonDocument | Mapping[str, Any] | None) -> JsonDocument:
    try:
        return JsonDocument.from_value(value)
    except ValueError as exc:
        raise LedgerValidationError(str(exc)) from exc


@dataclasses.dataclass(frozen=True)
class OutboxRequest:
    destination: str
    idempotency_key: str
    payload: JsonDocument | Mapping[str, Any] = dataclasses.field(
        default_factory=JsonDocument.empty
    )

    def __post_init__(self) -> None:
        try:
            object.__setattr__(
                self, "destination", require_text("destination", self.destination)
            )
            object.__setattr__(
                self,
                "idempotency_key",
                require_text("outbox idempotency_key", self.idempotency_key),
            )
        except ValueError as exc:
            raise LedgerValidationError(str(exc)) from exc
        object.__setattr__(self, "payload", _document(self.payload))


@dataclasses.dataclass(frozen=True)
class AppendRequest:
    aggregate_type: str
    aggregate_id: str
    expected_version: int
    event_type: str
    actor: EventActor
    idempotency_scope: str
    idempotency_key: str
    payload: JsonDocument | Mapping[str, Any] = dataclasses.field(
        default_factory=JsonDocument.empty
    )
    schema_version: int = 1
    occurred_at: str | None = None
    causation_id: str | None = None
    correlation_id: str | None = None
    outbox: tuple[OutboxRequest, ...] = ()

    def __post_init__(self) -> None:
        try:
            for name in (
                "aggregate_type",
                "aggregate_id",
                "event_type",
                "idempotency_scope",
                "idempotency_key",
            ):
                object.__setattr__(self, name, require_text(name, getattr(self, name)))
            object.__setattr__(
                self, "occurred_at", optional_text("occurred_at", self.occurred_at)
            )
            object.__setattr__(
                self, "causation_id", optional_text("causation_id", self.causation_id)
            )
            object.__setattr__(
                self,
                "correlation_id",
                optional_text("correlation_id", self.correlation_id),
            )
        except ValueError as exc:
            raise LedgerValidationError(str(exc)) from exc
        if self.expected_version < 0:
            raise LedgerValidationError("expected_version must not be negative")
        if self.schema_version != 1:
            raise LedgerValidationError(
                f"unsupported event schema_version: {self.schema_version}"
            )
        if not self.event_type.startswith("V1") or not self.event_type.endswith("Observed"):
            if self.event_type != "V1SnapshotBaselineImported":
                raise LedgerValidationError(
                    "shadow event_type must describe a V1 observed fact"
                )
        if not isinstance(self.actor, EventActor):
            raise LedgerValidationError("actor must be an EventActor")
        object.__setattr__(self, "payload", _document(self.payload))
        object.__setattr__(self, "outbox", tuple(self.outbox))
        identities = [(item.destination, item.idempotency_key) for item in self.outbox]
        if len(identities) != len(set(identities)):
            raise LedgerValidationError("outbox identities must be unique per append")


@dataclasses.dataclass(frozen=True)
class StoredEvent:
    cursor: int
    event: Event
    semantic_fingerprint: str


@dataclasses.dataclass(frozen=True)
class OutboxRecord:
    cursor: int
    outbox_id: str
    event_id: str
    destination: str
    idempotency_key: str
    payload: JsonDocument
    state: str
    attempts: int
    last_error: str | None
    created_at: str
    updated_at: str


@dataclasses.dataclass(frozen=True)
class AppendResult:
    stored_event: StoredEvent
    outbox: tuple[OutboxRecord, ...]
    duplicate: bool = False


@dataclasses.dataclass(frozen=True)
class InboxReceipt:
    cursor: int
    receipt_id: str
    source: str
    dedup_key: str
    provider_cursor: str | None
    received_at: str
    payload: JsonDocument
    payload_hash: str
    state: str
    normalized_event_id: str | None
    quarantine_reason: str | None


@dataclasses.dataclass(frozen=True)
class IdentityMapping:
    source_system: str
    source_type: str
    source_identity: str
    target_type: str
    target_id: str
    attribution_state: str
    created_at: str


@dataclasses.dataclass(frozen=True)
class ProjectionCheckpoint:
    projection_name: str
    last_cursor: int
    state: str
    updated_at: str
    last_error: str | None
