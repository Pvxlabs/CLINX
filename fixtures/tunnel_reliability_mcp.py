"""Qualification-only stdio requester; fake credentials and an isolated registry only."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from execution_owner import ExecutionOwnerClient  # noqa: E402
from task_registry import TaskRegistry  # noqa: E402


def main():
    registry = TaskRegistry(Path(sys.argv[1]))
    Path(sys.argv[2]).write_text(str(os.getpid()))
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request["method"]
        if method == "initialize":
            result = dict(
                protocolVersion="2025-06-18",
                capabilities={"tools": {}},
                serverInfo={"name": "isolated-clinx-owner-requester", "version": "p1"},
            )
        elif method == "tools/call":
            params = request["params"]
            assert params["name"] == "clinx_start_execution"
            try:
                result = ExecutionOwnerClient(registry).start(**params["arguments"])
                result = {"content": [{"type": "text", "text": json.dumps(result)}]}
            except Exception:
                result = {
                    "content": [
                        {"type": "text", "text": "OUTCOME_UNKNOWN_READ_EXACT_REQUEST"}
                    ],
                    "isError": True,
                }
        else:
            result = {}
        print(
            json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}),
            flush=True,
        )


if __name__ == "__main__":
    main()
