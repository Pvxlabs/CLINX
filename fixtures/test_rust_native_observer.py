"""Rust native read boundary: isolated history, identity, HTTP and activity cursors."""

import base64
import hashlib
import hmac
import json
import sqlite3
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from clinx.monitor.native_observation_bridge import (
    NativeCollectorBridge,
    RemoteNativeSource,
)
from clinx.monitor.observer_server import (
    ActivityReader,
    APIError,
    ObserverAPI,
    ObserverHTTPServer,
    activity_text,
)
from clinx.nodes.node_protocol import NodeProtocolError, SharingScope
from fixtures.test_rust_observation import TID, snapshot
from fixtures.test_rust_observation import native as native


@pytest.fixture
def bridge(native):
    with sqlite3.connect(native.root / "state_5.sqlite") as c:
        c.execute("UPDATE threads SET updated_at=?", (int(time.time()),))
    scope = SharingScope(
        user_scope="alice", read_sessions=True, projects=("/approved",)
    )
    directory = SimpleNamespace(
        local_identity={"node_id": "p620"},
        native_home=native.root,
        _scope=lambda _: scope,
    )
    bridge = NativeCollectorBridge(directory)
    yield bridge
    bridge.close()


def test_checkpoint_contract_tampering_scope_generation_and_readonly(bridge, native):
    before = snapshot(native.root)
    first = bridge.sample(TID)
    secret = bridge.secret
    wire, signature = first["checkpoint"].split(".")
    raw = base64.urlsafe_b64decode(wire + "=" * (-len(wire) % 4))
    assert hmac.new(secret, raw, hashlib.sha256).hexdigest() == signature
    saved = json.loads(raw)
    assert saved["binding"][0] == TID and saved["state"]
    assert bridge.sample(TID, first["checkpoint"])["thread_id"] == TID
    for token in (first["checkpoint"] + "x", wire + ".fake", wire + "." + "中" * 64):
        with pytest.raises(NodeProtocolError, match="NATIVE_COLLECTOR_CURSOR_RESET"):
            bridge.sample(TID, token)
    saved["expires"] = 0
    expired = json.dumps(saved).encode()
    token = (
        base64.urlsafe_b64encode(expired).decode().rstrip("=")
        + "."
        + hmac.new(secret, expired, hashlib.sha256).hexdigest()
    )
    with pytest.raises(NodeProtocolError, match="NATIVE_COLLECTOR_CURSOR_RESET"):
        bridge.sample(TID, token)
    bridge.secret = b"new-lifetime"
    with pytest.raises(NodeProtocolError, match="NATIVE_COLLECTOR_CURSOR_RESET"):
        bridge.sample(TID, first["checkpoint"])
    bridge.directory._scope = lambda _: SharingScope(
        user_scope="alice", read_sessions=True, projects=("/foreign",)
    )
    with pytest.raises(NodeProtocolError, match="PROJECT_SCOPE_DENIED"):
        bridge.sample(TID)
    bridge.directory._scope = lambda _: None
    with pytest.raises(NodeProtocolError, match="SHARING_SCOPE_DENIED"):
        bridge.inventory()
    assert snapshot(native.root) == before


def test_scope_revocation_during_read_is_rechecked(bridge):
    bridge.inventory()
    original = bridge.source.sample

    def revoke(*args):
        result = original(*args)
        bridge.directory._scope = lambda _: None
        return result

    bridge.source.sample = revoke
    with pytest.raises(NodeProtocolError, match="SHARING_SCOPE_DENIED"):
        bridge.sample(TID)


