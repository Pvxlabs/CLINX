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


def backend(config_path):
    """Read-only directory child for Rust MCP; business reads return to Rust."""
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    from clinx.authority.rust_authority import RustAuthorityError
    from clinx.monitor.network_observation import directory_from_env
    from clinx.monitor.rust_monitor import RustObservationDirectory

    class ManagedReads:
        cfg = SimpleNamespace(runtime_host=json.loads(Path(config_path).read_text())['runtime_host'])

        def get_context(self, **arguments):
            process = subprocess.run(
                [os.environ['CLINX_MCP_BINARY'], '--config', str(config_path),
                 '--call', 'clinx_get_context'],
                input=json.dumps(arguments), capture_output=True, text=True, timeout=30, check=True)
            response = json.loads(process.stdout)
            if response.get('error'):
                raise RustAuthorityError(response['error'])
            return response['result']

    directory = directory_from_env()
    if directory is None:
        raise ValueError('RUST_OBSERVATION_CONFIG_REQUIRED')
    directory = RustObservationDirectory(directory, ManagedReads())
    print(json.dumps({'ready': True}), flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request['method'] not in NAMES or not isinstance(request['arguments'], dict):
                raise ValueError('OBSERVATION_READ_ONLY')
            result = call_observation(directory, request['method'], request['arguments'])
            response = {'result': result}
        except NodeProtocolError as exc:
            response = {'error': exc.code}
        except Exception:
            response = {'error': 'OBSERVATION_UNAVAILABLE'}
        print(json.dumps(response), flush=True)


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 3 or sys.argv[1] != '--backend':
        raise SystemExit('OBSERVATION_BACKEND_ARGUMENT_INVALID')
    backend(sys.argv[2])
