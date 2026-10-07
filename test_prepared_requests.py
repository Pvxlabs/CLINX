"""Preparation readback, scope coverage and uncertain dispatch regression."""
import jsonschema
import pytest

import app_server
from dispatch_errors import failure_details
from mcp_server import ClinxMCPServer, tool_definitions
from task_registry import TaskRegistryError
from test_execution_authority import authority as authority, prepare_change, apply_change, scope
from test_m12 import make_fixture


def call(integration, name, **arguments):
    return ClinxMCPServer(integration).handle({'id': 'review-test', 'method': 'tools/call',
        'params': {'name': name, 'arguments': arguments}})['result']


def snapshot(registry):
    with registry._connect() as c:
        return {name: [tuple(r) for r in c.execute('SELECT * FROM ' + name)]
                for name in ['tasks', 'executions', 'prepared_executions', 'policy_reauthorizations',
                             'task_policy_versions', 'worktree_leases']}


def test_preview_is_read_only_sealed_and_applied_response_loss(authority):
    integration, registry, task, _, _ = authority
    request = prepare_change(integration, task)
    before = snapshot(registry)
    preview = call(integration, 'clinx_get_prepared_request', request_ref=request['prepared_reauthorization_ref'])
    assert not preview['isError']
    p = preview['structuredContent']
    definition = next(t for t in tool_definitions() if t['name'] == 'clinx_get_prepared_request')
    jsonschema.validate(p, definition['outputSchema'])
    assert p['request_state'] == 'PREPARED' and p['read_only']
    assert p['policy_difference']['operation_scopes']['proposed'] == [scope()]
    assert snapshot(registry) == before
    integration.apply_policy_reauthorization(approved=True, prepared_reauthorization_ref=p['request_ref'],
        expected_request_hash=p['request_hash'], expected_task_ref=task.task_id)  # response discarded
    applied = integration.get_prepared_request(request_ref=p['request_ref'])
    assert applied['request_state'] == 'APPLIED' and applied['applied_policy_version'] == 1
    assert applied['request_hash'] == p['request_hash'] and not applied['execution_started']
    assert apply_change(integration, request)['idempotent']
    identity = integration.get_effective_authority(task_ref=task.task_id)
    assert identity['policy_version'] == applied['current_policy_version']
    assert identity['future_execution_policy'] == applied['proposed_policy']


@pytest.mark.parametrize('expected', [{'expected_request_hash': '0'*64}, {'expected_task_ref': 'wrong-task'}])
def test_wrong_review_does_not_apply_or_dispatch(authority, expected):
    integration, registry, task, _, _ = authority
    request = prepare_change(integration, task)
    before = snapshot(registry)
    result = call(integration, 'clinx_apply_policy_reauthorization', approved=True,
                  prepared_reauthorization_ref=request['prepared_reauthorization_ref'], **expected)
    assert result['isError']
    d = result['structuredContent']
    assert d['failure_stage'] == 'AUTHORITY_VALIDATION'
    assert d['side_effect_certainty'] == 'NOT_EXECUTED'
    assert snapshot(registry) == before


def test_expired_and_stale_readback_do_not_change_old_receipts(authority, monkeypatch):
    integration, registry, task, _, _ = authority
    first = prepare_change(integration, task)
    stale = prepare_change(integration, registry.get_task(first['task_ref']))
    apply_change(integration, first)
    assert integration.get_prepared_request(request_ref=stale['prepared_reauthorization_ref'])['request_state'] == 'INVALIDATED'
    with pytest.raises(TaskRegistryError, match='POLICY_IDENTITY_CONFLICT'):
        apply_change(integration, stale)
    expired = prepare_change(integration, registry.get_task(first['task_ref']))
    monkeypatch.setattr('task_registry._now', lambda: '2099-01-01T00:00:00+00:00')
    monkeypatch.setattr('prepared_requests._now', lambda: '2099-01-01T00:00:00+00:00')
    assert integration.get_prepared_request(request_ref=expired['prepared_reauthorization_ref'])['request_state'] == 'EXPIRED'
    error = call(integration, 'clinx_apply_policy_reauthorization', approved=True,
                 prepared_reauthorization_ref=expired['prepared_reauthorization_ref'])['structuredContent']
    assert error['failure_code'] == 'POLICY_REAUTHORIZATION_EXPIRED'
    assert integration.get_prepared_request(request_ref=first['prepared_reauthorization_ref'])['request_state'] == 'APPLIED'


