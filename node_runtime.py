"""Production node registration and routing over existing paired mTLS identity.

The centre owns only the existing node index. Native history and canonical
execution remain on their owning node; registration never starts a Provider.
"""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from local_discovery.identity import (
    DeviceError, NodeIdentity, PrivateStore, TrustedPeerStore, certificate_public,
)
from local_discovery.transport import peer_certificate
from node_protocol import (
    NODE_PROTOCOL_VERSION, SUPPORTED_PROVIDER, NodeAuthorizationStore,
    NodeProtocolError, NodeRecord, NodeRegistry, NodeRPCClient, NodeRPCServer,
    NodeService, SharingScope, _now,
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
        peer = self.peers.all().get(node_id)
        if not peer or peer.get("trust_state") != "TRUSTED" or peer.get("security_status") != "OPAQUE_V1":
            raise NodeProtocolError("PAIRING_REQUIRED", "A production-paired identity is required")
        return peer

    def server_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.verify_mode = ssl.CERT_REQUIRED
        self.identity.load_tls_credentials(context)
        certificates = [p["certificate"] for p in self.peers.all().values()
                        if p.get("trust_state") == "TRUSTED" and p.get("security_status") == "OPAQUE_V1"]
        if certificates:
            context.load_verify_locations(cadata="".join(certificates))
        return context

    def client_context(self, node_id: str) -> ssl.SSLContext:
        peer = self.peer(node_id)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cadata=peer["certificate"])
        self.identity.load_tls_credentials(context)
        return context

    def authenticate(self, sock: Any) -> dict[str, Any]:
        public_key = certificate_public(peer_certificate(sock))
        matches = [p for p in self.peers.all().values() if p["public_key"] == public_key]
        if len(matches) != 1:
            raise NodeProtocolError("IDENTITY_MISMATCH", "mTLS identity has no unique paired owner")
        peer = matches[0]
        try:
            return self.peers.authenticate(peer["node_id"], public_key)
        except DeviceError:
            raise NodeProtocolError("PAIRING_REQUIRED", "Paired identity is revoked or unavailable") from None

    def client(self, node_id: str, endpoint: str, *, timeout: float = 3.0) -> NodeRPCClient:
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


def intersect_scope(local: SharingScope | None, centre: SharingScope | None) -> SharingScope:
    if local is None or centre is None or local.revoked or centre.revoked:
        return SharingScope(user_scope=(centre or local).user_scope if centre or local else "default", revoked=True)
    if local.user_scope != centre.user_scope:
        raise NodeProtocolError("USER_SCOPE_DENIED", "Owner and centre approvals do not share the same explicit user mapping")
    return SharingScope(
        user_scope=centre.user_scope,
        read_sessions=local.read_sessions and centre.read_sessions,
        execute_tasks=local.execute_tasks and centre.execute_tasks,
        providers=tuple(p for p in local.providers if p in centre.providers),
        updated_at=max(local.updated_at, centre.updated_at),
    )


class CentreService:
    """Authenticated registration uses observed peer address, never claimed host."""

    def __init__(self, tls: PairedTLS, registry: NodeRegistry, approvals: NodeAuthorizationStore, *, stale_after: int = 45):
        self.tls, self.registry, self.approvals = tls, registry, approvals
        self.stale_after = stale_after

    def handle(self, request: Mapping[str, Any], sock: Any, address: Any) -> dict[str, Any]:
        try:
            peer = self.tls.authenticate(sock)
            node_id = peer["node_id"]
            if request.get("node_id") != node_id:
                raise NodeProtocolError("NODE_IDENTITY_MISMATCH", "Claimed node does not match authenticated paired identity")
            if request.get("protocol_version") != NODE_PROTOCOL_VERSION:
                raise NodeProtocolError("NODE_VERSION_INCOMPATIBLE", "Unsupported node protocol")
            if request.get("operation") not in {"node.register", "node.heartbeat"}:
                raise NodeProtocolError("UNKNOWN_NODE_OPERATION", "Centre accepts registration and heartbeat only")
            local_raw = request.get("sharing_scope")
            if not isinstance(local_raw, dict):
                raise NodeProtocolError("SHARING_SCOPE_DENIED", "Node owner approval is required")
            try:
                local = SharingScope(**{key: value for key, value in local_raw.items() if key in SharingScope.__dataclass_fields__})
            except (TypeError, ValueError, KeyError) as exc:
                raise NodeProtocolError("INVALID_SHARING_SCOPE", "Sharing scope is invalid") from exc
            effective = intersect_scope(local, self.approvals.get(node_id))
            if effective.revoked or not (effective.read_sessions or effective.execute_tasks):
                existing = self.registry.get(node_id)
                if existing:
                    self.registry.revoke(node_id, existing.user_scope)
                raise NodeProtocolError("SHARING_SCOPE_DENIED", "Both owner and centre approvals are required")
            port = request.get("port")
            if type(port) is not int or not 1 <= port <= 65535:
                raise NodeProtocolError("INVALID_NODE_ENDPOINT", "Node listener port is invalid")
            host = address[0]
            endpoint = f"tls://{'[' + host + ']' if ':' in host else host}:{port}"
            supplied = request.get("capabilities", [])
            if not isinstance(supplied, list) or any(not isinstance(x, str) for x in supplied):
                raise NodeProtocolError("INVALID_NODE_CAPABILITIES", "Capabilities must be a bounded list")
            allowed = {"session.read", "session.status"} if effective.read_sessions else set()
            if effective.execute_tasks:
                allowed |= {"execution.prepare", "execution.start", "execution.status", "execution.cancel"}
            capabilities = tuple(sorted(set(supplied) & allowed))
            record = NodeRecord(
                node_id=node_id, user_scope=effective.user_scope,
                public_key_fingerprint=peer["fingerprint"], display_name=peer["display_name"],
                capabilities=capabilities, endpoint=endpoint, state="ONLINE", last_seen=_now(),
                stale_after_seconds=self.stale_after,
            )
            self.registry.register(record, effective)
            return {"registered": True, "node_id": node_id, "centre_node_id": self.tls.identity.public["node_id"],
                    "scope": effective.as_dict(), "heartbeat_seconds": max(1, self.stale_after // 3),
                    "endpoint": endpoint, "observed_at": record.last_seen, "read_only": True}
        except NodeProtocolError as exc:
            return exc.as_dict() | {"registered": False, "read_only": True}

    def server(self, *, address: str, port: int) -> NodeRPCServer:
        return NodeRPCServer(self, address=address, port=port,
                             ssl_context_factory=self.tls.server_context, request_handler=self.handle)


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
            if not effective.allows(operation, request.get("provider", SUPPORTED_PROVIDER)):
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


def node_request_handler(service: NodeService, tls: PairedTLS, approvals: NodeAuthorizationStore, centre_id: str, active_scope: Any = None):

    def handle(request: Mapping[str, Any], sock: Any, address: Any) -> dict[str, Any]:
        try:
            peer = tls.authenticate(sock)
            if peer["node_id"] != centre_id:
                raise NodeProtocolError("CENTRE_IDENTITY_MISMATCH", "Authenticated peer is not the configured centre")
            local_scope = approvals.get(centre_id)
            centre_scope = active_scope() if callable(active_scope) else service.scope
            if local_scope is None or local_scope.revoked or centre_scope is None or centre_scope.revoked:
                raise NodeProtocolError("SHARING_SCOPE_DENIED", "Node owner approval was revoked")
            service.set_scope(intersect_scope(local_scope, centre_scope))
            return service.handle(request)
        except NodeProtocolError as exc:
            return exc.as_dict() | {"read_only": True}
    return handle
