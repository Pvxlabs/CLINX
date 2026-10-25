"""Private loopback Observer protocol, callbacks and Rust-owned listener lifecycle."""

import contextlib
import io
import json
import socket
import sqlite3
import threading
from http import client as http_client
from types import SimpleNamespace

import pytest

from clinx.monitor.observer_server import APIError, ObserverAPI, ObserverHTTPServer
from clinx.nodes.node_protocol import NodeProtocolError

TOKEN = "observer_qualification_" + "x" * 32
TID = "12345678-1234-1234-1234-123456789abc"
OID = "obs_" + "a" * 40


class Store:
    def health(self):
        return {"ok": True}

    def list_tasks(self, state, offset=0):
        return {"state": state, "offset": offset}

    def task(self, task_ref):
        return {"task_ref": task_ref}

    def task_events(self, task_ref, after):
        return {"task_ref": task_ref, "after": after}

    def task_activity(self, task_ref, execution_ref, after, before):
        return dict(
            task_ref=task_ref, execution_ref=execution_ref, after=after, before=before
        )


class Network:
    def list(self, **args):
        return {"method": "list", "args": args}

    def detail(self, observation_id, **args):
        return {"method": "detail", "id": observation_id, "args": args}

    def context(self, observation_id, **args):
        return {"method": "context", "id": observation_id, "args": args}

    def activity(self, observation_id, **args):
        return {"method": "activity", "id": observation_id, "args": args}


@pytest.fixture
def api():
    api = ObserverAPI(Store(), lambda: TOKEN, network_observations=Network())
    yield api
    api.close()


def request(api, path, method="GET", auth=None):
    return api.request(method, path, auth if auth is not None else "Bearer " + TOKEN)


def raises(code):
    def call(*args, **kwargs):
        raise NodeProtocolError(code, code)

    return call


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/v1/health", {"ok": True}),
        ("/v1/tasks?state=active", {"state": "active", "offset": 0}),
        ("/v1/tasks?state=recent&offset=000123", {"state": "recent", "offset": 123}),
        ("/v1/tasks/task_A", {"task_ref": "task_A"}),
        ("/v1/tasks/task_A/events?after=a%2Bb", {"task_ref": "task_A", "after": "a+b"}),
        (
            "/v1/tasks/task_A/activity?execution_ref=exec_A&before=checkpoint",
            dict(
                task_ref="task_A",
                execution_ref="exec_A",
                after=None,
                before="checkpoint",
            ),
        ),
        (
            "/v2/observations?node=mac&limit=%2B1_0",
            {"method": "list", "args": {"node": "mac", "limit": 10}},
        ),
        ("/v2/observations?limit=１２", {"method": "list", "args": {"limit": 12}}),
        (
            "/v2/observations/" + OID + "?limit=4",
            {"method": "detail", "id": OID, "args": {"limit": 4}},
        ),
        (
            "/v2/observations/" + OID + "/context?cursor=x",
            {"method": "context", "id": OID, "args": {"cursor": "x"}},
        ),
        (
            "/v2/observations/" + OID + "/activity?limit=8",
            {"method": "activity", "id": OID, "args": {"limit": 8}},
        ),
    ],
)
def test_routes_and_exact_callback_arguments(api, path, expected):
    assert request(api, path) == expected


@pytest.mark.parametrize(
    "path,status,code",
    [
        ("/v1/tasks", 400, "INVALID_QUERY"),
        ("/v1/tasks?state=all", 400, "INVALID_QUERY"),
        ("/v1/tasks?state=active&offset=-1", 400, "INVALID_QUERY"),
        ("/v1/tasks?state=active&offset=100001", 400, "INVALID_QUERY"),
        ("/v1/tasks?state=active&offset=0000001", 400, "INVALID_QUERY"),
        ("/v1/tasks?state=active&offset=１２", 400, "INVALID_QUERY"),
        ("/v1/health?x", 400, "INVALID_QUERY"),
        ("/v1/health?x=1&x=2", 400, "INVALID_QUERY"),
        ("/v1/health?x=1&%78=2", 400, "INVALID_QUERY"),
        ("/v1/health?" + "&".join(f"x{i}=1" for i in range(9)), 400, "INVALID_QUERY"),
        ("/v1/health#fragment", 400, "INVALID_REQUEST"),
        ("https://foreign/v1/health", 400, "INVALID_REQUEST"),
        ("//foreign/v1/health", 400, "INVALID_REQUEST"),
        ("/v1/%68ealth", 400, "INVALID_REQUEST"),
        (
            "/v1/tasks/task_A/activity?execution_ref=e&after=x&before=y",
            400,
            "INVALID_QUERY",
        ),
        ("/v1/tasks/task_A/activity?execution_ref=e&after=", 400, "INVALID_QUERY"),
        ("/v1/tasks/task_A/activity?execution_ref=", 400, "INVALID_QUERY"),
        ("/v2/observations?limit=1__2", 400, "INVALID_QUERY"),
        ("/v2/observations?limit=1.5", 400, "INVALID_QUERY"),
        ("/v2/observations?limit=²", 400, "INVALID_QUERY"),
        ("/v2/observations/" + OID + "/context?limit=2", 400, "INVALID_QUERY"),
        ("/v2/native/" + TID + "?context=true", 400, "INVALID_QUERY"),
        ("/v1/health?extra=1", 404, "NOT_FOUND"),
        ("/does-not-exist", 404, "NOT_FOUND"),
        ("/" + "x" * 2048, 400, "INVALID_REQUEST"),
    ],
)
def test_invalid_route_query_bounds(api, path, status, code):
    with pytest.raises(APIError) as error:
        request(api, path)
    assert (error.value.status, error.value.code) == (status, code)


