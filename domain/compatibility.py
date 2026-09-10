"""Read-only adapters from V1 registry records to V2 domain snapshots."""

from __future__ import annotations

import dataclasses
from typing import Protocol

from ._model import DomainModel
from .project import Project
from .task import Task, TaskState
from .workspace import Workspace


class V1TaskRecord(Protocol):
    task_id: str
    host: str
    workspace_alias: str
    project_alias: str
    project_name: str
    cwd: str
    repository_origin: str | None
    title: str
    summary: str | None
    task_key: str | None
    status: str
    created_at: str
    updated_at: str
    current_blocker: str | None


@dataclasses.dataclass(frozen=True)
class V1TaskDomainSnapshot(DomainModel):
    """A non-authoritative V2 view over one authoritative V1 task record."""

    project: Project
    workspace: Workspace
    task: Task


def v1_project_id(record: V1TaskRecord) -> str:
    return f"v1:project:{record.workspace_alias}:{record.project_alias}"


def v1_workspace_id(record: V1TaskRecord) -> str:
    return f"v1:workspace:{record.workspace_alias}"


def project_from_v1_record(record: V1TaskRecord) -> Project:
    origins = (record.repository_origin,) if record.repository_origin else ()
    return Project(
        project_id=v1_project_id(record),
        name=record.project_name,
        product_scope=record.project_alias,
        repository_origins=origins,
    )


def workspace_from_v1_record(record: V1TaskRecord) -> Workspace:
    return Workspace(
        workspace_id=v1_workspace_id(record),
        project_id=v1_project_id(record),
        host=record.host,
        checkout=record.cwd,
        worktree=record.cwd,
        runtime_namespace=record.workspace_alias,
        resource_scope=record.repository_origin or record.cwd,
    )


def task_from_v1_record(record: V1TaskRecord) -> Task:
    return Task(
        task_id=record.task_id,
        project_id=v1_project_id(record),
        workspace_id=v1_workspace_id(record),
        intent=record.title,
        objective=record.summary or record.title,
        external_reference=record.task_key,
        created_at=record.created_at,
        state=TaskState(
            lifecycle=record.status,
            blocker=record.current_blocker,
            updated_at=record.updated_at,
        ),
    )


def snapshot_from_v1_record(record: V1TaskRecord) -> V1TaskDomainSnapshot:
    return V1TaskDomainSnapshot(
        project=project_from_v1_record(record),
        workspace=workspace_from_v1_record(record),
        task=task_from_v1_record(record),
    )


# Explicit aliases retain the source entity name at call sites that map several
# kinds of V1 records.
project_from_v1_task = project_from_v1_record
workspace_from_v1_task = workspace_from_v1_record
task_from_v1_task = task_from_v1_record
snapshot_from_v1_task = snapshot_from_v1_record
