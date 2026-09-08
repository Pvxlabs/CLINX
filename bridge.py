#!/usr/bin/env python3
"""linear-local-codex-bridge Dispatcher V1 / M3.

Linear (Todo + trigger label) -> claim In Progress -> selected app-server
transport -> exact durable thread -> turn/start.

The legacy local ``codex exec`` helper remains available for compatibility and
tests, but it is intentionally not part of the default dispatch path.
Python 3.11+, standard library only.
"""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from typing import Any

from app_server import (
    AppServerError,
    CodexAppServerClient,
    LocalStdioTransport,
    SSHStdioTransport,
    versions_compatible,
)
from task_registry import (
    ConversationBinding as DurableConversationBinding,
    ContextCheckpoint,
    DynamicProjectResolver,
    ProjectDescriptor,
    TaskExecutionBusy,
    WorktreeExecutionBusy,
    TaskIndexRecord,
    TaskRegistry,
    TaskRegistryError,
    WorkspaceConfig,
    WorkspaceRegistry,
)
from m9_integration import ExecutionResultService

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
BRIDGE_VERSION = "1.0.0-m6"
LEGACY_CODEX_EXEC_DEFAULT = False


def _resolve_dispatch_model(client: Any, model: str | None, reasoning_effort: str | None) -> tuple[str | None, str | None]:
    """Resolve logical model names only when the client exposes live capabilities."""
    resolver = getattr(client, "resolve_model", None)
    if not callable(resolver):
        return model, reasoning_effort
    requested = model or "gpt-5.6-luna"
    return resolver(requested, reasoning_effort)


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


class BridgeError(RuntimeError):
    pass


class LinearAPIError(BridgeError):
    pass


class TargetResolutionError(BridgeError):
    pass


class IdentityGuardError(BridgeError):
    pass


class DispatchContractError(BridgeError):
    pass


class ReadOnlyOnboardingError(BridgeError):
    pass


class ContextReadError(BridgeError):
    pass


class BoundedHistoryUnavailable(ContextReadError):
    """The connected app-server does not expose usable bounded history."""


@dataclasses.dataclass(frozen=True)
class TaskContext:
    task_ref: str
    task_title: str
    task_status: str
    host: str
    project: str
    context_source: str
    context_range: str
    context_truncated: bool
    checkpoint_stale: bool
    last_user_intent: str
    last_codex_result: str
    changed_files: str
    validation: str
    blockers: str
    current_state: str
    ready_for_continuation: bool
    provenance: dict[str, tuple[str, ...]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_context_read": "PASS",
            "task_ref": self.task_ref,
            "task_title": self.task_title,
            "task_status": self.task_status,
            "host": self.host,
            "project": self.project,
            "context_source": self.context_source,
            "context_range": self.context_range,
            "context_truncated": self.context_truncated,
            "checkpoint_stale": self.checkpoint_stale,
            "last_user_intent": self.last_user_intent,
            "last_codex_result": self.last_codex_result,
            "changed_files": self.changed_files,
            "validation": self.validation,
            "blockers": self.blockers,
            "current_state": self.current_state,
            "ready_for_continuation": self.ready_for_continuation,
            "provenance": self.provenance,
            "read_only": True,
        }


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


ProjectConfig = ProjectMapping


@dataclasses.dataclass(frozen=True)
class AppServerConfig:
    # This is the configured *remote* transport. Same-host dispatches always
    # use the local app-server proxy regardless of this value.
    transport: str = "local"
    command: tuple[str, ...] = ("codex", "app-server", "proxy")
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
class DispatchContract:
    target_alias: str | None
    model: str | None
    reasoning_effort: str | None
    expected_result: str | None = None
    project_alias: str | None = None
    thread_mode: str = "existing"
    thread_alias: str | None = None
    contract_kind: str = "legacy"
    host: str | None = None
    project_mode: str | None = None
    task_mode: str | None = None
    task_id: str | None = None
    execution_mode: str = "normal"
    task_action: str | None = None
    task_ref: str | None = None
    task_title: str | None = None
    task_summary_update: str | None = None
    task_key: str | None = None


def parse_dispatch_contract(description: str | None) -> DispatchContract | None:
    """Parse legacy M2/M3, M5, and canonical M6 task contracts."""
    if not description:
        return None

    def value_for(key: str) -> str | None:
        match = re.search(rf"(?m)^\s*{key}=([^\s]+)\s*$", description)
        return match.group(1) if match else None

    def line_value_for(key: str) -> str | None:
        match = re.search(rf"(?m)^\s*{key}=(.*?)\s*$", description)
        if not match:
            return None
        value = match.group(1).strip()
        return value or None

    marker = re.search(
        r"(?m)^\s*Return exactly this final marker:\s*$\n\s*([^\s]+)\s*$",
        description,
    )
    expected_result = marker.group(1) if marker else None

    task_action_value = value_for("TASK_ACTION")
    task_ref_value = value_for("TASK_REF")
    has_m5_task_mode = value_for("TASK_MODE") is not None
    has_m6_task_ref = re.search(r"(?m)^\s*TASK_REF=", description) is not None
    is_m6 = (
        task_action_value in {"create", "continue"}
        or has_m6_task_ref
        or (task_action_value == "reopen" and not has_m5_task_mode)
    )
    if is_m6:
        host = value_for("HOST")
        project_alias = value_for("PROJECT")
        project_mode = value_for("PROJECT_MODE") or "existing"
        model = value_for("MODEL")
        reasoning = value_for("REASONING")
        execution_mode = value_for("EXECUTION_MODE") or "normal"
        missing = [
            key for key, value in (
                ("HOST", host),
                ("PROJECT", project_alias),
                ("TASK_ACTION", task_action_value),
                ("MODEL", model),
                ("REASONING", reasoning),
            ) if value is None
        ]
        if missing:
            raise DispatchContractError(
                "Malformed M6 task handoff: missing " + ", ".join(missing)
            )
        if execution_mode not in {"normal", "fast"}:
            raise DispatchContractError(
                f"Malformed M6 task handoff: unsupported EXECUTION_MODE={execution_mode!r}"
            )
        if project_mode not in {"existing", "create"}:
            raise DispatchContractError(
                f"Malformed M6 task handoff: unsupported PROJECT_MODE={project_mode!r}"
            )
        if task_action_value in {"continue", "reopen"} and project_mode != "existing":
            raise DispatchContractError(
                f"Malformed M6 task handoff: TASK_ACTION={task_action_value} "
                "requires PROJECT_MODE=existing"
            )
        if task_action_value == "create" and task_ref_value is not None:
            raise DispatchContractError(
                "Malformed M6 task handoff: TASK_ACTION=create must not include TASK_REF"
            )
        if task_action_value in {"continue", "reopen"} and task_ref_value is None:
            raise DispatchContractError(
                f"Malformed M6 task handoff: TASK_ACTION={task_action_value} requires TASK_REF"
            )
        return DispatchContract(
            target_alias=None,
            model=model,
            reasoning_effort=reasoning,
            expected_result=expected_result,
            project_alias=project_alias,
            contract_kind="m6",
            host=host,
            project_mode=project_mode,
            task_mode="new" if task_action_value == "create" else "continue",
            task_id=task_ref_value,
            task_ref=task_ref_value,
            execution_mode=execution_mode,
            task_action=task_action_value,
            task_title=line_value_for("TASK_TITLE"),
            task_summary_update=line_value_for("TASK_SUMMARY_UPDATE"),
            task_key=value_for("TASK_KEY"),
        )

    m5_keys = ("TASK_MODE", "PROJECT_MODE", "EXECUTION_MODE", "TASK_ACTION")
    if any(value_for(key) is not None for key in m5_keys):
        project_alias = value_for("PROJECT")
        project_mode = value_for("PROJECT_MODE") or "existing"
        task_mode = value_for("TASK_MODE")
        task_id = value_for("TASK_ID")
        host = value_for("HOST")
        model = value_for("MODEL")
        reasoning = value_for("REASONING")
        execution_mode = value_for("EXECUTION_MODE") or "normal"
        task_action = value_for("TASK_ACTION")
        missing = [
            key for key, value in (("PROJECT", project_alias), ("TASK_MODE", task_mode))
            if value is None
        ]
        if missing:
            raise DispatchContractError(
                "Malformed M5 dispatch contract: missing " + ", ".join(missing)
            )
        if project_mode not in {"existing", "create"}:
            raise DispatchContractError(
                f"Malformed M5 dispatch contract: unsupported PROJECT_MODE={project_mode!r}"
            )
        if task_mode not in {"new", "continue"}:
            raise DispatchContractError(
                f"Malformed M5 dispatch contract: unsupported TASK_MODE={task_mode!r}"
            )
        if task_mode == "continue" and project_mode != "existing":
            raise DispatchContractError(
                "Malformed M5 dispatch contract: TASK_MODE=continue requires PROJECT_MODE=existing"
            )
        if task_mode == "continue" and not task_id:
            raise DispatchContractError(
                "Malformed M5 dispatch contract: TASK_MODE=continue requires TASK_ID"
            )
        if execution_mode not in {"normal", "fast"}:
            raise DispatchContractError(
                f"Malformed M5 dispatch contract: unsupported EXECUTION_MODE={execution_mode!r}"
            )
        if task_action not in {None, "complete", "reopen", "archive"}:
            raise DispatchContractError(
                f"Malformed M5 dispatch contract: unsupported TASK_ACTION={task_action!r}"
            )
        return DispatchContract(
            target_alias=None,
            model=model,
            reasoning_effort=reasoning,
            expected_result=expected_result,
            project_alias=project_alias,
            contract_kind="m5",
            host=host,
            project_mode=project_mode,
            task_mode=task_mode,
            task_id=task_id,
            execution_mode=execution_mode,
            task_action=task_action,
        )

    canonical_keys = ("PROJECT", "THREAD_MODE", "THREAD_ALIAS")
    has_canonical = any(value_for(key) is not None for key in canonical_keys)

    if has_canonical:
        project_alias = value_for("PROJECT")
        thread_mode = value_for("THREAD_MODE")
        model = value_for("MODEL")
        reasoning = value_for("REASONING")
        missing = [
            key
            for key, value in (
                ("PROJECT", project_alias),
                ("THREAD_MODE", thread_mode),
                ("MODEL", model),
                ("REASONING", reasoning),
            )
            if value is None
        ]
        if missing:
            raise DispatchContractError(
                "Malformed dispatch contract: missing " + ", ".join(missing)
            )
        if thread_mode not in {"existing", "new"}:
            raise DispatchContractError(
                f"Malformed dispatch contract: unsupported THREAD_MODE={thread_mode!r}"
            )
        thread_alias = value_for("THREAD_ALIAS")
        if thread_mode == "existing" and not thread_alias:
            raise DispatchContractError(
                "Malformed dispatch contract: existing THREAD_MODE requires THREAD_ALIAS"
            )
        return DispatchContract(
            target_alias=None,
            model=model,
            reasoning_effort=reasoning,
            expected_result=expected_result,
            project_alias=project_alias,
            thread_mode=thread_mode,
            thread_alias=thread_alias,
            contract_kind="canonical",
        )

    values: dict[str, str] = {}
    for key in ("TARGET_ALIAS", "MODEL", "REASONING"):
        value = value_for(key)
        if value is None:
            raise DispatchContractError(f"Malformed dispatch contract: missing {key}")
        values[key] = value
    return DispatchContract(
        target_alias=values["TARGET_ALIAS"],
        model=values["MODEL"],
        reasoning_effort=values["REASONING"],
        expected_result=expected_result,
        thread_alias="current",
        contract_kind="legacy",
    )


class ProjectRegistry:
    def __init__(self, projects: tuple[ProjectMapping, ...]):
        self._projects: dict[str, ProjectMapping] = {}
        for project in projects:
            alias = project.project_alias
            if alias in self._projects:
                raise TargetResolutionError(f"Duplicate project alias: {alias}")
            self._projects[alias] = project

    def resolve(self, alias: str) -> ProjectMapping:
        project = self._projects.get(alias)
        if project is None:
            raise TargetResolutionError(f"Unknown project alias: {alias}")
        return project

    def __iter__(self):
        return iter(self._projects.values())


class ThreadRegistry:
    def __init__(self, bindings: tuple[ThreadBinding, ...]):
        self._bindings: dict[tuple[str, str], ThreadBinding] = {}
        for binding in bindings:
            key = (binding.project_alias, binding.alias)
            if key in self._bindings:
                raise TargetResolutionError(
                    f"Duplicate thread alias: {binding.project_alias}.{binding.alias}"
                )
            self._bindings[key] = binding

    def resolve(self, project_alias: str, alias: str) -> ThreadBinding:
        binding = self._bindings.get((project_alias, alias))
        if binding is None:
            raise TargetResolutionError(
                f"Unknown thread alias: {project_alias}.{alias}"
            )
        return binding

    def __iter__(self):
        return iter(self._bindings.values())


