"""Python Provider binding smoke and shared private WebSocket fixture.

Rust owns the full request/IO/evidence qualification matrix in
kernel/tests/provider_client_qualification.rs. These cases retain exact
Python exception classes/objects, mutable receipt/creation attributes and
the injected legacy socket boundary."""

import base64
import hashlib
import socket
import threading
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


def test_rpc_preserves_remote_error_and_explicit_result_mark_without_retry():
    error = dict(code=-32601, message='exact unsupported method', data={'scope': 'fixture'})
    transport = Scripted(lambda r: [dict(id=r['id'], error=error)])
    client = CodexAppServerClient(transport)
    with pytest.raises(AppServerRemoteError) as raised:
        client._request('thread/turns/list', {'threadId': 'exact'})
    assert raised.value.method == 'thread/turns/list' and raised.value.error == error
    assert transport.marks == [(transport.sent[0]['id'], 'explicit_rejection')]
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


def test_native_input_confirmation_records_exact_item_without_sending():
    transport = Scripted(lambda _: [])
    client = CodexAppServerClient(transport)
    item = dict(id='native-item', type='userMessage', clientId='client', content=[dict(type='text', text='prompt')])
    client._confirm_native_input('thread', {'id': 'turn', 'items': [item]}, 'client', 'prompt')
    assert client.native_forwarding_receipt == dict(mode='NATIVE_INPUT_FORWARDING', exclusive_writer=False,
        thread_id='thread', turn_id='turn', client_message_id='client', native_item_id='native-item', input_confirmed=True)
    assert transport.sent == []


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


def test_provider_initialization_keeps_public_dto():
    transport = Scripted(lambda request: [dict(id=request['id'], result={'userAgent': 'fixture'})]
                         if request['method'] == 'initialize' else [])
    client = CodexAppServerClient(transport)
    result = client.initialize(client_name='exact-name', client_title='Exact', client_version='1')
    assert result.user_agent == 'fixture'
    assert [request['method'] for request in transport.sent] == ['initialize', 'initialized']


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
