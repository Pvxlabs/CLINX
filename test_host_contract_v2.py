"""Contract, continuation, real local Git and uncertainty boundary regressions."""
import dataclasses
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import app_server
import bridge
import test_m13b as support
from execution_policy import DEVELOPMENT_MUTATION, READ_ONLY_HOST, PRODUCTION_READ_ONLY
from host_executor import HostExecutor, RegisteredTarget, AuthorityDenied, TargetNotRegistered
from host_contract import operation_catalog
from m9_integration import ExecutionFinalizer
from tool_delivery import ToolDeliveryLedger, requires_reconciliation
from test_tool_delivery import Wire, delivery, rows


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.fixture
def v2():
    host = support.HostExecutorFixture()
    host.setUp()
    host.executor.config = dataclasses.replace(host.config, max_output_bytes=65536)
    task, ref, route, policy, lease = host.bound(
        capabilities=('GIT', 'SYSTEMD_USER', 'LOCAL_HOST_PROCESS'),
        classes=(READ_ONLY_HOST, DEVELOPMENT_MUTATION))
    wire = Wire()
    client = app_server.CodexAppServerClient(wire)
    ledger = ToolDeliveryLedger(host.registry, ref)

    def handler(params):
        args = params['arguments']
        request = host.request(task, ref, route, policy, args['capability'], args['operation'],
                               args.get('arguments', {}), operation_class=args.get('operation_class', READ_ONLY_HOST))
        return host.executor.execute(dataclasses.replace(request, tool_call_id=params['callId']))

    def configure(target):
        target.configure_dynamic_tool(namespace='clinx', name='clinx_host_operation', thread_id='thread',
                                      handler=handler, delivery_ledger=ledger)
        target.attach_dynamic_tool_turn('thread', 'turn')

    configure(client)

    def call(call_id, capability, operation, arguments=None, operation_class=READ_ONLY_HOST, ack=True):
        request = {'id': call_id, 'method': 'item/tool/call', 'params': {
            'threadId': 'thread', 'turnId': 'turn', 'callId': call_id,
            'namespace': 'clinx', 'tool': 'clinx_host_operation', 'arguments': {
                'capability': capability, 'operation': operation, 'arguments': arguments or {},
                'operation_class': operation_class}}}
        client._send_server_response(request)
        response = wire.sent[-1]['result']
        if ack:
            client._record_event({'method': 'item/completed', 'params': {
                'threadId': 'thread', 'turnId': 'turn', 'item': {
                    'id': call_id, 'type': 'dynamicToolCall', 'namespace': 'clinx',
                    'tool': 'clinx_host_operation', **response}}})
        return json.loads(response['contentItems'][0]['text'])

    yield SimpleNamespace(**locals())
    lease.__exit__(None, None, None)
    host.tearDown()


@pytest.mark.parametrize('cap,op,args,opclass,code', [
    ('GIT', 'nonexistent_operation', {}, READ_ONLY_HOST, 'OPERATION_NOT_SUPPORTED'),
    ('SYSTEMD_USER', 'service_is_active', {'target': 'unknown.service'}, READ_ONLY_HOST, 'TARGET_NOT_REGISTERED'),
    ('SYSTEMD_USER', 'service_is_active', {'lines': 4}, READ_ONLY_HOST, 'INVALID_ARGUMENTS'),
    ('SYSTEMD_USER', 'service_restart', {'target': 'clinx.service'}, READ_ONLY_HOST, 'AUTHORITY_DENIED'),
])
def test_predispatch_rejection_then_valid_service(v2, cap, op, args, opclass, code):
    d = v2
    with patch('host_executor.subprocess.Popen', side_effect=AssertionError('must not dispatch')):
        result = d.call('rejected', cap, op, args, opclass)
    assert result['result_state'] == code
    assert result['execution_state'] == 'COMMAND_NOT_DISPATCHED'
    assert result['side_effect_certainty'] == 'NOT_EXECUTED'
    assert result['continuation_state'] == 'SAFE_TO_CONTINUE'
    assert result['host_dispatched'] is False
    assert d.host.registry.list_host_executions(execution_ref=d.ref) == []
    # Real P620 service read; pre-dispatch failure did not poison this execution.
    result = d.call('legal', 'SYSTEMD_USER', 'service_is_active', {'target': 'clinx.service'})
    assert result['exit_code'] == 0
    records = ToolDeliveryLedger.records(d.host.registry, d.ref)
    assert records[0]['failure_code'] == code
    assert records[1]['delivery_state'] == 'DELIVERED'
    assert all(not requires_reconciliation(row) for row in records)


