from pathlib import Path
import tempfile
import unittest

from m9_integration import (
    ClinxIntegration,
    ExecutionResultService,
    ResultParseError,
    parse_execution_result,
)
from task_registry import TaskRegistry, WorkspaceConfig


RESULT = """CLINX_EXECUTION_RESULT
STATUS=PASS
SUMMARY=Implemented the integration surface.
CHANGED_FILES=bridge.py, task_registry.py
VALIDATION=118 tests passed; py_compile passed
BLOCKERS=NONE
NEXT_STATE=IN_REVIEW
"""


class FakeLinear:
    def __init__(self):
        self.comments = []
        self.states = []
        self.fail = True

    def add_comment(self, issue_id, body):
        if self.fail:
            self.fail = False
            raise RuntimeError("approval policy is never")
        self.comments.append((issue_id, body))

    def update_issue_state(self, issue_id, state_id):
        self.states.append((issue_id, state_id))


class M9ResultTests(unittest.TestCase):
    def _store(self, root):
        store = TaskRegistry(root / "tasks.sqlite3")
        task = store.create_task(
            host="p620", workspace_alias="p620", project_alias="clinx",
            project_name="CLINX", cwd=str(root), repository_origin=None,
            branch="main", title="M9 integration",
        )
        store.bind_conversation(
            task_id=task.task_id, thread_id="thread-m9", session_id="session-m9",
            project_id=None, app_server_version="0.152.1",
        )
        store.set_execution_state(
            task.task_id, "CODEX_RUNNING", current_stage="Codex turn",
            turn_id="turn-m9", codex_running=True,
        )
        return store, task

    def test_parser_is_strict_and_normalized(self):
        parsed = parse_execution_result(RESULT)
        self.assertEqual(parsed.status, "PASS")
        self.assertEqual(parsed.next_state, "IN_REVIEW")
        with self.assertRaises(ResultParseError):
            parse_execution_result(RESULT.replace("BLOCKERS=NONE", "BLOCKERS=missing"))

    def test_result_survives_restart_and_writeback_retry_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store, task = self._store(root)
            linear = FakeLinear()
            service = ExecutionResultService(store, linear)
            with self.assertRaisesRegex(RuntimeError, "approval policy"):
                service.receive_and_writeback(
                    execution_ref="PVX-1783",
                    task_id=task.task_id,
                    turn_id="turn-m9",
                    raw_result=RESULT,
                    issue_id="linear-m9",
                    review_state_id="review-state",
                )
            blocked = store.get_task(task.task_id)
            self.assertEqual(blocked.execution_state, "LINEAR_WRITEBACK")
            self.assertFalse(blocked.codex_running)
            self.assertTrue(blocked.retry_required)
            restarted = TaskRegistry(root / "tasks.sqlite3")
            retry = ExecutionResultService(restarted, linear)
            retry.receive_and_writeback(
                execution_ref="PVX-1783", task_id=task.task_id, turn_id="turn-m9",
                raw_result=RESULT, issue_id="linear-m9", review_state_id="review-state",
            )
            retry.receive_and_writeback(
                execution_ref="PVX-1783", task_id=task.task_id, turn_id="turn-m9",
                raw_result=RESULT, issue_id="linear-m9", review_state_id="review-state",
            )
            self.assertEqual(len(linear.comments), 1)
            self.assertEqual(linear.states, [("linear-m9", "review-state")])
            record = restarted.get_execution_result("PVX-1783")
            self.assertEqual(record.writeback_state, "WRITTEN")
            self.assertEqual(restarted.get_task(task.task_id).execution_state, "IN_REVIEW")

    def test_exact_execution_ref_cannot_change_task_or_turn_owner(self):
        with tempfile.TemporaryDirectory() as td:
            store, task = self._store(Path(td))
            linear = FakeLinear()
            linear.fail = False
            service = ExecutionResultService(store, linear)
            service.receive_and_writeback(
                execution_ref="PVX-1783", task_id=task.task_id, turn_id="turn-m9",
                raw_result=RESULT, issue_id="linear-m9",
            )
            with self.assertRaises(Exception):
                service.receive_and_writeback(
                    execution_ref="PVX-1783", task_id=task.task_id,
                    turn_id="turn-other", raw_result=RESULT,
                    issue_id="linear-m9",
                )


class M9SurfaceTests(unittest.TestCase):
    def test_execute_is_explicit_and_reuses_existing_task_binding(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = TaskRegistry(root / "tasks.sqlite3")
            task = store.create_task(
                host="p620", workspace_alias="p620", project_alias="clinx",
                project_name="CLINX", cwd=str(root), repository_origin=None,
                branch="main", title="M9",
            )
            store.bind_conversation(
                task_id=task.task_id, thread_id="thread-m9", session_id="session-m9",
                project_id=None, app_server_version="0.152.1",
            )

            class Context:
                def resolve_task(self, **kwargs):
                    return task

            class Dispatcher:
                def __init__(self):
                    self.kwargs = None

                def dispatch(self, **kwargs):
                    self.kwargs = kwargs
                    return type("Result", (), {
                        "task_id": task.task_id,
                        "turn_id": "turn-next",
                        "dispatch_status": "DISPATCHED",
                    })()

            dispatcher = Dispatcher()
            cfg = type("Cfg", (), {"projects": ()})()
            integration = ClinxIntegration(cfg, store, dispatcher, Context(), object())
            result = integration.execute(
                query="M9", prompt="continue the same task",
                execution_ref="PVX-1783",
            )
            self.assertEqual(result["task_ref"], task.task_id)
            self.assertEqual(dispatcher.kwargs["task_mode"], "continue")
            self.assertEqual(dispatcher.kwargs["task_id"], task.task_id)
