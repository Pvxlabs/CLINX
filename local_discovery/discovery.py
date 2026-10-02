"""mDNS/DNS-SD candidates are untrusted presence hints, never host registration."""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from zeroconf import ServiceListener

from . import PROTOCOL_VERSION, SERVICE_TYPE
from .identity import DeviceError, NodeIdentity, TrustedPeerStore, valid_name, valid_node

TXT_KEYS = {b"node_id", b"display_name", b"protocol_version", b"fingerprint"}
PRIVATE_V4 = tuple(
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8")
)


def lan_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise DeviceError("INVALID_LAN_ADDRESS") from exc
    if address.version != 4 or not any(address in network for network in PRIVATE_V4):
        raise DeviceError("NON_LAN_ADDRESS")
    return str(address)


@dataclass(frozen=True)
class Candidate:
    node_id: str
    display_name: str
    fingerprint: str
    endpoints: tuple[tuple[str, int], ...]
    mismatch: bool = False


class DiscoveryIndex:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.refresher: Callable[[], None] | None = None
        self._records: dict[str, tuple[dict[str, str], tuple[tuple[str, int], ...], float]] = {}
        self._lock = threading.Lock()

    def update(
        self,
        service: str,
        properties: dict[bytes, bytes],
        addresses: list[str],
        port: int,
        ttl: float,
    ) -> None:
        if set(properties) != TXT_KEYS or any(len(v) > 200 for v in properties.values()):
            raise DeviceError("INVALID_DISCOVERY_METADATA")
        try:
            props = {k.decode("ascii"): v.decode("utf-8") for k, v in properties.items()}
        except UnicodeError as exc:
            raise DeviceError("INVALID_DISCOVERY_METADATA") from exc
        valid_node(props["node_id"])
        valid_name(props["display_name"])
        if props["protocol_version"] != str(PROTOCOL_VERSION):
            raise DeviceError("UNSUPPORTED_DISCOVERY_PROTOCOL")
        if not re.fullmatch(r"[0-9a-f]{64}", props["fingerprint"]):
            raise DeviceError("INVALID_DISCOVERY_FINGERPRINT")
        if not 1 <= port <= 65535 or len(addresses) > 16:
            raise DeviceError("INVALID_DISCOVERY_ENDPOINT")
        endpoints = tuple(sorted({(lan_address(a), port) for a in addresses}))
        if not endpoints:
            raise DeviceError("NO_LAN_ADDRESSES")
        with self._lock:
            self._expire()
            if len(self._records) >= 256 and service not in self._records:
                raise DeviceError("DISCOVERY_CAPACITY")
            if ttl <= 0:
                self._records.pop(service, None)
            else:
                self._records[service] = (props, endpoints, self.clock() + min(ttl, 120.0))

    def remove(self, service: str) -> None:
        with self._lock:
            self._records.pop(service, None)

    def _expire(self) -> None:
        for name, (_, _, expires) in list(self._records.items()):
            if expires <= self.clock():
                del self._records[name]

    def candidates(self) -> dict[str, Candidate]:
        if self.refresher:
            self.refresher()
        with self._lock:
            self._expire()
            grouped: dict[str, list[Any]] = {}
            for record in self._records.values():
                grouped.setdefault(record[0]["node_id"], []).append(record)
            result = {}
            for node_id, records in grouped.items():
                props = records[0][0]
                result[node_id] = Candidate(
                    node_id,
                    props["display_name"],
                    props["fingerprint"],
                    tuple(sorted({e for _, endpoints, _ in records for e in endpoints})),
                    len({p["fingerprint"] for p, _, _ in records}) != 1,
                )
            return result


def device_rows(
    identity: NodeIdentity, peers: TrustedPeerStore, index: DiscoveryIndex
) -> list[dict[str, Any]]:
    rows = [dict(identity.public, state="This Device", authenticated=False)]
    trusted = peers.all()
    online = index.candidates()
    for node_id in sorted(set(trusted) | set(online)):
        candidate = online.get(node_id)
        peer = trusted.get(node_id)
        if node_id == identity.public["node_id"]:
            if candidate and (
                candidate.mismatch or candidate.fingerprint != identity.public["fingerprint"]
            ):
                rows.append(
                    dict(
                        node_id=node_id,
                        display_name=candidate.display_name,
                        state="Identity Mismatch",
                        authenticated=False,
                    )
                )
            continue
        if candidate and (
            candidate.mismatch or peer and peer["fingerprint"] != candidate.fingerprint
        ):
            state = "Identity Mismatch"
        elif peer and peer["trust_state"] == "TRUSTED":
            state = "Online Trusted" if candidate else "Offline Trusted"
        elif candidate:
            state = "Online Unpaired"
        else:
            continue
        display_name = (
            peer["friendly_name"] if peer else (candidate.display_name if candidate else node_id)
        )
        rows.append(
            dict(
                node_id=node_id,
                display_name=display_name,
                state=state,
                authenticated=False,
                security_status=peer["security_status"] if peer else "UNPAIRED",
            )
        )
    return rows


