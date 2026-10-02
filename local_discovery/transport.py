"""TLS 1.3 device transport. Session authentication does not dispatch work.

Initial PAKE is bound to the TLS server identity. Later connections use mutual
TLS with pinned self-signed Ed25519 certificates. No PIN/token is reused.
"""

from __future__ import annotations

import json
import secrets
import socket
import socketserver
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from .discovery import Candidate, lan_address
from .identity import (
    DeviceError,
    NodeIdentity,
    TrustedPeerStore,
    certificate_public,
    decoded,
    encoded,
    public_record,
)
from .pairing import (
    DEV_PROTOCOL,
    PRODUCTION_PROTOCOL,
    PRODUCTION_TRUST,
    OpaqueClientProtocol,
    OpaqueServerProtocol,
    PairingProtocol,
    PairingWindow,
    Spake2Protocol,
    confirmation,
    pairing_binding,
    production_backend,
    verify_confirmation,
)

MAX_FRAME = 16384


def send(sock: ssl.SSLSocket, raw: dict[str, Any]) -> None:
    data = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if len(data) > MAX_FRAME:
        raise DeviceError("FRAME_TOO_LARGE")
    sock.sendall(data)


def receive(sock: ssl.SSLSocket) -> dict[str, Any]:
    data = bytearray()
    deadline = time.monotonic() + 5.0
    while len(data) < MAX_FRAME:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeviceError("FRAME_TIMEOUT")
        sock.settimeout(remaining)
        byte = sock.recv(1)
        if not byte:
            raise DeviceError("CONNECTION_CLOSED")
        data.extend(byte)
        if byte == b"\n":
            try:
                raw = json.loads(data)
                if not isinstance(raw, dict):
                    raise ValueError()
            except (ValueError, UnicodeError) as exc:
                raise DeviceError("INVALID_FRAME") from exc
            if "error" in raw:
                raise DeviceError("REMOTE_REQUEST_REJECTED")
            return raw
    raise DeviceError("FRAME_TOO_LARGE")


def peer_certificate(sock: ssl.SSLSocket) -> str:
    der = sock.getpeercert(binary_form=True)
    if not der:
        raise DeviceError("CLIENT_CERTIFICATE_REQUIRED")
    return x509.load_der_x509_certificate(der).public_bytes(serialization.Encoding.PEM).decode()


@dataclass(frozen=True)
class DeviceSession:
    node_id: str
    fingerprint: str
    transport: str = "lan_tls"
    capabilities: tuple[str, ...] = ()
    authority_granted: bool = False
    authorization_required: bool = True


class DeviceTransport(Protocol):
    """Future Tailscale/direct/relay adapters must return the same auth result."""

    def reconnect(self, candidate: Candidate) -> DeviceSession: ...


def check_candidate(
    candidate: Candidate, peers: TrustedPeerStore, *, pairing: bool = False
) -> None:
    if candidate.mismatch:
        raise DeviceError("IDENTITY_MISMATCH")
    old = peers.all().get(candidate.node_id)
    if old and old["fingerprint"] != candidate.fingerprint:
        raise DeviceError("IDENTITY_MISMATCH")
    if old and old["trust_state"] != "TRUSTED":
        raise DeviceError("TRUST_REVOKED")
    if not pairing and not old:
        raise DeviceError("UNTRUSTED_DEVICE")