def test_authenticated_http_remote_checkpoint_reset_and_no_writes(
    bridge, native, tmp_path, monkeypatch
):
    before = snapshot(native.root)
    token = "a" * 40
    credential = tmp_path / "credential"
    credential.write_text(token)
    api = ObserverAPI(SimpleNamespace(), lambda: token, bridge.directory)
    api.native_collector = bridge
    server = ObserverHTTPServer(0, api)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    remote = RemoteNativeSource(credential)
    port = server.server_address[1]

    def loopback(request, **kwargs):
        request.full_url = request.full_url.replace(
            "127.0.0.1:8766", f"127.0.0.1:{port}"
        )
        return urlopen(request, **kwargs)

    monkeypatch.setattr("clinx.monitor.native_observation_bridge.urlopen", loopback)
    try:
        rows, _, coverage = remote.inventory(bridge.scope())
        assert coverage == "INDEXED" and rows[0]["id"] == TID
        state = {}
        turns, _ = remote.native_turns(rows[0], state)
        assert {t["turn_id"] for t in turns} == {"turn-0", "turn-1", "turn-2"}
        bridge.secret = b"restart-session"
        assert remote.native_turns(rows[0], state)[0]
        for method, expected in (("GET", 401), ("POST", 405)):
            req = Request(
                f"http://127.0.0.1:{port}/v2/native-collector/inventory",
                method=method,
                headers={
                    "Authorization": "Bearer " + ("wrong" if method == "GET" else token)
                },
            )
            with pytest.raises(HTTPError) as error:
                urlopen(req)
            assert error.value.code == expected
        for query in ("audit=2", "unexpected=1", "after=not-json"):
            with pytest.raises(APIError) as error:
                api.request(
                    "GET", "/v2/native-collector/inventory?" + query, "Bearer " + token
                )
            assert error.value.code == "INVALID_QUERY"
        bridge.directory._scope = lambda _: None
        with pytest.raises(NodeProtocolError, match="SHARING_SCOPE_DENIED"):
            remote.native_turns(rows[0], state)
    finally:
        remote.close()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    assert snapshot(native.root) == before


def test_remote_unavailable_cooldown_and_authority_claims(tmp_path, monkeypatch):
    credential = tmp_path / "credential"
    credential.write_text("a" * 40)
    remote = RemoteNativeSource(credential)
    count = []

    def unavailable(*args, **kwargs):
        count.append(1)
        raise TimeoutError()

    monkeypatch.setattr("clinx.monitor.native_observation_bridge.urlopen", unavailable)
    try:
        for _ in range(56):
            with pytest.raises(NodeProtocolError, match="NATIVE_OBSERVER_UNAVAILABLE"):
                remote.read("inventory")
        assert len(count) == 1
        remote.read = lambda *a, **k: dict(
            thread_id=TID,
            project="/approved",
            checkpoint="cursor",
            events=[
                dict(
                    native_thread_id=TID,
                    project="/approved",
                    task_ref="forged",
                    binding_evidence="CANONICAL_ROUTE",
                    turn={"execution_ref": "fake"},
                )
            ],
        )
        with pytest.raises(
            NodeProtocolError, match="NATIVE_OBSERVATION_IDENTITY_CONFLICT"
        ):
            remote.native_turns(dict(id=TID, cwd="/approved"), {})
    finally:
        remote.close()


TURN = "00000000-0000-0000-0000-000000000002"
ROUTE = {
    "host": {"stable_identifier": "p620"},
    "provider": {"stable_identifier": "codex_app_server"},
    "conversation": {"binding": TID},
}
EXECUTION = dict(
    execution_ref="exec-fixture", turn_id=TURN, routing_identity_json=json.dumps(ROUTE)
)


@pytest.fixture
def activity(tmp_path):
    root = tmp_path / "activity"
    root.mkdir()
    with sqlite3.connect(root / "state_5.sqlite") as c:
        c.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, history_mode TEXT)")
        c.execute("INSERT INTO threads VALUES (?,?)", (TID, "paginated"))
    history = root / "thread_history_1.sqlite"
    with sqlite3.connect(history) as c:
        c.executescript("""CREATE TABLE thread_turns(thread_id TEXT, turn_id TEXT);
            CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,rollout_ordinal INT,
            updated_at_ordinal INT,created_at_ms INT,item_type TEXT,item_json TEXT);
            CREATE INDEX items_page ON thread_items(thread_id,turn_id,rollout_ordinal);
            CREATE INDEX items_update ON thread_items(thread_id,turn_id,updated_at_ordinal);""")
        c.execute("INSERT INTO thread_turns VALUES (?,?)", (TID, TURN))
    reader = ActivityReader(root)

    def add(n, **item):
        with sqlite3.connect(history) as c:
            c.execute(
                "INSERT INTO thread_items VALUES (?,?,?,?,?,?,?,?)",
                (
                    TID,
                    TURN,
                    f"item{n}",
                    n,
                    n,
                    n * 1000,
                    item.pop("type", "agentMessage"),
                    json.dumps(dict(text=f"Feedback {n}", phase="commentary") | item),
                ),
            )

    yield SimpleNamespace(
        root=root,
        history=history,
        reader=reader,
        add=add,
        read=lambda **kw: reader.read("task-fixture", EXECUTION, **kw),
    )
    reader.close()


