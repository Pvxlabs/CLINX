"""Compatibility codec for read-only observations and explicit maintenance.

The normal business CLI execs the Rust clinx-mcp server using install-time JSON.
The injectable factory remains for qualified maintenance and retained tests;
it is not the normal MCP service or a second task authority.
"""

from __future__ import annotations

# Direct-file entrypoints use only this source/release package root.
if not __package__:
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

from clinx.authority.rust_authority import RustAuthorityError
from clinx.contracts.runtime_config import BridgeConfig
from clinx.contracts.runtime_contracts import BridgeError, M9IntegrationError, TaskRegistryError
from clinx.provider import app_server

MCP_PROTOCOL_VERSION = "2025-06-18"
SERVER_DISCOVER_PROTOCOL_VERSION = "2026-07-28"
SERVER_NAME = "clinx"
SERVER_VERSION = "native-conversation-relay-consultations-v1"
_REGISTRY = json.loads(Path(__file__).with_name('registry.json').read_text())
RUST_TOOL_NAMES = tuple(t['name'] for t in _REGISTRY['tools'])
DEFAULT_TOOL_NAMES = RUST_TOOL_NAMES


class MCPServerError(RuntimeError):
    pass


class MCPRequestError(MCPServerError):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


def tool_definitions(*, include_execute=False, include_nodes=False, rust_authority=True):
    """The Rust tool registry is the only business MCP surface."""
    import copy
    return copy.deepcopy(_REGISTRY['tools'])


def _authority_tools():
    names = {'clinx_prepare_policy_reauthorization', 'clinx_apply_policy_reauthorization',
             'clinx_register_derived_git_target', 'clinx_revoke_derived_git_target'}
    return [tool for tool in tool_definitions() if tool['name'] in names]


def _server_discover_result(public_tools):
    return dict(resultType='complete', supportedVersions=[SERVER_DISCOVER_PROTOCOL_VERSION],
                capabilities={'tools': {}},
                _meta={'io.modelcontextprotocol/serverInfo':
                       {'name': SERVER_NAME, 'version': SERVER_VERSION}},
                instructions=MCP_INSTRUCTIONS, ttlMs=3600000, cacheScope='public')


MCP_INSTRUCTIONS = _REGISTRY['instructions']


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
            # Both native cwd routes and historical Git seals are public.
            key: (dict(item) if key == 'workspace_contract' and isinstance(item, dict)
                  and 'cwd' in item and set(item) <= {'cwd', 'branch', 'head'}
                  or key == 'workspace_resolution' and isinstance(item, dict)
                  and set(item) <= {'cwd', 'branch', 'head', 'repository_identity', 'repository_root', 'repository_common_dir', 'worktree', 'observer_uid', 'observer_source'}
                  else _public_json(item, native_identity=native_identity))
            for key, item in value.items()
            if key not in forbidden
        }
    if isinstance(value, (list, tuple)):
        return [_public_json(item, native_identity=native_identity) for item in value]
    return value


