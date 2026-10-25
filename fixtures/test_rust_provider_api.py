"""Public compatibility signatures exercise Rust policy, process and codecs."""
import struct
import sys
from unittest.mock import Mock

import pytest

from clinx.provider.app_server import (
    AppServerProtocolError, AppServerRemoteError,
    AppServerTransportError, AppServerWriterConflict, CodexAppServerClient,
    ModelCapabilityError, ProcessStdioTransport, WebSocketStdioTransport,
    extract_version, versions_compatible, transport_lifecycle_state,
)


def test_rust_owned_process_exec_sets_session_and_jsonl_unicode():
    script = ('import sys,json,os; value=json.loads(sys.stdin.readline()); '
              'print(json.dumps(dict(value=value,pid=os.getpid(),sid=os.getsid(0),pgid=os.getpgrp())))')
    transport = ProcessStdioTransport((sys.executable, '-u', '-c', script))
    try:
        transport.connect()
        assert transport._process is None  # Normal Python never owns the child.
        transport.send({'id': 'exact', 'content': '中文'})
        result = transport.receive(2)
        assert result['pid'] == result['sid'] == result['pgid']
        assert result['value'] == {'id': 'exact', 'content': '中文'}
    finally:
        transport.close()
    assert transport_lifecycle_state(transport) == 'unavailable'


def test_rust_process_exec_failure_is_synchronous():
    transport = ProcessStdioTransport(('/missing-clinx-provider-contract-fixture',))
    with pytest.raises(AppServerTransportError, match='failed to start'):
        transport.connect()
    assert transport._session is None


def test_unsubscribe_failure_is_recorded_without_replay_or_active_turn_release():
    transport = Mock()
    client = CodexAppServerClient(transport)
    client._owned_thread_ids = {'idle', 'live'}
    client._owned_turns = {'live': 'running-turn'}
    client._request = Mock(side_effect=AppServerTransportError('ACK lost'))
    client.close()
    client._request.assert_called_once_with('thread/unsubscribe', {'threadId': 'idle'})
    assert client.writer_release == {'idle': 'UNCONFIRMED:AppServerTransportError'}
    assert client._owned_thread_ids == {'live'}
    assert client._owned_turns == {'live': 'running-turn'}
    transport.close.assert_called_once()


@pytest.mark.parametrize('other', [False, True])
def test_writer_conflict_is_exact_and_original_unknown_error_identity_survives(other):
    client = CodexAppServerClient(Mock())
    remote = AppServerRemoteError('thread/resume', {'code': -32600,
        'message': 'thread other already has an active writer' if other else 'thread exact already has an active writer'})
    client._request = Mock(side_effect=remote)
    with pytest.raises(AppServerRemoteError) as caught:
        client.thread_resume('exact')
    if other:
        assert caught.value is remote
    else:
        assert isinstance(caught.value, AppServerWriterConflict)
        assert caught.value.__cause__ is remote
    client._request.assert_called_once_with('thread/resume', {'threadId': 'exact', 'excludeTurns': True})


def test_model_alias_ambiguity_and_malformed_alias_are_not_resolved_by_guessing():
    client = CodexAppServerClient(Mock())
    client.model_list = lambda: {'data': [
        {'id': 'gpt-bad', 'model': 'Terra', 'supportedReasoningEfforts': None},
        {'id': 'gpt-good', 'model': 'Terra', 'supportedReasoningEfforts': ['high']},
        {'id': 'gpt-straße', 'model': 'Straße', 'supportedReasoningEfforts': ['medium']},
    ]}
    with pytest.raises(ModelCapabilityError, match='malformed capability'):
        client.resolve_model('Terra', 'high')
    assert client.resolve_model('STRASSE', 'medium') == ('gpt-straße', 'medium')
    assert client.resolve_model('gpt-good', 'high') == ('gpt-good', 'high')


