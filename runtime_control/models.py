"""Typed snapshots returned by the runtime-control store."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from domain._model import DomainModel, JsonDocument, json_document, require_text


def _doc(value: JsonDocument | Mapping[str, Any] | None) -> JsonDocument:
    return json_document(value)


@dataclasses.dataclass(frozen=True)
class CommandReceipt(DomainModel):
    command_id: str
    idempotency_scope: str
    idempotency_key: str
    original_committed_result: JsonDocument | Mapping[str, Any]
    current_authority_valid: bool | None = None
    duplicate: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "command_id", require_text("command_id", self.command_id))
        object.__setattr__(self, "idempotency_scope", require_text("idempotency_scope", self.idempotency_scope))
        object.__setattr__(self, "idempotency_key", require_text("idempotency_key", self.idempotency_key))
        object.__setattr__(self, "original_committed_result", _doc(self.original_committed_result))

    @property
    def result(self) -> dict[str, Any]:
        return self.original_committed_result.to_dict()

    def __getitem__(self, key: str) -> Any:
        return self.result[key]

    def __getattr__(self, name: str) -> Any:
        if name != "original_committed_result":
            result = self.__dict__.get("original_committed_result")
            if isinstance(result, JsonDocument) and name in result.to_dict():
                return result.to_dict()[name]
        raise AttributeError(name)


@dataclasses.dataclass(frozen=True)
class WorkerRecord(DomainModel):
    worker_id: str
    worker_kind: str
    host_reference: str
    capabilities: tuple[str, ...]
    capacity: int
    lifecycle: str
    version: int
    current_incarnation_id: str | None
    last_heartbeat_at: str | None


@dataclasses.dataclass(frozen=True)
class IncarnationRecord(DomainModel):
    incarnation_id: str
    worker_id: str
    generation: int
    lifecycle: str
    version: int
    started_at: str
    last_heartbeat_at: str | None


@dataclasses.dataclass(frozen=True)
class ExecutionRecord(DomainModel):
    execution_id: str
    task_id: str
    request_snapshot: JsonDocument | Mapping[str, Any]
    execution_policy: JsonDocument | Mapping[str, Any]
    route_constraints: JsonDocument | Mapping[str, Any]
    lifecycle: str
    version: int
    created_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_snapshot", _doc(self.request_snapshot))
        object.__setattr__(self, "execution_policy", _doc(self.execution_policy))
        object.__setattr__(self, "route_constraints", _doc(self.route_constraints))


@dataclasses.dataclass(frozen=True)
class AttemptRecord(DomainModel):
    attempt_id: str
    execution_id: str
    retry_index: int
    provider_session_id: str | None
    lifecycle: str
    version: int
    created_at: str


@dataclasses.dataclass(frozen=True)
class AssignmentRecord(DomainModel):
    assignment_id: str
    attempt_id: str
    worker_id: str
    incarnation_id: str
    resource_key: str
    resource_epoch: int
    lifecycle: str
    version: int
    lease_expires_at: str
    created_at: str
    allocation_id: str | None = None


@dataclasses.dataclass(frozen=True)
class AllocationRecord(DomainModel):
    allocation_id: str
    assignment_id: str
    attempt_id: str
    worker_id: str
    incarnation_id: str
    resource_key: str
    resource_epoch: int
    lifecycle: str
    version: int
    expires_at: str
    release_reason: str | None = None


@dataclasses.dataclass(frozen=True)
class RuntimeEventRecord(DomainModel):
    event_id: str
    stream_type: str
    stream_id: str
    sequence: int
    event_family: str
    event_type: str
    schema_version: int
    occurred_at: str
    recorded_at: str
    payload: JsonDocument | Mapping[str, Any]
    payload_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _doc(self.payload))


@dataclasses.dataclass(frozen=True)
class RecoveryRecord(DomainModel):
    assignment_id: str
    state: str
    reason: str
    attempts: int
    updated_at: str


@dataclasses.dataclass(frozen=True)
class ProtectedResourceRecord(DomainModel):
    resource_key: str
    fencing_epoch: int
    value: JsonDocument | Mapping[str, Any] | None
    version: int

    def __post_init__(self) -> None:
        if self.value is not None:
            object.__setattr__(self, "value", _doc(self.value))
