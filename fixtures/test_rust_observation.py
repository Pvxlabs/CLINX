"""Read-only native/observation regression against real SQLite and release Rust.

All homes, databases, identities and sockets are disposable fixtures. No live
Provider, production Authority or business task is contacted.
"""

import base64
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from clinx.monitor.network_observation import (
    NodeRegistryReadOnly,
    ObservationDirectory,
    ObservationWriter,
    compact,
    validate_event,
)
from clinx.monitor.observation_source import CodexObservationSource
from clinx.nodes.node_protocol import (
    NodeAuthorizationStore,
    NodeProtocolError,
    NodeRecord,
    NodeRegistry,
    _now,
)
from clinx.provider.native_history import NativeHistory, ThreadLookupError, message
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


@pytest.fixture
def directory(tmp_path):
    registry = NodeRegistry(tmp_path / "nodes.db")
    approvals = NodeAuthorizationStore(tmp_path / "identity")
    scope = approvals.grant(
        "air", user_scope="alice", read_sessions=True, projects=("/approved",)
    )
    registry.register(
        NodeRecord(
            "air",
            user_scope="alice",
            display_name="Air",
            state="ONLINE",
            last_seen=_now(),
        ),
        scope,
    )
    peers = SimpleNamespace(
        all=lambda: {"air": dict(trust_state="TRUSTED", security_status="OPAQUE_V1")}
    )
    writer = ObservationWriter(tmp_path / "observations.db")
    reader = ObservationDirectory(writer.path, registry, approvals, peers, "alice")
    yield writer, reader, registry, approvals, scope
    reader.close()
    registry.close()


def test_exact_history_pages_models_privacy_and_no_native_mutation(native):
    before = snapshot(native.root)
    page = native.history.context(TID, "air", native.metadata, 1, 16000)
    assert (
        page["last_user_intent"] == "User 2" and page["last_codex_result"] == "Answer 2"
    )
    assert page["provenance"]["context_turn_refs"] == ["turn-2"]
    assert "NEVER_" not in json.dumps(page)
    older = native.history.context(
        TID, "air", native.metadata, 1, 16000, cursor=page["next_cursor"]
    )
    assert older["last_codex_result"] == "Answer 1"
    turns, _ = CodexObservationSource(native.root, "air").native_turns(
        native.metadata, {}
    )
    assert {r["turn_id"]: r["model"] for r in turns} == {
        f"turn-{i}": f"model-{i}" for i in range(3)
    }
    assert snapshot(native.root) == before


@pytest.mark.parametrize("change", ["host", "thread", "mode", "negative", "bool"])
def test_deployed_python_native_cursors_keep_exact_binding(native, change):
    prefix = [1, TID, "air", "paginated", 2]
    valid = base64.urlsafe_b64encode(json.dumps(prefix).encode()).decode().rstrip("=")
    assert (
        native.history.context(TID, "air", native.metadata, 1, 16000, cursor=valid)[
            "last_codex_result"
        ]
        == "Answer 1"
    )
    if change == "host":
        prefix[2] = "another-node"
    if change == "thread":
        prefix[1] = "another-thread"
    if change == "mode":
        prefix[3] = "legacy"
    if change == "negative":
        prefix[4] = -1
    if change == "bool":
        prefix[4] = True
    bad = base64.urlsafe_b64encode(json.dumps(prefix).encode()).decode().rstrip("=")
    with pytest.raises(ThreadLookupError) as error:
        native.history.context(TID, "air", native.metadata, 1, 16000, cursor=bad)
    assert error.value.code == "INVALID_CONTEXT_CURSOR"


@pytest.mark.parametrize("conflict", ["symlink", "header", "turn", "offset"])
def test_fork_projection_cannot_borrow_another_identity(native, tmp_path, conflict):
    fork = "11111111-2222-3333-4444-555555555555"
    target = native.rollout.with_name(native.rollout.stem + "_" + fork + ".jsonl")
    native.rollout.rename(target)
    metadata = dict(native.metadata, rollout_path=str(target))
    with sqlite3.connect(native.root / "thread_history_1.sqlite") as c:
        c.execute("UPDATE thread_turns SET thread_id=?", (fork,))
        c.execute("UPDATE thread_items SET thread_id=?", (fork,))
        c.execute("UPDATE thread_history_projection_state SET thread_id=?", (fork,))
    assert (
        native.history.context(TID, "air", metadata, 1, 16000)["provenance"][
            "history_thread_id"
        ]
        == fork
    )
    if conflict == "symlink":
        outside = tmp_path / "outside.jsonl"
        target.rename(outside)
        target.symlink_to(outside)
    elif conflict == "header":
        target.write_text("[]\n")
    else:
        with sqlite3.connect(native.root / "thread_history_1.sqlite") as c:
            c.execute(
                "UPDATE thread_turns SET "
                + (
                    "turn_id='foreign'"
                    if conflict == "turn"
                    else "rollout_byte_offset=0"
                )
                + " WHERE turn_id='turn-2'"
            )
    with pytest.raises(ThreadLookupError) as error:
        native.history.context(TID, "air", metadata, 1, 16000)
    assert error.value.code == "NATIVE_IDENTITY_CONFLICT"


