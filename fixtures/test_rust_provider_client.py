"""Rust compatibility client; isolated sockets only, no daemon or real task."""
import base64
import hashlib
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from clinx.provider.app_server import (
    AppServerProtocolError, AppServerRemoteError, AppServerSharedWriterError,
    AppServerTransportError, CodexAppServerClient, UnixSocketTransport,
    transport_lifecycle_state,
)
from fixtures.test_native_transport_binary import read_exact, read_message, send_message


class Scripted:
    def __init__(self, reply):
        self.reply, self.sent, self.responses, self.marks = reply, [], [], []

    def send(self, request):
        self.sent.append(request)
        self.responses.extend(self.reply(request))

    def receive(self, timeout):
        if self.responses:
            value = self.responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        raise AppServerTransportError('fixture receive timed out')

    def mark_request_result(self, ident, outcome):
        self.marks.append((ident, outcome))

    def close(self):
        pass


class Provider:
    def __init__(self, path, reply):
        self.path, self.reply, self.calls, self.errors = path, reply, [], []
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(path))
        self.listener.listen(1)
        self.listener.settimeout(3)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        try:
            peer, _ = self.listener.accept()
            with peer:
                peer.settimeout(3)
                header = b''
                while not header.endswith(b'\r\n\r\n'):
                    header += read_exact(peer, 1)
                key = next(line.split(':', 1)[1].strip() for line in header.decode().splitlines()
                           if line.startswith('Sec-WebSocket-Key:'))
                accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
                peer.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
                while True:
                    message = read_message(peer)
                    self.calls.append(message)
                    values = self.reply(message)
                    if values is None:
                        return
                    for value in values:
                        send_message(peer, value)
        except (OSError, EOFError):
            pass
        except BaseException as exc:
            self.errors.append(exc)

    def close(self):
        self.listener.close()
        self.thread.join(5)
        assert not self.thread.is_alive()
        assert not self.errors


def test_native_rust_transport_initializes_once_preserves_identity_and_peer_uid(tmp_path):
    def reply(request):
        if request['method'] == 'initialize':
            return [dict(id=request['id'], result={'userAgent': 'fixture'})]
        if request['method'] == 'initialized':
            return []
        return [dict(id=request['id'], method='item/tool/requestUserInput', params={}),
                dict(id='another-request', result={'wrong': True}),
                dict(id=request['id'], result={'data': ['row'] * 12000})]
    provider = Provider(tmp_path / 'native.sock', reply)
    try:
        with CodexAppServerClient(UnixSocketTransport(provider.path), timeout_seconds=1) as client:
            assert provider.calls == []
            assert client.transport.local_peer_uid() == os.getuid()
            assert client.initialize(client_name='exact-name', client_title='Exact', client_version='1').user_agent == 'fixture'
            result = client._request('model/list', {})
            assert len(result['data']) == 12000
            assert client.received_events[0]['method'] == 'item/tool/requestUserInput'
            assert len(provider.calls) == 3
            assert provider.calls[0]['params']['clientInfo']['name'] == 'exact-name'
            assert transport_lifecycle_state(client.transport) == 'available'
    finally:
        provider.close()


def test_native_unknown_delivery_consumes_creation_and_never_replays(tmp_path):
    provider = Provider(tmp_path / 'unknown.sock', lambda request: None)
    try:
        with CodexAppServerClient(UnixSocketTransport(provider.path), timeout_seconds=.1) as client:
            client.shared_native_writer, client.native_creation_cwd = True, '/approved'
            request = dict(cwd='/approved', ephemeral=False, threadSource='user')
            with pytest.raises(AppServerTransportError):
                client._request('thread/start', request)
            assert client.native_creation_cwd is None
            assert transport_lifecycle_state(client.transport) == 'unavailable'
            with pytest.raises(AppServerSharedWriterError):
                client._request('thread/start', request)
            assert [r['method'] for r in provider.calls] == ['thread/start']
    finally:
        provider.close()


