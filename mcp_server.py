"""Small, dependency-free MCP boundary for the CLINX M11 surface.

The server intentionally supports stdio only.  A stdio server is safe to run
locally and is deterministic for qualification, but it is not a remotely
discoverable ChatGPT endpoint by itself.  Remote exposure must be provided by
an authenticated, TLS-terminated, officially supported MCP transport outside
this repository; this module never opens a public listener.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Callable

import bridge
from m9_integration import ClinxIntegration, M9IntegrationError
from task_registry import TaskRegistry, TaskRegistryError


MCP_PROTOCOL_VERSION = "2025-06-18"
SERVER_DISCOVER_PROTOCOL_VERSION = "2026-07-28"
SERVER_NAME = "clinx"
SERVER_VERSION = "m11"
READ_ONLY_TOOL_NAMES = (
    "clinx_find_task",
    "clinx_get_context",
    "clinx_get_topic_status",
    "clinx_list_projects",
    "clinx_get_status",
    "clinx_get_capabilities",
    "clinx_prepare_execution",
)


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
        "EXECUTION_STATE": {"type": "string"},
        "CODEX_RUNNING": {"type": "boolean"},
        "CURRENT_STAGE": {"type": "string"},
        "CURRENT_BLOCKER": {"type": "string"},
        "LAST_PROGRESS_AT": {"type": "string"},
        "TURN_PRESENT": {"type": "boolean"},
        "RETRY_REQUIRED": {"type": "boolean"},
        "execution_ref": {"type": "string"},
        "read_only": {"type": "boolean", "const": True},
    })


def _capabilities_output_schema() -> dict[str, Any]:
    return _json_schema({
        "context_plane": {"type": "object", "additionalProperties": True},
        "execution": {
            "type": "object",
            "properties": {
                "available": {"type": "boolean", "const": True},
                "direct_mcp_execution": {"type": "boolean", "const": False},
                "command_plane": {"type": "string", "const": "LINEAR"},
                "requires_user_approval": {"type": "boolean", "const": True},
                "prepare_tool": {"type": "string", "const": "clinx_prepare_execution"},
            },
            "required": [
                "available", "direct_mcp_execution", "command_plane",
                "requires_user_approval", "prepare_tool",
            ],
            "additionalProperties": False,
        },
        "status": {"type": "object", "additionalProperties": True},
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
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "prompt": {"type": "string"},
        "description": {"type": "string"},
    })
    return _json_schema({
        "execution_available": {"type": "boolean", "const": True},
        "command_plane": {"type": "string", "const": "LINEAR"},
        "requires_user_approval": {"type": "boolean", "const": True},
        "approval_state": {"type": "string", "const": "SATISFIED"},
        "handoff_ready": {"type": "boolean", "const": True},
        "task_action": {"type": "string", "enum": ["create", "continue", "reopen"]},
        "task_ref": {"type": ["string", "null"]},
        "host": {"type": "string"},
        "project": {"type": "string"},
        "model": {"type": "string"},
        "reasoning": {"type": "string"},
        "execution_mode": {"type": "string"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "prompt": {"type": "string"},
        "description": {"type": "string"},
        "linear_handoff": handoff,
        "next_action": {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "const": "LINEAR"},
                "operation": {"type": "string", "const": "CREATE_ISSUE"},
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
    "direct_mcp_execution=false is intentional. Execution uses LINEAR as the "
    "command plane and requires explicit user approval. Resolve the exact task "
    "with CLINX, call clinx_prepare_execution, then use an available Linear "
    "issue-create capability for its returned handoff when approved. The "
    "handoff has next_action.provider=LINEAR and operation=CREATE_ISSUE. CLINX "
    "never writes Linear or executes Codex directly, and humans do not need "
    "task, thread, session, turn, or cwd IDs."
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
                ["host", "project", "topic"],
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
            "description": "Discover how CLINX context and execution work. Execution uses Linear as the command plane.",
            "inputSchema": _json_schema({}, []),
            "outputSchema": _capabilities_output_schema(),
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
        {
            "name": "clinx_prepare_execution",
            "description": "Prepare, but do not execute, a canonical Linear handoff for a user-approved Codex task.",
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
                },
                "required": ["approved", "prompt"],
                "additionalProperties": False,
            },
            "outputSchema": _prepare_output_schema(),
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
    ]


def _execute_tool_definition() -> dict[str, Any]:
    """Return the internal/experimental execution schema when explicitly enabled."""
    return {
        "name": "clinx_execute",
        "description": (
            "Internal execution path. ChatGPT public context MCP does not expose this; "
            "execution belongs to the authenticated Linear command plane."
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
    if names != READ_ONLY_TOOL_NAMES:
        raise MCPServerError("server/discover requires the canonical read-only tool registry")
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
        elif name == "clinx_execute":
            if not self.allow_execute:
                result = {
                    "execution_action_required": "LINEAR_HANDOFF",
                    "status": "BLOCKED",
                    "reason": (
                        "The public CLINX Context MCP is read-only; use the existing "
                        "authenticated Linear command and audit plane."
                    ),
                    "read_only": True,
                }
            else:
                result = self.executor(**arguments)
        else:
            raise MCPRequestError(-32602, f"unknown tool: {name}")
        return _public_json(result)

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
    dispatcher = bridge.TaskDispatcher(cfg, task_registry=registry)
    reader = bridge.TaskContextReader(cfg, registry)
    topic_reader = bridge.TopicStatusReader(cfg, registry)
    integration = ClinxIntegration(
        cfg, registry, dispatcher, reader, linear=None, topic_reader=topic_reader
    )
    return ClinxMCPServer(integration, allow_execute=allow_execute)


def serve_stdio(server: ClinxMCPServer, stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CLINX M11 MCP stdio server")
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