def test_utf8_markdown_redaction_and_role_budget(native):
    text = "\n    code\n\n| 列 | 值 |\n|---|---|\npassword=fixture_secret\n"
    assert message(dict(type="agentMessage", text=text))[1] == text.replace(
        "fixture_secret", "[REDACTED]"
    )
    with sqlite3.connect(native.root / "thread_history_1.sqlite") as c:
        c.execute(
            "UPDATE thread_items SET item_json=? WHERE turn_id='turn-2' AND item_type='agentMessage'",
            (json.dumps(dict(type="agentMessage", text="字" * 4000)),),
        )
    value = native.history.context(TID, "air", native.metadata, 1, 1024)
    assert len(value["last_codex_result"].encode()) <= 512
    assert value["last_user_intent"] == "User 2"
    assert value["context_truncated"]


def test_native_legacy_incremental_checkpoint_and_partial_record(native):
    with sqlite3.connect(native.root / "state_5.sqlite") as c:
        c.execute("UPDATE threads SET history_mode='legacy'")
    with native.rollout.open("a") as f:
        f.write(
            json.dumps(
                dict(
                    type="response_item",
                    payload=dict(
                        type="message",
                        role="assistant",
                        content=[dict(type="output_text", text="Legacy result")],
                    ),
                )
            )
            + "\n"
        )
        f.write(
            json.dumps(
                dict(
                    type="event_msg",
                    payload=dict(type="task_complete", turn_id="turn-2"),
                )
            )
            + "\n"
        )
    source = CodexObservationSource(native.root, "air")
    metadata = source.history.metadata(TID)
    events, state = source.sample(metadata, {})
    assert events[-1]["turn"]["native_state"] == "COMPLETED"
    assert events[-1]["turn"]["summary"] == "Legacy result"
    saved = state["offset"]
    with native.rollout.open("a") as f:
        f.write('{"type":')
    _, state = source.sample(metadata, state)
    assert state["offset"] == saved
    assert (
        source.history.context(TID, "air", metadata, 4, 16000)["last_codex_result"]
        == "Legacy result"
    )


def test_observation_atomic_gap_conflict_duplicate_terminal_and_restart(directory):
    writer, reader, registry, approvals, scope = directory
    request = dict(stream_id="stream", events=[event()])
    assert writer.accept("air", scope, request)["ack_seq"] == 1
    assert writer.accept("air", scope, request)["ack_seq"] == 1
    gap = writer.accept(
        "air", scope, dict(stream_id="stream", events=[event(2), event(4)])
    )
    assert not gap["accepted"] and gap["expected_seq"] == 3 and gap["ack_seq"] == 1
    with sqlite3.connect(writer.path) as c:
        assert c.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    conflict = event()
    conflict["title"] = "different"
    with pytest.raises(NodeProtocolError) as error:
        writer.accept("air", scope, dict(stream_id="stream", events=[conflict]))
    assert error.value.code == "OBSERVATION_SEQUENCE_CONFLICT"
    stale = event(2)
    stale["turn"].update(native_state="RUNNING", summary=None)
    writer.accept("air", scope, dict(stream_id="stream", events=[stale]))
    item = reader.list()["items"][0]
    assert (
        item["turn"]["native_state"] == "COMPLETED"
        and item["turn"]["summary"] == "公开结果"
    )
    reader.close()
    reopened = ObservationDirectory(
        writer.path, registry, approvals, reader.peers, "alice"
    )
    try:
        assert (
            reopened.detail(item["observation_id"])["turns"][0]["native_state"]
            == "COMPLETED"
        )
        assert (
            writer.accept("air", scope, dict(stream_id="stream", events=[stale]))[
                "ack_seq"
            ]
            == 2
        )
    finally:
        reopened.close()


