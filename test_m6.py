import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

import app_server
import bridge
from task_registry import TaskRegistry, UnknownTaskError, WorkspaceConfig


def make_repo(root: Path) -> Path:
    repo = root / "pilot"
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


class FakeClient:
    def __init__(
        self,
        *,
        thread_id="thread-1",
        session_id="session-1",
        origin="https://example.invalid/pilot.git",
        branch="main",
        existing=None,
    ):
        self.thread_id = thread_id
        self.session_id = session_id
        self.origin = origin
        self.branch = branch
        self.thread = existing
        self.calls = []
        self.initialize_info = app_server.InitializeInfo("codex", "0.152.1", "codex-cli 0.152.1")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def initialize(self, **kwargs):
        self.calls.append(("initialize", kwargs))
        return self.initialize_info

    def thread_start(self, **kwargs):
        self.calls.append(("thread/start", kwargs))
        self.thread = {
            "id": self.thread_id,
            "sessionId": self.session_id,
            "projectId": None,
            "cwd": kwargs["cwd"],
            "ephemeral": False,
            "gitInfo": {
                "originUrl": self.origin,
                "branch": self.branch,
            },
            "canAcceptDirectInput": True,
            "status": {"type": "idle"},
        }
        return self.thread

    def thread_read(self, thread_id):
        self.calls.append(("thread/read", thread_id))
        if self.thread is None or self.thread["id"] != thread_id:
            raise app_server.AppServerRemoteError("thread/read", {"message": "missing"})
        return self.thread

    def turn_start(self, thread_id, prompt, **kwargs):
        self.calls.append(("turn/start", thread_id, prompt, kwargs))
        return app_server.TurnStartInfo(
            f"turn-{len([c for c in self.calls if c[0] == 'turn/start'])}",
            kwargs.get("model"),
            kwargs.get("reasoning_effort"),
        )


def dispatcher_fixture(root: Path, db: Path, clients):
    repo = make_repo(root)
    cfg = bridge.BridgeConfig(
        team_id="team",
        trigger_label="local-codex",
        todo_state="Todo",
        running_state="In Progress",
        review_state="In Review",
        poll_interval_seconds=15,
        max_batch=1,
        codex_binary="codex",
        sandbox="workspace-write",
        approval="never",
        log_dir=root / "logs",
        projects=(bridge.ProjectMapping(
            "Pilot",
            repo,
            alias="pilot",
            repository_origin="https://example.invalid/pilot.git",
            branch="main",
            workspace_alias="p620",
        ),),
        threads=(bridge.ThreadBinding(
            alias="current",
            project_alias="pilot",
            ssh_alias="p620",
            thread_id="project-current-must-not-win",
            session_id="project-current-session",
            project_id=None,
            app_server_version="0.152.1",
            target_host="p620",
        ),),
        app_server=bridge.AppServerConfig(client_version="0.152.1"),
        workspaces=(WorkspaceConfig("p620", root, host="p620"),),
        task_db_path=db,
        runtime_host="p620",
    )
    queue = list(clients)
    return bridge.TaskDispatcher(
        cfg,
        task_registry=TaskRegistry(db),
        client_factory=lambda _target: queue.pop(0),
    ), repo


