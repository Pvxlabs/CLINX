from __future__ import annotations

import datetime as dt
import multiprocessing
import os
from pathlib import Path
import sqlite3

import pytest

from runtime_control import (
    CapacityExceeded,
    ClockAnomaly,
    LeaseExpired,
    ManualClock,
    PayloadSizeExceeded,
    RecoveryBlocked,
    ResourceBusy,
    RuntimeConflict,
    RuntimeControlStore,
    RuntimeIdempotencyConflict,
    RuntimeSchemaError,
    RuntimeVersionConflict,
    StaleMutation,
)
from runtime_control.qualification import run_qualification
from task_registry import TaskRegistry


BASE_TIME = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def make_store(tmp_path: Path, *, clock: ManualClock | None = None, fault_injector=None):
    store = RuntimeControlStore(
        tmp_path / "runtime.sqlite3",
        clock=clock or ManualClock(BASE_TIME),
        fault_injector=fault_injector,
    )
    store.initialize()
    return store


def setup_attempt(store: RuntimeControlStore, *, attempt_id="attempt-1"):
    store.register_task_reference("task-1", command_id="task-1")
    store.register_execution("task-1", "execution-1", command_id="execution-1")
    store.register_attempt("execution-1", attempt_id, 0, command_id=f"{attempt_id}-register")


def setup_worker(store: RuntimeControlStore, worker_id="worker-1", incarnation_id="inc-1", *, capacity=1):
    store.register_worker(worker_id, capacity=capacity, command_id=f"{worker_id}-register")
    store.register_incarnation(worker_id, incarnation_id=incarnation_id, generation=1, command_id=f"{incarnation_id}-register")


def test_runtime_schema_is_explicit_and_does_not_appear_on_construction(tmp_path):
    db = tmp_path / "plain.sqlite3"
    store = RuntimeControlStore(db, clock=ManualClock(BASE_TIME))
    assert store.schema_version() is None
    with pytest.raises(RuntimeSchemaError):
        store.get_worker("missing")
    assert not db.exists()
    store.initialize()
    store.initialize()
    assert store.schema_version() == 3


def test_v1_registry_default_path_does_not_initialize_runtime_control(tmp_path):
    registry = TaskRegistry(tmp_path / "v1.sqlite3")
    assert registry.shadow_event_store is None
    with sqlite3.connect(tmp_path / "v1.sqlite3") as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='runtime_schema_migrations'"
        ).fetchone() is None


def test_runtime_schema_is_additive_on_existing_v1_database(tmp_path):
    db = tmp_path / "existing-v1.sqlite3"
    registry = TaskRegistry(db)
    task = registry.create_task(
        host="p620",
        workspace_alias="p620",
        project_alias="clinx",
        project_name="CLINX",
        cwd=str(tmp_path),
        repository_origin=None,
        branch="main",
        title="V1 compatibility fixture",
    )
    runtime = RuntimeControlStore(db, clock=ManualClock(BASE_TIME))
    runtime.initialize()
    runtime.register_task_reference("v2-task", source_system="explicit-v2", command_id="v2-task")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (task.task_id,)).fetchone() is not None
        assert conn.execute("SELECT source_system FROM runtime_tasks WHERE task_id='v2-task'").fetchone()[0] == "explicit-v2"
        assert conn.execute("SELECT COUNT(*) FROM runtime_tasks WHERE task_id=?", (task.task_id,)).fetchone()[0] == 0


def test_incarnation_retry_without_explicit_generation_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    store.register_worker("worker-1", command_id="worker")
    first = store.register_incarnation("worker-1", command_id="incarnation-command")
    second = store.register_incarnation("worker-1", command_id="incarnation-command")
    assert first.duplicate is False
    assert second.duplicate is True
    assert first.incarnation_id == second.incarnation_id
    assert first.generation == second.generation == 1