@pytest.mark.parametrize('policy,method,params,error', [
    ({'read_only_observer': True}, 'thread/resume', {'threadId': 't'}, AppServerProtocolError),
    ({'read_only_observer': True}, 'thread/list', {}, AppServerProtocolError),
    ({'read_only_observer': True}, 'command/exec', {'command': ['unsafe']}, AppServerProtocolError),
    ({'shared_native_writer': True}, 'turn/start', {'threadId': 't'}, AppServerSharedWriterError),
    ({'shared_native_writer': True, 'native_forwarding_thread_id': 'other'}, 'thread/resume', {'threadId': 't'}, AppServerSharedWriterError),
    ({'shared_native_writer': True, 'native_creation_cwd': '/approved'}, 'thread/start', {'cwd': '/elsewhere', 'ephemeral': False, 'threadSource': 'user'}, AppServerSharedWriterError),
])
def test_rust_admission_rejects_before_transport(policy, method, params, error):
    transport = Scripted(lambda request: [])
    client = CodexAppServerClient(transport)
    client.__dict__.update(policy)
    with pytest.raises(error):
        client._request(method, params)
    assert transport.sent == []


def test_exact_metadata_exception_and_index_read_preserve_read_only_scope():
    transport = Scripted(lambda r: [dict(id=r['id'], result=None)])
    client = CodexAppServerClient(transport)
    client.read_only_observer = True
    client._workspace_metadata_request = {'command': ['fixed', 'probe']}
    assert client._request('command/exec', client._workspace_metadata_request) is None
    assert client._request('thread/list', {'useStateDbOnly': True}) is None
    with pytest.raises(AppServerProtocolError):
        client._request('command/exec', {'command': ['fixed', 'other']})
    assert len(transport.sent) == 2


def test_rpc_preserves_remote_error_and_explicit_result_mark_without_retry():
    error = dict(code=-32601, message='exact unsupported method', data={'scope': 'fixture'})
    transport = Scripted(lambda r: [dict(id=r['id'], error=error)])
    client = CodexAppServerClient(transport)
    with pytest.raises(AppServerRemoteError) as raised:
        client._request('thread/turns/list', {'threadId': 'exact'})
    assert raised.value.method == 'thread/turns/list' and raised.value.error == error
    assert transport.marks == [(transport.sent[0]['id'], 'explicit_rejection')]
    assert len(transport.sent) == 1


def test_rpc_missing_result_is_unknown_and_never_retried():
    transport = Scripted(lambda r: [dict(id=r['id'])])
    with pytest.raises(AppServerProtocolError, match='no result or error'):
        CodexAppServerClient(transport)._request('turn/start', {})
    assert transport.marks == [(transport.sent[0]['id'], 'unknown')]
    assert len(transport.sent) == 1


def test_request_events_are_bounded_overflow_explicit_and_tools_unanswered():
    def reply(request):
        return [dict(method='event/' + str(i), params={}) for i in range(8)] + [dict(id=request['id'], result={})]
    transport = Scripted(reply)
    client = CodexAppServerClient(transport, max_received_events=3, max_method_history=2)
    client._request('model/list')
    assert len(client.received_events) == 3
    assert client.events == ['event/6', 'event/7']
    assert client.drain_events() == [{'method': 'clinx/observation_overflow', 'params': {'dropped': 'bounded_event_buffer_exhausted'}}]
    assert len(transport.sent) == 1


def test_drain_partial_events_preserves_original_transport_error():
    transport = Scripted(lambda _: [])
    event = dict(id='approval', method='approval/request', params={})
    failure = AppServerTransportError('connection closed')
    transport.responses = [event, failure]
    client = CodexAppServerClient(transport)
    with pytest.raises(AppServerTransportError) as raised:
        client.drain_events(timeout_seconds=.1)
    assert raised.value is failure
    assert raised.value.events == (event,)
    assert client.received_events == []
    assert transport.sent == []


def test_nonblocking_empty_drain_is_not_connection_loss():
    client = CodexAppServerClient(Scripted(lambda _: []))
    assert client.drain_events(timeout_seconds=0) == []
    for bad in (False, 0, 101):
        with pytest.raises(AppServerProtocolError):
            client.drain_events(max_events=bad)


