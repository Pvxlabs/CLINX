"""Compatibility event/outbox storage for Desktop metadata delivery only.

Keep the existing schema and semantic fingerprints so pending publications
continue across upgrades. Task/execution shadow state and replay are retired.
"""

from __future__ import annotations

import contextlib
import datetime as _datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterator
import uuid

from clinx.compat.domain import Event, EventActor, JsonDocument

from .errors import (
    AggregateVersionConflict,
    IdempotencyConflict,
    LedgerBusy,
    LedgerSchemaError,
    LedgerValidationError,
    PayloadSizeExceeded,
    TransactionOwnershipError,
)
from .models import (
    AppendRequest,
    AppendResult,
    OutboxRecord,
    StoredEvent,
)
from .schema import LATEST_SCHEMA_VERSION, MIGRATIONS


FaultInjector = Callable[[str], None]


def _now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise LedgerValidationError("value must contain finite JSON data") from exc


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).casefold()
    return "locked" in message or "busy" in message


class UnitOfWork:
    """A transaction participant; transaction completion belongs to its caller."""

    def __init__(self, store: EventStore, connection: sqlite3.Connection):
        self.store = store
        self.connection = connection

    def append(self, request: AppendRequest) -> AppendResult:
        return self.store._append(self.connection, request)


