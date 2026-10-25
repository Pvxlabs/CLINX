"""Python native Observer binding smoke and shared historical database fixture.

Checkpoint, native history, managed source, HTTP and MCP qualification now live
in kernel/tests/native_observer_qualification.rs. These cases retain Python
return/exception types and the public SQLite snapshot context-manager boundary.
"""

import base64
import json
import sqlite3
from types import SimpleNamespace

import pytest

from clinx.monitor.native_observation_bridge import NativeCollectorBridge
from clinx.monitor.observer_server import ActivityReader, APIError, activity_text
from fixtures.test_rust_observation import native as native

TID = "01a0fa4e-11e7-7540-91c8-1eee45d1a4d0"
TURN = "00000000-0000-0000-0000-000000000002"
ROUTE = {
    "host": {"stable_identifier": "p620"},
    "provider": {"stable_identifier": "codex_app_server"},
    "conversation": {"binding": TID},
}


def test_activity_cursor_codec_and_text_match_public_contract():
    expected = (
        base64.urlsafe_b64encode(json.dumps(["字😀", "after", 7, "item"]).encode())
        .decode()
        .rstrip("=")
    )
    assert ActivityReader.encode("字😀", "after", [7, "item"]) == expected
    assert ActivityReader.decode(expected, "字😀", "after") == (7, "item")
    for position in ([True, "item"], [-1, "item"], [2**63, "item"], [1, "x" * 257]):
        token = (
            base64.urlsafe_b64encode(json.dumps(["scope", "after", *position]).encode())
            .decode()
            .rstrip("=")
        )
        with pytest.raises(APIError):
            ActivityReader.decode(token, "scope", "after")
    assert (
        activity_text("prefix\x00 secret=bad Bearer token")
        == "prefix [REDACTED] Bearer [REDACTED]"
    )


@pytest.fixture
def historical(tmp_path):
    from clinx.monitor.observer_server import ObserverStore

    path = tmp_path / "historical.sqlite3"
    with sqlite3.connect(path) as c:
        c.executescript("""
            CREATE TABLE tasks(task_id TEXT PRIMARY KEY,project_alias TEXT,title TEXT,host TEXT,status TEXT,execution_state TEXT,current_stage TEXT,current_blocker TEXT,created_at TEXT,updated_at TEXT,last_progress_at TEXT,codex_running INT,retry_required INT,turn_id TEXT);
            CREATE TABLE executions(execution_ref TEXT,task_id TEXT,logical_model TEXT,resolved_model TEXT,stage TEXT,execution_state TEXT,turn_id TEXT,acquired_at TEXT,routing_identity_json TEXT,execution_policy_json TEXT);
            CREATE TABLE execution_history(execution_ref TEXT,task_id TEXT,logical_model TEXT,resolved_model TEXT,stage TEXT,execution_state TEXT,turn_id TEXT,acquired_at TEXT,routing_identity_json TEXT,execution_policy_json TEXT,released_at TEXT);
            CREATE TABLE execution_results(task_id TEXT,execution_ref TEXT,turn_id TEXT,status TEXT,summary TEXT,validation TEXT,blockers TEXT,next_state TEXT,received_at TEXT);
            CREATE TABLE conversation_bindings(task_id TEXT,thread_id TEXT);
            CREATE TABLE prepared_executions(resulting_execution_ref TEXT,resulting_task_id TEXT,reasoning_effort TEXT,created_at TEXT);
            CREATE TABLE host_executions(task_id TEXT,host_execution_ref TEXT,execution_ref TEXT,host TEXT,surface TEXT,operation_class TEXT,capability TEXT,operation TEXT,started_at TEXT,completed_at TEXT,duration_ms INT,exit_code INT,result_state TEXT,timed_out INT);
            CREATE TABLE v2_events(cursor INTEGER PRIMARY KEY,event_id TEXT,event_type TEXT,occurred_at TEXT,recorded_at TEXT,payload_json TEXT);
        """)
        c.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "task-fixture",
                "fixture",
                "Title",
                "p620",
                "ACTIVE",
                "COMPLETED",
                "COMPLETE",
                "",
                "2026-10-25T01:00:00Z",
                "2026-10-25T02:00:00Z",
                "2026-10-25T02:00:00Z",
                0,
                0,
                TURN,
            ),
        )
        c.execute(
            "INSERT INTO execution_history VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "exec-fixture",
                "task-fixture",
                "logical",
                "resolved",
                "COMPLETE",
                "COMPLETED",
                TURN,
                "2026-10-25T01:00:00Z",
                json.dumps(ROUTE),
                json.dumps(
                    dict(
                        operation_classes=["DEVELOPMENT_MUTATION"],
                        execution_surface="HOST_EXECUTOR",
                    )
                ),
                "2026-10-25T02:00:00Z",
            ),
        )
        c.execute(
            "INSERT INTO execution_results VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "task-fixture",
                "exec-fixture",
                TURN,
                "PASS",
                "Done",
                "checked",
                "NONE",
                "IN_REVIEW",
                "2026-10-25T02:00:00Z",
            ),
        )
        c.execute(
            "INSERT INTO conversation_bindings VALUES (?,?)", ("task-fixture", TID)
        )
    store = ObserverStore(path, codex_host_ids=dict(p620="remote-ssh-discovered:p620"))
    yield SimpleNamespace(path=path, store=store)
    store.close()


