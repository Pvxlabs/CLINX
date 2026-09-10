import dataclasses
import json
import unittest

from domain import (
    Attempt,
    AttemptState,
    DomainValidationError,
    Event,
    EventActor,
    Execution,
    JsonDocument,
    Project,
    Projection,
    ProviderSession,
    ProviderSessionState,
    ResourceAllocation,
    RuntimeWorker,
    Task,
    TaskState,
    Workspace,
    snapshot_from_v1_record,
)
from task_registry import TaskRecord


def v1_task_record(**overrides):
    values = {
        "task_id": "task_v1",
        "host": "p620",
        "workspace_alias": "primary",
        "project_alias": "clinx",
        "project_name": "CLINX",
        "cwd": "/home/pvxlabs/dev/clinx",
        "repository_origin": "https://example.invalid/clinx.git",
        "branch": "main",
        "title": "Introduce V2 domain entities",
        "summary": "Add stable contracts without changing runtime",
        "task_key": "PVX-1804",
        "execution_mode": "NEW_TASK",
        "status": "IN_PROGRESS",
        "created_at": "2026-09-10T00:00:00+00:00",
        "updated_at": "2026-09-10T00:01:00+00:00",
        "execution_state": "IDLE",
        "current_stage": "READY",
        "current_blocker": None,
        "last_progress_at": "2026-09-10T00:01:00+00:00",
        "codex_running": False,
        "turn_id": None,
        "retry_required": False,
        "failure_stage": None,
        "failure_code": None,
        "failure_evidence": None,
    }
    values.update(overrides)
    return TaskRecord(**values)


class DomainIdentityTests(unittest.TestCase):
    def test_identity_is_immutable_and_stable_across_state_snapshots(self):
        task = Task("task_1", "project_1", "Fix issue", "Repair the defect")
        updated = dataclasses.replace(task, state=TaskState(lifecycle="ACTIVE"))

        self.assertEqual(updated.task_id, task.task_id)
        self.assertNotEqual(updated.state, task.state)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            task.task_id = "task_2"  # type: ignore[misc]

    def test_serialization_is_deterministic_and_canonical(self):
        request = {"z": 1, "a": {"second": 2, "first": 1}}
        execution = Execution("execution_1", "task_1", request)
        request["z"] = 99

        first = execution.to_json()
        second = execution.to_json()

        self.assertEqual(first, second)
        self.assertEqual(json.loads(first)["request_snapshot"]["z"], 1)
        self.assertLess(first.index('"a"'), first.index('"z"'))

    def test_all_entities_are_serializable(self):
        models = (
            Project("project_1", "CLINX", "control-plane"),
            Workspace("workspace_1", "project_1", "p620", "/repo", "repo"),
            Task("task_1", "project_1", "Intent", "Objective"),
            Execution("execution_1", "task_1", {"prompt": "work"}),
            Attempt("attempt_1", "execution_1", 0),
            ProviderSession("session_1", "provider_1", "test-adapter"),
            RuntimeWorker("worker_1", "local", "p620"),
            ResourceAllocation("allocation_1", "worktree", "p620:/repo"),
            Event(
                event_id="event_1",
                aggregate_type="execution",
                aggregate_id="execution_1",
                aggregate_version=1,
                event_type="ExecutionRequested",
                schema_version=1,
                occurred_at="2026-09-10T00:00:00+00:00",
                recorded_at="2026-09-10T00:00:01+00:00",
                actor=EventActor("command", "operator_1"),
                idempotency_scope="execution:execution_1",
                idempotency_key="request_1",
            ),
            Projection("projection_1", "execution-status", "execution", "execution_1"),
        )

        for model in models:
            with self.subTest(model=type(model).__name__):
                self.assertIsInstance(model.to_dict(), dict)
                self.assertEqual(json.loads(model.to_json()), model.to_dict())

    def test_event_envelope_is_complete_and_deeply_snapshotted(self):
        payload = {"nested": {"items": [1, {"value": "original"}]}}
        event = Event(
            event_id="event_1",
            aggregate_type="execution",
            aggregate_id="execution_1",
            aggregate_version=1,
            event_type="V1ExecutionClaimObserved",
            schema_version=1,
            occurred_at="2026-09-10T00:00:00+00:00",
            recorded_at="2026-09-10T00:00:01+00:00",
            actor=EventActor("v1_registry", "task_registry"),
            idempotency_scope="execution:execution_1",
            idempotency_key="claim_1",
            payload=payload,
        )
        payload["nested"]["items"][1]["value"] = "mutated"

        serialized = event.to_dict()
        self.assertEqual(
            serialized["payload"]["nested"]["items"][1]["value"],
            "original",
        )
        self.assertEqual(len(serialized["payload_hash"]), 64)
        self.assertEqual(serialized["schema_version"], 1)

        with self.assertRaises(DomainValidationError):
            JsonDocument("{not-json")
        with self.assertRaises(DomainValidationError):
            JsonDocument('{"not_finite":NaN}')


