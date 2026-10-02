"""PAKE adapter and pairing window; no home-grown password exchange.

python-spake2 0.9 explicitly disclaims constant-time execution. This adapter is
hard-gated DEV_ONLY. Replace/qualify the backend before production admission.
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