@pytest.mark.parametrize('mismatch', ['threadId', 'turnId', 'clientId', 'text'])
def test_native_input_confirmation_requires_all_exact_evidence(mismatch):
    item = dict(id='item', type='userMessage', clientId='client', content=[dict(type='text', text='prompt')])
    params = dict(threadId='thread', turnId='turn', item=item)
    if mismatch in params:
        params[mismatch] = 'wrong'
    elif mismatch == 'clientId':
        item['clientId'] = 'wrong'
    else:
        item['content'][0]['text'] = 'wrong'
    transport = Scripted(lambda _: [])
    transport.responses = [dict(method='item/completed', params=params)]
    client = CodexAppServerClient(transport, timeout_seconds=.02)
    with pytest.raises(AppServerTransportError):
        client._confirm_native_input('thread', {'id': 'turn'}, 'client', 'prompt')
    assert not hasattr(client, 'native_forwarding_receipt')
    assert transport.sent == []


def test_native_input_confirmation_records_exact_item_without_sending():
    transport = Scripted(lambda _: [])
    client = CodexAppServerClient(transport)
    item = dict(id='native-item', type='userMessage', clientId='client', content=[dict(type='text', text='prompt')])
    client._confirm_native_input('thread', {'id': 'turn', 'items': [item]}, 'client', 'prompt')
    assert client.native_forwarding_receipt == dict(mode='NATIVE_INPUT_FORWARDING', exclusive_writer=False,
        thread_id='thread', turn_id='turn', client_message_id='client', native_item_id='native-item', input_confirmed=True)
    assert transport.sent == []


def test_concurrent_clients_cannot_consume_each_others_response(tmp_path):
    providers = [Provider(tmp_path / f'{i}.sock', lambda r: [dict(id=r['id'], result=r['params'])]) for i in range(4)]
    def read(i):
        with CodexAppServerClient(UnixSocketTransport(providers[i].path)) as client:
            return client._request('thread/read', {'threadId': str(i)})
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert list(pool.map(read, range(4))) == [{'threadId': str(i)} for i in range(4)]
    finally:
        for provider in providers:
            provider.close()


def config(*providers):
    from types import SimpleNamespace
    return SimpleNamespace(app_server=SimpleNamespace(local_socket=str(providers[0].path),
        native_socket=str(providers[1].path) if len(providers) > 1 else None,
        client_name='observer-fixture', client_title='Observer', client_version='1',
        request_timeout_seconds=1))


def observer_reply(state, turn_status, *, thread='thread', turn='turn'):
    def reply(request):
        method = request['method']
        if method == 'initialized':
            return []
        if method == 'initialize':
            result = {}
        elif method == 'thread/read':
            result = {'thread': dict(id=thread, status={'type': state}, sessionId='session', cwd='/approved')}
        elif method == 'thread/turns/list':
            result = {'data': [dict(id=turn, status=turn_status)], 'nextCursor': None}
        else:
            raise AssertionError('observer tried to mutate: ' + method)
        return [dict(id=request['id'], result=result)]
    return reply


def test_rust_observer_prefers_exact_live_owner_over_unloaded_interrupted(tmp_path):
    from clinx.provider.native_provider import observe_execution
    providers = [Provider(tmp_path / 'managed.sock', observer_reply('notLoaded', 'interrupted')),
                 Provider(tmp_path / 'native.sock', observer_reply('active', 'inProgress'))]
    try:
        proof = observe_execution(config(*providers), 'thread', 'turn')
        assert proof['provider_liveness'] == 'LIVE'
        assert proof['owner_endpoint'] == str(providers[1].path)
        assert proof['transport_health'] == 'HEALTHY' and proof['release_safe'] is False
        assert all([r['method'] for r in p.calls] == ['initialize', 'initialized', 'thread/read', 'thread/turns/list'] for p in providers)
    finally:
        for provider in providers:
            provider.close()


def test_rust_observer_wrong_turn_active_claim_never_proves_completion(tmp_path):
    from clinx.provider.native_provider import observe_execution
    providers = [Provider(tmp_path / 'other.sock', observer_reply('active', 'inProgress', turn='other')),
                 Provider(tmp_path / 'terminal.sock', observer_reply('idle', 'completed'))]
    try:
        proof = observe_execution(config(*providers), 'thread', 'turn')
        assert proof['provider_liveness'] == 'UNKNOWN'
        assert proof['liveness_reason'] == 'ACTIVE_OWNER_EXACT_TURN_UNCONFIRMED'
        assert proof['owner_endpoint'] is None and not proof['release_safe']
    finally:
        for provider in providers:
            provider.close()


