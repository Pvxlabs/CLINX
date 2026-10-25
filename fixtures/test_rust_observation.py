"""Shared native fixtures and Python package/exception compatibility smokes.

All homes, databases, identities and sockets are disposable fixtures. No live
Provider, production Authority or business task is contacted.
Semantic qualification is in kernel/tests/observation_qualification.rs.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from clinx.provider.native_history import NativeHistory, ThreadLookupError
from tools.build.runtime_sources import copy_monitor_sources

TID = "01a0fa4e-11e7-7540-91c8-1eee45d1a4d0"


def snapshot(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file() and not p.name.endswith(("-wal", "-shm"))
    }


@pytest.fixture
def native(tmp_path):
    root = tmp_path / "native"
    root.mkdir()
    rollout = root / ("rollout-fixture-" + TID + ".jsonl")
    offsets = []
    with rollout.open("wb") as stream:
        stream.write(
            (
                json.dumps(
                    dict(type="session_meta", payload=dict(id=TID, cwd="/approved"))
                )
                + "\n"
            ).encode()
        )
        for n in range(3):
            offsets.append(stream.tell())
            for row in [
                dict(
                    type="event_msg",
                    payload=dict(type="task_started", turn_id=f"turn-{n}"),
                ),
                dict(
                    type="turn_context",
                    payload=dict(
                        turn_id=f"turn-{n}", cwd="/approved", model=f"model-{n}"
                    ),
                ),
            ]:
                stream.write((json.dumps(row) + "\n").encode())
    with sqlite3.connect(root / "state_5.sqlite") as c:
        c.execute(
            "CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT,rollout_path TEXT,history_mode TEXT,name TEXT,updated_at INT)"
        )
        c.execute(
            "INSERT INTO threads VALUES(?,?,?,?,?,?)",
            (TID, "/approved", str(rollout), "paginated", "Native", 10),
        )
    with sqlite3.connect(root / "thread_history_1.sqlite") as c:
        c.executescript("""
            CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,rollout_ordinal INT,status TEXT,started_at INT,completed_at INT,rollout_byte_offset INT,PRIMARY KEY(thread_id,turn_id));
            CREATE INDEX turns_page ON thread_turns(thread_id,rollout_ordinal);
            CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,rollout_ordinal INT,created_at_ms INT,item_type TEXT,item_json TEXT,PRIMARY KEY(thread_id,turn_id,item_id));
            CREATE INDEX items_page ON thread_items(thread_id,turn_id,rollout_ordinal);
            CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY,next_rollout_byte_offset INT);
        """)
        for n in range(3):
            c.execute(
                "INSERT INTO thread_turns VALUES(?,?,?,?,?,?,?)",
                (
                    TID,
                    f"turn-{n}",
                    n,
                    "inProgress" if n == 2 else "completed",
                    n,
                    None if n == 2 else n + 1,
                    offsets[n],
                ),
            )
            for j, item in enumerate(
                [
                    dict(
                        type="userMessage",
                        content=[dict(type="text", text=f"User {n}")],
                    ),
                    dict(type="agentMessage", text=f"Answer {n}", phase="final_answer"),
                    dict(
                        type="agentMessage", text="NEVER_ANALYSIS", channel="analysis"
                    ),
                    dict(type="reasoning", text="NEVER_REASONING"),
                    dict(type="commandExecution", text="NEVER_TOOL"),
                ]
            ):
                c.execute(
                    "INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)",
                    (
                        TID,
                        f"turn-{n}",
                        str(j),
                        n * 10 + j,
                        n,
                        item["type"],
                        json.dumps(item),
                    ),
                )
        c.execute(
            "INSERT INTO thread_history_projection_state VALUES(?,?)",
            (TID, rollout.stat().st_size),
        )
    history = NativeHistory(root)
    return SimpleNamespace(
        root=root,
        rollout=rollout,
        history=history,
        metadata=history.metadata(TID),
        offsets=offsets,
    )


def event(seq=1, tid=TID, turn="turn-1", ordinal=1):
    return dict(
        seq=seq,
        native_thread_id=tid,
        provider="codex_app_server",
        project="/approved",
        title="简短标题\nsecond line",
        source_generation="generation-1",
        turn=dict(
            turn_id=turn, ordinal=ordinal, native_state="COMPLETED", summary="公开结果"
        ),
    )


def test_installed_monitor_history_uses_companion_rust_without_checkout(tmp_path):
    source = Path(__file__).resolve().parents[1]
    resources = tmp_path / "App/Contents/Resources"
    copy_monitor_sources(source, resources)
    helpers = resources.parent / "Helpers"
    helpers.mkdir()
    shutil.copyfile(
        Path(os.environ["CLINX_TEST_RUST_BIN_DIR"]) / "clinx-node",
        helpers / "clinx-node",
    )
    (helpers / "clinx-node").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "HOME")}
    code = """import sys,json
sys.path.insert(0,sys.argv[1])
from clinx.provider.native_history import message
print(json.dumps(message(dict(type='agentMessage',text='password=hidden'))))
"""
    p = subprocess.run(
        [os.sys.executable, "-I", "-B", "-c", code, str(resources)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert json.loads(p.stdout) == ["assistant", "password=[REDACTED]"]


def test_missing_native_index_never_creates_database(tmp_path):
    history = NativeHistory(tmp_path)
    with pytest.raises(ThreadLookupError) as error:
        history.metadata(TID)
    assert error.value.code == "NATIVE_INDEX_UNAVAILABLE"
    assert list(tmp_path.iterdir()) == []
