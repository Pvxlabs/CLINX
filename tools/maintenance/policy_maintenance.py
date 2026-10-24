"""Opt-in CLINX operator MCP profile for existing policy transitions.

No new grants, execution backend, worker tools, or policy semantics. Publication
must use an authenticated operator-only transport, separately authorized by the
installation's existing authority. Do not launch against production to bootstrap
permission from an execution that lacks it.
"""
from __future__ import annotations


# Direct-file entrypoints use only this source/release package root.
if not __package__:
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import argparse
import copy

from clinx.mcp.mcp_server import (
    MCP_PROTOCOL_VERSION,
    SERVER_DISCOVER_PROTOCOL_VERSION,
    ClinxMCPServer,
    MCPRequestError,
    _authority_tools,
    build_server,
    serve_stdio,
)

NAMES = (
    'clinx_get_effective_authority', 'clinx_get_prepared_request',
    'clinx_prepare_policy_reauthorization', 'clinx_apply_policy_reauthorization',
)
INSTRUCTIONS = (
    'Operator-only policy maintenance. Uses existing same-Task CAS and immutable '
    'history; only future execution policy can change. No deployment, execution, '
    'native permission grant, Owner takeover or replay. approved=true is an '
    'operator assertion, not independent human authentication. This profile must '
    'not be exposed to managed workers. Native provider permissions remain native.'
)


class PolicyMaintenanceServer(ClinxMCPServer):
    def __init__(self, integration):
        super().__init__(integration)
        self.public_tools = [copy.deepcopy(t) for t in _authority_tools() if t['name'] in NAMES]
        for tool in self.public_tools:
            tool['annotations']['openWorldHint'] = False
            schema = tool['inputSchema']
            if tool['name'] == 'clinx_apply_policy_reauthorization':
                # Operator must bind approval to the reviewed content and task.
                schema['required'] += ['expected_request_hash', 'expected_task_ref']
        self.public_tool_names = set(NAMES)

    def _call_tool(self, name, arguments):
        tool = next((t for t in self.public_tools if t['name'] == name), None)
        if tool is None:
            raise MCPRequestError(-32602, 'tool is not exposed by policy maintenance')
        schema = tool['inputSchema']
        if (not isinstance(arguments, dict)
                or set(arguments) - set(schema['properties'])
                or any(k not in arguments for k in schema['required'])):
            raise ValueError('POLICY_MAINTENANCE_EXACT_ARGUMENTS_REQUIRED')
        if name in NAMES[2:] and arguments.get('approved') is not True:
            raise ValueError('POLICY_MAINTENANCE_EXPLICIT_OPERATOR_APPROVAL_REQUIRED')
        if name == NAMES[3]:
            # Existing APIs allow optional review fields for old callers. This
            # operator boundary makes both nonempty and mandatory before dispatch.
            import re
            if (not isinstance(arguments['expected_request_hash'], str)
                    or not re.fullmatch('[0-9a-f]{64}', arguments['expected_request_hash'])
                    or not isinstance(arguments['expected_task_ref'], str)
                    or not arguments['expected_task_ref']):
                raise ValueError('POLICY_MAINTENANCE_REVIEW_BINDING_REQUIRED')
        return super()._call_tool(name, arguments)

    def handle(self, request):
        if isinstance(request, dict) and request.get('method') in ('initialize', 'server/discover'):
            if not isinstance(request.get('params') or {}, dict):
                raise MCPRequestError(-32602, 'params must be an object')
            if 'id' not in request:
                return None
            if request['method'] == 'initialize':
                result = dict(protocolVersion=MCP_PROTOCOL_VERSION,
                              capabilities={'tools': {'listChanged': False}},
                              serverInfo={'name': 'clinx-policy-maintenance', 'version': '1'},
                              instructions=INSTRUCTIONS)
            else:
                result = dict(resultType='complete',
                              supportedVersions=[SERVER_DISCOVER_PROTOCOL_VERSION],
                              capabilities={'tools': {}}, instructions=INSTRUCTIONS,
                              ttlMs=60000, cacheScope='private')
            return {'jsonrpc': '2.0', 'id': request['id'], 'result': result}
        result = super().handle(request)
        if isinstance(request, dict) and request.get('method') == 'tools/list' and result:
            result['result'].update(cacheScope='private', ttlMs=60000)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--stdio', action='store_true', required=True)
    args = parser.parse_args(argv)
    server = build_server(args.config, allow_execute=False, own_executions=False)
    # No recovery loop, second consumer, Owner listener or lifecycle operations.
    serve_stdio(PolicyMaintenanceServer(server.integration), recover_existing=False)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
