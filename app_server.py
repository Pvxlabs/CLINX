"""Minimal Codex app-server JSON-RPC client used by Dispatcher V1.

Codex's ``app-server proxy`` forwards raw bytes to the existing control
socket.  The control socket is a Unix-domain WebSocket endpoint, so the bridge
owns the small WebSocket handshake/framing layer and keeps JSON-RPC handling
above it.  No Desktop IPC, credential inspection, or thread discovery is done
here. Dispatch callers must provide an exact durable thread id.
"""

from __future__ import annotations

import dataclasses
import base64
import hashlib
import json
import os
import re
import select
import struct
import subprocess
import time
import uuid
from typing import Any, Callable, Protocol


class AppServerError(RuntimeError):
    """Base error for transport, protocol, and app-server failures."""


class AppServerTransportError(AppServerError):
    pass


class AppServerProtocolError(AppServerError):
    pass


class AppServerRemoteError(AppServerError):
    def __init__(self, method: str, error: dict[str, Any]):
        message = error.get("message", "unknown app-server error")
        code = error.get("code")
        suffix = f" (code {code})" if code is not None else ""
        super().__init__(f"{method} failed{suffix}: {message}")
        self.method = method
        self.error = error


def _process_group_id(process: Any) -> int | None:
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        return os.getpgid(pid)
    except (OSError, ProcessLookupError):
        return None


def _terminate_process_group(process: Any) -> int | None:
    """Terminate a proxy wrapper and return its group id for final cleanup."""
    pgid = _process_group_id(process)
    if pgid is not None:
        try:
            os.killpg(pgid, 15)
            return pgid
        except (OSError, ProcessLookupError):
            pass
    pid = getattr(process, "pid", None)
    terminate = getattr(process, "terminate", None)
    if terminate:
        terminate()
    return pgid


def _kill_process_group(process: Any, pgid: int | None = None) -> None:
    pgid = pgid if pgid is not None else _process_group_id(process)
    if pgid is not None:
        try:
            os.killpg(pgid, 9)
            return
        except (OSError, ProcessLookupError):
            pass
    kill = getattr(process, "kill", None)
    if kill:
        kill()


class JSONRPCTransport(Protocol):
    def send(self, message: dict[str, Any]) -> None: ...

    def receive(self, timeout_seconds: float) -> dict[str, Any]: ...

    def close(self) -> None: ...


