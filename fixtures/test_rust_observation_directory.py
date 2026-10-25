"""Real Rust observation MCP and paired reads; all state and services are private."""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from clinx.discovery.identity import NodeIdentity, PrivateStore
from clinx.monitor.network_observation import ObservationWriter
from clinx.nodes.node_protocol import NodeAuthorizationStore, NodeRecord, NodeRegistry, NodeRPCServer, _now
from fixtures.test_node_authority_route import route as route
from fixtures.test_rust_observation import native as native, event, TID
from fixtures.test_native_transport_binary import Provider

BIN = Path(os.environ["CLINX_TEST_RUST_BIN_DIR"])


def state_hash(root):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir() if p.is_file()}


def env_for(state, registry, observations, native, node):
    # No production credentials/configuration can reach the test processes or
    # leak into pytest's subprocess failure diagnostics.
    return dict(HOME=str(state.parent), LANG="C.UTF-8",
        CLINX_OBSERVATION_STATE=str(state), CLINX_OBSERVATION_REGISTRY=str(registry),
        CLINX_OBSERVATION_DB=str(observations), CLINX_OBSERVATION_NATIVE_HOME=str(native),
        CLINX_OBSERVATION_NODE_ID=node, CLINX_OBSERVATION_USER_SCOPE="alice",
        CLINX_OBSERVATION_PYTHON="/no-python-may-be-started", PATH="/no-python-may-be-started")