class LanTransport:
    def __init__(self, identity: NodeIdentity, peers: TrustedPeerStore, *, allow_dev: bool = False):
        self.identity, self.peers, self.allow_dev = identity, peers, allow_dev

    def _connect(self, candidate: Candidate, *, pairing: bool) -> ssl.SSLSocket:
        check_candidate(candidate, self.peers, pairing=pairing)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.check_hostname = False  # Verify pinned identity instead of IP/DNS.
        if pairing:
            context.verify_mode = ssl.CERT_NONE  # PAKE authenticates initial server key.
        else:
            peer = self.peers.all()[candidate.node_id]
            if peer["security_status"] == "DEV_ONLY" and not self.allow_dev:
                raise DeviceError("SECURITY_BLOCKER_DEV_ONLY")
            context.load_verify_locations(cadata=peer["certificate"])
            self.identity.load_tls_credentials(context)
        for address, port in candidate.endpoints:
            lan_address(address)
            raw = None
            sock = None
            try:
                raw = socket.create_connection((address, port), timeout=5)
                sock = context.wrap_socket(raw, server_hostname=None)
                pub = certificate_public(peer_certificate(sock))
                from .identity import fingerprint

                if fingerprint(pub) != candidate.fingerprint:
                    raise DeviceError("IDENTITY_MISMATCH")
                return sock
            except (DeviceError, ssl.SSLError):
                if sock:
                    sock.close()
                elif raw:
                    raw.close()
                # Never downgrade/fail over following an authentication failure.
                raise
            except OSError:
                if raw:
                    raw.close()
        raise DeviceError("DEVICE_UNREACHABLE")

    def pair(self, candidate: Candidate, code: str) -> DeviceSession:
        if not self.allow_dev:
            return self._pair_production(candidate, code)
        if candidate.node_id == self.identity.public["node_id"]:
            raise DeviceError("DUPLICATE_LOCAL_NODE_ID")
        with self._connect(candidate, pairing=True) as sock:
            cert = peer_certificate(sock)
            send(
                sock,
                dict(
                    op="pair",
                    protocol=DEV_PROTOCOL,
                    identity=self.identity.public,
                    certificate=self.identity.certificate,
                ),
            )
            hello = receive(sock)
            if hello.get("protocol") != DEV_PROTOCOL:
                raise DeviceError("PAIRING_PROTOCOL_MISMATCH")
            server = public_record(hello["identity"])
            if server["node_id"] != candidate.node_id or server["public_key"] != certificate_public(
                cert
            ):
                raise DeviceError("IDENTITY_MISMATCH")
            exchange: PairingProtocol = Spake2Protocol(
                code,
                role="A",
                client=self.identity.public,
                server=server,
                window_id=hello["window_id"],
                allow_dev=self.allow_dev,
            )
            outbound = exchange.start()
            key = exchange.finish(decoded(hello["message"]))
            send(
                sock, dict(message=encoded(outbound), confirmation=encoded(confirmation(key, "A")))
            )
            result = receive(sock)
            verify_confirmation(key, "B", decoded(result["confirmation"]))
            self.peers.trust(server, cert, security_status=exchange.security_status)
        # Prove both durable trust writes with a fresh mutual-TLS connection.
        return self.reconnect(candidate)

    def _pair_production(self, candidate: Candidate, code: str) -> DeviceSession:
        production_backend()  # Fail before any connection or enrollment.
        if candidate.node_id == self.identity.public["node_id"]:
            raise DeviceError("DUPLICATE_LOCAL_NODE_ID")
        client_nonce = secrets.token_hex(16)
        with self._connect(candidate, pairing=True) as sock:
            cert = peer_certificate(sock)
            send(
                sock,
                dict(
                    op="pair",
                    protocol=PRODUCTION_PROTOCOL,
                    client_nonce=client_nonce,
                    identity=self.identity.public,
                    certificate=self.identity.certificate,
                ),
            )
            hello = receive(sock)
            if hello.get("protocol") != PRODUCTION_PROTOCOL:
                raise DeviceError("PAIRING_PROTOCOL_MISMATCH")
            server = public_record(hello["identity"])
            if server["node_id"] != candidate.node_id or server["public_key"] != certificate_public(
                cert
            ):
                raise DeviceError("IDENTITY_MISMATCH")
            binding = pairing_binding(
                self.identity.public,
                server,
                hello["window_id"],
                hello["session_id"],
                client_nonce,
            )
            exchange: PairingProtocol = OpaqueClientProtocol(code, binding)
            code = ""
            send(sock, dict(protocol=PRODUCTION_PROTOCOL, message=encoded(exchange.start())))
            response = receive(sock)
            if response.get("protocol") != PRODUCTION_PROTOCOL:
                raise DeviceError("PAIRING_PROTOCOL_MISMATCH")
            finalization = exchange.finish(decoded(response["message"]))
            send(sock, dict(protocol=PRODUCTION_PROTOCOL, message=encoded(finalization)))
            # KE2 already authenticated the TLS server public key. This TLS
            # acknowledgement reports the durable server commit, not a new MAC.
            if receive(sock) != dict(protocol=PRODUCTION_PROTOCOL, paired=True):
                raise DeviceError("PAIRING_COMMIT_FAILED")
            self.peers.trust(
                server,
                cert,
                security_status=PRODUCTION_TRUST,
                pairing_protocol=PRODUCTION_PROTOCOL,
                fresh_pairing=True,
            )
        return self.reconnect(candidate)

    def reconnect(self, candidate: Candidate) -> DeviceSession:
        with self._connect(candidate, pairing=False) as sock:
            public = certificate_public(peer_certificate(sock))
            send(sock, dict(op="session", node_id=self.identity.public["node_id"]))
            reply = receive(sock)
            if reply != dict(
                node_id=candidate.node_id,
                capabilities=[],
                authority_granted=False,
                authorization_required=True,
            ):
                raise DeviceError("INVALID_CAPABILITY_NEGOTIATION")
            peer = self.peers.authenticate(candidate.node_id, public, allow_dev=self.allow_dev)
            return DeviceSession(candidate.node_id, peer["fingerprint"])

    def observer_connection(self, candidate: Candidate) -> dict[str, Any]:
        from .observer_bootstrap import validate_connection

        if self.allow_dev:
            raise DeviceError("SECURITY_BLOCKER_DEV_ONLY")
        with self._connect(candidate, pairing=False) as sock:
            public = certificate_public(peer_certificate(sock))
            self.peers.authenticate(candidate.node_id, public)
            send(sock, dict(op="observer_connection", node_id=self.identity.public["node_id"]))
            reply = receive(sock)
            if reply.get("node_id") != candidate.node_id or reply.get("read_only") is not True:
                raise DeviceError("INVALID_OBSERVER_CONNECTION")
            return validate_connection(reply)


