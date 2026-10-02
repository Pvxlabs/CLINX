"""One bounded GUI request over inherited private pipes; never log input/output."""

import json
import re
import socket
import sys
import time
from pathlib import Path

# -I startup ignores user PYTHONPATH; import only the matching bundled source.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from local_discovery.discovery import DiscoveryIndex, LanDiscovery, device_rows
from local_discovery.identity import DeviceError, NodeIdentity, PrivateStore, TrustedPeerStore
from local_discovery.pairing import backend_status
from local_discovery.transport import LanTransport


def handle(request, pin=""):
    root = Path.home() / "Library/Application Support/CLINX Monitor/Devices"
    private = PrivateStore(root)
    existing = private.read("identity.json")
    node_id = existing["node_id"] if existing else "mac-" + re.sub(
        r"[^a-z0-9-]", "-", socket.gethostname().split(".")[0].lower())[:48]
    identity = NodeIdentity(private, node_id, socket.gethostname()[:100])
    peers = TrustedPeerStore(private)
    operation = request.get("operation")
    if operation == "forget":
        # Forget is local removal, not a revocation tombstone: re-pair is possible.
        with private.lock():
            records = peers._read()
            records.pop(request["node_id"], None)
            private.write("peers.json", dict(schema_version=1, peers=records))
        return {"forgotten": True}
    if operation not in {"discover", "pair", "connect"}:
        raise DeviceError("INVALID_OPERATION")
    index = DiscoveryIndex()
    with LanDiscovery(index):
        time.sleep(2)
        if operation == "discover":
            return {"devices": device_rows(identity, peers, index),
                    "backend": backend_status(allow_dev=False)}
        candidate = index.candidates().get(request.get("node_id"))
        if candidate is None:
            raise DeviceError("DEVICE_OFFLINE")
        transport = LanTransport(identity, peers)
        if operation == "pair":
            if not re.fullmatch(r"[0-9]{4}", pin):
                raise DeviceError("INVALID_PAIRING_CODE")
            transport.pair(candidate, pin)
            pin = ""
        else:
            transport.reconnect(candidate)
        try:
            connection = transport.observer_connection(candidate)
        except DeviceError as exc:
            if str(exc) == "REMOTE_REQUEST_REJECTED":
                raise DeviceError("OBSERVER_SHARING_UNAVAILABLE") from exc
            raise
        return dict(connection, node_id=candidate.node_id, display_name=candidate.display_name)


def main():
    try:
        request = json.loads(sys.stdin.buffer.readline(4097))
        # Code has its own one-use line, is never an argument or JSON field.
        pin = sys.stdin.buffer.readline(6).decode().strip() if request.get("operation") == "pair" else ""
        result = handle(request, pin)
        pin = ""
    except DeviceError as exc:
        code = str(exc)
        result = {"error": code if re.fullmatch(r"[A-Z_]{1,80}", code) else "PAIRING_FAILED"}
    except Exception:
        result = {"error": "DISCOVERY_UNAVAILABLE"}
    payload = json.dumps(result)
    if len(payload.encode()) > 65536:
        payload = '{"error":"TOO_MANY_DEVICES"}'
    sys.stdout.write(payload)


if __name__ == "__main__":
    main()