def test_activity_pagination_revision_and_restart(activity):
    for n in range(1, 92):
        activity.add(n)
    first = activity.read()
    assert [r["ordinal"] for r in first["items"]] == list(range(52, 92))
    older = activity.read(before=first["older_cursor"])
    assert [r["ordinal"] for r in older["items"]] == list(range(12, 52))
    assert len(activity.read(before=older["older_cursor"])["items"]) == 11
    with sqlite3.connect(activity.history) as c:
        c.execute(
            "UPDATE thread_items SET item_json=?,updated_at_ordinal=92 WHERE item_id='item91'",
            (json.dumps(dict(text="Revised", phase="commentary")),),
        )
    activity.add(93)
    delta = activity.read(after=first["next_cursor"])
    assert [r["id"] for r in delta["items"]] == ["item91", "item93"]
    assert delta["items"][0]["text"] == "Revised"
    for n in range(94, 144):
        activity.add(n)
    delta2 = activity.read(after=delta["next_cursor"])
    assert delta2["has_more"] and len(delta2["items"]) == 40
    assert len(activity.read(after=delta2["next_cursor"])["items"]) == 10
    restarted = ActivityReader(activity.root)
    try:
        with pytest.raises(APIError) as error:
            restarted.read("task-fixture", EXECUTION, after=first["next_cursor"])
        assert error.value.code == "ACTIVITY_CURSOR_RESET_REQUIRED"
    finally:
        restarted.close()


def test_activity_scope_allowlist_redaction_and_readonly(activity):
    activity.add(1)
    activity.add(2, phase="analysis", text="NEVER_ANALYSIS")
    activity.add(3, channel="analysis", text="NEVER_ANALYSIS")
    activity.add(4, type="commandExecution", text="NEVER_TOOL")
    activity.add(
        5,
        text="secret=NEVER_SECRET Bearer NEVER_TOKEN sk-abcdefghijklmnopqrstuvwxyz "
        + "中" * 20000,
    )
    activity.add(6, text="-----BEGIN PRIVATE KEY-----\nNEVER_PRIVATE" + "x" * 20000)
    activity.add(7, text="Done", phase="final_answer")
    before = snapshot(activity.root)
    first = activity.read()
    encoded = json.dumps(first)
    for secret in (
        "NEVER_ANALYSIS",
        "NEVER_TOOL",
        "NEVER_SECRET",
        "NEVER_TOKEN",
        "NEVER_PRIVATE",
        "sk-abcdefghijklmnopqrstuvwxyz",
    ):
        assert secret not in encoded
    assert all(len(r["text"].encode()) <= 16384 for r in first["items"])
    assert first["items"][-1]["kind"] == "result"
    assert any(r["truncated"] for r in first["items"])
    assert snapshot(activity.root) == before
    for kwargs in (dict(after="garbage"), dict(before=first["next_cursor"])):
        with pytest.raises(APIError) as error:
            activity.read(**kwargs)
        assert error.value.status == 409
    with pytest.raises(APIError):
        activity.reader.read("task-other", EXECUTION, after=first["next_cursor"])
    with sqlite3.connect(activity.history) as c:
        c.execute("DELETE FROM thread_items")
    with pytest.raises(APIError):
        activity.read(after=first["next_cursor"])
    activity.history.unlink()
    assert activity.read()["reason"] == "NATIVE_HISTORY_UNAVAILABLE"
    assert not activity.history.exists()


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


