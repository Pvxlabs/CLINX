"""Regression of five ACKed Host calls followed by a stale Worker blocker."""
import json
from unittest.mock import patch

import pytest
import app_server
import bridge
from m9_integration import ClinxIntegration, ExecutionFinalizer
from test_tool_delivery import delivery, rows, Wire
from tool_delivery import ToolDeliveryLedger


def request(d, n):
    return {**d.request, 'id': n, 'params': {**d.request['params'], 'callId': f'call-{n}'}}


def ack(d, n):
    row = next(r for r in rows(d) if r['tool_call_id'] == f'call-{n}')
    body = {'host_execution_ref': row['host_execution_ref'], 'execution_ref': d.ref}
    return {'method': 'item/completed', 'params': {'threadId': 'thread', 'turnId': 'turn',
        'item': {'type': 'dynamicToolCall', 'id': f'call-{n}', 'namespace': 'fixture_tools',
                 'tool': 'perform', 'success': True,
                 'contentItems': [{'type': 'inputText', 'text': json.dumps(body)}]}}}


@pytest.mark.parametrize('batched', [False, True])
def test_five_calls_and_next_operation_with_delayed_ack(delivery, batched):
    d = delivery
    for n in range(1, 6):
        d.client._send_server_response(request(d, n))
        if not batched:
            d.client._record_event(ack(d, n))
    if batched:
        assert d.calls == ['call-1']
        assert len(d.client._delivery_waiting_requests) == 4
        # The same event reader processes an ACK then admits exactly one waiter.
        for n in range(1, 6):
            d.wire.receive = lambda timeout, n=n: ack(d, n)
            d.client.drain_events(max_events=1, timeout_seconds=1)
            # Drain prior recorded request notifications before reading the wire.
            while rows(d)[n-1]['delivery_state'] != 'DELIVERED':
                d.client.drain_events(max_events=1, timeout_seconds=1)
    assert d.calls == [f'call-{n}' for n in range(1, 6)]
    assert all(r['delivery_state'] == 'DELIVERED' for r in rows(d))
    d.client._send_server_response(request(d, 6))
    d.client._record_event(ack(d, 6))
    assert (d.host.root/'count').read_text() == '6'
    for reply in d.wire.sent:
        body = json.loads(reply['result']['contentItems'][0]['text'])
        assert body['delivery_state'] == 'PENDING'  # truthful send-time snapshot
        assert body['delivery_state_scope'] == 'RESPONSE_SNAPSHOT'
        assert body['execution_can_continue'] is True
        assert body['reconciliation_required'] is False
        assert body['retry_allowed'] is False


def test_pending_is_not_failed_and_lost_ack_still_blocks_replays(delivery):
    d = delivery
    d.client._send_server_response(request(d, 1))
    assert rows(d)[0]['failure_code'] is None
    assert rows(d)[0]['execution_state'] == 'COMMAND_EXECUTED_RESULT_DELIVERY_PENDING'
    assert rows(d)[0]['continuation_state'] == 'WAITING_INTERNAL_ACK'
    d.client._send_server_response(request(d, 2))
    # Expire the existing request deadline without sleeping or raising it.
    with patch('app_server.time.time', return_value=rows(d)[0]['response_sent_at'] + 31):
        with pytest.raises(app_server.AppServerTransportError, match='ACK deadline'):
            d.client._receive(30)
    assert rows(d)[0]['delivery_state'] == 'FAILED'
    fresh = app_server.CodexAppServerClient(Wire())
    d.configure(fresh)
    for n in [1, 2, 7]:
        fresh._send_server_response(request(d, n))
    assert d.calls == ['call-1']
    assert (d.host.root/'count').read_text() == '1'


def test_ack_owner_and_exact_turn_cannot_be_forged(delivery):
    d = delivery
    d.client._send_server_response(request(d, 1))
    event = ack(d, 1)
    d.ledger.observe(event, owner={**d.client._delivery_owner(), 'listener_id': 'foreign'})
    assert rows(d)[0]['delivery_state'] == 'PENDING'
    event['params']['turnId'] = 'wrong'
    d.client._record_event(event)
    assert rows(d)[0]['delivery_state'] == 'PENDING'
    d.client._record_event(ack(d, 1))
    assert rows(d)[0]['delivery_state'] == 'DELIVERED'


