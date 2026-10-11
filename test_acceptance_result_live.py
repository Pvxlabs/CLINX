"""Opt-in no-tool result-contract acceptance on a disposable real Provider."""
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

import bridge
from m9_integration import ClinxIntegration
from mcp_server import ClinxMCPServer
from provider_qualification import isolated_provider
from task_registry import TaskRegistry, WorkspaceConfig

pytestmark = pytest.mark.skipif(
    os.environ.get("CLINX_LIVE_RESULT_CONTRACT") != "1",
    reason="explicit isolated no-tool Provider acceptance required",
)


def test_acceptance_only_payload_returns_six_fields_without_tools(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=CLINX acceptance",
                    "-c", "user.email=acceptance@example.invalid", "commit", "-q",
                    "--allow-empty", "-m", "No-tool acceptance"], check=True)
    cfg = bridge.BridgeConfig.load(Path(__file__).with_name("bridge.toml"))
    evidence = {"source": str(Path(__file__).parent), "kind": "ISOLATED_REAL_PROVIDER",
                "shared_canonical_db_used": False, "historical_execution_replayed": False}
    with isolated_provider(cfg, tmp_path) as cfg:
        cfg = dataclasses.replace(
            cfg, task_db_path=tmp_path / "tasks.sqlite3", log_dir=tmp_path / "logs",
            targets=(), threads=(), host_executor=dataclasses.replace(cfg.host_executor, enabled=False),
            projects=(bridge.ProjectMapping(linear_name="Result contract", alias="result-contract",
                repo=root, branch="main", workspace_alias="p620"),),
            workspaces=(WorkspaceConfig(alias="p620", root=tmp_path, host="p620"),),
        )
        registry = TaskRegistry(cfg.task_db_path)
        dispatcher = bridge.TaskDispatcher(cfg, task_registry=registry)
        integration = ClinxIntegration(cfg, registry, dispatcher, bridge.TaskContextReader(cfg, registry), None)
        server = ClinxMCPServer(integration)

        def tool(name, arguments):
            result = server.handle({"id": name, "method": "tools/call", "params": {
                "name": name, "arguments": arguments}})["result"]
            assert not result["isError"], result
            return result["structuredContent"]

        try:
            token = "CLINX-NO-TOOL-RESULT-20261011"
            prepared = tool("clinx_prepare_execution", dict(
                approved=True, task_action="create", host="p620", project="result-contract",
                title="No-tool result contract", summary="One authorized echo-only regression",
                model="gpt-5.6-luna", network_access=False,
                prompt=f"验收标识 {token}。仅输出该验收标识，不调用任何工具，不读取或写入任何文件，不执行任何业务操作。",
            ))
            started = tool("clinx_start_execution", dict(
                approved=True, prepared_execution_ref=prepared["prepared_execution_ref"],
                expected_request_hash=prepared["request_hash"] if "request_hash" in prepared else None,
            ))
            evidence.update(task_ref=started["task_ref"], execution_ref=started["execution_ref"])
            deadline = time.monotonic() + 120
            result = None
            while time.monotonic() < deadline:
                result = registry.get_execution_result(started["execution_ref"])
                if result and not registry.get_active_execution(started["execution_ref"]):
                    break
                time.sleep(0.2)
            assert result is not None, "bounded Provider result timeout; do not replay"
            evidence["result"] = dataclasses.asdict(result)
            assert result.status == "PASS", result
            assert result.changed_files == "NONE" and result.blockers == "NONE"
            assert token in result.summary and token in result.validation
            assert result.next_state == "COMPLETED"
            assert not registry.list_host_executions(execution_ref=started["execution_ref"])
            rollouts = list(Path(cfg.app_server.native_home).rglob("rollout-*.jsonl"))
            assert rollouts
            native_calls = []
            for rollout in rollouts:
                for line in rollout.read_text().splitlines():
                    event = json.loads(line)
                    payload = event.get("payload", {})
                    if event.get("type") == "response_item" and payload.get("type") in {
                            "function_call", "custom_tool_call", "tool_call"}:
                        native_calls.append(payload.get("name", payload.get("type")))
            assert not native_calls, native_calls
            evidence.update(status="PASS", host_calls=0, native_tool_calls=0)
        finally:
            dispatcher.stop_completion_runtime()
            output = os.environ.get("CLINX_RESULT_CONTRACT_RECEIPT")
            if output:
                Path(output).write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
