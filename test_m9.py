from pathlib import Path
import tempfile
import unittest

from mcp_server import ClinxMCPServer, MCPRequestError, tool_definitions
from m9_integration import (
    ClinxIntegration,
    ExecutionResultService,
    M9IntegrationError,
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
                approved=True,
            )
            self.assertEqual(result["task_ref"], task.task_id)
            self.assertEqual(dispatcher.kwargs["task_mode"], "continue")
            self.assertEqual(dispatcher.kwargs["task_id"], task.task_id)
            self.assertNotIn("turn_id", result)

    def test_execute_requires_explicit_approval(self):
        integration = ClinxIntegration(None, None, None, None, None)
        with self.assertRaisesRegex(M9IntegrationError, "approved=true"):
            integration.execute(
                prompt="run",
                execution_ref="PVX-1783",
            )
        with self.assertRaisesRegex(M9IntegrationError, "approved=true"):
            integration.execute(
                prompt="run",
                execution_ref="PVX-1783",
                approved="true",
            )


class M9MCPTests(unittest.TestCase):
    class FakeIntegration:
        def __init__(self):
            self.calls = []

        def find_task(self, **kwargs):
            self.calls.append(("find_task", kwargs))
            return {"tasks": [], "read_only": True}

        def get_context(self, **kwargs):
            self.calls.append(("get_context", kwargs))
            return {
                "task_ref": "task_public",
                "provenance": {"turn_ids": ["turn-private"], "source": "local"},
                "cwd": "/private/worktree",
                "read_only": True,
            }

        def list_projects(self, **kwargs):
            self.calls.append(("list_projects", kwargs))
            return {"projects": [], "read_only": True}

        def get_status(self, **kwargs):
            self.calls.append(("get_status", kwargs))
            return {"task_ref": "task_public", "read_only": True}

        def execute(self, **kwargs):
            self.calls.append(("execute", kwargs))
            return {
                "execution_ref": kwargs["execution_ref"],
                "task_ref": "task_public",
                "turn_id": "turn-private",
            }

    def setUp(self):
        self.integration = self.FakeIntegration()
        self.server = ClinxMCPServer(self.integration)

    def test_initialize_and_tool_discovery_are_deterministic(self):
        initialized = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(initialized["result"]["serverInfo"], {"name": "clinx", "version": "m9"})
        listed = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [tool["name"] for tool in listed["result"]["tools"]]
        self.assertEqual(names, [
            "clinx_find_task", "clinx_get_context", "clinx_list_projects",
            "clinx_get_status", "clinx_execute",
        ])

    def test_public_schemas_and_results_do_not_expose_private_identity(self):
        forbidden = {
            "thread_id", "threadId", "session_id", "sessionId", "turn_id", "turnId",
            "cwd", "credentials", "token", "authorization",
        }

        def keys(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield key
                    yield from keys(item)
            elif isinstance(value, list):
                for item in value:
                    yield from keys(item)

        self.assertTrue(forbidden.isdisjoint(set(keys(tool_definitions()))))
        response = self.server.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "clinx_get_context", "arguments": {"task_ref": "task_public"}},
        })
        payload = response["result"]["structuredContent"]
        self.assertNotIn("cwd", payload)
        self.assertNotIn("turn_ids", payload.get("provenance", {}))
        self.assertNotIn("turn-private", response["result"]["content"][0]["text"])

    def test_read_tool_delegation_and_execute_read_only_fallback(self):
        response = self.server.handle({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "clinx_find_task", "arguments": {"query": "M9", "status": "ACTIVE"}},
        })
        self.assertEqual(response["result"]["structuredContent"]["read_only"], True)
        self.assertEqual(self.integration.calls[-1], ("find_task", {"query": "M9", "status": "ACTIVE"}))
        fallback = self.server.handle({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "clinx_execute", "arguments": {
                "approved": True, "prompt": "run", "execution_ref": "PVX-1783",
            }},
        })
        content = fallback["result"]["structuredContent"]
        self.assertEqual(content["execution"], "READ_ONLY_FALLBACK")
        self.assertFalse(any(name == "execute" for name, _kwargs in self.integration.calls))

    def test_execute_delegates_only_when_transport_is_enabled(self):
        server = ClinxMCPServer(self.integration, allow_execute=True)
        response = server.handle({
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "clinx_execute", "arguments": {
                "approved": True, "prompt": "run", "execution_ref": "PVX-1783",
            }},
        })
        self.assertEqual(response["result"]["structuredContent"]["task_ref"], "task_public")
        self.assertEqual(self.integration.calls[-1][0], "execute")
        self.assertNotIn("turn_id", response["result"]["structuredContent"])

    def test_malformed_requests_and_unknown_tools_fail_closed(self):
        with self.assertRaisesRegex(MCPRequestError, "method is required"):
            self.server.handle({"jsonrpc": "2.0", "id": 7})
        with self.assertRaisesRegex(MCPRequestError, "unknown tool"):
            self.server.handle({
                "jsonrpc": "2.0", "id": 8, "method": "tools/call",
                "params": {"name": "unknown", "arguments": {}},
            })

    def test_domain_resolution_errors_return_tool_errors(self):
        class FailingIntegration(self.FakeIntegration):
            def get_status(self, **kwargs):
                raise ValueError("No task matched the discovery query")

        response = ClinxMCPServer(FailingIntegration()).handle({
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": "clinx_get_status", "arguments": {"task_ref": "missing"}},
        })
        self.assertTrue(response["result"]["isError"])
        self.assertIn("No task matched", response["result"]["structuredContent"]["error"])