def test_managed_context_requires_exact_provider_identity_and_projection_recheck(
    monkeypatch,
):
    from clinx.monitor.rust_monitor import RustObservationDirectory

    item = dict(
        node_id="p620",
        project="/approved",
        native_thread_id=TID,
        task_ref="task-exact",
        turn=dict(execution_ref="exec-exact", turn_id="turn-exact"),
        source="CLINX_MANAGED",
        binding_evidence="CANONICAL_ROUTE",
    )
    result = dict(
        read_only=True,
        thread_id=TID,
        task_ref="task-exact",
        execution_ref="exec-exact",
        execution_turn_ref="turn-exact",
        context_scope="EXACT_EXECUTION_TURN",
    )
    calls = []
    directory = SimpleNamespace(
        detail=lambda *a, **k: dict(item=item),
        context=lambda *a, **k: dict(remote=True),
    )
    integration = SimpleNamespace(
        cfg=SimpleNamespace(runtime_host="p620"),
        get_context=lambda **kw: calls.append(kw) or result,
    )
    proxy = RustObservationDirectory(directory, integration)
    try:
        assert proxy.context("observation")["schema_version"] == "clinx-observation-v1"
        assert (
            calls[-1]["execution_ref"] == "exec-exact" and calls[-1]["thread_id"] == TID
        )
        for key, value in (
            ("read_only", False),
            ("thread_id", "wrong"),
            ("task_ref", "other"),
            ("execution_ref", "other"),
            ("execution_turn_ref", "other"),
            ("context_scope", "LATEST_THREAD"),
        ):
            original = result[key]
            result[key] = value
            with pytest.raises(
                NodeProtocolError, match="EXECUTION_TURN_IDENTITY_CONFLICT"
            ):
                proxy.context("observation")
            result[key] = original
        old = directory.detail
        reads = []
        directory.detail = lambda *a, **k: (
            reads.append(1)
            or dict(item=item if len(reads) == 1 else dict(item, project="/changed"))
        )
        with pytest.raises(NodeProtocolError, match="EXECUTION_TURN_IDENTITY_CONFLICT"):
            proxy.context("observation")
        directory.detail = old
        item["binding_evidence"] = "THREAD_UNBOUND"
        with pytest.raises(NodeProtocolError, match="EXACT_RUST_EXECUTION_REQUIRED"):
            proxy.context("observation")
        item["node_id"] = "air"
        assert proxy.context("observation") == {"remote": True}
        item.update(node_id="p620", source="CODEX_NATIVE")
        monkeypatch.setattr(
            "clinx.authority.rust_reads.desktop_read",
            lambda *a, **k: dict(project="/other"),
        )
        with pytest.raises(NodeProtocolError, match="THREAD_SCOPE_MISMATCH"):
            proxy.context("observation")
    finally:
        proxy.close()


@pytest.mark.parametrize("fault", [None, "id", "writable", "public-error", "truncated"])
def test_existing_unix_mcp_read_transport_checks_identity_and_public_errors(
    request, fault
):
    import socket
    import tempfile
    from pathlib import Path

    from clinx.monitor.rust_monitor import MCPReader

    socket_root = tempfile.TemporaryDirectory(prefix="clinx-observer-", dir="/tmp")
    request.addfinalizer(socket_root.cleanup)
    path = Path(socket_root.name) / "mcp.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    received = []

    def serve():
        with listener.accept()[0] as peer:
            request = json.loads(peer.makefile("rb").readline())
            received.append(request)
            payload = dict(read_only=fault != "writable", items=[])
            result = dict(structuredContent=payload)
            if fault == "public-error":
                result = dict(
                    isError=True,
                    structuredContent=dict(error_code="SHARING_SCOPE_DENIED"),
                )
            reply = dict(
                jsonrpc="2.0",
                id="wrong" if fault == "id" else request["id"],
                result=result,
            )
            peer.sendall(
                json.dumps(reply).encode() + (b"" if fault == "truncated" else b"\n")
            )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    reader = MCPReader(path)
    try:
        with pytest.raises(NodeProtocolError, match="OBSERVER_READ_ONLY"):
            reader.call("clinx_create_task")
        if fault:
            with pytest.raises(
                NodeProtocolError,
                match="SHARING_SCOPE_DENIED"
                if fault == "public-error"
                else "RUST_OBSERVATION_UNAVAILABLE",
            ):
                reader.call("clinx_list_observations", dict(limit=1))
        else:
            assert reader.call("clinx_list_observations", dict(limit=1)) == dict(
                read_only=True, items=[]
            )
        thread.join(timeout=5)
        assert received[0]["params"] == dict(
            name="clinx_list_observations", arguments=dict(limit=1)
        )
    finally:
        reader.close()
        listener.close()


