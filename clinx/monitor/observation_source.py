"""One bounded node collector, durable outbox and checkpoints; no execution APIs."""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import time
from pathlib import Path

from clinx.nodes.network_worker import read_command as _read_command
from clinx.nodes.node_protocol import SUPPORTED_PROVIDER, NodeProtocolError
from clinx.provider.native_history import NativeHistory


def read_command(operation, **arguments):
    from clinx.authority.rust_authority import RustAuthorityError

    try:
        return _read_command(operation, **arguments)
    except RustAuthorityError as exc:
        raise NodeProtocolError(str(exc), str(exc)) from None


OUTBOX_LIMIT = 2048


def order(value, fallback=0):
    # Existing local-time conversion is a format compatibility boundary. A naive
    # timestamp must retain the caller's timezone, including on macOS.
    try:
        if isinstance(value, (int, float)):
            return int(value * (1000 if value < 100000000000 else 1))
        return int(
            dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
            * 1000
        )
    except (ValueError, TypeError, OverflowError):
        return fallback


class CodexObservationSource:
    provider = SUPPORTED_PROVIDER

    def __init__(
        self,
        root,
        node_id,
        canonical_db=None,
        owner_host=None,
        provider_config=None,
        native_writer_observation=False,
    ):
        self.history = NativeHistory(root)
        self.node_id = node_id
        self.canonical_db = Path(canonical_db) if canonical_db else None
        self.provider_config = provider_config
        self.native_writer_observation = native_writer_observation
        self.live_cache = {}
        import threading

        self._source_lock = threading.Lock()
        self._source_worker = None
        from clinx.contracts.execution_semantics import normalize_host

        self.owner_host = normalize_host(owner_host or node_id).stable_identifier

    @property
    def generation(self):
        return read_command(
            "source.generation",
            native_index=str(self.history.root / "state_5.sqlite"),
            canonical_path=str(self.canonical_db) if self.canonical_db else None,
        )

    def inventory(self, scope, after=None, *, audit=False):
        return self.history._read(
            "inventory", scope=scope.as_dict(), after=after, audit=audit
        )

    def canonical_inventory(self, scope, after=""):
        return read_command(
            "source.canonical_inventory",
            canonical_path=str(self.canonical_db) if self.canonical_db else None,
            owner_host=self.owner_host,
            scope=scope.as_dict(),
            after=after,
        )

    def canonical_turns(self, tid, project, state):
        turns, updated = read_command(
            "source.canonical_turns",
            canonical_path=str(self.canonical_db) if self.canonical_db else None,
            owner_host=self.owner_host,
            tid=tid,
            project=project,
            state=state,
        )
        state.clear()
        state.update(updated)
        for turn in turns:
            turn["ordinal"] = order(turn["started_at"])
        return turns

    def native_turns(self, metadata, state):
        if metadata.get("history_mode") not in ("paginated", "canonical"):
            return self._legacy(metadata, state)
        turns, coverage, updated = self.history._read(
            "native_turns", metadata=metadata, state=state, node_id=self.node_id
        )
        state.clear()
        state.update(updated)
        return turns, coverage

    def _legacy(self, metadata, state):
        turns, coverage, updated = self.history._read(
            "native_turns", metadata=metadata, state=state, node_id=self.node_id
        )
        state.clear()
        state.update(updated)
        return turns, coverage

    def sample(self, metadata, state):
        from clinx.authority.rust_authority import RustAuthorityError
        from clinx.nodes.network_worker import Worker

        with self._source_lock:
            if self._source_worker is None:
                import atexit
                import weakref

                reference = weakref.ref(self)

                def callback(event):
                    source = reference()
                    if source is None:
                        raise NodeProtocolError(
                            "OBSERVATION_SOURCE_UNAVAILABLE", "Source closed"
                        )
                    try:
                        return source._callback(event)
                    except RustAuthorityError as exc:
                        # Keep existing Authority failures distinct from source
                        # projection failures across the private child protocol.
                        raise NodeProtocolError(
                            "SOURCE_AUTHORITY_ERROR:" + str(exc), str(exc)
                        ) from None

                self._source_worker = Worker(callback=callback)
                atexit.register(self._source_worker.close)
            worker = self._source_worker
        try:
            events, updated = worker.call(
                "source.sample",
                metadata=metadata,
                state=state,
                canonical_db=bool(self.canonical_db),
                live_enabled=bool(
                    self.provider_config or self.native_writer_observation
                ),
            )
        except RustAuthorityError as exc:
            if str(exc).startswith("SOURCE_AUTHORITY_ERROR:"):
                raise RustAuthorityError(str(exc).split(":", 1)[1]) from None
            raise NodeProtocolError(str(exc), str(exc)) from None
        state.clear()
        state.update(updated)
        return events, state

    def _callback(self, event):
        operation, args = event["operation"], event["arguments"]
        if operation == "source.native_turns":
            state = args["state"]
            turns, coverage = self.native_turns(args["metadata"], state)
            return turns, coverage, state
        if operation == "source.canonical_turns":
            state = args["state"]
            turns = self.canonical_turns(args["tid"], args["project"], state)
            return turns, state
        if operation == "source.observe_live":
            return self._observe_live(args["tid"], args["project"], args["turn_id"])
        if operation == "source.generation":
            return self.generation
        raise NodeProtocolError(
            "OBSERVATION_CALLBACK_INVALID", "Unexpected source callback"
        )

    def _observe_live(self,tid,project,turn_id):
        """Bounded reads of configured existing owners; no execution/lease writes."""
        from clinx.provider.native_provider import observe_thread
        from clinx.provider.liveness import classify
        stamp=time.time()
        cached=self.live_cache.get(tid)
        if cached and cached["turn_id"]==turn_id and 0<=stamp-cached["observed_at"]<10:
            return cached
        if self.native_writer_observation:
            from clinx.provider.native_provider import observe_native_writer
            evidence=observe_native_writer(self.history.root,tid,turn_id,project)
            endpoint=os.environ.get('CLINX_NATIVE_OBSERVER_SOCKET')
            if evidence['liveness'] != 'LIVE' and endpoint:
                from clinx.provider.native_writer_probe import observe
                evidence=observe(endpoint,self.history.root,tid,turn_id,project)
            proof=dict(thread_id=tid,turn_id=turn_id,observed_at=stamp,
                liveness=evidence["liveness"],reason=evidence["reason"])
            self.live_cache[tid]=proof
            if len(self.live_cache)>128: self.live_cache.pop(next(iter(self.live_cache)))
            return proof
        rows=observe_thread(self.provider_config,tid)["observations"]
        evidence=classify(rows,tid,turn_id)
        owner=next((r for r in rows if r.get("endpoint")==evidence["owner_endpoint"]),{})
        live=evidence["provider_liveness"]=="LIVE" and owner.get("cwd")==project
        proof=dict(thread_id=tid,turn_id=turn_id,observed_at=stamp,
            liveness="LIVE" if live else "UNKNOWN",
            reason=evidence["liveness_reason"] if owner.get("cwd")==project or not owner else "WORKSPACE_MISMATCH")
        if len(self.live_cache)>=128: self.live_cache.pop(next(iter(self.live_cache)))
        self.live_cache[tid]=proof
        return proof



