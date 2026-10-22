"""Local transport qualification: no registry, Owner, Provider or task dispatch."""
import json
import os
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(str(os.getpid()))
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "transport-only-fixture", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "fixture_echo", "description": "Local echo only",
                             "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": json.dumps(request["params"]["arguments"])}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