def test_monitor_v1_projection_requires_live_rust_reads():
    from clinx.monitor.rust_monitor import RustMonitorStore

    calls = []
    reader = SimpleNamespace(
        call=lambda name, args=None: (
            calls.append(name) or dict(authority="RUST", read_only=True)
        )
    )
    store = RustMonitorStore(reader)
    try:
        assert store.health()["observation_source"] == "RUST_CENTRE_V2"
        assert calls == ["clinx_get_capabilities", "clinx_list_observations"]
        assert store.list_tasks("RUNNING")["items"] == []
        assert calls[-1] == "clinx_list_observations"
        reader.call = lambda *a, **k: dict(authority="PYTHON")
        with pytest.raises(NodeProtocolError, match="RUST_AUTHORITY_REQUIRED"):
            store.health()
    finally:
        store.close()


def test_native_desktop_display_does_not_promote_history_and_rechecks_scope(
    bridge, native, monkeypatch
):
    from contextlib import contextmanager

    from clinx.authority.rust_authority import RustAuthorityError
    from clinx.authority.rust_reads import limits, observe_desktop

    before = snapshot(native.root)
    monkeypatch.delenv("CLINX_NATIVE_OBSERVER_SOCKET", raising=False)
    proof = dict(liveness="UNKNOWN", reason="NOT_OBSERVED")
    monkeypatch.setattr(
        "clinx.provider.native_provider.observe_native_writer",
        lambda *a, **k: dict(proof),
    )
    result = observe_desktop(bridge.directory, TID, context=True)
    assert result["read_only"] is True and result["task_ref"] is None
    assert (
        result["native_display_state"] == "UNKNOWN" and result["codex_running"] is False
    )
    assert "Answer 2" in json.dumps(result)
    for recent, max_bytes in (
        (True, 32000),
        (0, 32000),
        (21, 32000),
        (8, True),
        (8, 128001),
    ):
        with pytest.raises(RustAuthorityError, match="INVALID_CONTEXT_LIMIT"):
            limits(recent, max_bytes)
    assert limits(None, None) == (8, 32000)
    monkeypatch.setenv("CLINX_NATIVE_OBSERVER_SOCKET", "/fixture-read-only")
    thread = dict(id=TID, cwd="/approved", status=dict(type="idle"))

    @contextmanager
    def client(*a, **k):
        yield SimpleNamespace(initialize=lambda **k: None, thread_read=lambda _: thread)

    monkeypatch.setattr("clinx.provider.native_provider.existing_client", client)
    assert observe_desktop(bridge.directory, TID)["native_display_state"] == "IDLE"
    thread["cwd"] = "/different"
    with pytest.raises(NodeProtocolError, match="NATIVE_OBSERVATION_IDENTITY_CONFLICT"):
        observe_desktop(bridge.directory, TID)
    thread["cwd"] = "/approved"
    original = bridge.directory._scope
    reads = []
    bridge.directory._scope = lambda node: (
        reads.append(node) or (original(node) if len(reads) == 1 else None)
    )
    with pytest.raises(NodeProtocolError, match="SHARING_SCOPE_DENIED"):
        observe_desktop(bridge.directory, TID, context=True)
    assert snapshot(native.root) == before


def test_managed_source_capacity_projection_recovery_and_exact_error(native):
    from clinx.authority.rust_authority import RustAuthorityError
    from clinx.monitor.rust_observation import RustObservationSource

    failure = ["observation_inventory"]
    code = ["RESPONSE_CAPACITY_EXCEEDED"]
    row = dict(
        id="id-1",
        task_ref="task-owned",
        execution_ref="exec-owned",
        turn_id="turn-2",
        created_at="2026-10-25T00:00:00Z",
        status="RUNNING",
        result=None,
    )

    def command(operation, args):
        if operation == failure[0]:
            raise RustAuthorityError(code[0])
        return (
            {"tasks": []}
            if operation == "observation_inventory"
            else {"executions": [row]}
        )

    source = RustObservationSource(
        native.root, "p620", commands=SimpleNamespace(call=command)
    )
    scope = SharingScope(
        user_scope="alice", read_sessions=True, projects=("/approved",)
    )
    try:
        assert (
            source.canonical_inventory(scope) == []
            and source.managed_inventory_unavailable
        )
        events, state = source.sample(native.metadata, {})
        assert all(
            e["coverage"] == "RUST_MANAGED_PROJECTION_UNAVAILABLE" for e in events
        )
        assert state["active"] is True
        failure[0] = "observation_turns"
        source.canonical_inventory(scope)
        events, state = source.sample(native.metadata, {})
        assert state["rust_projection_unavailable"] is True
        assert all(
            e["coverage"] == "RUST_MANAGED_PROJECTION_UNAVAILABLE" for e in events
        )
        failure[0] = None
        events, state = source.sample(native.metadata, state)
        assert "rust_projection_unavailable" not in state
        match = [e for e in events if e["turn"]["turn_id"] == "turn-2"]
        assert len(match) == 1 and match[0]["turn"]["execution_ref"] == "exec-owned"
        assert match[0]["turn"]["summary"] == "Answer 2"
        failure[0] = "observation_inventory"
        code[0] = "IDENTITY_CONFLICT"
        with pytest.raises(RustAuthorityError, match="IDENTITY_CONFLICT"):
            source.canonical_inventory(SimpleNamespace(allows_project=lambda _: True))
        failure[0] = "observation_turns"
        with pytest.raises(RustAuthorityError, match="IDENTITY_CONFLICT"):
            source.sample(native.metadata, {})
    finally:
        source.close()


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


