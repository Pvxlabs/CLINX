"""Independent ownership oracle and adapter to the existing Python reducer."""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from runtime_control import RuntimeConflict, RuntimeEventRecord, replay_runtime_events

from .protocol import AUTHORITY


MAX_EVENTS = 1_024
MAX_JSON_DEPTH = 32
MAX_SAFE_INTEGER = 9_007_199_254_740_991
EVENT_FAMILY = "RUNTIME_WORKER_V1"

_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00$"
)
_EVENT_FIELDS = {
    "event_id",
    "stream_type",
    "stream_id",
    "sequence",
    "event_family",
    "event_type",
    "schema_version",
    "occurred_at",
    "recorded_at",
    "payload",
    "payload_hash",
    "global_position",
}


class KernelReferenceError(RuntimeError):
    """Stable Python-side category for invalid experimental kernel input."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _fail(code: str, message: str) -> KernelReferenceError:
    return KernelReferenceError(code, message)


def _path(root: Mapping[str, Any], dotted: str) -> Any:
    value: Any = root
    for part in dotted.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        return False
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo == dt.timezone.utc and parsed.isoformat(timespec="microseconds") == value


def _json_depth(value: Any) -> int:
    if isinstance(value, Mapping):
        return 1 + max((_json_depth(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_json_depth(item) for item in value), default=0)
    return 1


def _decision(decision: str, reason_code: str, details: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "authority": AUTHORITY,
        "decision": decision,
        "reason_code": reason_code,
        "details": dict(details),
    }


def evaluate_ownership_reference(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate the frozen ownership ordering without database or clock access."""
    if not isinstance(payload, Mapping) or set(payload) - {"snapshot", "request"}:
        raise _fail("INVALID_INPUT", "evaluate_ownership payload contains unknown fields")
    snapshot = payload.get("snapshot")
    request = payload.get("request")
    if not isinstance(snapshot, Mapping) or not isinstance(request, Mapping):
        raise _fail("INVALID_INPUT", "snapshot and request must be objects")

    text_paths = (
        "execution.execution_id",
        "execution.task_id",
        "execution.lifecycle",
        "attempt.attempt_id",
        "attempt.execution_id",
        "attempt.lifecycle",
        "worker.worker_id",
        "worker.lifecycle",
        "worker.current_incarnation_id",
        "incarnation.incarnation_id",
        "incarnation.worker_id",
        "incarnation.lifecycle",
        "assignment.assignment_id",
        "assignment.attempt_id",
        "assignment.worker_id",
        "assignment.incarnation_id",
        "assignment.resource_key",
        "assignment.lifecycle",
        "allocation.allocation_id",
        "allocation.assignment_id",
        "allocation.attempt_id",
        "allocation.worker_id",
        "allocation.incarnation_id",
        "allocation.resource_key",
        "allocation.lifecycle",
        "resource.resource_key",
        "clock.source",
        "safety_handoff.state",
        "safety_handoff.assignment_id",
        "safety_handoff.worker_id",
        "safety_handoff.incarnation_id",
        "safety_handoff.attempt_id",
        "safety_handoff.resource_key",
    )
    integer_paths = (
        "assignment.resource_epoch",
        "assignment.version",
        "allocation.resource_epoch",
        "allocation.version",
        "resource.fencing_epoch",
        "resource.version",
        "safety_handoff.resource_epoch",
    )
    timestamp_paths = (
        "assignment.lease_expires_at",
        "allocation.expires_at",
        "clock.now",
        "clock.watermark",
    )
    request_text_paths = (
        "execution_id",
        "attempt_id",
        "worker_id",
        "incarnation_id",
        "assignment_id",
        "allocation_id",
        "resource_key",
    )
    request_integer_paths = (
        "resource_epoch",
        "expected_assignment_version",
        "expected_allocation_version",
        "expected_resource_version",
    )

    missing: list[str] = []
    for path in text_paths:
        value = _path(snapshot, path)
        if value is None or value == "UNKNOWN":
            missing.append(path)
        elif not isinstance(value, str) or not value or len(value) > 512:
            raise _fail("INVALID_INPUT", f"{path} must contain 1..512 bytes")
    for path in integer_paths:
        value = _path(snapshot, path)
        if value is None or value == "UNKNOWN":
            missing.append(path)
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _fail("INVALID_INPUT", f"{path} must be a non-negative integer")
        elif value > MAX_SAFE_INTEGER:
            raise _fail("INTEGER_OUT_OF_RANGE", f"{path} exceeds {MAX_SAFE_INTEGER}")
    for path in timestamp_paths:
        value = _path(snapshot, path)
        if value is None or value == "UNKNOWN":
            missing.append(path)
        elif not _valid_timestamp(value):
            raise _fail("INVALID_INPUT", f"{path} must be canonical UTC with microsecond precision")
    for path in request_text_paths:
        value = _path(request, path)
        if value is None or value == "UNKNOWN":
            missing.append(path)
        elif not isinstance(value, str) or not value or len(value) > 512:
            raise _fail("INVALID_INPUT", f"{path} must contain 1..512 bytes")
    for path in request_integer_paths:
        value = _path(request, path)
        if value is None or value == "UNKNOWN":
            missing.append(path)
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _fail("INVALID_INPUT", f"{path} must be a non-negative integer")
        elif value > MAX_SAFE_INTEGER:
            raise _fail("INTEGER_OUT_OF_RANGE", f"{path} exceeds {MAX_SAFE_INTEGER}")
    if missing:
        return _decision(
            "INSUFFICIENT_EVIDENCE",
            "EVIDENCE_INCOMPLETE",
            {"missing_or_unknown": sorted(set(missing))},
        )

    s = lambda path: _path(snapshot, path)
    r = lambda path: _path(request, path)

    def reject(reason: str, path: str) -> dict[str, Any]:
        return _decision("REJECT_CANDIDATE", reason, {"failed_field": path})

    ordered_checks: tuple[tuple[bool, str, str], ...] = (
        (s("execution.execution_id") != r("execution_id"), "EXECUTION_ID_MISMATCH", "execution.execution_id"),
        (s("execution.lifecycle") == "TERMINAL", "EXECUTION_NOT_LIVE", "execution.lifecycle"),
        (
            s("attempt.execution_id") != s("execution.execution_id")
            or s("attempt.attempt_id") != r("attempt_id"),
            "ATTEMPT_EXECUTION_MISMATCH",
            "attempt.execution_id",
        ),
        (s("attempt.lifecycle") != "ASSIGNED", "ATTEMPT_NOT_ASSIGNED", "attempt.lifecycle"),
        (s("worker.worker_id") != r("worker_id"), "WORKER_ID_MISMATCH", "worker.worker_id"),
        (s("worker.lifecycle") != "REGISTERED", "WORKER_NOT_ACTIVE", "worker.lifecycle"),
        (
            s("worker.current_incarnation_id") != r("incarnation_id"),
            "WORKER_INCARNATION_NOT_CURRENT",
            "worker.current_incarnation_id",
        ),
        (
            s("incarnation.incarnation_id") != r("incarnation_id")
            or s("incarnation.worker_id") != r("worker_id"),
            "INCARNATION_IDENTITY_MISMATCH",
            "incarnation.incarnation_id",
        ),
        (s("incarnation.lifecycle") != "ACTIVE", "INCARNATION_NOT_ACTIVE", "incarnation.lifecycle"),
    )
    for failed, reason, path in ordered_checks:
        if failed:
            return reject(reason, path)

    for snapshot_path, request_path in (
        ("assignment.assignment_id", "assignment_id"),
        ("assignment.attempt_id", "attempt_id"),
        ("assignment.worker_id", "worker_id"),
        ("assignment.incarnation_id", "incarnation_id"),
        ("assignment.resource_key", "resource_key"),
    ):
        if s(snapshot_path) != r(request_path):
            return reject("ASSIGNMENT_IDENTITY_MISMATCH", snapshot_path)
    if s("assignment.lifecycle") != "ACTIVE":
        return reject("ASSIGNMENT_NOT_ACTIVE", "assignment.lifecycle")
    if s("assignment.resource_epoch") != r("resource_epoch"):
        return reject("ASSIGNMENT_EPOCH_MISMATCH", "assignment.resource_epoch")

    for snapshot_path, request_path in (
        ("allocation.allocation_id", "allocation_id"),
        ("allocation.assignment_id", "assignment_id"),
        ("allocation.attempt_id", "attempt_id"),
        ("allocation.worker_id", "worker_id"),
        ("allocation.incarnation_id", "incarnation_id"),
        ("allocation.resource_key", "resource_key"),
    ):
        if s(snapshot_path) != r(request_path):
            return reject("ALLOCATION_IDENTITY_MISMATCH", snapshot_path)
    if s("allocation.lifecycle") != "ACTIVE":
        return reject("ALLOCATION_NOT_ACTIVE", "allocation.lifecycle")
    if s("allocation.resource_epoch") != r("resource_epoch"):
        return reject("ALLOCATION_EPOCH_MISMATCH", "allocation.resource_epoch")
    if s("resource.resource_key") != r("resource_key"):
        return reject("RESOURCE_IDENTITY_MISMATCH", "resource.resource_key")
    if s("resource.fencing_epoch") != r("resource_epoch"):
        return reject("RESOURCE_EPOCH_MISMATCH", "resource.fencing_epoch")
    for snapshot_path, request_path, reason in (
        ("assignment.version", "expected_assignment_version", "ASSIGNMENT_VERSION_MISMATCH"),
        ("allocation.version", "expected_allocation_version", "ALLOCATION_VERSION_MISMATCH"),
        ("resource.version", "expected_resource_version", "RESOURCE_VERSION_MISMATCH"),
    ):
        if s(snapshot_path) != r(request_path):
            return reject(reason, snapshot_path)
    if s("assignment.lease_expires_at") != s("allocation.expires_at"):
        return reject("LEASE_SNAPSHOT_MISMATCH", "allocation.expires_at")
    if s("clock.source") != "COORDINATOR_TRUSTED":
        return reject("CLOCK_SOURCE_UNTRUSTED", "clock.source")
    if s("clock.now") < s("clock.watermark"):
        return reject("CLOCK_WATERMARK_REGRESSION", "clock.now")
    if s("clock.now") >= s("assignment.lease_expires_at"):
        return reject("LEASE_EXPIRED", "assignment.lease_expires_at")
    if s("safety_handoff.state") != "VERIFIED":
        return reject("SAFETY_HANDOFF_NOT_VERIFIED", "safety_handoff.state")
    for snapshot_path, request_path in (
        ("safety_handoff.assignment_id", "assignment_id"),
        ("safety_handoff.worker_id", "worker_id"),
        ("safety_handoff.incarnation_id", "incarnation_id"),
        ("safety_handoff.attempt_id", "attempt_id"),
        ("safety_handoff.resource_key", "resource_key"),
    ):
        if s(snapshot_path) != r(request_path):
            return reject("SAFETY_HANDOFF_OWNER_MISMATCH", snapshot_path)
    if s("safety_handoff.resource_epoch") != r("resource_epoch"):
        return reject("SAFETY_HANDOFF_OWNER_MISMATCH", "safety_handoff.resource_epoch")

    return _decision(
        "ALLOW_CANDIDATE",
        "OWNERSHIP_SNAPSHOT_MATCHES",
        {
            "assignment_version": s("assignment.version"),
            "allocation_version": s("allocation.version"),
            "resource_version": s("resource.version"),
            "resource_epoch": r("resource_epoch"),
            "receipt_used_as_authority": False,
        },
    )


