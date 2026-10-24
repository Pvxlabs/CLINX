"""Shared transport configuration; parsing never constructs a task authority."""

from __future__ import annotations

import dataclasses
import socket
import tomllib
from pathlib import Path
from typing import Any

from clinx.contracts.host_config import HostExecutorConfig, RegisteredTarget
from clinx.contracts.runtime_contracts import BridgeError
from clinx.host.production_workflows import load_workflows

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


def canonical_host(value: str) -> str:
    """Return the small, stable host identity used for transport selection."""
    host = value.strip().lower()
    if not host:
        raise BridgeError("host identity is required")
    if host.startswith("workstation-"):
        host = host.removeprefix("workstation-")
    if not host:
        raise BridgeError("host identity is required")
    return host


def detect_runtime_host(configured_host: str | None, *, hostname: str | None = None) -> str:
    """Prefer trusted config; hostname is only a deterministic local fallback."""
    return canonical_host(configured_host or hostname or socket.gethostname())


def resolve_transport(
    runtime_host: str,
    target_host: str,
    remote_transport: str,
) -> str:
    """Select local only for the same canonical host, otherwise fail closed."""
    if canonical_host(runtime_host) == canonical_host(target_host):
        return "local"
    if remote_transport != "ssh":
        raise BridgeError(
            "cross-host app-server dispatch requires configured remote transport 'ssh'"
        )
    return "ssh"


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
        with path.open("rb") as f:
            raw = tomllib.load(f)

        linear = raw.get("linear", {})
        codex = raw.get("codex", {})
        app_server_raw = raw.get("app_server", {})
        runtime = raw.get("runtime", {})
        workspace_tables = raw.get("workspaces", {})
        project_rows = raw.get("project", [])
        project_tables = raw.get("projects", {})
        target_rows = raw.get("targets", {})
        thread_tables = raw.get("threads", {})
        host_executor_raw = raw.get("host_executor", {})

        required = {
            "linear.team_id": linear.get("team_id"),
            "linear.trigger_label": linear.get("trigger_label"),
            "linear.todo_state": linear.get("todo_state"),
            "linear.running_state": linear.get("running_state"),
            "linear.review_state": linear.get("review_state"),
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise BridgeError(f"Missing required config: {', '.join(missing)}")

        projects: list[ProjectMapping] = []
        if project_tables:
            if not isinstance(project_tables, dict):
                raise BridgeError("[projects.<alias>] entries must be tables")
            for alias, row in project_tables.items():
                if not isinstance(row, dict):
                    raise BridgeError(f"Project {alias!r} must be a table")
                cwd = str(row.get("cwd", "")).strip()
                raw_origin = row.get("repository_origin")
                origin = str(raw_origin).strip() if raw_origin is not None else None
                branch = str(row.get("branch", "")).strip()
                if not cwd or not branch:
                    raise BridgeError(
                        f"Project {alias!r} requires cwd and branch; repository_origin is optional"
                    )
                if origin == "":
                    origin = None
                origin_policy = str(row.get("unexpected_origin_policy", "FAIL")).strip().upper()
                if origin_policy != "FAIL":
                    raise BridgeError(
                        f"Project {alias!r} has unsupported unexpected_origin_policy={origin_policy!r}"
                    )
                linear_name = str(row.get("linear_name", alias)).strip()
                projects.append(
                    ProjectMapping(
                        linear_name=linear_name,
                        repo=Path(cwd).expanduser().resolve(),
                        alias=str(alias),
                        repository_origin=origin,
                        unexpected_origin_policy=origin_policy,
                        branch=branch,
                        read_only=bool(row.get("read_only", False)),
                        workspace_alias=(
                            str(row["workspace"]).strip()
                            if row.get("workspace")
                            else None
                        ),
                    )
                )
        for row in project_rows:
            name = str(row.get("linear_name", "")).strip()
            repo = str(row.get("repo", "")).strip()
            if not name or not repo:
                raise BridgeError("Every [[project]] requires linear_name and repo")
            target_alias = str(row.get("target_alias", "")).strip() or None
            projects.append(ProjectMapping(name, Path(repo).expanduser().resolve(), target_alias))

        if not projects:
            raise BridgeError("At least one [[project]] mapping is required")

        if not isinstance(target_rows, dict):
            raise BridgeError("[targets.<alias>] entries must be tables")
        targets: list[TargetConfig] = []
        for alias, row in target_rows.items():
            if not isinstance(row, dict):
                raise BridgeError(f"Target {alias!r} must be a table")
            values = {
                "ssh_alias": row.get("ssh_alias"),
                "thread_id": row.get("thread_id"),
                "session_id": row.get("session_id"),
                "cwd": row.get("cwd"),
                "repository_origin": row.get("repository_origin"),
                "branch": row.get("branch"),
                "app_server_version": row.get("app_server_version"),
            }
            missing_target = [key for key, value in values.items() if not str(value or "").strip()]
            if missing_target:
                raise BridgeError(
                    f"Target {alias!r} missing required fields: {', '.join(missing_target)}"
                )
            project_id = row.get("project_id")
            if project_id is not None:
                project_id = str(project_id).strip() or None
            targets.append(
                TargetConfig(
                    alias=str(alias),
                    project_id=project_id,
                    target_host=(
                        str(row["host"]).strip() if row.get("host") else ""
                    ),
                    **{k: str(v) for k, v in values.items()},
                )
            )

        target_aliases = {target.alias for target in targets}
        unmapped_aliases = {
            mapping.target_alias
            for mapping in projects
            if mapping.target_alias and mapping.target_alias not in target_aliases
        }
        if unmapped_aliases and not thread_tables:
            raise BridgeError(
                "Project mappings reference unknown target aliases: "
                + ", ".join(sorted(unmapped_aliases))
            )

        remote_command = app_server_raw.get("remote_command")
        if not isinstance(remote_command, list) or not remote_command:
            remote_command = ["codex", "app-server", "proxy"]
        remote_command = tuple(str(part) for part in remote_command)
        command = app_server_raw.get("command", ["codex", "app-server", "proxy"])
        if not isinstance(command, list) or not command:
            raise BridgeError("[app_server].command must be a non-empty TOML string array")
        command = tuple(str(part) for part in command)
        local_socket = app_server_raw.get("local_socket")
        if local_socket is not None and (not isinstance(local_socket, str) or not local_socket.strip()):
            raise BridgeError("[app_server].local_socket must be a non-empty socket path")
        native_socket = app_server_raw.get("native_socket")
        if native_socket is not None and (not isinstance(native_socket, str) or not native_socket.strip()):
            raise BridgeError("[app_server].native_socket must be a non-empty socket path")
        native_home = app_server_raw.get('native_home')
        if native_home is not None and (not isinstance(native_home, str) or not native_home.strip()
                                       or not Path(native_home).expanduser().is_absolute()):
            raise BridgeError('[app_server].native_home must be an absolute native history root')
        transport = str(app_server_raw.get("transport", "local")).strip().lower()
        if transport not in {"local", "ssh"}:
            raise BridgeError("[app_server].transport must be 'local' or 'ssh'")
        ssh_args = app_server_raw.get("ssh_args", ["-T"])
        if not isinstance(ssh_args, list):
            raise BridgeError("[app_server].ssh_args must be a TOML string array")
        timeout = float(app_server_raw.get("request_timeout_seconds", 30))
        if timeout <= 0:
            raise BridgeError("request_timeout_seconds must be positive")

        poll = int(linear.get("poll_interval_seconds", 15))
        if poll < 5:
            raise BridgeError("poll_interval_seconds must be >= 5")

        max_batch = int(linear.get("max_batch", 1))
        if max_batch < 1 or max_batch > 10:
            raise BridgeError("max_batch must be between 1 and 10")

        threads: list[ThreadBinding] = []
        if thread_tables:
            if not isinstance(thread_tables, dict):
                raise BridgeError("[threads.<project>.<alias>] entries must be tables")
            for project_alias, aliases in thread_tables.items():
                if not isinstance(aliases, dict):
                    raise BridgeError(f"Threads for project {project_alias!r} must be tables")
                for thread_alias, row in aliases.items():
                    if not isinstance(row, dict):
                        raise BridgeError(
                            f"Thread {project_alias}.{thread_alias!r} must be a table"
                        )
                    required_thread = {
                        "ssh_alias": row.get("ssh_alias"),
                        "thread_id": row.get("thread_id"),
                        "session_id": row.get("session_id"),
                        "app_server_version": row.get("app_server_version"),
                    }
                    missing_thread = [
                        key for key, value in required_thread.items()
                        if not str(value or "").strip()
                    ]
                    if missing_thread:
                        raise BridgeError(
                            f"Thread {project_alias}.{thread_alias} missing required fields: "
                            + ", ".join(missing_thread)
                        )
                    project_id = row.get("project_id")
                    threads.append(
                        ThreadBinding(
                            alias=str(thread_alias),
                            project_alias=str(project_alias),
                            ssh_alias=str(row["ssh_alias"]),
                            thread_id=str(row["thread_id"]),
                            session_id=str(row["session_id"]),
                            project_id=str(project_id).strip() if project_id else None,
                            app_server_version=str(row["app_server_version"]),
                            target_host=(
                                str(row["host"]).strip() if row.get("host") else ""
                            ),
                        )
                    )

        if not threads:
            for target in targets:
                matching = next(
                    (project for project in projects if project.target_alias == target.alias),
                    None,
                )
                project_alias = matching.project_alias if matching else target.alias
                threads.append(
                    ThreadBinding(
                        alias="current",
                        project_alias=project_alias,
                        ssh_alias=target.ssh_alias,
                        thread_id=target.thread_id,
                        session_id=target.session_id,
                        project_id=target.project_id,
                        app_server_version=target.app_server_version,
                        target_host=target.target_host,
                    )
                )

        workspaces: list[WorkspaceConfig] = []
        if workspace_tables:
            if not isinstance(workspace_tables, dict):
                raise BridgeError("[workspaces.<alias>] entries must be tables")
            for alias, row in workspace_tables.items():
                if not isinstance(row, dict):
                    raise BridgeError(f"Workspace {alias!r} must be a table")
                root = str(row.get("root", "")).strip()
                if not root:
                    raise BridgeError(f"Workspace {alias!r} requires root")
                native_host_ids = row.get('codex_host_ids', [])
                if (not isinstance(native_host_ids, list) or any(not isinstance(v, str) or not v
                        for v in native_host_ids)):
                    raise BridgeError(f'Workspace {alias!r} codex_host_ids must be a string array')
                workspaces.append(
                    WorkspaceConfig(
                        alias=str(alias),
                        root=Path(root).expanduser().resolve(),
                        allow_existing_projects=bool(row.get("allow_existing_projects", True)),
                        codex_host_ids=tuple(native_host_ids),
                        allow_new_projects=bool(row.get("allow_new_projects", False)),
                        host=(str(row["host"]).strip() if row.get("host") else None),
                        ssh_alias=(
                            str(row["ssh_alias"]).strip()
                            if row.get("ssh_alias")
                            else None
                        ),
                    )
                )

        task_db_value = runtime.get("task_db_path")
        task_db_path = (
            Path(str(task_db_value)).expanduser().resolve()
            if task_db_value
            else None
        )
        runtime_host = detect_runtime_host(
            str(runtime["runtime_host"]).strip() if runtime.get("runtime_host") else None
        )

        if not isinstance(host_executor_raw, dict):
            raise BridgeError("[host_executor] must be a table")
        executor_enabled = bool(host_executor_raw.get("enabled", False))
        executor_host = str(host_executor_raw.get("host", runtime_host or "p620")).strip()
        default_host_timeout = float(host_executor_raw.get("default_timeout_seconds", 30))
        max_host_timeout = float(host_executor_raw.get("max_timeout_seconds", 120))
        max_output_bytes = int(host_executor_raw.get("max_output_bytes", 65536))
        trusted_roots = host_executor_raw.get("trusted_workspace_roots", [])
        if not isinstance(trusted_roots, list) or any(
            not isinstance(root, str) or not Path(root).is_absolute()
            or Path(root).resolve() == Path('/') for root in trusted_roots
        ):
            raise BridgeError("[host_executor].trusted_workspace_roots must be absolute non-root paths")
        trusted_roots = tuple(dict.fromkeys(Path(root).resolve() for root in trusted_roots))
        if default_host_timeout <= 0 or max_host_timeout < default_host_timeout:
            raise BridgeError("host executor timeout bounds are invalid")
        if not 1024 <= max_output_bytes <= 1024 * 1024:
            raise BridgeError("host executor max_output_bytes must be between 1024 and 1048576")
        if executor_enabled and canonical_host(executor_host) != canonical_host(runtime_host):
            raise BridgeError("HOST_EXECUTOR must run on the configured runtime host")

        def target_list(name: str, default_classes: tuple[str, ...]) -> tuple[RegisteredTarget, ...]:
            values = host_executor_raw.get(name, [])
            if not isinstance(values, list) or any(
                not isinstance(item, str) or not item.strip() for item in values
            ):
                raise BridgeError(f"[host_executor].{name} must be a string array")
            return tuple(
                RegisteredTarget(item.strip(), default_classes) for item in values
            )

        ssh_targets: tuple[RegisteredTarget, ...]
        ssh_rows = host_executor_raw.get("ssh_target_commands")
        if ssh_rows is None:
            ssh_targets = target_list("ssh_targets", ("PRODUCTION_READ_ONLY",))
        else:
            if not isinstance(ssh_rows, dict):
                raise BridgeError("[host_executor.ssh_target_commands] must contain tables")
            parsed_ssh: list[RegisteredTarget] = []
            for alias, row in ssh_rows.items():
                if not isinstance(row, dict):
                    raise BridgeError(f"ssh target {alias!r} must be a table")
                classes = row.get("operation_classes", ["PRODUCTION_READ_ONLY"])
                commands = row.get("commands", {})
                if not isinstance(classes, list) or any(not isinstance(x, str) or not x.strip() for x in classes):
                    raise BridgeError("ssh target operation_classes must be a string array")
                if not isinstance(commands, dict):
                    raise BridgeError("ssh target commands must be a table")
                parsed_commands=[]
                for name, argv in commands.items():
                    if not isinstance(name,str) or not name.strip() or not isinstance(argv,list) or not argv or any(not isinstance(x,str) or not x for x in argv):
                        raise BridgeError("ssh target command must be a named non-empty argv array")
                    parsed_commands.append((name.strip(), tuple(argv)))
                parsed_ssh.append(RegisteredTarget(str(alias), tuple(x.strip().upper() for x in classes), commands=tuple(parsed_commands)))
            ssh_targets=tuple(parsed_ssh)

        local_commands: list[RegisteredTarget] = []
        local_rows = host_executor_raw.get("local_commands", {})
        if not isinstance(local_rows, dict):
            raise BridgeError("[host_executor.local_commands] must contain tables")
        for alias, row in local_rows.items():
            if not isinstance(row, dict):
                raise BridgeError(f"local command {alias!r} must be a table")
            classes = row.get("operation_classes", [])
            argv = row.get("argv")
            if not isinstance(classes, list) or not classes or any(not isinstance(x,str) or not x.strip() for x in classes):
                raise BridgeError("local command operation_classes must be a non-empty string array")
            if not isinstance(argv, list) or not argv or any(not isinstance(x,str) or not x for x in argv):
                raise BridgeError("local command argv must be a non-empty string array")
            local_commands.append(RegisteredTarget(str(alias), tuple(x.strip().upper() for x in classes), commands=((str(alias), tuple(argv)),)))

        network_targets: list[RegisteredTarget] = []
        network_rows = host_executor_raw.get("network_targets", {})
        if not isinstance(network_rows, dict):
            raise BridgeError("[host_executor.network_targets] must contain tables")
        for alias, row in network_rows.items():
            if not isinstance(row, dict):
                raise BridgeError(f"host executor network target {alias!r} must be a table")
            dns_name = str(row.get("dns_name", "")).strip() or None
            url = str(row.get("url", "")).strip() or None
            if not dns_name and not url:
                raise BridgeError(f"host executor network target {alias!r} is empty")
            if url and not url.startswith("https://"):
                raise BridgeError("host executor network targets require https URLs")
            operation_classes = row.get(
                "operation_classes", ["READ_ONLY_HOST"]
            )
            if not isinstance(operation_classes, list) or any(
                not isinstance(item, str) or not item.strip()
                for item in operation_classes
            ):
                raise BridgeError("network target operation_classes must be a string array")
            network_targets.append(
                RegisteredTarget(
                    str(alias),
                    tuple(item.strip().upper() for item in operation_classes),
                    dns_name=dns_name,
                    url=url,
                )
            )

        return BridgeConfig(
            team_id=str(linear["team_id"]),
            trigger_label=str(linear["trigger_label"]),
            todo_state=str(linear["todo_state"]),
            running_state=str(linear["running_state"]),
            review_state=str(linear["review_state"]),
            poll_interval_seconds=poll,
            max_batch=max_batch,
            codex_binary=str(codex.get("binary", "codex")),
            sandbox=str(codex.get("sandbox", "workspace-write")),
            approval=str(codex.get("approval", "never")),
            log_dir=Path(runtime.get("log_dir", "~/.local/state/linear-local-codex-bridge"))
            .expanduser()
            .resolve(),
            projects=tuple(projects),
            app_server=AppServerConfig(
                local_socket=local_socket,
                native_socket=native_socket,
                native_home=str(Path(native_home).expanduser().resolve()) if native_home else None,
                transport=transport,
                command=command,
                ssh_binary=str(app_server_raw.get("ssh_binary", "ssh")),
                ssh_alias=(
                    str(app_server_raw["ssh_alias"]).strip()
                    if app_server_raw.get("ssh_alias")
                    else None
                ),
                ssh_args=tuple(str(arg) for arg in ssh_args),
                remote_command=remote_command,
                request_timeout_seconds=timeout,
                client_name=str(app_server_raw.get("client_name", "linear-local-codex-bridge")),
                client_title=str(app_server_raw.get("client_title", "Linear Local Codex Bridge")),
                client_version=str(app_server_raw.get("client_version", BRIDGE_VERSION)),
            ),
            targets=tuple(targets),
            threads=tuple(threads),
            workspaces=tuple(workspaces),
            task_db_path=task_db_path,
            runtime_host=runtime_host,
            host_executor=HostExecutorConfig(
                enabled=executor_enabled,
                host=executor_host,
                default_timeout_seconds=default_host_timeout,
                max_timeout_seconds=max_host_timeout,
                max_output_bytes=max_output_bytes,
                trusted_workspace_roots=trusted_roots,
                services=target_list(
                    "services", ("READ_ONLY_HOST", "DEVELOPMENT_MUTATION")
                ),
                ssh_targets=ssh_targets,
                network_targets=tuple(network_targets),
                local_commands=tuple(local_commands),
                workflows=load_workflows(host_executor_raw.get('workflows', {})),
            ),
        )

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
