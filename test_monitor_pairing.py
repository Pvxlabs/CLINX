"""Real production TLS/OPAQUE bootstrap qualification; no live credentials."""

import json

import pytest

from local_discovery.identity import DeviceError
from local_discovery.observer_bootstrap import ObserverBootstrap, validate_connection
from local_discovery.transport import DeviceServer, LanTransport
from test_local_discovery_production import make_node, candidate


@pytest.fixture
def bootstrap(tmp_path):
    token_file = tmp_path / "observer.env"
    token_file.write_text("CLINX_OBSERVER_TOKEN=" + "x" * 48 + "\n")
    token_file.chmod(0o600)
    config = tmp_path / "monitor.json"
    config.write_text(json.dumps({"share_read_only_observer": True,
                                 "endpoint": "https://p620.example.ts.net",
                                 "credential_file": str(token_file)}))
    config.chmod(0o600)
    return ObserverBootstrap(config)


def test_production_pair_bootstrap_reconnect_and_revoke(tmp_path, bootstrap):
    a, pa = make_node(tmp_path, "alpha")
    b, pb = make_node(tmp_path, "beta")
    client = LanTransport(a, pa)
    with DeviceServer(b, pb, address="127.0.0.1", observer_bootstrap=bootstrap) as server:
        target = candidate(b, server.port)
        with pytest.raises(DeviceError):
            client.observer_connection(target)
        session = client.pair(target, server.window.open())
        assert session.authority_granted is False
        assert session.capabilities == ()
        assert client.observer_connection(target) == bootstrap.connection()
        # Recreate client from persisted trust: no PIN and same authenticated endpoint.
        a2, pa2 = make_node(tmp_path, "alpha")
        assert LanTransport(a2, pa2).observer_connection(target) == bootstrap.connection()
        pb.revoke(a.public["node_id"])
        with pytest.raises((DeviceError, OSError)):
            client.observer_connection(target)


def test_pairing_without_host_opt_in_cannot_bootstrap(tmp_path):
    a, pa = make_node(tmp_path, "alpha")
    b, pb = make_node(tmp_path, "beta")
    with DeviceServer(b, pb, address="127.0.0.1") as server:
        target = candidate(b, server.port)
        client = LanTransport(a, pa)
        client.pair(target, server.window.open())
        with pytest.raises(DeviceError, match="REMOTE_REQUEST_REJECTED"):
            client.observer_connection(target)
        with pytest.raises(DeviceError, match="SECURITY_BLOCKER_DEV_ONLY"):
            LanTransport(a, pa, allow_dev=True).observer_connection(target)


@pytest.mark.parametrize("endpoint", ["http://192.168.1.5", "https://example.com",
    "https://user@p620.ts.net", "https://p620.ts.net/path", "https://p620.ts.net?x=y",
    "https://0.0.0.0", "https://p620.ts.net:0", "https://p620.ts.net:bad"])
def test_invalid_connection_rejected(endpoint):
    with pytest.raises(DeviceError, match="INVALID_OBSERVER_CONNECTION"):
        validate_connection({"endpoint": endpoint, "credential": "x" * 48})


def test_unsafe_and_disabled_local_configuration(bootstrap):
    bootstrap.config.chmod(0o644)
    with pytest.raises(DeviceError, match="UNSAFE_STATE_PERMISSIONS"):
        bootstrap.connection()
    bootstrap.config.chmod(0o600)
    raw = json.loads(bootstrap.config.read_text())
    raw["share_read_only_observer"] = False
    bootstrap.config.write_text(json.dumps(raw))
    with pytest.raises(DeviceError, match="OBSERVER_SHARING_DISABLED"):
        bootstrap.connection()
