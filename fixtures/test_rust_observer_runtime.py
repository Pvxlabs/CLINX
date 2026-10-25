"""Standalone Rust Observer: private fixtures, sockets and read-only sources."""

import json
import os
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http import client
from pathlib import Path
from urllib.parse import urlencode

import pytest

from clinx.discovery.identity import NodeIdentity, PrivateStore
from clinx.nodes.network_worker import read_command
from clinx.nodes.node_protocol import NodeAuthorizationStore, NodeRecord, NodeRegistry
from fixtures.test_rust_native_observer import historical as historical
from fixtures.test_rust_observation import TID, snapshot
from fixtures.test_rust_observation import native as native
from fixtures.test_rust_provider_client import Provider

TOKEN = "private_observer_" + "x" * 40


def binary():
    path = Path(os.environ["CLINX_TEST_RUST_BIN_DIR"]) / "clinx-observer"
    assert path.is_file(), "Build the standalone Rust Observer before qualification"
    return path


def environment(tmp_path, **values):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "CLINX_OBSERVER_TOKEN": TOKEN,
        "CLINX_OBSERVER_AUDIT_METHODS": "1",
        **{k: str(v) for k, v in values.items()},
    }


class Process:
    def __init__(self, root, env):
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            self.port = reserve.getsockname()[1]
        self.stdout_path = root / f"observer-{self.port}.out"
        self.stderr_path = root / f"observer-{self.port}.err"
        self.stdout = self.stdout_path.open("wb")
        self.stderr = self.stderr_path.open("wb")
        self.process = subprocess.Popen(
            [str(binary())],
            env={**env, "CLINX_OBSERVER_PORT": str(self.port)},
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=self.stdout,
            stderr=self.stderr,
        )
        try:
            deadline = time.monotonic() + 8
            while True:
                assert self.process.poll() is None, self.stderr_path.read_text()
                try:
                    if self.request("/v1/health")[0] == 200:
                        break
                except OSError:
                    pass
                assert time.monotonic() < deadline, self.stderr_path.read_text()
                time.sleep(0.02)
        except BaseException:
            self.close()
            raise

    def request(self, route, *, method="GET", token=TOKEN):
        connection = client.HTTPConnection("127.0.0.1", self.port, timeout=8)
        try:
            connection.request(method, route, headers={"Authorization": "Bearer " + token})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(3)
                raise AssertionError("Rust Observer did not stop on SIGTERM") from None
        self.stdout.close()
        self.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def without_time(value):
    if isinstance(value, dict):
        return {k: without_time(v) for k, v in value.items() if k != "observed_at"}
    if isinstance(value, list):
        return [without_time(v) for v in value]
    return value


def test_standalone_legacy_exact_projection_auth_audit_and_no_python(historical, tmp_path):
    env = environment(
        tmp_path,
        CLINX_OBSERVER_DB=historical.path,
        CLINX_OBSERVER_CODEX_HOST_IDS=json.dumps({"p620": "remote-ssh-discovered:p620"}),
    )
    before = historical.path.read_bytes()
    with Process(tmp_path, env) as observer:
        assert observer.request("/v1/health", token="bad")[0] == 401
        assert observer.request("/v1/health", method="POST")[0] == 405
        assert observer.request("/v1/tasks?state=active&offset=-1")[0] == 400
        assert observer.request("/v1/tasks/absent")[0] == 404
        status, item = observer.request("/v1/tasks/task-fixture")
        assert status == 200
        assert without_time(item) == historical.expected
        assert item["final_result"]["execution_ref"] == "exec-fixture"
        assert item["codex_uri"].endswith("?hostId=remote-ssh-discovered%3Ap620")
        assert observer.request("/v1/tasks/task-fixture/events")[0] == 200
        assert observer.request("/v2/observations")[1]["error"] == "OBSERVATION_NOT_CONFIGURED"
        assert Path(f"/proc/{observer.process.pid}/exe").resolve() == binary().resolve()
        assert not Path(f"/proc/{observer.process.pid}/task/{observer.process.pid}/children").read_text().strip()
    assert observer.process.returncode == 0
    rows = [json.loads(line) for line in observer.stdout_path.read_text().splitlines()]
    assert rows and all(set(row) == {"observer_http_method", "status"} for row in rows)
    assert TOKEN not in observer.stdout_path.read_text() + observer.stderr_path.read_text()
    assert historical.path.read_bytes() == before


