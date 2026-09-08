import json
import io
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest

import bridge
import app_server
from m9_integration import ClinxIntegration, M9IntegrationError
from mcp_server import ClinxMCPServer, DEFAULT_TOOL_NAMES, serve_stdio, tool_definitions
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


class FlakyDispatcher(FakeDispatcher):
    def __init__(self, root: Path, registry: TaskRegistry):
        super().__init__(root, registry)
        self.fail_next_dispatch = True

    def dispatch(self, **kwargs):
        self.dispatch_calls.append(kwargs)
        if self.fail_next_dispatch:
            self.fail_next_dispatch = False
            if kwargs.get("task_id"):
                self.registry.set_execution_state(
                    kwargs["task_id"],
                    "RECOVERY_REQUIRED",
                    current_stage="dispatch",
                    current_blocker="recoverable app-server failure",
                    codex_running=False,
                    retry_required=True,
                )
            raise app_server.ModelCapabilityError("model/list returned no usable models")
        task_id = kwargs["task_id"] or "task-created"
        if kwargs.get("task_id"):
            self.registry.set_execution_state(
                kwargs["task_id"],
                "CODEX_RUNNING",
                current_stage="Codex turn",
                current_blocker=None,
                codex_running=True,
                turn_id="turn-dispatched",
                retry_required=False,
            )
        return SimpleNamespace(
            task_id=task_id,
            thread_id="thread-dispatched",
            turn_id="turn-dispatched",
            dispatch_status="DISPATCHED",
            model=kwargs["model"],
            reasoning_effort=kwargs["reasoning_effort"],
            execution_mode=kwargs["execution_mode"],
        )


class OrphanReconcilingDispatcher(FakeDispatcher):
    def __init__(self, root: Path, registry: TaskRegistry):
        super().__init__(root, registry)
        self.reconcile_calls = []

    def reconcile_execution(self, execution_ref=None, *, task_id=None):
        self.reconcile_calls.append((execution_ref, task_id))
        if task_id is None:
            return {"state": "UNKNOWN", "authoritative": False}
        reconciled = self.registry.reconcile_orphaned_terminal(task_id, "COMPLETED")
        return {
            "state": "COMPLETED" if reconciled is not None else "UNKNOWN",
            "authoritative": reconciled is not None,
        }

    def reconcile_task(self, task_id):
        return self.reconcile_execution(None, task_id=task_id)