class TargetRegistry:
    def __init__(self, targets: tuple[TargetConfig, ...]):
        self._targets = {target.alias: target for target in targets}

    def resolve(self, alias: str) -> TargetConfig:
        target = self._targets.get(alias)
        if target is None:
            raise TargetResolutionError(f"Unknown target alias: {alias}")
        return target

    def __iter__(self):
        return iter(self._targets.values())


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
                workspaces.append(
                    WorkspaceConfig(
                        alias=str(alias),
                        root=Path(root).expanduser().resolve(),
                        allow_existing_projects=bool(row.get("allow_existing_projects", True)),
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


class LinearClient:
    def __init__(self, api_key: str, url: str = LINEAR_GRAPHQL_URL, timeout: int = 20):
        self.api_key = api_key
        self.url = url
        self.timeout = timeout

    def request(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = json.dumps(
            {"query": query, "variables": variables or {}},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": self.api_key,
                "User-Agent": f"linear-local-codex-bridge/{BRIDGE_VERSION}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise LinearAPIError(f"Linear HTTP {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise LinearAPIError(f"Linear network error: {e}") from e

        try:
            obj = json.loads(body)
        except json.JSONDecodeError as e:
            raise LinearAPIError(f"Linear returned non-JSON response: {body[:500]}") from e

        errors = obj.get("errors")
        if errors:
            msgs = "; ".join(str(err.get("message", err)) for err in errors)
            raise LinearAPIError(f"Linear GraphQL error: {msgs}")
        return obj.get("data") or {}

    def viewer(self) -> dict[str, Any]:
        data = self.request(
            """
            query Viewer {
              viewer { id name email }
            }
            """
        )
        return data["viewer"]

    def team_states(self, team_id: str) -> dict[str, str]:
        data = self.request(
            """
            query TeamStates($teamId: String!) {
              team(id: $teamId) {
                states(first: 50) {
                  nodes { id name type }
                }
              }
            }
            """,
            {"teamId": team_id},
        )
        team = data.get("team")
        if not team:
            raise LinearAPIError(f"Linear team not found: {team_id}")
        return {node["name"]: node["id"] for node in team["states"]["nodes"]}

    def eligible_issues(
        self,
        team_id: str,
        state_name: str,
        label_name: str,
        first: int,
    ) -> list[dict[str, Any]]:
        data = self.request(
            """
            query EligibleIssues(
              $teamId: ID!,
              $stateName: String!,
              $labelName: String!,
              $first: Int!
            ) {
              issues(
                first: $first,
                orderBy: updatedAt,
                filter: {
                  team: { id: { eq: $teamId } }
                  state: { name: { eq: $stateName } }
                  labels: { some: { name: { eq: $labelName } } }
                }
              ) {
                nodes {
                  id
                  identifier
                  title
                  description
                  url
                  updatedAt
                  state { id name type }
                  project { id name }
                }
              }
            }
            """,
            {
                "teamId": team_id,
                "stateName": state_name,
                "labelName": label_name,
                "first": first,
            },
        )
        return list(data["issues"]["nodes"])

    def get_issue(self, issue_id: str) -> dict[str, Any]:
        data = self.request(
            """
            query Issue($id: String!) {
              issue(id: $id) {
                id
                identifier
                title
                url
                updatedAt
                state { id name type }
                project { id name }
              }
            }
            """,
            {"id": issue_id},
        )
        issue = data.get("issue")
        if not issue:
            raise LinearAPIError(f"Issue not found: {issue_id}")
        return issue

    def update_issue_state(self, issue_id: str, state_id: str) -> dict[str, Any]:
        data = self.request(
            """
            mutation UpdateIssueState($id: String!, $stateId: String!) {
              issueUpdate(id: $id, input: { stateId: $stateId }) {
                success
                issue {
                  id
                  identifier
                  state { id name type }
                }
              }
            }
            """,
            {"id": issue_id, "stateId": state_id},
        )
        payload = data["issueUpdate"]
        if not payload.get("success"):
            raise LinearAPIError(f"issueUpdate returned success=false for {issue_id}")
        return payload["issue"]

    def add_comment(self, issue_id: str, body: str) -> None:
        data = self.request(
            """
            mutation AddComment($issueId: String!, $body: String!) {
              commentCreate(input: { issueId: $issueId, body: $body }) {
                success
              }
            }
            """,
            {"issueId": issue_id, "body": body},
        )
        if not data["commentCreate"].get("success"):
            raise LinearAPIError(f"commentCreate returned success=false for {issue_id}")

    def create_task_index_issue(
        self,
        *,
        team_id: str,
        project_id: str | None,
        title: str,
        description: str,
    ) -> dict[str, Any]:
        data = self.request(
            """
            mutation CreateTaskIndex(
              $teamId: String!,
              $projectId: String,
              $title: String!,
              $description: String!
            ) {
              issueCreate(input: {
                teamId: $teamId,
                projectId: $projectId,
                title: $title,
                description: $description
              }) {
                success
                issue { id identifier title url project { id name } }
              }
            }
            """,
            {
                "teamId": team_id,
                "projectId": project_id,
                "title": title,
                "description": description,
            },
        )
        payload = data["issueCreate"]
        if not payload.get("success"):
            raise LinearAPIError("issueCreate returned success=false for task index")
        return payload["issue"]

    def update_task_index_issue(
        self,
        issue_id: str,
        *,
        title: str,
        description: str,
    ) -> dict[str, Any]:
        data = self.request(
            """
            mutation UpdateTaskIndex(
              $id: String!,
              $title: String!,
              $description: String!
            ) {
              issueUpdate(id: $id, input: { title: $title, description: $description }) {
                success
                issue { id identifier title url project { id name } }
              }
            }
            """,
            {"id": issue_id, "title": title, "description": description},
        )
        payload = data["issueUpdate"]
        if not payload.get("success"):
            raise LinearAPIError(f"issueUpdate returned success=false for {issue_id}")
        return payload["issue"]


def _task_key(project_alias: str, title: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
    return f"{project_alias.casefold()}/{normalized or 'task'}"


def _task_public_record(registry: TaskRegistry, task: Any) -> dict[str, Any]:
    index = registry.get_task_index(task.task_id)
    return {
        "task_ref": task.task_id,
        "task_key": task.task_key,
        "host": task.host,
        "workspace": task.workspace_alias,
        "project": task.project_alias,
        "project_name": task.project_name,
        "title": task.title,
        "summary": task.summary,
        "status": task.status,
        "execution_mode": task.execution_mode,
        "execution_state": task.execution_state,
        "current_stage": task.current_stage,
        "current_blocker": task.current_blocker,
        "last_progress_at": task.last_progress_at,
        "codex_running": task.codex_running,
        "turn_id": task.turn_id,
        "retry_required": task.retry_required,
        "failure_stage": task.failure_stage,
        "failure_code": task.failure_code,
        "failure_evidence": task.failure_evidence,
        "bound_thread_exists": registry.get_binding(task.task_id) is not None,
        "last_execution": registry.last_linear_execution(task.task_id),
        "task_index_issue": index.identifier if index is not None else None,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


class LinearTaskIndex:
    """Idempotently mirror human-readable task metadata into one Linear issue."""

    def __init__(self, linear: LinearClient, registry: TaskRegistry, team_id: str):
        self.linear = linear
        self.registry = registry
        self.team_id = team_id

    def _description(self, task: Any) -> str:
        public = _task_public_record(self.registry, task)
        summary = task.summary or ""
        checkpoint = self.registry.latest_context_checkpoint(task.task_id)
        return (
            "CLINX_TASK_INDEX_V1\n\n"
            f"TASK_REF={task.task_id}\n"
            f"TASK_KEY={task.task_key or ''}\n"
            f"HOST={task.host}\n"
            f"PROJECT={task.project_alias}\n"
            f"TASK_STATUS={task.status}\n"
            f"TITLE={task.title}\n"
            f"SUMMARY={summary}\n"
            f"EXECUTION_MODE={task.execution_mode}\n"
            f"BOUND_THREAD_EXISTS={'YES' if public['bound_thread_exists'] else 'NO'}\n"
            f"CONTEXT_AVAILABLE={'YES' if public['bound_thread_exists'] else 'NO'}\n"
            f"LAST_EXECUTION={public['last_execution'] or ''}\n"
            f"LAST_CONTEXT_SYNC_AT={checkpoint.timestamp if checkpoint else ''}\n"
            f"UPDATED_AT={task.updated_at}\n\n"
            "This issue is the single durable CLINX task audit record. "
            "Execution and turn lifecycle updates are appended here; conversation IDs are omitted."
        )

    @staticmethod
    def _title(task: Any) -> str:
        return f"[CLINX Task] {task.title}"

    def sync(self, task_id: str, *, project_id: str | None = None) -> TaskIndexRecord:
        task = self.registry.get_task(task_id)
        existing = self.registry.get_task_index(task_id)
        title = self._title(task)
        description = self._description(task)
        if existing is None:
            issue = self.linear.create_task_index_issue(
                team_id=self.team_id,
                project_id=project_id,
                title=title,
                description=description,
            )
            return self.registry.record_task_index(
                task_id=task_id,
                issue_id=str(issue["id"]),
                identifier=str(issue["identifier"]),
                project_id=(issue.get("project") or {}).get("id") or project_id,
            )
        self.linear.update_task_index_issue(
            existing.issue_id,
            title=title,
            description=description,
        )
        return self.registry.record_task_index(
            task_id=task_id,
            issue_id=existing.issue_id,
            identifier=existing.identifier,
            project_id=existing.project_id,
        )


class SingleInstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+")
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise BridgeError(
                f"Another bridge process holds lock: {self.path}"
            ) from e
        self.file.seek(0)
        self.file.truncate()
        self.file.write(str(os.getpid()))
        self.file.flush()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.file:
            try:
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            finally:
                self.file.close()


def is_git_repo(path: Path) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def codex_prompt(issue: dict[str, Any], repo: Path, review_state: str) -> str:
    identifier = issue["identifier"]
    return f"""You were launched automatically by linear-local-codex-bridge v{BRIDGE_VERSION}.

AUTHORITATIVE TASK
- Linear issue: {identifier}
- Working repository: {repo}
- The Linear issue is the authoritative execution contract supplied by CLINX.
- Do not fetch or mutate that contract through Linear MCP.

EXECUTION PROTOCOL
1. Execute the requested task exactly as supplied in this prompt, inside the current repository only.
2. Do not use Linear MCP, any other control-plane MCP, or issue state transitions.
3. Do not create unrelated issues/projects and do not expand scope.
4. Do not modify ORION, Terminal, SSH, DNS, or public endpoint configuration.
5. Do not use legacy `codex exec`.
6. Never mark the issue Done; CLINX owns all Linear state transitions.
7. At the end, return exactly one structured result with this header and all fields:

CLINX_EXECUTION_RESULT
STATUS=<PASS|BLOCKED>
SUMMARY=<one concise paragraph>
CHANGED_FILES=<comma-separated paths or NONE>
VALIDATION=<tests/checks and outcomes>
BLOCKERS=<NONE or exact blocker>
NEXT_STATE=<IN_REVIEW|BLOCKED|COMPLETED>

Use plain text values on one line each. If blocked, STATUS must be BLOCKED,
BLOCKERS must contain the exact blocker, NEXT_STATE must be BLOCKED, and do not
claim validation that was not run. Linear writeback and state transitions are
owned by CLINX after this result is received.

This run is unattended. Do not wait for an operator prompt or confirmation.
"""


@dataclasses.dataclass(frozen=True)
class DispatchResult:
    target_alias: str
    thread_id: str
    turn_id: str
    model: str | None
    reasoning_effort: str | None
    dispatch_status: str
    repository_identity_source: str = "app_server"
    project_alias: str | None = None
    thread_alias: str | None = None
    thread_created: bool = False
    thread_durable: bool = True
    session_id: str | None = None
    cwd: str | None = None
    task_id: str | None = None
    execution_mode: str = "normal"
    network_access: bool = False


def write_dispatch_record(
    cfg: BridgeConfig,
    issue: dict[str, Any],
    result: DispatchResult,
) -> Path:
    """Persist non-sensitive dispatch metadata for the existing bridge logs."""
    stamp = time.strftime("%Y%m%dT%H%M%S")
    run_dir = cfg.log_dir / "runs" / f"{issue['identifier']}-{stamp}-{os.getpid()}-{time.time_ns()}"
    run_dir.mkdir(parents=True, exist_ok=False)
    record = {
        "bridge_version": BRIDGE_VERSION,
        "issue_identifier": issue["identifier"],
        "target_alias": result.target_alias,
        "thread_id": result.thread_id,
        "turn_id": result.turn_id,
        "model": result.model,
        "reasoning_effort": result.reasoning_effort,
        "dispatch_status": result.dispatch_status,
        "repository_identity_source": result.repository_identity_source,
        "project_alias": result.project_alias,
        "thread_alias": result.thread_alias,
        "thread_created": result.thread_created,
        "thread_durable": result.thread_durable,
        "session_id": result.session_id,
        "cwd": result.cwd,
        "task_id": result.task_id,
        "execution_mode": result.execution_mode,
        "network_access": result.network_access,
        "NETWORK_ACCESS": "ENABLED" if result.network_access else "DISABLED",
    }
    (run_dir / "dispatch.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return run_dir


def wait_for_codex_result(
    thread_id: str,
    turn_id: str,
    expected_result: str,
    *,
    timeout_seconds: float,
) -> str | None:
    """Read only the exact turn completion event from the local Codex session log."""
    root = Path.home() / ".codex" / "sessions"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        for path in root.glob("**/*.jsonl"):
            if thread_id not in path.name:
                continue
            try:
                with path.open(encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        payload = row.get("payload")
                        if row.get("type") != "event_msg" or not isinstance(payload, dict):
                            continue
                        if payload.get("type") != "task_complete" or payload.get("turn_id") != turn_id:
                            continue
                        value = payload.get("last_agent_message")
                        return value if isinstance(value, str) else None
            except OSError:
                continue
        time.sleep(1)
    raise BridgeError(
        f"Timed out waiting for exact Codex turn completion: thread={thread_id} turn={turn_id}"
    )


def _thread_field(thread: dict[str, Any], name: str) -> Any:
    if name in thread:
        return thread[name]
    return None


def _git_info(thread: dict[str, Any]) -> dict[str, Any]:
    value = thread.get("gitInfo")
    return value if isinstance(value, dict) else {}


@dataclasses.dataclass(frozen=True)
class RepositoryIdentityEvidence:
    source: str
    cwd: str
    origin: str | None
    branch: str
    head: str | None = None


def _local_git_identity(cwd: str) -> RepositoryIdentityEvidence:
    """Read repository identity only from the cwd returned by thread/read."""
    commands = {
        "top_level": ("git", "-C", cwd, "rev-parse", "--show-toplevel"),
        "branch": ("git", "-C", cwd, "branch", "--show-current"),
        "head": ("git", "-C", cwd, "rev-parse", "HEAD"),
    }
    outputs: dict[str, str] = {}
    for name, command in commands.items():
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise IdentityGuardError(
                "DISPATCH_IDENTITY_GUARD=FAIL\n"
                f"- local Git identity lookup failed for {name} at {cwd!r}"
            ) from exc
        outputs[name] = result.stdout.strip()

    if outputs["top_level"] != cwd:
        raise IdentityGuardError(
            "DISPATCH_IDENTITY_GUARD=FAIL\n"
            f"- repository top-level expected={cwd!r} actual={outputs['top_level']!r}"
        )
    if not outputs["branch"]:
        raise IdentityGuardError(
            "DISPATCH_IDENTITY_GUARD=FAIL\n- repository is detached HEAD"
        )
    origin_result = subprocess.run(
        ("git", "-C", cwd, "remote", "get-url", "origin"),
        capture_output=True,
        text=True,
    )
    if origin_result.returncode == 0:
        origin = origin_result.stdout.strip() or None
    else:
        stderr = origin_result.stderr.casefold()
        if "no such remote" in stderr or ("remote 'origin'" in stderr and "does not exist" in stderr):
            origin = None
        else:
            raise IdentityGuardError(
                "DISPATCH_IDENTITY_GUARD=FAIL\n"
                f"- local Git origin lookup failed at {cwd!r}: {origin_result.stderr.strip() or 'unknown error'}"
            )
    return RepositoryIdentityEvidence(
        source="local_git",
        cwd=cwd,
        origin=origin,
        branch=outputs["branch"],
        head=outputs["head"] or None,
    )


def _repository_identity_evidence(
    thread: dict[str, Any],
) -> RepositoryIdentityEvidence:
    cwd = thread.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise IdentityGuardError(
            "DISPATCH_IDENTITY_GUARD=FAIL\n- thread/read returned no usable cwd"
        )
    raw_git_info = thread.get("gitInfo")
    if raw_git_info is None:
        return _local_git_identity(cwd)
    if not isinstance(raw_git_info, dict):
        raise IdentityGuardError(
            "DISPATCH_IDENTITY_GUARD=FAIL\n- thread/read returned malformed gitInfo"
        )
    raw_origin = raw_git_info.get("originUrl")
    origin = raw_origin.strip() if isinstance(raw_origin, str) and raw_origin.strip() else None
    branch = raw_git_info.get("branch")
    if not isinstance(branch, str) or not branch:
        raise IdentityGuardError(
            "DISPATCH_IDENTITY_GUARD=FAIL\n- thread/read returned incomplete gitInfo"
        )
    head = raw_git_info.get("head")
    return RepositoryIdentityEvidence(
        source="app_server",
        cwd=cwd,
        origin=origin,
        branch=branch,
        head=head if isinstance(head, str) else None,
    )


def _status_type(thread: dict[str, Any]) -> str | None:
    status = thread.get("status")
    if isinstance(status, dict):
        value = status.get("type")
        return value if isinstance(value, str) else None
    return status if isinstance(status, str) else None


def _identity_mismatches(
    target: TargetConfig,
    thread: dict[str, Any],
    *,
    repository_evidence: RepositoryIdentityEvidence | None = None,
    require_direct_input: bool = True,
) -> list[str]:
    actual = {
        "threadId": _thread_field(thread, "id"),
        "sessionId": _thread_field(thread, "sessionId"),
        "projectId": _thread_field(thread, "projectId"),
        "cwd": _thread_field(thread, "cwd"),
    }
    expected = {
        "threadId": target.thread_id,
        "sessionId": target.session_id,
        "cwd": target.cwd,
    }
    mismatches = [
        f"{key} expected={expected[key]!r} actual={actual[key]!r}"
        for key in expected
        if actual[key] != expected[key]
    ]
    actual_project = actual["projectId"]
    if target.project_id is not None:
        if actual_project != target.project_id:
            mismatches.append(
                f"projectId expected={target.project_id!r} actual={actual_project!r}"
            )
    elif actual_project is not None:
        mismatches.append(
            f"projectId expected-unassigned=None actual={actual_project!r}"
        )
    if repository_evidence is not None:
        if repository_evidence.origin != target.repository_origin:
            mismatches.append(
                "repositoryOrigin "
                f"expected={target.repository_origin!r} actual={repository_evidence.origin!r}"
            )
        if repository_evidence.branch != target.branch:
            mismatches.append(
                f"branch expected={target.branch!r} actual={repository_evidence.branch!r}"
            )
    can_accept = thread.get("canAcceptDirectInput")
    if require_direct_input and can_accept is not True:
        mismatches.append(f"canAcceptDirectInput expected=True actual={can_accept!r}")
    return mismatches


def identity_guard(
    target: TargetConfig,
    thread: dict[str, Any],
    *,
    initialize_info: Any = None,
    allow_unloaded: bool = False,
    repository_evidence: RepositoryIdentityEvidence | None = None,
) -> None:
    """Fail closed unless every dispatch identity field matches exactly."""
    mismatches = _identity_mismatches(
        target,
        thread,
        repository_evidence=repository_evidence,
        require_direct_input=not allow_unloaded,
    )
    if allow_unloaded:
        can_accept = thread.get("canAcceptDirectInput")
        if can_accept is False:
            mismatches.append("canAcceptDirectInput expected=True actual=False")
        if can_accept is not True and _status_type(thread) not in {"notLoaded", "unloaded"}:
            mismatches.append(
                "canAcceptDirectInput unavailable outside an unloaded thread state"
            )

    if initialize_info is not None:
        actual_version = getattr(initialize_info, "server_version", None) or getattr(
            initialize_info, "user_agent", None
        ) or thread.get("cliVersion")
        if not versions_compatible(target.app_server_version, actual_version):
            mismatches.append(
                f"appServerVersion expected-compatible={target.app_server_version!r} actual={actual_version!r}"
            )

    if mismatches:
        raise IdentityGuardError(
            "DISPATCH_IDENTITY_GUARD=FAIL\n" + "\n".join(f"- {item}" for item in mismatches)
        )


def turn_start_guard(thread: dict[str, Any]) -> None:
    """Require a distinct idle-turn boundary before starting an execution."""
    status = _status_type(thread)
    if status != "idle":
        raise DispatchContractError(
            "DISPATCH_TURN_START_GUARD=FAIL\n"
            f"- thread status expected='idle' actual={status!r}"
        )


def project_identity_guard(
    project: ProjectMapping,
    evidence: RepositoryIdentityEvidence,
) -> None:
    """Validate project authority before any app-server thread creation."""
    mismatches: list[str] = []
    expected_cwd = str(project.repo)
    if evidence.cwd != expected_cwd:
        mismatches.append(f"cwd expected={expected_cwd!r} actual={evidence.cwd!r}")
    if project.repository_origin is not None and evidence.origin != project.repository_origin:
        mismatches.append(
            "repositoryOrigin "
            f"expected={project.repository_origin!r} actual={evidence.origin!r}"
        )
    elif project.repository_origin is None and evidence.origin is not None:
        mismatches.append(
            "repositoryOrigin expected-absent=None "
            f"actual={evidence.origin!r} policy={project.unexpected_origin_policy}"
        )
    if project.branch is not None and evidence.branch != project.branch:
        mismatches.append(
            f"branch expected={project.branch!r} actual={evidence.branch!r}"
        )
    if mismatches:
        raise IdentityGuardError(
            "DISPATCH_IDENTITY_GUARD=FAIL\n"
            + "\n".join(f"- {item}" for item in mismatches)
        )


def _read_only_transport_target(cfg: BridgeConfig, project: ProjectMapping) -> TargetConfig:
    binding = next((item for item in cfg.threads if item.project_alias == project.project_alias), None)
    ssh_alias = (
        binding.ssh_alias
        if binding is not None
        else (cfg.app_server.ssh_alias or (cfg.threads[0].ssh_alias if cfg.threads else ""))
    )
    return TargetConfig(
        alias=f"{project.project_alias}.read-only",
        ssh_alias=ssh_alias,
        thread_id="read-only",
        session_id="read-only",
        project_id=None,
        cwd=str(project.repo),
        repository_origin=project.repository_origin,
        branch=project.branch or "",
        app_server_version=(
            binding.app_server_version
            if binding is not None
            else (cfg.threads[0].app_server_version if cfg.threads else cfg.app_server.client_version)
        ),
        target_host=(
            binding.target_host
            if binding is not None and binding.target_host
            else _project_target_host(cfg, project)
        ),
    )


def _enumerate_threads(client: CodexAppServerClient) -> list[dict[str, Any]]:
    """Enumerate metadata without reading conversation content."""
    result = client.thread_list(limit=100)
    rows = [row for row in result["data"] if isinstance(row, dict)]
    cursor = result.get("nextCursor")
    seen_cursors: set[str] = set()
    while isinstance(cursor, str) and cursor and cursor not in seen_cursors:
        seen_cursors.add(cursor)
        page = client.thread_list(cursor=cursor, limit=100)
        rows.extend(row for row in page["data"] if isinstance(row, dict))
        cursor = page.get("nextCursor")

    loaded = client.thread_loaded_list()
    known_ids = {row.get("id") for row in rows if isinstance(row.get("id"), str)}
    for thread_id in loaded["data"]:
        if isinstance(thread_id, str) and thread_id not in known_ids:
            # loaded/list is an ID index; thread/read remains the authority.
            rows.append({"id": thread_id})
            known_ids.add(thread_id)

    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        thread_id = row.get("id")
        if isinstance(thread_id, str) and thread_id:
            unique.setdefault(thread_id, row)
    return list(unique.values())


def _onboarding_candidate(
    target: TargetConfig,
    thread: dict[str, Any],
) -> dict[str, Any]:
    evidence = _repository_identity_evidence(thread)
    status = thread.get("status")
    status_value = status.get("type") if isinstance(status, dict) else status
    candidate = {
        "thread_id": thread.get("id"),
        "session_id": thread.get("sessionId"),
        "project_id": thread.get("projectId"),
        "cwd": thread.get("cwd"),
        "source": thread.get("source"),
        "status": status_value,
        "ephemeral": thread.get("ephemeral"),
        "can_accept_direct_input": thread.get("canAcceptDirectInput"),
        "model": thread.get("model"),
        "reasoning_effort": thread.get("reasoningEffort"),
        "cli_version": thread.get("cliVersion"),
        "repository_origin": evidence.origin,
        "branch": evidence.branch,
        "target_host": target.target_host,
        "ssh_alias": target.ssh_alias,
    }
    mismatches: list[str] = []
    if candidate["thread_id"] != target.thread_id:
        mismatches.append("threadId")
    if not isinstance(candidate["session_id"], str) or not candidate["session_id"]:
        mismatches.append("sessionId")
    if candidate["cwd"] != target.cwd:
        mismatches.append("cwd")
    if candidate["repository_origin"] != target.repository_origin:
        mismatches.append("repositoryOrigin")
    if candidate["branch"] != target.branch:
        mismatches.append("branch")
    if candidate["ephemeral"] is not False:
        mismatches.append("ephemeral")
    if candidate["can_accept_direct_input"] is not True:
        mismatches.append("canAcceptDirectInput")
    candidate["eligible"] = not mismatches
    candidate["classification"] = (
        "ELIGIBLE_BUSY"
        if candidate["eligible"] and status_value in {"active", "running"}
        else "ELIGIBLE_IDLE"
        if candidate["eligible"]
        else "INELIGIBLE"
    )
    candidate["mismatches"] = mismatches
    return candidate


def _persist_orion_current(config_path: Path, candidate: dict[str, Any]) -> None:
    _persist_thread_binding(
        config_path,
        project_alias="orion",
        thread_alias="current",
        candidate=candidate,
    )


def _persist_thread_binding(
    config_path: Path,
    *,
    project_alias: str,
    thread_alias: str,
    candidate: dict[str, Any],
) -> None:
    """Append one identity-only binding without replacing an existing alias."""
    text = config_path.read_text(encoding="utf-8")
    table_header = f"[threads.{project_alias}.{thread_alias}]"
    if re.search(rf"(?m)^\s*{re.escape(table_header)}\s*$", text):
        raise ReadOnlyOnboardingError(
            f"{project_alias}.{thread_alias} is already registered"
        )
    thread_id = candidate.get("thread_id")
    session_id = candidate.get("session_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ReadOnlyOnboardingError("cannot register thread without exact thread_id")
    if not isinstance(session_id, str) or not session_id:
        raise ReadOnlyOnboardingError("cannot register thread without exact session_id")
    ssh_alias = candidate.get("ssh_alias", "p620")
    target_host = candidate.get("target_host")
    app_server_version = candidate.get("app_server_version") or candidate.get("cli_version")
    if not isinstance(app_server_version, str) or not app_server_version:
        raise ReadOnlyOnboardingError("cannot register thread without app-server version")
    block = (
        f"\n{table_header}\n"
        f"# Identity only; cwd, origin, and branch remain authoritative in projects.{project_alias}.\n"
        + (f"host = {json.dumps(str(target_host))}\n" if target_host else "")
        + f"ssh_alias = {json.dumps(str(ssh_alias))}\n"
        + f"thread_id = {json.dumps(thread_id)}\n"
        + f"session_id = {json.dumps(session_id)}\n"
        + (
            f"project_id = {json.dumps(candidate['project_id'])}\n"
            if candidate.get("project_id") is not None
            else ""
        )
        + f"app_server_version = {json.dumps(app_server_version)}\n"
    )
    temporary = config_path.with_suffix(config_path.suffix + ".tmp")
    temporary.write_text(text.rstrip() + block + "\n", encoding="utf-8")
    temporary.replace(config_path)


def register_existing_thread(
    cfg: BridgeConfig,
    project_alias: str,
    thread_alias: str,
    thread_id: str,
    *,
    client_factory=None,
    config_path: Path,
) -> dict[str, Any]:
    """Register a selected existing thread after a fresh identity-only read."""
    project = ProjectRegistry(cfg.projects).resolve(project_alias)
    project_evidence = _local_git_identity(str(project.repo))
    project_identity_guard(project, project_evidence)
    if not thread_id.strip():
        raise ReadOnlyOnboardingError("thread_id is required")
    if not thread_alias.strip():
        raise ReadOnlyOnboardingError("thread alias is required")
    # Parse the file before contacting the server so an alias can never be
    # silently replaced, even if the selected thread is otherwise valid.
    existing = tomllib.loads(config_path.read_text(encoding="utf-8"))
    existing_threads = existing.get("threads", {})
    if isinstance(existing_threads, dict):
        project_threads = existing_threads.get(project_alias, {})
        if isinstance(project_threads, dict) and thread_alias in project_threads:
            raise ReadOnlyOnboardingError(
                f"{project_alias}.{thread_alias} is already registered"
            )

    target = _read_only_transport_target(cfg, project)
    client_factory = client_factory or (lambda value: _default_app_server_client(cfg, value))
    client = client_factory(target)
    with client:
        initialize_info = client.initialize(
            client_name=cfg.app_server.client_name,
            client_title=cfg.app_server.client_title,
            client_version=cfg.app_server.client_version,
        )
        thread = client.thread_read(thread_id)
        if thread.get("id") != thread_id:
            raise IdentityGuardError(
                "DISPATCH_IDENTITY_GUARD=FAIL\n"
                f"- threadId expected={thread_id!r} actual={thread.get('id')!r}"
            )
        if thread.get("ephemeral") is not False:
            raise IdentityGuardError(
                "DISPATCH_IDENTITY_GUARD=FAIL\n"
                f"- ephemeral expected=False actual={thread.get('ephemeral')!r}"
            )
        session_id = thread.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise IdentityGuardError(
                "DISPATCH_IDENTITY_GUARD=FAIL\n- sessionId is missing"
            )
        evidence = _repository_identity_evidence(thread)
        target_for_guard = TargetConfig(
            alias=f"{project_alias}.{thread_alias}",
            ssh_alias=target.ssh_alias,
            thread_id=thread_id,
            session_id=session_id,
            project_id=thread.get("projectId"),
            cwd=target.cwd,
            repository_origin=target.repository_origin,
            branch=target.branch,
            app_server_version=target.app_server_version,
            target_host=target.target_host,
        )
        identity_guard(
            target_for_guard,
            thread,
            initialize_info=initialize_info,
            repository_evidence=evidence,
        )
        actual_version = (
            thread.get("cliVersion")
            or initialize_info.user_agent
            or initialize_info.server_version
            or target.app_server_version
        )
        candidate = {
            "thread_id": thread_id,
            "session_id": session_id,
            "project_id": thread.get("projectId"),
            "ssh_alias": target.ssh_alias,
            "target_host": target.target_host,
            "app_server_version": actual_version,
        }

        _persist_thread_binding(
            config_path,
            project_alias=project_alias,
            thread_alias=thread_alias,
            candidate=candidate,
        )
        loaded = BridgeConfig.load(config_path)
        binding = ThreadRegistry(loaded.threads).resolve(project_alias, thread_alias)
        readback = client.thread_read(binding.thread_id)
        readback_evidence = _repository_identity_evidence(readback)
        readback_target = _target_for_binding(project, binding)
        identity_guard(
            readback_target,
            readback,
            initialize_info=initialize_info,
            repository_evidence=readback_evidence,
        )
    return {
        "project_alias": project_alias,
        "thread_alias": thread_alias,
        "thread_id": binding.thread_id,
        "session_id": binding.session_id,
        "project_id": binding.project_id,
        "app_server_version": binding.app_server_version,
        "registered": True,
    }


def onboard_existing_thread(
    cfg: BridgeConfig,
    project_alias: str,
    *,
    client_factory=None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Read-only existing-thread discovery; no turn or thread mutation calls."""
    project = ProjectRegistry(cfg.projects).resolve(project_alias)
    project_evidence = _local_git_identity(str(project.repo))
    project_identity_guard(project, project_evidence)
    target = _read_only_transport_target(cfg, project)
    client_factory = client_factory or (lambda value: _default_app_server_client(cfg, value))
    client = client_factory(target)
    with client:
        initialize_info = client.initialize(
            client_name=cfg.app_server.client_name,
            client_title=cfg.app_server.client_title,
            client_version=cfg.app_server.client_version,
        )
        metadata = _enumerate_threads(client)
        candidates: list[dict[str, Any]] = []
        for row in metadata:
            if row.get("cwd") != str(project.repo):
                continue
            thread_id = row.get("id")
            if not isinstance(thread_id, str) or not thread_id:
                continue
            thread = client.thread_read(thread_id)
            candidates.append(_onboarding_candidate(TargetConfig(
                alias=target.alias,
                ssh_alias=target.ssh_alias,
                thread_id=thread_id,
                session_id="",
                project_id=None,
                cwd=target.cwd,
                repository_origin=target.repository_origin,
                branch=target.branch,
                app_server_version=target.app_server_version,
                target_host=target.target_host,
            ), thread))

    eligible = [item for item in candidates if item["eligible"]]
    if len(eligible) == 1:
        selection = "UNAMBIGUOUS"
        if config_path is not None:
            _persist_orion_current(config_path, eligible[0])
        registered = True
    elif len(eligible) > 1:
        selection = "AMBIGUOUS"
        registered = False
    else:
        selection = "NONE"
        registered = False
    return {
        "initialize_server_version": initialize_info.server_version,
        "initialize_user_agent": initialize_info.user_agent,
        "threads_enumerated": len(metadata),
        "orion_candidates": len(candidates),
        "eligible_orion_threads": len(eligible),
        "selection": selection,
        "needs_user_selection": len(eligible) > 1,
        "registered": registered,
        "candidates": candidates,
        "selected": eligible[0] if len(eligible) == 1 else None,
    }


def _target_for_binding(
    project: ProjectMapping,
    binding: ThreadBinding,
    *,
    thread_id: str | None = None,
    session_id: str | None = None,
    project_id: str | None = None,
) -> TargetConfig:
    """Adapt the M3 registries to the existing exact-field guard."""
    return TargetConfig(
        alias=binding.qualified_alias,
        ssh_alias=binding.ssh_alias,
        thread_id=thread_id or binding.thread_id,
        session_id=session_id or binding.session_id,
        project_id=binding.project_id if project_id is None else project_id,
        cwd=str(project.repo),
        repository_origin=project.repository_origin,
        branch=project.branch or "",
        app_server_version=binding.app_server_version,
        target_host=binding.target_host,
    )


def _project_target_host(cfg: BridgeConfig, project: ProjectMapping) -> str:
    if not project.workspace_alias:
        return ""
    workspace = next(
        (item for item in cfg.workspaces if item.alias.lower() == project.workspace_alias.lower()),
        None,
    )
    if workspace is None:
        raise TargetResolutionError(
            f"Project {project.project_alias!r} references unknown workspace {project.workspace_alias!r}"
        )
    return canonical_host(workspace.host or workspace.alias)


def _default_app_server_client(
    cfg: BridgeConfig,
    target: TargetConfig,
) -> CodexAppServerClient:
    if not target.target_host:
        raise BridgeError(
            f"Target {target.alias!r} has no target_host for transport selection"
        )
    selected_transport = resolve_transport(
        cfg.runtime_host,
        target.target_host,
        cfg.app_server.transport,
    )
    if selected_transport == "local":
        transport = LocalStdioTransport(
            cfg.app_server.command,
            timeout_seconds=cfg.app_server.request_timeout_seconds,
        )
    elif selected_transport == "ssh":
        ssh_alias = target.ssh_alias or cfg.app_server.ssh_alias
        if not ssh_alias:
            raise BridgeError("SSH app-server transport requires ssh_alias")
        transport = SSHStdioTransport(
            ssh_alias,
            cfg.app_server.remote_command,
            ssh_binary=cfg.app_server.ssh_binary,
            ssh_args=cfg.app_server.ssh_args,
            timeout_seconds=cfg.app_server.request_timeout_seconds,
        )
    else:
        raise BridgeError(f"Unsupported app-server transport: {selected_transport}")
    return CodexAppServerClient(
        transport,
        timeout_seconds=cfg.app_server.request_timeout_seconds,
    )


class TaskDispatcher:
    """M5 task dispatcher: durable task identity plus exact thread binding."""

    def __init__(
        self,
        cfg: BridgeConfig,
        *,
        task_registry: TaskRegistry | None = None,
        client_factory=None,
        linear=None,
    ):
        self.cfg = cfg
        self.workspaces = WorkspaceRegistry(cfg.workspaces)
        self.tasks = task_registry or TaskRegistry(
            cfg.task_db_path
            or (Path.home() / ".local" / "state" / "clinx" / "tasks.sqlite3")
        )
        self.client_factory = client_factory or (
            lambda target: _default_app_server_client(cfg, target)
        )
        self.linear = linear
        self.projects = DynamicProjectResolver(self.workspaces, cfg.projects)
        self.last_task_id: str | None = None
        self.last_execution_ref: str | None = None

    @staticmethod
    def _turn_text(value: Any) -> str:
        parts: list[str] = []
        def visit(item: Any) -> None:
            if isinstance(item, dict):
                kind = str(item.get("type", "")).casefold()
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
                for key in ("items", "item", "content", "message", "output", "parts"):
                    if key in item:
                        visit(item[key])
            elif isinstance(item, list):
                for child in item:
                    visit(child)
            elif isinstance(item, str) and item.strip():
                parts.append(item.strip())
        visit(value)
        return "\n".join(dict.fromkeys(parts))

    def _execution_state(self, task_id: str, state: str, **kwargs: Any) -> None:
        """Persist machine execution state without changing Linear's coarse state."""
        self.tasks.set_execution_state(task_id, state, **kwargs)

    def _workspace(self, host: str | None) -> WorkspaceConfig:
        if host:
            try:
                return self.workspaces.resolve(host)
            except TaskRegistryError:
                requested = canonical_host(host)
                matches = [
                    workspace for workspace in self.workspaces
                    if canonical_host(workspace.host or workspace.alias) == requested
                ]
                if len(matches) == 1:
                    return matches[0]
                raise TargetResolutionError(f"Unknown execution host: {host}")
        values = list(self.workspaces)
        if len(values) == 1:
            return values[0]
        raise TargetResolutionError("M5 HOST is required when multiple workspaces exist")

    def resolve_project(
        self,
        project_ref: str,
        *,
        host: str | None = None,
        project_mode: str,
    ) -> tuple[WorkspaceConfig, ProjectDescriptor, ProjectMapping]:
        workspace = self._workspace(host)
        descriptor = self.projects.resolve(
            project_ref,
            workspace_alias=workspace.alias,
            project_mode=project_mode,
        )
        mapping = ProjectMapping(
            linear_name=descriptor.name,
            repo=descriptor.cwd,
            alias=descriptor.alias,
            repository_origin=descriptor.repository_origin,
            branch=descriptor.branch,
            workspace_alias=descriptor.workspace_alias,
        )
        return workspace, descriptor, mapping

    def _target(
        self,
        workspace: WorkspaceConfig,
        project: ProjectMapping,
        binding: DurableConversationBinding,
    ) -> TargetConfig:
        return TargetConfig(
            alias=f"{workspace.alias}.{project.project_alias}.{binding.task_id}",
            ssh_alias=workspace.ssh_alias or workspace.alias,
            thread_id=binding.thread_id,
            session_id=binding.session_id,
            project_id=binding.project_id,
            cwd=str(project.repo),
            repository_origin=project.repository_origin,
            branch=project.branch or "",
            app_server_version=binding.app_server_version or self.cfg.app_server.client_version,
            target_host=canonical_host(workspace.host or workspace.alias),
        )

    def _new_target(
        self,
        workspace: WorkspaceConfig,
        project: ProjectMapping,
    ) -> TargetConfig:
        return TargetConfig(
            alias=f"{workspace.alias}.{project.project_alias}.new",
            ssh_alias=workspace.ssh_alias or workspace.alias,
            thread_id="",
            session_id="",
            project_id=None,
            cwd=str(project.repo),
            repository_origin=project.repository_origin,
            branch=project.branch or "",
            app_server_version=self.cfg.app_server.client_version,
            target_host=canonical_host(workspace.host or workspace.alias),
        )

    @staticmethod
    def _initialize_version(info: Any, fallback: str) -> str:
        return (
            getattr(info, "server_version", None)
            or getattr(info, "user_agent", None)
            or fallback
        )

    def _read_and_guard(
        self,
        client: Any,
        target: TargetConfig,
        initialize_info: Any,
    ) -> dict[str, Any]:
        thread = client.thread_read(target.thread_id)
        identity_guard(
            target,
            thread,
            initialize_info=initialize_info,
            allow_unloaded=True,
        )
        needs_resume = (
            thread.get("canAcceptDirectInput") is None
            and _status_type(thread) in {"notLoaded", "unloaded"}
        )
        if needs_resume:
            client.thread_resume(target.thread_id)
            thread = client.thread_read(target.thread_id)
        evidence = _repository_identity_evidence(thread)
        identity_guard(
            target,
            thread,
            initialize_info=initialize_info,
            repository_evidence=evidence,
        )
        return thread

    def dispatch(
        self,
        *,
        project_ref: str,
        host: str | None,
        project_mode: str,
        task_mode: str,
        task_id: str | None,
        prompt: str,
        title: str,
        summary: str | None,
        task_key: str | None = None,
        update_title: bool = False,
        model: str | None,
        reasoning_effort: str | None,
        execution_mode: str = "normal",
        network_access: bool = False,
        issue_id: str | None = None,
        execution_ref: str | None = None,
    ) -> DispatchResult:
        if task_mode not in {"new", "continue"}:
            raise DispatchContractError(f"Unsupported task mode: {task_mode!r}")
        if execution_mode not in {"normal", "fast"}:
            raise DispatchContractError(f"Unsupported execution mode: {execution_mode!r}")
        if not isinstance(network_access, bool):
            raise DispatchContractError("network_access must be a boolean")
        if network_access and self.cfg.sandbox != "workspace-write":
            raise DispatchContractError(
                "NETWORK_ACCESS=ENABLED requires configured sandbox=workspace-write"
            )

        if task_mode == "new":
            self.last_execution_ref = execution_ref or issue_id
            workspace, _descriptor, project = self.resolve_project(
                project_ref, host=host, project_mode=project_mode
            )
            conflict = self.tasks.active_worktree_conflict(
                host=host or workspace.alias, cwd=str(project.repo),
                repository_origin=project.repository_origin,
            )
            if conflict is not None:
                raise WorktreeExecutionBusy(
                    f"Worktree already has an active execution: {project.repo}",
                    active_task_id=conflict["active_task_id"],
                    active_execution_ref=conflict.get("active_execution_ref"),
                    stage=conflict.get("stage") or "ACTIVE",
                    worktree_key=conflict["worktree_key"],
                )
            task = self.tasks.create_task(
                host=host or workspace.alias,
                workspace_alias=workspace.alias,
                project_alias=project.project_alias,
                project_name=project.linear_name,
                cwd=str(project.repo),
                repository_origin=project.repository_origin,
                branch=project.branch,
                title=title,
                summary=summary,
                task_key=task_key or _task_key(project.project_alias, title),
                execution_mode=execution_mode,
            )
            self.last_task_id = task.task_id
            with self.tasks.execution(
                task.task_id, issue_id, execution_ref=execution_ref,
                retain=bool(execution_ref and execution_ref.startswith("exec_"))
            ) as leased:
                self._execution_state(leased.task_id, "CLAIMED", current_stage="claim")
                target = self._new_target(workspace, project)
                client = self.client_factory(target)
                try:
                    self._execution_state(leased.task_id, "DISPATCHING", current_stage="identity guard")
                    with client:
                        initialize_info = client.initialize(
                            client_name=self.cfg.app_server.client_name,
                            client_title=self.cfg.app_server.client_title,
                            client_version=self.cfg.app_server.client_version,
                        )
                        executable_model, executable_reasoning = _resolve_dispatch_model(
                            client, model, reasoning_effort
                        )
                        if execution_ref:
                            self.tasks.set_execution_models(
                                execution_ref, logical_model=model,
                                resolved_model=executable_model,
                            )
                        started = client.thread_start(
                            cwd=str(project.repo),
                            model=executable_model,
                            sandbox=self.cfg.sandbox,
                            ephemeral=False,
                        )
                        new_thread_id = started.get("id")
                        new_session_id = started.get("sessionId")
                        if not isinstance(new_thread_id, str) or not new_thread_id:
                            raise IdentityGuardError(
                                "DISPATCH_IDENTITY_GUARD=FAIL\n- thread/start returned no exact thread id"
                            )
                        if not isinstance(new_session_id, str) or not new_session_id:
                            raise IdentityGuardError(
                                "DISPATCH_IDENTITY_GUARD=FAIL\n- thread/start returned no exact session id"
                            )
                        if started.get("ephemeral") is True:
                            raise IdentityGuardError(
                                "DISPATCH_IDENTITY_GUARD=FAIL\n- thread/start returned ephemeral=true"
                            )
                        actual_project_id = started.get("projectId")
                        if actual_project_id is not None and not isinstance(actual_project_id, str):
                            raise IdentityGuardError(
                                "DISPATCH_IDENTITY_GUARD=FAIL\n- thread/start returned malformed projectId"
                            )
                        version = self._initialize_version(
                            initialize_info, self.cfg.app_server.client_version
                        )
                        binding = DurableConversationBinding(
                            task_id=leased.task_id,
                            thread_id=new_thread_id,
                            session_id=new_session_id,
                            project_id=actual_project_id,
                            bound_at="",
                            last_verified_at="",
                            app_server_version=version,
                        )
                        target = self._target(workspace, project, binding)
                        thread = self._read_and_guard(client, target, initialize_info)
                        turn_start_guard(thread)
                        binding = self.tasks.bind_conversation(
                            task_id=leased.task_id,
                            thread_id=new_thread_id,
                            session_id=new_session_id,
                            project_id=actual_project_id,
                            app_server_version=version,
                        )
                        if network_access:
                            turn = client.turn_start(
                                new_thread_id,
                                prompt,
                                cwd=str(project.repo),
                                model=executable_model,
                                reasoning_effort=executable_reasoning,
                                approval_policy=self.cfg.approval,
                                network_access=True,
                                writable_roots=[str(project.repo)],
                            )
                        else:
                            turn = client.turn_start(
                                new_thread_id,
                                prompt,
                                cwd=str(project.repo),
                                model=executable_model,
                                reasoning_effort=executable_reasoning,
                                approval_policy=self.cfg.approval,
                            )
                        self._execution_state(
                            leased.task_id,
                            "CODEX_RUNNING",
                            current_stage="Codex turn",
                            current_blocker=None,
                            codex_running=True,
                            turn_id=turn.turn_id,
                            retry_required=False,
                        )
                except IdentityGuardError:
                    self._execution_state(
                        leased.task_id,
                        "BLOCKED",
                        current_stage="project identity guard",
                        current_blocker="pre-dispatch handoff failed",
                        codex_running=False,
                        retry_required=True,
                    )
                    raise
                except AppServerError:
                    self._execution_state(
                        leased.task_id, "RECOVERY_REQUIRED", current_stage="dispatch",
                        current_blocker="recoverable app-server failure", codex_running=False,
                        retry_required=True,
                    )
                    raise
                except Exception as exc:
                    self._execution_state(
                        leased.task_id,
                        "BLOCKED",
                        current_stage="dispatch",
                        current_blocker=str(exc)[:2000],
                        codex_running=False,
                        retry_required=True,
                    )
                    raise
            if issue_id:
                self.tasks.record_linear_execution(
                    execution_ref or issue_id, leased.task_id
                )
            return DispatchResult(
                target_alias=f"{workspace.alias}.{project.project_alias}",
                thread_id=binding.thread_id,
                turn_id=turn.turn_id,
                model=turn.model or model,
                reasoning_effort=turn.reasoning_effort or reasoning_effort,
                dispatch_status="DISPATCHED",
                repository_identity_source="app_server",
                project_alias=project.project_alias,
                thread_alias=None,
                thread_created=True,
                thread_durable=True,
                session_id=binding.session_id,
                cwd=str(project.repo),
                task_id=leased.task_id,
                execution_mode=execution_mode,
                network_access=network_access,
            )

        if not task_id:
            raise DispatchContractError("TASK_MODE=continue requires TASK_ID")
        task = self.tasks.get_task(task_id)
        self.last_execution_ref = execution_ref or issue_id
        if task.status != "ACTIVE":
            raise TargetResolutionError(
                f"Task {task.task_id} is {task.status}; explicit reopen is required"
            )
        if host and canonical_host(host) != canonical_host(task.host):
            raise TargetResolutionError(
                f"Task host mismatch: expected {task.host!r}, got {host!r}"
            )
        binding = self.tasks.get_binding(task.task_id)
        if binding is None:
            raise TargetResolutionError(f"Task {task.task_id} has no conversation binding")
        workspace = self.workspaces.resolve(task.workspace_alias)
        if project_ref.strip().lower() not in {task.project_alias.lower(), task.project_name.lower()}:
            raise TargetResolutionError(
                f"Task project mismatch: expected {task.project_alias!r}, got {project_ref!r}"
            )
        project_path = self.workspaces.validate_path(workspace, Path(task.cwd))
        project = ProjectMapping(
            linear_name=task.project_name,
            repo=project_path,
            alias=task.project_alias,
            repository_origin=task.repository_origin,
            branch=task.branch,
            workspace_alias=task.workspace_alias,
        )
        task = self.tasks.update_metadata(
            task.task_id,
            title=title if update_title else None,
            summary=summary,
            execution_mode=execution_mode,
        )
        with self.tasks.execution(
            task.task_id, issue_id, execution_ref=execution_ref,
            retain=bool(execution_ref and execution_ref.startswith("exec_"))
        ) as leased:
            self.last_task_id = leased.task_id
            self._execution_state(leased.task_id, "CLAIMED", current_stage="claim")
            target = self._target(workspace, project, binding)
            client = self.client_factory(target)
            try:
                self._execution_state(leased.task_id, "DISPATCHING", current_stage="identity guard")
                with client:
                    initialize_info = client.initialize(
                        client_name=self.cfg.app_server.client_name,
                        client_title=self.cfg.app_server.client_title,
                        client_version=self.cfg.app_server.client_version,
                    )
                    executable_model, executable_reasoning = _resolve_dispatch_model(
                        client, model, reasoning_effort
                    )
                    if execution_ref:
                        self.tasks.set_execution_models(
                            execution_ref, logical_model=model,
                            resolved_model=executable_model,
                        )
                    thread = self._read_and_guard(client, target, initialize_info)
                    turn_start_guard(thread)
                    self.tasks.mark_verified(
                        task.task_id,
                        app_server_version=self._initialize_version(
                            initialize_info, target.app_server_version
                        ),
                    )
                    if network_access:
                        turn = client.turn_start(
                            binding.thread_id,
                            prompt,
                            cwd=str(project.repo),
                            model=executable_model,
                            reasoning_effort=executable_reasoning,
                            approval_policy=self.cfg.approval,
                            network_access=True,
                            writable_roots=[str(project.repo)],
                        )
                    else:
                        turn = client.turn_start(
                            binding.thread_id,
                            prompt,
                            cwd=str(project.repo),
                            model=executable_model,
                            reasoning_effort=executable_reasoning,
                            approval_policy=self.cfg.approval,
                        )
                    self._execution_state(
                        leased.task_id,
                        "CODEX_RUNNING",
                        current_stage="Codex turn",
                        current_blocker=None,
                        codex_running=True,
                        turn_id=turn.turn_id,
                        retry_required=False,
                    )
            except IdentityGuardError:
                self._execution_state(
                    leased.task_id, "BLOCKED", current_stage="project identity guard",
                    current_blocker="pre-dispatch handoff failed", codex_running=False,
                    retry_required=True,
                )
                raise
            except AppServerError:
                self._execution_state(
                    leased.task_id, "RECOVERY_REQUIRED", current_stage="dispatch",
                    current_blocker="recoverable app-server failure", codex_running=False,
                    retry_required=True,
                )
                raise
            except Exception as exc:
                self._execution_state(
                    leased.task_id, "BLOCKED", current_stage="dispatch",
                    current_blocker=str(exc)[:2000], codex_running=False,
                    retry_required=True,
                )
                raise
        if issue_id:
            self.tasks.record_linear_execution(execution_ref or issue_id, leased.task_id)
        return DispatchResult(
            target_alias=f"{workspace.alias}.{project.project_alias}",
            thread_id=binding.thread_id,
            turn_id=turn.turn_id,
            model=turn.model or model,
            reasoning_effort=turn.reasoning_effort or reasoning_effort,
            dispatch_status="DISPATCHED",
            repository_identity_source="app_server",
            project_alias=project.project_alias,
            session_id=binding.session_id,
            cwd=str(project.repo),
            task_id=leased.task_id,
            execution_mode=execution_mode,
            network_access=network_access,
        )

    def adopt_existing_conversation(
        self,
        *,
        project_ref: str,
        host: str | None,
        thread_id: str,
        title: str,
        summary: str,
        task_key: str | None = None,
        execution_mode: str = "normal",
        task_index: "LinearTaskIndex | None" = None,
        task_index_project_id: str | None = None,
        require_direct_input: bool = True,
    ) -> tuple[Any, DurableConversationBinding, TaskIndexRecord | None]:
        """Adopt one existing exact conversation without sending a turn."""
        if not thread_id.strip():
            raise DispatchContractError("Existing conversation adoption requires THREAD_ID")
        workspace, _descriptor, project = self.resolve_project(
            project_ref, host=host, project_mode="existing"
        )
        project_evidence = _local_git_identity(str(project.repo))
        project_identity_guard(project, project_evidence)
        target = _read_only_transport_target(self.cfg, project)
        client = self.client_factory(target)
        with client:
            initialize_info = client.initialize(
                client_name=self.cfg.app_server.client_name,
                client_title=self.cfg.app_server.client_title,
                client_version=self.cfg.app_server.client_version,
            )
            thread = client.thread_read(thread_id)
            if require_direct_input and _status_type(thread) in {"notLoaded", "unloaded"}:
                client.thread_resume(thread_id)
                thread = client.thread_read(thread_id)
            if thread.get("ephemeral") is not False:
                raise IdentityGuardError(
                    "DISPATCH_IDENTITY_GUARD=FAIL\n"
                    f"- ephemeral expected=False actual={thread.get('ephemeral')!r}"
                )
            session_id = thread.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                raise IdentityGuardError(
                    "DISPATCH_IDENTITY_GUARD=FAIL\n- sessionId is missing"
                )
            evidence = _repository_identity_evidence(thread)
            target_for_guard = TargetConfig(
                alias=f"{project.project_alias}.adopt",
                ssh_alias=target.ssh_alias,
                thread_id=thread_id,
                session_id=session_id,
                project_id=thread.get("projectId"),
                cwd=str(project.repo),
                repository_origin=project.repository_origin,
                branch=project.branch or "",
                app_server_version=target.app_server_version,
                target_host=target.target_host,
            )
            if require_direct_input:
                identity_guard(
                    target_for_guard,
                    thread,
                    initialize_info=initialize_info,
                    repository_evidence=evidence,
                )
            else:
                HistoricalConversationDiscovery._historical_guard(
                    target_for_guard,
                    thread,
                    initialize_info=initialize_info,
                    repository_evidence=evidence,
                )
            version = self._initialize_version(
                initialize_info, target.app_server_version
            )
            existing = self.tasks.get_binding_by_thread(thread_id)
            if existing is not None:
                raise TaskRegistryError(
                    f"ADOPTION=FAIL: thread {thread_id} is already bound to task {existing.task_id}"
                )
            task, binding = self.tasks.adopt_task(
                host=host or workspace.alias,
                workspace_alias=workspace.alias,
                project_alias=project.project_alias,
                project_name=project.linear_name,
                cwd=str(project.repo),
                repository_origin=project.repository_origin,
                branch=project.branch,
                title=title,
                summary=summary,
                task_key=task_key or _task_key(project.project_alias, title),
                execution_mode=execution_mode,
                thread_id=thread_id,
                session_id=session_id,
                project_id=thread.get("projectId"),
                app_server_version=version,
            )
        index = None
        if task_index is not None:
            index = task_index.sync(task.task_id, project_id=task_index_project_id)
        return task, binding, index

    def adopt_or_reuse_existing_conversation(
        self,
        *,
        project_ref: str,
        host: str | None,
        thread_id: str,
        title: str,
        summary: str,
        task_key: str | None = None,
        execution_mode: str = "normal",
        task_index: "LinearTaskIndex | None" = None,
        task_index_project_id: str | None = None,
        require_direct_input: bool = True,
    ) -> tuple[Any, DurableConversationBinding, TaskIndexRecord | None]:
        """Adopt a thread once, or reuse its canonical existing task binding."""
        existing = self.tasks.get_binding_by_thread(thread_id)
        if existing is not None:
            task = self.tasks.get_task(existing.task_id)
            if task.project_alias.casefold() != project_ref.casefold() and \
                    task.project_name.casefold() != project_ref.casefold():
                raise TargetResolutionError(
                    f"Existing binding project mismatch: {task.project_alias!r}"
                )
            if host and canonical_host(task.host) != canonical_host(host):
                raise TargetResolutionError(
                    f"Existing binding host mismatch: {task.host!r}"
                )
            if task.status != "ACTIVE":
                task = self.tasks.set_status(task.task_id, "ACTIVE")
            index = (
                task_index.sync(task.task_id, project_id=task_index_project_id)
                if task_index is not None else None
            )
            return task, existing, index
        return self.adopt_existing_conversation(
            project_ref=project_ref,
            host=host,
            thread_id=thread_id,
            title=title,
            summary=summary,
            task_key=task_key,
            execution_mode=execution_mode,
            task_index=task_index,
            task_index_project_id=task_index_project_id,
            require_direct_input=require_direct_input,
        )

    def task_action(self, task_id: str, action: str) -> str:
        if action == "complete":
            return self.tasks.set_status(task_id, "COMPLETED").status
        if action == "archive":
            return self.tasks.set_status(task_id, "ARCHIVED").status
        if action == "reopen":
            task = self.tasks.get_task(task_id)
            if task.status not in {"COMPLETED", "ARCHIVED"}:
                raise TaskRegistryError(f"Task is already active: {task_id}")
            self.tasks.reset_execution(task_id)
            return self.tasks.set_status(task_id, "ACTIVE").status
        raise DispatchContractError(f"Unsupported TASK_ACTION={action!r}")

    def cancel_execution(self, execution_ref: str) -> dict[str, Any]:
        """Durably request cancellation, then confirm it with the provider."""
        active = self.tasks.get_active_execution(execution_ref)
        if active is None:
            raise TaskRegistryError(f"Unknown or inactive execution: {execution_ref}")
        self.tasks.request_cancellation(execution_ref)
        task = self.tasks.get_task(active["task_id"])
        binding = self.tasks.get_binding(task.task_id)
        if binding is None or not task.turn_id:
            raise TaskRegistryError("Active execution has no exact conversation turn")
        workspace = self.workspaces.resolve(task.workspace_alias)
        project_path = self.workspaces.validate_path(workspace, Path(task.cwd))
        project = ProjectMapping(
            linear_name=task.project_name, repo=project_path, alias=task.project_alias,
            repository_origin=task.repository_origin, branch=task.branch,
            workspace_alias=task.workspace_alias,
        )
        client = self.client_factory(self._target(workspace, project, binding))
        bounded_items: list[dict[str, Any]] = []
        try:
            with client:
                client.initialize(
                    client_name=self.cfg.app_server.client_name,
                    client_title=self.cfg.app_server.client_title,
                    client_version=self.cfg.app_server.client_version,
                )
                client.turn_interrupt(binding.thread_id, task.turn_id)
        except Exception as exc:
            pending = self.tasks.mark_cancellation_pending(
                execution_ref, evidence=str(exc)
            )
            return {
                "execution_ref": execution_ref,
                "task_ref": pending.task_id,
                "status": "CANCELLATION_PENDING",
                "cancel_requested": True,
                "cancel_confirmed": False,
                "retry_required": True,
            }
        cancelled = self.tasks.finalize_cancellation(execution_ref)
        return {
            "execution_ref": execution_ref,
            "task_ref": cancelled.task_id,
            "status": "CANCELLED",
            "cancel_requested": True,
            "cancel_confirmed": True,
            "retry_required": False,
        }

    @staticmethod
    def _turn_status(row: dict[str, Any]) -> str:
        value = row.get("status")
        if isinstance(value, dict):
            value = value.get("type") or value.get("status") or value.get("state")
        if value is None:
            value = row.get("state") or row.get("statusType")
        return str(value or "UNKNOWN").replace("_", "").replace("-", "").casefold()

    def reconcile_execution(self, execution_ref: str) -> dict[str, Any]:
        """Perform one bounded provider read and converge durable execution truth."""
        active = self.tasks.get_active_execution(execution_ref)
        if active is None:
            return {"state": "UNKNOWN", "authoritative": False}
        task = self.tasks.get_task(active["task_id"])
        binding = self.tasks.get_binding(task.task_id)
        if binding is None or not task.turn_id:
            return {"state": "TRANSPORT_UNCERTAIN", "authoritative": False}
        workspace = self.workspaces.resolve(task.workspace_alias)
        project_path = self.workspaces.validate_path(workspace, Path(task.cwd))
        project = ProjectMapping(
            linear_name=task.project_name, repo=project_path, alias=task.project_alias,
            repository_origin=task.repository_origin, branch=task.branch,
            workspace_alias=task.workspace_alias,
        )
        client = self.client_factory(self._target(workspace, project, binding))
        try:
            with client:
                client.initialize(
                    client_name=self.cfg.app_server.client_name,
                    client_title=self.cfg.app_server.client_title,
                    client_version=self.cfg.app_server.client_version,
                )
                page = client.thread_turns_list(
                    binding.thread_id, limit=20, sort_direction="desc", items_view="summary"
                )
                candidate = next(
                    (item for item in page.get("data", ())
                     if isinstance(item, dict) and item.get("id") == task.turn_id),
                    None,
                )
                if candidate is not None and not candidate.get("items") and hasattr(client, "thread_items_list"):
                    item_page = client.thread_items_list(
                        binding.thread_id, turn_id=task.turn_id, limit=100,
                        sort_direction="desc",
                    )
                    bounded_items = [item for item in item_page.get("data", ()) if isinstance(item, dict)]
        except AppServerError as exc:
            evidence = str(exc)
            # A child exit/closed byte channel is terminal evidence for this
            # local execution; a generic reachability failure is uncertain.
            if "exit " in evidence.casefold() or "byte transport closed" in evidence.casefold():
                self.tasks.reconcile_terminal(
                    execution_ref, "RECOVERY_REQUIRED", failure_stage="transport",
                    failure_code="APP_SERVER_TRANSPORT_FAILURE", evidence=evidence,
                )
                return {"state": "RECOVERY_REQUIRED", "authoritative": True, "evidence": evidence}
            if task.execution_state in {"CANCEL_REQUESTED", "CANCELLATION_PENDING"}:
                self.tasks.mark_cancellation_pending(execution_ref, evidence=evidence)
                return {"state": "CANCELLATION_PENDING", "authoritative": False, "evidence": evidence}
            self.tasks.set_execution_state(
                task.task_id, "TRANSPORT_UNCERTAIN", current_stage="reconciliation",
                current_blocker=evidence[:2000], codex_running=bool(task.codex_running),
                retry_required=True, failure_stage="transport",
                failure_code="PROVIDER_UNAVAILABLE", failure_evidence=evidence[:4000],
            )
            return {"state": "TRANSPORT_UNCERTAIN", "authoritative": False, "evidence": evidence}
        except Exception as exc:
            evidence = str(exc)
            if task.execution_state in {"CANCEL_REQUESTED", "CANCELLATION_PENDING"}:
                self.tasks.mark_cancellation_pending(execution_ref, evidence=evidence)
                return {"state": "CANCELLATION_PENDING", "authoritative": False, "evidence": evidence}
            self.tasks.set_execution_state(
                task.task_id, "TRANSPORT_UNCERTAIN", current_stage="reconciliation",
                current_blocker=evidence[:2000], codex_running=bool(task.codex_running),
                retry_required=True, failure_stage="transport",
                failure_code="PROVIDER_UNAVAILABLE", failure_evidence=evidence[:4000],
            )
            return {"state": "TRANSPORT_UNCERTAIN", "authoritative": False, "evidence": evidence}
        rows = [item for item in page.get("data", ()) if isinstance(item, dict)]
        row = next((item for item in rows if item.get("id") == task.turn_id), None)
        if row is None:
            if task.execution_state in {"CANCEL_REQUESTED", "CANCELLATION_PENDING"}:
                self.tasks.mark_cancellation_pending(
                    execution_ref,
                    evidence="bounded thread/turns/list did not contain the exact turn",
                )
                return {"state": "CANCELLATION_PENDING", "authoritative": False}
            self.tasks.set_execution_state(
                task.task_id, "TRANSPORT_UNCERTAIN", current_stage="reconciliation",
                current_blocker="exact turn was not present in bounded provider history",
                codex_running=bool(task.codex_running), retry_required=True,
                failure_stage="reconciliation", failure_code="TURN_NOT_OBSERVED",
                failure_evidence="bounded thread/turns/list did not contain the exact turn",
            )
            return {"state": "TRANSPORT_UNCERTAIN", "authoritative": False}
        status = self._turn_status(row)
        if status in {"cancelled", "canceled", "interrupted", "aborted"}:
            self.tasks.reconcile_terminal(
                execution_ref, "CANCELLED", failure_stage="provider",
                failure_code="TURN_CANCELLED", evidence=f"provider turn status={status}",
                retry_required=False,
            )
            return {"state": "CANCELLED", "authoritative": True}
        if status in {"completed", "succeeded", "success"}:
            result = self.tasks.latest_execution_result(task.task_id)
            if result is not None:
                final_state = "BLOCKED" if result.status == "BLOCKED" else "COMPLETED"
                self.tasks.reconcile_terminal(
                    execution_ref, final_state, failure_stage=None,
                    failure_code=None, evidence=None,
                    retry_required=final_state == "BLOCKED",
                )
                return {"state": final_state, "authoritative": True}
            raw_result = self._turn_text(row.get("items", row))
            if not raw_result and bounded_items:
                raw_result = self._turn_text(bounded_items)
            if raw_result:
                index = self.tasks.get_task_index(task.task_id)
                try:
                    from m9_integration import parse_codex_result
                    parsed = parse_codex_result(raw_result)
                except Exception:
                    parsed = None
                if parsed is not None:
                    if self.linear is not None and index is not None:
                        try:
                            states = self.linear.team_states(self.cfg.team_id)
                            ExecutionResultService(self.tasks, self.linear).receive_and_writeback(
                                execution_ref=execution_ref,
                                task_id=task.task_id,
                                turn_id=task.turn_id,
                                raw_result=raw_result,
                                issue_id=index.issue_id,
                                review_state_id=states.get(self.cfg.review_state),
                                blocked_state_id=states.get(self.cfg.todo_state),
                            )
                        except Exception as exc:
                            self.tasks.set_execution_state(
                                task.task_id, "RECOVERY_REQUIRED", current_stage="result",
                                current_blocker=str(exc)[:2000], codex_running=False,
                                retry_required=True, failure_stage="linear_writeback",
                                failure_code="LINEAR_AUDIT_SYNC_FAILED",
                                failure_evidence=str(exc)[:4000],
                            )
                            return {"state": "RECOVERY_REQUIRED", "authoritative": True}
                    else:
                        self.tasks.record_execution_result(
                            execution_ref=execution_ref, task_id=task.task_id,
                            turn_id=task.turn_id, status=parsed.status,
                            summary=parsed.summary, changed_files=parsed.changed_files,
                            validation=parsed.validation, blockers=parsed.blockers,
                            next_state=parsed.next_state, raw_result=raw_result,
                        )
                        final_state = "BLOCKED" if parsed.status == "BLOCKED" else "COMPLETED"
                        self.tasks.reconcile_terminal(
                            execution_ref, final_state,
                            retry_required=final_state == "BLOCKED",
                        )
                    return {"state": "COMPLETED", "authoritative": True}
            self.tasks.reconcile_terminal(
                execution_ref, "RECOVERY_REQUIRED", failure_stage="result",
                failure_code="TURN_COMPLETED_WITHOUT_RESULT",
                evidence="provider turn is terminal but no CLINX result marker was persisted",
            )
            return {"state": "RECOVERY_REQUIRED", "authoritative": True}
        if status in {"failed", "error", "systemerror", "errored"}:
            self.tasks.reconcile_terminal(
                execution_ref, "RECOVERY_REQUIRED", failure_stage="provider",
                failure_code="PROVIDER_TURN_FAILURE", evidence=f"provider turn status={status}",
            )
            return {"state": "RECOVERY_REQUIRED", "authoritative": True}
        return {"state": "CODEX_RUNNING", "authoritative": False, "status": status}


@dataclasses.dataclass(frozen=True)
class HistoricalConversationCandidate:
    thread_id: str
    session_id: str
    project_id: str | None
    matched_terms: tuple[str, ...]
    relevance: str
    context_range: str
    anchor_turn_id: str | None
    context_text: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "session_id": self.session_id,
            "project_id": self.project_id,
            "matched_terms": self.matched_terms,
            "relevance": self.relevance,
            "context_range": self.context_range,
            "anchor_turn_id": self.anchor_turn_id,
        }


class HistoricalConversationDiscovery:
    """Find and adopt one historical task using bounded native history only."""

    INITIAL_TURNS = 4
    EXPANSION_TURNS = 8
    MAX_EXPANSION_PAGES = 8
    SEARCH_TERMS = (
        "product-active", "--product-active", "USER_PRODUCT_ACTIVE_SEMANTIC",
        "CustomerProduct", "TenantAccounts", "Subscription active", "OKX VERIFIED",
        "Strategy Launch RUNNING", "brand accent", "financial positive",
        "trading direction", "chart accent",
    )
    HIGH_TERMS = (
        "--product-active", "USER_PRODUCT_ACTIVE_SEMANTIC", "product-active",
    )

    def __init__(
        self,
        cfg: BridgeConfig,
        registry: TaskRegistry,
        *,
        client_factory=None,
    ):
        self.cfg = cfg
        self.registry = registry
        self.client_factory = client_factory or (
            lambda target: _default_app_server_client(cfg, target)
        )
        self.dispatcher = TaskDispatcher(
            cfg, task_registry=registry, client_factory=self.client_factory
        )
        self.reader = TaskContextReader(
            cfg, registry, client_factory=self.client_factory
        )

    @staticmethod
    def _historical_guard(
        target: TargetConfig,
        thread: dict[str, Any],
        *,
        initialize_info: Any,
        repository_evidence: RepositoryIdentityEvidence,
    ) -> None:
        """Validate project/thread identity without requiring direct-input readiness."""
        mismatches = _identity_mismatches(
            target, thread, repository_evidence=repository_evidence,
            require_direct_input=False,
        )
        if thread.get("ephemeral") is not False:
            mismatches.append(f"ephemeral expected=False actual={thread.get('ephemeral')!r}")
        actual_version = (
            getattr(initialize_info, "server_version", None)
            or getattr(initialize_info, "user_agent", None)
            or thread.get("cliVersion")
        )
        if not versions_compatible(target.app_server_version, actual_version):
            mismatches.append(
                f"appServerVersion expected-compatible={target.app_server_version!r} "
                f"actual={actual_version!r}"
            )
        if mismatches:
            raise IdentityGuardError(
                "DISPATCH_IDENTITY_GUARD=FAIL\n"
                + "\n".join(f"- {item}" for item in mismatches)
            )

    @staticmethod
    def _row_id(row: dict[str, Any]) -> str | None:
        value = row.get("id") or row.get("turnId")
        return value if isinstance(value, str) and value else None

    @classmethod
    def _terms(cls, texts: list[str]) -> tuple[str, ...]:
        combined = "\n".join(texts).casefold()
        return tuple(term for term in cls.SEARCH_TERMS if term.casefold() in combined)

    @classmethod
    def _candidate_from_text(
        cls,
        thread: dict[str, Any],
        turn_texts: dict[str, str],
    ) -> HistoricalConversationCandidate:
        ordered = list(turn_texts.items())
        all_text = [text for _turn_id, text in ordered]
        terms = cls._terms(all_text)
        high = any(term.casefold() in {item.casefold() for item in terms} for term in cls.HIGH_TERMS)
        relevance = "HIGH" if high else "MEDIUM" if terms else "NONE"
        anchor = None
        if high:
            ranked = sorted(
                ordered,
                key=lambda item: (
                    not any(term.casefold() in item[1].casefold() for term in cls.HIGH_TERMS),
                    -sum(term.casefold() in item[1].casefold() for term in cls.SEARCH_TERMS),
                ),
            )
            anchor = ranked[0][0]
        turn_ids = [turn_id for turn_id, _text in ordered]
        return HistoricalConversationCandidate(
            thread_id=str(thread.get("id")),
            session_id=str(thread.get("sessionId")),
            project_id=thread.get("projectId"),
            matched_terms=terms,
            relevance=relevance,
            context_range=(
                f"{turn_ids[-1]}..{turn_ids[0]}" if turn_ids else "bounded-tail"
            ),
            anchor_turn_id=anchor,
            context_text="\n".join(all_text),
        )

    def _turn_summary_texts(
        self, rows: list[dict[str, Any]]
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        for row in rows:
            turn_id = self._row_id(row)
            if turn_id is None:
                continue
            _users, _assistants, texts, _ids, _roles = self.reader._collect_item_text(
                row.get("items", row)
            )
            if texts:
                result[turn_id] = "\n".join(texts)
        return result

    def _expand_candidate(
        self,
        client: Any,
        thread_id: str,
        first_page: dict[str, Any],
    ) -> dict[str, str]:
        turn_texts = self._turn_summary_texts(
            [row for row in first_page.get("data", ()) if isinstance(row, dict)]
        )
        cursor = first_page.get("nextCursor")
        pages = 0
        while isinstance(cursor, str) and cursor and pages < self.MAX_EXPANSION_PAGES:
            pages += 1
            page = client.thread_turns_list(
                thread_id,
                limit=self.EXPANSION_TURNS,
                cursor=cursor,
                sort_direction="desc",
                items_view="summary",
            )
            rows = [row for row in page.get("data", ()) if isinstance(row, dict)]
            for row in rows:
                turn_id = self._row_id(row)
                if turn_id is None:
                    continue
                texts = self._turn_summary_texts([row]).get(turn_id, "")
                if texts:
                    turn_texts.setdefault(turn_id, texts)
                item_page = client.thread_items_list(
                    thread_id,
                    turn_id=turn_id,
                    limit=20,
                    sort_direction="desc",
                )
                _users, _assistants, texts, _ids, _roles = self.reader._collect_item_text(
                    item_page.get("data", ())
                )
                if texts:
                    turn_texts[turn_id] = "\n".join(texts)
            if self._terms(list(turn_texts.values())) and any(
                term.casefold() in "\n".join(turn_texts.values()).casefold()
                for term in self.HIGH_TERMS
            ):
                break
            cursor = page.get("nextCursor")
        return turn_texts

    def discover(
        self,
        *,
        project_ref: str,
        host: str | None = None,
    ) -> dict[str, Any]:
        """Return bounded candidates; no Task or Linear mutation occurs."""
        workspace, _descriptor, project = self.dispatcher.resolve_project(
            project_ref, host=host, project_mode="existing"
        )
        project_evidence = _local_git_identity(str(project.repo))
        project_identity_guard(project, project_evidence)
        target = _read_only_transport_target(self.cfg, project)
        client = self.client_factory(target)
        candidates: list[HistoricalConversationCandidate] = []
        eligible = 0
        discovered = 0
        with client:
            initialize_info = client.initialize(
                client_name=self.cfg.app_server.client_name,
                client_title=self.cfg.app_server.client_title,
                client_version=self.cfg.app_server.client_version,
            )
            metadata = _enumerate_threads(client)
            discovered = len(metadata)
            for row in metadata:
                thread_id = row.get("id")
                if not isinstance(thread_id, str) or not thread_id:
                    continue
                thread = client.thread_read(thread_id)
                if thread.get("cwd") != str(project.repo):
                    continue
                session_id = thread.get("sessionId")
                if not isinstance(session_id, str) or not session_id:
                    continue
                try:
                    evidence = _repository_identity_evidence(thread)
                    exact_target = dataclasses.replace(
                        target,
                        thread_id=thread_id,
                        session_id=session_id,
                        project_id=thread.get("projectId"),
                    )
                    self._historical_guard(
                        exact_target, thread, initialize_info=initialize_info,
                        repository_evidence=evidence,
                    )
                except (IdentityGuardError, AppServerError):
                    continue
                eligible += 1
                page = client.thread_turns_list(
                    thread_id,
                    limit=self.INITIAL_TURNS,
                    sort_direction="desc",
                    items_view="summary",
                )
                turn_texts = self._turn_summary_texts(
                    [item for item in page.get("data", ()) if isinstance(item, dict)]
                )
                preliminary = self._candidate_from_text(thread, turn_texts)
                if preliminary.relevance == "MEDIUM":
                    turn_texts = self._expand_candidate(client, thread_id, page)
                candidates.append(self._candidate_from_text(thread, turn_texts))
        return {
            "project": project.project_alias,
            "host": workspace.alias,
            "threads_discovered": discovered,
            "eligible_threads": eligible,
            "candidates": tuple(candidates),
        }

    def adopt_unique(
        self,
        *,
        project_ref: str,
        host: str | None = None,
        title: str = "ORION UI/UX Semantic Token Consolidation",
        task_index: "LinearTaskIndex | None" = None,
        task_index_project_id: str | None = None,
    ) -> dict[str, Any]:
        result = self.discover(project_ref=project_ref, host=host)
        candidates = list(result["candidates"])
        strong = [item for item in candidates if item.relevance == "HIGH"]
        if len(strong) != 1:
            raise TargetResolutionError(
                f"UI_TOKEN_HISTORICAL_CONVERSATION_MATCH=FAIL; "
                f"HIGH_CONFIDENCE_MATCHES={len(strong)}"
            )
        candidate = strong[0]
        summary = (
            "Historical Codex context confirms product-active semantic token work "
            f"with evidence: {', '.join(candidate.matched_terms)}."
        )
        task, binding, index = self.dispatcher.adopt_or_reuse_existing_conversation(
            project_ref=project_ref,
            host=host,
            thread_id=candidate.thread_id,
            title=title,
            summary=summary,
            task_key=_task_key(project_ref, title),
            task_index=task_index,
            task_index_project_id=task_index_project_id,
            require_direct_input=False,
        )
        if candidate.anchor_turn_id is None:
            raise ContextReadError("Historical match has no exact anchor turn")
        self.registry.set_context_anchor(
            task_id=task.task_id,
            thread_id=binding.thread_id,
            turn_id=candidate.anchor_turn_id,
            source="APP_SERVER_NATIVE",
        )
        return {
            "task": task,
            "binding": binding,
            "task_index": index,
            "candidate": candidate,
            "discovery": result,
        }


def _context_text(value: Any, maximum: int) -> str:
    """Convert one protocol value into bounded, non-secret display text."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (int, float, bool)):
        text = str(value)
    elif isinstance(value, list):
        text = "\n".join(_context_text(item, maximum) for item in value)
    elif isinstance(value, dict):
        for key in ("text", "value", "message", "content", "summary"):
            if key in value:
                text = _context_text(value[key], maximum)
                break
        else:
            text = ""
    else:
        text = ""
    text = re.sub(r"\blin_api_[A-Za-z0-9]+\b", "[REDACTED]", text)
    text = re.sub(
        r"<in-app-browser-context\b[^>]*>.*?</in-app-browser-context>",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = " ".join(text.replace("\x00", "").split())
    return text[:maximum]


def _context_extract_fields(texts: list[str]) -> tuple[str, str, str, str]:
    """Deterministically extract useful recovery markers from bounded text."""
    combined = "\n".join(item for item in texts if item)
    if not combined:
        return "UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN"

    boundary = (
        r"(?=\s+(?:changed[_ ]files?|files[_ ]changed|validation|tests?|blockers?|"
        r"blocker|current[_ ]state|next[_ ]state|next[_ ]step)\s*[:=]"
        r"|\s+[A-Z][A-Z0-9_]{2,}=|$)"
    )

    def marker(labels: str, default: str = "UNKNOWN") -> str:
        pattern = rf"(?:^|\s)(?:{labels})\s*[:=]\s*(.*?){boundary}"
        match = re.search(pattern, combined, flags=re.IGNORECASE | re.MULTILINE)
        return _context_text(match.group(1).rstrip(" ;"), 4000) if match else default

    changed = marker(r"changed[_ ]files?|files[_ ]changed")
    validation = marker(r"validation|tests?")
    blockers = marker(r"blockers?|blocker")
    state = marker(r"current[_ ]state|next[_ ]state|next[_ ]step")
    return changed, validation, blockers, state


def _bounded_context_text(texts: list[str], maximum_bytes: int) -> list[str]:
    """Keep the newest bounded text without exceeding a UTF-8 byte budget."""
    if maximum_bytes <= 0:
        return []
    result: list[str] = []
    used = 0
    for text in texts:
        encoded = text.encode("utf-8")
        separator = 1 if result else 0
        remaining = maximum_bytes - used - separator
        if remaining <= 0:
            break
        if len(encoded) > remaining:
            clipped = encoded[:remaining].decode("utf-8", errors="ignore")
            if clipped:
                result.append(clipped)
            break
        result.append(text)
        used += separator + len(encoded)
    return result


def _latest_bounded_text(texts: list[str], maximum_bytes: int) -> str:
    """Return the latest useful message, bounded by UTF-8 bytes."""
    if not texts:
        return "UNKNOWN"
    return texts[0].encode("utf-8")[:maximum_bytes].decode("utf-8", errors="ignore") or "UNKNOWN"


def _context_provenance_json(value: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for key in ("turn_ids", "item_ids", "message_ids", "execution_ids"):
        raw = value.get(key, ())
        if isinstance(raw, (list, tuple)):
            result[key] = tuple(
                item for item in raw if isinstance(item, str) and item
            )
    return result


class TaskContextReader:
    """Read authoritative, bounded context for one registry task.

    The normal entry point is a task reference.  Human discovery may resolve a
    unique task first, but no path in this class chooses a latest thread or
    accepts an arbitrary thread id as the task identity.
    """

    DEFAULT_RECENT_TURNS = 8
    DEFAULT_MAX_BYTES = 32_000
    MAX_RECENT_TURNS = 20
    MAX_CONTEXT_BYTES = 128_000
    MAX_ITEM_PAGES = 8

    def __init__(
        self,
        cfg: BridgeConfig,
        registry: TaskRegistry,
        *,
        client_factory=None,
        local_history_root: Path | None = None,
    ):
        self.cfg = cfg
        self.registry = registry
        self.workspaces = WorkspaceRegistry(cfg.workspaces)
        self.client_factory = client_factory or (
            lambda target: _default_app_server_client(cfg, target)
        )
        self.local_history_root = local_history_root or (Path.home() / ".codex" / "sessions")

    def resolve_task(
        self,
        *,
        task_ref: str | None = None,
        host: str | None = None,
        project: str | None = None,
        query: str | None = None,
    ) -> Any:
        if task_ref:
            task = self.registry.get_task(task_ref)
            if host and canonical_host(task.host) != canonical_host(host):
                raise TargetResolutionError(
                    f"Task host mismatch: expected {task.host!r}, got {host!r}"
                )
            if project and project.casefold() not in {
                task.project_alias.casefold(), task.project_name.casefold()
            }:
                raise TargetResolutionError(
                    f"Task project mismatch: expected {task.project_alias!r}, got {project!r}"
                )
            return task
        if not query:
            raise ContextReadError("task reference or discovery query is required")
        if not project:
            raise ContextReadError("project is required for task discovery")
        found = self.registry.find_tasks(
            query=query,
            host=host,
            project=project,
            include_archived=True,
        )
        if found.classification == "NONE":
            raise TargetResolutionError("No task matched the discovery query")
        if found.classification == "AMBIGUOUS":
            refs = ", ".join(item.task_id for item in found.tasks)
            raise TargetResolutionError(f"Task discovery is ambiguous: {refs}")
        return found.tasks[0]

    def _task_target(self, task: Any, binding: DurableConversationBinding) -> TargetConfig:
        return TargetConfig(
            alias=f"{task.project_alias}.context",
            ssh_alias=self._workspace_ssh_alias(task.workspace_alias, binding),
            thread_id=binding.thread_id,
            session_id=binding.session_id,
            project_id=binding.project_id,
            cwd=task.cwd,
            repository_origin=task.repository_origin,
            branch=task.branch or "",
            app_server_version=binding.app_server_version or self.cfg.app_server.client_version,
            target_host=canonical_host(task.host),
        )

    def _workspace_ssh_alias(
        self, workspace_alias: str, binding: DurableConversationBinding
    ) -> str:
        for workspace in self.workspaces:
            if workspace.alias.casefold() == workspace_alias.casefold():
                return workspace.ssh_alias or self.cfg.app_server.ssh_alias or ""
        return ""

    @staticmethod
    def _turn_id(row: dict[str, Any]) -> str | None:
        for key in ("id", "turnId"):
            value = row.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def _collect_item_text(
        self, value: Any, *, role: str | None = None
    ) -> tuple[list[str], list[str], list[str], list[str], list[str | None]]:
        users: list[str] = []
        assistants: list[str] = []
        all_text: list[str] = []
        message_ids: list[str] = []
        roles: list[str | None] = []

        def append_text(text: str, hint: str | None) -> None:
            all_text.append(text)
            roles.append(hint)
            if hint == "user":
                users.append(text)
            elif hint == "assistant":
                assistants.append(text)

        def visit(node: Any, hint: str | None = None) -> None:
            if isinstance(node, dict):
                item_id = node.get("id") or node.get("itemId") or node.get("messageId")
                if isinstance(item_id, str) and item_id:
                    message_ids.append(item_id)
                kind = str(node.get("type") or node.get("role") or "").casefold()
                local_hint = hint
                if any(token in kind for token in ("user", "human", "input")):
                    local_hint = "user"
                elif any(token in kind for token in ("assistant", "agent", "codex", "developer")):
                    local_hint = "assistant"
                for key in (
                    "text", "message", "summary", "lastAgentMessage", "last_agent_message",
                    "aggregatedOutput",
                ):
                    if key in node:
                        if key == "aggregatedOutput" and local_hint != "assistant":
                            # Command stdout is large, noisy, and may contain
                            # credentials.  It must not displace actual user
                            # and final-agent messages from the context budget.
                            continue
                        text = _context_text(node[key], 8000)
                        if text:
                            key_hint = (
                                "assistant"
                                if key.casefold() in {
                                    "lastagentmessage", "last_agent_message", "aggregatedoutput",
                                }
                                else local_hint
                            )
                            append_text(text, key_hint)
                # thread/items/list wraps each item as {item: {...}, turnId}.
                # Do not recurse through arbitrary metadata: fields such as
                # phase=final_answer are protocol metadata, not message text.
                if "item" in node:
                    visit(node["item"], local_hint)
                if "content" in node:
                    visit(node["content"], local_hint)
            elif isinstance(node, list):
                for child in node:
                    visit(child, hint)
            elif isinstance(node, str) and hint:
                text = _context_text(node, 8000)
                if text:
                    append_text(text, hint)

        visit(value, role)
        return users, assistants, all_text, message_ids, roles

    @staticmethod
    def _bounded_messages(
        texts: list[str], roles: list[str | None], maximum_bytes: int
    ) -> tuple[list[str], list[str], list[str], bool]:
        """Apply one shared byte budget to newest-first protocol messages."""
        bounded = _bounded_context_text(texts, maximum_bytes)
        bounded_roles = roles[:len(bounded)]
        users = [text for text, role in zip(bounded, bounded_roles) if role == "user"]
        assistants = [
            text for text, role in zip(bounded, bounded_roles) if role == "assistant"
        ]
        raw_size = len("\n".join(texts).encode("utf-8"))
        return users, assistants, bounded, raw_size > maximum_bytes

    def _read_native(
        self,
        client: Any,
        task: Any,
        binding: DurableConversationBinding,
        target: TargetConfig,
        initialize_info: Any,
        *,
        recent_turns: int,
        max_bytes: int,
        anchor_turn_id: str | None = None,
        allow_not_ready: bool = False,
    ) -> dict[str, Any]:
        thread = client.thread_read(binding.thread_id)
        evidence = _repository_identity_evidence(thread)
        if anchor_turn_id is not None or allow_not_ready:
            # Historical adoption is read-only and may point at a completed
            # thread that is not currently ready to accept direct input.
            HistoricalConversationDiscovery._historical_guard(
                target,
                thread,
                initialize_info=initialize_info,
                repository_evidence=evidence,
            )
        else:
            identity_guard(
                target,
                thread,
                initialize_info=initialize_info,
                allow_unloaded=True,
                repository_evidence=evidence,
            )
        if not hasattr(client, "thread_turns_list") or not hasattr(client, "thread_items_list"):
            raise BoundedHistoryUnavailable("app-server bounded history methods are unavailable")
        if anchor_turn_id is not None:
            # Historical adoption stores an exact relevant turn because a
            # thread can contain later, unrelated work.  Read only that
            # bounded segment; never fall back to the newest turn.
            turn_rows = [{"id": anchor_turn_id, "status": "completed", "items": []}]
            page: dict[str, Any] = {"data": turn_rows}
        else:
            try:
                page = client.thread_turns_list(
                    binding.thread_id,
                    limit=recent_turns,
                    sort_direction="desc",
                    items_view="summary",
                )
            except AppServerError as exc:
                raise BoundedHistoryUnavailable(str(exc)) from exc
            turn_rows = [row for row in page.get("data", ()) if isinstance(row, dict)]
        turn_ids = [item for item in (self._turn_id(row) for row in turn_rows) if item]
        users: list[str] = []
        assistants: list[str] = []
        all_text: list[str] = []
        item_ids: list[str] = []
        roles: list[str | None] = []
        item_page_truncated = False

        def add_messages(value: Any) -> None:
            nonlocal users, assistants, all_text, item_ids, roles
            u, a, text, ids, item_roles = self._collect_item_text(value)
            known_ids = set(item_ids)
            if ids and all(item_id in known_ids for item_id in ids):
                return
            users.extend(u)
            assistants.extend(a)
            all_text.extend(text)
            item_ids.extend(item_id for item_id in ids if item_id not in known_ids)
            roles.extend(item_roles)

        for turn_id in turn_ids:
            turn_row = next((row for row in turn_rows if self._turn_id(row) == turn_id), None)
            if turn_row is not None and isinstance(turn_row.get("items"), list):
                # With itemsView=summary the protocol includes the user and
                # final agent messages even when the bounded item page is
                # occupied by newer command/reasoning items.
                add_messages(turn_row["items"])
            item_cursor: str | None = None
            seen_item_cursors: set[str] = set()
            item_pages = 0
            while item_pages < self.MAX_ITEM_PAGES:
                item_kwargs: dict[str, Any] = {
                    "turn_id": turn_id,
                    "limit": 20,
                    "sort_direction": "desc",
                }
                if item_cursor is not None:
                    item_kwargs["cursor"] = item_cursor
                try:
                    item_page = client.thread_items_list(
                        binding.thread_id,
                        **item_kwargs,
                    )
                except AppServerError as exc:
                    raise BoundedHistoryUnavailable(str(exc)) from exc
                item_pages += 1
                for item in item_page.get("data", ()):
                    add_messages(item)

                next_cursor = item_page.get("nextCursor")
                raw_size = len("\n".join(all_text).encode("utf-8"))
                enough_messages = bool(users and assistants)
                byte_budget_exhausted = raw_size > max_bytes
                if enough_messages or byte_budget_exhausted:
                    # We deliberately stop with a bounded, useful slice.  A
                    # remaining cursor means older items were intentionally
                    # omitted and must be visible to callers as truncated.
                    if isinstance(next_cursor, str) and next_cursor:
                        item_page_truncated = True
                    break
                if not isinstance(next_cursor, str) or not next_cursor:
                    break
                if next_cursor in seen_item_cursors:
                    # A malformed server cursor must not create an unbounded
                    # loop or make the read appear complete.
                    item_page_truncated = True
                    break
                seen_item_cursors.add(next_cursor)
                item_cursor = next_cursor
            else:
                # The page cap, rather than the server, ended the read.
                item_page_truncated = True

            if users and assistants:
                break
            if len("\n".join(all_text).encode("utf-8")) > max_bytes:
                break
        users, assistants, bounded_text, byte_truncated = self._bounded_messages(
            all_text, roles, max_bytes
        )
        changed, validation, blockers, state = _context_extract_fields(bounded_text)
        status = _status_type(thread) or "UNKNOWN"
        if state == "UNKNOWN" and turn_rows:
            state = _context_text(turn_rows[0].get("status"), 4000) or status
        # backwardsCursor is a reverse-pagination anchor and is present on a
        # non-empty page; only nextCursor means older context was omitted.
        truncated = bool(
            (not anchor_turn_id and page.get("nextCursor"))
            or item_page_truncated
            or byte_truncated
        )
        return {
            "source": "APP_SERVER_NATIVE",
            "turn_ids": tuple(turn_ids),
            "item_ids": tuple(dict.fromkeys(item_ids)),
            "message_ids": tuple(dict.fromkeys(item_ids)),
            "user": _latest_bounded_text(users, max_bytes) if users else "UNKNOWN",
            "assistant": _latest_bounded_text(assistants, max_bytes) if assistants else "UNKNOWN",
            "changed": changed,
            "validation": validation,
            "blockers": blockers,
            "state": state,
            "status": status,
            "truncated": truncated,
            "anchor_turn_id": anchor_turn_id,
        }

    def _session_path(self, thread_id: str) -> Path | None:
        if not self.local_history_root.is_dir():
            return None
        matches = sorted(
            path for path in self.local_history_root.rglob("*.jsonl")
            if thread_id in path.name
        )
        return matches[0] if matches else None

    def _read_local(self, thread_id: str, *, max_bytes: int) -> dict[str, Any] | None:
        path = self._session_path(thread_id)
        if path is None:
            return None
        try:
            size = path.stat().st_size
            offset = max(0, size - max_bytes)
            with path.open("rb") as stream:
                stream.seek(offset)
                data = stream.read(max_bytes)
        except OSError:
            return None
        if offset:
            data = data.split(b"\n", 1)[1] if b"\n" in data else b""
        users: list[str] = []
        assistants: list[str] = []
        all_text: list[str] = []
        provenance: list[str] = []
        for line in data.splitlines():
            try:
                row = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(row, dict):
                continue
            payload = row.get("payload", row)
            u, a, text, ids, _roles = self._collect_item_text(payload)
            users.extend(u)
            assistants.extend(a)
            all_text.extend(text)
            provenance.extend(ids)
            if isinstance(row.get("id"), str):
                provenance.append(row["id"])
        if not all_text:
            return None
        changed, validation, blockers, state = _context_extract_fields(all_text)
        return {
            "source": "CODEX_LOCAL_SESSION",
            "turn_ids": (),
            "item_ids": tuple(dict.fromkeys(provenance)),
            "message_ids": tuple(dict.fromkeys(provenance)),
            "user": users[-1] if users else "UNKNOWN",
            "assistant": assistants[-1] if assistants else "UNKNOWN",
            "changed": changed,
            "validation": validation,
            "blockers": blockers,
            "state": state,
            "status": "UNKNOWN",
            "truncated": offset > 0,
            "path": str(path),
        }

    @staticmethod
    def _from_checkpoint(checkpoint: ContextCheckpoint) -> dict[str, Any]:
        return {
            "source": "CLINX_CHECKPOINT",
            "turn_ids": (checkpoint.turn_id,) if checkpoint.turn_id else (),
            "item_ids": (),
            "message_ids": (),
            "execution_ids": (checkpoint.execution_id,) if checkpoint.execution_id else (),
            "user": checkpoint.prompt_summary or "UNKNOWN",
            "assistant": checkpoint.result_summary or "UNKNOWN",
            "changed": checkpoint.changed_files or "UNKNOWN",
            "validation": checkpoint.validation_summary or "UNKNOWN",
            "blockers": checkpoint.blockers or "UNKNOWN",
            "state": checkpoint.next_state or "UNKNOWN",
            "status": "UNKNOWN",
            "truncated": False,
        }

    def read_task_context(
        self,
        task_ref: str,
        *,
        recent_turns: int = DEFAULT_RECENT_TURNS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        allow_not_ready: bool = False,
    ) -> TaskContext:
        if not isinstance(recent_turns, int) or isinstance(recent_turns, bool) or not 1 <= recent_turns <= self.MAX_RECENT_TURNS:
            raise ContextReadError("recent_turns must be an integer from 1 to 20")
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1024 <= max_bytes <= self.MAX_CONTEXT_BYTES:
            raise ContextReadError("max_bytes must be an integer from 1024 to 128000")
        task = self.resolve_task(task_ref=task_ref)
        binding = self.registry.get_binding(task.task_id)
        if binding is None:
            raise ContextReadError(f"Task {task.task_id} has no conversation binding")
        target = self._task_target(task, binding)
        checkpoint = self.registry.latest_context_checkpoint(task.task_id)
        anchor = self.registry.get_context_anchor(task.task_id)
        if anchor is not None and anchor.thread_id != binding.thread_id:
            raise ContextReadError("Stored context anchor does not match task binding")
        native: dict[str, Any] | None = None
        try:
            client = self.client_factory(target)
            with client:
                initialize_info = client.initialize(
                    client_name=self.cfg.app_server.client_name,
                    client_title=self.cfg.app_server.client_title,
                    client_version=self.cfg.app_server.client_version,
                )
                native = self._read_native(
                    client, task, binding, target, initialize_info,
                    recent_turns=recent_turns,
                    max_bytes=max_bytes,
                    anchor_turn_id=anchor.turn_id if anchor is not None else None,
                    allow_not_ready=allow_not_ready,
                )
        except BoundedHistoryUnavailable:
            native = None
        except AppServerError:
            native = None

        selected = native
        if selected is None:
            selected = self._read_local(binding.thread_id, max_bytes=max_bytes)
        if selected is None and checkpoint is not None:
            selected = self._from_checkpoint(checkpoint)
        if selected is None:
            raise ContextReadError(
                f"No bounded context source available for task {task.task_id}"
            )

        turn_ids = tuple(item for item in selected.get("turn_ids", ()) if item)
        checkpoint_stale = bool(
            native is not None and checkpoint is not None and
            checkpoint.turn_id not in {None, *turn_ids}
        )
        source = str(selected.get("source", "UNKNOWN"))
        context_range = (
            f"{turn_ids[-1]}..{turn_ids[0]}" if turn_ids else "bounded-tail"
        )
        provenance = {
            "turn_ids": turn_ids,
            "item_ids": tuple(selected.get("item_ids", ())),
            "message_ids": tuple(selected.get("message_ids", ())),
            "execution_ids": tuple(selected.get("execution_ids", ())),
        }
        return TaskContext(
            task_ref=task.task_id,
            task_title=task.title,
            task_status=task.status,
            host=task.host,
            project=task.project_alias,
            context_source=source,
            context_range=context_range,
            context_truncated=bool(selected.get("truncated", False)),
            checkpoint_stale=checkpoint_stale,
            last_user_intent=_context_text(selected.get("user"), 4000) or "UNKNOWN",
            last_codex_result=_context_text(selected.get("assistant"), 4000) or "UNKNOWN",
            changed_files=_context_text(selected.get("changed"), 4000) or "UNKNOWN",
            validation=_context_text(selected.get("validation"), 4000) or "UNKNOWN",
            blockers=_context_text(selected.get("blockers"), 4000) or "UNKNOWN",
            current_state=_context_text(selected.get("state"), 4000) or _context_text(selected.get("status"), 4000) or "UNKNOWN",
            ready_for_continuation=task.status == "ACTIVE",
            provenance=provenance,
        )

    def read_exact_turn_result(self, task_ref: str, turn_id: str) -> str:
        """Recover only one exact turn's assistant result, without dispatch."""
        task = self.resolve_task(task_ref=task_ref)
        if task.turn_id != turn_id:
            raise ContextReadError(
                f"Turn {turn_id} is not the current exact turn for task {task.task_id}"
            )
        binding = self.registry.get_binding(task.task_id)
        if binding is None:
            raise ContextReadError(f"Task {task.task_id} has no conversation binding")
        target = self._task_target(task, binding)
        try:
            client = self.client_factory(target)
            with client:
                initialize_info = client.initialize(
                    client_name=self.cfg.app_server.client_name,
                    client_title=self.cfg.app_server.client_title,
                    client_version=self.cfg.app_server.client_version,
                )
                selected = self._read_native(
                    client, task, binding, target, initialize_info,
                    recent_turns=1,
                    max_bytes=self.DEFAULT_MAX_BYTES,
                    anchor_turn_id=turn_id,
                )
        except AppServerError as exc:
            raise BoundedHistoryUnavailable(str(exc)) from exc
        result = _context_text(selected.get("assistant"), self.DEFAULT_MAX_BYTES)
        if not result or result == "UNKNOWN":
            raise ContextReadError(f"Exact turn {turn_id} has no assistant result")
        return result


@dataclasses.dataclass(frozen=True)
class TopicWorkItem:
    """One bounded task or unadopted conversation contributing to a topic."""

    title: str
    source_kind: str
    registry_status: str
    conversation_status: str
    last_activity: str
    last_user_intent: str
    last_codex_result: str
    current_state: str
    blockers: str
    validation: str
    context_source: str
    context_range: str
    context_truncated: bool
    task_ref: str | None = None
    thread_id: str | None = None
    provenance: dict[str, tuple[str, ...]] = dataclasses.field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "source_kind": self.source_kind,
            "registry_status": self.registry_status,
            "conversation_status": self.conversation_status,
            "last_activity": self.last_activity,
            "last_user_intent": self.last_user_intent,
            "last_codex_result": self.last_codex_result,
            "current_state": self.current_state,
            "blockers": self.blockers,
            "validation": self.validation,
            "context_source": self.context_source,
            "context_range": self.context_range,
            "context_truncated": self.context_truncated,
            "task_ref": self.task_ref,
            "thread_id": self.thread_id,
            "provenance": self.provenance,
        }


@dataclasses.dataclass(frozen=True)
class TopicStatus:
    host: str
    project: str
    topic: str
    summary_state: str
    completed_work: tuple[TopicWorkItem, ...]
    active_work: tuple[TopicWorkItem, ...]
    blocked_work: tuple[TopicWorkItem, ...]
    paused_work: tuple[TopicWorkItem, ...]
    superseded_work: tuple[TopicWorkItem, ...]
    unknown_work: tuple[TopicWorkItem, ...]
    latest_activity: str
    source_count: int
    task_count: int
    conversation_count: int
    context_coverage: str
    context_truncated: bool
    threads_screened: int
    topic_candidate_threads: int
    topic_matched_threads: int
    deduplication: str
    search_bounded: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic_status_read": "PASS",
            "host": self.host,
            "project": self.project,
            "topic": self.topic,
            "summary_state": self.summary_state,
            "completed_work": [item.as_dict() for item in self.completed_work],
            "active_work": [item.as_dict() for item in self.active_work],
            "blocked_work": [item.as_dict() for item in self.blocked_work],
            "paused_work": [item.as_dict() for item in self.paused_work],
            "superseded_work": [item.as_dict() for item in self.superseded_work],
            "unknown_work": [item.as_dict() for item in self.unknown_work],
            "latest_activity": self.latest_activity,
            "source_count": self.source_count,
            "task_count": self.task_count,
            "conversation_count": self.conversation_count,
            "context_coverage": self.context_coverage,
            "context_truncated": self.context_truncated,
            "threads_screened": self.threads_screened,
            "topic_candidate_threads": self.topic_candidate_threads,
            "topic_matched_threads": self.topic_matched_threads,
            "deduplication": self.deduplication,
            "search_bounded": self.search_bounded,
            "read_only": True,
        }


class TopicStatusReader:
    """Aggregate bounded authoritative context for one exact project topic."""

    INITIAL_TURNS = 4
    EXPANSION_TURNS = 8
    MAX_EXPANSION_PAGES = 8
    MAX_TOPIC_BYTES = TaskContextReader.DEFAULT_MAX_BYTES
    MAX_LIMIT = 100

    def __init__(
        self,
        cfg: BridgeConfig,
        registry: TaskRegistry,
        *,
        client_factory=None,
    ):
        self.cfg = cfg
        self.registry = registry
        self.client_factory = client_factory or (
            lambda target: _default_app_server_client(cfg, target)
        )
        self.dispatcher = TaskDispatcher(
            cfg, task_registry=registry, client_factory=self.client_factory
        )
        self.context_reader = TaskContextReader(
            cfg, registry, client_factory=self.client_factory
        )

    @staticmethod
    def _topic_tokens(topic: str) -> tuple[str, ...]:
        return tuple(
            token for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", topic.casefold())
            if token
        )

    @classmethod
    def _topic_match(cls, topic: str, text: str) -> bool:
        topic_text = " ".join(cls._topic_tokens(topic))
        haystack = " ".join(re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", text.casefold()))
        if not topic_text or not haystack:
            return False
        if topic_text in haystack:
            return True
        tokens = cls._topic_tokens(topic)
        return all(token in haystack.split() for token in tokens)

    @staticmethod
    def _activity(value: Any) -> str:
        if isinstance(value, str) and value:
            return value
        return ""

    @classmethod
    def _thread_activity(cls, thread: dict[str, Any]) -> str:
        for key in (
            "updatedAt", "updated_at", "lastActivityAt", "last_activity_at",
            "createdAt", "created_at",
        ):
            value = cls._activity(thread.get(key))
            if value:
                return value
        return ""

    @staticmethod
    def _status_text(value: Any) -> str:
        if isinstance(value, dict):
            value = value.get("type") or value.get("status")
        return _context_text(value, 4000).upper() if value else "UNKNOWN"

    @classmethod
    def _classify(cls, *, registry_status: str, conversation_status: str,
                  current_state: str, blockers: str, result: str) -> str:
        combined = " ".join(
            item for item in (current_state, blockers, result)
            if item
        ).casefold()
        if any(token in combined for token in ("superseded", "replaced", "obsolete")):
            return "SUPERSEDED"
        blocker_text = blockers.casefold()
        explicit_blocker = bool(blocker_text and not re.fullmatch(
            r"(?:none|no|unknown|nil|n/?a|clear|resolved|false|0)",
            blocker_text.strip(" .;,:"),
        ))
        if "blocked" in combined or explicit_blocker or "cannot continue" in combined:
            return "BLOCKED"
        if any(token in combined for token in ("paused", "on hold", "waiting")):
            return "PAUSED"
        if registry_status.upper() == "COMPLETED":
            return "COMPLETED"
        if any(token in combined for token in ("completed", "closed", "status=pass", "next_state=completed")):
            return "COMPLETED"
        if re.search(r"(?:^|[\s:=])pass(?:$|[\s;,.])", combined):
            return "COMPLETED"
        if registry_status.upper() == "ACTIVE" or current_state not in {"", "UNKNOWN"}:
            return "ACTIVE"
        return "UNKNOWN"

    @staticmethod
    def _context_range(turn_ids: tuple[str, ...]) -> str:
        return f"{turn_ids[-1]}..{turn_ids[0]}" if turn_ids else "bounded-tail"

    def _task_item(self, task: Any, *, recent_turns: int, max_bytes: int) -> TopicWorkItem:
        binding = self.registry.get_binding(task.task_id)
        if binding is None:
            state = "UNKNOWN"
            return TopicWorkItem(
                title=task.title,
                source_kind="TASK",
                registry_status=task.status,
                conversation_status="UNKNOWN",
                last_activity=task.updated_at,
                last_user_intent="UNKNOWN",
                last_codex_result="UNKNOWN",
                current_state=state,
                blockers=task.current_blocker or "UNKNOWN",
                validation="UNKNOWN",
                context_source="TASK_REGISTRY",
                context_range="none",
                context_truncated=False,
                task_ref=task.task_id,
            )
        try:
            context = self.context_reader.read_task_context(
                task.task_id,
                recent_turns=recent_turns,
                max_bytes=max_bytes,
                allow_not_ready=True,
            )
            conversation_status = context.current_state
            state = self._classify(
                registry_status=task.status,
                conversation_status=conversation_status,
                current_state=context.current_state,
                blockers=context.blockers,
                result=context.last_codex_result,
            )
            return TopicWorkItem(
                title=task.title,
                source_kind="TASK",
                registry_status=task.status,
                conversation_status=conversation_status,
                last_activity=task.updated_at,
                last_user_intent=context.last_user_intent,
                last_codex_result=context.last_codex_result,
                current_state=context.current_state,
                blockers=context.blockers,
                validation=context.validation,
                context_source=context.context_source,
                context_range=context.context_range,
                context_truncated=context.context_truncated,
                task_ref=task.task_id,
                thread_id=binding.thread_id,
                provenance=context.provenance,
            )
        except (ContextReadError, AppServerError):
            return TopicWorkItem(
                title=task.title,
                source_kind="TASK",
                registry_status=task.status,
                conversation_status="UNKNOWN",
                last_activity=task.updated_at,
                last_user_intent="UNKNOWN",
                last_codex_result="UNKNOWN",
                current_state="UNKNOWN",
                blockers=task.current_blocker or "UNKNOWN",
                validation="UNKNOWN",
                context_source="TASK_REGISTRY",
                context_range="none",
                context_truncated=False,
                task_ref=task.task_id,
                thread_id=binding.thread_id,
            )

    def _historical_item(
        self,
        thread: dict[str, Any],
        turn_texts: dict[str, str],
        *,
        page_truncated: bool,
        role_texts: dict[str, tuple[list[str], list[str], list[str]]] | None = None,
    ) -> TopicWorkItem:
        ordered = list(turn_texts.items())
        all_text = [text for _turn_id, text in ordered]
        users: list[str] = []
        assistants: list[str] = []
        item_ids: list[str] = []
        for turn_id, text in ordered:
            if role_texts and turn_id in role_texts:
                turn_users, turn_assistants, turn_items = role_texts[turn_id]
                users.extend(turn_users)
                assistants.extend(turn_assistants)
                item_ids.extend(turn_items)
                continue
            # Expanded pages currently expose normalized text only.  Honor
            # explicit role prefixes if present, but do not infer a role from
            # arbitrary prose.
            if text.casefold().startswith(("user:", "human:", "request:")):
                users.append(text.split(":", 1)[1].strip())
            elif text.casefold().startswith(("assistant:", "codex:", "result:")):
                assistants.append(text.split(":", 1)[1].strip())
        if not users and all_text:
            users = [all_text[0]]
        if not assistants and all_text:
            assistants = [all_text[-1]]
        changed, validation, blockers, state = _context_extract_fields(all_text)
        status = self._status_text(thread.get("status"))
        classified = self._classify(
            registry_status="UNKNOWN",
            conversation_status=status,
            current_state=state,
            blockers=blockers,
            result=assistants[0] if assistants else "UNKNOWN",
        )
        turn_ids = tuple(turn_id for turn_id, _text in ordered)
        return TopicWorkItem(
            title="Historical Codex conversation",
            source_kind="HISTORICAL_CONVERSATION",
            registry_status="UNADOPTED",
            conversation_status=status,
            last_activity=self._thread_activity(thread),
            last_user_intent=_latest_bounded_text(users, 4000),
            last_codex_result=_latest_bounded_text(assistants, 4000),
            current_state=state,
            blockers=blockers,
            validation=validation,
            context_source="APP_SERVER_NATIVE",
            context_range=self._context_range(turn_ids),
            context_truncated=bool(page_truncated),
            thread_id=str(thread.get("id")),
            provenance={
                "turn_ids": turn_ids,
                "item_ids": tuple(item_ids),
                "message_ids": tuple(item_ids),
            },
        )

    def _read_historical_candidate(
        self,
        client: Any,
        thread: dict[str, Any],
        first_page: dict[str, Any],
    ) -> TopicWorkItem:
        texts = HistoricalConversationDiscovery(self.cfg, self.registry,
                                                client_factory=self.client_factory)
        rows = [row for row in first_page.get("data", ()) if isinstance(row, dict)]
        turn_texts = texts._turn_summary_texts(rows)
        role_texts: dict[str, tuple[list[str], list[str], list[str]]] = {}
        for row in rows:
            turn_id = texts._row_id(row)
            if turn_id is None:
                continue
            users, assistants, _all, ids, _roles = self.context_reader._collect_item_text(
                row.get("items", row)
            )
            role_texts[turn_id] = (users, assistants, ids)
        expanded = texts._expand_candidate(client, str(thread["id"]), first_page)
        page_truncated = bool(first_page.get("nextCursor"))
        return self._historical_item(thread, expanded or turn_texts,
                                     page_truncated=page_truncated,
                                     role_texts=role_texts)

    def _apply_supersession(self, items: list[TopicWorkItem]) -> list[TopicWorkItem]:
        """Only mark explicit later replacement evidence as superseded."""
        result = list(items)
        for index, older in enumerate(result):
            if older.conversation_status not in {"BLOCKED", "PAUSED"} and older.current_state not in {"BLOCKED", "PAUSED"}:
                continue
            older_tokens = set(self._topic_tokens(older.title))
            for newer in result:
                if newer is older or newer.last_activity <= older.last_activity:
                    continue
                text = " ".join((newer.last_codex_result, newer.current_state)).casefold()
                if not any(word in text for word in ("superseded", "replaced", "resolved", "closed")):
                    continue
                if older_tokens and not older_tokens.intersection(self._topic_tokens(newer.title)):
                    continue
                result[index] = dataclasses.replace(older, conversation_status="SUPERSEDED")
                break
        return result

    def read_topic_status(
        self,
        *,
        host: str | None,
        project_ref: str,
        topic: str,
        include_completed: bool = True,
        include_historical: bool = True,
        limit: int = 20,
        recent_turns: int = INITIAL_TURNS,
        max_bytes: int = MAX_TOPIC_BYTES,
    ) -> TopicStatus:
        if not isinstance(topic, str) or not topic.strip():
            raise ContextReadError("topic is required")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= self.MAX_LIMIT:
            raise ContextReadError("limit must be an integer from 1 to 100")
        if not 1 <= recent_turns <= TaskContextReader.MAX_RECENT_TURNS:
            raise ContextReadError("recent_turns is outside the bounded range")
        if not 1024 <= max_bytes <= TaskContextReader.MAX_CONTEXT_BYTES:
            raise ContextReadError("max_bytes is outside the bounded range")
        workspace, _descriptor, project = self.dispatcher.resolve_project(
            project_ref, host=host, project_mode="existing"
        )
        resolved_host = canonical_host(workspace.host or workspace.alias)
        evidence = _local_git_identity(str(project.repo))
        project_identity_guard(project, evidence)
        tasks = self.registry.list_tasks(
            host=resolved_host, project=project.project_alias, include_archived=True
        )
        items: list[TopicWorkItem] = []
        for task in tasks:
            if not include_completed and task.status == "COMPLETED":
                continue
            metadata = " ".join((task.title, task.summary or "", task.task_key or ""))
            if self._topic_match(topic, metadata):
                items.append(self._task_item(task, recent_turns=recent_turns, max_bytes=max_bytes))

        bound_threads = {
            binding.thread_id
            for task in tasks
            if (binding := self.registry.get_binding(task.task_id)) is not None
        }
        screened = candidates = matched = 0
        if include_historical:
            target = _read_only_transport_target(self.cfg, project)
            client = self.client_factory(target)
            with client:
                initialize_info = client.initialize(
                    client_name=self.cfg.app_server.client_name,
                    client_title=self.cfg.app_server.client_title,
                    client_version=self.cfg.app_server.client_version,
                )
                metadata_rows = _enumerate_threads(client)
                for row in metadata_rows:
                    thread_id = row.get("id")
                    if not isinstance(thread_id, str) or not thread_id or thread_id in bound_threads:
                        continue
                    thread = client.thread_read(thread_id)
                    if thread.get("cwd") != str(project.repo):
                        continue
                    try:
                        thread_evidence = _repository_identity_evidence(thread)
                        target_for_thread = dataclasses.replace(
                            target,
                            thread_id=thread_id,
                            session_id=str(thread.get("sessionId") or ""),
                            project_id=thread.get("projectId"),
                        )
                        HistoricalConversationDiscovery._historical_guard(
                            target_for_thread, thread,
                            initialize_info=initialize_info,
                            repository_evidence=thread_evidence,
                        )
                    except (IdentityGuardError, AppServerError):
                        continue
                    screened += 1
                    page = client.thread_turns_list(
                        thread_id, limit=recent_turns,
                        sort_direction="desc", items_view="summary"
                    )
                    summary_text = "\n".join(
                        HistoricalConversationDiscovery(self.cfg, self.registry,
                                                        client_factory=self.client_factory)
                        ._turn_summary_texts([
                            entry for entry in page.get("data", ()) if isinstance(entry, dict)
                        ]).values()
                    )
                    if not self._topic_match(topic, summary_text):
                        continue
                    candidates += 1
                    item = self._read_historical_candidate(client, thread, page)
                    matched += 1
                    items.append(item)

        items = self._apply_supersession(items)
        # A topic query is a bounded discovery surface.  Keep newest records
        # while preserving deterministic source order for equal activity.
        items.sort(key=lambda item: (item.last_activity, item.title), reverse=True)
        items = items[:limit]
        completed = tuple(item for item in items if self._classify(
            registry_status=item.registry_status,
            conversation_status=item.conversation_status,
            current_state=item.current_state,
            blockers=item.blockers,
            result=item.last_codex_result,
        ) == "COMPLETED")
        active = tuple(item for item in items if self._classify(
            registry_status=item.registry_status,
            conversation_status=item.conversation_status,
            current_state=item.current_state,
            blockers=item.blockers,
            result=item.last_codex_result,
        ) == "ACTIVE")
        blocked = tuple(item for item in items if self._classify(
            registry_status=item.registry_status,
            conversation_status=item.conversation_status,
            current_state=item.current_state,
            blockers=item.blockers,
            result=item.last_codex_result,
        ) == "BLOCKED")
        paused = tuple(item for item in items if self._classify(
            registry_status=item.registry_status,
            conversation_status=item.conversation_status,
            current_state=item.current_state,
            blockers=item.blockers,
            result=item.last_codex_result,
        ) == "PAUSED")
        superseded = tuple(item for item in items if item.conversation_status == "SUPERSEDED")
        unknown = tuple(item for item in items if item not in completed + active + blocked + paused + superseded)
        contextual = sum(item.context_source not in {"TASK_REGISTRY", "UNKNOWN"} for item in items)
        coverage = f"{contextual}/{len(items)}" if items else "0/0"
        states = {"COMPLETED": completed, "ACTIVE": active, "BLOCKED": blocked,
                  "PAUSED": paused, "SUPERSEDED": superseded, "UNKNOWN": unknown}
        summary_state = next((name for name in ("BLOCKED", "ACTIVE", "PAUSED", "UNKNOWN", "COMPLETED") if states[name]), "UNKNOWN")
        latest = max((item.last_activity for item in items if item.last_activity), default="")
        return TopicStatus(
            host=resolved_host,
            project=project.project_alias,
            topic=topic,
            summary_state=summary_state,
            completed_work=completed,
            active_work=active,
            blocked_work=blocked,
            paused_work=paused,
            superseded_work=superseded,
            unknown_work=unknown,
            latest_activity=latest,
            source_count=len(items),
            task_count=sum(item.source_kind == "TASK" for item in items),
            conversation_count=sum(item.source_kind == "HISTORICAL_CONVERSATION" for item in items),
            context_coverage=coverage,
            context_truncated=any(item.context_truncated for item in items),
            threads_screened=screened,
            topic_candidate_threads=candidates,
            topic_matched_threads=matched,
            deduplication="ADOPTED_THREADS_EXCLUDED",
            search_bounded=True,
        )


class Dispatcher:
    """Dispatch prompts using explicit project and thread registries."""

    def __init__(
        self,
        cfg: BridgeConfig,
        client_factory=None,
        project_identity_reader=None,
    ):
        self.cfg = cfg
        self.registry = TargetRegistry(cfg.targets)
        self.project_registry = ProjectRegistry(cfg.projects)
        self.thread_registry = ThreadRegistry(cfg.threads)
        self.client_factory = client_factory or (
            lambda target: _default_app_server_client(cfg, target)
        )
        self.project_identity_reader = project_identity_reader or _local_git_identity

    def _validate_project(self, project: ProjectMapping) -> RepositoryIdentityEvidence:
        try:
            evidence = self.project_identity_reader(str(project.repo))
        except IdentityGuardError:
            raise
        except Exception as exc:
            raise IdentityGuardError(
                "DISPATCH_IDENTITY_GUARD=FAIL\n"
                f"- project Git identity lookup failed for {project.project_alias!r}"
            ) from exc
        project_identity_guard(project, evidence)
        return evidence

    def _dispatch_binding(
        self,
        project: ProjectMapping,
        binding: ThreadBinding,
        prompt: str,
        model: str | None,
        reasoning_effort: str | None,
        *,
        target: TargetConfig | None = None,
        repository_identity_source: str = "app_server",
    ) -> DispatchResult:
        target = target or _target_for_binding(project, binding)
        client = self.client_factory(target)
        try:
            with client:
                initialize_info = client.initialize(
                    client_name=self.cfg.app_server.client_name,
                    client_title=self.cfg.app_server.client_title,
                    client_version=self.cfg.app_server.client_version,
                )
                executable_model, executable_reasoning = _resolve_dispatch_model(
                    client, model, reasoning_effort
                )
                thread = client.thread_read(target.thread_id)
                identity_guard(
                    target,
                    thread,
                    initialize_info=initialize_info,
                    allow_unloaded=True,
                )
                needs_resume = (
                    thread.get("canAcceptDirectInput") is None
                    and _status_type(thread) in {"notLoaded", "unloaded"}
                )
                if needs_resume:
                    client.thread_resume(target.thread_id)
                    thread = client.thread_read(target.thread_id)
                repository_evidence = _repository_identity_evidence(thread)
                identity_guard(
                    target,
                    thread,
                    initialize_info=initialize_info,
                    repository_evidence=repository_evidence,
                )
                turn_start_guard(thread)
                turn = client.turn_start(
                    target.thread_id,
                    prompt,
                    cwd=str(project.repo),
                    model=executable_model,
                    reasoning_effort=executable_reasoning,
                    approval_policy=self.cfg.approval,
                )
                return DispatchResult(
                    target_alias=binding.qualified_alias,
                    thread_id=target.thread_id,
                    turn_id=turn.turn_id,
                    model=turn.model or model,
                    reasoning_effort=turn.reasoning_effort or reasoning_effort,
                    dispatch_status="DISPATCHED",
                    repository_identity_source=repository_identity_source
                    if repository_identity_source != "app_server"
                    else repository_evidence.source,
                    project_alias=project.project_alias,
                    thread_alias=binding.alias,
                    session_id=target.session_id,
                    cwd=str(project.repo),
                )
        except (IdentityGuardError, AppServerError):
            raise

    def dispatch_project(
        self,
        project_alias: str,
        thread_mode: str,
        prompt: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        thread_alias: str | None = None,
    ) -> DispatchResult:
        if thread_mode not in {"existing", "new"}:
            raise DispatchContractError(f"Unsupported thread mode: {thread_mode!r}")
        project = self.project_registry.resolve(project_alias)

        if thread_mode == "existing":
            if not thread_alias:
                raise DispatchContractError(
                    "THREAD_MODE=existing requires THREAD_ALIAS"
                )
            binding = self.thread_registry.resolve(project_alias, thread_alias)
            self._validate_project(project)
            return self._dispatch_binding(project, binding, prompt, model, reasoning_effort)

        # New threads have no configured identity.  The exact identity is
        # captured from thread/start and then guarded by a mandatory readback.
        self._validate_project(project)
        transport_binding = next(
            (item for item in self.cfg.threads if item.project_alias == project_alias),
            None,
        )
        transport_target = (
            _target_for_binding(project, transport_binding)
            if transport_binding is not None
            else TargetConfig(
                alias=f"{project_alias}.new",
                ssh_alias=self.cfg.app_server.ssh_alias or "",
                thread_id="",
                session_id="",
                project_id=None,
                cwd=str(project.repo),
                repository_origin=project.repository_origin,
                branch=project.branch or "",
                app_server_version=self.cfg.app_server.client_version,
                target_host=_project_target_host(self.cfg, project),
            )
        )
        client = self.client_factory(
            transport_target
        )
        try:
            with client:
                initialize_info = client.initialize(
                    client_name=self.cfg.app_server.client_name,
                    client_title=self.cfg.app_server.client_title,
                    client_version=self.cfg.app_server.client_version,
                )
                executable_model, executable_reasoning = _resolve_dispatch_model(
                    client, model, reasoning_effort
                )
                started = client.thread_start(
                    cwd=str(project.repo),
                    model=executable_model,
                    sandbox=self.cfg.sandbox,
                    ephemeral=False,
                )
                new_thread_id = started.get("id")
                new_session_id = started.get("sessionId")
                if not isinstance(new_thread_id, str) or not new_thread_id:
                    raise IdentityGuardError(
                        "DISPATCH_IDENTITY_GUARD=FAIL\n- thread/start returned no exact thread id"
                    )
                if not isinstance(new_session_id, str) or not new_session_id:
                    raise IdentityGuardError(
                        "DISPATCH_IDENTITY_GUARD=FAIL\n- thread/start returned no exact session id"
                    )
                if started.get("ephemeral") is True:
                    raise IdentityGuardError(
                        "DISPATCH_IDENTITY_GUARD=FAIL\n- thread/start returned ephemeral=true"
                    )
                actual_project_id = started.get("projectId")
                if actual_project_id is not None and not isinstance(actual_project_id, str):
                    raise IdentityGuardError(
                        "DISPATCH_IDENTITY_GUARD=FAIL\n- thread/start returned malformed projectId"
                    )
                binding = ThreadBinding(
                    alias="new",
                    project_alias=project_alias,
                    ssh_alias=transport_target.ssh_alias,
                    thread_id=new_thread_id,
                    session_id=new_session_id,
                    project_id=actual_project_id,
                    app_server_version=(
                        getattr(initialize_info, "server_version", None)
                        or getattr(initialize_info, "user_agent", None)
                        or transport_target.app_server_version
                    ),
                    target_host=transport_target.target_host,
                )
                target = _target_for_binding(project, binding)
                thread = client.thread_read(new_thread_id)
                identity_guard(target, thread, initialize_info=initialize_info)
                repository_evidence = _repository_identity_evidence(thread)
                identity_guard(
                    target,
                    thread,
                    initialize_info=initialize_info,
                    repository_evidence=repository_evidence,
                )
                turn_start_guard(thread)
                turn = client.turn_start(
                    new_thread_id,
                    prompt,
                    cwd=str(project.repo),
                    model=executable_model,
                    reasoning_effort=executable_reasoning,
                    approval_policy=self.cfg.approval,
                )
                return DispatchResult(
                    target_alias=binding.qualified_alias,
                    thread_id=new_thread_id,
                    turn_id=turn.turn_id,
                    model=turn.model or model,
                    reasoning_effort=turn.reasoning_effort or reasoning_effort,
                    dispatch_status="DISPATCHED",
                    repository_identity_source=repository_evidence.source,
                    thread_created=True,
                    thread_durable=True,
                    project_alias=project_alias,
                    thread_alias="new",
                    session_id=new_session_id,
                    cwd=str(project.repo),
                )
        except (IdentityGuardError, AppServerError):
            raise

    def dispatch(
        self,
        target_alias: str,
        prompt: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> DispatchResult:
        """Legacy target entry point; V1 callers should use dispatch_project."""
        target = self.registry.resolve(target_alias)
        project = next(
            (item for item in self.cfg.projects if item.target_alias == target_alias),
            ProjectMapping(
                linear_name=target_alias,
                repo=Path(target.cwd),
                target_alias=target_alias,
            ),
        )
        binding = ThreadBinding(
            alias="current",
            project_alias=project.project_alias,
            ssh_alias=target.ssh_alias,
            thread_id=target.thread_id,
            session_id=target.session_id,
            project_id=target.project_id,
            app_server_version=target.app_server_version,
            target_host=target.target_host,
        )
        client = self.client_factory(target)
        try:
            with client:
                initialize_info = client.initialize(
                    client_name=self.cfg.app_server.client_name,
                    client_title=self.cfg.app_server.client_title,
                    client_version=self.cfg.app_server.client_version,
                )
                executable_model, executable_reasoning = _resolve_dispatch_model(
                    client, model, reasoning_effort
                )
                thread = client.thread_read(target.thread_id)

                # Thread identity is checked before any repository command.
                identity_guard(
                    target,
                    thread,
                    initialize_info=initialize_info,
                    allow_unloaded=True,
                )

                # A durable thread may be known but not loaded.  Validate all
                # static identity fields before loading it, then read again so
                # canAcceptDirectInput is checked on the live thread.
                needs_resume = (
                    thread.get("canAcceptDirectInput") is None
                    and _status_type(thread) in {"notLoaded", "unloaded"}
                )
                if needs_resume:
                    identity_guard(
                        target,
                        thread,
                        initialize_info=initialize_info,
                        allow_unloaded=True,
                    )
                    client.thread_resume(target.thread_id)
                    thread = client.thread_read(target.thread_id)

                repository_evidence = _repository_identity_evidence(thread)
                identity_guard(
                    target,
                    thread,
                    initialize_info=initialize_info,
                    repository_evidence=repository_evidence,
                )
                turn_start_guard(thread)
                turn = client.turn_start(
                    target.thread_id,
                    prompt,
                    cwd=target.cwd,
                    model=executable_model,
                    reasoning_effort=executable_reasoning,
                    approval_policy=self.cfg.approval,
                )
                return DispatchResult(
                    target_alias=target.alias,
                    thread_id=target.thread_id,
                    turn_id=turn.turn_id,
                    model=turn.model or model,
                    reasoning_effort=turn.reasoning_effort or reasoning_effort,
                    dispatch_status="DISPATCHED",
                    repository_identity_source=repository_evidence.source,
                )
        except IdentityGuardError:
            raise
        except AppServerError:
            raise


@dataclasses.dataclass(frozen=True)
class CodexRunResult:
    returncode: int
    run_dir: Path
    stdout_log: Path
    stderr_log: Path
    final_message: Path


def run_codex(
    cfg: BridgeConfig,
    issue: dict[str, Any],
    repo: Path,
) -> CodexRunResult:
    """Legacy compatibility runner; Dispatcher V1 never calls this function."""
    identifier = issue["identifier"]
    stamp = time.strftime("%Y%m%dT%H%M%S")
    run_dir = cfg.log_dir / "runs" / f"{identifier}-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    stdout_log = run_dir / "events.jsonl"
    stderr_log = run_dir / "stderr.log"
    final_message = run_dir / "final-message.txt"

    cmd = [
        cfg.codex_binary,
        "--ask-for-approval",
        cfg.approval,
        "exec",
        "--sandbox",
        cfg.sandbox,
        "--json",
        "-C",
        str(repo),
        "-o",
        str(final_message),
        codex_prompt(issue, repo, cfg.review_state),
    ]

    with stdout_log.open("w", encoding="utf-8") as out, stderr_log.open(
        "w", encoding="utf-8"
    ) as err:
        proc = subprocess.run(cmd, stdout=out, stderr=err, text=True)

    return CodexRunResult(
        returncode=proc.returncode,
        run_dir=run_dir,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        final_message=final_message,
    )


class Bridge:
    def __init__(self, cfg: BridgeConfig, linear: LinearClient, dispatcher=None):
        self.cfg = cfg
        self.linear = linear
        self.states: dict[str, str] = {}
        self.dispatcher = dispatcher or Dispatcher(cfg)
        self.task_dispatcher: TaskDispatcher | None = None
        self.task_index: LinearTaskIndex | None = None
        self.execution_results: ExecutionResultService | None = None

    def _m5_dispatcher(self) -> TaskDispatcher:
        if self.task_dispatcher is None:
            self.task_dispatcher = TaskDispatcher(self.cfg)
        return self.task_dispatcher

    def _task_index(self) -> LinearTaskIndex:
        if self.task_index is None:
            self.task_index = LinearTaskIndex(
                self.linear,
                self._m5_dispatcher().tasks,
                self.cfg.team_id,
            )
        return self.task_index

    def _execution_result_service(self) -> ExecutionResultService:
        if self.execution_results is None:
            self.execution_results = ExecutionResultService(
                self._m5_dispatcher().tasks, self.linear
            )
        return self.execution_results

    def receive_execution_result(
        self,
        *,
        execution_ref: str,
        task_id: str,
        turn_id: str,
        raw_result: str,
        issue_id: str,
    ) -> Any:
        """Persist and write back one result; retries never dispatch a turn."""
        return self._execution_result_service().receive_and_writeback(
            execution_ref=execution_ref,
            task_id=task_id,
            turn_id=turn_id,
            raw_result=raw_result,
            issue_id=issue_id,
            review_state_id=self.states.get(self.cfg.review_state),
            blocked_state_id=self.states.get(self.cfg.todo_state),
        )

    def _m5_repo(self, contract: DispatchContract) -> Path:
        dispatcher = self._m5_dispatcher()
        if contract.task_mode == "continue":
            if not contract.task_id:
                raise DispatchContractError("TASK_MODE=continue requires TASK_ID")
            task = dispatcher.tasks.get_task(contract.task_id)
            workspace = dispatcher.workspaces.resolve(task.workspace_alias)
            return dispatcher.workspaces.validate_path(workspace, Path(task.cwd))
        _workspace, _descriptor, project = dispatcher.resolve_project(
            contract.project_alias or "",
            host=contract.host,
            project_mode=contract.project_mode or "existing",
        )
        return project.repo

    def initialize(self) -> None:
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        self.states = self.linear.team_states(self.cfg.team_id)
        required = [
            self.cfg.todo_state,
            self.cfg.running_state,
            self.cfg.review_state,
        ]
        missing = [name for name in required if name not in self.states]
        if missing:
            raise BridgeError(
                f"Configured Linear states not found on team: {', '.join(missing)}"
            )

    def poll_once(self) -> int:
        issues = self.linear.eligible_issues(
            team_id=self.cfg.team_id,
            state_name=self.cfg.todo_state,
            label_name=self.cfg.trigger_label,
            first=self.cfg.max_batch,
        )
        if not issues:
            print("No eligible issues.")
            return 0

        handled = 0
        for issue in issues:
            project_name = (issue.get("project") or {}).get("name")
            contract = None
            try:
                contract = parse_dispatch_contract(issue.get("description"))
            except DispatchContractError:
                # Let execute_issue produce the authoritative contract failure
                # comment.  A M5-shaped malformed contract must not be hidden
                # by the legacy static repo preflight.
                contract = None
            is_task_shape = any(
                re.search(rf"(?m)^\s*{key}=", issue.get("description") or "")
                for key in (
                    "TASK_MODE", "PROJECT_MODE", "EXECUTION_MODE", "TASK_ACTION", "TASK_REF"
                )
            )
            if contract and contract.contract_kind in {"m5", "m6"}:
                try:
                    repo = self._m5_repo(contract)
                except Exception as exc:
                    self._record_bridge_failure(
                        issue,
                        Path("."),
                        str(exc),
                        prefix="DISPATCH_PROJECT_RESOLUTION_FAILED",
                    )
                    continue
            elif is_task_shape:
                self._record_bridge_failure(
                    issue,
                    Path("."),
                    "Malformed task dispatch contract",
                    prefix="DISPATCH_CONTRACT_FAILED",
                )
                continue
            else:
                repo = self.cfg.repo_for_project(project_name)
            if repo is None:
                print(
                    f"Skip {issue['identifier']}: no repo mapping for Linear project "
                    f"{project_name!r}"
                )
                continue

            if not contract or contract.contract_kind not in {"m5", "m6"}:
                repo_valid = repo.is_dir() and is_git_repo(repo)
            else:
                # Explicit PROJECT_MODE=create is allowed to create the
                # workspace-relative project during execute_issue.
                repo_valid = (
                    contract.project_mode == "create"
                    or (repo.is_dir() and is_git_repo(repo))
                )
            if not repo_valid:
                body = (
                    "BRIDGE_CLAIM_FAILED\n\n"
                    f"Reason: configured repository is missing or not a Git repository.\n"
                    f"Repository: `{repo}`\n"
                    f"Bridge: `linear-local-codex-bridge/{BRIDGE_VERSION}`\n\n"
                    "Issue remains Todo."
                )
                try:
                    self.linear.add_comment(issue["id"], body)
                except Exception as e:
                    print(f"Warning: failed to comment on {issue['identifier']}: {e}")
                print(f"Skip {issue['identifier']}: invalid repo {repo}")
                continue

            self.execute_issue(issue, repo)
            handled += 1
        return handled

    def _execute_m5_issue(
        self,
        issue: dict[str, Any],
        repo: Path,
        contract: DispatchContract,
    ) -> None:
        """Claim and dispatch one explicit M5 task contract."""
        identifier = issue["identifier"]
        running_state_id = self.states[self.cfg.running_state]
        if contract.task_action and not contract.task_id:
            self._record_bridge_failure(
                issue,
                repo,
                "TASK_ACTION requires TASK_ID",
                prefix="TASK_ACTION_FAILED",
            )
            return

        claimed = self.linear.update_issue_state(issue["id"], running_state_id)
        if claimed["state"]["name"] != self.cfg.running_state:
            raise BridgeError(
                f"Failed to claim {identifier}: state is {claimed['state']['name']}"
            )

        try:
            self.linear.add_comment(
                issue["id"],
                "CLINX_EXECUTION_STATUS\n\n"
                "EXECUTION_STATE=DISPATCHING\n"
                "CODEX_RUNNING=NO\n"
                "CURRENT_STAGE=project identity guard\n"
                f"LAST_PROGRESS_AT={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
            )
        except Exception as exc:
            print(f"Warning: failed to write execution status for {identifier}: {exc}")
        try:
            self.linear.add_comment(
                issue["id"],
                "M5_TASK_CLAIMED\n\n"
                f"- Issue: `{identifier}`\n"
                f"- Project: `{contract.project_alias}`\n"
                f"- Task mode: `{contract.task_mode}`\n"
                f"- Execution mode: `{contract.execution_mode}`\n"
                "- Legacy codex exec: `NO`",
            )
        except Exception as exc:
            print(f"Warning: failed to write M5 claim for {identifier}: {exc}")

        dispatcher = self._m5_dispatcher()
        try:
            if contract.task_action:
                status = dispatcher.task_action(contract.task_id or "", contract.task_action)
                if dispatcher.tasks.get_task_index(contract.task_id or "") is not None:
                    self._task_index().sync(contract.task_id or "")
                body = (
                    "M5_TASK_ACTION_APPLIED\n\n"
                    f"TASK_ID={contract.task_id}\n"
                    f"TASK_ACTION={contract.task_action}\n"
                    f"TASK_STATUS={status}\n"
                    "CONVERSATION_BINDING_PRESERVED=YES"
                )
                self.linear.add_comment(issue["id"], body)
                return

            result = dispatcher.dispatch(
                project_ref=contract.project_alias or "",
                host=contract.host,
                project_mode=contract.project_mode or "existing",
                task_mode=contract.task_mode or "new",
                task_id=contract.task_id,
                prompt=codex_prompt(issue, repo, self.cfg.review_state),
                title=str(issue.get("title") or identifier),
                summary=None,
                task_key=None,
                model=contract.model,
                reasoning_effort=contract.reasoning_effort,
                execution_mode=contract.execution_mode,
                issue_id=issue.get("id"),
                execution_ref=identifier,
            )
        except Exception as exc:
            self._record_bridge_failure(issue, repo, f"Failed to dispatch M5 task: {exc}")
            return

        body = (
            "M5_TASK_DISPATCHED\n\n"
            f"TASK_ID={result.task_id}\n"
            f"THREAD_ID={result.thread_id}\n"
            f"SESSION_ID={result.session_id}\n"
            f"TURN_ID={result.turn_id}\n"
            f"PROJECT={result.project_alias}\n"
            f"MODEL={result.model or 'server default'}\n"
            f"REASONING={result.reasoning_effort or 'server default'}\n"
            f"EXECUTION_MODE={result.execution_mode}\n"
            "TASK_EXECUTION_SERIALIZATION=PASS\n"
            "EXACT_THREAD_DISPATCH=PASS\n"
            "LEGACY_CODEX_EXEC_DEFAULT=NO"
        )
        try:
            self.linear.add_comment(issue["id"], body)
        except Exception as exc:
            print(f"Warning: failed to write M5 dispatch for {identifier}: {exc}")
        try:
            log_dir = write_dispatch_record(self.cfg, issue, result)
        except Exception as exc:
            log_dir = None
            print(f"Warning: failed to write M5 dispatch log for {identifier}: {exc}")
        print(
            f"M5 dispatched {identifier}: task={result.task_id} "
            f"thread={result.thread_id} turn={result.turn_id}"
            + (f" logs={log_dir}" if log_dir else "")
        )

    def _execute_m6_issue(
        self,
        issue: dict[str, Any],
        repo: Path,
        contract: DispatchContract,
    ) -> None:
        """Execute one ChatGPT-resolved M6 task handoff."""
        identifier = issue["identifier"]
        running_state_id = self.states[self.cfg.running_state]
        claimed = self.linear.update_issue_state(issue["id"], running_state_id)
        if claimed["state"]["name"] != self.cfg.running_state:
            raise BridgeError(
                f"Failed to claim {identifier}: state is {claimed['state']['name']}"
            )

        dispatcher = self._m5_dispatcher()
        task_ref = contract.task_ref
        try:
            if contract.task_action == "reopen":
                dispatcher.task_action(task_ref or "", "reopen")
            result = dispatcher.dispatch(
                project_ref=contract.project_alias or "",
                host=contract.host,
                project_mode=contract.project_mode or "existing",
                task_mode="new" if contract.task_action == "create" else "continue",
                task_id=task_ref,
                prompt=codex_prompt(issue, repo, self.cfg.review_state),
                title=contract.task_title or str(issue.get("title") or identifier),
                summary=contract.task_summary_update,
                task_key=contract.task_key,
                update_title=contract.task_title is not None,
                model=contract.model,
                reasoning_effort=contract.reasoning_effort,
                execution_mode=contract.execution_mode,
                issue_id=issue.get("id"),
                execution_ref=identifier,
            )
            index = self._task_index().sync(
                result.task_id or "",
                project_id=(issue.get("project") or {}).get("id"),
            )
            task = dispatcher.tasks.get_task(result.task_id or "")
        except Exception as exc:
            self._record_bridge_failure(
                issue,
                repo,
                f"Failed to execute M6 task handoff: {exc}",
                prefix="M6_TASK_HANDOFF_FAILED",
                pre_turn_failure=True,
            )
            return

        body = (
            "M6_TASK_DISPATCHED\n\n"
            f"TASK_REF={result.task_id}\n"
            f"TASK_STATUS={task.status}\n"
            f"PROJECT={task.project_alias}\n"
            f"HOST={task.host}\n"
            f"TASK_TITLE={task.title}\n"
            f"TASK_INDEX_ISSUE={index.identifier}\n"
            f"MODEL={result.model or 'server default'}\n"
            f"REASONING={result.reasoning_effort or 'server default'}\n"
            f"EXECUTION_MODE={result.execution_mode}\n"
            "EXACT_BOUND_CONVERSATION=PASS\n"
            "HUMAN_THREAD_ID_REQUIRED=NO\n"
            "HUMAN_SESSION_ID_REQUIRED=NO\n"
            "LEGACY_CODEX_EXEC_DEFAULT=NO\n"
            "EXECUTION_STATE=CODEX_RUNNING\n"
            "CODEX_RUNNING=YES\n"
            f"TURN_ID={result.turn_id}"
        )
        try:
            self.linear.add_comment(issue["id"], body)
        except Exception as exc:
            print(f"Warning: failed to write M6 dispatch for {identifier}: {exc}")
        try:
            log_dir = write_dispatch_record(self.cfg, issue, result)
        except Exception as exc:
            log_dir = None
            print(f"Warning: failed to write M6 dispatch log for {identifier}: {exc}")
        print(
            f"M6 dispatched {identifier}: task={result.task_id} turn={result.turn_id}"
            + (f" logs={log_dir}" if log_dir else "")
        )

    def execute_issue(self, issue: dict[str, Any], repo: Path) -> None:
        identifier = issue["identifier"]
        running_state_id = self.states[self.cfg.running_state]
        project_name = (issue.get("project") or {}).get("name")
        try:
            contract = parse_dispatch_contract(issue.get("description"))
        except DispatchContractError as exc:
            self._record_bridge_failure(issue, repo, str(exc), prefix="DISPATCH_CONTRACT_FAILED")
            return

        if contract and contract.contract_kind == "m6":
            self._execute_m6_issue(issue, repo, contract)
            return

        if contract and contract.contract_kind == "m5":
            self._execute_m5_issue(issue, repo, contract)
            return

        mapped_project_alias = self.cfg.project_alias_for_linear(project_name)
        mapped_target_alias = self.cfg.target_alias_for_project(project_name)
        if contract and contract.contract_kind == "canonical":
            if mapped_project_alias != contract.project_alias:
                self._record_bridge_failure(
                    issue,
                    repo,
                    f"Issue project {contract.project_alias!r} does not match project mapping {mapped_project_alias!r}.",
                    prefix="DISPATCH_PROJECT_RESOLUTION_FAILED",
                )
                return
            project_alias = contract.project_alias
            target_alias = (
                f"{project_alias}.{contract.thread_alias}"
                if contract.thread_mode == "existing"
                else f"{project_alias}.new"
            )
        else:
            target_alias = contract.target_alias if contract else mapped_target_alias
            if contract and mapped_target_alias != contract.target_alias:
                self._record_bridge_failure(
                    issue,
                    repo,
                    f"Issue target {contract.target_alias!r} does not match project mapping {mapped_target_alias!r}.",
                    prefix="DISPATCH_TARGET_RESOLUTION_FAILED",
                )
                return
            project_alias = mapped_project_alias
        if target_alias is None:
            self._record_bridge_failure(
                issue,
                repo,
                f"No target registry entry is mapped to Linear project {project_name!r}.",
                prefix="DISPATCH_TARGET_RESOLUTION_FAILED",
            )
            return

        # v0.1 is explicitly single-worker. The state transition is the visible lease.
        claimed = self.linear.update_issue_state(issue["id"], running_state_id)
        if claimed["state"]["name"] != self.cfg.running_state:
            raise BridgeError(
                f"Failed to claim {identifier}: state is {claimed['state']['name']}"
            )

        claim_body = (
            "BRIDGE_CLAIMED\n\n"
            f"- Bridge: `linear-local-codex-bridge/{BRIDGE_VERSION}`\n"
            f"- Host: `{os.uname().nodename}`\n"
            f"- PID: `{os.getpid()}`\n"
            f"- Repository: `{repo}`\n"
            f"- Target: `{target_alias}`\n"
            f"- Trigger: `{self.cfg.todo_state} + {self.cfg.trigger_label}`\n"
            f"- State transition: `{self.cfg.todo_state} → {self.cfg.running_state}`\n"
            "- Next action: dispatch through SSH to the configured Codex app-server target."
        )
        try:
            self.linear.add_comment(issue["id"], claim_body)
        except Exception as e:
            # Claim visibility is useful, but execution can still proceed because
            # Codex will use Linear MCP for its own evidence.
            print(f"Warning: BRIDGE_CLAIMED comment failed for {identifier}: {e}")

        print(f"Claimed {identifier}; dispatching target {target_alias} from {repo}")
        try:
            if contract and contract.contract_kind == "canonical":
                result = self.dispatcher.dispatch_project(
                    project_alias=project_alias,
                    thread_mode=contract.thread_mode,
                    thread_alias=contract.thread_alias,
                    prompt=codex_prompt(issue, repo, self.cfg.review_state),
                    model=contract.model,
                    reasoning_effort=contract.reasoning_effort,
                )
            else:
                result = self.dispatcher.dispatch(
                    target_alias,
                    codex_prompt(issue, repo, self.cfg.review_state),
                    model=contract.model if contract else None,
                    reasoning_effort=contract.reasoning_effort if contract else None,
                )
        except Exception as e:
            self._record_bridge_failure(
                issue, repo, f"Failed to dispatch Codex: {e}", pre_turn_failure=True
            )
            return

        dispatch_body = (
            "BRIDGE_DISPATCHED\n\n"
            f"- Target: `{result.target_alias}`\n"
            f"- Project: `{result.project_alias or project_alias or 'legacy'}`\n"
            f"- Thread alias: `{result.thread_alias or 'current'}`\n"
            f"- Durable thread: `{result.thread_id}`\n"
            f"- Session: `{result.session_id or 'unknown'}`\n"
            f"- CWD: `{result.cwd or repo}`\n"
            f"- Turn: `{result.turn_id}`\n"
            f"- Model: `{result.model or 'server default'}`\n"
            f"- Reasoning effort: `{result.reasoning_effort or 'server default'}`\n"
            f"- Repository identity source: `{result.repository_identity_source.upper()}`\n"
            f"- Status: `{result.dispatch_status}`\n\n"
            f"- Thread created: `{result.thread_created}`\n"
            "Execution is now awaiting the exact turn completion marker."
        )
        try:
            self.linear.add_comment(issue["id"], dispatch_body)
        except Exception as e:
            print(f"Warning: failed to write BRIDGE_DISPATCHED comment for {identifier}: {e}")
        try:
            log_dir = write_dispatch_record(self.cfg, issue, result)
        except Exception as e:
            log_dir = None
            print(f"Warning: failed to write dispatch log for {identifier}: {e}")
        print(
            f"Dispatched {identifier}: target={result.target_alias} "
            f"thread={result.thread_id} turn={result.turn_id}"
            + (f" logs={log_dir}" if log_dir else "")
        )

        if contract and contract.expected_result:
            final_result = wait_for_codex_result(
                result.thread_id,
                result.turn_id,
                contract.expected_result,
                timeout_seconds=max(30, self.cfg.poll_interval_seconds * 8),
            )
            if final_result != contract.expected_result:
                self._record_bridge_failure(
                    issue,
                    repo,
                    f"Expected {contract.expected_result!r}, received {final_result!r}.",
                    prefix="CODEX_RESULT_FAILED",
                )
                return
            evidence = (
                "CLINX_M3_EXECUTION_COMPLETE\n\n"
                f"ISSUE={identifier}\n"
                f"TARGET={result.target_alias}\n"
                f"PROJECT={result.project_alias or project_alias}\n"
                f"THREAD_ALIAS={result.thread_alias or 'current'}\n"
                f"THREAD_ID={result.thread_id}\n"
                f"SESSION_ID={result.session_id or 'unknown'}\n"
                f"TURN_ID={result.turn_id}\n"
                f"MODEL={result.model or contract.model}\n"
                f"REASONING={result.reasoning_effort or contract.reasoning_effort}\n"
                f"THREAD_CREATED={'YES' if result.thread_created else 'NO'}\n"
                f"THREAD_DURABLE={'YES' if result.thread_durable else 'NO'}\n"
                f"CWD={repo}\n"
                "IDENTITY_GUARD=PASS\n"
                "EXACT_THREAD_DISPATCH=PASS\n"
                "RESULT=CLINX_M2_CHATGPT_ROUNDTRIP_PASS\n"
                "REAL_ORION_THREAD_TOUCHED=NO\n"
                "REAL_TERMINAL_THREAD_TOUCHED=NO\n"
                "VERDICT=READY_FOR_CHATGPT_REVIEW"
            )
            try:
                self.linear.add_comment(issue["id"], evidence)
                self.linear.update_issue_state(issue["id"], self.states[self.cfg.review_state])
            except Exception as exc:
                self._record_bridge_failure(issue, repo, f"Result writeback failed: {exc}")
                return
            print(f"Completed {identifier}: result={final_result}; state={self.cfg.review_state}")

    def _record_bridge_failure(
        self,
        issue: dict[str, Any],
        repo: Path,
        reason: str,
        prefix: str = "BRIDGE_EXECUTION_FAILED",
        pre_turn_failure: bool = False,
    ) -> None:
        task_id = getattr(self.task_dispatcher, "last_task_id", None)
        execution_ref = getattr(self.task_dispatcher, "last_execution_ref", None)
        issue_refs = {issue.get("id"), issue.get("identifier")}
        if (
            task_id
            and self.task_dispatcher is not None
            and execution_ref in issue_refs
        ):
            try:
                self.task_dispatcher.tasks.set_execution_state(
                    task_id,
                    "BLOCKED",
                    current_stage="project identity guard" if "IDENTITY" in reason else "dispatch",
                    current_blocker=reason[:2000],
                    codex_running=False,
                    retry_required=True,
                )
            except Exception as exc:
                print(f"Warning: failed to persist blocked execution state: {exc}")
        body = (
            f"{prefix}\n\n"
            f"- Bridge: `linear-local-codex-bridge/{BRIDGE_VERSION}`\n"
            f"- Repository: `{repo}`\n"
            f"- Reason:\n\n{reason}\n\n"
            "CLINX_EXECUTION_STATUS\n"
            "EXECUTION_STATE=BLOCKED\n"
            "CODEX_RUNNING=NO\n"
            f"CURRENT_BLOCKER={reason[:2000]}\n"
            "RETRY_REQUIRED=YES\n\n"
            f"The issue is intentionally left in `{self.cfg.running_state}` to prevent "
            "an infinite automatic retry loop. After correcting the root cause, move "
            f"the issue back to `{self.cfg.todo_state}` to retry."
        )
        if pre_turn_failure and self._is_self_project(repo):
            body += (
                "\n\nBOOTSTRAP_SELF_REPAIR_REQUIRED=YES\n"
                "AUTOMATIC_RETRY=NO\n"
                "SELF_PROJECT_PRE_TURN_DEADLOCK=DETECTED\n"
                "The CLINX self-project handoff stopped before turn/start; manual "
                "repair qualification is required before resuming this issue."
            )
        try:
            self.linear.add_comment(issue["id"], body)
        except Exception as e:
            print(f"Warning: failed to write bridge failure to Linear: {e}")
        print(f"{prefix} {issue['identifier']}: {reason}", file=sys.stderr)

    def _is_self_project(self, repo: Path) -> bool:
        """Identify only the registered CLINX checkout, never by issue text."""
        try:
            resolved = repo.expanduser().resolve()
        except OSError:
            return False
        return any(
            project.project_alias.casefold() == "clinx"
            and project.repo.expanduser().resolve() == resolved
            for project in self.cfg.projects
        )


def doctor(cfg: BridgeConfig, linear: LinearClient) -> int:
    failures: list[str] = []

    print(f"linear-local-codex-bridge v{BRIDGE_VERSION} doctor")

    try:
        viewer = linear.viewer()
        print(f"[PASS] Linear API auth: {viewer.get('name')} <{viewer.get('email')}>")
    except Exception as e:
        failures.append(f"Linear API auth failed: {e}")
        print(f"[FAIL] Linear API auth: {e}")

    try:
        states = linear.team_states(cfg.team_id)
        wanted = [cfg.todo_state, cfg.running_state, cfg.review_state]
        missing = [s for s in wanted if s not in states]
        if missing:
            raise BridgeError(f"Missing states: {', '.join(missing)}")
        print(f"[PASS] Linear team states: {', '.join(wanted)}")
    except Exception as e:
        failures.append(f"Linear team/status check failed: {e}")
        print(f"[FAIL] Linear team/status check: {e}")

    if cfg.app_server.transport == "local":
        if cfg.app_server.command:
            print(
                "[PASS] App-server transport configured: "
                f"local {' '.join(cfg.app_server.command)}"
            )
        else:
            failures.append("App-server local command is empty")
            print("[FAIL] App-server local command is empty")
    elif cfg.app_server.remote_command:
        transport_alias = cfg.app_server.ssh_alias
        if not transport_alias and cfg.threads:
            transport_alias = cfg.threads[0].ssh_alias
        if not transport_alias and cfg.targets:
            transport_alias = cfg.targets[0].ssh_alias
        if not transport_alias:
            failures.append("SSH app-server transport has no ssh_alias")
            print("[FAIL] SSH app-server transport has no ssh_alias")
            transport_alias = "<missing>"
        print(
            "[PASS] App-server transport configured: "
            f"ssh {transport_alias} {' '.join(cfg.app_server.remote_command)}"
        )
    else:
        failures.append("App-server remote command is empty")
        print("[FAIL] App-server remote command is empty")

    print(
        "[INFO] Legacy local codex exec path is retained for compatibility and "
        "is disabled as the V1 default."
    )

    for mapping in cfg.projects:
        if not mapping.repo.is_dir():
            failures.append(f"Repo missing: {mapping.repo}")
            print(f"[FAIL] Repo missing: {mapping.repo}")
        elif not is_git_repo(mapping.repo):
            failures.append(f"Not a Git repo: {mapping.repo}")
            print(f"[FAIL] Not a Git repo: {mapping.repo}")
        else:
            print(f"[PASS] Repo mapping: {mapping.linear_name} -> {mapping.repo}")
        project_alias = mapping.project_alias
        try:
            ProjectRegistry(cfg.projects).resolve(project_alias)
            binding = next(
                (item for item in cfg.threads if item.project_alias == project_alias),
                None,
            )
            legacy_target_alias = cfg.target_alias_for_project(mapping.linear_name)
            if binding is None and legacy_target_alias is not None:
                target = TargetRegistry(cfg.targets).resolve(legacy_target_alias)
                print(
                    f"[PASS] Legacy target mapping: {mapping.linear_name} -> {target.alias}"
                )
            elif binding is not None:
                print(
                    f"[PASS] Project/thread mapping: {mapping.linear_name} -> "
                    f"{project_alias}.{binding.alias}"
                )
            else:
                print(f"[PASS] Project mapping: {mapping.linear_name} -> {project_alias}")
        except TargetResolutionError as e:
            failures.append(str(e))
            print(f"[FAIL] Project/thread mapping: {mapping.linear_name}: {e}")

    try:
        issues = linear.eligible_issues(
            cfg.team_id,
            cfg.todo_state,
            cfg.trigger_label,
            min(cfg.max_batch, 3),
        )
        ids = ", ".join(i["identifier"] for i in issues) or "none"
        print(f"[PASS] Eligible-issue query works; currently eligible: {ids}")
    except Exception as e:
        failures.append(f"Eligible-issue query failed: {e}")
        print(f"[FAIL] Eligible-issue query: {e}")

    if failures:
        print("\nDOCTOR = FAIL")
        for item in failures:
            print(f"- {item}")
        return 1

    print("\nDOCTOR = PASS")
    return 0


def self_project_check(cfg: BridgeConfig) -> int:
    """Read-only RCA/qualification for the registered CLINX checkout."""
    project = next(
        (item for item in cfg.projects if item.project_alias.casefold() == "clinx"),
        None,
    )
    if project is None:
        print("PROJECT_RESOLUTION=FAIL")
        print("ROOT_CAUSE=CLINX project mapping is absent")
        return 1

    try:
        evidence = _local_git_identity(str(project.repo))
        project_identity_guard(project, evidence)
        identity = "PASS"
        root_cause = (
            "Local-only Git repository has no origin; missing origin is intentional "
            "and an unexpected origin is rejected."
        )
    except Exception as exc:
        evidence = None
        identity = "FAIL"
        root_cause = str(exc)

    print(f"CLINX_SELF_PROJECT_ROOT={project.repo}")
    print(f"CLINX_SELF_PROJECT_BRANCH={project.branch or ''}")
    print(f"CLINX_SELF_PROJECT_EXPECTED_ORIGIN={project.repository_origin or ''}")
    print(
        "CLINX_SELF_PROJECT_ACTUAL_ORIGIN="
        f"{evidence.origin if evidence and evidence.origin else ''}"
    )
    print(f"PROJECT_RESOLUTION=PASS")
    print(f"PROJECT_IDENTITY_GUARD={identity}")
    print(f"ROOT_CAUSE={root_cause}")
    return 0 if identity == "PASS" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="linear-local-codex-bridge",
        description="Linear -> local Codex CLI actuator",
    )
    parser.add_argument(
        "--config",
        default="bridge.toml",
        help="Path to TOML config (default: bridge.toml)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Validate Linear, Codex, MCP, and repo mappings")
    sub.add_parser(
        "self-project-check",
        help="Read-only CLINX local-project identity and RCA check",
    )
    onboard = sub.add_parser(
        "onboard-thread",
        help="Read-only existing durable thread discovery",
    )
    onboard.add_argument("--project", required=True)
    onboard.add_argument("--read-only", action="store_true", required=True)
    register = sub.add_parser(
        "register-thread",
        help="Register one selected existing durable thread (read-only)",
    )
    register.add_argument("--project", required=True)
    register.add_argument("--alias", required=True)
    register.add_argument("--thread-id", required=True)
    register.add_argument("--read-only", action="store_true", required=True)
    adopt = sub.add_parser(
        "adopt-thread",
        help="Adopt one existing durable thread into the task registry",
    )
    adopt.add_argument("--project", required=True)
    adopt.add_argument("--thread-id", required=True)
    adopt.add_argument("--title", required=True)
    adopt.add_argument("--summary", required=True)
    adopt.add_argument("--host")
    adopt.add_argument("--execution-mode", choices=("normal", "fast"), default="normal")
    adopt.add_argument("--task-key")
    adopt.add_argument("--sync-index", action="store_true")
    adopt.add_argument("--read-only", action="store_true", required=True)
    tasks = sub.add_parser(
        "tasks",
        help="Read-only persistent task discovery",
    )
    task_sub = tasks.add_subparsers(dest="tasks_command", required=True)
    tasks_list = task_sub.add_parser("list", help="List task registry records")
    tasks_list.add_argument("--host")
    tasks_list.add_argument("--project")
    tasks_list.add_argument("--status", choices=("ACTIVE", "COMPLETED", "ARCHIVED"))
    tasks_list.add_argument("--include-archived", action="store_true")
    tasks_find = task_sub.add_parser("find", help="Find tasks by human title or summary")
    tasks_find.add_argument("--host")
    tasks_find.add_argument("--project", required=True)
    tasks_find.add_argument("--query", required=True)
    tasks_find.add_argument("--status", choices=("ACTIVE", "COMPLETED", "ARCHIVED"))
    tasks_find.add_argument("--include-archived", action="store_true")
    tasks_show = task_sub.add_parser("show", help="Show one task by hidden task reference")
    tasks_show.add_argument("task_ref")
    tasks_context = task_sub.add_parser(
        "context", help="Read bounded authoritative context for one task"
    )
    tasks_context.add_argument("task_ref", nargs="?")
    tasks_context.add_argument("--host")
    tasks_context.add_argument("--project")
    tasks_context.add_argument("--query")
    tasks_context.add_argument("--recent-turns", type=int, default=TaskContextReader.DEFAULT_RECENT_TURNS)
    tasks_context.add_argument("--max-bytes", type=int, default=TaskContextReader.DEFAULT_MAX_BYTES)
    tasks_topic = task_sub.add_parser(
        "topic", help="Read bounded deterministic status for a project topic"
    )
    tasks_topic.add_argument("--host")
    tasks_topic.add_argument("--project", required=True)
    tasks_topic.add_argument("--topic", required=True)
    tasks_topic.add_argument("--include-completed", action=argparse.BooleanOptionalAction, default=True)
    tasks_topic.add_argument("--include-historical", action=argparse.BooleanOptionalAction, default=True)
    tasks_topic.add_argument("--limit", type=int, default=TopicStatusReader.MAX_LIMIT)
    tasks_topic.add_argument("--recent-turns", type=int, default=TopicStatusReader.INITIAL_TURNS)
    tasks_topic.add_argument("--max-bytes", type=int, default=TopicStatusReader.MAX_TOPIC_BYTES)
    sub.add_parser("once", help="Poll once and execute at most max_batch issues")
    sub.add_parser("run", help="Run foreground polling loop")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config_path = Path(args.config).expanduser().resolve()

    try:
        cfg = BridgeConfig.load(config_path)
    except Exception as e:
        print(f"CONFIG_ERROR: {e}", file=sys.stderr)
        return 2

    if args.command == "self-project-check":
        return self_project_check(cfg)

    if args.command == "onboard-thread":
        if not args.read_only:
            print("READ_ONLY is required for onboard-thread", file=sys.stderr)
            return 2
        try:
            result = onboard_existing_thread(
                cfg,
                args.project,
                config_path=config_path,
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        except Exception as e:
            print(f"M4_A=BLOCKED: {e}", file=sys.stderr)
            return 1

    if args.command == "register-thread":
        try:
            result = register_existing_thread(
                cfg,
                args.project,
                args.alias,
                args.thread_id,
                config_path=config_path,
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        except Exception as e:
            print(f"M4_A2_REGISTRATION=BLOCKED: {e}", file=sys.stderr)
            return 1

    if args.command == "adopt-thread":
        try:
            dispatcher = TaskDispatcher(cfg)
            index = None
            if args.sync_index:
                api_key = os.environ.get("LINEAR_API_KEY", "").strip()
                if not api_key:
                    raise LinearAPIError("LINEAR_API_KEY is required for --sync-index")
                index = LinearTaskIndex(
                    LinearClient(api_key), dispatcher.tasks, cfg.team_id
                )
            task, binding, index_record = dispatcher.adopt_existing_conversation(
                project_ref=args.project,
                host=args.host,
                thread_id=args.thread_id,
                title=args.title,
                summary=args.summary,
                task_key=args.task_key,
                execution_mode=args.execution_mode,
                task_index=index,
            )
            print(json.dumps({
                "adoption": "PASS",
                "task": _task_public_record(dispatcher.tasks, task),
                "thread_id": binding.thread_id,
                "session_id": binding.session_id,
                "task_index_issue": index_record.identifier if index_record else None,
                "thread_start_sent": False,
                "turn_start_sent": False,
            }, sort_keys=True))
            return 0
        except Exception as e:
            print(f"ADOPTION=FAIL: {e}", file=sys.stderr)
            return 1

    if args.command == "tasks":
        registry = TaskRegistry(
            cfg.task_db_path
            or (Path.home() / ".local" / "state" / "clinx" / "tasks.sqlite3")
        )
        try:
            if args.tasks_command == "list":
                rows = registry.list_tasks(
                    host=args.host,
                    project=args.project,
                    status=args.status,
                    include_archived=args.include_archived or args.status == "ARCHIVED",
                )
                payload: dict[str, Any] = {
                    "read_only": True,
                    "classification": "LIST",
                    "tasks": [_task_public_record(registry, task) for task in rows],
                }
            elif args.tasks_command == "find":
                result = registry.find_tasks(
                    host=args.host,
                    project=args.project,
                    query=args.query,
                    status=args.status,
                    include_archived=args.include_archived or args.status == "ARCHIVED",
                )
                payload = {
                    "read_only": True,
                    "classification": result.classification,
                    "tasks": [
                        _task_public_record(registry, task) for task in result.tasks
                    ],
                }
            elif args.tasks_command == "context":
                reader = TaskContextReader(cfg, registry)
                task = reader.resolve_task(
                    task_ref=args.task_ref,
                    host=args.host,
                    project=args.project,
                    query=args.query,
                )
                payload = reader.read_task_context(
                    task.task_id,
                    recent_turns=args.recent_turns,
                    max_bytes=args.max_bytes,
                ).as_dict()
            elif args.tasks_command == "topic":
                reader = TopicStatusReader(cfg, registry)
                payload = reader.read_topic_status(
                    host=args.host,
                    project_ref=args.project,
                    topic=args.topic,
                    include_completed=args.include_completed,
                    include_historical=args.include_historical,
                    limit=args.limit,
                    recent_turns=args.recent_turns,
                    max_bytes=args.max_bytes,
                ).as_dict()
            else:
                task = registry.get_task(args.task_ref)
                payload = {
                    "read_only": True,
                    "classification": "EXACT",
                    "task": _task_public_record(registry, task),
                }
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0
        except Exception as e:
            print(f"TASK_QUERY_ERROR: {e}", file=sys.stderr)
            return 1

    api_key = os.environ.get("LINEAR_API_KEY", "").strip()
    if not api_key:
        print(
            "CONFIG_ERROR: LINEAR_API_KEY is not set. "
            "Create a Linear personal API key and export it only in the bridge process.",
            file=sys.stderr,
        )
        return 2

    linear = LinearClient(api_key)

    if args.command == "doctor":
        return doctor(cfg, linear)

    lock_path = cfg.log_dir / "bridge.lock"
    try:
        with SingleInstanceLock(lock_path):
            bridge = Bridge(cfg, linear)
            bridge.initialize()

            if args.command == "once":
                bridge.poll_once()
                return 0

            print(
                f"linear-local-codex-bridge v{BRIDGE_VERSION} running; "
                f"poll={cfg.poll_interval_seconds}s; label={cfg.trigger_label!r}"
            )
            while True:
                try:
                    bridge.poll_once()
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    print(f"POLL_ERROR: {e}", file=sys.stderr)
                time.sleep(cfg.poll_interval_seconds)
    except KeyboardInterrupt:
        print("\nBridge stopped.")
        return 0
    except Exception as e:
        print(f"BRIDGE_ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
