"""Production OPAQUE, window policy and TLS binding regression tests."""

from __future__ import annotations

import secrets
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from local_discovery.discovery import Candidate
from local_discovery.identity import DeviceError, NodeIdentity, PrivateStore, TrustedPeerStore
from local_discovery.pairing import (
    DEV_PROTOCOL,
    PRODUCTION_PROTOCOL,
    PRODUCTION_TRUST,
    OpaqueClientProtocol,
    OpaqueServerProtocol,
    PairingWindow,
    pairing_binding,
    production_backend,
)
from local_discovery.transport import DeviceServer, LanTransport, receive, send


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def tick(self, seconds):
        self.value += seconds


def make_node(root, name):
    node = NodeIdentity(PrivateStore(root / name), name)
    return node, TrustedPeerStore(node.store)


def candidate(node, port):
    return Candidate(
        node.public["node_id"],
        node.public["display_name"],
        node.public["fingerprint"],
        (("127.0.0.1", port),),
    )


@pytest.fixture
def nodes(tmp_path):
    return make_node(tmp_path, "alpha"), make_node(tmp_path, "beta")


@pytest.fixture
def connection(nodes):
    (a, pa), (b, pb) = nodes
    clock = Clock()
    with DeviceServer(b, pb, address="127.0.0.1", window=PairingWindow(clock)) as server:
        yield a, pa, b, pb, server, candidate(b, server.port), LanTransport(a, pa), clock


def binding(nodes, **changes):
    (a, _), (b, _) = nodes
    fields = dict(
        client=a.public,
        server=b.public,
        window_id=secrets.token_hex(16),
        session_id=secrets.token_hex(16),
        client_nonce=secrets.token_hex(16),
    )
    fields.update(changes)
    return pairing_binding(**fields)


def exchanges(nodes):
    code = f"{secrets.randbelow(10000):04d}"
    context = binding(nodes)
    a = OpaqueClientProtocol(code, context)
    ke1 = a.start()
    b = OpaqueServerProtocol(code, context, ke1)
    return a, b, ke1, b.start()


def test_production_opaque_built_in_mutual_confirmation_and_one_shot(nodes):
    a, b, _, ke2 = exchanges(nodes)
    ke3 = a.finish(ke2)
    b.finish(ke3)
    assert a.security_status == b.security_status == PRODUCTION_TRUST
    with pytest.raises(DeviceError):
        a.finish(ke2)
    with pytest.raises(DeviceError):
        b.finish(ke3)


@pytest.mark.parametrize(
    "change", ["window", "session", "nonce", "node", "key", "roles", "protocol", "version"]
)
def test_context_and_identity_mismatch_rejected(nodes, change):
    (a, _), (b, _) = nodes
    code = f"{secrets.randbelow(10000):04d}"
    fields = dict(
        client=a.public,
        server=b.public,
        window_id=secrets.token_hex(16),
        session_id=secrets.token_hex(16),
        client_nonce=secrets.token_hex(16),
    )
    client_binding = pairing_binding(**fields)
    if change in {"window", "session", "nonce"}:
        fields[
            {"window": "window_id", "session": "session_id", "nonce": "client_nonce"}[change]
        ] = secrets.token_hex(16)
    elif change == "node":
        fields["client"] = dict(a.public, node_id="another")
    elif change == "version":
        fields["client"] = dict(a.public, protocol_version="unknown")
        with pytest.raises(DeviceError):
            pairing_binding(**fields)
        return
    elif change == "key":
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from local_discovery.identity import encoded, fingerprint

        replacement = encoded(
            Ed25519PrivateKey.generate()
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )
        fields["client"] = dict(
            a.public, public_key=replacement, fingerprint=fingerprint(replacement)
        )
    elif change == "roles":
        fields["client"], fields["server"] = fields["server"], fields["client"]
    server_binding = pairing_binding(**fields)
    if change == "protocol":
        server_binding = (server_binding[0].replace(b"OPAQUE-", b"UNKNOWN-"), *server_binding[1:])
    client = OpaqueClientProtocol(code, client_binding)
    server = OpaqueServerProtocol(code, server_binding, client.start())
    with pytest.raises(DeviceError, match="PAIRING_FAILED"):
        client.finish(server.start())


def test_reflected_oprf_value_rejected(nodes):
    a, b, ke1, ke2 = exchanges(nodes)
    # RFC 9807 ristretto255 first 32 bytes: blinded/evaluated OPRF element.
    with pytest.raises(DeviceError, match="PAIRING_FAILED"):
        a.finish(ke1[:32] + ke2[32:])
    with pytest.raises(DeviceError):
        b.finish(ke2)  # A KE2 cannot be reflected into the KE3 role.


def test_cross_exchange_replay_and_bad_client_mac(nodes):
    a, b, _, ke2 = exchanges(nodes)
    a2, b2, _, ke22 = exchanges(nodes)
    ke3 = a.finish(ke2)
    with pytest.raises(DeviceError):
        b2.finish(ke3)
    with pytest.raises(DeviceError):
        a2.finish(ke2)
    damaged = ke3[:-1] + bytes([ke3[-1] ^ 1])
    with pytest.raises(DeviceError):
        b.finish(damaged)
    assert ke22 != ke2


