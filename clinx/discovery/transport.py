"""TLS 1.3 device transport. Session authentication does not dispatch work.

Initial PAKE is bound to the TLS server identity. Later connections use mutual
TLS with pinned self-signed Ed25519 certificates. No PIN/token is reused.
"""

from __future__ import annotations

import json
import ssl
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from .discovery import Candidate, lan_address
from .identity import (
    DeviceError,
    NodeIdentity,
    TrustedPeerStore,
)
from .pairing import PairingWindow

MAX_FRAME = 16384


def send(sock: ssl.SSLSocket, raw: dict[str, Any]) -> None:
    if isinstance(sock, RustConnection):
        return sock.send(raw)
    data = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if len(data) > MAX_FRAME:
        raise DeviceError("FRAME_TOO_LARGE")
    sock.sendall(data)


def receive(sock: ssl.SSLSocket) -> dict[str, Any]:
    if isinstance(sock, RustConnection):
        return sock.receive()
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
    if isinstance(sock, RustConnection):
        return sock.certificate()
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
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


def _enrollment(operation, **arguments):
    from .identity import _state_call
    try:
        return _state_call('enrollment.' + operation, **arguments)
    except DeviceError as exc:
        if str(exc) == 'TLS_HANDSHAKE_FAILED':
            raise ssl.SSLError(str(exc)) from None
        raise


class RustConnection:
    """Qualification wire handle; all production bytes and TLS stay in Rust."""
    def __init__(self, handle):
        self.handle = handle
    def send(self, message):
        return _enrollment('client.send', connection=self.handle, message=message)
    def receive(self):
        return _enrollment('client.receive', connection=self.handle)
    def certificate(self):
        return _enrollment('client.certificate', connection=self.handle)
    def close(self):
        if self.handle is not None:
            handle, self.handle = self.handle, None
            _enrollment('client.close', connection=handle)
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()


class LanTransport:
    """Compatibility DTO boundary; enrollment, authentication and TLS are Rust-owned."""
    def __init__(self, identity: NodeIdentity, peers: TrustedPeerStore, *, allow_dev: bool = False, allow_direct: bool = False):
        if allow_dev:
            raise DeviceError("SECURITY_BLOCKER_DEV_ONLY")
        self.identity, self.peers, self.allow_direct = identity, peers, allow_direct

    def _connect(self, candidate: Candidate, *, pairing: bool) -> RustConnection:
        return RustConnection(_enrollment('client.connect', root=str(self.identity.store.root), candidate=asdict(candidate), pairing=pairing, allow_direct=self.allow_direct))

    def pair(self, candidate: Candidate, code: str) -> DeviceSession:
        raw = _enrollment('client.pair', root=str(self.identity.store.root), candidate=asdict(candidate), code=code, allow_direct=self.allow_direct)
        raw['capabilities'] = tuple(raw['capabilities'])
        return DeviceSession(**raw)

    def reconnect(self, candidate: Candidate) -> DeviceSession:
        raw = _enrollment('client.reconnect', root=str(self.identity.store.root), candidate=asdict(candidate), allow_direct=self.allow_direct)
        raw['capabilities'] = tuple(raw['capabilities'])
        return DeviceSession(**raw)

    def observer_connection(self, candidate: Candidate) -> dict[str, Any]:
        from .observer_bootstrap import validate_connection
        return validate_connection(_enrollment('client.observer', root=str(self.identity.store.root), candidate=asdict(candidate), allow_direct=self.allow_direct))


class DeviceServer:
    def __init__(
        self,
        identity: NodeIdentity,
        peers: TrustedPeerStore,
        *,
        address: str = "0.0.0.0",
        port: int = 0,
        allow_dev: bool = False,
        allow_direct: bool = False,
        window: PairingWindow | None = None,
        observer_bootstrap: Any = None,
    ):
        if allow_dev:
            raise DeviceError("SECURITY_BLOCKER_DEV_ONLY")
        if address != "0.0.0.0":
            lan_address(address)
        self.identity, self.peers, self.allow_direct = identity, peers, allow_direct
        self.window = window or PairingWindow()
        self.observer_bootstrap = observer_bootstrap
        self._rust_server = None
        import uuid
        from .identity import _enrollment_callbacks, _runtime_lock
        self._handler_token = uuid.uuid4().hex
        with _runtime_lock:
            _enrollment_callbacks[self._handler_token] = self
        try:
            result = _enrollment('server.start', root=str(identity.store.root), address=address, port=port,
                allow_direct=allow_direct, window=self.window._window, handler_token=self._handler_token,
                custom_clock=self.window.clock is not time.monotonic, observer_enabled=observer_bootstrap is not None)
            self._rust_server, self.port = result['server'], result['port']
        except Exception:
            with _runtime_lock:
                _enrollment_callbacks.pop(self._handler_token, None)
            raise

    def close(self) -> None:
        from .identity import _enrollment_callbacks, _runtime_lock
        server, self._rust_server = self._rust_server, None
        try:
            if server is not None:
                _enrollment('server.close', server=server)
        finally:
            with _runtime_lock:
                _enrollment_callbacks.pop(self._handler_token, None)

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
