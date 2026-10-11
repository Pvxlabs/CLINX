"""Idle local reads and retained revoked routes through the real MCP boundary."""
import dataclasses
import sqlite3
from unittest.mock import Mock, patch

import pytest

from mcp_server import ClinxMCPServer, build_server
from node_protocol import NodeProtocolError, NodeRegistry, NodeRouter
from test_native_interop import native as native, hashes, TID, URI
from test_node_protocol import record, scope


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("revoked", [False, True])
def test_node_status_separates_cache_connection_and_task_authority(native, stale, revoked):
    import jsonschema
    from mcp_server import tool_definitions

    registry = NodeRegistry()
    router = NodeRouter(registry, user_scope="user-a", local_node_id="p620")
    callback = Mock(side_effect=AssertionError("node status must not read a thread or start a provider"))
    node = expired(record("p620")) if stale else record("p620")
    router.attach(node, reader=callback, scope=scope())
    if revoked:
        registry.revoke("p620", "user-a")
    native.integration.cfg = dataclasses.replace(native.cfg, node_router=router)
    before = hashes(native)
    cached_before = registry.get("p620").as_dict()
    server = ClinxMCPServer(native.integration)
    result = server.handle({"id": 1, "method": "tools/call", "params": {
        "name": "clinx_get_node_status", "arguments": {"node_id": "p620"}}})["result"]
    assert not result["isError"]
    status = result["structuredContent"]
    schema = next(t["outputSchema"] for t in tool_definitions(include_nodes=True)
                  if t["name"] == "clinx_get_node_status")
    jsonschema.validate(status, schema)
    assert status["cache_status"]["stale"] is stale
    assert status["cache_status"]["node_reachable"] is (not stale and not revoked)
    assert status["connection_status"]["source"] == "NOT_PROBED"
    assert status["connection_status"]["state"] == "UNKNOWN"
    assert status["node_reachable"] is status["provider_reachable"] is None
    assert status["execution_active"] is None
    assert status["task_authority"]["task_authorized"] is None
    assert status["task_authority"]["source"] == "NO_TASK_SELECTED"
    assert status["scope"]["execute_tasks"] is False
    assert status["history_readable"] is (not revoked)
    callback.assert_not_called()
    assert registry.get("p620").last_seen == cached_before["last_seen"]
    assert hashes(native) == before


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


@pytest.fixture
def local_mcp(native, monkeypatch):
    cfg = dataclasses.replace(native.cfg, app_server=dataclasses.replace(
        native.cfg.app_server, native_home=str(native.root)))
    monkeypatch.setenv("CLINX_ENABLE_NODE_ROUTER", "1")
    monkeypatch.setenv("CLINX_NODE_ID", "p620")
    monkeypatch.setenv("CLINX_USER_SCOPE", "user-a")
    monkeypatch.setenv("LINEAR_API_KEY", "fixture-no-network")
    monkeypatch.delenv("CLINX_NODE_CENTRE_STATE", raising=False)
    monkeypatch.delenv("CLINX_NODE_STATE", raising=False)
    with patch("bridge.BridgeConfig.load", return_value=cfg), \
            patch("network_observation.directory_from_env", return_value=None), \
            patch("native_provider.observe_thread", return_value={
                "state": "active", "owner_endpoint": "fixture-owner", "observations": []}):
        yield build_server("fixture.toml", task_db_path=native.registry.path)


def mcp_call(server, tool, **args):
    return server.handle({"id": 1, "method": "tools/call", "params": {
        "name": tool, "arguments": args}})["result"]["structuredContent"]


