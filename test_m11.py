from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import bridge
from m9_integration import ClinxIntegration, M9IntegrationError
from mcp_server import (
    ClinxMCPServer,
    DEFAULT_TOOL_NAMES,
    MCP_INSTRUCTIONS,
    READ_ONLY_TOOL_NAMES,
    tool_definitions,
)
from task_registry import TaskRegistry


class _Context:
    def __init__(self, task=None, error=None):
        self.task = task
        self.error = error
        self.calls = []

    def resolve_task(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.task


class _Dispatcher:
    def __init__(self, cwd: Path, alias: str = "orion"):
        self.cwd = cwd
        self.alias = alias
        self.calls = []

    def resolve_project(self, project_ref, *, host, project_mode):
        self.calls.append({
            "method": "resolve_project",
            "project_ref": project_ref,
            "host": host,
            "project_mode": project_mode,
        })
        descriptor = bridge.ProjectDescriptor(
            alias=self.alias,
            name="ORION",
            workspace_alias="p620",
            cwd=self.cwd,
            repository_origin="git@github.com:Pvxlabs/ORION.git",
            branch="master",
            registered=True,
        )
        return SimpleNamespace(alias="p620", host="p620"), descriptor, None


class _ForbiddenSideEffect:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected side effect: {name}")


def _cfg():
    return SimpleNamespace(
        team_id="team-id",
        trigger_label="local-codex",
        todo_state="Todo",
        projects=(SimpleNamespace(
            linear_name="ChatGPT × Linear × Codex Dispatcher V1",
            project_alias="pilot",
        ),),
    )


def _fixture(root: Path, *, status: str = "ACTIVE", with_binding: bool = True):
    registry = TaskRegistry(root / "tasks.sqlite3")
    task = registry.create_task(
        host="p620",
        workspace_alias="p620",
        project_alias="orion",
        project_name="ORION",
        cwd=str(root),
        repository_origin="git@github.com:Pvxlabs/ORION.git",
        branch="master",
        title="Existing ORION task",
        summary="Bound task for M11 handoff qualification",
    )
    if status != "ACTIVE":
        task = registry.set_status(task.task_id, status)
    if with_binding:
        registry.bind_conversation(
            task_id=task.task_id,
            thread_id="thread-exact",
            session_id="session-exact",
            project_id=None,
            app_server_version="0.152.1",
        )
    return registry, task


class M11PrepareExecutionTests(unittest.TestCase):
    def _integration(self, root: Path, *, status="ACTIVE", with_binding=True, context=None):
        registry, task = _fixture(root, status=status, with_binding=with_binding)
        dispatcher = _Dispatcher(root)
        context = context or _Context(task)
        integration = ClinxIntegration(
            _cfg(), registry, dispatcher, context, _ForbiddenSideEffect()
        )
        return integration, registry, task, dispatcher, context

    def test_capabilities_describe_separated_read_only_and_linear_command_planes(self):
        integration = ClinxIntegration(_cfg(), None, None, None, None)
        result = integration.get_capabilities()
        self.assertTrue(result["context_plane"]["read_only"])
        self.assertTrue(result["execution"]["available"])
        self.assertTrue(result["execution"]["direct_mcp_execution"])
        self.assertEqual(result["execution"]["command_plane"], "CLINX")
        self.assertTrue(result["context_read_only"])
        self.assertTrue(result["execution_available"])
        self.assertEqual(result["command_plane"], "CLINX")
        self.assertEqual(result["prepare_tool"], "clinx_prepare_execution")
        self.assertEqual(result["status_tool"], "clinx_get_status")
        self.assertEqual(result["execution"], {
            "available": True,
            "direct_mcp_execution": True,
            "command_plane": "CLINX",
            "requires_user_approval": True,
            "prepare_tool": "clinx_prepare_execution",
            "start_tool": "clinx_start_execution",
        })
        self.assertEqual(result["status"]["tool"], "clinx_get_status")

    def test_explicit_boolean_approval_is_required(self):
        with tempfile.TemporaryDirectory() as td:
            integration, _registry, _task, _dispatcher, _context = self._integration(Path(td))
            for value in (False, "true"):
                with self.assertRaisesRegex(M9IntegrationError, "approved=true"):
                    integration.prepare_execution(prompt="run", approved=value)

    def test_active_continuation_preserves_task_ref_and_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            integration, _registry, task, dispatcher, context = self._integration(root)
            result = integration.prepare_execution(
                prompt="continue the approved task",
                approved=True,
                task_ref=task.task_id,
                model="gpt-test",
                reasoning_effort="low",
                execution_mode="fast",
            )
            self.assertEqual(result["task_action"], "continue")
            self.assertEqual(result["task_ref"], task.task_id)
            self.assertEqual(result["model"], "gpt-test")
            self.assertEqual(result["reasoning"], "low")
            self.assertEqual(result["execution_mode"], "fast")
            self.assertFalse(result["requires_command_write"])
            self.assertEqual(result["next_action"], {
                "provider": "CLINX",
                "operation": "START_EXECUTION",
                "required": True,
            })
            self.assertEqual(context.calls[-1]["task_ref"], task.task_id)
            self.assertEqual(dispatcher.calls[-1]["project_mode"], "existing")
            contract = bridge.parse_dispatch_contract(result["description"])
            self.assertEqual(contract.contract_kind, "m6")
            self.assertEqual(contract.task_action, "continue")
            self.assertEqual(contract.task_ref, task.task_id)
            self.assertEqual(contract.model, "gpt-test")
            self.assertEqual(contract.reasoning_effort, "low")

    def test_completed_and_archived_continuations_are_reopen_handoffs_without_mutation(self):
        for status in ("COMPLETED", "ARCHIVED"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                integration, registry, task, _dispatcher, _context = self._integration(
                    root, status=status
                )
                result = integration.prepare_execution(
                    prompt="resume this task",
                    approved=True,
                    task_ref=task.task_id,
                )
                self.assertEqual(result["task_action"], "reopen")
                self.assertEqual(registry.get_task(task.task_id).status, status)

    def test_explicit_reopen_requires_completed_or_archived_task(self):
        with tempfile.TemporaryDirectory() as td:
            integration, _registry, task, _dispatcher, _context = self._integration(Path(td))
            with self.assertRaisesRegex(M9IntegrationError, "completed or archived"):
                integration.prepare_execution(
                    prompt="reopen",
                    approved=True,
                    task_ref=task.task_id,
                    task_action="reopen",
                )

    def test_new_task_requires_existing_project_resolution_and_creates_no_registry_record(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            integration, registry, _task, dispatcher, _context = self._integration(root)
            before = registry.list_tasks(include_archived=True)
            result = integration.prepare_execution(
                prompt="start a new approved task",
                approved=True,
                task_mode="new",
                host="p620",
                project="orion",
                title="New handoff",
                summary="New task summary",
            )
            self.assertEqual(result["task_action"], "create")
            self.assertIsNone(result["task_ref"])
            self.assertEqual(len(registry.list_tasks(include_archived=True)), len(before))
            self.assertEqual(dispatcher.calls[-1], {
                "method": "resolve_project",
                "project_ref": "orion", "host": "p620", "project_mode": "existing",
            })
            self.assertEqual(bridge.parse_dispatch_contract(result["description"]).task_action, "create")

    def test_unknown_ambiguous_and_unbound_tasks_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            integration, registry, task, _dispatcher, _context = self._integration(root)
            for error in (
                ValueError("No task matched the discovery query"),
                ValueError("Task discovery is ambiguous"),
            ):
                integration.context_reader = _Context(error=error)
                with self.assertRaisesRegex(ValueError, error.args[0]):
                    integration.prepare_execution(prompt="run", approved=True, query="task", project="ORION")
            registry2, task2 = _fixture(root / "unbound", with_binding=False)
            integration2 = ClinxIntegration(
                _cfg(), registry2, _Dispatcher(root / "unbound"), _Context(task2), _ForbiddenSideEffect()
            )
            with self.assertRaisesRegex(M9IntegrationError, "no conversation binding"):
                integration2.prepare_execution(prompt="run", approved=True, task_ref=task2.task_id)

    def test_project_mismatch_and_invalid_modes_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            integration, _registry, task, _dispatcher, _context = self._integration(root)
            with self.assertRaisesRegex(bridge.TargetResolutionError, "project mismatch"):
                integration.prepare_execution(
                    prompt="run", approved=True, task_ref=task.task_id, project="pilot"
                )
            with self.assertRaisesRegex(M9IntegrationError, "execution_mode"):
                integration.prepare_execution(
                    prompt="run", approved=True, task_ref=task.task_id, execution_mode="turbo"
                )

    def test_prepare_never_calls_execution_or_app_server(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            integration, _registry, task, dispatcher, _context = self._integration(root)
            result = integration.prepare_execution(
                prompt="readiness only", approved=True, task_ref=task.task_id
            )
            self.assertTrue(result["read_only"])
            self.assertFalse(any(
                call.get("method") in {"dispatch", "execute"}
                for call in dispatcher.calls
                if isinstance(call, dict)
            ))


class M11MCPTests(unittest.TestCase):
    class FakeIntegration:
        def __init__(self):
            self.calls = []

        def get_capabilities(self, **kwargs):
            self.calls.append(("get_capabilities", kwargs))
            return {"execution": {"available": True}, "read_only": True}

        def prepare_execution(self, **kwargs):
            self.calls.append(("prepare_execution", kwargs))
            return {
                "task_ref": "task-public",
                "description": "handoff",
                "read_only": True,
            }

        def start_execution(self, **kwargs):
            self.calls.append(("start_execution", kwargs))
            return {
                "execution_started": True,
                "prepared_execution_ref": kwargs["prepared_execution_ref"],
                "execution_ref": "exec-public",
                "task_ref": "task-public",
                "task_action": "continue",
                "model": "gpt-test",
                "reasoning_effort": "high",
                "execution_mode": "normal",
                "dispatch_status": "DISPATCHED",
                "linear_audit": "NOT_CONFIGURED",
                "read_only": False,
            }

        def find_task(self, **kwargs):
            return {"tasks": [], "read_only": True}

        def get_context(self, **kwargs):
            return {"task_ref": "task-public", "read_only": True}

        def get_topic_status(self, **kwargs):
            return {"read_only": True}

        def list_projects(self, **kwargs):
            return {"projects": [], "read_only": True}

        def get_status(self, **kwargs):
            return {"task_ref": "task-public", "read_only": True}

        def execute(self, **kwargs):
            raise AssertionError("default M11 catalog must not execute")

    def test_default_catalog_exposes_prepare_not_execute_and_routes_read_only_tools(self):
        self.assertEqual(list(DEFAULT_TOOL_NAMES), [
            "clinx_find_task", "clinx_get_context", "clinx_get_topic_status",
            "clinx_list_projects", "clinx_get_status", "clinx_get_capabilities",
            "clinx_prepare_execution", "clinx_start_execution",
        ])
        self.assertNotIn("clinx_execute", [item["name"] for item in tool_definitions()])
        integration = self.FakeIntegration()
        server = ClinxMCPServer(integration)
        capabilities = server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "clinx_get_capabilities", "arguments": {}},
        })
        prepared = server.handle({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "clinx_prepare_execution", "arguments": {
                "approved": True, "prompt": "run",
            }},
        })
        self.assertTrue(capabilities["result"]["structuredContent"]["read_only"])
        self.assertEqual(prepared["result"]["structuredContent"]["task_ref"], "task-public")
        self.assertEqual([name for name, _ in integration.calls], [
            "get_capabilities", "prepare_execution",
        ])

        started = server.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "clinx_start_execution", "arguments": {
                "prepared_execution_ref": "prepared-public", "approved": True,
            }},
        })
        self.assertTrue(started["result"]["structuredContent"]["execution_started"])
        self.assertEqual(integration.calls[-1][0], "start_execution")

    def test_every_default_tool_declares_an_object_output_schema(self):
        tools = tool_definitions()
        self.assertEqual(len(tools), 8)
        self.assertEqual({tool["name"] for tool in tools}, set(DEFAULT_TOOL_NAMES))
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                schema = tool.get("outputSchema")
                self.assertIsNotNone(schema)
                self.assertEqual(schema["type"], "object")
                self.assertIn("properties", schema)

    def test_capabilities_and_prepare_schemas_freeze_plane_contract(self):
        tools = {tool["name"]: tool for tool in tool_definitions()}
        capabilities = tools["clinx_get_capabilities"]["outputSchema"]["properties"]
        execution = capabilities["execution"]["properties"]
        self.assertEqual(capabilities["context_read_only"], {"type": "boolean", "const": True})
        self.assertEqual(capabilities["execution_available"], {"type": "boolean", "const": True})
        self.assertEqual(capabilities["command_plane"], {"type": "string", "const": "CLINX"})
        self.assertEqual(capabilities["prepare_tool"], {
            "type": "string", "const": "clinx_prepare_execution",
        })
        self.assertEqual(capabilities["status_tool"], {
            "type": "string", "const": "clinx_get_status",
        })
        self.assertEqual(execution["available"], {"type": "boolean", "const": True})
        self.assertEqual(execution["direct_mcp_execution"], {"type": "boolean", "const": True})
        self.assertEqual(execution["command_plane"], {"type": "string", "const": "CLINX"})
        self.assertEqual(execution["start_tool"], {"type": "string", "const": "clinx_start_execution"})

        prepare = tools["clinx_prepare_execution"]["outputSchema"]["properties"]
        self.assertEqual(prepare["requires_command_write"], {"type": "boolean", "const": False})
        self.assertEqual(prepare["handoff_ready"], {"type": "boolean", "const": True})
        self.assertEqual(prepare["next_action"]["properties"]["provider"], {
            "type": "string", "const": "CLINX",
        })
        self.assertEqual(prepare["next_action"]["properties"]["operation"], {
            "type": "string", "const": "START_EXECUTION",
        })

    def test_initialize_and_discover_share_explicit_linear_handoff_instructions(self):
        integration = self.FakeIntegration()
        server = ClinxMCPServer(integration)
        initialized = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        discovered = server.handle({"jsonrpc": "2.0", "id": 2, "method": "server/discover"})
        self.assertEqual(initialized["result"]["instructions"], MCP_INSTRUCTIONS)
        self.assertEqual(discovered["result"]["instructions"], MCP_INSTRUCTIONS)
        self.assertIn("execution.available=true", MCP_INSTRUCTIONS)
        self.assertIn("direct_mcp_execution=true", MCP_INSTRUCTIONS)
        self.assertIn("clinx_start_execution", MCP_INSTRUCTIONS)

    def test_prepare_schema_requires_exact_boolean_approval_and_public_result_scrubs_identity(self):
        prepare = next(item for item in tool_definitions() if item["name"] == "clinx_prepare_execution")
        self.assertEqual(prepare["inputSchema"]["properties"]["approved"], {
            "type": "boolean", "const": True,
        })
        integration = self.FakeIntegration()
        server = ClinxMCPServer(integration)
        response = server.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "clinx_prepare_execution", "arguments": {
                "approved": True, "prompt": "run",
            }},
        })
        self.assertNotIn("thread_id", response["result"]["structuredContent"])
        with self.assertRaisesRegex(Exception, "tool is not exposed"):
            server.handle({
                "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "clinx_execute", "arguments": {}},
            })


if __name__ == "__main__":
    unittest.main()