def _normalize(value: Any) -> Any:
    return json.loads(
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    )


def _runtime_error_code(message: str) -> str:
    mapping = (
        ("unknown runtime event family or schema", "EVENT_FAMILY_SCHEMA_UNSUPPORTED"),
        ("multiple streams", "EVENT_STREAM_MIXED"),
        ("version gap", "EVENT_SEQUENCE_GAP"),
        ("invalid for assignment stream", "EVENT_TYPE_INVALID"),
        ("runtime event timestamp is invalid", "EVENT_TIMESTAMP_INVALID"),
        ("recorded before it occurred", "EVENT_RECORDED_BEFORE_OCCURRED"),
        ("payload hash mismatch", "EVENT_PAYLOAD_HASH_MISMATCH"),
        ("illegal assignment transition", "ASSIGNMENT_TRANSITION_INVALID"),
        ("illegal assignment lifecycle", "ASSIGNMENT_TRANSITION_INVALID"),
        ("illegal dependent lifecycle", "ASSIGNMENT_TRANSITION_INVALID"),
        ("unknown state payload version", "ASSIGNMENT_STATE_INVALID"),
        ("identity", "ASSIGNMENT_IDENTITY_INVALID"),
        ("version", "ASSIGNMENT_VERSION_INVALID"),
        ("assignment", "ASSIGNMENT_STATE_INVALID"),
        ("state", "ASSIGNMENT_STATE_INVALID"),
    )
    for fragment, code in mapping:
        if fragment in message:
            return code
    return "REFERENCE_RUNTIME_CONFLICT"


