"""Compatibility IPC uses the real Rust socket, exact identity and a single send."""
import json
import os
import socket
import threading

import pytest

from clinx.authority.rust_authority import Commands, RustAuthorityClient, RustAuthorityError, PROTOCOL


@pytest.mark.parametrize('reply', ['valid', 'lost', 'foreign', 'both', 'remote'])
def test_rust_authority_ipc_ack_identity_and_no_replay(tmp_path, reply):
    endpoint = tmp_path / 'a.sock'
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(endpoint))
    endpoint.chmod(0o600)
    server.listen()
    calls = []

    def serve():
        with server.accept()[0] as peer:
            with peer.makefile('rb') as stream:
                request = json.loads(stream.readline())
            calls.append(request)
            response = dict(protocol_version=PROTOCOL, request_id=request['request_id'], result={'received': True})
            if reply == 'lost':
                return
            if reply == 'foreign':
                response['request_id'] = 'wrong'
            if reply == 'both':
                response['error'] = 'CONFLICT'
            if reply == 'remote':
                response.pop('result')
                response['error'] = 'EXACT_CONFLICT'
            peer.sendall(json.dumps(response).encode() + b'\n')

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        client = Commands(RustAuthorityClient(endpoint, os.getuid(), timeout=1))
        if reply == 'valid':
            assert client.call('start', {'value': '资格'}, key='original') == {'received': True}
        else:
            with pytest.raises(RustAuthorityError, match='EXACT_CONFLICT' if reply == 'remote' else 'DELIVERY_UNKNOWN') as raised:
                client.call('start', {'value': '资格'}, key='original')
            assert raised.value.operation_id == 'original'
        thread.join(2)
        assert len(calls) == 1
        assert calls[0]['request_id'] == 'original'
        assert calls[0]['operation'] == 'business.start'
        assert calls[0]['payload'] == {'value': '资格'}
    finally:
        server.close()
        thread.join(2)


@pytest.mark.parametrize('operation,request_id,code', [
    ('execute_arbitrary', 'a', 'UNSUPPORTED_OPERATION'),
    ('health', '', 'INVALID_REQUEST_ID'),
    ('health', 'a' * 129, 'INVALID_REQUEST_ID'),
])
def test_validation_precedes_endpoint_io(tmp_path, operation, request_id, code):
    with pytest.raises(RustAuthorityError, match=code):
        RustAuthorityClient(tmp_path / 'missing', os.getuid()).request(operation, {}, request_id=request_id)


def test_endpoint_identity_remains_local_linux_contract(tmp_path):
    endpoint = tmp_path / 'a.sock'
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(endpoint))
        endpoint.chmod(0o600)
        link = tmp_path / 'alias.sock'
        link.symlink_to(endpoint)
        for path, uid in [(link, os.getuid()), (endpoint, os.getuid() + 1)]:
            with pytest.raises(RustAuthorityError, match='AUTHORITY_ENDPOINT_UNTRUSTED'):
                RustAuthorityClient(path, uid).request('health', {}, request_id='health')


def test_full_unix_backlog_has_bounded_pre_send_failure(tmp_path):
    import time
    endpoint = tmp_path / 'full.sock'
    with socket.socket(socket.AF_UNIX) as server, socket.socket(socket.AF_UNIX) as occupied:
        server.bind(str(endpoint))
        endpoint.chmod(0o600)
        server.listen(0)
        occupied.connect(str(endpoint))
        started = time.monotonic()
        with pytest.raises(RustAuthorityError, match='AUTHORITY_UNAVAILABLE'):
            RustAuthorityClient(endpoint, os.getuid(), timeout=.05).request('health', {}, request_id='bounded')
        assert time.monotonic() - started < 1
        with server.accept()[0] as peer:
            peer.settimeout(.05)
            with pytest.raises(socket.timeout):
                peer.recv(1)
