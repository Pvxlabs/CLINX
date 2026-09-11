"""Deterministic, store-backed differential traces for PVX-1808.

The trace harness deliberately uses the public RuntimeControlStore API.  It
does not write a second event source or alter the runtime-control reducer.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any, Callable

from runtime_control import ManualClock, RuntimeControlStore
from runtime_control.errors import RuntimeControlError

from .protocol import KernelSession, run_once
from .reference import replay_assignment_reference


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "fixtures" / "kernel_v1"
DEFAULT_BINARY = ROOT / "kernel" / "target" / "release" / "clinx-kernel-eval"
SEEDS = (1808, 18081, 18082, 18083)
BASE_TIME = dt.datetime(2026, 9, 11, 12, 0, tzinfo=dt.timezone.utc)
CANONICAL_MARKER = "SHA256_CANONICAL_PAYLOAD"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalize(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _patch(root: Any, path: str, value: Any) -> None:
    parts = path.split(".")
    current = root
    for part in parts[:-1]:
        current = current[int(part)] if isinstance(current, list) else current[part]
    final = parts[-1]
    if isinstance(current, list):
        current[int(final)] = value
    else:
        current[final] = value


def materialize_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hash authored payloads without changing any other fixture field."""
    result = copy.deepcopy(events)
    for event in result:
        if event.get("payload_hash") == CANONICAL_MARKER:
            event["payload_hash"] = payload_hash(event["payload"])
    return result


def load_fixture_events(filename: str, trace_name: str) -> list[dict[str, Any]]:
    document = json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))
    trace = next(item for item in document["traces"] if item["name"] == trace_name)
    return materialize_events(trace["events"])


def _event_dict(event: Any) -> dict[str, Any]:
    if hasattr(event, "to_dict"):
        return normalize(event.to_dict())
    return normalize(event)


def _state_from_store(store: RuntimeControlStore, assignment_id: str) -> dict[str, Any]:
    assignment = store.get_assignment(assignment_id)
    allocation = store.get_allocation(assignment.allocation_id or "")
    attempt = store.get_attempt(assignment.attempt_id)
    recovery = store.get_recovery_status(assignment_id)
    return {
        "state_version": 1,
        "assignment": {
            key: getattr(assignment, key)
            for key in (
                "assignment_id", "attempt_id", "worker_id", "incarnation_id", "resource_key",
                "resource_epoch", "lifecycle", "version", "lease_expires_at", "created_at",
            )
        }
        | {"released_at": _read_released_at(store, assignment_id)},
        "allocation": {
            key: getattr(allocation, key)
            for key in (
                "allocation_id", "assignment_id", "attempt_id", "worker_id", "incarnation_id",
                "resource_key", "resource_epoch", "lifecycle", "version", "expires_at",
                "release_reason",
            )
        },
        "attempt": {key: getattr(attempt, key) for key in ("attempt_id", "lifecycle", "version")},
        "recovery": (
            {key: getattr(recovery, key) for key in ("assignment_id", "state", "reason", "attempts")}
            if recovery is not None
            else None
        ),
    }


def _read_released_at(store: RuntimeControlStore, assignment_id: str) -> str | None:
    # The public AssignmentRecord intentionally omits released_at.  Read it
    # through the same SQLite file in a read-only query for final-state proof.
    import sqlite3

    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT released_at FROM runtime_assignments WHERE assignment_id=?", (assignment_id,)
        ).fetchone()
    return row[0] if row else None


