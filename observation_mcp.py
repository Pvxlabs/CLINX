"""Exact schemas shared by the formal MCP read-only observation entrypoint."""
from node_protocol import NodeProtocolError

NAMES = ("clinx_list_observations","clinx_get_observation","clinx_get_observation_context")


def observation_tools():
    common={"cursor":{"type":"string","maxLength":2048},"limit":{"type":"integer","minimum":1,"maximum":100}}
    props=dict(common,**{k:{"type":"string","maxLength":1024} for k in ("node","project","state")})
    props["kind"]={"type":"string","enum":["managed","external"]}
    identity={"observation_id":{"type":"string","pattern":"^obs_[a-f0-9]{40}$"}}
    specs=[
        (NAMES[0],"列出已授权的全网原生和受管观察项；无需 thread ID 或执行权限。",props,[]),
        (NAMES[1],"按稳定 observation_id 读取缓存详情和有界轮次历史。",dict(common,**identity),["observation_id"]),
        (NAMES[2],"只读分页源端用户可见上下文；离线内容未缓存时明确不可用。",
         dict(identity,cursor=common["cursor"]),["observation_id"]),
    ]
    return [{"name":name,"description":description,
        "inputSchema":{"type":"object","properties":properties,"required":required,"additionalProperties":False},
        "outputSchema":{"type":"object","properties":{"schema_version":{"type":"string"},"read_only":{"type":"boolean"}},"additionalProperties":True},
        "annotations":{"readOnlyHint":True,"destructiveHint":False,"idempotentHint":True}}
        for name,description,properties,required in specs]


def call_observation(directory,name,arguments):
    if directory is None: raise NodeProtocolError("OBSERVATION_NOT_CONFIGURED","Observation directory is unavailable")
    return {NAMES[0]:directory.list,NAMES[1]:directory.detail,NAMES[2]:directory.context}[name](**arguments)


def standalone_server(directory):
    from mcp_server import ClinxMCPServer
    # Use the existing JSON-RPC implementation without constructing TaskRegistry,
    # dispatcher, completion runtime or any canonical execution authority.
    server=ClinxMCPServer.__new__(ClinxMCPServer)
    server.integration=None
    server.observations=directory
    server.allow_execute=False
    server.public_tools=observation_tools()
    server.public_tool_names=set(NAMES)
    return server