class ObservationCollector:
    """Private client for the Rust-owned node spool, lock and collection policy."""

    def __init__(
        self,
        path,
        source,
        identity,
        registration,
        scope,
        port,
        capabilities,
        previous_stream_id=None,
    ):
        import weakref

        from clinx.nodes.network_worker import Worker

        self.path = Path(path)
        self.source = source
        self.identity = identity
        self.registration = registration
        self.scope = scope
        self.port = port
        self.capabilities = capabilities
        self.previous_stream_id = previous_stream_id
        self.status = "BOOTSTRAP"
        self.sampled = 0
        self._inspection = None
        reference = weakref.ref(self)

        def callback(event):
            collector = reference()
            if collector is None:
                raise NodeProtocolError("COLLECTOR_NOT_OPEN", "Collector closed")
            return collector._callback(event)

        self.worker = Worker(callback=callback)
        try:
            self._call("init", path=str(self.path))
        except Exception:
            self.worker.close()
            raise

    def _call(self, operation, **arguments):
        from clinx.authority.rust_authority import RustAuthorityError

        try:
            return self.worker.call("collector." + operation, **arguments)
        except RustAuthorityError as exc:
            raise NodeProtocolError(str(exc), str(exc)) from None

    def _callback(self, event):
        from clinx.nodes.node_protocol import SharingScope

        operation, args = event["operation"], event["arguments"]
        if operation == "collector.scope":
            scope = self.scope()
            return scope.as_dict() if scope else None
        if operation in {"collector.heartbeat", "collector.upload"}:
            scope = SharingScope(**args["scope"])
            try:
                if operation == "collector.heartbeat":
                    return self.registration.heartbeat(
                        self.identity,
                        scope,
                        port=self.port,
                        capabilities=tuple(self.capabilities),
                    )
                return self.registration.upload(self.identity, scope, **args["batch"])
            except (OSError, TimeoutError):
                return {"offline": True}
        if operation == "collector.generation":
            return self.source.generation
        if operation == "collector.inventory":
            return self.source.inventory(
                SharingScope(**args["scope"]), args["after"], audit=args["audit"]
            )
        if operation == "collector.canonical_inventory":
            return self.source.canonical_inventory(
                SharingScope(**args["scope"]), args["after"]
            )
        if operation == "collector.sample":
            return self.source.sample(args["metadata"], args["state"])
        raise NodeProtocolError(
            "COLLECTOR_CALLBACK_INVALID", "Unexpected collector callback"
        )

    def get(self, key, default=None):
        return self._call("get", key=key, default=default)

    def put(self, key, value):
        return self._call("put", key=key, value=value)

    def tick(self):
        result = self._call(
            "tick",
            previous_stream_id=self.previous_stream_id,
            outbox_limit=OUTBOX_LIMIT,
        )
        self.status, self.sampled = result["status"], result["sampled"]
        return result["delay"]

    @property
    def conn(self):
        # Retained explicit qualification inspection/injection surface. Normal
        # Helper/centre never constructs a Python connection to the spool.
        if self._inspection is None:
            self._inspection = sqlite3.connect(self.path)
            self._inspection.row_factory = sqlite3.Row
        return self._inspection

    def close(self):
        if self._inspection is not None:
            self._inspection.close()
            self._inspection = None
        if hasattr(self, "worker"):
            self.worker.close()

    def __del__(self):
        self.close()


