"""PAKE adapter and pairing window; no home-grown password exchange.

Production uses opaque-ke RFC 9807 with built-in mutual key confirmation.
python-spake2 remains an explicitly selected, incompatible DEV_ONLY backend.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
from typing import Any, Callable, Protocol

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .identity import DeviceError


class PairingProtocol(Protocol):
    """One-shot backend boundary. OPAQUE returns wire messages, DEV returns a key."""

    security_status: str

    def start(self) -> bytes: ...
    def finish(self, inbound: bytes) -> bytes: ...


class Spake2Protocol:
    security_status = "DEV_ONLY"
    security_blocker = (
        "python-spake2 is not constant-time; protocol composition needs security review"
    )

    def __init__(
        self,
        code: str,
        *,
        role: str,
        client: dict[str, Any],
        server: dict[str, Any],
        window_id: str,
        allow_dev: bool = False,
    ):
        if not allow_dev:
            raise DeviceError("SECURITY_BLOCKER_DEV_ONLY")
        if len(code) != 4 or not code.isascii() or not code.isdigit():
            raise DeviceError("INVALID_CODE_FORMAT")
        from spake2 import SPAKE2_A, SPAKE2_B

        if role not in {"A", "B"}:
            raise DeviceError("INVALID_PAKE_ROLE")

        # Identity strings bind both long-term keys, roles, service/version and
        # window nonce. The server public key MUST come from the current TLS
        # certificate at the client, not from an unsigned application message.
        def identity(peer: dict[str, Any], side: str) -> bytes:
            return json.dumps(
                dict(service="CLINX_PAIRING_V1", role=side, peer=peer, window_id=window_id),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()

        klass = SPAKE2_A if role == "A" else SPAKE2_B
        self._exchange = klass(
            code.encode("ascii"), idA=identity(client, "A"), idB=identity(server, "B")
        )

    def start(self) -> bytes:
        return self._exchange.start()

    def finish(self, inbound: bytes) -> bytes:
        if len(inbound) != 33:
            raise DeviceError("INVALID_PAKE_MESSAGE")
        try:
            return self._exchange.finish(inbound)
        except Exception as exc:
            raise DeviceError("PAIRING_FAILED") from exc


def confirmation(key: bytes, role: str) -> bytes:
    # Upstream SPAKE2 documented explicit key confirmation, RFC 5869 HKDF.
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=("CLINX_PAIRING_V1/confirm_" + role).encode(),
    ).derive(key)


def verify_confirmation(key: bytes, role: str, received: bytes) -> None:
    if not hmac.compare_digest(confirmation(key, role), received):
        raise DeviceError("PAIRING_FAILED")


class PairingWindow:
    lifetime = 60.0
    max_failures = 3

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._code: str | None = None
        self._id: str | None = None
        self._expires = 0.0
        self._next_attempt = 0.0
        self._next_open = 0.0
        self._busy: str | None = None
        self.failures = 0
        self._timer: threading.Timer | None = None

    def close(self) -> None:
        with self._lock:
            self._code = None
            self._id = None
            if self._timer:
                self._timer.cancel()
                self._timer = None

    def _expire(self, window_id: str | None) -> None:
        with self._lock:
            if self._id == window_id:
                self._code = None
                self._id = None

    def open(self) -> str:
        with self._lock:
            now = self.clock()
            if now < self._next_open or self._busy:
                raise DeviceError("PAIRING_RATE_LIMITED")
            self._code = f"{secrets.randbelow(10000):04d}"
            self._id = secrets.token_hex(16)
            self._expires = now + self.lifetime
            self._next_open = self._expires
            self._next_attempt = now
            self.failures = 0
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.lifetime, self._expire, (self._id,))
            self._timer.daemon = True
            self._timer.start()
            return self._code

    def _active(self) -> None:
        if self.clock() >= self._expires:
            self._code = None
            self._id = None
        if self._code is None:
            raise DeviceError("PAIRING_CLOSED")

    def reserve(self) -> tuple[str, str, str]:
        with self._lock:
            self._active()
            if self._busy:
                raise DeviceError("PAIRING_BUSY")
            if self.clock() < self._next_attempt:
                raise DeviceError("PAIRING_RATE_LIMITED")
            self._busy = secrets.token_hex(16)
            self._next_attempt = self.clock() + 1.0
            assert self._id is not None and self._code is not None
            return self._busy, self._id, self._code

    def complete(
        self, reservation: str, *, success: bool, commit: Callable[[], None] | None = None
    ) -> None:
        with self._lock:
            if self._busy != reservation:
                raise DeviceError("PAIRING_REPLAY")
            self._busy = None
            self._active()
            if success:
                # Consume before durable trust write. I/O failure cannot reopen
                # a consumed secret. Restart never restores any pairing window.
                self._code = None
                self._id = None
                if commit:
                    commit()
            else:
                self.failures += 1
                if self.failures >= self.max_failures:
                    self._code = None
                    self._id = None


# Exact protocol selection, never negotiation or implicit fallback.
PRODUCTION_PROTOCOL = "OPAQUE-3DH-RISTRETTO255-SHA512-ARGON2I-v1"
DEV_PROTOCOL = "DEV-SPAKE2-PYTHON-v1"
PRODUCTION_TRUST = "OPAQUE_V1"


def production_backend() -> Any:
    try:
        import _clinx_opaque as backend

        if backend.BACKEND_ID != PRODUCTION_PROTOCOL or backend.OPAQUE_KE_VERSION != "4.0.1":
            raise DeviceError("PRODUCTION_PAKE_VERSION_MISMATCH")
        return backend
    except (ImportError, AttributeError, OSError):
        raise DeviceError("PRODUCTION_PAKE_UNAVAILABLE") from None


def backend_status(*, allow_dev: bool = False) -> str:
    if allow_dev:
        return "DEV_ONLY"
    try:
        production_backend()
    except DeviceError:
        return "BACKEND_UNAVAILABLE"
    return PRODUCTION_TRUST


def pairing_binding(
    client: dict[str, Any],
    server: dict[str, Any],
    window_id: str,
    session_id: str,
    client_nonce: str,
) -> tuple[bytes, bytes, bytes]:
    """RFC 9807 context and explicit role identifiers, identical at both ends."""
    import re

    from .identity import public_record

    client, server = public_record(client), public_record(server)
    if client["node_id"] == server["node_id"] or client["public_key"] == server["public_key"]:
        raise DeviceError("DUPLICATE_PAIRING_IDENTITY")
    if any(
        not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value)
        for value in (window_id, session_id, client_nonce)
    ):
        raise DeviceError("INVALID_PAIRING_SESSION")

    def canonical(value: dict[str, Any]) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    client_id = canonical(dict(role="initiator", identity=client))
    server_id = canonical(dict(role="acceptor", identity=server))
    context = canonical(
        dict(
            service="CLINX_LOCAL_PAIRING",
            version=2,
            protocol=PRODUCTION_PROTOCOL,
            window_id=window_id,
            session_id=session_id,
            client_nonce=client_nonce,
            initiator=client,
            acceptor=server,
        )
    )
    return context, client_id, server_id


class OpaqueClientProtocol:
    """KE2 verifies the server; finish returns KE3, never a Python session key."""

    security_status = PRODUCTION_TRUST

    def __init__(self, code: str, binding: tuple[bytes, bytes, bytes]):
        backend = production_backend()
        try:
            self._exchange = backend.Client(code.encode("ascii"), *binding)
        except (ValueError, UnicodeError):
            raise DeviceError("PAIRING_FAILED") from None

    def start(self) -> bytes:
        try:
            return self._exchange.start()
        except ValueError:
            raise DeviceError("PAIRING_FAILED") from None

    def finish(self, inbound: bytes) -> bytes:
        try:
            return self._exchange.finish(inbound)
        except ValueError:
            raise DeviceError("PAIRING_FAILED") from None


class OpaqueServerProtocol:
    """Local ephemeral registration plus KE2; finish verifies KE3 in opaque-ke."""

    security_status = PRODUCTION_TRUST

    def __init__(self, code: str, binding: tuple[bytes, bytes, bytes], inbound: bytes):
        backend = production_backend()
        try:
            self._exchange = backend.Server(code.encode("ascii"), *binding, inbound)
        except (ValueError, UnicodeError):
            raise DeviceError("PAIRING_FAILED") from None

    def start(self) -> bytes:
        try:
            return self._exchange.start()
        except ValueError:
            raise DeviceError("PAIRING_FAILED") from None

    def finish(self, inbound: bytes) -> bytes:
        try:
            self._exchange.finish(inbound)
            return b""
        except ValueError:
            raise DeviceError("PAIRING_FAILED") from None
