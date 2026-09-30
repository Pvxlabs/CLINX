"""Observer contract tests use isolated fixture registries, never live authority."""
import contextlib
import hashlib
import http.client
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from task_registry import TaskRegistry
from observer_server import (APIError, ObserverAPI, ObserverHTTPServer, ObserverStore,
                             EVENT_LIMIT, menu_state, safe_text)


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=".")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "fixture.sqlite3"
        self.registry = TaskRegistry(self.path, shadow_events=True)
        self.task = self.registry.create_task(
            host="p620", workspace_alias="p620", project_alias="fixture",
            project_name="Fixture", cwd=str(Path(self.tmp.name).resolve()),
            repository_origin=None, branch="main", title="Observer fixture")
        self.ref = self.task.task_id
        self.store = ObserverStore(self.path)
        self.token = "x" * 48
        self.api = ObserverAPI(self.store, lambda: self.token)

    def request(self, path, method="GET", auth=None):
        return self.api.request(method, path, auth or "Bearer " + self.token)

    def sql(self, query, values=()):
        with sqlite3.connect(self.path) as conn:
            conn.execute(query, values)

    def claim(self):
        with self.registry.execution(self.ref, execution_ref="exec_fixture", retain=True):
            self.registry.set_execution_state(
                self.ref, "CODEX_RUNNING", current_stage="CODEX_RUNNING",
                turn_id="turn_fixture", codex_running=True)

    def result(self, execution="exec_fixture", turn="turn_fixture", status="PASS"):
        self.sql("""INSERT INTO execution_results
            (execution_ref,task_id,turn_id,status,summary,changed_files,validation,
             blockers,next_state,raw_result,received_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (execution, self.ref, turn, status, "Done", "/private/file",
             "fixture validation", "NONE", "IN_REVIEW", "RAW_SECRET",
             "2026-09-30T17:00:00+00:00"))

    def test_unknown_and_no_estimated_progress(self):
        self.sql("UPDATE tasks SET execution_state='FUTURE_STATE' WHERE task_id=?", (self.ref,))
        item = self.store.task(self.ref)
        self.assertEqual(item["state"], "FUTURE_STATE")
        self.assertEqual(item["menu_state"], "BLOCKED")
        self.assertEqual(item["execution_state"], "UNKNOWN")
        self.assertIsNone(item["progress_percent"])
        self.assertIsNone(item["elapsed_seconds"])
        self.assertEqual(item["phases"], [])
        self.assertEqual(item["artifacts"], [])
        self.assertIsNone(item["mutation_boundary"]["execution_operation_classes"])

    def test_exact_result_and_old_result_cannot_mark_new_run_pass(self):
        self.claim()
        self.result(execution="exec_old")
        self.assertIsNone(self.store.task(self.ref)["final_result"])
        self.result()
        self.assertEqual(self.store.task(self.ref)["menu_state"], "RUNNING")
        self.registry.set_execution_state(self.ref, "COMPLETED", current_stage="COMPLETED")
        self.registry.release_execution(self.ref, "exec_fixture", retain_history=True)
        item = self.store.task(self.ref)
        self.assertEqual(item["menu_state"], "PASS")
        self.assertEqual(item["final_result"]["execution_ref"], "exec_fixture")
        self.assertNotIn("RAW_SECRET", json.dumps(item))
        self.assertNotIn("/private/file", json.dumps(item))
        self.sql("UPDATE tasks SET turn_id='turn_new',execution_state='QUEUED' WHERE task_id=?", (self.ref,))
        item = self.store.task(self.ref)
        self.assertIsNone(item["execution_ref"])
        self.assertIsNone(item["final_result"])
        self.assertEqual(item["menu_state"], "IDLE")

    def test_mismatched_turn_result_hidden(self):
        self.claim()
        self.result(turn="turn_old")
        self.assertIsNone(self.store.task(self.ref)["final_result"])

    def test_serialization_allowlist_and_bounds(self):
        self.claim()
        self.sql("""UPDATE executions SET routing_identity_json=?,
                    execution_policy_json=? WHERE task_id=?""",
                 (json.dumps({"conversation": {"binding": "SECRET_BINDING"},
                              "workspace": {"worktree_key": "/private/path"},
                              "provider": {"stable_identifier": "codex", "status": "SUPPORTED",
                                           "secret": "SECRET_ROUTE"}}),
                  json.dumps({"operation_classes": ["DEVELOPMENT_MUTATION"],
                              "execution_surface": "HOST_EXECUTOR", "secret": "SECRET_POLICY"}),
                  self.ref))
        self.sql("UPDATE tasks SET title=?,current_blocker=? WHERE task_id=?",
                 ("A" * 2000, "token=DO_NOT_SHOW " + "B" * 9000, self.ref))
        self.result()
        self.sql("UPDATE execution_results SET summary=?", ("X" * 9000,))
        self.sql("""INSERT INTO host_executions
            (host_execution_ref,task_id,execution_ref,routing_identity_json,execution_policy_json,
             host,surface,operation_class,capability,operation,argv_json,cwd_identity,
             started_at,stdout,stderr,result_state,timeout_seconds,executor_instance)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("hostexec_fixture", self.ref, "exec_fixture", "{}", "{}", "p620",
             "HOST_EXECUTOR", "DEVELOPMENT_MUTATION", "LOCAL_HOST_PROCESS",
             "development_command", '["RAW_ARGV"]', "/private/cwd",
             "2026-09-30T16:00:00Z", "RAW_STDOUT", "RAW_STDERR", "RUNNING", 30, "private-instance"))
        item = self.store.task(self.ref)
        encoded = json.dumps(item, allow_nan=False)
        for secret in ("RAW_ARGV", "RAW_STDOUT", "RAW_STDERR", "SECRET_BINDING", "SECRET_ROUTE",
                       "SECRET_POLICY", "/private", "RAW_SECRET", "DO_NOT_SHOW", "private-instance"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(len(item["title"]), 512)
        self.assertTrue(item["final_result"]["text_truncated"])
        self.assertEqual(item["host_operations"][0]["operation"], "development_command")
        self.assertEqual(item["mutation_boundary"]["allowed_actions"], [])

    def test_read_only_even_when_authority_constructor_would_migrate(self):
        before = self.path.read_bytes()
        with sqlite3.connect(self.path) as conn:
            dump = list(conn.iterdump())
        self.store.health()
        self.store.list_tasks("active")
        self.store.task(self.ref)
        self.store.task_events(self.ref, None)
        with self.store.snapshot() as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM tasks")
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(dump, list(conn.iterdump()))
        self.assertEqual(before, self.path.read_bytes())

    def test_snapshot_does_not_initialize_missing_database(self):
        path = Path(self.tmp.name) / "missing.sqlite3"
        with self.assertRaises(sqlite3.OperationalError):
            ObserverStore(path).health()
        self.assertFalse(path.exists())

    def test_auth_fail_closed_and_rotate_revoke(self):
        for auth in ("", "Bearer wrong", "Bearer " + "é" * 48):
            with self.assertRaises(APIError) as caught:
                self.api.request("GET", "/v1/health", auth)
            self.assertEqual(caught.exception.status, 401)
        old = self.token
        self.token = "y" * 48
        with self.assertRaises(APIError):
            self.api.request("GET", "/v1/health", "Bearer " + old)
        self.assertEqual(self.request("/v1/health")["status"], "OK")
        self.token = None
        with self.assertRaises(APIError) as caught:
            self.api.request("GET", "/v1/health", "Bearer " + old)
        self.assertEqual(caught.exception.status, 503)

    def test_methods_and_paths_and_query_bounds(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"):
            with self.assertRaises(APIError) as caught:
                self.request("/v1/tasks/" + self.ref, method)
            self.assertEqual(caught.exception.status, 405)
        for path in ("/v1/tasks", "/v1/tasks?state=all", "/v1/tasks?state=active&state=recent",
                     "/v1/tasks?state=active&offset=-1", "/v1/tasks?state=active&offset=100001",
                     "/v1/tasks?state=active&shell=id", "/v1/tasks/%2e%2e", "https://other/v1/health",
                     "/v1/tasks/" + self.ref + "/events?after=-1"):
            with self.assertRaises(APIError):
                self.request(path)
        for path in ("/shell", "/logs", "/filesystem", "/v1/tasks/missing"):
            with self.assertRaises(APIError) as caught:
                self.request(path)
            self.assertEqual(caught.exception.status, 404)

    def test_event_cursor_order_scope_and_missing_coverage(self):
        self.claim()
        for i in range(EVENT_LIMIT + 5):
            self.registry.set_execution_state(self.ref, "CODEX_RUNNING", current_stage=f"stage_{i}")
        first = self.store.task_events(self.ref, None)
        self.assertEqual(len(first["items"]), EVENT_LIMIT)
        self.assertTrue(first["has_more"])
        second = self.store.task_events(self.ref, first["next_cursor"])
        all_events = first["items"] + second["items"]
        self.assertEqual(len({x["event_ref"] for x in all_events}), len(all_events))
        self.assertTrue(any(e["kind"] == "V1ExecutionClaimObserved" for e in all_events))
        third = self.store.task_events(self.ref, second["next_cursor"])
        self.assertEqual(third["items"], [])
        self.assertEqual(third["coverage"], "PARTIAL")
        with self.assertRaises(APIError):
            self.store.task_events(self.ref, self.store.event_cursor("other_task", 1))
        with self.assertRaises(APIError) as caught:
            self.store.task_events(self.ref, self.store.event_cursor(self.ref, 999999))
        self.assertEqual(caught.exception.status, 409)
        with self.store.snapshot() as conn:
            recent = self.store.events(conn, self.ref, recent=True)
        self.assertEqual(len(recent["items"]), 20)
        self.assertTrue(recent["has_more"])

    def test_events_disabled_not_fabricated(self):
        path = Path(self.tmp.name) / "noevents.sqlite3"
        registry = TaskRegistry(path)
        task = registry.create_task(host="p620", workspace_alias="p620", project_alias="empty",
            project_name="Empty", cwd=str(Path(self.tmp.name).resolve()), repository_origin=None,
            branch=None, title="Empty")
        events = ObserverStore(path).task_events(task.task_id, None)
        self.assertEqual(events["coverage"], "UNAVAILABLE")
        self.assertEqual(events["items"], [])

    def test_active_recent_classification_and_pagination(self):
        self.sql("UPDATE tasks SET execution_state='BLOCKED' WHERE task_id=?", (self.ref,))
        self.assertEqual(len(self.store.list_tasks("active")["items"]), 1)
        self.assertEqual(self.store.list_tasks("recent")["items"], [])
        self.sql("UPDATE tasks SET execution_state='IN_REVIEW' WHERE task_id=?", (self.ref,))
        self.assertEqual(len(self.store.list_tasks("recent")["items"]), 1)
        self.assertEqual(self.store.list_tasks("active")["items"], [])
        self.assertEqual(self.store.list_tasks("recent", 50)["items"], [])

    def test_menu_mapping(self):
        for state in ("BLOCKED", "FAILED", "RECOVERY_REQUIRED", "TRANSPORT_UNCERTAIN", "UNRECOGNIZED"):
            self.assertEqual(menu_state(state, False, False, None), "BLOCKED")
        self.assertEqual(menu_state("COMPLETED", False, False, None), "IDLE")
        self.assertEqual(menu_state("IN_REVIEW", False, False, {"status": "PASS"}), "PASS")
        self.assertEqual(menu_state("CODEX_RUNNING", True, False, {"status": "PASS"}), "RUNNING")
        self.assertEqual(menu_state("COMPLETED", False, True, {"status": "PASS"}), "BLOCKED")

    def test_http_routes_auth_redaction_and_no_logs(self):
        server = ObserverHTTPServer(0, self.api)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.sql("UPDATE tasks SET title=? WHERE task_id=?", (self.token, self.ref))
        logs = io.StringIO()
        with contextlib.redirect_stderr(logs):
            for method, path, headers, expected in (
                ("GET", "/v1/health", {}, 401),
                ("GET", "/v1/health", {"Authorization": "Bearer " + self.token}, 200),
                ("GET", "/v1/tasks?state=active", {"Authorization": "Bearer " + self.token}, 200),
                ("GET", "/v1/tasks/" + self.ref, {"Authorization": "Bearer " + self.token}, 200),
                ("GET", "/v1/tasks/" + self.ref + "/events", {"Authorization": "Bearer " + self.token}, 200),
                ("DELETE", "/v1/tasks/" + self.ref, {"Authorization": "Bearer " + self.token}, 405),
            ):
                client = http.client.HTTPConnection(*server.server_address, timeout=2)
                client.request(method, path, headers=headers)
                response = client.getresponse()
                body = response.read()
                self.assertEqual(response.status, expected)
                self.assertEqual(response.getheader("Cache-Control"), "no-store")
                self.assertNotIn(self.token.encode(), body)
                self.assertIsInstance(json.loads(body), dict)
                client.close()
        self.assertEqual(logs.getvalue(), "")
    def test_event_filters_other_tasks_and_unknown_kinds(self):
        self.claim()
        before = self.store.task_events(self.ref, None)["items"]
        other = self.registry.create_task(
            host="p620", workspace_alias="p620", project_alias="other", project_name="Other",
            cwd=str(Path(self.tmp.name).resolve()), repository_origin=None, branch=None, title="Other")
        self.registry.set_execution_state(other.task_id, "BLOCKED", current_stage="BLOCKED")
        with self.registry._shadow_write_connection() as conn:
            self.registry._append_shadow_observation(
                conn, task_id=self.ref, execution_ref="exec_fixture",
                event_type="V1FutureFactObserved", observed_at="2026-09-30T00:00:00Z", payload={})
        after = self.store.task_events(self.ref, None)["items"]
        self.assertEqual(before, after)
        self.assertEqual(len(self.store.task_events(other.task_id, None)["items"]), 1)

    def test_multiple_pages_do_not_hide_tasks(self):
        for i in range(51):
            self.registry.create_task(
                host="p620", workspace_alias="p620", project_alias="fixture", project_name="Fixture",
                cwd=str(Path(self.tmp.name).resolve()), repository_origin=None, branch=None,
                title=f"Fixture {i}")
        first = self.store.list_tasks("active")
        second = self.store.list_tasks("active", first["next_offset"])
        self.assertEqual(len(first["items"]), 50)
        self.assertTrue(first["has_more"])
        self.assertEqual(len(second["items"]), 2)
        self.assertFalse(second["has_more"])
        self.assertEqual(len({t["task_ref"] for t in first["items"] + second["items"]}), 52)

    def test_operations_bound_and_reasoning_exact_resulting_task(self):
        self.test_serialization_allowlist_and_bounds()
        with sqlite3.connect(self.path) as conn:
            columns = [r[1] for r in conn.execute("PRAGMA table_info(host_executions)")]
            row = dict(zip(columns, conn.execute("SELECT * FROM host_executions").fetchone()))
            for i in range(25):
                row["host_execution_ref"] = f"hostexec_{i:03}"
                conn.execute("INSERT INTO host_executions VALUES (" + ",".join("?" for _ in columns) + ")",
                             [row[c] for c in columns])
            conn.execute("""INSERT INTO prepared_executions
                (prepared_execution_ref,integrity_hash,approval_state,task_action,host,project,
                 title,prompt,model,reasoning_effort,execution_mode,status,created_at,updated_at,
                 resulting_task_id,resulting_execution_ref)
                VALUES ('prepared_fixture','hash','APPROVED','create','p620','fixture',
                        'title','PRIVATE_PROMPT','model','high','normal','DISPATCHED',
                        '2026-09-30T00:00:00Z','2026-09-30T00:00:00Z',?,?)""",
                (self.ref, "exec_fixture"))
        item = self.store.task(self.ref)
        self.assertEqual(len(item["host_operations"]), 20)
        self.assertTrue(item["host_operations_has_more"])
        self.assertEqual(item["reasoning"], "high")
        self.assertNotIn("PRIVATE_PROMPT", json.dumps(item))
        self.sql("UPDATE prepared_executions SET resulting_task_id='wrong'")
        self.assertIsNone(self.store.task(self.ref)["reasoning"])

    def test_http_body_duplicate_auth_failure_and_source_unavailable(self):
        server = ObserverHTTPServer(0, self.api)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = http.client.HTTPConnection(*server.server_address, timeout=2)
        client.putrequest("GET", "/v1/health")
        client.putheader("Authorization", "Bearer " + self.token)
        client.putheader("Authorization", "Bearer " + self.token)
        client.endheaders()
        response = client.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        client.close()
        client = http.client.HTTPConnection(*server.server_address, timeout=2)
        client.request("GET", "/v1/health", body="x",
                       headers={"Authorization": "Bearer " + self.token})
        response = client.getresponse()
        self.assertEqual(response.status, 400)
        response.read()
        client.close()
        self.store.path = Path(self.tmp.name).resolve() / "missing.sqlite3"
        client = http.client.HTTPConnection(*server.server_address, timeout=2)
        client.request("GET", "/v1/health", headers={"Authorization": "Bearer " + self.token})
        response = client.getresponse()
        self.assertEqual(response.status, 503)
        self.assertEqual(json.loads(response.read())["error"], "SOURCE_UNAVAILABLE")
        client.close()

    def test_elapsed_timestamp_bounds(self):
        from observer_server import elapsed, timestamp
        self.assertIsNone(timestamp("no-date"))
        self.assertIsNone(timestamp("2026-09-30T00:00:00"))
        self.assertIsNone(elapsed("2026-09-30T02:00:00Z", "2026-09-30T01:00:00Z"))
        self.assertEqual(elapsed("2026-09-30T01:00:00Z", "2026-09-30T02:00:00Z"), 3600)

if __name__ == "__main__":
    unittest.main()