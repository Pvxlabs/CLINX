"""mDNS/DNS-SD candidates are untrusted presence hints, never host registration."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from . import SERVICE_TYPE
from .identity import DeviceError, NodeIdentity

TXT_KEYS = {b"node_id", b"display_name", b"protocol_version", b"fingerprint"}


def _command(operation, **arguments):
    from clinx.authority.rust_authority import RustAuthorityError
    from clinx.nodes.network_worker import read_command

    try:
        return read_command("discovery_index." + operation, **arguments)
    except RustAuthorityError as exc:
        raise DeviceError(str(exc)) from None


def lan_address(value: str) -> str:
    return _command("lan_address", value=value)


@dataclass(frozen=True)
class Candidate:
    node_id: str
    display_name: str
    fingerprint: str
    endpoints: tuple[tuple[str, int], ...]
    mismatch: bool = False


class DiscoveryIndex:
    """mDNS IO state envelope; expiry, identity and candidates are Rust-owned."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.refresher: Callable[[], None] | None = None
        self._records = []
        self._lock = threading.Lock()

    def update(self, service, properties, addresses, port, ttl):
        with self._lock:
            self._records = _command(
                "update",
                records=self._records,
                now=self.clock(),
                service=service,
                properties=[(k.hex(), v.hex()) for k, v in properties.items()],
                addresses=addresses,
                port=port,
                ttl=ttl,
            )

    def remove(self, service):
        with self._lock:
            self._records = _command("remove", records=self._records, service=service)

    def _expire(self):
        self._records = _command("expire", records=self._records, now=self.clock())

    def candidates(self):
        runtime = getattr(self, "_runtime", None)
        if runtime is not None:
            return {
                key: Candidate(
                    **dict(value, endpoints=tuple(tuple(e) for e in value["endpoints"]))
                )
                for key, value in runtime._candidates().items()
            }
        if self.refresher:
            self.refresher()
        with self._lock:
            result = _command("candidates", records=self._records, now=self.clock())
            self._records = result["records"]
            return {
                key: Candidate(
                    **dict(value, endpoints=tuple(tuple(e) for e in value["endpoints"]))
                )
                for key, value in result["candidates"].items()
            }


def device_rows(identity, peers, index):
    return _command(
        "device_rows",
        identity=identity.public,
        trusted=peers.all(),
        online={key: asdict(value) for key, value in index.candidates().items()},
    )


def local_addresses():
    # Explicit legacy qualification injection; normal interface IO is Rust-owned.
    import sys

    module = sys.modules.get("ifaddr")
    injected = getattr(module, "get_adapters", None)
    if injected is not None and not getattr(
        injected, "__module__", "ifaddr"
    ).startswith("ifaddr"):
        return _command(
            "local_addresses",
            adapters=[
                {"name": adapter.name, "ips": [entry.ip for entry in adapter.ips]}
                for adapter in injected()
            ],
        )
    from clinx.nodes.network_worker import read_command

    return read_command("mdns.local_addresses")


class LanDiscovery:
    """Public object binding for the Rust DNS-SD socket/cache lifecycle."""

    def __init__(self, index: DiscoveryIndex, *, interfaces: list[str] | None = None):
        from clinx.nodes.network_worker import Worker

        self.index = index
        self._worker = Worker()
        self._closed = False
        try:
            result = self._call(
                "start",
                interfaces=local_addresses() if interfaces is None else interfaces,
            )
        except Exception:
            self._worker.close()
            raise
        self._id, self.interfaces = result["id"], result["interfaces"]
        self.advertisement = None
        self.zc = self if self.interfaces else None
        self.browser = self if self.interfaces else None
        self.index._runtime = self

    def _call(self, operation, **arguments):
        from clinx.authority.rust_authority import RustAuthorityError

        try:
            if hasattr(self, "_id"):
                arguments["id"] = self._id
            return self._worker.call("mdns." + operation, **arguments)
        except RustAuthorityError as exc:
            raise DeviceError(str(exc)) from None

    def _candidates(self):
        return self._call("candidates")["candidates"]

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
        if hasattr(self, "_worker"):
            return
        # ServiceBrowser suppresses callbacks for unchanged refreshed records.
        # Read actual remaining DNS TTLs instead of extending cached metadata
        # by its original TTL (which would keep an offline device alive).
        if self.zc is None:
            return
        now = time.monotonic() * 1000  # zeroconf 0.151.5 clock domain.

        def record(row):
            address = getattr(row, "address", None)
            return dict(
                type=row.type,
                alias=getattr(row, "alias", None),
                expired=row.is_expired(now),
                remaining=row.get_remaining_ttl(now),
                address=address.hex() if isinstance(address, bytes) else None,
            )

        pointers = [
            record(row) for row in self.zc.cache.entries_with_name(SERVICE_TYPE)
        ]
        with self._service_lock:
            services = list(self._services.items())
        for name, info in services:
            try:
                candidate = _command(
                    "refresh",
                    service=name,
                    pointers=pointers,
                    records=[
                        record(row) for row in self.zc.cache.entries_with_name(name)
                    ],
                    addresses=[
                        record(row)
                        for row in self.zc.cache.entries_with_name(info.server)
                    ],
                )
                if candidate["active"]:
                    self.index.update(
                        name,
                        info.properties,
                        candidate["addresses"],
                        info.port,
                        candidate["ttl"],
                    )
                else:
                    self.index.remove(name)
            except (DeviceError, TypeError):
                self.index.remove(name)

    def advertise(
        self, identity: NodeIdentity, port: int, addresses: list[str] | None = None
    ) -> None:
        self.advertisement = self._call(
            "advertise", identity=identity.public, port=port, addresses=addresses
        )

    def unregister_service(self, advertisement):
        if advertisement != self.advertisement:
            raise DeviceError("INVALID_DISCOVERY_METADATA")
        self._call("unregister")
        self.advertisement = None

    def cancel(self):
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._call("close")
            self._worker.close()
            self._closed = True
            self.index._runtime = None
            self.index.refresher = None

    def __enter__(self) -> LanDiscovery:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