def test_wrong_pin_and_new_clients_share_entire_window_budget(connection):
    a, pa, _, pb, server, c, _, clock = connection
    code = server.window.open()
    wrong = str((int(code) + 1) % 10000).zfill(4)
    for attempt in range(3):
        # Each request has a new native state, TLS connection, client nonce and
        # server reservation. None of those reset the node's failure budget.
        with pytest.raises(DeviceError):
            LanTransport(a, pa).pair(c, wrong)
        deadline = time.monotonic() + 2
        while server.window.failures < attempt + 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.window.failures == attempt + 1
        clock.tick(1)
    with pytest.raises(DeviceError):
        LanTransport(a, pa).pair(c, code)
    assert not pa.all() and not pb.all()


def test_production_pair_persist_restart_reconnect_and_no_authority(connection):
    a, pa, b, pb, server, c, client, _ = connection
    session = client.pair(c, server.window.open())
    assert session.node_id == b.public["node_id"]
    assert not session.capabilities and not session.authority_granted
    assert session.authorization_required
    assert pa.all()["beta"]["security_status"] == PRODUCTION_TRUST
    assert pb.all()["alpha"]["pairing_protocol"] == PRODUCTION_PROTOCOL
    restored = NodeIdentity(PrivateStore(a.store.root), "alpha")
    peers = TrustedPeerStore(restored.store)
    assert LanTransport(restored, peers).reconnect(c) == session
    assert set(peers.all()["beta"]).isdisjoint({"code", "pin", "private_key", "session_key"})
    for op in [
        "host_execute",
        "shell",
        "ssh",
        "terminal",
        "repo_write",
        "production_mutation",
        "trade",
    ]:
        with client._connect(c, pairing=False) as sock:
            send(sock, dict(op=op))
            with pytest.raises(DeviceError):
                receive(sock)


def test_production_expired_window_and_reuse(connection):
    _, pa, _, pb, server, c, client, clock = connection
    code = server.window.open()
    clock.tick(60)
    with pytest.raises(DeviceError):
        client.pair(c, code)
    assert not pa.all() and not pb.all()
    code = server.window.open()
    client.pair(c, code)
    with pytest.raises(DeviceError):
        client.pair(c, code)


def test_production_rate_limit_applies_to_new_sessions(connection):
    a, _, _, _, server, c, client, _ = connection
    server.window.open()
    with client._connect(c, pairing=True) as sock:
        send(
            sock, dict(op="pair", protocol="unknown", identity=a.public, certificate=a.certificate)
        )
        with pytest.raises(DeviceError):
            receive(sock)
    assert server.window.failures == 1
    with pytest.raises(DeviceError, match="PAIRING_RATE_LIMITED"):
        server.window.reserve()


def test_node_budget_cannot_be_reset_by_changing_identity(connection, tmp_path):
    _, _, _, pb, server, c, _, clock = connection
    code = server.window.open()
    wrong = str((int(code) + 1) % 10000).zfill(4)
    for index in range(3):
        a, pa = make_node(tmp_path, f"attacker-{index}")
        with pytest.raises(DeviceError):
            LanTransport(a, pa).pair(c, wrong)
        deadline = time.monotonic() + 2
        while server.window.failures <= index and time.monotonic() < deadline:
            time.sleep(0.01)
        clock.tick(1)
    assert server.window.failures == 3 and not pb.all()
    with pytest.raises(DeviceError, match="PAIRING_CLOSED"):
        server.window.reserve()


def test_atomic_concurrent_confirmation_single_commit():
    window = PairingWindow()
    window.open()
    token, _, _ = window.reserve()
    commits = []

    def complete(_):
        try:
            window.complete(token, success=True, commit=lambda: commits.append(True))
            return "accepted"
        except DeviceError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(complete, range(8)))
    assert outcomes.count("accepted") == len(commits) == 1
    window.close()


def test_idle_window_drops_pin_reference_without_network_activity():
    window = PairingWindow()
    window.lifetime = 0.02
    window.open()
    deadline = time.monotonic() + 1
    while window._code is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert window._code is None and window._id is None
    window.close()


def test_dev_trust_requires_fresh_production_pairing(connection):
    a, pa, b, pb, server, c, client, _ = connection
    pa.trust(b.public, b.certificate, security_status="DEV_ONLY")
    pb.trust(a.public, a.certificate, security_status="DEV_ONLY")
    with pytest.raises(DeviceError, match="SECURITY_BLOCKER_DEV_ONLY"):
        client.reconnect(c)
    with pytest.raises(DeviceError, match="FRESH_PRODUCTION_PAIRING_REQUIRED"):
        pa.trust(
            b.public,
            b.certificate,
            security_status=PRODUCTION_TRUST,
            pairing_protocol=PRODUCTION_PROTOCOL,
        )
    assert pa.all()["beta"]["security_status"] == "DEV_ONLY"
    client.pair(c, server.window.open())
    assert pa.all()["beta"]["security_status"] == PRODUCTION_TRUST
    with pytest.raises(DeviceError, match="TRUST_DOWNGRADE_REJECTED"):
        pa.trust(b.public, b.certificate, security_status="DEV_ONLY")


