from pathlib import Path
import tempfile
import unittest

import bridge
from task_registry import TaskRegistryError
from test_m6 import FakeClient, FakeLinearIndex, dispatcher_fixture


class AdoptionTests(unittest.TestCase):
    def _adopt(self, dispatcher, client, **kwargs):
        dispatcher.client_factory = lambda _target: client
        return dispatcher.adopt_existing_conversation(
            project_ref="pilot",
            host="p620",
            thread_id="thread-1",
            title="Existing Conversation Adoption Qualification",
            summary=(
                "Identity-only migration of an existing durable pilot conversation "
                "into the CLINX Task Registry."
            ),
            **kwargs,
        )

    def test_adopts_exact_thread_and_syncs_task_index_without_mutating_codex(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "dev"
            root.mkdir()
            client = FakeClient(existing={
                "id": "thread-1", "sessionId": "session-1", "projectId": None,
                "cwd": str(root / "pilot"), "ephemeral": False,
                "gitInfo": {"originUrl": "https://example.invalid/pilot.git", "branch": "main"},
                "canAcceptDirectInput": True, "status": {"type": "active"},
            })
            dispatcher, _repo = dispatcher_fixture(root, Path(td) / "tasks.sqlite3", [client])
            linear = FakeLinearIndex()
            index = bridge.LinearTaskIndex(linear, dispatcher.tasks, "team")
            task, binding, index_record = self._adopt(
                dispatcher, client, task_index=index, task_index_project_id=None
            )
            self.assertEqual(binding.thread_id, "thread-1")
            self.assertEqual(binding.session_id, "session-1")
            self.assertEqual(task.status, "ACTIVE")
            self.assertEqual(index_record.identifier, "PVX-INDEX")
            self.assertEqual([call[0] for call in client.calls], ["initialize", "thread/read"])
            self.assertEqual(len(dispatcher.tasks.list_tasks(project="pilot")), 1)

    def test_thread_cannot_be_adopted_twice(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "dev"
            root.mkdir()
            first = FakeClient(existing={
                "id": "thread-1", "sessionId": "session-1", "projectId": None,
                "cwd": str(root / "pilot"), "ephemeral": False,
                "gitInfo": {"originUrl": "https://example.invalid/pilot.git", "branch": "main"},
                "canAcceptDirectInput": True, "status": {"type": "active"},
            })
            dispatcher, _repo = dispatcher_fixture(root, Path(td) / "tasks.sqlite3", [first])
            self._adopt(dispatcher, first)
            second = FakeClient(existing=dict(first.thread))
            with self.assertRaisesRegex(TaskRegistryError, "ADOPTION=FAIL"):
                self._adopt(dispatcher, second)
            self.assertEqual(len(dispatcher.tasks.list_tasks(project="pilot")), 1)
            self.assertFalse(any(call[0] == "turn/start" for call in second.calls))

    def test_unloaded_thread_is_resumed_only_to_verify_direct_input(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "dev"
            root.mkdir()
            client = FakeClient(existing={
                "id": "thread-1", "sessionId": "session-1", "projectId": None,
                "cwd": str(root / "pilot"), "ephemeral": False,
                "gitInfo": {"originUrl": "https://example.invalid/pilot.git", "branch": "main"},
                "canAcceptDirectInput": None, "status": {"type": "notLoaded"},
            })
            client.thread_resume = lambda thread_id: client.calls.append(("thread/resume", thread_id)) or client.thread.update({
                "canAcceptDirectInput": True, "status": {"type": "idle"}
            })
            dispatcher, _repo = dispatcher_fixture(root, Path(td) / "tasks.sqlite3", [client])
            self._adopt(dispatcher, client)
            self.assertEqual(
                [call[0] for call in client.calls],
                ["initialize", "thread/read", "thread/resume", "thread/read"],
            )
            self.assertFalse(any(call[0] == "turn/start" for call in client.calls))

    def test_identity_failure_is_fail_closed_and_never_creates_task(self):
        cases = (
            {"sessionId": None},
            {"sessionId": ""},
            {"cwd": "/wrong"},
            {"ephemeral": True},
            {"canAcceptDirectInput": False},
            {"gitInfo": {"originUrl": "wrong", "branch": "main"}},
            {"gitInfo": {"originUrl": "https://example.invalid/pilot.git", "branch": "other"}},
        )
        for patch in cases:
            with self.subTest(patch=patch), tempfile.TemporaryDirectory() as td:
                root = Path(td) / "dev"
                root.mkdir()
                client = FakeClient(existing={
                    "id": "thread-1", "sessionId": "session-1", "projectId": None,
                    "cwd": str(root / "pilot"), "ephemeral": False,
                    "gitInfo": {"originUrl": "https://example.invalid/pilot.git", "branch": "main"},
                    "canAcceptDirectInput": True, "status": {"type": "active"},
                    **patch,
                })
                dispatcher, _repo = dispatcher_fixture(root, Path(td) / "tasks.sqlite3", [client])
                with self.assertRaises((bridge.IdentityGuardError, bridge.AppServerError)):
                    self._adopt(dispatcher, client)
                self.assertEqual(dispatcher.tasks.list_tasks(project="pilot"), [])
                self.assertEqual([call[0] for call in client.calls if call[0] != "thread/start"], ["initialize", "thread/read"])


if __name__ == "__main__":
    unittest.main()
