import dataclasses
import datetime as dt
import hashlib
import json
import multiprocessing
import os
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

from runtime_control import (
    ClockAnomaly,
    LeaseExpired,
    ManualClock,
    PayloadSizeExceeded,
    RuntimeConflict,
    RuntimeControlStore,
    RuntimeEventRecord,
    RuntimeIdempotencyConflict,
    RecoveryBlocked,
    SafetyDecisionPending,
    StaleMutation,
)
from runtime_control.errors import RuntimeAuthorizationError
from runtime_control.schema import MIGRATIONS
from runtime_control.replay import replay_runtime_events


BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _blank(path: Path, *, clock=None, fault_injector=None):
    clock = clock or ManualClock(BASE)
    store = RuntimeControlStore(path / "runtime.sqlite3", clock=clock, fault_injector=fault_injector)
    store.initialize()
    return store, clock


def _assigned(path: Path, *, lease_seconds=10):
    store, clock = _blank(path)
    store.register_task_reference("task-1", command_id="setup-task")
    store.register_execution("task-1", "execution-1", command_id="setup-execution")
    store.register_attempt("execution-1", "attempt-1", 0, command_id="setup-attempt")
    store.register_worker("worker-1", capacity=2, command_id="setup-worker")
    store.register_incarnation(
        "worker-1",
        incarnation_id="incarnation-1",
        generation=1,
        command_id="setup-incarnation",
    )
    receipt = store.assign_attempt(
        "attempt-1",
        "worker-1",
        "incarnation-1",
        "resource-1",
        lease_seconds=lease_seconds,
        command_id="setup-assignment",
    )
    owner = (
        receipt.assignment_id,
        "worker-1",
        "incarnation-1",
        "attempt-1",
        "resource-1",
        receipt.resource_epoch,
    )
    return store, clock, receipt, owner


class _ConnectionObserver:
    def __init__(self, connection, store, index):
        self.connection, self.store, self.index = connection, store, index

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, *args, **kwargs):
        if (
            self.store.armed
            and sql.strip().upper() == "BEGIN IMMEDIATE"
            and self.index == self.store.target_index
        ):
            self.store.begin_entered.set()
        return self.connection.execute(sql, *args, **kwargs)

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)


class _LockInterleavingStore(RuntimeControlStore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.armed = False
        self.connect_count = 0
        self.target_index = 2
        self.pause_after_observation = False
        self.observation_done = threading.Event()
        self.resume_business = threading.Event()
        self.begin_entered = threading.Event()

    def _connect(self, *, read_only=False):
        connection = super()._connect(read_only=read_only)
        if self.armed and not read_only:
            self.connect_count += 1
            return _ConnectionObserver(connection, self, self.connect_count)
        return connection

    def _observe_coordinator_time(self):
        result = super()._observe_coordinator_time()
        if self.armed and self.pause_after_observation:
            self.observation_done.set()
            assert self.resume_business.wait(5), "business barrier timeout"
        return result


def _interleaving_fixture(path: Path, lease_seconds: int):
    clock = ManualClock(BASE)
    store = _LockInterleavingStore(path / "runtime.sqlite3", clock=clock, busy_timeout_ms=5_000)
    store.initialize()
    store.register_task_reference("task", command_id="register-task")
    store.register_execution("task", "execution", command_id="register-execution")
    store.register_attempt("execution", "attempt", 0, command_id="register-attempt")
    store.register_worker("worker", capacity=2, command_id="register-worker")
    store.register_incarnation("worker", incarnation_id="incarnation", generation=1, command_id="register-incarnation")
    assigned = store.assign_attempt(
        "attempt", "worker", "incarnation", "resource", lease_seconds=lease_seconds, command_id="assign"
    )
    owner = (
        assigned.assignment_id, "worker", "incarnation", "attempt", "resource", assigned.resource_epoch
    )
    return store, clock, assigned, owner


OWNER_WRITE_OPERATIONS = (
    "mutate",
    "renew",
    "release",
    "revoke",
    "evidence",
)


def _invoke_owner_write(store, owner, operation, command_id):
    if operation == "mutate":
        return store.mutate_protected_resource(
            *owner,
            0,
            {"operation": operation, "command": command_id},
            command_id=command_id,
        )
    if operation == "renew":
        return store.renew_assignment(
            *owner,
            lease_seconds=20,
            command_id=command_id,
        )
    if operation == "release":
        return store.release_assignment(*owner, command_id=command_id)
    if operation == "revoke":
        return store.revoke_assignment(*owner, command_id=command_id)
    if operation == "evidence":
        return store.record_attempt_evidence(
            *owner,
            {"operation": operation, "command": command_id},
            command_id=command_id,
        )
    raise AssertionError(f"unknown owner operation: {operation}")


class _ExitBeforeSafetyHandoff(RuntimeControlStore):
    """Qualification-only child that exits after business rollback."""

    def _complete_safety_handoff(self, handoff_key, handoff_token, observed_at):
        os._exit(17)


def _exit_during_evidence_handoff(path, owner, queue):
    def fail_business(stage):
        if stage == "before_commit":
            raise RuntimeError("business rollback before child exit")

    store = _ExitBeforeSafetyHandoff(
        path,
        clock=ManualClock(BASE + dt.timedelta(seconds=8)),
        fault_injector=fail_business,
    )
    store.record_attempt_evidence(
        *owner,
        {"child": True},
        command_id="child-evidence",
    )
    queue.put("unreachable")


@pytest.mark.parametrize("phase", ["observation_lock", "business_lock"])
@pytest.mark.parametrize("operation", ["mutation", "renew"])
@pytest.mark.parametrize("outcome", ["valid", "exact", "expired"])
def test_lock_wait_authorization_uses_post_lock_clock(tmp_path, phase, operation, outcome):
    lease_seconds = 20 if outcome == "valid" else 10
    store, clock, assignment, owner = _interleaving_fixture(tmp_path, lease_seconds)
    store.armed = True
    store.target_index = 1 if phase == "observation_lock" else 2
    store.pause_after_observation = phase == "business_lock"
    blocker = sqlite3.connect(store.path, isolation_level=None, timeout=5)
    if phase == "observation_lock":
        blocker.execute("BEGIN IMMEDIATE")
    result = {}

    def write():
        try:
            if operation == "mutation":
                result["receipt"] = store.mutate_protected_resource(
                    *owner, 0, {"phase": phase, "outcome": outcome}, command_id="delayed-mutation"
                )
            else:
                result["receipt"] = store.renew_assignment(
                    *owner, lease_seconds=5, command_id="delayed-renew"
                )
        except BaseException as exc:
            result["exception"] = exc

    thread = threading.Thread(target=write)
    thread.start()
    try:
        if phase == "business_lock":
            assert store.observation_done.wait(5), "observation barrier timeout"
            blocker.execute("BEGIN IMMEDIATE")
            store.resume_business.set()
        assert store.begin_entered.wait(5), "lock acquisition barrier timeout"
        clock.set(BASE + dt.timedelta(seconds=11 if outcome == "expired" else (10 if outcome == "exact" else 11)))
        blocker.execute("COMMIT")
    finally:
        if blocker.in_transaction:
            blocker.execute("ROLLBACK")
        blocker.close()
        store.resume_business.set()
        thread.join(5)
    assert not thread.is_alive()
    store.armed = False
    if outcome == "valid":
        assert "exception" not in result
        if operation == "mutation":
            protected = store.get_protected_resource("resource")
            assert protected.version == 1
            assert protected.value.to_dict()["outcome"] == "valid"
        else:
            assert store.get_assignment(assignment.assignment_id).version == 1
    else:
        assert isinstance(result.get("exception"), (LeaseExpired, ClockAnomaly)), result
        assert store.get_assignment(assignment.assignment_id).version == 0
        assert store.get_protected_resource("resource").version == 0
        with closing(store.connect()) as independent:
            watermark = independent.execute(
                "SELECT last_coordinator_time FROM runtime_clock_state WHERE state_id=1"
            ).fetchone()[0]
        assert watermark.endswith("00:00:11.000000+00:00") or watermark.endswith("00:00:10.000000+00:00")


def test_expiry_rejection_persists_clock_before_business_rollback(tmp_path):
    store, clock, _, owner = _assigned(tmp_path)
    clock.advance(10)
    with pytest.raises(LeaseExpired):
        store.mutate_protected_resource(
            *owner,
            0,
            {"value": "expired"},
            command_id="expired-write",
        )
    assert store.get_protected_resource("resource-1").version == 0
    with closing(store.connect()) as independent:
        observed = independent.execute(
            "SELECT last_coordinator_time FROM runtime_clock_state WHERE state_id=1"
        ).fetchone()[0]
    assert observed == "2026-01-01T00:00:10.000000+00:00"


def test_authorized_business_commit_removes_safety_guard_atomically(tmp_path):
    store, clock, _, owner = _assigned(tmp_path, lease_seconds=20)
    clock.advance(1)
    receipt = store.mutate_protected_resource(
        *owner,
        0,
        {"value": "accepted"},
        command_id="guard-success",
    )
    assert receipt.version == 1
    assert store.get_safety_handoff(owner[0]) is None
    with closing(store.connect()) as conn:
        assert conn.execute(
            "SELECT last_coordinator_time FROM runtime_clock_state WHERE state_id=1"
        ).fetchone()[0] == "2026-01-01T00:00:01.000000+00:00"
        assert tuple(conn.execute(
            "SELECT version,value_json FROM runtime_protected_resources WHERE resource_key='resource-1'"
        ).fetchone()) == (1, '{"value":"accepted"}')
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_command_receipts WHERE command_id='guard-success'"
        ).fetchone()[0] == 1


