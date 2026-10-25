"""Real Node Rust workers, existing OpenSSL wire, and durable request uncertainty."""
import base64
import hashlib
import json
import os
import socket
import sqlite3
import ssl
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from clinx.authority.rust_authority import RustAuthorityError
from clinx.nodes.node_protocol import (
    NodeProtocolError,
    NodeRecord,
    NodeRegistry,
    NodeService,
    SessionIdentity,
    SharingScope,
)
from clinx.nodes.provider_relay import NodeProviderRelay, encoded
from fixtures import test_node_authority_route as route_fixtures
from fixtures.test_provider_relay import envelope

route = route_fixtures.route


def test_release_node_identifies_its_network_contract():
    binary = Path(os.environ['CLINX_TEST_RUST_BIN_DIR']) / 'clinx-node'
    result = subprocess.run([str(binary), '--network-contract'], capture_output=True, timeout=5, check=True)
    assert result.stdout == b'RUST_NODE_NETWORK_V1\n' and not result.stderr


def test_rust_listener_can_rebind_after_process_restart_without_replaying_requests():
    from clinx.nodes.node_protocol import NodeRPCClient, NodeRPCServer

    calls = []
    service = SimpleNamespace(handle=lambda request: calls.append(request) or {'read_only': True})
    first = NodeRPCServer(service, port=0, allow_insecure_loopback=True)
    address = first.address
    try:
        assert NodeRPCClient(address, allow_insecure_loopback=True).call({'operation': 'node.status'}) == {'read_only': True}
    finally:
        first.close()
    replacement = NodeRPCServer(service, address=address[0], port=address[1], allow_insecure_loopback=True)
    try:
        assert len(calls) == 1
        assert NodeRPCClient(address, allow_insecure_loopback=True).call({'operation': 'node.status'}) == {'read_only': True}
        assert len(calls) == 2
    finally:
        replacement.close()


def openssl(identity, peer, *, server=False):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
    if not server:
        context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    identity.load_tls_credentials(context)
    context.load_verify_locations(cadata=peer.certificate)
    return context


@pytest.mark.parametrize('terminator', [b'\n', b''])
def test_existing_openssl_and_rust_node_are_bidirectionally_wire_compatible(route, terminator):
    centre = route.service.tls.identity
    ctx = openssl(route.node, centre)
    address = route.registration.endpoint.removeprefix('tls://').rsplit(':', 1)
    with socket.create_connection((address[0], int(address[1])), timeout=3) as raw:
        with ctx.wrap_socket(raw, server_hostname='ignored-existing-check-hostname-false') as peer:
            request = dict(operation='authority.request', protocol_version='clinx-node-v1', node_id='air',
                sharing_scope=route.scope.as_dict(), request_id='openssl-to-rust', command='read_receipt', payload={})
            peer.sendall(encoded(request) + b'\n')
            reply = json.loads(peer.makefile('rb').readline())
            assert reply == {'request_id': 'openssl-to-rust', 'result': {'accepted_by_fixture': True}}
    calls, errors = [], []
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(5)
        server_context = openssl(centre, route.node, server=True)
        def serve():
            try:
                raw, _ = listener.accept()
                with raw, server_context.wrap_socket(raw, server_side=True) as stream:
                    calls.append(json.loads(stream.makefile('rb').readline()))
                    stream.sendall(b'{"old_openssl_server":true}' + terminator)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=serve)
        thread.start()
        result = route.registration.tls.client('centre', f'tls://127.0.0.1:{listener.getsockname()[1]}').call({'operation': 'node.status'})
        thread.join(timeout=6)
        assert not errors and not thread.is_alive()
        assert result == {'old_openssl_server': True}
        assert calls == [{'operation': 'node.status'}]


def test_rust_listener_reloads_revoked_trust_without_restart(route):
    # Revoke on the server; the already-built listener cannot cache this grant.
    route.service.tls.peers.revoke('air')
    client = route.registration.tls.client('centre', route.registration.endpoint)
    with pytest.raises((TimeoutError, OSError)):
        client.call(dict(operation='authority.request', protocol_version='clinx-node-v1', node_id='air',
            sharing_scope=route.scope.as_dict(), request_id='revoked', command='start', payload={}))
    assert not route.calls


