import subprocess
from pathlib import Path
import tempfile
import threading
import unittest

import app_server
import bridge
from task_registry import (
    DynamicProjectResolver,
    ProjectResolutionError,
    TaskExecutionBusy,
    TaskRegistry,
    WorkspaceConfig,
    WorkspaceRegistry,
    WorkspaceResolutionError,
    TaskRegistryError,
)


def make_repo(root: Path, name: str = "pilot", *, origin: str = "https://example.invalid/pilot.git") -> Path:
    repo = root / name
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    (repo / "README").write_text("pilot\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", origin], check=True)
    return repo


class WorkspaceAndProjectTests(unittest.TestCase):
    def test_workspace_inside_and_escape_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "dev"
            root.mkdir()
            outside = Path(td) / "outside"
            outside.mkdir()
            link = root / "link"
            link.symlink_to(outside, target_is_directory=True)
            registry = WorkspaceRegistry((WorkspaceConfig("p620", root),))
            workspace = registry.resolve("P620")
            self.assertEqual(registry.validate_path(workspace, root / "pilot"), root / "pilot")
            with self.assertRaises(WorkspaceResolutionError):
                registry.validate_path(workspace, root / ".." / "outside")
            with self.assertRaises(WorkspaceResolutionError):
                registry.validate_path(workspace, link / "project")

    def test_registered_unregistered_missing_and_explicit_create(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "dev"
            root.mkdir()
            registered = make_repo(root, "pilot")
            registry = WorkspaceRegistry((WorkspaceConfig("p620", root, allow_new_projects=True),))
            resolver = DynamicProjectResolver(
                registry,
                (bridge.ProjectMapping(
                    "Pilot", registered, alias="pilot",
                    repository_origin="https://example.invalid/pilot.git", branch="main",
                ),),
            )
            result = resolver.resolve("pilot", workspace_alias="p620")
            self.assertTrue(result.registered)
            discovered = make_repo(root, "unregistered")
            result = resolver.resolve("unregistered", workspace_alias="p620")
            self.assertFalse(result.registered)
            self.assertEqual(result.cwd, discovered)
            with self.assertRaises(ProjectResolutionError):
                resolver.resolve("missing", workspace_alias="p620")
            created = resolver.resolve("created", workspace_alias="p620", project_mode="create")
            self.assertTrue((created.cwd / ".git").exists())
            with self.assertRaises(ProjectResolutionError):
                resolver.resolve("../outside", workspace_alias="p620", project_mode="create")


class TaskRegistryTests(unittest.TestCase):
    def test_task_and_binding_survive_restart(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state" / "tasks.sqlite3"
            store = TaskRegistry(path)
            task = store.create_task(
                host="P620", workspace_alias="p620", project_alias="pilot",
                project_name="Pilot", cwd="/tmp/pilot", repository_origin="origin",
                branch="main", title="Task title",
            )
            binding = store.bind_conversation(
                task_id=task.task_id, thread_id="thread-1", session_id="session-1",
                project_id=None, app_server_version="0.152.1",
            )
            restarted = TaskRegistry(path)
            self.assertEqual(restarted.get_task(task.task_id).status, "ACTIVE")
            self.assertEqual(restarted.get_binding(task.task_id), binding)
            restarted.set_status(task.task_id, "COMPLETED")
            self.assertEqual(TaskRegistry(path).get_task(task.task_id).status, "COMPLETED")

    def test_one_thread_cannot_bind_to_two_tasks(self):
        with tempfile.TemporaryDirectory() as td:
            store = TaskRegistry(Path(td) / "tasks.sqlite3")
            task_a = store.create_task(
                host="p620", workspace_alias="p620", project_alias="a", project_name="a",
                cwd="/tmp/a", repository_origin=None, branch="main", title="a",
            )
            task_b = store.create_task(
                host="p620", workspace_alias="p620", project_alias="b", project_name="b",
                cwd="/tmp/b", repository_origin=None, branch="main", title="b",
            )
            store.bind_conversation(
                task_id=task_a.task_id, thread_id="thread-1", session_id="session-1",
                project_id=None, app_server_version="0.152.1",
            )
            with self.assertRaises(TaskRegistryError):
                store.bind_conversation(
                    task_id=task_b.task_id, thread_id="thread-1", session_id="session-1",
                    project_id=None, app_server_version="0.152.1",
                )

    def test_one_active_execution_per_task_and_different_tasks_are_independent(self):
        with tempfile.TemporaryDirectory() as td:
            store = TaskRegistry(Path(td) / "tasks.sqlite3")
            task_a = store.create_task(
                host="p620", workspace_alias="p620", project_alias="a", project_name="a",
                cwd="/tmp/a", repository_origin=None, branch="main", title="a",
            )
            task_b = store.create_task(
                host="p620", workspace_alias="p620", project_alias="b", project_name="b",
                cwd="/tmp/b", repository_origin=None, branch="main", title="b",
            )
            lease = store.execution(task_a.task_id, "issue-a")
            lease.__enter__()
            try:
                with self.assertRaises(TaskExecutionBusy):
                    with store.execution(task_a.task_id, "issue-a-2"):
                        pass
                with store.execution(task_b.task_id, "issue-b"):
                    pass
            finally:
                lease.__exit__(None, None, None)


class FakeM5Client:
    def __init__(self, thread_id="thread-new", session_id="session-new"):
        self.thread_id = thread_id
        self.session_id = session_id
        self.calls = []
        self.thread = None
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
            "gitInfo": {"originUrl": "https://example.invalid/pilot.git", "branch": "main"},
            "canAcceptDirectInput": True,
            "status": {"type": "active"},
        }
        return self.thread

    def thread_read(self, thread_id):
        self.calls.append(("thread/read", thread_id))
        if self.thread is None or thread_id != self.thread["id"]:
            raise app_server.AppServerRemoteError("thread/read", {"message": "missing"})
        return self.thread

    def thread_resume(self, thread_id):
        self.calls.append(("thread/resume", thread_id))
        return self.thread

    def turn_start(self, thread_id, prompt, **kwargs):
        self.calls.append(("turn/start", thread_id, prompt, kwargs))
        return app_server.TurnStartInfo(
            "turn-1", kwargs.get("model"), kwargs.get("reasoning_effort")
        )


def task_dispatcher_fixture(root: Path, db: Path, client: FakeM5Client, repo: Path | None = None):
    repo = repo or make_repo(root)
    cfg = bridge.BridgeConfig(
        team_id="team", trigger_label="local-codex", todo_state="Todo",
        running_state="In Progress", review_state="In Review", poll_interval_seconds=15,
        max_batch=1, codex_binary="codex", sandbox="workspace-write", approval="never",
        log_dir=root / "logs", projects=(bridge.ProjectMapping(
            "Pilot", repo, alias="pilot", repository_origin="https://example.invalid/pilot.git",
            branch="main", workspace_alias="p620",
        ),), app_server=bridge.AppServerConfig(client_version="0.152.1"),
        workspaces=(WorkspaceConfig("p620", root, allow_new_projects=True),),
        task_db_path=db,
    )
    return bridge.TaskDispatcher(cfg, task_registry=TaskRegistry(db), client_factory=lambda _target: client)


class TaskDispatcherTests(unittest.TestCase):
    def test_new_then_continue_reuses_exact_thread_and_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "dev"
            root.mkdir()
            db = Path(td) / "tasks.sqlite3"
            first_client = FakeM5Client()
            dispatcher = task_dispatcher_fixture(root, db, first_client)
            created = dispatcher.dispatch(
                project_ref="pilot", host="p620", project_mode="existing", task_mode="new",
                task_id=None, prompt="first", title="Task", summary=None,
                model="gpt-5.6-luna", reasoning_effort="high", execution_mode="fast",
                issue_id="LIN-1",
            )
            self.assertTrue(created.task_id)
            self.assertTrue(created.thread_created)
            self.assertEqual(created.execution_mode, "fast")
            self.assertEqual(created.model, "gpt-5.6-luna")
            self.assertEqual(created.reasoning_effort, "high")
            self.assertEqual(
                [call[0] for call in first_client.calls],
                ["initialize", "thread/start", "thread/read", "turn/start"],
            )
            self.assertEqual(
                next(call for call in first_client.calls if call[0] == "turn/start")[1],
                "thread-new",
            )

            second_client = FakeM5Client(thread_id="thread-new", session_id="session-new")
            second_client.thread = dict(first_client.thread)
            continued = task_dispatcher_fixture(root, db, second_client, repo=dispatcher.projects.resolve("pilot", workspace_alias="p620").cwd).dispatch(
                project_ref="pilot", host="p620", project_mode="existing", task_mode="continue",
                task_id=created.task_id, prompt="second", title="ignored", summary=None,
                model="gpt-5.2", reasoning_effort="low", execution_mode="normal",
                issue_id="LIN-2",
            )
            self.assertEqual(continued.task_id, created.task_id)
            self.assertEqual(continued.thread_id, created.thread_id)
            turn = next(call for call in second_client.calls if call[0] == "turn/start")
            self.assertEqual(turn[1], "thread-new")
            self.assertEqual(turn[3]["model"], "gpt-5.2")
            self.assertEqual(turn[3]["reasoning_effort"], "low")
            self.assertEqual(
                [call[0] for call in second_client.calls],
                ["initialize", "thread/read", "turn/start"],
            )

    def test_identity_failure_and_missing_binding_fail_before_turn(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "dev"
            root.mkdir()
            db = Path(td) / "tasks.sqlite3"
            client = FakeM5Client()
            dispatcher = task_dispatcher_fixture(root, db, client)
            created = dispatcher.dispatch(
                project_ref="pilot", host="p620", project_mode="existing", task_mode="new",
                task_id=None, prompt="first", title="Task", summary=None,
                model=None, reasoning_effort=None,
            )
            client.calls.clear()
            store = TaskRegistry(db)
            store.set_status(created.task_id, "COMPLETED")
            with self.assertRaises(bridge.TargetResolutionError):
                dispatcher.dispatch(
                    project_ref="pilot", host="p620", project_mode="existing", task_mode="continue",
                    task_id=created.task_id, prompt="second", title="Task", summary=None,
                    model=None, reasoning_effort=None,
                )
            task = store.create_task(
                host="p620", workspace_alias="p620", project_alias="pilot", project_name="Pilot",
                cwd=str(root / "pilot"), repository_origin="https://example.invalid/pilot.git",
                branch="main", title="orphan",
            )
            with self.assertRaises(bridge.TargetResolutionError):
                dispatcher.dispatch(
                    project_ref="pilot", host="p620", project_mode="existing", task_mode="continue",
                    task_id=task.task_id, prompt="second", title="Task", summary=None,
                    model=None, reasoning_effort=None,
                )
            self.assertFalse(any(call[0] == "turn/start" for call in client.calls))

    def test_continuation_host_mismatch_fails_before_app_server(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "dev"
            root.mkdir()
            db = Path(td) / "tasks.sqlite3"
            client = FakeM5Client()
            dispatcher = task_dispatcher_fixture(root, db, client)
            created = dispatcher.dispatch(
                project_ref="pilot", host="p620", project_mode="existing", task_mode="new",
                task_id=None, prompt="first", title="Task", summary=None,
                model=None, reasoning_effort=None,
            )
            client.calls.clear()
            with self.assertRaises(bridge.TargetResolutionError):
                dispatcher.dispatch(
                    project_ref="pilot", host="other", project_mode="existing", task_mode="continue",
                    task_id=created.task_id, prompt="second", title="Task", summary=None,
                    model=None, reasoning_effort=None,
                )
            self.assertEqual(client.calls, [])

    def test_complete_reopen_archive_are_explicit_and_binding_is_kept(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "dev"
            root.mkdir()
            db = Path(td) / "tasks.sqlite3"
            client = FakeM5Client()
            dispatcher = task_dispatcher_fixture(root, db, client)
            result = dispatcher.dispatch(
                project_ref="pilot", host="p620", project_mode="existing", task_mode="new",
                task_id=None, prompt="first", title="Task", summary=None,
                model=None, reasoning_effort=None,
            )
            self.assertEqual(dispatcher.task_action(result.task_id, "complete"), "COMPLETED")
            self.assertEqual(dispatcher.task_action(result.task_id, "reopen"), "ACTIVE")
            self.assertEqual(dispatcher.task_action(result.task_id, "archive"), "ARCHIVED")
            self.assertIsNotNone(TaskRegistry(db).get_binding(result.task_id))


class M5ContractTests(unittest.TestCase):
    def test_m5_contract_defaults_and_continue_requires_id(self):
        contract = bridge.parse_dispatch_contract(
            "HOST=P620\nPROJECT=ORION\nPROJECT_MODE=existing\nTASK_MODE=new\n"
            "MODEL=gpt-5.6-luna\nREASONING=high\nEXECUTION_MODE=fast\n"
        )
        self.assertEqual(contract.contract_kind, "m5")
        self.assertEqual(contract.host, "P620")
        self.assertEqual(contract.execution_mode, "fast")
        with self.assertRaises(bridge.DispatchContractError):
            bridge.parse_dispatch_contract(
                "PROJECT=ORION\nPROJECT_MODE=existing\nTASK_MODE=continue\n"
            )
        with self.assertRaises(bridge.DispatchContractError):
            bridge.parse_dispatch_contract(
                "PROJECT=pilot\nPROJECT_MODE=create\nTASK_MODE=continue\nTASK_ID=task_1\n"
            )


if __name__ == "__main__":
    unittest.main()