def test_existing_scope_is_reused_and_missing_scope_is_exact(authority):
    integration, registry, task, _, _ = authority
    apply_change(integration, prepare_change(integration, task))
    registry.bind_conversation(task_id=task.task_id, thread_id='existing-thread', session_id='existing-session',
                               project_id=None, app_server_version='test')
    before_versions = snapshot(registry)['task_policy_versions']
    covered = integration.get_effective_authority(task_ref=task.task_id, requested_operations=[scope()])
    assert covered['requirements_covered'] and covered['missing_operations'] == []
    prepared = integration.prepare_execution(approved=True, task_ref=task.task_id,
                 prompt='Bounded fixture workflow', requested_operations=[scope()])
    assert prepared['execution_policy'] == covered['future_execution_policy']
    assert snapshot(registry)['task_policy_versions'] == before_versions
    missing = {**scope(), 'target': 'another-target'}
    result = call(integration, 'clinx_prepare_execution', approved=True, task_ref=task.task_id,
                  prompt='Fixture scope check', requested_operations=[scope(), missing])['structuredContent']
    assert result['missing_operations'] == [missing]
    assert result['failure_code'] == 'AUTHORITY_REAUTHORIZATION_REQUIRED'


def test_execution_preview_integrity_and_review_guard(tmp_path):
    integration, registry, dispatcher, task = make_fixture(tmp_path)
    prepared = integration.prepare_execution(approved=True, task_ref=task.task_id, prompt='Non-sensitive fixture')
    p = integration.get_prepared_request(request_ref=prepared['prepared_execution_ref'])
    jsonschema.validate(p, next(t['outputSchema'] for t in tool_definitions() if t['name'] == 'clinx_get_prepared_request'))
    assert p['request_state'] == 'PREPARED' and p['expires_at'] is None
    assert 'prompt' not in p and not p['execution_present'] and p['turn_id'] is None
    with pytest.raises(TaskRegistryError, match='CONTENT_MISMATCH'):
        integration.start_execution(approved=True, prepared_execution_ref=p['request_ref'], expected_request_hash='0'*64)
    assert not dispatcher.dispatch_calls
    with registry._connect() as c:
        c.execute("UPDATE prepared_executions SET prompt='tampered' WHERE prepared_execution_ref=?", (p['request_ref'],))
    with pytest.raises(TaskRegistryError, match='INTEGRITY=FAIL'):
        integration.get_prepared_request(request_ref=p['request_ref'])


def test_lost_provider_response_is_not_restored_or_replayed(tmp_path):
    integration, registry, dispatcher, task = make_fixture(tmp_path)
    prepared = integration.prepare_execution(approved=True, task_ref=task.task_id, prompt='Fixture')
    calls = []
    def lost(**kwargs):
        calls.append(kwargs)
        raise app_server.AppServerTransportError('turn/start response lost')
    dispatcher.dispatch = lost
    error = call(integration, 'clinx_start_execution', approved=True,
                 prepared_execution_ref=prepared['prepared_execution_ref'])['structuredContent']
    assert error['failure_source'] == 'PROVIDER_TRANSPORT' and error['outcome_certainty'] == 'UNKNOWN'
    assert error['codex_running'] is None and not error['retry_allowed']
    assert registry.get_prepared_execution(prepared['prepared_execution_ref']).status == 'RUNNING'
    with pytest.raises(TaskRegistryError, match='not dispatchable'):
        integration.start_execution(approved=True, prepared_execution_ref=prepared['prepared_execution_ref'])
    assert len(calls) == 1