def test_native_collector_and_errors_use_exact_rust_route(api, monkeypatch):
    import clinx.authority.rust_reads as reads

    captured = []
    monkeypatch.setattr(
        reads,
        "observe_desktop",
        lambda directory, tid, **args: (
            captured.append((directory, tid, args)) or {"native": True}
        ),
    )
    assert request(
        api,
        "/v2/native/" + TID + "?context=1&recent_turns=2&max_bytes=300&cursor=exact",
    ) == {"native": True}
    assert captured == [
        (None, TID, dict(context=True, recent_turns=2, max_bytes=300, cursor="exact"))
    ]
    api.native_collector = SimpleNamespace(
        request=lambda route, query: {"route": route, "query": query}
    )
    assert request(api, "/v2/native-collector/sample/" + TID + "?checkpoint=exact") == {
        "route": "sample/" + TID,
        "query": {"checkpoint": "exact"},
    }
    api.native_collector.request = raises("NATIVE_COLLECTOR_CURSOR_RESET")
    with pytest.raises(APIError) as error:
        request(api, "/v2/native-collector/sample/" + TID)
    assert (error.value.status, error.value.code) == (
        409,
        "NATIVE_COLLECTOR_CURSOR_RESET",
    )
    api.network_observations.detail = raises("OBSERVATION_NOT_FOUND")
    with pytest.raises(APIError) as error:
        request(api, "/v2/observations/" + OID)
    assert (error.value.status, error.value.code) == (404, "OBSERVATION_NOT_FOUND")


def test_wide_integer_wire_preserves_value_at_source_boundary(api):
    seen = []
    api.network_observations.list = lambda **args: (
        seen.append(args["limit"]) or {"seen": True}
    )
    assert request(api, "/v2/observations?limit=" + "9" * 100) == {"seen": True}
    assert seen == [int("9" * 100)]


@contextlib.contextmanager
def listening(api, *, audit=False):
    server = ObserverHTTPServer(0, api, audit_methods=audit)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(2)
        assert not thread.is_alive()
        server.server_close()
        server.server_close()


def http(server, path="/v1/health", *, method="GET", headers=None, body=None):
    client = http_client.HTTPConnection(*server.server_address, timeout=3)
    try:
        client.request(
            method,
            path,
            headers=headers
            if headers is not None
            else {"Authorization": "Bearer " + TOKEN},
            body=body,
        )
        response = client.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        client.close()


def test_http_auth_rotation_readonly_redaction_response_bound_and_audit(api):
    output, errors = io.StringIO(), io.StringIO()
    with (
        contextlib.redirect_stdout(output),
        contextlib.redirect_stderr(errors),
        listening(api, audit=True) as server,
    ):
        api.store.health = lambda: {"secret": TOKEN, "text": "中文\x7f🙂"}
        status, headers, body = http(server)
        assert status == 200 and TOKEN.encode() not in body
        assert body.isascii() and b"\\u007f" in body
        assert json.loads(body) == {"secret": "[REDACTED]", "text": "中文\x7f🙂"}
        assert headers["Cache-Control"] == "no-store"
        assert headers["Connection"] == "close"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert int(headers["Content-Length"]) == len(body)
        assert http(server, headers={})[0] == 401
        assert http(server, headers={"Authorization": "Bearer wrong"})[0] == 401
        assert http(server, "/private-" + TOKEN, method="POST")[0] == 405
        assert http(server, method=TOKEN)[0] == 405
        status, headers, body = http(server, method="HEAD")
        assert status == 405 and headers["Allow"] == "GET" and body == b""
        api.credential = lambda: "new_" + TOKEN
        assert http(server)[0] == 401
        api.credential = lambda: TOKEN
        api.store.health = lambda: {"large": "x" * (2 * 1024 * 1024)}
        status, _, body = http(server)
        assert status == 503 and json.loads(body)["error"] == "RESPONSE_BOUND_EXCEEDED"
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [row["observer_http_method"] for row in rows] == [
        "GET",
        "GET",
        "GET",
        "POST",
        "OTHER",
        "HEAD",
        "GET",
        "GET",
    ]
    assert all(set(row) == {"observer_http_method", "status"} for row in rows)
    assert TOKEN not in output.getvalue() and errors.getvalue() == ""


