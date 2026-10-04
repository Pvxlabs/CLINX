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

import base64
import concurrent.futures
import dataclasses
import datetime as dt
import hashlib
import hmac
import json
import re
import secrets
import socket
import socketserver
import sqlite3
import ssl
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping


NODE_PROTOCOL_VERSION = "clinx-node-v1"
SUPPORTED_PROVIDER = "codex_app_server"
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
        from local_discovery.identity import PrivateStore

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


def encode_cursor(
    *,
    node_id: str,
    user_scope: str,
    provider: str,
    native_thread_id: str,
    source: str,
    source_version: str,
    offset: int,
    secret: bytes | str | None = None,
) -> str:
    if not isinstance(offset, int) or offset < 0:
        raise ValueError("cursor offset must be a non-negative integer")
    body = [1, node_id, user_scope, provider, native_thread_id, source, source_version, offset]
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
    if secret:
        key = secret.encode() if isinstance(secret, str) else secret
        raw += b"." + hmac.new(key, raw, hashlib.sha256).hexdigest().encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(
    cursor: str,
    *,
    node_id: str,
    user_scope: str,
    provider: str,
    native_thread_id: str,
    source: str,
    source_version: str,
    secret: bytes | str | None = None,
) -> int:
    try:
        if not isinstance(cursor, str) or len(cursor) > 4096:
            raise ValueError()
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        if secret:
            payload, signature = raw.rsplit(b".", 1)
            key = secret.encode() if isinstance(secret, str) else secret
            expected = hmac.new(key, payload, hashlib.sha256).hexdigest().encode()
            if not hmac.compare_digest(signature, expected):
                raise ValueError()
            raw = payload
        value = json.loads(raw)
        expected = [1, node_id, user_scope, provider, native_thread_id, source, source_version]
        if value[:7] != expected or len(value) != 8 or type(value[7]) is not int or value[7] < 0:
            raise ValueError()
        return value[7]
    except (ValueError, TypeError, IndexError, json.JSONDecodeError):
        raise NodeProtocolError("INVALID_CONTEXT_CURSOR", "Cursor is bound to another node, user, provider, thread or source") from None


def _encode_provider_cursor(
    *,
    node_id: str,
    user_scope: str,
    provider: str,
    native_thread_id: str,
    source: str,
    source_version: str,
    provider_cursor: str,
    secret: bytes | str | None = None,
) -> str:
    """Bind an opaque provider page token to this node context.

    Native providers are allowed to choose their own cursor format.  The node
    envelope prevents that token from being replayed for another node/user/
    provider/thread/source while keeping the provider payload opaque to the
    centre index.
    """
    if not isinstance(provider_cursor, str) or not provider_cursor or len(provider_cursor) > 4096:
        raise ValueError("provider cursor must be a bounded non-empty string")
    body = [2, node_id, user_scope, provider, native_thread_id, source, source_version, provider_cursor]
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
    if secret:
        key = secret.encode() if isinstance(secret, str) else secret
        raw += b"." + hmac.new(key, raw, hashlib.sha256).hexdigest().encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_node_cursor(
    cursor: str,
    *,
    node_id: str,
    user_scope: str,
    provider: str,
    native_thread_id: str,
    source: str,
    source_version: str,
    secret: bytes | str | None = None,
) -> str | None:
    """Decode a node cursor and return the provider token it carries.

    Version 1 cursors (the public integer-offset helper) remain accepted for
    callers that page a node-owned projection.  Version 2 carries the opaque
    native provider token used by ``NativeHistory``.
    """
    try:
        if not isinstance(cursor, str) or len(cursor) > 8192:
            raise ValueError()
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        if secret:
            payload, signature = raw.rsplit(b".", 1)
            key = secret.encode() if isinstance(secret, str) else secret
            expected = hmac.new(key, payload, hashlib.sha256).hexdigest().encode()
            if not hmac.compare_digest(signature, expected):
                raise ValueError()
            raw = payload
        value = json.loads(raw)
        expected = [value[0], node_id, user_scope, provider, native_thread_id, source, source_version]
        if len(value) != 8 or value[:7] != expected:
            raise ValueError()
        if value[0] == 1 and type(value[7]) is int and value[7] >= 0:
            return None
        if value[0] == 2 and isinstance(value[7], str) and value[7]:
            return value[7]
        raise ValueError()
    except (ValueError, TypeError, IndexError, json.JSONDecodeError):
        raise NodeProtocolError("INVALID_CONTEXT_CURSOR", "Cursor is bound to another node, user, provider, thread or source") from None


