"""Delivery safety uses real HostExecutor/SQLite with isolated provider wires."""
import dataclasses
import json
import threading
import time
from types import SimpleNamespace

import pytest

import app_server
import bridge
import test_m13b as support
from execution_policy import DEVELOPMENT_MUTATION, READ_ONLY_HOST
from m9_integration import ExecutionFinalizer
from tool_delivery import DeliveryReconciliationRequired, ToolDeliveryLedger


class Wire:
    def __init__(self):
        self.sent = []
        self.fail = False

    def send(self, message):
        if self.fail:
            raise app_server.AppServerTransportError('fixture disconnect after Host exit')
        self.sent.append(message)

    def close(self):
        pass


@pytest.fixture
def delivery():
    host = support.HostExecutorFixture()
    host.setUp()
    task, ref, route, policy, lease = host.bound(classes=(DEVELOPMENT_MUTATION, READ_ONLY_HOST))
    wire = Wire()
    ledger = ToolDeliveryLedger(host.registry, ref)
    client = app_server.CodexAppServerClient(wire)
    calls = []
    after_host = []

    def handler(params):
        calls.append(params['callId'])
        readonly = params['arguments'].get('readonly', False)
        request = host.request(task, ref, route, policy, 'LOCAL_HOST_PROCESS',
                               'working_directory' if readonly else 'development_command',
                               {} if readonly else {'argv': params['arguments'].get('argv', ['python3', '-c',
                                   "from pathlib import Path; p=Path('count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"])},
                               operation_class=READ_ONLY_HOST if readonly else DEVELOPMENT_MUTATION)
        result = host.executor.execute(dataclasses.replace(request, tool_call_id=params['callId']))
        for action in after_host:
            action()
        return result

    def configure(target=client):
        target.configure_dynamic_tool(namespace='fixture_tools', name='perform', thread_id='thread',
                                      handler=handler, delivery_ledger=ledger, connection_generation=1)
        target.attach_dynamic_tool_turn('thread', 'turn')

    configure()
    request = {'id': 17, 'method': 'item/tool/call', 'params': {'threadId': 'thread', 'turnId': 'turn',
               'callId': 'call-1', 'namespace': 'fixture_tools', 'tool': 'perform', 'arguments': {}}}

    def acknowledge(*, success=True, call_id='call-1', host_ref=None, thread='thread', namespace='fixture_tools'):
        rows = ToolDeliveryLedger.records(host.registry, ref)
        content = {'execution_ref': ref, 'host_execution_ref': host_ref or rows[0]['host_execution_ref']}
        client._record_event({'method': 'item/completed', 'params': {'threadId': thread, 'turnId': 'turn',
                             'item': {'id': call_id, 'type': 'dynamicToolCall', 'success': success,
                                      'namespace': namespace, 'tool': 'perform',
                                      'contentItems': [{'type': 'inputText', 'text': json.dumps(content)}]}}})

    yield SimpleNamespace(**locals())
    lease.__exit__(None, None, None)
    host.tearDown()


def rows(d):
    return ToolDeliveryLedger.records(d.host.registry, d.ref)


def test_success_requires_provider_ack_and_keeps_all_correlation(delivery):
    d = delivery
    d.client._send_server_response(d.request)
    assert rows(d)[0]['delivery_state'] == 'PENDING'
    d.acknowledge()
    row = rows(d)[0]
    assert row['execution_state'] == 'COMMAND_EXECUTED_RESULT_DELIVERED'
    assert row['request_id'] == row['response_id'] == '17'
    host = d.host.registry.list_host_executions(execution_ref=d.ref)[0]
    assert (host['host_execution_ref'], host['tool_call_id'], host['execution_ref']) == (
        row['host_execution_ref'], row['tool_call_id'], row['execution_ref'])
    d.client._send_server_response(d.request)
    assert d.calls == ['call-1']
    assert (d.host.root / 'count').read_text() == '1'


