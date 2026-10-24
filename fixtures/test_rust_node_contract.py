"""Node selector and cancellation receipts against the Rust adapter boundary."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from clinx.nodes.node_protocol import NodeProtocolError
from clinx.nodes.rust_node_adapter import RustNodeExecutionAdapter


@pytest.fixture
def node():
    task = SimpleNamespace(host="air", project_alias="project", thread_id="current-thread")
    receipt = dict(execution_ref="exec_exact", task_ref="task_exact", thread_id="historic-thread",
                   native_turn_id="exact-turn", status="RUNNING", resource_state="HELD")
    integration = SimpleNamespace(
        registry=SimpleNamespace(get_task=Mock(return_value=task)),
        get_status=Mock(return_value=receipt),
        cancel_execution=Mock(return_value=dict(receipt, status="CANCELLED", resource_state="RELEASED")),
    )
    return RustNodeExecutionAdapter(integration, "air", allowed_projects=["project"])


@pytest.mark.parametrize("selector", [
    {"host": "another-node"},
    {"project": "another-project"},
    {"thread_id": "another-thread"},
    {"native_turn_id": "another-turn"},
])
def test_cancel_rejects_conflicting_selector_before_mutation(node, selector):
    with pytest.raises(NodeProtocolError):
        node.cancel(execution_ref="exec_exact", approved=True, **selector)
    node.integration.cancel_execution.assert_not_called()


def test_historical_execution_keeps_its_exact_thread_after_successor(node):
    result = node.status(execution_ref="exec_exact", thread_id="historic-thread",
                         native_turn_id="exact-turn", host="air")
    assert result["thread_id"] == "historic-thread"
    assert result["native_turn_id"] == "exact-turn"
    assert result["task_ref"] == "task_exact"


@pytest.mark.parametrize("status,resource,expected", [
    ("CANCELLED", "RELEASED", "CANCELLED"),
    ("CANCELLED", "HELD", "CANCELLATION_PENDING"),
    ("RUNNING", "HELD", "CANCELLATION_PENDING"),
])
def test_cancel_reports_only_the_authoritative_terminal_receipt(node, status, resource, expected):
    node.integration.cancel_execution.return_value.update(status=status, resource_state=resource)
    result = node.cancel(execution_ref="exec_exact", approved=True)
    assert result["operation_state"] == expected
    assert result["status"] == status and result["resource_state"] == resource
    node.integration.cancel_execution.assert_called_once_with(execution_ref="exec_exact", approved=True)


def test_helper_close_has_no_authority_or_provider_lifecycle_side_effect(node):
    node.close()
    node.integration.get_status.assert_not_called()
    node.integration.cancel_execution.assert_not_called()


def test_mcp_factory_never_falls_back_to_a_python_registry(monkeypatch):
    import builtins
    from clinx.mcp import mcp_server
    from clinx.authority import rust_integration

    cfg = SimpleNamespace(runtime_host="air")
    monkeypatch.setattr(mcp_server.BridgeConfig, "load", lambda _: cfg)
    original_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert name not in {"bridge", "task_registry", "m9_integration", "execution_owner"}
        assert not name.startswith('clinx.compat')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    monkeypatch.setenv("CLINX_AUTHORITY", "python")
    with pytest.raises(ValueError, match="RUST_AUTHORITY_REQUIRED"):
        mcp_server.build_server("unused.toml")
    with pytest.raises(ValueError, match="PYTHON_OWNER_RETIRED"):
        mcp_server.build_server("unused.toml", own_executions=True)
    monkeypatch.setenv("CLINX_AUTHORITY", "rust")
    monkeypatch.delenv("CLINX_RUST_OBSERVATION_READS", raising=False)
    api = SimpleNamespace(cfg=cfg, rust_authority=True, execute=Mock())
    monkeypatch.setattr(rust_integration, "RustIntegration", lambda loaded: api)
    assert mcp_server.build_server("unused.toml").integration is api
    api.execute.assert_not_called()


@pytest.fixture
def canonical_node():
    import json
    import uuid
    from pathlib import Path

    thread = '11111111-2222-3333-4444-555555555555'
    identity = dict(node_id='air', host='air', provider='codex_app_server', thread_id=thread,
        user_scope='fixture-user', origin='CODEX_NATIVE', title='Native fixture', cwd='/fixture/project',
        task_id='task_' + uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(
            ['air', 'codex_app_server', thread], separators=(',', ':'))).hex,
        first_provenance_json=json.dumps(dict(node='air', node_fingerprint='paired-fingerprint',
            observation_id='fixture-observation', digest='fixture-digest')))
    integration = SimpleNamespace(
        cfg=SimpleNamespace(projects=[SimpleNamespace(project_alias='project', repo=Path('/fixture/project'))]),
        adopt_conversation=Mock(return_value=dict(adoption_status='PENDING', task_ref=identity['task_id'])),
        prepare_execution=Mock(return_value=dict(prepared_execution_ref='prepared_fixture')),
        registry=SimpleNamespace(get_task=Mock(return_value=SimpleNamespace(
            host='air', project_alias='project', thread_id=thread))),
    )
    request = dict(canonical_identity=identity, exact_turn_id='exact-predecessor',
                   _canonical_owner=dict(owner='air', user='fixture-user', fingerprint='paired-fingerprint'))
    return RustNodeExecutionAdapter(integration, 'air', allowed_projects=['project'], allowed_threads=[thread]), request


def test_canonical_adoption_forwards_identity_but_cannot_commit_provider_evidence(canonical_node):
    node, request = canonical_node
    result = node.adopt(**request)
    assert result['adoption_status'] == 'PENDING'
    node.integration.adopt_conversation.assert_called_once_with(host='air', project='project',
        thread_id=request['canonical_identity']['thread_id'], title='Native fixture',
        exact_turn_id='exact-predecessor', canonical_identity=request['canonical_identity'])
    node.integration.registry.get_task.assert_not_called()
    node.integration.prepare_execution.assert_not_called()


@pytest.mark.parametrize('replacement', [
    {'_canonical_owner': None},
    {'_canonical_owner': dict(owner='air', user='other', fingerprint='paired-fingerprint')},
    {'_canonical_owner': dict(owner='air', user='fixture-user', fingerprint='forged')},
    {'host': 'imac'}, {'thread_id': 'other-thread'}, {'task_ref': 'task_other'},
    {'project': 'other'}, {'exact_turn_id': None}, {'native_turn_id': 'conflicting-turn'},
])
def test_canonical_adoption_rejects_scope_or_identity_before_rust_request(canonical_node, replacement):
    node, request = canonical_node
    with pytest.raises(NodeProtocolError):
        node.adopt(**dict(request, **replacement))
    node.integration.adopt_conversation.assert_not_called()


def test_pending_canonical_adoption_cannot_prepare_an_execution(canonical_node):
    node, request = canonical_node
    with pytest.raises(NodeProtocolError, match='Transport') as error:
        node.prepare(**request)
    assert error.value.code == 'CANONICAL_ADOPTION_PENDING'
    node.integration.prepare_execution.assert_not_called()


def test_committed_canonical_adoption_prepares_the_same_rust_task(canonical_node):
    node, request = canonical_node
    node.integration.adopt_conversation.return_value['adoption_status'] = 'ADOPTED'
    received = {}

    def prepare_execution(*, task_ref, host, prompt):
        received.update(task_ref=task_ref, host=host, prompt=prompt)
        return dict(prepared_execution_ref='prepared_fixture')

    node.integration.prepare_execution = prepare_execution
    assert node.prepare(**request, prompt='Exact input')['prepared_execution_ref'] == 'prepared_fixture'
    assert received == dict(task_ref=request['canonical_identity']['task_id'], host='air', prompt='Exact input')


def test_canonical_adoption_readback_needs_no_api_side_provider_access(canonical_node, monkeypatch):
    from clinx.authority.rust_integration import RustIntegration

    _, request = canonical_node
    identity = request['canonical_identity']
    commands = SimpleNamespace(call=Mock(return_value=dict(adoption_status='ADOPTED', task_ref=identity['task_id'])))
    integration = RustIntegration(SimpleNamespace(runtime_host='air'), commands)
    monkeypatch.setattr(integration, '_native_cwd', Mock(side_effect=AssertionError('Provider unavailable')))
    result = integration.adopt_conversation(host='air', project='project', thread_id=identity['thread_id'],
        exact_turn_id='exact-predecessor', canonical_identity=identity)
    assert result['task_ref'] == identity['task_id']
    integration._native_cwd.assert_not_called()
    payload = commands.call.call_args.args[1]
    assert payload['workspace_contract'] == {'cwd': identity['cwd']}
    assert payload['canonical_identity'] == identity


def test_rust_start_uses_authority_without_a_second_node_writer_fence():
    from clinx.nodes.node_protocol import NodeRecord, NodeRegistry, NodeService, SharingScope

    prepared = dict(host='air', project='project', new_task=True, execution_ref=None,
                    task_ref='task_exact')
    integration = SimpleNamespace(
        commands=SimpleNamespace(call=Mock(return_value=prepared)),
        start_execution=Mock(return_value=dict(execution_ref='exec_exact', status='ADMITTED')),
    )
    adapter = RustNodeExecutionAdapter(integration, 'air')
    registry = Mock(spec=NodeRegistry)
    registry.claim_request.return_value = ('NEW', None)
    service = NodeService(NodeRecord(node_id='air'), registry,
        scope=SharingScope(user_scope='fixture', execute_tasks=True), read_thread=Mock(),
        start_execution=adapter.start)
    receipt = service.start(dict(request_id='start-exact', execution_ref='exec_exact',
        prepared_execution_ref='prepared_exact', thread_id='exact-thread', approved=True))
    assert receipt['status'] == 'ADMITTED'
    integration.start_execution.assert_called_once()
    registry.acquire_fence.assert_not_called()
    registry.finish_request.assert_called_once()
