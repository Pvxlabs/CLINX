"""Independent Rust Helper/Centre processes; Authority/desktop are explicit fixtures.

No Python service callback runs inside either daemon. These tests qualify wiring,
not a deployed Mac or a production Authority/Provider execution.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import select
import socket
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from clinx.discovery.identity import NodeIdentity, PrivateStore, TrustedPeerStore
from clinx.nodes.node_protocol import NodeRegistry
from clinx.nodes.node_runtime import PairedTLS
from fixtures.test_rust_provider_client import Provider

BIN = Path(os.environ["CLINX_TEST_RUST_BIN_DIR"])
DAEMON = BIN / "clinx-node-daemon"


def cli(root, node, *args, **kwargs):
    result = subprocess.run([str(DAEMON), "--state", str(root), "--node-id", node, *args],
                            text=True, capture_output=True, timeout=15, **kwargs)
    assert result.returncode == 0, (result.stdout, result.stderr)
    return json.loads(result.stdout)


def ready(process):
    assert select.select([process.stdout], [], [], 10)[0], "Rust daemon did not become ready"
    line = process.stdout.readline()
    assert line, process.stderr.read() if process.poll() is not None else "empty ready frame"
    return json.loads(line)


@contextmanager
def running(args, **kwargs):
    process = subprocess.Popen([str(DAEMON), *args], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, **kwargs)
    try:
        yield process
    finally:
        process.terminate()
        process.communicate(timeout=10)
        assert process.returncode == 0


class AuthorityFixture:
    """A recording IPC fixture, never a substitute for Core transaction tests."""
    def __init__(self, path):
        self.path, self.calls, self.errors = path, [], []
        self.cancel_route = False
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(path))
        path.chmod(0o600)
        self.server.listen(16)
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
                    response = dict(protocol_version=request["protocol_version"], request_id=request["request_id"])
                    if request["operation"] == "health":
                        response["result"] = dict(business_commands=True)
                    else:
                        self.calls.append(request)
                        # Loss occurs after receipt at the Centre IPC boundary.
                        if request["payload"]["payload"].get("prepared_execution_ref") == "prepared_lost":
                            continue
                        operation = request["payload"]["operation"]
                        if self.cancel_route and operation == "get_status":
                            response["result"] = dict(task_ref="task-exact", execution_ref="exec-exact", thread_id="thread-exact", native_turn_id="turn-exact")
                        elif self.cancel_route and operation == "list_tasks":
                            response["result"] = dict(tasks=[dict(task_id="task-exact", host="air", project="fixture", thread_id="thread-exact")])
                        elif self.cancel_route and operation == "cancel":
                            response["result"] = dict(status="CANCELLATION_PENDING", resource_state="HELD", execution_ref="exec-exact")
                        else:
                            response["error"] = "FIXTURE_AUTHORITY_BOUNDARY"
                    peer.sendall(json.dumps(response).encode() + b"\n")
        except BaseException as exc:
            if not self.stopped.is_set():
                self.errors.append(exc)

    def close(self):
        self.stopped.set()
        self.thread.join(3)
        self.server.close()
        assert not self.thread.is_alive() and not self.errors


@pytest.fixture
def daemons(tmp_path):
    air, centre = tmp_path / "air", tmp_path / "centre"
    public = cli(centre, "centre", "bootstrap")
    cli(air, "air", "bootstrap")
    with running(["--state", str(centre), "--node-id", "centre", "pair-listen", "--port", "0"]) as pair:
        offer = ready(pair)
        paired = cli(air, "air", "pair-connect", "--peer", "centre", "--fingerprint", public["fingerprint"],
                     "--endpoint", f'tls://127.0.0.1:{offer["port"]}', input=offer["pairing_code"] + "\n")
        assert paired == dict(paired=True, node_id="centre", authority_granted=False)
    for root, node, peer in [(centre, "centre", "air"), (air, "air", "centre")]:
        cli(root, node, "share", "--peer", peer, "--user-scope", "alice", "--read-sessions", "--execute-tasks", "--approved")
    centre_identity, air_identity = NodeIdentity(PrivateStore(centre), "centre"), NodeIdentity(PrivateStore(air), "air")
    centre_tls = PairedTLS(centre_identity, TrustedPeerStore(centre_identity.store))
    air_tls = PairedTLS(air_identity, TrustedPeerStore(air_identity.store))
    authority = AuthorityFixture(tmp_path / "authority.sock")
    native = tmp_path / "native"
    native.mkdir()
    with sqlite3.connect(native / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads (id TEXT, cwd TEXT, rollout_path TEXT)")
    cfg = tmp_path / "config.toml"
    cfg.write_text('[linear]\nteam_id="team"\ntrigger_label="label"\ntodo_state="Todo"\nrunning_state="Running"\nreview_state="Review"\n'
                   '[projects.fixture]\ncwd="/fixture"\nbranch="main"\n[runtime]\nruntime_host="air"\n'
                   f'[app_server]\nlocal_socket="{tmp_path / "provider.sock"}"\n')
    env = dict(os.environ, CLINX_AUTHORITY="rust", CLINX_RUST_SOCKET=str(authority.path), CLINX_RUST_UID=str(os.getuid()),
               CLINX_NODE_BINARY=str(BIN / "clinx-node"), CLINX_PROVIDER_BINARY=str(BIN / "clinx-provider"))
    registry_path = tmp_path / "nodes.sqlite3"
    try:
        with running(["--state", str(centre), "--node-id", "centre", "serve", "--registry", str(registry_path),
                      "--port", "0"], env=env) as process:
            centre_ready = ready(process)
            endpoint = f'tls://127.0.0.1:{centre_ready["address"][1]}'
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            cli(air, "air", "configure-node", "--centre", "centre", "--endpoint", endpoint,
                "--bind", "127.0.0.1", "--port", str(port), "--execution-config", str(cfg))
            helper_env = dict(env, CLINX_NODE_STATE=str(air), CLINX_NODE_HEALTH_STATE=str(tmp_path / "health"),
                              CLINX_NATIVE_HOME=str(native))
            registry = NodeRegistry(registry_path)
            try:
                yield SimpleNamespace(air=air, centre=centre, public=air_identity.public, env=helper_env,
                    registry=registry, tls=centre_tls, endpoint=f"tls://127.0.0.1:{port}",
                    centre_client=air_tls.client("centre", endpoint), authority=authority, tmp=tmp_path)
            finally:
                registry.close()
    finally:
        authority.close()


@contextmanager
def helper(route):
    with running(["helper"], env=route.env) as process:
        assert ready(process)["runtime"] == "RUST_NODE_DAEMON_V1"
        client = route.tls.client("air", route.endpoint)
        until = time.monotonic() + 8
        while True:
            registered = route.registry.get("air")
            if registered and registered.endpoint == route.endpoint:
                value = client.call(dict(operation="session.read", user_scope="alice", thread_id="absent"))
                if value.get("lookup_status") == "THREAD_NOT_FOUND":
                    break
            assert time.monotonic() < until, (registered, (route.tmp / "health/health.json").read_text())
            time.sleep(.05)
        yield client


def start_request(reference="one"):
    return dict(operation="execution.start", request_id=reference, execution_ref="exec_" + reference,
                prepared_execution_ref="prepared_" + reference, user_scope="alice", approved=True)


def test_standalone_pair_registration_read_and_exact_reverse_route(daemons):
    with helper(daemons) as client:
        result = client.call(start_request())
        assert result["error_code"] == "FIXTURE_AUTHORITY_BOUNDARY"
        assert len(daemons.authority.calls) == 1
        wire = daemons.authority.calls[0]
        assert wire["operation"] == "business.node_request"
        assert wire["payload"]["operation"] == "get_prepared"
        assert wire["payload"]["payload"] == {"prepared_execution_ref": "prepared_one"}
        assert wire["payload"]["scope"] == dict(principal=dict(node_id="air", user_scope="alice",
            fingerprint=daemons.public["fingerprint"]), projects=["*"])
        assert wire["payload"]["request_id"]
        assert client.call(start_request())["idempotent"]
        assert [v["payload"]["operation"] for v in daemons.authority.calls] == ["get_prepared", "get_status"]
        assert client.call(dict(start_request("other"), user_scope="bob"))["error_code"] == "USER_SCOPE_DENIED"
        cli(daemons.air, "air", "share", "--peer", "centre", "--user-scope", "alice", "--revoke", "--approved")
        assert client.call(start_request("revoked"))["error_code"] == "SHARING_SCOPE_DENIED"
        assert len(daemons.authority.calls) == 2


def test_unknown_after_lost_ack_is_read_back_after_restart_without_replaying_start(daemons):
    with helper(daemons) as client:
        result = client.call(start_request("lost"))
        # Existing protocol records callback failure separately from uncertain side effects.
        assert result["operation_state"] == "FAILED"
        assert result["error_code"] == "DELIVERY_UNKNOWN"
        assert result["side_effect"] == "UNKNOWN"
    first = list(daemons.authority.calls)
    assert len(first) == 1 and first[0]["payload"]["operation"] == "get_prepared"
    with helper(daemons) as client:
        result = client.call(start_request("lost"))
        assert result["operation_state"] == "UNKNOWN" and result["idempotent"]
    operations = [v["payload"]["operation"] for v in daemons.authority.calls]
    assert operations.count("get_prepared") == 1
    assert "start" not in operations
    assert operations[1:]


def test_concurrent_exact_request_claim_and_payload_conflict(daemons):
    with helper(daemons) as client:
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: client.call(start_request()), range(6)))
        assert all(r.get("error_code") == "FIXTURE_AUTHORITY_BOUNDARY" or r.get("operation_state") == "UNKNOWN" for r in results)
        assert sum(v["payload"]["operation"] == "get_prepared" for v in daemons.authority.calls) == 1
        conflict = client.call(dict(start_request(), prepared_execution_ref="prepared_changed"))
        assert conflict["error_code"] == "IDEMPOTENCY_KEY_REUSE"


def test_standalone_helper_provider_evidence_identity_and_no_duplicate_submission(daemons):
    evidence = dict(id="exact", cwd="/fixture", turns=[dict(id="turn-exact", status="completed")])
    def reply(request):
        if request["method"] == "initialized":
            return []
        result = dict(userAgent="fixture") if request["method"] == "initialize" else dict(thread=evidence)
        return [dict(id=request["id"], result=result)]
    provider = Provider(daemons.tmp / "provider.sock", reply)
    try:
        with helper(daemons) as client:
            body = json.dumps(dict(method="thread/read", params=dict(threadId="exact", includeTurns=True)), separators=(",", ":")).encode()
            context = dict(host="air", user_scope="alice", cwd="/fixture", thread_id="exact", mode="read")
            request = dict(operation="provider.upload", protocol_version="clinx-node-v1", node_id="air", request_id="a" * 32,
                context=context, index=0, count=1, digest=hashlib.sha256(body).hexdigest(), data=base64.b64encode(body).decode())
            assert client.call(request)["accepted"]
            assert client.call(request)["error_code"] == "PROVIDER_REQUEST_IDENTITY_CONFLICT"
            result = client.call(dict(operation="provider.read", protocol_version="clinx-node-v1", node_id="air", request_id="a" * 32, context=context, index=0))
            decoded = base64.b64decode(result["data"])
            assert hashlib.sha256(decoded).hexdigest() == result["digest"]
            assert json.loads(decoded) == {"result": {"thread": evidence}}
            denied = client.call(dict(request, request_id="b" * 32, context=dict(context, user_scope="bob")))
            assert denied["error_code"] == "PROVIDER_SCOPE_DENIED"
            assert [v["method"] for v in provider.calls] == ["initialize", "initialized", "thread/read"]
    finally:
        provider.close()


def test_cancel_routes_exact_execution_and_preserves_pending_until_authority_evidence(daemons):
    daemons.authority.cancel_route = True
    request = dict(operation="execution.cancel", request_id="cancel-exact", execution_ref="exec-exact",
                   user_scope="alice", thread_id="thread-exact", native_turn_id="turn-exact", approved=True)
    with helper(daemons) as client:
        result = client.call(request)
        assert result["operation_state"] == "CANCELLATION_PENDING"
        assert result["resource_state"] == "HELD"
        cancels = [v for v in daemons.authority.calls if v["payload"]["operation"] == "cancel"]
        assert len(cancels) == 1
        assert cancels[0]["payload"]["payload"] == dict(execution_ref="exec-exact", approved=True)
        assert client.call(request)["idempotent"]
        wrong = client.call(dict(request, request_id="cancel-wrong", native_turn_id="other-turn"))
        assert wrong["error_code"] == "EXECUTION_TURN_IDENTITY_CONFLICT"
    with helper(daemons) as client:
        assert client.call(request)["operation_state"] == "CANCELLATION_PENDING"
    assert len([v for v in daemons.authority.calls if v["payload"]["operation"] == "cancel"]) == 1


@pytest.mark.parametrize("reason", ["wrong_host", "no_socket"])
def test_unavailable_execution_route_preserves_independent_read_permission(daemons, reason):
    cfg = daemons.tmp / "config.toml"
    original = cfg.read_text()
    cfg.write_text(original.replace('runtime_host="air"', 'runtime_host="wrong"')
                   if reason == "wrong_host" else original.split("[app_server]")[0])
    with helper(daemons) as client:
        row = daemons.registry.get("air")
        assert "session.read" in row.capabilities
        assert "execution.start" not in row.capabilities
        result = client.call(start_request())
        assert result["error_code"] == "OPERATION_NOT_IMPLEMENTED"
        assert not daemons.authority.calls


def test_pairing_terminal_cancellation_restores_echo(tmp_path):
    import pty
    import signal
    import termios
    root = tmp_path / "device"
    cli(root, "air", "bootstrap")
    master, slave = pty.openpty()
    before = termios.tcgetattr(slave)
    process = subprocess.Popen([str(DAEMON), "--state", str(root), "--node-id", "air", "pair-connect",
        "--peer", "centre", "--fingerprint", "fixture", "--endpoint", "tls://127.0.0.1:1"],
        stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        until = time.monotonic() + 3
        while termios.tcgetattr(slave)[3] & termios.ECHO:
            assert time.monotonic() < until
            time.sleep(.02)
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=3)
        assert process.returncode == 1 and b"PAIRING_CANCELLED" in stderr
        assert not stdout
        assert termios.tcgetattr(slave) == before
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=3)
        os.close(master)
        os.close(slave)