def test_safety_operation_response_loss_retries_without_duplicate_commit(tmp_path):
    store, _, _, owner = _assigned(tmp_path, lease_seconds=20)
    fired = {"value": False}

    def lose_response(stage):
        if stage == "after_commit" and not fired["value"]:
            fired["value"] = True
            raise RuntimeError("mutation response lost")

    store.fault_injector = lose_response
    with pytest.raises(RuntimeError, match="mutation response lost"):
        store.mutate_protected_resource(
            *owner,
            0,
            {"value": "once"},
            command_id="guard-response-loss",
        )
    store.fault_injector = None
    retry = store.mutate_protected_resource(
        *owner,
        0,
        {"value": "once"},
        command_id="guard-response-loss",
    )
    assert retry.duplicate is True
    assert store.get_safety_handoff(owner[0]) is None
    assert store.get_protected_resource("resource-1").version == 1
    with closing(store.connect()) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_events WHERE stream_type='resource'"
        ).fetchone()[0] == 1


def test_safety_persist_failure_leaves_guard_and_fails_closed(tmp_path):
    store, clock, _, owner = _assigned(tmp_path)
    clock.set(BASE + dt.timedelta(seconds=11))

    def fail_safety_commit(stage):
        if stage == "safety_before_commit":
            raise RuntimeError("safety commit failed")

    store.fault_injector = fail_safety_commit
    with pytest.raises(RuntimeError, match="safety commit failed"):
        store.mutate_protected_resource(
            *owner,
            0,
            {"value": "must-not-commit"},
            command_id="guard-failure",
        )
    store.fault_injector = None
    assert store.get_protected_resource("resource-1").version == 0
    assert store.get_safety_handoff(owner[0]) is not None
    with closing(store.connect()) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_command_receipts WHERE command_id='guard-failure'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_events WHERE stream_type='resource'"
        ).fetchone()[0] == 0
    rollback_clock = RuntimeControlStore(
        store.path,
        clock=ManualClock(BASE + dt.timedelta(seconds=9.5)),
    )
    with pytest.raises(ClockAnomaly):
        rollback_clock.mutate_protected_resource(
            *owner,
            0,
            {"value": "old-authority"},
            command_id="guard-blocked",
        )
    current_clock = RuntimeControlStore(store.path, clock=ManualClock(BASE + dt.timedelta(seconds=11)))
    with pytest.raises(SafetyDecisionPending):
        current_clock.mutate_protected_resource(
            *owner,
            0,
            {"value": "pending-authority"},
            command_id="guard-blocked-current",
        )


