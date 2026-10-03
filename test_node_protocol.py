from __future__ import annotations

import dataclasses
import multiprocessing
import time

import pytest

from node_protocol import (
    NODE_PROTOCOL_VERSION,
    NodeProtocolError,
    NodeRecord,
    NodeRegistry,
    NodeRouter,
    NodeRPCClient,
    NodeRPCServer,
    NodeService,
    SessionIdentity,
    SharingScope,
    decode_cursor,
    encode_cursor,
)


def record(node_id: str, *, user: str = "user-a", route_ids: tuple[str, ...] = ()) -> NodeRecord:
    return NodeRecord(
        node_id=node_id,
        user_scope=user,
        public_key_fingerprint=f"fp-{node_id}",
        route_ids=route_ids,
        state="ONLINE",
        last_seen=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
    )


def scope(*, user: str = "user-a", execute: bool = False) -> SharingScope:
    return SharingScope(user_scope=user, read_sessions=True, execute_tasks=execute)


def test_identity_registry_and_cursor_are_scoped():
    registry = NodeRegistry(":memory:", cursor_secret="test-secret")
    node = record("air", route_ids=("air-host", "remote-air"))
    registry.register(node, scope())
    identity = SessionIdentity("air", "user-a", "codex_app_server", "thread-1", "native-v2")
    registry.update_thread(identity, source="native", status={"lookup_status": "RESOLVED"})
    assert registry.lookup_thread(native_thread_id="thread-1", user_scope="user-a")[0]["node_id"] == "air"
    cursor = encode_cursor(node_id="air", user_scope="user-a", provider="codex_app_server", native_thread_id="thread-1", source="native", source_version="native-v2", offset=4, secret="test-secret")
    assert decode_cursor(cursor, node_id="air", user_scope="user-a", provider="codex_app_server", native_thread_id="thread-1", source="native", source_version="native-v2", secret="test-secret") == 4
    with pytest.raises(NodeProtocolError) as exc:
        decode_cursor(cursor, node_id="p620", user_scope="user-a", provider="codex_app_server", native_thread_id="thread-1", source="native", source_version="native-v2", secret="test-secret")
    assert exc.value.code == "INVALID_CONTEXT_CURSOR"
    with pytest.raises(NodeProtocolError) as exc:
        registry.register(dataclasses.replace(node, node_id="imac"))
    assert exc.value.code == "DUPLICATE_NODE_IDENTITY"


def test_router_exact_and_bounded_unknown_search_with_partial_coverage():
    registry = NodeRegistry(":memory:")
    router = NodeRouter(registry, user_scope="user-a", max_workers=2, timeout_seconds=0.5)
    calls: list[str] = []

    def reader(node: str):
        def read(request):
            calls.append(node)
            if request["thread_id"] == "air-thread" and node == "air":
                return {"queried_thread_id": "air-thread", "lookup_status": "THREAD_UNBOUND", "provider_existence": "CONFIRMED"}
            return {"queried_thread_id": request["thread_id"], "lookup_status": "THREAD_NOT_FOUND", "provider_existence": "NOT_FOUND"}
        return read

    router.attach(record("p620", route_ids=("remote-p620",)), reader=reader("p620"), scope=scope())
    router.attach(record("air", route_ids=("remote-air",)), reader=reader("air"), scope=scope())
    result = router.read(thread_id="air-thread", host="remote-air")
    assert result["source_node_id"] == "air"
    assert result["coverage"]["complete"] is True
    assert calls == ["air"]

    result = router.read(thread_id="missing")
    assert result["lookup_status"] == "THREAD_NOT_FOUND"
    assert result["absence_scope"] == "AUTHORIZED_NODE_SET"
    assert sorted(result["coverage"]["nodes_queried"]) == ["air", "p620"]

    # One offline node keeps the result explicitly incomplete; it cannot be
    # collapsed into a global THREAD_NOT_FOUND.
    registry.register(dataclasses.replace(record("air"), state="OFFLINE", last_seen=None))
    result = router.read(thread_id="unknown")
    assert result["lookup_status"] == "THREAD_SEARCH_INCOMPLETE"
    assert "air" in result["coverage"]["nodes_unavailable"]


def test_router_rejects_duplicate_matches_and_revocation():
    registry = NodeRegistry(":memory:")
    router = NodeRouter(registry, user_scope="user-a")
    answer = lambda request: {"queried_thread_id": request["thread_id"], "lookup_status": "THREAD_UNBOUND", "provider_existence": "CONFIRMED"}
    router.attach(record("air"), reader=answer, scope=scope())
    router.attach(record("imac"), reader=answer, scope=scope())
    conflict = router.read(thread_id="same")
    assert conflict["error_code"] == "THREAD_IDENTITY_CONFLICT"
    registry.revoke("air", "user-a")
    denied = router.read(thread_id="same", node_id="air")
    assert denied["error_code"] == "SHARING_SCOPE_DENIED"


def test_node_service_read_has_no_execution_side_effect_and_idempotent_start():
    registry = NodeRegistry(":memory:")
    starts: list[str] = []
    service = NodeService(
        record("air"), registry, scope=scope(execute=True),
        read_thread=lambda thread_id, **kwargs: {"queried_thread_id": thread_id, "lookup_status": "THREAD_UNBOUND", "provider_existence": "CONFIRMED"},
        prepare_execution=lambda **request: {"prepared_execution_ref": "prep-1", "execution_started": False},
        start_execution=lambda **request: (starts.append(request["request_id"]) or {"execution_ref": request["execution_ref"], "operation_state": "STARTED"}),
    )
    read = service.handle({"operation": "session.read", "thread_id": "t", "user_scope": "user-a"})
    assert read["read_only"] is True
    assert starts == []
    first = service.handle({"operation": "execution.start", "thread_id": "t", "execution_ref": "e1", "request_id": "r1", "user_scope": "user-a"})
    second = service.handle({"operation": "execution.start", "thread_id": "t", "execution_ref": "e1", "request_id": "r1", "user_scope": "user-a"})
    assert first["operation_state"] == "STARTED"
    assert second["idempotent"] is True
    assert starts == ["r1"]
    denied = NodeService(record("air"), registry, scope=scope(execute=False), read_thread=lambda *_args, **_kwargs: {})
    assert denied.handle({"operation": "execution.prepare", "request_id": "x", "user_scope": "user-a"})["error_code"] == "SHARING_SCOPE_DENIED"


def _rpc_child(queue):
    registry = NodeRegistry(":memory:")
    service = NodeService(
        record("air"), registry, scope=scope(),
        read_thread=lambda thread_id, **kwargs: {"queried_thread_id": thread_id, "lookup_status": "THREAD_UNBOUND", "provider_existence": "CONFIRMED"},
    )
    server = NodeRPCServer(service, port=0, allow_insecure_loopback=True)
    queue.put(server.address)
    time.sleep(1.5)
    server.close()


def test_cross_process_rpc_route_is_real_transport():
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    process = ctx.Process(target=_rpc_child, args=(queue,))
    process.start()
    address = queue.get(timeout=5)
    try:
        result = NodeRPCClient(address, allow_insecure_loopback=True).read(thread_id="air-thread", user_scope="user-a")
        assert result["source_node_id"] == "air"
        assert result["lookup_status"] == "THREAD_UNBOUND"
    finally:
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
    assert process.exitcode == 0
