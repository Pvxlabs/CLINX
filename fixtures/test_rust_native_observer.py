"""Independent historical SQL input and frozen pre-retirement projection oracle.

Observer semantics are qualified in kernel/tests/native_observer_qualification.rs;
HTTP still compares against the captured 26af9ee result, not another native read.
"""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

TID = "01a0fa4e-11e7-7540-91c8-1eee45d1a4d0"
TURN = "00000000-0000-0000-0000-000000000002"
ROUTE = {
    "host": {"stable_identifier": "p620"},
    "provider": {"stable_identifier": "codex_app_server"},
    "conversation": {"binding": TID},
}


@pytest.fixture
def historical(tmp_path):
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
    expected = json.loads((Path(__file__).parent / "observer_historical_expected.json").read_text())
    return SimpleNamespace(path=path, expected=expected)