def test_previous_completed_turn_is_not_new_execution_evidence(tmp_path):
    integration, registry, _, task = make_fixture(tmp_path)
    registry.set_execution_state(task.task_id, 'COMPLETED', current_stage='completed', turn_id='old-turn')
    prepared = integration.prepare_execution(approved=True, task_ref=task.task_id, prompt='New fixture request')
    p = integration.get_prepared_request(request_ref=prepared['prepared_execution_ref'])
    assert p['execution_state'] is None and p['turn_id'] is None and p['provider_running'] == 'NOT_OBSERVED'


def test_started_execution_survives_lost_response_with_exact_turn(tmp_path):
    integration, registry, dispatcher, task = make_fixture(tmp_path)
    registry.set_execution_state(task.task_id, 'COMPLETED', current_stage='completed', turn_id='old-turn')
    prepared = integration.prepare_execution(approved=True, task_ref=task.task_id, prompt='Fixture')
    ref = 'exec_' + prepared['prepared_execution_ref'].removeprefix('prepared_')
    contexts, calls = [], []
    def started_then_lost(**kwargs):
        calls.append(kwargs)
        ctx = registry.execution(task.task_id, execution_ref=ref)
        ctx.__enter__()
        contexts.append(ctx)
        registry.set_execution_state(task.task_id, 'CODEX_RUNNING', turn_id='new-turn', codex_running=True)
        raise app_server.AppServerTransportError('Provider accepted; response lost')
    dispatcher.dispatch = started_then_lost
    try:
        result = call(integration, 'clinx_start_execution', approved=True,
                      prepared_execution_ref=prepared['prepared_execution_ref'])['structuredContent']
        p = integration.get_prepared_request(request_ref=prepared['prepared_execution_ref'])
        assert result['outcome_certainty'] == 'UNKNOWN' and result['correlation']['execution_ref'] == ref
        assert p['execution_present'] and p['execution_ref'] == ref and p['turn_id'] == 'new-turn'
        with pytest.raises(TaskRegistryError, match='not dispatchable'):
            integration.start_execution(approved=True, prepared_execution_ref=p['request_ref'])
        assert len(calls) == 1
    finally:
        for ctx in contexts:
            ctx.__exit__(None, None, None)


def test_provider_rejection_and_uncertainty_are_distinct():
    rejection = failure_details('clinx_start_execution', {}, app_server.AppServerRemoteError('turn/start', {'code': -1, 'message': 'request refused'}))
    unknown = failure_details('clinx_start_execution', {}, app_server.AppServerTransportError('response lost'))
    assert rejection['failure_source'] == 'PROVIDER' and rejection['outcome_certainty'] == 'KNOWN'
    assert unknown['failure_source'] == 'PROVIDER_TRANSPORT' and unknown['outcome_certainty'] == 'UNKNOWN'


def test_bad_arguments_still_rejected_and_examples_match_schema(authority):
    integration, _, _, _, _ = authority
    extra = call(integration, 'clinx_get_status', task_ref='fixture', max_bytes=1000)['structuredContent']
    assert extra['failure_code'] == 'CALL_ARGUMENT_INVALID'
    bad_uri = call(integration, 'clinx_get_status', thread_id='codex://threads/01a11492-d1ae-7872-ab01-ca022e9cda3d')['structuredContent']
    assert bad_uri['error_code'] == 'INVALID_THREAD_ID'
    tools = {t['name']: t for t in tool_definitions()}
    jsonschema.validate({'codex_uri': 'codex://threads/01a11492-d1ae-7872-ab01-ca022e9cda3d?hostId=remote-ssh-discovered%3Ap620'}, tools['clinx_get_status']['inputSchema'])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({'task_ref': 'fixture', 'max_bytes': 1000}, tools['clinx_get_status']['inputSchema'])
    assert tools['clinx_get_prepared_request']['annotations']['readOnlyHint']
    assert tools['clinx_apply_policy_reauthorization']['annotations']['destructiveHint']
    assert 'expected_request_hash' in tools['clinx_start_execution']['inputSchema']['properties']
