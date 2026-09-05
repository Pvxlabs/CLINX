"""Minimal Codex app-server JSON-RPC client used by Dispatcher V1.

The transport is deliberately small and stdio-oriented: SSH starts the
configured remote app-server proxy/command and JSON objects are exchanged one
per line.  No Desktop IPC, credential inspection, or thread discovery is done
here.  Dispatch callers must provide an exact durable thread id.
"""

from __future__ import annotations

import dataclasses
import json
import re
import select
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


class JSONRPCTransport(Protocol):
    def send(self, message: dict[str, Any]) -> None: ...

    def receive(self, timeout_seconds: float) -> dict[str, Any]: ...

    def close(self) -> None: ...


class SSHStdioTransport:
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
        self.timeout_seconds = timeout_seconds
        self._popen = popen
        self._process: subprocess.Popen[str] | None = None

    def connect(self) -> None:
        if self._process is not None:
            return
        command = [
            self.ssh_binary,
            *self.ssh_args,
            self.ssh_alias,
            *self.remote_command,
        ]
        try:
            self._process = self._popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as exc:
            raise AppServerTransportError(f"failed to start SSH transport: {exc}") from exc

    def send(self, message: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise AppServerTransportError("SSH transport is not connected")
        try:
            self._process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AppServerTransportError(f"failed to write app-server request: {exc}") from exc

    def receive(self, timeout_seconds: float) -> dict[str, Any]:
        if self._process is None or self._process.stdout is None:
            raise AppServerTransportError("SSH transport is not connected")
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
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass


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