def test_same_task_different_execution_attempt_evidence_isolation(tmp_path):
    store = make_store(tmp_path)
    store.register_task("task-1", command_id="task")
    for index in (1, 2):
        store.register_execution("task-1", f"execution-{index}", command_id=f"execution-{index}")
        store.register_attempt(f"execution-{index}", f"attempt-{index}", 0, command_id=f"attempt-{index}")
    setup_worker(store)
    first = store.assign_attempt("attempt-1", "worker-1", "inc-1", "resource-a", command_id="assign-1")
    with pytest.raises(StaleMutation):
        store.record_attempt_evidence(first.assignment_id, "worker-1", "inc-1", "attempt-2", "resource-a", first.resource_epoch, {"cross": True}, command_id="cross-attempt")
    store.release_assignment(first.assignment_id, "worker-1", "inc-1", "attempt-1", "resource-a", first.resource_epoch, command_id="release-1")
    second = store.assign_attempt("attempt-2", "worker-1", "inc-1", "resource-a", command_id="assign-2")
    assert second.resource_epoch == 2


def test_bounded_fixed_seed_sequence_uses_independent_reference(tmp_path):
    result = run_qualification(seed=1806, operations=200)
    assert result["qualification"] == "PASS"
    assert result["workers"] == 8
    assert result["attempts"] == 32
    assert result["operations"] == 200
    assert result["attempted_operations"] == (
        result["effective_state_changes"] + result["no_op_operations"]
    )
    matrix = result["transition_matrix"]
    assert matrix["attempted_operations"] == (
        matrix["effective_state_changes"]
        + matrix["no_op_operations"]
        + matrix["rejected_operations"]
    )


def test_durable_identity_chain_and_reopen(tmp_path):
    store = make_store(tmp_path)
    setup_attempt(store)
    setup_worker(store)
    result = store.assign_attempt("attempt-1", "worker-1", "inc-1", "resource-a", command_id="assign-1")
    reopened = RuntimeControlStore(store.path, clock=ManualClock(BASE_TIME + dt.timedelta(seconds=1)))
    assignment = reopened.get_assignment(result.assignment_id)
    assert assignment.attempt_id == "attempt-1"
    assert reopened.get_attempt("attempt-1").execution_id == "execution-1"
    assert reopened.get_allocation(result.allocation_id).resource_epoch == 1
    with sqlite3.connect(store.path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO runtime_executions(execution_id,task_id,request_json,policy_json,route_json,lifecycle,created_at) VALUES('bad','missing','{}','{}','{}','REQUESTED','now')"
            )


def test_single_current_assignment_and_capacity_admission(tmp_path):
    store = make_store(tmp_path)
    setup_attempt(store)
    store.register_attempt("execution-1", "attempt-2", 1, command_id="attempt-2-register")
    setup_worker(store, capacity=1)
    first = store.assign_attempt("attempt-1", "worker-1", "inc-1", "resource-a", command_id="assign-1")
    with pytest.raises(CapacityExceeded):
        store.assign_attempt("attempt-2", "worker-1", "inc-1", "resource-b", command_id="assign-2")
    with pytest.raises(RuntimeConflict):
        store.assign_attempt("attempt-1", "worker-1", "inc-1", "resource-b", command_id="assign-again")
    assert store.get_assignment(first.assignment_id).lifecycle == "ACTIVE"


def test_resource_exclusive_and_epoch_is_monotonic_after_release(tmp_path):
    store = make_store(tmp_path)
    setup_attempt(store)
    setup_worker(store)
    first = store.assign_attempt("attempt-1", "worker-1", "inc-1", "resource-a", command_id="assign-1")
    store.release_assignment(
        first.assignment_id, "worker-1", "inc-1", "attempt-1", "resource-a", first.resource_epoch,
        command_id="release-1",
    )
    store.register_attempt("execution-1", "attempt-2", 1, command_id="attempt-2-register")
    second = store.assign_attempt("attempt-2", "worker-1", "inc-1", "resource-a", command_id="assign-2")
    assert second.resource_epoch == 2
    assert store.get_protected_resource("resource-a").fencing_epoch == 2


