"""Python facade and real package oracles retained after native qualification.

Rust router/worker coverage: kernel/tests/node_router_qualification.rs.
"""
import base64
import os
import shutil
import socket
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from types import SimpleNamespace
from pathlib import Path

import pytest
from fixtures.test_node_authority_route import route as route
from clinx.nodes.node_protocol import (
    NodeProtocolError, NodeRecord, NodeRegistry, NodeRouter, NodeService, SharingScope,
    _decode_node_cursor, _encode_provider_cursor, _now, encode_cursor,
)


def test_cursor_python_exception_type_and_reason():
    binding = dict(node_id="air", user_scope="alice", provider="codex_app_server",
        native_thread_id="线程", source="native", source_version="1", secret=b"fixture-key")
    for token in (encode_cursor(offset=4, **binding),
                  _encode_provider_cursor(provider_cursor="opaque-提供者-token", **binding)):
        with pytest.raises(NodeProtocolError, match="another node"):
            _decode_node_cursor(token, **dict(binding, node_id="imac"))
        with pytest.raises(NodeProtocolError):
            _decode_node_cursor(token, **dict(binding, secret=b"wrong-key"))
        body, signature = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).rsplit(b".", 1)
        uppercase = base64.urlsafe_b64encode(body + b"." + signature.upper()).decode().rstrip("=")
        with pytest.raises(NodeProtocolError):
            _decode_node_cursor(uppercase, **binding)

def record(node, **kwargs):
    return NodeRecord(
        node, user_scope="alice", state="ONLINE", last_seen=_now(), **kwargs
    )


def scope(execute=False):
    return SharingScope("alice", read_sessions=True, execute_tasks=execute)


def found(request):
    return dict(
        queried_thread_id=request["thread_id"],
        lookup_status="THREAD_UNBOUND",
        provider_existence="CONFIRMED",
    )


@contextmanager
def router(tmp_path, **kwargs):
    registry = NodeRegistry(tmp_path / "nodes.db")
    try:
        yield NodeRouter(registry, user_scope="alice", **kwargs)
    finally:
        registry.close()


def test_read_identity_validation_precedes_provider_io(tmp_path):
    with router(tmp_path) as r:
        calls = []
        service = NodeService(
            record("air"),
            r.registry,
            scope=scope(),
            read_thread=lambda thread, **kwargs: (
                calls.append((thread, kwargs))
                or {"next_cursor": "page2", "lookup_status": "THREAD_UNBOUND"}
            ),
        )
        request = dict(operation="session.read", user_scope="alice", thread_id=" t ")
        for version in [None, "", "v\n1", 4, "x" * 513]:
            result = service.handle(dict(request, source_version=version))
            assert result["error_code"] == "ValueError"
        assert not calls
        result = service.handle(dict(request, source_version=" v1 "))
        assert calls == [("t", {})]
        assert (
            _decode_node_cursor(
                result["next_cursor"],
                node_id="air",
                user_scope="alice",
                provider="codex_app_server",
                native_thread_id="t",
                source="native",
                source_version="v1",
                secret=r.registry.cursor_secret,
            )
            == "page2"
        )


def test_cached_remote_request_rechecks_bilateral_scope_and_io_uncertainty(tmp_path):
    from clinx.nodes.node_runtime import AuthorizedRemoteClient

    with router(tmp_path) as r:
        r.attach(
            record("air", endpoint="tls://fixture:1"), reader=found, scope=scope(True)
        )
        granted = [scope(True)]
        sent = []
        responses = [{"read_only": True}]

        def exchange(value):
            sent.append(value)
            result = responses[0]
            if isinstance(result, Exception):
                raise result
            return result

        client = AuthorizedRemoteClient(
            SimpleNamespace(
                peer=lambda node: {},
                client=lambda *a: SimpleNamespace(call=exchange),
            ),
            r.registry,
            SimpleNamespace(get=lambda node: granted[0]),
            "air",
        )
        request = dict(operation="execution.context", user_scope="alice", thread_id="t")
        assert client.call(request)["read_only"]
        assert sent[0]["node_id"] == "air"
        granted[0] = SharingScope("alice", revoked=True)
        assert client.call(request)["error_code"] == "SHARING_SCOPE_DENIED"
        assert len(sent) == 1
        granted[0] = scope(True)
        assert (
            client.call(dict(request, user_scope="other"))["error_code"]
            == "USER_SCOPE_DENIED"
        )
        assert len(sent) == 1
        responses[0] = TimeoutError("ACK lost")
        with pytest.raises(TimeoutError, match="ACK lost"):
            client.call(dict(request, operation="execution.start"))
        assert len(sent) == 2


