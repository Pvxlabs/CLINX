"""Existing Desktop metadata outbox contracts; no task state or replay engine."""

from .errors import (
    AggregateVersionConflict,
    IdempotencyConflict,
    LedgerBusy,
    LedgerSchemaError,
    LedgerValidationError,
    PayloadSizeExceeded,
    ShadowLedgerError,
    TransactionOwnershipError,
)
from .models import AppendRequest, AppendResult, OutboxRecord, OutboxRequest, StoredEvent
from .store import EventStore, UnitOfWork

__all__ = (
    "AggregateVersionConflict", "AppendRequest", "AppendResult", "EventStore",
    "IdempotencyConflict", "LedgerBusy", "LedgerSchemaError", "LedgerValidationError",
    "PayloadSizeExceeded", "OutboxRecord", "OutboxRequest", "ShadowLedgerError",
    "StoredEvent", "TransactionOwnershipError", "UnitOfWork",
)