def test_incarnation_isolation_quarantines_old_assignment(tmp_path):
    store = make_store(tmp_path)
    setup_attempt(store)
    setup_worker(store)
    first = store.assign_attempt("attempt-1", "worker-1", "inc-1", "resource-a", command_id="assign-1")
    store.register_incarnation("worker-1", incarnation_id="inc-2", generation=2, command_id="inc-2-register")
    with pytest.raises(StaleMutation):
        store.heartbeat_worker("worker-1", "inc-1", command_id="old-heartbeat")
    assert store.get_assignment(first.assignment_id).lifecycle == "ORPHANED"
    assert store.get_allocation(first.allocation_id).lifecycle == "QUARANTINED"
    with pytest.raises(ResourceBusy):
        store.assign_attempt("attempt-1", "worker-1", "inc-2", "resource-a", command_id="new-assign")


def test_expiry_requires_explicit_recovery_and_rejects_renewal_at_boundary(tmp_path):
    clock = ManualClock(BASE_TIME)
    store = make_store(tmp_path, clock=clock)
    setup_attempt(store)
    setup_worker(store)
    first = store.assign_attempt("attempt-1", "worker-1", "inc-1", "resource-a", lease_seconds=10, command_id="assign-1")
    clock.advance(10)
    with pytest.raises(LeaseExpired):
        store.renew_assignment(
            first.assignment_id, "worker-1", "inc-1", "attempt-1", "resource-a", first.resource_epoch,
            command_id="renew-expired",
        )
    reopened = RuntimeControlStore(store.path, clock=clock)
    assert reopened.reconcile_expired_once() == (first.assignment_id,)
    with pytest.raises(RecoveryBlocked):
        reopened.recover_assignment(first.assignment_id, old_process_stopped=False, side_effect_fence_verified=False)
    with pytest.raises(ResourceBusy):
        reopened.assign_attempt("attempt-1", "worker-1", "inc-1", "resource-a", command_id="still-blocked")
    recovered = reopened.recover_assignment(first.assignment_id, old_process_stopped=True, side_effect_fence_verified=True, command_id="recover-1")
    assert recovered.lifecycle == "RECOVERED"
    recovered_retry = reopened.recover_assignment(first.assignment_id, old_process_stopped=True, side_effect_fence_verified=True, command_id="recover-1")
    assert recovered_retry.duplicate is True
    event_types = tuple(
        event.event_type
        for event in reopened.read_events(stream_type="assignment", stream_id=first.assignment_id, limit=10)
    )
    assert event_types == ("AssignmentGranted", "AssignmentExpired", "AssignmentRecovered")
    second = reopened.assign_attempt("attempt-1", "worker-1", "inc-1", "resource-a", command_id="assign-2")
    assert second.resource_epoch == first.resource_epoch + 1


def test_stale_release_and_evidence_cannot_mutate_new_owner(tmp_path):
    store = make_store(tmp_path)
    setup_attempt(store)
    setup_worker(store, worker_id="worker-1", incarnation_id="inc-1")
    first = store.assign_attempt("attempt-1", "worker-1", "inc-1", "resource-a", command_id="assign-1")
    store.revoke_assignment(first.assignment_id, "worker-1", "inc-1", "attempt-1", "resource-a", first.resource_epoch, command_id="revoke-1")
    second = store.assign_attempt("attempt-1", "worker-1", "inc-1", "resource-a", command_id="assign-2")
    with pytest.raises(StaleMutation):
        store.release_assignment(first.assignment_id, "worker-1", "inc-1", "attempt-1", "resource-a", first.resource_epoch, command_id="old-release")
    with pytest.raises(StaleMutation):
        store.record_attempt_evidence(first.assignment_id, "worker-1", "inc-1", "attempt-1", "resource-a", first.resource_epoch, {"late": True}, command_id="old-evidence")
    assert store.get_assignment(second.assignment_id).lifecycle == "ACTIVE"
    assert store.get_allocation(second.allocation_id).resource_epoch == first.resource_epoch + 1


