"""Opt-in real Provider fault acceptance; never injected into a shared task.

CLINX_LIVE_DELIVERY_ACCEPTANCE=1 python3 -m pytest -q -s test_tool_delivery_live.py
Uses an isolated endpoint/state and existing machine authentication. All task,
lease, result and Host writes are isolated in pytest's temporary workspace.
"""
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

import bridge
from provider_qualification import isolated_provider
from m9_integration import ClinxIntegration
from mcp_server import ClinxMCPServer
from task_registry import TaskRegistry, WorkspaceConfig
from tool_delivery import ToolDeliveryLedger


@pytest.mark.skipif(os.environ.get('CLINX_LIVE_DELIVERY_ACCEPTANCE') != '1',
                    reason='explicit real Provider acceptance opt-in required')
def test_real_provider_rejection_after_host_exit_never_reexecutes(request, tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=CLINX acceptance',
                    '-c', 'user.email=acceptance@example.invalid', 'commit', '-q',
                    '--allow-empty', '-m', 'Initialize disposable delivery fixture'], check=True)
    cfg = bridge.BridgeConfig.load(Path(__file__).with_name('bridge.toml'))
    isolated = isolated_provider(cfg, tmp_path)
    cfg = isolated.__enter__()
    request.addfinalizer(lambda: isolated.__exit__(None, None, None))
    assert cfg.app_server.local_socket, 'an owned Provider endpoint is required'
    cfg = dataclasses.replace(cfg, task_db_path=tmp_path/'tasks.sqlite3',
        projects=(bridge.ProjectMapping(linear_name='Delivery fault acceptance', alias='delivery-fault',
            repo=root, branch='main', workspace_alias='p620'),), targets=(), threads=(),
        workspaces=(WorkspaceConfig(alias='p620', root=tmp_path, host='p620'),),
        log_dir=tmp_path/'logs')
    registry = TaskRegistry(cfg.task_db_path)
    injected = []
    owner_clients = []
    provider_targets = []

    def client_factory(target):
        provider_targets.append(target)
        client = bridge._default_app_server_client(cfg, target)
        original = client._cache_and_send_server_response

        def send(request_id, response, record):
            # The response envelope exists only after execute() durably records
            # Host completion. Replace just this disposable task's first result
            # with a namespace-unavailable reply, consumed by the real Provider.
            if response.get('success') and client._delivery_ledger is not None:
                body = json.loads(response['contentItems'][0]['text'])
                if body.get('host_execution_ref') and not injected:
                    assert body['exit_code'] == 0
                    call_id = record.request['params']['callId']
                    client._delivery_ledger.failed(call_id)
                    injected.append({'execution_ref': body['execution_ref'],
                                     'host_execution_ref': body['host_execution_ref'], 'tool_call_id': call_id})
                    owner_clients.append(client)
                    response = {'success': False, 'contentItems': [{'type': 'inputText', 'text': json.dumps({
                        'result_state': 'RESULT_DELIVERY_FAILED_AFTER_EXECUTION',
                        'execution_state': 'COMMAND_EXECUTED_RESULT_DELIVERY_FAILED',
                        'error': 'Acceptance fault: namespace registry unavailable after Host exit; reconcile, do not retry.',
                    })}]}
            return original(request_id, response, record)

        client._cache_and_send_server_response = send
        return client

    dispatcher = bridge.TaskDispatcher(cfg, task_registry=registry, client_factory=client_factory)
    integration = ClinxIntegration(cfg, registry, dispatcher, bridge.TaskContextReader(cfg, registry), None)
    server = ClinxMCPServer(integration, allow_execute=True)

    def tool(name, arguments):
        result = server.handle({'jsonrpc': '2.0', 'id': name, 'method': 'tools/call',
                                'params': {'name': name, 'arguments': arguments}})['result']
        assert not result['isError'], result['structuredContent']
        return result['structuredContent']

    readonly_reconcile = os.environ.get('CLINX_LIVE_DELIVERY_READONLY_RECONCILE') == '1'
    if readonly_reconcile:
        request = {'capability': 'GIT', 'operation_class': 'READ_ONLY_HOST',
                   'operation': 'status', 'arguments': {}}
        project, title = 'delivery-fault', 'Isolated Provider read-only delivery reconciliation'
        summary, changed_files = 'One temporary GIT/status read with delivery reconciliation', 'NONE'
        replay_operation = request
    else:
        command = ['python3', '-c', "from pathlib import Path; p=Path('counter'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); print('FAULT_COUNTER_WRITTEN')"]
        request = {'capability': 'LOCAL_HOST_PROCESS', 'operation_class': 'DEVELOPMENT_MUTATION',
                   'operation': 'development_command', 'arguments': {'argv': command}}
        project, title = 'delivery-fault', 'Isolated Provider delivery fault acceptance'
        summary, changed_files = 'One temporary counter increment; no other commands', 'counter'
        replay_operation = request
    prepared = tool('clinx_prepare_execution', dict(approved=True, task_action='create',
        host='p620', project=project, title=title, summary=summary,
        execution_surface='HOST_EXECUTOR', required_capabilities=[request['capability']],
        operation_classes=[request['operation_class']], production_mutation_intent=False,
        prompt='Invoke exactly one clinx.clinx_host_operation with request='
               + json.dumps(request) + '. Do not use any other tools or commands. '
               'Do not retry on failure. If the tool reports delivery failure, return '
               'CLINX_EXECUTION_RESULT with STATUS=BLOCKED, CHANGED_FILES=' + changed_files + ', '
               'BLOCKERS=RESULT_DELIVERY_FAILED_AFTER_EXECUTION, NEXT_STATE=BLOCKED, '
               'and concise SUMMARY and VALIDATION. No other files or services may be modified.'))
    started = tool('clinx_start_execution', {'approved': True,
                                           'prepared_execution_ref': prepared['prepared_execution_ref']})
    reference = started['execution_ref']
    print('LIVE_FAULT_EXECUTION', reference, flush=True)
    try:
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            result = registry.get_execution_result(reference)
            # A known delivery failure is terminal evidence for the result, but
            # this mutating fixture is intentionally not eligible for automatic
            # lease release. The explicit GIT/status reconciliation path is
            # tested separately; waiting for the lease here would hide that
            # safety boundary behind the live-provider deadline.
            if result is not None:
                break
            time.sleep(0.2)
        assert result is not None, 'real Provider did not produce a terminal result'
        hosts = registry.list_host_executions(execution_ref=reference)
        deliveries = ToolDeliveryLedger.records(registry, reference)
        task = registry.get_task(started['task_ref'])
        assert len(injected) == len(hosts) == 1
        if not readonly_reconcile:
            assert (root/'counter').read_text() == '1'
        assert hosts[0]['exit_code'] == 0
        assert result.status == 'BLOCKED'
        assert 'RESULT_DELIVERY_FAILED_AFTER_EXECUTION' in result.blockers
        assert not task.retry_required
        assert registry.get_active_execution(reference) is not None
        assert registry.get_execution_record(reference)['stage'] == 'RECOVERY_REQUIRED'
        assert deliveries[0]['execution_state'] == 'COMMAND_EXECUTED_RESULT_DELIVERY_FAILED'
        assert deliveries[0]['delivery_state'] == 'FAILED'
        assert deliveries[0]['acknowledged_at'] is not None
        # Open a real new Provider connection and read the same durable thread;
        # no new turn is created. Adversarial replay frames then exercise the
        # app-server admission path on a fresh listener with the same ledger.
        owner = owner_clients[0]
        identity = json.loads(deliveries[0]['identity_json'])
        reconnected = bridge._default_app_server_client(cfg, provider_targets[0])
        try:
            reconnected.__enter__()
            reconnected.initialize(client_name='host-v2-fault-reconnect',
                client_title='CLINX isolated fault acceptance', client_version='2')
            thread = reconnected.thread_read(identity['thread_id'])
            assert thread['id'] == identity['thread_id']
        finally:
            reconnected.close()
        import app_server
        from test_tool_delivery import Wire
        replay_client = app_server.CodexAppServerClient(Wire())
        handler_calls = []
        def forbidden_handler(params):
            handler_calls.append(params['callId'])
            raise AssertionError('replay reached Host handler')
        replay_client.configure_dynamic_tool(namespace=identity['namespace'], name=identity['tool'],
            thread_id=identity['thread_id'], handler=forbidden_handler,
            delivery_ledger=ToolDeliveryLedger(registry, reference))
        replay_client.attach_dynamic_tool_turn(identity['thread_id'], identity['turn_id'])
        replay_results = []
        for request_id, call_id in [('reconnected', deliveries[0]['tool_call_id']),
                                    ('new-request', deliveries[0]['tool_call_id']),
                                    ('new-call-request', 'worker-retry-new-call')]:
            replay_client._send_server_response({'id': request_id, 'method': 'item/tool/call', 'params': {
                'threadId': identity['thread_id'], 'turnId': identity['turn_id'],
                'namespace': identity['namespace'], 'tool': identity['tool'], 'callId': call_id,
                'arguments': replay_operation}})
            reply = json.loads(replay_client.transport.sent[-1]['result']['contentItems'][0]['text'])
            assert reply['reconciliation_required'] is True
            replay_results.append({'request_id': request_id, 'call_id': call_id, 'result_state': reply['result_state']})
        assert handler_calls == []
        if not readonly_reconcile:
            assert (root/'counter').read_text() == '1'
        assert len(registry.list_host_executions(execution_ref=reference)) == 1
        reconciliation = None
        if readonly_reconcile:
            with registry._connect() as conn:
                host = dict(conn.execute('SELECT * FROM host_executions WHERE host_execution_ref=?',
                    (hosts[0]['host_execution_ref'],)).fetchone())
            identity = json.loads(deliveries[0]['identity_json'])
            proof = {'task_id': host['task_id'], 'thread_id': identity['thread_id'],
                     'turn_id': identity['turn_id'], 'namespace': identity['namespace'],
                     'tool': identity['tool'], 'host_execution_ref': host['host_execution_ref'],
                     'operation_class': host['operation_class'], 'capability': host['capability'],
                     'operation': host['operation'], 'argv': json.loads(host['argv_json']),
                     'mutating': bool(host['mutating']),
                     'provider_failure_sha256': deliveries[0]['provider_failure_sha256']}
            reconciliation = registry.reconcile_host_delivery_failure(reference,
                deliveries[0]['tool_call_id'], proof)
            assert reconciliation['terminal_state'] == 'BLOCKED'
            assert reconciliation['original_result_preserved'] is True
            assert not registry.has_execution_lease(reference)
            assert ToolDeliveryLedger(registry, reference).reconcile_failed(
                deliveries[0]['tool_call_id'], proof=proof)['state'] == 'ALREADY_APPLIED'
            assert ToolDeliveryLedger.records(registry, reference)[0]['delivery_state'] == 'FAILED'
        evidence = {'execution_ref': reference, 'prepared_execution_ref': prepared['prepared_execution_ref'],
                    'task_ref': started['task_ref'], 'status': result.status, 'blockers': result.blockers,
                    'retry_required': task.retry_required, 'HOST_EXECUTION_COUNT': len(hosts),
                    'injected': injected, 'deliveries': deliveries, 'provider_reconnect': 'PASS',
                    'replay_results': replay_results, 'reconciliation': reconciliation,
                    'DUPLICATE_EXECUTION_PREVENTION': 'PASS'}
        (tmp_path/'acceptance.json').write_text(json.dumps(evidence, indent=2))
        print('LIVE_FAULT_EVIDENCE', tmp_path/'acceptance.json', flush=True)
    finally:
        dispatcher.stop_completion_runtime()