class UncertainOrphanDispatcher(OrphanReconcilingDispatcher):
    def reconcile_execution(self, execution_ref=None, *, task_id=None):
        self.reconcile_calls.append((execution_ref, task_id))
        return {"state": "TRANSPORT_UNCERTAIN", "authoritative": False}


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
            self.assertFalse(prepared.network_access)
            self.assertFalse(result["network_access"])
            self.assertEqual(result["NETWORK_ACCESS"], "DISABLED")
            self.assertIn("NETWORK_ACCESS=DISABLED", result["description"])
            self.assertEqual(dispatcher.dispatch_calls, [])

    def test_network_access_is_sealed_and_start_cannot_override_it(self):
        with tempfile.TemporaryDirectory() as td:
            integration, registry, dispatcher, task = make_fixture(Path(td))
            prepared_result = integration.prepare_execution(
                prompt="inspect production readiness",
                approved=True,
                task_ref=task.task_id,
                network_access=True,
            )
            prepared = registry.verify_prepared_execution(
                prepared_result["prepared_execution_ref"]
            )
            self.assertTrue(prepared.network_access)
            self.assertTrue(prepared_result["network_access"])
            self.assertEqual(prepared_result["NETWORK_ACCESS"], "ENABLED")
            self.assertIn("NETWORK_ACCESS=ENABLED", prepared_result["description"])

            status = integration.get_status(task_ref=task.task_id)
            self.assertTrue(status["network_access"])
            self.assertEqual(status["NETWORK_ACCESS"], "ENABLED")

            with self.assertRaises(TypeError):
                integration.start_execution(
                    prepared_execution_ref=prepared_result["prepared_execution_ref"],
                    approved=True,
                    network_access=False,
                )

            result = integration.start_execution(
                prepared_execution_ref=prepared_result["prepared_execution_ref"],
                approved=True,
            )
            self.assertTrue(result["network_access"])
            self.assertEqual(result["NETWORK_ACCESS"], "ENABLED")
            self.assertTrue(dispatcher.dispatch_calls[0]["network_access"])

    def test_mcp_network_contract_is_explicit_and_start_has_no_override(self):
        tools = {tool["name"]: tool for tool in tool_definitions()}
        prepare = tools["clinx_prepare_execution"]
        self.assertEqual(
            prepare["inputSchema"]["properties"]["network_access"],
            {"type": "boolean", "default": False},
        )
        self.assertEqual(
            tools["clinx_start_execution"]["inputSchema"]["properties"].keys(),
            {"prepared_execution_ref", "approved"},
        )
        self.assertEqual(
            tools["clinx_start_execution"]["outputSchema"]["properties"]["NETWORK_ACCESS"],
            {"type": "string", "enum": ["ENABLED", "DISABLED"]},
        )
        self.assertEqual(
            tools["clinx_get_status"]["outputSchema"]["properties"]["network_access"],
            {"type": "boolean"},
        )

    def test_prepare_persists_logical_and_provider_model_layers(self):
        with tempfile.TemporaryDirectory() as td:
            integration, registry, _dispatcher, task = make_fixture(Path(td))
            result = integration.prepare_execution(
                prompt="continue the task", approved=True, task_ref=task.task_id,
                model="Terra", reasoning_effort="low",
            )
            prepared = registry.get_prepared_execution(result["prepared_execution_ref"])
            self.assertEqual(prepared.logical_model, "Terra")
            self.assertEqual(prepared.resolved_executable_model, "Terra")

    def test_model_capability_failure_restores_prepared_execution_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
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
            dispatcher = FlakyDispatcher(root, registry)
            context = FakeContext(task)
            cfg = SimpleNamespace(team_id="team", trigger_label="local-codex", todo_state="Todo", projects=())
            integration = ClinxIntegration(cfg, registry, dispatcher, context, None)
            prepared = integration.prepare_execution(
                prompt="continue the task",
                approved=True,
                task_ref=task.task_id,
                model="Terra",
                reasoning_effort="medium",
            )
            with self.assertRaises(app_server.ModelCapabilityError):
                integration.start_execution(
                    prepared_execution_ref=prepared["prepared_execution_ref"],
                    approved=True,
                )
            current = registry.get_prepared_execution(prepared["prepared_execution_ref"])
            self.assertEqual(current.status, "PREPARED")
            self.assertEqual(registry.get_task(task.task_id).execution_state, "RECOVERY_REQUIRED")
            self.assertFalse(registry.get_task(task.task_id).codex_running)
            retry = integration.start_execution(
                prepared_execution_ref=prepared["prepared_execution_ref"],
                approved=True,
            )
            self.assertEqual(retry["dispatch_status"], "DISPATCHED")
            self.assertEqual(registry.get_prepared_execution(prepared["prepared_execution_ref"]).status, "DISPATCHED")
            self.assertEqual(len(dispatcher.dispatch_calls), 2)


class M12RecoveryAndCancellationTests(unittest.TestCase):
    def _active(self, registry, task):
        with registry.execution(task.task_id, execution_ref="exec_recovery", retain=True):
            registry.set_execution_state(
                task.task_id, "CODEX_RUNNING", current_stage="Codex turn",
                turn_id="turn-recovery", codex_running=True,
            )

    def test_provider_unavailable_cancel_is_durable_and_retains_lease(self):
        with tempfile.TemporaryDirectory() as td:
            integration, registry, dispatcher, task = make_fixture(Path(td))
            self._active(registry, task)
            calls = []
            dispatcher.cancel_execution = lambda _ref: calls.append(True) or (_ for _ in ()).throw(
                RuntimeError("provider unavailable")
            )
            result = integration.cancel_execution(execution_ref="exec_recovery")
            self.assertEqual(result["status"], "CANCELLATION_PENDING")
            self.assertTrue(result["cancel_requested"])
            self.assertFalse(result["cancel_confirmed"])
            self.assertIsNotNone(registry.get_active_execution("exec_recovery"))
            self.assertEqual(registry.get_task(task.task_id).execution_state, "CANCELLATION_PENDING")
            repeated = integration.cancel_execution(execution_ref="exec_recovery")
            self.assertTrue(repeated["idempotent"])
            self.assertEqual(len(calls), 1)

    def test_terminal_reconciliation_clears_running_and_releases_lease(self):
        with tempfile.TemporaryDirectory() as td:
            _integration, registry, _dispatcher, task = make_fixture(Path(td))
            self._active(registry, task)
            reconciled = registry.reconcile_terminal(
                "exec_recovery", "RECOVERY_REQUIRED", failure_stage="transport",
                failure_code="APP_SERVER_TRANSPORT_FAILURE", evidence="exit 127",
            )
            self.assertFalse(reconciled.codex_running)
            self.assertEqual(reconciled.execution_state, "RECOVERY_REQUIRED")
            self.assertEqual(reconciled.failure_code, "APP_SERVER_TRANSPORT_FAILURE")
            self.assertIsNone(registry.get_active_execution("exec_recovery"))

    def test_confirmed_cancel_is_idempotent_after_lease_release(self):
        with tempfile.TemporaryDirectory() as td:
            integration, registry, dispatcher, task = make_fixture(Path(td))
            self._active(registry, task)
            dispatcher.cancel_execution = None
            first = integration.cancel_execution(execution_ref="exec_recovery")
            second = integration.cancel_execution(execution_ref="exec_recovery")
            self.assertEqual(first["status"], "CANCELLED")
            self.assertTrue(second["idempotent"])
            self.assertTrue(second["cancel_confirmed"])
            self.assertIsNone(registry.get_active_execution("exec_recovery"))