def _make_snapshot(store: RuntimeControlStore, assignment_id: str, now: dt.datetime) -> dict[str, Any]:
    assignment = store.get_assignment(assignment_id)
    allocation = store.get_allocation(assignment.allocation_id or "")
    attempt = store.get_attempt(assignment.attempt_id)
    execution = store.get_execution("execution-owner")
    worker = store.get_worker(assignment.worker_id)
    incarnation = store.get_incarnation(assignment.incarnation_id)
    resource = store.get_protected_resource(assignment.resource_key)
    timestamp = now.isoformat(timespec="microseconds")
    return {
        "execution": {
            "execution_id": execution.execution_id,
            "task_id": execution.task_id,
            "lifecycle": execution.lifecycle,
        },
        "attempt": {
            "attempt_id": attempt.attempt_id,
            "execution_id": attempt.execution_id,
            "lifecycle": attempt.lifecycle,
        },
        "worker": {
            "worker_id": worker.worker_id,
            "lifecycle": worker.lifecycle,
            "current_incarnation_id": worker.current_incarnation_id,
        },
        "incarnation": {
            "incarnation_id": incarnation.incarnation_id,
            "worker_id": incarnation.worker_id,
            "lifecycle": incarnation.lifecycle,
        },
        "assignment": {
            "assignment_id": assignment.assignment_id,
            "attempt_id": assignment.attempt_id,
            "worker_id": assignment.worker_id,
            "incarnation_id": assignment.incarnation_id,
            "resource_key": assignment.resource_key,
            "resource_epoch": assignment.resource_epoch,
            "lifecycle": assignment.lifecycle,
            "version": assignment.version,
            "lease_expires_at": assignment.lease_expires_at,
        },
        "allocation": {
            "allocation_id": allocation.allocation_id,
            "assignment_id": allocation.assignment_id,
            "attempt_id": allocation.attempt_id,
            "worker_id": allocation.worker_id,
            "incarnation_id": allocation.incarnation_id,
            "resource_key": allocation.resource_key,
            "resource_epoch": allocation.resource_epoch,
            "lifecycle": allocation.lifecycle,
            "version": allocation.version,
            "expires_at": allocation.expires_at,
        },
        "resource": {
            "resource_key": resource.resource_key,
            "fencing_epoch": resource.fencing_epoch,
            "version": resource.version,
        },
        "clock": {"source": "COORDINATOR_TRUSTED", "now": timestamp, "watermark": timestamp},
        "safety_handoff": {
            "state": "VERIFIED",
            "assignment_id": assignment.assignment_id,
            "worker_id": assignment.worker_id,
            "incarnation_id": assignment.incarnation_id,
            "attempt_id": assignment.attempt_id,
            "resource_key": assignment.resource_key,
            "resource_epoch": assignment.resource_epoch,
        },
    }


def _make_request(store: RuntimeControlStore, assignment_id: str) -> dict[str, Any]:
    assignment = store.get_assignment(assignment_id)
    allocation = store.get_allocation(assignment.allocation_id or "")
    resource = store.get_protected_resource(assignment.resource_key)
    execution_id = store.get_attempt(assignment.attempt_id).execution_id
    return {
        "execution_id": execution_id,
        "attempt_id": assignment.attempt_id,
        "worker_id": assignment.worker_id,
        "incarnation_id": assignment.incarnation_id,
        "assignment_id": assignment.assignment_id,
        "allocation_id": allocation.allocation_id,
        "resource_key": assignment.resource_key,
        "resource_epoch": assignment.resource_epoch,
        "expected_assignment_version": assignment.version,
        "expected_allocation_version": allocation.version,
        "expected_resource_version": resource.version,
    }


def _setup(store: RuntimeControlStore, seed: int) -> dict[str, Any]:
    task = f"task-{seed}"
    store.register_task(task, command_id=f"{seed}-task")
    for name in ("release", "revoke", "expire", "orphan", "owner"):
        execution_id = f"execution-{name}" if name == "owner" else f"execution-{seed}-{name}"
        store.register_execution(task, execution_id, command_id=f"{seed}-{name}-execution")
        store.register_attempt(execution_id, f"attempt-{seed}-{name}", 0, command_id=f"{seed}-{name}-attempt")
    store.register_worker(f"worker-{seed}", capacity=8, command_id=f"{seed}-worker")
    store.register_incarnation(
        f"worker-{seed}", incarnation_id=f"inc-{seed}-1", generation=1, command_id=f"{seed}-inc-1"
    )
    return {"worker": f"worker-{seed}", "inc1": f"inc-{seed}-1"}


def _invoke(counter: dict[str, int], store: RuntimeControlStore, operation: Callable[[], Any]) -> None:
    counter["attempted"] += 1
    before = store.count_events()
    try:
        operation()
    except (RuntimeControlError, ValueError, KeyError, AssertionError):
        counter["rejected"] += 1
    else:
        if store.count_events() == before:
            counter["no_op"] += 1
        else:
            counter["effective"] += 1