class M6ContractTests(unittest.TestCase):
    def test_create_handoff_requires_no_human_identity(self):
        contract = bridge.parse_dispatch_contract(
            "HOST=P620\nPROJECT=pilot\nTASK_ACTION=create\n"
            "MODEL=gpt-5.6-luna\nREASONING=high\nEXECUTION_MODE=normal\n"
        )
        self.assertEqual(contract.contract_kind, "m6")
        self.assertIsNone(contract.task_ref)
        self.assertEqual(contract.task_mode, "new")

    def test_continuation_uses_hidden_task_ref(self):
        contract = bridge.parse_dispatch_contract(
            "HOST=P620\nPROJECT=pilot\nTASK_ACTION=continue\nTASK_REF=task_hidden\n"
            "MODEL=gpt-5.6-luna\nREASONING=high\n"
        )
        self.assertEqual(contract.task_ref, "task_hidden")
        self.assertEqual(contract.task_mode, "continue")

    def test_create_rejects_task_ref_and_continue_requires_it(self):
        with self.assertRaises(bridge.DispatchContractError):
            bridge.parse_dispatch_contract(
                "HOST=P620\nPROJECT=pilot\nTASK_ACTION=create\nTASK_REF=task_bad\n"
                "MODEL=x\nREASONING=high\n"
            )

    def test_continuation_requires_existing_project_mode(self):
        with self.assertRaises(bridge.DispatchContractError):
            bridge.parse_dispatch_contract(
                "HOST=P620\nPROJECT=pilot\nPROJECT_MODE=create\n"
                "TASK_ACTION=continue\nTASK_REF=task_hidden\n"
                "MODEL=x\nREASONING=high\n"
            )
        with self.assertRaises(bridge.DispatchContractError):
            bridge.parse_dispatch_contract(
                "HOST=P620\nPROJECT=pilot\nPROJECT_MODE=invalid\n"
                "TASK_ACTION=create\nMODEL=x\nREASONING=high\n"
            )
        with self.assertRaises(bridge.DispatchContractError):
            bridge.parse_dispatch_contract(
                "HOST=P620\nPROJECT=pilot\nTASK_ACTION=continue\n"
                "MODEL=x\nREASONING=high\n"
            )

    def test_reopen_without_hidden_task_ref_fails_as_m6(self):
        with self.assertRaisesRegex(
            bridge.DispatchContractError,
            "Malformed M6 task handoff: TASK_ACTION=reopen requires TASK_REF",
        ):
            bridge.parse_dispatch_contract(
                "HOST=P620\nPROJECT=pilot\nTASK_ACTION=reopen\n"
                "MODEL=x\nREASONING=high\n"
            )


