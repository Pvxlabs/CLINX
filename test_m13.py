import json
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest

import bridge
from execution_semantics import (
    SemanticsError,
    build_routing_identity,
    legacy_routing_identity,
    normalize_host,
    normalize_network_policy,
    normalize_surface,
    normalize_transport,
    parse_routing_identity,
)
from m9_integration import ClinxIntegration
from mcp_server import ClinxMCPServer, DEFAULT_TOOL_NAMES, tool_definitions
from task_registry import TaskRegistry, TaskRegistryError
from test_m6 import FakeClient, dispatcher_fixture


def route(*, conversation_binding="UNBOUND", network_access=False, transport="local"):
    return build_routing_identity(
        host="P620",
        surface="codex-app-server",
        provider="codex",
        transport=transport,
        workspace_alias="p620",
        project_alias="clinx",
        worktree_key="worktree:p620:clinx",
        project_identity="clinx",
        conversation_binding=conversation_binding,
        network_access=network_access,
    )


def make_task(registry, *, routing=None):
    return registry.create_task(
        host="p620",
        workspace_alias="p620",
        project_alias="clinx",
        project_name="CLINX",
        cwd=str(registry.path.parent),
        repository_origin="git@github.com:example/clinx.git",
        branch="main",
        title="M13-A qualification task",
        summary="bounded semantics",
        routing_identity=routing or route(),
    )


class M13SemanticsTests(unittest.TestCase):
    def test_result_parser_uses_only_assistant_turn_text(self):
        turn = {
            "items": [
                {
                    "type": "userMessage",
                    "content": "CLINX_EXECUTION_RESULT\nSTATUS=PASS\n"
                    "SUMMARY=prompt copy\nCHANGED_FILES=NONE\n"
                    "VALIDATION=prompt\nBLOCKERS=NONE\nNEXT_STATE=COMPLETED",
                },
                {
                    "type": "agentMessage",
                    "text": "CLINX_EXECUTION_RESULT\nSTATUS=PASS\n"
                    "SUMMARY=provider response\nCHANGED_FILES=NONE\n"
                    "VALIDATION=provider\nBLOCKERS=NONE\nNEXT_STATE=COMPLETED",
                },
            ]
        }
        text = bridge.TaskDispatcher._turn_text(turn, assistant_only=True)
        self.assertNotIn("prompt copy", text)
        from m9_integration import parse_codex_result
        self.assertEqual(parse_codex_result(text).summary, "provider response")

    def test_host_identity_separates_stable_display_machine_and_alias(self):
        identity = normalize_host(
            "WORKSTATION-P620",
            display="P620 development host",
            machine_id="machine-123",
        )
        self.assertEqual(identity.stable_identifier, "p620")
        self.assertEqual(identity.alias, "p620")
        self.assertEqual(identity.display, "P620 development host")
        self.assertEqual(identity.machine_id, "machine-123")

    def test_ip_is_identity_only_and_normalized_deterministically(self):
        self.assertEqual(normalize_host("2001:0db8::1").stable_identifier, "ip:2001:db8::1")
        self.assertEqual(normalize_host("192.0.2.1").stable_identifier, "ip:192.0.2.1")
        self.assertNotEqual(normalize_host("192.0.2.1").stable_identifier, "clinx_task_bounded")

    def test_unknown_and_malformed_host_fail_closed(self):
        for value in ("unknown", "unset", "", "?", None):
            with self.subTest(value=value), self.assertRaises(SemanticsError):
                normalize_host(value)

    def test_surface_transport_and_network_normalization_are_explicit(self):
        self.assertEqual(normalize_surface("app-server").stable_identifier, "codex_app_server")
        self.assertEqual(normalize_transport("stdio").stable_identifier, "local_stdio")
        self.assertFalse(normalize_network_policy(False).remote_execution)
        with self.assertRaises(SemanticsError):
            normalize_surface("generic-shell")
        with self.assertRaises(SemanticsError):
            normalize_transport("remote-shell")
        with self.assertRaises(SemanticsError):
            normalize_network_policy("true")

    def test_route_separates_provider_transport_authority_and_network(self):
        identity = route(network_access=True)
        public = identity.public_dict()
        self.assertEqual(public["routing_contract"], "execution -> host -> surface -> provider -> transport")
        self.assertFalse(public["separation"]["host_is_authority"])
        self.assertFalse(public["separation"]["transport_is_authority"])
        self.assertFalse(public["network_policy"]["remote_execution"])
        self.assertTrue(public["network_policy"]["network_access"])
        self.assertEqual(public["conversation"]["binding"], "REDACTED")

    def test_invalid_combinations_and_silent_fallback_fail_closed(self):
        with self.assertRaisesRegex(SemanticsError, "unsupported host/surface pair"):
            build_routing_identity(
                host="not-configured",
                workspace_alias="p620",
                project_alias="clinx",
                worktree_key="w",
                project_identity="clinx",
                supported_hosts={"p620"},
            )
        with self.assertRaisesRegex(SemanticsError, "unsupported provider"):
            build_routing_identity(
                host="p620",
                surface="codex_app_server",
                provider="other-provider",
                workspace_alias="p620",
                project_alias="clinx",
                worktree_key="w",
                project_identity="clinx",
            )
        raw = json.loads(route().to_json())
        raw["surface"]["stable_identifier"] = "unsupported_surface"
        with self.assertRaises(SemanticsError):
            parse_routing_identity(json.dumps(raw))

    def test_transport_failure_is_not_terminal_execution_proof(self):
        identity = route()
        self.assertTrue(identity.executable)
        self.assertFalse(identity.network_policy.remote_execution)
        self.assertEqual(identity.transport.status, "SUPPORTED")
        self.assertNotIn("transport_failure", identity.as_dict())