def test_capability_unavailable_then_legal_call(v2):
    with patch('host_executor.shutil.which', return_value=None):
        result = v2.call('missing-runtime', 'LOCAL_HOST_PROCESS', 'working_directory')
    assert result['result_state'] == 'CAPABILITY_UNAVAILABLE'
    assert result['execution_state'] == 'COMMAND_NOT_DISPATCHED'
    assert not v2.host.registry.list_host_executions(execution_ref=v2.ref)
    assert v2.call('legal', 'LOCAL_HOST_PROCESS', 'working_directory')['exit_code'] == 0


def test_multiple_rejections_and_same_call_replay_never_poison(v2):
    d = v2
    for call_id, cap, op, args in [
        ('a', 'GIT', 'nonexistent_operation', {}),
        ('b', 'SYSTEMD_USER', 'service_is_active', {'target': 'unknown.service'}),
        ('c', 'LOCAL_HOST_PROCESS', 'working_directory', {'extra': True})]:
        assert d.call(call_id, cap, op, args)['execution_state'] == 'COMMAND_NOT_DISPATCHED'
    assert not d.host.registry.list_host_executions(execution_ref=d.ref)
    # Changed request/call semantics cannot repurpose a recorded call ID.
    fresh = app_server.CodexAppServerClient(Wire())
    d.configure(fresh)
    fresh._send_server_response({'id': 'fresh', 'method': 'item/tool/call', 'params': {
        'threadId': 'thread', 'turnId': 'turn', 'namespace': 'clinx', 'tool': 'clinx_host_operation',
        'callId': 'a', 'arguments': {'capability': 'LOCAL_HOST_PROCESS', 'operation': 'working_directory'}}})
    assert not d.host.registry.list_host_executions(execution_ref=d.ref)
    assert d.call('legal', 'LOCAL_HOST_PROCESS', 'working_directory')['exit_code'] == 0
    decision = ExecutionFinalizer(d.host.registry)._decision(d.ref,
        'CLINX_EXECUTION_RESULT\nSTATUS=PASS\nSUMMARY=continued\nCHANGED_FILES=NONE\n'
        'VALIDATION=one Host command\nBLOCKERS=NONE\nNEXT_STATE=COMPLETED',
        provider_outcome='PROVIDER_TERMINAL', provider_status='completed')
    assert decision.result.status == 'PASS'


@pytest.mark.parametrize('exit_code', [0, 7])
def test_known_completion_delivered_allows_next_call(v2, exit_code):
    result = v2.call('first', 'LOCAL_HOST_PROCESS', 'development_command',
                     {'argv': ['python3', '-c', f'raise SystemExit({exit_code})']}, DEVELOPMENT_MUTATION)
    assert result['exit_code'] == exit_code
    assert v2.call('next', 'LOCAL_HOST_PROCESS', 'working_directory')['exit_code'] == 0
    assert len(v2.host.registry.list_host_executions(execution_ref=v2.ref)) == 2