@pytest.mark.parametrize('fault', ['disconnect', 'registry_replaced', 'connection_replaced', 'closed'])
def test_completed_mutation_cannot_execute_again_after_lifecycle_fault(delivery, fault):
    d = delivery
    if fault == 'disconnect':
        d.after_host.append(lambda: setattr(d.wire, 'fail', True))
    elif fault == 'registry_replaced':
        d.after_host.append(d.client.clear_dynamic_tool)
    elif fault == 'closed':
        d.after_host.append(d.client.close)
    else:
        d.after_host.append(lambda: setattr(d.client, 'transport', Wire()))
    with pytest.raises(app_server.AppServerTransportError):
        d.client._send_server_response(d.request)
    assert rows(d)[0]['execution_state'] == 'COMMAND_EXECUTED_RESULT_DELIVERY_FAILED'
    assert rows(d)[0]['failure_code'] == 'RESULT_DELIVERY_FAILED_AFTER_EXECUTION'
    # A fresh connection and changed request id cannot rerun the same call.
    fresh = app_server.CodexAppServerClient(Wire())
    d.configure(fresh)
    fresh._send_server_response({**d.request, 'id': 'reconnected-request'})
    assert 'reconciliation' in fresh.transport.sent[-1]['result']['contentItems'][0]['text']
    # Nor can a worker silently retry the command with a new tool call id.
    fresh._send_server_response({**d.request, 'id': 99, 'params': {**d.request['params'], 'callId': 'retry'}})
    assert d.calls == ['call-1']
    assert (d.host.root / 'count').read_text() == '1'
    assert len(d.host.registry.list_host_executions(execution_ref=d.ref)) == 1


def test_early_competing_rejection_keeps_host_execution_evidence(delivery):
    d = delivery
    d.after_host.append(lambda: d.acknowledge(success=False))
    d.client._send_server_response(d.request)
    assert rows(d)[0]['delivery_state'] == 'FAILED'
    assert rows(d)[0]['execution_state'] == 'COMMAND_EXECUTED_RESULT_DELIVERY_FAILED'
    assert rows(d)[0]['host_exit_code'] == 0


def test_handler_failure_after_execution_recovers_correlation(delivery):
    d = delivery
    def fail():
        raise RuntimeError('fixture failure after side effect')
    d.after_host.append(fail)
    d.client._send_server_response(d.request)
    row = rows(d)[0]
    assert row['host_execution_ref']
    assert row['host_exit_code'] == 0
    assert row['execution_state'] == 'COMMAND_EXECUTED_RESULT_DELIVERY_FAILED'


def test_finalizer_recovers_host_commit_before_envelope_crash(delivery):
    d = delivery
    d.ledger.admit(d.request, {})
    d.handler(d.request['params'])
    assert rows(d)[0]['host_execution_ref'] is None
    decision = ExecutionFinalizer(d.host.registry)._decision(
        d.ref, None, provider_outcome='PROVIDER_DISCONNECTED', provider_status=None)
    assert decision.failure_code == 'RESULT_DELIVERY_FAILED_AFTER_EXECUTION'
    assert decision.retry_required is False
    assert rows(d)[0]['host_exit_code'] == 0
    assert rows(d)[0]['host_execution_ref']


def test_registry_replaced_while_host_process_is_running(delivery):
    d = delivery
    d.request['params']['arguments']['argv'] = ['python3', '-c',
        "import time; from pathlib import Path; time.sleep(0.4); Path('count').write_text('1')"]
    errors = []
    def run():
        try:
            d.client._send_server_response(d.request)
        except app_server.AppServerTransportError as exc:
            errors.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 2
    while not d.host.registry.list_host_executions(execution_ref=d.ref) and time.monotonic() < deadline:
        time.sleep(0.005)
    assert d.host.registry.list_host_executions(execution_ref=d.ref)[0]['result_state'] == 'RUNNING'
    d.client.clear_dynamic_tool()
    assert rows(d)[0]['execution_state'] == 'COMMAND_DISPATCHED'
    assert rows(d)[0]['side_effect_certainty'] == 'UNKNOWN'
    fresh = app_server.CodexAppServerClient(Wire())
    d.configure(fresh)
    fresh._send_server_response({**d.request, 'id': 'during-disconnect',
        'params': {**d.request['params'], 'callId': 'retry-while-running'}})
    assert d.calls == ['call-1']
    worker.join(3)
    assert not worker.is_alive()
    assert len(errors) == 1
    assert (d.host.root/'count').read_text() == '1'
    assert rows(d)[0]['execution_state'] == 'COMMAND_EXECUTED_RESULT_DELIVERY_FAILED'
    assert len(d.host.registry.list_host_executions(execution_ref=d.ref)) == 1


def test_command_failure_and_delivery_success_are_separate(delivery):
    d = delivery
    d.request['params']['arguments']['argv'] = ['python3', '-c', 'raise SystemExit(7)']
    d.client._send_server_response(d.request)
    d.acknowledge()
    assert rows(d)[0]['host_exit_code'] == 7
    assert rows(d)[0]['execution_state'] == 'COMMAND_EXECUTION_FAILED'
    assert rows(d)[0]['delivery_state'] == 'DELIVERED'