def test_version_soft_guard_keeps_unbounded_decimal_components():
    huge = '99999999999999999999999999999999999999999999'
    assert extract_version('codex/٣.٢.١') == (3, 2, 1)
    assert not versions_compatible(huge + '.1.0', str(int(huge) + 1) + '.1.0')
    assert versions_compatible(None, '3.0.0')
    assert versions_compatible('3.0.0', '3.999.1')


class InjectedBytes:
    def __init__(self, payload=b''):
        self.pending, self.sent = bytearray(payload), []

    def receive_bytes(self, length, timeout):
        chunk = bytes(self.pending[:length])
        del self.pending[:length]
        if len(chunk) != length:
            raise AppServerTransportError('fixture EOF')
        return chunk

    def send_bytes(self, payload):
        self.sent.append(payload)


def wire(payload):
    transport = object.__new__(WebSocketStdioTransport)
    transport._byte_transport = InjectedBytes(payload)
    transport.timeout_seconds = 1
    return transport


def test_rust_websocket_handles_control_frames_and_exact_fragment_assembly():
    transport = wire(b'\x89\x01?' + b'\x01\x05{"x":' + b'\x80\x02' + b'1}')
    assert transport.receive(1) == {'x': 1}
    pong = transport._byte_transport.sent[0]
    assert pong[:2] == b'\x8a\x81'
    assert pong[-1] ^ pong[2] == ord('?')


@pytest.mark.parametrize('payload,reason', [
    (b'\x80\x00', 'unexpected.*continuation'),
    (b'\x82\x02{}', 'not text'),
    (b'\x81\x02[]', 'JSON object'),
    (b'\x81\x02\xff\xff', 'malformed websocket JSON'),
    (b'\x81\x7f' + struct.pack('!Q', 16 * 1024 * 1024 + 1), 'too large'),
])
def test_rust_websocket_rejects_invalid_provider_evidence(payload, reason):
    with pytest.raises(AppServerProtocolError, match=reason):
        wire(payload).receive(1)


def test_rust_process_websocket_uses_same_codec_without_python_byte_io():
    script = r'''
import sys,base64,hashlib,json,struct
r,w=sys.stdin.buffer,sys.stdout.buffer
header=b''
while not header.endswith(b'\r\n\r\n'): header+=r.read(1)
key=next(line.split(b':',1)[1].strip() for line in header.split(b'\r\n') if line.lower().startswith(b'sec-websocket-key:'))
accept=base64.b64encode(hashlib.sha1(key+b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
w.write(b'HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: '+accept+b'\r\n\r\n');w.flush()
h=r.read(2);assert h[1]&128
size=h[1]&127
if size==126: size=struct.unpack("!H",r.read(2))[0]
elif size==127: size=struct.unpack("!Q",r.read(8))[0]
mask=r.read(4);payload=r.read(size)
value=json.loads(bytes(b^mask[i%4] for i,b in enumerate(payload)))
reply=json.dumps({'id':value['id'],'result':{'thread':{'id':value['params']['threadId']}}}).encode()
w.write(bytes([129,len(reply)])+reply);w.flush()
'''
    transport = WebSocketStdioTransport((sys.executable, '-u', '-c', script))
    with CodexAppServerClient(transport, timeout_seconds=2) as client:
        assert transport._byte_transport._process is None
        assert client.thread_read('exact-thread') == {'id': 'exact-thread'}


def test_concurrent_public_api_returns_merge_distinct_owned_turns():
    from concurrent.futures import ThreadPoolExecutor
    import threading
    client = CodexAppServerClient(Mock())
    ready = threading.Barrier(2)
    def request(method, params):
        ready.wait(timeout=3)
        return {'turn': {'id': params['threadId'] + '-turn'}}
    client._request = request
    with ThreadPoolExecutor(2) as pool:
        turns = list(pool.map(lambda thread: client.turn_start(thread, 'run', cwd='/project'), ['a', 'b']))
    assert {turn.turn_id for turn in turns} == {'a-turn', 'b-turn'}
    assert client._owned_turns == {'a': 'a-turn', 'b': 'b-turn'}
