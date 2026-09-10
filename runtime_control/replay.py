"""Validated runtime event reducers with explicit legacy upcasts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from .clock import decode_time
from .errors import RuntimeConflict
from .models import RuntimeEventRecord


EVENT_FAMILY = "RUNTIME_WORKER_V1"

_ASSIGNMENT_LIFECYCLES = {
    "AssignmentGranted": "ACTIVE",
    "AssignmentRenewed": "ACTIVE",
    "AssignmentReleased": "RELEASED",
    "AssignmentRevoked": "REVOKED",
    "AssignmentExpired": "EXPIRED",
    "AssignmentOrphaned": "ORPHANED",
    "AssignmentRecovered": "RECOVERED",
}
_ASSIGNMENT_PREVIOUS = {
    "AssignmentGranted": {None},
    "AssignmentRenewed": {"ACTIVE"},
    "AssignmentReleased": {"ACTIVE"},
    "AssignmentRevoked": {"ACTIVE"},
    "AssignmentExpired": {"ACTIVE"},
    "AssignmentOrphaned": {"ACTIVE"},
    "AssignmentRecovered": {"EXPIRED", "ORPHANED"},
}
_STREAM_EVENTS = {
    "task": {"TaskReferenceRegistered"},
    "execution": {"ExecutionRegistered"},
    "attempt": {"AttemptRegistered", "AttemptEvidenceRecorded"},
    "worker": {"WorkerRegistered", "WorkerIncarnationRegistered"},
    "incarnation": {"WorkerHeartbeatRecorded"},
    "assignment": set(_ASSIGNMENT_LIFECYCLES),
    "resource": {"ProtectedResourceMutated"},
}


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _payload(event: RuntimeEventRecord) -> dict[str, Any]:
    value = event.payload.to_dict()
    if not isinstance(value, dict):
        raise RuntimeConflict("runtime event payload must be an object")
    digest = hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()
    if digest != event.payload_hash:
        raise RuntimeConflict("runtime event payload hash mismatch")
    return value


def _required(mapping: Mapping[str, Any], fields: Sequence[str], context: str) -> None:
    missing = [field for field in fields if field not in mapping]
    if missing:
        raise RuntimeConflict(f"{context} is missing required fields: {missing}")


def _text(mapping: Mapping[str, Any], field: str, context: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value:
        raise RuntimeConflict(f"{context}.{field} must be non-empty text")
    return value


def _integer(mapping: Mapping[str, Any], field: str, context: str) -> int:
    value = mapping.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeConflict(f"{context}.{field} must be a non-negative integer")
    return value


def _legacy_assignment_state(
    event: RuntimeEventRecord,
    payload: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
) -> dict[str, Any]:
    event_type = event.event_type
    if event_type == "AssignmentGranted":
        fields = (
            "assignment_id", "attempt_id", "worker_id", "incarnation_id",
            "resource_key", "resource_epoch", "lease_expires_at", "allocation_id",
        )
        _required(payload, fields, event_type)
        assignment_id = _text(payload, "assignment_id", event_type)
        attempt_id = _text(payload, "attempt_id", event_type)
        worker_id = _text(payload, "worker_id", event_type)
        incarnation_id = _text(payload, "incarnation_id", event_type)
        resource_key = _text(payload, "resource_key", event_type)
        allocation_id = _text(payload, "allocation_id", event_type)
        epoch = _integer(payload, "resource_epoch", event_type)
        if epoch < 1:
            raise RuntimeConflict("AssignmentGranted.resource_epoch must be positive")
        lease = _text(payload, "lease_expires_at", event_type)
        decode_time(lease)
        return {
            "state_version": 1,
            "assignment": {
                "assignment_id": assignment_id,
                "attempt_id": attempt_id,
                "worker_id": worker_id,
                "incarnation_id": incarnation_id,
                "resource_key": resource_key,
                "resource_epoch": epoch,
                "lifecycle": "ACTIVE",
                "version": 0,
                "lease_expires_at": lease,
                "created_at": event.occurred_at,
                "released_at": None,
            },
            "allocation": {
                "allocation_id": allocation_id,
                "assignment_id": assignment_id,
                "attempt_id": attempt_id,
                "worker_id": worker_id,
                "incarnation_id": incarnation_id,
                "resource_key": resource_key,
                "resource_epoch": epoch,
                "lifecycle": "ACTIVE",
                "version": 0,
                "expires_at": lease,
                "release_reason": None,
            },
            "attempt": {"attempt_id": attempt_id, "lifecycle": "ASSIGNED", "version": 1},
            "recovery": None,
        }
    if previous is None:
        raise RuntimeConflict(f"{event_type} has no preceding AssignmentGranted")
    _required(payload, ("assignment_id",), event_type)
    if payload["assignment_id"] != previous["assignment"]["assignment_id"]:
        raise RuntimeConflict("legacy assignment event identity changed")
    state = json.loads(_canonical(previous))
    assignment = state["assignment"]
    allocation = state["allocation"]
    attempt = state["attempt"]
    assignment["version"] += 1
    assignment["lifecycle"] = _ASSIGNMENT_LIFECYCLES[event_type]
    if event_type == "AssignmentRenewed":
        _required(payload, ("lease_expires_at",), event_type)
        lease = _text(payload, "lease_expires_at", event_type)
        decode_time(lease)
        assignment["lease_expires_at"] = lease
        allocation["expires_at"] = lease
        allocation["version"] += 1
    elif event_type == "AssignmentOrphaned":
        allocation["lifecycle"] = "QUARANTINED"
        allocation["release_reason"] = "worker_incarnation_superseded"
        state["recovery"] = {
            "assignment_id": assignment["assignment_id"],
            "state": "PENDING",
            "reason": "worker incarnation superseded",
            "attempts": 0,
        }
    elif event_type == "AssignmentExpired":
        allocation["lifecycle"] = "QUARANTINED"
        allocation["release_reason"] = "lease_expired"
        allocation["version"] += 1
        state["recovery"] = {
            "assignment_id": assignment["assignment_id"],
            "state": "PENDING",
            "reason": "lease expired; process death not proven",
            "attempts": 0,
        }
    elif event_type in {"AssignmentReleased", "AssignmentRevoked"}:
        reason = _text(payload, "reason", event_type)
        allocation["lifecycle"] = "RELEASED"
        allocation["release_reason"] = reason
        allocation["version"] += 1
        assignment["released_at"] = event.occurred_at
        attempt["lifecycle"] = "RELEASED" if event_type == "AssignmentReleased" else "PENDING"
        attempt["version"] += 1
    elif event_type == "AssignmentRecovered":
        allocation["lifecycle"] = "RELEASED"
        allocation["release_reason"] = "controlled_recovery"
        allocation["version"] += 1
        assignment["released_at"] = event.occurred_at
        attempt["lifecycle"] = "PENDING"
        attempt["version"] += 1
        recovery = state.get("recovery")
        if not isinstance(recovery, dict):
            raise RuntimeConflict("AssignmentRecovered requires pending recovery state")
        recovery.update({"state": "DONE", "attempts": int(recovery["attempts"]) + 1})
    return state


def _validate_assignment_state(
    event: RuntimeEventRecord,
    payload: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    raw = payload.get("state")
    legacy = raw is None
    if raw is None:
        raw = _legacy_assignment_state(event, payload, previous)
    if not isinstance(raw, dict) or raw.get("state_version") != 1:
        raise RuntimeConflict("assignment event has unknown state payload version")
    _required(raw, ("assignment", "allocation", "attempt", "recovery"), "state")
    assignment = raw["assignment"]
    allocation = raw["allocation"]
    attempt = raw["attempt"]
    recovery = raw["recovery"]
    if not all(isinstance(item, dict) for item in (assignment, allocation, attempt)):
        raise RuntimeConflict("assignment event state objects are malformed")
    assignment_fields = (
        "assignment_id", "attempt_id", "worker_id", "incarnation_id", "resource_key",
        "resource_epoch", "lifecycle", "version", "lease_expires_at", "created_at", "released_at",
    )
    allocation_fields = (
        "allocation_id", "assignment_id", "attempt_id", "worker_id", "incarnation_id",
        "resource_key", "resource_epoch", "lifecycle", "version", "expires_at", "release_reason",
    )
    _required(assignment, assignment_fields, "state.assignment")
    _required(allocation, allocation_fields, "state.allocation")
    _required(attempt, ("attempt_id", "lifecycle", "version"), "state.attempt")
    identity_fields = ("assignment_id", "attempt_id", "worker_id", "incarnation_id", "resource_key", "resource_epoch")
    for field in identity_fields:
        if assignment[field] != allocation[field]:
            raise RuntimeConflict(f"assignment/allocation identity mismatch: {field}")
    if assignment["assignment_id"] != event.stream_id or assignment["assignment_id"] != payload.get("assignment_id"):
        raise RuntimeConflict("assignment event stream identity mismatch")
    if attempt["attempt_id"] != assignment["attempt_id"]:
        raise RuntimeConflict("assignment/attempt identity mismatch")
    for field in ("assignment_id", "attempt_id", "worker_id", "incarnation_id", "resource_key"):
        _text(assignment, field, "state.assignment")
    _text(allocation, "allocation_id", "state.allocation")
    _text(attempt, "attempt_id", "state.attempt")
    epoch = _integer(assignment, "resource_epoch", "state.assignment")
    assignment_version = _integer(assignment, "version", "state.assignment")
    allocation_version = _integer(allocation, "version", "state.allocation")
    attempt_version = _integer(attempt, "version", "state.attempt")
    if epoch < 1:
        raise RuntimeConflict("assignment resource epoch must be positive")
    decode_time(_text(assignment, "lease_expires_at", "state.assignment"))
    decode_time(_text(assignment, "created_at", "state.assignment"))
    if allocation["expires_at"] != assignment["lease_expires_at"]:
        raise RuntimeConflict("assignment/allocation lease mismatch")
    expected_lifecycle = _ASSIGNMENT_LIFECYCLES[event.event_type]
    payload_lifecycle = payload.get("lifecycle", expected_lifecycle if legacy else None)
    if assignment["lifecycle"] != expected_lifecycle or payload_lifecycle != expected_lifecycle:
        raise RuntimeConflict(f"{event.event_type} has illegal assignment lifecycle")
    expected_allocation = {
        "AssignmentGranted": "ACTIVE",
        "AssignmentRenewed": "ACTIVE",
        "AssignmentOrphaned": "QUARANTINED",
        "AssignmentExpired": "QUARANTINED",
        "AssignmentRecovered": "RELEASED",
        "AssignmentReleased": "RELEASED",
        "AssignmentRevoked": "RELEASED",
    }[event.event_type]
    expected_attempt = {
        "AssignmentGranted": "ASSIGNED",
        "AssignmentRenewed": "ASSIGNED",
        "AssignmentOrphaned": "ASSIGNED",
        "AssignmentExpired": "ASSIGNED",
        "AssignmentRecovered": "PENDING",
        "AssignmentReleased": "RELEASED",
        "AssignmentRevoked": "PENDING",
    }[event.event_type]
    if allocation["lifecycle"] != expected_allocation or attempt["lifecycle"] != expected_attempt:
        raise RuntimeConflict(f"{event.event_type} has illegal dependent lifecycle")
    if event.event_type in {"AssignmentOrphaned", "AssignmentExpired"}:
        if not isinstance(recovery, dict) or recovery.get("state") != "PENDING":
            raise RuntimeConflict(f"{event.event_type} requires pending recovery state")
    elif event.event_type == "AssignmentRecovered":
        if not isinstance(recovery, dict) or recovery.get("state") != "DONE":
            raise RuntimeConflict("AssignmentRecovered requires done recovery state")
    elif recovery is not None:
        raise RuntimeConflict(f"{event.event_type} cannot introduce recovery state")
    if recovery is not None:
        _required(recovery, ("assignment_id", "state", "reason", "attempts"), "state.recovery")
        if recovery["assignment_id"] != assignment["assignment_id"]:
            raise RuntimeConflict("assignment/recovery identity mismatch")
        _text(recovery, "reason", "state.recovery")
        recovery_attempts = _integer(recovery, "attempts", "state.recovery")
        if event.event_type in {"AssignmentOrphaned", "AssignmentExpired"} and recovery_attempts != 0:
            raise RuntimeConflict("new recovery work must start with zero attempts")
        if event.event_type == "AssignmentRecovered" and recovery_attempts < 1:
            raise RuntimeConflict("completed recovery must record an attempt")
    if previous is None:
        if (
            event.event_type != "AssignmentGranted"
            or (assignment_version, allocation_version) != (0, 0)
            or attempt_version < 1
        ):
            raise RuntimeConflict("assignment stream has invalid initial versions")
    else:
        old_assignment = previous["assignment"]
        old_allocation = previous["allocation"]
        old_attempt = previous["attempt"]
        for field in identity_fields:
            if assignment[field] != old_assignment[field]:
                raise RuntimeConflict(f"assignment identity changed during replay: {field}")
        if assignment_version != old_assignment["version"] + 1:
            raise RuntimeConflict("assignment version did not advance exactly once")
        allocation_delta = int(not (legacy and event.event_type == "AssignmentOrphaned"))
        attempt_delta = int(event.event_type in {"AssignmentRecovered", "AssignmentReleased", "AssignmentRevoked"})
        if allocation_version != old_allocation["version"] + allocation_delta:
            raise RuntimeConflict("allocation version did not advance exactly once")
        if attempt_version != old_attempt["version"] + attempt_delta:
            raise RuntimeConflict("attempt version transition is invalid")
    known_state = {"state_version", "assignment", "allocation", "attempt", "recovery"}
    unknown = [f"state.{key}" for key in raw if key not in known_state]
    unknown.extend(
        f"state.assignment.{key}" for key in assignment if key not in assignment_fields
    )
    unknown.extend(
        f"state.allocation.{key}" for key in allocation if key not in allocation_fields
    )
    attempt_fields = {"attempt_id", "lifecycle", "version"}
    unknown.extend(f"state.attempt.{key}" for key in attempt if key not in attempt_fields)
    recovery_fields = {"assignment_id", "state", "reason", "attempts"}
    if recovery is not None:
        unknown.extend(
            f"state.recovery.{key}" for key in recovery if key not in recovery_fields
        )
    if not legacy:
        top_fields = {"assignment_id", "lifecycle", "state", "reason"}
        unknown.extend(f"payload.{key}" for key in payload if key not in top_fields)
    clean = {
        "state_version": 1,
        "assignment": {field: assignment[field] for field in assignment_fields},
        "allocation": {field: allocation[field] for field in allocation_fields},
        "attempt": {field: attempt[field] for field in attempt_fields},
        "recovery": (
            {field: recovery[field] for field in recovery_fields}
            if recovery is not None
            else None
        ),
    }
    return json.loads(_canonical(clean)), tuple(sorted(set(unknown)))


def _replay_assignment(
    events: tuple[RuntimeEventRecord, ...],
    payloads: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    state: dict[str, Any] | None = None
    unknown: list[str] = []
    for event, payload in zip(events, payloads, strict=True):
        previous_lifecycle = state["assignment"]["lifecycle"] if state else None
        if previous_lifecycle not in _ASSIGNMENT_PREVIOUS[event.event_type]:
            raise RuntimeConflict(
                f"illegal assignment transition {previous_lifecycle!r} -> {event.event_type}"
            )
        state, event_unknown = _validate_assignment_state(event, payload, state)
        unknown.extend(event_unknown)
    assert state is not None
    return state, tuple(sorted(set(unknown)))


def _replay_worker(
    events: tuple[RuntimeEventRecord, ...],
    payloads: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    worker: dict[str, Any] | None = None
    incarnations: dict[str, dict[str, Any]] = {}
    unknown: set[str] = set()

    def version_or_unknown(payload: Mapping[str, Any], field: str, context: str) -> int | str:
        if field not in payload:
            unknown.add(context)
            return "UNKNOWN"
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeConflict(f"{context} must be a non-negative integer")
        return value

    def advance_version(
        current: int | str,
        supplied: int | str,
        field: str,
    ) -> int | str:
        if supplied == "UNKNOWN":
            unknown.add(field)
            return "UNKNOWN"
        if isinstance(current, int) and supplied != current + 1:
            raise RuntimeConflict(f"{field} did not advance exactly once")
        return supplied

    for event, payload in zip(events, payloads, strict=True):
        if event.event_type == "WorkerRegistered":
            if worker is not None:
                raise RuntimeConflict("worker stream contains duplicate registration")
            _required(payload, ("worker_id", "capacity"), event.event_type)
            worker = {
                "worker_id": _text(payload, "worker_id", event.event_type),
                "worker_kind": "UNKNOWN",
                "host_reference": "UNKNOWN",
                "capabilities": "NOT_COVERED",
                "capacity": _integer(payload, "capacity", event.event_type),
                "lifecycle": "REGISTERED",
                "version": version_or_unknown(payload, "worker_version", "worker.version"),
                "current_incarnation_id": None,
                "last_heartbeat_at": "NOT_COVERED",
            }
            continue
        if event.event_type == "WorkerHeartbeatRecorded":
            if worker is None:
                raise RuntimeConflict("worker heartbeat precedes worker registration")
            _required(payload, ("worker_id", "incarnation_id"), event.event_type)
            if payload["worker_id"] != worker["worker_id"]:
                raise RuntimeConflict("worker heartbeat identity changed")
            incarnation_id = _text(payload, "incarnation_id", event.event_type)
            incarnation = incarnations.get(incarnation_id)
            if incarnation is None or worker["current_incarnation_id"] != incarnation_id:
                raise RuntimeConflict("worker heartbeat is not for the current incarnation")
            worker_version = version_or_unknown(payload, "worker_version", "worker.version")
            incarnation_version = version_or_unknown(
                payload, "incarnation_version", f"incarnation[{incarnation_id}].version"
            )
            worker["version"] = advance_version(worker["version"], worker_version, "worker.version")
            incarnation["version"] = advance_version(
                incarnation["version"], incarnation_version, f"incarnation[{incarnation_id}].version"
            )
            heartbeat_at = payload.get("heartbeat_at")
            if not isinstance(heartbeat_at, str):
                unknown.add("worker.last_heartbeat_at")
                unknown.add(f"incarnation[{incarnation_id}].last_heartbeat_at")
            else:
                try:
                    decode_time(heartbeat_at)
                except (TypeError, ValueError) as exc:
                    raise RuntimeConflict("WorkerHeartbeatRecorded.heartbeat_at is invalid") from exc
                worker["last_heartbeat_at"] = heartbeat_at
                incarnation["last_heartbeat_at"] = heartbeat_at
            continue
        if event.event_type != "WorkerIncarnationRegistered":
            raise RuntimeConflict(f"unknown worker event type: {event.event_type}")
        if worker is None:
            raise RuntimeConflict("worker incarnation precedes worker registration")
        _required(payload, ("worker_id", "incarnation_id", "generation"), event.event_type)
        if payload["worker_id"] != worker["worker_id"]:
            raise RuntimeConflict("worker event identity changed")
        incarnation_id = _text(payload, "incarnation_id", event.event_type)
        generation = _integer(payload, "generation", event.event_type)
        maximum_generation = max(
            (item["generation"] for item in incarnations.values()),
            default=0,
        )
        if generation <= maximum_generation or incarnation_id in incarnations:
            raise RuntimeConflict("worker incarnation identity or generation is invalid")
        previous = worker["current_incarnation_id"]
        if previous is not None:
            supplied_previous = payload.get("superseded_incarnation_id", previous)
            if supplied_previous != previous:
                raise RuntimeConflict("superseded incarnation identity changed")
            incarnations[previous]["lifecycle"] = "SUPERSEDED"
            previous_version = payload.get("superseded_incarnation_version")
            if "superseded_incarnation_version" not in payload:
                unknown.add(f"incarnation[{previous}].version")
                incarnations[previous]["version"] = "UNKNOWN"
            elif isinstance(previous_version, int) and not isinstance(previous_version, bool):
                if isinstance(incarnations[previous]["version"], int) and previous_version != incarnations[previous]["version"] + 1:
                    raise RuntimeConflict("superseded incarnation version did not advance exactly once")
                incarnations[previous]["version"] = previous_version
            else:
                raise RuntimeConflict(
                    f"incarnation[{previous}].version must be a non-negative integer"
                )
        worker_version = version_or_unknown(payload, "worker_version", "worker.version")
        incarnation_version = version_or_unknown(
            payload, "incarnation_version", f"incarnation[{incarnation_id}].version"
        )
        worker["version"] = advance_version(worker["version"], worker_version, "worker.version")
        incarnations[incarnation_id] = {
            "incarnation_id": incarnation_id,
            "worker_id": worker["worker_id"],
            "generation": generation,
            "lifecycle": "ACTIVE",
            "version": incarnation_version,
            "started_at": event.occurred_at,
            "last_heartbeat_at": "NOT_COVERED",
        }
        worker["current_incarnation_id"] = incarnation_id
    assert worker is not None
    unknown.update(("worker.worker_kind", "worker.host_reference", "worker.capabilities"))
    if worker["last_heartbeat_at"] == "NOT_COVERED":
        unknown.add("worker.last_heartbeat_at")
    for incarnation_id, incarnation in incarnations.items():
        if incarnation["last_heartbeat_at"] == "NOT_COVERED":
            unknown.add(f"incarnation[{incarnation_id}].last_heartbeat_at")
    return {"worker": worker, "incarnations": incarnations}, tuple(sorted(unknown))


def _replay_simple(
    stream_type: str,
    stream_id: str,
    events: tuple[RuntimeEventRecord, ...],
    payloads: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    identity_field = {
        "task": "task_id",
        "execution": "execution_id",
        "attempt": "attempt_id",
        "incarnation": "incarnation_id",
        "resource": "resource_key",
    }[stream_type]
    for event, payload in zip(events, payloads, strict=True):
        if _text(payload, identity_field, event.event_type) != stream_id:
            raise RuntimeConflict("runtime event payload identity does not match stream")
    event_types = tuple(event.event_type for event in events)
    if stream_type in {"task", "execution"} and len(events) != 1:
        raise RuntimeConflict(f"{stream_type} registration stream contains extra events")
    if stream_type == "attempt" and event_types[0] != "AttemptRegistered":
        raise RuntimeConflict("attempt evidence precedes attempt registration")
    return {
        "identity": stream_id,
        "last_event_type": events[-1].event_type,
        "event_count": len(events),
    }, ("aggregate_fields",)


def _validate_stream(
    events: tuple[RuntimeEventRecord, ...] | list[RuntimeEventRecord],
    *,
    expected_type: str | None = None,
    expected_id: str | None = None,
) -> tuple[tuple[RuntimeEventRecord, ...], tuple[dict[str, Any], ...]]:
    ordered = tuple(events)
    if not ordered:
        return (), ()
    first = ordered[0]
    if expected_type is not None and first.stream_type != expected_type:
        raise RuntimeConflict(f"expected {expected_type} stream, got {first.stream_type}")
    if expected_id is not None and first.stream_id != expected_id:
        raise RuntimeConflict("runtime event stream identity changed")
    if first.stream_type not in _STREAM_EVENTS:
        raise RuntimeConflict(f"unknown runtime stream type: {first.stream_type}")
    payloads: list[dict[str, Any]] = []
    for expected, event in enumerate(ordered, start=1):
        if event.event_family != EVENT_FAMILY or event.schema_version != 1:
            raise RuntimeConflict("unknown runtime event family or schema")
        if event.stream_type != first.stream_type or event.stream_id != first.stream_id:
            raise RuntimeConflict("runtime replay contains multiple streams")
        if event.sequence != expected:
            raise RuntimeConflict("runtime event stream has a version gap")
        if event.event_type not in _STREAM_EVENTS[first.stream_type]:
            raise RuntimeConflict(
                f"event type {event.event_type} is invalid for {first.stream_type} stream"
            )
        try:
            occurred_at = decode_time(event.occurred_at)
            recorded_at = decode_time(event.recorded_at)
        except (TypeError, ValueError) as exc:
            raise RuntimeConflict("runtime event timestamp is invalid") from exc
        if recorded_at < occurred_at:
            raise RuntimeConflict("runtime event was recorded before it occurred")
        payloads.append(_payload(event))
    return ordered, tuple(payloads)


def replay_worker_events(
    worker_events: tuple[RuntimeEventRecord, ...] | list[RuntimeEventRecord],
    incarnation_events: tuple[RuntimeEventRecord, ...] | list[RuntimeEventRecord] | None = None,
) -> dict[str, Any]:
    """Replay a worker stream with its immutable heartbeat streams.

    A worker stream alone is a registration-only view.  Passing the related
    incarnation streams makes heartbeat version changes replayable without
    consulting mutable tables.
    """
    workers, worker_payloads = _validate_stream(worker_events, expected_type="worker")
    if not workers:
        return replay_runtime_events([])
    related = tuple(incarnation_events or ())
    streams: dict[tuple[str, str], list[RuntimeEventRecord]] = {}
    for event in related:
        streams.setdefault((event.stream_type, event.stream_id), []).append(event)
    validated_related: list[tuple[RuntimeEventRecord, dict[str, Any]]] = []
    for (stream_type, stream_id), stream_events in streams.items():
        if stream_type != "incarnation":
            raise RuntimeConflict("worker replay related input must be incarnation streams")
        valid_events, valid_payloads = _validate_stream(
            stream_events, expected_type="incarnation", expected_id=stream_id
        )
        validated_related.extend(zip(valid_events, valid_payloads, strict=True))
    merged = list(zip(workers, worker_payloads, strict=True)) + validated_related
    if all(event.global_position is not None for event, _ in merged):
        merged.sort(key=lambda item: int(item[0].global_position))
        positions = [int(event.global_position) for event, _ in merged]
        if len(positions) != len(set(positions)):
            raise RuntimeConflict("worker replay related streams contain duplicate positions")
    events = tuple(event for event, _ in merged)
    payloads = tuple(payload for _, payload in merged)
    state, unknown = _replay_worker(events, payloads)
    return {
        "event_family": EVENT_FAMILY,
        "stream_type": "worker",
        "stream_id": workers[0].stream_id,
        "stream_version": len(workers),
        "event_types": tuple(event.event_type for event in events),
        "states": {workers[0].stream_id: state["worker"]["lifecycle"]},
        "state": state,
        "not_covered_fields": unknown,
        "view": "aggregate" if incarnation_events is not None else "registration_only",
        "related_streams": tuple(sorted(streams)),
    }


def replay_runtime_events(
    events: tuple[RuntimeEventRecord, ...] | list[RuntimeEventRecord],
) -> dict[str, Any]:
    """Replay exactly one complete stream without consulting mutable tables."""
    ordered = tuple(events)
    if not ordered:
        return {
            "event_family": EVENT_FAMILY,
            "stream_version": 0,
            "event_types": (),
            "states": {},
            "state": None,
            "not_covered_fields": (),
        }
    first = ordered[0]
    ordered, payload_tuple = _validate_stream(ordered)
    if first.stream_type == "assignment":
        state, unknown = _replay_assignment(ordered, payload_tuple)
        states = {first.stream_id: state["assignment"]["lifecycle"]}
    elif first.stream_type == "worker":
        state, unknown = _replay_worker(ordered, payload_tuple)
        states = {first.stream_id: state["worker"]["lifecycle"]}
    else:
        state, unknown = _replay_simple(
            first.stream_type,
            first.stream_id,
            ordered,
            payload_tuple,
        )
        states = {}
    return {
        "event_family": EVENT_FAMILY,
        "stream_type": first.stream_type,
        "stream_id": first.stream_id,
        "stream_version": len(ordered),
        "event_types": tuple(event.event_type for event in ordered),
        "states": states,
        "state": state,
        "not_covered_fields": unknown,
        **({"view": "registration_only"} if first.stream_type == "worker" else {}),
    }
