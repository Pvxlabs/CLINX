"""Local routing regressions: temporary registries and a fake Codex provider only."""

import dataclasses
from unittest import mock

import pytest

import bridge
from execution_policy import (
    DEVELOPMENT_CAPABILITIES,
    DEVELOPMENT_MUTATION,
    HOST_EXECUTOR,
    NETWORKED_SANDBOX,
    PRODUCTION_MUTATION,
    SANDBOX_WORKSPACE,
    ExecutionPolicyError,
    build_development_policy,
    build_execution_policy,
    parse_execution_policy,
)
from m9_integration import ClinxIntegration, M9IntegrationError
from test_m6 import FakeClient, dispatcher_fixture


class Provider(FakeClient):
    def configure_dynamic_tool(self, **kwargs):
        self.calls.append(("configure_dynamic_tool", kwargs))

    def thread_resume(self, thread_id, **kwargs):
        self.calls.append(("thread/resume", kwargs))
        return self.thread_read(thread_id)

    def supervise_turn(self, *args):
        self.calls.append(("supervise_turn", args))


@pytest.fixture
def local(tmp_path):
    provider = Provider()
    dispatcher, repo = dispatcher_fixture(tmp_path, tmp_path / "tasks.sqlite3", [provider])
    dispatcher.client_factory = lambda _target: provider
    dispatcher.cfg = dataclasses.replace(
        dispatcher.cfg, host_executor=dataclasses.replace(dispatcher.cfg.host_executor, enabled=True)
    )
    # Discovery is configuration evidence only; never probe or execute real Host work.
    dispatcher.host_executor = mock.Mock()
    dispatcher.host_executor.capabilities.return_value = {
        "available": True, "default": False, "capabilities": {}
    }
    from host_executor import HostExecutor
    dispatcher.host_executor.dynamic_tool_spec.side_effect = HostExecutor.dynamic_tool_spec
    integration = ClinxIntegration(
        dispatcher.cfg, dispatcher.tasks, dispatcher,
        bridge.TaskContextReader(dispatcher.cfg, dispatcher.tasks), None,
    )
    yield integration, dispatcher, provider, repo
    dispatcher.stop_completion_runtime()


def prepare(integration, **kwargs):
    return integration.prepare_execution(
        approved=True, task_action="create", host="p620", project="pilot",
        title="Native development", summary="Local routing regression",
        prompt="Run project checks", **kwargs,
    )


@pytest.mark.parametrize("network", [False, True])
def test_development_builder_is_native(network):
    expected = NETWORKED_SANDBOX if network else SANDBOX_WORKSPACE
    for policy in (
        build_development_policy(network_access=network),
        build_execution_policy(development_workspace=True, network_access=network),
    ):
        assert policy.execution_surface == expected
        assert policy.required_capabilities == policy.operation_classes == ()
        assert policy.authority_scopes == ("workspace_write",)
        assert not policy.production_mutation_intent
        assert not policy.as_dict()["host_executor_default"]


@pytest.mark.parametrize("network,explicit", [(False, False), (False, True), (True, False), (True, True)])
@pytest.mark.parametrize("host_enabled", [False, True])
def test_prepare_start_native_route_matches_discovery(local, network, explicit, host_enabled):
    integration, dispatcher, provider, repo = local
    dispatcher.cfg = dataclasses.replace(
        dispatcher.cfg, host_executor=dataclasses.replace(dispatcher.cfg.host_executor, enabled=host_enabled)
    )
    dispatcher.host_executor.capabilities.return_value["available"] = host_enabled
    expected = NETWORKED_SANDBOX if network else SANDBOX_WORKSPACE
    options = {"execution_surface": expected} if explicit else {}
    prepared = prepare(integration, network_access=network, **options)
    policy = prepared["execution_policy"]
    assert policy["execution_surface"] == expected
    assert policy["required_capabilities"] == policy["operation_classes"] == []
    dispatcher.host_executor.capabilities.assert_not_called()
    discovery = integration.get_capabilities()
    assert discovery["execution_surfaces"][SANDBOX_WORKSPACE]["default"]
    assert not discovery["host_executor_default"]
    assert not discovery["execution_surfaces"][HOST_EXECUTOR]["default"]
    assert discovery["execution_surfaces"][expected]["network_access"] == network
    result = integration.start_execution(
        prepared_execution_ref=prepared["prepared_execution_ref"], approved=True
    )
    task = dispatcher.tasks.get_task(result["task_ref"])
    assert parse_execution_policy(task.execution_policy_json).as_dict() == policy
    route = dispatcher.tasks.get_execution_routing_identity(result["execution_ref"])
    assert route.surface.stable_identifier == "codex_app_server"
    assert route.network_policy.network_access == network
    start = next(call[1] for call in provider.calls if call[0] == "thread/start")
    turn = next(call for call in provider.calls if call[0] == "turn/start")
    assert start["sandbox"] == "workspace-write"
    assert start["dynamic_tools"] is None
    assert turn[2].endswith("\n\nRun project checks")
    instructions = start["developer_instructions"]
    assert "MANAGED EXECUTION CONTRACT" not in turn[2]
    assert "Use Codex native workspace tools" in instructions
    assert "CLINX_EXECUTION_RESULT\nSTATUS=" in instructions
    assert "Do not call clinx_prepare_execution" in instructions
    assert "clinx_host_operation" not in instructions
    assert "Use only the Host capability" not in instructions
    assert turn[3]["approval_policy"] == "never"
    assert turn[3].get("network_access", False) == network
    if network:
        assert turn[3]["writable_roots"] == [str(repo)]
    assert not any(call[0] == "configure_dynamic_tool" for call in provider.calls)
    dispatcher.host_executor.execute.assert_not_called()


