import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import app_server
import bridge
from task_registry import TaskRegistry, TaskRegistryError, WorkspaceConfig


def make_repo(root: Path, name: str = "pilot") -> Path:
    repo = root / name
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    (repo / "README").write_text("pilot\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://example.invalid/pilot.git"],
        check=True,
    )
    return repo


class ContextClient:
    def __init__(self, cwd: str, *, bounded=True, next_cursor=None, item_next_cursor=None):
        self.calls = []
        self.bounded = bounded
        self.next_cursor = next_cursor
        self.item_next_cursor = item_next_cursor
        self.thread = {
            "id": "thread-exact",
            "sessionId": "session-exact",
            "projectId": None,
            "cwd": cwd,
            "ephemeral": False,
            "gitInfo": {
                "originUrl": "https://example.invalid/pilot.git",
                "branch": "main",
            },
            "canAcceptDirectInput": True,
            "status": {"type": "idle"},
            "cliVersion": "codex-cli 0.152.1",
        }
        self.initialize_info = app_server.InitializeInfo("codex", "0.152.1", "codex-cli 0.152.1")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def initialize(self, **kwargs):
        self.calls.append(("initialize", kwargs))
        return self.initialize_info

    def thread_read(self, thread_id):
        self.calls.append(("thread/read", thread_id))
        return self.thread

    def thread_turns_list(self, thread_id, **kwargs):
        self.calls.append(("thread/turns/list", thread_id, kwargs))
        if not self.bounded:
            raise app_server.AppServerRemoteError(
                "thread/turns/list", {"message": "method not found"}
            )
        return {
            "data": [
                {
                    "id": "turn-2",
                    "status": "completed",
                    "items": [
                        {
                            "id": "summary-user-2",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "Continue the UI token task"}],
                        },
                        {
                            "id": "summary-agent-2",
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "Changed files: src/tokens.ts; Validation: 92 tests passed; Blocker: none",
                        },
                    ],
                },
                {"id": "turn-1", "status": "completed", "items": []},
            ],
            **({"nextCursor": self.next_cursor} if self.next_cursor else {}),
        }

    def thread_items_list(self, thread_id, **kwargs):
        self.calls.append(("thread/items/list", thread_id, kwargs))
        return {
            "data": [
                {
                    "item": {
                        "id": f"item-{kwargs['turn_id']}-assistant",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "Changed files: src/tokens.ts; Validation: 92 tests passed; Blocker: none",
                    },
                    "turnId": kwargs["turn_id"],
                },
                {
                    "item": {
                        "id": f"item-{kwargs['turn_id']}-user",
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "Continue the UI token task"}],
                    },
                    "turnId": kwargs["turn_id"],
                },
            ],
            **({"nextCursor": self.item_next_cursor} if self.item_next_cursor else {}),
        }


class ContextFixture:
    def __init__(self, root: Path, *, client=None):
        self.repo = make_repo(root)
        self.db = root / "tasks.sqlite3"
        self.registry = TaskRegistry(self.db)
        task = self.registry.create_task(
            host="P620", workspace_alias="p620", project_alias="pilot", project_name="Pilot",
            cwd=str(self.repo), repository_origin="https://example.invalid/pilot.git",
            branch="main", title="UI token historical task", summary="semantic UI token work",
            task_key="pilot/ui-token-historical-task",
        )
        self.task = task
        self.registry.bind_conversation(
            task_id=task.task_id, thread_id="thread-exact", session_id="session-exact",
            project_id=None, app_server_version="codex-cli 0.152.1",
        )
        self.client = client or ContextClient(str(self.repo))
        cfg = bridge.BridgeConfig(
            team_id="team", trigger_label="local-codex", todo_state="Todo",
            running_state="In Progress", review_state="In Review", poll_interval_seconds=15,
            max_batch=1, codex_binary="codex", sandbox="workspace-write", approval="never",
            log_dir=root / "logs", projects=(bridge.ProjectMapping(
                "Pilot", self.repo, alias="pilot", repository_origin="https://example.invalid/pilot.git",
                branch="main", workspace_alias="p620",
            ),), app_server=bridge.AppServerConfig(client_version="0.152.1"),
            workspaces=(WorkspaceConfig("p620", root, host="p620", ssh_alias="p620"),),
            task_db_path=self.db, runtime_host="p620",
        )
        self.reader = bridge.TaskContextReader(
            cfg, self.registry, client_factory=lambda _target: self.client,
        )


