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
MAX_JSON_DEPTH = 32
DEFAULT_TIMEOUT_SECONDS = 2.0
_IO_CHUNK_BYTES = 65_536


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


def _reject_non_finite(value: str) -> Any:
    raise KernelProtocolError(f"non-finite JSON constant is not allowed: {value}")


def _json_depth(value: Any) -> int:
    deepest = 1
    pending: list[tuple[Any, int]] = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        deepest = max(deepest, depth)
        if isinstance(current, Mapping):
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
    return deepest


def strict_json_loads(raw: bytes) -> Any:
    try:
        decoded = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_non_finite,
        )
        if _json_depth(decoded) > MAX_JSON_DEPTH + 8:
            raise KernelProtocolError("JSON nesting exceeds the adapter bound")
        return decoded
    except KernelProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
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
    payload = envelope.get("payload")
    if payload is not None and _json_depth(payload) > MAX_JSON_DEPTH:
        raise KernelProtocolError(f"request payload nesting exceeds {MAX_JSON_DEPTH}")
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
        self._generation = 0
        self._closed = False

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

    @property
    def generation(self) -> int:
        return self._generation

    def start(self) -> None:
        if self._closed:
            raise KernelProcessError("kernel session is closed; use a new session or restart()")
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
        self._generation += 1

    def _reset_stream_state(self) -> None:
        self._stdout.clear()
        self._stderr.clear()
        self._requests = 0

    def restart(self) -> None:
        """Explicitly start a fresh process generation after closing this one."""
        self.close()
        self._closed = False
        self._reset_stream_state()
        self.start()

    def close(self) -> None:
        process = self._process
        selector = self._selector
        self._process = None
        self._selector = None
        self._closed = True
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
        self._reset_stream_state()

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
        if self._closed:
            raise KernelProcessError("kernel session is closed; use a new session or restart()")
        if self._requests >= self.max_requests:
            raise KernelProtocolError("client process request bound reached")
        request_id = envelope.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise KernelProtocolError("request_id must be non-empty text")
        operation = envelope.get("operation")
        if not isinstance(operation, str) or not operation:
            raise KernelProtocolError("operation must be non-empty text")
        deadline = time.monotonic() + self.timeout_seconds
        frame = encode_request(envelope)
        self.start()
        process = self._process
        assert process is not None and process.stdin is not None
        try:
            self._reject_immediate_stdout()
            self._write_frame(process.stdin, frame, deadline, request_id)
            self._requests += 1
            raw = self._read_response(request_id, deadline)
            return self._validate_response(raw, request_id, operation)
        except (KernelProcessError, KernelProtocolError, BrokenPipeError, OSError):
            self.close()
            raise

    def _write_frame(
        self,
        stream: Any,
        frame: bytes,
        deadline: float,
        request_id: str,
    ) -> None:
        selector = self._selector
        process = self._process
        assert selector is not None and process is not None
        fd = stream.fileno()
        os.set_blocking(fd, False)
        selector.register(stream, selectors.EVENT_WRITE, "stdin")
        offset = 0
        try:
            while offset < len(frame):
                self._raise_if_child_exited(request_id)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise KernelProcessError(f"kernel request {request_id!r} timed out during stdin write")
                ready = selector.select(remaining)
                if not ready:
                    raise KernelProcessError(f"kernel request {request_id!r} timed out during stdin write")
                ready.sort(key=lambda item: item[0].data == "stdin")
                for key, _ in ready:
                    if key.data == "stdin":
                        try:
                            written = os.write(fd, frame[offset:])
                        except BlockingIOError:
                            continue
                        if written == 0:
                            raise KernelProcessError(
                                f"kernel process stopped accepting request {request_id!r}"
                            )
                        offset += written
                    else:
                        self._read_ready(key)
        finally:
            try:
                selector.unregister(stream)
            except KeyError:
                pass

    def _raise_if_child_exited(self, request_id: str) -> None:
        process = self._process
        if process is not None and process.poll() is not None:
            raise KernelProcessError(
                f"kernel process exited with code {process.returncode} before response {request_id!r}"
            )

    def _read_ready(self, key: selectors.SelectorKey) -> None:
        stream = key.data
        chunk = os.read(key.fileobj.fileno(), _IO_CHUNK_BYTES)
        if not chunk:
            selector = self._selector
            if selector is not None:
                try:
                    selector.unregister(key.fileobj)
                except KeyError:
                    pass
            return
        if stream == "stderr":
            self._stderr.extend(chunk)
            if len(self._stderr) > self.max_stderr_bytes:
                raise KernelProcessError("kernel stderr exceeded the byte bound")
        else:
            self._stdout.extend(chunk)
            if len(self._stdout) > self.max_response_bytes:
                raise KernelProtocolError("kernel response exceeded the byte bound")

    def _read_response(self, request_id: str, deadline: float) -> bytes:
        while True:
            newline = self._stdout.find(b"\n")
            if newline >= 0:
                if newline > self.max_response_bytes:
                    raise KernelProtocolError("kernel response exceeded the byte bound")
                self._drain_immediate_stderr()
                self._drain_immediate_stdout()
                raw = bytes(self._stdout[:newline])
                del self._stdout[: newline + 1]
                if self._stdout:
                    raise KernelProtocolError("kernel emitted unsolicited stdout after response")
                return raw
            if len(self._stdout) > self.max_response_bytes:
                raise KernelProtocolError("kernel response exceeded the byte bound")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise KernelProcessError(f"kernel request {request_id!r} timed out while reading response")
            selector = self._selector
            assert selector is not None
            ready = selector.select(remaining)
            if not ready:
                continue
            # Drain diagnostics first so a child cannot hide an over-limit stderr
            # write by racing a syntactically valid stdout line.
            ready.sort(key=lambda item: item[0].data != "stderr")
            for key, _ in ready:
                self._read_ready(key)
            process = self._process
            if process is not None and process.poll() is not None and not selector.get_map():
                if self._stdout:
                    raise KernelProtocolError("kernel output was truncated before newline")
                code = process.returncode
                raise KernelProcessError(
                    f"kernel process exited with code {code} before response {request_id!r}"
                )

    def _drain_immediate_stderr(self) -> None:
        """Consume already-readable diagnostics before accepting stdout."""
        selector = self._selector
        if selector is None:
            return
        for _ in range(2):
            ready = [item for item in selector.select(0) if item[0].data == "stderr"]
            if not ready:
                return
            for key, _ in ready:
                self._read_ready(key)

    def _drain_immediate_stdout(self) -> None:
        """Detect protocol bytes already queued after the current response."""
        selector = self._selector
        if selector is None:
            return
        for _ in range(2):
            ready = [item for item in selector.select(0) if item[0].data == "stdout"]
            if not ready:
                return
            for key, _ in ready:
                self._read_ready(key)

    def _reject_immediate_stdout(self) -> None:
        self._drain_immediate_stdout()
        if self._stdout:
            raise KernelProtocolError("kernel emitted unsolicited stdout before request")

    @staticmethod
    def _validate_response(raw: bytes, request_id: str, operation: str) -> dict[str, Any]:
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
            KernelSession._validate_result(decoded["result"], operation)
        else:
            error = decoded.get("error")
            if not isinstance(error, dict) or set(error) != {"code", "message"}:
                raise KernelProtocolError("failed response must contain a structured error")
            if not all(isinstance(error[field], str) and error[field] for field in error):
                raise KernelProtocolError("kernel error code and message must be non-empty text")
            if decoded.get("result") is not None:
                raise KernelProtocolError("failed response cannot contain a result")
        return decoded

    @staticmethod
    def _validate_result(result: Any, operation: str) -> None:
        if not isinstance(result, dict):
            raise KernelProtocolError("successful kernel result must be an object")
        if _json_depth(result) > MAX_JSON_DEPTH + 8:
            raise KernelProtocolError("kernel result nesting exceeds the adapter bound")
        if operation == "evaluate_ownership":
            required = {"authority", "decision", "reason_code", "details"}
            if set(result) != required:
                raise KernelProtocolError("ownership result does not match CLINX_KERNEL_V1")
            if result["authority"] != AUTHORITY or result["decision"] not in {
                "ALLOW_CANDIDATE", "REJECT_CANDIDATE", "INSUFFICIENT_EVIDENCE"
            }:
                raise KernelProtocolError("ownership result has an invalid decision contract")
            if not isinstance(result["reason_code"], str) or not result["reason_code"]:
                raise KernelProtocolError("ownership result reason_code must be non-empty text")
            if not isinstance(result["details"], dict):
                raise KernelProtocolError("ownership result details must be an object")
            return
        if operation == "replay_assignment":
            required = {"event_family", "event_types", "not_covered_fields", "state", "states", "stream_version"}
            optional = {"stream_type", "stream_id"}
            if set(result) - required - optional or required - set(result):
                raise KernelProtocolError("replay result does not match CLINX_KERNEL_V1")
            if result["event_family"] != "RUNTIME_WORKER_V1":
                raise KernelProtocolError("replay result event family is invalid")
            if not isinstance(result["event_types"], list) or not all(isinstance(item, str) for item in result["event_types"]):
                raise KernelProtocolError("replay result event_types are invalid")
            if not isinstance(result["not_covered_fields"], list) or not all(isinstance(item, str) for item in result["not_covered_fields"]):
                raise KernelProtocolError("replay result not_covered_fields are invalid")
            if isinstance(result["stream_version"], bool) or not isinstance(result["stream_version"], int) or result["stream_version"] < 0:
                raise KernelProtocolError("replay result stream_version is invalid")
            if not isinstance(result["states"], dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in result["states"].items()):
                raise KernelProtocolError("replay result states are invalid")
            if result["state"] is not None and not isinstance(result["state"], dict):
                raise KernelProtocolError("replay result state is invalid")
            return
        raise KernelProtocolError(f"unknown operation result contract: {operation}")


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
