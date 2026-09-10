"""CLINX V2 shadow event ledger with V1 authority preserved."""

from .errors import (
    AggregateVersionConflict,
    IdempotencyConflict,
    IdentityMappingConflict,
    LedgerBusy,
    LedgerSchemaError,
    LedgerValidationError,
    PayloadSizeExceeded,
    ReplayBoundaryExceeded,
    ReplayError,
    ShadowLedgerError,
    TransactionOwnershipError,
)
from .models import (
    AppendRequest,
    AppendResult,
    IdentityMapping,
    InboxReceipt,
    OutboxRecord,
    OutboxRequest,
    ProjectionCheckpoint,
    StoredEvent,
)
from .store import EventStore, UnitOfWork

__all__ = (
    "AggregateVersionConflict",
    "AppendRequest",
    "AppendResult",
    "EventStore",
    "IdempotencyConflict",
    "IdentityMapping",
    "IdentityMappingConflict",
    "InboxReceipt",
    "LedgerBusy",
    "LedgerSchemaError",
    "LedgerValidationError",
    "PayloadSizeExceeded",
    "OutboxRecord",
    "OutboxRequest",
    "ProjectionCheckpoint",
    "ReplayBoundaryExceeded",
    "ReplayError",
    "ShadowLedgerError",
    "StoredEvent",
    "TransactionOwnershipError",
    "UnitOfWork",
)
