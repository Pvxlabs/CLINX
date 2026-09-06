#!/usr/bin/env python3
"""linear-local-codex-bridge Dispatcher V1 / M0.

Linear (Todo + trigger label) -> claim In Progress -> SSH P620 -> Codex
app-server -> exact durable thread -> turn/start.

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

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
BRIDGE_VERSION = "1.0.0-m0"
LEGACY_CODEX_EXEC_DEFAULT = False


class BridgeError(RuntimeError):
    pass


class LinearAPIError(BridgeError):
    pass


class TargetResolutionError(BridgeError):
    pass


class IdentityGuardError(BridgeError):
    pass


@dataclasses.dataclass(frozen=True)
class ProjectMapping:
    linear_name: str
    repo: Path
    target_alias: str | None = None


@dataclasses.dataclass(frozen=True)
class AppServerConfig:
    transport: str = "local"
    command: tuple[str, ...] = ("codex", "app-server", "proxy")
    ssh_binary: str = "ssh"
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

    @staticmethod
    def load(path: Path) -> "BridgeConfig":
        with path.open("rb") as f:
            raw = tomllib.load(f)

        linear = raw.get("linear", {})
        codex = raw.get("codex", {})
        app_server_raw = raw.get("app_server", {})
        runtime = raw.get("runtime", {})
        project_rows = raw.get("project", [])
        target_rows = raw.get("targets", {})

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
        for row in project_rows:
            name = str(row.get("linear_name", "")).strip()
            repo = str(row.get("repo", "")).strip()
            if not name or not repo:
                raise BridgeError("Every [[project]] requires linear_name and repo")
            target_alias = str(row.get("target_alias", "")).strip() or None
            projects.append(
                ProjectMapping(name, Path(repo).expanduser().resolve(), target_alias)
            )

        if not projects:
            raise BridgeError("At least one [[project]] mapping is required")

        if not isinstance(target_rows, dict) or not target_rows:
            raise BridgeError("At least one [targets.<alias>] entry is required")
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
                    **{k: str(v) for k, v in values.items()},
                )
            )

        target_aliases = {target.alias for target in targets}
        unmapped_aliases = {
            mapping.target_alias
            for mapping in projects
            if mapping.target_alias and mapping.target_alias not in target_aliases
        }
        if unmapped_aliases:
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
                ssh_args=tuple(str(arg) for arg in ssh_args),
                remote_command=remote_command,
                request_timeout_seconds=timeout,
                client_name=str(app_server_raw.get("client_name", "linear-local-codex-bridge")),
                client_title=str(app_server_raw.get("client_title", "Linear Local Codex Bridge")),
                client_version=str(app_server_raw.get("client_version", BRIDGE_VERSION)),
            ),
            targets=tuple(targets),
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
    }
    (run_dir / "dispatch.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return run_dir


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
    if not outputs["origin"]:
        raise IdentityGuardError(
            "DISPATCH_IDENTITY_GUARD=FAIL\n- repository origin is missing"
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
    origin = raw_git_info.get("originUrl")
    branch = raw_git_info.get("branch")
    if not isinstance(origin, str) or not origin or not isinstance(branch, str) or not branch:
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


def _default_app_server_client(
    cfg: BridgeConfig,
    target: TargetConfig,
) -> CodexAppServerClient:
    if cfg.app_server.transport == "local":
        transport = LocalStdioTransport(
            cfg.app_server.command,
            timeout_seconds=cfg.app_server.request_timeout_seconds,
        )
    elif cfg.app_server.transport == "ssh":
        transport = SSHStdioTransport(
            target.ssh_alias,
            cfg.app_server.remote_command,
            ssh_binary=cfg.app_server.ssh_binary,
            ssh_args=cfg.app_server.ssh_args,
            timeout_seconds=cfg.app_server.request_timeout_seconds,
        )
    else:
        raise BridgeError(f"Unsupported app-server transport: {cfg.app_server.transport}")
    return CodexAppServerClient(
        transport,
        timeout_seconds=cfg.app_server.request_timeout_seconds,
    )


class Dispatcher:
    """Dispatch prompts to an explicit target's exact durable Codex thread."""

    def __init__(
        self,
        cfg: BridgeConfig,
        client_factory=None,
    ):
        self.cfg = cfg
        self.registry = TargetRegistry(cfg.targets)
        self.client_factory = client_factory or (
            lambda target: _default_app_server_client(cfg, target)
        )

    def dispatch(
        self,
        target_alias: str,
        prompt: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> DispatchResult:
        target = self.registry.resolve(target_alias)
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
            repo = self.cfg.repo_for_project(project_name)
            if repo is None:
                print(
                    f"Skip {issue['identifier']}: no repo mapping for Linear project "
                    f"{project_name!r}"
                )
                continue

            if not repo.is_dir() or not is_git_repo(repo):
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

    def execute_issue(self, issue: dict[str, Any], repo: Path) -> None:
        identifier = issue["identifier"]
        running_state_id = self.states[self.cfg.running_state]
        project_name = (issue.get("project") or {}).get("name")
        target_alias = self.cfg.target_alias_for_project(project_name)
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
            result = self.dispatcher.dispatch(
                target_alias,
                codex_prompt(issue, repo, self.cfg.review_state),
            )
        except Exception as e:
            self._record_bridge_failure(issue, repo, f"Failed to dispatch Codex: {e}")
            return

        dispatch_body = (
            "BRIDGE_DISPATCHED\n\n"
            f"- Target: `{result.target_alias}`\n"
            f"- Durable thread: `{result.thread_id}`\n"
            f"- Turn: `{result.turn_id}`\n"
            f"- Model: `{result.model or 'server default'}`\n"
            f"- Reasoning effort: `{result.reasoning_effort or 'server default'}`\n"
            f"- Repository identity source: `{result.repository_identity_source.upper()}`\n"
            f"- Status: `{result.dispatch_status}`\n\n"
            "M0 stops after turn/start. The issue remains In Progress until a later milestone adds completion/result handling."
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
        print(
            "[PASS] App-server transport configured: "
            f"ssh {cfg.targets[0].ssh_alias} {' '.join(cfg.app_server.remote_command)}"
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
        target_alias = cfg.target_alias_for_project(mapping.linear_name)
        if target_alias is None:
            failures.append(f"No target mapping for project: {mapping.linear_name}")
            print(f"[FAIL] Target mapping: {mapping.linear_name}")
        else:
            try:
                target = TargetRegistry(cfg.targets).resolve(target_alias)
                print(f"[PASS] Target mapping: {mapping.linear_name} -> {target.alias}")
            except TargetResolutionError as e:
                failures.append(str(e))
                print(f"[FAIL] Target mapping: {mapping.linear_name} -> {target_alias}: {e}")

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