def test_expired_certificate_is_rejected_before_dispatch(route):
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    original = x509.load_pem_x509_certificate(route.node.certificate.encode())
    key = serialization.load_pem_private_key(route.node._private_pem.encode(), password=None)
    now = dt.datetime.now(dt.timezone.utc)
    expired = (x509.CertificateBuilder().subject_name(original.subject).issuer_name(original.issuer)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=2)).not_valid_after(now - dt.timedelta(days=1))
        .sign(key, algorithm=None))
    store = route.service.tls.identity.store
    with store.lock():
        peers = store.read('peers.json')
        peers['peers']['air']['certificate'] = expired.public_bytes(serialization.Encoding.PEM).decode()
        store.write('peers.json', peers)
    with pytest.raises(RustAuthorityError, match='DELIVERY_UNKNOWN'):
        route.client.request('business.start', {}, request_id='expired')
    assert not route.calls


def test_historical_non_ascii_float_request_digest_survives_rust_reopen(tmp_path):
    path = tmp_path / 'existing.sqlite3'
    payload = {'prompt': '准确🙂', 'weight': 1e-7, 'zero': -0.0}
    # Reproduce the deployed Python ledger, including ensure_ascii and float bytes.
    wire = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    with sqlite3.connect(path) as old:
        old.execute('CREATE TABLE request_ledger (node_id TEXT NOT NULL, request_id TEXT NOT NULL, operation TEXT NOT NULL, payload_hash TEXT NOT NULL, state TEXT NOT NULL, response_json TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(node_id,request_id))')
        old.execute('INSERT INTO request_ledger VALUES (?,?,?,?,?,?,?)',
            ('air', 'old-exact', 'execution.start', hashlib.sha256(wire.encode()).hexdigest(), 'IN_FLIGHT', None, '2026-10-25T00:00:00+00:00'))
    registry = NodeRegistry(path)
    try:
        assert registry.claim_request(node_id='air', request_id='old-exact', operation='execution.start', payload=payload) == ('IN_FLIGHT', None)
        with pytest.raises(NodeProtocolError, match='contract rejected') as error:
            registry.claim_request(node_id='air', request_id='old-exact', operation='execution.start', payload=dict(payload, prompt='changed'))
        assert error.value.code == 'IDEMPOTENCY_KEY_REUSE'
        registry.finish_request(node_id='air', request_id='old-exact', state='COMPLETED', response={'execution_ref': 'exact'})
    finally:
        registry.close()
    reopened = NodeRegistry(path)
    try:
        assert reopened.claim_request(node_id='air', request_id='old-exact', operation='execution.start', payload=payload) == ('COMPLETED', {'execution_ref': 'exact'})
    finally:
        reopened.close()


def test_distinct_rust_workers_claim_once_and_keep_unknown_after_restart(tmp_path):
    path = tmp_path / 'index.sqlite3'
    first, second = NodeRegistry(path), NodeRegistry(path)
    def claim(i):
        registry = first if i % 2 else second
        return registry.claim_request(node_id='air', request_id='one', operation='execution.start', payload={'execution_ref': 'exec_exact'})[0]
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            states = list(pool.map(claim, range(20)))
        assert states.count('NEW') == 1 and states.count('IN_FLIGHT') == 19
    finally:
        first.close()
        second.close()
    reopened = NodeRegistry(path)
    try:
        assert reopened.claim_request(node_id='air', request_id='one', operation='execution.start', payload={'execution_ref': 'exec_exact'}) == ('IN_FLIGHT', None)
    finally:
        reopened.close()


def test_start_lost_reply_is_reconciled_exactly_without_reexecuting(tmp_path):
    path = tmp_path / 'node.sqlite3'
    registry = NodeRegistry(path)
    request = dict(operation='execution.start', execution_ref='exec_exact', request_id='lost', user_scope='alice')
    registry.claim_request(node_id='air', request_id='lost', operation='execution.start', payload=request)
    registry.close()
    registry = NodeRegistry(path)
    calls = []
    service = NodeService(NodeRecord('air'), registry, scope=SharingScope('alice', execute_tasks=True),
        read_thread=lambda *a: {}, start_execution=lambda **k: pytest.fail('unknown start replayed'),
        status_execution=lambda **k: calls.append(k) or {'execution_ref': 'other'})
    try:
        unknown = service.start(request)
        assert unknown['operation_state'] == 'UNKNOWN'
        service.status_execution = lambda **k: {'execution_ref': 'exec_exact', 'status': 'RUNNING'}
        reconciled = service.start(request)
        assert reconciled['operation_state'] == 'RECONCILED' and reconciled['execution_started'] is False
        assert reconciled['execution_ref'] == 'exec_exact' and len(calls) == 1
    finally:
        registry.close()