@contextmanager
def mcp(env, *, normal=False, config=None):
    args=[str(BIN / "clinx-mcp"), "--stdio"]
    args += ["--config", str(config)] if normal else ["--observations-only"]
    process = subprocess.Popen(args, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    sequence = 0
    def request(method, params=None):
        nonlocal sequence
        sequence += 1
        process.stdin.write(json.dumps(dict(jsonrpc="2.0", id=sequence, method=method, params=params or {})) + "\n")
        process.stdin.flush()
        assert select.select([process.stdout], [], [], 10)[0], "MCP response deadline"
        line = process.stdout.readline()
        assert line, process.stderr.read() if process.poll() is not None else "MCP EOF"
        response = json.loads(line)
        assert response["id"] == (None if "error" in response else sequence)
        return response
    def call(name, arguments=None):
        response = request("tools/call", dict(name=name, arguments=arguments or {}))
        assert "error" not in response, response
        return response["result"]
    try:
        yield SimpleNamespace(request=request, call=call, process=process)
    finally:
        process.stdin.close()
        process.wait(timeout=10)
        assert process.returncode == 0, process.stderr.read()


@pytest.fixture
def local(tmp_path, native):
    identity = NodeIdentity(PrivateStore(tmp_path / "device"), "air")
    approvals = NodeAuthorizationStore(identity.store.root)
    scope = approvals.grant("air", user_scope="alice", read_sessions=True, projects=("/approved",))
    registry = NodeRegistry(tmp_path / "registry.db")
    registry.register(NodeRecord("air", user_scope="alice", public_key_fingerprint=identity.public["fingerprint"],
        state="ONLINE", last_seen=_now()), scope)
    registry.close()
    writer = ObservationWriter(tmp_path / "observations.db")
    writer.accept("air", scope, dict(stream_id="fixture", events=[event(1), event(2, turn="turn-2", ordinal=2)]))
    # Read-only startup and queries must neither replace trust nor create this file.
    (identity.store.root / "store.lock").unlink(missing_ok=True)
    return SimpleNamespace(identity=identity, approvals=approvals, scope=scope, writer=writer, native=native,
        env=env_for(identity.store.root, tmp_path / "registry.db", writer.path, native.root, "air"))


def test_standalone_four_tools_read_exact_private_history_without_python_or_state_writes(local):
    before = state_hash(local.identity.store.root)
    with mcp(local.env) as api:
        assert api.request("initialize")["result"]["protocolVersion"]
        tools=api.request("tools/list")["result"]["tools"]
        expected=json.loads((Path(__file__).resolve().parents[1] / "clinx/mcp/registry.json").read_text())["observations"]
        assert tools == expected
        names={v["name"] for v in tools}
        assert names == {"clinx_list_observations", "clinx_get_observation", "clinx_get_observation_context", "clinx_get_observation_activity"}
        listed=api.call("clinx_list_observations")["structuredContent"]
        assert listed["read_only"] and len(listed["items"]) == 1
        observation=listed["items"][0]["observation_id"]
        detail=api.call("clinx_get_observation", dict(observation_id=observation))["structuredContent"]
        assert detail["item"]["native_thread_id"] == TID and detail["read_only"]
        context=api.call("clinx_get_observation_context", dict(observation_id=observation))["structuredContent"]
        assert context["last_codex_result"] == "Answer 2" and context["read_only"]
        assert "NEVER_" not in json.dumps(context)
        activity=api.call("clinx_get_observation_activity", dict(observation_id=observation))["structuredContent"]
        assert activity["read_only"] and len(activity["items"]) == 2
        assert activity["coverage"] == "RECEIVED_EVENTS_ONLY"
        denied=api.request("tools/call", dict(name="clinx_start_execution", arguments={}))
        assert denied["error"]["code"] == -32602
        children=Path(f"/proc/{api.process.pid}/task/{api.process.pid}/children")
        assert children.read_text().strip() == ""
    assert state_hash(local.identity.store.root) == before


def test_read_scope_revocation_and_cursor_rebinding_are_checked_without_restart(local):
    with mcp(local.env) as api:
        listed=api.call("clinx_list_observations")["structuredContent"]
        observation=listed["items"][0]["observation_id"]
        detail=api.call("clinx_get_observation", dict(observation_id=observation, limit=1))["structuredContent"]
        assert detail["next_cursor"]
        local.approvals.revoke("air", user_scope="alice")
        result=api.call("clinx_get_observation", dict(observation_id=observation, cursor=detail["next_cursor"]))
        assert result["isError"] and result["structuredContent"]["read_only"]
        assert api.call("clinx_list_observations")["structuredContent"]["items"] == []


def test_restart_keeps_exact_cursor_and_missing_state_never_bootstraps(local, tmp_path):
    with mcp(local.env) as api:
        observation=api.call("clinx_list_observations")["structuredContent"]["items"][0]["observation_id"]
        page=api.call("clinx_get_observation", dict(observation_id=observation, limit=1))["structuredContent"]
    with mcp(local.env) as api:
        older=api.call("clinx_get_observation", dict(observation_id=observation, limit=1, cursor=page["next_cursor"]))["structuredContent"]
        assert older["turns"][0]["turn_id"] != page["turns"][0]["turn_id"]
    missing=tmp_path / "not-created"
    run=subprocess.run([str(BIN / "clinx-mcp"), "--observations-only", "--stdio"],
        env=dict(local.env, CLINX_OBSERVATION_STATE=str(missing)), input="", capture_output=True, text=True, timeout=10)
    assert run.returncode != 0 and not missing.exists()


@pytest.mark.parametrize("change", ["none", "scope", "projection", "peer_revoked", "identity"])
def test_native_directory_uses_exact_paired_remote_and_rechecks_before_return(route, tmp_path, change):
    scope=route.approvals.grant("air", user_scope="alice", read_sessions=True, projects=("/approved",))
    registry=route.service.registry
    record=registry.get("air")
    writer=ObservationWriter(tmp_path / "observations.db")
    writer.accept("air",scope,dict(stream_id="remote",events=[event()]))
    replacement = NodeIdentity(PrivateStore(tmp_path / "replacement"), "air") if change == "identity" else None
    requests=[]
    def respond(request, *_):
        requests.append(request)
        if change=="scope":
            route.approvals.revoke("air",user_scope="alice")
        elif change=="projection":
            writer.accept("air",scope,dict(stream_id="remote",events=[event(2,turn="new-turn",ordinal=2)]))
        elif change=="peer_revoked":
            route.service.tls.peers.revoke("air")
        elif change=="identity":
            # Simulate atomic replacement with a complete, valid but different
            # trust identity after the old peer authenticated the request.
            state = route.service.tls.identity.store.read("peers.json")
            identity = replacement.store.read("identity.json")
            state["peers"]["air"].update(replacement.public, certificate=identity["certificate"])
            route.service.tls.identity.store.write("peers.json", state)
        return dict(queried_thread_id=TID, lookup_status="THREAD_UNBOUND", provider_existence="CONFIRMED",
                    last_codex_result="remote exact result", read_only=True)
    server=NodeRPCServer(None,ssl_context_factory=route.registration.tls.server_context,request_handler=respond)
    try:
        registry.register(dataclasses.replace(record, endpoint=f"tls://127.0.0.1:{server.address[1]}",last_seen=_now()),scope)
        root=route.service.tls.identity.store.root
        (root / "store.lock").unlink(missing_ok=True)
        before=state_hash(root)
        env=env_for(root,registry.path,writer.path,tmp_path / "no-native-history","centre")
        with mcp(env) as api:
            observation=api.call("clinx_list_observations")["structuredContent"]["items"][0]["observation_id"]
            result=api.call("clinx_get_observation_context",dict(observation_id=observation))
            if change=="none":
                assert not result["isError"] and result["structuredContent"]["last_codex_result"] == "remote exact result"
            else:
                assert result["isError"]
                assert result["structuredContent"]["error_code"] in {"OBSERVATION_NOT_FOUND","SHARING_SCOPE_DENIED","EXECUTION_TURN_IDENTITY_CONFLICT","NODE_IDENTITY_MISMATCH"}
                if change == "identity":
                    assert result["structuredContent"]["error_code"] == "NODE_IDENTITY_MISMATCH"
        assert len(requests)==1 and requests[0]["node_id"]=="air" and requests[0]["user_scope"]=="alice"
        if change=="none":
            assert state_hash(root)==before
    finally:
        server.close()


class AuthorityReads:
    """Recording read-only IPC fixture; this is not a business Authority."""
    def __init__(self, path, hook):
        self.path, self.hook, self.calls, self.errors = path, hook, [], []
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(path))
        path.chmod(0o600)
        self.server.listen(8)
        self.server.settimeout(.1)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        try:
            while not self.stopped.is_set():
                try:
                    peer, _ = self.server.accept()
                except socket.timeout:
                    continue
                with peer, peer.makefile("rb") as stream:
                    request = json.loads(stream.readline())
                    self.calls.append(request)
                    operation = request["operation"]
                    if operation == "health":
                        result = dict(business_commands=True)
                    elif operation == "business.get_status":
                        self.hook()
                        result = dict(task_ref="task-exact", execution_ref="exec-exact", thread_id=TID,
                                      native_turn_id="turn-2", status="SUCCEEDED")
                    elif operation == "business.list_tasks":
                        result = dict(tasks=[dict(task_id="task-exact", thread_id=TID, host="air", project="/approved")])
                    else:
                        raise AssertionError("Unexpected Authority operation: " + operation)
                    peer.sendall(json.dumps(dict(protocol_version=request["protocol_version"],
                        request_id=request["request_id"], result=result)).encode() + b"\n")
        except BaseException as exc:
            self.errors.append(exc)

    def close(self):
        self.stopped.set()
        self.thread.join(3)
        self.server.close()
        assert not self.thread.is_alive() and not self.errors


