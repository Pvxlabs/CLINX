"""Shared transport configuration; parsing never constructs a task authority."""

from __future__ import annotations

import dataclasses
import math
import socket
import tomllib
from pathlib import Path
from typing import Any

from clinx.contracts.host_config import HostExecutorConfig, RegisteredTarget
from clinx.contracts.runtime_contracts import BridgeError

BRIDGE_VERSION = "1.0.0-m6"


@dataclasses.dataclass(frozen=True)
class WorkspaceConfig:
    alias: str
    root: Path
    allow_existing_projects: bool = True
    allow_new_projects: bool = False
    # Execution-host identity is separate from the optional remote SSH alias.
    host: str | None = None
    ssh_alias: str | None = None
    codex_host_ids: tuple[str, ...] = ()


def _orders(value, path=()):
    if isinstance(value, dict):
        return [[path, list(value)], *[row for key, child in value.items()
                                     for row in _orders(child, (*path, key))]]
    return []


def _numeric_wire(value, *, decode=False):
    if decode and isinstance(value, dict):
        if set(value) == {'__config_int'}:
            return int(value['__config_int'])
        if set(value) == {'__config_float'}:
            return float(value['__config_float'])
    if not decode:
        if type(value) is int and not -(1 << 63) <= value < (1 << 64):
            return {'__config_int': str(value)}
        if type(value) is float and not math.isfinite(value):
            return {'__config_float': str(value)}
    if isinstance(value, dict):
        return {k: _numeric_wire(v, decode=decode) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_numeric_wire(v, decode=decode) for v in value]
    return value


def _call(operation, **arguments):
    from clinx.authority.request_pipe import invoke
    from clinx.authority.rust_authority import RustAuthorityError

    def callback(event):
        if event['kind'] != 'config' or event['operation'] != 'path':
            raise RustAuthorityError('CONFIG_CALLBACK_INVALID')
        args = event['arguments']
        path = Path(args['value'])
        if args['expand']:
            path = path.expanduser()
        if args['resolve']:
            path = path.resolve()
        return dict(path=str(path), absolute=path.is_absolute())

    try:
        result = invoke(dict(mode='config', operation=operation, arguments=_numeric_wire(arguments)), callback)
        return _numeric_wire(result, decode=True)
    except RustAuthorityError as exc:
        message = str(exc)
        for prefix, error_type in (('VALUE_ERROR:', ValueError), ('TYPE_ERROR:', TypeError),
                                   ('OVERFLOW_ERROR:', OverflowError)):
            if message.startswith(prefix):
                raise error_type(message.removeprefix(prefix)) from exc
        raise BridgeError(message) from exc


def canonical_host(value: str) -> str:
    return _call('canonical_host', value=value)


def detect_runtime_host(configured_host: str | None, *, hostname: str | None = None) -> str:
    return canonical_host(configured_host or hostname or socket.gethostname())


def resolve_transport(runtime_host: str, target_host: str, remote_transport: str) -> str:
    return _call('resolve_transport', runtime_host=runtime_host, target_host=target_host,
                 remote_transport=remote_transport)


@dataclasses.dataclass(frozen=True)
class ProjectMapping:
    linear_name: str
    repo: Path
    target_alias: str | None = None
    alias: str | None = None
    repository_origin: str | None = None
    unexpected_origin_policy: str = "FAIL"
    branch: str | None = None
    read_only: bool = False
    workspace_alias: str | None = None

    @property
    def project_alias(self) -> str:
        return self.alias or self.target_alias or self.linear_name.lower().replace(" ", "-")


@dataclasses.dataclass(frozen=True)
class AppServerConfig:
    # This is the configured *remote* transport. Same-host dispatches always
    # use the local app-server proxy regardless of this value.
    transport: str = "local"
    command: tuple[str, ...] = ("codex", "app-server", "proxy")
    local_socket: str | None = None
    native_socket: str | None = None
    native_home: str | None = None
    ssh_binary: str = "ssh"
    ssh_alias: str | None = None
    ssh_args: tuple[str, ...] = ("-T",)
    remote_command: tuple[str, ...] = ("codex", "app-server", "proxy")
    request_timeout_seconds: float = 30.0
    client_name: str = "linear-local-codex-bridge"
    client_title: str = "Linear Local Codex Bridge"
    client_version: str = BRIDGE_VERSION


