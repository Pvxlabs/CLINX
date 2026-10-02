"""Isolated simulations plus real loopback TLS; no shared CLINX daemon."""

from __future__ import annotations

import json
import os
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

pytest.importorskip("spake2", reason="optional discovery dependencies not installed")
pytest.importorskip("zeroconf", reason="optional discovery dependencies not installed")

from execution_policy import build_execution_policy
from execution_semantics import normalize_host
from local_discovery import SERVICE_TYPE
from local_discovery.cli import handle
from local_discovery.discovery import Candidate, DiscoveryIndex, LanDiscovery, device_rows
from local_discovery.identity import DeviceError, NodeIdentity, PrivateStore, TrustedPeerStore
from local_discovery.pairing import PairingWindow, Spake2Protocol, confirmation, verify_confirmation
from local_discovery.transport import (
    DeviceServer,
    LanTransport,
    receive,
    reconnect_discovered,
    send,
)


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


def properties(node):
    return {
        k.encode(): str(node.public[k]).encode()
        for k in ("node_id", "display_name", "protocol_version", "fingerprint")
    }


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
def connected(nodes):
    (a, pa), (b, pb) = nodes
    clock = Clock()
    with DeviceServer(
        b, pb, address="127.0.0.1", allow_dev=True, window=PairingWindow(clock)
    ) as server:
        c = candidate(b, server.port)
        client = LanTransport(a, pa, allow_dev=True)
        yield a, pa, b, pb, server, c, client, clock


def test_identity_reuses_routing_host_and_restart(tmp_path):
    store = PrivateStore(tmp_path / "node")
    node = NodeIdentity(store, "workstation-p620", "P620 Desk")
    restarted = NodeIdentity(store, "p620")
    assert restarted.public == node.public
    assert (
        node.host_identity.stable_identifier == normalize_host("workstation-p620").stable_identifier
    )
    assert node.host_identity.machine_id == node.public["fingerprint"]
    assert (store.root / "identity.json").stat().st_mode & 0o777 == 0o600
    assert store.root.stat().st_mode & 0o777 == 0o700
    with pytest.raises(DeviceError, match="CONFIGURED_HOST_IDENTITY_CHANGED"):
        NodeIdentity(store, "different-host")


@pytest.mark.parametrize("host", ["192.168.1.2", "10.0.0.1", "unknown", "::1"])
def test_addresses_and_unknown_are_not_node_identity(tmp_path, host):
    with pytest.raises(ValueError):
        NodeIdentity(PrivateStore(tmp_path / "state"), host)


def test_two_nodes_multiple_addresses_ttl_and_ip_change(nodes):
    (a, _), (b, _) = nodes
    clock = Clock()
    index = DiscoveryIndex(clock)
    index.update("a-1", properties(a), ["10.0.0.1"], 9000, 30)
    index.update("a-2", properties(a), ["192.168.1.2"], 9000, 20)
    index.update("b", properties(b), ["10.0.0.2"], 9001, 30)
    assert len(index.candidates()) == 2
    assert len(index.candidates()["alpha"].endpoints) == 2
    index.update("a-1", properties(a), ["10.0.0.9"], 9000, 30)
    assert ("10.0.0.1", 9000) not in index.candidates()["alpha"].endpoints
    clock.tick(21)
    assert index.candidates()["alpha"].endpoints == (("10.0.0.9", 9000),)
    clock.tick(10)
    assert index.candidates() == {}


def test_goodbye_and_conflicting_advertisements(nodes):
    (a, _), (b, _) = nodes
    index = DiscoveryIndex()
    index.update("a", properties(a), ["10.0.0.1"], 1234, 30)
    conflicting = properties(a) | {b"fingerprint": b.public["fingerprint"].encode()}
    index.update("imposter", conflicting, ["10.0.0.2"], 1234, 30)
    assert index.candidates()["alpha"].mismatch
    index.remove("imposter")
    assert not index.candidates()["alpha"].mismatch
    index.update("a", properties(a), ["10.0.0.1"], 1234, 0)
    assert not index.candidates()