def test_duplicate_auth_body_source_faults_and_auth_unavailable(api):
    with listening(api) as server:
        client = http_client.HTTPConnection(*server.server_address, timeout=3)
        client.putrequest("GET", "/v1/health")
        for _ in range(2):
            client.putheader("Authorization", "Bearer " + TOKEN)
        client.endheaders()
        response = client.getresponse()
        assert response.status == 401
        response.read()
        client.close()
        assert http(server, body="x")[0] == 400
        assert (
            http(
                server,
                headers={
                    "Authorization": "Bearer " + TOKEN,
                    "Transfer-Encoding": "chunked",
                },
            )[0]
            == 400
        )

        def unavailable():
            raise sqlite3.OperationalError("sensitive source")

        api.store.health = unavailable
        status, _, body = http(server)
        assert status == 503 and json.loads(body)["error"] == "SOURCE_UNAVAILABLE"
        api.store.health = lambda: 1 / 0
        status, _, body = http(server)
        assert status == 503 and json.loads(body)["error"] == "PROJECTION_UNAVAILABLE"
        api.credential = lambda: "invalid"
        status, _, body = http(server)
        assert status == 503 and json.loads(body)["error"] == "AUTH_UNAVAILABLE"


def test_listener_restart_close_and_new_socket_ownership(api):
    server = ObserverHTTPServer(0, api)
    assert server.server_address[0] == "127.0.0.1"
    for _ in range(2):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        assert http(server)[0] == 200
        server.shutdown()
        thread.join(2)
        assert not thread.is_alive()
    address = server.server_address
    server.server_close()
    with socket.socket() as replacement:
        replacement.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        replacement.bind(address)


def raw(server, wire):
    with socket.create_connection(server.server_address, timeout=3) as client:
        client.sendall(wire)
        data = b""
        while True:
            try:
                piece = client.recv(65536)
            except ConnectionResetError:
                return data
            if not piece:
                return data
            data += piece


@pytest.mark.parametrize(
    "wire,status",
    [
        (b"GET / HTTP/2.0\r\n\r\n", 505),
        (b"GET / HTTP/1.1\r\n" + b"X: a\r\n" * 100 + b"\r\n", 431),
        (b"GET / HTTP/1.1\r\nX: " + b"a" * 65536 + b"\r\n\r\n", 431),
        (b"GET /" + b"x" * 65536 + b" HTTP/1.1\r\n\r\n", 414),
    ],
)
def test_http_parser_bounds_and_version(api, wire, status):
    with listening(api) as server:
        response = raw(server, wire)
        assert response.startswith(f"HTTP/1.0 {status} ".encode())


def test_sixteen_concurrent_requests_bound_without_leaking_slots(api):
    entered, release = threading.Condition(), threading.Event()
    calls = []
    results = []

    def blocked():
        with entered:
            calls.append(True)
            entered.notify_all()
        assert release.wait(5)
        return {"ok": True}

    api.store.health = blocked
    with listening(api) as server:
        threads = [
            threading.Thread(target=lambda: results.append(http(server)[0]))
            for _ in range(16)
        ]
        for thread in threads:
            thread.start()
        try:
            with entered:
                assert entered.wait_for(lambda: len(calls) == 16, timeout=3)
            with socket.create_connection(server.server_address, timeout=3) as overflow:
                overflow.sendall(b"GET /v1/health HTTP/1.0\r\n\r\n")
                try:
                    assert overflow.recv(1) == b""
                except ConnectionResetError:
                    pass
            assert len(calls) == 16
        finally:
            release.set()
            for thread in threads:
                thread.join(4)
        assert results == [200] * 16
        assert http(server)[0] == 200