def _exercise_store(store: RuntimeControlStore, clock: ManualClock, seed: int, identities: dict[str, str], operations: int) -> tuple[dict[str, int], list[str]]:
    counts = {"attempted": 0, "effective": 0, "rejected": 0, "no_op": 0}
    worker, inc1 = identities["worker"], identities["inc1"]
    assignments: list[str] = []

    release = store.assign_attempt(f"attempt-{seed}-release", worker, inc1, f"resource-{seed}-release", lease_seconds=30, assignment_id=f"asn-{seed}-release", command_id=f"{seed}-assign-release")
    assignments.append(release.assignment_id)
    _invoke(counts, store, lambda: store.renew_assignment(release.assignment_id, worker, inc1, f"attempt-{seed}-release", f"resource-{seed}-release", release.resource_epoch, lease_seconds=40, expected_version=0, command_id=f"{seed}-renew-release"))
    _invoke(counts, store, lambda: store.release_assignment(release.assignment_id, worker, inc1, f"attempt-{seed}-release", f"resource-{seed}-release", release.resource_epoch, expected_version=1, command_id=f"{seed}-release"))

    revoke = store.assign_attempt(f"attempt-{seed}-revoke", worker, inc1, f"resource-{seed}-revoke", lease_seconds=30, assignment_id=f"asn-{seed}-revoke", command_id=f"{seed}-assign-revoke")
    assignments.append(revoke.assignment_id)
    _invoke(counts, store, lambda: store.revoke_assignment(revoke.assignment_id, worker, inc1, f"attempt-{seed}-revoke", f"resource-{seed}-revoke", revoke.resource_epoch, expected_version=0, command_id=f"{seed}-revoke"))

    expire = store.assign_attempt(f"attempt-{seed}-expire", worker, inc1, f"resource-{seed}-expire", lease_seconds=1, assignment_id=f"asn-{seed}-expire", command_id=f"{seed}-assign-expire")
    assignments.append(expire.assignment_id)
    _invoke(counts, store, lambda: store.recover_assignment(expire.assignment_id, old_process_stopped=False, side_effect_fence_verified=False, command_id=f"{seed}-illegal-recover"))
    clock.advance(2)
    _invoke(counts, store, lambda: store.reconcile_expired_once(command_id=f"{seed}-expiry"))
    _invoke(counts, store, lambda: store.recover_assignment(expire.assignment_id, old_process_stopped=True, side_effect_fence_verified=True, command_id=f"{seed}-recover-expire"))

    orphan = store.assign_attempt(f"attempt-{seed}-orphan", worker, inc1, f"resource-{seed}-orphan", lease_seconds=30, assignment_id=f"asn-{seed}-orphan", command_id=f"{seed}-assign-orphan")
    assignments.append(orphan.assignment_id)
    inc2 = f"inc-{seed}-2"
    _invoke(counts, store, lambda: store.register_incarnation(worker, incarnation_id=inc2, generation=2, command_id=f"{seed}-inc-2"))
    _invoke(counts, store, lambda: store.recover_assignment(orphan.assignment_id, old_process_stopped=True, side_effect_fence_verified=True, command_id=f"{seed}-recover-orphan"))

    owner = store.assign_attempt(f"attempt-{seed}-owner", worker, inc2, f"resource-{seed}-owner", lease_seconds=300, assignment_id=f"asn-{seed}-owner", command_id=f"{seed}-assign-owner")
    assignments.append(owner.assignment_id)

    # Exercise stale identity, version, resource contention, idempotency, and
    # independent stream handling without mutating the active owner fixture.
    for index in range(max(0, operations - counts["attempted"])):
        mode = (index + seed) % 8
        if mode == 0:
            _invoke(counts, store, lambda: store.renew_assignment(owner.assignment_id, "wrong-worker", inc2, f"attempt-{seed}-owner", f"resource-{seed}-owner", owner.resource_epoch, command_id=f"{seed}-wrong-{index}"))
        elif mode == 1:
            _invoke(counts, store, lambda: store.renew_assignment(owner.assignment_id, worker, inc2, f"attempt-{seed}-owner", f"resource-{seed}-owner", owner.resource_epoch, expected_version=0, command_id=f"{seed}-stale-{index}"))
        elif mode == 2:
            _invoke(counts, store, lambda: store.recover_assignment(owner.assignment_id, old_process_stopped=True, side_effect_fence_verified=True, command_id=f"{seed}-bad-recover-{index}"))
        elif mode == 3:
            _invoke(counts, store, lambda: store.assign_attempt(f"attempt-{seed}-release", worker, inc2, f"resource-{seed}-owner", command_id=f"{seed}-busy-{index}"))
        elif mode == 4:
            _invoke(counts, store, lambda: store.reconcile_expired_once(command_id=f"{seed}-empty-expiry-{index}"))
        elif mode == 5:
            _invoke(counts, store, lambda: store.assign_attempt(f"attempt-{seed}-owner", worker, inc2, f"resource-{seed}-owner", command_id=f"{seed}-duplicate-{index}"))
        elif mode == 6:
            _invoke(counts, store, lambda: store.release_assignment(owner.assignment_id, worker, inc2, f"attempt-{seed}-owner", f"resource-{seed}-owner", owner.resource_epoch, expected_version=99, command_id=f"{seed}-version-{index}"))
        else:
            _invoke(counts, store, lambda: store.revoke_assignment(release.assignment_id, worker, inc1, f"attempt-{seed}-release", f"resource-{seed}-release", release.resource_epoch, command_id=f"{seed}-released-{index}"))
    return counts, assignments


