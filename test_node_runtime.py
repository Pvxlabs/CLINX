from __future__ import annotations

import time

from local_discovery.discovery import Candidate
from local_discovery.identity import PrivateStore, NodeIdentity, TrustedPeerStore
from local_discovery.transport import DeviceServer, LanTransport
from node_protocol import NodeAuthorizationStore, NodeRecord, NodeRegistry, NodeRPCServer, NodeService, SharingScope, NodeRouter
from node_runtime import CentreService, NodeRegistrationClient, PairedTLS, node_request_handler, remote_client_factory


def paired(tmp_path):
    node = NodeIdentity(PrivateStore(tmp_path / "air"), "air")
    centre = NodeIdentity(PrivateStore(tmp_path / "p620"), "p620")
    node_peers = TrustedPeerStore(node.store)
    centre_peers = TrustedPeerStore(centre.store)
    with DeviceServer(centre, centre_peers, address="127.0.0.1") as pairing:
        candidate = Candidate(centre.public["node_id"], centre.public["display_name"], centre.public["fingerprint"], (("127.0.0.1", pairing.port),))
        LanTransport(node, node_peers).pair(candidate, pairing.window.open())
    # Pair in the other direction is the same production pair: the server's
    # peer store already contains the node after the commit.
    assert centre_peers.all()["air"]["security_status"] == "OPAQUE_V1"
    return node, node_peers, centre, centre_peers


def test_authenticated_registration_remote_read_heartbeat_and_revoke(tmp_path):
    node, node_peers, centre, centre_peers = paired(tmp_path)
    node_scope = NodeAuthorizationStore(node.store.root)
    centre_scope = NodeAuthorizationStore(centre.store.root)
    node_scope.grant("p620", user_scope="tinzleung", read_sessions=True)
    centre_scope.grant("air", user_scope="tinzleung", read_sessions=True)

    centre_registry = NodeRegistry(tmp_path / "centre.sqlite3")
    centre_service = CentreService(PairedTLS(centre, centre_peers), centre_registry, centre_scope, stale_after=1)
    centre_server = centre_service.server(address="127.0.0.1", port=0)
    node_registry = NodeRegistry(":memory:")
    node_record = NodeRecord(
        node_id="air", user_scope="tinzleung", public_key_fingerprint=node.public["fingerprint"],
        display_name="air", state="UNKNOWN", last_seen=None,
    )
    active = [None]
    calls = []
    node_service = NodeService(
        node_record, node_registry, scope=node_scope.get("p620"),
        read_thread=lambda thread_id, **kwargs: calls.append(thread_id) or {
            "queried_thread_id": thread_id, "lookup_status": "THREAD_UNBOUND", "provider_existence": "CONFIRMED",
        },
    )
    node_server = NodeRPCServer(
        node_service, address="127.0.0.1", port=0,
        ssl_context_factory=PairedTLS(node, node_peers).server_context,
        request_handler=node_request_handler(node_service, PairedTLS(node, node_peers), node_scope, "p620", lambda: active[0]),
    )
    try:
        registration = NodeRegistrationClient(PairedTLS(node, node_peers), "p620", f"tls://127.0.0.1:{centre_server.address[1]}")
        reply = registration.register(node, node_scope.get("p620"), port=node_server.address[1], capabilities=("session.read", "session.status"))
        assert reply["registered"] is True
        active[0] = SharingScope(**reply["scope"])
        record = centre_registry.get("air")
        assert record is not None and record.endpoint.endswith(f":{node_server.address[1]}")
        assert centre_registry.scope("air", "tinzleung").read_sessions is True

        router = NodeRouter(
            centre_registry, user_scope="tinzleung",
            remote_client_factory=remote_client_factory(centre.store.root, "p620", centre_registry),
        )
        result = router.read(thread_id="air-thread", node_id="air")
        assert result["source_node_id"] == "air"
        assert result["coverage"]["complete"] is True
        assert calls == ["air-thread"]

        # Expire the durable observation before any request; a stale route is
        # explicitly offline rather than becoming a false global not-found.
        time.sleep(1.2)
        offline = router.read(thread_id="air-thread", node_id="air")
        assert offline["error_code"] == "NODE_OFFLINE"

        # A centre-side revoke is durable and the next registration is denied.
        centre_scope.revoke("air", user_scope="tinzleung")
        denied = registration.heartbeat(node, node_scope.get("p620"), port=node_server.address[1], capabilities=("session.read",))
        assert denied["error_code"] == "SHARING_SCOPE_DENIED"
        centre_registry.refresh_states()
        assert centre_registry.get("air").state == "REVOKED"
    finally:
        node_server.close()
        centre_server.close()
        node_registry.close()
        centre_registry.close()


def test_product_centre_and_air_helper_register_as_separate_processes(tmp_path):
    import json
    import os
    import subprocess
    import sys

    node, node_peers, centre, centre_peers = paired(tmp_path)
    node_scope = NodeAuthorizationStore(node.store.root)
    centre_scope = NodeAuthorizationStore(centre.store.root)
    node_scope.grant("p620", user_scope="tinzleung", read_sessions=True)
    centre_scope.grant("air", user_scope="tinzleung", read_sessions=True)
    private = PrivateStore(node.store.root)
    private.write("node-config.json", {
        "schema_version": 1,
        "centre_id": "p620",
        "centre_endpoint": "tls://127.0.0.1:18771",
        "bind_address": "127.0.0.1",
        "port": 18772,
    })
    centre_root = tmp_path / "centre-runtime"
    centre_root.mkdir()
    registry_path = tmp_path / "process-centre.sqlite3"
    centre_cmd = [sys.executable, "node_centre_entrypoint.py", "--state", str(centre.store.root), "--node-id", "p620", "serve", "--registry", str(registry_path), "--bind", "127.0.0.1", "--port", "18771", "--stale-after", "5"]
    env = dict(os.environ, PYTHONPATH=str(tmp_path) + os.pathsep + os.getcwd())
    centre_proc = subprocess.Popen(centre_cmd, cwd=os.getcwd(), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    helper_env = dict(env, CLINX_NODE_STATE=str(node.store.root), CLINX_NODE_HEALTH_STATE=str(tmp_path / "health"), CLINX_NATIVE_HOME=str(tmp_path / "codex"))
    helper_proc = None
    try:
        assert centre_proc.stdout is not None
        ready = json.loads(centre_proc.stdout.readline())
        assert ready["ready"] is True
        helper_proc = subprocess.Popen([sys.executable, "MonitorApp/Scripts/node_service_entrypoint.py"], cwd=os.getcwd(), env=helper_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        health_path = tmp_path / "health" / "health.json"
        deadline = time.monotonic() + 8
        health = {}
        while time.monotonic() < deadline:
            if health_path.exists():
                health = json.loads(health_path.read_text())
                if health.get("status") == "running":
                    break
            time.sleep(0.1)
        assert health.get("status") == "running", health
        registry = NodeRegistry(registry_path)
        try:
            assert registry.get("air") is not None
            router = NodeRouter(registry, user_scope="tinzleung", remote_client_factory=remote_client_factory(centre.store.root, "p620", registry))
            result = router.read(thread_id="process-thread", node_id="air")
            assert result["source_node_id"] == "air"
            assert result["lookup_status"] in {"THREAD_NOT_FOUND", "NATIVE_INDEX_UNAVAILABLE"}
            assert result["read_only"] is True
        finally:
            registry.close()
    finally:
        if helper_proc is not None:
            helper_proc.terminate()
            helper_proc.wait(timeout=5)
        centre_proc.terminate()
        centre_proc.wait(timeout=5)