def test_two_rust_workers_concurrently_claim_same_observation_batch(directory):
    writer, reader, _, _, scope = directory
    from clinx.nodes.network_worker import Worker

    workers = [Worker(), Worker()]

    def accept(n):
        return workers[n % 2].call(
            "observation.accept",
            path=str(writer.path),
            node="air",
            scope=scope.as_dict(),
            request=dict(stream_id="stream", events=[event()]),
        )

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert all(r["ack_seq"] == 1 for r in pool.map(accept, range(8)))
    finally:
        for worker in workers:
            worker.close()
    with sqlite3.connect(writer.path) as c:
        assert c.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    assert len(reader.list()["items"]) == 1


def test_old_python_observation_digest_and_cursor_survive_migration(directory):
    writer, reader, registry, _, scope = directory
    for n in range(1, 4):
        writer.accept(
            "air",
            scope,
            dict(stream_id="stream", events=[event(n, tid=f"thread-{n}", ordinal=n)]),
        )
    first = reader.list(limit=1)
    token = first["next_cursor"]
    raw, signature = token.split(".")
    with sqlite3.connect(writer.path) as c:
        secret = (
            c.execute("SELECT value FROM settings WHERE key='cursor_secret'")
            .fetchone()[0]
            .encode()
        )
        for payload, digest in c.execute("SELECT payload,digest FROM events"):
            assert (
                hashlib.sha256(compact(json.loads(payload)).encode()).hexdigest()
                == digest
            )
    data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    assert hmac.compare_digest(
        hmac.new(secret, data, hashlib.sha256).hexdigest(), signature
    )
    decoded = json.loads(data)
    old = compact(decoded).encode()
    old_token = (
        base64.urlsafe_b64encode(old).decode().rstrip("=")
        + "."
        + hmac.new(secret, old, hashlib.sha256).hexdigest()
    )
    assert (
        reader.list(limit=1, cursor=old_token)["items"][0]["native_thread_id"]
        != first["items"][0]["native_thread_id"]
    )
    readonly = NodeRegistryReadOnly(registry.path)
    before = writer.path.read_bytes()
    assert readonly.get("air").node_id == "air"
    assert readonly.scope("air", "alice").read_sessions
    assert writer.path.read_bytes() == before


@pytest.mark.parametrize("stamp", [0.00001, 1e-6, 1e16, 1.0, -0.0, 1.2345678901234567])
def test_historical_digest_float_encoding(directory, stamp):
    writer, _, _, _, scope = directory
    item = event()
    item["turn"].update(
        native_state="RUNNING",
        live_observation=dict(
            thread_id=TID, turn_id="turn-1", liveness="UNKNOWN", observed_at=stamp
        ),
    )
    payload = validate_event(item, "air", scope)
    writer.accept("air", scope, dict(stream_id="stream", events=[item]))
    expected = hashlib.sha256(compact(payload).encode()).hexdigest()
    with sqlite3.connect(writer.path) as c:
        assert c.execute("SELECT digest FROM events").fetchone()[0] == expected


def test_revoke_filters_cursor_and_remote_context_after_read(directory):
    writer, reader, _, approvals, scope = directory
    writer.accept(
        "air",
        scope,
        dict(stream_id="stream", events=[event(1), event(2, tid="another")]),
    )
    first = reader.list(limit=1)
    item = first["items"][0]

    def remote(_):
        def call(_):
            approvals.revoke("air")
            return dict(read_only=True, last_codex_result="WITHHELD")

        return SimpleNamespace(call=call)

    reader.remote_factory = remote
    with pytest.raises(NodeProtocolError):
        reader.context(item["observation_id"])
    assert reader.list()["items"] == []
    with pytest.raises(NodeProtocolError):
        reader.list(cursor=first["next_cursor"])


def test_copied_node_identity_never_proves_running_or_enables_control(directory):
    writer, reader, registry, approvals, scope = directory
    item = event()
    item["turn"].update(
        native_state="RUNNING",
        live_observation=dict(
            thread_id=TID, turn_id="turn-1", observed_at=time.time(), liveness="LIVE"
        ),
    )
    writer.accept("air", scope, dict(stream_id="one", events=[item]))
    approvals.grant(
        "imac", user_scope="alice", read_sessions=True, projects=("/approved",)
    )
    registry.register(
        NodeRecord("imac", user_scope="alice", state="ONLINE", last_seen=_now()), scope
    )
    reader.peers = SimpleNamespace(
        all=lambda: {
            n: dict(trust_state="TRUSTED", security_status="OPAQUE_V1")
            for n in ("air", "imac")
        }
    )
    writer.accept("imac", scope, dict(stream_id="two", events=[item]))
    for p in reader.list()["items"]:
        assert p["coverage"] == "MULTI_SOURCE_THREAD_IDENTITY"
        assert p["liveness"] == "NOT_PROVEN" and not p["control"]["enabled"]


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