class ProcessStdioTransport:
    """Exchange line-delimited JSON with a child process over stdio."""

    def __init__(
        self,
        command: tuple[str, ...],
        *,
        timeout_seconds: float = 30.0,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ):
        if not command:
            raise AppServerTransportError("app-server command is required")
        self.command = command
        self.timeout_seconds = timeout_seconds
        self._popen = popen
        self._process: subprocess.Popen[str] | None = None

    def connect(self) -> None:
        if self._process is not None:
            return
        try:
            self._process = self._popen(
                list(self.command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
                text=True,
                encoding="utf-8",
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            raise AppServerTransportError(
                f"failed to start app-server transport: {exc}"
            ) from exc

    def send(self, message: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise AppServerTransportError("app-server transport is not connected")
        try:
            self._process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AppServerTransportError(f"failed to write app-server request: {exc}") from exc

    def receive(self, timeout_seconds: float) -> dict[str, Any]:
        if self._process is None or self._process.stdout is None:
            raise AppServerTransportError("app-server transport is not connected")
        ready, _, _ = select.select([self._process.stdout], [], [], timeout_seconds)
        if not ready:
            raise AppServerTransportError(
                f"timed out waiting for app-server response after {timeout_seconds:.1f}s"
            )
        line = self._process.stdout.readline()
        if not line:
            code = self._process.poll()
            raise AppServerTransportError(
                "app-server transport closed" + (f" (exit {code})" if code is not None else "")
            )
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AppServerProtocolError("app-server returned malformed JSON") from exc
        if not isinstance(message, dict):
            raise AppServerProtocolError("app-server message must be a JSON object")
        return message

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        pgid = _terminate_process_group(process)
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        # The node wrapper can exit while its native proxy child remains in
        # the original group, so finish the group explicitly after waiting.
        _kill_process_group(process, pgid)


class ProcessByteTransport:
    """Exchange raw bytes with a child process over stdio."""

    def __init__(
        self,
        command: tuple[str, ...],
        *,
        timeout_seconds: float = 30.0,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ):
        if not command:
            raise AppServerTransportError("app-server command is required")
        self.command = command
        self.timeout_seconds = timeout_seconds
        self._popen = popen
        self._process: subprocess.Popen[bytes] | None = None

    def connect(self) -> None:
        if self._process is not None:
            return
        try:
            self._process = self._popen(
                list(self.command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
                text=False,
                bufsize=0,
                start_new_session=True,
            )
        except OSError as exc:
            raise AppServerTransportError(
                f"failed to start app-server transport: {exc}"
            ) from exc

    def _stdout(self):
        if self._process is None or self._process.stdout is None:
            raise AppServerTransportError("app-server transport is not connected")
        return self._process.stdout

    def send_bytes(self, payload: bytes) -> None:
        if self._process is None or self._process.stdin is None:
            raise AppServerTransportError("app-server transport is not connected")
        try:
            self._process.stdin.write(payload)
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AppServerTransportError(f"failed to write app-server bytes: {exc}") from exc

    def receive_bytes(self, length: int, timeout_seconds: float) -> bytes:
        if length < 0:
            raise AppServerTransportError("byte read length must not be negative")
        if length == 0:
            return b""
        stream = self._stdout()
        deadline = time.monotonic() + timeout_seconds
        result = bytearray()
        while len(result) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerTransportError(
                    f"timed out waiting for {length} app-server bytes"
                )
            try:
                fd = stream.fileno()
            except (AttributeError, OSError):
                fd = None
            if fd is not None:
                ready, _, _ = select.select([fd], [], [], remaining)
                if not ready:
                    raise AppServerTransportError(
                        f"timed out waiting for {length} app-server bytes"
                    )
                chunk = os.read(fd, length - len(result))
            else:
                chunk = stream.read(length - len(result))
            if not chunk:
                code = self._process.poll() if self._process is not None else None
                raise AppServerTransportError(
                    "app-server byte transport closed"
                    + (f" (exit {code})" if code is not None else "")
                )
            result.extend(chunk)
        return bytes(result)

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        pgid = _terminate_process_group(process)
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        _kill_process_group(process, pgid)


class WebSocketStdioTransport:
    """Speak WebSocket frames over a raw stdio byte tunnel."""

    _MAX_FRAME_BYTES = 16 * 1024 * 1024

    def __init__(
        self,
        command: tuple[str, ...],
        *,
        timeout_seconds: float = 30.0,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        websocket_path: str = "/",
    ):
        if not websocket_path.startswith("/"):
            raise AppServerTransportError("websocket path must start with '/'")
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.websocket_path = websocket_path
        self._byte_transport = ProcessByteTransport(
            command,
            timeout_seconds=timeout_seconds,
            popen=popen,
        )
        self._connected = False

    def connect(self) -> None:
        if self._connected:
            return
        self._byte_transport.connect()
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.websocket_path} HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        self._byte_transport.send_bytes(request)
        header = self._read_http_header()
        lines = header.decode("latin1").split("\r\n")
        if not lines or not lines[0].startswith("HTTP/1.1 101"):
            status = lines[0] if lines else "empty response"
            raise AppServerProtocolError(f"app-server websocket handshake failed: {status}")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.lower()] = value.strip()
        expected_accept = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected_accept:
            raise AppServerProtocolError("app-server websocket accept mismatch")
        self._connected = True

    def _read_http_header(self) -> bytes:
        result = bytearray()
        deadline = time.monotonic() + self.timeout_seconds
        while b"\r\n\r\n" not in result:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerTransportError("timed out during app-server websocket handshake")
            result.extend(self._byte_transport.receive_bytes(1, remaining))
            if len(result) > 64 * 1024:
                raise AppServerProtocolError("app-server websocket handshake is too large")
        return bytes(result).split(b"\r\n\r\n", 1)[0]

    def send(self, message: dict[str, Any]) -> None:
        if not self._connected:
            raise AppServerTransportError("app-server websocket is not connected")
        payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self._send_frame(payload, opcode=0x1)

    def _send_frame(self, payload: bytes, *, opcode: int) -> None:
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes([0x80 | opcode, 0x80 | length])
        elif length <= 0xFFFF:
            header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", length)
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack("!Q", length)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self._byte_transport.send_bytes(header + mask + masked)

    def _receive_frame(self, timeout_seconds: float) -> tuple[int, bytes, bool]:
        first, second = self._byte_transport.receive_bytes(2, timeout_seconds)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._byte_transport.receive_bytes(2, timeout_seconds))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._byte_transport.receive_bytes(8, timeout_seconds))[0]
        if length > self._MAX_FRAME_BYTES:
            raise AppServerProtocolError("app-server websocket frame is too large")
        mask = self._byte_transport.receive_bytes(4, timeout_seconds) if masked else b""
        payload = bytearray(self._byte_transport.receive_bytes(length, timeout_seconds))
        if masked:
            for index in range(length):
                payload[index] ^= mask[index % 4]
        return opcode, bytes(payload), fin

    def receive(self, timeout_seconds: float) -> dict[str, Any]:
        fragments = bytearray()
        fragmented_opcode: int | None = None
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerTransportError("timed out waiting for app-server websocket message")
            opcode, payload, fin = self._receive_frame(remaining)
            if opcode == 0x9:  # ping
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:
                raise AppServerTransportError("app-server websocket closed")
            if opcode in {0x1, 0x2}:
                if fragmented_opcode is not None:
                    raise AppServerProtocolError("nested app-server websocket fragment")
                fragmented_opcode = opcode
                fragments.extend(payload)
            elif opcode == 0x0:
                if fragmented_opcode is None:
                    raise AppServerProtocolError("unexpected app-server websocket continuation")
                fragments.extend(payload)
            else:
                raise AppServerProtocolError(f"unsupported app-server websocket opcode: {opcode}")
            if not fin:
                continue
            if fragmented_opcode != 0x1:
                raise AppServerProtocolError("app-server websocket message is not text")
            try:
                message = json.loads(bytes(fragments).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AppServerProtocolError("app-server returned malformed websocket JSON") from exc
            if not isinstance(message, dict):
                raise AppServerProtocolError("app-server message must be a JSON object")
            return message

    def close(self) -> None:
        self._connected = False
        self._byte_transport.close()


class SSHStdioTransport(WebSocketStdioTransport):
    """Run a configured remote app-server command over an SSH stdio channel."""

    def __init__(
        self,
        ssh_alias: str,
        remote_command: tuple[str, ...],
        *,
        ssh_binary: str = "ssh",
        ssh_args: tuple[str, ...] = ("-T",),
        timeout_seconds: float = 30.0,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ):
        if not ssh_alias:
            raise AppServerTransportError("SSH alias is required")
        if not remote_command:
            raise AppServerTransportError("remote app-server command is required")
        self.ssh_alias = ssh_alias
        self.remote_command = remote_command
        self.ssh_binary = ssh_binary
        self.ssh_args = ssh_args
        super().__init__(
            (ssh_binary, *ssh_args, ssh_alias, *remote_command),
            timeout_seconds=timeout_seconds,
            popen=popen,
        )


class LocalStdioTransport(WebSocketStdioTransport):
    """Run the local app-server proxy command over a stdio byte tunnel."""


@dataclasses.dataclass(frozen=True)
class InitializeInfo:
    server_name: str | None
    server_version: str | None
    user_agent: str | None


@dataclasses.dataclass(frozen=True)
class TurnStartInfo:
    turn_id: str
    model: str | None
    reasoning_effort: str | None


class CodexAppServerClient:
    """Synchronous JSON-RPC client with notification/event draining."""

    def __init__(self, transport: JSONRPCTransport, timeout_seconds: float = 30.0):
        self.transport = transport
        self.timeout_seconds = timeout_seconds
        self.events: list[str] = []
        self.initialize_info: InitializeInfo | None = None

    def __enter__(self) -> "CodexAppServerClient":
        connect = getattr(self.transport, "connect", None)
        if connect:
            connect()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def close(self) -> None:
        self.transport.close()

    def _send_server_response(self, request: dict[str, Any]) -> None:
        # M0 intentionally has no approval UI.  Reply explicitly so a server
        # request cannot leave the connection waiting indefinitely.
        self.transport.send(
            {
                "id": request.get("id"),
                "error": {
                    "code": -32601,
                    "message": "linear-local-codex-bridge does not handle server requests",
                },
            }
        )

    def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request_id = str(uuid.uuid4())
        request: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        self.transport.send(request)

        deadline = time.monotonic() + self.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerTransportError(f"timed out waiting for {method} response")
            message = self.transport.receive(remaining)

            # A server-initiated request has both id and method.  Handle it
            # before matching response ids because both sides share the id space.
            if "method" in message and "id" in message:
                self._send_server_response(message)
                continue

            if message.get("id") != request_id:
                if "method" in message:
                    event_method = message["method"]
                    if isinstance(event_method, str):
                        self.events.append(event_method)
                continue

            if "error" in message:
                error = message["error"]
                if not isinstance(error, dict):
                    error = {"message": str(error)}
                raise AppServerRemoteError(method, error)
            if "result" not in message:
                raise AppServerProtocolError(f"{method} response has no result or error")
            return message["result"]

    def initialize(
        self,
        *,
        client_name: str,
        client_title: str,
        client_version: str,
    ) -> InitializeInfo:
        result = self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": client_name,
                    "title": client_title,
                    "version": client_version,
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        if not isinstance(result, dict):
            raise AppServerProtocolError("initialize result must be an object")
        server_info = result.get("serverInfo") or {}
        if not isinstance(server_info, dict):
            server_info = {}
        self.transport.send({"method": "initialized"})
        self.initialize_info = InitializeInfo(
            server_name=_optional_string(server_info.get("name")),
            server_version=_optional_string(server_info.get("version")),
            user_agent=_optional_string(result.get("userAgent")),
        )
        return self.initialize_info

    def thread_read(self, thread_id: str) -> dict[str, Any]:
        result = self._request("thread/read", {"threadId": thread_id})
        return _thread_result(result, "thread/read")

    def thread_start(
        self,
        *,
        cwd: str,
        model: str | None = None,
        project_id: str | None = None,
        sandbox: str | None = None,
        ephemeral: bool = False,
        thread_source: str | None = None,
    ) -> dict[str, Any]:
        """Create a durable thread for controlled pilot qualification."""
        params: dict[str, Any] = {"cwd": cwd, "ephemeral": ephemeral}
        if model is not None:
            params["model"] = model
        if project_id is not None:
            params["projectId"] = project_id
        if sandbox is not None:
            params["sandbox"] = sandbox
        if thread_source is not None:
            params["threadSource"] = thread_source
        result = self._request("thread/start", params)
        return _thread_result(result, "thread/start")

    def thread_resume(self, thread_id: str) -> dict[str, Any]:
        result = self._request("thread/resume", {"threadId": thread_id})
        return _thread_result(result, "thread/resume")

    def model_list(self) -> dict[str, Any]:
        result = self._request("model/list", {})
        if not isinstance(result, dict):
            raise AppServerProtocolError("model/list result must be an object")
        return result

    def turn_start(
        self,
        thread_id: str,
        prompt: str,
        *,
        cwd: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        approval_policy: str | None = None,
    ) -> TurnStartInfo:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "cwd": cwd,
        }
        if model is not None:
            params["model"] = model
        if reasoning_effort is not None:
            params["effort"] = reasoning_effort
        if approval_policy is not None:
            params["approvalPolicy"] = approval_policy
        result = self._request("turn/start", params)
        if not isinstance(result, dict):
            raise AppServerProtocolError("turn/start result must be an object")
        turn = result.get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            raise AppServerProtocolError("turn/start result is missing turn.id")
        return TurnStartInfo(
            turn_id=turn["id"],
            model=_optional_string(result.get("model")) or model,
            reasoning_effort=(
                _optional_string(result.get("reasoningEffort")) or reasoning_effort
            ),
        )


def _thread_result(result: Any, method: str) -> dict[str, Any]:
    if not isinstance(result, dict) or not isinstance(result.get("thread"), dict):
        raise AppServerProtocolError(f"{method} result is missing thread")
    return result["thread"]


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def extract_version(value: str | None) -> tuple[int, int, int] | None:
    """Extract a Codex semver from serverInfo or userAgent for a soft guard."""
    if not value:
        return None
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", value)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def versions_compatible(expected: str | None, actual: str | None) -> bool:
    """Reject only a known major-version conflict; patch skew is allowed."""
    if not expected or not actual:
        return True
    expected_version = extract_version(expected)
    actual_version = extract_version(actual)
    if expected_version is None or actual_version is None:
        return True
    return expected_version[0] == actual_version[0]
