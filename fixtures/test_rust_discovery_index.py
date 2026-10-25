"""Rust-owned LAN hints and Observer bootstrap; no multicast or live devices."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from clinx.discovery import SERVICE_TYPE
from clinx.discovery.discovery import (
    DiscoveryIndex,
    LanDiscovery,
    device_rows,
    lan_address,
    local_addresses,
)
from clinx.discovery.identity import DeviceError
from clinx.discovery.observer_bootstrap import ObserverBootstrap, validate_connection


def props(node="alpha", fingerprint="a" * 64):
    return {
        b"node_id": node.encode(),
        b"display_name": node.title().encode(),
        b"protocol_version": b"1",
        b"fingerprint": fingerprint.encode(),
    }


@pytest.mark.parametrize(
    "address,expected",
    [
        ("10.0.0.1", "10.0.0.1"),
        ("172.16.0.1", "172.16.0.1"),
        ("172.31.255.255", "172.31.255.255"),
        ("192.168.1.2", "192.168.1.2"),
        ("169.254.1.2", "169.254.1.2"),
        ("127.0.0.1", "127.0.0.1"),
        ("8.8.8.8", "NON_LAN_ADDRESS"),
        ("172.32.0.1", "NON_LAN_ADDRESS"),
        ("192.0.2.1", "NON_LAN_ADDRESS"),
        ("::1", "NON_LAN_ADDRESS"),
        ("fe80::1%eth0", "NON_LAN_ADDRESS"),
        ("0.0.0.0", "NON_LAN_ADDRESS"),
        ("010.0.0.1", "INVALID_LAN_ADDRESS"),
        ("bogus", "INVALID_LAN_ADDRESS"),
    ],
)
def test_lan_address_contract(address, expected):
    if expected.endswith("ADDRESS"):
        with pytest.raises(DeviceError, match=expected):
            lan_address(address)
    else:
        assert lan_address(address) == expected


def test_candidate_ttl_conflict_goodbye_ip_change_and_capacity():
    clock = [100.0]
    index = DiscoveryIndex(lambda: clock[0])
    index.update("a1", props(), ["10.0.0.1"], 9000, 30)
    index.update("a2", props(), ["192.168.1.2"], 9000, 20)
    assert index.candidates()["alpha"].endpoints == (
        ("10.0.0.1", 9000),
        ("192.168.1.2", 9000),
    )
    index.update("a1", props(), ["10.0.0.9"], 9000, 30)
    clock[0] += 21
    assert index.candidates()["alpha"].endpoints == (("10.0.0.9", 9000),)
    index.update("imposter", props(fingerprint="b" * 64), ["10.0.0.2"], 9000, 30)
    assert index.candidates()["alpha"].mismatch
    index.remove("imposter")
    assert not index.candidates()["alpha"].mismatch
    index.update("a1", props(), ["10.0.0.9"], 9000, 0)
    assert index.candidates() == {}
    for n in range(256):
        index.update(str(n), props("node" + str(n)), ["10.0.0.1"], 9000, 1000)
    with pytest.raises(DeviceError, match="DISCOVERY_CAPACITY"):
        index.update("overflow", props("overflow"), ["10.0.0.1"], 9000, 30)
    clock[0] += 121
    assert index.candidates() == {}


@pytest.mark.parametrize(
    "change,code",
    [
        ({b"password": b"never"}, "INVALID_DISCOVERY_METADATA"),
        ({b"display_name": b"x" * 201}, "INVALID_DISCOVERY_METADATA"),
        ({b"display_name": b"bad\nname"}, "INVALID_DISPLAY_NAME"),
        ({b"protocol_version": b"2"}, "UNSUPPORTED_DISCOVERY_PROTOCOL"),
        ({b"fingerprint": b"G" * 64}, "INVALID_DISCOVERY_FINGERPRINT"),
        ({b"node_id": b"10.0.0.1"}, "ADDRESS_IS_NOT_IDENTITY"),
        ({b"node_id": b"unknown"}, "NON_CANONICAL_NODE_ID"),
    ],
)
def test_metadata_has_no_implicit_authority_or_identity(change, code):
    with pytest.raises(DeviceError, match=code):
        DiscoveryIndex().update("fixture", props() | change, ["10.0.0.1"], 9000, 30)


def test_concurrent_discovery_updates_and_untrusted_device_projection():
    index = DiscoveryIndex()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda n: index.update(
                    "s" + str(n), props("node" + str(n)), ["10.0.0.1"], 9000, 30
                ),
                range(40),
            )
        )
    assert len(index.candidates()) == 40
    identity = SimpleNamespace(
        public=dict(node_id="alpha", display_name="Alpha", fingerprint="a" * 64)
    )
    trusted = {
        "beta": dict(
            friendly_name="Beta",
            fingerprint="b" * 64,
            trust_state="TRUSTED",
            security_status="OPAQUE_V1",
        )
    }
    peers = SimpleNamespace(all=lambda: trusted)
    index = DiscoveryIndex()
    assert [r["state"] for r in device_rows(identity, peers, index)] == [
        "This Device",
        "Offline Trusted",
    ]
    index.update("beta", props("beta", "b" * 64), ["10.0.0.2"], 9000, 30)
    index.update("gamma", props("gamma", "c" * 64), ["10.0.0.3"], 9000, 30)
    assert {r["state"] for r in device_rows(identity, peers, index)} == {
        "This Device",
        "Online Trusted",
        "Online Unpaired",
    }
    index.update("beta", props("beta", "a" * 64), ["10.0.0.2"], 9000, 30)
    rows = device_rows(identity, peers, index)
    assert (
        next(r for r in rows if r["node_id"] == "beta")["state"] == "Identity Mismatch"
    )
    assert all(r["authenticated"] is False for r in rows)


def test_actual_dns_remaining_ttl_and_interface_policy(monkeypatch):
    import ifaddr

    monkeypatch.setattr(
        ifaddr,
        "get_adapters",
        lambda: [
            SimpleNamespace(name=n, ips=[SimpleNamespace(ip=ip)])
            for n, ip in [
                ("enp1s0", "10.1.1.2"),
                ("docker0", "172.17.0.1"),
                ("br-test", "10.2.0.1"),
                ("lo", "127.0.0.1"),
                ("enp2", "8.8.8.8"),
            ]
        ],
    )
    assert local_addresses() == ["10.1.1.2"]

    class Record:
        def __init__(self, type_, **values):
            self.type = type_
            self.remaining = 30
            self.__dict__.update(values)

        def is_expired(self, now):
            return self.remaining <= 0

        def get_remaining_ttl(self, now):
            return self.remaining

    name = "alpha." + SERVICE_TYPE
    pointer = Record(12, alias=name)
    srv = Record(33)
    txt = Record(16)
    address = Record(1, address=b"\x0a\x00\x00\x01")
    cache = {SERVICE_TYPE: [pointer], name: [srv, txt], "alpha.local.": [address]}
    clock = [0.0]
    index = DiscoveryIndex(lambda: clock[0])
    browser = LanDiscovery.__new__(LanDiscovery)
    browser.index = index
    browser._service_lock = threading.Lock()
    browser._services = {
        name: SimpleNamespace(properties=props(), server="alpha.local.", port=9000)
    }
    browser.zc = SimpleNamespace(
        cache=SimpleNamespace(entries_with_name=lambda key: cache.get(key, []))
    )
    index.refresher = browser.refresh
    assert "alpha" in index.candidates()
    clock[0] = 31
    assert "alpha" in index.candidates()
    pointer.remaining = 0
    assert "alpha" not in index.candidates()
    pointer.remaining = 30
    cache[name] = [srv]
    assert "alpha" not in index.candidates()


@pytest.mark.parametrize(
    "endpoint,allowed",
    [
        ("https://p620.example.ts.net", True),
        ("https://host.local:8766/", True),
        ("https://10.0.0.1", True),
        ("https://127.0.0.1", True),
        ("https://[::1]", True),
        ("https://[fc00::1]", True),
        ("https://192.0.2.1", True),
        ("https://100.64.0.1", False),
        ("https://10.0.0.1%eth0", False),
        ("https://[fe80::1%eth0]", True),
        ("http://192.168.1.5", False),
        ("https://example.com", False),
        ("https://user@p620.ts.net", False),
        ("https://p620.ts.net/path", False),
        ("https://p620.ts.net?x=y", False),
        ("https://0.0.0.0", False),
        ("https://p620.ts.net:0", False),
        ("https://p620.ts.net:bad", False),
    ],
)
def test_observer_private_https_contract(endpoint, allowed):
    raw = dict(endpoint=endpoint, credential="x" * 48)
    if allowed:
        assert validate_connection(raw) == dict(raw, read_only=True)
    else:
        with pytest.raises(DeviceError, match="INVALID_OBSERVER_CONNECTION"):
            validate_connection(raw)


def test_bootstrap_opt_in_private_files_bounded_tokens(tmp_path):
    credential = tmp_path / "observer.env"
    credential.write_text("CLINX_OBSERVER_TOKEN=" + "x" * 48 + "\n")
    credential.chmod(0o600)
    config = tmp_path / "monitor.json"
    raw = dict(
        share_read_only_observer=True,
        endpoint="https://p620.example.ts.net",
        credential_file=str(credential),
    )
    config.write_text(json.dumps(raw))
    config.chmod(0o600)
    bootstrap = ObserverBootstrap(config)
    assert bootstrap.connection() == dict(
        endpoint=raw["endpoint"], credential="x" * 48, read_only=True
    )
    credential.write_text(("CLINX_OBSERVER_TOKEN=" + "x" * 48 + "\n") * 2)
    with pytest.raises(DeviceError, match="INVALID_OBSERVER_CONFIGURATION"):
        bootstrap.connection()
    config.write_text(json.dumps(dict(raw, share_read_only_observer=False)))
    with pytest.raises(DeviceError, match="OBSERVER_SHARING_DISABLED"):
        bootstrap.connection()
    config.chmod(0o644)
    with pytest.raises(DeviceError, match="UNSAFE_STATE_PERMISSIONS"):
        bootstrap.connection()