def test_release_observation_marker_and_invalid_unicode_cursor_fail_promptly(directory):
    binary = Path(os.environ["CLINX_TEST_RUST_BIN_DIR"]) / "clinx-node"
    result = subprocess.run(
        [str(binary), "--observation-contract"],
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )
    assert result.stdout.strip() == "RUST_OBSERVATION_V1"
    writer, reader, _, _, scope = directory
    writer.accept(
        "air", scope, dict(stream_id="s", events=[event(1), event(2, tid="second")])
    )
    token = reader.list(limit=1)["next_cursor"]
    start = time.monotonic()
    with pytest.raises(NodeProtocolError):
        reader.list(cursor=token.split(".")[0] + "." + "字" * 21 + "x")
    assert time.monotonic() - start < 5


def test_missing_native_index_never_creates_database(tmp_path):
    history = NativeHistory(tmp_path)
    with pytest.raises(ThreadLookupError) as error:
        history.metadata(TID)
    assert error.value.code == "NATIVE_INDEX_UNAVAILABLE"
    assert list(tmp_path.iterdir()) == []


def test_local_directory_context_preserves_public_read_only_flag(native, directory):
    writer, reader, _, _, scope = directory
    writer.accept("air", scope, dict(stream_id="s", events=[event()]))
    reader.local_identity = {"node_id": "air"}
    reader.native_home = native.root
    before = snapshot(native.root)
    result = reader.context(reader.list()["items"][0]["observation_id"])
    assert result["last_codex_result"] == "Answer 2"
    assert result["read_only"] is True
    assert snapshot(native.root) == before


def test_remote_context_rechecks_a_new_projection_snapshot(directory):
    writer, reader, _, _, scope = directory
    writer.accept("air", scope, dict(stream_id="s", events=[event()]))
    identity = reader.list()["items"][0]["observation_id"]
    called = []

    def remote(record):
        def read(request):
            called.append(request)
            writer.accept(
                "air",
                scope,
                dict(stream_id="s", events=[event(2, turn="next", ordinal=2)]),
            )
            return {"context_status": "AVAILABLE", "last_codex_result": "stale turn"}

        return SimpleNamespace(call=read)

    reader.remote_factory = remote
    with pytest.raises(NodeProtocolError) as error:
        reader.context(identity)
    assert error.value.code == "EXECUTION_TURN_IDENTITY_CONFLICT"
    assert len(called) == 1
    assert reader.detail(identity)["item"]["turn"]["turn_id"] == "next"


@pytest.mark.parametrize("index", ["state_5.sqlite", "thread_history_1.sqlite"])
def test_native_permission_error_is_preserved_without_writes(native, index):
    path = native.root / index
    before = snapshot(native.root)
    mode = path.stat().st_mode & 0o777
    path.chmod(0)
    try:
        assert not os.access(path, os.R_OK), (
            "qualification requires an unprivileged user"
        )
        with pytest.raises(ThreadLookupError) as error:
            if index == "state_5.sqlite":
                native.history.metadata(TID)
            else:
                native.history.context(TID, "air", native.metadata, 1, 16000)
        assert error.value.code == "NATIVE_ACCESS_DENIED"
    finally:
        path.chmod(mode)
    assert snapshot(native.root) == before


@pytest.mark.parametrize(
    "field",
    ["title", "source_client", "coverage", "result_processing", "progress", "summary"],
)
def test_malformed_display_text_rejected_atomically(directory, field):
    writer, reader, _, _, scope = directory
    invalid = event()
    target = (
        invalid["turn"]
        if field in ("result_processing", "progress", "summary")
        else invalid
    )
    target[field] = {"text": "not display text"}
    with pytest.raises(NodeProtocolError) as error:
        writer.accept("air", scope, dict(stream_id="s", events=[invalid]))
    assert error.value.code == "INVALID_OBSERVATION"
    assert reader.list()["items"] == []
    assert (
        writer.accept("air", scope, dict(stream_id="s", events=[event()]))["ack_seq"]
        == 1
    )