class TaskDiscoveryTests(unittest.TestCase):
    def create(self, store, title, *, status="ACTIVE", summary=None, project="pilot"):
        task = store.create_task(
            host="P620",
            workspace_alias="p620",
            project_alias=project,
            project_name=project,
            cwd=f"/tmp/{project}",
            repository_origin=None,
            branch="main",
            title=title,
            summary=summary,
            task_key=bridge._task_key(project, title),
        )
        if status != "ACTIVE":
            task = store.set_status(task.task_id, status)
        return task

    def test_list_filters_host_project_and_status(self):
        with tempfile.TemporaryDirectory() as td:
            store = TaskRegistry(Path(td) / "tasks.sqlite3")
            wanted = self.create(store, "Data node lifecycle")
            self.create(store, "Other", project="other")
            rows = store.list_tasks(host="p620", project="pilot", status="ACTIVE")
            self.assertEqual([row.task_id for row in rows], [wanted.task_id])

    def test_find_unique_and_ambiguous(self):
        with tempfile.TemporaryDirectory() as td:
            store = TaskRegistry(Path(td) / "tasks.sqlite3")
            one = self.create(store, "DATA NODE lifecycle", summary="archive retention")
            unique = store.find_tasks(project="pilot", query="DATA NODE lifecycle")
            self.assertEqual(unique.classification, "UNIQUE")
            self.assertEqual(unique.tasks[0].task_id, one.task_id)
            self.create(store, "DATA NODE lifecycle")
            ambiguous = store.find_tasks(project="pilot", query="DATA NODE lifecycle")
            self.assertEqual(ambiguous.classification, "AMBIGUOUS")
            self.assertEqual(len(ambiguous.tasks), 2)

    def test_completed_is_discoverable_and_archived_is_excluded_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            store = TaskRegistry(Path(td) / "tasks.sqlite3")
            completed = self.create(store, "Completed recovery", status="COMPLETED")
            archived = self.create(store, "Archived recovery", status="ARCHIVED")
            self.assertEqual(
                store.find_tasks(project="pilot", query="Completed recovery").tasks[0].task_id,
                completed.task_id,
            )
            self.assertEqual(
                store.find_tasks(project="pilot", query="Archived recovery").classification,
                "NONE",
            )
            self.assertEqual(
                store.find_tasks(
                    project="pilot", query="Archived recovery", include_archived=True
                ).tasks[0].task_id,
                archived.task_id,
            )

    def test_old_m5_schema_migrates_without_losing_task(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tasks.sqlite3"
            with sqlite3.connect(path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE tasks (
                      task_id TEXT PRIMARY KEY, host TEXT NOT NULL,
                      workspace_alias TEXT NOT NULL, project_alias TEXT NOT NULL,
                      project_name TEXT NOT NULL, cwd TEXT NOT NULL,
                      repository_origin TEXT, branch TEXT, title TEXT NOT NULL,
                      summary TEXT, status TEXT NOT NULL,
                      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                    );
                    INSERT INTO tasks VALUES (
                      'task_old','P620','p620','pilot','Pilot','/tmp/pilot',NULL,'main',
                      'Old M5 task',NULL,'ACTIVE','2026-09-01','2026-09-01'
                    );
                    """
                )
            task = TaskRegistry(path).get_task("task_old")
            self.assertEqual(task.execution_mode, "normal")
            self.assertIsNone(task.task_key)


class FakeLinearIndex:
    def __init__(self):
        self.created = []
        self.updated = []

    def create_task_index_issue(self, **kwargs):
        self.created.append(kwargs)
        return {
            "id": "index-id",
            "identifier": "PVX-INDEX",
            "project": {"id": kwargs.get("project_id"), "name": "Pilot"},
        }

    def update_task_index_issue(self, issue_id, **kwargs):
        self.updated.append((issue_id, kwargs))
        return {"id": issue_id, "identifier": "PVX-INDEX"}


class FakeBridgeLinear(FakeLinearIndex):
    def __init__(self):
        super().__init__()
        self.comments = []
        self.claimed = []

    def update_issue_state(self, issue_id, state_id):
        self.claimed.append((issue_id, state_id))
        return {"state": {"name": "In Progress"}}

    def add_comment(self, issue_id, body):
        self.comments.append((issue_id, body))


class TaskIndexTests(unittest.TestCase):
    def test_create_once_then_update_same_index_without_conversation_ids(self):
        with tempfile.TemporaryDirectory() as td:
            store = TaskRegistry(Path(td) / "tasks.sqlite3")
            task = store.create_task(
                host="P620", workspace_alias="p620", project_alias="pilot",
                project_name="Pilot", cwd="/tmp/pilot", repository_origin=None,
                branch="main", title="Long task", summary="First summary",
                task_key="pilot/long-task", execution_mode="fast",
            )
            store.bind_conversation(
                task_id=task.task_id, thread_id="secret-thread", session_id="secret-session",
                project_id=None, app_server_version="0.152.1",
            )
            linear = FakeLinearIndex()
            index = bridge.LinearTaskIndex(linear, store, "team")
            index.sync(task.task_id, project_id="project-1")
            store.set_status(task.task_id, "COMPLETED")
            restarted = bridge.LinearTaskIndex(
                linear,
                TaskRegistry(Path(td) / "tasks.sqlite3"),
                "team",
            )
            restarted.sync(task.task_id, project_id="project-1")
            self.assertEqual(len(linear.created), 1)
            self.assertEqual(len(linear.updated), 1)
            descriptions = [linear.created[0]["description"], linear.updated[0][1]["description"]]
            self.assertNotIn("secret-thread", "\n".join(descriptions))
            self.assertNotIn("secret-session", "\n".join(descriptions))
            self.assertIn("TASK_STATUS=COMPLETED", descriptions[-1])

    def test_public_record_omits_conversation_and_workspace_path_identity(self):
        with tempfile.TemporaryDirectory() as td:
            store = TaskRegistry(Path(td) / "tasks.sqlite3")
            task = store.create_task(
                host="P620", workspace_alias="p620", project_alias="pilot",
                project_name="Pilot", cwd="/secret/pilot",
                repository_origin="secret-origin", branch="main", title="Task",
            )
            store.bind_conversation(
                task_id=task.task_id, thread_id="secret-thread",
                session_id="secret-session", project_id="secret-project",
                app_server_version="0.152.1",
            )
            public = bridge._task_public_record(store, task)
            self.assertNotIn("thread_id", public)
            self.assertNotIn("session_id", public)
            self.assertNotIn("cwd", public)
            self.assertNotIn("repository_origin", public)
            self.assertNotIn("branch", public)
            self.assertEqual(public["task_ref"], task.task_id)

    def test_m6_bridge_claims_dispatches_and_writes_public_index_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "dev"
            root.mkdir()
            client = FakeClient()
            dispatcher, repo = dispatcher_fixture(
                root, Path(td) / "tasks.sqlite3", [client]
            )
            linear = FakeBridgeLinear()
            runner = bridge.Bridge(dispatcher.cfg, linear)
            runner.task_dispatcher = dispatcher
            runner.states = {"In Progress": "running-state"}
            issue = {
                "id": "linear-internal-id",
                "identifier": "PVX-M6",
                "title": "Execution wrapper title",
                "description": (
                    "HOST=P620\nPROJECT=pilot\nTASK_ACTION=create\n"
                    "MODEL=gpt-5.6-luna\nREASONING=high\nEXECUTION_MODE=fast\n"
                    "TASK_TITLE=Human task title\nTASK_SUMMARY_UPDATE=Short summary\n"
                ),
                "project": {"id": "linear-project", "name": "Pilot"},
                "url": "https://example.invalid/PVX-M6",
            }
            runner.execute_issue(issue, repo)
            self.assertEqual(linear.claimed, [("linear-internal-id", "running-state")])
            self.assertEqual(len(linear.created), 1)
            task = dispatcher.tasks.list_tasks(project="pilot")[0]
            self.assertEqual(task.title, "Human task title")
            self.assertEqual(task.summary, "Short summary")
            self.assertEqual(task.execution_mode, "fast")
            self.assertEqual(
                dispatcher.tasks.last_linear_execution(task.task_id), "PVX-M6"
            )
            public_comments = "\n".join(body for _issue_id, body in linear.comments)
            self.assertIn("M6_TASK_DISPATCHED", public_comments)
            self.assertIn(f"TASK_REF={task.task_id}", public_comments)
            self.assertNotIn("thread-1", public_comments)
            self.assertNotIn("session-1", public_comments)
            self.assertIsNotNone(dispatcher.tasks.get_task_index(task.task_id))


class PersistentConversationTests(unittest.TestCase):
    def test_active_remote_turn_fails_closed_without_starting_another_execution(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "dev"
            root.mkdir()
            first = FakeClient()
            dispatcher, _repo = dispatcher_fixture(root, Path(td) / "tasks.sqlite3", [first])
            created = dispatcher.dispatch(
                project_ref="pilot", host="p620", project_mode="existing",
                task_mode="new", task_id=None, prompt="first", title="Task",
                summary=None, model="x", reasoning_effort="high",
            )
            active = FakeClient(existing={
                **first.thread,
                "status": {"type": "active"},
                "canAcceptDirectInput": True,
            })
            dispatcher.client_factory = lambda _target: active
            with self.assertRaisesRegex(
                bridge.DispatchContractError, "DISPATCH_TURN_START_GUARD=FAIL"
            ):
                dispatcher.dispatch(
                    project_ref="pilot", host="p620", project_mode="existing",
                    task_mode="continue", task_id=created.task_id, prompt="second",
                    title="ignored", summary=None, model="x", reasoning_effort="high",
                )
            self.assertFalse(any(call[0] == "turn/start" for call in active.calls))

    def test_thirty_continuations_reuse_one_task_and_thread(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "dev"
            root.mkdir()
            first = FakeClient()
            clients = [first]
            dispatcher, _repo = dispatcher_fixture(root, Path(td) / "tasks.sqlite3", clients)
            created = dispatcher.dispatch(
                project_ref="pilot", host="P620", project_mode="existing", task_mode="new",
                task_id=None, prompt="first", title="Long task", summary="context",
                model="gpt-5.6-luna", reasoning_effort="high", execution_mode="fast",
            )
            all_clients = [first]
            for index in range(30):
                client = FakeClient(existing=dict(first.thread))
                all_clients.append(client)
                dispatcher.client_factory = lambda _target, client=client: client
                result = dispatcher.dispatch(
                    project_ref="pilot", host="p620", project_mode="existing",
                    task_mode="continue", task_id=created.task_id,
                    prompt=f"continue {index}", title="ignored", summary=None,
                    model="gpt-5.6-luna", reasoning_effort="high", execution_mode="normal",
                )
                self.assertEqual(result.task_id, created.task_id)
                self.assertEqual(result.thread_id, created.thread_id)
            self.assertEqual(
                sum(call[0] == "thread/start" for client in all_clients for call in client.calls),
                1,
            )
            self.assertEqual(dispatcher.tasks.get_task(created.task_id).execution_mode, "normal")

    def test_reopen_and_project_current_hint_do_not_change_task_binding(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "dev"
            root.mkdir()
            first = FakeClient()
            dispatcher, _repo = dispatcher_fixture(root, Path(td) / "tasks.sqlite3", [first])
            created = dispatcher.dispatch(
                project_ref="pilot", host="p620", project_mode="existing", task_mode="new",
                task_id=None, prompt="first", title="Task", summary=None,
                model="x", reasoning_effort="high",
            )
            dispatcher.task_action(created.task_id, "complete")
            dispatcher.task_action(created.task_id, "reopen")
            continued_client = FakeClient(existing=dict(first.thread))
            dispatcher.client_factory = lambda _target: continued_client
            result = dispatcher.dispatch(
                project_ref="pilot", host="p620", project_mode="existing",
                task_mode="continue", task_id=created.task_id, prompt="again",
                title="ignored", summary=None, model="x", reasoning_effort="high",
            )
            self.assertEqual(result.thread_id, "thread-1")
            self.assertNotEqual(result.thread_id, "project-current-must-not-win")
            self.assertFalse(any(call[0] == "thread/start" for call in continued_client.calls))

    def test_continuation_updates_human_metadata_and_readable_execution_reference(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "dev"
            root.mkdir()
            first = FakeClient()
            dispatcher, _repo = dispatcher_fixture(root, Path(td) / "tasks.sqlite3", [first])
            created = dispatcher.dispatch(
                project_ref="pilot", host="p620", project_mode="existing",
                task_mode="new", task_id=None, prompt="first", title="Initial title",
                summary="initial", model="x", reasoning_effort="high",
                issue_id="linear-internal-1", execution_ref="PVX-1",
            )
            continued_client = FakeClient(existing=dict(first.thread))
            dispatcher.client_factory = lambda _target: continued_client
            dispatcher.dispatch(
                project_ref="pilot", host="P620", project_mode="existing",
                task_mode="continue", task_id=created.task_id, prompt="again",
                title="Updated title", update_title=True, summary="updated summary",
                model="x", reasoning_effort="high", execution_mode="fast",
                issue_id="linear-internal-2", execution_ref="PVX-2",
            )
            task = dispatcher.tasks.get_task(created.task_id)
            self.assertEqual(task.title, "Updated title")
            self.assertEqual(task.summary, "updated summary")
            self.assertEqual(task.execution_mode, "fast")
            self.assertEqual(dispatcher.tasks.last_linear_execution(created.task_id), "PVX-2")

    def test_task_host_project_and_missing_binding_fail_before_app_server(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "dev"
            root.mkdir()
            first = FakeClient()
            dispatcher, _repo = dispatcher_fixture(root, Path(td) / "tasks.sqlite3", [first])
            created = dispatcher.dispatch(
                project_ref="pilot", host="p620", project_mode="existing",
                task_mode="new", task_id=None, prompt="first", title="Task",
                summary=None, model="x", reasoning_effort="high",
            )
            for host, project in (("other", "pilot"), ("p620", "other")):
                client = FakeClient(existing=dict(first.thread))
                dispatcher.client_factory = lambda _target, client=client: client
                with self.assertRaises(bridge.TargetResolutionError):
                    dispatcher.dispatch(
                        project_ref=project, host=host, project_mode="existing",
                        task_mode="continue", task_id=created.task_id, prompt="no",
                        title="ignored", summary=None, model="x", reasoning_effort="high",
                    )
                self.assertEqual(client.calls, [])

            orphan = dispatcher.tasks.create_task(
                host="P620", workspace_alias="p620", project_alias="pilot",
                project_name="Pilot", cwd=str(root / "pilot"),
                repository_origin="https://example.invalid/pilot.git",
                branch="main", title="Orphan",
            )
            client = FakeClient()
            dispatcher.client_factory = lambda _target: client
            with self.assertRaises(bridge.TargetResolutionError):
                dispatcher.dispatch(
                    project_ref="pilot", host="p620", project_mode="existing",
                    task_mode="continue", task_id=orphan.task_id, prompt="no",
                    title="ignored", summary=None, model="x", reasoning_effort="high",
                )
            self.assertEqual(client.calls, [])

    def test_unregistered_existing_project_remains_dispatchable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "dev"
            root.mkdir()
            repo = make_repo(root)
            dynamic = root / "historical"
            dynamic.mkdir()
            subprocess.run(
                ["git", "-C", str(dynamic), "init", "-b", "main"],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(dynamic), "config", "user.name", "Test"], check=True
            )
            subprocess.run(
                ["git", "-C", str(dynamic), "config", "user.email", "test@example.invalid"],
                check=True,
            )
            (dynamic / "README").write_text("historical\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(dynamic), "add", "README"], check=True, capture_output=True
            )
            subprocess.run(
                ["git", "-C", str(dynamic), "commit", "-m", "init"],
                check=True, capture_output=True,
            )
            origin = "https://example.invalid/historical.git"
            subprocess.run(
                ["git", "-C", str(dynamic), "remote", "add", "origin", origin], check=True
            )
            cfg = bridge.BridgeConfig(
                team_id="team", trigger_label="local-codex", todo_state="Todo",
                running_state="In Progress", review_state="In Review",
                poll_interval_seconds=15, max_batch=1, codex_binary="codex",
                sandbox="workspace-write", approval="never", log_dir=root / "logs",
                projects=(bridge.ProjectMapping(
                    "Pilot", repo, alias="pilot",
                    repository_origin="https://example.invalid/pilot.git", branch="main",
                    workspace_alias="workstation",
                ),),
                app_server=bridge.AppServerConfig(client_version="0.152.1"),
                workspaces=(WorkspaceConfig("workstation", root, host="p620"),),
                task_db_path=Path(td) / "tasks.sqlite3", runtime_host="p620",
            )
            client = FakeClient(origin=origin)
            dispatcher = bridge.TaskDispatcher(
                cfg,
                task_registry=TaskRegistry(Path(td) / "tasks.sqlite3"),
                client_factory=lambda _target: client,
            )
            result = dispatcher.dispatch(
                project_ref="historical", host="P620", project_mode="existing",
                task_mode="new", task_id=None, prompt="probe", title="Historical task",
                summary=None, model="x", reasoning_effort="high",
            )
            self.assertEqual(result.project_alias, "historical")
            self.assertEqual(result.cwd, str(dynamic))

    def test_invalid_task_ref_fails_before_app_server(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "dev"
            root.mkdir()
            client = FakeClient()
            dispatcher, _repo = dispatcher_fixture(root, Path(td) / "tasks.sqlite3", [client])
            with self.assertRaises(UnknownTaskError):
                dispatcher.dispatch(
                    project_ref="pilot", host="p620", project_mode="existing",
                    task_mode="continue", task_id="task_missing", prompt="no",
                    title="ignored", summary=None, model="x", reasoning_effort="high",
                )
            self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
