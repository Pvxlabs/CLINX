from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest

import bridge
from m9_integration import ClinxIntegration, M9IntegrationError
from mcp_server import ClinxMCPServer, DEFAULT_TOOL_NAMES, tool_definitions
from task_registry import TaskRegistry, TaskRegistryError


class FakeContext:
    def __init__(self, task):
        self.task = task
        self.calls = []

    def resolve_task(self, **kwargs):
        self.calls.append(kwargs)
        return self.task


class FakeDispatcher:
    def __init__(self, root: Path, registry: TaskRegistry):
        self.root = root
        self.registry = registry
        self.dispatch_calls = []
        self.action_calls = []

    def resolve_project(self, project_ref, *, host, project_mode):
        descriptor = bridge.ProjectDescriptor(
            alias=project_ref,
            name="ORION",
            workspace_alias="p620",
            cwd=self.root,
            repository_origin="git@github.com:Pvxlabs/ORION.git",
            branch="master",
            registered=True,
        )
        return SimpleNamespace(alias="p620", host="p620"), descriptor, None

    def task_action(self, task_id, action):
        self.action_calls.append((task_id, action))

    def dispatch(self, **kwargs):
        self.dispatch_calls.append(kwargs)
        task_id = kwargs["task_id"] or "task-created"
        return SimpleNamespace(
            task_id=task_id,
            thread_id="thread-dispatched",
            turn_id="turn-dispatched",
            dispatch_status="DISPATCHED",
            model=kwargs["model"],
            reasoning_effort=kwargs["reasoning_effort"],
            execution_mode=kwargs["execution_mode"],
        )


class FailingLinear:
    def create_task_index_issue(self, **_kwargs):
        raise RuntimeError("linear audit unavailable")


def make_fixture(root: Path, *, linear=None):
    registry = TaskRegistry(root / "tasks.sqlite3")
    task = registry.create_task(
        host="p620",
        workspace_alias="p620",
        project_alias="orion",
        project_name="ORION",
        cwd=str(root),
        repository_origin="git@github.com:Pvxlabs/ORION.git",
        branch="master",
        title="M12 task",
        summary="M12 direct execution qualification",
    )
    registry.bind_conversation(
        task_id=task.task_id,
        thread_id="thread-existing",
        session_id="session-existing",
        project_id=None,
        app_server_version="0.152.1",
    )
    dispatcher = FakeDispatcher(root, registry)
    context = FakeContext(task)
    cfg = SimpleNamespace(team_id="team", trigger_label="local-codex", todo_state="Todo", projects=())
    integration = ClinxIntegration(cfg, registry, dispatcher, context, linear)
    return integration, registry, dispatcher, task


