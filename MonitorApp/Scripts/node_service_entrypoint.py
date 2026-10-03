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
from native_history import NativeHistory, ThreadLookupError  # noqa: E402
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
    native_history = NativeHistory(Path.home() / ".codex")

    def read_thread(thread_id: str, **kwargs):
        """Read the current user's Codex index and bounded native history.

        This is deliberately the read plane only: it does not initialize an
        app-server client, resume a thread, create a CLINX task, or acquire a
        writer lease.  The centre receives the same native-history projection
        used by the existing MCP context reader.
        """
        try:
            metadata = native_history.metadata(thread_id)
            if metadata is None:
                return {
                    "queried_thread_id": thread_id,
                    "lookup_status": "THREAD_NOT_FOUND",
                    "provider_existence": "NOT_FOUND",
                    "absence_scope": "CURRENT_USER_NATIVE_INDEX",
                    "context_status": "CONTEXT_UNAVAILABLE",
                }
            context = native_history.context(
                thread_id,
                record.node_id,
                metadata,
                kwargs.get("recent_turns", 8),
                kwargs.get("max_bytes", 32000),
                cursor=kwargs.get("cursor"),
            )
            return {
                "queried_thread_id": thread_id,
                "lookup_status": "THREAD_UNBOUND",
                "binding_status": "NOT_FOUND",
                "provider_existence": "CONFIRMED",
                "host": record.node_id,
                "native_thread": {
                    "thread_id": thread_id,
                    "cwd": metadata.get("cwd"),
                    "history_mode": metadata.get("history_mode"),
                    "archived": metadata.get("archived"),
                    "created_at": metadata.get("created_at"),
                    "updated_at": metadata.get("updated_at"),
                    "name": metadata.get("name") or metadata.get("title") or "",
                },
                "native_status": native_history.status(thread_id, metadata),
                "provider_observation": {"state": "UNKNOWN", "reason": "Live provider observation is separate from read-only history"},
                **context,
            }
        except ThreadLookupError as exc:
            return {
                "queried_thread_id": thread_id,
                "lookup_status": exc.code,
                "error_code": exc.code,
                "unavailable_reason": exc.reason,
                "provider_existence": "UNKNOWN",
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
