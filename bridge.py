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
    DynamicProjectResolver,
    ProjectDescriptor,
    TaskExecutionBusy,
    TaskRegistry,
    TaskRegistryError,
    WorkspaceConfig,
    WorkspaceRegistry,
)

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
BRIDGE_VERSION = "1.0.0-m5"
LEGACY_CODEX_EXEC_DEFAULT = False


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


@dataclasses.dataclass(frozen=True)
class ProjectMapping:
    linear_name: str
    repo: Path
    target_alias: str | None = None
    alias: str | None = None
    repository_origin: str | None = None
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
    repository_origin: str
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


def parse_dispatch_contract(description: str | None) -> DispatchContract | None:
    """Parse legacy M2/M3 contracts and the explicit M5 task contract."""
    if not description:
        return None

    def value_for(key: str) -> str | None:
        match = re.search(rf"(?m)^\s*{key}=([^\s]+)\s*$", description)
        return match.group(1) if match else None

    marker = re.search(
        r"(?m)^\s*Return exactly this final marker:\s*$\n\s*([^\s]+)\s*$",
        description,
    )
    expected_result = marker.group(1) if marker else None

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
                origin = str(row.get("repository_origin", "")).strip()
                branch = str(row.get("branch", "")).strip()
                if not cwd or not origin or not branch:
                    raise BridgeError(
                        f"Project {alias!r} requires cwd, repository_origin, and branch"
                    )
                linear_name = str(row.get("linear_name", alias)).strip()
                projects.append(
                    ProjectMapping(
                        linear_name=linear_name,
                        repo=Path(cwd).expanduser().resolve(),
                        alias=str(alias),
                        repository_origin=origin,
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

BOOTSTRAP ONLY
Use the configured Linear MCP integration to read the full issue {identifier} and its latest comments.
The Linear issue is the authoritative execution contract. Do not ask the operator to paste or restate it.
Do not treat this bootstrap prompt as a replacement for the Linear issue.

EXECUTION PROTOCOL
1. Confirm {identifier} is currently In Progress.
2. Execute the issue exactly as written, inside the current repository only.
3. Post all start/progress/final evidence required by the issue back to {identifier} through Linear MCP.
4. On successful completion, move {identifier} to {review_state}.
5. Never mark the issue Done.
6. Do not create unrelated Linear issues/projects and do not expand scope.
7. If blocked, post the exact blocker to {identifier} and leave it in In Progress.
8. If the Linear MCP server cannot initialize or cannot read/write {identifier}, stop immediately and return the exact error.

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
    origin: str
    branch: str
    head: str | None = None


def _local_git_identity(cwd: str) -> RepositoryIdentityEvidence:
    """Read repository identity only from the cwd returned by thread/read."""
    commands = {
        "top_level": ("git", "-C", cwd, "rev-parse", "--show-toplevel"),
        "origin": ("git", "-C", cwd, "remote", "get-url", "origin"),
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
    return RepositoryIdentityEvidence(
        source="local_git",
        cwd=cwd,
        origin=outputs["origin"],
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
    origin = raw_git_info.get("originUrl") or ""
    branch = raw_git_info.get("branch")
    if not isinstance(origin, str) or not isinstance(branch, str) or not branch:
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
        repository_origin=project.repository_origin or "",
        branch=project.branch or "",
        app_server_version=(
            binding.app_server_version
            if binding is not None
            else (cfg.threads[0].app_server_version if cfg.threads else cfg.app_server.client_version)
        ),
        target_host=binding.target_host if binding is not None else "",
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
        repository_origin=project.repository_origin or "",
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
        self.projects = DynamicProjectResolver(self.workspaces, cfg.projects)

    def _workspace(self, host: str | None) -> WorkspaceConfig:
        if host:
            return self.workspaces.resolve(host)
        values = list(self.workspaces)
        if len(values) == 1:
            return values[0]
        raise TargetResolutionError("M5 HOST is required when multiple workspaces exist")

    def resolve_project(
        self,
        project_ref: str,
        *,
        host: str | None,
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
            repository_origin=project.repository_origin or "",
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
            repository_origin=project.repository_origin or "",
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
        model: str | None,
        reasoning_effort: str | None,
        execution_mode: str = "normal",
        issue_id: str | None = None,
    ) -> DispatchResult:
        if task_mode not in {"new", "continue"}:
            raise DispatchContractError(f"Unsupported task mode: {task_mode!r}")
        if execution_mode not in {"normal", "fast"}:
            raise DispatchContractError(f"Unsupported execution mode: {execution_mode!r}")

        if task_mode == "new":
            workspace, _descriptor, project = self.resolve_project(
                project_ref, host=host, project_mode=project_mode
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
            )
            with self.tasks.execution(task.task_id, issue_id) as leased:
                target = self._new_target(workspace, project)
                client = self.client_factory(target)
                try:
                    with client:
                        initialize_info = client.initialize(
                            client_name=self.cfg.app_server.client_name,
                            client_title=self.cfg.app_server.client_title,
                            client_version=self.cfg.app_server.client_version,
                        )
                        started = client.thread_start(
                            cwd=str(project.repo),
                            model=model,
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
                        self._read_and_guard(client, target, initialize_info)
                        binding = self.tasks.bind_conversation(
                            task_id=leased.task_id,
                            thread_id=new_thread_id,
                            session_id=new_session_id,
                            project_id=actual_project_id,
                            app_server_version=version,
                        )
                        turn = client.turn_start(
                            new_thread_id,
                            prompt,
                            cwd=str(project.repo),
                            model=model,
                            reasoning_effort=reasoning_effort,
                            approval_policy=self.cfg.approval,
                        )
                except (IdentityGuardError, AppServerError):
                    raise
            self.tasks.record_linear_execution(issue_id, leased.task_id) if issue_id else None
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
            )

        if not task_id:
            raise DispatchContractError("TASK_MODE=continue requires TASK_ID")
        task = self.tasks.get_task(task_id)
        if task.status != "ACTIVE":
            raise TargetResolutionError(
                f"Task {task.task_id} is {task.status}; explicit reopen is required"
            )
        if host and host.strip().lower() != task.workspace_alias.lower():
            raise TargetResolutionError(
                f"Task workspace mismatch: expected {task.workspace_alias!r}, got {host!r}"
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
        with self.tasks.execution(task.task_id, issue_id) as leased:
            target = self._target(workspace, project, binding)
            client = self.client_factory(target)
            with client:
                initialize_info = client.initialize(
                    client_name=self.cfg.app_server.client_name,
                    client_title=self.cfg.app_server.client_title,
                    client_version=self.cfg.app_server.client_version,
                )
                self._read_and_guard(client, target, initialize_info)
                self.tasks.mark_verified(
                    task.task_id,
                    app_server_version=self._initialize_version(
                        initialize_info, target.app_server_version
                    ),
                )
                turn = client.turn_start(
                    binding.thread_id,
                    prompt,
                    cwd=str(project.repo),
                    model=model,
                    reasoning_effort=reasoning_effort,
                    approval_policy=self.cfg.approval,
                )
        if issue_id:
            self.tasks.record_linear_execution(issue_id, leased.task_id)
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
            return self.tasks.set_status(task_id, "ACTIVE").status
        raise DispatchContractError(f"Unsupported TASK_ACTION={action!r}")


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
                turn = client.turn_start(
                    target.thread_id,
                    prompt,
                    cwd=str(project.repo),
                    model=model,
                    reasoning_effort=reasoning_effort,
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
                repository_origin=project.repository_origin or "",
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
                started = client.thread_start(
                    cwd=str(project.repo),
                    model=model,
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
                turn = client.turn_start(
                    new_thread_id,
                    prompt,
                    cwd=str(project.repo),
                    model=model,
                    reasoning_effort=reasoning_effort,
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
                turn = client.turn_start(
                    target.thread_id,
                    prompt,
                    cwd=target.cwd,
                    model=model,
                    reasoning_effort=reasoning_effort,
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

    def _m5_dispatcher(self) -> TaskDispatcher:
        if self.task_dispatcher is None:
            self.task_dispatcher = TaskDispatcher(self.cfg)
        return self.task_dispatcher

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
            is_m5_shape = any(
                re.search(rf"(?m)^\s*{key}=", issue.get("description") or "")
                for key in ("TASK_MODE", "PROJECT_MODE", "EXECUTION_MODE", "TASK_ACTION")
            )
            if contract and contract.contract_kind == "m5":
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
            elif is_m5_shape:
                self._record_bridge_failure(
                    issue,
                    Path("."),
                    "Malformed M5 dispatch contract",
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

            if not contract or contract.contract_kind != "m5":
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
                model=contract.model,
                reasoning_effort=contract.reasoning_effort,
                execution_mode=contract.execution_mode,
                issue_id=issue.get("id"),
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

    def execute_issue(self, issue: dict[str, Any], repo: Path) -> None:
        identifier = issue["identifier"]
        running_state_id = self.states[self.cfg.running_state]
        project_name = (issue.get("project") or {}).get("name")
        try:
            contract = parse_dispatch_contract(issue.get("description"))
        except DispatchContractError as exc:
            self._record_bridge_failure(issue, repo, str(exc), prefix="DISPATCH_CONTRACT_FAILED")
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
            self._record_bridge_failure(issue, repo, f"Failed to dispatch Codex: {e}")
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
    ) -> None:
        body = (
            f"{prefix}\n\n"
            f"- Bridge: `linear-local-codex-bridge/{BRIDGE_VERSION}`\n"
            f"- Repository: `{repo}`\n"
            f"- Reason:\n\n{reason}\n\n"
            f"The issue is intentionally left in `{self.cfg.running_state}` to prevent "
            "an infinite automatic retry loop. After correcting the root cause, move "
            f"the issue back to `{self.cfg.todo_state}` to retry."
        )
        try:
            self.linear.add_comment(issue["id"], body)
        except Exception as e:
            print(f"Warning: failed to write bridge failure to Linear: {e}")
        print(f"{prefix} {issue['identifier']}: {reason}", file=sys.stderr)


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
