"""Independent Python/OpenSSL TLS and certificate compatibility oracles.

Rust process/ledger coverage: kernel/tests/node_network_qualification.rs.
"""
import json
import socket
import ssl
import threading

import pytest
from clinx.authority.rust_authority import RustAuthorityError
from clinx.nodes.provider_relay import encoded
from fixtures import test_node_authority_route as route_fixtures

route = route_fixtures.route

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