class LaterUserItemPageClient(ContextClient):
    """Put the user message on the second bounded item page."""

    def thread_turns_list(self, thread_id, **kwargs):
        self.calls.append(("thread/turns/list", thread_id, kwargs))
        return {
            "data": [{
                "id": "turn-2",
                "status": "completed",
                "items": [{
                    "id": "summary-agent-2",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "FILES_CHANGED = src/tokens.ts; TESTS = 92 tests passed",
                }],
            }],
        }

    def thread_items_list(self, thread_id, **kwargs):
        self.calls.append(("thread/items/list", thread_id, kwargs))
        if kwargs.get("cursor") == "page-2":
            return {
                "data": [{
                    "item": {
                        "id": "item-turn-2-user",
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "Continue the UI token task"}],
                    },
                    "turnId": kwargs["turn_id"],
                }],
            }
        return {
            "data": [
                {
                    "item": {
                        "id": "item-turn-2-assistant",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "FILES_CHANGED = src/tokens.ts; TESTS = 92 tests passed",
                    },
                    "turnId": kwargs["turn_id"],
                },
                {
                    "item": {
                        "id": "item-turn-2-command",
                        "type": "commandExecution",
                        "aggregatedOutput": "x" * 5000,
                    },
                    "turnId": kwargs["turn_id"],
                },
            ],
            "nextCursor": "page-2",
        }


