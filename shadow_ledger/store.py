"""Transactional SQLite EventStore and UnitOfWork for V1 shadow facts."""

from __future__ import annotations

import contextlib
import datetime as _datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterator
import uuid

from domain import Event, EventActor, JsonDocument

from .errors import (
    AggregateVersionConflict,
    IdempotencyConflict,
    IdentityMappingConflict,
    LedgerBusy,
    LedgerSchemaError,
    LedgerValidationError,
    PayloadSizeExceeded,
    ReplayBoundaryExceeded,
    TransactionOwnershipError,
)
from .models import (
    AppendRequest,
    AppendResult,
    IdentityMapping,
    InboxReceipt,
    OutboxRecord,
    ProjectionCheckpoint,
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

    def current_version(self, aggregate_type: str, aggregate_id: str) -> int:
        return self.store._current_version(
            self.connection, aggregate_type, aggregate_id
        )

    def map_legacy_identity(
        self,
        *,
        source_system: str,
        source_type: str,
        source_identity: str,
        target_type: str,
        attribution_state: str = "EXACT",
    ) -> IdentityMapping:
        return self.store._map_legacy_identity(
            self.connection,
            source_system=source_system,
            source_type=source_type,
            source_identity=source_identity,
            target_type=target_type,
            attribution_state=attribution_state,
        )


class EventStore:
    """Narrow shadow ledger; initialization and authority are always explicit."""

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

    def schema_version(self) -> int | None:
        if not self.path.exists():
            return None
        with self._connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='v2_schema_migrations'"
            ).fetchone()
            if row is None:
                return None
            result = conn.execute(
                "SELECT MAX(version) AS version FROM v2_schema_migrations"
            ).fetchone()
        return int(result["version"]) if result["version"] is not None else 0

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
        execution_id = payload.get("execution_id")
        if request.aggregate_type == "execution":
            if execution_id != request.aggregate_id:
                raise LedgerValidationError(
                    "execution event payload must contain the aggregate execution_id"
                )
            source_ref = payload.get("source_execution_ref")
            if not isinstance(source_ref, str) or not source_ref.strip():
                raise LedgerValidationError(
                    "execution event requires an exact source_execution_ref"
                )
            if payload.get("attribution") != "EXACT":
                raise LedgerValidationError("execution event attribution must be EXACT")
        elif request.aggregate_type == "task":
            if payload.get("task_id") != request.aggregate_id:
                raise LedgerValidationError(
                    "task event payload must contain the aggregate task_id"
                )
            if payload.get("attribution") != "UNATTRIBUTED":
                raise LedgerValidationError("task observation must be UNATTRIBUTED")
            forbidden = {"execution_id", "attempt_id", "provider_session_id"}
            if forbidden.intersection(payload):
                raise LedgerValidationError(
                    "unattributed task event must not invent execution identity"
                )
        else:
            raise LedgerValidationError(
                f"unsupported shadow aggregate_type: {request.aggregate_type}"
            )
        evidence_ref = payload.get("evidence_ref")
        if evidence_ref is not None and (
            not isinstance(evidence_ref, str) or not evidence_ref.strip()
        ):
            raise LedgerValidationError("evidence_ref must be a non-empty string")
        attempt_id = payload.get("attempt_id")
        if attempt_id is not None and (
            not isinstance(attempt_id, str) or not attempt_id.strip()
        ):
            raise LedgerValidationError("attempt_id must be a non-empty string")
        if request.event_type in {
            "V1HostExecutionStartedObserved",
            "V1HostEvidenceObserved",
        } and evidence_ref is None:
            raise LedgerValidationError("host observation requires evidence_ref")
        if request.event_type == "V1ExecutionResultPersistedObserved":
            if request.aggregate_type != "execution":
                raise LedgerValidationError("result observation requires execution stream")
            if payload.get("exact_result_ref") != payload.get("source_execution_ref"):
                raise LedgerValidationError(
                    "result reference must match exact source execution"
                )
        if request.event_type in {
            "V1ExecutionClaimObserved",
            "V1UnattributedExecutionClaimObserved",
            "V1SnapshotBaselineImported",
        }:
            required = {"execution_state", "current_stage", "lease_state"}
            missing = sorted(required.difference(payload))
            if missing:
                raise LedgerValidationError(
                    "baseline observation is missing: " + ", ".join(missing)
                )

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

    def current_version(self, aggregate_type: str, aggregate_id: str) -> int:
        with self._connect(read_only=True) as conn:
            self._require_schema(conn)
            return self._current_version(conn, aggregate_type, aggregate_id)

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

    def get_event(self, event_id: str) -> StoredEvent | None:
        with self._connect(read_only=True) as conn:
            self._require_schema(conn)
            row = conn.execute(
                "SELECT * FROM v2_events WHERE event_id=?", (event_id,)
            ).fetchone()
        return self._stored_event(row) if row is not None else None

    def read_events(
        self,
        *,
        after_cursor: int = 0,
        limit: int = 100,
        max_payload_bytes: int = 1_048_576,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
    ) -> tuple[StoredEvent, ...]:
        limit = max(1, min(int(limit), 1000))
        max_payload_bytes = max(1, int(max_payload_bytes))
        clauses = ["cursor > ?"]
        args: list[Any] = [max(0, int(after_cursor))]
        if aggregate_type is not None:
            clauses.append("aggregate_type = ?")
            args.append(aggregate_type)
        if aggregate_id is not None:
            clauses.append("aggregate_id = ?")
            args.append(aggregate_id)
        args.append(limit)
        conn = self._connect(read_only=True)
        try:
            self._require_schema(conn)
            # SQLite can calculate the UTF-8 byte length without returning the
            # payload body. Reject the page before a full row materialization.
            metadata_cursor = conn.execute(
                f"SELECT cursor, length(CAST(payload_json AS BLOB)) AS payload_bytes "
                f"FROM v2_events WHERE {' AND '.join(clauses)} "
                "ORDER BY cursor LIMIT ?",
                tuple(args),
            )
            try:
                metadata_rows = metadata_cursor.fetchall()
            finally:
                metadata_cursor.close()
            total_payload_bytes = 0
            for row in metadata_rows:
                payload_bytes = int(row["payload_bytes"] or 0)
                if payload_bytes > max_payload_bytes:
                    raise ReplayBoundaryExceeded(
                        f"event payload {payload_bytes} exceeds page budget "
                        f"{max_payload_bytes} bytes"
                    )
                total_payload_bytes += payload_bytes
                if total_payload_bytes > max_payload_bytes:
                    raise ReplayBoundaryExceeded(
                        f"event page payload {total_payload_bytes} exceeds "
                        f"{max_payload_bytes} bytes"
                    )
            if not metadata_rows:
                return ()
            cursors = tuple(int(row["cursor"]) for row in metadata_rows)
            placeholders = ",".join("?" for _ in cursors)
            event_cursor = conn.execute(
                f"SELECT * FROM v2_events WHERE cursor IN ({placeholders}) "
                "ORDER BY cursor",
                cursors,
            )
            try:
                rows = event_cursor.fetchall()
            finally:
                event_cursor.close()
        finally:
            conn.close()
        return tuple(self._stored_event(row) for row in rows)

    def count_events(self) -> int:
        with self._connect(read_only=True) as conn:
            self._require_schema(conn)
            return int(conn.execute("SELECT COUNT(*) FROM v2_events").fetchone()[0])

    def _map_legacy_identity(
        self,
        conn: sqlite3.Connection,
        *,
        source_system: str,
        source_type: str,
        source_identity: str,
        target_type: str,
        attribution_state: str,
    ) -> IdentityMapping:
        if not conn.in_transaction:
            raise TransactionOwnershipError("identity mapping requires an active transaction")
        values = (source_system, source_type, source_identity, target_type)
        if not all(isinstance(value, str) and value.strip() for value in values):
            raise LedgerValidationError("identity mapping values must be non-empty")
        if attribution_state not in {"EXACT", "UNATTRIBUTED"}:
            raise LedgerValidationError("unsupported attribution state")
        row = conn.execute(
            """SELECT * FROM v2_legacy_identity_map
               WHERE source_system=? AND source_type=? AND source_identity=?""",
            (source_system, source_type, source_identity),
        ).fetchone()
        digest = _sha256("\0".join(values))[:32]
        target_id = f"shadow_{target_type}_{digest}"
        if row is not None:
            if (
                row["target_type"] != target_type
                or row["target_id"] != target_id
                or row["attribution_state"] != attribution_state
            ):
                raise IdentityMappingConflict("legacy source identity mapping conflicts")
            return IdentityMapping(**dict(row))
        stamp = _now()
        try:
            conn.execute(
                """INSERT INTO v2_legacy_identity_map
                   (source_system,source_type,source_identity,target_type,target_id,
                    attribution_state,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    source_system,
                    source_type,
                    source_identity,
                    target_type,
                    target_id,
                    attribution_state,
                    stamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise IdentityMappingConflict("legacy target identity already mapped") from exc
        return IdentityMapping(
            source_system,
            source_type,
            source_identity,
            target_type,
            target_id,
            attribution_state,
            stamp,
        )

    def map_legacy_identity(
        self,
        *,
        source_system: str,
        source_type: str,
        source_identity: str,
        target_type: str,
        attribution_state: str = "EXACT",
    ) -> IdentityMapping:
        with self.unit_of_work() as unit:
            return unit.map_legacy_identity(
                source_system=source_system,
                source_type=source_type,
                source_identity=source_identity,
                target_type=target_type,
                attribution_state=attribution_state,
            )

    @staticmethod
    def _inbox_receipt(row: sqlite3.Row) -> InboxReceipt:
        return InboxReceipt(
            cursor=int(row["cursor"]),
            receipt_id=row["receipt_id"],
            source=row["source"],
            dedup_key=row["dedup_key"],
            provider_cursor=row["provider_cursor"],
            received_at=row["received_at"],
            payload=JsonDocument(row["payload_json"]),
            payload_hash=row["payload_hash"],
            state=row["state"],
            normalized_event_id=row["normalized_event_id"],
            quarantine_reason=row["quarantine_reason"],
        )

    def receive_inbox(
        self,
        *,
        source: str,
        dedup_key: str,
        payload: JsonDocument | dict[str, Any],
        provider_cursor: str | None = None,
        received_at: str | None = None,
    ) -> InboxReceipt:
        document = JsonDocument.from_value(payload)
        payload_hash = _sha256(document.to_json())
        stamp = received_at or _now()
        with self.unit_of_work() as unit:
            conn = unit.connection
            existing = conn.execute(
                "SELECT * FROM v2_event_inbox WHERE source=? AND dedup_key=?",
                (source, dedup_key),
            ).fetchone()
            if existing is not None:
                if (
                    existing["payload_hash"] != payload_hash
                    or existing["provider_cursor"] != provider_cursor
                ):
                    raise IdempotencyConflict(
                        "inbox source/dedup key has conflicting content"
                    )
                return self._inbox_receipt(existing)
            receipt_id = "receipt_" + uuid.uuid4().hex
            cursor = conn.execute(
                """INSERT INTO v2_event_inbox
                   (receipt_id,source,dedup_key,provider_cursor,received_at,payload_json,
                    payload_hash,state,normalized_event_id,quarantine_reason)
                   VALUES (?,?,?,?,?,?,?,'RECEIVED',NULL,NULL)""",
                (
                    receipt_id,
                    source,
                    dedup_key,
                    provider_cursor,
                    stamp,
                    document.to_json(),
                    payload_hash,
                ),
            ).lastrowid
            row = conn.execute(
                "SELECT * FROM v2_event_inbox WHERE cursor=?", (cursor,)
            ).fetchone()
            return self._inbox_receipt(row)

    def get_inbox(self, receipt_id: str) -> InboxReceipt | None:
        with self._connect(read_only=True) as conn:
            self._require_schema(conn)
            row = conn.execute(
                "SELECT * FROM v2_event_inbox WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
        return self._inbox_receipt(row) if row is not None else None

    def normalize_inbox(
        self, receipt_id: str, request: AppendRequest
    ) -> AppendResult:
        if request.correlation_id != receipt_id:
            raise LedgerValidationError(
                "normalized event correlation_id must equal inbox receipt_id"
            )
        with self.unit_of_work() as unit:
            conn = unit.connection
            row = conn.execute(
                "SELECT * FROM v2_event_inbox WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
            if row is None:
                raise LedgerValidationError(f"unknown inbox receipt: {receipt_id}")
            if row["state"] == "QUARANTINED":
                raise LedgerValidationError("quarantined inbox receipt cannot normalize")
            result = unit.append(request)
            if row["state"] == "PROCESSED":
                if row["normalized_event_id"] != result.stored_event.event.event_id:
                    raise IdempotencyConflict(
                        "inbox receipt already normalized to a different event"
                    )
                return result
            self._fault("before_inbox_processed")
            updated = conn.execute(
                """UPDATE v2_event_inbox SET state='PROCESSED',normalized_event_id=?
                   WHERE receipt_id=? AND state='RECEIVED'""",
                (result.stored_event.event.event_id, receipt_id),
            ).rowcount
            if updated != 1:
                raise IdempotencyConflict("inbox receipt state changed during normalization")
            self._fault("after_inbox_processed")
            return result

    def quarantine_inbox(self, receipt_id: str, reason: str) -> InboxReceipt:
        reason = reason.strip()
        if not reason:
            raise LedgerValidationError("quarantine reason is required")
        with self.unit_of_work() as unit:
            updated = unit.connection.execute(
                """UPDATE v2_event_inbox
                   SET state='QUARANTINED',quarantine_reason=?
                   WHERE receipt_id=? AND state='RECEIVED'""",
                (reason, receipt_id),
            ).rowcount
            if updated != 1:
                raise LedgerValidationError(
                    "only a received inbox item can be quarantined"
                )
        result = self.get_inbox(receipt_id)
        if result is None:
            raise LedgerValidationError(f"unknown inbox receipt: {receipt_id}")
        return result

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

    def list_outbox(
        self,
        *,
        after_cursor: int = 0,
        limit: int = 100,
        states: tuple[str, ...] = ("PENDING", "RETRY"),
    ) -> tuple[OutboxRecord, ...]:
        if not states or not set(states).issubset({"PENDING", "RETRY", "APPLIED", "FAILED"}):
            raise LedgerValidationError("invalid outbox states")
        limit = max(1, min(int(limit), 1000))
        placeholders = ",".join("?" for _ in states)
        with self._connect(read_only=True) as conn:
            self._require_schema(conn)
            rows = conn.execute(
                f"""SELECT * FROM v2_event_outbox
                    WHERE cursor>? AND state IN ({placeholders})
                    ORDER BY cursor LIMIT ?""",
                (max(0, int(after_cursor)), *states, limit),
            ).fetchall()
        return tuple(self._outbox_record(row) for row in rows)

    def _set_outbox_result(
        self,
        outbox_id: str,
        *,
        state: str,
        error: str | None,
    ) -> OutboxRecord:
        with self.unit_of_work() as unit:
            updated = unit.connection.execute(
                """UPDATE v2_event_outbox
                   SET state=?,attempts=attempts+1,last_error=?,updated_at=?
                   WHERE outbox_id=? AND state IN ('PENDING','RETRY')""",
                (state, error, _now(), outbox_id),
            ).rowcount
            if updated != 1:
                raise LedgerValidationError("outbox item is absent or already terminal")
            row = unit.connection.execute(
                "SELECT * FROM v2_event_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            return self._outbox_record(row)

    def mark_outbox_applied(self, outbox_id: str) -> OutboxRecord:
        return self._set_outbox_result(outbox_id, state="APPLIED", error=None)

    def record_outbox_failure(
        self, outbox_id: str, error: str, *, terminal: bool = False
    ) -> OutboxRecord:
        error = error.strip()
        if not error:
            raise LedgerValidationError("outbox error is required")
        return self._set_outbox_result(
            outbox_id,
            state="FAILED" if terminal else "RETRY",
            error=error[:4000],
        )

    def deliver_outbox_once(
        self, outbox_id: str, consumer: Callable[[OutboxRecord], None]
    ) -> OutboxRecord:
        with self._connect(read_only=True) as conn:
            self._require_schema(conn)
            row = conn.execute(
                "SELECT * FROM v2_event_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
        record = self._outbox_record(row) if row is not None else None
        if record is None:
            raise LedgerValidationError(f"unknown outbox item: {outbox_id}")
        if record.state not in {"PENDING", "RETRY"}:
            return record
        try:
            consumer(record)
        except Exception as exc:
            return self.record_outbox_failure(outbox_id, str(exc) or type(exc).__name__)
        return self.mark_outbox_applied(outbox_id)

    def save_checkpoint(
        self,
        projection_name: str,
        last_cursor: int,
        *,
        state: str = "APPLIED",
        last_error: str | None = None,
    ) -> ProjectionCheckpoint:
        if state not in {"PENDING", "APPLIED", "DEGRADED"}:
            raise LedgerValidationError("invalid projection checkpoint state")
        if last_cursor < 0:
            raise LedgerValidationError("checkpoint cursor must not be negative")
        stamp = _now()
        with self.unit_of_work() as unit:
            existing = unit.connection.execute(
                """SELECT last_cursor FROM v2_projection_checkpoints
                   WHERE projection_name=?""",
                (projection_name,),
            ).fetchone()
            if existing is not None and int(existing["last_cursor"]) > last_cursor:
                raise AggregateVersionConflict("projection checkpoint cannot move backward")
            unit.connection.execute(
                """INSERT INTO v2_projection_checkpoints
                   (projection_name,last_cursor,state,updated_at,last_error)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(projection_name) DO UPDATE SET
                       last_cursor=excluded.last_cursor,state=excluded.state,
                       updated_at=excluded.updated_at,last_error=excluded.last_error""",
                (projection_name, last_cursor, state, stamp, last_error),
            )
        return ProjectionCheckpoint(
            projection_name, last_cursor, state, stamp, last_error
        )
