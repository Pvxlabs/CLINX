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

from task_registry import TaskRegistry, TaskRegistryError


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
    title: str
    summary: str
    prompt: str
    linear_project: str
    team_id: str
    trigger_label: str
    todo_state: str
    description: str
    prepared_execution_ref: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "execution_available": True,
            "command_plane": "CLINX",
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
            "title": self.title,
            "summary": self.summary,
            "prompt": self.prompt,
            "description": self.description,
            "next_action": {
                "provider": "CLINX",
                "operation": "START_EXECUTION",
                "required": True,
            },
            "linear_audit": {"optional": True, "purpose": "audit/history compatibility"},
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
        if execution_ref:
            execution = self.registry.get_execution_result(execution_ref)
            if execution is None:
                raise M9IntegrationError(f"Unknown execution ref: {execution_ref}")
            task = self.registry.get_task(execution.task_id)
        else:
            task = self.context_reader.resolve_task(
                task_ref=task_ref,
                query=query,
                project=project,
                host=host,
            )
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
        status.update(
            {
                "EXECUTION_STATE": task.execution_state,
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
        selected_model = self._validated_text("model", model, required=False) or "gpt-5.6-luna"
        selected_reasoning = (
            self._validated_text("reasoning_effort", reasoning_effort, required=False)
            or self._validated_text("reasoning", reasoning, required=False)
            or "high"
        )

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
        else:
            selected_host = self._validated_text("host", host)
            selected_project = self._validated_text("project", project)
            selected_title = self._validated_text("title", title)
            selected_summary = self._validated_text("summary", summary, required=False)
            selected_action = "create"
            selected_ref = None
            if not hasattr(self.dispatcher, "resolve_project"):
                raise M9IntegrationError("project resolver is not configured")
            self.dispatcher.resolve_project(
                selected_project,
                host=selected_host,
                project_mode="existing",
            )

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
            reasoning_effort=selected_reasoning,
            execution_mode=execution_mode,
        )
        return ExecutionHandoff(
            task_action=selected_action,
            task_ref=selected_ref,
            host=selected_host,
            project=selected_project,
            model=selected_model,
            reasoning=selected_reasoning,
            execution_mode=execution_mode,
            title=selected_title,
            summary=selected_summary,
            prompt=prompt,
            linear_project=linear_project,
            team_id=str(getattr(self.cfg, "team_id", "")),
            trigger_label=str(getattr(self.cfg, "trigger_label", "local-codex")),
            todo_state=str(getattr(self.cfg, "todo_state", "Todo")),
            description="\n".join(description_lines),
            prepared_execution_ref=prepared.prepared_execution_ref,
        ).as_dict()

    def _audit_task_index(self, task_id: str) -> str:
        """Best-effort Linear audit; never gates Codex dispatch."""
        if self.linear is None:
            return "NOT_CONFIGURED"
        try:
            from bridge import LinearTaskIndex

            LinearTaskIndex(
                self.linear,
                self.registry,
                str(getattr(self.cfg, "team_id", "")),
            ).sync(task_id)
        except Exception:
            return "FAILED"
        return "PASS"

    @staticmethod
    def _execution_public(
        *,
        prepared: Any,
        execution_ref: str,
        task_ref: str,
        dispatch_status: str,
        linear_audit: str,
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
            )
        self.registry.mark_prepared_execution_running(prepared_execution_ref)
        execution_ref = "exec_" + prepared_execution_ref.removeprefix("prepared_")
        try:
            task_id = prepared.task_ref
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
                task_mode="new" if prepared.task_action == "create" else "continue",
                task_id=None if prepared.task_action == "create" else task_id,
                prompt=prepared.prompt,
                title=prepared.title,
                summary=prepared.summary,
                model=prepared.model,
                reasoning_effort=prepared.reasoning_effort,
                execution_mode=prepared.execution_mode,
                execution_ref=execution_ref,
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
        except Exception:
            self.registry.fail_prepared_execution(prepared_execution_ref)
            raise
        audit = self._audit_task_index(result.task_id)
        current = self.registry.get_prepared_execution(prepared_execution_ref)
        assert current is not None
        return self._execution_public(
            prepared=current,
            execution_ref=execution_ref,
            task_ref=result.task_id,
            dispatch_status=result.dispatch_status,
            linear_audit=audit,
        )

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


def clinx_execute(integration: ClinxIntegration, **kwargs: Any) -> dict[str, Any]:
    return integration.execute(**kwargs)