def compare_assignment_stream(events: list[dict[str, Any]], binary: Path) -> dict[str, Any]:
    prefixes: list[dict[str, Any]] = []
    for length in range(1, len(events) + 1):
        prefix = events[:length]
        reference = normalize(replay_assignment_reference(prefix))
        rust = run_once(binary, "replay_assignment", {"events": prefix}, request_id=f"prefix-{length}")
        if not rust["ok"]:
            raise AssertionError(f"Rust rejected valid prefix {length}: {rust}")
        actual = normalize(rust["result"])
        prefixes.append({"length": length, "matched": actual == reference})
        if actual != reference:
            raise AssertionError(f"prefix {length} differential mismatch")
    return {"events": len(events), "prefixes": prefixes, "final": prefixes[-1] if prefixes else None}


def run_seed(seed: int, *, binary: Path = DEFAULT_BINARY, operations: int = 256) -> dict[str, Any]:
    if not binary.is_file():
        raise FileNotFoundError(f"release binary is required: {binary}")
    with tempfile.TemporaryDirectory(prefix=f"pvx1808-{seed}-") as directory:
        clock = ManualClock(BASE_TIME)
        store = RuntimeControlStore(Path(directory) / "runtime.sqlite3", clock=clock)
        store.initialize()
        identities = _setup(store, seed)
        counts, assignments = _exercise_store(store, clock, seed, identities, operations)
        streams: dict[str, Any] = {}
        for assignment_id in assignments:
            events = [_event_dict(event) for event in store.read_events(stream_type="assignment", stream_id=assignment_id, limit=100)]
            streams[assignment_id] = compare_assignment_stream(events, binary)
            persisted = _state_from_store(store, assignment_id)
            rust = run_once(binary, "replay_assignment", {"events": events}, request_id=f"final-{assignment_id}")
            if not rust["ok"] or normalize(rust["result"]["state"]) != normalize(persisted):
                raise AssertionError(f"persisted state mismatch for {assignment_id}")
        snapshot = _make_snapshot(store, assignments[-1], clock.now())
        request = _make_request(store, assignments[-1])
        ownership = run_once(binary, "evaluate_ownership", {"snapshot": snapshot, "request": request}, request_id=f"ownership-{seed}")
        if not ownership["ok"] or ownership["result"]["decision"] != "ALLOW_CANDIDATE":
            raise AssertionError(f"ownership trace did not allow active owner: {ownership}")
        counts["total_events"] = store.count_events()
        counts["assignment_streams"] = len(assignments)
        return {"seed": seed, "operations": counts, "assignment_streams": streams, "ownership": ownership["result"]}


def run_traces(*, binary: Path = DEFAULT_BINARY, seeds: tuple[int, ...] = SEEDS, operations: int = 256) -> dict[str, Any]:
    if operations < 256:
        raise ValueError("each seed requires at least 256 attempted operations")
    results = [run_seed(seed, binary=binary, operations=operations) for seed in seeds]
    return {
        "contract_version": "CLINX_KERNEL_V1",
        "binary": str(binary),
        "seeds": list(seeds),
        "operations_per_seed": operations,
        "results": results,
        "passed": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run PVX-1808 real SQLite differential traces")
    parser.add_argument("--binary", type=Path, default=DEFAULT_BINARY)
    parser.add_argument("--seed", type=int, action="append", dest="seeds")
    parser.add_argument("--operations", type=int, default=256)
    args = parser.parse_args()
    report = run_traces(binary=args.binary, seeds=tuple(args.seeds or SEEDS), operations=args.operations)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