def _event_record(raw: Mapping[str, Any]) -> RuntimeEventRecord:
    unknown = set(raw) - _EVENT_FIELDS
    required = _EVENT_FIELDS - {"global_position"}
    if unknown or required - set(raw):
        raise _fail("INVALID_INPUT", "event envelope has unknown or missing fields")
    for field in ("sequence", "schema_version"):
        value = raw[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _fail("INVALID_INPUT", f"event {field} must be a non-negative integer")
        if value > MAX_SAFE_INTEGER:
            raise _fail("INTEGER_OUT_OF_RANGE", f"event {field} exceeds protocol range")
    global_position = raw.get("global_position")
    if global_position is not None:
        if isinstance(global_position, bool) or not isinstance(global_position, int) or global_position < 0:
            raise _fail("INVALID_INPUT", "event global_position must be a non-negative integer")
        if global_position > MAX_SAFE_INTEGER:
            raise _fail("INTEGER_OUT_OF_RANGE", "event global_position exceeds protocol range")
    for field in ("occurred_at", "recorded_at"):
        if not _valid_timestamp(raw[field]):
            raise _fail("EVENT_TIMESTAMP_INVALID", f"event {field} is invalid")
    if raw["recorded_at"] < raw["occurred_at"]:
        raise _fail("EVENT_RECORDED_BEFORE_OCCURRED", "runtime event was recorded before it occurred")
    return RuntimeEventRecord(**dict(raw))


def replay_assignment_reference(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Replay through the repository reducer after protocol-level validation."""
    if isinstance(events, (str, bytes)) or not isinstance(events, Sequence):
        raise _fail("INVALID_INPUT", "events must be an array")
    if len(events) > MAX_EVENTS:
        raise _fail("INPUT_LIMIT_EXCEEDED", f"events exceeds {MAX_EVENTS}")
    if _json_depth(list(events)) > MAX_JSON_DEPTH + 8:
        raise _fail("INPUT_LIMIT_EXCEEDED", "event nesting exceeds the Python adapter bound")
    records = tuple(_event_record(event) for event in events)
    if any(record.stream_type != "assignment" for record in records):
        raise _fail("EVENT_STREAM_MIXED", "runtime replay contains a non-assignment stream")
    try:
        result = replay_runtime_events(records)
    except RuntimeConflict as exc:
        message = str(exc)
        raise _fail(_runtime_error_code(message), message) from exc
    return _normalize(result)
