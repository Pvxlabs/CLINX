"""Exact schemas shared by the formal MCP read-only observation entrypoint."""
from clinx.nodes.node_protocol import NodeProtocolError

NAMES = ("clinx_list_observations","clinx_get_observation","clinx_get_observation_context","clinx_get_observation_activity")


def observation_tools():
    import json
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / 'mcp/registry.json'
    return json.loads(path.read_text())['observations']


def call_observation(directory,name,arguments):
    if directory is None:
        raise NodeProtocolError("OBSERVATION_NOT_CONFIGURED","Observation directory is unavailable")
    return {NAMES[0]:directory.list,NAMES[1]:directory.detail,NAMES[2]:directory.context,NAMES[3]:directory.activity}[name](**arguments)


def standalone_server(directory):
    from clinx.mcp.mcp_server import ClinxMCPServer
    # Use the existing JSON-RPC implementation without constructing TaskRegistry,
    # dispatcher, completion runtime or any canonical execution authority.
    server=ClinxMCPServer.__new__(ClinxMCPServer)
    server.integration=None
    server.observations=directory
    server.allow_execute=False
    server.public_tools=observation_tools()
    server.public_tool_names=set(NAMES)
    return server
