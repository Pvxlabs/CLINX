"""Small, dependency-free MCP boundary for the CLINX M12 surface.

The server intentionally supports stdio only.  A stdio server is safe to run
locally and is deterministic for qualification, but it is not a remotely
discoverable ChatGPT endpoint by itself.  Remote exposure must be provided by
an authenticated, TLS-terminated, officially supported MCP transport outside
this repository; this module never opens a public listener.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable

import app_server
import bridge
from execution_policy import EXECUTION_SURFACES, HOST_CAPABILITIES, OPERATION_CLASSES
from m9_integration import ClinxIntegration, M9IntegrationError
from task_registry import TaskRegistry, TaskRegistryError


MCP_PROTOCOL_VERSION = "2025-06-18"
SERVER_DISCOVER_PROTOCOL_VERSION = "2026-07-28"
SERVER_NAME = "clinx"
SERVER_VERSION = "execution-authority-v1"
READ_ONLY_TOOL_NAMES = (
    "clinx_find_task",
    "clinx_get_context",
    "clinx_get_topic_status",
    "clinx_list_projects",
    "clinx_get_status",
    "clinx_get_capabilities",
    "clinx_prepare_execution",
)
DEFAULT_TOOL_NAMES = READ_ONLY_TOOL_NAMES + ("clinx_start_execution", "clinx_cancel_execution", "clinx_get_effective_authority", "clinx_prepare_policy_reauthorization", "clinx_apply_policy_reauthorization", "clinx_adopt_conversation")


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


def _operation_scope_schema():
    return _json_schema({
        'capability': {'type': 'string', 'enum': list(HOST_CAPABILITIES)},
        'operation': {'type': 'string', 'minLength': 1},
        'operation_class': {'type': 'string', 'enum': list(OPERATION_CLASSES)},
        'target': {'type': 'string'},
    }, ['capability', 'operation', 'operation_class', 'target'])


def _authority_tools():
    scope = {'type': 'array', 'items': _operation_scope_schema(), 'maxItems': 128}
    target = _json_schema({
        'execution_surface': {'type': 'string', 'enum': list(EXECUTION_SURFACES)},
        'required_capabilities': {'type': 'array', 'items': {'type': 'string', 'enum': list(HOST_CAPABILITIES)}},
        'operation_classes': {'type': 'array', 'items': {'type': 'string', 'enum': list(OPERATION_CLASSES)}},
        'production_mutation_intent': {'type': 'boolean'}, 'operation_scopes': scope,
    })
    return [
        {'name': 'clinx_get_effective_authority',
         'description': 'Read effective FUTURE execution authority for the exact canonical task. Separate policy, operation/target grants, client exposure and runtime health. Discovery never grants authority.',
         'inputSchema': _json_schema({'task_ref': {'type': 'string'}}, ['task_ref']),
         'annotations': {'readOnlyHint': True, 'destructiveHint': False}},
        {'name': 'clinx_prepare_policy_reauthorization',
         'description': 'Prepare a reviewed authority change on the SAME task. Existing authorization must cover the exact target scope. No execution starts; historical execution policies remain unchanged. Only the outer operator may call this, never the managed worker.',
         'inputSchema': _json_schema({'approved': {'type': 'boolean', 'const': True},
             'task_ref': {'type': 'string'}, 'expected_policy_hash': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
             'target_policy': target, 'network_access': {'type': 'boolean'}, 'reason': {'type': 'string', 'minLength': 1}},
             ['approved', 'task_ref', 'expected_policy_hash', 'target_policy', 'reason']),
         'annotations': {'readOnlyHint': False, 'destructiveHint': False}},
        {'name': 'clinx_apply_policy_reauthorization',
         'description': 'Apply exactly one prepared authority change with explicit operator approval, CAS and immutable audit. Idempotent. Active ownership or unresolved side effects block application. Does not execute or replay deployment.',
         'inputSchema': _json_schema({'approved': {'type': 'boolean', 'const': True},
             'prepared_reauthorization_ref': {'type': 'string'}}, ['approved', 'prepared_reauthorization_ref']),
         'annotations': {'readOnlyHint': False, 'destructiveHint': True}},
    ]


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
        "operation_scopes": {"type": "array", "items": _operation_scope_schema()},
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
        "provider_liveness": {"type": "string", "enum": ["LIVE", "TERMINAL", "UNKNOWN"]},
        "transport_health": {"type": "string", "enum": ["HEALTHY", "DEGRADED", "UNAVAILABLE"]},
        "last_provider_activity_at": {"type": ["string", "null"]},
        "last_host_delivery_at": {"type": ["string", "null"]},
        "last_live_owner_at": {"type": ["string", "null"]},
        "liveness_observed_at": {"type": ["string", "null"]},
        "liveness_reason": {"type": "string"},
        "ownership_conflict": {"type": "boolean"},
        "liveness_grace_seconds": {"type": "number"},
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
        "dynamic_tool_deliveries": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "provider_delivery": {"type": "object", "additionalProperties": True},
        "task_current_projection": {"type": "object", "additionalProperties": True},
        "selection_reason": {"type": "string"},
        "status_source": {"type": "string"},
        "execution_turn_ref": {"type": ["string", "null"]},
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
    "When a user supplies thread_id or codex://threads/..., call clinx_get_context or "
    "clinx_get_status with the exact selector directly. Never call clinx_find_task first "
    "or put the ID in query. Historical execution is separate from task_current_projection. "
    "Preserve Desktop URI hostId. Native reads require no task/project registration. "
    "Only an explicit adoption request calls clinx_adopt_conversation; adoption never starts a turn. "
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


def _read_only_tool_definitions(*, include_nodes: bool = False) -> list[dict[str, Any]]:
    """Return the public read-only context catalog."""
    task_selector = {
        "task_ref": {"type": "string", "description": "Opaque CLINX task reference."},
        "node_id": {"type": "string", "description": "Explicit trusted node identity for an exact native thread."},
        "host": {"type": "string"},
        "project": {"type": "string"},
        "query": {"type": "string"},
    }
    tools = [
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
                        "enum": list(EXECUTION_SURFACES),
                    },
                    "required_capabilities": {
                        "type": "array",
                        "items": {"type": "string", "examples": list(HOST_CAPABILITIES)},
                        "description": (
                            "Canonical values (case-insensitive, surrounding whitespace ignored): "
                            + ", ".join(HOST_CAPABILITIES)
                            + ". Discovery probe labels and operation names are not a request vocabulary."
                        ),
                    },
                    "operation_classes": {
                        "type": "array",
                        "items": {"type": "string", "examples": list(OPERATION_CLASSES)},
                        "description": (
                            "Canonical values (case-insensitive, surrounding whitespace ignored): "
                            + ", ".join(OPERATION_CLASSES)
                            + ". DEVELOPMENT_MUTATION for bounded repository commands; "
                            "production mutation requires explicit intent; BUSINESS_ACTION is denied."
                        ),
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
    if include_nodes:
        # Node tools are opt-in with a configured centre router so the legacy
        # connector catalog remains byte-for-byte compatible on P620 installs
        # that have not enabled multi-device routing.
        insert_at = next((i for i, item in enumerate(tools) if item["name"] == "clinx_get_status"), len(tools))
        tools[insert_at:insert_at] = [
            {
                "name": "clinx_list_nodes",
                "description": "List trusted CLINX nodes, protocol versions, capabilities and freshness.",
                "inputSchema": _json_schema({}, []),
                "outputSchema": _json_schema({
                    "nodes": {"type": "array", "items": {"type": "object"}},
                    "coverage": {"type": "string"},
                    "read_only": {"type": "boolean", "const": True},
                }),
                "annotations": {"readOnlyHint": True, "destructiveHint": False},
            },
            {
                "name": "clinx_get_node_status",
                "description": "Read one trusted node's reachability, provider, history and sharing scope state.",
                "inputSchema": _json_schema({"node_id": {"type": "string"}}, ["node_id"]),
                "outputSchema": _json_schema({
                    "node": {"type": ["object", "null"]},
                    "scope": {"type": ["object", "null"]},
                    "node_reachable": {"type": "boolean"},
                    "provider_reachable": {"type": "boolean"},
                    "history_readable": {"type": "boolean"},
                    "execution_active": {"type": "boolean"},
                    "read_only": {"type": "boolean", "const": True},
                }),
                "annotations": {"readOnlyHint": True, "destructiveHint": False},
            },
        ]
    return tools


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


def tool_definitions(*, include_execute: bool = False, include_nodes: bool = False) -> list[dict[str, Any]]:
    """Return the public catalog, with execution opt-in for internal use only."""
    tools = _read_only_tool_definitions(include_nodes=include_nodes) + _authority_tools()
    authority_outputs = {
        'task_ref': {'type': 'string'}, 'policy_version': {'type': 'integer'},
        'policy_hash': {'type': 'string'}, 'expected_policy_hash': {'type': 'string'},
        'prepared_reauthorization_ref': {'type': 'string'}, 'reauthorization_ref': {'type': 'string'},
        'idempotent': {'type': 'boolean'}, 'execution_started': {'type': 'boolean', 'const': False},
        'future_execution_policy': {'type': ['object', 'null']}, 'new_policy': {'type': 'object'},
        'previous_policy': {'type': 'object'}, 'requested_scope': {'type': 'object'},
        'effective_authority': {'type': 'object'}, 'operations': {'type': 'object'},
        'expires_at': {'type': 'string'}, 'network_access': {'type': 'boolean'},
        'backend_implemented': {'type': 'boolean'}, 'mcp_exposed': {'type': 'boolean'},
        'client_exposure': {'type': 'string'}, 'runtime_health': {'type': 'string'},
        'read_only': {'type': 'boolean'},
    }
    for tool in tools:
        if tool['name'] in {'clinx_get_effective_authority', 'clinx_prepare_policy_reauthorization', 'clinx_apply_policy_reauthorization'}:
            tool['outputSchema'] = _json_schema(authority_outputs)
    for tool in tools:
        if tool['name'] == 'clinx_prepare_execution':
            tool['inputSchema']['properties']['requested_operations'] = {
                'type': 'array', 'items': _operation_scope_schema(), 'maxItems': 128,
                'description': 'Structured intent and authorized exact operation/target scope. Required for workflow dispatch; never infer production authority from prompt keywords.'}
            tool['inputSchema']['properties']['network_access'].pop('default', None)
    from thread_identity import ID_PATTERN
    uri_schema = {"type": "string", "maxLength": 2048, "pattern": "^codex://threads/" + ID_PATTERN + r"(?:\?[^\s#]*)?$"}
    for tool in tools:
        if tool["name"] == "clinx_get_capabilities":
            tool["outputSchema"]["properties"]["thread_lookup"] = {"type": "object"}
        if tool["name"] not in {"clinx_get_context", "clinx_get_status"}:
            continue
        tool["description"] += (
            " When the user supplies thread_id or codex://threads/..., use the exact "
            "selector directly; do not call clinx_find_task first or put the ID in query. "
            "Thread reads are read-only and separate historical execution from task_current_projection."
        )
        schema = tool["inputSchema"]
        schema["properties"].update({
            "thread_id": {"type": "string", "pattern": "^" + ID_PATTERN + "$"},
            "codex_uri": uri_schema,
            "execution_ref": {"type": "string"},
        })
        legacy = schema.pop("anyOf")
        if tool["name"] == "clinx_get_context":
            schema["properties"]["cursor"] = {"type": "string", "maxLength": 1024}
            legacy = [{"allOf": [{"anyOf": legacy}, {"not": {"required": ["execution_ref"]}}]}]
        schema["anyOf"] = [
            {"anyOf": legacy, "not": {"anyOf": [{"required": ["thread_id"]}, {"required": ["codex_uri"]}, {"required": ["node_id"]}]}},
            {"required": ["thread_id"], "not": {"anyOf": [{"required": [k]} for k in ("codex_uri", "task_ref", "query")]}},
            {"required": ["codex_uri"], "not": {"anyOf": [{"required": [k]} for k in ("thread_id", "task_ref", "query")]}},
        ]
        nullable = {"type": ["string", "null"]}
        thread_properties = {k: nullable for k in (
            "queried_thread_id", "codex_uri", "node_id", "source_node_id", "lookup_status", "error_code", "unavailable_reason",
            "task_ref", "task_key", "host", "project", "workspace", "provider", "current_thread_id",
            "relationship", "predecessor_thread_id", "successor_thread_id", "adoption_source",
            "execution_ref", "execution_state", "selection_reason", "selection_scope", "execution_turn_ref",
            "status_source", "context_status", "context_source", "context_scope", "context_range",
            "context_unavailable_reason", "last_user_intent", "last_codex_result", "observed_at",
            "binding_status", "provider_existence", "absence_scope", "next_cursor", "failure_stage", "failure_code", "failure_evidence", "failure_source",
            "native_turn_state", "native_display_state", "native_status_source",
        )}
        thread_properties.update({k: {"type": "boolean"} for k in ("is_current_thread", "context_truncated", "read_only")})
        thread_properties.update({k: {"type": "array", "items": {"type": "string"}} for k in ("binding_sources", "other_execution_refs")})
        thread_properties.update({k: {"type": "array", "items": {"type": "object"}} for k in ("dynamic_tool_deliveries", "host_executions")})
        thread_properties.update({
            "provider_liveness": {"type": "string"},
            "transport_health": {"type": "string"},
            "codex_running": {"type": "boolean"},
            "ownership_conflict": {"type": "boolean"},
            "liveness_grace_seconds": {"type": "number"},
            "liveness_reason": {"type": "string"},
            **{key: {"type": ["string", "null"]} for key in (
                "last_provider_activity_at", "last_host_delivery_at", "last_live_owner_at", "liveness_observed_at")},
        })
        thread_properties.update({k: {"type": ["object", "null"]} for k in ("provider_observation", "native_thread", "native_status", "task_current_projection", "execution_result", "provenance", "provider_delivery")})
        # Keep one strict root object for connector discovery and legacy clients.
        tool["outputSchema"]["properties"].update(thread_properties)

    tools.append({
        "name": "clinx_adopt_conversation",
        "description": "Explicitly associate one native Codex thread with its canonical CLINX task. Accept the original Desktop URI including hostId. No project registration prerequisite, no new thread, resume, interrupt or execution; active owners keep control. Execution needs a separate current authorized request.",
        "inputSchema": {"type": "object", "additionalProperties": False,
            "properties": {"thread_id": {"type": "string", "pattern": "^" + ID_PATTERN + "$"},
                "codex_uri": uri_schema, "host": {"type": "string"}, "project": {"type": "string"},
                "title": {"type": "string", "maxLength": 240}, "summary": {"type": "string", "maxLength": 4000}},
            "oneOf": [{"required": ["thread_id"], "not": {"required": ["codex_uri"]}},
                      {"required": ["codex_uri"], "not": {"required": ["thread_id"]}}]},
        "outputSchema": {"type": "object", "properties": {
            "adoption_status": {"type": "string"}, "task_ref": {"type": ["string", "null"]},
            "control_transferred": {"type": "boolean"}, "execution_started": {"type": "boolean"}},
            "required": ["adoption_status", "control_transferred"], "additionalProperties": True},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    })
    if include_execute:
        tools.append(_execute_tool_definition())
    return tools


def _server_discover_result(public_tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the confirmed connector-discovery schema from the canonical registry."""
    names = tuple(tool["name"] for tool in public_tools)
    node_names = ("clinx_list_nodes", "clinx_get_node_status")
    canonical_names = tuple(name for name in names if name not in node_names)
    if canonical_names not in {DEFAULT_TOOL_NAMES, DEFAULT_TOOL_NAMES + ("clinx_execute",)}:
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