def local_addresses() -> list[str]:
    import ifaddr

    addresses = set()
    for adapter in ifaddr.get_adapters():
        if adapter.name.startswith(
            ("lo", "docker", "veth", "br-", "bridge", "virbr", "tailscale", "tun", "tap", "vnet")
        ):
            continue
        for entry in adapter.ips:
            if isinstance(entry.ip, str):
                try:
                    value = lan_address(entry.ip)
                    if not value.startswith("127."):
                        addresses.add(value)
                except DeviceError:
                    pass
    return sorted(addresses)[:16]


class LanDiscovery(ServiceListener):
    def __init__(self, index: DiscoveryIndex, *, interfaces: list[str] | None = None):
        from zeroconf import IPVersion, ServiceBrowser, Zeroconf

        self.index = index
        self.interfaces = (
            local_addresses() if interfaces is None else [lan_address(a) for a in interfaces]
        )
        self._services: dict[str, Any] = {}
        self._service_lock = threading.Lock()
        self.advertisement: Any = None
        self.zc: Any = None
        self.browser: Any = None
        # Offline browsing must still render This Device / Offline Trusted.
        if self.interfaces:
            self.zc = Zeroconf(interfaces=self.interfaces, ip_version=IPVersion.V4Only)
            self.browser = ServiceBrowser(self.zc, SERVICE_TYPE, listener=self)
            self.index.refresher = self.refresh

    def add_service(self, zc: Any, type_: str, name: str) -> None:
        self.update_service(zc, type_, name)

    def update_service(self, zc: Any, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name, timeout=1500)
        if info:
            with self._service_lock:
                if len(self._services) < 256 or name in self._services:
                    self._services[name] = info
            self.refresh()

    def remove_service(self, zc: Any, type_: str, name: str) -> None:
        with self._service_lock:
            self._services.pop(name, None)
        self.index.remove(name)

    def refresh(self) -> None:
        # ServiceBrowser suppresses callbacks for unchanged refreshed records.
        # Read actual remaining DNS TTLs instead of extending cached metadata
        # by its original TTL (which would keep an offline device alive).
        if self.zc is None:
            return
        now = time.monotonic() * 1000  # zeroconf 0.151.5 clock domain.
        pointers = {
            r.alias: r
            for r in self.zc.cache.entries_with_name(SERVICE_TYPE)
            if r.type == 12 and not r.is_expired(now)
        }
        with self._service_lock:
            services = list(self._services.items())
        for name, info in services:
            pointer = pointers.get(name)
            records = [
                r
                for r in self.zc.cache.entries_with_name(name)
                if r.type in {16, 33} and not r.is_expired(now)
            ]
            addresses = [
                r
                for r in self.zc.cache.entries_with_name(info.server)
                if r.type == 1 and not r.is_expired(now)
            ]
            if not pointer or {r.type for r in records} != {16, 33} or not addresses:
                self.index.remove(name)
                continue
            ttl = min(r.get_remaining_ttl(now) for r in [pointer, *records, *addresses])
            try:
                self.index.update(
                    name,
                    info.properties,
                    [socket.inet_ntoa(r.address) for r in addresses],
                    info.port,
                    ttl,
                )
            except (DeviceError, TypeError):
                self.index.remove(name)

    def advertise(
        self, identity: NodeIdentity, port: int, addresses: list[str] | None = None
    ) -> None:
        from zeroconf import ServiceInfo

        addresses = self.interfaces if addresses is None else addresses
        if not addresses or self.zc is None:
            raise DeviceError("NO_PRIVATE_LAN_INTERFACE")
        props = {key: str(identity.public[key.decode()]).encode("utf-8") for key in TXT_KEYS}
        label = identity.public["node_id"]
        info = ServiceInfo(
            SERVICE_TYPE,
            label + "." + SERVICE_TYPE,
            addresses=[socket.inet_aton(lan_address(a)) for a in addresses],
            port=port,
            properties=props,
            server=label + ".local.",
            host_ttl=30,
            other_ttl=30,
        )
        self.zc.register_service(info, allow_name_change=False)
        self.advertisement = info

    def close(self) -> None:
        self.index.refresher = None
        if self.browser:
            self.browser.cancel()
        if self.zc:
            if self.advertisement:
                self.zc.unregister_service(self.advertisement)
            self.zc.close()

    def __enter__(self) -> LanDiscovery:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
