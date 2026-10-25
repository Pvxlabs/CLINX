"""Real private Provider/HTTP sockets; native Collector reads never call Python IO."""

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import pytest

from clinx.discovery.discovery import DiscoveryIndex, LanDiscovery
from clinx.monitor.observation_source import CodexObservationSource
from clinx.nodes.network_worker import Worker
from fixtures.test_rust_observation import TID, snapshot
from fixtures.test_rust_observation import native as native
from fixtures.test_rust_provider_client import Provider


def config(path, other=None):
    return {
        "app_server": {
            "local_socket": str(path),
            "native_socket": str(other) if other else None,
            "client_name": "collector-fixture",
            "client_title": "Collector fixture",
            "client_version": "1",
        }
    }


def replies(turn="exact-turn", cwd="/approved", thread=TID):
    def reply(request):
        method = request["method"]
        if method == "initialized":
            return []
        if method == "initialize":
            value = {"userAgent": "private fixture"}
        elif method == "thread/read":
            value = {"thread": {"id": thread, "status": "active", "cwd": cwd}}
        elif method == "thread/turns/list":
            value = {"data": [{"id": turn, "status": "inProgress"}], "nextCursor": None}
        else:
            raise AssertionError("Collector called mutation: " + method)
        return [{"id": request["id"], "result": value}]

    return reply


def close_source(source):
    if source._source_worker:
        source._source_worker.close()


def test_real_provider_read_cache_exact_turn_and_workspace_without_python_probe(
    tmp_path,
):
    socket = tmp_path / "private.sock"
    provider = Provider(socket, replies())
    source = CodexObservationSource(tmp_path, "fixture", provider_config=config(socket))
    try:
        with patch(
            "clinx.provider.native_provider.observe_thread",
            side_effect=AssertionError("Python probe forbidden"),
        ):
            first = source._observe_live(TID, "/approved", "exact-turn")
            assert (
                first["liveness"] == "LIVE" and first["reason"] == "EXACT_OWNER_ACTIVE"
            )
            assert source._observe_live(TID, "/approved", "exact-turn") == first
            assert [c["method"] for c in provider.calls] == [
                "initialize",
                "initialized",
                "thread/read",
                "thread/turns/list",
            ]
            provider.close()
            socket.unlink()
            provider = Provider(socket, replies())
            changed = source._observe_live(TID, "/different", "exact-turn")
            assert (
                changed["liveness"] == "UNKNOWN"
                and changed["reason"] == "WORKSPACE_MISMATCH"
            )
            assert len(provider.calls) == 4
    finally:
        close_source(source)
        provider.close()


@pytest.mark.parametrize(
    "variant,reason",
    [
        ("turn", "ACTIVE_OWNER_EXACT_TURN_UNCONFIRMED"),
        ("thread", "OWNER_UNCONFIRMED"),
        ("conflict", "OWNERSHIP_CONFLICT"),
    ],
)
def test_provider_identity_conflict_never_becomes_live(tmp_path, variant, reason):
    a = Provider(
        tmp_path / "a.sock",
        replies(
            turn="other" if variant == "turn" else "exact-turn",
            thread="other" if variant == "thread" else TID,
        ),
    )
    b = Provider(tmp_path / "b.sock", replies()) if variant == "conflict" else None
    source = CodexObservationSource(
        tmp_path, "fixture", provider_config=config(a.path, b.path if b else None)
    )
    try:
        proof = source._observe_live(TID, "/approved", "exact-turn")
        assert proof["liveness"] == "UNKNOWN" and proof["reason"] == reason
    finally:
        close_source(source)
        a.close()
        if b:
            b.close()


def test_normal_sample_merges_history_in_rust_without_source_callbacks(native):
    source = CodexObservationSource(
        native.root, "fixture", native_writer_observation=True
    )
    before = snapshot(native.root)
    source._callback = lambda *args: (_ for _ in ()).throw(
        AssertionError("Python source callback forbidden")
    )
    try:
        events, state = source.sample(native.metadata, {})
        assert events and all(e["native_thread_id"] == TID for e in events)
        assert all(e["binding_evidence"] == "THREAD_UNBOUND" for e in events)
        assert snapshot(native.root) == before
    finally:
        close_source(source)


def test_injected_provider_keeps_old_public_hook_with_rust_cache(tmp_path):
    source = CodexObservationSource(tmp_path, "fixture", provider_config=object())
    rows = [
        {
            "endpoint": "fixture",
            "state": "active",
            "thread_id": TID,
            "turn_id": "exact-turn",
            "turn_status": "inProgress",
            "cwd": "/approved",
        }
    ]
    try:
        with patch(
            "clinx.provider.native_provider.observe_thread",
            return_value={"observations": rows},
        ) as probe:
            first = source._observe_live(TID, "/approved", "exact-turn")
            assert first["liveness"] == "LIVE"
            assert source._observe_live(TID, "/approved", "exact-turn") == first
            assert probe.call_count == 1
            assert (
                source._observe_live(TID, "/approved", "different")["liveness"]
                == "UNKNOWN"
            )
            assert probe.call_count == 2
    finally:
        close_source(source)


def test_managed_remote_inventory_uses_rust_http_credential_and_scope(tmp_path):
    token = "private_collector_" + "x" * 32
    credential = tmp_path / "token"
    credential.write_text(token)
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            payload = {
                "read_only": True,
                "rows": [{"id": TID, "cwd": "/approved", "history_mode": "paginated"}],
                "after": None,
                "coverage": "INDEXED",
            }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    worker = Worker(
        callback=lambda *a: (_ for _ in ()).throw(
            AssertionError("Python callback forbidden")
        )
    )
    try:
        worker.call(
            "source_runtime.init",
            config={
                "native_home": None,
                "node_id": "fixture",
                "owner_host": "fixture",
                "managed": True,
                "remote_native": {
                    "credential_file": str(credential),
                    "port": server.server_port,
                },
            },
        )
        result = worker.call(
            "source_runtime.inventory",
            scope={
                "user_scope": "fixture",
                "read_sessions": True,
                "projects": ["/approved"],
            },
            after=None,
            audit=False,
        )
        assert result[0][0]["id"] == TID and result[2] == "INDEXED"
        assert len(seen) == 1 and seen[0][1] == "Bearer " + token
        assert seen[0][0].startswith("/v2/native-collector/inventory?")
    finally:
        worker.close()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_native_writer_probe_rejects_foreign_home_without_python_exec(tmp_path):
    binary = os.environ["CLINX_NODE_BINARY"]
    request = {
        "root": str(tmp_path / "foreign"),
        "thread_id": TID,
        "turn_id": "exact",
        "project": "/approved",
    }
    result = subprocess.run(
        [binary, "--native-writer-probe", json.dumps(request)],
        capture_output=True,
        text=True,
        check=True,
        timeout=3,
    )
    proof = json.loads(result.stdout)
    assert proof["liveness"] == "UNKNOWN" and "writer_pid" not in proof
    assert proof["observer_uid"] == os.getuid()


def test_rust_mdns_offline_lifecycle_never_imports_python_zeroconf():
    index = DiscoveryIndex()
    with patch("builtins.__import__", wraps=__import__) as imports:
        with LanDiscovery(index, interfaces=[]) as browser:
            assert browser.interfaces == [] and browser.zc is None
            assert index.candidates() == {}
    assert not any(c.args[0] in {"zeroconf", "ifaddr"} for c in imports.call_args_list)