class M124OrphanedLeaseTests(unittest.TestCase):
    def _orphan(self, registry, task):
        with registry.execution(task.task_id, execution_ref=None, retain=True):
            registry.set_execution_state(
                task.task_id, "CODEX_RUNNING", current_stage="Codex turn",
                turn_id="turn-orphan", codex_running=True,
            )

    def test_get_status_self_heals_null_ref_terminal_owner_durably_and_idempotently(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            integration, registry, _dispatcher, task = make_fixture(root)
            dispatcher = OrphanReconcilingDispatcher(root, registry)
            integration.dispatcher = dispatcher
            self._orphan(registry, task)

            status = integration.get_status(task_ref=task.task_id)
            self.assertEqual(status["EXECUTION_STATE"], "COMPLETED")
            self.assertFalse(status["CODEX_RUNNING"])
            self.assertIsNone(status["active_execution"])
            self.assertIsNone(registry.get_latest_execution_for_task(task.task_id))
            self.assertIsNone(registry.active_worktree_conflict(
                host=task.host, cwd=task.cwd, repository_origin=task.repository_origin
            ))

            repeated = integration.get_status(task_ref=task.task_id)
            self.assertEqual(repeated["EXECUTION_STATE"], "COMPLETED")
            self.assertFalse(repeated["CODEX_RUNNING"])
            self.assertEqual(len(dispatcher.reconcile_calls), 1)

    def test_start_conflict_self_heals_same_prepared_ref_and_proceeds(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            integration, registry, _dispatcher, task = make_fixture(root)
            dispatcher = OrphanReconcilingDispatcher(root, registry)
            integration.dispatcher = dispatcher
            self._orphan(registry, task)
            prepared = integration.prepare_execution(
                prompt="continue after stale lease", approved=True, task_ref=task.task_id,
            )

            result = integration.start_execution(
                prepared_execution_ref=prepared["prepared_execution_ref"], approved=True,
            )
            self.assertTrue(result["execution_started"])
            self.assertEqual(result["dispatch_status"], "DISPATCHED")
            self.assertEqual(dispatcher.reconcile_calls, [(None, task.task_id)])
            self.assertEqual(len(dispatcher.dispatch_calls), 1)

    def test_uncertain_owner_keeps_null_ref_lease(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            integration, registry, _dispatcher, task = make_fixture(root)
            dispatcher = UncertainOrphanDispatcher(root, registry)
            integration.dispatcher = dispatcher
            self._orphan(registry, task)

            status = integration.get_status(task_ref=task.task_id)
            self.assertEqual(status["EXECUTION_STATE"], "CODEX_RUNNING")
            self.assertTrue(status["CODEX_RUNNING"])
            self.assertIsNotNone(registry.active_worktree_conflict(
                host=task.host, cwd=task.cwd, repository_origin=task.repository_origin
            ))

    def test_missing_execution_row_is_reconciled_from_task_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            integration, registry, _dispatcher, task = make_fixture(root)
            dispatcher = OrphanReconcilingDispatcher(root, registry)
            integration.dispatcher = dispatcher
            registry.set_execution_state(
                task.task_id, "CODEX_RUNNING", current_stage="Codex turn",
                turn_id="turn-legacy", codex_running=True,
            )

            status = integration.get_status(task_ref=task.task_id)
            self.assertEqual(status["EXECUTION_STATE"], "COMPLETED")
            self.assertFalse(status["CODEX_RUNNING"])
            self.assertEqual(dispatcher.reconcile_calls, [(None, task.task_id)])


class M12ModelCapabilityTests(unittest.TestCase):
    class FakeTransport:
        def __init__(self, models):
            self.models = models
            self.responses = []

        def send(self, message):
            if message.get("method") == "model/list":
                self.responses.append({"id": message["id"], "result": {"data": self.models}})

        def receive(self, _timeout):
            return self.responses.pop(0)

        def close(self):
            pass

    def client(self, models):
        return app_server.CodexAppServerClient(self.FakeTransport(models))

    def test_logical_model_resolves_to_unique_provider(self):
        client = self.client([{
            "id": "gpt-5.6-terra", "model": "Terra", "displayName": "Terra",
            "supportedReasoningEfforts": ["low", "high"],
            "defaultReasoningEffort": "high",
        }])
        self.assertEqual(client.resolve_model("Terra", "low"), ("gpt-5.6-terra", "low"))

    def test_unsupported_and_ambiguous_models_fail_closed(self):
        models = [
            {"id": "gpt-5.6-terra", "model": "Terra", "supportedReasoningEfforts": ["high"]},
            {"id": "gpt-5.7-terra", "model": "Terra", "supportedReasoningEfforts": ["high"]},
        ]
        with self.assertRaises(app_server.ModelCapabilityError):
            self.client(models).resolve_model("Terra", "high")
        with self.assertRaises(app_server.ModelCapabilityError):
            self.client(models).resolve_model("missing", "high")

    def test_reasoning_must_be_supported(self):
        client = self.client([{
            "id": "gpt-5.6-terra", "model": "Terra",
            "supportedReasoningEfforts": ["high"], "defaultReasoningEffort": "high",
        }])
        with self.assertRaises(app_server.ModelCapabilityError):
            client.resolve_model("Terra", "low")

    def test_unrelated_malformed_model_does_not_block_valid_selection(self):
        client = self.client([
            {"id": "gpt-4o-audio-preview", "supportedReasoningEfforts": "unsupported"},
            {"id": "gpt-5.6-terra", "model": "Terra",
             "supportedReasoningEfforts": ["medium"], "defaultReasoningEffort": "medium"},
        ])
        self.assertEqual(client.resolve_model("Terra", "medium"), ("gpt-5.6-terra", "medium"))

    def test_requested_malformed_model_still_fails_closed(self):
        client = self.client([
            {"id": "gpt-4o-audio-preview", "model": "Audio",
             "supportedReasoningEfforts": "unsupported"},
            {"id": "gpt-5.6-terra", "model": "Terra",
             "supportedReasoningEfforts": ["medium"]},
        ])
        with self.assertRaises(app_server.ModelCapabilityError):
            client.resolve_model("Audio", "medium")

    def test_current_provider_shape_resolves_to_usable_model(self):
        client = self.client([
            {
                "id": "gpt-5.6-terra",
                "model": "Terra",
                "displayName": "GPT-5.6-Terra",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low", "description": "Fast responses"},
                    {"reasoningEffort": "medium", "description": "Balanced responses"},
                    {"reasoningEffort": "high", "description": "Deeper reasoning"},
                ],
                "defaultReasoningEffort": "medium",
            }
        ])
        self.assertEqual(client.resolve_model("Terra", "medium"), ("gpt-5.6-terra", "medium"))

    def test_empty_model_list_fails_closed(self):
        with self.assertRaises(app_server.ModelCapabilityError):
            self.client([]).resolve_model("Terra", "medium")

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
        self.assertEqual(len(DEFAULT_TOOL_NAMES), 9)
        self.assertIn("clinx_start_execution", DEFAULT_TOOL_NAMES)
        self.assertIn("clinx_cancel_execution", DEFAULT_TOOL_NAMES)
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

    def test_stdio_loop_survives_model_capability_failure(self):
        class FlakyIntegration:
            def __init__(self):
                self.calls = []

            def start_execution(self, **kwargs):
                self.calls.append(("start_execution", kwargs))
                raise app_server.ModelCapabilityError("model/list returned no usable models")

            def get_capabilities(self, **kwargs):
                self.calls.append(("get_capabilities", kwargs))
                return {"execution_available": True, "read_only": True}

            def execute(self, **kwargs):
                self.calls.append(("execute", kwargs))
                return {"read_only": True}

        server = ClinxMCPServer(FlakyIntegration())
        stdin = io.StringIO(
            "\n".join([
                json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "clinx_start_execution",
                        "arguments": {"prepared_execution_ref": "prepared-public", "approved": True},
                    },
                }),
                json.dumps({
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "clinx_get_capabilities",
                        "arguments": {},
                    },
                }),
                "",
            ])
        )
        stdout = io.StringIO()
        serve_stdio(server, stdin=stdin, stdout=stdout)
        lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0]["result"]["isError"])
        self.assertEqual(lines[0]["result"]["structuredContent"]["failure_stage"], "MODEL_RESOLUTION")
        self.assertEqual(lines[0]["result"]["structuredContent"]["failure_code"], "MODEL_CAPABILITY_UNAVAILABLE")
        self.assertFalse(lines[0]["result"]["structuredContent"]["codex_running"])
        self.assertTrue(lines[0]["result"]["structuredContent"]["retry_required"])
        self.assertIn("execution_available", lines[1]["result"]["structuredContent"])
        self.assertTrue(lines[1]["result"]["structuredContent"]["read_only"])


if __name__ == "__main__":
    unittest.main()