def test_no_host_evidence_after_unknown_handler_crash_is_not_nonexecution(delivery):
    d = delivery
    def crash(params):
        raise RuntimeError('unknown completion boundary')
    d.client._dynamic_tool_handler = crash
    d.client._send_server_response(d.request)
    assert rows(d)[0]['execution_state'] == 'COMMAND_DISPATCHED'
    assert rows(d)[0]['side_effect_certainty'] == 'UNKNOWN'
    d.client._dynamic_tool_handler = d.handler
    d.client._send_server_response({**d.request, 'id': 33, 'params': {**d.request['params'], 'callId': 'new'}})
    assert not d.calls
    assert not d.host.registry.list_host_executions(execution_ref=d.ref)
    decision = ExecutionFinalizer(d.host.registry)._decision(d.ref, None,
        provider_outcome='PROVIDER_DISCONNECTED', provider_status=None)
    assert decision.failure_code == 'RESULT_DELIVERY_UNCONFIRMED_AFTER_DISPATCH'
    assert decision.retry_required is False


@pytest.mark.parametrize('argv', [
    ['git', 'push', '--force'], ['git', 'pull'], ['git', 'remote', 'set-url', 'origin', 'evil'],
    ['git', 'config', 'credential.helper', 'evil'], ['git', '-c', 'alias.bad=!sh', 'bad'],
    ['git', '-calias.escape=!sh', 'escape'], ['git', '--config-env=alias.escape=EVIL', 'escape'],
    ['bash', '-lc', 'true'], ['env', 'sudo', 'true'], ['sudo', 'true'], ['su'],
    ['ssh', 'unknown'], ['scp', 'a', 'b'], ['rsync', 'a', 'b'], ['aws', 'sts'],
    ['kubectl', 'get'], ['terraform', 'apply'], ['systemctl', 'status'],
])
def test_development_escape_denied(v2, argv):
    result = v2.call('escape', 'LOCAL_HOST_PROCESS', 'development_command', {'argv': argv}, DEVELOPMENT_MUTATION)
    assert result['result_state'] == 'AUTHORITY_DENIED'
    assert not v2.host.registry.list_host_executions(execution_ref=v2.ref)


def test_catalog_is_consumed_by_discovery_dynamic_schema_and_dispatch(v2):
    d = v2
    config = bridge.BridgeConfig.load(__import__('pathlib').Path(__file__).with_name('bridge.toml')).host_executor
    config = dataclasses.replace(config, trusted_workspace_roots=(d.host.root,))
    # No private target command/URL/argv is published; aliases and class bounds are safe.
    executor = HostExecutor(config, d.host.registry)
    catalog = operation_catalog(config)
    discovered = executor.capabilities()['executable_contract']
    description = executor.dynamic_tool_spec(config)['tools'][0]['description']
    encoded = json.dumps(discovered)
    assert '/home/pvxlabs/dev/ORION' not in encoded and '172.26.9.38' not in encoded
    assert 'production_deploy_status' in encoded and 'gateway_ready' in encoded
    for capability, operations in catalog.items():
        assert discovered['capabilities'][capability]['operations'] == operations
        for operation, spec in operations.items():
            assert operation in description
            assert spec['argument_schema']['additionalProperties'] is False
            args = {'argv': ['python3', '-V'], 'path': '.', 'name': '.clinx-host-executor-test'}
            args = {key: args.get(key, 'clinx.service') for key in spec['required_arguments']}
            if spec.get('registered_targets') and 'target' in args:
                args['target'] = spec['registered_targets'][0]['identity']
            request = d.host.request(d.task, d.ref, d.route, d.policy, capability, operation, args,
                                     operation_class=spec['operation_class'])
            with patch.object(executor, '_registered_origin'), patch.object(executor, '_git_identity', return_value='main'):
                command = executor._command(request)
            assert command.argv
            assert command.mutating == spec['mutating']
    service_targets = discovered['capabilities']['SYSTEMD_USER']['operations']['service_is_active']['registered_targets']
    assert [t['identity'] for t in service_targets] == [t.alias for t in config.services]