@pytest.mark.parametrize("protocol", [DEV_PROTOCOL, "unknown", None])
def test_production_rejects_protocol_downgrade(connection, protocol):
    a, pa, _, pb, server, c, client, _ = connection
    server.window.open()
    with client._connect(c, pairing=True) as sock:
        send(sock, dict(op="pair", protocol=protocol, identity=a.public, certificate=a.certificate))
        with pytest.raises(DeviceError):
            receive(sock)
    assert not pa.all() and not pb.all()


def test_missing_backend_fails_closed_before_network(connection, monkeypatch):
    _, pa, _, pb, server, c, client, _ = connection
    code = server.window.open()
    monkeypatch.setitem(sys.modules, "_clinx_opaque", None)
    with pytest.raises(DeviceError, match="PRODUCTION_PAKE_UNAVAILABLE"):
        client.pair(c, code)
    assert server.window.failures == 0 and not pa.all() and not pb.all()


def test_unknown_backend_version_fails_closed(monkeypatch):
    import types

    monkeypatch.setitem(
        sys.modules,
        "_clinx_opaque",
        types.SimpleNamespace(BACKEND_ID=PRODUCTION_PROTOCOL, OPAQUE_KE_VERSION="0"),
    )
    with pytest.raises(DeviceError, match="VERSION_MISMATCH"):
        production_backend()


def test_actual_tls_key_substitution_and_wrong_client_rejected(connection, tmp_path):
    a, pa, b, _, server, c, client, _ = connection
    client.pair(c, server.window.open())
    imposter, pi = make_node(tmp_path, "imposter")
    with DeviceServer(imposter, pi, address="127.0.0.1") as evil:
        with pytest.raises(ssl.SSLError):
            client.reconnect(replace(c, endpoints=(("127.0.0.1", evil.port),)))
    # Locally copying the public server trust cannot authorize this new client key.
    pi.trust(
        b.public,
        b.certificate,
        security_status=PRODUCTION_TRUST,
        pairing_protocol=PRODUCTION_PROTOCOL,
        fresh_pairing=True,
    )
    with pytest.raises((ssl.SSLError, DeviceError, BrokenPipeError)):
        LanTransport(imposter, pi).reconnect(c)
    assert pa.all()["beta"]["public_key"] == b.public["public_key"]


def test_client_identity_certificate_mismatch_not_persisted(connection):
    a, pa, b, pb, server, c, client, _ = connection
    server.window.open()
    with client._connect(c, pairing=True) as sock:
        send(
            sock,
            dict(
                op="pair",
                protocol=PRODUCTION_PROTOCOL,
                client_nonce=secrets.token_hex(16),
                identity=a.public,
                certificate=b.certificate,
            ),
        )
        with pytest.raises(DeviceError):
            receive(sock)
    assert not pa.all() and not pb.all()


def test_production_pair_never_imports_dev_backend(connection, monkeypatch):
    import importlib.abc

    class BlockDev(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "spake2" or fullname.startswith("spake2."):
                raise AssertionError("production attempted development import")
            return None

    monkeypatch.setattr(sys, "meta_path", [BlockDev(), *sys.meta_path])
    _, _, _, _, server, c, client, _ = connection
    client.pair(c, server.window.open())


def test_concurrent_network_pairing_has_exactly_one_success(connection):
    a, pa, _, pb, server, c, _, _ = connection
    code = server.window.open()

    def attempt(_):
        try:
            LanTransport(a, pa).pair(c, code)
            return True
        except (DeviceError, ssl.SSLError, OSError):
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert sum(results) == 1
    assert len(pa.all()) == len(pb.all()) == 1


def test_unknown_persisted_protocol_and_legacy_qualified_fail_closed(connection):
    import json

    _, pa, _, _, server, c, client, _ = connection
    client.pair(c, server.window.open())
    path = pa.store.root / "peers.json"
    raw = json.loads(path.read_text())
    raw["peers"]["beta"]["pairing_protocol"] = "future-unknown"
    path.write_text(json.dumps(raw))
    with pytest.raises(DeviceError, match="INVALID_PEER_SECURITY_STATUS"):
        client.reconnect(c)
    raw["peers"]["beta"]["security_status"] = "QUALIFIED"
    path.write_text(json.dumps(raw))
    with pytest.raises(DeviceError, match="INVALID_PEER_SECURITY_STATUS"):
        pa.all()


def test_tls_credentials_create_no_staging_key_files(nodes):
    import ssl

    (identity, _), _ = nodes
    before = set(identity.store.root.iterdir())
    identity.load_tls_credentials(ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    assert set(identity.store.root.iterdir()) == before


def test_backend_status_does_not_claim_available_when_missing(monkeypatch):
    from local_discovery.pairing import backend_status

    monkeypatch.setitem(sys.modules, "_clinx_opaque", None)
    assert backend_status() == "BACKEND_UNAVAILABLE"
    assert backend_status(allow_dev=True) == "DEV_ONLY"