@pytest.mark.parametrize("network", [False, True])
def test_direct_dispatch_uses_same_native_default(local, network):
    _integration, dispatcher, provider, _repo = local
    result = dispatcher.dispatch(
        project_ref="pilot", host="p620", project_mode="existing", task_mode="new", task_id=None,
        prompt="Run project checks", title="Direct native", summary=None,
        model="model", reasoning_effort=None, network_access=network,
    )
    policy = parse_execution_policy(dispatcher.tasks.get_task(result.task_id).execution_policy_json)
    assert policy == build_development_policy(network_access=network)
    prompt = next(call[2] for call in provider.calls if call[0] == "turn/start")
    assert prompt == "Direct native\n\nRun project checks"
    assert "Use only the Host capability" not in prompt
    dispatcher.host_executor.dynamic_tool_spec.assert_not_called()


@pytest.mark.parametrize("archived", [False, True])
def test_explicit_host_and_sealed_continuation_remain_host(local, archived):
    integration, dispatcher, provider, _repo = local
    prepared = prepare(
        integration, execution_surface=HOST_EXECUTOR,
        required_capabilities=list(DEVELOPMENT_CAPABILITIES),
        operation_classes=[DEVELOPMENT_MUTATION],
    )
    first = integration.start_execution(
        prepared_execution_ref=prepared["prepared_execution_ref"], approved=True
    )
    task = dispatcher.tasks.get_task(first["task_ref"])
    original_policy = task.execution_policy_json
    dispatcher.tasks.reconcile_terminal(first["execution_ref"], "COMPLETED")
    if archived:
        dispatcher.tasks.set_status(task.task_id, "ARCHIVED")
    provider.calls.clear()
    continued = integration.prepare_execution(
        approved=True, task_ref=task.task_id, prompt="Continue original authorization"
    )
    assert continued["execution_policy"] == parse_execution_policy(original_policy).as_dict()
    second = integration.start_execution(
        prepared_execution_ref=continued["prepared_execution_ref"], approved=True
    )
    assert dispatcher.tasks.get_task(task.task_id).execution_policy_json == original_policy
    assert dispatcher.tasks.get_execution_routing_identity(second["execution_ref"]).surface.stable_identifier == "host_executor"
    assert any(call[0] == "configure_dynamic_tool" for call in provider.calls)
    assert "Use only the Host capability" in next(call[1]["developer_instructions"] for call in provider.calls if call[0] == "thread/resume")
    assert next(call[2] for call in provider.calls if call[0] == "turn/start") == task.title + "\n\nContinue original authorization"
    with pytest.raises(M9IntegrationError, match="cannot override the sealed execution policy"):
        integration.prepare_execution(
            approved=True, task_ref=task.task_id, prompt="Change route",
            execution_surface=SANDBOX_WORKSPACE, required_capabilities=[], operation_classes=[],
        )
    assert dispatcher.tasks.get_task(task.task_id).execution_policy_json == original_policy


def test_production_and_host_authority_are_not_native_development(local):
    integration, dispatcher, provider, _repo = local
    with pytest.raises(M9IntegrationError, match="production_mutation_intent"):
        prepare(integration, execution_surface=HOST_EXECUTOR,
                required_capabilities=["SYSTEMD_USER"], operation_classes=[PRODUCTION_MUTATION])
    with pytest.raises(M9IntegrationError, match="require HOST_EXECUTOR"):
        prepare(integration, execution_surface=SANDBOX_WORKSPACE,
                required_capabilities=["SYSTEMD_USER"], operation_classes=[PRODUCTION_MUTATION],
                production_mutation_intent=True)
    with pytest.raises(M9IntegrationError, match="approved=true"):
        integration.prepare_execution(prompt="Unapproved development")
    with pytest.raises(ExecutionPolicyError, match="explicit capabilities"):
        build_execution_policy(development_workspace=True, production_mutation_intent=True)
    assert not provider.calls
    dispatcher.host_executor.execute.assert_not_called()
