import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import app_server
from execution_policy import (
    BUSINESS_ACTION,
    DEVELOPMENT_MUTATION,
    HOST_EXECUTOR,
    NETWORKED_SANDBOX,
    PRODUCTION_READ_ONLY,
    PRODUCTION_MUTATION,
    READ_ONLY_HOST,
    SANDBOX_WORKSPACE,
    ExecutionPolicyError,
    build_execution_policy,
    parse_execution_policy,
)
from execution_semantics import SemanticsError, build_routing_identity, normalize_surface
from host_executor import (
    AuthorityDenied,
    HostExecutionRequest,
    HostExecutor,
    HostExecutorConfig,
    RegisteredTarget,
    TargetNotRegistered,
    _Command,
)
from m9_integration import ClinxIntegration, M9IntegrationError
from mcp_server import DEFAULT_TOOL_NAMES, tool_definitions
from task_registry import TaskRegistry, TaskRegistryError, WorktreeExecutionBusy


def host_policy(*, capabilities=("LOCAL_HOST_PROCESS",), classes=(READ_ONLY_HOST,)):
    return build_execution_policy(
        execution_surface=HOST_EXECUTOR,
        required_capabilities=list(capabilities),
        operation_classes=list(classes),
    )


def host_route(root: Path, policy, *, conversation="thread-m13b"):
    return build_routing_identity(
        host="p620",
        surface="host_executor",
        provider="codex_app_server",
        transport="local_stdio",
        workspace_alias="p620",
        project_alias="pilot",
        worktree_key=TaskRegistry.worktree_key(
            host="p620", cwd=str(root), repository_origin=None
        ),
        project_identity="pilot",
        conversation_binding=conversation,
        authority_scopes=policy.authority_scopes,
    )


class PolicyTests(unittest.TestCase):
    def test_sandbox_remains_default(self):
        self.assertEqual(build_execution_policy().execution_surface, SANDBOX_WORKSPACE)

    def test_network_selects_only_networked_sandbox(self):
        policy = build_execution_policy(network_access=True)
        self.assertEqual(policy.execution_surface, NETWORKED_SANDBOX)
        self.assertNotEqual(policy.execution_surface, HOST_EXECUTOR)

    def test_host_identity_does_not_select_host_executor(self):
        route = build_routing_identity(
            host="p620", workspace_alias="p620", project_alias="pilot",
            worktree_key="w", project_identity="pilot",
        )
        self.assertEqual(route.surface.stable_identifier, "codex_app_server")

    def test_host_executor_requires_explicit_capability(self):
        with self.assertRaisesRegex(ExecutionPolicyError, "explicit capabilities"):
            build_execution_policy(
                execution_surface=HOST_EXECUTOR,
                operation_classes=[READ_ONLY_HOST],
            )

    def test_host_executor_requires_explicit_operation_class(self):
        with self.assertRaisesRegex(ExecutionPolicyError, "operation classes"):
            build_execution_policy(
                execution_surface=HOST_EXECUTOR,
                required_capabilities=["HOST_FILESYSTEM"],
            )

    def test_business_action_is_never_implicit(self):
        with self.assertRaisesRegex(ExecutionPolicyError, "BUSINESS_ACTION"):
            build_execution_policy(
                execution_surface=HOST_EXECUTOR,
                required_capabilities=["LOCAL_HOST_PROCESS"],
                operation_classes=[BUSINESS_ACTION],
            )

    def test_production_mutation_requires_explicit_intent(self):
        with self.assertRaisesRegex(ExecutionPolicyError, "production_mutation_intent"):
            build_execution_policy(
                execution_surface=HOST_EXECUTOR,
                required_capabilities=["SYSTEMD_USER"],
                operation_classes=[PRODUCTION_MUTATION],
            )

    def test_production_read_only_is_explicit_and_does_not_imply_mutation(self):
        policy = host_policy(
            capabilities=("SSH",), classes=(PRODUCTION_READ_ONLY,)
        )
        self.assertTrue(policy.permits(PRODUCTION_READ_ONLY, "SSH"))
        self.assertFalse(policy.permits(PRODUCTION_MUTATION, "SSH"))
        self.assertFalse(policy.production_mutation_intent)

    def test_policy_round_trip_is_canonical(self):
        policy = host_policy()
        self.assertEqual(parse_execution_policy(policy.to_json()), policy)
        self.assertFalse(policy.as_dict()["host_executor_default"])
        self.assertFalse(policy.as_dict()["business_action_authority"])

    def test_host_surface_reuses_canonical_route_model(self):
        policy = host_policy()
        route = host_route(Path("/tmp"), policy)
        policy.validate_route(route)
        self.assertEqual(normalize_surface("HOST_EXECUTOR").stable_identifier, "host_executor")
        self.assertEqual(route.provider.stable_identifier, "codex_app_server")

    def test_host_surface_rejects_remote_provider_transport(self):
        with self.assertRaisesRegex(SemanticsError, "local provider transport"):
            build_routing_identity(
                host="p620", surface="host_executor", provider="codex_app_server",
                transport="ssh_stdio", workspace_alias="p620", project_alias="pilot",
                worktree_key="w", project_identity="pilot",
            )