def _public_json(value: Any, *, native_identity: bool = False) -> Any:
    """Defensive response scrubber for accidental internal-field leakage."""
    forbidden = {
        "thread_id", "threadId", "session_id", "sessionId", "turn_id", "turnId",
        "turn_ids", "item_ids", "message_ids", "execution_ids", "task_id", "taskId",
        "cwd", "origin", "repository_origin", "branch", "raw_result", "credentials",
        "token", "api_key", "authorization",
    }
    if native_identity:
        forbidden -= {'thread_id', 'threadId', 'session_id', 'sessionId', 'turn_id', 'turnId', 'cwd'}
    if isinstance(value, dict):
        return {
            key: _public_json(item, native_identity=native_identity)
            for key, item in value.items()
            if key not in forbidden
        }
    if isinstance(value, (list, tuple)):
        return [_public_json(item, native_identity=native_identity) for item in value]
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
        self.public_tools = tool_definitions(
            include_execute=allow_execute,
            include_nodes=getattr(getattr(integration, "cfg", None), "node_router", None) is not None,
        )
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
        elif name == "clinx_list_nodes":
            result = self.integration.list_nodes()
        elif name == "clinx_get_node_status":
            result = self.integration.get_node_status(**arguments)
        elif name == "clinx_get_status":
            result = self.integration.get_status(**arguments)
        elif name == "clinx_adopt_conversation":
            result = self.integration.adopt_conversation(**arguments)
        elif name == "clinx_get_capabilities":
            result = self.integration.get_capabilities(**arguments)
        elif name == "clinx_get_effective_authority":
            result = self.integration.get_effective_authority(**arguments)
        elif name == "clinx_prepare_policy_reauthorization":
            result = self.integration.prepare_policy_reauthorization(**arguments)
        elif name == "clinx_apply_policy_reauthorization":
            result = self.integration.apply_policy_reauthorization(**arguments)
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
        return _public_json(result, native_identity=(name == 'clinx_adopt_conversation' or
            (name in {'clinx_get_context', 'clinx_get_status'} and
             any(k in arguments for k in ('thread_id', 'codex_uri')))))

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
                failure_stage=getattr(exc, 'method', 'APP_SERVER_PROTOCOL'),
                failure_code=getattr(exc, 'code', 'MODEL_LIST_MALFORMED'),
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
                payload = {"error": str(exc)}
                code = str(exc).split(':', 1)[0]
                if code in {'AUTHORITY_REAUTHORIZATION_REQUIRED', 'POLICY_REAUTHORIZATION_BLOCKED', 'PRODUCTION_SCOPE_REQUIRED',
                            'POLICY_IDENTITY_CONFLICT', 'TARGET_NOT_AUTHORIZED', 'HOST_EXECUTOR_UNAVAILABLE',
                            'WORKFLOW_EXECUTABLE_UNAVAILABLE', 'OPERATION_NOT_IMPLEMENTED'}:
                    payload.update({'failure_code': code, 'execution_started': False,
                        'evaluation_scope': 'PROPOSED_EXECUTION_ONLY_PRIOR_EVIDENCE_UNCHANGED',
                        'root_blocker': {'layer': 'AUTHORITY' if code.startswith(('AUTHORITY', 'POLICY', 'TARGET', 'PRODUCTION_SCOPE')) else 'CAPABILITY',
                                         'status': 'BLOCKED', 'reason': str(exc)},
                        'downstream': {'QUALIFICATION': 'NOT_RUN', 'CAPACITY': 'UNVERIFIED',
                                       'DEPLOYMENT': 'NOT_RUN', 'READBACK': 'NOT_RUN', 'OBSERVATION': 'NOT_RUN'}})
                result = self._tool_result(payload, is_error=True)
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
    if os.environ.get("CLINX_ENABLE_NODE_ROUTER") == "1":
        # Build the local node route in the same process as the MCP boundary.
        # The node index is separate from the task DB and stores identity/status
        # only; native history stays on its owner.
        from node_protocol import NodeRecord, NodeRegistry, NodeRouter, NodeService, SharingScope
        node_db = Path(str(registry.path) + ".nodes.sqlite3")
        node_registry = NodeRegistry(node_db)
        user_scope = os.environ.get("CLINX_USER_SCOPE", os.environ.get("USER", "default"))
        node_id = os.environ.get("CLINX_NODE_ID") or cfg.runtime_host or "local"
        node_record = NodeRecord(
            node_id=node_id,
            user_scope=user_scope,
            public_key_fingerprint=os.environ.get("CLINX_NODE_PUBLIC_KEY_FINGERPRINT", ""),
            route_ids=(cfg.runtime_host,),
            state="ONLINE",
            last_seen=dt.datetime.now(dt.timezone.utc).isoformat(),
        )
        local_cfg = dataclasses.replace(cfg, node_router=None)
        from thread_identity import ThreadIdentityReader
        native_reader = ThreadIdentityReader(local_cfg, registry.path, reader)
        local_service = NodeService(
            node_record,
            node_registry,
            scope=SharingScope(user_scope=user_scope, read_sessions=True, execute_tasks=False),
            read_thread=lambda thread_id, **kwargs: native_reader.read(context=True, thread_id=thread_id, **kwargs),
        )
        router = NodeRouter(node_registry, user_scope=user_scope, local_node_id=node_id)
        router.attach(node_record, reader=lambda request: local_service.handle(request), scope=local_service.scope)
        cfg = dataclasses.replace(cfg, node_router=router)
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
