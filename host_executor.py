"""Policy-bound trusted host operations for the P620 CLINX runtime."""

from __future__ import annotations

import dataclasses
import datetime as _datetime
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any

from execution_policy import (
    BUSINESS_ACTION,
    DEVELOPMENT_MUTATION,
    HOST_EXECUTOR,
    PRODUCTION_MUTATION,
    ExecutionPolicy,
)
from execution_semantics import RoutingIdentity, normalize_host
from task_registry import TaskRegistry, TaskRegistryError


class HostExecutorError(RuntimeError):
    code = "HOST_EXECUTOR_ERROR"


class HostExecutorUnavailable(HostExecutorError):
    code = "HOST_EXECUTOR_UNAVAILABLE"


class CapabilityUnavailable(HostExecutorError):
    code = "CAPABILITY_UNAVAILABLE"


class AuthorityDenied(HostExecutorError):
    code = "AUTHORITY_DENIED"


class TargetNotRegistered(HostExecutorError):
    code = "TARGET_NOT_REGISTERED"


@dataclasses.dataclass(frozen=True)
class RegisteredTarget:
    alias: str
    operation_classes: tuple[str, ...]
    dns_name: str | None = None
    url: str | None = None


@dataclasses.dataclass(frozen=True)
class HostExecutorConfig:
    enabled: bool = False
    host: str = "p620"
    default_timeout_seconds: float = 30.0
    max_timeout_seconds: float = 120.0
    max_output_bytes: int = 65536
    services: tuple[RegisteredTarget, ...] = ()
    ssh_targets: tuple[RegisteredTarget, ...] = ()
    network_targets: tuple[RegisteredTarget, ...] = ()


@dataclasses.dataclass(frozen=True)
class HostExecutionRequest:
    task_ref: str
    execution_ref: str
    route: RoutingIdentity
    policy: ExecutionPolicy
    operation_class: str
    capability: str
    operation: str
    arguments: dict[str, Any]
    project_root: Path
    timeout_seconds: float | None = None


@dataclasses.dataclass(frozen=True)
class _Command:
    argv: tuple[str, ...]
    target_identity: str | None = None
    mutating: bool = False


def _now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


def _target_map(values: tuple[RegisteredTarget, ...]) -> dict[str, RegisteredTarget]:
    return {item.alias.casefold(): item for item in values}


