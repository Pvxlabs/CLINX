"""Durable M5 task, workspace, and conversation registries.

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
    origin = run("remote", "get-url", "origin") if _has_origin(path) else ""
    branch = run("branch", "--show-current")
    if not branch:
        raise ProjectResolutionError(f"project is detached HEAD: {path}")
    return origin, branch


def _has_origin(path: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), "remote", "get-url", "origin"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


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
    status: str
    created_at: str
    updated_at: str


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
                    status TEXT NOT NULL CHECK(status IN ('ACTIVE','COMPLETED','ARCHIVED')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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
                """
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
    ) -> TaskRecord:
        task_id = "task_" + uuid.uuid4().hex
        stamp = _now()
        row = (
            task_id, host, workspace_alias, project_alias, project_name, cwd,
            repository_origin, branch, title or project_name, summary, "ACTIVE", stamp, stamp,
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO tasks
                (task_id,host,workspace_alias,project_alias,project_name,cwd,
                 repository_origin,branch,title,summary,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                row,
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> TaskRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise UnknownTaskError(f"Unknown task: {task_id}")
        return TaskRecord(**dict(row))

    def list_tasks(self, *, status: str | None = None) -> list[TaskRecord]:
        query = "SELECT * FROM tasks"
        args: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            args = (status,)
        query += " ORDER BY created_at"
        with self._connect() as conn:
            return [TaskRecord(**dict(row)) for row in conn.execute(query, args)]

    def get_binding(self, task_id: str) -> ConversationBinding | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_bindings WHERE task_id = ?", (task_id,)
            ).fetchone()
        return ConversationBinding(**dict(row)) if row is not None else None

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
