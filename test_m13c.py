from pathlib import Path
import tempfile
import unittest

from m9_integration import ClinxIntegration, M9IntegrationError
from test_m8 import HistoricalDiscoveryClient, historical_fixture


class HistoricalConversationAdoptionTests(unittest.TestCase):
    def test_active_conversation_is_not_running_and_legacy_projection_reconciles(self):
        for turn_status, cursor, expected in [
            ("completed", None, False), ("inProgress", None, True),
            ("UNKNOWN", None, True), ("completed", "older", True),
        ]:
            with self.subTest(turn_status=turn_status, cursor=cursor), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                client = HistoricalDiscoveryClient(str(root / "pilot"), high_threads=("thread-hit",), medium_threads=())
                integration, registry = self._integration(root, client)
                original_read = client.thread_read
                def read(*args, **kwargs):
                    result = original_read(*args, **kwargs)
                    result["status"] = {"type": "active"}
                    return result
                client.thread_read = read
                result = integration.adopt_conversation(project="pilot", host="p620", conversation_ref="codex://threads/thread-hit")
                task_id = result["task_ref"]
                self.assertFalse(registry.get_task(task_id).codex_running)
                # Reproduce the persisted pre-fix adoption projection, not a real execution.
                with registry._connect() as conn:
                    conn.execute("UPDATE tasks SET codex_running=1, execution_state='CODEX_RUNNING', current_stage='CODEX_RUNNING' WHERE task_id=?", (task_id,))
                client.thread_turns_list = lambda *args, **kwargs: {"data": [{"id": "turn-hit", "status": turn_status}], "nextCursor": cursor}
                integration.get_status(task_ref=task_id)
                self.assertEqual(bool(registry.get_task(task_id).codex_running), expected)
                self.assertFalse(any(call[0] in {"thread/start", "thread/resume", "turn/start"} for call in client.calls))

    def _integration(self, root: Path, client: HistoricalDiscoveryClient):
        discovery, registry, _ = historical_fixture(root, client)
        return ClinxIntegration(
            discovery.cfg,
            registry,
            discovery.dispatcher,
            discovery.reader,
            linear=None,
            topic_reader=discovery.reader,
        ), registry

    def test_exact_adoption_is_idempotent_and_does_not_start_conversation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            client = HistoricalDiscoveryClient(
                str(root / "pilot"), high_threads=("thread-hit",), medium_threads=()
            )
            integration, registry = self._integration(root, client)
            first = integration.adopt_conversation(
                project="pilot", host="p620",
                conversation_ref="codex://threads/thread-hit",
                title="Semantic token task",
            )
            second = integration.adopt_conversation(
                project="pilot", host="p620",
                conversation_ref="codex://threads/thread-hit",
                title="ignored",
            )
            self.assertEqual(first["adoption_status"], "ADOPTED")
            self.assertEqual(second["adoption_status"], "ALREADY_ADOPTED")
            self.assertEqual(first["task_ref"], second["task_ref"])
            self.assertFalse(first["new_codex_conversation_created"])
            self.assertEqual(len(registry.list_tasks(project="pilot")), 1)
            self.assertIsNotNone(registry.get_adoption(first["task_ref"]))
            self.assertFalse(any(call[0] in {"thread/start", "turn/start"} for call in client.calls))

    def test_query_requires_one_bounded_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            client = HistoricalDiscoveryClient(
                str(root / "pilot"), high_threads=("thread-hit", "thread-medium"), medium_threads=()
            )
            integration, registry = self._integration(root, client)
            with self.assertRaises(M9IntegrationError):
                integration.adopt_conversation(project="pilot", host="p620", query="product-active")
            self.assertEqual(registry.list_tasks(project="pilot"), [])

    def test_project_guard_rejects_mismatched_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            client = HistoricalDiscoveryClient(
                str(root / "wrong"), high_threads=("thread-hit",), medium_threads=()
            )
            integration, registry = self._integration(root, client)
            with self.assertRaises(Exception):
                integration.adopt_conversation(
                    project="pilot", host="p620",
                    conversation_ref="codex://threads/thread-hit",
                )
            self.assertEqual(registry.list_tasks(project="pilot"), [])


if __name__ == "__main__":
    unittest.main()