class HostExecutor:
    """Execute only registered structured operations in the real host namespace."""

    _SENSITIVE_PATTERNS = (
        re.compile(
            r"(?i)\b(token|secret|password|api[_-]?key|authorization)\b"
            r"(\s*[:=]\s*)([^\s,;]+)"
        ),
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
        re.compile(r"\b(?:sk|rk|pk)_[A-Za-z0-9_-]{16,}\b"),
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    )

    def __init__(self, config: HostExecutorConfig, registry: TaskRegistry):
        self.config = config
        self.registry = registry
        self.instance_id = "host_executor_" + uuid.uuid4().hex
        self._active: dict[str, tuple[str, subprocess.Popen[bytes]]] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.RLock()
        self._capability_cache: tuple[float, dict[str, Any]] | None = None
        self.registry.reconcile_stale_host_executions(self.instance_id)

    @staticmethod
    def dynamic_tool_spec() -> dict[str, Any]:
        return {
            "type": "function",
            "name": "clinx_host_operation",
            "description": (
                "Request one structured operation from the CLINX-managed trusted P620 "
                "host executor. The task, execution, route, cwd, and targets are supplied "
                "and validated by CLINX; no shell, PID, cwd, or raw host is accepted."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "operation_class": {
                        "type": "string",
                        "enum": [
                            "READ_ONLY_HOST",
                            "DEVELOPMENT_MUTATION",
                            "PRODUCTION_READ_ONLY",
                            "PRODUCTION_MUTATION",
                            "BUSINESS_ACTION",
                        ],
                    },
                    "capability": {
                        "type": "string",
                        "enum": [
                            "LOCAL_HOST_PROCESS",
                            "SYSTEMD_USER",
                            "OUTBOUND_NETWORK",
                            "SSH",
                            "HOST_FILESYSTEM",
                        ],
                    },
                    "operation": {"type": "string"},
                    "arguments": {"type": "object"},
                    "timeout_seconds": {
                        "type": "number",
                        "minimum": 0.1,
                        "maximum": 120,
                    },
                },
                "required": [
                    "operation_class", "capability", "operation", "arguments"
                ],
                "additionalProperties": False,
            },
        }

    def _clean_environment(self) -> dict[str, str]:
        runtime_dir = f"/run/user/{os.getuid()}"
        values = {
            "HOME": str(Path.home()),
            "PATH": self._executor_path(),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "USER": pwd.getpwuid(os.getuid()).pw_name,
            "LOGNAME": pwd.getpwuid(os.getuid()).pw_name,
            "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", runtime_dir),
            "DBUS_SESSION_BUS_ADDRESS": os.environ.get(
                "DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus"
            ),
            "AWS_PROFILE": "p620",
        }
        if os.environ.get("SSH_AUTH_SOCK"):
            values["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
        return values

    @staticmethod
    def _executor_path() -> str:
        return os.pathsep.join(
            (
                str(Path.home() / ".local" / "bin"),
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
            )
        )

    @classmethod
    def _redact(cls, value: str) -> str:
        result = value
        result = cls._SENSITIVE_PATTERNS[0].sub(
            lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", result
        )
        for pattern in cls._SENSITIVE_PATTERNS[1:]:
            result = pattern.sub("[REDACTED]", result)
        return result

    def _capture(self, stream: Any) -> tuple[str, int, str, bool]:
        stream.flush()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(0)
        digest = hashlib.sha256()
        retained = bytearray()
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            digest.update(chunk)
            if len(retained) < self.config.max_output_bytes:
                retained.extend(chunk[: self.config.max_output_bytes - len(retained)])
        text = retained.decode("utf-8", errors="replace")
        return self._redact(text), size, digest.hexdigest(), size > len(retained)

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                process.terminate()
            except (OSError, ProcessLookupError):
                return
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                try:
                    process.kill()
                except (OSError, ProcessLookupError):
                    pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    @staticmethod
    def _arguments(
        values: dict[str, Any],
        *,
        required: tuple[str, ...] = (),
        optional: tuple[str, ...] = (),
    ) -> None:
        if not isinstance(values, dict):
            raise HostExecutorError("operation arguments must be an object")
        missing = [name for name in required if name not in values]
        extra = set(values).difference(required).difference(optional)
        if missing or extra:
            raise HostExecutorError(
                "invalid operation arguments"
                + (f"; missing={','.join(missing)}" if missing else "")
                + (f"; extra={','.join(sorted(extra))}" if extra else "")
            )

    @staticmethod
    def _text_argument(values: dict[str, Any], name: str) -> str:
        value = values.get(name)
        if not isinstance(value, str) or not value.strip():
            raise HostExecutorError(f"{name} must be a non-empty string")
        return value.strip()

    def _registered_target(
        self,
        values: tuple[RegisteredTarget, ...],
        alias: str,
        operation_class: str,
    ) -> RegisteredTarget:
        target = _target_map(values).get(alias.casefold())
        if target is None:
            raise TargetNotRegistered(f"target is not registered: {alias}")
        if operation_class not in target.operation_classes:
            raise AuthorityDenied(
                f"operation class {operation_class} is not allowed for target {target.alias}"
            )
        return target

    @staticmethod
    def _relative_path(root: Path, value: str) -> Path:
        candidate = Path(value)
        if candidate.is_absolute():
            raise TargetNotRegistered("absolute filesystem paths are not accepted")
        resolved_root = root.resolve()
        resolved = (resolved_root / candidate).resolve()
        try:
            resolved.relative_to(resolved_root)
        except ValueError as exc:
            raise TargetNotRegistered("filesystem target is outside the registered project") from exc
        return resolved

    def _command(self, request: HostExecutionRequest) -> _Command:
        capability = request.capability
        operation = request.operation
        arguments = request.arguments

        if capability == "LOCAL_HOST_PROCESS":
            commands = {
                "host_identity": ("hostname",),
                "user_identity": ("whoami",),
                "working_directory": ("pwd",),
                "uptime": ("uptime",),
            }
            if operation in commands:
                self._arguments(arguments)
                return _Command(commands[operation])
            if operation == "service_process":
                self._arguments(arguments, required=("target",))
                alias = self._text_argument(arguments, "target")
                target = self._registered_target(
                    self.config.services, alias, request.operation_class
                )
                return _Command(
                    (
                        "systemctl", "--user", "show", target.alias,
                        "--property=MainPID", "--property=ExecStart", "--no-pager",
                    ),
                    target.alias,
                )

        if capability == "SYSTEMD_USER":
            self._arguments(
                arguments,
                required=("target",),
                optional=("lines",),
            )
            alias = self._text_argument(arguments, "target")
            target = self._registered_target(
                self.config.services, alias, request.operation_class
            )
            if operation == "service_is_active":
                return _Command(("systemctl", "--user", "is-active", target.alias), target.alias)
            if operation == "service_status":
                return _Command(
                    ("systemctl", "--user", "status", target.alias, "--no-pager"),
                    target.alias,
                )
            if operation == "service_restart":
                if request.operation_class not in {
                    DEVELOPMENT_MUTATION, PRODUCTION_MUTATION
                }:
                    raise AuthorityDenied("service_restart requires a mutation operation class")
                return _Command(
                    ("systemctl", "--user", "restart", target.alias),
                    target.alias,
                    True,
                )
            if operation == "service_journal":
                lines = arguments.get("lines", 50)
                if not isinstance(lines, int) or not 1 <= lines <= 500:
                    raise HostExecutorError("journal lines must be between 1 and 500")
                return _Command(
                    (
                        "journalctl", "--user", "-u", target.alias,
                        "-n", str(lines), "--no-pager",
                    ),
                    target.alias,
                )

        if capability == "OUTBOUND_NETWORK":
            self._arguments(arguments, required=("target",))
            alias = self._text_argument(arguments, "target")
            target = self._registered_target(
                self.config.network_targets, alias, request.operation_class
            )
            if operation == "dns_lookup" and target.dns_name:
                return _Command(("getent", "hosts", target.dns_name), target.alias)
            if operation == "https_head" and target.url:
                return _Command(
                    (
                        "curl", "--head", "--location", "--silent", "--show-error",
                        "--max-time", "20", target.url,
                    ),
                    target.alias,
                )

        if capability == "SSH":
            self._arguments(arguments, required=("target",))
            alias = self._text_argument(arguments, "target")
            target = self._registered_target(
                self.config.ssh_targets, alias, request.operation_class
            )
            remote = {
                "remote_hostname": "hostname",
                "remote_uptime": "uptime",
                "remote_true": "true",
            }.get(operation)
            if remote:
                timeout = min(
                    int(request.timeout_seconds or self.config.default_timeout_seconds), 30
                )
                return _Command(
                    (
                        "ssh", "-T", "-o", "BatchMode=yes", "-o",
                        f"ConnectTimeout={max(1, timeout)}", "--", target.alias, remote,
                    ),
                    target.alias,
                )

        if capability == "HOST_FILESYSTEM":
            if operation == "git_head":
                self._arguments(arguments)
                return _Command(("git", "rev-parse", "HEAD"), request.route.workspace.project_alias)
            if operation == "git_status":
                self._arguments(arguments)
                return _Command(
                    ("git", "status", "--short", "--branch"),
                    request.route.workspace.project_alias,
                )
            if operation == "path_read":
                self._arguments(arguments, required=("path",), optional=("lines",))
                relative = self._text_argument(arguments, "path")
                path = self._relative_path(request.project_root, relative)
                lines = arguments.get("lines", 100)
                if not isinstance(lines, int) or not 1 <= lines <= 500:
                    raise HostExecutorError("read lines must be between 1 and 500")
                return _Command(("head", "-n", str(lines), "--", str(path)), relative)
            if operation in {"marker_create", "marker_remove"}:
                self._arguments(arguments, required=("name",))
                name = self._text_argument(arguments, "name")
                if not re.fullmatch(r"\.clinx-host-executor-[a-z0-9-]{1,80}", name):
                    raise TargetNotRegistered("marker name is outside the CLINX marker namespace")
                path = self._relative_path(request.project_root, name)
                if request.operation_class != DEVELOPMENT_MUTATION:
                    raise AuthorityDenied("filesystem marker operation requires DEVELOPMENT_MUTATION")
                argv = ("touch", "--", str(path)) if operation == "marker_create" else (
                    "rm", "-f", "--", str(path)
                )
                return _Command(argv, name, True)

        raise CapabilityUnavailable(
            f"operation {operation!r} is not supported by capability {capability}"
        )

    def _validate(self, request: HostExecutionRequest) -> _Command:
        if not self.config.enabled:
            raise HostExecutorUnavailable("trusted host executor is disabled")
        configured_host = normalize_host(self.config.host).stable_identifier
        if request.route.host.stable_identifier != configured_host:
            raise HostExecutorUnavailable("executor host does not match the sealed route")
        if request.route.surface.stable_identifier != "host_executor":
            raise AuthorityDenied("execution surface is not HOST_EXECUTOR")
        if request.policy.execution_surface != HOST_EXECUTOR:
            raise AuthorityDenied("sealed execution policy is not HOST_EXECUTOR")
        request.policy.validate_route(request.route)
        if request.operation_class == BUSINESS_ACTION:
            raise AuthorityDenied("AI_DOES_NOT_DECIDE_LIVE_BUSINESS_ACTION")
        if not request.policy.permits(request.operation_class, request.capability):
            raise AuthorityDenied("operation is outside sealed task authority")
        task = self.registry.get_task(request.task_ref)
        if normalize_host(task.host).stable_identifier != request.route.host.stable_identifier:
            raise TargetNotRegistered("executor host does not match the registered task")
        if task.workspace_alias.casefold() != request.route.workspace.workspace_alias.casefold():
            raise TargetNotRegistered("executor workspace does not match the registered task")
        if task.project_alias.casefold() != request.route.workspace.project_alias.casefold():
            raise TargetNotRegistered("executor project does not match the registered task")
        if Path(task.cwd).resolve() != request.project_root.resolve():
            raise TargetNotRegistered("executor cwd does not match the registered project")
        expected_worktree = self.registry.worktree_key(
            host=task.host,
            cwd=task.cwd,
            repository_origin=task.repository_origin,
        )
        if request.route.workspace.worktree_key != expected_worktree:
            raise TargetNotRegistered("executor route does not match the registered worktree")
        command = self._command(request)
        self.registry.validate_host_operation_binding(
            task_id=request.task_ref,
            execution_ref=request.execution_ref,
            routing_identity=request.route,
            execution_policy=request.policy,
            mutating=command.mutating,
        )
        return command

    def execute(self, request: HostExecutionRequest) -> dict[str, Any]:
        command = self._validate(request)
        timeout = request.timeout_seconds or self.config.default_timeout_seconds
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise HostExecutorError("timeout_seconds must be positive")
        timeout = min(float(timeout), self.config.max_timeout_seconds)
        host_execution_ref = "hostexec_" + uuid.uuid4().hex
        started_at = _now()
        argv_json = json.dumps(
            [self._redact(value) for value in command.argv], separators=(",", ":")
        )
        self.registry.begin_host_execution(
            host_execution_ref=host_execution_ref,
            task_id=request.task_ref,
            execution_ref=request.execution_ref,
            routing_identity_json=request.route.to_json(),
            execution_policy_json=request.policy.to_json(),
            host=request.route.host.stable_identifier,
            surface=request.route.surface.stable_identifier,
            operation_class=request.operation_class,
            capability=request.capability,
            operation=request.operation,
            argv_json=argv_json,
            cwd_identity=request.route.workspace.project_alias,
            target_identity=command.target_identity,
            started_at=started_at,
            result_state="RUNNING",
            timeout_seconds=timeout,
            executor_instance=self.instance_id,
        )

        began = time.monotonic()
        exit_code: int | None = None
        timed_out = False
        error_text = ""
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                with self._lock:
                    cancelled_before_start = request.execution_ref in self._cancelled
                    if not cancelled_before_start:
                        process = subprocess.Popen(
                            list(command.argv),
                            cwd=str(request.project_root),
                            env=self._clean_environment(),
                            stdin=subprocess.DEVNULL,
                            stdout=stdout_file,
                            stderr=stderr_file,
                            shell=False,
                            start_new_session=True,
                        )
                        self._active[host_execution_ref] = (request.execution_ref, process)
                if not cancelled_before_start:
                    try:
                        exit_code = process.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        self._terminate(process)
                        exit_code = process.returncode
            except OSError as exc:
                error_text = self._redact(str(exc))
            finally:
                with self._lock:
                    self._active.pop(host_execution_ref, None)

            stdout, stdout_bytes, stdout_hash, stdout_truncated = self._capture(stdout_file)
            stderr, stderr_bytes, stderr_hash, stderr_truncated = self._capture(stderr_file)
        if error_text:
            stderr = (stderr + "\n" + error_text).strip()
            stderr_bytes += len(error_text.encode("utf-8"))
            stderr_hash = hashlib.sha256(stderr.encode("utf-8")).hexdigest()
        with self._lock:
            cancelled = (
                host_execution_ref in self._cancelled
                or request.execution_ref in self._cancelled
            )
            self._cancelled.discard(host_execution_ref)
        if cancelled:
            state = "CANCELLED"
        elif timed_out:
            state = "TIMEOUT"
        elif error_text:
            state = "TRANSPORT_FAILED"
        elif exit_code == 0:
            state = "SUCCEEDED"
        else:
            state = "COMMAND_FAILED"
        result = {
            "host_execution_ref": host_execution_ref,
            "task_ref": request.task_ref,
            "execution_ref": request.execution_ref,
            "host": request.route.host.stable_identifier,
            "surface": "HOST_EXECUTOR",
            "operation_class": request.operation_class,
            "capability": request.capability,
            "operation": request.operation,
            "target_identity": command.target_identity,
            "started_at": started_at,
            "completed_at": _now(),
            "duration_ms": int((time.monotonic() - began) * 1000),
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_bytes": stdout_bytes,
            "stderr_bytes": stderr_bytes,
            "stdout_sha256": stdout_hash,
            "stderr_sha256": stderr_hash,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "result_state": state,
            "timeout_seconds": timeout,
            "timed_out": timed_out,
            "cancel_requested": cancelled,
        }
        self.registry.complete_host_execution(
            host_execution_ref,
            **{
                key: int(value) if key in {
                    "stdout_truncated", "stderr_truncated", "timed_out", "cancel_requested"
                } else value
                for key, value in result.items()
                if key in {
                    "completed_at", "duration_ms", "exit_code", "stdout", "stderr",
                    "stdout_bytes", "stderr_bytes", "stdout_sha256", "stderr_sha256",
                    "stdout_truncated", "stderr_truncated", "result_state", "timed_out",
                    "cancel_requested",
                }
            },
        )
        return result

    def cancel_execution(self, execution_ref: str) -> int:
        count = self.registry.request_host_execution_cancellation(execution_ref)
        with self._lock:
            self._cancelled.add(execution_ref)
            active = [
                process
                for parent_ref, process in self._active.values()
                if parent_ref == execution_ref
            ]
        for process in active:
            self._terminate(process)
        return count

    def _binary_status(self, binary: str) -> str:
        return (
            "AVAILABLE"
            if shutil.which(binary, path=self._executor_path())
            else "UNAVAILABLE"
        )

    def capabilities(self, *, refresh: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not refresh and self._capability_cache and now - self._capability_cache[0] < 60:
            return self._capability_cache[1]
        enabled = self.config.enabled
        dns_available = bool(shutil.which("getent", path=self._executor_path()))
        https_available = bool(shutil.which("curl", path=self._executor_path()))
        ssh_available = bool(shutil.which("ssh", path=self._executor_path()))
        capability = {
            "filesystem": "AVAILABLE" if enabled and Path.home().is_dir() else "UNAVAILABLE",
            "network": (
                "NOT_CONFIGURED"
                if not enabled or not self.config.network_targets
                else "AVAILABLE" if dns_available or https_available else "UNAVAILABLE"
            ),
            "dns": (
                "NOT_CONFIGURED"
                if not enabled or not self.config.network_targets
                else "AVAILABLE" if dns_available else "UNAVAILABLE"
            ),
            "https": (
                "NOT_CONFIGURED"
                if not enabled or not self.config.network_targets
                else "AVAILABLE" if https_available else "UNAVAILABLE"
            ),
            "ssh": (
                "NOT_CONFIGURED"
                if not enabled or not self.config.ssh_targets
                else "AVAILABLE" if ssh_available else "UNAVAILABLE"
            ),
            "systemd_user": (
                "AVAILABLE"
                if enabled and shutil.which("systemctl", path=self._executor_path())
                else "UNAVAILABLE"
            ),
            "host_process": "AVAILABLE" if enabled and Path("/proc/self").is_dir() else "UNAVAILABLE",
            "aws_cli": self._binary_status("aws") if enabled else "NOT_CONFIGURED",
            "cloudflare_cli": self._binary_status("wrangler") if enabled else "NOT_CONFIGURED",
            "cloud_api": "NOT_CONFIGURED",
            "git": self._binary_status("git") if enabled else "NOT_CONFIGURED",
            "docker": (
                "AVAILABLE"
                if enabled and shutil.which("docker") and os.access("/var/run/docker.sock", os.R_OK)
                else "UNAVAILABLE"
            ),
            "postgres": self._binary_status("psql") if enabled else "NOT_CONFIGURED",
            "tailscale": self._binary_status("tailscale") if enabled else "NOT_CONFIGURED",
        }
        result = {
            "available": enabled,
            "host": normalize_host(self.config.host).stable_identifier,
            "hostname": socket.gethostname(),
            "user": pwd.getpwuid(os.getuid()).pw_name,
            "persistent": True,
            "runtime_managed": True,
            "runtime": "clinx-tunnel.service:mcp-child",
            "default": False,
            "capability_probe_timestamp": _now(),
            "capabilities": capability,
            "registered_services": [item.alias for item in self.config.services],
            "registered_ssh_targets": [item.alias for item in self.config.ssh_targets],
            "registered_network_targets": [item.alias for item in self.config.network_targets],
            "sudo_available": False,
        }
        self._capability_cache = (now, result)
        return result