class DeviceServer:
    def __init__(
        self,
        identity: NodeIdentity,
        peers: TrustedPeerStore,
        *,
        address: str = "0.0.0.0",
        port: int = 0,
        allow_dev: bool = False,
        window: PairingWindow | None = None,
        observer_bootstrap: Any = None,
    ):
        if address != "0.0.0.0":
            lan_address(address)
        self.identity, self.peers, self.allow_dev = identity, peers, allow_dev
        self.window = window or PairingWindow()
        self.observer_bootstrap = observer_bootstrap
        self._slots = threading.BoundedSemaphore(16)
        self._lease = identity.store.lock("server.lock", nonblocking=True)
        self._lease.__enter__()
        owner = self

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = False
            block_on_close = True
            allow_reuse_address = True

            def process_request(self, request: Any, client_address: Any) -> None:
                if not owner._slots.acquire(blocking=False):
                    request.close()
                    return
                try:
                    super().process_request(request, client_address)
                except Exception:
                    owner._slots.release()
                    raise

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                try:
                    # LAN v1 rejects public source addresses even on a wildcard bind.
                    lan_address(self.client_address[0])
                    self.request.settimeout(5)
                    with owner._context().wrap_socket(self.request, server_side=True) as sock:
                        owner._handle(sock)
                except Exception:
                    # Do not log wire frames, identities, PIN, or crypto exceptions.
                    pass
                finally:
                    owner._slots.release()

        try:
            self.server = Server((address, port), Handler)
        except Exception:
            self._lease.__exit__(None, None, None)
            raise
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def _context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        self.identity.load_tls_credentials(context)
        context.verify_mode = ssl.CERT_OPTIONAL
        # Rebuild on each connection so trust/revocation changes are immediate.
        certificates = [
            p["certificate"]
            for p in self.peers.all().values()
            if p["trust_state"] == "TRUSTED"
            and (p["security_status"] == PRODUCTION_TRUST or self.allow_dev)
        ]
        if certificates:
            context.load_verify_locations(cadata="".join(certificates))
        return context

    def _handle(self, sock: ssl.SSLSocket) -> None:
        reservation = None
        try:
            request = receive(sock)
            if request.get("op") == "session":
                pub = certificate_public(peer_certificate(sock))
                self.peers.authenticate(request["node_id"], pub, allow_dev=self.allow_dev)
                send(
                    sock,
                    dict(
                        node_id=self.identity.public["node_id"],
                        capabilities=[],
                        authority_granted=False,
                        authorization_required=True,
                    ),
                )
            elif request.get("op") == "observer_connection":
                pub = certificate_public(peer_certificate(sock))
                self.peers.authenticate(request["node_id"], pub)
                if self.allow_dev or self.observer_bootstrap is None:
                    raise DeviceError("OBSERVER_SHARING_DISABLED")
                connection = self.observer_bootstrap.connection()
                send(sock, dict(connection, node_id=self.identity.public["node_id"]))
            elif request.get("op") == "pair":
                reservation, window_id, code = self.window.reserve()
                expected_protocol = DEV_PROTOCOL if self.allow_dev else PRODUCTION_PROTOCOL
                if request.get("protocol") != expected_protocol:
                    raise DeviceError("PAIRING_PROTOCOL_MISMATCH")
                client = public_record(request["identity"])
                if client["node_id"] == self.identity.public["node_id"]:
                    raise DeviceError("DUPLICATE_LOCAL_NODE_ID")
                cert = request["certificate"]
                if certificate_public(cert) != client["public_key"]:
                    raise DeviceError("IDENTITY_MISMATCH")
                old = self.peers.all().get(client["node_id"])
                if old and (
                    old["public_key"] != client["public_key"] or old["trust_state"] != "TRUSTED"
                ):
                    raise DeviceError("IDENTITY_MISMATCH")
                if not self.allow_dev:
                    self._accept_production(
                        sock, request, client, cert, reservation, window_id, code
                    )
                    reservation = None
                    return
                exchange: PairingProtocol = Spake2Protocol(
                    code,
                    role="B",
                    client=client,
                    server=self.identity.public,
                    window_id=window_id,
                    allow_dev=self.allow_dev,
                )
                code = ""  # The PAKE library still owns its in-memory secret until return.
                send(
                    sock,
                    dict(
                        protocol=DEV_PROTOCOL,
                        identity=self.identity.public,
                        window_id=window_id,
                        message=encoded(exchange.start()),
                    ),
                )
                answer = receive(sock)
                key = exchange.finish(decoded(answer["message"]))
                verify_confirmation(key, "A", decoded(answer["confirmation"]))
                token, reservation = reservation, None
                self.window.complete(
                    token,
                    success=True,
                    commit=lambda: self.peers.trust(
                        client, cert, security_status=exchange.security_status
                    ),
                )
                send(sock, dict(confirmation=encoded(confirmation(key, "B"))))
            else:
                # No host/terminal/task/MCP/production operations on this listener.
                raise DeviceError("EXISTING_AUTHORIZATION_REQUIRED")
        except Exception:
            if reservation is not None:
                try:
                    self.window.complete(reservation, success=False)
                except DeviceError:
                    pass
            try:
                send(sock, {"error": "REQUEST_REJECTED"})
            except OSError:
                pass

    def _accept_production(
        self,
        sock: ssl.SSLSocket,
        request: dict[str, Any],
        client: dict[str, Any],
        cert: str,
        reservation: str,
        window_id: str,
        code: str,
    ) -> None:
        production_backend()
        binding = pairing_binding(
            client, self.identity.public, window_id, reservation, request["client_nonce"]
        )
        send(
            sock,
            dict(
                protocol=PRODUCTION_PROTOCOL,
                identity=self.identity.public,
                window_id=window_id,
                session_id=reservation,
            ),
        )
        first = receive(sock)
        if first.get("protocol") != PRODUCTION_PROTOCOL:
            raise DeviceError("PAIRING_PROTOCOL_MISMATCH")
        exchange: PairingProtocol = OpaqueServerProtocol(code, binding, decoded(first["message"]))
        code = ""
        send(sock, dict(protocol=PRODUCTION_PROTOCOL, message=encoded(exchange.start())))
        answer = receive(sock)
        if answer.get("protocol") != PRODUCTION_PROTOCOL:
            raise DeviceError("PAIRING_PROTOCOL_MISMATCH")
        exchange.finish(decoded(answer["message"]))
        self.window.complete(
            reservation,
            success=True,
            commit=lambda: self.peers.trust(
                client,
                cert,
                security_status=PRODUCTION_TRUST,
                pairing_protocol=PRODUCTION_PROTOCOL,
                fresh_pairing=True,
            ),
        )
        send(sock, dict(protocol=PRODUCTION_PROTOCOL, paired=True))

    def close(self) -> None:
        self.window.close()
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=6)
        self._lease.__exit__(None, None, None)

    def __enter__(self) -> DeviceServer:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def reconnect_discovered(
    identity: NodeIdentity,
    peers: TrustedPeerStore,
    candidates: dict[str, Candidate],
    *,
    allow_dev: bool = False,
) -> dict[str, str]:
    transport = LanTransport(identity, peers, allow_dev=allow_dev)
    states = {}
    trusted = peers.all()
    for node_id, candidate in candidates.items():
        if node_id == identity.public["node_id"] or node_id not in trusted:
            continue
        try:
            transport.reconnect(candidate)
            states[node_id] = "AUTHENTICATED"
        except ssl.SSLCertVerificationError:
            states[node_id] = "IDENTITY_MISMATCH"
        except DeviceError as exc:
            states[node_id] = (
                "IDENTITY_MISMATCH" if str(exc) == "IDENTITY_MISMATCH" else "AUTHENTICATION_FAILED"
            )
        except OSError:
            states[node_id] = "AUTHENTICATION_FAILED"
    return states