def test_rust_thread_observer_missing_socket_and_wrong_identity_remain_unknown(tmp_path):
    from clinx.provider.native_provider import observe_thread
    provider = Provider(tmp_path / 'wrong.sock', observer_reply('idle', 'completed', thread='another'))
    cfg = config(provider)
    cfg.app_server.native_socket = str(tmp_path / 'absent.sock')
    try:
        proof = observe_thread(cfg, 'thread')
        assert proof['state'] == 'UNKNOWN' and proof['owner_endpoint'] is None
        assert proof['observations'][0]['reason'] == 'PROVIDER_READ_UNAVAILABLE'
        assert proof['observations'][1]['state'] == 'OFFLINE'
        assert proof['observations'][1]['reason'] == 'SOCKET_NOT_PRESENT'
        assert [r['method'] for r in provider.calls] == ['initialize', 'initialized', 'thread/read']
    finally:
        provider.close()


@pytest.mark.parametrize('statuses,expected,safe', [
    (('notLoaded', 'unloaded'), 'EXACT_COMPLETED_TURN_UNLOADED', True),
    (('active', 'active'), 'OWNERSHIP_CONFLICT', False),
    (('idle', 'UNKNOWN'), 'EXACT_OWNER_TERMINAL', False),
])
def test_rust_liveness_conflict_and_release_rules(statuses, expected, safe):
    from clinx.provider.liveness import classify
    rows = [dict(endpoint=str(i), state=state, thread_id='t', turn_id='turn',
        turn_status='inProgress' if state == 'active' else 'completed') for i, state in enumerate(statuses)]
    proof = classify(rows, 't', 'turn')
    assert proof['liveness_reason'] == expected
    assert proof['release_safe'] is safe


def test_full_unix_backlog_has_bounded_connect_and_no_provider_request(tmp_path):
    import time
    listener = socket.socket(socket.AF_UNIX)
    path = tmp_path / 'full.sock'
    listener.bind(str(path))
    listener.listen(0)
    held = socket.socket(socket.AF_UNIX)
    held.connect(str(path))
    try:
        start = time.monotonic()
        with pytest.raises(AppServerTransportError):
            UnixSocketTransport(path, timeout_seconds=.05).connect()
        assert time.monotonic() - start < 1
        peer, _ = listener.accept()
        peer.setblocking(False)
        try:
            with pytest.raises(BlockingIOError):
                peer.recv(1)
        finally:
            peer.close()
    finally:
        held.close()
        listener.close()


def test_existing_long_timeout_and_truthy_read_only_guard_are_preserved():
    transport = Scripted(lambda r: [dict(id=r['id'], result={'accepted': True})])
    client = CodexAppServerClient(transport, timeout_seconds=300)
    assert client._request('thread/read', {'threadId': 'thread'}) == {'accepted': True}
    client.read_only_observer = 1
    with pytest.raises(AppServerProtocolError):
        client._request('turn/start', {'threadId': 'thread'})
    assert len(transport.sent) == 1


def test_rust_peer_credentials_failure_never_falls_back_to_legacy_socket(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from clinx.provider import native_provider, native_writer_probe
    from clinx.authority import rust_worker_identity
    transport = UnixSocketTransport('/unused-qualification-socket')
    transport.local_peer_uid = Mock(side_effect=AppServerTransportError('PROVIDER_PEER_IDENTITY_CONFLICT'))
    transport._byte_transport.sock = Mock()
    client = Mock()
    client.transport = transport
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(native_provider, 'existing_client', lambda *a, **kw: client)
    monkeypatch.setattr(rust_worker_identity, 'worker_account', lambda: SimpleNamespace(pw_uid=1001))
    result = native_writer_probe.observe('/desktop/socket', '/nonexistent/qualification-native', 'thread', 'turn', '/project')
    assert result['liveness'] == 'UNKNOWN'
    transport._byte_transport.sock.getsockopt.assert_not_called()
    client._request.assert_not_called()