class EventStore:
    """Append metadata intents with the existing durable outbox identity."""

    def __init__(
        self,
        path: Path | str,
        *,
        busy_timeout_ms: int = 30_000,
        fault_injector: FaultInjector | None = None,
        max_append_payload_bytes: int = 1_048_576,
    ):
        self.path = Path(path).expanduser()
        self.busy_timeout_ms = max(0, int(busy_timeout_ms))
        self.fault_injector = fault_injector
        self.max_append_payload_bytes = max(1, int(max_append_payload_bytes))

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        target: str | Path = self.path
        kwargs: dict[str, Any] = {}
        if read_only:
            target = self.path.resolve().as_uri() + "?mode=ro"
            kwargs["uri"] = True
        conn = sqlite3.connect(
            target,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
            **kwargs,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS v2_schema_migrations (
                       version INTEGER PRIMARY KEY,
                       name TEXT NOT NULL UNIQUE,
                       applied_at TEXT NOT NULL
                   )"""
            )
            applied = {
                int(row["version"])
                for row in conn.execute("SELECT version FROM v2_schema_migrations")
            }
            unknown = sorted(version for version in applied if version > LATEST_SCHEMA_VERSION)
            if unknown:
                raise LedgerSchemaError(
                    f"database has unsupported shadow schema versions: {unknown}"
                )
            for migration in MIGRATIONS:
                if migration.version in applied:
                    continue
                for statement in migration.statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO v2_schema_migrations(version,name,applied_at) VALUES (?,?,?)",
                    (migration.version, migration.name, _now()),
                )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


    def _require_schema(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='v2_schema_migrations'"
        ).fetchone()
        if row is None:
            raise LedgerSchemaError("shadow ledger schema is not initialized")
        version = conn.execute(
            "SELECT MAX(version) AS version FROM v2_schema_migrations"
        ).fetchone()["version"]
        if version != LATEST_SCHEMA_VERSION:
            raise LedgerSchemaError(
                f"shadow ledger schema version {version!r} is not supported"
            )

    @contextlib.contextmanager
    def unit_of_work(
        self, connection: sqlite3.Connection | None = None
    ) -> Iterator[UnitOfWork]:
        if connection is not None:
            if not connection.in_transaction:
                raise TransactionOwnershipError(
                    "caller-owned connection must already have an active transaction"
                )
            self._require_schema(connection)
            yield UnitOfWork(self, connection)
            return

        conn = self._connect()
        try:
            self._require_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            yield UnitOfWork(self, conn)
            conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if _is_busy(exc):
                raise LedgerBusy(str(exc)) from exc
            raise
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def append(self, request: AppendRequest) -> AppendResult:
        with self.unit_of_work() as unit:
            return unit.append(request)

    def _fault(self, stage: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(stage)

    @staticmethod
    def _validate_relationships(request: AppendRequest) -> None:
        payload = request.payload.to_dict()
        fields = {"host", "thread_id", "cwd", "title", "originator", "endpoint"}
        if (request.aggregate_type != "desktop_publication"
                or request.event_type != "V1DesktopPublicationRequestedObserved"
                or set(payload) != fields | {"task_id"}
                or any(not isinstance(payload[k], str) or not payload[k] or len(payload[k]) > 4096 for k in fields)
                or (payload["task_id"] is not None and not isinstance(payload["task_id"], str))
                or len(request.outbox) != 1
                or request.outbox[0].destination != "codex_desktop_metadata_v1"
                or request.outbox[0].payload.to_dict() != payload):
            raise LedgerValidationError("invalid metadata-only publication intent")

    @staticmethod
    def _fingerprint(request: AppendRequest) -> str:
        outbox = sorted(
            (
                {
                    "destination": item.destination,
                    "idempotency_key": item.idempotency_key,
                    "payload": item.payload.to_dict(),
                }
                for item in request.outbox
            ),
            key=lambda item: (item["destination"], item["idempotency_key"]),
        )
        semantic = {
            "aggregate_type": request.aggregate_type,
            "aggregate_id": request.aggregate_id,
            "event_type": request.event_type,
            "schema_version": request.schema_version,
            "actor": request.actor.to_dict(),
            "causation_id": request.causation_id,
            "correlation_id": request.correlation_id,
            "payload": request.payload.to_dict(),
            "outbox": outbox,
        }
        return _sha256(_canonical_json(semantic))

    def _append(
        self, conn: sqlite3.Connection, request: AppendRequest
    ) -> AppendResult:
        if not conn.in_transaction:
            raise TransactionOwnershipError("append requires an active transaction")
        self._validate_relationships(request)
        fingerprint = self._fingerprint(request)
        existing = conn.execute(
            """SELECT * FROM v2_events
               WHERE idempotency_scope=? AND idempotency_key=?""",
            (request.idempotency_scope, request.idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing["semantic_fingerprint"] != fingerprint:
                raise IdempotencyConflict(
                    "idempotency scope/key already belongs to different semantics"
                )
            stored = self._stored_event(existing)
            return AppendResult(
                stored_event=stored,
                outbox=self._outbox_for_event(conn, stored.event.event_id),
                duplicate=True,
            )

        # Preserve exact-retry semantics for events accepted under an older
        # write budget. New events are bounded before their payload is stored.
        payload_bytes = len(request.payload.to_json().encode("utf-8"))
        if payload_bytes > self.max_append_payload_bytes:
            raise PayloadSizeExceeded(
                f"event payload {payload_bytes} exceeds write limit "
                f"{self.max_append_payload_bytes} bytes"
            )

        current = self._current_version(
            conn, request.aggregate_type, request.aggregate_id
        )
        if current != request.expected_version:
            raise AggregateVersionConflict(
                f"expected stream version {request.expected_version}, actual {current}"
            )

        recorded_at = _now()
        occurred_at = request.occurred_at or recorded_at
        next_version = current + 1
        conn.execute(
            """INSERT OR IGNORE INTO v2_aggregate_versions
               (aggregate_type,aggregate_id,current_version,updated_at)
               VALUES (?,?,0,?)""",
            (request.aggregate_type, request.aggregate_id, recorded_at),
        )
        advanced = conn.execute(
            """UPDATE v2_aggregate_versions
               SET current_version=?,updated_at=?
               WHERE aggregate_type=? AND aggregate_id=? AND current_version=?""",
            (
                next_version,
                recorded_at,
                request.aggregate_type,
                request.aggregate_id,
                current,
            ),
        ).rowcount
        if advanced != 1:
            raise AggregateVersionConflict(
                f"stream version changed while appending from {current}"
            )

        event = Event(
            event_id="evt_" + uuid.uuid4().hex,
            aggregate_type=request.aggregate_type,
            aggregate_id=request.aggregate_id,
            aggregate_version=next_version,
            event_type=request.event_type,
            schema_version=request.schema_version,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            actor=request.actor,
            idempotency_scope=request.idempotency_scope,
            idempotency_key=request.idempotency_key,
            payload=request.payload,
            causation_id=request.causation_id,
            correlation_id=request.correlation_id,
        )
        self._fault("before_event_append")
        try:
            cursor = conn.execute(
                """INSERT INTO v2_events
                   (event_id,aggregate_type,aggregate_id,aggregate_version,event_type,
                    schema_version,occurred_at,recorded_at,actor_json,causation_id,
                    correlation_id,idempotency_scope,idempotency_key,
                    semantic_fingerprint,payload_json,payload_hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event.event_id,
                    event.aggregate_type,
                    event.aggregate_id,
                    event.aggregate_version,
                    event.event_type,
                    event.schema_version,
                    event.occurred_at,
                    event.recorded_at,
                    event.actor.to_json(),
                    event.causation_id,
                    event.correlation_id,
                    event.idempotency_scope,
                    event.idempotency_key,
                    fingerprint,
                    event.payload.to_json(),
                    event.payload_hash,
                ),
            ).lastrowid
        except sqlite3.IntegrityError as exc:
            raise AggregateVersionConflict("event uniqueness constraint failed") from exc
        self._fault("after_event_append")

        for item in request.outbox:
            stamp = _now()
            try:
                conn.execute(
                    """INSERT INTO v2_event_outbox
                       (outbox_id,event_id,destination,idempotency_key,payload_json,
                        state,attempts,last_error,created_at,updated_at)
                       VALUES (?,?,?,?,?,'PENDING',0,NULL,?,?)""",
                    (
                        "outbox_" + uuid.uuid4().hex,
                        event.event_id,
                        item.destination,
                        item.idempotency_key,
                        item.payload.to_json(),
                        stamp,
                        stamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise IdempotencyConflict(
                    "outbox destination/idempotency key already exists"
                ) from exc
        self._fault("after_outbox_insert")
        row = conn.execute(
            "SELECT * FROM v2_events WHERE cursor=?", (cursor,)
        ).fetchone()
        return AppendResult(
            stored_event=self._stored_event(row),
            outbox=self._outbox_for_event(conn, event.event_id),
        )

    @staticmethod
    def _current_version(
        conn: sqlite3.Connection, aggregate_type: str, aggregate_id: str
    ) -> int:
        row = conn.execute(
            """SELECT current_version FROM v2_aggregate_versions
               WHERE aggregate_type=? AND aggregate_id=?""",
            (aggregate_type, aggregate_id),
        ).fetchone()
        return int(row["current_version"]) if row is not None else 0


    @staticmethod
    def _stored_event(row: sqlite3.Row) -> StoredEvent:
        actor = json.loads(row["actor_json"])
        event = Event(
            event_id=row["event_id"],
            aggregate_type=row["aggregate_type"],
            aggregate_id=row["aggregate_id"],
            aggregate_version=int(row["aggregate_version"]),
            event_type=row["event_type"],
            schema_version=int(row["schema_version"]),
            occurred_at=row["occurred_at"],
            recorded_at=row["recorded_at"],
            actor=EventActor(**actor),
            idempotency_scope=row["idempotency_scope"],
            idempotency_key=row["idempotency_key"],
            payload=JsonDocument(row["payload_json"]),
            payload_hash=row["payload_hash"],
            causation_id=row["causation_id"],
            correlation_id=row["correlation_id"],
        )
        return StoredEvent(
            cursor=int(row["cursor"]),
            event=event,
            semantic_fingerprint=row["semantic_fingerprint"],
        )


    @staticmethod
    def _outbox_record(row: sqlite3.Row) -> OutboxRecord:
        return OutboxRecord(
            cursor=int(row["cursor"]),
            outbox_id=row["outbox_id"],
            event_id=row["event_id"],
            destination=row["destination"],
            idempotency_key=row["idempotency_key"],
            payload=JsonDocument(row["payload_json"]),
            state=row["state"],
            attempts=int(row["attempts"]),
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _outbox_for_event(
        self, conn: sqlite3.Connection, event_id: str
    ) -> tuple[OutboxRecord, ...]:
        rows = conn.execute(
            "SELECT * FROM v2_event_outbox WHERE event_id=? ORDER BY cursor",
            (event_id,),
        ).fetchall()
        return tuple(self._outbox_record(row) for row in rows)