class ClinxMCPServer:
    def __init__(
        self,
        integration: Any,
        *,
        allow_execute: bool = False,
        executor: Callable[..., dict[str, Any]] | None = None,
        observations=None,
    ):
        self.integration = integration
        self.observations = observations
        self.allow_execute = allow_execute
        self.executor = executor or integration.execute
        self.public_tools = tool_definitions(
            include_execute=allow_execute,
            include_nodes=getattr(getattr(integration, "cfg", None), "node_router", None) is not None,
            rust_authority=getattr(integration, 'rust_authority', False),
        )
        if observations is not None:
            from clinx.monitor.observation_mcp import observation_tools
            self.public_tools.extend(observation_tools())
        self.public_tool_names = {
            tool["name"] for tool in self.public_tools
        }

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise MCPRequestError(-32602, "tool arguments must be an object")
        if name in {"clinx_list_observations", "clinx_get_observation", "clinx_get_observation_context", "clinx_get_observation_activity"}:
            from clinx.monitor.observation_mcp import call_observation
            return call_observation(self.observations, name, arguments)
        if name == "clinx_find_task":
            result = self.integration.find_task(**arguments)
        elif name == "clinx_get_context":
            result = self.integration.get_context(**arguments)
        elif name == "clinx_list_projects":
            result = self.integration.list_projects(**arguments)
        elif name == "clinx_get_status":
            result = self.integration.get_status(**arguments)
        elif name == "clinx_adopt_conversation":
            result = self.integration.adopt_conversation(**arguments)
        elif name == "clinx_get_capabilities":
            result = self.integration.get_capabilities(**arguments)
        elif name == "clinx_get_effective_authority":
            result = self.integration.get_effective_authority(**arguments)
        elif name == "clinx_get_prepared_request":
            result = self.integration.get_prepared_request(**arguments)
        elif name == "clinx_prepare_policy_reauthorization":
            result = self.integration.prepare_policy_reauthorization(**arguments)
        elif name == "clinx_apply_policy_reauthorization":
            result = self.integration.apply_policy_reauthorization(**arguments)
        elif name == "clinx_register_derived_git_target":
            result = self.integration.register_derived_git_target(**arguments)
        elif name == "clinx_revoke_derived_git_target":
            result = self.integration.revoke_derived_git_target(**arguments)
        elif name == "clinx_prepare_execution":
            if ('workspace_contract' in arguments
                    and not getattr(self.integration, 'rust_authority', False)):
                from clinx.contracts.workspace_contract import validate_workspace_contract
                validate_workspace_contract(arguments['workspace_contract'])
            result = self.integration.prepare_execution(**arguments)
        elif name == "clinx_start_execution":
            unexpected = set(arguments) - {"prepared_execution_ref", "approved", "expected_request_hash", "expected_task_ref"}
            if unexpected:
                raise TypeError(
                    "clinx_start_execution accepts prepared_execution_ref, approved and optional review hash/task assertions"
                )
            result = self.integration.start_execution(**arguments)
        elif name == "clinx_cancel_execution":
            unexpected = set(arguments) - {"execution_ref", "task_ref", "native_turn_id", "approved"}
            if unexpected:
                raise TypeError("clinx_cancel_execution accepts execution_ref or an explicitly approved canonical native turn")
            result = self.integration.cancel_execution(**arguments)
        else:
            raise MCPRequestError(-32602, f"unknown tool: {name}")
        return _public_json(result, native_identity=(getattr(self.integration, 'rust_authority', False) or name in {'clinx_adopt_conversation', 'clinx_get_prepared_request'} or
            (name in {'clinx_get_context', 'clinx_get_status'} and
             any(k in arguments for k in ('thread_id', 'codex_uri')))))

    @staticmethod
    def _app_server_error_result(exc: app_server.AppServerError) -> dict[str, Any]:
        from clinx.contracts.dispatch_errors import safe_reason
        payload = {
            "error": safe_reason(exc),
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
            except RustAuthorityError as exc:
                result = self._tool_result({"error_code": str(exc).split(':', 1)[0],
                    "operation_id": getattr(exc, 'operation_id', None), "authority": "RUST",
                    "retry_required": False,
                    "reconciliation_required": str(exc) == 'DELIVERY_UNKNOWN'}, is_error=True)
            except app_server.AppServerError as exc:
                from clinx.contracts.dispatch_errors import failure_details
                payload = self._app_server_error_result(exc)
                payload.update(failure_details(name, arguments, exc, getattr(self.integration, 'registry', None)))
                if payload['outcome_certainty'] == 'UNKNOWN':
                    payload.update(codex_running=None, retry_required=False)
                result = self._tool_result(payload, is_error=True)
            except (M9IntegrationError, TaskRegistryError, BridgeError, KeyError, TypeError, ValueError, sqlite3.Error) as exc:
                from clinx.contracts.dispatch_errors import failure_details, safe_reason
                payload = {"error": safe_reason(exc)}
                if getattr(exc, "code", None):
                    payload.update(error_code=exc.code, execution_started=False)
                code = str(exc).split(':', 1)[0]
                if code in {'AUTHORITY_REAUTHORIZATION_REQUIRED', 'POLICY_REAUTHORIZATION_BLOCKED', 'PRODUCTION_SCOPE_REQUIRED',
                            'POLICY_IDENTITY_CONFLICT', 'TARGET_NOT_AUTHORIZED', 'HOST_EXECUTOR_UNAVAILABLE',
                            'WORKFLOW_EXECUTABLE_UNAVAILABLE', 'OPERATION_NOT_IMPLEMENTED'}:
                    payload.update({'failure_code': code, 'execution_started': False,
                        'evaluation_scope': 'PROPOSED_EXECUTION_ONLY_PRIOR_EVIDENCE_UNCHANGED',
                        'root_blocker': {'layer': 'AUTHORITY' if code.startswith(('AUTHORITY', 'POLICY', 'TARGET', 'PRODUCTION_SCOPE')) else 'CAPABILITY',
                                         'status': 'BLOCKED', 'reason': safe_reason(exc)},
                        'downstream': {'QUALIFICATION': 'NOT_RUN', 'CAPACITY': 'UNVERIFIED',
                                       'DEPLOYMENT': 'NOT_RUN', 'READBACK': 'NOT_RUN', 'OBSERVATION': 'NOT_RUN'}})
                payload.update(failure_details(name, arguments, exc, getattr(self.integration, 'registry', None)))
                if payload['outcome_certainty'] == 'UNKNOWN':
                    payload.pop('execution_started', None)
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
    own_executions: bool = False,
) -> ClinxMCPServer:
    """Construct the Rust adapter only; legacy database ownership is retired."""
    if own_executions or task_db_path is not None:
        raise ValueError("PYTHON_OWNER_RETIRED_USE_RUST_AUTHORITY")
    cfg = BridgeConfig.load(Path(config_path).expanduser().resolve())
    from clinx.authority.rust_integration import RustIntegration, enabled
    if not enabled():
        raise ValueError("RUST_AUTHORITY_REQUIRED")
    integration = RustIntegration(cfg)
    observations = None
    if os.environ.get('CLINX_RUST_OBSERVATION_READS') == '1':
        from clinx.monitor.network_observation import directory_from_env
        observations = directory_from_env()
        if observations is None:
            raise ValueError('RUST_OBSERVATION_CONFIG_REQUIRED')
        from clinx.monitor.rust_monitor import RustObservationDirectory
        observations = RustObservationDirectory(observations, integration)
    return ClinxMCPServer(integration, allow_execute=False, observations=observations)


