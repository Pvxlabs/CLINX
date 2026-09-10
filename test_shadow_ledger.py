import dataclasses
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from domain import Event, EventActor, JsonDocument
from shadow_ledger import (
    AggregateVersionConflict,
    AppendRequest,
    EventStore,
    IdempotencyConflict,
    LedgerBusy,
    LedgerSchemaError,
    LedgerValidationError,
    PayloadSizeExceeded,
    ReplayBoundaryExceeded,
    OutboxRequest,
    ReplayError,
    TransactionOwnershipError,
)
from shadow_ledger.models import StoredEvent
from shadow_ledger.replay import ReplayReducer, ReplayService
from task_registry import TaskRegistry


ACTOR = EventActor("v1_registry", "task_registry")


def exact_request(
    execution_id,
    source_execution_ref,
    expected_version,
    event_type,
    key,
    *,
    payload=None,
    correlation_id=None,
    outbox=True,
):
    body = {
        "task_id": "shadow_task_1",
        "source_task_id": "task_v1",
        "execution_id": execution_id,
        "source_execution_ref": source_execution_ref,
        "attribution": "EXACT",
    }
    body.update(payload or {})
    messages = (
        OutboxRequest("shadow_projection", key, {"event_type": event_type}),
    ) if outbox else ()
    return AppendRequest(
        aggregate_type="execution",
        aggregate_id=execution_id,
        expected_version=expected_version,
        event_type=event_type,
        actor=ACTOR,
        idempotency_scope=f"execution:{execution_id}",
        idempotency_key=key,
        payload=body,
        occurred_at="2026-09-10T00:00:00+00:00",
        correlation_id=correlation_id or source_execution_ref,
        outbox=messages,
    )


def task_request(task_id, expected_version, event_type, key, *, payload=None):
    body = {
        "task_id": task_id,
        "source_task_id": "task_v1",
        "attribution": "UNATTRIBUTED",
    }
    body.update(payload or {})
    return AppendRequest(
        aggregate_type="task",
        aggregate_id=task_id,
        expected_version=expected_version,
        event_type=event_type,
        actor=ACTOR,
        idempotency_scope=f"task:{task_id}",
        idempotency_key=key,
        payload=body,
    )


def _multiprocess_append(db, execution_id, key, gate, queue):
    store = EventStore(db, busy_timeout_ms=5_000)
    gate.wait()
    try:
        result = store.append(
            exact_request(
                execution_id,
                "exec_v1",
                0,
                "V1ExecutionClaimObserved",
                key,
                payload={
                    "execution_state": "CLAIMED",
                    "current_stage": "CLAIMED",
                    "lease_state": "HELD",
                    "history_before_baseline": "UNKNOWN",
                },
            )
        )
        queue.put(("committed", result.stored_event.event.event_id))
    except Exception as exc:
        queue.put((type(exc).__name__, str(exc)))


def _crash_before_commit(db, execution_id):
    def crash(stage):
        if stage == "after_event_append":
            os._exit(17)

    store = EventStore(db, fault_injector=crash)
    store.append(
        exact_request(
            execution_id,
            "exec_v1",
            0,
            "V1ExecutionClaimObserved",
            "crash-before",
            payload={
                "execution_state": "CLAIMED",
                "current_stage": "CLAIMED",
                "lease_state": "HELD",
                "history_before_baseline": "UNKNOWN",
            },
        )
    )


def _commit_then_exit(db, execution_id):
    store = EventStore(db)
    store.append(
        exact_request(
            execution_id,
            "exec_v1",
            0,
            "V1ExecutionClaimObserved",
            "commit-before-response",
            payload={
                "execution_state": "CLAIMED",
                "current_stage": "CLAIMED",
                "lease_state": "HELD",
                "history_before_baseline": "UNKNOWN",
            },
        )
    )
    os._exit(18)


class EventStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "ledger.sqlite3"
        self.store = EventStore(self.db)
        self.store.initialize()
        self.execution_id = self.store.map_legacy_identity(
            source_system="clinx_v1",
            source_type="execution_ref",
            source_identity="exec_v1",
            target_type="execution",
        ).target_id

    def tearDown(self):
        self.temp.cleanup()

    def claim_request(self, key="claim"):
        return exact_request(
            self.execution_id,
            "exec_v1",
            0,
            "V1ExecutionClaimObserved",
            key,
            payload={
                "execution_state": "CLAIMED",
                "current_stage": "CLAIMED",
                "lease_state": "HELD",
                "history_before_baseline": "UNKNOWN",
            },
        )