def test_git_six_operations_in_real_disposable_repositories(tmp_path):
    remote = tmp_path/'remote.git'
    root = tmp_path/'project'
    remote.mkdir(); root.mkdir()
    git(remote, 'init', '--bare', '-b', 'main')
    git(root, 'init', '-b', 'main')
    git(root, 'remote', 'add', 'origin', str(remote))
    git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '--allow-empty', '-m', 'initial')
    git(root, 'push', 'origin', 'main')
    from task_registry import TaskRegistry
    from execution_semantics import build_routing_identity
    registry = TaskRegistry(tmp_path/'tasks.sqlite3')
    policy = support.host_policy(capabilities=('GIT',), classes=(READ_ONLY_HOST, DEVELOPMENT_MUTATION))
    route = build_routing_identity(host='p620', surface='host_executor', provider='codex_app_server',
        transport='local_stdio', workspace_alias='p620', project_alias='pilot',
        worktree_key=registry.worktree_key(host='p620', cwd=str(root), repository_origin=str(remote)),
        project_identity='pilot', conversation_binding='thread', authority_scopes=policy.authority_scopes)
    task = registry.create_task(host='p620', workspace_alias='p620', project_alias='pilot', project_name='fixture',
        cwd=str(root), repository_origin=str(remote), branch='main', title='Git fixture', routing_identity=route, execution_policy=policy)
    executor = HostExecutor(dataclasses.replace(support.HostExecutorConfig(), enabled=True), registry)
    from host_executor import HostExecutionRequest
    def request(operation, args=None):
        return HostExecutionRequest(task.task_id, 'exec_git', route, policy,
            DEVELOPMENT_MUTATION if operation == 'push_current_branch' else READ_ONLY_HOST,
            'GIT', operation, args or {}, root)
    with registry.execution(task.task_id, execution_ref='exec_git', retain=True):
        (root/'dirty').write_text('preserve this worktree file')
        before = git(root, 'status', '--porcelain')
        for operation in ('head', 'status', 'fetch_origin', 'remote_main_head', 'ahead_behind'):
            assert executor.execute(request(operation))['exit_code'] == 0
        assert git(root, 'status', '--porcelain') == before
        git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            'commit', '--allow-empty', '-m', 'local change')
        assert executor.execute(request('ahead_behind'))['stdout'].strip() == '1\t0'
        assert executor.execute(request('push_current_branch'))['exit_code'] == 0
        assert executor.execute(request('remote_main_head'))['stdout'].split()[0] == git(root, 'rev-parse', 'HEAD')
        assert executor.execute(request('fetch_origin'))['exit_code'] == 0
        assert executor.execute(request('ahead_behind'))['stdout'].strip() == '0\t0'
        for arguments in ({'force': True}, {'remote': 'other'}, {'refspec': '+main:main'}):
            with pytest.raises(Exception):
                executor.execute(request('push_current_branch', arguments))
        git(root, 'config', 'remote.origin.pushurl', str(tmp_path/'evil'))
        with pytest.raises(TargetNotRegistered):
            executor.execute(request('push_current_branch'))
        git(root, 'remote', 'set-url', 'origin', str(tmp_path/'evil'))
        with pytest.raises(TargetNotRegistered):
            executor.execute(request('fetch_origin'))


def test_unknown_host_completion_ack_cannot_clear_reconciliation(v2):
    with patch('host_executor.subprocess.Popen', side_effect=OSError('fixture spawn uncertainty')):
        response = v2.call('uncertain', 'LOCAL_HOST_PROCESS', 'working_directory')
    assert response['execution_state'] == 'COMMAND_DISPATCHED'
    assert response['side_effect_certainty'] == 'UNKNOWN'
    assert response['failure_code'] == 'HOST_TRANSPORT_FAILED'
    response = v2.call('retry', 'LOCAL_HOST_PROCESS', 'working_directory')
    assert response['reconciliation_required'] is True
    assert len(v2.host.registry.list_host_executions(execution_ref=v2.ref)) == 1


@pytest.mark.parametrize('args', [{'target': 'clinx.service', 'lines': True},
                                 {'target': 'clinx.service', 'lines': 0},
                                 {'target': 123}])
def test_catalog_argument_types_are_enforced_before_dispatch(v2, args):
    result = v2.call('invalid', 'SYSTEMD_USER', 'service_journal', args)
    assert result['result_state'] == 'INVALID_ARGUMENTS'
    assert not v2.host.registry.list_host_executions(execution_ref=v2.ref)