def serve_stdio(server: ClinxMCPServer, stdin=None, stdout=None, *, recover_existing: bool = True) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    # Keep the legacy keyword for callers of the codec. Completion and recovery
    # belong to the Rust transport service, never an MCP stdio request process.
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
        # No payload, credentials or exception text: distinguish EOF from an
        # uncaught request failure in the service-owned stderr log.
        if os.environ.get('CLINX_STDIO_DIAGNOSTICS') == '1':
            print(f"MCP_STDIO_CLOSED pid={os.getpid()} reason={'ERROR' if sys.exc_info()[0] else 'EOF'}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CLINX M12 MCP stdio server")
    parser.add_argument("--config", default="bridge.toml")
    parser.add_argument("--observations-only", action="store_true", help="Read-only network directory; no canonical runtime construction")
    parser.add_argument("--stdio", action="store_true", help="serve JSON-RPC over stdio")
    # Existing immutable Rust caller units may still include this former flag.
    parser.add_argument("--no-recover-existing", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--allow-execute", action="store_true", help="enable explicit local action calls")
    args = parser.parse_args(argv)
    if not args.stdio:
        print("MCP_TRANSPORT=BLOCKED: only authenticated external transport may expose CLINX", file=sys.stderr)
        return 2
    try:
        if args.observations_only:
            binary = os.environ.get('CLINX_MCP_BINARY') or str(
                Path(__file__).resolve().parents[3] / 'clinx-mcp')
            os.execv(binary, [binary, '--observations-only', '--stdio'])
        else:
            # Existing direct-file launchers may consume the install-time JSON
            # next to their TOML. No runtime TOML compiler or Python fallback.
            config = Path(args.config)
            if config.suffix != '.json':
                config = config.with_name('mcp-runtime.json')
            binary = os.environ.get('CLINX_MCP_BINARY') or str(
                Path(__file__).resolve().parents[3] / 'clinx-mcp')
            os.execv(binary, [binary, '--config', str(config), '--stdio'])
    except Exception as exc:
        print(f"MCP_SERVER=BLOCKED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
