"""Small, dependency-free MCP boundary for the CLINX M12 surface.

The server intentionally supports stdio only.  A stdio server is safe to run
locally and is deterministic for qualification, but it is not a remotely
discoverable ChatGPT endpoint by itself.  Remote exposure must be provided by
an authenticated, TLS-terminated, officially supported MCP transport outside
this repository; this module never opens a public listener.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable

import app_server
import bridge
from m9_integration import ClinxIntegration, M9IntegrationError
from task_registry import TaskRegistry, TaskRegistryError


MCP_PROTOCOL_VERSION = "2025-06-18"
SERVER_DISCOVER_PROTOCOL_VERSION = "2026-07-28"
SERVER_NAME = "clinx"
SERVER_VERSION = "m12"
READ_ONLY_TOOL_NAMES = (
    "clinx_find_task",
    "clinx_get_context",
    "clinx_get_topic_status",
    "clinx_list_projects",
    "clinx_get_status",
    "clinx_get_capabilities",
    "clinx_prepare_execution",
)
DEFAULT_TOOL_NAMES = READ_ONLY_TOOL_NAMES + ("clinx_start_execution", "clinx_cancel_execution")


class MCPServerError(RuntimeError):
    pass


class MCPRequestError(MCPServerError):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


def _json_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _routing_identity_schema() -> dict[str, Any]:
    """Public route shape; conversation bindings are intentionally redacted."""
    identity = lambda properties: _json_schema(properties)
    return _json_schema({
        "host": identity({
            "stable_identifier": {"type": "string"},
            "display": {"type": "string"},
            "machine_id": {"type": ["string", "null"]},
            "alias": {"type": "string"},
            "status": {"type": "string", "enum": ["KNOWN", "UNKNOWN"]},
        }),
        "surface": identity({
            "stable_identifier": {"type": "string"},
            "display": {"type": "string"},
            "capability_boundary": {"type": "string"},
            "status": {"type": "string"},
        }),
        "provider": identity({
            "stable_identifier": {"type": "string"},
            "display": {"type": "string"},
            "bounded_task_backend": {"type": "boolean"},
            "status": {"type": "string"},
        }),
        "transport": identity({
            "stable_identifier": {"type": "string"},
            "display": {"type": "string"},
            "remote": {"type": "boolean"},
            "status": {"type": "string"},
        }),
        "workspace": identity({
            "workspace_alias": {"type": "string"},
            "project_alias": {"type": "string"},
            "worktree_key": {"type": "string"},
        }),
        "conversation": identity({
            "status": {"type": "string"},
            "binding": {"type": "string", "const": "REDACTED"},
        }),
        "authority": identity({
            "stable_identifier": {"type": "string"},
            "scopes": {"type": "array", "items": {"type": "string"}},
            "status": {"type": "string"},
            "identity_is_authority": {"type": "boolean", "const": False},
        }),
        "network_policy": identity({
            "stable_identifier": {"type": "string"},
            "network_access": {"type": "boolean"},
            "mode": {"type": "string", "enum": ["ENABLED", "DISABLED"]},
            "remote_execution": {"type": "boolean", "const": False},
        }),
        "project_identity": {"type": "string"},
        "routing_contract": {"type": "string", "const": "execution -> host -> surface -> provider -> transport"},
        "separation": {"type": "object", "additionalProperties": {"type": "boolean"}},
    })


def _execution_policy_schema() -> dict[str, Any]:
    return _json_schema({
        "contract": {"type": "string", "const": "CLINX_EXECUTION_POLICY_V1"},
        "execution_surface": {
            "type": "string",
            "enum": ["SANDBOX_WORKSPACE", "NETWORKED_SANDBOX", "HOST_EXECUTOR"],
        },
        "required_capabilities": {"type": "array", "items": {"type": "string"}},
        "operation_classes": {"type": "array", "items": {"type": "string"}},
        "production_mutation_intent": {"type": "boolean"},
        "host_executor_default": {"type": "boolean", "const": False},
        "business_action_authority": {"type": "boolean", "const": False},
    })


def _public_task_schema() -> dict[str, Any]:
    """Describe the public task projection without exposing private identity."""
    return _json_schema({
        "task_ref": {"type": "string"},
        "task_key": {"type": ["string", "null"]},
        "host": {"type": "string"},
        "project": {"type": "string"},
        "project_name": {"type": "string"},
        "title": {"type": "string"},
        "summary": {"type": ["string", "null"]},
        "status": {"type": "string"},
        "updated_at": {"type": "string"},
        "context_available": {"type": "boolean"},
        "execution_state": {"type": "string"},
        "current_stage": {"type": "string"},
        "current_blocker": {"type": ["string", "null"]},
        "last_progress_at": {"type": "string"},
        "codex_running": {"type": "boolean"},
        "retry_required": {"type": "boolean"},
        "failure_stage": {"type": ["string", "null"]},
        "failure_code": {"type": ["string", "null"]},
        "failure_evidence": {"type": ["string", "null"]},
        "routing_identity": {"anyOf": [_routing_identity_schema(), {"type": "null"}]},
        "adoption_source": {"type": ["string", "null"]},
        "adopted_at": {"type": ["string", "null"]},
        "historical_status": {"type": ["string", "null"]},
        "historical_route_evidence": {"type": ["string", "null"]},
    })


def _context_output_schema() -> dict[str, Any]:
    return _json_schema({
        "task_context_read": {"type": "string", "const": "PASS"},
        "task_ref": {"type": "string"},
        "task_title": {"type": "string"},
        "task_status": {"type": "string"},
        "host": {"type": "string"},
        "project": {"type": "string"},
        "context_source": {"type": "string"},
        "context_range": {"type": "string"},
        "context_truncated": {"type": "boolean"},
        "checkpoint_stale": {"type": "boolean"},
        "last_user_intent": {"type": "string"},
        "last_codex_result": {"type": "string"},
        "changed_files": {"type": "string"},
        "validation": {"type": "string"},
        "blockers": {"type": "string"},
        "current_state": {"type": "string"},
        "ready_for_continuation": {"type": "boolean"},
        "provenance": {"type": "object", "additionalProperties": True},
        "routing_identity": {"anyOf": [_routing_identity_schema(), {"type": "null"}]},
        "read_only": {"type": "boolean", "const": True},
    })


def _topic_work_item_schema() -> dict[str, Any]:
    return _json_schema({
        "title": {"type": "string"},
        "source_kind": {"type": "string"},
        "registry_status": {"type": "string"},
        "conversation_status": {"type": "string"},
        "last_activity": {"type": "string"},
        "last_user_intent": {"type": "string"},
        "last_codex_result": {"type": "string"},
        "current_state": {"type": "string"},
        "blockers": {"type": "string"},
        "validation": {"type": "string"},
        "context_source": {"type": "string"},
        "context_range": {"type": "string"},
        "context_truncated": {"type": "boolean"},
        "task_ref": {"type": ["string", "null"]},
        "provenance": {"type": "object", "additionalProperties": True},
        "routing_identity": {"anyOf": [_routing_identity_schema(), {"type": "null"}]},
    })


def _topic_status_output_schema() -> dict[str, Any]:
    work = {"type": "array", "items": _topic_work_item_schema()}
    return _json_schema({
        "topic_status_read": {"type": "string", "const": "PASS"},
        "host": {"type": "string"},
        "project": {"type": "string"},
        "topic": {"type": "string"},
        "summary_state": {"type": "string"},
        "completed_work": work,
        "active_work": work,
        "blocked_work": work,
        "paused_work": work,
        "superseded_work": work,
        "unknown_work": work,
        "latest_activity": {"type": "string"},
        "source_count": {"type": "integer", "minimum": 0},
        "task_count": {"type": "integer", "minimum": 0},
        "conversation_count": {"type": "integer", "minimum": 0},
        "context_coverage": {"type": "string"},
        "context_truncated": {"type": "boolean"},
        "threads_screened": {"type": "integer", "minimum": 0},
        "topic_candidate_threads": {"type": "integer", "minimum": 0},
        "topic_matched_threads": {"type": "integer", "minimum": 0},
        "deduplication": {"type": "string"},
        "search_bounded": {"type": "boolean", "const": True},
        "read_only": {"type": "boolean", "const": True},
    })


def _status_output_schema() -> dict[str, Any]:
    execution_result = _json_schema({
        "status": {"type": "string"},
        "summary": {"type": "string"},
        "changed_files": {"type": "string"},
        "validation": {"type": "string"},
        "blockers": {"type": "string"},
        "next_state": {"type": "string"},
        "received_at": {"type": "string"},
        "writeback_state": {"type": "string"},
    })
    return _json_schema({
        **_public_task_schema()["properties"],
        "execution_result": {"anyOf": [execution_result, {"type": "null"}]},
        "routing_identity": {"anyOf": [_routing_identity_schema(), {"type": "null"}]},
        "execution_routing_identity": {"anyOf": [_routing_identity_schema(), {"type": "null"}]},
        "execution_policy": {"anyOf": [_execution_policy_schema(), {"type": "null"}]},
        "host_executions": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "EXECUTION_STATE": {"type": "string"},
        "CODEX_RUNNING": {"type": "boolean"},
        "CURRENT_STAGE": {"type": "string"},
        "CURRENT_BLOCKER": {"type": "string"},
        "LAST_PROGRESS_AT": {"type": "string"},
        "TURN_PRESENT": {"type": "boolean"},
        "RETRY_REQUIRED": {"type": "boolean"},
        "failure_stage": {"type": ["string", "null"]},
        "failure_code": {"type": ["string", "null"]},
        "failure_evidence": {"type": ["string", "null"]},
        "execution_ref": {"type": "string"},
        "active_execution": {"anyOf": [{"type": "object", "additionalProperties": True}, {"type": "null"}]},
        "linear_audit_sync": {"type": "string"},
        "linear_retry_required": {"type": "boolean"},
        "linear_last_error": {"type": ["string", "null"]},
        "linear_issue_ref": {"type": ["string", "null"]},
        "network_access": {"type": "boolean"},
        "NETWORK_ACCESS": {"type": "string", "enum": ["ENABLED", "DISABLED"]},
        "read_only": {"type": "boolean", "const": True},
    })


def _capabilities_output_schema() -> dict[str, Any]:
    return _json_schema({
        "context_plane": {"type": "object", "additionalProperties": True},
        "context_read_only": {"type": "boolean", "const": True},
        "execution_available": {"type": "boolean", "const": True},
        "command_plane": {"type": "string", "const": "CLINX"},
        "linear_role": {"type": "string", "const": "AUDIT"},
        "prepare_tool": {"type": "string", "const": "clinx_prepare_execution"},
        "start_tool": {"type": "string", "const": "clinx_start_execution"},
        "cancel_tool": {"type": "string", "const": "clinx_cancel_execution"},
        "status_tool": {"type": "string", "const": "clinx_get_status"},
        "execution": {
            "type": "object",
            "properties": {
                "available": {"type": "boolean", "const": True},
                "direct_mcp_execution": {"type": "boolean", "const": True},
                "command_plane": {"type": "string", "const": "CLINX"},
                "linear_role": {"type": "string", "const": "AUDIT"},
                "requires_user_approval": {"type": "boolean", "const": True},
                "prepare_tool": {"type": "string", "const": "clinx_prepare_execution"},
                "start_tool": {"type": "string", "const": "clinx_start_execution"},
                "cancel_tool": {"type": "string", "const": "clinx_cancel_execution"},
            },
            "required": [
                "available", "direct_mcp_execution", "command_plane",
                "requires_user_approval", "prepare_tool", "start_tool",
            ],
            "additionalProperties": False,
        },
        "execution_surfaces": {"type": "object", "additionalProperties": True},
        "host_executor_available": {"type": "boolean"},
        "host_executor_default": {"type": "boolean", "const": False},
        "status": {"type": "object", "additionalProperties": True},
        "linear": {"type": "object", "additionalProperties": True},
        "instructions": {"type": "string"},
        "read_only": {"type": "boolean", "const": True},
    })


def _prepare_output_schema() -> dict[str, Any]:
    handoff = _json_schema({
        "team": {"type": "string"},
        "project": {"type": "string"},
        "state": {"type": "string"},
        "labels": {"type": "array", "items": {"type": "string"}},
        "host": {"type": "string"},
        "project_alias": {"type": "string"},
        "task_action": {"type": "string", "enum": ["create", "continue", "reopen"]},
        "task_ref": {"type": ["string", "null"]},
        "model": {"type": "string"},
        "reasoning": {"type": "string"},
        "execution_mode": {"type": "string"},
        "network_access": {"type": "boolean"},
        "NETWORK_ACCESS": {"type": "string", "enum": ["ENABLED", "DISABLED"]},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "prompt": {"type": "string"},
        "description": {"type": "string"},
        "routing_identity": {"anyOf": [_routing_identity_schema(), {"type": "null"}]},
        "execution_policy": {"anyOf": [_execution_policy_schema(), {"type": "null"}]},
    })
    return _json_schema({
        "execution_available": {"type": "boolean", "const": True},
        "command_plane": {"type": "string", "const": "CLINX"},
        "requires_user_approval": {"type": "boolean", "const": True},
        "requires_command_write": {"type": "boolean", "const": False},
        "approval_state": {"type": "string", "const": "SATISFIED"},
        "handoff_ready": {"type": "boolean", "const": True},
        "task_action": {"type": "string", "enum": ["create", "continue", "reopen"]},
        "task_ref": {"type": ["string", "null"]},
        "host": {"type": "string"},
        "project": {"type": "string"},
        "model": {"type": "string"},
        "reasoning": {"type": "string"},
        "execution_mode": {"type": "string"},
        "network_access": {"type": "boolean"},
        "NETWORK_ACCESS": {"type": "string", "enum": ["ENABLED", "DISABLED"]},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "prompt": {"type": "string"},
        "description": {"type": "string"},
        "routing_identity": {"anyOf": [_routing_identity_schema(), {"type": "null"}]},
        "execution_policy": {"anyOf": [_execution_policy_schema(), {"type": "null"}]},
        "linear_handoff": handoff,
        "next_action": {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "const": "CLINX"},
                "operation": {"type": "string", "const": "START_EXECUTION"},
                "required": {"type": "boolean", "const": True},
            },
            "required": ["provider", "operation", "required"],
            "additionalProperties": False,
        },
        "status_lookup": {"type": "object", "additionalProperties": True},
        "read_only": {"type": "boolean", "const": True},
    })


MCP_INSTRUCTIONS = (
    "CLINX MCP is the authoritative read-only context and execution-preparation "
    "plane. execution.available=true means the execution capability exists; "
    "direct_mcp_execution=true is intentional. Execution uses CLINX as the "
    "command plane and requires explicit user approval. Resolve the exact task "
    "with CLINX, call clinx_prepare_execution, then call "
    "clinx_start_execution with only the returned prepared_execution_ref and "
    "approved=true. Linear role=AUDIT: it is the human-notification projection "
    "only and never gates execution. CLINX "
    "Use clinx_cancel_execution only with an opaque CLINX execution_ref returned "
    "by CLINX. It never accepts PID, shell, thread, turn, or cwd values. CLINX "
    "does not expose arbitrary execution inputs, and humans do not need task, "
    "thread, session, turn, or cwd IDs."
)


def _read_only_tool_definitions() -> list[dict[str, Any]]:
    """Return the public read-only context catalog."""
    task_selector = {
        "task_ref": {"type": "string", "description": "Opaque CLINX task reference."},
        "host": {"type": "string"},
        "project": {"type": "string"},
        "query": {"type": "string"},
    }
    return [
        {
            "name": "clinx_find_task",
            "description": "Find active or historical CLINX tasks by human query.",
            "inputSchema": _json_schema(
                {
                    "host": {"type": "string"},
                    "project": {"type": "string"},
                    "query": {"type": "string"},
                    "status": {"type": "string"},
                },
                ["query"],
            ),
            "outputSchema": _json_schema({
                "classification": {"type": "string"},
                "tasks": {"type": "array", "items": _public_task_schema()},
                "read_only": {"type": "boolean", "const": True},
            }),
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
        {
            "name": "clinx_get_context",
            "description": "Read bounded authoritative context for one CLINX task.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **task_selector,
                    "recent_turns": {"type": "integer", "minimum": 1, "maximum": 20},
                    "max_bytes": {"type": "integer", "minimum": 1024, "maximum": 128000},
                },
                "anyOf": [
                    {"required": ["task_ref"]},
                    {"required": ["query", "project"]},
                ],
                "additionalProperties": False,
            },
            "outputSchema": _context_output_schema(),
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
        {
            "name": "clinx_get_topic_status",
            "description": "Read bounded deterministic status for a project topic.",
            "inputSchema": _json_schema(
                {
                    "host": {"type": "string"},
                    "project": {"type": "string"},
                    "topic": {"type": "string"},
                    "include_completed": {"type": "boolean", "default": True},
                    "include_historical": {"type": "boolean", "default": True},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "recent_turns": {"type": "integer", "minimum": 1, "maximum": 20},
                    "max_bytes": {"type": "integer", "minimum": 1024, "maximum": 128000},
                },
                ["project", "topic"],
            ),
            "outputSchema": _topic_status_output_schema(),
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
        {
            "name": "clinx_list_projects",
            "description": "List registered and bounded workspace projects.",
            "inputSchema": _json_schema(
                {"host": {"type": "string"}, "query": {"type": "string"}},
            ),
            "outputSchema": _json_schema({
                "projects": {"type": "array", "items": _json_schema({
                    "project": {"type": "string"},
                    "name": {"type": "string"},
                    "workspace": {"type": "string"},
                    "host": {"type": "string"},
                    "availability": {"type": "string"},
                    "registered": {"type": "boolean"},
                })},
                "read_only": {"type": "boolean", "const": True},
            }),
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
        {
            "name": "clinx_get_status",
            "description": "Read task execution state and existing audit evidence.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **task_selector,
                    "execution_ref": {"type": "string"},
                },
                "anyOf": [
                    {"required": ["task_ref"]},
                    {"required": ["query", "project"]},
                    {"required": ["execution_ref"]},
                ],
                "additionalProperties": False,
            },
            "outputSchema": _status_output_schema(),
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
        {
            "name": "clinx_get_capabilities",
            "description": "Discover how CLINX context and execution work. CLINX is the command plane; Linear is optional audit history.",
            "inputSchema": _json_schema({}, []),
            "outputSchema": _capabilities_output_schema(),
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
        {
            "name": "clinx_prepare_execution",
            "description": "Prepare, but do not execute, a canonical CLINX command for a user-approved Codex task.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "approved": {"type": "boolean", "const": True},
                    "prompt": {"type": "string", "minLength": 1},
                    "task_mode": {"type": "string", "enum": ["new", "continue"]},
                    "task_action": {"type": "string", "enum": ["create", "continue", "reopen"]},
                    "task_ref": {"type": "string"},
                    "host": {"type": "string"},
                    "project": {"type": "string"},
                    "query": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "model": {"type": "string"},
                    "reasoning_effort": {"type": "string"},
                    "reasoning": {"type": "string"},
                    "execution_mode": {"type": "string", "enum": ["normal", "fast"]},
                    "network_access": {"type": "boolean", "default": False},
                    "execution_surface": {
                        "type": "string",
                        "enum": ["SANDBOX_WORKSPACE", "NETWORKED_SANDBOX", "HOST_EXECUTOR"],
                    },
                    "required_capabilities": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "operation_classes": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "production_mutation_intent": {"type": "boolean", "default": False},
                },
                "required": ["approved", "prompt"],
                "additionalProperties": False,
            },
            "outputSchema": _prepare_output_schema(),
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
        {
            "name": "clinx_start_execution",
            "description": (
                "Start exactly one previously prepared CLINX execution after explicit "
                "approval. This does not accept arbitrary thread or repository identity."
            ),
            "inputSchema": _json_schema(
                {
                    "prepared_execution_ref": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Opaque reference returned by clinx_prepare_execution.",
                    },
                    "approved": {"type": "boolean", "const": True},
                },
                ["prepared_execution_ref", "approved"],
            ),
            "outputSchema": _json_schema({
                "execution_started": {"type": "boolean"},
                "prepared_execution_ref": {"type": "string"},
                "execution_ref": {"type": "string"},
                "task_ref": {"type": "string"},
                "task_action": {"type": "string", "enum": ["create", "continue", "reopen"]},
                "model": {"type": "string"},
                "reasoning_effort": {"type": "string"},
                "execution_mode": {"type": "string", "enum": ["normal", "fast"]},
                "network_access": {"type": "boolean"},
                "NETWORK_ACCESS": {"type": "string", "enum": ["ENABLED", "DISABLED"]},
                "dispatch_status": {"type": "string"},
                "linear_audit": {"type": "string"},
                "status": {"type": "string"},
                "duplicate_prevented": {"type": "boolean"},
                "active_task_ref": {"type": "string"},
                "active_execution_ref": {"type": ["string", "null"]},
                "active_stage": {"type": "string"},
                "routing_identity": {"anyOf": [_routing_identity_schema(), {"type": "null"}]},
                "execution_policy": {"anyOf": [_execution_policy_schema(), {"type": "null"}]},
                "read_only": {"type": "boolean", "const": False},
            }),
            "annotations": {"readOnlyHint": False, "destructiveHint": True},
        },
        {
            "name": "clinx_cancel_execution",
            "description": (
                "Cancel one active CLINX-managed Codex turn by opaque execution_ref. "
                "PID, shell, thread, turn, and cwd inputs are not accepted."
            ),
            "inputSchema": _json_schema(
                {"execution_ref": {"type": "string", "pattern": "^exec_[A-Za-z0-9]+$"}},
                ["execution_ref"],
            ),
            "outputSchema": _json_schema({
                "execution_cancelled": {"type": "boolean"},
                "execution_ref": {"type": "string"},
                "task_ref": {"type": "string"},
                "status": {"type": "string", "enum": ["CANCELLED", "CANCELLATION_PENDING"]},
                "cancel_requested": {"type": "boolean", "const": True},
                "cancel_confirmed": {"type": "boolean"},
                "retry_required": {"type": "boolean"},
                "idempotent": {"type": "boolean"},
                "routing_identity": {"anyOf": [_routing_identity_schema(), {"type": "null"}]},
                "read_only": {"type": "boolean", "const": False},
            }),
            "annotations": {"readOnlyHint": False, "destructiveHint": True},
        },
    ]


def _execute_tool_definition() -> dict[str, Any]:
    """Return the internal/experimental execution schema when explicitly enabled."""
    return {
        "name": "clinx_execute",
        "description": (
            "Internal execution path. ChatGPT public context MCP does not expose this; "
            "execution belongs to the CLINX command plane; Linear is audit-only."
        ),
        "inputSchema": _json_schema(
            {
                "approved": {"type": "boolean", "const": True},
                "prompt": {"type": "string"},
                "execution_ref": {"type": "string"},
                "task_mode": {"type": "string", "enum": ["new", "continue"]},
                "task_ref": {"type": "string"},
                "host": {"type": "string"},
                "project": {"type": "string"},
                "query": {"type": "string"},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "model": {"type": "string"},
                "reasoning_effort": {"type": "string"},
                "execution_mode": {"type": "string", "enum": ["normal", "fast"]},
            },
            ["approved", "prompt", "execution_ref"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    }


def tool_definitions(*, include_execute: bool = False) -> list[dict[str, Any]]:
    """Return the public catalog, with execution opt-in for internal use only."""
    tools = _read_only_tool_definitions()
    if include_execute:
        tools.append(_execute_tool_definition())
    return tools


def _server_discover_result(public_tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the confirmed connector-discovery schema from the canonical registry."""
    names = tuple(tool["name"] for tool in public_tools)
    if names not in {DEFAULT_TOOL_NAMES, DEFAULT_TOOL_NAMES + ("clinx_execute",)}:
        raise MCPServerError("server/discover requires the canonical tool registry")
    return {
        "resultType": "complete",
        "supportedVersions": [SERVER_DISCOVER_PROTOCOL_VERSION],
        "capabilities": {"tools": {}},
        "_meta": {
            "io.modelcontextprotocol/serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
            },
        },
        "instructions": MCP_INSTRUCTIONS,
        "ttlMs": 3600000,
        "cacheScope": "public",
    }


