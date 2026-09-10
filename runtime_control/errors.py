"""Errors raised by the explicitly initialized runtime-control boundary."""

from __future__ import annotations


class RuntimeControlError(RuntimeError):
    """Base class for runtime-control contract failures."""


class RuntimeSchemaError(RuntimeControlError):
    """The additive runtime schema is absent or unsupported."""


class RuntimeBusy(RuntimeControlError):
    """SQLite could not acquire the bounded coordinator write transaction."""


class RuntimeNotFound(RuntimeControlError):
    """A required runtime entity does not exist."""


class RuntimeConflict(RuntimeControlError):
    """A durable identity or state transition conflicts with existing data."""


class RuntimeIdempotencyConflict(RuntimeConflict):
    """An idempotency key was reused for different command semantics."""


class RuntimeVersionConflict(RuntimeConflict):
    """An expected aggregate or resource version is stale."""


class RuntimeAuthorizationError(RuntimeControlError):
    """The supplied durable ownership tuple is not currently authorized."""


class StaleMutation(RuntimeAuthorizationError):
    """A revoked, expired, or superseded owner attempted a mutation."""


class LeaseExpired(StaleMutation):
    """The coordinator clock is at or beyond the assignment expiry."""


class CapacityExceeded(RuntimeConflict):
    """Worker or resource admission would exceed a durable limit."""


class ResourceBusy(CapacityExceeded):
    """A resource is active or quarantined under another allocation."""


class InvalidTransition(RuntimeConflict):
    """An entity state cannot make the requested transition."""


class RecoveryBlocked(RuntimeControlError):
    """Recovery lacks explicit proof that old side effects are fenced."""


class ClockAnomaly(RuntimeControlError):
    """The coordinator clock moved backwards relative to persisted time."""


class PayloadSizeExceeded(RuntimeControlError):
    """A runtime event or evidence payload exceeds its byte budget."""


class FaultInjected(RuntimeControlError):
    """A controlled qualification fault was injected."""