class PersistenceAndIdempotencyTests(EventStoreTestCase):
    def test_uninitialized_and_unknown_schema_fail_explicitly(self):
        missing = Path(self.temp.name) / "uninitialized.sqlite3"
        store = EventStore(missing)
        with self.assertRaises(LedgerSchemaError):
            store.append(task_request("shadow_task_1", 0, "V1TaskObserved", "key"))
        with self.assertRaises(LedgerValidationError):
            dataclasses.replace(self.claim_request(), schema_version=2)

    def test_schema_migration_is_explicit_repeatable_and_persistent(self):
        self.assertEqual(self.store.schema_version(), 1)
        self.store.initialize()
        result = self.store.append(self.claim_request())

        reopened = EventStore(self.db)
        self.assertEqual(reopened.schema_version(), 1)
        self.assertEqual(reopened.current_version("execution", self.execution_id), 1)
        self.assertEqual(
            reopened.get_event(result.stored_event.event.event_id),
            result.stored_event,
        )

    def test_exact_retry_returns_first_event_without_duplicate_outbox(self):
        first = self.store.append(self.claim_request())
        retry = self.store.append(self.claim_request())

        self.assertFalse(first.duplicate)
        self.assertTrue(retry.duplicate)
        self.assertEqual(first.stored_event.event.event_id, retry.stored_event.event.event_id)
        self.assertEqual(self.store.count_events(), 1)
        self.assertEqual(len(self.store.list_outbox()), 1)
        self.assertEqual(self.store.current_version("execution", self.execution_id), 1)

    def test_same_scoped_key_with_different_semantics_conflicts(self):
        self.store.append(self.claim_request())
        conflict = dataclasses.replace(
            self.claim_request(),
            payload={
                **self.claim_request().payload.to_dict(),
                "current_stage": "DIFFERENT",
            },
        )

        with self.assertRaises(IdempotencyConflict):
            self.store.append(conflict)
        self.assertEqual(self.store.count_events(), 1)

    def test_payload_is_deeply_snapshotted_and_non_finite_values_fail(self):
        nested = {"items": [{"value": "before"}]}
        request = self.claim_request()
        request = dataclasses.replace(
            request,
            payload={**request.payload.to_dict(), "nested": nested},
        )
        nested["items"][0]["value"] = "after"
        result = self.store.append(request)
        self.assertEqual(
            result.stored_event.event.payload.to_dict()["nested"]["items"][0]["value"],
            "before",
        )

        with self.assertRaises(LedgerValidationError):
            dataclasses.replace(request, payload={"not_finite": float("nan")})

    def test_append_rejects_payload_above_write_budget_with_distinct_error(self):
        request = dataclasses.replace(
            self.claim_request(),
            payload={
                **self.claim_request().payload.to_dict(),
                "large_utf8": "界" * 128,
            },
        )
        bounded = EventStore(self.db, max_append_payload_bytes=128)
        with self.assertRaises(PayloadSizeExceeded):
            bounded.append(request)
        self.assertEqual(self.store.count_events(), 0)

    def test_append_accepts_payload_exactly_at_utf8_write_budget(self):
        request = dataclasses.replace(
            self.claim_request(),
            payload={
                **self.claim_request().payload.to_dict(),
                "multibyte": "界" * 16,
            },
        )
        budget = len(request.payload.to_json().encode("utf-8"))
        bounded = EventStore(self.db, max_append_payload_bytes=budget)
        result = bounded.append(request)
        self.assertFalse(result.duplicate)
        self.assertEqual(
            len(result.stored_event.event.payload.to_json().encode("utf-8")),
            budget,
        )

    def test_exact_retry_of_historical_large_payload_survives_lower_write_budget(self):
        db = Path(self.temp.name) / "historical-large.sqlite3"
        request = dataclasses.replace(
            self.claim_request(),
            payload={
                **self.claim_request().payload.to_dict(),
                "historical": "x" * 512,
            },
        )
        writer = EventStore(db, max_append_payload_bytes=2_000_000)
        writer.initialize()
        first = writer.append(request)
        payload_size = len(request.payload.to_json().encode("utf-8"))
        self.assertGreater(payload_size, 128)

        bounded_retry = EventStore(db, max_append_payload_bytes=128)
        retry = bounded_retry.append(request)
        self.assertTrue(retry.duplicate)
        self.assertEqual(retry.stored_event.event.event_id, first.stored_event.event.event_id)

    def test_read_preflights_existing_oversized_payload_before_full_select(self):
        writer = EventStore(self.db, max_append_payload_bytes=2_000_000)
        writer.append(
            dataclasses.replace(
                self.claim_request(),
                payload={
                    **self.claim_request().payload.to_dict(),
                    "large_utf8": "界" * 1024,
                },
            )
        )

        class TracingStore(EventStore):
            def __init__(self, path):
                super().__init__(path)
                self.statements = []
                self.materialized_payload_bytes = 0

            def _connect(self, *, read_only=False):
                conn = super()._connect(read_only=read_only)
                conn.set_trace_callback(self.statements.append)
                def row_factory(cursor, values):
                    for index, description in enumerate(cursor.description or ()):
                        if (
                            description[0] == "payload_json"
                            and isinstance(values[index], str)
                        ):
                            self.materialized_payload_bytes += len(
                                values[index].encode("utf-8")
                            )
                    return sqlite3.Row(cursor, values)
                conn.row_factory = row_factory
                return conn

        reader = TracingStore(self.db)
        with self.assertRaises(ReplayBoundaryExceeded):
            reader.read_events(limit=1, max_payload_bytes=128)
        self.assertFalse(
            any(
                "SELECT * FROM v2_events WHERE" in statement
                for statement in reader.statements
            ),
            reader.statements,
        )
        self.assertEqual(reader.materialized_payload_bytes, 0)

    def test_read_budget_is_utf8_exact_and_rejects_one_byte_below(self):
        request = dataclasses.replace(
            self.claim_request(),
            payload={
                **self.claim_request().payload.to_dict(),
                "multibyte": "界" * 16,
            },
        )
        writer = EventStore(self.db, max_append_payload_bytes=2_000_000)
        writer.append(request)
        budget = len(request.payload.to_json().encode("utf-8"))

        self.assertEqual(
            len(self.store.read_events(limit=1, max_payload_bytes=budget)), 1
        )
        with self.assertRaises(ReplayBoundaryExceeded):
            self.store.read_events(limit=1, max_payload_bytes=budget - 1)

    def test_read_cumulative_budget_does_not_skip_unread_events(self):
        first = self.store.append(self.claim_request("budget-first"))
        second = self.store.append(
            exact_request(
                self.execution_id,
                "exec_v1",
                1,
                "V1ExecutionProgressObserved",
                "budget-second",
                payload={
                    "execution_state": "CODEX_RUNNING",
                    "current_stage": "provider_wait",
                    "turn_id": "turn_budget",
                },
            )
        )
        first_size = len(first.stored_event.event.payload.to_json().encode("utf-8"))
        second_size = len(second.stored_event.event.payload.to_json().encode("utf-8"))
        budget = max(first_size, second_size)
        self.assertGreater(first_size + second_size, budget)

        with self.assertRaises(ReplayBoundaryExceeded):
            self.store.read_events(limit=2, max_payload_bytes=budget)

        page = self.store.read_events(limit=1, max_payload_bytes=budget)
        self.assertEqual(page[0].cursor, first.stored_event.cursor)
        self.assertEqual(
            self.store.read_events(
                after_cursor=page[-1].cursor,
                limit=1,
                max_payload_bytes=budget,
            )[0].cursor,
            second.stored_event.cursor,
        )

    def test_read_exception_closes_connection_and_cursor_resources(self):
        connections = []

        class TrackingStore(EventStore):
            def _connect(self, *, read_only=False):
                connection = super()._connect(read_only=read_only)
                connections.append(connection)
                return connection

        writer = EventStore(self.db, max_append_payload_bytes=2_000_000)
        writer.append(
            dataclasses.replace(
                self.claim_request(),
                payload={
                    **self.claim_request().payload.to_dict(),
                    "large_utf8": "界" * 1024,
                },
            )
        )
        reader = TrackingStore(self.db)
        with self.assertRaises(ReplayBoundaryExceeded):
            reader.read_events(limit=1, max_payload_bytes=128)
        self.assertEqual(len(connections), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute("SELECT 1")

    def test_append_only_triggers_reject_update_and_delete(self):
        result = self.store.append(self.claim_request())
        conn = sqlite3.connect(self.db)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                conn.execute(
                    "UPDATE v2_events SET event_type='changed' WHERE event_id=?",
                    (result.stored_event.event.event_id,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                conn.execute(
                    "DELETE FROM v2_events WHERE event_id=?",
                    (result.stored_event.event.event_id,),
                )
        finally:
            conn.close()


class ConcurrencyAndCrashTests(EventStoreTestCase):
    def test_sqlite_lock_contention_is_distinct_from_version_conflict(self):
        blocker = self.store._connect()
        try:
            blocker.execute("BEGIN IMMEDIATE")
            contender = EventStore(self.db, busy_timeout_ms=1)
            with self.assertRaises(LedgerBusy):
                contender.append(self.claim_request())
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()

    def test_participating_uow_never_begins_or_commits_outer_transaction(self):
        conn = self.store._connect()
        try:
            with self.assertRaises(TransactionOwnershipError):
                with self.store.unit_of_work(conn):
                    pass
            conn.execute("BEGIN IMMEDIATE")
            with self.store.unit_of_work(conn) as unit:
                unit.append(self.claim_request())
            self.assertTrue(conn.in_transaction)
            conn.execute("ROLLBACK")
        finally:
            conn.close()

        self.assertEqual(self.store.count_events(), 0)
        self.assertEqual(self.store.current_version("execution", self.execution_id), 0)

    def test_two_independent_connections_reject_stale_expected_version(self):
        first = EventStore(self.db)
        second = EventStore(self.db)
        first.append(self.claim_request("first"))

        with self.assertRaises(AggregateVersionConflict):
            second.append(self.claim_request("second"))
        self.assertEqual(self.store.count_events(), 1)
        self.assertEqual(len(self.store.list_outbox()), 1)

    def test_real_multiprocess_expected_version_race_has_one_winner(self):
        context = multiprocessing.get_context("fork")
        gate = context.Event()
        queue = context.Queue()
        processes = [
            context.Process(
                target=_multiprocess_append,
                args=(self.db, self.execution_id, f"process-{index}", gate, queue),
            )
            for index in range(2)
        ]
        for process in processes:
            process.start()
        gate.set()
        for process in processes:
            process.join(10)
            self.assertFalse(process.is_alive())
        outcomes = sorted(queue.get(timeout=2)[0] for _ in processes)

        self.assertEqual(outcomes, ["AggregateVersionConflict", "committed"])
        self.assertEqual(self.store.count_events(), 1)
        self.assertEqual(len(self.store.list_outbox()), 1)

    def test_fault_after_event_and_outbox_each_rolls_back(self):
        for stage in ("after_event_append", "after_outbox_insert"):
            with self.subTest(stage=stage):
                db = Path(self.temp.name) / f"{stage}.sqlite3"
                store = EventStore(
                    db,
                    fault_injector=lambda actual, expected=stage: (
                        (_ for _ in ()).throw(RuntimeError(expected))
                        if actual == expected else None
                    ),
                )
                store.initialize()
                mapped = store.map_legacy_identity(
                    source_system="clinx_v1",
                    source_type="execution_ref",
                    source_identity="exec_v1",
                    target_type="execution",
                ).target_id
                with self.assertRaisesRegex(RuntimeError, stage):
                    store.append(
                        exact_request(
                            mapped,
                            "exec_v1",
                            0,
                            "V1ExecutionClaimObserved",
                            stage,
                            payload={
                                "execution_state": "CLAIMED",
                                "current_stage": "CLAIMED",
                                "lease_state": "HELD",
                            },
                        )
                    )
                self.assertEqual(store.count_events(), 0)
                self.assertEqual(store.current_version("execution", mapped), 0)
                self.assertEqual(len(store.list_outbox()), 0)

    def test_process_exit_before_commit_leaves_no_partial_append(self):
        context = multiprocessing.get_context("fork")
        process = context.Process(
            target=_crash_before_commit, args=(self.db, self.execution_id)
        )
        process.start()
        process.join(10)

        self.assertEqual(process.exitcode, 17)
        self.assertEqual(self.store.count_events(), 0)
        self.assertEqual(self.store.current_version("execution", self.execution_id), 0)
        self.assertEqual(len(self.store.list_outbox()), 0)

    def test_commit_then_response_loss_is_recovered_by_exact_retry(self):
        context = multiprocessing.get_context("fork")
        process = context.Process(
            target=_commit_then_exit, args=(self.db, self.execution_id)
        )
        process.start()
        process.join(10)
        self.assertEqual(process.exitcode, 18)

        retry = self.store.append(
            exact_request(
                self.execution_id,
                "exec_v1",
                0,
                "V1ExecutionClaimObserved",
                "commit-before-response",
                payload={
                    "execution_state": "CLAIMED",
                    "current_stage": "CLAIMED",
                    "lease_state": "HELD",
                    "history_before_baseline": "UNKNOWN",
                },
            )
        )
        self.assertTrue(retry.duplicate)
        self.assertEqual(self.store.count_events(), 1)
        self.assertEqual(len(self.store.list_outbox()), 1)


class InboxOutboxTests(EventStoreTestCase):
    def test_inbox_deduplicates_by_native_identity_not_payload_hash(self):
        first = self.store.receive_inbox(
            source="fake-provider", dedup_key="event-1", payload={"same": True}
        )
        duplicate = self.store.receive_inbox(
            source="fake-provider", dedup_key="event-1", payload={"same": True}
        )
        independent = self.store.receive_inbox(
            source="fake-provider", dedup_key="event-2", payload={"same": True}
        )

        self.assertEqual(first.receipt_id, duplicate.receipt_id)
        self.assertNotEqual(first.receipt_id, independent.receipt_id)
        with self.assertRaises(IdempotencyConflict):
            self.store.receive_inbox(
                source="fake-provider",
                dedup_key="event-1",
                payload={"same": False},
            )

    def test_interrupted_normalization_recovers_atomically(self):
        receipt = self.store.receive_inbox(
            source="fake-provider", dedup_key="native-1", payload={"status": "done"}
        )
        request = exact_request(
            self.execution_id,
            "exec_v1",
            0,
            "V1ExecutionClaimObserved",
            "normalized-1",
            correlation_id=receipt.receipt_id,
            payload={
                "execution_state": "CLAIMED",
                "current_stage": "CLAIMED",
                "lease_state": "HELD",
                "history_before_baseline": "UNKNOWN",
            },
        )
        self.store.fault_injector = lambda stage: (
            (_ for _ in ()).throw(RuntimeError("normalize interrupted"))
            if stage == "before_inbox_processed" else None
        )
        with self.assertRaisesRegex(RuntimeError, "normalize interrupted"):
            self.store.normalize_inbox(receipt.receipt_id, request)

        self.assertEqual(self.store.get_inbox(receipt.receipt_id).state, "RECEIVED")
        self.assertEqual(self.store.count_events(), 0)
        self.store.fault_injector = None
        result = self.store.normalize_inbox(receipt.receipt_id, request)
        self.assertEqual(self.store.get_inbox(receipt.receipt_id).state, "PROCESSED")
        self.assertEqual(
            self.store.get_inbox(receipt.receipt_id).normalized_event_id,
            result.stored_event.event.event_id,
        )

    def test_unattributed_inbox_can_be_quarantined_without_guessing(self):
        receipt = self.store.receive_inbox(
            source="fake-provider", dedup_key="unknown", payload={"status": "done"}
        )
        quarantined = self.store.quarantine_inbox(
            receipt.receipt_id, "missing exact execution correlation"
        )
        self.assertEqual(quarantined.state, "QUARANTINED")
        self.assertIsNone(quarantined.normalized_event_id)

    def test_fake_outbox_consumer_failure_is_retained_for_retry(self):
        result = self.store.append(self.claim_request())
        outbox_id = result.outbox[0].outbox_id
        failed = self.store.deliver_outbox_once(
            outbox_id, lambda _record: (_ for _ in ()).throw(RuntimeError("offline"))
        )
        self.assertEqual(failed.state, "RETRY")
        self.assertEqual(failed.attempts, 1)
        self.assertEqual(len(self.store.list_outbox(states=("RETRY",))), 1)

        applied = self.store.deliver_outbox_once(outbox_id, lambda _record: None)
        self.assertEqual(applied.state, "APPLIED")
        self.assertEqual(applied.attempts, 2)


class ReplayTests(EventStoreTestCase):
    def append_trace(self):
        requests = (
            self.claim_request(),
            exact_request(
                self.execution_id,
                "exec_v1",
                1,
                "V1ExecutionProgressObserved",
                "progress",
                payload={
                    "execution_state": "CODEX_RUNNING",
                    "current_stage": "CODEX_RUNNING",
                    "turn_id": "turn_1",
                },
            ),
            exact_request(
                self.execution_id,
                "exec_v1",
                2,
                "V1ExecutionResultPersistedObserved",
                "result",
                payload={"exact_result_ref": "exec_v1", "turn_id": "turn_1"},
            ),
            exact_request(
                self.execution_id,
                "exec_v1",
                3,
                "V1TerminalStateObserved",
                "terminal",
                payload={
                    "execution_state": "COMPLETED",
                    "current_stage": "COMPLETED",
                    "turn_id": "turn_1",
                },
            ),
            exact_request(
                self.execution_id,
                "exec_v1",
                4,
                "V1LeaseReleasedObserved",
                "release",
                payload={"lease_state": "RELEASED"},
            ),
        )
        return [self.store.append(request) for request in requests]

    def test_replay_is_deterministic_and_compares_fixed_fields(self):
        self.store.append(
            dataclasses.replace(
                self.claim_request(),
                payload={
                    **self.claim_request().payload.to_dict(),
                    "resource_key": "worktree-a",
                },
            )
        )
        self.store.append(
            exact_request(
                self.execution_id,
                "exec_v1",
                1,
                "V1ExecutionProgressObserved",
                "progress",
                payload={
                    "execution_state": "CODEX_RUNNING",
                    "current_stage": "CODEX_RUNNING",
                    "turn_id": "turn_1",
                },
            )
        )
        self.store.append(
            exact_request(
                self.execution_id,
                "exec_v1",
                2,
                "V1ExecutionResultPersistedObserved",
                "result",
                payload={
                    "exact_result_ref": "exec_v1",
                    "turn_id": "turn_1",
                    "result_status": "PASS",
                },
            )
        )
        self.store.append(
            exact_request(
                self.execution_id,
                "exec_v1",
                3,
                "V1TerminalStateObserved",
                "terminal",
                payload={
                    "execution_state": "COMPLETED",
                    "current_stage": "COMPLETED",
                    "turn_id": "turn_1",
                },
            )
        )
        self.store.append(
            exact_request(
                self.execution_id,
                "exec_v1",
                4,
                "V1LeaseReleasedObserved",
                "release",
                payload={"lease_state": "RELEASED", "resource_key": "worktree-a"},
            )
        )
        service = ReplayService(self.store)
        first = service.replay_stream("execution", self.execution_id, batch_size=2)
        second = service.replay_stream("execution", self.execution_id, batch_size=1)

        self.assertEqual(first, second)
        expected = {
            "aggregate_type": "execution",
            "task_id": "shadow_task_1",
            "execution_id": self.execution_id,
            "source_task_id": "task_v1",
            "source_execution_ref": "exec_v1",
            "execution_state": "COMPLETED",
            "current_stage": "COMPLETED",
            "exact_result_ref": "exec_v1",
            "result_status": "PASS",
            "turn_id": "turn_1",
            "lease_state": "RELEASED",
            "resource_key": "worktree-a",
            "stream_version": 5,
            "attribution": "EXACT",
        }
        self.assertEqual(service.compare(first, expected).status, "PASS")
        expected["current_stage"] = "FAILED"
        comparison = service.compare(first, expected)
        self.assertEqual(comparison.status, "FAIL")
        self.assertEqual(comparison.differences[0].field, "current_stage")

    def test_comparison_requires_result_status_and_resource_key_from_v1_fixture(self):
        self.store.append(
            dataclasses.replace(
                self.claim_request(),
                payload={
                    **self.claim_request().payload.to_dict(),
                    "resource_key": "worktree-a",
                },
            )
        )
        self.store.append(
            exact_request(
                self.execution_id,
                "exec_v1",
                1,
                "V1ExecutionResultPersistedObserved",
                "result-status",
                payload={
                    "exact_result_ref": "exec_v1",
                    "turn_id": "turn_1",
                    "result_status": "PASS",
                },
            )
        )
        replayed = ReplayService(self.store).replay_stream(
            "execution", self.execution_id
        )
        self.assertEqual(replayed.result_status, "PASS")
        self.assertEqual(replayed.resource_key, "worktree-a")
        with self.assertRaises(ReplayError):
            ReplayService.compare(replayed, {})

        expected = {
            "aggregate_type": "execution",
            "task_id": "shadow_task_1",
            "execution_id": self.execution_id,
            "source_task_id": "task_v1",
            "source_execution_ref": "exec_v1",
            "execution_state": "CLAIMED",
            "current_stage": "CLAIMED",
            "exact_result_ref": "exec_v1",
            "result_status": "PASS",
            "turn_id": "turn_1",
            "lease_state": "HELD",
            "resource_key": "worktree-a",
            "stream_version": 2,
            "attribution": "EXACT",
        }
        self.assertEqual(ReplayService.compare(replayed, expected).status, "PASS")
        blocked = dict(expected)
        blocked["result_status"] = "BLOCKED"
        self.assertEqual(ReplayService.compare(replayed, blocked).status, "FAIL")
        other_resource = dict(expected)
        other_resource["resource_key"] = "worktree-b"
        self.assertEqual(ReplayService.compare(replayed, other_resource).status, "FAIL")

    def test_legacy_task_stream_replays_two_real_resource_cycles(self):
        task_id = self.store.map_legacy_identity(
            source_system="clinx_v1",
            source_type="task_id",
            source_identity="task_v1",
            target_type="task",
        ).target_id
        requests = (
            task_request(
                task_id, 0, "V1UnattributedExecutionClaimObserved", "cycle-1-claim",
                payload={
                    "execution_state": "CLAIMED",
                    "current_stage": "CLAIMED",
                    "lease_state": "HELD",
                    "resource_key": "worktree-a",
                },
            ),
            task_request(
                task_id, 1, "V1UnattributedLeaseReleasedObserved", "cycle-1-release",
                payload={"lease_state": "RELEASED", "resource_key": "worktree-a"},
            ),
            task_request(
                task_id, 2, "V1UnattributedExecutionClaimObserved", "cycle-2-claim",
                payload={
                    "execution_state": "CLAIMED",
                    "current_stage": "CLAIMED",
                    "lease_state": "HELD",
                    "resource_key": "worktree-b",
                },
            ),
            task_request(
                task_id, 3, "V1UnattributedLeaseReleasedObserved", "cycle-2-release",
                payload={"lease_state": "RELEASED", "resource_key": "worktree-b"},
            ),
        )
        events = [self.store.append(request).stored_event for request in requests]
        replayed = ReplayReducer().replay(events)
        self.assertEqual(replayed.attribution, "UNATTRIBUTED")
        self.assertIsNone(replayed.execution_id)
        self.assertEqual(replayed.lease_state, "RELEASED")
        self.assertEqual(replayed.resource_key, "worktree-b")

        duplicate_claim = dataclasses.replace(
            events[0],
            cursor=2,
            event=dataclasses.replace(
                events[0].event,
                aggregate_version=2,
                event_id="duplicate-claim",
            ),
        )
        with self.assertRaises(ReplayError):
            ReplayReducer().replay((events[0], duplicate_claim))

    def test_legacy_release_requires_held_lease_and_matching_resource(self):
        task_id = self.store.map_legacy_identity(
            source_system="clinx_v1",
            source_type="task_id",
            source_identity="task_v1",
            target_type="task",
        ).target_id
        baseline = self.store.append(
            task_request(
                task_id, 0, "V1SnapshotBaselineImported", "snapshot",
                payload={
                    "execution_state": "UNKNOWN",
                    "current_stage": "UNKNOWN",
                    "lease_state": "UNKNOWN",
                    "history_before_baseline": "UNKNOWN",
                },
            )
        ).stored_event
        release = dataclasses.replace(
            baseline,
            cursor=2,
            event=dataclasses.replace(
                baseline.event,
                aggregate_version=2,
                event_id="release-without-claim",
                event_type="V1UnattributedLeaseReleasedObserved",
                payload_hash=None,
                payload={
                    "task_id": task_id,
                    "source_task_id": "task_v1",
                    "attribution": "UNATTRIBUTED",
                    "lease_state": "RELEASED",
                    "resource_key": "worktree-a",
                },
            ),
        )
        with self.assertRaises(ReplayError):
            ReplayReducer().replay((baseline, release))

    def test_replay_rejects_duplicate_bootstrap_after_valid_baseline(self):
        baseline = self.store.append(self.claim_request()).stored_event
        duplicate_bootstrap = self.store.append(
            exact_request(
                self.execution_id,
                "exec_v1",
                1,
                "V1SnapshotBaselineImported",
                "duplicate-bootstrap",
                payload={
                    "execution_state": "CLAIMED",
                    "current_stage": "CLAIMED",
                    "lease_state": "HELD",
                    "history_before_baseline": "UNKNOWN",
                },
            )
        ).stored_event

        with self.assertRaisesRegex(ReplayError, "bootstrap event must be first"):
            ReplayReducer().replay((baseline, duplicate_bootstrap))

    def test_replay_rejects_version_gap_after_valid_baseline(self):
        baseline = self.store.append(self.claim_request()).stored_event
        gap = dataclasses.replace(
            baseline,
            cursor=2,
            event=dataclasses.replace(
                baseline.event,
                event_id="gap-progress",
                aggregate_version=3,
                event_type="V1ExecutionProgressObserved",
                payload_hash=None,
                payload={
                    **baseline.event.payload.to_dict(),
                    "execution_state": "CODEX_RUNNING",
                    "current_stage": "provider_wait",
                    "turn_id": "turn-gap",
                },
            ),
        )

        with self.assertRaisesRegex(ReplayError, "version gap or order error"):
            ReplayReducer().replay((baseline, gap))

    def test_replay_rejects_wrong_attribution_and_turn_ownership(self):
        baseline = self.store.append(
            dataclasses.replace(
                self.claim_request(),
                payload={
                    **self.claim_request().payload.to_dict(),
                    "turn_id": "turn-owned",
                },
            )
        ).stored_event
        wrong_attribution = dataclasses.replace(
            baseline,
            cursor=2,
            event=dataclasses.replace(
                baseline.event,
                event_id="wrong-attribution",
                aggregate_version=2,
                event_type="V1ExecutionProgressObserved",
                payload_hash=None,
                payload={
                    **baseline.event.payload.to_dict(),
                    "attribution": "UNATTRIBUTED",
                    "execution_state": "CODEX_RUNNING",
                },
            ),
        )
        with self.assertRaisesRegex(ReplayError, "correlation conflict for attribution"):
            ReplayReducer().replay((baseline, wrong_attribution))

        result = self.store.append(
            exact_request(
                self.execution_id,
                "exec_v1",
                1,
                "V1ExecutionResultPersistedObserved",
                "turn-conflict",
                payload={
                    "exact_result_ref": "exec_v1",
                    "turn_id": "turn-other",
                    "result_status": "PASS",
                },
            )
        ).stored_event
        with self.assertRaisesRegex(ReplayError, "result turn correlation conflicts"):
            ReplayReducer().replay((baseline, result))

    def test_offline_cli_replays_and_compares_without_writing(self):
        self.append_trace()
        service = ReplayService(self.store)
        replayed = service.replay_stream("execution", self.execution_id)
        expected = {
            "aggregate_type": "execution",
            "task_id": "shadow_task_1",
            "execution_id": self.execution_id,
            "source_task_id": "task_v1",
            "source_execution_ref": "exec_v1",
            "execution_state": "COMPLETED",
            "current_stage": "COMPLETED",
            "exact_result_ref": "exec_v1",
            "result_status": "UNKNOWN",
            "turn_id": "turn_1",
            "lease_state": "RELEASED",
            "resource_key": "UNKNOWN",
            "stream_version": 5,
            "attribution": "EXACT",
        }
        expected_path = Path(self.temp.name) / "expected.json"
        expected_path.write_text(json.dumps(expected), encoding="utf-8")
        before = self.store.count_events()

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "shadow_ledger",
                "--db",
                str(self.db),
                "--aggregate-type",
                "execution",
                "--aggregate-id",
                self.execution_id,
                "--expected-json",
                str(expected_path),
                "--batch-size",
                "2",
            ],
            cwd=Path(__file__).parent,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["comparison"]["status"], "PASS")
        self.assertEqual(self.store.count_events(), before)

    def test_two_execution_streams_for_one_task_remain_isolated(self):
        first = self.store.append(self.claim_request("first-exec"))
        second_id = self.store.map_legacy_identity(
            source_system="clinx_v1",
            source_type="execution_ref",
            source_identity="exec_v2",
            target_type="execution",
        ).target_id
        second = self.store.append(
            exact_request(
                second_id,
                "exec_v2",
                0,
                "V1ExecutionClaimObserved",
                "second-exec",
                payload={
                    "execution_state": "CLAIMED",
                    "current_stage": "CLAIMED",
                    "lease_state": "HELD",
                },
            )
        )

        self.assertNotEqual(
            first.stored_event.event.aggregate_id,
            second.stored_event.event.aggregate_id,
        )
        self.assertEqual(self.store.current_version("execution", self.execution_id), 1)
        self.assertEqual(self.store.current_version("execution", second_id), 1)

    def test_old_execution_result_cannot_apply_to_new_execution_stream(self):
        baseline = self.store.append(self.claim_request()).stored_event
        mismatched = Event(
            event_id="evt_old_result",
            aggregate_type="execution",
            aggregate_id=self.execution_id,
            aggregate_version=2,
            event_type="V1ExecutionResultPersistedObserved",
            schema_version=1,
            occurred_at="2026-09-10T00:00:01+00:00",
            recorded_at="2026-09-10T00:00:01+00:00",
            actor=ACTOR,
            idempotency_scope=f"execution:{self.execution_id}",
            idempotency_key="old-result",
            payload={
                "task_id": "shadow_task_1",
                "source_task_id": "task_v1",
                "execution_id": self.execution_id,
                "source_execution_ref": "exec_old",
                "exact_result_ref": "exec_old",
                "attribution": "EXACT",
            },
        )

        with self.assertRaisesRegex(ReplayError, "correlation conflict"):
            ReplayReducer().replay(
                (baseline, StoredEvent(2, mismatched, "fingerprint"))
            )

    def test_legacy_null_reference_remains_task_level_and_unattributed(self):
        task_id = self.store.map_legacy_identity(
            source_system="clinx_v1",
            source_type="task_id",
            source_identity="task_v1",
            target_type="task",
        ).target_id
        result = self.store.append(
            task_request(
                task_id,
                0,
                "V1UnattributedExecutionClaimObserved",
                "legacy-claim",
                payload={
                    "execution_state": "CLAIMED",
                    "current_stage": "CLAIMED",
                    "lease_state": "HELD",
                },
            )
        )
        replayed = ReplayReducer().replay((result.stored_event,))

        self.assertEqual(replayed.attribution, "UNATTRIBUTED")
        self.assertIsNone(replayed.execution_id)
        self.assertIsNone(replayed.source_execution_ref)

    def test_replay_rejects_unknown_schema_gap_missing_baseline_and_correlation(self):
        baseline = self.store.append(self.claim_request()).stored_event
        event = baseline.event
        cases = (
            dataclasses.replace(
                baseline,
                event=dataclasses.replace(event, schema_version=2),
            ),
            dataclasses.replace(
                baseline,
                event=dataclasses.replace(event, aggregate_version=2),
            ),
            dataclasses.replace(
                baseline,
                event=dataclasses.replace(
                    event,
                    event_type="V1ExecutionProgressObserved",
                ),
            ),
        )
        for bad in cases:
            with self.subTest(event=bad.event.to_json()):
                with self.assertRaises(ReplayError):
                    ReplayReducer().replay((bad,))

        conflict_event = Event(
            event_id="evt_conflict",
            aggregate_type="execution",
            aggregate_id=self.execution_id,
            aggregate_version=2,
            event_type="V1ExecutionProgressObserved",
            schema_version=1,
            occurred_at="2026-09-10T00:00:01+00:00",
            recorded_at="2026-09-10T00:00:01+00:00",
            actor=ACTOR,
            idempotency_scope=f"execution:{self.execution_id}",
            idempotency_key="conflict",
            payload={
                "task_id": "other_task",
                "source_task_id": "task_v1",
                "execution_id": self.execution_id,
                "source_execution_ref": "exec_v1",
                "attribution": "EXACT",
                "execution_state": "RUNNING",
            },
        )
        with self.assertRaisesRegex(ReplayError, "correlation conflict"):
            ReplayReducer().replay(
                (baseline, StoredEvent(2, conflict_event, "fingerprint"))
            )

    def test_bounded_multistream_pages_and_checkpoint(self):
        self.store.append(self.claim_request())
        for index in range(1, 21):
            execution_id = self.store.map_legacy_identity(
                source_system="clinx_v1",
                source_type="execution_ref",
                source_identity=f"exec_{index}",
                target_type="execution",
            ).target_id
            self.store.append(
                exact_request(
                    execution_id,
                    f"exec_{index}",
                    0,
                    "V1ExecutionClaimObserved",
                    f"batch-{index}",
                    payload={
                        "execution_state": "CLAIMED",
                        "current_stage": "CLAIMED",
                        "lease_state": "HELD",
                    },
                )
            )
        page = self.store.read_events(limit=7)
        self.assertEqual(len(page), 7)
        next_page = self.store.read_events(after_cursor=page[-1].cursor, limit=7)
        self.assertEqual(len(next_page), 7)
        checkpoint = self.store.save_checkpoint("test-projection", next_page[-1].cursor)
        self.assertEqual(checkpoint.last_cursor, next_page[-1].cursor)
        replayed = ReplayService(self.store).replay_stream(
            "execution", self.execution_id, batch_size=1
        )
        self.assertEqual(replayed.event_count, 1)


class TaskRegistryShadowIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "registry.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def create_task(registry, *, title="Shadow lifecycle"):
        task = registry.create_task(
            host="p620",
            workspace_alias="p620",
            project_alias="clinx",
            project_name="CLINX",
            cwd="/tmp/clinx-shadow",
            repository_origin=None,
            branch="main",
            title=title,
        )
        registry.bind_conversation(
            task_id=task.task_id,
            thread_id="thread-" + task.task_id,
            session_id="session-" + task.task_id,
            project_id=None,
            app_server_version="test",
        )
        return task

    def test_shadow_default_off_does_not_create_v2_schema(self):
        registry = TaskRegistry(self.db)
        task = self.create_task(registry)
        registry.set_execution_state(task.task_id, "CLAIMED", current_stage="CLAIMED")

        with sqlite3.connect(self.db) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertFalse(any(name.startswith("v2_") for name in tables))
        self.assertIsNone(registry.shadow_event_store)

    def test_on_then_off_retains_history_without_new_events(self):
        enabled = TaskRegistry(self.db, shadow_events=True)
        task = self.create_task(enabled)
        with enabled.execution(task.task_id, execution_ref="exec_on"):
            pass
        count = enabled.shadow_event_store.count_events()
        self.assertEqual(count, 2)

        disabled = TaskRegistry(self.db)
        disabled.set_execution_state(
            task.task_id, "DISPATCHING", current_stage="DISPATCHING"
        )
        self.assertEqual(EventStore(self.db).count_events(), count)

    def test_each_shadow_fault_boundary_rolls_back_v1_claim_and_ledger(self):
        for stage in (
            "before_event_append",
            "after_event_append",
            "after_outbox_insert",
        ):
            with self.subTest(stage=stage):
                db = Path(self.temp.name) / f"registry-{stage}.sqlite3"

                def inject(actual, expected=stage):
                    if actual == expected:
                        raise RuntimeError(expected)

                registry = TaskRegistry(
                    db,
                    shadow_events=True,
                    shadow_fault_injector=inject,
                )
                task = self.create_task(registry, title=stage)
                with self.assertRaisesRegex(RuntimeError, stage):
                    registry.execution(
                        task.task_id, execution_ref=f"exec-{stage}"
                    ).__enter__()

                with sqlite3.connect(db) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM worktree_leases").fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM v2_events").fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM v2_event_outbox").fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM v2_aggregate_versions").fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM v2_legacy_identity_map").fetchone()[0], 0)

    def test_real_registry_lifecycle_replays_to_selected_v1_fields(self):
        registry = TaskRegistry(self.db, shadow_events=True)
        task = self.create_task(registry)
        execution_ref = "exec_registry_fixture"
        turn_id = "turn_registry_fixture"
        with registry.execution(
            task.task_id, execution_ref=execution_ref, retain=True
        ):
            registry.set_execution_state(
                task.task_id,
                "CODEX_RUNNING",
                current_stage="CODEX_RUNNING",
                codex_running=True,
                turn_id=turn_id,
            )
            registry.begin_host_execution(
                host_execution_ref="host_fixture",
                task_id=task.task_id,
                execution_ref=execution_ref,
                routing_identity_json=task.routing_identity_json,
                execution_policy_json=task.execution_policy_json,
                host="p620",
                surface="SANDBOX_WORKSPACE",
                operation_class="READ_ONLY_HOST",
                capability="LOCAL_HOST_PROCESS",
                operation="fixture",
                argv_json="[]",
                cwd_identity=task.cwd,
                started_at="2026-09-10T01:00:00+00:00",
                result_state="RUNNING",
                timeout_seconds=1.0,
                executor_instance="fixture",
            )
            registry.complete_host_execution(
                "host_fixture",
                completed_at="2026-09-10T01:00:01+00:00",
                duration_ms=1000,
                exit_code=0,
                stdout="ok",
                stderr="",
                stdout_bytes=2,
                stderr_bytes=0,
                stdout_sha256="2689367b205c16ce32ed4200942b8b1e",
                stderr_sha256="e3b0c44298fc1c149afbf4c8996fb924",
                stdout_truncated=0,
                stderr_truncated=0,
                result_state="COMPLETED",
                timed_out=0,
                cancel_requested=0,
            )
            registry.record_execution_result(
                execution_ref=execution_ref,
                task_id=task.task_id,
                turn_id=turn_id,
                status="PASS",
                summary="complete",
                changed_files="NONE",
                validation="PASS",
                blockers="NONE",
                next_state="IN_REVIEW",
                raw_result="fixture result",
            )
            registry.reconcile_terminal(execution_ref, "COMPLETED")

        store = registry.shadow_event_store
        execution_id = store.map_legacy_identity(
            source_system="clinx_v1",
            source_type="execution_ref",
            source_identity=execution_ref,
            target_type="execution",
        ).target_id
        mapped_task_id = store.map_legacy_identity(
            source_system="clinx_v1",
            source_type="task_id",
            source_identity=task.task_id,
            target_type="task",
        ).target_id
        replayed = ReplayService(store).replay_stream(
            "execution", execution_id, batch_size=2
        )
        final_task = registry.get_task(task.task_id)
        expected = {
            "aggregate_type": "execution",
            "task_id": mapped_task_id,
            "execution_id": execution_id,
            "source_task_id": task.task_id,
            "source_execution_ref": execution_ref,
            "execution_state": final_task.execution_state,
            "current_stage": final_task.current_stage,
            "exact_result_ref": execution_ref,
            "result_status": "PASS",
            "turn_id": turn_id,
            "lease_state": "RELEASED",
            "resource_key": registry.worktree_key(
                host="p620", cwd="/tmp/clinx-shadow", repository_origin=None
            ),
            "stream_version": store.current_version("execution", execution_id),
            "attribution": "EXACT",
        }
        comparison = ReplayService.compare(replayed, expected)

        self.assertEqual(comparison.status, "PASS", comparison.to_dict())
        self.assertEqual(
            [item.event.event_type for item in store.read_events(
                aggregate_type="execution", aggregate_id=execution_id
            )],
            [
                "V1ExecutionClaimObserved",
                "V1ExecutionProgressObserved",
                "V1HostExecutionStartedObserved",
                "V1HostEvidenceObserved",
                "V1ExecutionResultPersistedObserved",
                "V1TerminalStateObserved",
                "V1LeaseReleasedObserved",
            ],
        )

    def test_noop_legacy_reconcile_does_not_emit_progress(self):
        registry = TaskRegistry(self.db, shadow_events=True)
        task = self.create_task(registry)
        lease = registry.execution(task.task_id, execution_ref=None, retain=True)
        lease.__enter__()
        try:
            registry.reconcile_orphaned_terminal(
                task.task_id, "BLOCKED", evidence="legacy fixture"
            )
        finally:
            lease.__exit__(None, None, None)
        before = registry.shadow_event_store.count_events()

        result = registry.reconcile_orphaned_terminal(
            task.task_id, "BLOCKED", evidence="legacy fixture"
        )

        self.assertEqual(result.execution_state, "BLOCKED")
        self.assertEqual(registry.shadow_event_store.count_events(), before)

    def test_existing_exact_execution_requires_explicit_unknown_history_baseline(self):
        v1 = TaskRegistry(self.db)
        task = self.create_task(v1)
        lease = v1.execution(task.task_id, execution_ref="exec_existing", retain=True)
        lease.__enter__()
        lease.__exit__(None, None, None)

        shadow = TaskRegistry(self.db, shadow_events=True)
        self.assertEqual(shadow.shadow_event_store.count_events(), 0)
        shadow.import_shadow_baseline(
            task_id=task.task_id, execution_ref="exec_existing"
        )
        execution_id = shadow.shadow_event_store.map_legacy_identity(
            source_system="clinx_v1",
            source_type="execution_ref",
            source_identity="exec_existing",
            target_type="execution",
        ).target_id
        replayed = ReplayService(shadow.shadow_event_store).replay_stream(
            "execution", execution_id
        )

        self.assertEqual(replayed.history_before_baseline, "UNKNOWN")
        self.assertEqual(replayed.event_count, 1)

    def test_snapshot_baseline_preserves_active_state_stage_and_turn_without_result(self):
        v1 = TaskRegistry(self.db)
        task = self.create_task(v1)
        with v1.execution(task.task_id, execution_ref="exec_active", retain=True):
            v1.set_execution_state(
                task.task_id,
                "CODEX_RUNNING",
                current_stage="provider_wait",
                codex_running=True,
                turn_id="turn_existing",
            )

        shadow = TaskRegistry(self.db, shadow_events=True)
        shadow.import_shadow_baseline(
            task_id=task.task_id, execution_ref="exec_active"
        )
        execution_id = shadow.shadow_event_store.map_legacy_identity(
            source_system="clinx_v1",
            source_type="execution_ref",
            source_identity="exec_active",
            target_type="execution",
        ).target_id
        replayed = ReplayService(shadow.shadow_event_store).replay_stream(
            "execution", execution_id
        )
        self.assertEqual(replayed.execution_state, "CODEX_RUNNING")
        self.assertEqual(replayed.current_stage, "provider_wait")
        self.assertEqual(replayed.turn_id, "turn_existing")
        self.assertIsNone(replayed.exact_result_ref)
        self.assertEqual(replayed.result_status, "UNKNOWN")

        event = shadow.shadow_event_store.read_events(
            aggregate_type="execution", aggregate_id=execution_id
        )[0].event
        provenance = event.payload.to_dict()["field_provenance"]
        self.assertEqual(
            provenance["execution_state"],
            "V1_TASK_PROJECTION_EXACT_ACTIVE_EXECUTION",
        )
        self.assertEqual(
            provenance["current_stage"],
            "V1_TASK_PROJECTION_EXACT_ACTIVE_EXECUTION",
        )
        self.assertEqual(
            provenance["turn_id"],
            "V1_TASK_PROJECTION_EXACT_ACTIVE_EXECUTION",
        )

    def test_retained_old_execution_does_not_borrow_new_task_projection(self):
        v1 = TaskRegistry(self.db)
        task = self.create_task(v1)
        with v1.execution(task.task_id, execution_ref="exec_old", retain=True):
            v1.set_execution_state(
                task.task_id,
                "CODEX_RUNNING",
                current_stage="old_provider_wait",
                codex_running=True,
                turn_id="turn_old",
            )
            v1.record_execution_result(
                execution_ref="exec_old",
                task_id=task.task_id,
                turn_id="turn_old",
                status="PASS",
                summary="old result",
                changed_files="NONE",
                validation="PASS",
                blockers="NONE",
                next_state="COMPLETED",
                raw_result="old result",
            )
        v1.reconcile_terminal("exec_old", "COMPLETED")

        with v1.execution(task.task_id, execution_ref="exec_new", retain=True):
            v1.set_execution_state(
                task.task_id,
                "CODEX_RUNNING",
                current_stage="new_provider_wait",
                codex_running=True,
                turn_id="turn_new",
            )

        shadow = TaskRegistry(self.db, shadow_events=True)
        shadow.import_shadow_baseline(task_id=task.task_id, execution_ref="exec_old")
        old_id = shadow.shadow_event_store.map_legacy_identity(
            source_system="clinx_v1",
            source_type="execution_ref",
            source_identity="exec_old",
            target_type="execution",
        ).target_id
        replayed = ReplayService(shadow.shadow_event_store).replay_stream(
            "execution", old_id
        )
        self.assertEqual(replayed.execution_state, "UNKNOWN")
        self.assertEqual(replayed.current_stage, "COMPLETED")
        self.assertEqual(replayed.turn_id, "turn_old")
        self.assertEqual(replayed.result_status, "PASS")
        self.assertNotEqual(replayed.current_stage, "new_provider_wait")
        self.assertNotEqual(replayed.turn_id, "turn_new")

    def test_legacy_snapshot_then_new_cycle_replays_without_invented_identity(self):
        v1 = TaskRegistry(self.db)
        task = self.create_task(v1)
        legacy = v1.execution(task.task_id, execution_ref=None, retain=True)
        legacy.__enter__()
        try:
            shadow = TaskRegistry(self.db, shadow_events=True)
            shadow.import_shadow_baseline(task_id=task.task_id)
            shadow.release_execution(task.task_id, None)
            with shadow.execution(task.task_id, execution_ref=None):
                pass
        finally:
            legacy.__exit__(None, None, None)

        store = shadow.shadow_event_store
        task_id = store.map_legacy_identity(
            source_system="clinx_v1",
            source_type="task_id",
            source_identity=task.task_id,
            target_type="task",
        ).target_id
        events = store.read_events(aggregate_type="task", aggregate_id=task_id)
        self.assertEqual(
            [event.event.event_type for event in events],
            [
                "V1SnapshotBaselineImported",
                "V1UnattributedLeaseReleasedObserved",
                "V1UnattributedExecutionClaimObserved",
                "V1UnattributedLeaseReleasedObserved",
            ],
        )
        replayed = ReplayService(store).replay_stream("task", task_id)
        self.assertEqual(replayed.attribution, "UNATTRIBUTED")
        self.assertIsNone(replayed.execution_id)
        self.assertEqual(replayed.lease_state, "RELEASED")
        self.assertEqual(replayed.event_count, 4)

    def test_sequential_v1_executions_map_to_separate_streams(self):
        registry = TaskRegistry(self.db, shadow_events=True)
        task = self.create_task(registry)
        for execution_ref in ("exec_first", "exec_second"):
            with registry.execution(task.task_id, execution_ref=execution_ref):
                pass

        store = registry.shadow_event_store
        identities = [
            store.map_legacy_identity(
                source_system="clinx_v1",
                source_type="execution_ref",
                source_identity=execution_ref,
                target_type="execution",
            ).target_id
            for execution_ref in ("exec_first", "exec_second")
        ]
        self.assertNotEqual(*identities)
        self.assertEqual(
            [store.current_version("execution", identity) for identity in identities],
            [2, 2],
        )


if __name__ == "__main__":
    unittest.main()
