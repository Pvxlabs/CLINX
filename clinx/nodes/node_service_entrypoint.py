#!/usr/bin/env python3
"""Launch the bundled Air node and maintain its authenticated centre route."""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from clinx.discovery.identity import DeviceError, NodeIdentity, PrivateStore, TrustedPeerStore  # noqa: E402
from clinx.provider.native_history import NativeHistory  # noqa: E402
from clinx.nodes.rust_node_adapter import RustNodeExecutionAdapter  # noqa: E402
from clinx.nodes.node_protocol import NodeProtocolError, NodeAuthorizationStore, NodeRecord, NodeRegistry, NodeRPCServer, NodeService, SharingScope  # noqa: E402
from clinx.nodes.node_runtime import NodeRegistrationClient, PairedTLS, endpoint_address, node_request_handler  # noqa: E402



def configured_observation_source(native_home, node_id, config):
    """Load an explicit read-only provider route, independent of execution grants."""
    from clinx.monitor.observation_source import CodexObservationSource
    provider = None
    path = config.get("observation_provider_config")
    native_writer = config.get("observation_native_writer", False)
    if type(native_writer) is not bool or (native_writer and path is not None):
        raise ValueError("Invalid native writer observation route")
    if path is not None:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Invalid observation_provider_config")
        from clinx.contracts.runtime_config import BridgeConfig
        provider = BridgeConfig.load(Path(path).expanduser())
    return CodexObservationSource(native_home, node_id,
        config.get("observation_canonical_db"), config.get("observation_owner_host"),
        provider_config=provider,native_writer_observation=native_writer)


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
            from clinx.nodes.network_worker import Worker
            from clinx.authority.rust_authority import RustAuthorityError
            failures=[]
            def callback(event):
                try:
                    if event['operation']=='helper.scopes':
                        current=approvals.get(centre_id)
                        return dict(local=current.as_dict() if current else None,
                            active=active_scope[0].as_dict() if active_scope[0] else None)
                    if event['operation']=='helper.context':
                        return execution.context(**event['arguments'])
                    raise RustAuthorityError('NODE_CALLBACK_INVALID')
                except Exception as exc:
                    failures.append(exc)
                    raise
            worker=Worker(callback=callback)
            try:
                return worker.call('router.helper_read',native_home=str(native_history.root),
                    node_id=record.node_id,thread_id=thread_id,kwargs=kwargs,execution=execution is not None)
            except RustAuthorityError as exc:
                if failures: raise failures[-1]
                raise NodeProtocolError(str(exc),'Helper native read rejected') from None
            finally:
                worker.close()

        active_scope: list[SharingScope | None] = [None]
        registration = NodeRegistrationClient(tls, centre_id, centre_endpoint)
        execution = None
        execution_config = config.get("execution_config")
        if local_scope.execute_tasks and isinstance(execution_config, str) and execution_config:
            try:
                from clinx.authority.rust_authority import Commands
                from clinx.contracts.runtime_config import BridgeConfig
                from clinx.nodes.node_runtime import NodeAuthorityClient
                execution = RustNodeExecutionAdapter.from_config(BridgeConfig.load(Path(execution_config)),
                    Commands(NodeAuthorityClient(registration, lambda: active_scope[0])), identity.public["node_id"],
                    allowed_threads=config.get('execution_threads'),
                    allowed_projects=tuple(p.casefold() for p in config['execution_projects']) if config.get('execution_projects') is not None else None,
                    allowed_new_projects=tuple(p.casefold() for p in config['execution_new_projects']) if config.get('execution_new_projects') is not None else None,
                    allowed_cancel_projects=tuple(p.casefold() for p in config['execution_cancel_projects']) if config.get('execution_cancel_projects') is not None else None)
            except Exception as exc:
                write_health("degraded", "EXECUTION_CONFIG_UNAVAILABLE", detail=type(exc).__name__)
        capabilities = ["session.read", "session.status"] if local_scope.read_sessions else []
        if execution is not None and local_scope.execute_tasks:
            from clinx.nodes.node_protocol import NATIVE_HANDOFF_CAPABILITY
            capabilities.extend(["execution.adopt", "execution.prepare", "execution.start", "execution.status", "execution.context", "execution.cancel", "execution.reconcile"])
            capabilities.append(NATIVE_HANDOFF_CAPABILITY)
        service = NodeService(
            record, registry, scope=local_scope, read_thread=read_thread,
            adopt_conversation=execution.adopt if execution else None,
            context_execution=execution.context if execution else None,
            prepare_execution=execution.prepare if execution else None,
            start_execution=execution.start if execution else None,
            status_execution=execution.status if execution else None,
            cancel_execution=execution.cancel if execution else None,
            reconcile_execution=execution.reconcile if execution else None,
        )
        bind_address = str(config.get("bind_address") or os.environ.get("CLINX_NODE_BIND_ADDRESS", "0.0.0.0"))
        port = int(config.get("port") or os.environ.get("CLINX_NODE_PORT", "8772"))
        provider_relay = None
        if execution_config:
            from clinx.contracts.runtime_config import BridgeConfig
            from clinx.nodes.provider_relay import NodeProviderRelay
            provider_relay = NodeProviderRelay(BridgeConfig.load(Path(execution_config)), record.node_id)
        server = NodeRPCServer(
            service, address=bind_address, port=port,
            ssl_context_factory=tls.server_context,
            request_handler=node_request_handler(service, tls, approvals, centre_id, lambda: active_scope[0], provider_relay),
        )
        actual_port = server.address[1]
        from clinx.monitor.observation_source import ObservationCollector
        collector = ObservationCollector(state / "observation-outbox.sqlite3",
            configured_observation_source(native_history.root, record.node_id, config),
            identity, registration, lambda: approvals.get(centre_id), actual_port, capabilities,
            previous_stream_id=config.get("observation_previous_stream"))
        from clinx.nodes.network_worker import Worker
        def supervise(event):
            operation,args=event['operation'],event['arguments']
            try:
                if operation=='helper.approval':
                    current=approvals.get(centre_id)
                    result=current.as_dict() if current else None
                elif operation=='helper.active':
                    active_scope[0]=SharingScope(**args) if args else None
                    result=None
                elif operation=='helper.register':
                    method=registration.heartbeat if args['heartbeat'] else registration.register
                    result=method(identity,SharingScope(**args['scope']),port=actual_port,capabilities=tuple(capabilities))
                elif operation=='helper.state':
                    service.set_state(**args)
                    result=None
                elif operation=='helper.health':
                    write_health(**args)
                    result=None
                elif operation=='helper.collect':
                    result=dict(delay=collector.tick(),status=collector.status,sampled=collector.sampled)
                else:
                    raise ValueError('NODE_CALLBACK_INVALID')
                return dict(result=result)
            except Exception as exc:
                return dict(error_type=type(exc).__name__)
        supervisor=Worker(callback=supervise)
        stop_event = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
        signal.signal(signal.SIGINT, lambda *_: stop_event.set())
        try:
            while not stop_event.is_set():
                supervisor.call('router.helper_tick',centre_id=centre_id,node_id=record.node_id,
                    endpoint=f'tls://{server.address[0]}:{actual_port}',capabilities=capabilities)
                stop_event.wait(0.25)
        finally:
            active_scope[0] = None
            write_health("stopped")
            supervisor.close()
            collector.close()
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
