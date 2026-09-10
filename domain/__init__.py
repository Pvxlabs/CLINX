"""Stable, runtime-independent CLINX V2 domain contracts.

V1 remains authoritative. Importing this package has no registry, database,
provider, MCP, or execution side effects.
"""

from ._model import DomainValidationError, JsonDocument
from .attempt import Attempt, AttemptState
from .compatibility import (
    V1TaskDomainSnapshot,
    project_from_v1_record,
    snapshot_from_v1_record,
    task_from_v1_record,
    workspace_from_v1_record,
)
from .event import Event, EventActor
from .execution import Execution, ExecutionState, TerminalOutcome
from .project import Project, ProjectState
from .projection import Projection, ProjectionState
from .provider_session import ProviderSession, ProviderSessionState
from .resource_allocation import ResourceAllocation, ResourceAllocationState
from .runtime_worker import RuntimeWorker, RuntimeWorkerState
from .task import Task, TaskState
from .workspace import Workspace, WorkspaceState

__all__ = (
    "Attempt",
    "AttemptState",
    "DomainValidationError",
    "Event",
    "EventActor",
    "Execution",
    "ExecutionState",
    "JsonDocument",
    "Project",
    "ProjectState",
    "Projection",
    "ProjectionState",
    "ProviderSession",
    "ProviderSessionState",
    "ResourceAllocation",
    "ResourceAllocationState",
    "RuntimeWorker",
    "RuntimeWorkerState",
    "Task",
    "TaskState",
    "TerminalOutcome",
    "V1TaskDomainSnapshot",
    "Workspace",
    "WorkspaceState",
    "project_from_v1_record",
    "snapshot_from_v1_record",
    "task_from_v1_record",
    "workspace_from_v1_record",
)