def test_protected_mutation_checks_fence_at_actual_sqlite_write(tmp_path):
    store = make_store(tmp_path)
    setup_attempt(store)
    setup_worker(store)
    first = store.assign_attempt("attempt-1", "worker-1", "inc-1", "resource-a", command_id="assign-1")
    changed = store.mutate_protected_resource(
        first.assignment_id, "worker-1", "inc-1", "attempt-1", "resource-a", first.resource_epoch, 0,
        {"owner": "first"}, command_id="mutation-1",
    )
    assert changed.version == 1
    with pytest.raises(RuntimeVersionConflict):
        store.mutate_protected_resource(
            first.assignment_id, "worker-1", "inc-1", "attempt-1", "resource-a", first.resource_epoch, 0,
            {"owner": "stale-version"}, command_id="mutation-stale-version",
        )
    store.revoke_assignment(first.assignment_id, "worker-1", "inc-1", "attempt-1", "resource-a", first.resource_epoch, command_id="revoke-1")
    second = store.assign_attempt("attempt-1", "worker-1", "inc-1", "resource-a", command_id="assign-2")
    with pytest.raises(StaleMutation):
        store.mutate_protected_resource(
            first.assignment_id, "worker-1", "inc-1", "attempt-1", "resource-a", first.resource_epoch, 1,
            {"owner": "old"}, command_id="mutation-old-owner",
        )
    assert store.get_protected_resource("resource-a").value.to_dict() == {"owner": "first"}
    assert second.resource_epoch == 2


def test_idempotency_returns_receipt_without_regrant_and_conflicts_on_semantics(tmp_path):
    store = make_store(tmp_path)
    setup_attempt(store)
    setup_worker(store)
    first = store.assign_attempt("attempt-1", "worker-1", "inc-1", "resource-a", command_id="assign-same")
    duplicate = store.assign_attempt("attempt-1", "worker-1", "inc-1", "resource-a", command_id="assign-same")
    assert duplicate.duplicate is True
    assert duplicate.assignment_id == first.assignment_id
    assert duplicate.current_authority_valid is True
    assert store.get_allocation(first.allocation_id).resource_epoch == 1
    with pytest.raises(RuntimeIdempotencyConflict):
        store.assign_attempt("attempt-1", "worker-1", "inc-1", "resource-b", command_id="assign-same")
    store.revoke_assignment(first.assignment_id, "worker-1", "inc-1", "attempt-1", "resource-a", first.resource_epoch, command_id="revoke-1")
    old_receipt = store.assign_attempt("attempt-1", "worker-1", "inc-1", "resource-a", command_id="assign-same")
    assert old_receipt.duplicate is True
    assert old_receipt.current_authority_valid is False


def test_fault_before_commit_rolls_back_and_after_commit_retry_is_safe(tmp_path):
    stages = {"after_event", "after_outbox", "after_receipt"}
    for stage in stages:
        def inject(actual, expected=stage):
            if actual == expected:
                raise RuntimeError(expected)
        store = make_store(tmp_path / stage, fault_injector=inject)
        with pytest.raises(RuntimeError, match=stage):
            store.register_task_reference("task-1", command_id="task-1")
        assert store.count_events() == 0
        with sqlite3.connect(store.path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM runtime_tasks").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM runtime_events").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM runtime_event_positions").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM runtime_outbox").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM runtime_command_receipts").fetchone()[0] == 0

    fired = {"value": False}
    def after_commit(stage):
        if stage == "after_commit" and not fired["value"]:
            fired["value"] = True
            raise RuntimeError("response lost")
    store = make_store(tmp_path / "after-commit", fault_injector=after_commit)
    with pytest.raises(RuntimeError, match="response lost"):
        store.register_task_reference("task-1", command_id="task-1")
    retry = store.register_task_reference("task-1", command_id="task-1")
    assert retry.duplicate is True
    assert store.count_events() == 1


def _crash_runtime_command(path: str, stage: str):
    def inject(actual):
        if actual == stage:
            os._exit(23 if stage == "before_commit" else 24)
    store = RuntimeControlStore(path, clock=ManualClock(BASE_TIME), fault_injector=inject)
    store.register_task_reference("crash-task", command_id="crash-task")