def test_concurrent_start_and_cancel_receipts_survive_node_restart(tmp_path):
    path = tmp_path / 'node.sqlite3'
    registry = NodeRegistry(path)
    starts, cancels = [], []
    def service(registry):
        return NodeService(NodeRecord('air'), registry, scope=SharingScope('alice', execute_tasks=True),
            read_thread=lambda *a: {},
            start_execution=lambda **k: starts.append(k) or {'execution_ref': 'exec_exact', 'operation_state': 'STARTED'},
            cancel_execution=lambda **k: cancels.append(k) or {'execution_ref': 'exec_exact', 'operation_state': 'CANCELLED'})
    node = service(registry)
    request = dict(execution_ref='exec_exact', request_id='start-once', user_scope='alice')
    cancel = dict(execution_ref='exec_exact', request_id='cancel-once', user_scope='alice')
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            receipts = list(pool.map(lambda _: node.start(request), range(8)))
        assert len(starts) == 1
        assert all(r.get('operation_state') in {'UNKNOWN', 'STARTED'} for r in receipts)
        assert node.cancel(cancel)['operation_state'] == 'CANCELLED'
        with pytest.raises(NodeProtocolError) as error:
            node.start(dict(request, execution_ref='exec_conflict'))
        assert error.value.code == 'IDEMPOTENCY_KEY_REUSE'
    finally:
        registry.close()
    reopened = NodeRegistry(path)
    try:
        assert service(reopened).cancel(cancel)['idempotent'] is True
        assert service(reopened).start(request)['idempotent'] is True
        assert len(starts) == len(cancels) == 1
    finally:
        reopened.close()


def test_full_wire_budget_unicode_keeps_exact_legacy_ledger_digest(tmp_path):
    registry = NodeRegistry(tmp_path / 'node.sqlite3')
    calls = []
    service = NodeService(NodeRecord('air'), registry, scope=SharingScope('alice', execute_tasks=True),
        read_thread=lambda *a: {}, prepare_execution=lambda **k: calls.append(k) or {'prepared_execution_ref': 'prepared_exact'})
    request = dict(operation='execution.prepare', user_scope='alice', request_id='unicode-budget', prompt='🙂' * 60000)
    assert len(encoded(request)) < 256 * 1024  # Valid deployed Node wire frame.
    try:
        assert service.prepare(request)['prepared_execution_ref'] == 'prepared_exact'
        assert service.prepare(request)['idempotent'] is True
        assert len(calls) == 1 and calls[0]['prompt'] == request['prompt']
        state, receipt = registry.claim_request(node_id='air', request_id='unicode-budget', operation='execution.prepare', payload=request)
        assert state == 'COMPLETED' and receipt == {'prepared_execution_ref': 'prepared_exact'}
    finally:
        registry.close()


def test_independent_callback_fence_conflict_is_durably_blocked_before_execution(tmp_path):
    registry = NodeRegistry(tmp_path / 'node.sqlite3')
    identity = SessionIdentity('air', 'alice', 'codex_app_server', 'thread_exact')
    registry.acquire_fence(identity, 'holder')
    node = NodeService(NodeRecord('air'), registry, scope=SharingScope('alice', execute_tasks=True),
        read_thread=lambda *a: {}, start_execution=lambda **k: pytest.fail('occupied independent fence executed'))
    request = dict(thread_id='thread_exact', execution_ref='exec_other', request_id='fence-conflict', user_scope='alice')
    try:
        result = node.start(request)
        assert result['operation_state'] == 'BLOCKED' and result['side_effect'] == 'NONE'
        assert result['error_code'] == 'SINGLE_WRITER_BUSY' and result['owner_execution'] == 'holder'
        state, receipt = registry.claim_request(node_id='air', request_id='fence-conflict', operation='execution.start', payload=request)
        assert state == 'FAILED' and receipt == result
    finally:
        registry.close()


