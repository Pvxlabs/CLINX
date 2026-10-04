#!/usr/bin/env python3
"""Launch the bundled Air node and maintain its authenticated centre route."""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "Discovery"))

from local_discovery.identity import DeviceError, NodeIdentity, PrivateStore, TrustedPeerStore  # noqa: E402
from native_history import NativeHistory, ThreadLookupError  # noqa: E402
from node_execution_adapter import CanonicalNodeExecutionAdapter  # noqa: E402
from node_protocol import NodeAuthorizationStore, NodeRecord, NodeRegistry, NodeRPCServer, NodeService, SharingScope  # noqa: E402
from node_runtime import NodeRegistrationClient, PairedTLS, endpoint_address, node_request_handler  # noqa: E402


def main() -> int:
    state = Path(os.environ.get(
        "CLINX_NODE_STATE",
        str(Path.home() / "Library/Application Support/CLINX Monitor/Devices"),
    )).expanduser()
    health_state = Path(os.environ.get(
        "CLINX_NODE_HEALTH_STATE",
        str(Path.home() / "Library/Application Support/CLINX Monitor/Node"),
    )).expanduser()
    health_path = health_state / "health.json"

    def write_health(status: str, reason: str | None = None, **extra: object) -> None:
        payload: dict[str, object] = {"status": status, "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        if reason:
            payload["reason"] = reason
        payload.update(extra)
        try:
            health_state.mkdir(parents=True, exist_ok=True)
            temporary = health_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            temporary.replace(health_path)
        except OSError:
            pass

    try:
        private = PrivateStore(state)
        persisted = private.read("identity.json")
        configured_id = os.environ.get("CLINX_NODE_ID") or (persisted or {}).get("node_id") or os.uname().nodename[:63].lower()
        identity = NodeIdentity(private, configured_id, os.uname().nodename[:100])
        peers = TrustedPeerStore(private)
        config = private.read("node-config.json")
        if not config:
            write_health("blocked", "CENTRE_NOT_CONFIGURED")
            return 0
        centre_id = config.get("centre_id")
        centre_endpoint = config.get("centre_endpoint")
        if not isinstance(centre_id, str) or not isinstance(centre_endpoint, str):
            write_health("blocked", "CENTRE_CONFIG_INVALID")
            return 0
        endpoint_address(centre_endpoint)
        tls = PairedTLS(identity, peers)
        tls.peer(centre_id)
        approvals = NodeAuthorizationStore(state)
        local_scope = approvals.get(centre_id)
        if local_scope is None or local_scope.revoked or not (local_scope.read_sessions or local_scope.execute_tasks):
            write_health("blocked", "SHARING_AUTHORIZATION_REQUIRED")
            return 0

        user_scope = local_scope.user_scope
        registry = NodeRegistry(state / "node.sqlite3")
        record = NodeRecord(
            node_id=identity.public["node_id"], user_scope=user_scope,
            public_key_fingerprint=identity.public["fingerprint"],
            display_name=identity.public["display_name"], state="UNKNOWN", last_seen=None,
            capabilities=tuple(),
        )
        native_history = NativeHistory(Path(os.environ.get("CLINX_NATIVE_HOME", str(Path.home() / ".codex"))))

        def read_thread(thread_id: str, **kwargs: object) -> dict[str, object]:
            if execution is not None:
                return execution.context(thread_id=thread_id, host=record.node_id, **kwargs)
            try:
                metadata = native_history.metadata(thread_id)
                if metadata is None:
                    return {"queried_thread_id": thread_id, "lookup_status": "THREAD_NOT_FOUND",
                            "provider_existence": "NOT_FOUND", "absence_scope": "CURRENT_USER_NATIVE_INDEX",
                            "context_status": "CONTEXT_UNAVAILABLE"}
                context = native_history.context(
                    thread_id, record.node_id, metadata, int(kwargs.get("recent_turns", 8)),
                    int(kwargs.get("max_bytes", 32000)), cursor=kwargs.get("cursor"),
                )
                return {"queried_thread_id": thread_id, "lookup_status": "THREAD_UNBOUND",
                        "binding_status": "NOT_FOUND", "provider_existence": "CONFIRMED", "host": record.node_id,
                        "native_thread": {"thread_id": thread_id, "cwd": metadata.get("cwd"),
                            "history_mode": metadata.get("history_mode"), "archived": metadata.get("archived"),
                            "created_at": metadata.get("created_at"), "updated_at": metadata.get("updated_at"),
                            "name": metadata.get("name") or metadata.get("title") or ""},
                        "native_status": native_history.status(thread_id, metadata),
                        "provider_observation": {"state": "UNKNOWN", "reason": "Live provider observation is separate from read-only history"},
                        **context}
            except ThreadLookupError as exc:
                return {"queried_thread_id": thread_id, "lookup_status": exc.code, "error_code": exc.code,
                        "unavailable_reason": exc.reason, "provider_existence": "UNKNOWN",
                        "context_status": "CONTEXT_UNAVAILABLE"}

        execution = None
        execution_config = config.get("execution_config")
        if isinstance(execution_config, str) and execution_config:
            try:
                execution = CanonicalNodeExecutionAdapter.from_config(Path(execution_config), identity.public["node_id"],
                    allowed_threads=config.get('execution_threads'),
                    allowed_projects=tuple(p.casefold() for p in config['execution_projects']) if config.get('execution_projects') is not None else None,
                    allowed_new_projects=tuple(p.casefold() for p in config['execution_new_projects']) if config.get('execution_new_projects') is not None else None,
                    allowed_cancel_projects=tuple(p.casefold() for p in config['execution_cancel_projects']) if config.get('execution_cancel_projects') is not None else None)
            except Exception as exc:
                write_health("degraded", "EXECUTION_CONFIG_UNAVAILABLE", detail=type(exc).__name__)
        capabilities = ["session.read", "session.status"] if local_scope.read_sessions else []
        if execution is not None and local_scope.execute_tasks:
            capabilities.extend(["execution.adopt", "execution.prepare", "execution.start", "execution.status", "execution.context", "execution.cancel"])
        service = NodeService(
            record, registry, scope=local_scope, read_thread=read_thread,
            adopt_conversation=execution.adopt if execution else None,
            context_execution=execution.context if execution else None,
            prepare_execution=execution.prepare if execution else None,
            start_execution=execution.start if execution else None,
            status_execution=execution.status if execution else None,
            cancel_execution=execution.cancel if execution else None,
        )
        active_scope: list[SharingScope | None] = [None]
        bind_address = str(config.get("bind_address") or os.environ.get("CLINX_NODE_BIND_ADDRESS", "0.0.0.0"))
        port = int(config.get("port") or os.environ.get("CLINX_NODE_PORT", "8772"))
        server = NodeRPCServer(
            service, address=bind_address, port=port,
            ssl_context_factory=tls.server_context,
            request_handler=node_request_handler(service, tls, approvals, centre_id, lambda: active_scope[0]),
        )
        actual_port = server.address[1]
        registration = NodeRegistrationClient(tls, centre_id, centre_endpoint)
        stop_event = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
        signal.signal(signal.SIGINT, lambda *_: stop_event.set())
        heartbeat_seconds = 15
        next_attempt = 0.0
        try:
            while not stop_event.is_set():
                now = time.monotonic()
                if now >= next_attempt:
                    try:
                        current = approvals.get(centre_id)
                        if current is None or current.revoked or not (current.read_sessions or current.execute_tasks):
                            active_scope[0] = None
                            write_health("blocked", "SHARING_AUTHORIZATION_REVOKED")
                        else:
                            reply = (registration.register(identity, current, port=actual_port, capabilities=tuple(capabilities))
                                     if active_scope[0] is None else
                                     registration.heartbeat(identity, current, port=actual_port, capabilities=tuple(capabilities)))
                            if not reply.get("registered"):
                                active_scope[0] = None
                                service.set_state("OFFLINE")
                                write_health("offline", reply.get("error_code", "REGISTRATION_REJECTED"))
                            else:
                                raw_scope = reply.get("scope")
                                if not isinstance(raw_scope, dict):
                                    raise DeviceError("INVALID_CENTRE_SCOPE")
                                active_scope[0] = SharingScope(**{key: value for key, value in raw_scope.items() if key in SharingScope.__dataclass_fields__})
                                service.set_state("ONLINE", last_seen=reply.get("observed_at"))
                                heartbeat_seconds = max(3, int(reply.get("heartbeat_seconds", 15)))
                                write_health("running", centre_id=centre_id, node_id=identity.public["node_id"],
                                             endpoint=f"tls://{server.address[0]}:{actual_port}", capabilities=capabilities)
                    except Exception as exc:
                        active_scope[0] = None
                        service.set_state("OFFLINE")
                        write_health("offline", type(exc).__name__)
                    next_attempt = time.monotonic() + min(60, heartbeat_seconds)
                stop_event.wait(0.5)
        finally:
            active_scope[0] = None
            write_health("stopped")
            server.close()
            registry.close()
            if execution is not None:
                execution.close()
        return 0
    except (DeviceError, ValueError, OSError) as exc:
        write_health("blocked", type(exc).__name__)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
