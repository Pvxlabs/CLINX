"""Durable M5/M6 task, workspace, and conversation registries.

This module deliberately contains no Linear or Codex protocol code.  It owns
the local durable identity that connects a Linear execution to one project and
one exact Codex conversation.  SQLite is used because it gives us atomic
updates and a process-safe lease without introducing a service or dependency.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import unicodedata
import uuid
from typing import Any, Iterator


class TaskRegistryError(RuntimeError):
    pass


class UnknownTaskError(TaskRegistryError):
    pass


class TaskExecutionBusy(TaskRegistryError):
    pass


class WorktreeExecutionBusy(TaskExecutionBusy):
    """A second mutating execution attempted to claim an active worktree."""

    def __init__(self, message: str, *, active_task_id: str, active_execution_ref: str | None,
                 stage: str, worktree_key: str):
        super().__init__(message)
        self.active_task_id = active_task_id
        self.active_execution_ref = active_execution_ref
        self.stage = stage
        self.worktree_key = worktree_key


class WorkspaceResolutionError(TaskRegistryError):
    pass


class ProjectResolutionError(TaskRegistryError):
    pass


MAX_TASK_TITLE_LENGTH = 240
MAX_TASK_SUMMARY_LENGTH = 2000
_UNSET = object()


@dataclasses.dataclass(frozen=True)
class WorkspaceConfig:
    alias: str
    root: Path
    allow_existing_projects: bool = True
    allow_new_projects: bool = False
    # Execution-host identity is separate from the optional remote SSH alias.
    host: str | None = None
    ssh_alias: str | None = None


class WorkspaceRegistry:
    def __init__(self, workspaces: tuple[WorkspaceConfig, ...] | list[WorkspaceConfig]):
        self._workspaces = {item.alias.lower(): item for item in workspaces}
        if len(self._workspaces) != len(workspaces):
            raise WorkspaceResolutionError("Duplicate workspace alias")

    def resolve(self, alias: str) -> WorkspaceConfig:
        workspace = self._workspaces.get(alias.strip().lower())
        if workspace is None:
            raise WorkspaceResolutionError(f"Unknown workspace alias: {alias}")
        return workspace

    def __iter__(self):
        return iter(self._workspaces.values())

    @staticmethod
    def _relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def validate_path(self, workspace: WorkspaceConfig, path: Path) -> Path:
        """Resolve a path and reject both ``..`` and symlink escapes."""
        root = workspace.root.expanduser().resolve(strict=True)
        candidate = path.expanduser()
        resolved = candidate.resolve(strict=False)
        if not self._relative_to(resolved, root):
            raise WorkspaceResolutionError(
                f"Path escapes workspace {workspace.alias!r}: {path}"
            )
        return resolved

    def project_path(self, workspace: WorkspaceConfig, project_name: str) -> Path:
        name = project_name.strip()
        if not name or Path(name).is_absolute() or name in {".", ".."}:
            raise ProjectResolutionError("project name must be a relative name")
        if "/" in name or "\\" in name or ".." in Path(name).parts:
            raise ProjectResolutionError("project name must not contain path traversal")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            raise ProjectResolutionError(
                "project name must contain only letters, numbers, '.', '_' or '-'")
        return self.validate_path(workspace, workspace.root / name)


@dataclasses.dataclass(frozen=True)
class ProjectDescriptor:
    alias: str
    name: str
    workspace_alias: str
    cwd: Path
    repository_origin: str | None
    branch: str | None
    registered: bool


def _git_identity(path: Path) -> tuple[str, str]:
    def run(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(path), *args],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ProjectResolutionError(f"Git identity lookup failed at {path}") from exc
        return result.stdout.strip()

    top_level = Path(run("rev-parse", "--show-toplevel")).resolve()
    if top_level != path.resolve():
        raise ProjectResolutionError(
            f"project cwd is not Git top-level: expected={path} actual={top_level}")
    origin = _git_origin(path)
    branch = run("branch", "--show-current")
    if not branch:
        raise ProjectResolutionError(f"project is detached HEAD: {path}")
    return origin or "", branch


def _has_origin(path: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), "remote", "get-url", "origin"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _git_origin(path: Path) -> str | None:
    """Return origin, treating only Git's missing-origin result as absent."""
    result = subprocess.run(
        ["git", "-C", str(path), "remote", "get-url", "origin"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode == 0:
        value = result.stdout.strip()
        return value or None
    stderr = result.stderr.casefold()
    if "no such remote" in stderr or "remote 'origin'" in stderr and "does not exist" in stderr:
        return None
    raise ProjectResolutionError(
        f"Git origin lookup failed at {path}: {result.stderr.strip() or 'unknown error'}"
    )


class DynamicProjectResolver:
    """Resolve registered projects or safe workspace-relative project names."""

    def __init__(
        self,
        workspaces: WorkspaceRegistry,
        registered_projects: Any = (),
    ):
        self.workspaces = workspaces
        self._registered: dict[str, Any] = {}
        for project in registered_projects:
            alias = str(getattr(project, "project_alias", "")).strip().lower()
            name = str(getattr(project, "linear_name", "")).strip().lower()
            if alias:
                self._registered[alias] = project
            if name:
                self._registered.setdefault(name, project)

    def resolve(
        self,
        project_ref: str,
        *,
        workspace_alias: str,
        project_mode: str = "existing",
    ) -> ProjectDescriptor:
        if project_mode not in {"existing", "create"}:
            raise ProjectResolutionError(f"Unsupported project mode: {project_mode}")
        workspace = self.workspaces.resolve(workspace_alias)
        registered = self._registered.get(project_ref.strip().lower())
        if registered is not None:
            cwd = self.workspaces.validate_path(workspace, Path(registered.repo))
            if project_mode == "create":
                raise ProjectResolutionError("registered projects cannot use PROJECT_MODE=create")
            if not workspace.allow_existing_projects:
                raise ProjectResolutionError(
                    f"workspace {workspace.alias!r} does not allow existing projects")
            if not cwd.is_dir():
                raise ProjectResolutionError(f"registered project does not exist: {cwd}")
            return ProjectDescriptor(
                alias=str(getattr(registered, "project_alias", project_ref)),
                name=str(getattr(registered, "linear_name", project_ref)),
                workspace_alias=workspace.alias,
                cwd=cwd,
                repository_origin=getattr(registered, "repository_origin", None),
                branch=getattr(registered, "branch", None),
                registered=True,
            )

        path = self.workspaces.project_path(workspace, project_ref)
        if project_mode == "create":
            if not workspace.allow_new_projects:
                raise ProjectResolutionError(
                    f"workspace {workspace.alias!r} does not allow new projects")
            if path.exists():
                raise ProjectResolutionError(f"project path already exists: {path}")
            path.mkdir(parents=False, exist_ok=False)
            try:
                subprocess.run(
                    ["git", "-C", str(path), "init", "-b", "main"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                raise ProjectResolutionError(f"failed to initialize project Git repo: {path}") from exc
            return ProjectDescriptor(
                alias=project_ref,
                name=project_ref,
                workspace_alias=workspace.alias,
                cwd=path,
                repository_origin=None,
                branch="main",
                registered=False,
            )

        if not workspace.allow_existing_projects:
            raise ProjectResolutionError(
                f"workspace {workspace.alias!r} does not allow existing projects")
        if not path.is_dir():
            raise ProjectResolutionError(f"project does not exist: {path}")
        origin, branch = _git_identity(path)
        return ProjectDescriptor(
            alias=project_ref,
            name=project_ref,
            workspace_alias=workspace.alias,
            cwd=path,
            repository_origin=origin or None,
            branch=branch,
            registered=False,
        )


@dataclasses.dataclass(frozen=True)
class ConversationBinding:
    task_id: str
    thread_id: str
    session_id: str
    project_id: str | None
    bound_at: str
    last_verified_at: str
    app_server_version: str | None


@dataclasses.dataclass(frozen=True)
class TaskRecord:
    task_id: str
    host: str
    workspace_alias: str
    project_alias: str
    project_name: str
    cwd: str
    repository_origin: str | None
    branch: str | None
    title: str
    summary: str | None
    task_key: str | None
    execution_mode: str
    status: str
    created_at: str
    updated_at: str
    execution_state: str
    current_stage: str
    current_blocker: str | None
    last_progress_at: str
    codex_running: bool
    turn_id: str | None
    retry_required: bool
    failure_stage: str | None
    failure_code: str | None
    failure_evidence: str | None


@dataclasses.dataclass(frozen=True)
class LinearAuditRecord:
    task_id: str
    sync_state: str
    retry_required: bool
    last_error: str | None
    updated_at: str


@dataclasses.dataclass(frozen=True)
class TaskIndexRecord:
    task_id: str
    issue_id: str
    identifier: str
    project_id: str | None
    created_at: str
    updated_at: str


@dataclasses.dataclass(frozen=True)
class PreparedExecutionRecord:
    """Durable, integrity-checked command-plane preparation."""

    prepared_execution_ref: str
    integrity_hash: str
    approval_state: str
    task_action: str
    task_ref: str | None
    host: str
    project: str
    title: str
    summary: str | None
    prompt: str
    model: str
    logical_model: str
    resolved_executable_model: str
    reasoning_effort: str
    execution_mode: str
    network_access: bool
    status: str
    created_at: str
    updated_at: str
    resulting_task_id: str | None
    resulting_thread_id: str | None
    resulting_turn_id: str | None
    resulting_execution_ref: str | None


@dataclasses.dataclass(frozen=True)
class TaskSearchResult:
    classification: str
    tasks: tuple[TaskRecord, ...]


@dataclasses.dataclass(frozen=True)
class ContextCheckpoint:
    checkpoint_id: str
    task_id: str
    execution_id: str | None
    thread_id: str
    turn_id: str | None
    timestamp: str
    prompt_summary: str
    result_summary: str
    changed_files: str
    validation_summary: str
    blockers: str
    next_state: str
    source: str
    provenance: str


@dataclasses.dataclass(frozen=True)
class ContextAnchor:
    """A bounded, task-specific live-history anchor.

    The anchor is only a selector for a native history segment.  It is not a
    transcript or a replacement for the live Codex conversation.
    """

    task_id: str
    thread_id: str
    turn_id: str
    source: str
    created_at: str


@dataclasses.dataclass(frozen=True)
class ExecutionResultRecord:
    execution_ref: str
    task_id: str
    turn_id: str
    status: str
    summary: str
    changed_files: str
    validation: str
    blockers: str
    next_state: str
    raw_result: str
    received_at: str
    writeback_state: str
    writeback_body_hash: str | None
    written_at: str | None


def _now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


class TaskRegistry:
    """SQLite-backed durable task registry with one active execution lease."""

    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._initialize()
        except sqlite3.OperationalError as exc:
            # The tunnel child may be intentionally launched with a read-only
            # state directory. Existing M11 read paths remain usable; a normal
            # CLINX process will run migrations when its DB is writable.
            if "readonly" not in str(exc).casefold():
                raise

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    host TEXT NOT NULL,
                    workspace_alias TEXT NOT NULL,
                    project_alias TEXT NOT NULL,
                    project_name TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    repository_origin TEXT,
                    branch TEXT,
                    title TEXT NOT NULL,
                    summary TEXT,
                    task_key TEXT,
                    execution_mode TEXT NOT NULL DEFAULT 'normal'
                        CHECK(execution_mode IN ('normal','fast')),
                    status TEXT NOT NULL CHECK(status IN ('ACTIVE','COMPLETED','ARCHIVED')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    execution_state TEXT NOT NULL DEFAULT 'QUEUED',
                    current_stage TEXT NOT NULL DEFAULT 'queued',
                    current_blocker TEXT,
                    last_progress_at TEXT NOT NULL DEFAULT '',
                    codex_running INTEGER NOT NULL DEFAULT 0,
                    turn_id TEXT,
                    retry_required INTEGER NOT NULL DEFAULT 0,
                    failure_stage TEXT,
                    failure_code TEXT,
                    failure_evidence TEXT
                );
                CREATE TABLE IF NOT EXISTS conversation_bindings (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
                    thread_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    project_id TEXT,
                    bound_at TEXT NOT NULL,
                    last_verified_at TEXT NOT NULL,
                    app_server_version TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_conversation_bindings_thread_id
                    ON conversation_bindings(thread_id);
                CREATE TABLE IF NOT EXISTS executions (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
                    issue_id TEXT,
                    execution_ref TEXT,
                    worktree_key TEXT,
                    logical_model TEXT,
                    resolved_model TEXT,
                    stage TEXT NOT NULL DEFAULT 'CLAIMED',
                    acquired_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_item_fingerprints (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
                    fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_work_item_fingerprint
                    ON work_item_fingerprints(fingerprint);
                CREATE TABLE IF NOT EXISTS worktree_leases (
                    worktree_key TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    execution_ref TEXT,
                    stage TEXT NOT NULL,
                    acquired_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS linear_executions (
                    issue_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_indexes (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
                    issue_id TEXT NOT NULL UNIQUE,
                    identifier TEXT NOT NULL,
                    project_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS prepared_executions (
                    prepared_execution_ref TEXT PRIMARY KEY,
                    integrity_hash TEXT NOT NULL,
                    approval_state TEXT NOT NULL CHECK(approval_state IN ('APPROVED')),
                    task_action TEXT NOT NULL CHECK(task_action IN ('create','continue','reopen')),
                    task_ref TEXT,
                    host TEXT NOT NULL,
                    project TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT,
                    prompt TEXT NOT NULL,
                model TEXT NOT NULL,
                    logical_model TEXT NOT NULL DEFAULT '',
                    resolved_executable_model TEXT NOT NULL DEFAULT '',
                    reasoning_effort TEXT NOT NULL,
                    execution_mode TEXT NOT NULL CHECK(execution_mode IN ('normal','fast')),
                    network_access INTEGER NOT NULL DEFAULT 0 CHECK(network_access IN (0,1)),
                    status TEXT NOT NULL CHECK(status IN ('PREPARED','RUNNING','DISPATCHED','FAILED')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resulting_task_id TEXT,
                    resulting_thread_id TEXT,
                    resulting_turn_id TEXT,
                    resulting_execution_ref TEXT
                );
                CREATE TABLE IF NOT EXISTS context_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    execution_id TEXT,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT,
                    timestamp TEXT NOT NULL,
                    prompt_summary TEXT NOT NULL,
                    result_summary TEXT NOT NULL,
                    changed_files TEXT NOT NULL,
                    validation_summary TEXT NOT NULL,
                    blockers TEXT NOT NULL,
                    next_state TEXT NOT NULL,
                    source TEXT NOT NULL,
                    provenance TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_context_checkpoints_task_timestamp
                    ON context_checkpoints(task_id, timestamp DESC);
                CREATE TABLE IF NOT EXISTS context_anchors (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_results (
                    execution_ref TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    turn_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('PASS','BLOCKED')),
                    summary TEXT NOT NULL,
                    changed_files TEXT NOT NULL,
                    validation TEXT NOT NULL,
                    blockers TEXT NOT NULL,
                    next_state TEXT NOT NULL,
                    raw_result TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    writeback_state TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK(writeback_state IN ('PENDING','WRITTEN','FAILED')),
                    writeback_body_hash TEXT,
                    written_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_execution_results_task
                    ON execution_results(task_id, received_at DESC);
                CREATE TABLE IF NOT EXISTS linear_audit_events (
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    event_key TEXT NOT NULL,
                    body_hash TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('PENDING','WRITTEN','FAILED')),
                    retry_required INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(task_id, event_key)
                );
                """
            )
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(tasks)")
            }
            if "task_key" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN task_key TEXT")
            if "execution_mode" not in columns:
                conn.execute(
                    "ALTER TABLE tasks ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'normal'"
                )
            execution_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(executions)")
            }
            if "execution_ref" not in execution_columns:
                conn.execute("ALTER TABLE executions ADD COLUMN execution_ref TEXT")
            if "stage" not in execution_columns:
                conn.execute("ALTER TABLE executions ADD COLUMN stage TEXT NOT NULL DEFAULT 'CLAIMED'")
            if "worktree_key" not in execution_columns:
                conn.execute("ALTER TABLE executions ADD COLUMN worktree_key TEXT")
            if "logical_model" not in execution_columns:
                conn.execute("ALTER TABLE executions ADD COLUMN logical_model TEXT")
            if "resolved_model" not in execution_columns:
                conn.execute("ALTER TABLE executions ADD COLUMN resolved_model TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_executions_ref ON executions(execution_ref)")
            migrations = {
                "execution_state": "ALTER TABLE tasks ADD COLUMN execution_state TEXT NOT NULL DEFAULT 'QUEUED'",
                "current_stage": "ALTER TABLE tasks ADD COLUMN current_stage TEXT NOT NULL DEFAULT 'queued'",
                "current_blocker": "ALTER TABLE tasks ADD COLUMN current_blocker TEXT",
                "last_progress_at": "ALTER TABLE tasks ADD COLUMN last_progress_at TEXT NOT NULL DEFAULT ''",
                "codex_running": "ALTER TABLE tasks ADD COLUMN codex_running INTEGER NOT NULL DEFAULT 0",
                "turn_id": "ALTER TABLE tasks ADD COLUMN turn_id TEXT",
                "retry_required": "ALTER TABLE tasks ADD COLUMN retry_required INTEGER NOT NULL DEFAULT 0",
                "failure_stage": "ALTER TABLE tasks ADD COLUMN failure_stage TEXT",
                "failure_code": "ALTER TABLE tasks ADD COLUMN failure_code TEXT",
                "failure_evidence": "ALTER TABLE tasks ADD COLUMN failure_evidence TEXT",
            }
            for column, statement in migrations.items():
                if column not in columns:
                    conn.execute(statement)
            prepared_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(prepared_executions)")
            }
            if "logical_model" not in prepared_columns:
                conn.execute("ALTER TABLE prepared_executions ADD COLUMN logical_model TEXT NOT NULL DEFAULT ''")
            if "resolved_executable_model" not in prepared_columns:
                conn.execute("ALTER TABLE prepared_executions ADD COLUMN resolved_executable_model TEXT NOT NULL DEFAULT ''")
            if "network_access" not in prepared_columns:
                conn.execute(
                    "ALTER TABLE prepared_executions ADD COLUMN network_access INTEGER NOT NULL DEFAULT 0"
                )
            conn.execute(
                """UPDATE prepared_executions
                   SET logical_model = CASE WHEN logical_model = '' THEN model ELSE logical_model END,
                       resolved_executable_model = CASE WHEN resolved_executable_model = '' THEN model ELSE resolved_executable_model END
                   WHERE logical_model = '' OR resolved_executable_model = ''"""
            )
            conn.execute(
                "UPDATE tasks SET last_progress_at = COALESCE(NULLIF(last_progress_at, ''), updated_at)"
            )
            for task in conn.execute("SELECT * FROM tasks").fetchall():
                fingerprint = self.canonical_work_item_fingerprint(
                    host=task["host"], project_alias=task["project_alias"],
                    cwd=task["cwd"], repository_origin=task["repository_origin"],
                    title=task["title"], summary=task["summary"], task_key=task["task_key"],
                )
                conn.execute(
                    "INSERT OR IGNORE INTO work_item_fingerprints(task_id,fingerprint,created_at) VALUES (?,?,?)",
                    (task["task_id"], fingerprint, task["created_at"]),
                )

    def create_task(
        self,
        *,
        host: str,
        workspace_alias: str,
        project_alias: str,
        project_name: str,
        cwd: str,
        repository_origin: str | None,
        branch: str | None,
        title: str,
        summary: str | None = None,
        task_key: str | None = None,
        execution_mode: str = "normal",
    ) -> TaskRecord:
        if execution_mode not in {"normal", "fast"}:
            raise TaskRegistryError(f"Unsupported execution mode: {execution_mode}")
        title = self._validate_metadata_value(
            "title", title or project_name, MAX_TASK_TITLE_LENGTH
        )
        summary = self._validate_metadata_value(
            "summary", summary, MAX_TASK_SUMMARY_LENGTH
        )
        task_id = "task_" + uuid.uuid4().hex
        stamp = _now()
        row = (
            task_id, host, workspace_alias, project_alias, project_name, cwd,
            repository_origin, branch, title, summary, task_key,
            execution_mode, "ACTIVE", stamp, stamp, "QUEUED", "queued", None,
            stamp, 0, None, 0,
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO tasks
                (task_id,host,workspace_alias,project_alias,project_name,cwd,
                 repository_origin,branch,title,summary,task_key,execution_mode,
                 status,created_at,updated_at,execution_state,current_stage,current_blocker,
                 last_progress_at,codex_running,turn_id,retry_required)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                row,
            )
            fingerprint = self.canonical_work_item_fingerprint(
                host=host, project_alias=project_alias, cwd=cwd,
                repository_origin=repository_origin, title=title,
                summary=summary, task_key=task_key,
            )
            conn.execute(
                "INSERT INTO work_item_fingerprints(task_id,fingerprint,created_at) VALUES (?,?,?)",
                (task_id, fingerprint, stamp),
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> TaskRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise UnknownTaskError(f"Unknown task: {task_id}")
        return TaskRecord(**dict(row))

    def list_tasks(
        self,
        *,
        host: str | None = None,
        project: str | None = None,
        status: str | None = None,
        include_archived: bool = True,
    ) -> list[TaskRecord]:
        query = "SELECT * FROM tasks"
        clauses: list[str] = []
        args: list[str] = []
        if host is not None:
            clauses.append("lower(host) = lower(?)")
            args.append(host)
        if project is not None:
            clauses.append("(lower(project_alias) = lower(?) OR lower(project_name) = lower(?))")
            args.extend((project, project))
        if status is not None:
            clauses.append("status = ?")
            args.append(status.upper())
        elif not include_archived:
            clauses.append("status != 'ARCHIVED'")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, created_at DESC"
        with self._connect() as conn:
            return [TaskRecord(**dict(row)) for row in conn.execute(query, tuple(args))]

    @staticmethod
    def _normalize(value: str | None) -> str:
        normalized = unicodedata.normalize("NFKC", value or "").casefold()
        return " ".join(re.findall(r"[\w]+", normalized, flags=re.UNICODE))

    @classmethod
    def canonical_work_item_fingerprint(
        cls,
        *,
        host: str,
        project_alias: str,
        cwd: str,
        repository_origin: str | None,
        title: str,
        summary: str | None = None,
        task_key: str | None = None,
    ) -> str:
        """Build a deterministic exact-work-item identity without NLP."""
        payload = "\x1f".join((
            cls._normalize(host), cls._normalize(project_alias),
            os.path.realpath(cwd), cls._normalize(repository_origin),
            cls._normalize(title), cls._normalize(summary),
        ))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def record_work_item_fingerprint(self, task_id: str, fingerprint: str) -> None:
        self.get_task(task_id)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO work_item_fingerprints(task_id,fingerprint,created_at)
                   VALUES (?,?,?) ON CONFLICT(task_id) DO UPDATE SET fingerprint=excluded.fingerprint""",
                (task_id, fingerprint, _now()),
            )

    def find_active_work_item(
        self, *, fingerprint: str, exclude_task_id: str | None = None
    ) -> TaskRecord | None:
        query = """SELECT t.* FROM tasks t JOIN work_item_fingerprints f ON f.task_id=t.task_id
                   WHERE f.fingerprint=? AND t.status='ACTIVE'
                   ORDER BY t.updated_at DESC LIMIT 1"""
        args: list[Any] = [fingerprint]
        if exclude_task_id:
            query = query.replace("AND t.status='ACTIVE'", "AND t.status='ACTIVE' AND t.task_id != ?")
            args.append(exclude_task_id)
        with self._connect() as conn:
            row = conn.execute(query, tuple(args)).fetchone()
        return TaskRecord(**dict(row)) if row is not None else None

    @staticmethod
    def worktree_key(*, host: str, cwd: str, repository_origin: str | None) -> str:
        payload = "\x1f".join((host.strip().casefold(), os.path.realpath(cwd),
                               (repository_origin or "").strip().casefold()))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_metadata_value(
        name: str,
        value: str | None,
        maximum: int,
    ) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise TaskRegistryError(f"Task {name} must not be empty")
        if len(cleaned) > maximum:
            raise TaskRegistryError(
                f"Task {name} exceeds maximum length {maximum}"
            )
        return cleaned

    @classmethod
    def _match_score(cls, task: TaskRecord, query: str) -> int:
        needle = cls._normalize(query)
        if not needle:
            return 0
        title = cls._normalize(task.title)
        summary = cls._normalize(task.summary)
        task_key = cls._normalize(task.task_key)
        if needle == title:
            return 1000
        if needle == task_key:
            return 950
        if needle in title:
            return 800
        if needle in task_key:
            return 750
        if needle in summary:
            return 650
        tokens = set(needle.split())
        if not tokens:
            return 0
        title_tokens = set(title.split())
        summary_tokens = set(summary.split())
        key_tokens = set(task_key.split())
        if not tokens.issubset(title_tokens | summary_tokens | key_tokens):
            return 0
        title_hits = len(tokens & title_tokens)
        summary_hits = len(tokens & summary_tokens)
        key_hits = len(tokens & key_tokens)
        hits = title_hits * 5 + key_hits * 4 + summary_hits * 2
        return hits

    def find_tasks(
        self,
        *,
        query: str,
        host: str | None = None,
        project: str | None = None,
        status: str | None = None,
        include_archived: bool = False,
    ) -> TaskSearchResult:
        candidates = self.list_tasks(
            host=host,
            project=project,
            status=status,
            include_archived=include_archived,
        )
        scored = [
            (self._match_score(task, query), task) for task in candidates
        ]
        matches = [(score, task) for score, task in scored if score > 0]
        matches.sort(key=lambda item: (-item[0], item[1].updated_at, item[1].task_id))
        if not matches:
            return TaskSearchResult("NONE", ())
        best_score = matches[0][0]
        best = tuple(task for score, task in matches if score == best_score)
        return TaskSearchResult("UNIQUE" if len(best) == 1 else "AMBIGUOUS", best)

    def update_metadata(
        self,
        task_id: str,
        *,
        title: str | None = None,
        summary: str | None = None,
        task_key: str | None = None,
        execution_mode: str | None = None,
    ) -> TaskRecord:
        self.get_task(task_id)
        if execution_mode is not None and execution_mode not in {"normal", "fast"}:
            raise TaskRegistryError(f"Unsupported execution mode: {execution_mode}")
        title = self._validate_metadata_value(
            "title", title, MAX_TASK_TITLE_LENGTH
        )
        summary = self._validate_metadata_value(
            "summary", summary, MAX_TASK_SUMMARY_LENGTH
        )
        changes: list[str] = []
        values: list[str | None] = []
        for column, value in (
            ("title", title),
            ("summary", summary),
            ("task_key", task_key),
            ("execution_mode", execution_mode),
        ):
            if value is not None:
                changes.append(f"{column} = ?")
                values.append(value)
        if not changes:
            return self.get_task(task_id)
        changes.append("updated_at = ?")
        values.extend((_now(), task_id))
        with self._connect() as conn:
            conn.execute(
                f"UPDATE tasks SET {', '.join(changes)} WHERE task_id = ?",
                tuple(values),
            )
        refreshed = self.get_task(task_id)
        self.record_work_item_fingerprint(
            task_id,
            self.canonical_work_item_fingerprint(
                host=refreshed.host, project_alias=refreshed.project_alias,
                cwd=refreshed.cwd, repository_origin=refreshed.repository_origin,
                title=refreshed.title, summary=refreshed.summary,
                task_key=refreshed.task_key,
            ),
        )
        return refreshed

    def get_binding(self, task_id: str) -> ConversationBinding | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_bindings WHERE task_id = ?", (task_id,)
            ).fetchone()
        return ConversationBinding(**dict(row)) if row is not None else None

    def get_binding_by_thread(self, thread_id: str) -> ConversationBinding | None:
        """Return the canonical task binding for an exact thread, if any."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_bindings WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return ConversationBinding(**dict(row)) if row is not None else None

    def adopt_task(
        self,
        *,
        host: str,
        workspace_alias: str,
        project_alias: str,
        project_name: str,
        cwd: str,
        repository_origin: str | None,
        branch: str | None,
        title: str,
        summary: str | None,
        task_key: str | None,
        execution_mode: str,
        thread_id: str,
        session_id: str,
        project_id: str | None,
        app_server_version: str | None,
    ) -> tuple[TaskRecord, ConversationBinding]:
        """Atomically create an ACTIVE task and bind one verified conversation."""
        if execution_mode not in {"normal", "fast"}:
            raise TaskRegistryError(f"Unsupported execution mode: {execution_mode}")
        title = self._validate_metadata_value(
            "title", title or project_name, MAX_TASK_TITLE_LENGTH
        )
        summary = self._validate_metadata_value(
            "summary", summary, MAX_TASK_SUMMARY_LENGTH
        )
        if not thread_id or not session_id:
            raise TaskRegistryError("Adopted conversation requires thread and session IDs")
        task_id = "task_" + uuid.uuid4().hex
        stamp = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT task_id FROM conversation_bindings WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if existing is not None:
                raise TaskRegistryError(
                    f"Thread {thread_id} is already bound to task {existing['task_id']}"
                )
            conn.execute(
                """INSERT INTO tasks
                (task_id,host,workspace_alias,project_alias,project_name,cwd,
                 repository_origin,branch,title,summary,task_key,execution_mode,
                 status,created_at,updated_at,execution_state,current_stage,current_blocker,
                 last_progress_at,codex_running,turn_id,retry_required)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id, host, workspace_alias, project_alias, project_name, cwd,
                    repository_origin, branch, title, summary, task_key,
                    execution_mode, "ACTIVE", stamp, stamp, "QUEUED", "queued", None,
                    stamp, 0, None, 0,
                ),
            )
            conn.execute(
                """INSERT INTO conversation_bindings
                (task_id,thread_id,session_id,project_id,bound_at,last_verified_at,app_server_version)
                VALUES (?,?,?,?,?,?,?)""",
                (task_id, thread_id, session_id, project_id, stamp, stamp, app_server_version),
            )
            fingerprint = self.canonical_work_item_fingerprint(
                host=host, project_alias=project_alias, cwd=cwd,
                repository_origin=repository_origin, title=title,
                summary=summary, task_key=task_key,
            )
            conn.execute(
                "INSERT INTO work_item_fingerprints(task_id,fingerprint,created_at) VALUES (?,?,?)",
                (task_id, fingerprint, stamp),
            )
        task = self.get_task(task_id)
        binding = self.get_binding(task_id)
        assert binding is not None
        return task, binding

    def bind_conversation(
        self,
        *,
        task_id: str,
        thread_id: str,
        session_id: str,
        project_id: str | None,
        app_server_version: str | None,
        verified_at: str | None = None,
    ) -> ConversationBinding:
        stamp = verified_at or _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone() is None:
                raise UnknownTaskError(f"Unknown task: {task_id}")
            existing = conn.execute(
                "SELECT * FROM conversation_bindings WHERE task_id = ?", (task_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["thread_id"] != thread_id
                    or existing["session_id"] != session_id
                    or existing["project_id"] != project_id
                ):
                    raise TaskRegistryError(
                        f"Task {task_id} already has a different conversation binding")
                conn.execute(
                    "UPDATE conversation_bindings SET last_verified_at = ? WHERE task_id = ?",
                    (stamp, task_id),
                )
            else:
                try:
                    conn.execute(
                        """INSERT INTO conversation_bindings
                        (task_id,thread_id,session_id,project_id,bound_at,last_verified_at,app_server_version)
                        VALUES (?,?,?,?,?,?,?)""",
                        (task_id, thread_id, session_id, project_id, stamp, stamp, app_server_version),
                    )
                except sqlite3.IntegrityError as exc:
                    raise TaskRegistryError(
                        f"Thread {thread_id} is already bound to another task"
                    ) from exc
        binding = self.get_binding(task_id)
        assert binding is not None
        return binding

    def mark_verified(self, task_id: str, *, app_server_version: str | None = None) -> None:
        with self._connect() as conn:
            stamp = _now()
            if app_server_version is None:
                conn.execute(
                    "UPDATE conversation_bindings SET last_verified_at = ? WHERE task_id = ?",
                    (stamp, task_id),
                )
            else:
                conn.execute(
                    """UPDATE conversation_bindings
                    SET last_verified_at = ?, app_server_version = ? WHERE task_id = ?""",
                    (stamp, app_server_version, task_id),
                )

    def record_linear_execution(self, issue_id: str, task_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO linear_executions(issue_id,task_id,created_at) VALUES (?,?,?)",
                (issue_id, task_id, _now()),
            )

    def last_linear_execution(self, task_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT issue_id FROM linear_executions
                WHERE task_id = ? ORDER BY created_at DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
        return str(row["issue_id"]) if row is not None else None

    def get_task_index(self, task_id: str) -> TaskIndexRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_indexes WHERE task_id = ?", (task_id,)
            ).fetchone()
        return TaskIndexRecord(**dict(row)) if row is not None else None

    def record_task_index(
        self,
        *,
        task_id: str,
        issue_id: str,
        identifier: str,
        project_id: str | None,
    ) -> TaskIndexRecord:
        self.get_task(task_id)
        stamp = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM task_indexes WHERE task_id = ?", (task_id,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO task_indexes
                    (task_id,issue_id,identifier,project_id,created_at,updated_at)
                    VALUES (?,?,?,?,?,?)""",
                    (task_id, issue_id, identifier, project_id, stamp, stamp),
                )
            elif existing["issue_id"] != issue_id:
                raise TaskRegistryError(
                    f"Task {task_id} already has a different Linear task index"
                )
            else:
                conn.execute(
                    """UPDATE task_indexes SET identifier = ?, project_id = ?, updated_at = ?
                    WHERE task_id = ?""",
                    (identifier, project_id, stamp, task_id),
                )
        result = self.get_task_index(task_id)
        assert result is not None
        return result

    @staticmethod
    def _prepared_payload(
        *,
        task_action: str,
        task_ref: str | None,
        host: str,
        project: str,
        title: str,
        summary: str | None,
        prompt: str,
        model: str,
        reasoning_effort: str,
        execution_mode: str,
        logical_model: str | None = None,
        resolved_executable_model: str | None = None,
        network_access: bool = False,
    ) -> dict[str, Any]:
        return {
            "approval_state": "APPROVED",
            "task_action": task_action,
            "task_ref": task_ref,
            "host": host,
            "project": project,
            "title": title,
            "summary": summary,
            "prompt": prompt,
            "model": model,
            "logical_model": logical_model if logical_model is not None else model,
            "resolved_executable_model": resolved_executable_model if resolved_executable_model is not None else model,
            "reasoning_effort": reasoning_effort,
            "execution_mode": execution_mode,
            "network_access": network_access,
        }

    @staticmethod
    def _prepared_hash(payload: dict[str, Any]) -> str:
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def create_prepared_execution(
        self,
        *,
        task_action: str,
        task_ref: str | None,
        host: str,
        project: str,
        title: str,
        summary: str | None,
        prompt: str,
        model: str,
        reasoning_effort: str,
        execution_mode: str,
        logical_model: str | None = None,
        resolved_executable_model: str | None = None,
        network_access: bool = False,
    ) -> PreparedExecutionRecord:
        if task_action not in {"create", "continue", "reopen"}:
            raise TaskRegistryError(f"Unsupported prepared task action: {task_action}")
        if execution_mode not in {"normal", "fast"}:
            raise TaskRegistryError(f"Unsupported execution mode: {execution_mode}")
        if not isinstance(network_access, bool):
            raise TaskRegistryError("network_access must be a boolean")
        values = {
            "host": host, "project": project, "title": title,
            "prompt": prompt, "model": model,
            "reasoning_effort": reasoning_effort,
        }
        for name, value in values.items():
            if not isinstance(value, str) or not value.strip():
                raise TaskRegistryError(f"Prepared execution {name} is required")
        summary = self._validate_metadata_value("prepared summary", summary, MAX_TASK_SUMMARY_LENGTH)
        payload = self._prepared_payload(
            task_action=task_action, task_ref=task_ref, host=host.strip(),
            project=project.strip(), title=title.strip(), summary=summary,
            prompt=prompt.strip(), model=model.strip(),
            logical_model=(logical_model or model).strip(),
            resolved_executable_model=(resolved_executable_model or model).strip(),
            reasoning_effort=reasoning_effort.strip(), execution_mode=execution_mode,
            network_access=network_access,
        )
        stamp = _now()
        record = PreparedExecutionRecord(
            prepared_execution_ref="prepared_" + uuid.uuid4().hex,
            integrity_hash=self._prepared_hash(payload),
            approval_state="APPROVED",
            status="PREPARED",
            created_at=stamp,
            updated_at=stamp,
            resulting_task_id=None,
            resulting_thread_id=None,
            resulting_turn_id=None,
            resulting_execution_ref=None,
            **{key: payload[key] for key in (
                "task_action", "task_ref", "host", "project", "title", "summary",
                "prompt", "model", "reasoning_effort", "execution_mode",
                "logical_model", "resolved_executable_model", "network_access",
            )},
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO prepared_executions
                (prepared_execution_ref,integrity_hash,approval_state,task_action,task_ref,
                 host,project,title,summary,prompt,model,logical_model,resolved_executable_model,reasoning_effort,execution_mode,
                network_access,
                 status,created_at,updated_at,resulting_task_id,resulting_thread_id,
                 resulting_turn_id,resulting_execution_ref)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                tuple(getattr(record, field.name) for field in dataclasses.fields(record)),
            )
        return record

    def get_prepared_execution(self, prepared_execution_ref: str) -> PreparedExecutionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM prepared_executions WHERE prepared_execution_ref = ?",
                (prepared_execution_ref,),
            ).fetchone()
        return PreparedExecutionRecord(**dict(row)) if row is not None else None

    def get_prepared_execution_for_execution(
        self, execution_ref: str
    ) -> PreparedExecutionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM prepared_executions
                   WHERE resulting_execution_ref=?
                   ORDER BY updated_at DESC LIMIT 1""",
                (execution_ref,),
            ).fetchone()
        return PreparedExecutionRecord(**dict(row)) if row is not None else None

    def get_prepared_execution_for_task(
        self, task_id: str
    ) -> PreparedExecutionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM prepared_executions
                   WHERE resulting_task_id=? OR task_ref=?
                   ORDER BY updated_at DESC LIMIT 1""",
                (task_id, task_id),
            ).fetchone()
        return PreparedExecutionRecord(**dict(row)) if row is not None else None

    def verify_prepared_execution(self, prepared_execution_ref: str) -> PreparedExecutionRecord:
        record = self.get_prepared_execution(prepared_execution_ref)
        if record is None:
            raise TaskRegistryError(f"Unknown prepared execution: {prepared_execution_ref}")
        payload = self._prepared_payload(
            task_action=record.task_action, task_ref=record.task_ref, host=record.host,
            project=record.project, title=record.title, summary=record.summary,
            prompt=record.prompt, model=record.model,
            logical_model=record.logical_model,
            resolved_executable_model=record.resolved_executable_model,
            reasoning_effort=record.reasoning_effort,
            execution_mode=record.execution_mode,
            network_access=bool(record.network_access),
        )
        valid_hash = self._prepared_hash(payload) == record.integrity_hash
        if not valid_hash and not bool(record.network_access):
            # Existing preparations predate the explicit capability field. Keep
            # them safely disabled while allowing their original sealed hash.
            legacy_payload = dict(payload)
            legacy_payload.pop("network_access", None)
            valid_hash = self._prepared_hash(legacy_payload) == record.integrity_hash
        if record.approval_state != "APPROVED" or not valid_hash:
            raise TaskRegistryError(
                f"PREPARED_EXECUTION_INTEGRITY=FAIL: {prepared_execution_ref}"
            )
        return record

    def mark_prepared_execution_running(self, prepared_execution_ref: str) -> PreparedExecutionRecord:
        record = self.verify_prepared_execution(prepared_execution_ref)
        if record.status == "DISPATCHED":
            return record
        if record.status == "FAILED" and any(
            value is not None for value in (
                record.resulting_task_id,
                record.resulting_thread_id,
                record.resulting_turn_id,
                record.resulting_execution_ref,
            )
        ):
            raise TaskRegistryError(
                f"Prepared execution is not retryable: {prepared_execution_ref}"
            )
        if record.status not in {"PREPARED", "FAILED"}:
            raise TaskRegistryError(
                f"Prepared execution is not dispatchable: {prepared_execution_ref}"
            )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                """UPDATE prepared_executions SET status='RUNNING', updated_at=?
                   WHERE prepared_execution_ref=? AND status IN ('PREPARED','FAILED')""",
                (_now(), prepared_execution_ref),
            ).rowcount
            if updated != 1:
                current = conn.execute(
                    "SELECT * FROM prepared_executions WHERE prepared_execution_ref=?",
                    (prepared_execution_ref,),
                ).fetchone()
                if current is not None and current["status"] == "DISPATCHED":
                    return PreparedExecutionRecord(**dict(current))
                raise TaskRegistryError(
                    f"Prepared execution is no longer dispatchable: {prepared_execution_ref}"
                )
        return self.get_prepared_execution(prepared_execution_ref)  # type: ignore[return-value]

    def restore_prepared_execution(self, prepared_execution_ref: str) -> PreparedExecutionRecord:
        record = self.verify_prepared_execution(prepared_execution_ref)
        if record.status == "DISPATCHED":
            return record
        with self._connect() as conn:
            conn.execute(
                """UPDATE prepared_executions SET status='PREPARED', updated_at=?,
                   resulting_task_id=NULL, resulting_thread_id=NULL,
                   resulting_turn_id=NULL, resulting_execution_ref=NULL
                   WHERE prepared_execution_ref=?""",
                (_now(), prepared_execution_ref),
            )
        return self.get_prepared_execution(prepared_execution_ref)  # type: ignore[return-value]

    def complete_prepared_execution(
        self,
        prepared_execution_ref: str,
        *,
        task_id: str,
        thread_id: str,
        turn_id: str,
        execution_ref: str,
    ) -> PreparedExecutionRecord:
        self.verify_prepared_execution(prepared_execution_ref)
        for name, value in {
            "task_id": task_id, "thread_id": thread_id,
            "turn_id": turn_id, "execution_ref": execution_ref,
        }.items():
            if not isinstance(value, str) or not value.strip():
                raise TaskRegistryError(f"Prepared execution result {name} is required")
        with self._connect() as conn:
            conn.execute(
                """UPDATE prepared_executions SET status='DISPATCHED', updated_at=?,
                   resulting_task_id=?, resulting_thread_id=?, resulting_turn_id=?,
                   resulting_execution_ref=? WHERE prepared_execution_ref=?""",
                (_now(), task_id, thread_id, turn_id, execution_ref, prepared_execution_ref),
            )
        return self.get_prepared_execution(prepared_execution_ref)  # type: ignore[return-value]

    def fail_prepared_execution(self, prepared_execution_ref: str) -> PreparedExecutionRecord:
        with self._connect() as conn:
            conn.execute(
                "UPDATE prepared_executions SET status='FAILED', updated_at=? WHERE prepared_execution_ref=?",
                (_now(), prepared_execution_ref),
            )
        record = self.get_prepared_execution(prepared_execution_ref)
        if record is None:
            raise TaskRegistryError(f"Unknown prepared execution: {prepared_execution_ref}")
        return record

    def save_context_checkpoint(
        self,
        *,
        task_id: str,
        execution_id: str | None,
        thread_id: str,
        turn_id: str | None,
        prompt_summary: str,
        result_summary: str,
        changed_files: str,
        validation_summary: str,
        blockers: str,
        next_state: str,
        source: str,
        provenance: str,
        checkpoint_id: str | None = None,
        timestamp: str | None = None,
    ) -> ContextCheckpoint:
        """Persist bounded recovery metadata, never a transcript or stdout dump."""
        self.get_task(task_id)
        binding = self.get_binding(task_id)
        if binding is None or binding.thread_id != thread_id:
            raise TaskRegistryError("Checkpoint thread does not match task conversation binding")
        values = {
            "prompt_summary": prompt_summary,
            "result_summary": result_summary,
            "changed_files": changed_files,
            "validation_summary": validation_summary,
            "blockers": blockers,
            "next_state": next_state,
            "source": source,
            "provenance": provenance,
        }
        for name, value in values.items():
            if not isinstance(value, str) or len(value) > 4000:
                raise TaskRegistryError(f"Checkpoint {name} must be a string <= 4000 characters")
        record = ContextCheckpoint(
            checkpoint_id=checkpoint_id or "checkpoint_" + uuid.uuid4().hex,
            task_id=task_id,
            execution_id=execution_id,
            thread_id=thread_id,
            turn_id=turn_id,
            timestamp=timestamp or _now(),
            **values,
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO context_checkpoints
                (checkpoint_id,task_id,execution_id,thread_id,turn_id,timestamp,
                 prompt_summary,result_summary,changed_files,validation_summary,
                 blockers,next_state,source,provenance)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                dataclasses.astuple(record),
            )
        return record

    def latest_context_checkpoint(self, task_id: str) -> ContextCheckpoint | None:
        self.get_task(task_id)
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM context_checkpoints
                WHERE task_id = ? ORDER BY timestamp DESC, checkpoint_id DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
        return ContextCheckpoint(**dict(row)) if row is not None else None

    def set_context_anchor(
        self,
        *,
        task_id: str,
        thread_id: str,
        turn_id: str,
        source: str = "APP_SERVER_NATIVE",
    ) -> ContextAnchor:
        """Persist one exact turn selector for task-specific live context."""
        self.get_task(task_id)
        binding = self.get_binding(task_id)
        if binding is None or binding.thread_id != thread_id:
            raise TaskRegistryError("Context anchor thread does not match task binding")
        for name, value in (("thread_id", thread_id), ("turn_id", turn_id), ("source", source)):
            if not isinstance(value, str) or not value.strip() or len(value) > 512:
                raise TaskRegistryError(f"Context anchor {name} is invalid")
        record = ContextAnchor(
            task_id=task_id,
            thread_id=thread_id,
            turn_id=turn_id,
            source=source,
            created_at=_now(),
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO context_anchors(task_id,thread_id,turn_id,source,created_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET
                    thread_id=excluded.thread_id,
                    turn_id=excluded.turn_id,
                    source=excluded.source,
                    created_at=excluded.created_at""",
                dataclasses.astuple(record),
            )
        return self.get_context_anchor(task_id)  # type: ignore[return-value]

    def get_context_anchor(self, task_id: str) -> ContextAnchor | None:
        self.get_task(task_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM context_anchors WHERE task_id = ?", (task_id,)
            ).fetchone()
        return ContextAnchor(**dict(row)) if row is not None else None

    def set_status(self, task_id: str, status: str) -> TaskRecord:
        if status not in {"ACTIVE", "COMPLETED", "ARCHIVED"}:
            raise TaskRegistryError(f"Unsupported task status: {status}")
        task = self.get_task(task_id)
        if task.status == "ARCHIVED" and status == "ACTIVE":
            # Reopening an archived task is an explicit caller action.  This
            # method is only called by the explicit TASK_ACTION=reopen path.
            pass
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                (status, _now(), task_id),
            )
        return self.get_task(task_id)

    def set_execution_state(
        self,
        task_id: str,
        state: str,
        *,
        current_stage: str | None | object = _UNSET,
        current_blocker: str | None | object = _UNSET,
        codex_running: bool | None | object = _UNSET,
        turn_id: str | None | object = _UNSET,
        retry_required: bool | None | object = _UNSET,
        failure_stage: str | None | object = _UNSET,
        failure_code: str | None | object = _UNSET,
        failure_evidence: str | None | object = _UNSET,
    ) -> TaskRecord:
        allowed = {
            "QUEUED", "CLAIMED", "DISPATCHING", "TURN_STARTED", "CODEX_RUNNING",
            "RESULT_RECEIVED", "RESULT_PARSE", "LINEAR_WRITEBACK",
            "TRANSPORT_UNCERTAIN", "CANCEL_REQUESTED", "CANCELLATION_PENDING",
            "BLOCKED", "RECOVERY_REQUIRED", "IN_REVIEW", "COMPLETED", "CANCELLED", "STOPPED",
        }
        if state not in allowed:
            raise TaskRegistryError(f"Unsupported execution state: {state}")
        task = self.get_task(task_id)
        running = task.codex_running if codex_running is _UNSET else bool(codex_running)
        if state == "CODEX_RUNNING":
            running = True
            candidate_turn = task.turn_id if turn_id is _UNSET else turn_id
            if not isinstance(candidate_turn, str) or not candidate_turn.strip():
                raise TaskRegistryError("CODEX_RUNNING requires an exact turn_id")
        if state in {
            "BLOCKED", "RECOVERY_REQUIRED", "IN_REVIEW",
            "COMPLETED", "CANCELLED", "STOPPED",
        }:
            running = False
        effective_turn = task.turn_id if turn_id is _UNSET else turn_id
        if running and not effective_turn:
            raise TaskRegistryError("CODEX_RUNNING requires an exact turn_id")
        stage = task.current_stage if current_stage is _UNSET else current_stage
        blocker = task.current_blocker if current_blocker is _UNSET else current_blocker
        retry = task.retry_required if retry_required is _UNSET else bool(retry_required)
        effective_failure_stage = (
            task.failure_stage if failure_stage is _UNSET else failure_stage
        )
        effective_failure_code = (
            task.failure_code if failure_code is _UNSET else failure_code
        )
        effective_failure_evidence = (
            task.failure_evidence if failure_evidence is _UNSET else failure_evidence
        )
        if state in {"CLAIMED", "DISPATCHING", "TURN_STARTED", "CODEX_RUNNING"}:
            # A new/continued turn starts a fresh failure window; stale
            # transport evidence from an earlier execution must not project
            # onto the active turn.
            if failure_stage is _UNSET:
                effective_failure_stage = None
            if failure_code is _UNSET:
                effective_failure_code = None
            if failure_evidence is _UNSET:
                effective_failure_evidence = None
        changed = (
            task.execution_state != state or task.current_stage != stage
            or task.current_blocker != blocker or task.codex_running != running
            or task.turn_id != effective_turn or task.retry_required != retry
            or task.failure_stage != effective_failure_stage
            or task.failure_code != effective_failure_code
            or task.failure_evidence != effective_failure_evidence
        )
        stamp = _now() if changed else task.last_progress_at
        with self._connect() as conn:
            conn.execute(
                """UPDATE tasks SET execution_state=?, current_stage=?, current_blocker=?,
                   last_progress_at=?, codex_running=?, turn_id=?, retry_required=?,
                   failure_stage=?, failure_code=?, failure_evidence=?, updated_at=?
                   WHERE task_id=?""",
                (state, stage, blocker, stamp, int(running), effective_turn, int(retry),
                 effective_failure_stage, effective_failure_code, effective_failure_evidence,
                 _now() if changed else task.updated_at, task_id),
            )
            conn.execute(
                "UPDATE executions SET stage=? WHERE task_id=?",
                (str(stage or state), task_id),
            )
        return self.get_task(task_id)

    def request_cancellation(self, execution_ref: str) -> TaskRecord:
        """Persist cancellation intent before contacting the provider."""
        active = self.get_active_execution(execution_ref)
        if active is None:
            raise TaskRegistryError(f"Unknown or inactive execution: {execution_ref}")
        task = self.get_task(active["task_id"])
        if task.execution_state == "CANCELLED":
            return task
        if task.execution_state in {"CANCEL_REQUESTED", "CANCELLATION_PENDING"}:
            return task
        if task.execution_state not in {
            "CLAIMED", "DISPATCHING", "TURN_STARTED", "CODEX_RUNNING",
            "TRANSPORT_UNCERTAIN",
        }:
            raise TaskRegistryError(f"Execution is not active: {execution_ref}")
        return self.set_execution_state(
            task.task_id,
            "CANCEL_REQUESTED",
            current_stage="CANCEL_REQUESTED",
            current_blocker="cancellation requested by CLINX",
            codex_running=task.codex_running,
            retry_required=False,
            failure_stage=None,
            failure_code=None,
            failure_evidence=None,
        )

    def mark_cancellation_pending(
        self, execution_ref: str, *, evidence: str | None = None
    ) -> TaskRecord:
        active = self.get_active_execution(execution_ref)
        if active is None:
            raise TaskRegistryError(f"Unknown or inactive execution: {execution_ref}")
        return self.set_execution_state(
            active["task_id"],
            "CANCELLATION_PENDING",
            current_stage="CANCELLATION_PENDING",
            current_blocker="provider cancellation could not be confirmed",
            codex_running=True,
            retry_required=True,
            failure_stage="cancel",
            failure_code="PROVIDER_UNAVAILABLE",
            failure_evidence=(evidence or "provider cancellation unavailable")[:4000],
        )

    def finalize_cancellation(self, execution_ref: str) -> TaskRecord:
        active = self.get_active_execution(execution_ref)
        if active is None:
            raise TaskRegistryError(f"Unknown or inactive execution: {execution_ref}")
        task = self.set_execution_state(
            active["task_id"],
            "CANCELLED",
            current_stage="CANCELLED",
            current_blocker="cancelled by CLINX",
            codex_running=False,
            retry_required=False,
            failure_stage=None,
            failure_code=None,
            failure_evidence=None,
        )
        # Keep the terminal identity for idempotent repeated cancellation,
        # while releasing only the mutating worktree lease.
        with self._connect() as conn:
            conn.execute(
                "UPDATE executions SET stage='CANCELLED' WHERE task_id=? AND execution_ref=?",
                (task.task_id, execution_ref),
            )
            conn.execute(
                "DELETE FROM worktree_leases WHERE task_id=? AND execution_ref=?",
                (task.task_id, execution_ref),
            )
        return task

    def reconcile_terminal(
        self,
        execution_ref: str,
        state: str,
        *,
        failure_stage: str | None = None,
        failure_code: str | None = None,
        evidence: str | None = None,
        retry_required: bool | None = None,
    ) -> TaskRecord:
        """Persist authoritative terminal evidence and release its lease."""
        if state not in {"COMPLETED", "RECOVERY_REQUIRED", "BLOCKED", "CANCELLED"}:
            raise TaskRegistryError(f"Unsupported terminal reconciliation state: {state}")
        active = self.get_active_execution(execution_ref)
        if active is None:
            raise TaskRegistryError(f"Unknown or inactive execution: {execution_ref}")
        task = self.set_execution_state(
            active["task_id"], state,
            current_stage=state,
            current_blocker=(evidence or None) if state != "COMPLETED" else None,
            codex_running=False,
            retry_required=(state == "RECOVERY_REQUIRED") if retry_required is None else retry_required,
            failure_stage=failure_stage,
            failure_code=failure_code,
            failure_evidence=evidence,
        )
        self.release_execution(task.task_id, execution_ref)
        return task

    def get_execution_result(self, execution_ref: str) -> ExecutionResultRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM execution_results WHERE execution_ref = ?",
                (execution_ref,),
            ).fetchone()
        return ExecutionResultRecord(**dict(row)) if row is not None else None

    def latest_execution_result(self, task_id: str) -> ExecutionResultRecord | None:
        self.get_task(task_id)
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM execution_results
                   WHERE task_id = ? ORDER BY received_at DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
        return ExecutionResultRecord(**dict(row)) if row is not None else None

    def record_execution_result(
        self,
        *,
        execution_ref: str,
        task_id: str,
        turn_id: str,
        status: str,
        summary: str,
        changed_files: str,
        validation: str,
        blockers: str,
        next_state: str,
        raw_result: str,
    ) -> ExecutionResultRecord:
        """Persist one exact turn result without allowing cross-task ownership."""
        task = self.get_task(task_id)
        binding = self.get_binding(task_id)
        if binding is None:
            raise TaskRegistryError(f"Task {task_id} has no conversation binding")
        if not execution_ref.strip() or not turn_id.strip():
            raise TaskRegistryError("execution_ref and turn_id are required")
        if task.turn_id is not None and task.turn_id != turn_id:
            raise TaskRegistryError(
                f"Execution turn {turn_id} does not own task {task_id}; expected {task.turn_id}"
            )
        if status not in {"PASS", "BLOCKED"}:
            raise TaskRegistryError(f"Unsupported execution result status: {status}")
        fields = {
            "summary": summary, "changed_files": changed_files,
            "validation": validation, "blockers": blockers,
            "next_state": next_state, "raw_result": raw_result,
        }
        for name, value in fields.items():
            if not isinstance(value, str) or len(value) > 16000:
                raise TaskRegistryError(f"Execution result {name} must be a string <= 16000 characters")
        existing = self.get_execution_result(execution_ref)
        if existing is not None:
            if existing.task_id != task.task_id or existing.turn_id != turn_id:
                raise TaskRegistryError(
                    f"Execution ref {execution_ref} is owned by another task or turn"
                )
            if existing.raw_result != raw_result:
                raise TaskRegistryError(
                    f"Execution ref {execution_ref} already has a different result"
                )
            return existing
        stamp = _now()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO execution_results
                (execution_ref,task_id,turn_id,status,summary,changed_files,validation,
                 blockers,next_state,raw_result,received_at,writeback_state,
                 writeback_body_hash,written_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,'PENDING',NULL,NULL)""",
                (execution_ref, task_id, turn_id, status, summary, changed_files,
                 validation, blockers, next_state, raw_result, stamp),
            )
        return self.get_execution_result(execution_ref)  # type: ignore[return-value]

    def mark_execution_result_writeback(
        self,
        execution_ref: str,
        *,
        state: str,
        body_hash: str | None = None,
    ) -> ExecutionResultRecord:
        if state not in {"PENDING", "WRITTEN", "FAILED"}:
            raise TaskRegistryError(f"Unsupported writeback state: {state}")
        existing = self.get_execution_result(execution_ref)
        if existing is None:
            raise TaskRegistryError(f"Unknown execution ref: {execution_ref}")
        stamp = _now() if state == "WRITTEN" else existing.written_at
        with self._connect() as conn:
            conn.execute(
                """UPDATE execution_results SET writeback_state=?,
                   writeback_body_hash=?, written_at=? WHERE execution_ref=?""",
                (state, body_hash, stamp, execution_ref),
            )
        return self.get_execution_result(execution_ref)  # type: ignore[return-value]

    def reset_execution(self, task_id: str) -> TaskRecord:
        return self.set_execution_state(
            task_id, "QUEUED", current_stage="queued", current_blocker=None,
            codex_running=False, turn_id=None, retry_required=False,
        )

    def get_active_execution(self, execution_ref: str) -> dict[str, Any] | None:
        if not isinstance(execution_ref, str) or not execution_ref.strip():
            return None
        with self._connect() as conn:
            row = conn.execute(
                """SELECT e.*, t.host, t.cwd, t.repository_origin, t.execution_state,
                          t.current_stage, t.codex_running
                   FROM executions e JOIN tasks t ON t.task_id=e.task_id
                   WHERE e.execution_ref=?
                     AND e.stage NOT IN ('CANCELLED','COMPLETED','IN_REVIEW','BLOCKED','RECOVERY_REQUIRED','STOPPED')""",
                (execution_ref,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_execution_record(self, execution_ref: str) -> dict[str, Any] | None:
        """Read one execution identity, including a retained terminal cancel."""
        if not isinstance(execution_ref, str) or not execution_ref.strip():
            return None
        with self._connect() as conn:
            row = conn.execute(
                """SELECT e.*, t.host, t.cwd, t.repository_origin, t.execution_state,
                          t.current_stage, t.codex_running
                   FROM executions e JOIN tasks t ON t.task_id=e.task_id
                   WHERE e.execution_ref=?""",
                (execution_ref,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_latest_execution_for_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM executions WHERE task_id=? ORDER BY acquired_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def set_execution_models(self, execution_ref: str, *, logical_model: str | None,
                             resolved_model: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE executions SET logical_model=?, resolved_model=? WHERE execution_ref=?",
                (logical_model, resolved_model, execution_ref),
            )

    def get_execution_models(self, execution_ref: str) -> tuple[str | None, str | None]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT logical_model,resolved_model FROM executions WHERE execution_ref=?",
                (execution_ref,),
            ).fetchone()
        return (row["logical_model"], row["resolved_model"]) if row is not None else (None, None)

    def active_worktree_conflict(
        self, *, host: str, cwd: str, repository_origin: str | None,
        exclude_task_id: str | None = None,
    ) -> dict[str, Any] | None:
        key = self.worktree_key(host=host, cwd=cwd, repository_origin=repository_origin)
        active_states = (
            "CLAIMED", "DISPATCHING", "TURN_STARTED", "CODEX_RUNNING",
            "TRANSPORT_UNCERTAIN", "CANCEL_REQUESTED", "CANCELLATION_PENDING",
        )
        with self._connect() as conn:
            row = conn.execute(
                """SELECT l.*, t.host, t.cwd, t.repository_origin, t.execution_state,
                          t.current_stage, t.codex_running
                   FROM worktree_leases l JOIN tasks t ON t.task_id=l.task_id
                   WHERE l.worktree_key=? AND (? IS NULL OR l.task_id != ?)""",
                (key, exclude_task_id, exclude_task_id),
            ).fetchone()
            if row is None:
                # Durable task state is the fallback for a process that exited
                # after turn/start but before its lease row was flushed.
                candidates = conn.execute(
                    """SELECT * FROM tasks WHERE status='ACTIVE' AND lower(host)=lower(?)
                       AND (? IS NULL OR task_id != ?)""",
                    (host, exclude_task_id, exclude_task_id),
                ).fetchall()
                for candidate in candidates:
                    candidate_key = self.worktree_key(
                        host=candidate["host"], cwd=candidate["cwd"],
                        repository_origin=candidate["repository_origin"],
                    )
                    if candidate_key == key and (
                        candidate["execution_state"] in active_states or candidate["codex_running"]
                    ):
                        return {
                            "active_task_id": candidate["task_id"],
                            "active_execution_ref": None,
                            "stage": candidate["current_stage"] if candidate["current_stage"] not in {None, "queued"}
                            else candidate["execution_state"],
                            "worktree_key": key,
                        }
                return None
        return {
            "active_task_id": row["task_id"],
            "active_execution_ref": row["execution_ref"],
            "stage": row["current_stage"] if row["stage"] == "CLAIMED"
            and row["current_stage"] not in {None, "queued"}
            else row["stage"] or row["current_stage"],
            "worktree_key": key,
        }

    def cancel_execution(self, execution_ref: str) -> TaskRecord:
        self.request_cancellation(execution_ref)
        return self.finalize_cancellation(execution_ref)

    def release_execution(self, task_id: str, execution_ref: str | None = None) -> None:
        with self._connect() as conn:
            if execution_ref:
                row = conn.execute(
                    "SELECT worktree_key FROM executions WHERE task_id=? AND execution_ref=?",
                    (task_id, execution_ref),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT worktree_key FROM executions WHERE task_id=?", (task_id,)
                ).fetchone()
            conn.execute(
                "DELETE FROM executions WHERE task_id=?" + (" AND execution_ref=?" if execution_ref else ""),
                (task_id, execution_ref) if execution_ref else (task_id,),
            )
            if row is not None:
                conn.execute("DELETE FROM worktree_leases WHERE worktree_key=?", (row["worktree_key"],))

    def get_linear_audit(self, task_id: str) -> LinearAuditRecord | None:
        self.get_task(task_id)
        with self._connect() as conn:
            row = conn.execute(
                """SELECT task_id,
                          CASE WHEN SUM(state='FAILED') > 0 THEN 'DEGRADED'
                               WHEN SUM(state='WRITTEN') > 0 THEN 'PASS'
                               ELSE 'PENDING' END AS sync_state,
                          MAX(retry_required) AS retry_required,
                          (SELECT last_error FROM linear_audit_events x
                           WHERE x.task_id=linear_audit_events.task_id
                           ORDER BY updated_at DESC LIMIT 1) AS last_error,
                          MAX(updated_at) AS updated_at
                   FROM linear_audit_events WHERE task_id=? GROUP BY task_id""",
                (task_id,),
            ).fetchone()
        return LinearAuditRecord(**dict(row)) if row is not None else None

    def get_linear_event(self, task_id: str, event_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM linear_audit_events WHERE task_id=? AND event_key=?",
                (task_id, event_key),
            ).fetchone()
        return dict(row) if row is not None else None

    def record_linear_event(
        self, task_id: str, event_key: str, body_hash: str, *,
        state: str, retry_required: bool = False, last_error: str | None = None,
    ) -> None:
        if state not in {"PENDING", "WRITTEN", "FAILED"}:
            raise TaskRegistryError(f"Unsupported Linear audit state: {state}")
        self.get_task(task_id)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO linear_audit_events
                   (task_id,event_key,body_hash,state,retry_required,last_error,updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(task_id,event_key) DO UPDATE SET
                     body_hash=excluded.body_hash,state=excluded.state,
                     retry_required=excluded.retry_required,last_error=excluded.last_error,
                     updated_at=excluded.updated_at""",
                (task_id, event_key, body_hash, state, int(retry_required), last_error, _now()),
            )

    @contextlib.contextmanager
    def execution(
        self, task_id: str, issue_id: str | None = None, *,
        execution_ref: str | None = None, retain: bool = False,
    ) -> Iterator[TaskRecord]:
        task = self.get_task(task_id)
        if task.status == "ARCHIVED":
            raise TaskRegistryError(f"Archived task requires explicit reopen: {task_id}")
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    worktree_key = self.worktree_key(
                        host=task.host, cwd=task.cwd, repository_origin=task.repository_origin
                    )
                    try:
                        conn.execute(
                            "INSERT INTO executions(task_id,issue_id,execution_ref,worktree_key,logical_model,resolved_model,stage,acquired_at) VALUES (?,?,?,?,?,?,?,?)",
                            (task_id, issue_id, execution_ref, worktree_key, None, None, "CLAIMED", _now()),
                        )
                        conn.execute(
                            "INSERT INTO worktree_leases(worktree_key,task_id,execution_ref,stage,acquired_at) VALUES (?,?,?,?,?)",
                            (worktree_key, task_id, execution_ref, "CLAIMED", _now()),
                        )
                    except sqlite3.IntegrityError as exc:
                        active = conn.execute(
                            "SELECT * FROM worktree_leases WHERE worktree_key=?", (worktree_key,)
                        ).fetchone()
                        if active is not None:
                            raise WorktreeExecutionBusy(
                                f"Worktree already has an active execution: {task.cwd}",
                                active_task_id=active["task_id"],
                                active_execution_ref=active["execution_ref"],
                                stage=active["stage"], worktree_key=worktree_key,
                            ) from exc
                        raise TaskExecutionBusy(f"Task already has an active execution: {task_id}") from exc
                except sqlite3.IntegrityError as exc:
                    raise TaskExecutionBusy(f"Task already has an active execution: {task_id}") from exc
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        completed = False
        try:
            yield task
            completed = True
        finally:
            terminal = False
            try:
                terminal = self.get_task(task_id).execution_state in {
                    "COMPLETED", "IN_REVIEW", "BLOCKED", "RECOVERY_REQUIRED", "CANCELLED", "STOPPED",
                }
            except UnknownTaskError:
                terminal = True
            if not retain or not completed or terminal:
                self.release_execution(task_id, execution_ref)