@pytest.mark.parametrize(
    "values,error",
    [
        ({"CLINX_OBSERVER_TOKEN": "bad"}, "CLINX_OBSERVER_TOKEN"),
        ({"CLINX_OBSERVER_CODEX_HOST_IDS": "[]"}, "CLINX_OBSERVER_CODEX_HOST_IDS"),
        ({"CLINX_OBSERVER_CODEX_HOST_IDS": '{"p620":"host/name"}'}, "CLINX_OBSERVER_CODEX_HOST_IDS"),
        ({"CLINX_OBSERVER_PORT": "65536"}, "OBSERVER_HTTP_BIND_FAILED"),
    ],
)
def test_bad_configuration_never_initializes_source(tmp_path, values, error):
    absent = tmp_path / "absent.db"
    result = subprocess.run(
        [str(binary())],
        env=environment(tmp_path, CLINX_OBSERVER_DB=absent, **values),
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 1 and error.encode() in result.stderr
    assert not absent.exists()


def test_missing_legacy_database_fails_without_creating_it(tmp_path):
    absent = tmp_path / "absent.db"
    result = subprocess.run(
        [str(binary())],
        env=environment(tmp_path, CLINX_OBSERVER_DB=absent),
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 1 and b"LEGACY_SOURCE_UNAVAILABLE" in result.stderr
    assert not absent.exists()


@pytest.fixture
def native_config(native, historical, tmp_path):
    state = tmp_path / "state"
    identity = NodeIdentity(PrivateStore(state), "observer-fixture")
    approvals = NodeAuthorizationStore(state)
    scope = approvals.grant("observer-fixture", user_scope="alice", read_sessions=True, projects=("/approved",))
    registry = NodeRegistry(tmp_path / "nodes.db")
    registry.register(
        NodeRecord("observer-fixture", user_scope="alice", public_key_fingerprint=identity.public["fingerprint"], state="ONLINE"),
        scope,
    )
    observations = tmp_path / "observations.db"
    read_command("observation.init", path=str(observations))
    env = environment(
        tmp_path,
        CLINX_OBSERVER_DB=historical.path,
        CLINX_OBSERVATION_DB=observations,
        CLINX_OBSERVATION_STATE=state,
        CLINX_OBSERVATION_REGISTRY=registry.path,
        CLINX_OBSERVATION_USER_SCOPE="alice",
        CLINX_OBSERVATION_NODE_ID="observer-fixture",
        CLINX_OBSERVATION_NATIVE_HOME=native.root,
    )
    yield env, approvals, registry
    registry.close()


def test_native_collector_checkpoints_concurrency_restart_revocation_and_no_writes(native_config, native, tmp_path):
    env, approvals, registry = native_config
    before = snapshot(native.root)
    registry_before = Path(registry.path).read_bytes()
    with Process(tmp_path, env) as observer:
        status, inventory = observer.request("/v2/native-collector/inventory")
        assert status == 200 and inventory["read_only"] is True
        status, sampled = observer.request(f"/v2/native-collector/sample/{TID}")
        assert status == 200 and sampled["thread_id"] == TID
        assert sampled["events"] and "NEVER_" not in json.dumps(sampled)
        route = f"/v2/native-collector/sample/{TID}?" + urlencode({"checkpoint": sampled["checkpoint"]})
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: observer.request(route), range(8)))
        assert all(status == 200 and body["thread_id"] == TID for status, body in replies)
        status, denied = observer.request(route + "corrupted")
        assert status == 409 and denied["error"] == "NATIVE_COLLECTOR_CURSOR_RESET"
        status, context = observer.request(f"/v2/native/{TID}?context=1")
        assert status == 200 and context["last_user_intent"] == "User 2"
        assert context["codex_running"] is False
        assert context["native_display_state"] == "UNKNOWN"
        assert "NEVER_" not in json.dumps(context)
        assert observer.request(f"/v2/native/{TID}?recent_turns=999999999999999999999")[1]["error"] == "INVALID_CONTEXT_LIMIT"
    with Process(tmp_path, env) as reopened:
        assert reopened.request(route)[0] == 409
        assert reopened.request(f"/v2/native-collector/sample/{TID}")[0] == 200
        approvals.revoke("observer-fixture")
        assert reopened.request("/v2/native-collector/inventory")[1]["error"] == "SHARING_SCOPE_DENIED"
        assert reopened.request(f"/v2/native/{TID}")[1]["error"] == "SHARING_SCOPE_DENIED"
    assert snapshot(native.root) == before
    assert Path(registry.path).read_bytes() == registry_before