@dataclasses.dataclass(frozen=True)
class TargetConfig:
    alias: str
    ssh_alias: str
    thread_id: str
    session_id: str
    project_id: str | None
    cwd: str
    repository_origin: str | None
    branch: str
    app_server_version: str
    target_host: str = ""
    transport: str = ""


@dataclasses.dataclass(frozen=True)
class ThreadBinding:
    """A named durable thread binding; project Git identity lives elsewhere."""

    alias: str
    project_alias: str
    ssh_alias: str
    thread_id: str
    session_id: str
    project_id: str | None
    app_server_version: str
    target_host: str = ""

    @property
    def qualified_alias(self) -> str:
        return f"{self.project_alias}.{self.alias}"


@dataclasses.dataclass(frozen=True)
class BridgeConfig:
    team_id: str
    trigger_label: str
    todo_state: str
    running_state: str
    review_state: str
    poll_interval_seconds: int
    max_batch: int
    codex_binary: str
    sandbox: str
    approval: str
    log_dir: Path
    projects: tuple[ProjectMapping, ...]
    app_server: AppServerConfig = dataclasses.field(default_factory=AppServerConfig)
    targets: tuple[TargetConfig, ...] = ()
    threads: tuple[ThreadBinding, ...] = ()
    workspaces: tuple[WorkspaceConfig, ...] = ()
    task_db_path: Path | None = None
    runtime_host: str = ""
    host_executor: HostExecutorConfig = dataclasses.field(default_factory=HostExecutorConfig)
    # Optional centre-side node router.  It is injected by the managed
    # service/bootstrap path; config parsing alone never opens a socket.
    node_router: Any = None

    @staticmethod
    def load(path: Path) -> "BridgeConfig":
        with path.open('rb') as stream:
            raw = tomllib.load(stream)
        data = _call('load', raw=raw, orders=_orders(raw), hostname=socket.gethostname())
        # Only wire shape/Path/tuple conversion remains in this public Python DTO API.
        data['log_dir'] = Path(data['log_dir'])
        data['task_db_path'] = Path(data['task_db_path']) if data['task_db_path'] else None
        data['projects'] = tuple(ProjectMapping(**dict(p, repo=Path(p['repo']))) for p in data['projects'])
        data['targets'] = tuple(TargetConfig(**p) for p in data['targets'])
        data['threads'] = tuple(ThreadBinding(**p) for p in data['threads'])
        data['workspaces'] = tuple(WorkspaceConfig(**dict(p, root=Path(p['root']),
            codex_host_ids=tuple(p['codex_host_ids']))) for p in data['workspaces'])
        app = data['app_server']
        for key in ('command', 'remote_command', 'ssh_args'):
            app[key] = tuple(app[key])
        data['app_server'] = AppServerConfig(**app)
        host = data['host_executor']
        host['trusted_workspace_roots'] = tuple(Path(p) for p in host['trusted_workspace_roots'])
        for key in ('services', 'ssh_targets', 'network_targets', 'local_commands'):
            host[key] = tuple(RegisteredTarget(**dict(p,
                operation_classes=tuple(p['operation_classes']),
                commands=tuple((name, tuple(argv)) for name, argv in p['commands']))) for p in host[key])
        from clinx.host.production_workflows import _decode
        host['workflows'] = _decode(host['workflows'])
        data['host_executor'] = HostExecutorConfig(**host)
        return BridgeConfig(**data)

    def repo_for_project(self, project_name: str | None) -> Path | None:
        if not project_name:
            return None
        for mapping in self.projects:
            if mapping.linear_name == project_name:
                return mapping.repo
        return None

    def target_alias_for_project(self, project_name: str | None) -> str | None:
        if not project_name:
            return None
        for mapping in self.projects:
            if mapping.linear_name == project_name:
                if mapping.target_alias:
                    return mapping.target_alias
                if len(self.targets) == 1:
                    return self.targets[0].alias
        return None

    def project_alias_for_linear(self, project_name: str | None) -> str | None:
        if not project_name:
            return None
        for mapping in self.projects:
            if mapping.linear_name == project_name:
                return mapping.project_alias
        return None


ProjectConfig = ProjectMapping