@pytest.mark.parametrize("field", [b"token", b"pin", b"password", b"endpoint", b"authority"])
def test_txt_rejects_non_allowlisted_metadata(nodes, field):
    (a, _), _ = nodes
    with pytest.raises(DeviceError, match="INVALID_DISCOVERY_METADATA"):
        DiscoveryIndex().update("a", properties(a) | {field: b"x"}, ["10.0.0.1"], 1234, 30)


def test_public_endpoint_rejected(nodes):
    (a, _), _ = nodes
    with pytest.raises(DeviceError, match="NON_LAN_ADDRESS"):
        DiscoveryIndex().update("a", properties(a), ["8.8.8.8"], 1234, 30)


def test_all_device_states(nodes, tmp_path):
    (a, pa), (b, _) = nodes
    c, _ = make_node(tmp_path, "gamma")
    pa.trust(b.public, b.certificate, security_status="DEV_ONLY")
    index = DiscoveryIndex()
    assert [r["state"] for r in device_rows(a, pa, index)] == ["This Device", "Offline Trusted"]
    index.update("b", properties(b), ["10.0.0.2"], 1234, 30)
    index.update("c", properties(c), ["10.0.0.3"], 1234, 30)
    assert {r["state"] for r in device_rows(a, pa, index)} == {
        "This Device",
        "Online Trusted",
        "Online Unpaired",
    }
    index.update(
        "b",
        properties(b) | {b"fingerprint": a.public["fingerprint"].encode()},
        ["10.0.0.2"],
        1234,
        30,
    )
    assert "Identity Mismatch" in {r["state"] for r in device_rows(a, pa, index)}


def test_pake_default_hard_gate(nodes):
    (a, _), (b, _) = nodes
    with pytest.raises(DeviceError, match="SECURITY_BLOCKER_DEV_ONLY"):
        Spake2Protocol("1234", role="A", client=a.public, server=b.public, window_id="abc")


@pytest.mark.parametrize(
    "change", ["wrong_pin", "different_window", "different_key", "different_node"]
)
def test_real_pake_rejects_transcript_and_pin_mismatch(nodes, change):
    (a, _), (b, _) = nodes
    server = dict(b.public)
    if change == "different_key":
        server["public_key"] = a.public["public_key"]
    if change == "different_node":
        server["node_id"] = "imposter"
    aa = Spake2Protocol(
        "1234",
        role="A",
        client=a.public,
        server=server,
        window_id="other" if change == "different_window" else "one",
        allow_dev=True,
    )
    bb = Spake2Protocol(
        "4321" if change == "wrong_pin" else "1234",
        role="B",
        client=a.public,
        server=b.public,
        window_id="one",
        allow_dev=True,
    )
    ma, mb = aa.start(), bb.start()
    ka, kb = aa.finish(mb), bb.finish(ma)
    with pytest.raises(DeviceError, match="PAIRING_FAILED"):
        verify_confirmation(kb, "A", confirmation(ka, "A"))


def test_policy_exact_60_seconds_max_failures_and_replay():
    clock = Clock()
    window = PairingWindow(clock)
    code = window.open()
    assert len(code) == 4 and code.isdecimal()
    for i in range(3):
        token, _, _ = window.reserve()
        window.complete(token, success=False)
        clock.tick(1)
    with pytest.raises(DeviceError, match="PAIRING_CLOSED"):
        window.reserve()
    assert window.failures == 3
    with pytest.raises(DeviceError, match="PAIRING_RATE_LIMITED"):
        window.open()
    clock.tick(60)
    window.open()
    token, _, _ = window.reserve()
    window.complete(token, success=True)
    with pytest.raises(DeviceError, match="PAIRING_REPLAY"):
        window.complete(token, success=True)
    with pytest.raises(DeviceError, match="PAIRING_CLOSED"):
        window.reserve()


def test_expiry_before_commit_and_pending_reservation():
    clock = Clock()
    window = PairingWindow(clock)
    window.open()
    token, _, _ = window.reserve()
    clock.tick(60)
    commits = []
    with pytest.raises(DeviceError, match="PAIRING_CLOSED"):
        window.complete(token, success=True, commit=lambda: commits.append(True))
    assert not commits


