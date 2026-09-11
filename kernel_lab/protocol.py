"""Bounded NDJSON subprocess protocol for the experimental Rust kernel."""

from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import subprocess
import time
from collections.abc import Mapping, Sequence
from typing import Any


PROTOCOL_VERSION = "CLINX_KERNEL_V1"
AUTHORITY = "NON_AUTHORITATIVE"
MAX_FRAME_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 1_048_576
MAX_STDERR_BYTES = 65_536
DEFAULT_TIMEOUT_SECONDS = 2.0


class KernelProtocolError(RuntimeError):
    """The child returned data that violates CLINX_KERNEL_V1."""


class KernelProcessError(RuntimeError):
    """The bounded child process failed before a valid response arrived."""


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise KernelProtocolError(f"duplicate JSON field in response: {key}")
        result[key] = value
    return result


def strict_json_loads(raw: bytes) -> Any:
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_pairs)
    except KernelProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KernelProtocolError(f"response is not valid UTF-8 JSON: {exc}") from exc


def encode_request(envelope: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            dict(envelope),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:
        raise KernelProtocolError(f"request cannot be encoded as finite JSON: {exc}") from exc
    if len(encoded) > MAX_FRAME_BYTES:
        raise KernelProtocolError(
            f"request frame is {len(encoded)} bytes; maximum is {MAX_FRAME_BYTES}"
        )
    return encoded


class KernelSession:
    """One finite Rust process with correlated, self-contained requests."""

    def __init__(
        self,
        command: str | Path | Sequence[str | Path],
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        max_stderr_bytes: int = MAX_STDERR_BYTES,
        max_requests: int = 4_096,
    ) -> None:
        if isinstance(command, (str, Path)):
            command = (command,)
        self.command = tuple(str(part) for part in command)
        if not self.command:
            raise ValueError("kernel command must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if min(max_response_bytes, max_stderr_bytes, max_requests) < 1:
            raise ValueError("protocol limits must be positive")
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.max_requests = max_requests
        self._process: subprocess.Popen[bytes] | None = None
        self._selector: selectors.BaseSelector | None = None
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._requests = 0

    def __enter__(self) -> KernelSession:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def stderr(self) -> str:
        return bytes(self._stderr).decode("utf-8", errors="replace")

    def start(self) -> None:
        if self._process is not None:
            return
        try:
            process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise KernelProcessError(
                f"cannot start Rust kernel command {self.command!r}: {exc}"
            ) from exc
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        self._process = process
        self._selector = selector

    def close(self) -> None:
        process = self._process
        selector = self._selector
        self._process = None
        self._selector = None
        if selector is not None:
            selector.close()
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=0.5)

    def request(
        self,
        operation: str,
        payload: Mapping[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if request_id is None:
            request_id = f"python-{self._requests + 1}"
        return self.send_envelope(
            {
                "request_id": request_id,
                "protocol_version": PROTOCOL_VERSION,
                "operation": operation,
                "payload": dict(payload),
            }
        )

    def send_envelope(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        self.start()
        if self._requests >= self.max_requests:
            raise KernelProtocolError("client process request bound reached")
        request_id = envelope.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise KernelProtocolError("request_id must be non-empty text")
        frame = encode_request(envelope)
        process = self._process
        assert process is not None and process.stdin is not None
        try:
            process.stdin.write(frame)
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise KernelProcessError(
                f"kernel process exited before accepting request {request_id!r}"
            ) from exc
        self._requests += 1
        raw = self._read_response(request_id)
        return self._validate_response(raw, request_id)

    def _read_response(self, request_id: str) -> bytes:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            newline = self._stdout.find(b"\n")
            if newline >= 0:
                if newline > self.max_response_bytes:
                    self.close()
                    raise KernelProtocolError("kernel response exceeded the byte bound")
                self._drain_immediate_stderr()
                raw = bytes(self._stdout[:newline])
                del self._stdout[: newline + 1]
                return raw
            if len(self._stdout) > self.max_response_bytes:
                self.close()
                raise KernelProtocolError("kernel response exceeded the byte bound")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise KernelProcessError(f"kernel request {request_id!r} timed out")
            process = self._process
            selector = self._selector
            assert process is not None and selector is not None
            ready = selector.select(remaining)
            if not ready:
                continue
            # Drain diagnostics first so a child cannot hide an over-limit stderr
            # write by racing a syntactically valid stdout line.
            ready.sort(key=lambda item: item[0].data != "stderr")
            for key, _ in ready:
                stream = key.data
                chunk = os.read(key.fileobj.fileno(), 65_536)
                if not chunk:
                    try:
                        selector.unregister(key.fileobj)
                    except KeyError:
                        pass
                    continue
                if stream == "stderr":
                    self._stderr.extend(chunk)
                    if len(self._stderr) > self.max_stderr_bytes:
                        self.close()
                        raise KernelProcessError("kernel stderr exceeded the byte bound")
                else:
                    self._stdout.extend(chunk)
            if process.poll() is not None and not selector.get_map():
                if self._stdout:
                    self.close()
                    raise KernelProtocolError("kernel output was truncated before newline")
                code = process.returncode
                self.close()
                raise KernelProcessError(
                    f"kernel process exited with code {code} before response {request_id!r}"
                )

    def _drain_immediate_stderr(self) -> None:
        """Consume already-readable diagnostics before accepting stdout."""
        selector = self._selector
        if selector is None:
            return
        while True:
            ready = [item for item in selector.select(0) if item[0].data == "stderr"]
            if not ready:
                return
            for key, _ in ready:
                chunk = os.read(key.fileobj.fileno(), 65_536)
                if not chunk:
                    try:
                        selector.unregister(key.fileobj)
                    except KeyError:
                        pass
                    continue
                self._stderr.extend(chunk)
                if len(self._stderr) > self.max_stderr_bytes:
                    self.close()
                    raise KernelProcessError("kernel stderr exceeded the byte bound")

    @staticmethod
    def _validate_response(raw: bytes, request_id: str) -> dict[str, Any]:
        decoded = strict_json_loads(raw)
        if not isinstance(decoded, dict):
            raise KernelProtocolError("kernel response must be a JSON object")
        allowed = {"request_id", "protocol_version", "ok", "authority", "result", "error"}
        required = {"request_id", "protocol_version", "ok", "authority"}
        if unknown := set(decoded) - allowed:
            raise KernelProtocolError(f"kernel response has unknown fields: {sorted(unknown)}")
        if missing := required - set(decoded):
            raise KernelProtocolError(f"kernel response is missing fields: {sorted(missing)}")
        if decoded["request_id"] != request_id:
            raise KernelProtocolError(
                f"response request_id {decoded['request_id']!r} does not match {request_id!r}"
            )
        if decoded["protocol_version"] != PROTOCOL_VERSION:
            raise KernelProtocolError("kernel response protocol version mismatch")
        if decoded["authority"] != AUTHORITY:
            raise KernelProtocolError("kernel response authority is not NON_AUTHORITATIVE")
        if not isinstance(decoded["ok"], bool):
            raise KernelProtocolError("kernel response ok field must be boolean")
        if decoded["ok"]:
            if "result" not in decoded or decoded.get("error") is not None:
                raise KernelProtocolError("successful response must contain only a result")
        else:
            error = decoded.get("error")
            if not isinstance(error, dict) or set(error) != {"code", "message"}:
                raise KernelProtocolError("failed response must contain a structured error")
            if not all(isinstance(error[field], str) and error[field] for field in error):
                raise KernelProtocolError("kernel error code and message must be non-empty text")
            if decoded.get("result") is not None:
                raise KernelProtocolError("failed response cannot contain a result")
        return decoded


def run_once(
    command: str | Path | Sequence[str | Path],
    operation: str,
    payload: Mapping[str, Any],
    *,
    request_id: str = "python-once",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    with KernelSession(command, timeout_seconds=timeout_seconds, max_requests=1) as session:
        return session.request(operation, payload, request_id=request_id)
