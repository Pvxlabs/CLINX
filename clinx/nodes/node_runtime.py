"""Production node registration and routing over existing paired mTLS identity.

The centre owns only the existing node index. Native history and canonical
execution remain on their owning node; registration never starts a Provider.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from clinx.discovery.identity import (
    NodeIdentity,
    PrivateStore,
    TrustedPeerStore,
)
from clinx.nodes.node_protocol import (
    NODE_PROTOCOL_VERSION,
    SUPPORTED_PROVIDER,
    NodeAuthorizationStore,
    NodeProtocolError,
    NodeRecord,
    NodeRegistry,
    NodeRPCClient,
    NodeRPCServer,
    NodeService,
    SharingScope,
)


def endpoint_address(endpoint: str) -> tuple[str, int]:
    value = urlsplit(endpoint)
    if value.scheme != "tls" or not value.hostname or not value.port or value.path not in {"", "/"}:
        raise NodeProtocolError("INVALID_NODE_ENDPOINT", "A tls://host:port endpoint is required")
    if value.username or value.password or value.query or value.fragment:
        raise NodeProtocolError("INVALID_NODE_ENDPOINT", "Endpoint cannot contain credentials or query data")
    return value.hostname, value.port


class PairedTLS:
    def __init__(self, identity: NodeIdentity, peers: TrustedPeerStore):
        self.identity, self.peers = identity, peers

    def peer(self, node_id: str) -> dict[str, Any]:
        from clinx.authority.rust_authority import RustAuthorityError
        from clinx.nodes.network_worker import command
        try:
            return command('network.peer', state=str(self.identity.store.root), peer_id=node_id)
        except RustAuthorityError as exc:
            raise NodeProtocolError(str(exc), 'A production-paired identity is required') from None

    def server_context(self):
        from clinx.nodes.network_worker import PairedContext
        return PairedContext(self.identity.store.root)

    def client_context(self, node_id):
        from clinx.nodes.network_worker import PairedContext
        self.peer(node_id)
        return PairedContext(self.identity.store.root, node_id)

    def authenticate(self, sock):
        from clinx.authority.rust_authority import RustAuthorityError
        from clinx.nodes.network_worker import command
        try:
            return command('network.authenticate', state=str(self.identity.store.root),
                certificate_hex=sock.getpeercert(binary_form=True).hex())
        except RustAuthorityError as exc:
            raise NodeProtocolError(str(exc), 'Paired identity is revoked or unavailable') from None

    def client(self, node_id, endpoint, *, timeout=3.0):
        return NodeRPCClient(endpoint_address(endpoint), ssl_context=self.client_context(node_id), timeout_seconds=timeout)


class NodeRegistrationClient:
    def __init__(self, tls: PairedTLS, centre_id: str, endpoint: str, *, timeout: float = 3.0):
        self.tls, self.centre_id, self.endpoint, self.timeout = tls, centre_id, endpoint, timeout

    def register(self, identity: NodeIdentity, scope: SharingScope, *, port: int, capabilities: tuple[str, ...]) -> dict[str, Any]:
        client = self.tls.client(self.centre_id, self.endpoint, timeout=self.timeout)
        return client.call({
            "operation": "node.register",
            "protocol_version": NODE_PROTOCOL_VERSION,
            "node_id": identity.public["node_id"],
            "port": port,
            "capabilities": list(capabilities),
            "sharing_scope": scope.as_dict(),
        })

    def heartbeat(self, identity: NodeIdentity, scope: SharingScope, *, port: int, capabilities: tuple[str, ...]) -> dict[str, Any]:
        client = self.tls.client(self.centre_id, self.endpoint, timeout=self.timeout)
        return client.call({
            "operation": "node.heartbeat",
            "protocol_version": NODE_PROTOCOL_VERSION,
            "node_id": identity.public["node_id"],
            "port": port,
            "capabilities": list(capabilities),
            "sharing_scope": scope.as_dict(),
        })

    def upload(self, identity, scope, **batch):
        return self.tls.client(self.centre_id, self.endpoint, timeout=self.timeout).call({
            "operation": "observation.upload", "protocol_version": NODE_PROTOCOL_VERSION,
            "node_id": identity.public["node_id"], "sharing_scope": scope.as_dict(), **batch})


def intersect_scope(local: SharingScope | None, centre: SharingScope | None) -> SharingScope:
    from clinx.authority.rust_authority import RustAuthorityError
    from clinx.nodes.network_worker import command
    try:
        return SharingScope(**command('scope.intersect', local=local.as_dict() if local else None,
            centre=centre.as_dict() if centre else None))
    except RustAuthorityError as exc:
        raise NodeProtocolError(str(exc), 'Owner and Centre scope rejected') from None


class CentreService:
    """Authenticated registration uses observed peer address, never claimed host."""

    def __init__(self, tls: PairedTLS, registry: NodeRegistry, approvals: NodeAuthorizationStore, *, stale_after: int = 45, observations=None, authority=None):
        self.tls, self.registry, self.approvals = tls, registry, approvals
        self.stale_after = stale_after
        self.observations = observations
        self.authority = authority

    def handle(self, request: Mapping[str, Any], sock: Any, address: Any) -> dict[str, Any]:
        from clinx.authority.request_pipe import invoke
        from clinx.authority.rust_authority import RustAuthorityError
        def callback(message):
            operation, args = message['operation'], message['arguments']
            if message['kind'] != 'network':
                raise RustAuthorityError('NETWORK_CALLBACK_INVALID')
            if operation == 'registry.get':
                record = self.registry.get(args['node_id'])
                return record.as_dict() if record else None
            if operation == 'registry.scope':
                scope = self.registry.scope(**args)
                return scope.as_dict() if scope else None
            if operation == 'registry.revoke':
                self.registry.revoke(**args)
                return None
            if operation == 'registry.register':
                self.registry.register(NodeRecord(**args['record']), SharingScope(**args['scope']))
                return None
            if operation == 'authority.node_request':
                return self.authority.call('node_request', args)
            if operation == 'observation.accept':
                return self.observations.accept(args['node_id'], SharingScope(**args['scope']), args['request'])
            raise RustAuthorityError('NETWORK_CALLBACK_INVALID')
        try:
            return invoke(dict(mode='centre', operation='centre.handle', arguments=dict(
                state=str(self.tls.identity.store.root), request=dict(request),
                certificate_hex=sock.getpeercert(binary_form=True).hex(), address=list(address),
                stale_after=self.stale_after, authority=self.authority is not None,
                observations=self.observations is not None)), callback)
        except RustAuthorityError as exc:
            return NodeProtocolError(str(exc), 'Centre route rejected').as_dict() | {'registered': False, 'read_only': True}

    def server(self, *, address: str, port: int) -> NodeRPCServer:
        return NodeRPCServer(self, address=address, port=port,
                             ssl_context_factory=self.tls.server_context, request_handler=self.handle)


class NodeAuthorityClient:
    """Rust client interface over the existing paired channel; no retry or DB.

    The Centre supplies the principal. This works on Mac without Linux IPC
    credentials. Commands preserves request_id for explicit receipt readback.
    """

    def __init__(self, registration: NodeRegistrationClient, scope):
        self.registration, self.scope = registration, scope

    def request(self, operation: str, payload: dict, *, request_id: str) -> dict:
        import json

        from clinx.authority.rust_authority import RustAuthorityError
        from clinx.nodes.node_protocol import MAX_FRAME_BYTES
        if not operation.startswith("business."):
            raise RustAuthorityError("UNSUPPORTED_OPERATION")
        scope = self.scope() if callable(self.scope) else self.scope
        if scope is None or not scope.allows("execution.authority"):
            raise RustAuthorityError("SHARING_SCOPE_DENIED")
        registration = self.registration
        request = dict(operation="authority.request", protocol_version=NODE_PROTOCOL_VERSION,
            node_id=registration.tls.identity.public["node_id"], sharing_scope=scope.as_dict(),
            request_id=request_id, command=operation.removeprefix("business."), payload=payload)
        if len(json.dumps(request, separators=(",", ":")).encode()) + 1 > MAX_FRAME_BYTES:
            raise RustAuthorityError("REQUEST_TOO_LARGE")
        try:
            result = registration.tls.client(registration.centre_id, registration.endpoint,
                timeout=registration.timeout).call(request)
        except (OSError, ValueError):
            # Includes a partial send or lost reply. Never replay here.
            raise RustAuthorityError("DELIVERY_UNKNOWN") from None
        if result.get("error_code"):
            raise RustAuthorityError(result["error_code"])
        if result.get("request_id") != request_id or not isinstance(result.get("result"), dict):
            raise RustAuthorityError("DELIVERY_UNKNOWN")
        return result["result"]


class AuthorizedRemoteClient:
    """Checks durable trust/scope on every new request, including cached routes."""

    def __init__(self, tls: PairedTLS, registry: NodeRegistry, approvals: NodeAuthorizationStore, node_id: str):
        self.tls, self.registry, self.approvals, self.node_id = tls, registry, approvals, node_id

    def call(self, request: Mapping[str, Any]) -> dict[str, Any]:
        record = self.registry.get(self.node_id)
        scope = self.approvals.get(self.node_id)
        operation = request.get("operation", "")
        try:
            self.tls.peer(self.node_id)
            registered_scope = self.registry.scope(self.node_id, record.user_scope) if record else None
            effective = intersect_scope(registered_scope, scope)
            permission = "session.read" if operation in {"execution.context", "execution.status"} else operation
            if not effective.allows(permission, request.get("provider", SUPPORTED_PROVIDER)):
                raise NodeProtocolError("SHARING_SCOPE_DENIED", "Current centre approval denies this request")
            if record is None or not record.endpoint or record.stale or record.state == "OFFLINE":
                raise NodeProtocolError("NODE_OFFLINE", "Registered node is offline")
            if request.get("user_scope") != effective.user_scope:
                raise NodeProtocolError("USER_SCOPE_DENIED", "Request has no approved user mapping")
            return self.tls.client(self.node_id, record.endpoint).call(dict(request, node_id=self.node_id))
        except NodeProtocolError as exc:
            return exc.as_dict() | {"node_id": self.node_id, "read_only": not operation.startswith("execution.")}

    def execute(self, operation: str, **request: Any) -> dict[str, Any]:
        return self.call(dict(request, operation=operation, protocol_version=NODE_PROTOCOL_VERSION))


def remote_client_factory(identity_root: Path, host: str, registry: NodeRegistry):
    private = PrivateStore(identity_root)
    identity = NodeIdentity(private, host)
    tls = PairedTLS(identity, TrustedPeerStore(private))
    approvals = NodeAuthorizationStore(identity_root)
    return lambda record: AuthorizedRemoteClient(tls, registry, approvals, record.node_id)


def node_request_handler(service: NodeService, tls: PairedTLS, approvals: NodeAuthorizationStore, centre_id: str, active_scope: Any = None, provider_relay=None):

    def handle(request: Mapping[str, Any], sock: Any, address: Any) -> dict[str, Any]:
        from clinx.authority.request_pipe import invoke
        from clinx.authority.rust_authority import RustAuthorityError
        def callback(event):
            if event['kind'] != 'owner':
                raise RustAuthorityError('NODE_CALLBACK_INVALID')
            op, args = event['operation'], event['arguments']
            if op == 'scope.apply':
                service.set_scope(SharingScope(**args))
                return None
            if op == 'provider.handle':
                return provider_relay.handle(args['request'], SharingScope(**args['scope']))
            if op == 'node.handle':
                return service.handle(args)
            raise RustAuthorityError('NODE_CALLBACK_INVALID')
        scope = active_scope() if callable(active_scope) else service.scope
        try:
            return invoke(dict(mode='node_owner', operation='owner.handle', arguments=dict(
                state=str(tls.identity.store.root), centre_id=centre_id, node_id=service.record.node_id,
                certificate_hex=sock.getpeercert(binary_form=True).hex(), request=dict(request),
                active_scope=scope.as_dict() if scope else None, provider_relay=provider_relay is not None)), callback)
        except RustAuthorityError as exc:
            return NodeProtocolError(str(exc), 'Node owner route rejected').as_dict() | {'read_only': True}
    return handle