class DomainOwnershipTests(unittest.TestCase):
    def test_task_does_not_require_or_contain_runtime_ownership(self):
        task = Task("task_1", "project_1", "Intent", "Objective")
        fields = {field.name for field in dataclasses.fields(task)}

        self.assertFalse(
            fields
            & {
                "provider_session_id",
                "thread_id",
                "turn_id",
                "worker_id",
                "allocation_id",
            }
        )

    def test_execution_has_no_codex_or_subprocess_contract(self):
        execution = Execution("execution_1", "task_1", {"prompt": "work"})
        serialized = execution.to_dict()

        self.assertNotIn("thread_id", serialized)
        self.assertNotIn("turn_id", serialized)
        self.assertNotIn("subprocess", serialized)
        self.assertNotIn("codex", execution.to_json().casefold())

    def test_attempt_owns_provider_and_worker_correlation(self):
        state = AttemptState(
            lifecycle="ASSIGNED",
            provider_session_id="provider_session_1",
            worker_id="worker_1",
            assignment_epoch=3,
            provider_correlation={"opaque_turn": "provider-value"},
            evidence_references=("evidence_1",),
        )
        attempt = Attempt("attempt_1", "execution_1", 1, state=state)

        self.assertEqual(attempt.state.provider_session_id, "provider_session_1")
        self.assertEqual(attempt.state.worker_id, "worker_1")
        self.assertEqual(attempt.state.assignment_epoch, 3)

    def test_provider_session_changes_independently_of_execution_identity(self):
        execution = Execution("execution_1", "task_1", {"prompt": "work"})
        session = ProviderSession(
            "provider_session_1",
            "provider_1",
            "generic-adapter",
            state=ProviderSessionState(provider_cursor="cursor_1"),
        )
        advanced = dataclasses.replace(
            session,
            state=dataclasses.replace(session.state, provider_cursor="cursor_2"),
        )

        self.assertEqual(advanced.provider_session_id, session.provider_session_id)
        self.assertEqual(advanced.state.provider_cursor, "cursor_2")
        self.assertEqual(execution.execution_id, "execution_1")
        self.assertNotIn("provider_session_id", execution.to_dict())


class DomainCompatibilityTests(unittest.TestCase):
    def test_v1_task_conversion_is_read_only_and_stable(self):
        record = v1_task_record()

        first = snapshot_from_v1_record(record)
        second = snapshot_from_v1_record(record)

        self.assertEqual(first, second)
        self.assertEqual(first.task.task_id, record.task_id)
        self.assertEqual(first.task.external_reference, "PVX-1804")
        self.assertEqual(first.task.project_id, first.project.project_id)
        self.assertEqual(first.task.workspace_id, first.workspace.workspace_id)
        self.assertEqual(record.status, "IN_PROGRESS")

    def test_v1_conversion_does_not_invent_provider_or_execution_identity(self):
        record = v1_task_record(
            cwd="/repo",
            repository_origin=None,
            title="Intent",
            summary=None,
            task_key=None,
            status="OPEN",
        )

        snapshot = snapshot_from_v1_record(record)
        encoded = snapshot.to_json()

        self.assertNotIn("provider_session", encoded)
        self.assertNotIn("execution_id", encoded)
        self.assertNotIn("turn_id", encoded)


if __name__ == "__main__":
    unittest.main()
