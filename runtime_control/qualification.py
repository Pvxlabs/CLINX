"""Repeatable bounded qualification fixture for PVX-1806."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import tempfile
from pathlib import Path

from .clock import ManualClock
from .errors import CapacityExceeded, ResourceBusy
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
                    continue
                pending.remove(attempt_id)
                active[attempt_id] = result
                expected_epochs[resource_key] = expected_epochs.get(resource_key, 0) + 1
                if result.resource_epoch != expected_epochs[resource_key]:
                    raise AssertionError("reference epoch diverged from durable epoch")
                successful_assignments += 1
            elif attempt_id in active:
                result = active.pop(attempt_id)
                store.release_assignment(
                    result.assignment_id, result.worker_id, result.incarnation_id,
                    result.attempt_id, result.resource_key, result.resource_epoch,
                    command_id=f"operation-release-{operation}",
                )
                successful_releases += 1

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
        return {
            "qualification": "PASS",
            "seed": seed,
            "operations": operations,
            "workers": 8,
            "attempts": 32,
            "successful_assignments": successful_assignments,
            "successful_releases": successful_releases,
            "assignment_streams_replayed": replayed,
            "event_count": store.count_events(),
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
