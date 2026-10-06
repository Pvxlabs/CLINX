"""Provider ingress must be durable even before the Host callback is reached."""
import dataclasses
import json

import pytest

from test_tool_delivery import delivery, rows


def started(request):
    params = request['params']
    return {'method': 'item/started', 'params': {
        'threadId': params['threadId'], 'turnId': params['turnId'],
        'item': {'type': 'dynamicToolCall', 'id': params['callId'],
                 'namespace': params['namespace'], 'tool': params['tool'],
                 'arguments': params['arguments'], 'status': 'inProgress'}}}


def test_provider_started_read_is_durable_before_rpc_delivery(delivery):
    d = delivery
    d.request['params']['arguments']['readonly'] = True
    # Same provider lifecycle shape as the historical exec-8ac274f6 call.
    # A missing request frame is not evidence that no Host call was requested.
    d.client._record_event(started(d.request))
    assert len(rows(d)) == 1
    assert rows(d)[0]['call_state'] == 'RECEIVED'
    assert not d.calls
    assert not d.host.registry.list_host_executions(execution_ref=d.ref)
    d.client._send_server_response(d.request)
    d.acknowledge()
    assert rows(d)[0]['call_state'] == 'SUCCEEDED'
    assert len(d.calls) == 1


@pytest.mark.parametrize('change', [{'namespace': 'unregistered'}, {'connectionGeneration': 2}])
def test_owned_prevalidation_rejection_is_durable(delivery, change):
    d = delivery
    d.client._send_server_response({**d.request, 'params': {**d.request['params'], **change}})
    result = json.loads(d.wire.sent[-1]['result']['contentItems'][0]['text'])
    assert result['host_dispatched'] is False
    assert len(rows(d)) == 1
    assert rows(d)[0]['call_state'] == 'REJECTED'
    assert not d.calls


def test_missing_call_id_has_durable_local_rejection_identity(delivery):
    d = delivery
    request = {**d.request, 'params': {**d.request['params'], 'callId': None}}
    d.client._send_server_response(request)
    row = rows(d)[0]
    assert row['tool_call_id'].startswith('invalid-')
    assert row['call_state'] == 'REJECTED'
    assert row['host_dispatched'] is False
    assert row['response_sent_at']
    assert not d.calls


def test_rejected_new_call_does_not_hide_previous_uncertainty(delivery):
    d = delivery
    d.client._send_server_response(d.request)
    d.ledger.failed('call-1')
    request = {**d.request, 'id': 18, 'params': {**d.request['params'], 'callId': 'call-2'}}
    d.client._send_server_response(request)
    previous, new = rows(d)
    assert previous['reconciliation_required'] is True
    assert new['call_state'] == 'REJECTED'
    assert new['host_dispatched'] is False
    result = json.loads(d.wire.sent[-1]['result']['contentItems'][0]['text'])
    assert result['reconciliation_required'] is True
    assert d.calls == ['call-1']


def test_capacity_rejection_is_durable(delivery):
    d = delivery
    d.client.max_received_events = 0
    d.client._send_server_response(d.request)
    assert rows(d)[0]['call_state'] == 'REJECTED'
    assert rows(d)[0]['failure_code'] == 'TOOL_ADMISSION_CAPACITY_EXHAUSTED'
    assert not d.calls


def test_observer_and_foreign_turn_do_not_write_ingress(delivery):
    d = delivery
    event = started(d.request)
    event['params']['turnId'] = 'foreign'
    d.client._record_event(event)
    d.client.read_only_observer = True
    d.client._record_event(started(d.request))
    d.client._send_server_response(d.request)
    assert rows(d) == []
    assert not d.wire.sent


def test_malformed_duplicate_cannot_claim_prior_side_effect_was_not_executed(delivery):
    d = delivery
    d.client._send_server_response(d.request)
    d.acknowledge()
    prior = rows(d)
    d.client._send_server_response({**d.request, 'id': 18,
        'params': {**d.request['params'], 'namespace': 'wrong'}})
    body = json.loads(d.wire.sent[-1]['result']['contentItems'][0]['text'])
    assert body['host_dispatched'] is True
    assert body['side_effect_certainty'] == 'EXECUTED'
    assert rows(d) == prior
    assert d.calls == ['call-1']


def test_request_id_conflict_persists_new_call_rejection(delivery):
    d = delivery
    d.client._send_server_response(d.request)
    d.acknowledge()
    d.client._send_server_response({**d.request,
        'params': {**d.request['params'], 'callId': 'call-2'}})
    previous, rejected = rows(d)
    assert previous['call_state'] == 'SUCCEEDED'
    assert rejected['call_state'] == 'REJECTED'
    assert rejected['failure_code'] == 'TOOL_REQUEST_ID_CONFLICT'
    assert d.calls == ['call-1']


@pytest.mark.parametrize('failure', ['AUTHORITY_DENIED', 'TARGET_NOT_REGISTERED'])
def test_real_policy_and_target_rejections_round_trip_without_host_process(delivery, failure):
    d = delivery
    from execution_policy import READ_ONLY_HOST

    def reject(params):
        request = d.host.request(d.task, d.ref, d.route, d.policy, 'LOCAL_HOST_PROCESS',
                                 'working_directory', {}, operation_class=READ_ONLY_HOST)
        if failure == 'AUTHORITY_DENIED':
            request = dataclasses.replace(request, capability='SYSTEMD_USER',
                operation='service_is_active', arguments={'target': 'unregistered-fixture'})
        else:
            request = dataclasses.replace(request, project_root=d.host.root/'unregistered-fixture')
        return d.host.executor.execute(dataclasses.replace(request, tool_call_id=params['callId']))

    d.client._dynamic_tool_handler = reject
    d.client._send_server_response(d.request)
    response = d.wire.sent[-1]['result']
    body = json.loads(response['contentItems'][0]['text'])
    assert body['result_state'] == failure
    assert body['host_dispatched'] is False
    assert not d.host.registry.list_host_executions(execution_ref=d.ref)
    assert rows(d)[0]['call_state'] == 'REJECTED'
    d.client._record_event({'method': 'item/completed', 'params': {
        'threadId': 'thread', 'turnId': 'turn', 'item': {
            'id': 'call-1', 'type': 'dynamicToolCall', 'namespace': 'fixture_tools',
            'tool': 'perform', **response}}})
    assert rows(d)[0]['delivery_state'] == 'DELIVERED'
    assert rows(d)[0]['reconciliation_required'] is False
