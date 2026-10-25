"""Private Node routing/cursor regressions; all registries and callbacks are fixtures."""

import base64
import dataclasses
import hashlib
import hmac
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from pathlib import Path

import pytest
from fixtures.test_node_authority_route import route as route
from clinx.nodes.node_protocol import (
    NodeProtocolError,
    NodeRecord,
    NodeRegistry,
    NodeRouter,
    NodeService,
    SharingScope,
    _decode_node_cursor,
    _encode_provider_cursor,
    _now,
    decode_cursor,
    encode_cursor,
)


def record(node, **kwargs):
    return NodeRecord(
        node, user_scope="alice", state="ONLINE", last_seen=_now(), **kwargs
    )


def scope(execute=False):
    return SharingScope("alice", read_sessions=True, execute_tasks=execute)


def absent(request):
    return dict(
        queried_thread_id=request["thread_id"],
        lookup_status="THREAD_NOT_FOUND",
        provider_existence="NOT_FOUND",
    )


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


def test_deployed_cursor_bytes_roundtrip_and_cross_identity_rejection():
    binding = dict(
        node_id="air",
        user_scope="alice",
        provider="codex_app_server",
        native_thread_id="线程",
        source="native",
        source_version="1",
        secret=b"fixture-key",
    )
    for version, payload in [(1, 4), (2, "opaque-提供者-token")]:
        body = json.dumps(
            [
                version,
                "air",
                "alice",
                "codex_app_server",
                "线程",
                "native",
                "1",
                payload,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        raw = (
            body
            + b"."
            + hmac.new(b"fixture-key", body, hashlib.sha256).hexdigest().encode()
        )
        token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        if version == 1:
            assert encode_cursor(offset=payload, **binding) == token
            assert decode_cursor(token, **binding) == payload
            assert _decode_node_cursor(token, **binding) is None
        else:
            assert _encode_provider_cursor(provider_cursor=payload, **binding) == token
            assert _decode_node_cursor(token, **binding) == payload
        with pytest.raises(NodeProtocolError, match="another node"):
            _decode_node_cursor(token, **dict(binding, node_id="imac"))
        with pytest.raises(NodeProtocolError):
            _decode_node_cursor(token, **dict(binding, secret=b"wrong-key"))
        uppercase = (
            base64.urlsafe_b64encode(body + b"." + raw.rsplit(b".", 1)[1].upper())
            .decode()
            .rstrip("=")
        )
        with pytest.raises(NodeProtocolError):
            _decode_node_cursor(uppercase, **binding)


def test_exact_host_route_casefold_and_alias_conflict(tmp_path):
    with router(tmp_path) as r:
        calls = []
        r.attach(
            record("air", route_ids=("Straße",)),
            reader=lambda q: calls.append("air") or found(q),
            scope=scope(),
        )
        assert r.read(thread_id="t", host="STRASSE")["source_node_id"] == "air"
        assert calls == ["air"]
        r.attach(record("imac", route_ids=("strasse",)), reader=found, scope=scope())
        with pytest.raises(NodeProtocolError) as error:
            r.read(thread_id="t", host="STRASSE")
        assert error.value.code == "THREAD_HOST_CONFLICT"


def test_authorized_search_requires_exact_evidence_and_reports_conflicts(tmp_path):
    with router(tmp_path, max_workers=2) as r:
        r.attach(record("air"), reader=found, scope=scope())
        r.attach(
            record("imac"),
            reader=lambda q: dict(found(q), queried_thread_id="another"),
            scope=scope(),
        )
        result = r.read(thread_id="t")
        assert result["source_node_id"] == "air" and not result["coverage"]["complete"]
        assert result["coverage"]["nodes_unavailable"] == ["imac"]
        r._readers["imac"] = found
        result = r.read(thread_id="t")
        assert result["error_code"] == "THREAD_IDENTITY_CONFLICT" and result[
            "matching_nodes"
        ] == ["air", "imac"]
        r.registry.revoke("air", "alice")
        assert (
            r.read(thread_id="t", node_id="air")["error_code"] == "SHARING_SCOPE_DENIED"
        )


def test_only_complete_exact_negatives_prove_absence(tmp_path):
    with router(tmp_path) as r:
        r.attach(record("air"), reader=absent, scope=scope())
        r.attach(record("imac"), reader=lambda q: {}, scope=scope())
        assert r.read(thread_id="missing")["error_code"] == "THREAD_SEARCH_INCOMPLETE"
        r._readers["imac"] = absent
        result = r.read(thread_id="missing")
        assert (
            result["error_code"] == "THREAD_NOT_FOUND"
            and result["absence_scope"] == "AUTHORIZED_NODE_SET"
        )
        assert result["coverage"]["nodes_queried"] == ["air", "imac"]


def test_fanout_is_bounded_and_does_not_start_queued_reads_after_timeout(tmp_path):
    with router(tmp_path, max_workers=2, timeout_seconds=0.05) as r:
        active = 0
        maximum = 0
        seen = []
        lock = threading.Lock()

        def read(q):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(active, maximum)
                seen.append(q["thread_id"])
            time.sleep(0.15)
            with lock:
                active -= 1
            return absent(q)

        for i in range(8):
            r.attach(record(f"node-{i}"), reader=read, scope=scope())
        result = r.read(thread_id="t")
        assert maximum == 2 and len(seen) == 2
        assert result["error_code"] == "THREAD_SEARCH_INCOMPLETE"
        assert result["coverage"]["nodes_queried"] == []
        assert len(result["coverage"]["nodes_unavailable"]) == 8


def test_idle_local_read_recovers_but_write_remains_offline(tmp_path):
    with router(tmp_path, local_node_id="p620") as r:
        node = dataclasses.replace(record("p620"), last_seen=None, state="OFFLINE")
        calls = []
        r.attach(
            node,
            reader=found,
            executor=lambda op, q: calls.append(op) or {"read_only": True},
            scope=scope(True),
        )
        # Context reads can establish local liveness; writes have no exception.
        assert (
            r.execute(node_id="p620", operation="execution.start", request={})[
                "error_code"
            ]
            == "NODE_OFFLINE"
        )
        assert not calls
        assert r.execute(node_id="p620", operation="execution.context", request={})[
            "read_only"
        ]
        assert calls == ["execution.context"]
        assert r.registry.get("p620").state == "ONLINE"


def test_unknown_execution_is_not_retried_or_bound(tmp_path):
    with router(tmp_path) as r:
        calls = []

        def execute(operation, request):
            calls.append((operation, request))
            raise TimeoutError("ACK lost")

        r.attach(record("air"), reader=found, executor=execute, scope=scope(True))
        result = r.execute(
            node_id="air",
            operation="execution.start",
            request={"execution_ref": "e", "request_id": "one"},
        )
        assert (
            result["operation_state"] == "UNKNOWN"
            and result["retry"] == "RECONCILIATION_REQUIRED"
        )
        assert len(calls) == 1 and r.registry.reference_node("e", "alice") is None


def test_canonical_ack_conflict_is_not_published_as_success(tmp_path):
    with router(tmp_path) as r:
        r.attach(
            record("air"),
            reader=found,
            executor=lambda *a: {"task_ref": "wrong", "execution_ref": "exec"},
            scope=scope(True),
        )
        result = r.execute(
            node_id="air",
            operation="execution.adopt",
            request={"canonical_identity": {"task_id": "exact"}},
        )
        assert result["error_code"] == "CANONICAL_HANDOFF_ACK_CONFLICT"
        assert (
            result["side_effect"] == "UNKNOWN"
            and r.registry.reference_node("exec", "alice") is None
        )
        r._executors["air"] = lambda *a: {"task_ref": "exact", "execution_ref": "exec"}
        assert (
            r.execute(
                node_id="air",
                operation="execution.adopt",
                request={"canonical_identity": {"task_id": "exact"}},
            )["task_ref"]
            == "exact"
        )
        assert r.registry.reference_node("exec", "alice") == "air"


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


def test_service_scope_denial_cannot_call_provider(tmp_path):
    with router(tmp_path) as r:
        calls = []
        service = NodeService(
            record("air"),
            r.registry,
            scope=scope(),
            read_thread=lambda *args, **kwargs: calls.append("read") or {},
            status_thread=lambda *args, **kwargs: calls.append("status") or {},
        )
        for operation in ["session.read", "session.status"]:
            result = service.handle(
                dict(operation=operation, user_scope="other", thread_id="t")
            )
            assert result["error_code"] == "USER_SCOPE_DENIED"
        assert not calls


def test_reverse_authority_ack_identity_frame_limit_and_no_replay():
    from clinx.authority.rust_authority import RustAuthorityError
    from clinx.nodes.node_runtime import NodeAuthorityClient

    sent = []
    responses = [dict(request_id="one", result={"task_ref": "t"})]

    def request(value):
        sent.append(value)
        result = responses[0]
        if isinstance(result, Exception):
            raise result
        return result

    tls = SimpleNamespace(
        identity=SimpleNamespace(public={"node_id": "air"}),
        client=lambda *a, **kw: SimpleNamespace(call=request),
    )
    registration = SimpleNamespace(
        tls=tls, centre_id="p620", endpoint="tls://fixture:1", timeout=1
    )
    client = NodeAuthorityClient(registration, scope(True))
    assert client.request(
        "business.prepare", {"prompt": "exact"}, request_id="one"
    ) == {"task_ref": "t"}
    assert sent[0]["command"] == "prepare" and sent[0]["node_id"] == "air"
    responses[0] = dict(request_id="another", result={"task_ref": "t"})
    with pytest.raises(RustAuthorityError, match="DELIVERY_UNKNOWN"):
        client.request("business.prepare", {}, request_id="one")
    responses[0] = TimeoutError("lost ACK")
    with pytest.raises(RustAuthorityError, match="DELIVERY_UNKNOWN"):
        client.request("business.prepare", {}, request_id="one")
    assert len(sent) == 3
    with pytest.raises(RustAuthorityError, match="REQUEST_TOO_LARGE"):
        client.request(
            "business.prepare", {"prompt": "线程" * 23000}, request_id="large"
        )
    with pytest.raises(RustAuthorityError, match="UNSUPPORTED_OPERATION"):
        client.request("arbitrary.start", {}, request_id="denied")
    client.scope = scope(False)
    with pytest.raises(RustAuthorityError, match="SHARING_SCOPE_DENIED"):
        client.request("business.prepare", {}, request_id="denied")
    assert len(sent) == 3


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


def test_helper_native_read_uses_exact_rust_metadata_and_project_scope(tmp_path):
    from clinx.nodes.network_worker import Worker

    native = tmp_path / "native"
    native.mkdir()
    with sqlite3.connect(native / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads (id TEXT, cwd TEXT, rollout_path TEXT)")
        db.execute("INSERT INTO threads VALUES ('t', '/project', '')")
    calls = []
    current = [SharingScope("alice", read_sessions=True, projects=("/other",))]

    def callback(event):
        calls.append(event["operation"])
        if event["operation"] == "helper.scopes":
            return dict(local=current[0].as_dict(), active=current[0].as_dict())
        assert event["operation"] == "helper.context"
        return dict(exact=True, **event["arguments"])

    worker = Worker(callback=callback)
    arguments = dict(native_home=str(native), node_id="air", kwargs={}, execution=True)
    try:
        missing = worker.call("router.helper_read", thread_id="missing", **arguments)
        assert missing["provider_existence"] == "NOT_FOUND" and not calls
        denied = worker.call("router.helper_read", thread_id="t", **arguments)
        assert denied["error_code"] == "PROJECT_SCOPE_DENIED"
        assert calls == ["helper.scopes"]
        current[0] = SharingScope("alice", read_sessions=True, projects=("/project",))
        result = worker.call("router.helper_read", thread_id="t", **arguments)
        assert result == dict(exact=True, host="air", thread_id="t")
        assert calls == ["helper.scopes", "helper.scopes", "helper.context"]
        failure = worker.call(
            "router.helper_read",
            thread_id="t",
            **dict(arguments, native_home=str(tmp_path / "absent")),
        )
        assert failure["error_code"] == "NATIVE_INDEX_UNAVAILABLE"
        assert failure["provider_existence"] == "UNKNOWN"
    finally:
        worker.close()


def test_helper_supervisor_registers_once_schedules_and_fails_closed_on_restart():
    from clinx.nodes.network_worker import Worker

    calls = []
    reply = [dict(registered=True, scope=scope(True).as_dict(), heartbeat_seconds=3)]

    def callback(event):
        op, args = event["operation"], event["arguments"]
        calls.append((op, args))
        if op == "helper.approval":
            result = scope(True).as_dict()
        elif op == "helper.register":
            if isinstance(reply[0], Exception):
                return dict(error_type=type(reply[0]).__name__)
            result = reply[0]
        elif op == "helper.collect":
            result = dict(delay=30, status="INDEXED", sampled=1)
        else:
            result = None
        return dict(result=result)

    args = dict(
        centre_id="p620", node_id="air", endpoint="tls://fixture:1", capabilities=[]
    )
    worker = Worker(callback=callback)
    try:
        assert worker.call("router.helper_tick", **args)["active"]
        first_count = len(calls)
        assert worker.call("router.helper_tick", **args)["active"]
        assert len(calls) == first_count
        assert [a["heartbeat"] for op, a in calls if op == "helper.register"] == [False]
        assert [a for op, a in calls if op == "helper.state"] == [
            dict(state="ONLINE", last_seen=None)
        ]
    finally:
        worker.close()

    reply[0] = TimeoutError("lost registration ACK")
    calls.clear()
    worker = Worker(callback=callback)
    try:
        assert not worker.call("router.helper_tick", **args)["active"]
        assert [a["heartbeat"] for op, a in calls if op == "helper.register"] == [False]
        assert [a for op, a in calls if op == "helper.active"] == [None]
        assert [a for op, a in calls if op == "helper.state"] == [dict(state="OFFLINE")]
        count = len(calls)
        worker.call("router.helper_tick", **args)
        assert len(calls) == count
    finally:
        worker.close()


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