class LocalObservationRegistration:
    """Authenticated in-process source on the centre's own stable NodeIdentity.

    The local read_sessions grant is explicit and persisted. No self-pair,
    alternate identity, canonical task, execution or Provider is created.
    """
    def __init__(self,identity,registry,approvals,writer,stale_after=45,codex_host_id=None):
        self.identity,self.registry,self.approvals,self.writer=identity,registry,approvals,writer
        self.stale_after=stale_after
        self.codex_host_id=codex_host_id

    def heartbeat(self,identity,scope,*,port,capabilities):
        from clinx.nodes.node_protocol import NodeRecord,_now
        approved=self.approvals.get(self.identity.public["node_id"])
        if identity is not self.identity or approved is None or not approved.allows("observation.upload"):
            old=self.registry.get(self.identity.public["node_id"])
            if old: self.registry.revoke(old.node_id,old.user_scope)
            return {"registered":False,"error_code":"LOCAL_OBSERVATION_SCOPE_DENIED"}
        previous=self.registry.get(identity.public["node_id"])
        routes=(self.codex_host_id,) if self.codex_host_id else previous.route_ids if previous else ()
        record=NodeRecord(node_id=identity.public["node_id"],user_scope=approved.user_scope,
            public_key_fingerprint=identity.public["fingerprint"],display_name=identity.public["display_name"],
            route_ids=routes,
            capabilities=("session.read","session.status"),state="ONLINE",last_seen=_now(),stale_after_seconds=self.stale_after)
        self.registry.register(record,approved)
        return {"registered":True,"scope":approved.as_dict()}

    def upload(self,identity,scope,**batch):
        approved=self.approvals.get(self.identity.public["node_id"])
        if identity is not self.identity or approved is None or not approved.allows("observation.upload"):
            raise NodeProtocolError("LOCAL_OBSERVATION_SCOPE_DENIED","Local read_sessions approval is required")
        return self.writer.accept(identity.public["node_id"],approved,batch)