def test_historical_events_cursor_scope_capacity_and_redaction(historical):
    with sqlite3.connect(historical.path) as c:
        for n in range(105):
            c.execute(
                "INSERT INTO v2_events VALUES (?,?,?,?,?,?)",
                (
                    n + 1,
                    f"event-{n}",
                    "V1ExecutionProgressObserved",
                    "2026-10-25T01:00:00Z",
                    "2026-10-25T01:00:00Z",
                    json.dumps(
                        dict(
                            source_task_id="task-fixture",
                            source_execution_ref="exec-fixture",
                        )
                    ),
                ),
            )
        c.execute(
            "UPDATE tasks SET title=?,current_blocker=?",
            ("A" * 2000, "token=NEVER_SHOW " + "B" * 9000),
        )
        c.execute("UPDATE execution_results SET summary=?", ("X" * 9000,))
    first = historical.store.task_events("task-fixture", None)
    assert len(first["items"]) == 100 and first["has_more"]
    second = historical.store.task_events("task-fixture", first["next_cursor"])
    assert len(second["items"]) == 5 and not second["has_more"]
    assert (
        historical.store.task_events("task-fixture", second["next_cursor"])["items"]
        == []
    )
    with pytest.raises(APIError) as error:
        historical.store.task_events(
            "task-fixture", historical.store.event_cursor("other", 1)
        )
    assert error.value.status == 400
    with pytest.raises(APIError) as error:
        historical.store.task_events(
            "task-fixture", historical.store.event_cursor("task-fixture", 9999)
        )
    assert error.value.status == 409
    with historical.store.snapshot() as c:
        recent = historical.store.events(c, "task-fixture", recent=True)
    assert len(recent["items"]) == 20 and recent["has_more"]
    task = historical.store.task("task-fixture")
    assert len(task["title"]) == 512 and task["final_result"]["text_truncated"] is True
    assert "NEVER_SHOW" not in json.dumps(task)


def test_historical_native_binding_requires_current_grants(historical):
    with sqlite3.connect(historical.path) as c:
        c.execute("DELETE FROM conversation_bindings")
        c.execute(
            "CREATE TABLE native_task_bindings(task_id TEXT,node_id TEXT,thread_id TEXT,origin TEXT,first_provenance_json TEXT,user_scope TEXT,provider TEXT,project TEXT)"
        )
        c.execute(
            "INSERT INTO native_task_bindings VALUES (?,?,?,?,?,?,?,?)",
            (
                "task-fixture",
                "air",
                TID,
                "CODEX_NATIVE",
                json.dumps(dict(node_fingerprint="fingerprint")),
                "alice",
                "codex_app_server",
                "/approved",
            ),
        )
    with pytest.raises(APIError) as error:
        historical.store.task("task-fixture")
    assert error.value.status == 404
    scope = SharingScope(
        user_scope="alice", read_sessions=True, projects=("/approved",)
    )
    historical.store.observation_directory = SimpleNamespace(
        user_scope="alice",
        _scope=lambda _: scope,
        registry=SimpleNamespace(
            get=lambda _: SimpleNamespace(public_key_fingerprint="fingerprint")
        ),
    )
    assert (
        historical.store.task("task-fixture")["native_conversation"]["node_id"] == "air"
    )
    historical.store.observation_directory._scope = lambda _: None
    assert historical.store.list_tasks("recent")["items"] == []
