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


@dataclasses.dataclass(frozen=True)
class TaskIndexRecord:
    task_id: str
    issue_id: str
    identifier: str
    project_id: str | None
    created_at: str
    updated_at: str


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


def _now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


class TaskRegistry:
    """SQLite-backed durable task registry with one active execution lease."""

    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

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
                    retry_required INTEGER NOT NULL DEFAULT 0
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
            migrations = {
                "execution_state": "ALTER TABLE tasks ADD COLUMN execution_state TEXT NOT NULL DEFAULT 'QUEUED'",
                "current_stage": "ALTER TABLE tasks ADD COLUMN current_stage TEXT NOT NULL DEFAULT 'queued'",
                "current_blocker": "ALTER TABLE tasks ADD COLUMN current_blocker TEXT",
                "last_progress_at": "ALTER TABLE tasks ADD COLUMN last_progress_at TEXT NOT NULL DEFAULT ''",
                "codex_running": "ALTER TABLE tasks ADD COLUMN codex_running INTEGER NOT NULL DEFAULT 0",
                "turn_id": "ALTER TABLE tasks ADD COLUMN turn_id TEXT",
                "retry_required": "ALTER TABLE tasks ADD COLUMN retry_required INTEGER NOT NULL DEFAULT 0",
            }
            for column, statement in migrations.items():
                if column not in columns:
                    conn.execute(statement)
            conn.execute(
                "UPDATE tasks SET last_progress_at = COALESCE(NULLIF(last_progress_at, ''), updated_at)"
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
        return self.get_task(task_id)

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
    ) -> TaskRecord:
        allowed = {"QUEUED", "CLAIMED", "DISPATCHING", "CODEX_RUNNING", "BLOCKED", "IN_REVIEW", "COMPLETED"}
        if state not in allowed:
            raise TaskRegistryError(f"Unsupported execution state: {state}")
        task = self.get_task(task_id)
        running = task.codex_running if codex_running is _UNSET else bool(codex_running)
        if state == "CODEX_RUNNING":
            running = True
            candidate_turn = task.turn_id if turn_id is _UNSET else turn_id
            if not isinstance(candidate_turn, str) or not candidate_turn.strip():
                raise TaskRegistryError("CODEX_RUNNING requires an exact turn_id")
        if state == "BLOCKED":
            running = False
        effective_turn = task.turn_id if turn_id is _UNSET else turn_id
        if running and not effective_turn:
            raise TaskRegistryError("CODEX_RUNNING requires an exact turn_id")
        stage = task.current_stage if current_stage is _UNSET else current_stage
        blocker = task.current_blocker if current_blocker is _UNSET else current_blocker
        retry = task.retry_required if retry_required is _UNSET else bool(retry_required)
        changed = (
            task.execution_state != state or task.current_stage != stage
            or task.current_blocker != blocker or task.codex_running != running
            or task.turn_id != effective_turn or task.retry_required != retry
        )
        stamp = _now() if changed else task.last_progress_at
        with self._connect() as conn:
            conn.execute(
                """UPDATE tasks SET execution_state=?, current_stage=?, current_blocker=?,
                   last_progress_at=?, codex_running=?, turn_id=?, retry_required=?, updated_at=?
                   WHERE task_id=?""",
                (state, stage, blocker, stamp, int(running), effective_turn, int(retry),
                 _now() if changed else task.updated_at, task_id),
            )
        return self.get_task(task_id)

    def reset_execution(self, task_id: str) -> TaskRecord:
        return self.set_execution_state(
            task_id, "QUEUED", current_stage="queued", current_blocker=None,
            codex_running=False, turn_id=None, retry_required=False,
        )

    @contextlib.contextmanager
    def execution(self, task_id: str, issue_id: str | None = None) -> Iterator[TaskRecord]:
        task = self.get_task(task_id)
        if task.status == "ARCHIVED":
            raise TaskRegistryError(f"Archived task requires explicit reopen: {task_id}")
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        "INSERT INTO executions(task_id,issue_id,acquired_at) VALUES (?,?,?)",
                        (task_id, issue_id, _now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise TaskExecutionBusy(f"Task already has an active execution: {task_id}") from exc
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        try:
            yield task
        finally:
            with self._connect() as release:
                release.execute("DELETE FROM executions WHERE task_id = ?", (task_id,))