def _public_json(value: Any) -> Any:
    """Defensive response scrubber for accidental internal-field leakage."""
    forbidden = {
        "thread_id", "threadId", "session_id", "sessionId", "turn_id", "turnId",
        "turn_ids", "item_ids", "message_ids", "execution_ids", "task_id", "taskId",
        "cwd", "origin", "repository_origin", "branch", "raw_result", "credentials",
        "token", "api_key", "authorization",
    }
    if isinstance(value, dict):
        return {
            key: _public_json(item)
            for key, item in value.items()
            if key not in forbidden
        }
    if isinstance(value, (list, tuple)):
        return [_public_json(item) for item in value]
    return value


class ClinxMCPServer:
    def __init__(
        self,
        integration: ClinxIntegration,
        *,
        allow_execute: bool = False,
        executor: Callable[..., dict[str, Any]] | None = None,
    ):
        self.integration = integration
        self.allow_execute = allow_execute
        self.executor = executor or integration.execute
        self.public_tools = tool_definitions(include_execute=allow_execute)
        self.public_tool_names = {
            tool["name"] for tool in self.public_tools
        }

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise MCPRequestError(-32602, "tool arguments must be an object")
        if name == "clinx_find_task":
            result = self.integration.find_task(**arguments)
        elif name == "clinx_get_context":
            result = self.integration.get_context(**arguments)
        elif name == "clinx_get_topic_status":
            result = self.integration.get_topic_status(**arguments)
        elif name == "clinx_list_projects":
            result = self.integration.list_projects(**arguments)
        elif name == "clinx_get_status":
            result = self.integration.get_status(**arguments)
        elif name == "clinx_get_capabilities":
            result = self.integration.get_capabilities(**arguments)
        elif name == "clinx_prepare_execution":
            result = self.integration.prepare_execution(**arguments)
        elif name == "clinx_start_execution":
            unexpected = set(arguments) - {"prepared_execution_ref", "approved"}
            if unexpected:
                raise TypeError(
                    "clinx_start_execution accepts only prepared_execution_ref and approved"
                )
            result = self.integration.start_execution(**arguments)
        elif name == "clinx_cancel_execution":
            unexpected = set(arguments) - {"execution_ref"}
            if unexpected:
                raise TypeError("clinx_cancel_execution accepts only execution_ref")
            result = self.integration.cancel_execution(**arguments)
        elif name == "clinx_execute":
            if not self.allow_execute:
                result = {
                    "execution_action_required": "LINEAR_HANDOFF",
                    "status": "BLOCKED",
                    "reason": (
                        "The public CLINX Context MCP is read-only; use the existing "
                        "CLINX command plane with Linear as audit-only projection."
                    ),
                    "read_only": True,
                }
            else:
                result = self.executor(**arguments)
        else:
            raise MCPRequestError(-32602, f"unknown tool: {name}")
        return _public_json(result)

    @staticmethod
    def _app_server_error_result(exc: app_server.AppServerError) -> dict[str, Any]:
        payload = {
            "error": str(exc),
            "error_type": type(exc).__name__,
            "codex_running": False,
            "retry_required": True,
        }
        if isinstance(exc, app_server.ModelCapabilityError):
            payload.update(
                failure_stage="MODEL_RESOLUTION",
                failure_code="MODEL_CAPABILITY_UNAVAILABLE",
            )
        elif isinstance(exc, app_server.AppServerTransportError):
            payload.update(
                failure_stage="APP_SERVER_TRANSPORT",
                failure_code="PROVIDER_UNAVAILABLE",
            )
        elif isinstance(exc, app_server.AppServerProtocolError):
            payload.update(
                failure_stage="APP_SERVER_PROTOCOL",
                failure_code="MODEL_LIST_MALFORMED",
            )
        else:
            payload.update(
                failure_stage="APP_SERVER_REQUEST",
                failure_code="APP_SERVER_REQUEST_ERROR",
            )
        return payload

    @staticmethod
    def _tool_result(value: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
        return {
            "resultType": "complete",
            "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, sort_keys=True)}],
            "structuredContent": value,
            "isError": is_error,
        }

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(request, dict):
            raise MCPRequestError(-32600, "request must be an object")
        request_id = request.get("id")
        method = request.get("method")
        if not isinstance(method, str):
            raise MCPRequestError(-32600, "method is required")
        params = request.get("params") or {}
        if not isinstance(params, dict):
            raise MCPRequestError(-32602, "params must be an object")

        if method == "notifications/initialized":
            return None
        if method == "ping":
            result: dict[str, Any] = {}
        elif method == "initialize":
            result = {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": MCP_INSTRUCTIONS,
            }
        elif method == "server/discover":
            result = _server_discover_result(self.public_tools)
        elif method == "tools/list":
            result = {
                "resultType": "complete",
                "tools": self.public_tools,
                "ttlMs": 3600000,
                "cacheScope": "public",
            }
        elif method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                raise MCPRequestError(-32602, "tools/call requires name")
            if name not in self.public_tool_names:
                if name == "clinx_execute":
                    raise MCPRequestError(-32602, f"tool is not exposed: {name}")
                raise MCPRequestError(-32602, f"unknown tool: {name}")
            arguments = params.get("arguments", {})
            try:
                result = self._tool_result(self._call_tool(name, arguments))
            except app_server.AppServerError as exc:
                result = self._tool_result(self._app_server_error_result(exc), is_error=True)
            except (M9IntegrationError, TaskRegistryError, bridge.BridgeError, KeyError, TypeError, ValueError) as exc:
                result = self._tool_result({"error": str(exc)}, is_error=True)
        else:
            raise MCPRequestError(-32601, f"method not found: {method}")

        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


def build_server(
    config_path: Path | str,
    *,
    task_db_path: Path | str | None = None,
    allow_execute: bool = False,
) -> ClinxMCPServer:
    cfg = bridge.BridgeConfig.load(Path(config_path).expanduser().resolve())
    registry = TaskRegistry(
        task_db_path
        or cfg.task_db_path
        or (Path.home() / ".local" / "state" / "clinx" / "tasks.sqlite3")
    )
    # The stdio launcher deliberately starts with a minimal environment.  The
    # service-owned runtime.env remains the configured secret source; it is
    # read locally and never returned through MCP or written to the registry.
    api_key = os.environ.get("LINEAR_API_KEY", "").strip()
    if not api_key:
        runtime_env = Path.home() / ".config" / "clinx" / "runtime.env"
        try:
            for line in runtime_env.read_text(encoding="utf-8").splitlines():
                match = re.fullmatch(r"\s*LINEAR_API_KEY\s*=\s*(.*?)\s*", line)
                if match:
                    api_key = match.group(1).strip().strip("'\"")
                    break
        except OSError:
            pass
    linear = bridge.LinearClient(api_key) if api_key else None
    dispatcher = bridge.TaskDispatcher(cfg, task_registry=registry, linear=linear)
    reader = bridge.TaskContextReader(cfg, registry)
    topic_reader = bridge.TopicStatusReader(cfg, registry)
    integration = ClinxIntegration(
        cfg, registry, dispatcher, reader, linear=linear, topic_reader=topic_reader
    )
    return ClinxMCPServer(integration, allow_execute=allow_execute)


def serve_stdio(server: ClinxMCPServer, stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    dispatcher = getattr(getattr(server, "integration", None), "dispatcher", None)
    start_completion = getattr(dispatcher, "start_completion_runtime", None)
    stop_completion = getattr(dispatcher, "stop_completion_runtime", None)
    if callable(start_completion):
        start_completion()
    try:
        for line in stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
                response = server.handle(request)
                if response is not None:
                    stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                    stdout.flush()
            except json.JSONDecodeError as exc:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
                stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
                stdout.flush()
            except MCPRequestError as exc:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": exc.code, "message": str(exc)}}
                if exc.data is not None:
                    response["error"]["data"] = _public_json(exc.data)
                stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
                stdout.flush()
    finally:
        if callable(stop_completion):
            stop_completion()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CLINX M12 MCP stdio server")
    parser.add_argument("--config", default="bridge.toml")
    parser.add_argument("--stdio", action="store_true", help="serve JSON-RPC over stdio")
    parser.add_argument("--allow-execute", action="store_true", help="enable explicit local action calls")
    args = parser.parse_args(argv)
    if not args.stdio:
        print("MCP_TRANSPORT=BLOCKED: only authenticated external transport may expose CLINX", file=sys.stderr)
        return 2
    try:
        serve_stdio(build_server(args.config, allow_execute=args.allow_execute))
    except Exception as exc:
        print(f"MCP_SERVER=BLOCKED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