def test_runtime_process_exit_before_and_after_commit_has_no_partial_rows(tmp_path):
    before = make_store(tmp_path / "before-crash")
    context = multiprocessing.get_context("fork")
    process = context.Process(target=_crash_runtime_command, args=(str(before.path), "before_commit"))
    process.start()
    process.join(10)
    assert process.exitcode == 23
    assert before.count_events() == 0
    with sqlite3.connect(before.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runtime_tasks").fetchone()[0] == 0

    after = make_store(tmp_path / "after-crash")
    process = context.Process(target=_crash_runtime_command, args=(str(after.path), "after_commit"))
    process.start()
    process.join(10)
    assert process.exitcode == 24
    assert after.count_events() == 1
    retry = after.register_task_reference("crash-task", command_id="crash-task")
    assert retry.duplicate is True


def test_worker_reported_future_time_does_not_extend_assignment(tmp_path):
    clock = ManualClock(BASE_TIME)
    store = make_store(tmp_path, clock=clock)
    setup_attempt(store)
    setup_worker(store)
    assignment = store.assign_attempt("attempt-1", "worker-1", "inc-1", "resource-a", lease_seconds=10, command_id="assign-1")
    store.heartbeat_worker(
        "worker-1", "inc-1", worker_reported_at="2099-01-01T00:00:00+00:00", command_id="heartbeat-future"
    )
    assert store.get_assignment(assignment.assignment_id).lease_expires_at.endswith("00:00:10.000000+00:00")
    clock.advance(10)
    with pytest.raises(LeaseExpired):
        store.renew_assignment(
            assignment.assignment_id, "worker-1", "inc-1", "attempt-1", "resource-a", assignment.resource_epoch,
            command_id="renew-after-future-heartbeat",
        )


def test_clock_backward_is_conservative(tmp_path):
    clock = ManualClock(BASE_TIME)
    store = make_store(tmp_path, clock=clock)
    clock.set(BASE_TIME - dt.timedelta(seconds=1))
    with pytest.raises(ClockAnomaly):
        store.register_task_reference("task-1", command_id="task-1")


def test_runtime_events_are_append_only_bounded_and_replayable(tmp_path):
    store = make_store(tmp_path)
    setup_attempt(store)
    setup_worker(store)
    result = store.assign_attempt("attempt-1", "worker-1", "inc-1", "resource-a", command_id="assign-1")
    events = store.read_events(stream_type="assignment", stream_id=result.assignment_id, limit=10)
    assert events[0].event_family == "RUNTIME_WORKER_V1"
    assert store.replay_events(events)["stream_version"] == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with sqlite3.connect(store.path) as conn:
            conn.execute("DELETE FROM runtime_events WHERE event_id=?", (events[0].event_id,))
    with pytest.raises(PayloadSizeExceeded):
        store.read_events(stream_type="assignment", stream_id=result.assignment_id, max_payload_bytes=1)


def _race_assign(path: str, worker_id: str, incarnation_id: str, attempt_id: str, command_id: str, barrier, queue):
    store = RuntimeControlStore(path, clock=ManualClock(BASE_TIME), busy_timeout_ms=5000)
    barrier.wait()
    try:
        result = store.assign_attempt(attempt_id, worker_id, incarnation_id, "resource-race", command_id=command_id)
        queue.put(("duplicate" if result.duplicate else "committed", result.assignment_id))
    except Exception as exc:
        queue.put((type(exc).__name__, str(exc)))


def test_two_process_competition_has_one_owner(tmp_path):
    store = make_store(tmp_path)
    store.register_task_reference("task-1", command_id="task-1")
    store.register_execution("task-1", "execution-1", command_id="execution-1")
    store.register_attempt("execution-1", "attempt-1", 0, command_id="attempt-1-register")
    store.register_worker("worker-1", capacity=2, command_id="worker-1-register")
    store.register_incarnation("worker-1", incarnation_id="inc-1", generation=1, command_id="inc-1-register")
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    queue = context.Queue()
    processes = [
        context.Process(target=_race_assign, args=(str(store.path), "worker-1", "inc-1", "attempt-1", "race-1", barrier, queue)),
        context.Process(target=_race_assign, args=(str(store.path), "worker-1", "inc-1", "attempt-1", "race-2", barrier, queue)),
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert not process.is_alive()
    outcomes = sorted(queue.get(timeout=2)[0] for _ in processes)
    assert outcomes.count("committed") == 1
    assert store.count_events() == 6
