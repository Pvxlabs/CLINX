#!/usr/bin/env python3
"""Launch the bundled user-session CLINX node helper.

The helper owns no CLINX task database and never starts Codex.  A future
centre connection is established through the already paired mTLS peer store;
until a centre is configured the local service remains a read-only node.
"""

from __future__ import annotations

import os
import signal
import ssl
import sys
import time
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "Discovery"))

from local_discovery.identity import NodeIdentity, PrivateStore, TrustedPeerStore  # noqa: E402
from node_protocol import NodeRecord, NodeRegistry, NodeRPCServer, NodeService, SharingScope  # noqa: E402


def main() -> int:
    state = Path.home() / "Library/Application Support/CLINX Monitor/Node"
    private = PrivateStore(state)
    persisted = private.read("identity.json")
    configured_id = os.environ.get("CLINX_NODE_ID") or (persisted or {}).get("node_id") or "mac-node"
    identity = NodeIdentity(private, configured_id, os.uname().nodename[:100])
    peers = TrustedPeerStore(private)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    identity.load_tls_credentials(context)
    certificates = [
        row["certificate"] for row in peers.all().values()
        if row.get("trust_state") == "TRUSTED" and row.get("certificate")
    ]
    if not certificates:
        # Do not run an unauthenticated listener when pairing has not happened.
        return 2
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata="".join(certificates))

    user_scope = os.environ.get("CLINX_USER_SCOPE", os.environ.get("USER", "default"))
    registry = NodeRegistry(state / "node.sqlite3")
    record = NodeRecord(
        node_id=identity.public["node_id"], user_scope=user_scope,
        public_key_fingerprint=identity.public["fingerprint"],
        display_name=identity.public["display_name"], state="ONLINE",
        last_seen=dt.datetime.now(dt.timezone.utc).isoformat(),
    )

    def read_thread(thread_id: str, **kwargs):
        # Native history is intentionally delegated to the node's configured
        # CLINX context reader by the centre.  This helper never invents a
        # task or performs a provider call when no reader is bundled.
        return {
            "queried_thread_id": thread_id,
            "lookup_status": "THREAD_LOOKUP_UNAVAILABLE",
            "provider_existence": "UNKNOWN",
            "unavailable_reason": "NATIVE_READER_NOT_CONFIGURED",
            "context_status": "CONTEXT_UNAVAILABLE",
        }

    service = NodeService(
        record, registry,
        scope=SharingScope(user_scope=user_scope, read_sessions=True, execute_tasks=False),
        read_thread=read_thread,
    )
    address = os.environ.get("CLINX_NODE_BIND_ADDRESS", "127.0.0.1")
    port = int(os.environ.get("CLINX_NODE_PORT", "0"))
    server = NodeRPCServer(service, address=address, port=port, ssl_context=context)
    signal.signal(signal.SIGTERM, lambda *_: server.close())
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        return 0
    finally:
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())