def test_rate_limit_and_concurrent_reservation():
    clock = Clock()
    window = PairingWindow(clock)
    window.open()

    def reserve():
        try:
            return window.reserve()[0]
        except DeviceError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: reserve(), range(8)))
    tokens = [r for r in outcomes if r != "PAIRING_BUSY"]
    assert len(tokens) == 1 and len(outcomes) == 8
    window.complete(tokens[0], success=False)
    with pytest.raises(DeviceError, match="PAIRING_RATE_LIMITED"):
        window.reserve()


def test_pairing_mutual_tls_persistence_reconnect_and_authority(connected):
    a, pa, b, pb, server, c, client, _ = connected
    policy = build_execution_policy()
    before = policy.as_dict()
    code = server.window.open()
    session = client.pair(c, code)
    assert not session.authority_granted and not session.capabilities
    assert session.authorization_required
    assert policy.as_dict() == before
    for operation in (
        "READ_ONLY_HOST",
        "DEVELOPMENT_MUTATION",
        "PRODUCTION_MUTATION",
        "BUSINESS_ACTION",
    ):
        for capability in ("GIT", "LOCAL_HOST_PROCESS", "HOST_FILESYSTEM"):
            assert not policy.permits(operation, capability)
    restarted = NodeIdentity(PrivateStore(a.store.root), "alpha")
    restored = TrustedPeerStore(restarted.store)
    assert restored.all()["beta"]["public_key"] == b.public["public_key"]
    assert pb.all()["alpha"]["trust_state"] == "TRUSTED"
    assert reconnect_discovered(restarted, restored, {"beta": c}, allow_dev=True) == {
        "beta": "AUTHENTICATED"
    }
    assert "private_key" not in (a.store.root / "peers.json").read_text()
    assert "pairing_secret" not in (a.store.root / "peers.json").read_text()
    assert "code" not in restored.all()["beta"]
    assert (a.store.root / "peers.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(DeviceError, match="SECURITY_BLOCKER_DEV_ONLY"):
        LanTransport(restarted, restored).reconnect(c)


def test_network_wrong_pin_limit_and_no_trust(connected):
    a, pa, b, pb, server, c, client, clock = connected
    correct = server.window.open()
    wrong = "0000" if correct != "0000" else "9999"
    for _ in range(3):
        with pytest.raises(DeviceError):
            client.pair(c, wrong)
        clock.tick(1)
    assert server.window.failures == 3
    with pytest.raises(DeviceError):
        client.pair(c, correct)
    assert pa.all() == pb.all() == {}


def test_network_expired_code_rejected(connected):
    _, pa, _, pb, server, c, client, clock = connected
    code = server.window.open()
    clock.tick(60)
    with pytest.raises(DeviceError):
        client.pair(c, code)
    assert pa.all() == pb.all() == {}


def test_network_success_consumes_code_and_replay_fails(connected):
    _, pa, _, pb, server, c, client, _ = connected
    code = server.window.open()
    client.pair(c, code)
    with pytest.raises(DeviceError):
        client.pair(c, code)
    assert len(pa.all()) == len(pb.all()) == 1


def test_identity_mismatch_advertisement_and_actual_tls_fail_closed(connected, tmp_path):
    _, pa, b, _, server, c, client, _ = connected
    client.pair(c, server.window.open())
    with pytest.raises(DeviceError, match="IDENTITY_MISMATCH"):
        client.reconnect(replace(c, fingerprint="0" * 64))
    imposter = NodeIdentity(PrivateStore(tmp_path / "imposter"), "beta")
    with DeviceServer(
        imposter, TrustedPeerStore(imposter.store), address="127.0.0.1", allow_dev=True
    ) as evil:
        # Spoof mDNS with the old pinned key, but TLS presents a replacement key.
        with pytest.raises(ssl.SSLError):
            client.reconnect(replace(c, endpoints=(("127.0.0.1", evil.port),)))
    assert pa.all()["beta"]["public_key"] == b.public["public_key"]


def test_no_client_certificate_cannot_reconnect_or_execute(connected):
    _, _, _, pb, server, c, client, _ = connected
    client.pair(c, server.window.open())
    for request in (
        {"op": "session", "node_id": "alpha"},
        {"op": "host_execute", "capability": "GIT"},
        {"op": "terminal"},
        {"op": "production_mutation"},
    ):
        # Pairing-mode TLS sends no client cert, regardless of claimed node ID.
        with client._connect(c, pairing=True) as sock:
            send(sock, request)
            with pytest.raises(DeviceError):
                receive(sock)
    assert pb.all()["alpha"]["trust_state"] == "TRUSTED"


def test_authenticated_peer_cannot_dispatch_execution(connected):
    _, _, _, _, server, c, client, _ = connected
    client.pair(c, server.window.open())
    with client._connect(c, pairing=False) as sock:
        send(sock, {"op": "host_execute", "capability": "GIT", "operation": "status"})
        with pytest.raises(DeviceError):
            receive(sock)


def test_revocation_blocks_reconnect(connected):
    _, _, _, pb, server, c, client, _ = connected
    client.pair(c, server.window.open())
    pb.revoke("alpha")
    with pytest.raises((DeviceError, ssl.SSLError)):
        client.reconnect(c)


def test_duplicate_server_cannot_open_second_window(connected):
    _, _, b, pb, _, _, _, _ = connected
    with pytest.raises(DeviceError, match="DEVICE_ALREADY_SERVING"):
        DeviceServer(b, pb, address="127.0.0.1", allow_dev=True)


def test_store_parallel_writes_restart_and_schema_fail_closed(tmp_path):
    local, peers = make_node(tmp_path, "local")
    remotes = [make_node(tmp_path, f"peer{i}")[0] for i in range(8)]

    def trust(remote):
        TrustedPeerStore(PrivateStore(local.store.root)).trust(
            remote.public, remote.certificate, security_status="DEV_ONLY"
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(trust, remotes))
    assert len(TrustedPeerStore(PrivateStore(local.store.root)).all()) == 8
    raw = json.loads((local.store.root / "peers.json").read_text())
    raw["schema_version"] = 2
    (local.store.root / "peers.json").write_text(json.dumps(raw))
    with pytest.raises(DeviceError):
        peers.all()


def test_unrestricted_state_and_symlink_rejected(tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir(mode=0o755)
    with pytest.raises(DeviceError, match="UNSAFE_STATE_PERMISSIONS"):
        PrivateStore(bad)
    good = tmp_path / "good"
    good.mkdir(mode=0o700)
    symlink = tmp_path / "link"
    symlink.symlink_to(good, target_is_directory=True)
    with pytest.raises(DeviceError, match="UNSAFE_STATE_DIRECTORY"):
        PrivateStore(symlink)


def test_legacy_manual_paths_and_parser_remain_compatible():
    from bridge import build_parser, resolve_transport

    assert resolve_transport("workstation-p620", "p620", "ssh") == "local"
    assert resolve_transport("air", "p620", "ssh") == "ssh"
    with pytest.raises(Exception):
        resolve_transport("air", "p620", "unsupported")
    parser = build_parser()
    for command in ("doctor", "run", "once", "self-project-check"):
        assert parser.parse_args([command]).command == command
    assert parser.parse_args(["tasks", "list", "--host", "p620"]).host == "p620"


def test_cli_devices_output_and_default_pairing_gate(nodes, monkeypatch, capsys, tmp_path):
    import local_discovery.discovery as discovery
    from bridge import build_parser

    (a, _), (b, _) = nodes

    class Browse:
        def __init__(self, index):
            index.update("b", properties(b), ["10.0.0.2"], 1234, 30)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(discovery, "LanDiscovery", Browse)
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('[runtime]\nruntime_host = "alpha"\n')
    args = build_parser().parse_args(
        ["--config", str(cfg), "devices", "--state-dir", str(a.store.root), "--wait", "0", "--json"]
    )
    assert handle(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert {r["state"] for r in output["devices"]} == {"This Device", "Online Unpaired"}
    args = build_parser().parse_args(["pair", "accept"])
    assert handle(args) == 2
    assert "PAIRING_REQUIRES_LOCAL_TTY" in capsys.readouterr().err


@pytest.mark.skipif(os.environ.get("CLINX_LAN_SELFTEST") != "1", reason="opt-in local multicast")
def test_real_p620_advertise_browse(tmp_path):
    # Two independent zeroconf sockets, real multicast, temporary node IDs.
    # Same-machine test only; this does not qualify AIR/iMac.
    a, _ = make_node(tmp_path, "clinx-test-" + os.urandom(4).hex())
    index = DiscoveryIndex()
    with LanDiscovery(DiscoveryIndex()) as advertiser, LanDiscovery(index):
        advertiser.advertise(a, 34567)
        deadline = time.monotonic() + 8
        while a.public["node_id"] not in index.candidates() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert a.public["node_id"] in index.candidates()
        assert SERVICE_TYPE == "_clinx._tcp.local."
        advertiser.zc.unregister_service(advertiser.advertisement)
        advertiser.advertisement = None
        deadline = time.monotonic() + 5
        while a.public["node_id"] in index.candidates() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert a.public["node_id"] not in index.candidates()


def test_server_restart_address_change_and_no_pin(nodes):
    (a, pa), (b, pb) = nodes
    client = LanTransport(a, pa, allow_dev=True)
    with DeviceServer(b, pb, address="127.0.0.1", allow_dev=True) as server:
        client.pair(candidate(b, server.port), server.window.open())
    # Both server objects and stores are reconstructed from disk. A new endpoint
    # (including a different loopback address) does not change node identity.
    restored = NodeIdentity(PrivateStore(b.store.root), "beta")
    with DeviceServer(
        restored, TrustedPeerStore(restored.store), address="127.0.0.2", allow_dev=True
    ) as server:
        c = replace(candidate(restored, server.port), endpoints=(("127.0.0.2", server.port),))
        assert client.reconnect(c).node_id == "beta"
        with pytest.raises(DeviceError, match="PAIRING_CLOSED"):
            server.window.reserve()


def test_disconnected_pairing_attempt_consumes_failure(connected):
    a, _, _, pb, server, c, client, _ = connected
    server.window.open()
    with client._connect(c, pairing=True) as sock:
        send(
            sock,
            dict(
                op="pair",
                protocol="DEV-SPAKE2-PYTHON-v1",
                identity=a.public,
                certificate=a.certificate,
            ),
        )
        assert "message" in receive(sock)
    deadline = time.monotonic() + 2
    while server.window.failures == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.window.failures == 1
    assert not pb.all()


def test_failure_during_atomic_trust_commit_consumes_secret():
    clock = Clock()
    window = PairingWindow(clock)
    window.open()
    token, _, _ = window.reserve()

    def disk_failure():
        raise OSError("fixture")

    with pytest.raises(OSError):
        window.complete(token, success=True, commit=disk_failure)
    with pytest.raises(DeviceError, match="PAIRING_CLOSED"):
        window.reserve()


def test_trust_rotation_and_duplicate_key_binding_are_rejected(nodes, tmp_path):
    _, (b, peers) = nodes
    a, _ = make_node(tmp_path, "third")
    peers.trust(a.public, a.certificate, security_status="DEV_ONLY")
    with pytest.raises(DeviceError, match="IDENTITY_MISMATCH"):
        peers.trust(
            dict(a.public, public_key=b.public["public_key"], fingerprint=b.public["fingerprint"]),
            b.certificate,
            security_status="DEV_ONLY",
        )
    with pytest.raises(DeviceError, match="PUBLIC_KEY_ALREADY_BOUND"):
        peers.trust(
            dict(a.public, node_id="another-node"), a.certificate, security_status="DEV_ONLY"
        )


def test_offline_cli_still_reports_persisted_devices(nodes, monkeypatch):
    from local_discovery import discovery

    (a, peers), (b, _) = nodes
    peers.trust(b.public, b.certificate, security_status="DEV_ONLY")
    monkeypatch.setattr(discovery, "local_addresses", lambda: [])
    index = DiscoveryIndex()
    with LanDiscovery(index) as browser:
        assert browser.zc is None
        assert [r["state"] for r in device_rows(a, peers, index)] == [
            "This Device",
            "Offline Trusted",
        ]


def test_default_interfaces_exclude_virtual_networks(monkeypatch):
    from types import SimpleNamespace

    import ifaddr

    from local_discovery.discovery import local_addresses

    adapters = [
        SimpleNamespace(name=n, ips=[SimpleNamespace(ip=ip)])
        for n, ip in [
            ("enp1s0", "10.1.1.2"),
            ("docker0", "172.17.0.1"),
            ("br-abcd", "10.2.0.1"),
            ("tailscale0", "100.64.0.1"),
            ("lo", "127.0.0.1"),
        ]
    ]
    monkeypatch.setattr(ifaddr, "get_adapters", lambda: adapters)
    assert local_addresses() == ["10.1.1.2"]


def test_zeroconf_refresh_uses_remaining_ttl_not_original_ttl(nodes):
    import threading
    from types import SimpleNamespace

    (a, _), _ = nodes
    clock = Clock()
    index = DiscoveryIndex(clock)

    class Record:
        def __init__(self, type_, remaining=30, **values):
            self.type = type_
            self.remaining = remaining
            self.__dict__.update(values)

        def is_expired(self, now):
            return self.remaining <= 0

        def get_remaining_ttl(self, now):
            return self.remaining

    name = "alpha." + SERVICE_TYPE
    pointer = Record(12, alias=name)
    srv, txt = Record(33), Record(16)
    address = Record(1, address=b"\x0a\x00\x00\x01")
    cache = {SERVICE_TYPE: [pointer], name: [srv, txt], "alpha.local.": [address]}
    browser = LanDiscovery.__new__(LanDiscovery)
    browser.index = index
    browser._service_lock = threading.Lock()
    browser._services = {
        name: SimpleNamespace(properties=properties(a), server="alpha.local.", port=9000)
    }
    browser.zc = SimpleNamespace(
        cache=SimpleNamespace(entries_with_name=lambda key: cache.get(key, []))
    )
    index.refresher = browser.refresh
    assert "alpha" in index.candidates()
    clock.tick(31)  # No changed-record callback: same records have been renewed.
    assert "alpha" in index.candidates()
    pointer.remaining = 0  # Lost peer: expired PTR cannot be kept alive by cached TXT.
    assert "alpha" not in index.candidates()


def test_cli_launcher_from_another_directory(tmp_path):
    import subprocess
    from pathlib import Path

    root = Path(__file__).parent
    cfg = tmp_path / "cli.toml"
    cfg.write_text('[runtime]\nruntime_host = "cli-node"\n')
    state = tmp_path / "cli-state"
    env = dict(os.environ, PYTHONPATH=str(root) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    result = subprocess.run(
        [
            str(root / "bin/clinx"),
            "--config",
            str(cfg),
            "devices",
            "--state-dir",
            str(state),
            "--wait",
            "0",
            "--json",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["devices"][0]["state"] == "This Device"


@pytest.mark.skipif(os.environ.get("CLINX_LAN_SELFTEST") != "1", reason="opt-in local multicast")
def test_real_mdns_two_nodes_pair_and_reconnect(tmp_path):
    suffix = os.urandom(3).hex()
    a, pa = make_node(tmp_path, "clinx-a-" + suffix)
    b, pb = make_node(tmp_path, "clinx-b-" + suffix)
    ia, ib = DiscoveryIndex(), DiscoveryIndex()
    with DeviceServer(a, pa, allow_dev=True) as sa, DeviceServer(b, pb, allow_dev=True) as sb:
        with LanDiscovery(ia) as da, LanDiscovery(ib) as db:
            da.advertise(a, sa.port)
            db.advertise(b, sb.port)
            deadline = time.monotonic() + 8
            while (
                b.public["node_id"] not in ia.candidates()
                or a.public["node_id"] not in ib.candidates()
            ) and time.monotonic() < deadline:
                time.sleep(0.1)
            c = ia.candidates()[b.public["node_id"]]
            assert (
                LanTransport(a, pa, allow_dev=True).pair(c, sb.window.open()).node_id
                == b.public["node_id"]
            )
            assert (
                reconnect_discovered(a, pa, ia.candidates(), allow_dev=True)[b.public["node_id"]]
                == "AUTHENTICATED"
            )
            assert (
                reconnect_discovered(b, pb, ib.candidates(), allow_dev=True)[a.public["node_id"]]
                == "AUTHENTICATED"
            )
