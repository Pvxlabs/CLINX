"""CLINX multi-device node protocol and bounded routing.

The existing LAN pairing code proves a device identity, but it deliberately
only exposes an Observer bootstrap.  This module is the small, provider
neutral coordination layer that sits above that trust.  It keeps the centre
as an index and router; native history remains on the node that owns it.

No method in this module starts a provider or creates a task while reading a
thread.  Execution methods require an explicit sharing scope and an opaque,
idempotent request id.  A missing acknowledgement is represented as UNKNOWN
and is never retried by the router.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import socket
import ssl
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

NODE_PROTOCOL_VERSION = "clinx-node-v1"
SUPPORTED_PROVIDER = "codex_app_server"
NATIVE_HANDOFF_CAPABILITY = "execution.native_canonical_v1"
MAX_FRAME_BYTES = 256 * 1024
READ_OPERATIONS = frozenset({"node.status", "session.read", "session.status", "execution.context", "execution.status"})
NODE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}\Z")


class NodeProtocolError(ValueError):
    """A structured, safe-to-return protocol error."""

    def __init__(self, code: str, reason: str, **details: Any):
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {"error_code": self.code, "unavailable_reason": self.reason, **self.details}


def _text(name: str, value: str, *, max_len: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise ValueError(f"{name} must be a non-empty bounded string")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{name} contains control characters")
    return value.strip()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _age_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        stamp = dt.datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            return None
        return max(0.0, (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds())
    except (TypeError, ValueError):
        return None


@dataclasses.dataclass(frozen=True)
class SharingScope:
    """The durable user-approved range shared with a centre."""

    user_scope: str
    read_sessions: bool = False
    execute_tasks: bool = False
    providers: tuple[str, ...] = (SUPPORTED_PROVIDER,)
    revoked: bool = False
    updated_at: str = dataclasses.field(default_factory=_now)
    projects: tuple[str, ...] = ("*",)

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_scope", _text("user_scope", self.user_scope, max_len=256))
        if not isinstance(self.read_sessions, bool) or not isinstance(self.execute_tasks, bool) or not isinstance(self.revoked, bool):
            raise ValueError("permissions must be boolean")
        if any(not isinstance(v,(tuple,list)) or len(v)>64 for v in (self.projects,self.providers)):
            raise ValueError("providers and projects must be bounded lists")
        object.__setattr__(self, "projects", tuple(dict.fromkeys(_text("project", p, max_len=1024) for p in self.projects)))
        values = tuple(_text("provider", item, max_len=128) for item in self.providers)
        object.__setattr__(self, "providers", tuple(dict.fromkeys(values)))

    def allows(self, operation: str, provider: str = SUPPORTED_PROVIDER) -> bool:
        if self.revoked or provider not in self.providers:
            return False
        if operation in {"session.read", "session.status", "session.inventory", "observation.upload", "node.status"}:
            return self.read_sessions
        if operation.startswith("execution."):
            return self.execute_tasks
        return False

    def allows_project(self, project: str) -> bool:
        return "*" in self.projects or project in self.projects

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class NodeAuthorizationStore:
    """Atomic, durable user approvals for a centre/node relationship.

    The store is deliberately a small JSON file beside the existing device
    identity.  It is changed through the product entrypoints, never by a
    remote registration request, and contains no task or conversation data.
    """

    def __init__(self, root: str | Path, *, filename: str = "sharing.json"):
        from clinx.discovery.identity import PrivateStore

        self._store = PrivateStore(Path(root).expanduser())
        self.filename = filename
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        raw = self._store.read(self.filename)
        if raw is None:
            return {"schema_version": 1, "scopes": {}}
        scopes = raw.get("scopes")
        if not isinstance(scopes, dict):
            raise NodeProtocolError("INVALID_SHARING_STORE", "Sharing approvals are invalid")
        return raw

    def get(self, peer_id: str) -> SharingScope | None:
        with self._lock, self._store.lock():
            raw = self._read()
            value = raw["scopes"].get(peer_id)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise NodeProtocolError("INVALID_SHARING_STORE", "Sharing approval is invalid")
        return SharingScope(
            user_scope=value["user_scope"],
            read_sessions=value.get("read_sessions", False),
            execute_tasks=value.get("execute_tasks", False),
            providers=tuple(value.get("providers", (SUPPORTED_PROVIDER,))),
            projects=tuple(value.get("projects", ("*",))),
            revoked=value.get("revoked", False),
            updated_at=value.get("updated_at", _now()),
        )

    def grant(
        self,
        peer_id: str,
        *,
        user_scope: str,
        read_sessions: bool = False,
        execute_tasks: bool = False,
        providers: tuple[str, ...] = (SUPPORTED_PROVIDER,),
        projects: tuple[str, ...] = ("*",),
    ) -> SharingScope:
        scope = SharingScope(
            user_scope=user_scope,
            read_sessions=read_sessions,
            execute_tasks=execute_tasks,
            providers=providers,
            projects=projects,
            revoked=False,
            updated_at=_now(),
        )
        self._write(peer_id, scope)
        return scope

    def revoke(self, peer_id: str, *, user_scope: str | None = None) -> SharingScope:
        existing = self.get(peer_id)
        scope = SharingScope(
            user_scope=user_scope or (existing.user_scope if existing else "default"),
            read_sessions=False,
            execute_tasks=False,
            providers=existing.providers if existing else (SUPPORTED_PROVIDER,),
            revoked=True,
            updated_at=_now(),
        )
        self._write(peer_id, scope)
        return scope

    def _write(self, peer_id: str, scope: SharingScope) -> None:
        _text("peer_id", peer_id, max_len=63)
        with self._lock, self._store.lock():
            raw = self._read()
            scopes = dict(raw["scopes"])
            scopes[peer_id] = scope.as_dict()
            self._store.write(self.filename, {"schema_version": 1, "scopes": scopes})


@dataclasses.dataclass(frozen=True)
class NodeRecord:
    node_id: str
    user_scope: str = "default"
    public_key_fingerprint: str = ""
    protocol_version: str = NODE_PROTOCOL_VERSION
    providers: tuple[str, ...] = (SUPPORTED_PROVIDER,)
    capabilities: tuple[str, ...] = ("session.read", "session.status")
    route_ids: tuple[str, ...] = ()
    display_name: str = ""
    endpoint: str | None = None
    state: str = "UNKNOWN"
    last_seen: str | None = None
    stale_after_seconds: int = 120
    trusted: bool = True
    authorized: bool = True
    version_compatible: bool = True
    identity_generation: int = 1

    def __post_init__(self) -> None:
        node = _text("node_id", self.node_id, max_len=63).lower()
        if not NODE_ID_RE.fullmatch(node):
            raise ValueError("node_id must be a stable lowercase identifier")
        object.__setattr__(self, "node_id", node)
        object.__setattr__(self, "user_scope", _text("user_scope", self.user_scope, max_len=256))
        if not isinstance(self.identity_generation, int) or self.identity_generation < 1:
            raise ValueError("identity_generation must be positive")
        for name in ("providers", "capabilities", "route_ids"):
            values = tuple(_text(name, value, max_len=256) for value in getattr(self, name))
            object.__setattr__(self, name, tuple(dict.fromkeys(values)))
        if self.state not in {"ONLINE", "OFFLINE", "DEGRADED", "UNKNOWN", "REVOKED", "VERSION_INCOMPATIBLE"}:
            raise ValueError("unknown node state")

    @property
    def stale(self) -> bool:
        age = _age_seconds(self.last_seen)
        return age is None or age > self.stale_after_seconds

    def as_dict(self) -> dict[str, Any]:
        result = dataclasses.asdict(self)
        result["stale"] = self.stale
        result["age_seconds"] = _age_seconds(self.last_seen)
        return result


@dataclasses.dataclass(frozen=True)
class SessionIdentity:
    node_id: str
    user_scope: str
    provider: str
    native_thread_id: str
    source_version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _text("node_id", self.node_id, max_len=63).lower())
        for name in ("user_scope", "provider", "native_thread_id", "source_version"):
            object.__setattr__(self, name, _text(name, getattr(self, name), max_len=512))

    def as_dict(self) -> dict[str, str]:
        return dataclasses.asdict(self)


def _cursor(operation, values):
    from clinx.nodes.network_worker import read_command
    from clinx.authority.rust_authority import RustAuthorityError
    arguments=dict(values)
    secret=arguments.pop('secret')
    key=secret.encode() if isinstance(secret,str) else secret
    try:
        return read_command('cursor.'+operation,secret_hex=key.hex() if key else None,**arguments)
    except RustAuthorityError as exc:
        if str(exc) in {'INVALID_PROVIDER_CURSOR','INVALID_CURSOR_OFFSET'}:
            raise ValueError(str(exc)) from None
        raise NodeProtocolError(str(exc),'Cursor is bound to another node, user, provider, thread or source') from None


def encode_cursor(*,node_id,user_scope,provider,native_thread_id,source,source_version,offset,secret=None):
    return _cursor('encode',locals())


def decode_cursor(cursor,*,node_id,user_scope,provider,native_thread_id,source,source_version,secret=None):
    return _cursor('decode',locals())


def _encode_provider_cursor(*,node_id,user_scope,provider,native_thread_id,source,source_version,provider_cursor,secret=None):
    return _cursor('provider_encode',locals())


def _decode_node_cursor(cursor,*,node_id,user_scope,provider,native_thread_id,source,source_version,secret=None):
    return _cursor('provider_decode',locals())


class NodeRegistry:
    """Typed client for the Rust-owned Node index; never a task-state database."""

    def __init__(self, path: str | Path = ':memory:', *, cursor_secret: bytes | str | None = None):
        from clinx.nodes.network_worker import Worker
        self.path, self.cursor_secret = str(path), cursor_secret
        self.worker = Worker()
        try:
            self._call('init', path=self.path)
        except Exception:
            self.worker.close()
            raise

    def _call(self, method, **args):
        from clinx.authority.rust_authority import RustAuthorityError
        try:
            result = self.worker.call('registry.' + method, **args)
        except RustAuthorityError as exc:
            raise NodeProtocolError(str(exc), 'Node index request rejected') from None
        if isinstance(result, dict) and 'registry_error' in result:
            error = result['registry_error']
            raise NodeProtocolError(error['error_code'], 'Node index contract rejected', **error['details'])
        return result

    def close(self):
        self.worker.close()

    def __del__(self):
        if hasattr(self, 'worker'):
            self.close()

    def register(self, record, scope=None):
        self._call('register', record=dataclasses.asdict(record), scope=scope.as_dict() if scope else None)
        return record

    def get(self, node_id):
        record = self._call('get', node_id=node_id)
        return NodeRecord(**record) if record else None

    def list_nodes(self, *, user_scope=None, authorized_only=False):
        return [NodeRecord(**r) for r in self._call('list_nodes', user_scope=user_scope, authorized_only=authorized_only)]

    def scope(self, node_id, user_scope):
        scope = self._call('scope', node_id=node_id, user_scope=user_scope)
        return SharingScope(**scope) if scope else None

    def authorize(self, node_id, scope):
        self._call('authorize', node_id=node_id, scope=scope.as_dict())

    def revoke(self, node_id, user_scope):
        self._call('revoke', node_id=node_id, user_scope=user_scope)

    def bind_reference(self, reference, node_id, user_scope):
        self._call('bind_reference', reference=reference, node_id=node_id, user_scope=user_scope)

    def reference_node(self, reference, user_scope):
        return self._call('reference_node', reference=reference, user_scope=user_scope)

    def touch(self, node_id, *, state='ONLINE'):
        if state not in {'ONLINE', 'DEGRADED', 'OFFLINE', 'UNKNOWN'}:
            raise ValueError('invalid node state')
        self._call('touch', node_id=node_id, state=state)

    def update_endpoint(self, node_id, endpoint):
        self._call('update_endpoint', node_id=node_id, endpoint=_text('endpoint', endpoint, max_len=256))

    def refresh_states(self):
        self._call('refresh_states')

    def update_thread(self, identity, *, source, status=None, observed_at=None):
        self._call('update_thread', identity=identity.as_dict(), source=_text('source', source),
            status=dict(status or {}), observed_at=observed_at)

    def lookup_thread(self, *, native_thread_id, user_scope, provider=SUPPORTED_PROVIDER):
        return self._call('lookup_thread', native_thread_id=native_thread_id, user_scope=user_scope, provider=provider)

    def claim_request(self, *, node_id, request_id, operation, payload):
        # Preserve the deployed Python JSON bytes; hashing and the atomic claim
        # are Rust-owned. Existing non-ASCII/float ledger entries remain valid.
        return tuple(self._call('claim_request', node_id=node_id, request_id=request_id, operation=operation,
            payload=dict(payload), payload_wire=json.dumps(dict(payload), sort_keys=True, separators=(',', ':'))))

    def finish_request(self, *, node_id, request_id, state, response):
        self._call('finish_request', node_id=node_id, request_id=request_id, state=state, response=dict(response))

    def acquire_fence(self, identity, execution_ref):
        return self._call('acquire_fence', identity=identity.as_dict(), execution_ref=execution_ref)

    def release_fence(self, identity, execution_ref):
        self._call('release_fence', identity=identity.as_dict(), execution_ref=execution_ref)


class NodeService:
    """Node-side request handler.  Callbacks are existing provider adapters."""

    def __init__(
        self,
        record: NodeRecord,
        registry: NodeRegistry,
        *,
        scope: SharingScope,
        read_thread: Callable[..., Mapping[str, Any]],
        status_thread: Callable[..., Mapping[str, Any]] | None = None,
        adopt_conversation: Callable[..., Mapping[str, Any]] | None = None,
        context_execution: Callable[..., Mapping[str, Any]] | None = None,
        prepare_execution: Callable[..., Mapping[str, Any]] | None = None,
        start_execution: Callable[..., Mapping[str, Any]] | None = None,
        status_execution: Callable[..., Mapping[str, Any]] | None = None,
        cancel_execution: Callable[..., Mapping[str, Any]] | None = None,
        reconcile_execution: Callable[..., Mapping[str, Any]] | None = None,
    ):
        self.record, self.registry, self.scope = record, registry, scope
        self.read_thread = read_thread
        self.status_thread = status_thread or read_thread
        self.adopt_conversation = adopt_conversation
        self.context_execution = context_execution
        self.prepare_execution = prepare_execution
        self.start_execution = start_execution
        self.status_execution = status_execution
        self.cancel_execution = cancel_execution
        self.reconcile_execution = reconcile_execution
        self._scope_lock = threading.RLock()

    def set_scope(self, scope: SharingScope) -> None:
        """Apply the latest centre approval without replacing the service."""
        with self._scope_lock:
            self.scope = scope
            self.record = dataclasses.replace(
                self.record,
                user_scope=scope.user_scope,
                authorized=not scope.revoked,
                state=("REVOKED" if scope.revoked else
                       "ONLINE" if self.record.state == "REVOKED" else self.record.state),
            )

    def _snapshot(self) -> tuple[NodeRecord, SharingScope]:
        with self._scope_lock:
            return self.record, self.scope

    def set_state(self, state: str, *, last_seen: str | None = None) -> None:
        if state not in {"ONLINE", "OFFLINE", "DEGRADED", "UNKNOWN", "REVOKED", "VERSION_INCOMPATIBLE"}:
            raise ValueError("invalid node state")
        with self._scope_lock:
            self.record = dataclasses.replace(self.record, state=state, last_seen=last_seen or self.record.last_seen)

    def _authorize(self, operation, request):
        self._invoke('authorize', request, permission=operation)

    def _project(self,operation,request):
        from clinx.nodes.network_worker import Worker
        from clinx.authority.rust_authority import RustAuthorityError
        def callback(event):
            method,args=event['operation'],event['arguments']
            if method=='service.snapshot':
                record,scope=self._snapshot()
                return dict(record=record.as_dict(),scope=scope.as_dict())
            if method=='service.read':
                return dict(self.read_thread(args['thread_id'],**args['kwargs']))
            if method=='service.status':
                return dict(self.status_thread(args['thread_id']))
            if method=='registry.update_thread':
                self.registry.update_thread(SessionIdentity(**args['identity']),source=args['source'],status=args['status'])
                return None
            raise NodeProtocolError('NODE_CALLBACK_INVALID','Unexpected read projection callback')
        secret=self.registry.cursor_secret
        key=secret.encode() if isinstance(secret,str) else secret
        record,scope=self._snapshot()
        worker=Worker(callback=callback)
        try:
            return worker.call('router.service_'+operation,request=dict(request),record=record.as_dict(),
                scope=scope.as_dict(),secret_hex=key.hex() if key else None)
        except RustAuthorityError as exc:
            raise NodeProtocolError(str(exc),'Node read projection rejected') from None
        finally:
            worker.close()

    def read(self,request):
        return self._project('read',request)

    def status(self,request):
        return self._project('status',request)

    def _invoke(self, operation, request, *, permission=None):
        from clinx.authority.rust_authority import RustAuthorityError
        from clinx.nodes.network_worker import Worker
        callbacks = {
            'session.read': self.read, 'session.status': self.status,
            'execution.adopt': self.adopt_conversation, 'execution.context': self.context_execution,
            'execution.prepare': self.prepare_execution, 'execution.start': self.start_execution,
            'execution.status': self.status_execution, 'execution.cancel': self.cancel_execution,
            'execution.reconcile': self.reconcile_execution,
        }
        def callback(message):
            if message['kind'] != 'callback':
                raise RustAuthorityError('NODE_CALLBACK_INVALID')
            method, args = message['operation'], message['arguments']
            if method.startswith('registry.'):
                args = dict(args)
                if 'identity' in args:
                    args['identity'] = SessionIdentity(**args['identity'])
                try:
                    result = getattr(self.registry, method.removeprefix('registry.'))(**args)
                except NodeProtocolError as exc:
                    if method != 'registry.acquire_fence':
                        raise
                    return dict(callback_error=dict(code=exc.code, reason=exc.reason, details=exc.details))
                return None if method in {'registry.finish_request', 'registry.release_fence'} else result
            if method != 'execute':
                raise RustAuthorityError('NODE_CALLBACK_INVALID')
            target = callbacks[args['operation']]
            try:
                value = target(args['request']) if args['operation'].startswith('session.') else target(**args['request'])
                return dict(value)
            except Exception as exc:
                error = dict(code=getattr(exc, 'code', type(exc).__name__), reason=str(exc)[:500],
                    details=getattr(exc, 'details', {}))
                if hasattr(exc, 'side_effect'):
                    error['side_effect'] = exc.side_effect
                return dict(callback_error=error)
        record, scope = self._snapshot()
        worker = Worker(callback=callback)
        try:
            return worker.call('service.handle', operation=operation, permission=permission,
                request=dict(request), record=record.as_dict(), scope=scope.as_dict(),
                available={k: v is not None for k, v in callbacks.items()},
                authority_owned=getattr(getattr(self.start_execution, '__self__', None), 'authority_owns_execution', False) is True)
        except RustAuthorityError as exc:
            raise NodeProtocolError(str(exc), 'Node request rejected') from None
        finally:
            worker.close()

    def prepare(self, request):
        return self._invoke('execution.prepare', request)

    def start(self, request):
        return self._invoke('execution.start', request)

    def execution_status(self, request):
        return self._invoke('execution.status', request)

    def reconcile(self, request):
        return self._invoke('execution.reconcile', request)

    def cancel(self, request):
        return self._invoke('execution.cancel', request)

    def handle(self, request):
        operation = request.get('operation')
        try:
            return self._invoke(operation, request)
        except NodeProtocolError as exc:
            return exc.as_dict() | {'node_id': self.record.node_id,
                'read_only': operation.startswith('session.') if isinstance(operation, str) else True}


class NodeRouter:
    """Centre-side exact routing and bounded unknown-source discovery."""

    def __init__(self, registry: NodeRegistry, *, user_scope: str, local_node_id: str | None = None, max_workers: int = 4, timeout_seconds: float = 2.0, remote_client_factory: Callable[[NodeRecord], Any] | None = None):
        self.registry, self.user_scope, self.local_node_id = registry, _text("user_scope", user_scope), local_node_id
        self.max_workers, self.timeout_seconds = max(1, min(max_workers, 16)), max(0.05, timeout_seconds)
        self._readers: dict[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] = {}
        self._executors: dict[str, Callable[[str, Mapping[str, Any]], Mapping[str, Any]]] = {}
        self._remote_client_factory = remote_client_factory
        self._remote_clients: dict[str, Any] = {}

    def attach(self, record: NodeRecord, *, reader: Callable[[Mapping[str, Any]], Mapping[str, Any]], executor: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None, scope: SharingScope | None = None) -> None:
        self.registry.register(record, scope)
        self._readers[record.node_id] = reader
        if executor is not None:
            self._executors[record.node_id] = executor

    def attach_existing(self, node_id: str, *, reader: Callable[[Mapping[str, Any]], Mapping[str, Any]], executor: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None) -> None:
        """Bind callbacks without registering identity or changing centre grants."""
        if self.registry.get(node_id) is None:
            raise NodeProtocolError("UNKNOWN_NODE", "Reader requires an existing centre node")
        self._readers[node_id] = reader
        if executor is not None:
            self._executors[node_id] = executor

    def set_remote_client_factory(self, factory: Callable[[NodeRecord], Any] | None) -> None:
        self._remote_client_factory = factory

    def attach_remote(self, record: NodeRecord, client: Any, *, scope: SharingScope | None = None) -> None:
        """Attach a real NodeRPCClient; registration remains centre-owned."""
        self.registry.register(record, scope)
        self._remote_clients[record.node_id] = client
        self._readers[record.node_id] = lambda request, rpc=client: rpc.call(request)
        self._executors[record.node_id] = lambda operation, request, rpc=client: rpc.execute(
            operation, **{key: value for key, value in request.items() if key != "operation"}
        )

    def _reader_for(self, record: NodeRecord) -> Callable[[Mapping[str, Any]], Mapping[str, Any]] | None:
        reader = self._readers.get(record.node_id)
        if reader is not None:
            return reader
        if self._remote_client_factory is None or not record.endpoint:
            return None
        try:
            client = self._remote_client_factory(record)
        except Exception:
            return None
        if client is None:
            return None
        self._remote_clients[record.node_id] = client
        self.attach_existing(record.node_id, reader=lambda request: client.call(request),
            executor=lambda operation, request: client.execute(operation, **{
                key: value for key, value in request.items() if key != "operation"}))
        return self._readers.get(record.node_id)

    def _invoke(self,rpc_operation,**arguments):
        from clinx.nodes.network_worker import Worker
        from clinx.authority.rust_authority import RustAuthorityError
        def callback(event):
            method,args=event['operation'],event['arguments']
            if method.startswith('registry.'):
                result=getattr(self.registry,method.removeprefix('registry.'))(**args)
                if method=='registry.list_nodes':
                    return [record.as_dict() for record in result]
                return result.as_dict() if hasattr(result,'as_dict') else result
            record=NodeRecord(**{k:v for k,v in args.get('record',{}).items() if k not in {'stale','age_seconds'}}) if args.get('record') else None
            if method=='router.reader':
                return self._reader_for(record) is not None
            if method=='router.executor':
                if record.node_id not in self._executors:
                    self._reader_for(record)
                return record.node_id in self._executors
            try:
                if method=='router.read':
                    return dict(result=dict(self._readers[record.node_id](args['request'])))
                if method=='router.execute':
                    return dict(result=dict(self._executors[args['node_id']](args['operation'],args['request'])))
            except (OSError,TimeoutError) as exc:
                return dict(error_type=type(exc).__name__)
            raise NodeProtocolError('NODE_CALLBACK_INVALID','Unexpected router callback')
        worker=Worker(callback=callback)
        try:
            return worker.call('router.'+rpc_operation,user_scope=self.user_scope,local_node_id=self.local_node_id,
                local_readers=[node for node in self._readers if node not in self._remote_clients],
                executors=list(self._executors),max_workers=self.max_workers,timeout_seconds=self.timeout_seconds,**arguments)
        except RustAuthorityError as exc:
            raise NodeProtocolError(str(exc),'Node routing request rejected') from None
        finally:
            worker.close()

    def nodes(self):
        return self._invoke('nodes')

    def _node_for(self,*,node_id=None,host=None):
        record=self._invoke('target',node_id=node_id,host=host)
        return NodeRecord(**{k:v for k,v in record.items() if k not in {'stale','age_seconds'}})

    @staticmethod
    def _found(result,thread_id):
        from clinx.nodes.network_worker import command
        return command('router.found',result=dict(result),thread_id=thread_id)

    @staticmethod
    def _answered(result,thread_id):
        from clinx.nodes.network_worker import command
        return command('router.answered',result=dict(result),thread_id=thread_id)

    def read(self,*,thread_id,node_id=None,host=None,provider=SUPPORTED_PROVIDER,cursor=None,recent_turns=8,max_bytes=32000):
        return self._invoke('read',thread_id=thread_id,node_id=node_id,host=host,provider=provider,
            cursor=cursor,recent_turns=recent_turns,max_bytes=max_bytes)

    def execute(self,*,node_id,operation,request):
        return self._invoke('execute',node_id=node_id,operation=operation,request=dict(request))


class NodeRPCServer:
    """Rust owns the listener, TLS, peer verification, framing and connection IO."""
    def __init__(self, service, *, address='127.0.0.1', port=0, ssl_context=None,
                 ssl_context_factory=None, request_handler=None, allow_insecure_loopback=False):
        from clinx.nodes.network_worker import PairedContext, PeerSocket, Worker
        self.service, self.request_handler = service, request_handler
        context = ssl_context_factory() if ssl_context_factory else ssl_context
        if context is not None and not isinstance(context, PairedContext):
            raise ValueError('NodeRPCServer requires an existing paired identity context')
        if context is None and not (allow_insecure_loopback and address in {'127.0.0.1', '::1'}):
            raise ValueError('NodeRPCServer requires mTLS or explicit loopback test mode')
        self.ssl_context, self.ssl_context_factory = context, ssl_context_factory
        def incoming(event):
            sock = PeerSocket(event['certificate_hex'], event['peer'])
            return self.handle_request(event['request'], sock, tuple(event['address']))
        self.worker = Worker(incoming)
        try:
            result = self.worker.call('listen', address=address, port=port,
                state=str(context.state) if context else None,
                allow_insecure_loopback=allow_insecure_loopback)
        except Exception:
            self.worker.close()
            raise
        self.address = tuple(result['address'])

    def handle_request(self, request, sock, address):
        if self.request_handler is not None:
            return dict(self.request_handler(request, sock, address))
        return dict(self.service.handle(request))

    def close(self):
        self.worker.close()


class NodeRPCClient:
    def __init__(self, address: tuple[str, int], *, ssl_context: ssl.SSLContext | None = None, timeout_seconds: float = 2.0, allow_insecure_loopback: bool = False):
        if ssl_context is None and not (allow_insecure_loopback and address[0] in {"127.0.0.1", "::1"}):
            raise ValueError("NodeRPCClient requires mTLS or explicit loopback test mode")
        self.address, self.ssl_context, self.timeout_seconds = address, ssl_context, timeout_seconds

    def call(self, request: Mapping[str, Any]) -> dict[str, Any]:
        from clinx.authority.rust_authority import RustAuthorityError
        from clinx.nodes.network_worker import PairedContext, command
        if isinstance(self.ssl_context, PairedContext):
            host, port = self.address
            endpoint = f'tls://{("["+host+"]") if ":" in host else host}:{port}'
            try:
                return command('network.call', state=str(self.ssl_context.state),
                    peer_id=self.ssl_context.peer_id, endpoint=endpoint, timeout=self.timeout_seconds,
                    request=dict(request))
            except RustAuthorityError as exc:
                if str(exc) == 'NODE_RESPONSE_INVALID':
                    raise ValueError('node response is not an object') from None
                raise TimeoutError(str(exc)) from None

        with socket.create_connection(self.address, timeout=self.timeout_seconds) as raw:
            sock = self.ssl_context.wrap_socket(raw, server_hostname=self.address[0]) if self.ssl_context is not None else raw
            with sock:
                payload = json.dumps(dict(request), separators=(",", ":")).encode() + b"\n"
                sock.sendall(payload)
                data = b""
                while not data.endswith(b"\n") and len(data) <= MAX_FRAME_BYTES:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                if not data or len(data) > MAX_FRAME_BYTES:
                    raise TimeoutError("node response unavailable")
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise ValueError("node response is not an object")
                return result

    def read(self, **kwargs: Any) -> dict[str, Any]:
        return self.call({"operation": "session.read", "protocol_version": NODE_PROTOCOL_VERSION, **kwargs})

    def execute(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        return self.call({"operation": operation, "protocol_version": NODE_PROTOCOL_VERSION, **kwargs})
