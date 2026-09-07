from pathlib import Path
import tempfile
import unittest

from mcp_server import (
    READ_ONLY_TOOL_NAMES,
    ClinxMCPServer,
    MCPRequestError,
    tool_definitions,
)
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

    def test_status_resolves_exact_execution_ref_and_exposes_observability(self):
        with tempfile.TemporaryDirectory() as td:
            store, task = self._store(Path(td))
            store.record_execution_result(
                execution_ref="PVX-1783", task_id=task.task_id, turn_id="turn-m9",
                status="BLOCKED", summary="blocked", changed_files="NONE",
                validation="PASS", blockers="operator action", next_state="BLOCKED",
                raw_result=RESULT.replace("STATUS=PASS", "STATUS=BLOCKED")
                    .replace("BLOCKERS=NONE", "BLOCKERS=operator action")
                    .replace("NEXT_STATE=IN_REVIEW", "NEXT_STATE=BLOCKED"),
            )
            integration = ClinxIntegration(None, store, None, None, None)
            status = integration.get_status(execution_ref="PVX-1783")

            self.assertEqual(status["execution_ref"], "PVX-1783")
            self.assertEqual(status["EXECUTION_STATE"], "CODEX_RUNNING")
            self.assertTrue(status["CODEX_RUNNING"])
            self.assertTrue(status["TURN_PRESENT"])


class M9SurfaceTests(unittest.TestCase):
    def test_find_task_includes_archived_historical_tasks(self):
        with tempfile.TemporaryDirectory() as td:
            store = TaskRegistry(Path(td) / "tasks.sqlite3")
            task = store.create_task(
                host="p620", workspace_alias="p620", project_alias="orion",
                project_name="ORION", cwd=td, repository_origin=None,
                branch="main", title="UI token historical task",
            )
            store.set_status(task.task_id, "ARCHIVED")
            integration = ClinxIntegration(None, store, None, None, None)

            result = integration.find_task(
                host="p620", project="ORION", query="UI token", status="ARCHIVED",
            )

            self.assertEqual(result["classification"], "UNIQUE")
            self.assertEqual(result["tasks"][0]["task_ref"], task.task_id)

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
        self.assertEqual(names, list(READ_ONLY_TOOL_NAMES))
        self.assertEqual(names, [tool["name"] for tool in tool_definitions()])
        self.assertNotIn("clinx_execute", names)

    def test_server_discover_uses_confirmed_schema_and_canonical_catalog(self):
        discovered = self.server.handle({
            "jsonrpc": "2.0", "id": 2, "method": "server/discover", "params": {},
        })
        self.assertEqual(
            discovered["result"],
            {
                "resultType": "complete",
                "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {}},
                "_meta": {
                    "io.modelcontextprotocol/serverInfo": {
                        "name": "clinx",
                        "version": "m9",
                    },
                },
                "instructions": (
                    "CLINX Context MCP is read-only: use it to find tasks, read "
                    "authoritative context, inspect status, and discover bounded "
                    "projects."
                ),
                "ttlMs": 3600000,
                "cacheScope": "public",
            },
        )
        listed = self.server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        self.assertEqual(
            [tool["name"] for tool in listed["result"]["tools"]],
            list(READ_ONLY_TOOL_NAMES),
        )
        self.assertNotIn("clinx_execute", discovered["result"])

    def test_modern_results_are_complete_and_legacy_initialize_remains_stable(self):
        modern_meta = {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        }
        discovered = self.server.handle({
            "jsonrpc": "2.0", "id": "d", "method": "server/discover", "params": modern_meta,
        })
        listed = self.server.handle({
            "jsonrpc": "2.0", "id": "l", "method": "tools/list", "params": modern_meta,
        })
        called = self.server.handle({
            "jsonrpc": "2.0", "id": "c", "method": "tools/call", "params": {
                **modern_meta,
                "name": "clinx_list_projects",
                "arguments": {},
            },
        })
        self.assertEqual(discovered["result"]["resultType"], "complete")
        self.assertEqual(listed["result"]["resultType"], "complete")
        self.assertEqual(called["result"]["resultType"], "complete")
        self.assertEqual(listed["result"]["tools"], tool_definitions())
        self.assertEqual(self.server.handle({
            "jsonrpc": "2.0", "id": "i", "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}},
        })["result"]["protocolVersion"], "2025-06-18")

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

    def test_read_tool_delegation_and_execute_is_not_public(self):
        response = self.server.handle({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "clinx_find_task", "arguments": {"query": "M9", "status": "ACTIVE"}},
        })
        self.assertEqual(response["result"]["structuredContent"]["read_only"], True)
        self.assertEqual(self.integration.calls[-1], ("find_task", {"query": "M9", "status": "ACTIVE"}))
        with self.assertRaisesRegex(MCPRequestError, "tool is not exposed"):
            self.server.handle({
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "clinx_execute", "arguments": {
                    "approved": True, "prompt": "run", "execution_ref": "PVX-1783",
                }},
            })
        self.assertFalse(any(name == "execute" for name, _kwargs in self.integration.calls))

    def test_execute_delegates_only_when_transport_is_enabled(self):
        server = ClinxMCPServer(self.integration, allow_execute=True)
        listed = server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
        self.assertIn("clinx_execute", [tool["name"] for tool in listed["result"]["tools"]])
        response = server.handle({
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "clinx_execute", "arguments": {
                "approved": True, "prompt": "run", "execution_ref": "PVX-1783",
            }},
        })
        self.assertEqual(response["result"]["structuredContent"]["task_ref"], "task_public")
        self.assertEqual(self.integration.calls[-1][0], "execute")
        self.assertNotIn("turn_id", response["result"]["structuredContent"])

    def test_read_only_calls_never_reach_executor(self):
        for request_id, name, arguments in (
            (10, "clinx_find_task", {"query": "M9"}),
            (11, "clinx_get_context", {"task_ref": "task_public"}),
            (12, "clinx_list_projects", {}),
            (13, "clinx_get_status", {"task_ref": "task_public"}),
        ):
            self.server.handle({
                "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            })
        self.assertFalse(any(name == "execute" for name, _kwargs in self.integration.calls))

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