def test_historical_blocked_result_is_immutable(delivery):
    d = delivery
    d.host.registry.bind_conversation(task_id=d.task.task_id, thread_id='thread',
                                     session_id='session', project_id=None, app_server_version='fixture')
    d.client._send_server_response(d.request)
    d.acknowledge(success=False)
    finalizer = ExecutionFinalizer(d.host.registry)
    result = finalizer.finalize(execution_ref=d.ref, task_id=d.task.task_id, turn_id='turn',
                                raw_result=None, provider_outcome='PROVIDER_DISCONNECTED')
    assert result.status == 'BLOCKED'
    assert result.terminal_state == 'RECOVERY_REQUIRED'
    assert d.host.registry.has_execution_lease(d.ref)
    assert not d.host.registry.get_task(d.task.task_id).retry_required
    before = d.host.registry.get_execution_result(d.ref)
    # Even a later provider acknowledgement cannot rewrite an existing result.
    d.acknowledge()
    finalizer.finalize(execution_ref=d.ref, task_id=d.task.task_id, turn_id='turn',
                       raw_result='CLINX_EXECUTION_RESULT\nSTATUS=PASS\nSUMMARY=late claim\n'
                                  'CHANGED_FILES=NONE\nVALIDATION=late\nBLOCKERS=NONE\nNEXT_STATE=COMPLETED')
    assert d.host.registry.get_execution_result(d.ref) == before
    assert d.host.registry.has_execution_lease(d.ref)


@pytest.mark.parametrize('change', [{'namespace': 'missing'}, {'connectionGeneration': 2}])
def test_namespace_or_generation_unavailable_before_dispatch(delivery, change):
    d = delivery
    d.client._send_server_response({**d.request, 'params': {**d.request['params'], **change}})
    result = json.loads(d.wire.sent[-1]['result']['contentItems'][0]['text'])
    assert result['execution_state'] == 'COMMAND_NOT_DISPATCHED'
    assert not d.calls
    assert not d.host.registry.list_host_executions(execution_ref=d.ref)


def test_nonowner_listener_does_not_race_owning_listener(delivery):
    d = delivery
    observer = app_server.CodexAppServerClient(Wire())
    observer._send_server_response(d.request)
    assert observer.transport.sent == []
    d.client._send_server_response(d.request)
    d.acknowledge()
    assert rows(d)[0]['delivery_state'] == 'DELIVERED'


@pytest.mark.parametrize('readonly', [False, True])
def test_rejection_after_execution_is_not_not_executed_or_retryable(delivery, readonly):
    d = delivery
    d.request['params']['arguments']['readonly'] = readonly
    d.client._send_server_response(d.request)
    d.acknowledge(success=False)
    assert rows(d)[0]['execution_state'] == 'COMMAND_EXECUTED_RESULT_DELIVERY_FAILED'
    decision = ExecutionFinalizer(d.host.registry)._decision(
        d.ref, 'CLINX_EXECUTION_RESULT\nSTATUS=PASS\nSUMMARY=worker claim\nCHANGED_FILES=NONE\n'
        'VALIDATION=claim\nBLOCKERS=NONE\nNEXT_STATE=COMPLETED',
        provider_outcome='PROVIDER_TERMINAL', provider_status='completed')
    assert decision.terminal_state == 'RECOVERY_REQUIRED'
    assert decision.failure_code == 'RESULT_DELIVERY_FAILED_AFTER_EXECUTION'
    assert decision.retry_required is False
    assert d.host.registry.list_host_executions(execution_ref=d.ref)[0]['exit_code'] == 0


def test_wrong_ack_cannot_claim_delivery(delivery):
    d = delivery
    d.client._send_server_response(d.request)
    d.acknowledge(thread='foreign')
    d.acknowledge(call_id='foreign')
    d.acknowledge(namespace='foreign')
    assert rows(d)[0]['delivery_state'] == 'PENDING'
    d.acknowledge(host_ref='hostexec_foreign')
    assert rows(d)[0]['delivery_state'] == 'FAILED'