@pytest.mark.parametrize("change", ["none", "scope", "execution", "identity"])
def test_normal_mcp_managed_context_reads_exact_authority_provider_without_recursion(local, tmp_path, change):
    row = event(3, turn="turn-2", ordinal=2)
    row.update(task_ref="task-exact", binding_evidence="CANONICAL_ROUTE")
    row["turn"]["execution_ref"] = "exec-exact"
    local.writer.accept("air", local.scope, dict(stream_id="fixture", events=[row]))
    replacement = NodeIdentity(PrivateStore(tmp_path / "replacement"), "air") if change == "identity" else None
    def hook():
        if change == "scope":
            local.approvals.revoke("air", user_scope="alice")
        elif change == "execution":
            next_execution = event(4, turn="turn-new", ordinal=3)
            next_execution.update(task_ref="task-exact", binding_evidence="CANONICAL_ROUTE")
            next_execution["turn"]["execution_ref"] = "exec-new"
            local.writer.accept("air", local.scope, dict(stream_id="fixture", events=[next_execution]))
        elif change == "identity":
            local.identity.store.write("identity.json", replacement.store.read("identity.json"))
    authority = AuthorityReads(tmp_path / "a.sock", hook)
    provider = Provider(tmp_path / "p.sock")
    provider.threads[TID] = dict(id=TID, cwd="/approved", status=dict(type="idle"))
    provider.turns[TID] = [dict(id="turn-2", status="completed", items=[
        dict(id="input", type="userMessage", content=[dict(type="text", text="Exact input")]),
        dict(id="answer", type="agentMessage", phase="final_answer", text="Exact provider result")])]
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps(dict(runtime_host="air", provider_endpoint=str(provider.path),
        provider_timeout_seconds=2, host_aliases={"air": ["air"]}, codex_host_ids={})))
    env = dict(local.env, CLINX_RUST_SOCKET=str(authority.path), CLINX_RUST_UID=str(os.getuid()),
        CLINX_RUST_OBSERVATION_READS="1", CLINX_MCP_BINARY="/must-not-recurse", CLINX_OBSERVER_PORT="0")
    before = state_hash(local.identity.store.root)
    try:
        with mcp(env, normal=True, config=config) as api:
            tools={v["name"] for v in api.request("tools/list")["result"]["tools"]}
            assert {"clinx_get_context", "clinx_get_observation_context"} <= tools
            listed=api.call("clinx_list_observations")["structuredContent"]
            observation=listed["items"][0]["observation_id"]
            assert api.call("clinx_get_observation", dict(observation_id=observation))["structuredContent"]["item"]["task_ref"] == "task-exact"
            assert api.call("clinx_get_observation_activity", dict(observation_id=observation))["structuredContent"]["read_only"]
            result=api.call("clinx_get_observation_context", dict(observation_id=observation))
            if change == "none":
                content=result["structuredContent"]
                assert not result["isError"] and content["last_codex_result"] == "Exact provider result"
                assert (content["task_ref"], content["execution_ref"], content["thread_id"], content["execution_turn_ref"], content["context_scope"]) == (
                    "task-exact", "exec-exact", TID, "turn-2", "EXACT_EXECUTION_TURN")
                assert content["read_only"] and state_hash(local.identity.store.root) == before
            else:
                assert result["isError"]
                assert result["structuredContent"]["error_code"] in {"OBSERVATION_NOT_FOUND", "EXECUTION_TURN_IDENTITY_CONFLICT", "NODE_IDENTITY_MISMATCH"}
                if change == "identity":
                    assert result["structuredContent"]["error_code"] == "NODE_IDENTITY_MISMATCH"
            assert Path(f"/proc/{api.process.pid}/task/{api.process.pid}/children").read_text().strip() == ""
        assert [r["operation"] for r in authority.calls] == [
            "health", "health", "business.get_status", "business.list_tasks"]
        assert not provider.inputs and not provider.interrupts
    finally:
        provider.close()
        authority.close()
