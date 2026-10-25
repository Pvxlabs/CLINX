"""PAKE adapter and pairing window; no home-grown password exchange.

Production uses opaque-ke RFC 9807 with built-in mutual key confirmation.
python-spake2 remains an explicitly selected, incompatible DEV_ONLY backend.
"""

from __future__ import annotations

import hmac
import json
import queue
import threading
import time
from typing import Any, Callable, Protocol

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
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
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


_window_disposals = queue.SimpleQueue()
_window_cleanup_lock = threading.Lock()
_window_cleanup_started = False


def _drain_window_disposals():
    from .identity import _state_call
    while True:
        window = _window_disposals.get()
        try:
            if isinstance(window, tuple):
                _state_call('opaque.delete', exchange=window[1])
            else:
                _state_call('window.delete', window=window)
        except Exception:
            # Best-effort disposal only; an unknown delete is never replayed.
            pass


def _start_window_cleanup():
    global _window_cleanup_started
    with _window_cleanup_lock:
        if not _window_cleanup_started:
            threading.Thread(target=_drain_window_disposals,
                             name='clinx-pairing-cleanup', daemon=True).start()
            _window_cleanup_started = True


class PairingWindow:
    """Private Rust-owned window; PAKE stays in the existing backend."""
    lifetime = 60.0
    max_failures = 3

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        from .identity import _state_call
        import weakref
        self.clock = clock
        _start_window_cleanup()
        self._window = _state_call('window.create')
        # GC may run while Worker.call holds its non-reentrant lock. The
        # finalizer only uses CPython's reentrant queue put; RPC happens on the
        # cleanup thread after the interrupted call can release its locks.
        self._finalizer = weakref.finalize(self, _window_disposals.put, self._window)

    def _call(self, operation, **arguments):
        from .identity import _state_call
        now = None if self.clock is time.monotonic else self.clock()
        return _state_call('window.' + operation, window=self._window, now=now, **arguments)

    @property
    def failures(self):
        return self._call('snapshot')['failures']

    @property
    def _code(self):
        return self._call('snapshot')['code']

    @property
    def _id(self):
        return self._call('snapshot')['id']

    def close(self) -> None:
        self._call('close')

    def _expire(self, window_id: str | None) -> None:
        self._call('expire', id=window_id)

    def open(self) -> str:
        return self._call('open', lifetime=self.lifetime)

    def _active(self) -> None:
        self._call('active')

    def reserve(self) -> tuple[str, str, str]:
        return tuple(self._call('reserve'))

    def complete(self, reservation: str, *, success: bool, commit: Callable[[], None] | None = None) -> None:
        self._call('complete', reservation=reservation, success=success,
                   commit=commit is not None, _commit=commit, max_failures=self.max_failures)


# Exact protocol selection, never negotiation or implicit fallback.
PRODUCTION_PROTOCOL = "OPAQUE-3DH-RISTRETTO255-SHA512-ARGON2I-v1"
DEV_PROTOCOL = "DEV-SPAKE2-PYTHON-v1"
PRODUCTION_TRUST = "OPAQUE_V1"


class _RustOpaqueExchange:
    def __init__(self, code, context, client, server, inbound=None):
        import weakref
        from .identity import _state_call, encoded
        _start_window_cleanup()
        self._exchange = _state_call('opaque.client' if inbound is None else 'opaque.server',
            code=encoded(code), binding=[encoded(v) for v in (context, client, server)],
            message=encoded(inbound) if inbound is not None else None)
        self._finalizer = weakref.finalize(self, _window_disposals.put, ('opaque', self._exchange))

    def start(self):
        from .identity import _state_call, decoded
        return decoded(_state_call('opaque.start', exchange=self._exchange))

    def finish(self, inbound):
        from .identity import _state_call, decoded, encoded
        return decoded(_state_call('opaque.finish', exchange=self._exchange, message=encoded(inbound)))


class _RustOpaqueBackend:
    BACKEND_ID = PRODUCTION_PROTOCOL
    OPAQUE_KE_VERSION = '4.0.1'
    Client = _RustOpaqueExchange
    Server = _RustOpaqueExchange


def production_backend() -> Any:
    from clinx.authority.rust_authority import RustAuthorityError
    from .identity import _state_call
    try:
        actual = _state_call('opaque.status')
    except (RustAuthorityError, OSError, TimeoutError):
        raise DeviceError('PRODUCTION_PAKE_UNAVAILABLE') from None
    if not isinstance(actual, dict) or actual.get('BACKEND_ID') != PRODUCTION_PROTOCOL or actual.get('OPAQUE_KE_VERSION') != '4.0.1':
        raise DeviceError('PRODUCTION_PAKE_VERSION_MISMATCH')
    return _RustOpaqueBackend


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
    """Rust canonicalizes the existing RFC 9807 transcript and role identity."""
    from .identity import _state_call, decoded
    return tuple(decoded(v) for v in _state_call('opaque.binding', client=client, server=server,
        window_id=window_id, session_id=session_id, client_nonce=client_nonce))


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