def test_safety_handoff_recovery_waits_for_recorded_lease_bound(tmp_path):
    store, clock, _, owner = _assigned(tmp_path)
    clock.set(BASE + dt.timedelta(seconds=9))
    store._observe_coordinator_time()
    store._begin_safety_handoff(owner[0])
    reopened = RuntimeControlStore(store.path, clock=ManualClock(BASE + dt.timedelta(seconds=9.5)))
    with pytest.raises(RecoveryBlocked):
        reopened.recover_safety_handoff(
            owner[0], old_process_stopped=True, side_effect_fence_verified=True
        )
    reopened.clock.set(BASE + dt.timedelta(seconds=10))
    assert reopened.recover_safety_handoff(
        owner[0], old_process_stopped=True, side_effect_fence_verified=True
    ) is True
    with pytest.raises(LeaseExpired):
        reopened.mutate_protected_resource(
            *owner,
            0,
            {"value": "still-expired"},
            command_id="after-guard-recovery",
        )
    assert reopened.get_safety_handoff(owner[0]) is None
    assert reopened.get_protected_resource("resource-1").version == 0
    reopened = RuntimeControlStore(
        store.path,
        clock=ManualClock(BASE + dt.timedelta(seconds=5)),
    )
    with pytest.raises(ClockAnomaly):
        reopened.mutate_protected_resource(
            *owner,
            0,
            {"value": "resurrected"},
            command_id="rollback-write",
        )
    assert reopened.get_protected_resource("resource-1").version == 0


def test_successful_decision_and_failed_business_commit_both_latch_time(tmp_path):
    store, clock, _, owner = _assigned(tmp_path / "success", lease_seconds=20)
    clock.advance(6)
    store.mutate_protected_resource(*owner, 0, {"value": "accepted"}, command_id="accepted")
    reopened = RuntimeControlStore(
        store.path,
        clock=ManualClock(BASE + dt.timedelta(seconds=5)),
    )
    with pytest.raises(ClockAnomaly):
        reopened.mutate_protected_resource(*owner, 1, {"value": "old-clock"}, command_id="old-clock")
    assert reopened.get_protected_resource("resource-1").value.to_dict() == {"value": "accepted"}
    assert reopened.get_protected_resource("resource-1").version == 1

    failed, failed_clock, _, failed_owner = _assigned(tmp_path / "failed", lease_seconds=20)
    failed_clock.advance(8)

    def fail_before_commit(stage):
        if stage == "before_commit":
            raise RuntimeError("business rollback")

    failed.fault_injector = fail_before_commit
    with pytest.raises(RuntimeError, match="business rollback"):
        failed.mutate_protected_resource(
            *failed_owner,
            0,
            {"value": "must-roll-back"},
            command_id="rolled-back",
        )
    failed.fault_injector = None
    assert failed.get_protected_resource("resource-1").version == 0
    reopened_failed = RuntimeControlStore(
        failed.path,
        clock=ManualClock(BASE + dt.timedelta(seconds=7)),
    )
    with pytest.raises(ClockAnomaly):
        reopened_failed.mutate_protected_resource(
            *failed_owner,
            0,
            {"value": "must-not-commit"},
            command_id="after-rollback",
        )
    assert reopened_failed.get_protected_resource("resource-1").version == 0


def test_expired_renewal_without_reconciliation_cannot_revive_after_reopen(tmp_path):
    store, clock, receipt, owner = _assigned(tmp_path)
    clock.advance(10)
    with pytest.raises(LeaseExpired):
        store.renew_assignment(*owner, command_id="expired-renew")
    assert store.get_assignment(receipt.assignment_id).lifecycle == "ACTIVE"
    reopened = RuntimeControlStore(
        store.path,
        clock=ManualClock(BASE + dt.timedelta(seconds=9)),
    )
    with pytest.raises(ClockAnomaly):
        reopened.renew_assignment(*owner, command_id="rollback-renew")
    assert reopened.get_assignment(receipt.assignment_id).lease_expires_at.endswith(
        "00:00:10.000000+00:00"
    )


def test_older_coordinator_cannot_lower_time_watermark_or_write(tmp_path):
    store, clock, _, owner = _assigned(tmp_path, lease_seconds=30)
    newer_clock = ManualClock(BASE + dt.timedelta(seconds=10))
    newer = RuntimeControlStore(store.path, clock=newer_clock)
    newer.mutate_protected_resource(*owner, 0, {"writer": "newer"}, command_id="newer-write")
    assert store.get_protected_resource("resource-1").version == 1

    clock.set(BASE + dt.timedelta(seconds=9))
    with pytest.raises(ClockAnomaly):
        store.mutate_protected_resource(*owner, 1, {"writer": "older"}, command_id="older-write")
    assert store.get_protected_resource("resource-1").version == 1
    with closing(store.connect()) as independent:
        watermark = independent.execute(
            "SELECT last_coordinator_time FROM runtime_clock_state WHERE state_id=1"
        ).fetchone()[0]
    assert watermark == "2026-01-01T00:00:10.000000+00:00"


def test_reconciliation_response_loss_returns_original_batch_once(tmp_path):
    store, clock, receipt, _ = _assigned(tmp_path)
    clock.advance(10)
    fired = {"value": False}

    def lose_after_commit(stage):
        if stage == "after_commit" and not fired["value"]:
            fired["value"] = True
            raise RuntimeError("reconcile response lost")

    store.fault_injector = lose_after_commit
    with pytest.raises(RuntimeError, match="reconcile response lost"):
        store.reconcile_expired_once(command_id="reconcile-batch")
    store.fault_injector = None
    reopened = RuntimeControlStore(store.path, clock=ManualClock(BASE + dt.timedelta(seconds=10)))
    assert reopened.reconcile_expired_once(command_id="reconcile-batch") == (
        receipt.assignment_id,
    )
    events = reopened.read_events(
        stream_type="assignment",
        stream_id=receipt.assignment_id,
    )
    assert tuple(event.event_type for event in events) == (
        "AssignmentGranted",
        "AssignmentExpired",
    )
    with pytest.raises(RuntimeIdempotencyConflict):
        reopened.reconcile_expired_once(limit=1, command_id="reconcile-batch")


@pytest.mark.parametrize("lose_response", [False, True])
def test_generated_evidence_identity_recovers_receipt_across_reopen(tmp_path, lose_response):
    store, _, _, owner = _assigned(tmp_path)
    before = store.count_events()
    if lose_response:
        fired = {"value": False}

        def lose_after_commit(stage):
            if stage == "after_commit" and not fired["value"]:
                fired["value"] = True
                raise RuntimeError("response lost")

        store.fault_injector = lose_after_commit
        with pytest.raises(RuntimeError, match="response lost"):
            store.record_attempt_evidence(
                *owner,
                {"same": True},
                command_id="evidence-command",
            )
        store.fault_injector = None
    else:
        store.record_attempt_evidence(
            *owner,
            {"same": True},
            command_id="evidence-command",
        )
    reopened = RuntimeControlStore(store.path, clock=ManualClock(BASE))
    duplicate = reopened.record_attempt_evidence(
        *owner,
        {"same": True},
        command_id="evidence-command",
    )
    assert duplicate.duplicate is True
    with closing(sqlite3.connect(store.path)) as conn:
        evidence_ids = conn.execute("SELECT evidence_id FROM runtime_evidence").fetchall()
    assert evidence_ids == [(duplicate.evidence_id,)]
    assert reopened.count_events() == before + 1