@pytest.mark.parametrize("idle", [False, True])
def test_built_mcp_scoped_native_reads_preserve_identity_and_storage(local_mcp, native, idle):
    router = local_mcp.integration.cfg.node_router
    if idle:
        router.registry.register(expired(router.registry.get("p620")))
    before = hashes(native)
    for tool in ("clinx_get_status", "clinx_get_context"):
        result = mcp_call(local_mcp, tool, codex_uri=URI, project="pilot")
        assert result.get("lookup_status") == "THREAD_UNBOUND", result
        assert result["queried_thread_id"] == TID
        assert result["node_id"] == "p620" and result["read_only"] is True
        assert result["provider_observation"]["state"] == "active"
    assert not router.registry.get("p620").stale
    denied = mcp_call(local_mcp, "clinx_get_status", codex_uri=URI, project="wrong")
    assert denied["error_code"] == "THREAD_SCOPE_MISMATCH"
    denied = mcp_call(local_mcp, "clinx_get_status", codex_uri=URI, execution_ref="exec_wrong")
    assert denied["error_code"] == "THREAD_SCOPE_MISMATCH"
    assert hashes(native) == before


def test_built_mcp_project_adoption_after_idle_keeps_active_owner(local_mcp, native):
    router = local_mcp.integration.cfg.node_router
    router.registry.register(expired(router.registry.get("p620")))
    first = mcp_call(local_mcp, "clinx_adopt_conversation", codex_uri=URI, project="pilot")
    assert first["adoption_status"] == "ADOPTED", first
    second = mcp_call(local_mcp, "clinx_adopt_conversation", codex_uri=URI, project="pilot")
    assert second["adoption_status"] == "ALREADY_ADOPTED"
    assert second["task_ref"] == first["task_ref"]
    assert not first["control_transferred"] and not first["execution_started"]
    assert first["provider_observation"]["state"] == "active"
    with sqlite3.connect(native.registry.path) as conn:
        for table in ("executions", "execution_history", "worktree_leases", "context_checkpoints"):
            assert conn.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0


def test_local_scoped_read_failure_does_not_renew_liveness():
    registry = NodeRegistry()
    router = NodeRouter(registry, user_scope="user-a", local_node_id="p620")
    node = expired(record("p620"))
    callback = Mock(side_effect=OSError("native storage unavailable"))
    router.attach(node, reader=answer, executor=callback, scope=scope())
    result = router.execute(node_id="p620", operation="execution.context", request={"thread_id": TID})
    assert result["error_code"] == "NODE_READ_UNAVAILABLE"
    callback.assert_called_once()
    assert registry.get("p620").last_seen == node.last_seen


def test_local_scoped_read_error_response_does_not_renew_liveness():
    registry = NodeRegistry()
    router = NodeRouter(registry, user_scope="user-a", local_node_id="p620")
    node = expired(record("p620"))
    callback = Mock(return_value={"error_code": "THREAD_SCOPE_MISMATCH"})
    router.attach(node, reader=answer, executor=callback, scope=scope())
    result = router.execute(node_id="p620", operation="execution.context", request={"thread_id": TID})
    assert result["error_code"] == "THREAD_SCOPE_MISMATCH"
    callback.assert_called_once()
    assert registry.get("p620").last_seen == node.last_seen


def test_local_scoped_read_recovery_requires_authorized_scope(local_mcp):
    router = local_mcp.integration.cfg.node_router
    router.registry.revoke("p620", "user-a")
    result = mcp_call(local_mcp, "clinx_get_status", thread_id=TID, node_id="p620", project="pilot")
    assert result["error_code"] == "SHARING_SCOPE_DENIED"


def test_local_scoped_read_recovery_keeps_remote_and_write_fences():
    registry = NodeRegistry()
    router = NodeRouter(registry, user_scope="user-a", local_node_id="p620")
    callback = Mock(return_value={"lookup_status": "THREAD_UNBOUND"})
    router.attach(expired(record("p620")), reader=answer, executor=callback, scope=scope(execute=True))
    assert router.execute(node_id="p620", operation="execution.start", request={})["error_code"] == "NODE_OFFLINE"
    callback.assert_not_called()
    client = Mock()
    router.attach_remote(expired(record("p620")), client, scope=scope())
    assert router.execute(node_id="p620", operation="execution.context", request={})["error_code"] == "NODE_OFFLINE"
    client.execute.assert_not_called()