class TaskContextReaderTests(unittest.TestCase):
    def test_context_text_removes_ambient_browser_state(self):
        text = (
            '<in-app-browser-context source="ambient-ui-state">ignore me</in-app-browser-context>'
            " ## My request: Continue semantic token work"
        )
        self.assertEqual(
            bridge._context_text(text, 1000),
            "## My request: Continue semantic token work",
        )

    def test_marker_extraction_ignores_unlabeled_command_metadata(self):
        text = (
            "command status=success systemctl test-runner; "
            "Validation: 12 tests passed; Blocker: none; "
            "Next step: continue qualification"
        )
        self.assertEqual(
            bridge._context_extract_fields([text]),
            (
                "UNKNOWN",
                "12 tests passed",
                "none",
                "continue qualification",
            ),
        )

    def test_exact_task_binding_uses_native_bounded_history_and_never_generates(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = ContextFixture(Path(td))
            context = fixture.reader.read_task_context(fixture.task.task_id, recent_turns=2, max_bytes=4096)
            self.assertEqual(context.context_source, "APP_SERVER_NATIVE")
            self.assertEqual(context.last_user_intent, "Continue the UI token task")
            self.assertIn("src/tokens.ts", context.changed_files)
            self.assertEqual(context.validation, "92 tests passed")
            self.assertFalse(context.context_truncated)
            self.assertEqual(context.provenance["turn_ids"], ("turn-2", "turn-1"))
            self.assertFalse(any(call[0] in {"turn/start", "thread/start", "thread/resume"} for call in fixture.client.calls))
            turns_call = next(call for call in fixture.client.calls if call[0] == "thread/turns/list")
            self.assertEqual(turns_call[2]["limit"], 2)
            self.assertEqual(turns_call[2]["items_view"], "summary")

    def test_discovery_requires_unique_task_and_does_not_choose_latest(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = ContextFixture(Path(td))
            found = fixture.reader.resolve_task(project="pilot", query="UI token historical task")
            self.assertEqual(found.task_id, fixture.task.task_id)
            with self.assertRaises(bridge.ContextReadError):
                fixture.reader.resolve_task(query="UI token")
            with self.assertRaises(bridge.TargetResolutionError):
                fixture.reader.resolve_task(task_ref=fixture.task.task_id, project="ORION")

    def test_missing_binding_fails_before_app_server(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = ContextFixture(Path(td))
            task = fixture.registry.create_task(
                host="P620", workspace_alias="p620", project_alias="pilot", project_name="Pilot",
                cwd=str(fixture.repo), repository_origin="https://example.invalid/pilot.git",
                branch="main", title="orphan",
            )
            with self.assertRaises(bridge.ContextReadError):
                fixture.reader.read_task_context(task.task_id)
            self.assertEqual(fixture.client.calls, [])

    def test_native_page_boundary_sets_truncated_without_unbounded_read(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = ContextFixture(Path(td), client=ContextClient(str(Path(td) / "pilot"), next_cursor="more"))
            context = fixture.reader.read_task_context(fixture.task.task_id, max_bytes=4096)
            self.assertTrue(context.context_truncated)
            self.assertEqual(context.context_range, "turn-1..turn-2")

    def test_item_page_boundary_sets_truncated(self):
        with tempfile.TemporaryDirectory() as td:
            client = ContextClient(str(Path(td) / "pilot"), item_next_cursor="more-items")
            fixture = ContextFixture(Path(td), client=client)
            context = fixture.reader.read_task_context(fixture.task.task_id, max_bytes=4096)
            self.assertTrue(context.context_truncated)

    def test_descending_pages_choose_latest_messages_and_enforce_shared_byte_budget(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = ContextFixture(Path(td))
            context = fixture.reader.read_task_context(
                fixture.task.task_id, recent_turns=2, max_bytes=1024
            )
            self.assertEqual(context.last_codex_result.split(";")[0], "Changed files: src/tokens.ts")
            self.assertEqual(context.last_user_intent, "Continue the UI token task")
            context_bytes = len(
                "\n".join(
                    (context.last_codex_result, context.last_user_intent)
                ).encode("utf-8")
            )
            self.assertLessEqual(context_bytes, 1024)

    def test_turn_summary_preserves_user_message_when_item_page_is_recent_tail(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = ContextFixture(Path(td))
            context = fixture.reader.read_task_context(fixture.task.task_id, max_bytes=4096)
            self.assertEqual(context.last_user_intent, "Continue the UI token task")

    def test_item_pagination_finds_user_message_on_later_page(self):
        with tempfile.TemporaryDirectory() as td:
            client = LaterUserItemPageClient(str(Path(td) / "pilot"))
            fixture = ContextFixture(Path(td), client=client)
            context = fixture.reader.read_task_context(fixture.task.task_id, max_bytes=4096)
            self.assertEqual(context.last_user_intent, "Continue the UI token task")
            self.assertIn("FILES_CHANGED = src/tokens.ts", context.last_codex_result)
            self.assertEqual(context.changed_files, "src/tokens.ts")
            self.assertEqual(context.validation, "92 tests passed")
            self.assertFalse(context.context_truncated)
            item_calls = [call for call in client.calls if call[0] == "thread/items/list"]
            self.assertEqual(len(item_calls), 2)
            self.assertEqual(item_calls[0][2]["limit"], 20)
            self.assertEqual(item_calls[1][2]["cursor"], "page-2")

    def test_checkpoint_fallback_and_live_native_context_wins(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixture = ContextFixture(root, client=ContextClient(str(root / "pilot"), bounded=False))
            fixture.registry.save_context_checkpoint(
                task_id=fixture.task.task_id, execution_id="exec-1", thread_id="thread-exact",
                turn_id="turn-old", prompt_summary="Old prompt", result_summary="Old result",
                changed_files="old.py", validation_summary="old tests", blockers="old blocker",
                next_state="old state", source="CLINX", provenance='{"execution_ids":["exec-1"]}',
            )
            context = fixture.reader.read_task_context(fixture.task.task_id)
            self.assertEqual(context.context_source, "CLINX_CHECKPOINT")
            self.assertEqual(context.last_codex_result, "Old result")

            live = ContextClient(str(fixture.repo))
            fixture.reader.client_factory = lambda _target: live
            context = fixture.reader.read_task_context(fixture.task.task_id)
            self.assertEqual(context.context_source, "APP_SERVER_NATIVE")
            self.assertTrue(context.checkpoint_stale)
            self.assertNotEqual(context.last_codex_result, "Old result")

    def test_pre_clinx_exact_session_fallback_is_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            client = ContextClient(str(root / "pilot"), bounded=False)
            fixture = ContextFixture(root, client=client)
            sessions = root / "sessions"
            sessions.mkdir()
            session = sessions / "rollout-thread-exact.jsonl"
            rows = [
                {"type": "event_msg", "payload": {"type": "user_message", "message": "Historical UI token request"}},
                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-local", "last_agent_message": "Historical result"}},
            ]
            session.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            fixture.reader.local_history_root = sessions
            context = fixture.reader.read_task_context(fixture.task.task_id, max_bytes=4096)
            self.assertEqual(context.context_source, "CODEX_LOCAL_SESSION")
            self.assertEqual(context.last_user_intent, "Historical UI token request")
            self.assertEqual(context.last_codex_result, "Historical result")
            self.assertTrue(context.context_truncated is False)

    def test_identity_mismatch_fails_closed_without_items_or_generation(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = ContextFixture(Path(td))
            fixture.client.thread["sessionId"] = "wrong-session"
            with self.assertRaises(bridge.IdentityGuardError):
                fixture.reader.read_task_context(fixture.task.task_id)
            self.assertFalse(any(call[0] == "thread/items/list" for call in fixture.client.calls))

    def test_checkpoint_rejects_wrong_thread(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = ContextFixture(Path(td))
            with self.assertRaises(TaskRegistryError):
                fixture.registry.save_context_checkpoint(
                    task_id=fixture.task.task_id, execution_id=None, thread_id="wrong",
                    turn_id=None, prompt_summary="p", result_summary="r", changed_files="f",
                    validation_summary="v", blockers="b", next_state="n", source="test", provenance="{}",
                )


class BoundedAppServerClientTests(unittest.TestCase):
    class Transport:
        def __init__(self):
            self.sent = []
            self.responses = []

        def send(self, message):
            self.sent.append(message)
            method = message.get("method")
            if method == "initialize":
                self.responses.append({"id": message["id"], "result": {"serverInfo": {"version": "0.152.1"}}})
            elif method == "thread/turns/list":
                self.responses.append({"id": message["id"], "result": {"data": [], "nextCursor": None}})
            elif method == "thread/items/list":
                self.responses.append({"id": message["id"], "result": {"data": []}})

        def receive(self, _timeout):
            return self.responses.pop(0)

        def close(self):
            pass

    def test_bounded_methods_send_verified_schema(self):
        transport = self.Transport()
        client = app_server.CodexAppServerClient(transport)
        client.initialize(client_name="x", client_title="x", client_version="0.152.1")
        client.thread_turns_list("thread-1", limit=5, cursor="c", sort_direction="desc", items_view="summary")
        client.thread_items_list("thread-1", turn_id="turn-1", limit=3, sort_direction="desc")
        turns = next(item for item in transport.sent if item.get("method") == "thread/turns/list")
        items = next(item for item in transport.sent if item.get("method") == "thread/items/list")
        self.assertEqual(turns["params"], {
            "threadId": "thread-1", "limit": 5, "sortDirection": "desc",
            "itemsView": "summary", "cursor": "c",
        })
        self.assertEqual(items["params"], {
            "threadId": "thread-1", "turnId": "turn-1", "limit": 3,
            "sortDirection": "desc",
        })


class HistoricalDiscoveryClient:
    def __init__(self, cwd: str, *, high_threads=("thread-hit",), medium_threads=("thread-medium",)):
        self.cwd = cwd
        self.high_threads = set(high_threads)
        self.medium_threads = set(medium_threads)
        self.calls = []
        self.initialize_info = app_server.InitializeInfo("codex", "0.152.1", "codex-cli 0.152.1")
        self.threads = {}
        for thread_id in (self.high_threads | self.medium_threads | {"thread-other"}):
            self.threads[thread_id] = {
                "id": thread_id,
                "sessionId": f"session-{thread_id}",
                "projectId": None,
                "cwd": cwd if thread_id != "thread-other" else "/tmp/other",
                "ephemeral": False,
                "gitInfo": {
                    "originUrl": "https://example.invalid/pilot.git",
                    "branch": "main",
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
        return {"data": [{"id": thread_id} for thread_id in self.threads]}

    def thread_loaded_list(self):
        self.calls.append(("thread/loaded/list",))
        return {"data": []}

    def thread_read(self, thread_id):
        self.calls.append(("thread/read", thread_id))
        return self.threads[thread_id]

    def thread_turns_list(self, thread_id, **kwargs):
        self.calls.append(("thread/turns/list", thread_id, kwargs))
        if kwargs.get("cursor"):
            if thread_id in self.medium_threads:
                return {
                    "data": [{
                        "id": f"turn-{thread_id}-hit",
                        "status": "completed",
                        "items": [],
                    }],
                }
            return {"data": []}
        if thread_id in self.high_threads:
            return {
                "data": [{
                    "id": f"turn-{thread_id}-hit",
                    "status": "completed",
                    "items": [{
                        "id": "summary-user",
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "Continue semantic token work"}],
                    }, {
                        "id": "summary-agent",
                        "type": "agentMessage",
                        "text": "USER_PRODUCT_ACTIVE_SEMANTIC = PASS; PRODUCT_ACTIVE_TOKEN = --product-active; Validation: 105 tests passed; Current state: ready",
                    }],
                }],
            }
        if thread_id in self.medium_threads:
            return {
                "data": [{
                    "id": f"turn-{thread_id}-recent",
                    "status": "completed",
                    "items": [{
                        "id": "summary-medium",
                        "type": "assistantMessage",
                        "text": "CustomerProduct and TenantAccounts naming discussion",
                    }],
                }],
                "nextCursor": "older",
            }
        return {"data": [{"id": "turn-other", "status": "completed", "items": []}]}

    def thread_items_list(self, thread_id, **kwargs):
        self.calls.append(("thread/items/list", thread_id, kwargs))
        turn_id = kwargs.get("turn_id")
        if turn_id and turn_id.endswith("hit"):
            return {"data": [{
                "item": {
                    "id": f"item-{turn_id}-user",
                    "type": "userMessage",
                    "content": [{"type": "text", "text": "Continue semantic token work"}],
                },
            }, {
                "item": {
                    "id": f"item-{turn_id}-agent",
                    "type": "agentMessage",
                    "text": "USER_PRODUCT_ACTIVE_SEMANTIC = PASS; PRODUCT_ACTIVE_TOKEN = --product-active; Changed files: src/tokens.css; Validation: 105 tests passed; Blocker: none; Current state: ready",
                },
            }]}
        return {"data": []}


def historical_fixture(root: Path, client: HistoricalDiscoveryClient):
    repo = make_repo(root)
    db = root / "tasks.sqlite3"
    registry = TaskRegistry(db)
    cfg = bridge.BridgeConfig(
        team_id="team", trigger_label="local-codex", todo_state="Todo",
        running_state="In Progress", review_state="In Review", poll_interval_seconds=15,
        max_batch=1, codex_binary="codex", sandbox="workspace-write", approval="never",
        log_dir=root / "logs", projects=(bridge.ProjectMapping(
            "Pilot", repo, alias="pilot", repository_origin="https://example.invalid/pilot.git",
            branch="main", workspace_alias="p620",
        ),), threads=(bridge.ThreadBinding(
            alias="current", project_alias="pilot", ssh_alias="p620",
            thread_id="configured-thread", session_id="configured-session", project_id=None,
            app_server_version="0.152.1", target_host="p620",
        ),), app_server=bridge.AppServerConfig(client_version="0.152.1"),
        workspaces=(WorkspaceConfig("p620", root, host="p620", ssh_alias="p620"),),
        task_db_path=db, runtime_host="p620",
    )
    discovery = bridge.HistoricalConversationDiscovery(
        cfg, registry, client_factory=lambda _target: client,
    )
    return discovery, registry, client


class HistoricalConversationDiscoveryTests(unittest.TestCase):
    def test_scopes_to_exact_project_and_expands_only_medium_candidates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            client = HistoricalDiscoveryClient(str(root / "pilot"))
            discovery, _registry, _client = historical_fixture(root, client)
            result = discovery.discover(project_ref="pilot", host="p620")
            self.assertEqual(result["threads_discovered"], 3)
            self.assertEqual(result["eligible_threads"], 2)
            candidates = {item.thread_id: item for item in result["candidates"]}
            self.assertEqual(candidates["thread-hit"].relevance, "HIGH")
            self.assertEqual(candidates["thread-medium"].relevance, "HIGH")
            self.assertTrue(any(
                call[0] == "thread/turns/list" and call[1] == "thread-medium"
                and call[2].get("cursor") == "older"
                for call in client.calls
            ))
            self.assertFalse(any(
                call[0] == "thread/turns/list" and call[1] == "thread-hit"
                and call[2].get("cursor")
                for call in client.calls
            ))
            self.assertFalse(any(call[0] in {"turn/start", "thread/start"} for call in client.calls))

    def test_zero_and_multiple_high_matches_fail_closed_without_adoption(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            no_hit = HistoricalDiscoveryClient(str(root / "pilot"), high_threads=(), medium_threads=())
            discovery, registry, _client = historical_fixture(root, no_hit)
            with self.assertRaises(bridge.TargetResolutionError):
                discovery.adopt_unique(project_ref="pilot", host="p620")
            self.assertEqual(registry.list_tasks(), [])

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            multiple = HistoricalDiscoveryClient(
                str(root / "pilot"), high_threads=("thread-a", "thread-b"), medium_threads=()
            )
            discovery, registry, _client = historical_fixture(root, multiple)
            with self.assertRaises(bridge.TargetResolutionError):
                discovery.adopt_unique(project_ref="pilot", host="p620")
            self.assertEqual(registry.list_tasks(), [])

    def test_unique_match_adopts_once_and_reader_uses_exact_historical_anchor(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            client = HistoricalDiscoveryClient(
                str(root / "pilot"), high_threads=("thread-hit",), medium_threads=()
            )
            discovery, registry, _client = historical_fixture(root, client)
            adopted = discovery.adopt_unique(project_ref="pilot", host="p620")
            task = adopted["task"]
            self.assertEqual(task.status, "ACTIVE")
            anchor = registry.get_context_anchor(task.task_id)
            self.assertEqual(anchor.turn_id, "turn-thread-hit-hit")
            self.assertEqual(len(registry.list_tasks()), 1)
            context = discovery.reader.read_task_context(task.task_id, max_bytes=4096)
            self.assertEqual(context.context_source, "APP_SERVER_NATIVE")
            self.assertEqual(context.last_user_intent, "Continue semantic token work")
            self.assertIn("--product-active", context.last_codex_result)
            self.assertEqual(context.provenance["turn_ids"], ("turn-thread-hit-hit",))
            self.assertTrue(any(
                call[0] == "thread/items/list" and call[2].get("turn_id") == "turn-thread-hit-hit"
                for call in client.calls
            ))
            self.assertFalse(any(call[0] in {"turn/start", "thread/start", "thread/resume"} for call in client.calls))

    def test_already_bound_historical_thread_is_reused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            client = HistoricalDiscoveryClient(
                str(root / "pilot"), high_threads=("thread-hit",), medium_threads=()
            )
            discovery, registry, _client = historical_fixture(root, client)
            existing = registry.create_task(
                host="p620", workspace_alias="p620", project_alias="pilot", project_name="Pilot",
                cwd=str(root / "pilot"), repository_origin="https://example.invalid/pilot.git",
                branch="main", title="Existing historical task", summary="old",
            )
            registry.bind_conversation(
                task_id=existing.task_id, thread_id="thread-hit", session_id="session-thread-hit",
                project_id=None, app_server_version="0.152.1",
            )
            adopted = discovery.adopt_unique(project_ref="pilot", host="p620")
            self.assertEqual(adopted["task"].task_id, existing.task_id)
            self.assertEqual(len(registry.list_tasks()), 1)


if __name__ == "__main__":
    unittest.main()