class PreparationPolicyTests(unittest.TestCase):
    @staticmethod
    def fixture(root: Path, policy, capability_status):
        registry = TaskRegistry(root / "tasks.sqlite3")
        route = host_route(root, policy)
        task = registry.create_task(
            host="p620", workspace_alias="p620", project_alias="pilot",
            project_name="Pilot", cwd=str(root), repository_origin=None,
            branch="main", title="M13-B preparation",
            summary="Bounded host executor preparation",
            routing_identity=route,
            execution_policy=policy,
        )
        registry.bind_conversation(
            task_id=task.task_id, thread_id="thread-m13b", session_id="session-m13b",
            project_id=None, app_server_version="0.153.4",
        )

        class Context:
            def resolve_task(self, **_kwargs):
                return task

        class Executor:
            def capabilities(self):
                return {"available": True, "capabilities": capability_status}

        class Dispatcher:
            host_executor = Executor()

            def resolve_project(self, _project_ref, *, host, project_mode):
                return (
                    SimpleNamespace(alias="p620", host=host),
                    SimpleNamespace(alias="pilot", cwd=root),
                    None,
                )

        cfg = SimpleNamespace(
            team_id="team", trigger_label="local-codex", todo_state="Todo", projects=()
        )
        return ClinxIntegration(cfg, registry, Dispatcher(), Context(), None), registry, task

    def test_unsupported_capability_preparation_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            policy = host_policy(capabilities=("DOCKER",))
            integration, _registry, task = self.fixture(
                root, policy, {"docker": "UNAVAILABLE"}
            )
            with self.assertRaisesRegex(M9IntegrationError, "CAPABILITY_UNAVAILABLE: DOCKER"):
                integration.prepare_execution(
                    prompt="inspect Docker", approved=True, task_ref=task.task_id
                )

    def test_explicit_production_read_only_preparation_is_sealed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            policy = host_policy(
                capabilities=("SSH",), classes=(PRODUCTION_READ_ONLY,)
            )
            integration, registry, task = self.fixture(
                root, policy, {"ssh": "AVAILABLE"}
            )
            result = integration.prepare_execution(
                prompt="read production hostname", approved=True, task_ref=task.task_id
            )
            prepared = registry.verify_prepared_execution(
                result["prepared_execution_ref"]
            )
            self.assertEqual(result["execution_policy"], policy.as_dict())
            self.assertEqual(
                parse_execution_policy(prepared.execution_policy_json), policy
            )

    def test_direct_preparation_rejects_non_boolean_production_intent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            policy = host_policy()
            integration, _registry, _task = self.fixture(
                root, policy, {"systemd_user": "AVAILABLE"}
            )
            with self.assertRaisesRegex(M9IntegrationError, "must be a boolean"):
                integration.prepare_execution(
                    prompt="restart service", approved=True, task_action="create",
                    host="p620", project="pilot", title="invalid intent",
                    execution_surface=HOST_EXECUTOR,
                    required_capabilities=["SYSTEMD_USER"],
                    operation_classes=[PRODUCTION_MUTATION],
                    production_mutation_intent="false",
                )


class HostExecutorFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "pilot"
        self.root.mkdir()
        self.registry = TaskRegistry(Path(self.temp.name) / "tasks.sqlite3")
        self.config = HostExecutorConfig(
            enabled=True,
            host="p620",
            default_timeout_seconds=1,
            max_timeout_seconds=2,
            max_output_bytes=64,
            services=(RegisteredTarget("clinx.service", (READ_ONLY_HOST, DEVELOPMENT_MUTATION)),),
            ssh_targets=(RegisteredTarget("orion-core", ("PRODUCTION_READ_ONLY",)),),
            network_targets=(
                RegisteredTarget(
                    "public_https", (READ_ONLY_HOST,),
                    dns_name="example.com", url="https://example.com",
                ),
            ),
        )
        self.executor = HostExecutor(self.config, self.registry)

    def tearDown(self):
        self.temp.cleanup()

    def bound(self, *, capabilities=("LOCAL_HOST_PROCESS",), classes=(READ_ONLY_HOST,)):
        policy = host_policy(capabilities=capabilities, classes=classes)
        route = host_route(self.root, policy)
        task = self.registry.create_task(
            host="p620", workspace_alias="p620", project_alias="pilot",
            project_name="Pilot", cwd=str(self.root), repository_origin=None,
            branch="main", title="M13-B test", routing_identity=route,
            execution_policy=policy,
        )
        context = self.registry.execution(
            task.task_id, execution_ref="exec_" + task.task_id.removeprefix("task_"),
            retain=True,
        )
        context.__enter__()
        execution_ref = "exec_" + task.task_id.removeprefix("task_")
        return task, execution_ref, route, policy, context

    def request(self, task, execution_ref, route, policy, capability, operation,
                arguments=None, operation_class=READ_ONLY_HOST, timeout=None):
        return HostExecutionRequest(
            task_ref=task.task_id, execution_ref=execution_ref,
            route=route, policy=policy, operation_class=operation_class,
            capability=capability, operation=operation, arguments=arguments or {},
            project_root=self.root, timeout_seconds=timeout,
        )

    def test_stdout_exit_code_and_evidence_are_persisted(self):
        task, ref, route, policy, context = self.bound()
        try:
            result = self.executor.execute(
                self.request(task, ref, route, policy, "LOCAL_HOST_PROCESS", "host_identity")
            )
            self.assertEqual(result["result_state"], "SUCCEEDED")
            self.assertEqual(result["exit_code"], 0)
            self.assertTrue(result["stdout"].strip())
            stored = self.registry.list_host_executions(execution_ref=ref)
            self.assertEqual(stored[0]["host_execution_ref"], result["host_execution_ref"])
            self.assertEqual(stored[0]["stdout_sha256"], result["stdout_sha256"])
        finally:
            context.__exit__(None, None, None)

    def test_stderr_and_command_failure_are_separate(self):
        task, ref, route, policy, context = self.bound(
            capabilities=("HOST_FILESYSTEM",)
        )
        try:
            result = self.executor.execute(
                self.request(task, ref, route, policy, "HOST_FILESYSTEM", "git_head")
            )
            self.assertEqual(result["result_state"], "COMMAND_FAILED")
            self.assertNotEqual(result["exit_code"], 0)
            self.assertEqual(result["stdout"], "")
            self.assertGreater(result["stderr_bytes"], 0)
        finally:
            context.__exit__(None, None, None)

    def test_subprocess_uses_argv_and_shell_false(self):
        task, ref, route, policy, context = self.bound()
        real_popen = __import__("subprocess").Popen
        observed = {}

        def capture(*args, **kwargs):
            observed.update(kwargs)
            return real_popen(*args, **kwargs)

        try:
            with mock.patch("host_executor.subprocess.Popen", side_effect=capture):
                self.executor.execute(
                    self.request(task, ref, route, policy, "LOCAL_HOST_PROCESS", "user_identity")
                )
            self.assertIs(observed["shell"], False)
            self.assertIsInstance(observed["cwd"], str)
        finally:
            context.__exit__(None, None, None)

    def test_executor_path_is_bounded_and_includes_user_cli_directory(self):
        with mock.patch.dict(
            os.environ,
            {"PATH": "/tmp/untrusted", "AWS_SECRET_ACCESS_KEY": "do-not-copy"},
        ):
            environment = self.executor._clean_environment()
        self.assertEqual(
            environment["PATH"].split(os.pathsep)[0],
            str(Path.home() / ".local" / "bin"),
        )
        self.assertNotIn("/tmp/untrusted", environment["PATH"])
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", environment)

    def test_timeout_terminates_child_and_persists_terminal_state(self):
        task, ref, route, policy, context = self.bound()
        try:
            with mock.patch.object(self.executor, "_command", return_value=_Command(("sleep", "2"))):
                result = self.executor.execute(
                    self.request(
                        task, ref, route, policy, "LOCAL_HOST_PROCESS", "host_identity",
                        timeout=0.05,
                    )
                )
            self.assertEqual(result["result_state"], "TIMEOUT")
            self.assertTrue(result["timed_out"])
            self.assertIsNotNone(result["completed_at"])
        finally:
            context.__exit__(None, None, None)

    def test_cancellation_terminates_only_bound_execution(self):
        task, ref, route, policy, context = self.bound()
        result = {}

        def run():
            with mock.patch.object(self.executor, "_command", return_value=_Command(("sleep", "5"))):
                result.update(self.executor.execute(
                    self.request(task, ref, route, policy, "LOCAL_HOST_PROCESS", "uptime")
                ))

        thread = threading.Thread(target=run)
        try:
            thread.start()
            deadline = time.monotonic() + 2
            while not self.executor._active and time.monotonic() < deadline:
                time.sleep(0.01)
            first = self.executor.cancel_execution(ref)
            thread.join(3)
            second = self.executor.cancel_execution(ref)
            self.assertFalse(thread.is_alive())
            self.assertEqual(first, 1)
            self.assertEqual(second, 0)
            self.assertEqual(result["result_state"], "CANCELLED")
            self.assertTrue(result["cancel_requested"])
        finally:
            context.__exit__(None, None, None)

    def test_output_is_truncated_with_full_size_and_hash(self):
        task, ref, route, policy, context = self.bound()
        try:
            command = _Command(("python3", "-c", "print('x' * 1024)"))
            with mock.patch.object(self.executor, "_command", return_value=command):
                result = self.executor.execute(
                    self.request(task, ref, route, policy, "LOCAL_HOST_PROCESS", "host_identity")
                )
            self.assertTrue(result["stdout_truncated"])
            self.assertGreater(result["stdout_bytes"], len(result["stdout"]))
            self.assertEqual(len(result["stdout_sha256"]), 64)
        finally:
            context.__exit__(None, None, None)

    def test_basic_secret_redaction(self):
        task, ref, route, policy, context = self.bound()
        try:
            command = _Command(("python3", "-c", "print('api_key=supersecretvalue')"))
            with mock.patch.object(self.executor, "_command", return_value=command):
                result = self.executor.execute(
                    self.request(task, ref, route, policy, "LOCAL_HOST_PROCESS", "host_identity")
                )
            self.assertNotIn("supersecretvalue", result["stdout"])
            self.assertIn("[REDACTED]", result["stdout"])
        finally:
            context.__exit__(None, None, None)

    def test_arbitrary_absolute_cwd_target_is_rejected(self):
        task, ref, route, policy, context = self.bound(
            capabilities=("HOST_FILESYSTEM",)
        )
        try:
            with self.assertRaisesRegex(TargetNotRegistered, "absolute"):
                self.executor.execute(self.request(
                    task, ref, route, policy, "HOST_FILESYSTEM", "path_read",
                    {"path": "/etc/passwd"},
                ))
        finally:
            context.__exit__(None, None, None)

    def test_executor_rejects_project_root_mismatch(self):
        task, ref, route, policy, context = self.bound()
        other_root = Path(self.temp.name) / "other"
        other_root.mkdir()
        try:
            request = self.request(
                task, ref, route, policy, "LOCAL_HOST_PROCESS", "working_directory"
            )
            request = HostExecutionRequest(
                task_ref=request.task_ref,
                execution_ref=request.execution_ref,
                route=request.route,
                policy=request.policy,
                operation_class=request.operation_class,
                capability=request.capability,
                operation=request.operation,
                arguments=request.arguments,
                project_root=other_root,
            )
            with self.assertRaisesRegex(TargetNotRegistered, "cwd does not match"):
                self.executor.execute(request)
        finally:
            context.__exit__(None, None, None)

    def test_unknown_ssh_target_fails_closed(self):
        task, ref, route, policy, context = self.bound(
            capabilities=("SSH",), classes=("PRODUCTION_READ_ONLY",)
        )
        try:
            with self.assertRaisesRegex(TargetNotRegistered, "not registered"):
                self.executor.execute(self.request(
                    task, ref, route, policy, "SSH", "remote_true",
                    {"target": "arbitrary.example"}, operation_class="PRODUCTION_READ_ONLY",
                ))
        finally:
            context.__exit__(None, None, None)

    def test_ssh_option_terminator_precedes_registered_target(self):
        task, ref, route, policy, context = self.bound(
            capabilities=("SSH",), classes=(PRODUCTION_READ_ONLY,)
        )
        try:
            request = self.request(
                task, ref, route, policy, "SSH", "remote_true",
                {"target": "orion-core"}, operation_class=PRODUCTION_READ_ONLY,
            )
            argv = self.executor._command(request).argv
            self.assertEqual(argv[-3:], ("--", "orion-core", "true"))
        finally:
            context.__exit__(None, None, None)

    def test_authority_denied_for_unsealed_capability(self):
        task, ref, route, policy, context = self.bound()
        try:
            with self.assertRaisesRegex(AuthorityDenied, "sealed task authority"):
                self.executor.execute(self.request(
                    task, ref, route, policy, "SYSTEMD_USER", "service_is_active",
                    {"target": "clinx.service"},
                ))
        finally:
            context.__exit__(None, None, None)

    def test_mutation_requires_same_worktree_lease(self):
        task, ref, route, policy, context = self.bound(
            capabilities=("HOST_FILESYSTEM",), classes=(DEVELOPMENT_MUTATION,)
        )
        try:
            with self.registry._connect() as conn:
                conn.execute("DELETE FROM worktree_leases WHERE execution_ref=?", (ref,))
            with self.assertRaisesRegex(TaskRegistryError, "canonical worktree lease"):
                self.executor.execute(self.request(
                    task, ref, route, policy, "HOST_FILESYSTEM", "marker_create",
                    {"name": ".clinx-host-executor-test"},
                    operation_class=DEVELOPMENT_MUTATION,
                ))
        finally:
            context.__exit__(None, None, None)

    def test_active_host_execution_blocks_second_worktree_owner(self):
        task, ref, route, policy, context = self.bound()
        try:
            other = self.registry.create_task(
                host="p620", workspace_alias="p620", project_alias="pilot",
                project_name="Pilot", cwd=str(self.root), repository_origin=None,
                branch="main", title="Other", routing_identity=route,
                execution_policy=policy,
            )
            with self.assertRaises(WorktreeExecutionBusy):
                with self.registry.execution(other.task_id, execution_ref="exec_other"):
                    pass
        finally:
            context.__exit__(None, None, None)

    def test_stale_running_operation_reconciles_terminal(self):
        task, ref, route, policy, context = self.bound()
        try:
            self.registry.begin_host_execution(
                host_execution_ref="hostexec_stale", task_id=task.task_id,
                execution_ref=ref, routing_identity_json=route.to_json(),
                execution_policy_json=policy.to_json(), host="p620",
                surface="host_executor", operation_class=READ_ONLY_HOST,
                capability="LOCAL_HOST_PROCESS", operation="host_identity",
                argv_json='["hostname"]', cwd_identity="pilot",
                started_at="2026-01-01T00:00:00+00:00", result_state="RUNNING",
                timeout_seconds=1, executor_instance="dead-owner",
            )
            HostExecutor(self.config, self.registry)
            evidence = self.registry.list_host_executions(execution_ref=ref)
            stale = next(item for item in evidence if item["host_execution_ref"] == "hostexec_stale")
            self.assertEqual(stale["result_state"], "TRANSPORT_FAILED")
        finally:
            context.__exit__(None, None, None)

    def test_status_exposes_host_execution_evidence(self):
        task, ref, route, policy, context = self.bound()
        try:
            result = self.executor.execute(
                self.request(task, ref, route, policy, "LOCAL_HOST_PROCESS", "host_identity")
            )
            integration = ClinxIntegration(
                SimpleNamespace(team_id="", projects=()), self.registry,
                SimpleNamespace(host_executor=self.executor), None, None,
            )
            status = integration.get_status(execution_ref=ref)
            self.assertEqual(
                status["host_executions"][0]["host_execution_ref"],
                result["host_execution_ref"],
            )
            self.assertEqual(status["execution_policy"], policy.as_dict())
        finally:
            context.__exit__(None, None, None)

    def test_terminal_parent_execution_releases_worktree_lease(self):
        task, ref, route, policy, context = self.bound()
        try:
            self.executor.execute(
                self.request(task, ref, route, policy, "LOCAL_HOST_PROCESS", "host_identity")
            )
            self.registry.reconcile_terminal(ref, "COMPLETED")
            other = self.registry.create_task(
                host="p620", workspace_alias="p620", project_alias="pilot",
                project_name="Pilot", cwd=str(self.root), repository_origin=None,
                branch="main", title="After terminal", routing_identity=route,
                execution_policy=policy,
            )
            with self.registry.execution(other.task_id, execution_ref="exec_after_terminal"):
                pass
        finally:
            context.__exit__(None, None, None)

    def test_prepared_integrity_seals_execution_policy(self):
        policy = host_policy()
        route = host_route(self.root, policy, conversation="UNBOUND")
        prepared = self.registry.create_prepared_execution(
            task_action="create", task_ref=None, host="p620", project="pilot",
            title="sealed", summary=None, prompt="test", model="gpt-test",
            reasoning_effort="low", execution_mode="normal",
            routing_identity=route, execution_policy=policy,
        )
        with self.registry._connect() as conn:
            conn.execute(
                "UPDATE prepared_executions SET execution_policy_json='{}' "
                "WHERE prepared_execution_ref=?", (prepared.prepared_execution_ref,),
            )
        with self.assertRaisesRegex(TaskRegistryError, "INTEGRITY=FAIL"):
            self.registry.verify_prepared_execution(prepared.prepared_execution_ref)