def test_known_provider_failure_reconciles_without_host_replay(delivery):
    d = delivery
    d.request['params']['arguments']['readonly'] = True
    d.client._send_server_response(d.request)
    d.acknowledge(success=False)
    row = rows(d)[0]
    assert row['delivery_state'] == 'FAILED'
    assert row['execution_state'] == 'COMMAND_EXECUTED_RESULT_DELIVERY_FAILED'
    assert row['provider_failure_sha256']
    identity = json.loads(row['identity_json'])
    with d.host.registry._connect() as conn:
        host = dict(conn.execute('SELECT * FROM host_executions WHERE host_execution_ref=?',
                                 (row['host_execution_ref'],)).fetchone())
    proof = {
        'task_id': host['task_id'], 'thread_id': identity['thread_id'],
        'turn_id': identity['turn_id'], 'namespace': identity['namespace'],
        'tool': identity['tool'], 'host_execution_ref': row['host_execution_ref'],
        'operation_class': host['operation_class'], 'capability': host['capability'],
        'operation': host['operation'], 'argv': json.loads(host['argv_json']),
        'mutating': bool(host['mutating']),
        'provider_failure_sha256': row['provider_failure_sha256'],
    }
    outcome = d.ledger.reconcile_failed('call-1', proof=proof)
    assert outcome['state'] == 'RESOLVED'
    assert outcome['delivery_state'] == 'FAILED'
    after = rows(d)[0]
    assert after['reconciliation_state'] == 'RESOLVED'
    assert after['reconciliation_required'] is False
    assert d.ledger.reconcile_failed('call-1', proof=proof)['state'] == 'ALREADY_APPLIED'
    with pytest.raises(DeliveryReconciliationRequired, match='new execution is required'):
        d.ledger.admit({**d.request, 'params': {**d.request['params'], 'callId': 'call-2'}}, {})


def test_registry_delivery_reconciliation_releases_only_old_execution(delivery):
    d = delivery
    d.request['params']['arguments']['readonly'] = True
    d.host.registry.bind_conversation(task_id=d.task.task_id, thread_id='thread',
                                      session_id='session', project_id=None, app_server_version='fixture')
    d.client._send_server_response(d.request)
    d.acknowledge(success=False)
    row = rows(d)[0]
    d.host.registry.set_execution_state(d.task.task_id, 'RECOVERY_REQUIRED',
        current_stage='RECOVERY_REQUIRED', turn_id='turn', retry_required=True,
        failure_code='RESULT_DELIVERY_FAILED_AFTER_EXECUTION',
        failure_stage='delivery', failure_evidence='provider item persisted')
    d.host.registry.record_execution_result(
        execution_ref=d.ref, task_id=d.task.task_id, turn_id='turn', status='BLOCKED',
        summary='Provider delivery rejected after Host success', changed_files='NONE',
        validation='Host receipt and Provider item persisted',
        blockers='RESULT_DELIVERY_FAILED_AFTER_EXECUTION: command retry prohibited',
        next_state='BLOCKED', raw_result='CLINX_EXECUTION_RESULT\nSTATUS=BLOCKED')
    identity = json.loads(row['identity_json'])
    with d.host.registry._connect() as conn:
        host = dict(conn.execute('SELECT * FROM host_executions WHERE host_execution_ref=?',
                                 (row['host_execution_ref'],)).fetchone())
    proof = {'task_id': host['task_id'], 'thread_id': identity['thread_id'], 'turn_id': identity['turn_id'],
             'namespace': identity['namespace'], 'tool': identity['tool'],
             'host_execution_ref': row['host_execution_ref'], 'operation_class': host['operation_class'],
             'capability': host['capability'], 'operation': host['operation'],
             'argv': json.loads(host['argv_json']), 'mutating': bool(host['mutating']),
             'provider_failure_sha256': row['provider_failure_sha256']}
    result = d.host.registry.reconcile_host_delivery_failure(d.ref, 'call-1', proof)
    assert result['terminal_state'] == 'BLOCKED'
    assert result['original_result_preserved'] is True
    assert not d.host.registry.has_execution_lease(d.ref)
    assert d.host.registry.get_execution_result(d.ref).status == 'BLOCKED'


def test_owned_endpoint_has_no_shared_daemon_fallback(tmp_path):
    cfg = SimpleNamespace(runtime_host='p620', app_server=bridge.AppServerConfig(local_socket=str(tmp_path/'provider.sock')))
    target = SimpleNamespace(target_host='p620', transport='local', alias='fixture')
    client = bridge._default_app_server_client(cfg, target)
    assert client.transport.command[-2:] == ('--sock', str(tmp_path/'provider.sock'))
    with pytest.raises(app_server.AppServerError):
        # Explicit missing endpoint cannot reconnect to the shared daemon.
        with client:
            client.initialize(client_name='fixture', client_title='fixture', client_version='1')
