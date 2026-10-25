"""Real paired mTLS reverse route; gateway supplies identity, Rust owns state."""
from types import SimpleNamespace

import pytest

from clinx.authority.rust_authority import RustAuthorityError
from clinx.authority.rust_authority import Commands
from clinx.discovery.discovery import Candidate
from clinx.discovery.identity import NodeIdentity, PrivateStore, TrustedPeerStore
from clinx.discovery.transport import DeviceServer, LanTransport
from clinx.nodes.node_protocol import NodeAuthorizationStore, NodeRegistry, SharingScope
from clinx.nodes.node_runtime import CentreService, NodeAuthorityClient, NodeRegistrationClient, PairedTLS


@pytest.fixture
def route(tmp_path):
    node = NodeIdentity(PrivateStore(tmp_path / 'air'), 'air')
    centre = NodeIdentity(PrivateStore(tmp_path / 'centre'), 'centre')
    np, cp = TrustedPeerStore(node.store), TrustedPeerStore(centre.store)
    with DeviceServer(centre, cp, address='127.0.0.1') as server:
        candidate = Candidate('centre', 'centre', centre.public['fingerprint'], (('127.0.0.1', server.port),))
        LanTransport(node, np).pair(candidate, server.window.open())
    scope = SharingScope(user_scope='alice', execute_tasks=True, projects=('/fixture/repo',))
    approvals = NodeAuthorizationStore(centre.store.root)
    approvals.grant('air', user_scope='alice', execute_tasks=True, projects=('/fixture/repo',))
    registry = NodeRegistry(tmp_path / 'registry.sqlite3')
    calls = []

    def call(op, payload):
        calls.append((op, payload))
        return {'accepted_by_fixture': True}

    service = CentreService(PairedTLS(centre, cp), registry, approvals, authority=SimpleNamespace(call=call))
    server = service.server(address='127.0.0.1', port=0)
    registration = NodeRegistrationClient(PairedTLS(node, np), 'centre', f'tls://127.0.0.1:{server.address[1]}')
    assert registration.register(node, scope, port=12345, capabilities=())['registered']
    try:
        yield SimpleNamespace(client=NodeAuthorityClient(registration, scope), calls=calls, approvals=approvals,
            registration=registration, node=node, scope=scope, service=service)
    finally:
        server.close()
        registry.close()


def test_reverse_gateway_uses_authenticated_scope_and_preserves_request_id(route):
    client = Commands(route.client)
    assert client.call('prepare', {'prompt': 'intent'}, key='exact-id') == {'accepted_by_fixture': True}
    op, envelope = route.calls[0]
    assert op == 'node_request'
    assert envelope == dict(scope=dict(principal=dict(node_id='air', user_scope='alice',
        fingerprint=route.node.public['fingerprint']), projects=['/fixture/repo']),
        operation='prepare', request_id='exact-id', payload={'prompt': 'intent'})
    wire = dict(operation='authority.request', protocol_version='clinx-node-v1', node_id='air',
        sharing_scope=route.scope.as_dict(), request_id='another-id', command='read_receipt', payload={},
        scope={'principal': {'node_id': 'victim', 'user_scope': 'bob'}}, principal={'node_id': 'victim'})
    remote = route.registration.tls.client('centre', route.registration.endpoint)
    assert remote.call(wire)['result']
    assert route.calls[-1][1]['scope'] == envelope['scope']
    wire['node_id'] = 'victim'
    assert remote.call(wire)['error_code'] == 'NODE_IDENTITY_MISMATCH'
    assert len(route.calls) == 2


def test_revocation_and_user_conflict_prevent_forwarding(route):
    route.approvals.grant('air', user_scope='bob', execute_tasks=True)
    with pytest.raises(RustAuthorityError, match='USER_SCOPE_DENIED'):
        Commands(route.client).call('list_tasks', {})
    route.approvals.revoke('air', user_scope='alice')
    with pytest.raises(RustAuthorityError, match='SHARING_SCOPE_DENIED'):
        Commands(route.client).call('list_tasks', {})
    assert not route.calls


def test_lost_ack_is_not_retried_and_keeps_receipt_key(route, monkeypatch):
    sent = []
    def lost(request):
        sent.append(request)
        raise TimeoutError('reply lost')
    monkeypatch.setattr(route.registration.tls, 'client', lambda *a, **kw: SimpleNamespace(call=lost))
    with pytest.raises(RustAuthorityError, match='DELIVERY_UNKNOWN') as error:
        Commands(route.client).call('start', {}, key='recover-this-id')
    assert error.value.operation_id == 'recover-this-id'
    assert len(sent) == 1 and sent[0]['request_id'] == 'recover-this-id'


def test_frame_limit_rejects_before_send(route):
    with pytest.raises(RustAuthorityError, match='REQUEST_TOO_LARGE'):
        Commands(route.client).call('prepare', {'prompt': 'x' * (256 * 1024)})
    assert not route.calls