class M12PreparationAndStartTests(unittest.TestCase):
    def test_prepare_persists_ref_and_never_dispatches(self):
        with tempfile.TemporaryDirectory() as td:
            integration, registry, dispatcher, task = make_fixture(Path(td))
            result = integration.prepare_execution(
                prompt="continue the task",
                approved=True,
                task_ref=task.task_id,
                model="gpt-test",
                reasoning_effort="low",
            )
            prepared = registry.get_prepared_execution(result["prepared_execution_ref"])
            self.assertIsNotNone(prepared)
            self.assertEqual(prepared.status, "PREPARED")
            self.assertEqual(prepared.model, "gpt-test")
            self.assertEqual(prepared.reasoning_effort, "low")
            self.assertEqual(dispatcher.dispatch_calls, [])

    def test_prepare_and_start_require_literal_true(self):
        with tempfile.TemporaryDirectory() as td:
            integration, _registry, _dispatcher, task = make_fixture(Path(td))
            for value in (False, "true", 1):
                with self.subTest(value=value), self.assertRaisesRegex(M9IntegrationError, "approved=true"):
                    integration.prepare_execution(
                        prompt="run", approved=value, task_ref=task.task_id
                    )
            prepared = integration.prepare_execution(
                prompt="run", approved=True, task_ref=task.task_id
            )
            for value in (False, "true", 1):
                with self.subTest(value=value), self.assertRaisesRegex(M9IntegrationError, "approved=true"):
                    integration.start_execution(
                        prepared_execution_ref=prepared["prepared_execution_ref"],
                        approved=value,
                    )

    def test_missing_or_tampered_prepared_ref_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            integration, registry, dispatcher, task = make_fixture(root)
            with self.assertRaisesRegex(TaskRegistryError, "Unknown prepared execution"):
                integration.start_execution(prepared_execution_ref="prepared-missing", approved=True)
            prepared = integration.prepare_execution(
                prompt="run", approved=True, task_ref=task.task_id
            )
            with sqlite3.connect(registry.path) as conn:
                conn.execute(
                    "UPDATE prepared_executions SET prompt=? WHERE prepared_execution_ref=?",
                    ("tampered", prepared["prepared_execution_ref"]),
                )
            with self.assertRaisesRegex(TaskRegistryError, "INTEGRITY=FAIL"):
                integration.start_execution(
                    prepared_execution_ref=prepared["prepared_execution_ref"], approved=True
                )
            self.assertEqual(dispatcher.dispatch_calls, [])

    def test_continue_reuses_exact_task_binding_and_overrides_model_reasoning(self):
        with tempfile.TemporaryDirectory() as td:
            integration, registry, dispatcher, task = make_fixture(Path(td))
            prepared = integration.prepare_execution(
                prompt="continue it",
                approved=True,
                task_ref=task.task_id,
                model="gpt-override",
                reasoning_effort="low",
                execution_mode="fast",
            )
            result = integration.start_execution(
                prepared_execution_ref=prepared["prepared_execution_ref"], approved=True
            )
            self.assertEqual(result["task_ref"], task.task_id)
            self.assertEqual(result["model"], "gpt-override")
            self.assertEqual(result["reasoning_effort"], "low")
            self.assertEqual(result["execution_mode"], "fast")
            self.assertEqual(len(dispatcher.dispatch_calls), 1)
            call = dispatcher.dispatch_calls[0]
            self.assertEqual(call["task_mode"], "continue")
            self.assertEqual(call["task_id"], task.task_id)
            self.assertEqual(registry.get_prepared_execution(prepared["prepared_execution_ref"]).status, "DISPATCHED")

    def test_reopen_calls_reopen_action_before_dispatch(self):
        with tempfile.TemporaryDirectory() as td:
            integration, registry, dispatcher, task = make_fixture(Path(td))
            registry.set_status(task.task_id, "COMPLETED")
            integration.context_reader.task = registry.get_task(task.task_id)
            prepared = integration.prepare_execution(
                prompt="reopen it", approved=True, task_ref=task.task_id
            )
            self.assertEqual(prepared["task_action"], "reopen")
            integration.start_execution(
                prepared_execution_ref=prepared["prepared_execution_ref"], approved=True
            )
            self.assertEqual(dispatcher.action_calls, [(task.task_id, "reopen")])
            self.assertEqual(dispatcher.dispatch_calls[0]["task_mode"], "continue")

    def test_create_dispatches_without_creating_registry_task_during_prepare(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            integration, registry, dispatcher, _task = make_fixture(root)
            before = {item.task_id for item in registry.list_tasks(include_archived=True)}
            prepared = integration.prepare_execution(
                prompt="new task", approved=True, task_action="create",
                host="p620", project="orion", title="Created task", summary="summary",
            )
            self.assertEqual(before, {item.task_id for item in registry.list_tasks(include_archived=True)})
            result = integration.start_execution(
                prepared_execution_ref=prepared["prepared_execution_ref"], approved=True
            )
            self.assertEqual(result["task_action"], "create")
            self.assertEqual(dispatcher.dispatch_calls[0]["task_mode"], "new")
            self.assertIsNotNone(registry.get_prepared_execution(prepared["prepared_execution_ref"]).resulting_turn_id)

    def test_same_prepared_ref_is_idempotent_and_does_not_dispatch_twice(self):
        with tempfile.TemporaryDirectory() as td:
            integration, _registry, dispatcher, task = make_fixture(Path(td))
            prepared = integration.prepare_execution(
                prompt="once", approved=True, task_ref=task.task_id
            )
            first = integration.start_execution(
                prepared_execution_ref=prepared["prepared_execution_ref"], approved=True
            )
            second = integration.start_execution(
                prepared_execution_ref=prepared["prepared_execution_ref"], approved=True
            )
            self.assertEqual(len(dispatcher.dispatch_calls), 1)
            self.assertEqual(second["dispatch_status"], "DISPATCHED_REPLAY")
            self.assertEqual(second["execution_ref"], first["execution_ref"])

    def test_linear_audit_failure_does_not_block_codex_dispatch(self):
        with tempfile.TemporaryDirectory() as td:
            integration, _registry, dispatcher, task = make_fixture(Path(td), linear=FailingLinear())
            prepared = integration.prepare_execution(
                prompt="audit independently", approved=True, task_ref=task.task_id
            )
            result = integration.start_execution(
                prepared_execution_ref=prepared["prepared_execution_ref"], approved=True
            )
            self.assertEqual(result["dispatch_status"], "DISPATCHED")
            self.assertEqual(result["linear_audit"], "FAILED")
            self.assertEqual(len(dispatcher.dispatch_calls), 1)

    def test_public_start_result_does_not_expose_execution_identity(self):
        with tempfile.TemporaryDirectory() as td:
            integration, _registry, _dispatcher, task = make_fixture(Path(td))
            prepared = integration.prepare_execution(
                prompt="private identity", approved=True, task_ref=task.task_id
            )
            result = integration.start_execution(
                prepared_execution_ref=prepared["prepared_execution_ref"], approved=True
            )
            for forbidden in ("thread_id", "session_id", "turn_id", "cwd"):
                self.assertNotIn(forbidden, result)


class M12MCPSurfaceTests(unittest.TestCase):
    class FakeIntegration:
        def __init__(self):
            self.calls = []

        def start_execution(self, **kwargs):
            self.calls.append(kwargs)
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
                "thread_id": "must-be-scrubbed",
                "read_only": False,
            }

        def execute(self, **_kwargs):
            raise AssertionError("legacy execute must not be the default path")

    def test_default_catalog_adds_start_and_keeps_legacy_execute_hidden(self):
        self.assertEqual(len(DEFAULT_TOOL_NAMES), 8)
        self.assertIn("clinx_start_execution", DEFAULT_TOOL_NAMES)
        self.assertNotIn("clinx_execute", DEFAULT_TOOL_NAMES)
        self.assertNotIn("clinx_execute", {tool["name"] for tool in tool_definitions()})

    def test_start_tool_routes_prepared_ref_and_scrubs_identity(self):
        integration = self.FakeIntegration()
        server = ClinxMCPServer(integration)
        response = server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "clinx_start_execution", "arguments": {
                "prepared_execution_ref": "prepared-public", "approved": True,
            }},
        })
        self.assertEqual(response["result"]["structuredContent"]["execution_started"], True)
        self.assertNotIn("thread_id", response["result"]["structuredContent"])
        self.assertEqual(integration.calls, [{
            "prepared_execution_ref": "prepared-public", "approved": True,
        }])

    def test_start_rejects_arbitrary_execution_identity(self):
        integration = self.FakeIntegration()
        server = ClinxMCPServer(integration)
        response = server.handle({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "clinx_start_execution", "arguments": {
                "prepared_execution_ref": "prepared-public", "approved": True,
                "thread_id": "arbitrary",
            }},
        })
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(integration.calls, [])


if __name__ == "__main__":
    unittest.main()