def test_explicit_and_generated_evidence_ids_keep_semantic_conflicts(tmp_path):
    store, _, receipt, owner = _assigned(tmp_path)
    generated = store.record_attempt_evidence(
        *owner,
        {"kind": "generated"},
        command_id="generated",
    )
    assert generated.evidence_id.startswith("evidence_")
    assert store.record_attempt_evidence(
        *owner,
        {"kind": "generated"},
        command_id="generated",
    ).evidence_id == generated.evidence_id
    explicit = store.record_attempt_evidence(
        *owner,
        {"kind": "explicit"},
        evidence_id="caller-evidence-id",
        command_id="explicit",
    )
    assert explicit.evidence_id == "caller-evidence-id"
    assert store.record_attempt_evidence(
        *owner,
        {"kind": "explicit"},
        evidence_id="caller-evidence-id",
        command_id="explicit",
    ).duplicate is True
    with pytest.raises(RuntimeIdempotencyConflict):
        store.record_attempt_evidence(
            *owner,
            {"kind": "changed"},
            command_id="generated",
        )
    changed_owner = list(owner)
    changed_owner[1] = "other-worker"
    with pytest.raises(RuntimeIdempotencyConflict):
        store.record_attempt_evidence(
            *changed_owner,
            {"kind": "generated"},
            command_id="generated",
        )
    store.revoke_assignment(*owner, command_id="revoke")
    old = store.record_attempt_evidence(
        *owner,
        {"kind": "generated"},
        command_id="generated",
    )
    assert old.duplicate is True
    assert old.current_authority_valid is False
    assert store.get_assignment(receipt.assignment_id).lifecycle == "REVOKED"


def test_same_client_key_is_scoped_while_receipt_storage_identity_is_global(tmp_path):
    store, _ = _blank(tmp_path)
    task_a = store.register_task_reference("task-a", idempotency_key="create")
    task_b = store.register_task_reference("task-b", idempotency_key="create")
    worker_a = store.register_worker("worker-a", command_id="register")
    worker_b = store.register_worker("worker-b", command_id="register")
    assert {task_a.command_id, task_b.command_id} == {"create"}
    assert {worker_a.command_id, worker_b.command_id} == {"register"}
    assert len({task_a.receipt_id, task_b.receipt_id, worker_a.receipt_id, worker_b.receipt_id}) == 4
    assert store.register_task_reference("task-a", idempotency_key="create").duplicate is True
    assert store.register_task_reference("task-b", idempotency_key="create").duplicate is True
    with pytest.raises(RuntimeIdempotencyConflict):
        store.register_task_reference("task-a", source_system="changed", idempotency_key="create")


def _record_evidence_race(path, owner, barrier, queue):
    store = RuntimeControlStore(path, clock=ManualClock(BASE), busy_timeout_ms=5_000)
    barrier.wait()
    try:
        result = store.record_attempt_evidence(
            *owner,
            {"concurrent": True},
            command_id="same-evidence-command",
        )
        queue.put((result.duplicate, result.evidence_id))
    except Exception as exc:
        queue.put((type(exc).__name__, str(exc)))


