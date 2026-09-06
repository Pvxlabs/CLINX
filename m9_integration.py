"""CLINX-owned M9 integration surface.

The adapter deliberately accepts human task queries and keeps Codex/Linear
identity details behind the local registry.  It is transport-neutral so a
future remote MCP adapter can expose these same operations without adding
arbitrary shell or filesystem access.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from typing import Any

from task_registry import TaskRegistry, TaskRegistryError


class M9IntegrationError(RuntimeError):
    pass


class ResultParseError(M9IntegrationError):
    pass


@dataclasses.dataclass(frozen=True)
class ExecutionResult:
    status: str
    summary: str
    changed_files: str
    validation: str
    blockers: str
    next_state: str
    raw_result: str


_RESULT_KEYS = (
    "STATUS", "SUMMARY", "CHANGED_FILES", "VALIDATION", "BLOCKERS", "NEXT_STATE"
)


def parse_execution_result(text: str) -> ExecutionResult:
    """Parse the strict CLINX result contract; reject ambiguous output."""
    if not isinstance(text, str) or "CLINX_EXECUTION_RESULT" not in text:
        raise ResultParseError("missing CLINX_EXECUTION_RESULT header")
    lines = [line.strip() for line in text.splitlines()]
    try:
        start = lines.index("CLINX_EXECUTION_RESULT")
    except ValueError as exc:
        raise ResultParseError("missing exact CLINX_EXECUTION_RESULT header") from exc
    values: dict[str, str] = {}
    pattern = re.compile(r"^(STATUS|SUMMARY|CHANGED_FILES|VALIDATION|BLOCKERS|NEXT_STATE)=(.*)$")
    for line in lines[start + 1:]:
        if not line:
            continue
        match = pattern.match(line)
        if match:
            key, value = match.groups()
            if key in values:
                raise ResultParseError(f"duplicate result field: {key}")
            values[key] = value.strip()
    missing = [key for key in _RESULT_KEYS if not values.get(key)]
    if missing:
        raise ResultParseError("missing result fields: " + ", ".join(missing))
    if values["STATUS"] not in {"PASS", "BLOCKED"}:
        raise ResultParseError("STATUS must be PASS or BLOCKED")
    if values["NEXT_STATE"] not in {"IN_REVIEW", "BLOCKED", "COMPLETED"}:
        raise ResultParseError("NEXT_STATE is invalid")
    if values["STATUS"] == "BLOCKED" and values["NEXT_STATE"] != "BLOCKED":
        raise ResultParseError("BLOCKED results must use NEXT_STATE=BLOCKED")
    if values["STATUS"] == "PASS" and values["BLOCKERS"].upper() != "NONE":
        raise ResultParseError("PASS results must use BLOCKERS=NONE")
    return ExecutionResult(
        status=values["STATUS"],
        summary=values["SUMMARY"],
        changed_files=values["CHANGED_FILES"],
        validation=values["VALIDATION"],
        blockers=values["BLOCKERS"],
        next_state=values["NEXT_STATE"],
        raw_result=text,
    )


class ExecutionResultService:
    """Receive one exact turn result and own its idempotent Linear writeback."""

    def __init__(self, registry: TaskRegistry, linear: Any):
        self.registry = registry
        self.linear = linear

    @staticmethod
    def _body(execution_ref: str, task_id: str, turn_id: str, result: ExecutionResult) -> str:
        return (
            "CLINX_EXECUTION_RESULT_V1\n\n"
            f"EXECUTION_REF={execution_ref}\n"
            f"TASK_ID={task_id}\n"
            f"TURN_ID={turn_id}\n"
            f"STATUS={result.status}\n"
            f"SUMMARY={result.summary}\n"
            f"CHANGED_FILES={result.changed_files}\n"
            f"VALIDATION={result.validation}\n"
            f"BLOCKERS={result.blockers}\n"
            f"NEXT_STATE={result.next_state}\n"
            "CLINX_LINEAR_MCP_WRITE_REQUIRED=NO\n"
        )

    def receive_and_writeback(
        self,
        *,
        execution_ref: str,
        task_id: str,
        turn_id: str,
        raw_result: str,
        issue_id: str,
        review_state_id: str | None = None,
    ) -> ExecutionResult:
        result = parse_execution_result(raw_result)
        record = self.registry.record_execution_result(
            execution_ref=execution_ref,
            task_id=task_id,
            turn_id=turn_id,
            status=result.status,
            summary=result.summary,
            changed_files=result.changed_files,
            validation=result.validation,
            blockers=result.blockers,
            next_state=result.next_state,
            raw_result=result.raw_result,
        )
        self.registry.set_execution_state(
            task_id,
            "RESULT_RECEIVED",
            current_stage="CODEX_RESULT_RECEIVED",
            current_blocker=None,
            codex_running=False,
            turn_id=turn_id,
            retry_required=False,
        )
        self.registry.set_execution_state(
            task_id,
            "RESULT_PARSE",
            current_stage="RESULT_PARSE",
            current_blocker=None,
            codex_running=False,
            turn_id=turn_id,
            retry_required=False,
        )
        body = self._body(execution_ref, task_id, turn_id, result)
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if record.writeback_state != "WRITTEN":
            self.registry.set_execution_state(
                task_id,
                "LINEAR_WRITEBACK",
                current_stage="LINEAR_WRITEBACK",
                current_blocker=None,
                codex_running=False,
                turn_id=turn_id,
                retry_required=False,
            )
            try:
                self.linear.add_comment(issue_id, body)
                if result.status == "PASS" and review_state_id:
                    self.linear.update_issue_state(issue_id, review_state_id)
            except Exception as exc:
                self.registry.mark_execution_result_writeback(
                    execution_ref, state="FAILED", body_hash=body_hash
                )
                self.registry.set_execution_state(
                    task_id,
                    "LINEAR_WRITEBACK",
                    current_stage="LINEAR_WRITEBACK",
                    current_blocker=str(exc)[:2000],
                    codex_running=False,
                    turn_id=turn_id,
                    retry_required=True,
                )
                raise
            self.registry.mark_execution_result_writeback(
                execution_ref, state="WRITTEN", body_hash=body_hash
            )
        final_state = "BLOCKED" if result.status == "BLOCKED" else "IN_REVIEW"
        self.registry.set_execution_state(
            task_id,
            final_state,
            current_stage=final_state,
            current_blocker=result.blockers if final_state == "BLOCKED" else None,
            codex_running=False,
            turn_id=turn_id,
            retry_required=final_state == "BLOCKED",
        )
        return result


class ClinxIntegration:
    """Public M9 operations backed by existing CLINX services."""

    def __init__(self, cfg: Any, registry: TaskRegistry, dispatcher: Any, context_reader: Any, linear: Any):
        self.cfg = cfg
        self.registry = registry
        self.dispatcher = dispatcher
        self.context_reader = context_reader
        self.linear = linear

    def _resolve(self, task_ref: str | None = None, query: str | None = None, project: str | None = None) -> Any:
        return self.context_reader.resolve_task(task_ref=task_ref, query=query, project=project)

    def find_task(self, query: str, *, project: str | None = None, host: str | None = None) -> dict[str, Any]:
        found = self.registry.find_tasks(query=query, project=project, host=host, include_archived=False)
        return {
            "classification": found.classification,
            "tasks": [self._public(task) for task in found.tasks],
            "read_only": True,
        }

    def get_context(self, *, task_ref: str | None = None, query: str | None = None, project: str | None = None) -> dict[str, Any]:
        task = self._resolve(task_ref, query, project)
        return self.context_reader.read_task_context(task.task_id).as_dict()

    def list_projects(self) -> dict[str, Any]:
        return {
            "projects": [
                {
                    "project": item.project_alias,
                    "name": item.linear_name,
                    "workspace": item.workspace_alias,
                    "read_only": item.read_only,
                }
                for item in self.cfg.projects
            ],
            "read_only": True,
        }

    def get_status(self, *, task_ref: str | None = None, query: str | None = None, project: str | None = None) -> dict[str, Any]:
        task = self._resolve(task_ref, query, project)
        result = self.registry.latest_execution_result(task.task_id)
        return {
            **self._public(task),
            "execution_result": dataclasses.asdict(result) if result else None,
            "read_only": True,
        }

    def execute(self, *, prompt: str, execution_ref: str, task_ref: str | None = None, query: str | None = None, project: str | None = None, model: str | None = None, reasoning_effort: str | None = None) -> dict[str, Any]:
        if not prompt.strip() or not execution_ref.strip():
            raise M9IntegrationError("explicit prompt and execution_ref are required")
        task = self._resolve(task_ref, query, project)
        result = self.dispatcher.dispatch(
            project_ref=task.project_alias,
            host=task.host,
            project_mode="existing",
            task_mode="continue",
            task_id=task.task_id,
            prompt=prompt,
            title=task.title,
            summary=task.summary,
            model=model,
            reasoning_effort=reasoning_effort,
            issue_id=self.registry.last_linear_execution(task.task_id),
            execution_ref=execution_ref,
        )
        return {
            "execution_ref": execution_ref,
            "task_ref": result.task_id,
            "turn_id": result.turn_id,
            "dispatch_status": result.dispatch_status,
            "conversation_binding_preserved": True,
        }

    def _public(self, task: Any) -> dict[str, Any]:
        return {
            "task_ref": task.task_id,
            "task_key": task.task_key,
            "project": task.project_alias,
            "title": task.title,
            "summary": task.summary,
            "status": task.status,
            "execution_state": task.execution_state,
            "current_stage": task.current_stage,
            "current_blocker": task.current_blocker,
            "last_progress_at": task.last_progress_at,
            "codex_running": bool(task.codex_running),
            "turn_id": task.turn_id,
            "retry_required": bool(task.retry_required),
        }


def clinx_find_task(integration: ClinxIntegration, query: str, **kwargs: Any) -> dict[str, Any]:
    return integration.find_task(query, **kwargs)


def clinx_get_context(integration: ClinxIntegration, **kwargs: Any) -> dict[str, Any]:
    return integration.get_context(**kwargs)


def clinx_list_projects(integration: ClinxIntegration) -> dict[str, Any]:
    return integration.list_projects()


def clinx_get_status(integration: ClinxIntegration, **kwargs: Any) -> dict[str, Any]:
    return integration.get_status(**kwargs)


def clinx_execute(integration: ClinxIntegration, **kwargs: Any) -> dict[str, Any]:
    return integration.execute(**kwargs)
