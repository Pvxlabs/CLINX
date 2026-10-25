"""Python Observer bindings only; full protocol/IO matrix is Rust qualification."""

import contextlib
import json
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


@pytest.fixture
def api():
    api = ObserverAPI(SimpleNamespace(), lambda: TOKEN, network_observations=SimpleNamespace())
    yield api
    api.close()


def request(api, path, method="GET", auth=None):
    return api.request(method, path, auth if auth is not None else "Bearer " + TOKEN)


def raises(code):
    def call(*args, **kwargs):
        raise NodeProtocolError(code, code)

    return call


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
