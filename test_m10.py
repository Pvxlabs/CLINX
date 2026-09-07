from pathlib import Path
import subprocess
import tempfile
import unittest

import app_server
import bridge
from m9_integration import ClinxIntegration
from mcp_server import ClinxMCPServer, READ_ONLY_TOOL_NAMES, tool_definitions
from task_registry import ProjectResolutionError, TaskRegistry, WorkspaceConfig


def _repo(root: Path) -> Path:
    path = root / "ORION"
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-b", "master"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    (path / "README").write_text("orion\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run([
        "git", "-C", str(path), "remote", "add", "origin",
        "git@github.com:Pvxlabs/ORION.git",
    ], check=True)
    return path


class TopicClient:
    def __init__(self, cwd: str):
        self.cwd = cwd
        self.calls = []
        self.initialize_info = app_server.InitializeInfo(
            "codex", "0.152.1", "codex-cli 0.152.1"
        )
        self.threads = {
            "thread-adopted": self._thread("thread-adopted"),
            "thread-historical": self._thread("thread-historical"),
            "thread-other": self._thread("thread-other", cwd="/tmp/other"),
        }

    def _thread(self, thread_id: str, *, cwd: str | None = None):
        return {
            "id": thread_id,
            "sessionId": "session-" + thread_id,
            "projectId": None,
            "cwd": cwd or self.cwd,
            "ephemeral": False,
            "gitInfo": {
                "originUrl": "git@github.com:Pvxlabs/ORION.git",
                "branch": "master",
            },
            "canAcceptDirectInput": False,
            "status": {"type": "completed"},
            "cliVersion": "codex-cli 0.152.1",
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def initialize(self, **kwargs):
        self.calls.append(("initialize", kwargs))
        return self.initialize_info

    def thread_list(self, **kwargs):
        self.calls.append(("thread/list", kwargs))
        return {"data": [{"id": key} for key in self.threads]}

    def thread_loaded_list(self):
        self.calls.append(("thread/loaded/list",))
        return {"data": []}

    def thread_read(self, thread_id):
        self.calls.append(("thread/read", thread_id))
        return self.threads[thread_id]

    def thread_turns_list(self, thread_id, **kwargs):
        self.calls.append(("thread/turns/list", thread_id, kwargs))
        if thread_id == "thread-adopted":
            return {"data": [{
                "id": "turn-adopted",
                "status": "completed",
                "items": [{
                    "id": "item-adopted-user", "type": "userMessage",
                    "content": [{"type": "text", "text": "DATA NODE implementation"}],
                }, {
                    "id": "item-adopted-agent", "type": "agentMessage",
                    "text": "STATUS=PASS; Changed files: src/data_node.ts; Validation: 105 tests passed; Current state: completed",
                }],
            }]}
        if thread_id == "thread-historical":
            return {"data": [{
                "id": "turn-historical",
                "status": "completed",
                "items": [{
                    "id": "item-historical-user", "type": "userMessage",
                    "content": [{"type": "text", "text": "Continue DATA NODE token migration"}],
                }, {
                    "id": "item-historical-agent", "type": "agentMessage",
                    "text": "Changed files: src/data_node_tokens.ts; Validation: 105 tests passed; Blocker: none; Current state: ready",
                }],
            }]}
        return {"data": []}

    def thread_items_list(self, thread_id, **kwargs):
        self.calls.append(("thread/items/list", thread_id, kwargs))
        return {"data": []}


def _fixture(root: Path):
    repo = _repo(root)
    db = root / "tasks.sqlite3"
    registry = TaskRegistry(db)
    task = registry.create_task(
        host="p620", workspace_alias="p620", project_alias="orion",
        project_name="ORION", cwd=str(repo),
        repository_origin="git@github.com:Pvxlabs/ORION.git", branch="master",
        title="DATA NODE managed task", summary="DATA NODE active registry task",
    )
    registry.bind_conversation(
        task_id=task.task_id, thread_id="thread-adopted",
        session_id="session-thread-adopted", project_id=None,
        app_server_version="0.152.1",
    )
    cfg = bridge.BridgeConfig(
        team_id="team", trigger_label="local-codex", todo_state="Todo",
        running_state="In Progress", review_state="In Review", poll_interval_seconds=15,
        max_batch=1, codex_binary="codex", sandbox="workspace-write", approval="never",
        log_dir=root / "logs", projects=(bridge.ProjectMapping(
            "ORION", repo, alias="orion",
            repository_origin="git@github.com:Pvxlabs/ORION.git", branch="master",
            workspace_alias="p620", read_only=True,
        ),), threads=(bridge.ThreadBinding(
            alias="current", project_alias="orion", ssh_alias="p620",
            thread_id="thread-adopted", session_id="session-thread-adopted",
            project_id=None, app_server_version="0.152.1", target_host="p620",
        ),), app_server=bridge.AppServerConfig(client_version="0.152.1"),
        workspaces=(WorkspaceConfig("p620", root, host="p620", ssh_alias="p620"),),
        task_db_path=db, runtime_host="p620",
    )
    client = TopicClient(str(repo))
    reader = bridge.TopicStatusReader(
        cfg, registry, client_factory=lambda _target: client,
    )
    return cfg, registry, client, reader, task


class TopicStatusReaderTests(unittest.TestCase):
    def test_aggregates_task_and_unadopted_history_with_exact_scope_and_dedup(self):
        with tempfile.TemporaryDirectory() as td:
            _cfg, registry, client, reader, task = _fixture(Path(td))
            result = reader.read_topic_status(
                host="P620", project_ref="ORION", topic="DATA NODE",
            )
            self.assertEqual(result.project, "orion")
            self.assertEqual(result.task_count, 1)
            self.assertEqual(result.conversation_count, 1)
            self.assertEqual(len(result.completed_work), 1)
            self.assertEqual(len(result.active_work), 1)
            self.assertEqual(result.deduplication, "ADOPTED_THREADS_EXCLUDED")
            self.assertEqual(result.topic_candidate_threads, 1)
            self.assertFalse(any(call[0] in {
                "turn/start", "thread/start", "thread/fork", "thread/queue/add",
            } for call in client.calls))
            self.assertIsNotNone(registry.get_task(task.task_id))

    def test_screening_is_bounded_and_only_candidates_expand(self):
        with tempfile.TemporaryDirectory() as td:
            _cfg, _registry, client, reader, _task = _fixture(Path(td))
            reader.read_topic_status(
                host="p620", project_ref="orion", topic="DATA NODE",
                recent_turns=2, max_bytes=4096,
            )
            turns = [call for call in client.calls if call[0] == "thread/turns/list"]
            self.assertTrue(all(call[2]["limit"] == 2 for call in turns))
            self.assertTrue(all(call[2]["items_view"] == "summary" for call in turns))
            self.assertFalse(any(call[2].get("cursor") for call in turns))
            self.assertFalse(any(
                call[0] == "thread/turns/list" and call[1] == "thread-other"
                for call in client.calls
            ))

    def test_invalid_project_fails_before_app_server(self):
        with tempfile.TemporaryDirectory() as td:
            _cfg, _registry, client, reader, _task = _fixture(Path(td))
            with self.assertRaises(ProjectResolutionError):
                reader.read_topic_status(host="p620", project_ref="missing", topic="DATA NODE")
            self.assertEqual(client.calls, [])


class TopicIntegrationAndMCPTests(unittest.TestCase):
    def test_integration_and_public_catalog_expose_only_read_topic_status(self):
        with tempfile.TemporaryDirectory() as td:
            cfg, registry, client, reader, _task = _fixture(Path(td))
            dispatcher = bridge.TaskDispatcher(cfg, task_registry=registry, client_factory=lambda _target: client)
            integration = ClinxIntegration(cfg, registry, dispatcher, bridge.TaskContextReader(cfg, registry, client_factory=lambda _target: client), None, reader)
            result = integration.get_topic_status(host="p620", project="orion", topic="DATA NODE")
            self.assertEqual(result["topic_status_read"], "PASS")
            self.assertEqual(list(READ_ONLY_TOOL_NAMES), [
                "clinx_find_task", "clinx_get_context", "clinx_get_topic_status",
                "clinx_list_projects", "clinx_get_status", "clinx_get_capabilities",
                "clinx_prepare_execution",
            ])
            server = ClinxMCPServer(integration)
            names = [item["name"] for item in tool_definitions()]
            self.assertEqual(names, list(READ_ONLY_TOOL_NAMES))
            response = server.handle({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "clinx_get_topic_status", "arguments": {
                    "host": "p620", "project": "orion", "topic": "DATA NODE",
                }},
            })
            self.assertEqual(response["result"]["structuredContent"]["topic_status_read"], "PASS")
            forbidden = {
                "thread_id", "threadId", "session_id", "sessionId", "turn_id", "turnId",
                "turn_ids", "item_ids", "message_ids", "execution_ids", "cwd",
                "origin", "repository_origin", "branch", "credentials", "token",
            }

            def keys(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        yield key
                        yield from keys(item)
                elif isinstance(value, list):
                    for item in value:
                        yield from keys(item)

            self.assertTrue(forbidden.isdisjoint(set(keys(response["result"]["structuredContent"]))))
            self.assertFalse(any(call[0] in {"turn/start", "thread/start"} for call in client.calls))


if __name__ == "__main__":
    unittest.main()
