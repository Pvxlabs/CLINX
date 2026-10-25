"""Python injected resolver/interface and state-envelope bindings only."""
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from clinx.discovery import SERVICE_TYPE
from clinx.discovery.discovery import DiscoveryIndex, LanDiscovery, local_addresses


def props(node="alpha", fingerprint="a" * 64):
    return {
        b"node_id": node.encode(),
        b"display_name": node.title().encode(),
        b"protocol_version": b"1",
        b"fingerprint": fingerprint.encode(),
    }


def test_python_discovery_envelope_serializes_concurrent_updates():
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