def test_reliable_command_failure_does_not_stop_next_call(delivery):
    d = delivery
    first = request(d, 1)
    first['params'] = {**first['params'], 'arguments': {'argv': ['python3', '-c', 'raise SystemExit(9)']}}
    d.client._send_server_response(first)
    body = json.loads(d.wire.sent[-1]['result']['contentItems'][0]['text'])
    assert body['exit_code'] == 9 and body['execution_can_continue']
    d.client._record_event(ack(d, 1))
    d.client._send_server_response(request(d, 2))
    d.client._record_event(ack(d, 2))
    assert d.calls == ['call-1', 'call-2']
    assert rows(d)[0]['execution_state'] == 'COMMAND_EXECUTION_FAILED'
    assert all(r['delivery_state'] == 'DELIVERED' for r in rows(d))


def test_stale_worker_blocker_read_is_exact_and_never_finalized_again(delivery):
    d = delivery
    d.host.registry.bind_conversation(task_id=d.task.task_id, thread_id=d.route.conversation.binding,
                                     session_id='session', project_id=None, app_server_version='fixture')
    d.host.registry.set_execution_state(d.task.task_id, 'CODEX_RUNNING', turn_id='turn', codex_running=True)
    for n in range(1, 6):
        d.client._send_server_response(request(d, n))
        d.client._record_event(ack(d, n))
    raw = ('CLINX_EXECUTION_RESULT\nSTATUS=BLOCKED\nSUMMARY=AWAITING_PROVIDER_ACK\n'
           'CHANGED_FILES=NONE\nVALIDATION=five results\nBLOCKERS=AWAITING_PROVIDER_ACK\nNEXT_STATE=BLOCKED')
    ExecutionFinalizer(d.host.registry).finalize(execution_ref=d.ref, task_id=d.task.task_id,
                                               turn_id='turn', raw_result=raw)
    before = d.host.registry.get_execution_result(d.ref)
    from types import SimpleNamespace
    reader = SimpleNamespace(resolve_task=lambda **kw: d.host.registry.get_task(d.task.task_id))
    integration = ClinxIntegration(SimpleNamespace(), d.host.registry, None, reader, None)
    with patch.object(d.host.registry, 'reclaim_stale_worktree_leases', side_effect=AssertionError('read mutation')):
        by_task = integration.get_status(task_ref=d.task.task_id)
        by_exec = integration.get_status(execution_ref=d.ref)
    for key in ('execution_ref', 'execution_result', 'host_executions', 'dynamic_tool_deliveries', 'provider_delivery'):
        assert by_task[key] == by_exec[key]
    assert by_task['provider_delivery']['state'] == 'DELIVERED'
    assert by_task['provider_delivery']['worker_pending_is_stale_snapshot'] is True
    assert by_task['execution_result']['status'] == 'BLOCKED'
    assert d.host.registry.get_execution_result(d.ref) == before

    with d.host.registry.execution(d.task.task_id, execution_ref='exec_successor', retain=True):
        d.host.registry.set_execution_state(d.task.task_id, 'CODEX_RUNNING',
                                           turn_id='turn-2', codex_running=True)
        current = integration.get_status(task_ref=d.task.task_id)
        assert current['execution_ref'] == 'exec_successor'
        assert current['host_executions'] == current['dynamic_tool_deliveries'] == []
        assert current['execution_result'] is None
        historical = integration.get_status(execution_ref=d.ref)
        assert historical['provider_delivery']['count'] == 5
        assert historical['CODEX_RUNNING'] is False
        assert historical['task_current_projection']['codex_running'] is True


def test_predispatch_rejection_ack_is_delivered_and_next_call_is_legal(delivery):
    import dataclasses
    from execution_policy import READ_ONLY_HOST
    d = delivery
    original = d.client._dynamic_tool_handler
    def reject(params):
        operation = d.host.request(d.task, d.ref, d.route, d.policy, 'LOCAL_HOST_PROCESS',
                                   'unsupported_operation', operation_class=READ_ONLY_HOST)
        return d.host.executor.execute(dataclasses.replace(operation, tool_call_id=params['callId']))
    d.client._dynamic_tool_handler = reject
    d.client._send_server_response(request(d, 1))
    response = d.wire.sent[-1]['result']
    body = json.loads(response['contentItems'][0]['text'])
    assert body['execution_state'] == 'COMMAND_NOT_DISPATCHED'
    assert body['execution_can_continue'] is True
    assert not d.host.registry.list_host_executions(execution_ref=d.ref)
    d.client._record_event({'method':'item/completed','params':{'threadId':'thread','turnId':'turn',
        'item': {'id':'call-1','type':'dynamicToolCall','namespace':'fixture_tools','tool':'perform',**response}}})
    assert rows(d)[0]['delivery_state'] == 'DELIVERED'
    assert rows(d)[0]['reconciliation_required'] is False
    d.client._dynamic_tool_handler = original
    d.client._send_server_response(request(d, 2))
    d.client._record_event(ack(d, 2))
    assert d.calls == ['call-2']
    assert (d.host.root/'count').read_text() == '1'