def test_generated_evidence_concurrent_retry_creates_one_identity(tmp_path):
    store, _, _, owner = _assigned(tmp_path)
    before = store.count_events()
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    queue = context.Queue()
    processes = [
        context.Process(
            target=_record_evidence_race,
            args=(str(store.path), owner, barrier, queue),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert not process.is_alive()
        assert process.exitcode == 0
    outcomes = [queue.get(timeout=2) for _ in processes]
    if all(isinstance(item[0], bool) for item in outcomes):
        assert sorted(item[0] for item in outcomes) == [False, True]
        evidence_ids = {item[1] for item in outcomes}
    else:
        # The assignment guard may legitimately reject the overlapping call.
        # An exact retry after the winner commits must recover its receipt.
        assert sum(item[0] is False for item in outcomes) == 1
        assert sum(item[0] == "SafetyDecisionPending" for item in outcomes) == 1
        committed_id = next(item[1] for item in outcomes if item[0] is False)
        retry = store.record_attempt_evidence(
            *owner,
            {"concurrent": True},
            command_id="same-evidence-command",
        )
        assert retry.duplicate is True
        assert retry.evidence_id == committed_id
        evidence_ids = {committed_id, retry.evidence_id}
    assert len(evidence_ids) == 1
    with closing(sqlite3.connect(store.path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runtime_evidence").fetchone()[0] == 1
    assert store.count_events() == before + 1


def _assign_race(path, attempt_id, resource_key, command_id, barrier, queue):
    store = RuntimeControlStore(path, clock=ManualClock(BASE), busy_timeout_ms=5_000)
    barrier.wait()
    try:
        result = store.assign_attempt(
            attempt_id,
            "worker-1",
            "incarnation-1",
            resource_key,
            command_id=command_id,
        )
        queue.put(("committed", result.assignment_id))
    except Exception as exc:
        queue.put((type(exc).__name__, str(exc)))


@pytest.mark.parametrize("constraint", ["resource", "capacity"])
def test_multiprocess_resource_and_capacity_admission(constraint, tmp_path):
    store, _ = _blank(tmp_path)
    store.register_task_reference("task-1", command_id="task")
    for index in (1, 2):
        store.register_execution(
            "task-1",
            f"execution-{index}",
            command_id=f"execution-{index}",
        )
        store.register_attempt(
            f"execution-{index}",
            f"attempt-{index}",
            0,
            command_id=f"attempt-{index}",
        )
    store.register_worker(
        "worker-1",
        capacity=2 if constraint == "resource" else 1,
        command_id="worker",
    )
    store.register_incarnation(
        "worker-1",
        incarnation_id="incarnation-1",
        generation=1,
        command_id="incarnation",
    )
    resources = (
        ("shared-resource", "shared-resource")
        if constraint == "resource"
        else ("resource-1", "resource-2")
    )
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    queue = context.Queue()
    processes = [
        context.Process(
            target=_assign_race,
            args=(
                str(store.path),
                f"attempt-{index}",
                resources[index - 1],
                f"race-{index}",
                barrier,
                queue,
            ),
        )
        for index in (1, 2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert not process.is_alive()
        assert process.exitcode == 0
    outcomes = [queue.get(timeout=2) for _ in processes]
    assert [item[0] for item in outcomes].count("committed") == 1
    with closing(sqlite3.connect(store.path)) as conn:
        active_assignments = conn.execute(
            "SELECT COUNT(*) FROM runtime_assignments WHERE lifecycle='ACTIVE'"
        ).fetchone()[0]
        active_allocations = conn.execute(
            "SELECT COUNT(*) FROM runtime_allocations WHERE lifecycle='ACTIVE'"
        ).fetchone()[0]
    assert (active_assignments, active_allocations) == (1, 1)


def _assert_assignment_replay_matches_database(store, assignment_id):
    events = store.read_events(
        stream_type="assignment",
        stream_id=assignment_id,
        limit=100,
    )
    replay = store.replay_events(events)
    with closing(sqlite3.connect(store.path)) as conn:
        conn.row_factory = sqlite3.Row
        assignment = dict(
            conn.execute(
                "SELECT * FROM runtime_assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
        )
        allocation = dict(
            conn.execute(
                "SELECT * FROM runtime_allocations WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
        )
        attempt_row = conn.execute(
            "SELECT attempt_id,lifecycle,version FROM runtime_attempts WHERE attempt_id=?",
            (assignment["attempt_id"],),
        ).fetchone()
        recovery_row = conn.execute(
            "SELECT assignment_id,state,reason,attempts FROM runtime_recovery_work WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
    assert replay["state"]["assignment"] == assignment
    assert replay["state"]["allocation"] == allocation
    assert replay["state"]["attempt"] == dict(attempt_row)
    assert replay["state"]["recovery"] == (
        dict(recovery_row) if recovery_row is not None else None
    )
    assert replay["states"] == {assignment_id: assignment["lifecycle"]}
    return replay


@pytest.mark.parametrize(
    "transition,expected_events",
    [
        ("grant", ("AssignmentGranted",)),
        ("renew", ("AssignmentGranted", "AssignmentRenewed")),
        ("release", ("AssignmentGranted", "AssignmentReleased")),
        ("revoke", ("AssignmentGranted", "AssignmentRevoked")),
        ("expire", ("AssignmentGranted", "AssignmentExpired")),
        ("recover", ("AssignmentGranted", "AssignmentExpired", "AssignmentRecovered")),
        ("orphan", ("AssignmentGranted", "AssignmentOrphaned")),
    ],
)
def test_assignment_event_replay_matrix_matches_persisted_state(
    tmp_path,
    transition,
    expected_events,
):
    store, clock, receipt, owner = _assigned(tmp_path)
    if transition == "renew":
        clock.advance(1)
        store.renew_assignment(*owner, lease_seconds=20, command_id="renew")
    elif transition == "release":
        store.release_assignment(*owner, command_id="release")
    elif transition == "revoke":
        store.revoke_assignment(*owner, command_id="revoke")
    elif transition in {"expire", "recover"}:
        clock.advance(10)
        assert store.reconcile_expired_once() == (receipt.assignment_id,)
        if transition == "recover":
            store.recover_assignment(
                receipt.assignment_id,
                old_process_stopped=True,
                side_effect_fence_verified=True,
                command_id="recover",
            )
    elif transition == "orphan":
        store.register_incarnation(
            "worker-1",
            incarnation_id="incarnation-2",
            generation=2,
            command_id="replace-incarnation",
        )
    replay = _assert_assignment_replay_matches_database(store, receipt.assignment_id)
    assert replay["event_types"] == expected_events


def test_worker_incarnation_replay_tracks_current_and_superseded_generations(tmp_path):
    store, _ = _blank(tmp_path)
    store.register_worker("worker-1", capacity=2, command_id="worker")
    store.register_incarnation(
        "worker-1",
        incarnation_id="incarnation-1",
        generation=1,
        command_id="incarnation-1",
    )
    store.register_incarnation(
        "worker-1",
        incarnation_id="incarnation-2",
        generation=2,
        command_id="incarnation-2",
    )
    replay = store.replay_events(
        store.read_events(stream_type="worker", stream_id="worker-1", limit=100)
    )
    worker = store.get_worker("worker-1")
    assert replay["state"]["worker"]["current_incarnation_id"] == worker.current_incarnation_id
    assert replay["state"]["worker"]["version"] == worker.version
    for incarnation_id in ("incarnation-1", "incarnation-2"):
        actual = store.get_incarnation(incarnation_id)
        projected = replay["state"]["incarnations"][incarnation_id]
        assert (projected["generation"], projected["lifecycle"], projected["version"]) == (
            actual.generation,
            actual.lifecycle,
            actual.version,
        )
    assert "worker.capabilities" in replay["not_covered_fields"]


@pytest.mark.parametrize("heartbeats", [0, 1, 3])
def test_worker_and_incarnation_replay_versions_follow_heartbeat_events(tmp_path, heartbeats):
    store, clock = _blank(tmp_path)
    store.register_worker("worker-1", capacity=2, command_id="worker")
    store.register_incarnation(
        "worker-1", incarnation_id="incarnation-1", generation=1, command_id="incarnation-1"
    )
    for index in range(heartbeats):
        clock.advance(1)
        first = store.heartbeat_worker("worker-1", "incarnation-1", command_id=f"heartbeat-{index}")
        assert first.duplicate is False
    replay = store.replay_events(
        store.read_events(stream_type="worker", stream_id="worker-1", limit=100)
    )
    actual_worker = store.get_worker("worker-1")
    actual_incarnation = store.get_incarnation("incarnation-1")
    projected_worker = replay["state"]["worker"]
    projected_incarnation = replay["state"]["incarnations"]["incarnation-1"]
    assert replay["view"] == "aggregate"
    assert projected_worker["version"] == actual_worker.version == 1 + heartbeats
    assert projected_incarnation["version"] == actual_incarnation.version == heartbeats
    if heartbeats:
        assert projected_worker["last_heartbeat_at"] == actual_worker.last_heartbeat_at
        assert projected_incarnation["last_heartbeat_at"] == actual_incarnation.last_heartbeat_at


def test_worker_replay_tracks_heartbeat_then_replacement_and_reopen(tmp_path):
    store, clock = _blank(tmp_path)
    store.register_worker("worker-1", capacity=2, command_id="worker")
    store.register_incarnation(
        "worker-1", incarnation_id="incarnation-1", generation=1, command_id="incarnation-1"
    )
    clock.advance(1)
    store.heartbeat_worker("worker-1", "incarnation-1", command_id="heartbeat-1")
    clock.advance(1)
    store.register_incarnation(
        "worker-1", incarnation_id="incarnation-2", generation=2, command_id="incarnation-2"
    )
    replay = store.replay_events(
        store.read_events(stream_type="worker", stream_id="worker-1", limit=100)
    )
    assert replay["state"]["worker"]["version"] == store.get_worker("worker-1").version == 3
    for incarnation_id in ("incarnation-1", "incarnation-2"):
        actual = store.get_incarnation(incarnation_id)
        projected = replay["state"]["incarnations"][incarnation_id]
        assert (projected["lifecycle"], projected["version"]) == (actual.lifecycle, actual.version)
    reopened = RuntimeControlStore(store.path, clock=clock)
    replay_after_reopen = reopened.replay_events(
        reopened.read_events(stream_type="worker", stream_id="worker-1", limit=100)
    )
    assert replay_after_reopen["state"] == replay["state"]


def test_legacy_worker_events_report_unknown_versions_instead_of_inference(tmp_path):
    del tmp_path
    registered = {"worker_id": "legacy-worker", "capacity": 1}
    incarnation = {
        "worker_id": "legacy-worker", "incarnation_id": "legacy-incarnation", "generation": 1
    }
    events = [
        RuntimeEventRecord(
            event_id="legacy-worker-1", stream_type="worker", stream_id="legacy-worker", sequence=1,
            event_family="RUNTIME_WORKER_V1", event_type="WorkerRegistered", schema_version=1,
            occurred_at=BASE.isoformat(), recorded_at=BASE.isoformat(), payload=registered,
            payload_hash=_digest(registered),
        ),
        RuntimeEventRecord(
            event_id="legacy-worker-2", stream_type="worker", stream_id="legacy-worker", sequence=2,
            event_family="RUNTIME_WORKER_V1", event_type="WorkerIncarnationRegistered", schema_version=1,
            occurred_at=BASE.isoformat(), recorded_at=BASE.isoformat(), payload=incarnation,
            payload_hash=_digest(incarnation),
        ),
    ]
    replay = replay_runtime_events(events)
    assert replay["view"] == "registration_only"
    assert replay["state"]["worker"]["version"] == "UNKNOWN"
    assert replay["state"]["incarnations"]["legacy-incarnation"]["version"] == "UNKNOWN"
    assert "worker.version" in replay["not_covered_fields"]
    assert "incarnation[legacy-incarnation].version" in replay["not_covered_fields"]


def test_legacy_assignment_events_are_upcast_and_bad_events_are_rejected(tmp_path):
    grant_payload = {
        "assignment_id": "assignment-1",
        "attempt_id": "attempt-1",
        "worker_id": "worker-1",
        "incarnation_id": "incarnation-1",
        "resource_key": "resource-1",
        "resource_epoch": 1,
        "lease_expires_at": "2026-01-01T00:00:10.000000+00:00",
        "allocation_id": "allocation-1",
        "lifecycle": "ACTIVE",
    }
    orphan_payload = {
        "assignment_id": "assignment-1",
        "reason": "worker_incarnation_superseded",
    }
    grant = RuntimeEventRecord(
        event_id="event-1",
        stream_type="assignment",
        stream_id="assignment-1",
        sequence=1,
        event_family="RUNTIME_WORKER_V1",
        event_type="AssignmentGranted",
        schema_version=1,
        occurred_at="2026-01-01T00:00:00.000000+00:00",
        recorded_at="2026-01-01T00:00:00.000000+00:00",
        payload=grant_payload,
        payload_hash=_digest(grant_payload),
    )
    orphan = RuntimeEventRecord(
        event_id="event-2",
        stream_type="assignment",
        stream_id="assignment-1",
        sequence=2,
        event_family="RUNTIME_WORKER_V1",
        event_type="AssignmentOrphaned",
        schema_version=1,
        occurred_at="2026-01-01T00:00:01.000000+00:00",
        recorded_at="2026-01-01T00:00:01.000000+00:00",
        payload=orphan_payload,
        payload_hash=_digest(orphan_payload),
    )
    store = RuntimeControlStore(tmp_path / "unused.sqlite3", clock=ManualClock(BASE))
    replay = store.replay_events([grant, orphan])
    assert replay["states"] == {"assignment-1": "ORPHANED"}
    assert replay["state"]["allocation"]["lifecycle"] == "QUARANTINED"
    assert replay["state"]["allocation"]["version"] == 0
    with pytest.raises(RuntimeConflict, match="version gap"):
        store.replay_events([grant, dataclasses.replace(orphan, sequence=3)])
    with pytest.raises(RuntimeConflict, match="invalid for assignment"):
        store.replay_events([dataclasses.replace(grant, event_type="UnexpectedEvent")])
    with pytest.raises(RuntimeConflict, match="payload hash"):
        store.replay_events([dataclasses.replace(grant, payload_hash="0" * 64)])


def test_unknown_state_fields_are_not_covered_and_cannot_change_lifecycle(tmp_path):
    store, _, receipt, _ = _assigned(tmp_path)
    event = store.read_events(
        stream_type="assignment",
        stream_id=receipt.assignment_id,
    )[0]
    payload = event.payload.to_dict()
    payload["state"]["future_field"] = {"lifecycle": "REVOKED"}
    extended = dataclasses.replace(event, payload=payload, payload_hash=_digest(payload))
    replay = store.replay_events([extended])
    assert replay["states"][receipt.assignment_id] == "ACTIVE"
    assert replay["not_covered_fields"] == ("state.future_field",)
    payload["lifecycle"] = "REVOKED"
    conflicting = dataclasses.replace(event, payload=payload, payload_hash=_digest(payload))
    with pytest.raises(RuntimeConflict, match="illegal assignment lifecycle"):
        store.replay_events([conflicting])


def test_lossless_multistream_cursor_survives_page_changes_empty_page_and_reopen(tmp_path):
    store, _ = _blank(tmp_path)
    store.register_worker("worker-a", command_id="worker-a")
    store.register_incarnation(
        "worker-a",
        incarnation_id="incarnation-a",
        command_id="incarnation-a",
    )
    store.register_worker("worker-b", command_id="worker-b")
    expected = store.read_events(limit=100)
    first = store.scan_events(limit=2)
    second = store.scan_events(cursor=first.next_cursor, limit=1)
    empty = store.scan_events(cursor=second.next_cursor, limit=7)
    assert [event.event_id for event in first.events + second.events] == [
        event.event_id for event in expected
    ]
    assert empty.events == ()
    assert empty.next_cursor == second.next_cursor
    store.register_worker("worker-c", command_id="worker-c")
    reopened = RuntimeControlStore(store.path, clock=ManualClock(BASE))
    later = reopened.scan_events(cursor=empty.next_cursor, limit=3)
    assert [event.stream_id for event in later.events] == ["worker-c"]
    assert later.events[0].sequence == 1
    with pytest.raises(ValueError, match="scan_events cursor"):
        store.read_events(after_sequence=2, limit=2)


def test_multistream_cursor_never_advances_on_byte_rejection(tmp_path):
    store, _ = _blank(tmp_path)
    store.register_task_reference(
        "large-task",
        source_system="x" * 500,
        command_id="large-task",
    )
    with pytest.raises(PayloadSizeExceeded):
        store.scan_events(limit=1, max_payload_bytes=1)
    recovered = store.scan_events(limit=1, max_payload_bytes=2_000)
    assert len(recovered.events) == 1
    assert recovered.events[0].stream_id == "large-task"
    with pytest.raises(RuntimeConflict, match="invalid runtime event cursor"):
        store.scan_events(cursor="not-a-cursor")


def _create_fca699e9_v1_database(path: Path):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """CREATE TABLE runtime_schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                applied_at TEXT NOT NULL
            )"""
        )
        for statement in MIGRATIONS[0].statements:
            conn.execute(statement)
        stamp = "2026-01-01T00:00:00.000000+00:00"
        conn.execute(
            "INSERT INTO runtime_schema_migrations VALUES(1,?,?)",
            (MIGRATIONS[0].name, stamp),
        )
        conn.execute("INSERT INTO runtime_clock_state VALUES(1,?)", (stamp,))
        conn.execute(
            "INSERT INTO runtime_tasks VALUES('old-task','runtime_control',?)",
            (stamp,),
        )
        payload = {"task_id": "old-task", "source_system": "runtime_control"}
        semantic = {
            "operation": "register_task",
            "task_id": "old-task",
            "source_system": "runtime_control",
        }
        conn.execute("INSERT INTO runtime_stream_versions VALUES('task','old-task',1)")
        conn.execute(
            """INSERT INTO runtime_events VALUES(
                'old-event','task','old-task',1,'RUNTIME_WORKER_V1',
                'TaskReferenceRegistered',1,?,?,?,?,?,'legacy-create:event'
            )""",
            (stamp, stamp, _canonical(payload), _digest(payload), "task:old-task"),
        )
        conn.execute(
            """INSERT INTO runtime_command_receipts VALUES(
                'legacy-create','task:old-task','legacy-create',?,?,?
            )""",
            (_digest(semantic), _canonical({"task_id": "old-task"}), stamp),
        )
        conn.execute(
            """INSERT INTO runtime_outbox VALUES(
                'old-outbox','old-event','runtime_projection','old-outbox-key',
                '{}','PENDING',0,NULL
            )"""
        )
        conn.commit()


def test_fca699e9_schema_receipt_and_event_upgrade_is_additive(tmp_path):
    path = tmp_path / "old-runtime.sqlite3"
    _create_fca699e9_v1_database(path)
    with closing(sqlite3.connect(path)) as conn:
        before_event = conn.execute(
            "SELECT event_id,payload_json,payload_hash FROM runtime_events"
        ).fetchone()
    store = RuntimeControlStore(path, clock=ManualClock(BASE))
    store.initialize()
    assert store.schema_version() == 3
    duplicate = store.register_task_reference("old-task", command_id="legacy-create")
    assert duplicate.duplicate is True
    assert duplicate.command_id == "legacy-create"
    assert duplicate.receipt_id == "legacy-create"
    new_scope = store.register_task_reference("new-task", idempotency_key="legacy-create")
    assert new_scope.command_id == "legacy-create"
    assert new_scope.receipt_id != duplicate.receipt_id
    page = store.scan_events(limit=10)
    assert page.events[0].event_id == "old-event"
    assert page.events[0].global_position == 1
    with closing(sqlite3.connect(path)) as conn:
        after_event = conn.execute(
            "SELECT event_id,payload_json,payload_hash FROM runtime_events WHERE event_id='old-event'"
        ).fetchone()
        versions = conn.execute(
            "SELECT version FROM runtime_schema_migrations ORDER BY version"
        ).fetchall()
        receipt_count = conn.execute(
            "SELECT COUNT(*) FROM runtime_command_receipts WHERE command_id='legacy-create'"
        ).fetchone()[0]
    assert after_event == before_event
    assert versions == [(1,), (2,), (3,)]
    assert receipt_count == 2


@pytest.mark.parametrize("operation", OWNER_WRITE_OPERATIONS)
def test_all_owner_write_entries_successfully_complete_their_own_guard(tmp_path, operation):
    store, clock, _, owner = _assigned(tmp_path / operation, lease_seconds=30)
    clock.advance(1)
    receipt = _invoke_owner_write(store, owner, operation, f"guard-success-{operation}")
    assert receipt.duplicate is False
    assert store.get_safety_handoff(owner[0]) is None
    if operation == "mutate":
        assert store.get_protected_resource(owner[4]).version == 1
    elif operation == "renew":
        assert store.get_assignment(owner[0]).version == 1
    elif operation in {"release", "revoke"}:
        assert store.get_assignment(owner[0]).lifecycle == (
            "RELEASED" if operation == "release" else "REVOKED"
        )
    else:
        with closing(sqlite3.connect(store.path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM runtime_evidence").fetchone()[0] == 1


@pytest.mark.parametrize("operation", OWNER_WRITE_OPERATIONS)
def test_pending_guard_rejects_every_new_owner_write_without_business_side_effects(
    tmp_path, operation
):
    store, _, _, owner = _assigned(tmp_path / operation)
    blocker = RuntimeControlStore(store.path, clock=ManualClock(BASE))
    blocker._begin_safety_handoff(owner[0])
    before_assignment = store.get_assignment(owner[0])
    before_resource = store.get_protected_resource(owner[4])
    with closing(sqlite3.connect(store.path)) as conn:
        before_counts = tuple(
            conn.execute(
                "SELECT (SELECT COUNT(*) FROM runtime_evidence),"
                "(SELECT COUNT(*) FROM runtime_events),"
                "(SELECT COUNT(*) FROM runtime_command_receipts),"
                "(SELECT COUNT(*) FROM runtime_outbox)"
            ).fetchone()
        )
    with pytest.raises(SafetyDecisionPending):
        _invoke_owner_write(store, owner, operation, f"guard-pending-{operation}")
    assert store.get_assignment(owner[0]) == before_assignment
    assert store.get_protected_resource(owner[4]) == before_resource
    assert blocker.get_safety_handoff(owner[0]) is not None
    with closing(sqlite3.connect(store.path)) as conn:
        after_counts = tuple(
            conn.execute(
                "SELECT (SELECT COUNT(*) FROM runtime_evidence),"
                "(SELECT COUNT(*) FROM runtime_events),"
                "(SELECT COUNT(*) FROM runtime_command_receipts),"
                "(SELECT COUNT(*) FROM runtime_outbox)"
            ).fetchone()
        )
    assert after_counts == before_counts


@pytest.mark.parametrize("prior_operation", OWNER_WRITE_OPERATIONS)
@pytest.mark.parametrize("followup_operation", OWNER_WRITE_OPERATIONS)
def test_failed_owner_write_handoff_blocks_every_followup_entry(
    tmp_path, prior_operation, followup_operation
):
    store, _, _, owner = _assigned(tmp_path / f"{prior_operation}-{followup_operation}")
    with closing(sqlite3.connect(store.path)) as conn:
        before_receipts = conn.execute(
            "SELECT COUNT(*) FROM runtime_command_receipts"
        ).fetchone()[0]
        before_events = conn.execute("SELECT COUNT(*) FROM runtime_events").fetchone()[0]
        before_outbox = conn.execute("SELECT COUNT(*) FROM runtime_outbox").fetchone()[0]

    def fail_business_and_safety(stage):
        if stage in {"before_commit", "safety_before_commit"}:
            raise RuntimeError(f"injected {stage}")

    store.fault_injector = fail_business_and_safety
    with pytest.raises(RuntimeError, match="injected safety_before_commit"):
        _invoke_owner_write(store, owner, prior_operation, f"failed-{prior_operation}")
    store.fault_injector = None
    assert store.get_safety_handoff(owner[0]) is not None
    with pytest.raises(SafetyDecisionPending):
        _invoke_owner_write(store, owner, followup_operation, f"followup-{followup_operation}")
    assert store.get_safety_handoff(owner[0]) is not None
    assert store.get_assignment(owner[0]).lifecycle == "ACTIVE"
    assert store.get_assignment(owner[0]).version == 0
    assert store.get_protected_resource(owner[4]).version == 0
    with closing(sqlite3.connect(store.path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runtime_evidence").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM runtime_command_receipts").fetchone()[0] == before_receipts
        assert conn.execute("SELECT COUNT(*) FROM runtime_events").fetchone()[0] == before_events
        assert conn.execute("SELECT COUNT(*) FROM runtime_outbox").fetchone()[0] == before_outbox


def test_owner_rows_requires_persistent_current_transaction_token(tmp_path):
    store, _, _, owner = _assigned(tmp_path)
    token, _ = store._begin_safety_handoff(owner[0])
    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeAuthorizationError, match="durable handoff"):
            store._owner_rows(
                conn, *owner, BASE.isoformat(), handoff_token=None
            )
        with pytest.raises(SafetyDecisionPending, match="owned by another"):
            store._owner_rows(
                conn, *owner, BASE.isoformat(), handoff_token="wrong-token"
            )
        assignment, allocation = store._owner_rows(
            conn, *owner, BASE.isoformat(), handoff_token=token
        )
        assert assignment["assignment_id"] == owner[0]
        assert allocation["resource_epoch"] == owner[5]
        conn.execute("ROLLBACK")
    finally:
        conn.close()
    store._clear_safety_handoff(owner[0], token)


def test_pending_guard_does_not_cross_assignment_boundary(tmp_path):
    store, _, _, owner_a = _assigned(tmp_path, lease_seconds=30)
    store.register_attempt("execution-1", "attempt-2", 1, command_id="attempt-2")
    owner_b_receipt = store.assign_attempt(
        "attempt-2", "worker-1", "incarnation-1", "resource-2", command_id="assign-2"
    )
    owner_b = (
        owner_b_receipt.assignment_id,
        "worker-1",
        "incarnation-1",
        "attempt-2",
        "resource-2",
        owner_b_receipt.resource_epoch,
    )
    RuntimeControlStore(store.path, clock=ManualClock(BASE))._begin_safety_handoff(owner_a[0])
    receipt = store.mutate_protected_resource(
        *owner_b,
        0,
        {"assignment": "B"},
        command_id="assignment-b-write",
    )
    assert receipt.duplicate is False
    assert store.get_protected_resource("resource-2").version == 1
    assert store.get_safety_handoff(owner_a[0]) is not None


def test_evidence_handoff_guard_survives_child_exit_before_safety_publish(tmp_path):
    store, _, _, owner = _assigned(tmp_path, lease_seconds=30)
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    process = context.Process(
        target=_exit_during_evidence_handoff,
        args=(str(store.path), owner, queue),
    )
    process.start()
    process.join(10)
    assert not process.is_alive()
    assert process.exitcode == 17
    reopened = RuntimeControlStore(
        store.path,
        clock=ManualClock(BASE + dt.timedelta(seconds=9, microseconds=500_000)),
    )
    assert reopened.get_safety_handoff(owner[0]) is not None
    with pytest.raises(SafetyDecisionPending):
        reopened.mutate_protected_resource(
            *owner,
            0,
            {"must": "stay-blocked"},
            command_id="after-child-exit",
        )
    assert reopened.get_protected_resource(owner[4]).version == 0


def test_history_receipt_is_not_replayed_while_assignment_guard_is_pending(tmp_path):
    store, _, _, owner = _assigned(tmp_path, lease_seconds=30)
    first = store.record_attempt_evidence(
        *owner,
        {"receipt": "committed"},
        command_id="history-receipt",
    )
    blocker = RuntimeControlStore(store.path, clock=ManualClock(BASE))
    blocker._begin_safety_handoff(owner[0])
    with pytest.raises(SafetyDecisionPending):
        store.record_attempt_evidence(
            *owner,
            {"receipt": "committed"},
            command_id="history-receipt",
        )
    assert first.current_authority_valid is True
    assert store.get_safety_handoff(owner[0]) is not None
    with closing(sqlite3.connect(store.path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runtime_evidence").fetchone()[0] == 1