def test_cancel_receipt_recovery_finishes_interrupted_independent_fence_release(tmp_path):
    path = tmp_path / 'node.sqlite3'
    registry = NodeRegistry(path)
    identity = SessionIdentity('air', 'alice', 'codex_app_server', 'thread_exact')
    request = dict(thread_id='thread_exact', execution_ref='exec_exact', request_id='cancel-crash', user_scope='alice')
    assert registry.acquire_fence(identity, 'exec_exact') == 1
    registry.claim_request(node_id='air', request_id='cancel-crash', operation='execution.cancel', payload=request)
    registry.finish_request(node_id='air', request_id='cancel-crash', state='COMPLETED', response={'operation_state': 'CANCELLED'})
    registry.close()  # Crash after receipt, before the independent callback fence release.
    reopened = NodeRegistry(path)
    node = NodeService(NodeRecord('air'), reopened, scope=SharingScope('alice', execute_tasks=True),
        read_thread=lambda *a: {}, cancel_execution=lambda **k: pytest.fail('cancel replayed after receipt'))
    try:
        assert node.cancel(request) == {'operation_state': 'CANCELLED', 'idempotent': True}
        assert reopened.acquire_fence(identity, 'next') == 2
    finally:
        reopened.close()


def test_receipt_write_failure_after_start_stays_unknown_and_never_replays(tmp_path, monkeypatch):
    registry = NodeRegistry(tmp_path / 'node.sqlite3')
    calls = []
    node = NodeService(NodeRecord('air'), registry, scope=SharingScope('alice', execute_tasks=True),
        read_thread=lambda *a: {}, start_execution=lambda **k: calls.append(k) or {'execution_ref': 'exec_exact'},
        status_execution=lambda **k: {'execution_ref': 'exec_exact', 'status': 'RUNNING'})
    request = dict(operation='execution.start', execution_ref='exec_exact', request_id='receipt-lost', user_scope='alice')
    finish = registry.finish_request
    def unavailable(**kwargs):
        raise NodeProtocolError('NODE_REGISTRY_UNAVAILABLE', 'fixture write failure')
    monkeypatch.setattr(registry, 'finish_request', unavailable)
    try:
        reply = node.handle(request)
        assert reply['operation_state'] == reply['side_effect'] == 'UNKNOWN'
        assert reply['retry'] == 'RECONCILIATION_REQUIRED' and len(calls) == 1
        monkeypatch.setattr(registry, 'finish_request', finish)
        assert node.handle(request)['operation_state'] == 'RECONCILED'
        assert len(calls) == 1
    finally:
        registry.close()


def test_provider_unknown_and_consumed_reply_cannot_be_replayed_after_worker_loss(monkeypatch):
    cfg = SimpleNamespace(app_server=SimpleNamespace(local_socket='/fixture/provider.sock', native_socket=None))
    relay = NodeProviderRelay(cfg, 'air')
    scope = SharingScope('alice', execute_tasks=True, projects=('/fixture',))
    request = envelope(dict(method='thread/read', params={'threadId': 'exact'}))
    calls = []
    def unknown(*args):
        calls.append(args)
        raise TimeoutError('partial send, no evidence')
    monkeypatch.setattr(relay, 'exchange', unknown)
    read = dict(operation='provider.read', request_id=request['request_id'], context=request['context'], index=0)
    try:
        assert relay.handle(request, scope)['accepted'] is True
        reply = relay.handle(read, scope)
        assert json.loads(base64.b64decode(reply['data'])) == {'error': 'PROVIDER_DELIVERY_UNKNOWN'}
        with pytest.raises(NodeProtocolError) as error:
            relay.handle(read, scope)
        assert error.value.code == 'PROVIDER_DELIVERY_UNKNOWN'
        with pytest.raises(NodeProtocolError):
            relay.handle(request, scope)
        assert len(calls) == 1
    finally:
        relay.close()
    replacement = NodeProviderRelay(cfg, 'air')
    try:
        with pytest.raises(NodeProtocolError) as error:
            replacement.handle(read, scope)
        assert error.value.code == 'PROVIDER_REPLY_UNAVAILABLE'
    finally:
        replacement.close()
