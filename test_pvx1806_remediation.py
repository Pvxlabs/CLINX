import dataclasses
import datetime as dt
import hashlib
import json
import multiprocessing
import sqlite3
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
    StaleMutation,
)
from runtime_control.schema import MIGRATIONS


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
    assert sorted(item[0] for item in outcomes) == [False, True]
    assert len({item[1] for item in outcomes}) == 1
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
    assert store.schema_version() == 2
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
    assert versions == [(1,), (2,)]
    assert receipt_count == 2
