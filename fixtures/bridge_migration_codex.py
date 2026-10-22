"""Isolated app-server stand-in with accepted work whose ACK never arrives.

No real Codex, task, model, credentials or database is used. Receipts expose
fixture ground truth that the old Bridge's public snapshot cannot expose.
"""
import json
import os
import signal
import sys
from pathlib import Path

directory = Path(sys.argv[1])
pending = []


def stop(signum, _frame):
    (directory / "codex-stop.json").write_text(json.dumps({
        "pid": os.getpid(), "signal": signum, "unresolved_methods": pending,
        "work_completed": False,
    }))
    raise SystemExit(0)


signal.signal(signal.SIGTERM, stop)
(directory / "codex.pid").write_text(str(os.getpid()))
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialized":
        for name, params in (("remoteControl/status/changed", {"status": "disabled"}),
                             ("account/updated", {"authMode": "chatgpt"})):
            print(json.dumps({"method": name, "params": params}), flush=True)
    if "id" not in request:
        continue
    if method == "initialize":
        result = {"userAgent": "first-migration-fixture"}
    elif method == "getAuthStatus":
        result = {"authMethod": "chatgpt", "requiresOpenaiAuth": False}
    elif method == "account/read":
        result = {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": False}
    else:
        pending.append(method)
        (directory / "unresolved.json").write_text(json.dumps(pending))
        continue
    print(json.dumps({"id": request["id"], "result": result}), flush=True)