def test_packaged_helper_registers_reads_and_restarts_without_business_execution(
    route, tmp_path
):
    from clinx.nodes.node_protocol import NodeAuthorizationStore
    from clinx.nodes.node_runtime import AuthorizedRemoteClient
    from tools.build.runtime_sources import copy_native_monitor_resources

    resources = tmp_path / "App/Contents/Resources"
    copy_native_monitor_resources(Path(__file__).resolve().parents[1], resources)
    assert not list(resources.rglob("*.py"))
    assert not (resources / "DiscoveryRuntime").exists()
    binary = resources.parent / "Helpers/clinx-node"
    binary.parent.mkdir()
    shutil.copyfile(Path(os.environ["CLINX_TEST_RUST_BIN_DIR"]) / "clinx-node", binary)
    binary.chmod(0o755)
    daemon = binary.with_name("clinx-node-daemon")
    shutil.copyfile(Path(os.environ["CLINX_TEST_RUST_BIN_DIR"]) / daemon.name, daemon)
    daemon.chmod(0o755)
    provider = binary.with_name("clinx-provider")
    shutil.copyfile(Path(os.environ["CLINX_TEST_RUST_BIN_DIR"]) / provider.name, provider)
    provider.chmod(0o755)
    launcher = resources / "CLINXNodeService"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "MonitorApp/Scripts/CLINXNodeService", launcher)
    launcher.chmod(0o755)
    NodeAuthorizationStore(route.node.store.root).grant(
        "centre", user_scope="alice", read_sessions=True
    )
    route.approvals.grant("air", user_scope="alice", read_sessions=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    route.node.store.write(
        "node-config.json",
        dict(
            schema_version=1,
            centre_id="centre",
            centre_endpoint=route.registration.endpoint,
            bind_address="127.0.0.1",
            port=port,
        ),
    )
    native = tmp_path / "native-home"
    native.mkdir()
    with sqlite3.connect(native / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads (id TEXT, cwd TEXT, rollout_path TEXT)")
    env = dict(
        os.environ,
        CLINX_NODE_STATE=str(route.node.store.root),
        CLINX_NODE_HEALTH_STATE=str(tmp_path / "health"),
        CLINX_NATIVE_HOME=str(native),
    )
    env.pop("CLINX_NODE_BINARY", None)
    client = AuthorizedRemoteClient(
        route.service.tls, route.service.registry, route.approvals, "air"
    )
    for generation in range(2):
        with (tmp_path / f"helper-{generation}.log").open("wb") as log:
            process = subprocess.Popen(
                [
                    str(launcher),
                ],
                cwd=resources,
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 8
                result = {}
                while time.monotonic() < deadline:
                    assert process.poll() is None, (
                        tmp_path / f"helper-{generation}.log"
                    ).read_text()
                    registered = route.service.registry.get("air")
                    if registered and registered.endpoint == f"tls://127.0.0.1:{port}":
                        try:
                            result = client.call(
                                dict(
                                    operation="session.read",
                                    user_scope="alice",
                                    thread_id="absent",
                                )
                            )
                        except OSError:
                            pass
                        if result.get("lookup_status") == "THREAD_NOT_FOUND":
                            break
                    time.sleep(0.05)
                assert result.get("lookup_status") == "THREAD_NOT_FOUND", result
                assert (
                    result["provider_existence"] == "NOT_FOUND" and result["read_only"]
                )
                assert not route.calls
            finally:
                process.terminate()
                process.wait(timeout=8)
            assert process.returncode == 0