@pytest.mark.parametrize("mode", ["exact", "wrong_workspace", "revoked"])
def test_native_provider_read_exact_identity_and_scope_recheck(native_config, native, tmp_path, mode):
    env, approvals, _ = native_config
    socket_path = tmp_path / "p.sock"

    def reply(request):
        if request["method"] == "initialized":
            return []
        if request["method"] == "initialize":
            value = {"userAgent": "private fixture"}
        else:
            assert request["method"] == "thread/read"
            assert request["params"] == {"threadId": TID, "includeTurns": False}
            value = {"thread": {"id": TID, "cwd": "/foreign" if mode == "wrong_workspace" else "/approved", "status": {"type": "active"}}}
            if mode == "revoked":
                approvals.revoke("observer-fixture")
        return [{"id": request["id"], "result": value}]

    provider = Provider(socket_path, reply)
    try:
        with Process(tmp_path, {**env, "CLINX_NATIVE_OBSERVER_SOCKET": str(socket_path)}) as observer:
            status, result = observer.request(f"/v2/native/{TID}")
            if mode == "exact":
                assert status == 200
                assert result["codex_running"] is True
                assert result["native_display_state"] == "RUNNING"
                assert result["queried_thread_id"] == TID
            else:
                assert status == 400
                assert result["error"] == ("NATIVE_OBSERVATION_IDENTITY_CONFLICT" if mode == "wrong_workspace" else "SHARING_SCOPE_DENIED")
    finally:
        provider.close()
    assert [r["method"] for r in provider.calls] == ["initialize", "initialized", "thread/read"]


def test_rust_monitor_uses_existing_mcp_read_transport_and_no_legacy_database(tmp_path):
    endpoint = tmp_path / "m.sock"
    calls, errors = [], []
    stop = threading.Event()
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(endpoint))
    listener.listen(8)
    listener.settimeout(0.1)

    def serve():
        try:
            while not stop.is_set():
                try:
                    peer, _ = listener.accept()
                except TimeoutError:
                    continue
                with peer, peer.makefile("rb") as stream:
                    request = json.loads(stream.readline())
                    name = request["params"]["name"]
                    calls.append(name)
                    assert name in {"clinx_get_capabilities", "clinx_list_observations"}
                    result = {"authority": "RUST", "read_only": True} if name == "clinx_get_capabilities" else {"items": [], "read_only": True}
                    peer.sendall((json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"structuredContent": result}}) + "\n").encode())
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=serve)
    thread.start()
    absent = tmp_path / "not-created.db"
    try:
        with Process(tmp_path, environment(tmp_path, CLINX_OBSERVER_RUST_MCP=endpoint, CLINX_OBSERVER_DB=absent)) as observer:
            assert observer.request("/v1/health")[1]["authority"] == "RUST"
            assert observer.request("/v1/tasks?state=active")[1]["legacy_history"] is False
            assert observer.request("/v1/tasks/old")[1]["error"] == "RUST_TASKS_USE_OBSERVATIONS_V2"
            assert observer.request("/v2/observations")[0] == 200
    finally:
        stop.set()
        thread.join(3)
        listener.close()
    assert not thread.is_alive() and not errors
    assert calls and not absent.exists()
