"""Provider-neutral, serializable contracts.

These objects describe provider observations and operation results.  They do
not own CLINX task, execution, attempt, lease, or terminal state.
"""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any, Mapping


def _text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_text(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    return _text(name, value)


def _json_value(value: Any) -> Any:
    """Return a bounded, JSON-shaped copy without retaining provider objects."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise TypeError("metadata and payload must contain JSON values")


class CapabilityStatus(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class OutcomeCode(str, Enum):
    ACCEPTED = "accepted"
    OBSERVED = "observed"
    UNSUPPORTED = "unsupported"
    PROTOCOL_ERROR = "protocol_error"
    TRANSPORT_LOSS = "transport_loss"
    TIMEOUT = "timeout"
    CANCEL_REQUESTED = "cancel_requested"
    CANCEL_CONFIRMED = "cancel_confirmed"
    CORRELATION_FAILURE = "correlation_failure"
    SIDE_EFFECT_UNKNOWN = "side_effect_outcome_unknown"
    UNKNOWN_OUTCOME = "unknown_outcome"


class CancelState(str, Enum):
    NOT_REQUESTED = "not_requested"
    REQUESTED = "requested"
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"


class CorrelationStatus(str, Enum):
    EXACT = "exact"
    UNKNOWN = "unknown"
    MISMATCH = "mismatch"
    STALE_GENERATION = "stale_generation"
    HISTORICAL = "historical"


class LifecycleState(str, Enum):
    """Bounded adapter lifecycle states exposed for diagnostics."""

    CREATED = "created"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    CLOSED = "closed"


class EventKind(str, Enum):
    NOTIFICATION = "notification"
    TOOL_REQUEST = "tool_request"
    TURN_STARTED = "turn_started"
    TURN_COMPLETED = "turn_completed"
    UNKNOWN = "unknown"


@dataclasses.dataclass(frozen=True)
class Capability:
    name: str
    status: CapabilityStatus
    source: str
    provider_version: str | None = None
    details: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text("capability name", self.name))
        object.__setattr__(self, "source", _text("capability source", self.source))
        if not isinstance(self.status, CapabilityStatus):
            object.__setattr__(self, "status", CapabilityStatus(self.status))
        object.__setattr__(self, "provider_version", _optional_text("provider_version", self.provider_version))
        object.__setattr__(self, "details", _json_value(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "source": self.source,
            "provider_version": self.provider_version,
            "details": dict(self.details),
        }


@dataclasses.dataclass(frozen=True)
class Capabilities:
    provider_id: str
    items: tuple[Capability, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _text("provider_id", self.provider_id))
        values = tuple(self.items)
        if any(not isinstance(item, Capability) for item in values):
            raise TypeError("capabilities must contain Capability values")
        names = [item.name for item in values]
        if len(names) != len(set(names)):
            raise ValueError("capability names must be unique")
        object.__setattr__(self, "items", values)

    def get(self, name: str) -> Capability:
        for item in self.items:
            if item.name == name:
                return item
        return Capability(name, CapabilityStatus.UNKNOWN, "not_observed")

    def to_dict(self) -> dict[str, Any]:
        return {"provider_id": self.provider_id, "items": [item.to_dict() for item in self.items]}


@dataclasses.dataclass(frozen=True)
class ProviderSessionRef:
    """CLINX identity plus an opaque provider continuity handle."""

    clinx_session_id: str
    provider_id: str
    provider_handle: str | None
    connection_generation: int
    metadata: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "clinx_session_id", _text("clinx_session_id", self.clinx_session_id))
        object.__setattr__(self, "provider_id", _text("provider_id", self.provider_id))
        object.__setattr__(self, "provider_handle", _optional_text("provider_handle", self.provider_handle))
        if not isinstance(self.connection_generation, int) or isinstance(self.connection_generation, bool) or self.connection_generation < 1:
            raise ValueError("connection_generation must be a positive integer")
        object.__setattr__(self, "metadata", _json_value(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "clinx_session_id": self.clinx_session_id,
            "provider_id": self.provider_id,
            "provider_handle": self.provider_handle,
            "connection_generation": self.connection_generation,
            "metadata": dict(self.metadata),
        }


@dataclasses.dataclass(frozen=True)
class OperationContext:
    execution_id: str
    attempt_id: str
    session_id: str
    operation_id: str
    connection_generation: int
    assignment_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("execution_id", "attempt_id", "session_id", "operation_id"):
            object.__setattr__(self, name, _text(name, getattr(self, name)))
        if not isinstance(self.connection_generation, int) or isinstance(self.connection_generation, bool) or self.connection_generation < 1:
            raise ValueError("connection_generation must be a positive integer")
        object.__setattr__(self, "assignment_id", _optional_text("assignment_id", self.assignment_id))

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class DynamicToolConfiguration:
    namespace: str
    name: str
    handler: Any = dataclasses.field(compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "namespace", _text("dynamic tool namespace", self.namespace))
        object.__setattr__(self, "name", _text("dynamic tool name", self.name))
        if not callable(self.handler):
            raise TypeError("dynamic tool handler must be callable")


@dataclasses.dataclass(frozen=True)
class NormalizedEvent:
    receipt_id: str
    provider_id: str
    connection_generation: int
    kind: EventKind
    event_type: str
    operation: OperationContext | None
    correlation: CorrelationStatus
    native_event_id: str | None = None
    local_sequence: int | None = None
    cursor: str | None = None
    terminal_observed: bool = False
    cancel_state: CancelState = CancelState.NOT_REQUESTED
    error_code: str | None = None
    evidence_reference: str | None = None
    duplicate: bool = False
    payload: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    terminal_status: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "receipt_id", _text("receipt_id", self.receipt_id))
        object.__setattr__(self, "provider_id", _text("provider_id", self.provider_id))
        object.__setattr__(self, "event_type", _text("event_type", self.event_type))
        if not isinstance(self.kind, EventKind):
            object.__setattr__(self, "kind", EventKind(self.kind))
        if not isinstance(self.correlation, CorrelationStatus):
            object.__setattr__(self, "correlation", CorrelationStatus(self.correlation))
        if not isinstance(self.cancel_state, CancelState):
            object.__setattr__(self, "cancel_state", CancelState(self.cancel_state))
        if not isinstance(self.connection_generation, int) or self.connection_generation < 1:
            raise ValueError("connection_generation must be a positive integer")
        if self.local_sequence is not None and (not isinstance(self.local_sequence, int) or self.local_sequence < 0):
            raise ValueError("local_sequence must be a non-negative integer")
        object.__setattr__(self, "native_event_id", _optional_text("native_event_id", self.native_event_id))
        object.__setattr__(self, "cursor", _optional_text("cursor", self.cursor))
        object.__setattr__(self, "terminal_status", _optional_text("terminal_status", self.terminal_status))
        object.__setattr__(self, "error_code", _optional_text("error_code", self.error_code))
        object.__setattr__(self, "evidence_reference", _optional_text("evidence_reference", self.evidence_reference))
        object.__setattr__(self, "payload", _json_value(self.payload))

    @property
    def authority_eligible(self) -> bool:
        return (
            self.operation is not None
            and self.correlation is CorrelationStatus.EXACT
            and self.connection_generation == self.operation.connection_generation
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "provider_id": self.provider_id,
            "connection_generation": self.connection_generation,
            "kind": self.kind.value,
            "event_type": self.event_type,
            "operation": self.operation.to_dict() if self.operation else None,
            "correlation": self.correlation.value,
            "native_event_id": self.native_event_id,
            "local_sequence": self.local_sequence,
            "cursor": self.cursor,
            "terminal_observed": self.terminal_observed,
            "terminal_status": self.terminal_status,
            "cancel_state": self.cancel_state.value,
            "error_code": self.error_code,
            "evidence_reference": self.evidence_reference,
            "duplicate": self.duplicate,
            "payload": dict(self.payload),
        }


@dataclasses.dataclass(frozen=True)
class Outcome:
    code: OutcomeCode
    detail: str | None = None
    provider_reference: str | None = None
    side_effect_state: str = "not_proven"

    def __post_init__(self) -> None:
        if not isinstance(self.code, OutcomeCode):
            object.__setattr__(self, "code", OutcomeCode(self.code))
        object.__setattr__(self, "detail", _optional_text("detail", self.detail))
        object.__setattr__(self, "provider_reference", _optional_text("provider_reference", self.provider_reference))
        object.__setattr__(self, "side_effect_state", _text("side_effect_state", self.side_effect_state))

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self) | {"code": self.code.value}


@dataclasses.dataclass(frozen=True)
class AdapterOperation:
    outcome: Outcome
    session: ProviderSessionRef | None = None
    context: OperationContext | None = None
    events: tuple[NormalizedEvent, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))
