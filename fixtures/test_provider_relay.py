"""Relay framing/identity guards. Business outcomes remain Rust-only."""
import base64
import hashlib
import json
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest

from clinx.nodes.node_protocol import NodeProtocolError, SharingScope
from clinx.nodes.provider_relay import CHUNK, NodeProviderRelay, encoded


def envelope(body, context=None, reference='a'*32):
    data = encoded(body)
    return dict(operation='provider.upload', request_id=reference,
        context=context or dict(host='air', user_scope='alice', cwd='/fixture', thread_id='exact'),
        index=0, count=1, digest=hashlib.sha256(data).hexdigest(), data=base64.b64encode(data).decode())


@pytest.fixture
def relay():
    cfg = SimpleNamespace(app_server=SimpleNamespace(local_socket='/fixture/provider.sock', native_socket=None))
    return NodeProviderRelay(cfg, 'air')


SCOPE = SharingScope(user_scope='alice', execute_tasks=True, projects=('/fixture',))


def test_submission_is_sealed_before_io_and_duplicate_final_chunk_never_replays(relay, monkeypatch):
    calls = []
    monkeypatch.setattr(relay, 'exchange', lambda context, body: calls.append(body) or {'raw_provider': True})
    request = envelope(dict(method='thread/read', params=dict(threadId='exact')))
    reply = relay.handle(request, SCOPE)
    assert reply['accepted'] is True
    with pytest.raises(NodeProtocolError) as error:
        relay.handle(request, SCOPE)
    assert error.value.code == 'PROVIDER_REQUEST_IDENTITY_CONFLICT'
    assert len(calls) == 1
    read = dict(operation='provider.read', context=request['context'], request_id=request['request_id'], index=0)
    result = relay.handle(read, SCOPE)
    assert hashlib.sha256(base64.b64decode(result['data'])).hexdigest() == result['digest']
    assert json.loads(base64.b64decode(result['data'])) == {'result': {'raw_provider': True}}
    with pytest.raises(NodeProtocolError):
        relay.handle(request, SCOPE)
    assert len(calls) == 1


def test_concurrent_duplicate_request_runs_provider_at_most_once(relay, monkeypatch):
    calls = []
    monkeypatch.setattr(relay, 'exchange', lambda context, body: calls.append(body) or {})
    request = envelope(dict(method='thread/read', params={}))
    def send(_):
        try:
            return relay.handle(request, SCOPE)
        except NodeProtocolError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(send, range(8)))
    assert sum(isinstance(r, dict) and r.get('accepted') is True for r in results) == 1
    assert len(calls) == 1


@pytest.mark.parametrize('field,value', [('host', 'other'), ('user_scope', 'bob'), ('cwd', '/another-project')])
def test_scope_conflicts_are_rejected_before_provider(relay, monkeypatch, field, value):
    monkeypatch.setattr(relay, 'exchange', lambda *args: pytest.fail('scope conflict reached Provider'))
    request = envelope({})
    request['context'][field] = value
    with pytest.raises(NodeProtocolError) as error:
        relay.handle(request, SCOPE)
    assert error.value.code == 'PROVIDER_SCOPE_DENIED'
    assert not relay.buffers


def test_chunk_digest_and_concurrent_payload_identity_are_not_truncated(relay, monkeypatch):
    calls = []
    monkeypatch.setattr(relay, 'exchange', lambda context, body: calls.append(body) or {})
    body = dict(method='turn/start', params=dict(input='x' * (CHUNK + 100)))
    wire = encoded(body)
    request = envelope(body)
    request.update(count=2, data=base64.b64encode(wire[:CHUNK]).decode())
    assert relay.handle(request, SCOPE)['next_index'] == 1
    conflict = dict(request, index=1, digest='0'*64, data=base64.b64encode(wire[CHUNK:]).decode())
    with pytest.raises(NodeProtocolError) as error:
        relay.handle(conflict, SCOPE)
    assert error.value.code == 'PROVIDER_REQUEST_IDENTITY_CONFLICT'
    request.update(index=1, data=base64.b64encode(wire[CHUNK:]).decode())
    assert relay.handle(request, SCOPE)['accepted'] is True
    assert calls == [body]


@pytest.mark.parametrize('method,params,context,code', [
    ('turn/start', dict(threadId='wrong'), dict(mode='dispatch', thread_id='exact'), 'PROVIDER_THREAD_IDENTITY_CONFLICT'),
    ('turn/start', dict(threadId='exact'), dict(mode='read', thread_id='exact'), 'PROVIDER_INPUT_IDENTITY_CONFLICT'),
    ('turn/interrupt', dict(threadId='exact', turnId='wrong'), dict(mode='cancel', thread_id='exact', turn_id='turn'), 'PROVIDER_CANCEL_DENIED'),
    ('command/exec', dict(command=['rm']), dict(mode='dispatch'), 'PROVIDER_METHOD_DENIED'),
    ('thread/resume', dict(threadId='exact', excludeTurns=True), dict(mode='read', thread_id='exact'), 'PROVIDER_RESUME_DENIED'),
])
def test_protocol_guards_do_not_open_provider_on_identity_conflict(relay, monkeypatch, method, params, context, code):
    import clinx.provider.native_provider as native
    monkeypatch.setattr(native, 'existing_client', lambda *a, **kw: pytest.fail('identity conflict opened Provider'))
    with pytest.raises(NodeProtocolError) as error:
        relay.exchange(dict(host='air', cwd='/fixture', **context), dict(method=method, params=params))
    assert error.value.code == code
