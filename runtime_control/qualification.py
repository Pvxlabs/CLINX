"""Repeatable bounded qualification fixture for PVX-1806."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import tempfile
from pathlib import Path

from .clock import ManualClock
from .errors import CapacityExceeded, LeaseExpired, ResourceBusy
from .store import RuntimeControlStore


def run_qualification(*, seed: int = 1806, operations: int = 200) -> dict[str, object]:
    if operations < 1 or operations > 2_000:
        raise ValueError("operations must be between 1 and 2000")
    rng = random.Random(seed)
    clock = ManualClock(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    with tempfile.TemporaryDirectory(prefix="clinx-pvx1806-") as directory:
        path = Path(directory) / "qualification.sqlite3"
        store = RuntimeControlStore(path, clock=clock)
        store.initialize()
        store.register_task("qualification-task", command_id="task")
        for index in range(32):
            execution_id = f"qualification-execution-{index}"
            attempt_id = f"qualification-attempt-{index}"
            store.register_execution("qualification-task", execution_id, command_id=f"execution-{index}")
            store.register_attempt(execution_id, attempt_id, 0, command_id=f"attempt-{index}")
        for index in range(8):
            worker_id = f"qualification-worker-{index}"
            store.register_worker(worker_id, capacity=4, command_id=f"worker-{index}")
            store.register_incarnation(worker_id, incarnation_id=f"incarnation-{index}", generation=1, command_id=f"incarnation-{index}")

        pending = {f"qualification-attempt-{index}" for index in range(32)}
        active: dict[str, object] = {}
        expected_epochs: dict[str, int] = {}
        successful_assignments = 0
        successful_releases = 0
        effective_state_changes = 0
        no_op_operations = 0
        for operation in range(operations):
            attempt_id = f"qualification-attempt-{rng.randrange(32)}"
            worker_index = rng.randrange(8)
            worker_id = f"qualification-worker-{worker_index}"
            incarnation_id = f"incarnation-{worker_index}"
            resource_key = f"qualification-resource-{int(attempt_id.rsplit('-', 1)[1])}"
            if attempt_id in pending:
                try:
                    result = store.assign_attempt(
                        attempt_id, worker_id, incarnation_id, resource_key,
                        lease_seconds=30, command_id=f"operation-assign-{operation}",
                    )
                except (CapacityExceeded, ResourceBusy):
                    no_op_operations += 1
                    continue
                pending.remove(attempt_id)
                active[attempt_id] = result
                expected_epochs[resource_key] = expected_epochs.get(resource_key, 0) + 1
                if result.resource_epoch != expected_epochs[resource_key]:
                    raise AssertionError("reference epoch diverged from durable epoch")
                successful_assignments += 1
                effective_state_changes += 1
            elif attempt_id in active:
                result = active.pop(attempt_id)
                store.release_assignment(
                    result.assignment_id, result.worker_id, result.incarnation_id,
                    result.attempt_id, result.resource_key, result.resource_epoch,
                    command_id=f"operation-release-{operation}",
                )
                successful_releases += 1
                effective_state_changes += 1
            else:
                no_op_operations += 1

        for assignment_id in tuple(
            result.assignment_id for result in active.values()
        ):
            assignment = store.get_assignment(assignment_id)
            store.release_assignment(
                assignment.assignment_id, assignment.worker_id, assignment.incarnation_id,
                assignment.attempt_id, assignment.resource_key, assignment.resource_epoch,
                command_id=f"cleanup-{assignment_id}",
            )
            successful_releases += 1

        assignment_ids = [
            row.payload.to_dict()["assignment_id"]
            for row in store.read_events(limit=10_000)
            if row.event_type == "AssignmentGranted"
        ]
        replayed = 0
        for assignment_id in assignment_ids:
            events = store.read_events(stream_type="assignment", stream_id=assignment_id, limit=100)
            replay = store.replay_events(events)
            if replay["event_family"] != "RUNTIME_WORKER_V1":
                raise AssertionError("unexpected runtime event family")
            replayed += 1

        matrix_path = Path(directory) / "transition-matrix.sqlite3"
        matrix_clock = ManualClock(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
        matrix = RuntimeControlStore(matrix_path, clock=matrix_clock)
        matrix.initialize()
        matrix.register_task("matrix-task", command_id="matrix-task")
        matrix.register_execution("matrix-task", "matrix-execution", command_id="matrix-execution")
        matrix.register_attempt("matrix-execution", "matrix-attempt", 0, command_id="matrix-attempt")
        matrix.register_worker("matrix-worker", capacity=1, command_id="matrix-worker")
        matrix.register_incarnation(
            "matrix-worker",
            incarnation_id="matrix-incarnation-1",
            generation=1,
            command_id="matrix-incarnation-1",
        )
        matrix_attempted = 0
        matrix_effective = 0
        matrix_no_op = 0
        matrix_rejected = 0

        def assert_assignment_state(
            assignment_id: str,
            *,
            lifecycle: str,
            assignment_version: int,
            allocation_version: int,
            allocation_lifecycle: str,
            attempt_lifecycle: str,
            attempt_version: int,
            recovery_state: str | None,
        ) -> None:
            actual_assignment = matrix.get_assignment(assignment_id)
            actual_allocation = matrix.get_allocation(actual_assignment.allocation_id)
            actual_attempt = matrix.get_attempt(actual_assignment.attempt_id)
            actual_recovery = matrix.get_recovery_status(assignment_id)
            replay = matrix.replay_events(
                matrix.read_events(
                    stream_type="assignment",
                    stream_id=assignment_id,
                    limit=100,
                )
            )["state"]
            expected = (
                lifecycle,
                assignment_version,
                actual_assignment.worker_id,
                actual_assignment.incarnation_id,
                actual_assignment.resource_epoch,
                actual_assignment.lease_expires_at,
                allocation_lifecycle,
                allocation_version,
                attempt_lifecycle,
                attempt_version,
                recovery_state,
            )
            persisted = (
                actual_assignment.lifecycle,
                actual_assignment.version,
                actual_assignment.worker_id,
                actual_assignment.incarnation_id,
                actual_assignment.resource_epoch,
                actual_assignment.lease_expires_at,
                actual_allocation.lifecycle,
                actual_allocation.version,
                actual_attempt.lifecycle,
                actual_attempt.version,
                actual_recovery.state if actual_recovery else None,
            )
            projected = (
                replay["assignment"]["lifecycle"],
                replay["assignment"]["version"],
                replay["assignment"]["worker_id"],
                replay["assignment"]["incarnation_id"],
                replay["assignment"]["resource_epoch"],
                replay["assignment"]["lease_expires_at"],
                replay["allocation"]["lifecycle"],
                replay["allocation"]["version"],
                replay["attempt"]["lifecycle"],
                replay["attempt"]["version"],
                replay["recovery"]["state"] if replay["recovery"] else None,
            )
            if persisted != expected or projected != expected:
                raise AssertionError("independent assignment reference model diverged")

        first = matrix.assign_attempt(
            "matrix-attempt",
            "matrix-worker",
            "matrix-incarnation-1",
            "matrix-resource",
            lease_seconds=10,
            command_id="matrix-grant",
        )
        matrix_attempted += 1
        matrix_effective += 1
        assert_assignment_state(
            first.assignment_id,
            lifecycle="ACTIVE",
            assignment_version=0,
            allocation_version=0,
            allocation_lifecycle="ACTIVE",
            attempt_lifecycle="ASSIGNED",
            attempt_version=1,
            recovery_state=None,
        )
        duplicate = matrix.assign_attempt(
            "matrix-attempt",
            "matrix-worker",
            "matrix-incarnation-1",
            "matrix-resource",
            lease_seconds=10,
            command_id="matrix-grant",
        )
        matrix_attempted += 1
        matrix_no_op += int(duplicate.duplicate)
        matrix_clock.advance(2)
        matrix.renew_assignment(
            first.assignment_id,
            first.worker_id,
            first.incarnation_id,
            first.attempt_id,
            first.resource_key,
            first.resource_epoch,
            lease_seconds=20,
            command_id="matrix-renew",
        )
        matrix_attempted += 1
        matrix_effective += 1
        assert_assignment_state(
            first.assignment_id,
            lifecycle="ACTIVE",
            assignment_version=1,
            allocation_version=1,
            allocation_lifecycle="ACTIVE",
            attempt_lifecycle="ASSIGNED",
            attempt_version=1,
            recovery_state=None,
        )
        response_lost = {"value": False}

        def lose_response(stage: str) -> None:
            if stage == "after_commit" and not response_lost["value"]:
                response_lost["value"] = True
                raise RuntimeError("qualification response loss")

        matrix.fault_injector = lose_response
        try:
            matrix.record_attempt_evidence(
                first.assignment_id,
                first.worker_id,
                first.incarnation_id,
                first.attempt_id,
                first.resource_key,
                first.resource_epoch,
                {"qualification": "response-loss"},
                command_id="matrix-evidence",
            )
        except RuntimeError as exc:
            if str(exc) != "qualification response loss":
                raise
        else:
            raise AssertionError("qualification response-loss fault did not fire")
        matrix.fault_injector = None
        matrix_attempted += 1
        matrix_effective += 1
        evidence_retry = matrix.record_attempt_evidence(
            first.assignment_id,
            first.worker_id,
            first.incarnation_id,
            first.attempt_id,
            first.resource_key,
            first.resource_epoch,
            {"qualification": "response-loss"},
            command_id="matrix-evidence",
        )
        matrix_attempted += 1
        matrix_no_op += int(evidence_retry.duplicate)
        matrix_clock.advance(20)
        try:
            matrix.renew_assignment(
                first.assignment_id,
                first.worker_id,
                first.incarnation_id,
                first.attempt_id,
                first.resource_key,
                first.resource_epoch,
                command_id="matrix-expired-renew",
            )
        except LeaseExpired:
            matrix_rejected += 1
        else:
            raise AssertionError("exact-expiry renewal was accepted")
        matrix_attempted += 1
        if matrix.reconcile_expired_once() != (first.assignment_id,):
            raise AssertionError("matrix expiry reconciliation did not change one assignment")
        matrix_attempted += 1
        matrix_effective += 1
        assert_assignment_state(
            first.assignment_id,
            lifecycle="EXPIRED",
            assignment_version=2,
            allocation_version=2,
            allocation_lifecycle="QUARANTINED",
            attempt_lifecycle="ASSIGNED",
            attempt_version=1,
            recovery_state="PENDING",
        )
        matrix.recover_assignment(
            first.assignment_id,
            old_process_stopped=True,
            side_effect_fence_verified=True,
            command_id="matrix-recover",
        )
        matrix_attempted += 1
        matrix_effective += 1
        assert_assignment_state(
            first.assignment_id,
            lifecycle="RECOVERED",
            assignment_version=3,
            allocation_version=3,
            allocation_lifecycle="RELEASED",
            attempt_lifecycle="PENDING",
            attempt_version=2,
            recovery_state="DONE",
        )
        second = matrix.assign_attempt(
            "matrix-attempt",
            "matrix-worker",
            "matrix-incarnation-1",
            "matrix-resource",
            lease_seconds=10,
            command_id="matrix-reassign",
        )
        matrix_attempted += 1
        matrix_effective += 1
        matrix.register_incarnation(
            "matrix-worker",
            incarnation_id="matrix-incarnation-2",
            generation=2,
            command_id="matrix-incarnation-2",
        )
        matrix_attempted += 1
        matrix_effective += 1
        assert_assignment_state(
            second.assignment_id,
            lifecycle="ORPHANED",
            assignment_version=1,
            allocation_version=1,
            allocation_lifecycle="QUARANTINED",
            attempt_lifecycle="ASSIGNED",
            attempt_version=3,
            recovery_state="PENDING",
        )
        worker_replay = matrix.replay_events(
            matrix.read_events(stream_type="worker", stream_id="matrix-worker", limit=100)
        )["state"]
        if worker_replay["worker"]["current_incarnation_id"] != "matrix-incarnation-2":
            raise AssertionError("worker incarnation projection diverged")
        # Heartbeats mutate both worker aggregates.  Their immutable post-write
        # versions live on the incarnation stream and are replayed through the
        # worker + related-stream entry point.
        matrix_clock.advance(1)
        matrix.heartbeat_worker(
            "matrix-worker", "matrix-incarnation-2", command_id="matrix-heartbeat-1"
        )
        matrix_clock.advance(1)
        matrix.heartbeat_worker(
            "matrix-worker", "matrix-incarnation-2", command_id="matrix-heartbeat-2"
        )
        worker_snapshot = matrix.get_worker("matrix-worker")
        incarnation_snapshot = matrix.get_incarnation("matrix-incarnation-2")
        worker_replay_after_heartbeat = matrix.replay_events(
            matrix.read_events(stream_type="worker", stream_id="matrix-worker", limit=100)
        )["state"]
        if (
            worker_replay_after_heartbeat["worker"]["version"] != worker_snapshot.version
            or worker_replay_after_heartbeat["incarnations"]["matrix-incarnation-2"]["version"]
            != incarnation_snapshot.version
        ):
            raise AssertionError("heartbeat aggregate version replay diverged")
        if matrix_attempted != matrix_effective + matrix_no_op + matrix_rejected:
            raise AssertionError("matrix operation accounting diverged")
        return {
            "qualification": "PASS",
            "seed": seed,
            "operations": operations,
            "workers": 8,
            "attempts": 32,
            "successful_assignments": successful_assignments,
            "successful_releases": successful_releases,
            "attempted_operations": operations,
            "effective_state_changes": effective_state_changes,
            "no_op_operations": no_op_operations,
            "assignment_streams_replayed": replayed,
            "event_count": store.count_events(),
            "transition_matrix": {
                "attempted_operations": matrix_attempted,
                "effective_state_changes": matrix_effective,
                "no_op_operations": matrix_no_op,
                "rejected_operations": matrix_rejected,
                "assignment_streams_compared": 2,
                "incarnation_replacement_compared": True,
                "response_loss_retry_compared": True,
            },
            "worker_version_matrix": {
                "heartbeats_attempted": 2,
                "worker_version_replayed": worker_replay_after_heartbeat["worker"]["version"],
                "incarnation_version_replayed": worker_replay_after_heartbeat["incarnations"]["matrix-incarnation-2"]["version"],
                "aggregate_streams_compared": 2,
                "legacy_missing_versions": "UNKNOWN_NOT_COVERED",
            },
            "production_load_claim": "NOT_CLAIMED",
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run bounded CLINX PVX-1806 runtime-control qualification")
    parser.add_argument("--seed", type=int, default=1806)
    parser.add_argument("--operations", type=int, default=200)
    args = parser.parse_args(argv)
    print(json.dumps(run_qualification(seed=args.seed, operations=args.operations), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
