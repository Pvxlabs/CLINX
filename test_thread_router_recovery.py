"""Idle local reads and retained revoked routes through the real MCP boundary."""
import dataclasses
from unittest.mock import Mock

import pytest

from mcp_server import ClinxMCPServer
from node_protocol import NodeProtocolError, NodeRegistry, NodeRouter
from test_native_interop import native, hashes, TID, URI
from test_node_protocol import record, scope


def expired(node):
    return dataclasses.replace(node, state="OFFLINE", last_seen="2000-01-01T00:00:00+00:00")


def answer(request):
    return {"queried_thread_id": request["thread_id"], "lookup_status": "THREAD_UNBOUND",
            "provider_existence": "CONFIRMED"}


def test_retired_alias_keeps_evidence_without_blocking_authorized_host():
    registry = NodeRegistry()
    router = NodeRouter(registry, user_scope="user-a", local_node_id="p620")
    router.attach(record("p620"), reader=answer, scope=scope())
    registry.register(record("p620-smoke", route_ids=("p620",)), scope())
    with pytest.raises(NodeProtocolError, match="multiple nodes"):
        router.read(thread_id=TID, host="p620")
    registry.revoke("p620-smoke", "user-a")
    assert router.read(thread_id=TID, host="p620")["source_node_id"] == "p620"
    assert registry.get("p620-smoke").route_ids == ("p620",)
    assert router.read(thread_id=TID, node_id="p620-smoke")["error_code"] == "SHARING_SCOPE_DENIED"


@pytest.mark.parametrize("selector", [{"host": "p620"}, {}])
def test_real_local_reader_recovers_after_idle(selector):
    registry = NodeRegistry()
    router = NodeRouter(registry, user_scope="user-a", local_node_id="p620")
    callback = Mock(side_effect=answer)
    router.attach(expired(record("p620")), reader=callback, scope=scope())
    result = router.read(thread_id=TID, **selector)
    assert result["source_node_id"] == "p620"
    callback.assert_called_once()
    assert not registry.get("p620").stale


def test_failed_local_read_does_not_manufacture_liveness():
    registry = NodeRegistry()
    router = NodeRouter(registry, user_scope="user-a", local_node_id="p620")
    node = expired(record("p620"))
    router.attach(node, reader=Mock(side_effect=OSError("native storage unavailable")), scope=scope())
    assert router.read(thread_id=TID, host="p620")["error_code"] == "NODE_UNAVAILABLE"
    assert registry.get("p620").last_seen == node.last_seen
    assert registry.get("p620").state == "OFFLINE"


def test_stale_remote_is_still_offline_and_search_coverage_is_incomplete():
    registry = NodeRegistry()
    router = NodeRouter(registry, user_scope="user-a", local_node_id="p620")
    router.attach(expired(record("p620")), reader=answer, scope=scope())
    callback = Mock(side_effect=answer)
    router.attach(expired(record("air")), reader=callback, scope=scope())
    assert router.read(thread_id=TID, host="air")["error_code"] == "NODE_OFFLINE"
    result = router.read(thread_id=TID)
    assert result["source_node_id"] == "p620"
    assert result["coverage"]["complete"] is False
    assert result["coverage"]["nodes_unavailable"] == ["air"]
    callback.assert_not_called()
    assert registry.get("air").stale


def test_remote_transport_with_local_name_cannot_skip_freshness():
    registry = NodeRegistry()
    router = NodeRouter(registry, user_scope="user-a", local_node_id="p620")
    client = Mock()
    router.attach_remote(expired(record("p620")), client, scope=scope())
    assert router.read(thread_id=TID, host="p620")["error_code"] == "NODE_OFFLINE"
    client.call.assert_not_called()


def test_mcp_exact_uri_reads_idle_local_node_after_retiring_smoke(native):
    registry = NodeRegistry()
    router = NodeRouter(registry, user_scope="user-a", local_node_id="p620")
    router.attach(expired(record("p620")), scope=scope(), reader=lambda req:
                  native.reader.read(context=True, thread_id=req["thread_id"]))
    registry.register(expired(record("p620-smoke", route_ids=("p620",))), scope())
    native.integration.cfg = dataclasses.replace(native.cfg, node_router=router)
    server = ClinxMCPServer(native.integration)
    before = hashes(native)
    args = {"codex_uri": URI, "host": "p620"}
    def call(tool):
        return server.handle({"id": 1, "method": "tools/call", "params": {
            "name": tool, "arguments": args}})["result"]["structuredContent"]

    blocked = call("clinx_get_status")
    assert blocked["error_code"] == "THREAD_HOST_CONFLICT"
    registry.revoke("p620-smoke", "user-a")
    for tool in ("clinx_get_status", "clinx_get_context"):
        result = call(tool)
        assert result["queried_thread_id"] == TID
        assert result["source_node_id"] == "p620"
        assert result["read_only"] is True
        assert not result.get("error_code")
    assert hashes(native) == before