class M13PersistenceTests(unittest.TestCase):
    def test_pre_m13_schema_migrates_existing_rows_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "legacy.sqlite3"
            with sqlite3.connect(path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE tasks (
                        task_id TEXT PRIMARY KEY,
                        host TEXT NOT NULL,
                        workspace_alias TEXT NOT NULL,
                        project_alias TEXT NOT NULL,
                        project_name TEXT NOT NULL,
                        cwd TEXT NOT NULL,
                        repository_origin TEXT,
                        branch TEXT,
                        title TEXT NOT NULL,
                        summary TEXT,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE executions (
                        task_id TEXT PRIMARY KEY,
                        issue_id TEXT,
                        execution_ref TEXT,
                        worktree_key TEXT,
                        logical_model TEXT,
                        resolved_model TEXT,
                        stage TEXT NOT NULL DEFAULT 'CLAIMED',
                        acquired_at TEXT NOT NULL
                    );
                    CREATE TABLE prepared_executions (
                        prepared_execution_ref TEXT PRIMARY KEY,
                        integrity_hash TEXT NOT NULL,
                        approval_state TEXT NOT NULL,
                        task_action TEXT NOT NULL,
                        task_ref TEXT,
                        host TEXT NOT NULL,
                        project TEXT NOT NULL,
                        title TEXT NOT NULL,
                        summary TEXT,
                        prompt TEXT NOT NULL,
                        model TEXT NOT NULL,
                        logical_model TEXT NOT NULL DEFAULT '',
                        resolved_executable_model TEXT NOT NULL DEFAULT '',
                        reasoning_effort TEXT NOT NULL,
                        execution_mode TEXT NOT NULL,
                        network_access INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        resulting_task_id TEXT,
                        resulting_thread_id TEXT,
                        resulting_turn_id TEXT,
                        resulting_execution_ref TEXT
                    );
                    INSERT INTO tasks VALUES (
                        'task-legacy', 'P620', 'p620', 'pilot', 'Pilot',
                        '/tmp/pilot', 'https://example.invalid/pilot.git', 'main',
                        'Legacy task', 'Pre-M13 row', 'ACTIVE',
                        '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00'
                    );
                    INSERT INTO executions VALUES (
                        'task-legacy', 'PVX-LEGACY', 'exec-legacy',
                        'legacy-worktree', 'logical-model', 'provider-model',
                        'CODEX_RUNNING', '2026-09-01T00:00:01+00:00'
                    );
                    INSERT INTO prepared_executions VALUES (
                        'prepared-legacy', 'sealed-hash', 'APPROVED', 'create',
                        NULL, 'p620', 'pilot', 'Legacy preparation', NULL,
                        'legacy prompt', 'logical-model', '', '', 'medium',
                        'normal', 0, 'PREPARED',
                        '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00',
                        NULL, NULL, NULL, NULL
                    );
                    """
                )

            registry = TaskRegistry(path)
            task = registry.get_task("task-legacy")
            prepared = registry.get_prepared_execution("prepared-legacy")
            self.assertIsNotNone(prepared)
            task_route = parse_routing_identity(task.routing_identity_json)
            prepared_route = parse_routing_identity(prepared.routing_identity_json)
            self.assertIsNotNone(task_route)
            self.assertIsNotNone(prepared_route)
            self.assertEqual(task_route.surface.status, "UNKNOWN")
            self.assertEqual(prepared_route.provider.status, "UNKNOWN")
            self.assertIsNone(registry.get_execution_routing_identity("exec-legacy"))
            self.assertEqual(prepared.logical_model, "logical-model")
            self.assertEqual(prepared.resolved_executable_model, "logical-model")

            with sqlite3.connect(path) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertIn("execution_history", tables)
                task_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(tasks)")
                }
                execution_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(executions)")
                }
                prepared_columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(prepared_executions)")
                }
                self.assertIn("routing_identity_json", task_columns)
                self.assertIn("routing_identity_json", execution_columns)
                self.assertIn("routing_identity_json", prepared_columns)
                self.assertEqual(
                    conn.execute(
                        "SELECT routing_identity_json FROM executions "
                        "WHERE execution_ref='exec-legacy'"
                    ).fetchone()[0],
                    "{}",
                )
            task_route_json = task.routing_identity_json
            prepared_route_json = prepared.routing_identity_json

            restarted = TaskRegistry(path)
            self.assertEqual(
                restarted.get_task("task-legacy").routing_identity_json,
                task_route_json,
            )
            self.assertEqual(
                restarted.get_prepared_execution("prepared-legacy").routing_identity_json,
                prepared_route_json,
            )

    def test_route_survives_restart_and_terminal_reconciliation_releases_only_lease(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tasks.sqlite3"
            registry = TaskRegistry(path)
            task = make_task(registry)
            with registry.execution(task.task_id, execution_ref="exec_m13", retain=True):
                registry.set_execution_state(
                    task.task_id,
                    "CODEX_RUNNING",
                    current_stage="Codex turn",
                    turn_id="turn-m13",
                    codex_running=True,
                )
            registry.reconcile_terminal("exec_m13", "COMPLETED")
            self.assertIsNone(registry.get_active_execution("exec_m13"))
            self.assertIsNotNone(registry.get_execution_routing_identity("exec_m13"))
            self.assertIsNone(registry.active_worktree_conflict(
                host=task.host, cwd=task.cwd, repository_origin=task.repository_origin
            ))
            restarted = TaskRegistry(path)
            self.assertEqual(
                restarted.get_execution_routing_identity("exec_m13").as_dict(),
                route().as_dict(),
            )

    def test_cancellation_retains_execution_route_and_legacy_orphan_cleanup_still_deletes_row(self):
        with tempfile.TemporaryDirectory() as td:
            registry = TaskRegistry(Path(td) / "tasks.sqlite3")
            task = make_task(registry)
            with registry.execution(task.task_id, execution_ref="exec_cancel", retain=True):
                registry.set_execution_state(
                    task.task_id, "CODEX_RUNNING", current_stage="Codex turn",
                    turn_id="turn-cancel", codex_running=True,
                )
            registry.finalize_cancellation("exec_cancel")
            self.assertIsNotNone(registry.get_execution_routing_identity("exec_cancel"))
            orphan = make_task(registry)
            with registry.execution(orphan.task_id, execution_ref=None, retain=True):
                registry.set_execution_state(
                    orphan.task_id, "CODEX_RUNNING", current_stage="Codex turn",
                    turn_id="turn-orphan", codex_running=True,
                )
            registry.reconcile_orphaned_terminal(orphan.task_id, "COMPLETED")
            self.assertIsNone(registry.get_latest_execution_for_task(orphan.task_id))

    def test_binding_updates_only_the_selected_execution(self):
        with tempfile.TemporaryDirectory() as td:
            registry = TaskRegistry(Path(td) / "tasks.sqlite3")
            old_task = make_task(registry)
            with registry.execution(old_task.task_id, execution_ref="exec_old"):
                pass
            current_task = make_task(registry)
            bound = route(conversation_binding="thread-current")
            with registry.execution(current_task.task_id, execution_ref="exec_current"):
                registry.bind_routing_identity(
                    task_id=current_task.task_id,
                    routing_identity=bound,
                    execution_ref="exec_current",
                )
            self.assertEqual(
                registry.get_execution_routing_identity("exec_old").conversation.status,
                "UNBOUND",
            )
            self.assertEqual(
                registry.get_execution_routing_identity("exec_current").conversation.binding,
                "thread-current",
            )

    def test_legacy_normalization_is_deterministic_and_does_not_backfill_execution(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tasks.sqlite3"
            registry = TaskRegistry(path)
            task = make_task(registry)
            with registry.execution(task.task_id, execution_ref="exec_legacy", retain=True):
                pass
            with sqlite3.connect(path) as conn:
                conn.execute("UPDATE tasks SET routing_identity_json='{}' WHERE task_id=?", (task.task_id,))
                conn.execute("UPDATE executions SET routing_identity_json='{}' WHERE execution_ref=?", ("exec_legacy",))
            restarted = TaskRegistry(path)
            normalized = parse_routing_identity(restarted.get_task(task.task_id).routing_identity_json)
            self.assertEqual(normalized.surface.status, "UNKNOWN")
            self.assertIsNone(restarted.get_execution_routing_identity("exec_legacy"))
            self.assertEqual(
                restarted.get_task(task.task_id).routing_identity_json,
                TaskRegistry(path).get_task(task.task_id).routing_identity_json,
            )
            legacy = legacy_routing_identity(
                host="p620", workspace_alias="p620", project_alias="clinx",
                cwd=str(Path(td)), project_identity="clinx", worktree_key="",
            )
            self.assertEqual(legacy.workspace.worktree_key, legacy_routing_identity(
                host="p620", workspace_alias="p620", project_alias="clinx",
                cwd=str(Path(td)), project_identity="clinx", worktree_key="",
            ).workspace.worktree_key)

    def test_route_immutability_rejects_host_surface_and_network_drift(self):
        with tempfile.TemporaryDirectory() as td:
            registry = TaskRegistry(Path(td) / "tasks.sqlite3")
            task = make_task(registry)
            with self.assertRaisesRegex(TaskRegistryError, "host changed"):
                registry.update_routing_identity(task.task_id, build_routing_identity(
                    host="other-host", workspace_alias="p620", project_alias="clinx",
                    worktree_key="worktree:p620:clinx", project_identity="clinx",
                ))
            with self.assertRaisesRegex(TaskRegistryError, "network policy changed"):
                registry.bind_routing_identity(
                    task_id=task.task_id,
                    routing_identity=route(conversation_binding="thread", network_access=True),
                )


class M13LifecycleRoutingTests(unittest.TestCase):
    class FakeProvider:
        def __init__(self, target, *, turn_status="inProgress"):
            self.target = target
            self.turn_status = turn_status

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def initialize(self, **_kwargs):
            return {"version": "test"}

        def turn_interrupt(self, _thread_id, _turn_id):
            return {"ok": True}

        def thread_turns_list(self, _thread_id, **_kwargs):
            return {
                "data": [{
                    "id": "turn-lifecycle",
                    "status": self.turn_status,
                    "items": [],
                }]
            }

    def _dispatcher(self, root: Path, registry: TaskRegistry, captured: list):
        dispatcher = bridge.TaskDispatcher.__new__(bridge.TaskDispatcher)
        dispatcher.tasks = registry
        dispatcher.cfg = SimpleNamespace(
            runtime_host="p620",
            approval="never",
            app_server=bridge.AppServerConfig(
                transport="local",
                ssh_alias="p620",
                client_name="test",
                client_title="test",
                client_version="test",
            ),
        )
        dispatcher.workspaces = bridge.WorkspaceRegistry((
            bridge.WorkspaceConfig(
                alias="p620", root=root, host="p620", ssh_alias="p620",
            ),
        ))

        def factory(target):
            captured.append(target)
            return self.FakeProvider(target)

        dispatcher.client_factory = factory
        return dispatcher

    def _active_task(self, registry: TaskRegistry, *, execution_ref="exec_lifecycle"):
        task = make_task(
            registry,
            routing=route(
                conversation_binding="thread-lifecycle",
                transport="ssh",
            ),
        )
        registry.bind_conversation(
            task_id=task.task_id,
            thread_id="thread-lifecycle",
            session_id="session-lifecycle",
            project_id=None,
            app_server_version="test",
        )
        with registry.execution(task.task_id, execution_ref=execution_ref, retain=True):
            registry.set_execution_state(
                task.task_id,
                "CODEX_RUNNING",
                current_stage="Codex turn",
                turn_id="turn-lifecycle",
                codex_running=True,
            )
        return task

    def test_cancel_uses_persisted_transport_when_current_default_is_local(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = TaskRegistry(root / "tasks.sqlite3")
            task = self._active_task(registry, execution_ref="exec_cancel_route")
            captured = []
            dispatcher = self._dispatcher(root, registry, captured)

            result = dispatcher.cancel_execution("exec_cancel_route")

            self.assertEqual(result["status"], "CANCELLED")
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0].transport, "ssh")
            self.assertEqual(
                result["routing_identity"]["transport"]["stable_identifier"],
                "ssh_stdio",
            )
            self.assertIsNone(registry.active_worktree_conflict(
                host=task.host, cwd=task.cwd, repository_origin=task.repository_origin,
            ))

    def test_reconcile_uses_persisted_transport_when_current_default_is_local(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = TaskRegistry(root / "tasks.sqlite3")
            task = self._active_task(registry, execution_ref="exec_reconcile_route")
            captured = []
            dispatcher = self._dispatcher(root, registry, captured)

            result = dispatcher.reconcile_execution("exec_reconcile_route")

            self.assertEqual(result["state"], "CODEX_RUNNING")
            self.assertFalse(result["authoritative"])
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0].transport, "ssh")
            self.assertIsNotNone(registry.active_worktree_conflict(
                host=task.host, cwd=task.cwd, repository_origin=task.repository_origin,
            ))

    def test_missing_execution_route_fails_closed_without_provider_or_lease_release(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "tasks.sqlite3"
            registry = TaskRegistry(path)
            task = self._active_task(registry, execution_ref="exec_missing_route")
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "UPDATE executions SET routing_identity_json='{}' "
                    "WHERE execution_ref=?",
                    ("exec_missing_route",),
                )
            captured = []
            dispatcher = self._dispatcher(root, registry, captured)

            result = dispatcher.reconcile_execution("exec_missing_route")

            self.assertEqual(result["state"], "RECOVERY_REQUIRED")
            self.assertFalse(result["authoritative"])
            self.assertEqual(result["failure_code"], "ROUTING_IDENTITY_UNAVAILABLE")
            self.assertEqual(captured, [])
            self.assertIsNotNone(registry.active_worktree_conflict(
                host=task.host, cwd=task.cwd, repository_origin=task.repository_origin,
            ))
            self.assertEqual(
                registry.get_task(task.task_id).execution_state,
                "RECOVERY_REQUIRED",
            )

    def test_continuation_route_reuses_persisted_transport_over_local_default(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = TaskRegistry(root / "tasks.sqlite3")
            task = self._active_task(registry, execution_ref="exec_continue_route")
            captured = []
            dispatcher = self._dispatcher(root, registry, captured)
            workspace = dispatcher.workspaces.resolve(task.workspace_alias)
            project = bridge.ProjectMapping(
                linear_name=task.project_name,
                repo=Path(task.cwd),
                alias=task.project_alias,
                repository_origin=task.repository_origin,
                branch=task.branch,
                workspace_alias=task.workspace_alias,
            )
            binding = registry.get_binding(task.task_id)
            self.assertIsNotNone(binding)

            persisted = dispatcher._routing_identity(
                workspace=workspace,
                project=project,
                conversation_bound=True,
                network_access=False,
                supplied=task.routing_identity_json,
            )

            self.assertEqual(persisted.transport.stable_identifier, "ssh_stdio")
            self.assertEqual(
                dispatcher._target(workspace, project, binding, persisted).transport,
                "ssh",
            )


class M13AdoptionRoutingTests(unittest.TestCase):
    def test_adopted_route_is_bound_and_reused_by_continuation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "dev"
            root.mkdir()
            client = FakeClient(existing={
                "id": "thread-adopted",
                "sessionId": "session-adopted",
                "projectId": None,
                "cwd": str(root / "pilot"),
                "ephemeral": False,
                "gitInfo": {
                    "originUrl": "https://example.invalid/pilot.git",
                    "branch": "main",
                },
                "canAcceptDirectInput": True,
                "status": {"type": "idle"},
            })
            dispatcher, _repo = dispatcher_fixture(
                root, Path(td) / "tasks.sqlite3", [client]
            )
            task, binding, _index = dispatcher.adopt_existing_conversation(
                project_ref="pilot",
                host="p620",
                thread_id="thread-adopted",
                title="Adopted route qualification",
                summary="Verify route durability across adoption and continuation.",
            )

            adopted = dispatcher.tasks.get_task(task.task_id)
            route = parse_routing_identity(adopted.routing_identity_json)
            self.assertIsNotNone(route)
            self.assertEqual(binding.thread_id, "thread-adopted")
            self.assertEqual(route.conversation.status, "BOUND")
            self.assertEqual(route.conversation.binding, "thread-adopted")
            self.assertEqual(route.host.stable_identifier, "p620")
            self.assertEqual(route.surface.stable_identifier, "codex_app_server")
            self.assertEqual(route.provider.stable_identifier, "codex_app_server")
            self.assertEqual(route.transport.stable_identifier, "local_stdio")

            dispatcher.client_factory = lambda _target: client
            continued = dispatcher.dispatch(
                project_ref="pilot",
                host="p620",
                project_mode="existing",
                task_mode="continue",
                task_id=task.task_id,
                prompt="Continue using the adopted conversation.",
                title=task.title,
                summary=task.summary,
                model=None,
                reasoning_effort=None,
                execution_mode="normal",
                execution_ref="exec_adopted_continue",
                routing_identity=adopted.routing_identity_json,
            )
            self.assertEqual(
                continued.routing_identity["transport"]["stable_identifier"],
                "local_stdio",
            )
            persisted = dispatcher.tasks.get_execution_routing_identity(
                "exec_adopted_continue"
            )
            self.assertIsNotNone(persisted)
            self.assertEqual(persisted.as_dict(), route.as_dict())


class M13ProjectionTests(unittest.TestCase):
    class Context:
        def __init__(self, task):
            self.task = task

        def resolve_task(self, **_kwargs):
            return self.task

    class Dispatcher:
        def __init__(self, root):
            self.root = root

        def resolve_project(self, project_ref, *, host, project_mode):
            descriptor = bridge.ProjectDescriptor(
                alias=project_ref,
                name="CLINX",
                workspace_alias="p620",
                cwd=self.root,
                repository_origin="git@github.com:example/clinx.git",
                branch="main",
                registered=True,
            )
            return SimpleNamespace(alias="p620", host="p620"), descriptor, None

    def test_status_readback_exposes_task_and_execution_route(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = TaskRegistry(root / "tasks.sqlite3")
            task = make_task(registry, routing=route(conversation_binding="thread-m13"))
            with registry.execution(task.task_id, execution_ref="exec_status", retain=True):
                registry.set_execution_state(
                    task.task_id, "CODEX_RUNNING", current_stage="Codex turn",
                    turn_id="turn-status", codex_running=True,
                )
            integration = ClinxIntegration(
                SimpleNamespace(team_id="", projects=()), registry,
                self.Dispatcher(root), self.Context(task), None,
            )
            status = integration.get_status(task_ref=task.task_id)
            self.assertEqual(status["routing_identity"]["host"]["stable_identifier"], "p620")
            self.assertEqual(status["execution_routing_identity"]["transport"]["stable_identifier"], "local_stdio")
            self.assertEqual(status["execution_routing_identity"]["conversation"]["binding"], "REDACTED")

    def test_retained_terminal_execution_is_not_overwritten_or_reported_active(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = TaskRegistry(root / "tasks.sqlite3")
            task = make_task(registry)
            with registry.execution(task.task_id, execution_ref="exec_a", retain=True):
                registry.set_execution_state(
                    task.task_id, "CODEX_RUNNING", current_stage="turn-a",
                    turn_id="turn-a", codex_running=True,
                )
            registry.reconcile_terminal("exec_a", "COMPLETED")

            with registry.execution(task.task_id, execution_ref="exec_b", retain=True):
                registry.set_execution_state(
                    task.task_id, "CODEX_RUNNING", current_stage="turn-b",
                    turn_id="turn-b", codex_running=True,
                )

            self.assertEqual(registry.get_execution_record("exec_a")["stage"], "COMPLETED")
            self.assertEqual(registry.get_execution_record("exec_b")["stage"], "turn-b")

            integration = ClinxIntegration(
                SimpleNamespace(team_id="", projects=()), registry,
                SimpleNamespace(), self.Context(task), None,
            )
            status = integration.get_status(task_ref=task.task_id)
            self.assertEqual(status["active_execution"], {
                "execution_ref": "exec_b", "stage": "turn-b",
            })

            registry.reconcile_terminal("exec_b", "COMPLETED")

    def test_status_retries_only_the_retained_recovery_execution(self):
        class RecoveryDispatcher:
            def __init__(self):
                self.execution_refs = []

            def reconcile_execution(self, execution_ref):
                self.execution_refs.append(execution_ref)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = TaskRegistry(root / "tasks.sqlite3")
            task = make_task(registry)
            with registry.execution(task.task_id, execution_ref="exec_recovery", retain=True):
                registry.set_execution_state(
                    task.task_id, "CODEX_RUNNING", current_stage="Codex turn",
                    turn_id="turn-recovery", codex_running=True,
                )
            registry.reconcile_terminal(
                "exec_recovery", "RECOVERY_REQUIRED", retry_required=True,
            )
            dispatcher = RecoveryDispatcher()
            integration = ClinxIntegration(
                SimpleNamespace(team_id="", projects=()), registry,
                dispatcher, self.Context(task), None,
            )

            status = integration.get_status(execution_ref="exec_recovery")

            self.assertEqual(status["execution_ref"], "exec_recovery")
            self.assertEqual(dispatcher.execution_refs, ["exec_recovery"])

    def test_mcp_catalog_remains_nine_and_execute_is_absent(self):
        self.assertEqual(len(DEFAULT_TOOL_NAMES), 9)
        self.assertNotIn("clinx_execute", DEFAULT_TOOL_NAMES)
        self.assertNotIn("clinx_execute", {item["name"] for item in tool_definitions()})

    def test_mcp_scrubber_does_not_leak_provider_identity_fields(self):
        class Integration:
            def execute(self, **_kwargs):
                raise AssertionError("execute should not be called")

            def get_capabilities(self, **_kwargs):
                return {
                    "routing_identity": route(conversation_binding="thread").public_dict(),
                    "thread_id": "private-thread",
                    "cwd": "/private/worktree",
                    "repository_origin": "private-origin",
                    "authorization": "secret",
                }

        server = ClinxMCPServer(Integration())
        response = server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "clinx_get_capabilities", "arguments": {}},
        })
        structured = response["result"]["structuredContent"]
        self.assertNotIn("thread_id", structured)
        self.assertNotIn("cwd", structured)
        self.assertNotIn("repository_origin", structured)
        self.assertNotIn("authorization", structured)


if __name__ == "__main__":
    unittest.main()