class NodeRegistry:
    """Incremental centre index; it never stores native conversation bodies."""

    def __init__(self, path: str | Path = ":memory:", *, cursor_secret: bytes | str | None = None):
        self.path = str(path)
        self.cursor_secret = cursor_secret
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=2000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                node_id TEXT PRIMARY KEY, user_scope TEXT NOT NULL,
                public_key_fingerprint TEXT NOT NULL, protocol_version TEXT NOT NULL,
                providers_json TEXT NOT NULL, capabilities_json TEXT NOT NULL,
                route_ids_json TEXT NOT NULL, display_name TEXT NOT NULL,
                endpoint TEXT, state TEXT NOT NULL, last_seen TEXT,
                stale_after_seconds INTEGER NOT NULL, trusted INTEGER NOT NULL,
                authorized INTEGER NOT NULL, version_compatible INTEGER NOT NULL,
                identity_generation INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sharing_scopes (
                node_id TEXT NOT NULL, user_scope TEXT NOT NULL,
                read_sessions INTEGER NOT NULL, execute_tasks INTEGER NOT NULL,
                providers_json TEXT NOT NULL, revoked INTEGER NOT NULL,
                updated_at TEXT NOT NULL, PRIMARY KEY(node_id, user_scope)
            );
            CREATE TABLE IF NOT EXISTS observation_projects (
                node_id TEXT NOT NULL, user_scope TEXT NOT NULL, projects_json TEXT NOT NULL,
                PRIMARY KEY(node_id,user_scope)
            );
            CREATE TABLE IF NOT EXISTS thread_index (
                node_id TEXT NOT NULL, user_scope TEXT NOT NULL, provider TEXT NOT NULL,
                native_thread_id TEXT NOT NULL, source TEXT NOT NULL, source_version TEXT NOT NULL,
                last_seen TEXT NOT NULL, status_json TEXT NOT NULL,
                PRIMARY KEY(node_id, user_scope, provider, native_thread_id, source)
            );
            CREATE INDEX IF NOT EXISTS idx_thread_exact ON thread_index(user_scope, provider, native_thread_id);
            CREATE TABLE IF NOT EXISTS request_ledger (
                node_id TEXT NOT NULL, request_id TEXT NOT NULL, operation TEXT NOT NULL,
                payload_hash TEXT NOT NULL, state TEXT NOT NULL, response_json TEXT,
                updated_at TEXT NOT NULL, PRIMARY KEY(node_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS reference_routes (
                reference TEXT PRIMARY KEY, node_id TEXT NOT NULL, user_scope TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS route_fences (
                node_id TEXT NOT NULL, user_scope TEXT NOT NULL, provider TEXT NOT NULL,
                native_thread_id TEXT NOT NULL, fence INTEGER NOT NULL,
                owner_execution TEXT, PRIMARY KEY(node_id, user_scope, provider, native_thread_id)
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def register(self, record: NodeRecord, scope: SharingScope | None = None) -> NodeRecord:
        with self._lock, self._conn:
            duplicate = self._conn.execute(
                "SELECT node_id FROM nodes WHERE public_key_fingerprint=? AND node_id<>?",
                (record.public_key_fingerprint, record.node_id),
            ).fetchone() if record.public_key_fingerprint else None
            if duplicate:
                raise NodeProtocolError("DUPLICATE_NODE_IDENTITY", "Trusted public key is already bound to another node", existing_node_id=duplicate[0])
            old = self._conn.execute("SELECT * FROM nodes WHERE node_id=?", (record.node_id,)).fetchone()
            if old and old["public_key_fingerprint"] != record.public_key_fingerprint:
                raise NodeProtocolError("NODE_IDENTITY_CONFLICT", "Stable node_id changed its trusted public key")
            self._conn.execute(
                """INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(node_id) DO UPDATE SET
                   user_scope=excluded.user_scope, protocol_version=excluded.protocol_version,
                   providers_json=excluded.providers_json, capabilities_json=excluded.capabilities_json,
                   route_ids_json=excluded.route_ids_json, display_name=excluded.display_name,
                   endpoint=excluded.endpoint, state=excluded.state, last_seen=excluded.last_seen,
                   stale_after_seconds=excluded.stale_after_seconds, trusted=excluded.trusted,
                   authorized=excluded.authorized, version_compatible=excluded.version_compatible,
                   identity_generation=excluded.identity_generation""",
                (
                    record.node_id, record.user_scope, record.public_key_fingerprint, record.protocol_version,
                    json.dumps(record.providers), json.dumps(record.capabilities), json.dumps(record.route_ids),
                    record.display_name, record.endpoint, record.state, record.last_seen, record.stale_after_seconds,
                    int(record.trusted), int(record.authorized), int(record.version_compatible), record.identity_generation,
                ),
            )
            if scope is not None:
                self._conn.execute("INSERT OR REPLACE INTO observation_projects VALUES (?,?,?)",
                    (record.node_id, scope.user_scope, json.dumps(scope.projects)))
                self._conn.execute(
                    """INSERT INTO sharing_scopes VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(node_id,user_scope) DO UPDATE SET
                       read_sessions=excluded.read_sessions, execute_tasks=excluded.execute_tasks,
                       providers_json=excluded.providers_json, revoked=excluded.revoked,
                       updated_at=excluded.updated_at""",
                    (record.node_id, scope.user_scope, int(scope.read_sessions), int(scope.execute_tasks),
                     json.dumps(scope.providers), int(scope.revoked), scope.updated_at),
                )
        return record

    def bind_reference(self, reference: str, node_id: str, user_scope: str) -> None:
        """Routing only: canonical task/execution state remains on its owner."""
        with self._lock, self._conn:
            row = self._conn.execute("SELECT node_id,user_scope FROM reference_routes WHERE reference=?", (reference,)).fetchone()
            if row and tuple(row) != (node_id, user_scope):
                raise NodeProtocolError("REFERENCE_ROUTE_CONFLICT", "Reference already belongs to another node")
            self._conn.execute("INSERT OR IGNORE INTO reference_routes VALUES (?,?,?)", (reference, node_id, user_scope))

    def reference_node(self, reference: str, user_scope: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT node_id,user_scope FROM reference_routes WHERE reference=?", (reference,)).fetchone()
        if row and row['user_scope'] != user_scope:
            raise NodeProtocolError("USER_SCOPE_DENIED", "Reference belongs to another user scope")
        return row['node_id'] if row else None

    def revoke(self, node_id: str, user_scope: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE nodes SET authorized=0,state='REVOKED' WHERE node_id=?", (node_id,))
            self._conn.execute("UPDATE sharing_scopes SET revoked=1,updated_at=? WHERE node_id=? AND user_scope=?", (_now(), node_id, user_scope))

    def _record(self, row: sqlite3.Row) -> NodeRecord:
        return NodeRecord(
            node_id=row["node_id"], user_scope=row["user_scope"], public_key_fingerprint=row["public_key_fingerprint"],
            protocol_version=row["protocol_version"], providers=tuple(json.loads(row["providers_json"])),
            capabilities=tuple(json.loads(row["capabilities_json"])), route_ids=tuple(json.loads(row["route_ids_json"])),
            display_name=row["display_name"], endpoint=row["endpoint"], state=row["state"], last_seen=row["last_seen"],
            stale_after_seconds=row["stale_after_seconds"], trusted=bool(row["trusted"]), authorized=bool(row["authorized"]),
            version_compatible=bool(row["version_compatible"]), identity_generation=row["identity_generation"],
        )

    def get(self, node_id: str) -> NodeRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM nodes WHERE node_id=?", (node_id,)).fetchone()
        return self._record(row) if row else None

    def touch(self, node_id: str, *, state: str = "ONLINE") -> None:
        """Record one successful authenticated observation without scanning history."""
        if state not in {"ONLINE", "DEGRADED", "OFFLINE", "UNKNOWN"}:
            raise ValueError("invalid node state")
        with self._lock, self._conn:
            self._conn.execute("UPDATE nodes SET state=?,last_seen=? WHERE node_id=?", (state, _now(), node_id))

    def update_endpoint(self, node_id: str, endpoint: str) -> None:
        endpoint = _text("endpoint", endpoint, max_len=256)
        with self._lock, self._conn:
            self._conn.execute("UPDATE nodes SET endpoint=? WHERE node_id=?", (endpoint, node_id))

    def refresh_states(self) -> None:
        """Mark expired authenticated observations offline before routing."""
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT node_id,last_seen,stale_after_seconds,state FROM nodes"
            ).fetchall()
            for row in rows:
                if row["state"] in {"REVOKED", "VERSION_INCOMPATIBLE"}:
                    continue
                age = _age_seconds(row["last_seen"])
                if age is None or age > row["stale_after_seconds"]:
                    self._conn.execute(
                        "UPDATE nodes SET state='OFFLINE' WHERE node_id=?", (row["node_id"],)
                    )

    def authorize(self, node_id: str, scope: SharingScope) -> None:
        """Persist an explicit centre-side approval without changing identity."""
        with self._lock, self._conn:
            row = self._conn.execute("SELECT node_id FROM nodes WHERE node_id=?", (node_id,)).fetchone()
            if row is None:
                raise NodeProtocolError("UNKNOWN_NODE", "Cannot authorize an unregistered node")
            self._conn.execute(
                """INSERT INTO sharing_scopes VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(node_id,user_scope) DO UPDATE SET
                   read_sessions=excluded.read_sessions, execute_tasks=excluded.execute_tasks,
                   providers_json=excluded.providers_json, revoked=excluded.revoked,
                   updated_at=excluded.updated_at""",
                (node_id, scope.user_scope, int(scope.read_sessions), int(scope.execute_tasks),
                 json.dumps(scope.providers), int(scope.revoked), scope.updated_at),
            )
            self._conn.execute("INSERT OR REPLACE INTO observation_projects VALUES (?,?,?)",
                (node_id, scope.user_scope, json.dumps(scope.projects)))
            state = "REVOKED" if scope.revoked else "ONLINE"
            self._conn.execute(
                "UPDATE nodes SET user_scope=?,authorized=?,state=? WHERE node_id=?",
                (scope.user_scope, int(not scope.revoked), state, node_id),
            )

    def list_nodes(self, *, user_scope: str | None = None, authorized_only: bool = False) -> list[NodeRecord]:
        sql, args = "SELECT * FROM nodes", []
        where = []
        if user_scope is not None:
            where.append("user_scope=?"); args.append(user_scope)
        if authorized_only:
            where.append("authorized=1 AND trusted=1")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY node_id"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._record(row) for row in rows]

    def scope(self, node_id: str, user_scope: str) -> SharingScope | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sharing_scopes WHERE node_id=? AND user_scope=?", (node_id, user_scope)).fetchone()
            projects = self._conn.execute("SELECT projects_json FROM observation_projects WHERE node_id=? AND user_scope=?", (node_id,user_scope)).fetchone()
        if not row:
            return None
        return SharingScope(projects=tuple(json.loads(projects[0])) if projects else ("*",), user_scope=row["user_scope"], read_sessions=bool(row["read_sessions"]), execute_tasks=bool(row["execute_tasks"]), providers=tuple(json.loads(row["providers_json"])), revoked=bool(row["revoked"]), updated_at=row["updated_at"])

    def update_thread(self, identity: SessionIdentity, *, source: str, status: Mapping[str, Any] | None = None, observed_at: str | None = None) -> None:
        source = _text("source", source, max_len=256)
        stamp = observed_at or _now()
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO thread_index VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(node_id,user_scope,provider,native_thread_id,source) DO UPDATE SET
                   source_version=excluded.source_version,last_seen=excluded.last_seen,status_json=excluded.status_json""",
                (identity.node_id, identity.user_scope, identity.provider, identity.native_thread_id,
                 source, identity.source_version, stamp, json.dumps(dict(status or {}), sort_keys=True)),
            )

    def lookup_thread(self, *, native_thread_id: str, user_scope: str, provider: str = SUPPORTED_PROVIDER) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM thread_index WHERE native_thread_id=? AND user_scope=? AND provider=? ORDER BY node_id,source",
                (native_thread_id, user_scope, provider),
            ).fetchall()
        return [dict(row, status=json.loads(row["status_json"])) for row in rows]

    def claim_request(self, *, node_id: str, request_id: str, operation: str, payload: Mapping[str, Any]) -> tuple[str, dict[str, Any] | None]:
        digest = hashlib.sha256(json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM request_ledger WHERE node_id=? AND request_id=?", (node_id, request_id)).fetchone()
            if row:
                if row["operation"] != operation or row["payload_hash"] != digest:
                    raise NodeProtocolError("IDEMPOTENCY_KEY_REUSE", "request_id was used for another operation")
                return row["state"], json.loads(row["response_json"]) if row["response_json"] else None
            self._conn.execute("INSERT INTO request_ledger VALUES (?,?,?,?,?,?,?)", (node_id, request_id, operation, digest, "IN_FLIGHT", None, _now()))
        return "NEW", None

    def finish_request(self, *, node_id: str, request_id: str, state: str, response: Mapping[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE request_ledger SET state=?,response_json=?,updated_at=? WHERE node_id=? AND request_id=?", (state, json.dumps(dict(response), sort_keys=True), _now(), node_id, request_id))

    def acquire_fence(self, identity: SessionIdentity, execution_ref: str) -> int:
        with self._lock, self._conn:
            row = self._conn.execute("SELECT fence,owner_execution FROM route_fences WHERE node_id=? AND user_scope=? AND provider=? AND native_thread_id=?", (identity.node_id, identity.user_scope, identity.provider, identity.native_thread_id)).fetchone()
            if row and row["owner_execution"] not in (None, execution_ref):
                raise NodeProtocolError("SINGLE_WRITER_BUSY", "Another execution owns this native thread", owner_execution=row["owner_execution"], fence=row["fence"])
            fence = (row["fence"] if row else 0) + 1
            self._conn.execute("INSERT INTO route_fences VALUES (?,?,?,?,?,?) ON CONFLICT(node_id,user_scope,provider,native_thread_id) DO UPDATE SET fence=excluded.fence,owner_execution=excluded.owner_execution", (identity.node_id, identity.user_scope, identity.provider, identity.native_thread_id, fence, execution_ref))
        return fence

    def release_fence(self, identity: SessionIdentity, execution_ref: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE route_fences SET owner_execution=NULL WHERE node_id=? AND user_scope=? AND provider=? AND native_thread_id=? AND owner_execution=?", (identity.node_id, identity.user_scope, identity.provider, identity.native_thread_id, execution_ref))


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

    def _authorize(self, operation: str, request: Mapping[str, Any]) -> None:
        record, scope = self._snapshot()
        if request.get("node_id") not in (None, record.node_id):
            raise NodeProtocolError("NODE_TARGET_MISMATCH", "Request targets another node")
        if request.get("user_scope") not in (None, scope.user_scope):
            raise NodeProtocolError("USER_SCOPE_DENIED", "Request user scope is not shared with this node")
        provider = request.get("provider", SUPPORTED_PROVIDER)
        if not record.trusted or not record.authorized or not scope.allows(operation, provider):
            raise NodeProtocolError("SHARING_SCOPE_DENIED", "The approved sharing scope does not allow this operation")
        if record.protocol_version != NODE_PROTOCOL_VERSION or not record.version_compatible:
            raise NodeProtocolError("NODE_VERSION_INCOMPATIBLE", "Node protocol version is incompatible")

    def _identity(self, request: Mapping[str, Any]) -> SessionIdentity:
        record, scope = self._snapshot()
        return SessionIdentity(record.node_id, scope.user_scope, request.get("provider", SUPPORTED_PROVIDER), request["thread_id"], request.get("source_version", "1"))

    def read(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self._authorize("session.read", request)
        thread_id = _text("thread_id", request.get("thread_id"), max_len=256)
        identity = self._identity(request)
        source = request.get("source", "native")
        provider_cursor = None
        if request.get("cursor"):
            provider_cursor = _decode_node_cursor(
                request["cursor"], node_id=identity.node_id, user_scope=identity.user_scope,
                provider=identity.provider, native_thread_id=thread_id,
                source=source, source_version=identity.source_version,
                secret=self.registry.cursor_secret,
            )
        kwargs = {key: request[key] for key in ("recent_turns", "max_bytes") if key in request}
        if provider_cursor is not None:
            kwargs["cursor"] = provider_cursor
        result = dict(self.read_thread(thread_id, **kwargs))
        result.setdefault("queried_thread_id", thread_id)
        record, scope = self._snapshot()
        result.update(node_id=record.node_id, user_scope=scope.user_scope, provider=identity.provider, source_node_id=record.node_id, source_observed_at=_now(), read_only=True)
        if result.get("next_cursor"):
            result["next_cursor"] = _encode_provider_cursor(
                node_id=identity.node_id, user_scope=identity.user_scope,
                provider=identity.provider, native_thread_id=thread_id,
                source=source, source_version=identity.source_version,
                provider_cursor=result["next_cursor"], secret=self.registry.cursor_secret,
            )
        self.registry.update_thread(identity, source=source, status={"lookup_status": result.get("lookup_status"), "context_status": result.get("context_status")})
        return result

    def status(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self._authorize("session.status", request)
        result = dict(self.status_thread(request["thread_id"]))
        record, scope = self._snapshot()
        result.update(node_id=record.node_id, user_scope=scope.user_scope, provider=request.get("provider", SUPPORTED_PROVIDER), source_node_id=record.node_id, source_observed_at=_now(), read_only=True)
        return result

    def _idempotent(self, operation: str, request: Mapping[str, Any], callback: Callable[..., Mapping[str, Any]] | None, *, mutating: bool = True, claimed: bool = False) -> dict[str, Any]:
        if callback is None:
            raise NodeProtocolError("OPERATION_NOT_IMPLEMENTED", f"{operation} is not available on this node")
        request_id = _text("request_id", request.get("request_id"), max_len=256)
        if claimed:
            state, prior = "NEW", None
        else:
            state, prior = self.registry.claim_request(node_id=self.record.node_id, request_id=request_id, operation=operation, payload=request)
            if state != "NEW":
                if prior is not None:
                    return dict(prior, idempotent=True)
                return {"request_id": request_id, "operation_state": "UNKNOWN", "side_effect": "UNKNOWN", "retry": "RECONCILIATION_REQUIRED", "idempotent": True}
        try:
            result = dict(callback(**dict(request)))
        except Exception as exc:
            side_effect = getattr(exc, 'side_effect', "UNKNOWN" if mutating else "NONE")
            result = {"request_id": request_id, "operation_state": "BLOCKED" if mutating and side_effect == "NONE" else "FAILED", "error_code": getattr(exc, "code", type(exc).__name__), "unavailable_reason": str(exc)[:500], "side_effect": side_effect}
            if operation == 'execution.start':
                result.update(execution_ref=request.get('execution_ref'),
                              prepared_execution_ref=request.get('prepared_execution_ref'),
                              execution_started=False)
            self.registry.finish_request(node_id=self.record.node_id, request_id=request_id, state="FAILED", response=result)
            return result
        self.registry.finish_request(node_id=self.record.node_id, request_id=request_id, state="COMPLETED", response=result)
        return dict(result, idempotent=False)

    def prepare(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self._authorize("execution.prepare", request)
        return self._idempotent("execution.prepare", request, self.prepare_execution, mutating=False)

    def start(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self._authorize("execution.start", request)
        # Canonical adapters own the single-writer lease. Legacy callbacks
        # carrying a raw thread still use their existing transport fence.
        identity = self._identity(request) if request.get("thread_id") else None
        execution_ref = _text("execution_ref", request.get("execution_ref"), max_len=256)
        request_id = _text("request_id", request.get("request_id"), max_len=256)
        state, prior = self.registry.claim_request(node_id=self.record.node_id, request_id=request_id, operation="execution.start", payload=request)
        if state != "NEW":
            if prior is not None and prior.get('side_effect') != 'UNKNOWN':
                return dict(prior, idempotent=True)
            # Reconcile the deterministic canonical execution before returning
            # an unknown outcome. Never call start again for this request.
            if self.status_execution is not None:
                try:
                    reconciled = dict(self.status_execution(**dict(request)))
                    if reconciled.get('execution_ref') == execution_ref:
                        result = dict(reconciled, execution_started=False, idempotent=True,
                                      operation_state='RECONCILED', request_id=request_id)
                        self.registry.finish_request(node_id=self.record.node_id, request_id=request_id, state='COMPLETED', response=result)
                        return result
                except Exception:
                    pass
            return {"request_id": request_id, "operation_state": "UNKNOWN", "side_effect": "UNKNOWN", "retry": "RECONCILIATION_REQUIRED", "idempotent": True}
        try:
            fence = self.registry.acquire_fence(identity, execution_ref) if identity else None
        except NodeProtocolError as exc:
            result = exc.as_dict() | {"request_id": request_id, "operation_state": "BLOCKED", "side_effect": "NONE"}
            self.registry.finish_request(node_id=self.record.node_id, request_id=request_id, state="FAILED", response=result)
            return result
        request = dict(request, fence=fence, target_node_id=self.record.node_id)
        return self._idempotent("execution.start", request, self.start_execution, claimed=True)

    def execution_status(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self._authorize("session.status", request)
        if self.status_execution is None:
            raise NodeProtocolError("OPERATION_NOT_IMPLEMENTED", "execution.status is not available on this node")
        return dict(self.status_execution(**dict(request)), node_id=self.record.node_id, read_only=True)

    def cancel(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self._authorize("execution.cancel", request)
        result = self._idempotent("execution.cancel", request, self.cancel_execution)
        if result.get("operation_state") in {"CANCELLED", "COMPLETED", "CONFIRMED"} and request.get("thread_id"):
            self.registry.release_fence(self._identity(request), request.get("execution_ref", ""))
        return result

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        operation = request.get("operation")
        try:
            if request.get("protocol_version", NODE_PROTOCOL_VERSION) != NODE_PROTOCOL_VERSION:
                raise NodeProtocolError("NODE_VERSION_INCOMPATIBLE", "Unsupported node protocol version")
            if operation == "node.status":
                self._authorize(operation, request)
                return {"node": self.record.as_dict(), "scope": self.scope.as_dict(), "node_reachable": True, "provider_reachable": True, "history_readable": self.scope.read_sessions, "execution_active": False, "observed_at": _now(), "read_only": True}
            if operation == "session.read": return self.read(request)
            if operation == "session.status": return self.status(request)
            if operation == "execution.adopt":
                self._authorize(operation, request)
                if self.adopt_conversation is None:
                    raise NodeProtocolError("OPERATION_NOT_IMPLEMENTED", "Native adoption is unavailable")
                try:
                    return dict(self.adopt_conversation(**dict(request)), node_id=self.record.node_id)
                except Exception as exc:
                    return {'adoption_status': 'BLOCKED', 'error_code': getattr(exc, 'code', type(exc).__name__),
                            'unavailable_reason': str(exc)[:500], 'execution_started': False,
                            'control_transferred': False, 'node_id': self.record.node_id}
            if operation == "execution.context":
                self._authorize("session.read", request)
                if self.context_execution is None:
                    raise NodeProtocolError("OPERATION_NOT_IMPLEMENTED", "Canonical context is unavailable")
                return dict(self.context_execution(**dict(request)), node_id=self.record.node_id)
            if operation == "execution.prepare": return self.prepare(request)
            if operation == "execution.start": return self.start(request)
            if operation == "execution.status": return self.execution_status(request)
            if operation == "execution.cancel": return self.cancel(request)
            raise NodeProtocolError("UNKNOWN_NODE_OPERATION", "Operation is not part of the node protocol")
        except NodeProtocolError as exc:
            return exc.as_dict() | {"node_id": self.record.node_id, "read_only": operation.startswith("session.") if isinstance(operation, str) else True}


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
        self.attach_remote(record, client)
        return self._readers.get(record.node_id)

    def nodes(self) -> list[dict[str, Any]]:
        self.registry.refresh_states()
        return [record.as_dict() | {"scope": (self.registry.scope(record.node_id, self.user_scope).as_dict() if self.registry.scope(record.node_id, self.user_scope) else None)} for record in self.registry.list_nodes(user_scope=self.user_scope)]

    def _node_for(self, *, node_id: str | None = None, host: str | None = None) -> NodeRecord:
        if node_id:
            record = self.registry.get(node_id)
            if record is None:
                raise NodeProtocolError("UNKNOWN_NODE", "node_id is not a trusted registered node")
            return record
        if host:
            matches = [record for record in self.registry.list_nodes(user_scope=self.user_scope) if host.casefold() in {record.node_id.casefold(), record.display_name.casefold(), *(route.casefold() for route in record.route_ids)}]
            if not matches:
                raise NodeProtocolError("UNKNOWN_THREAD_HOST", "Host is not an authorized node route")
            if len(matches) > 1:
                raise NodeProtocolError("THREAD_HOST_CONFLICT", "Host alias maps to multiple nodes")
            return matches[0]
        raise NodeProtocolError("NODE_TARGET_REQUIRED", "An explicit node is required for this operation")

    @staticmethod
    def _found(result: Mapping[str, Any], thread_id: str) -> bool:
        return (
            result.get("queried_thread_id") == thread_id
            and result.get("lookup_status") in {"RESOLVED", "THREAD_UNBOUND", "AVAILABLE", "FOUND"}
            and result.get("provider_existence") != "NOT_FOUND"
            and not result.get("error_code")
        )

    def read(self, *, thread_id: str, node_id: str | None = None, host: str | None = None, provider: str = SUPPORTED_PROVIDER, cursor: str | None = None, recent_turns: int = 8, max_bytes: int = 32000) -> dict[str, Any]:
        self.registry.refresh_states()
        request = {"operation": "session.read", "protocol_version": NODE_PROTOCOL_VERSION, "thread_id": thread_id, "user_scope": self.user_scope, "provider": provider, "recent_turns": recent_turns, "max_bytes": max_bytes}
        if cursor is not None: request["cursor"] = cursor
        if node_id or host:
            record = self._node_for(node_id=node_id, host=host)
            if record.user_scope != self.user_scope or not record.authorized or not record.trusted:
                return {"error_code": "SHARING_SCOPE_DENIED", "lookup_status": "SHARING_SCOPE_DENIED", "coverage": {"requested_node_id": record.node_id}}
            reader = self._reader_for(record)
            if reader is None or record.stale or record.state == "OFFLINE":
                return {"error_code": "NODE_OFFLINE", "lookup_status": "NODE_OFFLINE", "coverage": {"requested_node_id": record.node_id, "stale": record.stale}}
            try:
                result = dict(reader(request))
            except (OSError, TimeoutError, socket.timeout):
                return {"error_code": "NODE_UNAVAILABLE", "lookup_status": "NODE_UNAVAILABLE", "coverage": {"requested_node_id": record.node_id}}
            self.registry.touch(record.node_id)
            return result | {"source_node_id": record.node_id, "coverage": {"requested_node_id": record.node_id, "complete": True, "nodes_queried": [record.node_id]}}

        records = [record for record in self.registry.list_nodes(user_scope=self.user_scope, authorized_only=True) if self._reader_for(record) is not None]
        if not records:
            return {"error_code": "THREAD_LOOKUP_UNAVAILABLE", "lookup_status": "THREAD_LOOKUP_UNAVAILABLE", "coverage": {"complete": False, "nodes_queried": [], "reason": "NO_AUTHORIZED_NODES"}}
        results: list[tuple[NodeRecord, Mapping[str, Any] | None, str]] = []
        def ask(record: NodeRecord) -> tuple[NodeRecord, Mapping[str, Any] | None, str]:
            if record.stale or record.state == "OFFLINE":
                return record, None, "OFFLINE"
            try:
                reader = self._reader_for(record)
                if reader is None:
                    return record, None, "UNAVAILABLE"
                result = dict(reader(request))
                self.registry.touch(record.node_id)
                return record, result, "OK"
            except (OSError, TimeoutError, socket.timeout, concurrent.futures.TimeoutError):
                return record, None, "TIMEOUT"
            except Exception:
                return record, None, "UNAVAILABLE"
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(self.max_workers, len(records))) as pool:
            futures = [pool.submit(ask, record) for record in records]
            try:
                completed = concurrent.futures.as_completed(futures, timeout=self.timeout_seconds)
                for future in completed:
                    try: results.append(future.result())
                    except Exception: pass
            except concurrent.futures.TimeoutError:
                for future in futures:
                    future.cancel()
        found = [(record, result) for record, result, state in results if result is not None and self._found(result, thread_id)]
        queried_ids = {record.node_id for record, _, _ in results}
        unavailable = [record.node_id for record, result, state in results if result is None or state != "OK"]
        unavailable.extend(record.node_id for record in records if record.node_id not in queried_ids)
        unavailable = sorted(set(unavailable))
        coverage = {"complete": not unavailable and len(results) == len(records), "nodes_queried": sorted(queried_ids), "nodes_unavailable": unavailable, "nodes_authorized": sorted(record.node_id for record in records)}
        if len(found) > 1:
            return {"error_code": "THREAD_IDENTITY_CONFLICT", "lookup_status": "THREAD_IDENTITY_CONFLICT", "unavailable_reason": "Exact thread matched multiple authorized nodes", "coverage": coverage, "matching_nodes": sorted(record.node_id for record, _ in found), "read_only": True}
        if len(found) == 1:
            record, result = found[0]
            return dict(result, source_node_id=record.node_id, coverage=coverage, read_only=True)
        if unavailable:
            return {"error_code": "THREAD_SEARCH_INCOMPLETE", "lookup_status": "THREAD_SEARCH_INCOMPLETE", "unavailable_reason": "Some authorized nodes were offline, timed out or unavailable", "coverage": coverage, "read_only": True}
        return {"error_code": "THREAD_NOT_FOUND", "lookup_status": "THREAD_NOT_FOUND", "absence_scope": "AUTHORIZED_NODE_SET", "coverage": coverage, "read_only": True}

    def execute(self, *, node_id: str, operation: str, request: Mapping[str, Any]) -> dict[str, Any]:
        self.registry.refresh_states()
        record = self._node_for(node_id=node_id)
        if record.user_scope != self.user_scope or not record.authorized or not record.trusted:
            return {"error_code": "SHARING_SCOPE_DENIED", "execution_started": False, "node_id": node_id}
        scope = self.registry.scope(node_id, self.user_scope)
        permission = "session.read" if operation in {"execution.context", "execution.status"} else operation
        if scope is None or not scope.allows(permission):
            return {"error_code": "SHARING_SCOPE_DENIED", "execution_started": False, "node_id": node_id}
        if record.stale or record.state != "ONLINE":
            return {"error_code": "NODE_OFFLINE", "execution_started": False, "node_id": node_id}
        if record.node_id not in self._executors:
            self._reader_for(record)
        executor = self._executors.get(record.node_id)
        if executor is None:
            return {"error_code": "NODE_EXECUTION_UNAVAILABLE", "execution_started": False, "node_id": node_id}
        # There is deliberately no failover path.  An acknowledged request is
        # owned by this node; an unacknowledged request stays UNKNOWN.
        try:
            result = dict(executor(operation, dict(request, node_id=node_id, user_scope=self.user_scope)), node_id=record.node_id)
            if not result.get("error_code"):
                for key in ("task_ref", "prepared_execution_ref", "execution_ref"):
                    if result.get(key):
                        self.registry.bind_reference(result[key], node_id, self.user_scope)
            return result
        except (OSError, TimeoutError, socket.timeout) as exc:
            if operation in READ_OPERATIONS:
                return {"error_code": "NODE_READ_UNAVAILABLE", "read_only": True,
                        "operation_state": "READ_FAILED", "side_effect": "NONE",
                        "retry": "READ_ONLY_RETRY", "failure_stage": "rpc_transport",
                        "error_type": type(exc).__name__,
                        "node_id": record.node_id}
            return {"error_code": "EXECUTION_OUTCOME_UNKNOWN", "operation_state": "UNKNOWN", "side_effect": "UNKNOWN", "retry": "RECONCILIATION_REQUIRED", "node_id": record.node_id}


class _RPCHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: "NodeRPCServer" = self.server.owner  # type: ignore[attr-defined]
        line = self.rfile.readline(MAX_FRAME_BYTES + 1)
        if len(line) > MAX_FRAME_BYTES:
            return
        payload = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or not isinstance(request.get("operation"), str):
                raise ValueError()
        except (ValueError, UnicodeError):
            result = {"error_code": "INVALID_NODE_REQUEST", "read_only": True}
        else:
            stage = "rpc_dispatch"
            try:
                result = server.handle_request(request, self.request, self.client_address)
                stage = "rpc_serialize"
                # Serialization belongs to the RPC boundary too. Otherwise a
                # valid read can close the connection without any diagnostic.
                payload = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
            except Exception as exc:
                read_only = request.get("operation") in READ_OPERATIONS
                result = {
                    "error_code": "NODE_READ_FAILED" if read_only else "NODE_REQUEST_FAILED",
                    "read_only": read_only,
                    "operation_state": "READ_FAILED" if read_only else "UNKNOWN",
                    "side_effect": "NONE" if read_only else "UNKNOWN",
                    "retry": "READ_ONLY_RETRY" if read_only else "RECONCILIATION_REQUIRED",
                    "failure_stage": stage, "error_type": type(exc).__name__,
                }
        if payload is None:
            payload = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        if len(payload) <= MAX_FRAME_BYTES:
            self.wfile.write(payload)
            self.wfile.flush()


class NodeRPCServer:
    """Small framed transport for a node service.

    Production callers pass the existing mTLS ``SSLContext``.  Plain sockets
    are accepted only for an explicitly enabled loopback test server.
    """

    def __init__(self, service: Any, *, address: str = "127.0.0.1", port: int = 0, ssl_context: ssl.SSLContext | None = None, ssl_context_factory: Callable[[], ssl.SSLContext] | None = None, request_handler: Callable[..., Mapping[str, Any]] | None = None, allow_insecure_loopback: bool = False):
        if ssl_context is None and ssl_context_factory is None and not (allow_insecure_loopback and address in {"127.0.0.1", "::1"}):
            raise ValueError("NodeRPCServer requires mTLS or explicit loopback test mode")
        self.service = service
        self.request_handler = request_handler
        owner = self
        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True
            def get_request(self):
                sock, addr = super().get_request()
                sock.settimeout(5)
                context = owner.ssl_context_factory() if owner.ssl_context_factory else owner.ssl_context
                if context is not None:
                    try:
                        sock = context.wrap_socket(sock, server_side=True)
                    except Exception:
                        sock.close()
                        raise
                return sock, addr
        self.ssl_context = ssl_context
        self.ssl_context_factory = ssl_context_factory
        self.server = Server((address, port), _RPCHandler)
        self.server.owner = self  # type: ignore[attr-defined]
        self.address = self.server.server_address
        self._thread = threading.Thread(target=self.server.serve_forever, name="clinx-node-rpc", daemon=True)
        self._thread.start()

    def handle_request(self, request: Mapping[str, Any], sock: Any, address: Any) -> dict[str, Any]:
        if self.request_handler is not None:
            return dict(self.request_handler(request, sock, address))
        return dict(self.service.handle(request))

    def close(self) -> None:
        self.server.shutdown(); self.server.server_close(); self._thread.join(timeout=2)


class NodeRPCClient:
    def __init__(self, address: tuple[str, int], *, ssl_context: ssl.SSLContext | None = None, timeout_seconds: float = 2.0, allow_insecure_loopback: bool = False):
        if ssl_context is None and not (allow_insecure_loopback and address[0] in {"127.0.0.1", "::1"}):
            raise ValueError("NodeRPCClient requires mTLS or explicit loopback test mode")
        self.address, self.ssl_context, self.timeout_seconds = address, ssl_context, timeout_seconds

    def call(self, request: Mapping[str, Any]) -> dict[str, Any]:
        with socket.create_connection(self.address, timeout=self.timeout_seconds) as raw:
            sock = self.ssl_context.wrap_socket(raw, server_hostname=self.address[0]) if self.ssl_context is not None else raw
            with sock:
                payload = json.dumps(dict(request), separators=(",", ":")).encode() + b"\n"
                sock.sendall(payload)
                data = b""
                while not data.endswith(b"\n") and len(data) <= MAX_FRAME_BYTES:
                    chunk = sock.recv(4096)
                    if not chunk: break
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