class DynamicToolProtocolTests(unittest.TestCase):
    class Transport:
        def __init__(self):
            self.sent = []

        def send(self, value):
            self.sent.append(value)

        def receive(self, _timeout):
            raise app_server.AppServerTransportError("closed")

        def close(self):
            pass

    def test_dynamic_tool_request_returns_structured_content(self):
        transport = self.Transport()
        client = app_server.CodexAppServerClient(transport)
        client.configure_dynamic_tool(
            name="clinx_host_operation", thread_id="thread",
            handler=lambda params: {"result_state": "SUCCEEDED", "call": params["callId"]},
        )
        client._dynamic_turn_id = "turn"
        client._send_server_response({
            "id": 1, "method": "item/tool/call",
            "params": {
                "threadId": "thread", "turnId": "turn", "callId": "call",
                "tool": "clinx_host_operation", "namespace": None, "arguments": {},
            },
        })
        response = transport.sent[-1]["result"]
        self.assertTrue(response["success"])
        self.assertEqual(
            json.loads(response["contentItems"][0]["text"])["result_state"],
            "SUCCEEDED",
        )

    def test_dynamic_tool_identity_mismatch_fails_closed(self):
        transport = self.Transport()
        client = app_server.CodexAppServerClient(transport)
        client.configure_dynamic_tool(
            name="clinx_host_operation", thread_id="expected", handler=lambda _params: {},
        )
        client._send_server_response({
            "id": 2, "method": "item/tool/call",
            "params": {
                "threadId": "other", "turnId": "turn", "callId": "call",
                "tool": "clinx_host_operation", "arguments": {},
            },
        })
        self.assertFalse(transport.sent[-1]["result"]["success"])

    def test_thread_start_accepts_dynamic_tool_spec(self):
        class StartTransport(self.Transport):
            def receive(self, _timeout):
                request = self.sent[-1]
                return {
                    "id": request["id"],
                    "result": {"thread": {"id": "thread", "sessionId": "session"}},
                }

        transport = StartTransport()
        client = app_server.CodexAppServerClient(transport)
        spec = HostExecutor.dynamic_tool_spec()
        client.thread_start(cwd="/tmp", dynamic_tools=[spec])
        self.assertEqual(transport.sent[0]["params"]["dynamicTools"], [spec])

    def test_public_catalog_remains_nine_without_shell_tools(self):
        names = tuple(item["name"] for item in tool_definitions())
        self.assertEqual(names, DEFAULT_TOOL_NAMES)
        self.assertEqual(len(names), 9)
        self.assertFalse(any("shell" in name or "host_exec" in name or "ssh" in name for name in names))

    def test_dynamic_tool_schema_has_no_raw_identity_or_shell_fields(self):
        properties = HostExecutor.dynamic_tool_spec()["inputSchema"]["properties"]
        for forbidden in ("task_ref", "execution_ref", "cwd", "pid", "thread_id", "argv", "command"):
            self.assertNotIn(forbidden, properties)


if __name__ == "__main__":
    unittest.main()
