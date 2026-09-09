"""CLINX-owned M9 integration surface.

The adapter deliberately accepts human task queries and keeps Codex/Linear
identity details behind the local registry.  It is transport-neutral so a
future remote MCP adapter can expose these same operations without adding
arbitrary shell or filesystem access.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from app_server import AppServerError
from execution_semantics import RoutingIdentity, parse_routing_identity
from task_registry import (
    TERMINAL_EXECUTION_STAGES,
    TaskRegistry,
    TaskRegistryError,
    TaskExecutionBusy,
    WorktreeExecutionBusy,
)


class M9IntegrationError(RuntimeError):
    pass


class ResultParseError(M9IntegrationError):
    pass


@dataclasses.dataclass(frozen=True)
class ExecutionHandoff:
    """A canonical CLINX command-plane preparation."""

    task_action: str
    task_ref: str | None
    host: str
    project: str
    model: str
    reasoning: str
    execution_mode: str
    network_access: bool
    title: str
    summary: str
    prompt: str
    linear_project: str
    team_id: str
    trigger_label: str
    todo_state: str
    description: str
    prepared_execution_ref: str
    routing_identity: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "execution_available": True,
            "command_plane": "CLINX",
            "linear_role": "AUDIT",
            "requires_user_approval": True,
            "requires_command_write": False,
            "approval_state": "SATISFIED",
            "handoff_ready": True,
            "prepared_execution_ref": self.prepared_execution_ref,
            "task_action": self.task_action,
            "task_ref": self.task_ref,
            "host": self.host,
            "project": self.project,
            "model": self.model,
            "reasoning": self.reasoning,
            "execution_mode": self.execution_mode,
            "network_access": self.network_access,
            "NETWORK_ACCESS": "ENABLED" if self.network_access else "DISABLED",
            "title": self.title,
            "summary": self.summary,
            "prompt": self.prompt,
            "description": self.description,
            "routing_identity": self.routing_identity,
            "next_action": {
                "provider": "CLINX",
                "operation": "START_EXECUTION",
                "required": True,
            },
            "linear_audit": {"optional": True, "purpose": "AUDIT + HUMAN_NOTIFICATION only"},
            "status_lookup": {"tool": "clinx_get_status"},
            "read_only": True,
        }


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
        # Some app-server summary views collapse line breaks.  Preserve the
        # strict field contract while accepting that bounded representation.
        compact = " ".join(text.split())
        header_offset = compact.find("CLINX_EXECUTION_RESULT")
        if header_offset < 0:
            raise ResultParseError("missing exact CLINX_EXECUTION_RESULT header") from exc
        compact = compact[header_offset:]
        keys = "STATUS|SUMMARY|CHANGED_FILES|VALIDATION|BLOCKERS|NEXT_STATE"
        values = {
            key: value.strip()
            for key, value in re.findall(
                rf"(?:^|\s)({keys})=(.*?)(?=\s+(?:{keys})=|$)", compact
            )
        }
        missing = [key for key in _RESULT_KEYS if not values.get(key)]
        if missing:
            raise ResultParseError("missing result fields: " + ", ".join(missing)) from exc
        if values["STATUS"] not in {"PASS", "BLOCKED"}:
            raise ResultParseError("STATUS must be PASS or BLOCKED") from exc
        if values["NEXT_STATE"] not in {"IN_REVIEW", "BLOCKED", "COMPLETED"}:
            raise ResultParseError("NEXT_STATE is invalid") from exc
        if values["STATUS"] == "BLOCKED" and values["NEXT_STATE"] != "BLOCKED":
            raise ResultParseError("BLOCKED results must use NEXT_STATE=BLOCKED") from exc
        if values["STATUS"] == "PASS" and values["BLOCKERS"].upper() != "NONE":
            raise ResultParseError("PASS results must use BLOCKERS=NONE") from exc
        return ExecutionResult(
            status=values["STATUS"], summary=values["SUMMARY"],
            changed_files=values["CHANGED_FILES"], validation=values["VALIDATION"],
            blockers=values["BLOCKERS"], next_state=values["NEXT_STATE"], raw_result=text,
        )
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


def parse_codex_result(text: str) -> ExecutionResult:
    """Parse a strict CLINX result or the bounded external Codex result shape.

    The direct-execution pilot returned a structured JSON qualification result
    rather than the line-oriented CLINX contract.  Keep the line parser strict,
    but accept this separately validated shape so result ingestion does not
    depend on a model formatting its final response exactly as CLINX text.
    Every normalized field below is derived from an explicitly required JSON
    field; missing or contradictory evidence remains a parse failure.
    """
    try:
        return parse_execution_result(text)
    except ResultParseError as strict_error:
        candidate = text.strip()
        if candidate.startswith("```") and candidate.endswith("```"):
            if candidate.startswith("```json"):
                candidate = candidate.removeprefix("```json").removesuffix("```").strip()
            else:
                lines = candidate.splitlines()
                if len(lines) < 3 or lines[0].strip() != "```json" or lines[-1].strip() != "```":
                    raise strict_error
                candidate = "\n".join(lines[1:-1]).strip()
        try:
            payload = json.loads(candidate)
        except (TypeError, json.JSONDecodeError) as exc:
            raise strict_error from exc
        if not isinstance(payload, dict):
            raise strict_error
        if payload.get("execution") != "CLINX" or payload.get("result") != "PASS":
            raise strict_error
        repository = payload.get("repository")
        head = payload.get("head")
        modifications = payload.get("modifications")
        business_actions = payload.get("business_actions")
        git_status = payload.get("git_status")
        required_strings = {
            "repository": repository,
            "head": head,
            "modifications": modifications,
            "business_actions": business_actions,
        }
        if any(not isinstance(value, str) or not value.strip() for value in required_strings.values()):
            raise strict_error
        if not re.fullmatch(r"[0-9a-fA-F]{40}", head):
            raise strict_error
        if modifications.casefold() != "none" or business_actions.casefold() != "none":
            raise strict_error
        if not isinstance(git_status, dict):
            raise strict_error
        if (
            git_status.get("working_tree") != "clean"
            or git_status.get("staged_changes") is not False
            or git_status.get("unstaged_changes") is not False
            or git_status.get("untracked_files") is not False
        ):
            raise strict_error
        branch = git_status.get("branch")
        if not isinstance(branch, str) or not branch.strip():
            raise strict_error
        return ExecutionResult(
            status="PASS",
            summary=(
                f"Read-only Codex qualification passed for {repository.strip()} "
                f"on branch {branch.strip()} at HEAD {head.lower()}."
            ),
            changed_files="NONE (reported modifications=none)",
            validation=(
                "Repository reported clean; staged_changes=false, "
                "unstaged_changes=false, untracked_files=false; "
                f"HEAD={head.lower()}"
            ),
            blockers="NONE",
            next_state="IN_REVIEW",
            raw_result=text,
        )


class ExecutionResultService:
    """Receive one exact turn result and own its idempotent Linear writeback."""

    def __init__(self, registry: TaskRegistry, linear: Any):
        self.registry = registry
        self.linear = linear

    @staticmethod
    def _body(execution_ref: str, task_id: str, turn_id: str, result: ExecutionResult,
              logical_model: str | None = None, resolved_model: str | None = None) -> str:
        return (
            "CLINX_EXECUTION_RESULT_V1\n\n"
            f"EXECUTION_REF={execution_ref}\n"
            f"TASK_REF={task_id}\n"
            f"STATUS={result.status}\n"
            f"SUMMARY={result.summary}\n"
            f"CHANGED_FILES={result.changed_files}\n"
            f"VALIDATION={result.validation}\n"
            f"BLOCKERS={result.blockers}\n"
            f"NEXT_STATE={result.next_state}\n"
            f"MODEL_LOGICAL_TO_RESOLVED={logical_model or 'UNKNOWN'} -> {resolved_model or 'UNKNOWN'}\n"
            f"TESTS={result.validation}\n"
            "COMMIT=UNKNOWN\n"
            "WORKTREE_STATE=UNKNOWN\n"
            f"BLOCKER={result.blockers}\n"
            f"SUMMARY={result.summary}\n"
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
        blocked_state_id: str | None = None,
    ) -> ExecutionResult:
        result = parse_codex_result(raw_result)
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
        index = self.registry.get_task_index(task_id)
        target_issue_id = index.issue_id if index is not None else issue_id
        if not target_issue_id:
            raise TaskRegistryError(f"Task {task_id} has no Linear mirror issue")
        logical_model, resolved_model = self.registry.get_execution_models(execution_ref)
        body = self._body(
            execution_ref, task_id, turn_id, result,
            logical_model=logical_model, resolved_model=resolved_model,
        )
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
            event_key = f"completion:{execution_ref}"
            event = self.registry.get_linear_event(task_id, event_key)
            if event is not None and event["state"] == "WRITTEN" and event["body_hash"] == body_hash:
                record = self.registry.mark_execution_result_writeback(
                    execution_ref, state="WRITTEN", body_hash=body_hash
                )
            else:
                self.registry.record_linear_event(task_id, event_key, body_hash, state="PENDING")
                try:
                    self.linear.add_comment(target_issue_id, body)
                    self.registry.record_linear_event(task_id, event_key, body_hash, state="WRITTEN")
                except Exception as exc:
                    self.registry.record_linear_event(
                        task_id, event_key, body_hash, state="FAILED",
                        retry_required=True, last_error=str(exc)[:2000],
                    )
                    self.registry.mark_execution_result_writeback(
                        execution_ref, state="FAILED", body_hash=body_hash
                    )
                    self.registry.set_execution_state(
                        task_id, "LINEAR_WRITEBACK", current_stage="LINEAR_WRITEBACK",
                        current_blocker=str(exc)[:2000], codex_running=False,
                        turn_id=turn_id, retry_required=True,
                    )
                    self.registry.release_execution(task_id, execution_ref)
                    raise
                if result.status == "PASS" and review_state_id:
                    self.linear.update_issue_state(target_issue_id, review_state_id)
                elif result.status == "BLOCKED" and blocked_state_id:
                    self.linear.update_issue_state(target_issue_id, blocked_state_id)
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
        self.registry.release_execution(task_id, execution_ref, retain_history=True)
        return result


class ClinxIntegration:
    """Public M11 operations backed by existing CLINX services."""

    def __init__(
        self,
        cfg: Any,
        registry: TaskRegistry,
        dispatcher: Any,
        context_reader: Any,
        linear: Any,
        topic_reader: Any = None,
    ):
        self.cfg = cfg
        self.registry = registry
        self.dispatcher = dispatcher
        self.context_reader = context_reader
        self.linear = linear
        # Keep the legacy test construction lightweight.  Production callers
        # inject the reader explicitly because it owns the app-server client.
        self.topic_reader = topic_reader

    def _resolve(self, task_ref: str | None = None, query: str | None = None, project: str | None = None) -> Any:
        return self.context_reader.resolve_task(task_ref=task_ref, query=query, project=project)

    @staticmethod
    def _public_route(value: str | None) -> dict[str, Any] | None:
        try:
            route = parse_routing_identity(value)
        except (TypeError, ValueError):
            return None
        return route.public_dict() if route is not None else None

    def find_task(
        self,
        query: str,
        *,
        project: str | None = None,
        host: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        found = self.registry.find_tasks(
            query=query,
            project=project,
            host=host,
            status=status,
            include_archived=True,
        )
        return {
            "classification": found.classification,
            "tasks": [self._public(task) for task in found.tasks],
            "read_only": True,
        }

    def get_context(
        self,
        *,
        task_ref: str | None = None,
        query: str | None = None,
        project: str | None = None,
        host: str | None = None,
        recent_turns: int | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        task = self.context_reader.resolve_task(
            task_ref=task_ref,
            query=query,
            project=project,
            host=host,
        )
        options: dict[str, Any] = {}
        if recent_turns is not None:
            options["recent_turns"] = recent_turns
        if max_bytes is not None:
            options["max_bytes"] = max_bytes
        return self.context_reader.read_task_context(task.task_id, **options).as_dict()

    def get_topic_status(
        self,
        *,
        host: str | None = None,
        project: str,
        topic: str,
        include_completed: bool = True,
        include_historical: bool = True,
        limit: int = 20,
        recent_turns: int | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Read deterministic topic status without executing or writing."""
        if self.topic_reader is None:
            raise M9IntegrationError("topic status reader is not configured")
        options: dict[str, Any] = {
            "host": host,
            "project_ref": project,
            "topic": topic,
            "include_completed": include_completed,
            "include_historical": include_historical,
            "limit": limit,
        }
        if recent_turns is not None:
            options["recent_turns"] = recent_turns
        if max_bytes is not None:
            options["max_bytes"] = max_bytes
        return self.topic_reader.read_topic_status(**options).as_dict()

    def list_projects(
        self,
        *,
        host: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        """List bounded registered/workspace projects without exposing paths."""
        candidates: list[tuple[str, str]] = []
        for mapping in self.cfg.projects:
            candidates.append((mapping.workspace_alias or "", mapping.project_alias))
        for workspace in self.dispatcher.workspaces:
            if host and (workspace.host or workspace.alias).casefold() != host.casefold():
                continue
            if not workspace.allow_existing_projects or not workspace.root.is_dir():
                continue
            try:
                entries = sorted(workspace.root.iterdir(), key=lambda item: item.name.casefold())
            except OSError:
                continue
            candidates.extend(
                (workspace.alias, entry.name)
                for entry in entries
                if entry.is_dir() and not entry.name.startswith(".")
            )

        seen: set[tuple[str, str]] = set()
        projects: list[dict[str, Any]] = []
        for workspace_alias, project_ref in candidates:
            key = (workspace_alias.casefold(), project_ref.casefold())
            if key in seen:
                continue
            seen.add(key)
            if query and query.casefold() not in project_ref.casefold():
                continue
            try:
                workspace, descriptor, _mapping = self.dispatcher.resolve_project(
                    project_ref,
                    host=host or workspace_alias or None,
                    project_mode="existing",
                )
            except Exception:
                continue
            projects.append(
                {
                    "project": descriptor.alias,
                    "name": descriptor.name,
                    "workspace": workspace.alias,
                    "host": workspace.host or workspace.alias,
                    "availability": "registered" if descriptor.registered else "discoverable",
                    "registered": descriptor.registered,
                }
            )
        return {
            "projects": projects,
            "read_only": True,
        }

    def get_status(
        self,
        *,
        task_ref: str | None = None,
        execution_ref: str | None = None,
        query: str | None = None,
        project: str | None = None,
        host: str | None = None,
    ) -> dict[str, Any]:
        retained_recovery: dict[str, Any] | None = None
        if execution_ref:
            execution = self.registry.get_execution_result(execution_ref)
            if execution is not None:
                task = self.registry.get_task(execution.task_id)
            else:
                active = self.registry.get_active_execution(execution_ref)
                if active is not None:
                    task = self.registry.get_task(active["task_id"])
                else:
                    retained_recovery = self.registry.get_execution_record(execution_ref)
                    if retained_recovery is None:
                        raise M9IntegrationError(f"Unknown execution ref: {execution_ref}")
                    task = self.registry.get_task(retained_recovery["task_id"])
        else:
            task = self.context_reader.resolve_task(
                task_ref=task_ref,
                query=query,
                project=project,
                host=host,
            )
        # Context adapters may return a cached TaskRecord.  Refresh the
        # durable row before selecting reconciliation evidence so a previous
        # self-heal is visible on the next status read.
        if self.registry is not None:
            task = self.registry.get_task(task.task_id)
        # A status read is also a bounded recovery point.  The registry is
        # authoritative for identity, while the dispatcher may reconcile one
        # exact active turn against provider state without reading history.
        active_for_reconcile = (
            self.registry.get_active_execution(execution_ref)
            if execution_ref else self.registry.get_latest_execution_for_task(task.task_id)
        )
        if (
            active_for_reconcile is None
            and retained_recovery is not None
            and task.execution_state == "RECOVERY_REQUIRED"
            and task.retry_required
            and retained_recovery.get("stage") == "RECOVERY_REQUIRED"
        ):
            # A retained exact execution can still gain result evidence on a
            # later bounded provider read.  It is never treated as active.
            active_for_reconcile = retained_recovery
        if (
            active_for_reconcile is None
            and not execution_ref
            and task.codex_running
            and task.execution_state in {
                "CLAIMED", "DISPATCHING", "TURN_STARTED", "CODEX_RUNNING",
                "TRANSPORT_UNCERTAIN", "CANCEL_REQUESTED", "CANCELLATION_PENDING",
            }
        ):
            # Some pre-M12 runtimes never flushed an executions row at all;
            # the task projection is still a bounded owner candidate.
            active_for_reconcile = {"execution_ref": None, "stage": task.current_stage}
        reconcile = getattr(self.dispatcher, "reconcile_execution", None)
        if not callable(reconcile):
            reconcile = getattr(self.dispatcher, "reconcile_task", None)
        if active_for_reconcile and callable(reconcile):
            try:
                active_ref = active_for_reconcile.get("execution_ref")
                if active_ref:
                    reconcile(active_ref)
                else:
                    # Legacy executions may have a lease but no opaque
                    # execution identity.  Reconcile the exact bound task;
                    # the dispatcher will release it only on terminal proof.
                    try:
                        reconcile(None, task_id=task.task_id)
                    except TypeError:
                        # Lightweight adapters may expose only task-level
                        # reconciliation; keep this compatibility bounded to
                        # the null-ref legacy path.
                        reconcile(task.task_id)
                task = self.registry.get_task(task.task_id)
            except Exception:
                # Status remains useful even when a bounded provider read is
                # unavailable; the registry keeps the explicit uncertainty.
                task = self.registry.get_task(task.task_id)
        result = self.registry.latest_execution_result(task.task_id)
        execution_result = None
        if result is not None:
            execution_result = {
                "status": result.status,
                "summary": result.summary,
                "changed_files": result.changed_files,
                "validation": result.validation,
                "blockers": result.blockers,
                "next_state": result.next_state,
                "received_at": result.received_at,
                "writeback_state": result.writeback_state,
            }
        status = {
            **self._public(task),
            "execution_result": execution_result,
            "read_only": True,
        }
        execution_route = (
            self.registry.get_execution_routing_identity(execution_ref)
            if execution_ref
            else self.registry.get_latest_execution_routing_identity(task.task_id)
        )
        status["routing_identity"] = self._public_route(task.routing_identity_json)
        status["execution_routing_identity"] = (
            execution_route.public_dict() if execution_route is not None else None
        )
        prepared = (
            self.registry.get_prepared_execution_for_execution(execution_ref)
            if execution_ref
            else self.registry.get_prepared_execution_for_task(task.task_id)
        )
        network_access = bool(prepared.network_access) if prepared is not None else False
        status["network_access"] = network_access
        status["NETWORK_ACCESS"] = "ENABLED" if network_access else "DISABLED"
        audit = self.registry.get_linear_audit(task.task_id)
        status["linear_audit_sync"] = (
            "NOT_CONFIGURED" if self.linear is None else audit.sync_state if audit else "PENDING"
        )
        status["linear_retry_required"] = bool(audit.retry_required) if audit else False
        status["linear_last_error"] = audit.last_error if audit else None
        active_execution = None
        with self.registry._connect() as conn:
            row = conn.execute(
                "SELECT execution_ref, stage FROM executions WHERE task_id=? "
                "AND stage NOT IN (?,?,?,?,?,?) "
                "ORDER BY acquired_at DESC, rowid DESC LIMIT 1",
                (task.task_id, *TERMINAL_EXECUTION_STAGES),
            ).fetchone()
        if row is not None and row["execution_ref"]:
            active_execution = {"execution_ref": row["execution_ref"], "stage": row["stage"]}
        status["active_execution"] = active_execution
        index = self.registry.get_task_index(task.task_id)
        status["linear_issue_ref"] = index.identifier if index else None
        status.update(
            {
                # Linear's human lifecycle is already In Review, while the
                # execution contract reports the provider terminal state as
                # COMPLETED for a successful result.
                "EXECUTION_STATE": (
                    "COMPLETED"
                    if task.execution_state == "IN_REVIEW"
                    and result is not None
                    and result.status == "PASS"
                    else task.execution_state
                ),
                "CODEX_RUNNING": bool(task.codex_running),
                "CURRENT_STAGE": task.current_stage,
                "CURRENT_BLOCKER": task.current_blocker,
                "LAST_PROGRESS_AT": task.last_progress_at,
                "TURN_PRESENT": bool(task.turn_id),
                "RETRY_REQUIRED": bool(task.retry_required),
            }
        )
        if execution_ref:
            status["execution_ref"] = execution_ref
        return status

    def get_capabilities(self) -> dict[str, Any]:
        """Describe the separated CLINX command, Codex execution, and Linear audit planes."""
        return {
            "context_plane": {
                "available": True,
                "transport": "CLINX_MCP",
                "name": "CLINX",
                "read_only": True,
            },
            "context_read_only": True,
            "execution_available": True,
            "command_plane": "CLINX",
            "prepare_tool": "clinx_prepare_execution",
            "start_tool": "clinx_start_execution",
            "cancel_tool": "clinx_cancel_execution",
            "status_tool": "clinx_get_status",
            "execution": {
                "available": True,
                "direct_mcp_execution": True,
                "command_plane": "CLINX",
                "requires_user_approval": True,
                "prepare_tool": "clinx_prepare_execution",
                "start_tool": "clinx_start_execution",
            },
            "status": {
                "available": True,
                "tool": "clinx_get_status",
            },
            "linear": {
                "role": "AUDIT",
                "failure_does_not_block_codex": True,
                "task_issue_binding": "1:1",
            },
            "instructions": (
                "Use CLINX for authoritative task context and command preparation. "
                "Prepare with clinx_prepare_execution, then call "
                "clinx_start_execution with only the returned prepared_execution_ref "
                "and approved=true. Linear is optional audit/history compatibility "
                "and is never required for Codex execution."
            ),
            "read_only": True,
        }

    def _linear_project_name(self) -> str:
        configured = getattr(self.cfg, "linear_project_name", None)
        if configured:
            return str(configured)
        for mapping in getattr(self.cfg, "projects", ()):
            name = str(getattr(mapping, "linear_name", ""))
            if name == "ChatGPT × Linear × Codex Dispatcher V1":
                return name
        # Existing configurations predate an explicit control-plane setting;
        # preserve their configured project name without inventing a new one.
        names = [str(getattr(item, "linear_name", "")) for item in getattr(self.cfg, "projects", ())]
        return next((name for name in names if name), "ChatGPT × Linear × Codex Dispatcher V1")

    @staticmethod
    def _validated_text(name: str, value: str | None, *, required: bool = True) -> str:
        if not isinstance(value, str) or not value.strip():
            if required:
                raise M9IntegrationError(f"{name} is required")
            return ""
        return value.strip()

    def prepare_execution(
        self,
        *,
        prompt: str,
        approved: bool = False,
        task_mode: str = "continue",
        task_action: str | None = None,
        task_ref: str | None = None,
        query: str | None = None,
        host: str | None = None,
        project: str | None = None,
        title: str | None = None,
        summary: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        reasoning: str | None = None,
        execution_mode: str = "normal",
        network_access: bool = False,
    ) -> dict[str, Any]:
        """Prepare an integrity-checked CLINX execution command without dispatching."""
        if approved is not True:
            raise M9IntegrationError("explicit approved=true is required for preparation")
        prompt = self._validated_text("prompt", prompt)
        if task_action is not None:
            if task_action not in {"create", "continue", "reopen"}:
                raise M9IntegrationError("task_action must be create, continue, or reopen")
            if task_action == "create":
                # The default task_mode is continue so callers may provide the
                # concise task_action=create form without also repeating mode.
                if task_mode not in {"new", "continue"}:
                    raise M9IntegrationError("task_mode and task_action disagree")
                task_mode = "new"
            else:
                if task_mode != "continue":
                    raise M9IntegrationError("task_mode and task_action disagree")
                task_mode = "continue"
        if task_mode not in {"new", "continue"}:
            raise M9IntegrationError("task_mode must be new or continue")
        if execution_mode not in {"normal", "fast"}:
            raise M9IntegrationError("execution_mode must be normal or fast")
        if not isinstance(network_access, bool):
            raise M9IntegrationError("network_access must be a boolean")
        selected_model = self._validated_text("model", model, required=False) or "gpt-5.6-luna"
        selected_reasoning = (
            self._validated_text("reasoning_effort", reasoning_effort, required=False)
            or self._validated_text("reasoning", reasoning, required=False)
            or "high"
        )

        prepared_route = "{}"
        prepared_route_public: dict[str, Any] | None = None
        if task_mode == "continue":
            task = self.context_reader.resolve_task(
                task_ref=task_ref,
                query=query,
                project=project,
                host=host,
            )
            # Import lazily because bridge imports the integration service.
            from bridge import TargetResolutionError, canonical_host

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
            binding = self.registry.get_binding(task.task_id)
            if binding is None:
                raise M9IntegrationError(f"Task {task.task_id} has no conversation binding")
            if not task.project_alias or not task.workspace_alias or not task.cwd:
                raise M9IntegrationError(f"Task {task.task_id} has incomplete project identity")
            if hasattr(self.dispatcher, "resolve_project"):
                _workspace, descriptor, _mapping = self.dispatcher.resolve_project(
                    task.project_alias,
                    host=task.host,
                    project_mode="existing",
                )
                if descriptor.alias.casefold() != task.project_alias.casefold():
                    raise M9IntegrationError("Resolved project does not match task project")
                if str(descriptor.cwd.resolve()) != str(Path(task.cwd).resolve()):
                    raise M9IntegrationError("Resolved project cwd does not match task")
            if task.status not in {"ACTIVE", "COMPLETED", "ARCHIVED"}:
                raise M9IntegrationError(f"Unsupported task status: {task.status}")
            if task_action == "reopen" and task.status not in {"COMPLETED", "ARCHIVED"}:
                raise M9IntegrationError("task_action=reopen requires a completed or archived task")
            selected_action = (
                task_action
                if task_action is not None
                else "reopen" if task.status in {"COMPLETED", "ARCHIVED"} else "continue"
            )
            selected_host = task.host
            selected_project = task.project_alias
            selected_title = self._validated_text("title", title, required=False) or task.title
            selected_summary = (
                self._validated_text("summary", summary, required=False)
                if summary is not None else (task.summary or "")
            )
            selected_ref = task.task_id
            prepared_route = task.routing_identity_json or "{}"
            parsed_route = parse_routing_identity(prepared_route)
            prepared_route_public = parsed_route.public_dict() if parsed_route is not None else None
        else:
            selected_host = self._validated_text("host", host)
            selected_project = self._validated_text("project", project)
            selected_title = self._validated_text("title", title)
            selected_summary = self._validated_text("summary", summary, required=False)
            selected_action = "create"
            selected_ref = None
            if not hasattr(self.dispatcher, "resolve_project"):
                raise M9IntegrationError("project resolver is not configured")
            workspace, descriptor, project_mapping = self.dispatcher.resolve_project(
                selected_project,
                host=selected_host,
                project_mode="existing",
            )
            route_builder = getattr(self.dispatcher, "_routing_identity", None)
            if callable(route_builder):
                route = route_builder(
                    workspace=workspace,
                    project=project_mapping,
                    conversation_bound=False,
                    network_access=network_access,
                )
                prepared_route = route.to_json()
                prepared_route_public = route.public_dict()

        linear_project = self._linear_project_name()
        description_lines = [
            "CLINX_M12_EXECUTION_PREPARATION_V1",
            "",
            f"HOST={selected_host}",
            f"PROJECT={selected_project}",
            "PROJECT_MODE=existing",
            f"TASK_ACTION={selected_action}",
        ]
        if selected_ref is not None:
            description_lines.append(f"TASK_REF={selected_ref}")
        description_lines.extend([
            f"MODEL={selected_model}",
            f"REASONING={selected_reasoning}",
            f"EXECUTION_MODE={execution_mode}",
            f"NETWORK_ACCESS={'ENABLED' if network_access else 'DISABLED'}",
            f"TASK_TITLE={selected_title}",
            f"TASK_SUMMARY_UPDATE={selected_summary or 'UNKNOWN'}",
            "",
            "PROMPT:",
            prompt,
        ])
        prepared = self.registry.create_prepared_execution(
            task_action=selected_action,
            task_ref=selected_ref,
            host=selected_host,
            project=selected_project,
            title=selected_title,
            summary=selected_summary,
            prompt=prompt,
            model=selected_model,
            logical_model=selected_model,
            resolved_executable_model=selected_model,
            reasoning_effort=selected_reasoning,
            execution_mode=execution_mode,
            network_access=network_access,
            routing_identity=prepared_route,
        )
        return ExecutionHandoff(
            task_action=selected_action,
            task_ref=selected_ref,
            host=selected_host,
            project=selected_project,
            model=selected_model,
            reasoning=selected_reasoning,
            execution_mode=execution_mode,
            network_access=network_access,
            title=selected_title,
            summary=selected_summary,
            prompt=prompt,
            linear_project=linear_project,
            team_id=str(getattr(self.cfg, "team_id", "")),
            trigger_label=str(getattr(self.cfg, "trigger_label", "local-codex")),
            todo_state=str(getattr(self.cfg, "todo_state", "Todo")),
            description="\n".join(description_lines),
            prepared_execution_ref=prepared.prepared_execution_ref,
            routing_identity=prepared_route_public,
        ).as_dict()

    def _audit_task_index(self, task_id: str) -> str:
        """Best-effort 1:1 Linear mirror; never gates Codex dispatch."""
        if self.linear is None:
            return "NOT_CONFIGURED"
        try:
            from bridge import LinearTaskIndex
            index = LinearTaskIndex(
                self.linear,
                self.registry,
                str(getattr(self.cfg, "team_id", "")),
            ).sync(task_id)
            transition = self._linear_transition(task_id, index.issue_id, "TODO")
            if transition != "PASS":
                return "DEGRADED"
        except Exception as exc:
            self.registry.record_linear_event(
                task_id, "task-mirror", hashlib.sha256(task_id.encode()).hexdigest(),
                state="FAILED", retry_required=True, last_error=str(exc)[:2000],
            )
            # Preserve the historical public audit label while the durable
            # registry records the M12 DEGRADED/retry state.
            return "FAILED"
        return "PASS"

    def _linear_state_id(self, state_name: str) -> str | None:
        if self.linear is None:
            return None
        direct = getattr(self.linear, "state_id", None)
        if callable(direct):
            return direct(state_name)
        states = getattr(self.linear, "team_states", None)
        if not callable(states):
            return None
        values = states(str(getattr(self.cfg, "team_id", "")))
        return values.get(state_name)

    def _linear_transition(self, task_id: str, issue_id: str, state_name: str) -> str:
        """Write a deterministic lifecycle event and state when available."""
        body = (
            "CLINX_TASK_LIFECYCLE_V1\n"
            f"TASK_REF={task_id}\nSTATE={state_name}\n"
            f"LINEAR_AUDIT_SYNC=PASS\n"
        )
        event_key = f"lifecycle:{state_name.lower()}"
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        existing = self.registry.get_linear_event(task_id, event_key)
        if existing and existing["state"] == "WRITTEN" and existing["body_hash"] == body_hash:
            return "PASS"
        self.registry.record_linear_event(task_id, event_key, body_hash, state="PENDING")
        try:
            state_id = self._linear_state_id(
                getattr(self.cfg, "todo_state", "Todo") if state_name == "TODO"
                else getattr(self.cfg, "running_state", "In Progress")
                if state_name == "IN_PROGRESS" else getattr(self.cfg, "review_state", "In Review")
            )
            if state_id and hasattr(self.linear, "update_issue_state"):
                self.linear.update_issue_state(issue_id, state_id)
            if hasattr(self.linear, "add_comment"):
                self.linear.add_comment(issue_id, body)
            self.registry.record_linear_event(task_id, event_key, body_hash, state="WRITTEN")
            return "PASS"
        except Exception as exc:
            self.registry.record_linear_event(
                task_id, event_key, body_hash, state="FAILED",
                retry_required=True, last_error=str(exc)[:2000],
            )
            return "DEGRADED"

    def _linear_failure_writeback(self, task_id: str, *, state: str, detail: str) -> str:
        index = self.registry.get_task_index(task_id)
        if index is None or self.linear is None:
            return "NOT_CONFIGURED"
        body = (
            "CLINX_EXECUTION_FAILURE_V1\n"
            f"TASK_REF={task_id}\nSTATE={state}\n"
            f"RETRY_REQUIRED={'YES' if state == 'RECOVERY_REQUIRED' else 'NO'}\n"
            f"BLOCKER={detail[:2000]}\n"
            "LINEAR_AUDIT_SYNC=DEGRADED\n"
        )
        event_key = f"failure:{state.lower()}:{hashlib.sha256(detail.encode()).hexdigest()[:16]}"
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        existing = self.registry.get_linear_event(task_id, event_key)
        if existing and existing["state"] == "WRITTEN":
            return "PASS"
        self.registry.record_linear_event(task_id, event_key, body_hash, state="PENDING")
        try:
            if hasattr(self.linear, "add_comment"):
                self.linear.add_comment(index.issue_id, body)
            self.registry.record_linear_event(task_id, event_key, body_hash, state="WRITTEN")
            self._linear_transition(task_id, index.issue_id, "IN_PROGRESS" if state == "RECOVERY_REQUIRED" else "TODO")
            return "PASS"
        except Exception as exc:
            self.registry.record_linear_event(
                task_id, event_key, body_hash, state="FAILED", retry_required=True,
                last_error=str(exc)[:2000],
            )
            return "DEGRADED"

    @staticmethod
    def _execution_public(
        *,
        prepared: Any,
        execution_ref: str,
        task_ref: str,
        dispatch_status: str,
        linear_audit: str,
        routing_identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "execution_started": True,
            "prepared_execution_ref": prepared.prepared_execution_ref,
            "execution_ref": execution_ref,
            "task_ref": task_ref,
            "task_action": prepared.task_action,
            "model": prepared.model,
            "reasoning_effort": prepared.reasoning_effort,
            "execution_mode": prepared.execution_mode,
            "network_access": bool(prepared.network_access),
            "NETWORK_ACCESS": "ENABLED" if prepared.network_access else "DISABLED",
            "routing_identity": routing_identity,
            "dispatch_status": dispatch_status,
            "linear_audit": linear_audit,
            "read_only": False,
        }

    def start_execution(
        self,
        *,
        prepared_execution_ref: str,
        approved: bool = False,
    ) -> dict[str, Any]:
        """Start exactly one previously prepared CLINX command."""
        if approved is not True:
            raise M9IntegrationError("explicit approved=true is required for execution")
        if not isinstance(prepared_execution_ref, str) or not prepared_execution_ref.strip():
            raise M9IntegrationError("prepared_execution_ref is required")
        prepared = self.registry.verify_prepared_execution(prepared_execution_ref)
        if prepared.status == "DISPATCHED":
            if not prepared.resulting_execution_ref or not prepared.resulting_task_id:
                raise M9IntegrationError("dispatched preparation has incomplete result")
            return self._execution_public(
                prepared=prepared,
                execution_ref=prepared.resulting_execution_ref,
                task_ref=prepared.resulting_task_id,
                dispatch_status="DISPATCHED_REPLAY",
                linear_audit="NOT_REPEATED",
                routing_identity=(
                    self.registry.get_execution_routing_identity(
                        prepared.resulting_execution_ref
                    ).public_dict()
                    if prepared.resulting_execution_ref
                    and self.registry.get_execution_routing_identity(
                        prepared.resulting_execution_ref
                    ) is not None else None
                ),
            )
        self.registry.mark_prepared_execution_running(prepared_execution_ref)
        execution_ref = "exec_" + prepared_execution_ref.removeprefix("prepared_")
        try:
            task_id = prepared.task_ref
            dispatch_mode = "new" if prepared.task_action == "create" else "continue"
            duplicate = None
            # Resolve the canonical worktree before dispatch so a second
            # mutating command cannot reach app-server turn/start.
            if hasattr(self.dispatcher, "resolve_project"):
                _workspace, descriptor, _mapping = self.dispatcher.resolve_project(
                    prepared.project, host=prepared.host, project_mode="existing"
                )
                conflict = self.registry.active_worktree_conflict(
                    host=prepared.host, cwd=str(descriptor.cwd),
                    repository_origin=descriptor.repository_origin,
                    exclude_task_id=task_id if prepared.task_action == "create" else None,
                )
                if conflict is not None and prepared.task_action != "create":
                    if conflict.get("active_execution_ref") is None:
                        reconcile_task = getattr(self.dispatcher, "reconcile_task", None)
                        if callable(reconcile_task):
                            try:
                                reconcile_task(conflict["active_task_id"])
                            except Exception:
                                # The conflict remains authoritative when the
                                # bounded owner inspection is uncertain.
                                pass
                            conflict = self.registry.active_worktree_conflict(
                                host=prepared.host, cwd=str(descriptor.cwd),
                                repository_origin=descriptor.repository_origin,
                                exclude_task_id=task_id if prepared.task_action == "create" else None,
                            )
                    if conflict is not None:
                        return {
                            "execution_started": False,
                            "status": "WORKTREE_CONFLICT",
                            "worktree_lease": "ACTIVE",
                            "active_task_ref": conflict["active_task_id"],
                            "active_execution_ref": conflict.get("active_execution_ref"),
                            "active_stage": conflict.get("stage"),
                            "read_only": False,
                        }
                if prepared.task_action == "create":
                    fingerprint = self.registry.canonical_work_item_fingerprint(
                        host=prepared.host, project_alias=descriptor.alias,
                        cwd=str(descriptor.cwd), repository_origin=descriptor.repository_origin,
                        title=prepared.title, summary=prepared.summary,
                    )
                    duplicate = self.registry.find_active_work_item(fingerprint=fingerprint)
                    if duplicate is None:
                        fingerprint = self.registry.canonical_work_item_fingerprint(
                            host=prepared.host, project_alias=descriptor.alias,
                            cwd=str(descriptor.cwd), repository_origin=descriptor.repository_origin,
                            title=prepared.title, summary=prepared.summary,
                            task_key=f"{descriptor.alias.casefold()}/{re.sub(r'[^a-z0-9]+', '-', prepared.title.casefold()).strip('-') or 'task'}",
                        )
                        duplicate = self.registry.find_active_work_item(fingerprint=fingerprint)
                    if duplicate is not None:
                        if duplicate.codex_running or duplicate.execution_state in {
                            "CLAIMED", "DISPATCHING", "TURN_STARTED", "CODEX_RUNNING"
                        }:
                            active = self.registry.get_latest_execution_for_task(duplicate.task_id) or {}
                            return {
                                "execution_started": False,
                                "status": "DUPLICATE_ACTIVE",
                                "duplicate_prevented": True,
                                "task_ref": duplicate.task_id,
                                "execution_ref": active.get("execution_ref"),
                                "stage": duplicate.current_stage,
                                "linear_audit": "NOT_ATTEMPTED",
                                "read_only": False,
                            }
                        task_id = duplicate.task_id
                        dispatch_mode = "continue"
                if conflict is not None and duplicate is None:
                    return {
                        "execution_started": False,
                        "status": "WORKTREE_CONFLICT",
                        "worktree_lease": "ACTIVE",
                        "active_task_ref": conflict["active_task_id"],
                        "active_execution_ref": conflict.get("active_execution_ref"),
                        "active_stage": conflict.get("stage"),
                        "read_only": False,
                    }
            if task_id and dispatch_mode == "continue":
                # Ensure a continuing/reopened task has its one audit mirror
                # before any provider failure can require recovery writeback.
                self._audit_task_index(task_id)
            if prepared.task_action == "reopen":
                if not task_id:
                    raise M9IntegrationError("reopen preparation has no task reference")
                task = self.registry.get_task(task_id)
                if task.status in {"COMPLETED", "ARCHIVED"}:
                    self.dispatcher.task_action(task_id, "reopen")
                elif task.status != "ACTIVE":
                    raise M9IntegrationError(f"Unsupported task status: {task.status}")
            result = self.dispatcher.dispatch(
                project_ref=prepared.project,
                host=prepared.host,
                project_mode="existing",
                task_mode=dispatch_mode,
                task_id=None if dispatch_mode == "new" else task_id,
                prompt=prepared.prompt,
                title=prepared.title,
                summary=prepared.summary,
                model=prepared.model,
                reasoning_effort=prepared.reasoning_effort,
                execution_mode=prepared.execution_mode,
                network_access=bool(prepared.network_access),
                execution_ref=execution_ref,
                routing_identity=prepared.routing_identity_json,
            )
            if not getattr(result, "task_id", None) or not getattr(result, "thread_id", None):
                raise M9IntegrationError("dispatcher returned incomplete execution identity")
            self.registry.complete_prepared_execution(
                prepared_execution_ref,
                task_id=result.task_id,
                thread_id=result.thread_id,
                turn_id=result.turn_id,
                execution_ref=execution_ref,
            )
            try:
                self.registry.set_execution_state(
                    result.task_id, "TURN_STARTED", current_stage="TURN_STARTED",
                    current_blocker=None, codex_running=False, turn_id=result.turn_id,
                    retry_required=False,
                )
                self.registry.set_execution_state(
                    result.task_id, "CODEX_RUNNING", current_stage="Codex turn",
                    current_blocker=None, codex_running=True, turn_id=result.turn_id,
                    retry_required=False,
                )
            except TaskRegistryError:
                # Dispatcher-owned state remains authoritative for lightweight
                # adapters that do not persist the task locally.
                pass
        except WorktreeExecutionBusy as exc:
            self.registry.restore_prepared_execution(prepared_execution_ref)
            return {
                "execution_started": False,
                "status": "WORKTREE_CONFLICT",
                "worktree_lease": "ACTIVE",
                "active_task_ref": exc.active_task_id,
                "active_execution_ref": exc.active_execution_ref,
                "active_stage": exc.stage,
                "read_only": False,
            }
        except TaskExecutionBusy as exc:
            self.registry.restore_prepared_execution(prepared_execution_ref)
            active = self.registry.get_latest_execution_for_task(task_id) if task_id else None
            if active is not None:
                return {
                    "execution_started": False,
                    "status": "ACTIVE_EXECUTION_CONFLICT",
                    "active_task_ref": task_id,
                    "active_execution_ref": active.get("execution_ref"),
                    "active_stage": active.get("stage"),
                    "read_only": False,
                }
            raise exc
        except AppServerError:
            if task_id:
                self._linear_failure_writeback(
                    task_id, state="RECOVERY_REQUIRED", detail="recoverable app-server failure"
                )
            self.registry.restore_prepared_execution(prepared_execution_ref)
            raise
        except Exception:
            if task_id:
                self._linear_failure_writeback(
                    task_id, state="BLOCKED", detail="CLINX dispatch failure"
                )
            self.registry.fail_prepared_execution(prepared_execution_ref)
            raise
        audit = self._audit_task_index(result.task_id)
        index = self.registry.get_task_index(result.task_id)
        if index is not None:
            self._linear_transition(result.task_id, index.issue_id, "IN_PROGRESS")
        current = self.registry.get_prepared_execution(prepared_execution_ref)
        assert current is not None
        return self._execution_public(
            prepared=current,
            execution_ref=execution_ref,
            task_ref=result.task_id,
            dispatch_status=result.dispatch_status,
            linear_audit=audit,
            routing_identity=(
                self.registry.get_execution_routing_identity(execution_ref).public_dict()
                if self.registry.get_execution_routing_identity(execution_ref) is not None
                else None
            ),
        )

    def cancel_execution(self, *, execution_ref: str) -> dict[str, Any]:
        """Cancel one active opaque CLINX execution; never accepts raw IDs."""
        if not isinstance(execution_ref, str) or not execution_ref.startswith("exec_"):
            raise M9IntegrationError("execution_ref must be an opaque CLINX execution reference")
        terminal_record = self.registry.get_execution_record(execution_ref)
        if terminal_record is not None and terminal_record.get("stage") == "CANCELLED":
            return {
                "execution_cancelled": True,
                "execution_ref": execution_ref,
                "task_ref": terminal_record["task_id"],
                "status": "CANCELLED",
                "cancel_requested": True,
                "cancel_confirmed": True,
                "retry_required": False,
                "idempotent": True,
                "read_only": False,
            }
        active = self.registry.get_active_execution(execution_ref)
        if active is None:
            raise M9IntegrationError(f"Unknown or inactive execution: {execution_ref}")
        if active.get("stage") == "CANCELLED":
            return {
                "execution_cancelled": True,
                "execution_ref": execution_ref,
                "task_ref": active["task_id"],
                "status": "CANCELLED",
                "idempotent": True,
                "read_only": False,
            }
        if active.get("stage") in {"CANCEL_REQUESTED", "CANCELLATION_PENDING"}:
            return {
                "execution_cancelled": False,
                "execution_ref": execution_ref,
                "task_ref": active["task_id"],
                "status": "CANCELLATION_PENDING",
                "cancel_requested": True,
                "cancel_confirmed": False,
                "retry_required": True,
                "idempotent": True,
                "read_only": False,
            }
        # Persist intent before invoking any provider code.  This is the
        # durable contract even when the app-server transport is unavailable.
        requested = self.registry.request_cancellation(execution_ref)
        cancel = getattr(self.dispatcher, "cancel_execution", None)
        try:
            if callable(cancel):
                result = cancel(execution_ref)
            else:
                task = self.registry.finalize_cancellation(execution_ref)
                result = {
                    "execution_ref": execution_ref,
                    "task_ref": task.task_id,
                    "status": "CANCELLED",
                    "cancel_requested": True,
                    "cancel_confirmed": True,
                }
        except Exception as exc:
            pending = self.registry.mark_cancellation_pending(execution_ref, evidence=str(exc))
            result = {
                "execution_ref": execution_ref,
                "task_ref": pending.task_id,
                "status": "CANCELLATION_PENDING",
                "cancel_requested": True,
                "cancel_confirmed": False,
                "retry_required": True,
            }
        task_ref = result.get("task_ref", requested.task_id)
        index = self.registry.get_task_index(task_ref)
        if index is not None:
            # Cancellation remains a CLINX state; Linear receives a bounded
            # audit event but is never allowed to gate or alter execution.
            self._linear_transition(task_ref, index.issue_id, "TODO")
        return {
            "execution_cancelled": result.get("status") == "CANCELLED",
            "execution_ref": execution_ref,
            "task_ref": task_ref,
            "status": result.get("status", "CANCELLATION_PENDING"),
            "cancel_requested": True,
            "cancel_confirmed": bool(result.get("cancel_confirmed", False)),
            "retry_required": bool(result.get("retry_required", False)),
            "idempotent": active.get("stage") in {"CANCEL_REQUESTED", "CANCELLATION_PENDING"},
            "routing_identity": (
                self.registry.get_execution_routing_identity(execution_ref).public_dict()
                if self.registry.get_execution_routing_identity(execution_ref) is not None
                else None
            ),
            "read_only": False,
        }

    def execute(
        self,
        *,
        prompt: str,
        execution_ref: str,
        approved: bool = False,
        task_mode: str = "continue",
        task_ref: str | None = None,
        query: str | None = None,
        host: str | None = None,
        project: str | None = None,
        title: str | None = None,
        summary: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        execution_mode: str = "normal",
    ) -> dict[str, Any]:
        if approved is not True:
            raise M9IntegrationError("explicit approved=true is required for execution")
        if not prompt.strip() or not execution_ref.strip():
            raise M9IntegrationError("explicit prompt and execution_ref are required")
        if task_mode not in {"new", "continue"}:
            raise M9IntegrationError("task_mode must be new or continue")
        if task_mode == "continue":
            task = self.context_reader.resolve_task(
                task_ref=task_ref,
                query=query,
                project=project,
                host=host,
            )
            project_ref = task.project_alias
            dispatch_task_id = task.task_id
            dispatch_title = task.title
            dispatch_summary = summary if summary is not None else task.summary
        else:
            if not project or not title:
                raise M9IntegrationError("new task execution requires project and title")
            project_ref = project
            dispatch_task_id = None
            dispatch_title = title
            dispatch_summary = summary
        result = self.dispatcher.dispatch(
            project_ref=project_ref,
            host=host if task_mode == "new" else task.host,
            project_mode="existing",
            task_mode=task_mode,
            task_id=dispatch_task_id,
            prompt=prompt,
            title=dispatch_title,
            summary=dispatch_summary,
            model=model,
            reasoning_effort=reasoning_effort,
            execution_mode=execution_mode,
            issue_id=execution_ref,
            execution_ref=execution_ref,
        )
        return {
            "execution_ref": execution_ref,
            "task_ref": result.task_id,
            "dispatch_status": result.dispatch_status,
            "conversation_binding_preserved": task_mode == "continue",
            "task_created": bool(getattr(result, "thread_created", False)),
        }

    def _public(self, task: Any) -> dict[str, Any]:
        context_available = bool(
            self.registry.get_binding(task.task_id)
            or self.registry.latest_context_checkpoint(task.task_id)
        )
        return {
            "task_ref": task.task_id,
            "task_key": task.task_key,
            "host": task.host,
            "project": task.project_alias,
            "project_name": task.project_name,
            "title": task.title,
            "summary": task.summary,
            "status": task.status,
            "updated_at": task.updated_at,
            "context_available": context_available,
            "execution_state": task.execution_state,
            "current_stage": task.current_stage,
            "current_blocker": task.current_blocker,
            "last_progress_at": task.last_progress_at,
            "codex_running": bool(task.codex_running),
            "retry_required": bool(task.retry_required),
            "failure_stage": task.failure_stage,
            "failure_code": task.failure_code,
            "failure_evidence": task.failure_evidence,
            "routing_identity": self._public_route(task.routing_identity_json),
        }


def clinx_find_task(integration: ClinxIntegration, query: str, **kwargs: Any) -> dict[str, Any]:
    return integration.find_task(query, **kwargs)


def clinx_get_context(integration: ClinxIntegration, **kwargs: Any) -> dict[str, Any]:
    return integration.get_context(**kwargs)


def clinx_get_topic_status(integration: ClinxIntegration, **kwargs: Any) -> dict[str, Any]:
    return integration.get_topic_status(**kwargs)


def clinx_list_projects(integration: ClinxIntegration, **kwargs: Any) -> dict[str, Any]:
    return integration.list_projects(**kwargs)


def clinx_get_status(integration: ClinxIntegration, **kwargs: Any) -> dict[str, Any]:
    return integration.get_status(**kwargs)


def clinx_get_capabilities(integration: ClinxIntegration, **kwargs: Any) -> dict[str, Any]:
    return integration.get_capabilities(**kwargs)


def clinx_prepare_execution(integration: ClinxIntegration, **kwargs: Any) -> dict[str, Any]:
    return integration.prepare_execution(**kwargs)


def clinx_start_execution(integration: ClinxIntegration, **kwargs: Any) -> dict[str, Any]:
    return integration.start_execution(**kwargs)


def clinx_cancel_execution(integration: ClinxIntegration, **kwargs: Any) -> dict[str, Any]:
    return integration.cancel_execution(**kwargs)


def clinx_execute(integration: ClinxIntegration, **kwargs: Any) -> dict[str, Any]:
    return integration.execute(**kwargs)