def test_historical_observer_rust_projection_exact_result_and_snapshot(historical):
    from clinx.monitor.observer_server import ObserverStore

    before = historical.path.read_bytes()
    item = historical.store.task("task-fixture")
    assert item["menu_state"] == "PASS" and item["elapsed_seconds"] == 3600
    assert item["final_result"]["execution_ref"] == "exec-fixture"
    assert (
        item["codex_uri"]
        == "codex://threads/" + TID + "?hostId=remote-ssh-discovered%3Ap620"
    )
    assert item["native_conversation"]["thread_id"] == TID
    assert item["routing"]["provider"]["identifier"] == "codex_app_server"
    assert item["mutation_boundary"]["allowed_actions"] == []
    assert historical.store.list_tasks("active")["items"] == []
    assert len(historical.store.list_tasks("recent")["items"]) == 1
    with historical.store.snapshot() as c:
        with pytest.raises(sqlite3.OperationalError):
            c.execute("DELETE FROM tasks")
        assert historical.store.task_row(c, "task-fixture")["turn_id"] == TURN
        projected = historical.store.project(
            c, dict(c.execute("SELECT * FROM tasks").fetchone()), "fixed-observation"
        )
        assert projected["timestamps"]["observed_at"] == "fixed-observation"
    assert historical.path.read_bytes() == before
    with sqlite3.connect(historical.path) as c:
        c.execute("UPDATE tasks SET turn_id='new-turn',execution_state='QUEUED'")
    next_task = historical.store.task("task-fixture")
    assert next_task["execution_ref"] is None and next_task["final_result"] is None
    assert next_task["menu_state"] == "IDLE"
    absent = ObserverStore(historical.path.parent / "missing.sqlite")
    try:
        with pytest.raises(sqlite3.OperationalError):
            absent.health()
        assert not absent.path.exists()
    finally:
        absent.close()


def test_native_collector_secret_binding_keeps_bytes():
    bridge = NativeCollectorBridge(None)
    try:
        bridge.secret = b"new-lifetime"
        assert bridge.secret == b"new-lifetime"
    finally:
        bridge.close()


def test_managed_source_preserves_python_authority_exception(native):
    from clinx.authority.rust_authority import RustAuthorityError
    from clinx.monitor.rust_observation import RustObservationSource

    def command(operation, args):
        raise RustAuthorityError("IDENTITY_CONFLICT")

    source = RustObservationSource(native.root, "p620", commands=SimpleNamespace(call=command))
    try:
        with pytest.raises(RustAuthorityError, match="IDENTITY_CONFLICT"):
            source.canonical_inventory(SimpleNamespace(allows_project=lambda _: True))
        with pytest.raises(RustAuthorityError, match="IDENTITY_CONFLICT"):
            source.sample(native.metadata, {})
    finally:
        source.close()


def test_native_desktop_limits_keep_python_tuple_and_exception():
    from clinx.authority.rust_authority import RustAuthorityError
    from clinx.authority.rust_reads import limits

    for recent, max_bytes in ((True, 32000), (0, 32000), (21, 32000), (8, True), (8, 128001)):
        with pytest.raises(RustAuthorityError, match="INVALID_CONTEXT_LIMIT"):
            limits(recent, max_bytes)
    assert limits(None, None) == (8, 32000)
