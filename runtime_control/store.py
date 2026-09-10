"""Transactional SQLite implementation of the PVX-1806 foundation."""

from __future__ import annotations

import base64
import contextlib
import datetime as _datetime
import hashlib
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
import sqlite3
from typing import Any, Callable
import uuid

from .clock import Clock, SystemClock, decode_time, encode_time
from .errors import (
    CapacityExceeded,
    ClockAnomaly,
    InvalidTransition,
    LeaseExpired,
    PayloadSizeExceeded,
    RecoveryBlocked,
    ResourceBusy,
    RuntimeAuthorizationError,
    RuntimeBusy,
    RuntimeConflict,
    RuntimeIdempotencyConflict,
    RuntimeNotFound,
    RuntimeSchemaError,
    RuntimeVersionConflict,
    SafetyDecisionPending,
    StaleMutation,
)
from .models import (
    AllocationRecord,
    AssignmentRecord,
    AttemptRecord,
    CommandReceipt,
    ExecutionRecord,
    IncarnationRecord,
    ProtectedResourceRecord,
    RecoveryRecord,
    RuntimeEventPage,
    RuntimeEventRecord,
    WorkerRecord,
)
from .replay import replay_runtime_events, replay_worker_events
from .schema import LATEST_SCHEMA_VERSION, MIGRATIONS


FaultInjector = Callable[[str], None]


def _canonical(value: Any) -> str:
    if hasattr(value, "to_json"):
        value = value.to_dict()
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeConflict("value must contain finite JSON data") from exc


def _mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("value must be a mapping")
    return dict(value)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


class RuntimeControlStore:
    """A default-off, explicitly initialized runtime ownership store."""

    EVENT_FAMILY = "RUNTIME_WORKER_V1"
    CURSOR_VERSION = 1

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Clock | None = None,
        busy_timeout_ms: int = 30_000,
        max_event_payload_bytes: int = 1_048_576,
        max_evidence_payload_bytes: int = 262_144,
        max_lease_seconds: int = 3_600,
        fault_injector: FaultInjector | None = None,
    ):
        self.path = Path(path).expanduser()
        self.clock = clock or SystemClock()
        self.busy_timeout_ms = max(0, int(busy_timeout_ms))
        self.max_event_payload_bytes = max(1, int(max_event_payload_bytes))
        self.max_evidence_payload_bytes = max(1, int(max_evidence_payload_bytes))
        self.max_lease_seconds = max(1, int(max_lease_seconds))
        self.fault_injector = fault_injector

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

    def connect(self) -> sqlite3.Connection:
        """Return an independent configured connection for controlled fixtures."""
        self._require_schema_path()
        return self._connect()

    def _require_schema_path(self) -> None:
        if not self.path.exists():
            raise RuntimeSchemaError("runtime-control schema is not initialized")

    def initialize(self) -> None:
        """Create or upgrade only the additive runtime-control schema."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS runtime_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    applied_at TEXT NOT NULL
                )"""
            )
            applied = {
                int(row["version"])
                for row in conn.execute("SELECT version FROM runtime_schema_migrations")
            }
            unknown = sorted(version for version in applied if version > LATEST_SCHEMA_VERSION)
            if unknown:
                raise RuntimeSchemaError(
                    f"unsupported runtime schema versions: {unknown}"
                )
            now = encode_time(self.clock.now())
            for migration in MIGRATIONS:
                if migration.version in applied:
                    continue
                for statement in migration.statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO runtime_schema_migrations(version,name,applied_at) VALUES (?,?,?)",
                    (migration.version, migration.name, now),
                )
            conn.execute(
                "INSERT OR IGNORE INTO runtime_clock_state(state_id,last_coordinator_time) VALUES(1,?)",
                (now,),
            )
            conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
                raise RuntimeBusy(str(exc)) from exc
            raise
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def schema_version(self) -> int | None:
        if not self.path.exists():
            return None
        conn = self._connect(read_only=True)
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_schema_migrations'"
            ).fetchone()
            if row is None:
                return None
            row = conn.execute(
                "SELECT MAX(version) AS version FROM runtime_schema_migrations"
            ).fetchone()
            return int(row["version"]) if row["version"] is not None else 0
        finally:
            conn.close()

    def _require_schema(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_schema_migrations'"
        ).fetchone()
        if row is None:
            raise RuntimeSchemaError("runtime-control schema is not initialized")
        version = conn.execute(
            "SELECT MAX(version) AS version FROM runtime_schema_migrations"
        ).fetchone()["version"]
        if version != LATEST_SCHEMA_VERSION:
            raise RuntimeSchemaError(
                f"runtime schema version {version!r} is not supported"
            )

    def _fault(self, stage: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(stage)

    @contextlib.contextmanager
    def _transaction(
        self,
        *,
        safety_key: str | None = None,
    ) -> Iterator[tuple[sqlite3.Connection, str, str | None]]:
        """Run one business transaction with an optional durable safety handoff.

        Authorization operations create a durable guard before taking the
        business lock.  That guard is the fail-closed barrier if the process
        exits after business rollback but before the fresh clock observation is
        committed.  Non-authorization writes retain the original short
        transaction behavior and do not serialize unrelated runtime work.
        """
        self._observe_coordinator_time()
        handoff_token: str | None = None
        handoff_at: str | None = None
        if safety_key is not None:
            handoff_token, handoff_at = self._begin_safety_handoff(safety_key)
        conn: sqlite3.Connection | None = None
        fresh_at: str | None = None
        committed = False
        try:
            conn = self._connect()
            self._require_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            # Authorization must use a clock sample taken after this
            # transaction owns the protected write lock.
            fresh_at = encode_time(self.clock.now())
            now = self._coordinator_now(conn, fresh_at)
            yield conn, now, handoff_token
            if handoff_token is not None:
                self._clear_safety_handoff_in_transaction(conn, safety_key, handoff_token)
            self._fault("before_commit")
            conn.execute("COMMIT")
            committed = True
            self._fault("after_commit")
        except sqlite3.OperationalError as exc:
            if conn is not None and conn.in_transaction:
                conn.execute("ROLLBACK")
            if not committed:
                if handoff_token is not None:
                    safety_error = self._complete_safety_handoff(
                        safety_key,
                        handoff_token,
                        fresh_at or handoff_at,
                    )
                    if safety_error is not None:
                        if hasattr(exc, "add_note"):
                            exc.add_note(f"safety handoff incomplete: {safety_error}")
                        raise safety_error from exc
                elif fresh_at is not None:
                    self._persist_coordinator_time(fresh_at)
            if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
                raise RuntimeBusy(str(exc)) from exc
            raise
        except Exception as exc:
            if conn is not None and conn.in_transaction:
                conn.execute("ROLLBACK")
            if not committed:
                if handoff_token is not None:
                    safety_error = self._complete_safety_handoff(
                        safety_key,
                        handoff_token,
                        fresh_at or handoff_at,
                    )
                    if safety_error is not None:
                        if hasattr(exc, "add_note"):
                            exc.add_note(f"safety handoff incomplete: {safety_error}")
                        raise safety_error from exc
                elif fresh_at is not None:
                    self._persist_coordinator_time(fresh_at)
            raise
        finally:
            if conn is not None:
                conn.close()

    def _observe_coordinator_time(self) -> str:
        """Persist trusted time before a business transaction can be rejected."""
        now = encode_time(self.clock.now())
        self._persist_coordinator_time(now)
        return now

    def _persist_coordinator_time(self, now: str) -> None:
        """Commit a monotonic trusted-time observation in its own transaction."""
        conn = self._connect()
        try:
            self._require_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT last_coordinator_time FROM runtime_clock_state WHERE state_id=1"
            ).fetchone()
            if row is None:
                raise RuntimeSchemaError("runtime clock state is not initialized")
            if decode_time(now) < decode_time(row["last_coordinator_time"]):
                raise ClockAnomaly(
                    f"coordinator clock moved backwards from {row['last_coordinator_time']} to {now}"
                )
            conn.execute(
                "UPDATE runtime_clock_state SET last_coordinator_time=? WHERE state_id=1",
                (now,),
            )
            conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
                raise RuntimeBusy(str(exc)) from exc
            raise
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _begin_safety_handoff(self, handoff_key: str) -> tuple[str, str]:
        """Durably block one assignment before its authorization transaction."""
        handoff_key = _text("handoff_key", handoff_key)
        now = encode_time(self.clock.now())
        token = _new_id("safety")
        conn = self._connect()
        try:
            self._require_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT handoff_token FROM runtime_safety_handoffs WHERE handoff_key=?",
                (handoff_key,),
            ).fetchone()
            if existing is not None:
                conn.execute("ROLLBACK")
                raise SafetyDecisionPending(
                    f"safety handoff is pending for assignment {handoff_key}"
                )
            row = conn.execute(
                "SELECT lease_expires_at FROM runtime_assignments WHERE assignment_id=?",
                (handoff_key,),
            ).fetchone()
            # An unknown assignment still gets a guard for the duration of this
            # failed attempt; the value is only a conservative recovery bound.
            blocked_until = row["lease_expires_at"] if row is not None else now
            conn.execute(
                """INSERT INTO runtime_safety_handoffs(
                    handoff_key,handoff_token,observed_at,blocked_until,created_at
                ) VALUES(?,?,?,?,?)""",
                (handoff_key, token, now, blocked_until, now),
            )
            conn.execute("COMMIT")
            return token, now
        except sqlite3.OperationalError as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
                raise RuntimeBusy(str(exc)) from exc
            raise
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @staticmethod
    def _clear_safety_handoff_in_transaction(
        conn: sqlite3.Connection,
        handoff_key: str | None,
        handoff_token: str,
    ) -> None:
        if handoff_key is None:
            raise RuntimeConflict("safety handoff key is missing")
        deleted = conn.execute(
            "DELETE FROM runtime_safety_handoffs WHERE handoff_key=? AND handoff_token=?",
            (handoff_key, handoff_token),
        ).rowcount
        if deleted != 1:
            raise SafetyDecisionPending("safety handoff is no longer owned by this transaction")

    def _clear_safety_handoff(self, handoff_key: str, handoff_token: str) -> None:
        conn = self._connect()
        try:
            self._require_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            deleted = conn.execute(
                "DELETE FROM runtime_safety_handoffs WHERE handoff_key=? AND handoff_token=?",
                (handoff_key, handoff_token),
            ).rowcount
            if deleted != 1:
                raise SafetyDecisionPending("safety handoff is no longer owned by this transaction")
            conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
                raise RuntimeBusy(str(exc)) from exc
            raise
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _complete_safety_handoff(
        self,
        handoff_key: str | None,
        handoff_token: str,
        observed_at: str | None,
    ) -> Exception | None:
        """Publish the post-lock observation, then remove the durable guard."""
        if handoff_key is None or observed_at is None:
            return RuntimeConflict("safety handoff has no durable observation")
        try:
            # The watermark is committed before the guard is removed.  During
            # this short two-transaction handoff, new authorization still sees
            # the guard and fails closed.
            self._fault("safety_before_commit")
            self._persist_coordinator_time(observed_at)
            self._fault("safety_after_commit")
            self._clear_safety_handoff(handoff_key, handoff_token)
        except Exception as exc:
            return exc
        return None

    def get_safety_handoff(self, handoff_key: str) -> dict[str, str] | None:
        """Read a pending safety guard for qualification and recovery tooling."""
        handoff_key = _text("handoff_key", handoff_key)
        with self._read_connection() as conn:
            row = conn.execute(
                """SELECT handoff_key,handoff_token,observed_at,blocked_until,created_at
                   FROM runtime_safety_handoffs WHERE handoff_key=?""",
                (handoff_key,),
            ).fetchone()
            return dict(row) if row is not None else None

    def recover_safety_handoff(
        self,
        handoff_key: str,
        *,
        old_process_stopped: bool,
        side_effect_fence_verified: bool,
    ) -> bool:
        """Clear an orphaned guard only after explicit fencing evidence.

        Recovery is intentionally conservative: the current coordinator time
        must be at or beyond the lease bound recorded with the guard.  A clock
        rollback therefore cannot turn an unresolved handoff into authority.
        """
        handoff_key = _text("handoff_key", handoff_key)
        if not old_process_stopped or not side_effect_fence_verified:
            raise RecoveryBlocked(
                "old process stop and side-effect fence evidence are required"
            )
        self._observe_coordinator_time()
        now = encode_time(self.clock.now())
        conn = self._connect()
        try:
            self._require_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT blocked_until FROM runtime_safety_handoffs WHERE handoff_key=?",
                (handoff_key,),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return False
            if decode_time(now) < decode_time(row["blocked_until"]):
                conn.execute("ROLLBACK")
                raise RecoveryBlocked(
                    "safety handoff remains inside its recorded lease bound"
                )
            watermark = conn.execute(
                "SELECT last_coordinator_time FROM runtime_clock_state WHERE state_id=1"
            ).fetchone()
            if watermark is None:
                raise RuntimeSchemaError("runtime clock state is not initialized")
            if decode_time(now) < decode_time(watermark["last_coordinator_time"]):
                raise ClockAnomaly(
                    f"coordinator clock moved backwards from {watermark['last_coordinator_time']} to {now}"
                )
            conn.execute(
                "UPDATE runtime_clock_state SET last_coordinator_time=? WHERE state_id=1",
                (now,),
            )
            conn.execute(
                "DELETE FROM runtime_safety_handoffs WHERE handoff_key=?",
                (handoff_key,),
            )
            conn.execute("COMMIT")
            return True
        except sqlite3.OperationalError as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
                raise RuntimeBusy(str(exc)) from exc
            raise
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @staticmethod
    def _coordinator_now(conn: sqlite3.Connection, observed_at: str) -> str:
        row = conn.execute(
            "SELECT last_coordinator_time FROM runtime_clock_state WHERE state_id=1"
        ).fetchone()
        if row is None:
            raise RuntimeSchemaError("runtime clock state is not initialized")
        if decode_time(observed_at) < decode_time(row["last_coordinator_time"]):
            raise ClockAnomaly(
                "a newer coordinator time was observed before the business transaction "
                f"({observed_at} < {row['last_coordinator_time']})"
            )
        if decode_time(observed_at) > decode_time(row["last_coordinator_time"]):
            conn.execute(
                "UPDATE runtime_clock_state SET last_coordinator_time=? WHERE state_id=1",
                (observed_at,),
            )
        return observed_at

    @staticmethod
    def _command_args(
        command_id: str | None,
        idempotency_key: str | None,
        default_key: str,
    ) -> tuple[str, str]:
        if command_id is not None:
            command_id = _text("command_id", command_id)
        if idempotency_key is not None:
            idempotency_key = _text("idempotency_key", idempotency_key)
        if command_id and idempotency_key and command_id != idempotency_key:
            raise RuntimeConflict("command_id and idempotency_key must match")
        key = command_id or idempotency_key or default_key
        return key, key

    def _existing_command(
        self,
        conn: sqlite3.Connection,
        *,
        scope: str,
        key: str,
        fingerprint: str,
        now: str,
    ) -> CommandReceipt | None:
        row = conn.execute(
            "SELECT * FROM runtime_command_receipts WHERE idempotency_scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["semantic_fingerprint"] != fingerprint:
            raise RuntimeIdempotencyConflict(
                f"idempotency key {scope}/{key} has different semantics"
            )
        return self._receipt_from_row(conn, row, duplicate=True, now=now)

    def _receipt_from_row(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        duplicate: bool,
        now: str,
    ) -> CommandReceipt:
        result = json.loads(row["result_json"])
        authority: bool | None = None
        assignment_id = result.get("assignment_id")
        if assignment_id:
            assignment = conn.execute(
                "SELECT lifecycle,lease_expires_at FROM runtime_assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            authority = bool(
                assignment is not None
                and assignment["lifecycle"] == "ACTIVE"
                and decode_time(assignment["lease_expires_at"]) > decode_time(now)
            )
        return CommandReceipt(
            command_id=row["command_id"],
            idempotency_scope=row["idempotency_scope"],
            idempotency_key=row["idempotency_key"],
            original_committed_result=result,
            current_authority_valid=authority,
            duplicate=duplicate,
            receipt_id=row["receipt_id"],
        )

    def _save_receipt(
        self,
        conn: sqlite3.Connection,
        *,
        command_id: str,
        scope: str,
        key: str,
        fingerprint: str,
        result: Mapping[str, Any],
        now: str,
    ) -> CommandReceipt:
        receipt_id = _new_id("rcr")
        conn.execute(
            """INSERT INTO runtime_command_receipts(
                receipt_id,command_id,idempotency_scope,idempotency_key,
                semantic_fingerprint,result_json,committed_at
            ) VALUES(?,?,?,?,?,?,?)""",
            (receipt_id, command_id, scope, key, fingerprint, _canonical(result), now),
        )
        row = conn.execute(
            "SELECT * FROM runtime_command_receipts WHERE receipt_id=?", (receipt_id,)
        ).fetchone()
        assert row is not None
        self._fault("after_receipt")
        return self._receipt_from_row(conn, row, duplicate=False, now=now)

    def _append_event(
        self,
        conn: sqlite3.Connection,
        *,
        stream_type: str,
        stream_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        scope: str,
        key: str,
        now: str,
    ) -> RuntimeEventRecord:
        payload_json = _canonical(payload)
        payload_bytes = len(payload_json.encode("utf-8"))
        if payload_bytes > self.max_event_payload_bytes:
            raise PayloadSizeExceeded(
                f"runtime event payload {payload_bytes} exceeds {self.max_event_payload_bytes} bytes"
            )
        row = conn.execute(
            "SELECT current_sequence FROM runtime_stream_versions WHERE stream_type=? AND stream_id=?",
            (stream_type, stream_id),
        ).fetchone()
        current = int(row["current_sequence"]) if row is not None else 0
        sequence = current + 1
        conn.execute(
            """INSERT INTO runtime_stream_versions(stream_type,stream_id,current_sequence)
               VALUES(?,?,?)
               ON CONFLICT(stream_type,stream_id) DO UPDATE SET current_sequence=excluded.current_sequence""",
            (stream_type, stream_id, sequence),
        )
        event_id = _new_id("rte")
        payload_hash = _hash(payload)
        conn.execute(
            """INSERT INTO runtime_events(
                event_id,stream_type,stream_id,sequence,event_family,event_type,schema_version,
                occurred_at,recorded_at,payload_json,payload_hash,idempotency_scope,idempotency_key
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id,
                stream_type,
                stream_id,
                sequence,
                self.EVENT_FAMILY,
                event_type,
                1,
                now,
                now,
                payload_json,
                payload_hash,
                scope,
                f"{key}:event",
            ),
        )
        position_row = conn.execute(
            "SELECT global_position FROM runtime_event_positions WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if position_row is None:
            raise RuntimeSchemaError("runtime event position trigger did not run")
        position = int(position_row["global_position"])
        self._fault("after_event")
        outbox_id = _new_id("rto")
        conn.execute(
            """INSERT INTO runtime_outbox(
                outbox_id,event_id,destination,idempotency_key,payload_json,state
            ) VALUES(?,?,?,?,?,'PENDING')""",
            (
                outbox_id,
                event_id,
                "runtime_projection",
                f"{scope}:{key}:outbox",
                _canonical({"event_id": event_id, "event_type": event_type}),
            ),
        )
        self._fault("after_outbox")
        return RuntimeEventRecord(
            event_id=event_id,
            stream_type=stream_type,
            stream_id=stream_id,
            sequence=sequence,
            event_family=self.EVENT_FAMILY,
            event_type=event_type,
            schema_version=1,
            occurred_at=now,
            recorded_at=now,
            payload=payload,
            payload_hash=payload_hash,
            global_position=position,
        )

    @staticmethod
    def _worker_from(row: sqlite3.Row) -> WorkerRecord:
        return WorkerRecord(
            worker_id=row["worker_id"],
            worker_kind=row["worker_kind"],
            host_reference=row["host_reference"],
            capabilities=tuple(json.loads(row["capabilities_json"])),
            capacity=int(row["capacity"]),
            lifecycle=row["lifecycle"],
            version=int(row["version"]),
            current_incarnation_id=row["current_incarnation_id"],
            last_heartbeat_at=row["last_heartbeat_at"],
        )

    @staticmethod
    def _incarnation_from(row: sqlite3.Row) -> IncarnationRecord:
        return IncarnationRecord(
            incarnation_id=row["incarnation_id"],
            worker_id=row["worker_id"],
            generation=int(row["generation"]),
            lifecycle=row["lifecycle"],
            version=int(row["version"]),
            started_at=row["started_at"],
            last_heartbeat_at=row["last_heartbeat_at"],
        )

    @staticmethod
    def _execution_from(row: sqlite3.Row) -> ExecutionRecord:
        return ExecutionRecord(
            execution_id=row["execution_id"],
            task_id=row["task_id"],
            request_snapshot=json.loads(row["request_json"]),
            execution_policy=json.loads(row["policy_json"]),
            route_constraints=json.loads(row["route_json"]),
            lifecycle=row["lifecycle"],
            version=int(row["version"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _attempt_from(row: sqlite3.Row) -> AttemptRecord:
        return AttemptRecord(
            attempt_id=row["attempt_id"],
            execution_id=row["execution_id"],
            retry_index=int(row["retry_index"]),
            provider_session_id=row["provider_session_id"],
            lifecycle=row["lifecycle"],
            version=int(row["version"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _assignment_from(row: sqlite3.Row) -> AssignmentRecord:
        return AssignmentRecord(
            assignment_id=row["assignment_id"],
            attempt_id=row["attempt_id"],
            worker_id=row["worker_id"],
            incarnation_id=row["incarnation_id"],
            resource_key=row["resource_key"],
            resource_epoch=int(row["resource_epoch"]),
            lifecycle=row["lifecycle"],
            version=int(row["version"]),
            lease_expires_at=row["lease_expires_at"],
            created_at=row["created_at"],
            allocation_id=row["allocation_id"] if "allocation_id" in row.keys() else None,
        )

    @staticmethod
    def _allocation_from(row: sqlite3.Row) -> AllocationRecord:
        return AllocationRecord(
            allocation_id=row["allocation_id"],
            assignment_id=row["assignment_id"],
            attempt_id=row["attempt_id"],
            worker_id=row["worker_id"],
            incarnation_id=row["incarnation_id"],
            resource_key=row["resource_key"],
            resource_epoch=int(row["resource_epoch"]),
            lifecycle=row["lifecycle"],
            version=int(row["version"]),
            expires_at=row["expires_at"],
            release_reason=row["release_reason"],
        )

    @staticmethod
    def _assignment_event_payload(
        conn: sqlite3.Connection,
        assignment_id: str,
        **metadata: Any,
    ) -> dict[str, Any]:
        assignment = conn.execute(
            "SELECT * FROM runtime_assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
        allocation = conn.execute(
            "SELECT * FROM runtime_allocations WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
        if assignment is None or allocation is None:
            raise RuntimeNotFound(f"incomplete assignment event state: {assignment_id}")
        attempt = conn.execute(
            "SELECT attempt_id,lifecycle,version FROM runtime_attempts WHERE attempt_id=?",
            (assignment["attempt_id"],),
        ).fetchone()
        if attempt is None:
            raise RuntimeNotFound(f"unknown assignment attempt: {assignment['attempt_id']}")
        recovery = conn.execute(
            "SELECT assignment_id,state,reason,attempts FROM runtime_recovery_work WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
        state = {
            "state_version": 1,
            "assignment": {
                key: assignment[key]
                for key in (
                    "assignment_id", "attempt_id", "worker_id", "incarnation_id",
                    "resource_key", "resource_epoch", "lifecycle", "version",
                    "lease_expires_at", "created_at", "released_at",
                )
            },
            "allocation": {
                key: allocation[key]
                for key in (
                    "allocation_id", "assignment_id", "attempt_id", "worker_id",
                    "incarnation_id", "resource_key", "resource_epoch", "lifecycle",
                    "version", "expires_at", "release_reason",
                )
            },
            "attempt": {key: attempt[key] for key in ("attempt_id", "lifecycle", "version")},
            "recovery": (
                {key: recovery[key] for key in ("assignment_id", "state", "reason", "attempts")}
                if recovery is not None
                else None
            ),
        }
        return {
            "assignment_id": assignment_id,
            "lifecycle": assignment["lifecycle"],
            "state": state,
            **metadata,
        }

    def register_task_reference(
        self,
        task_id: str,
        *,
        source_system: str = "runtime_control",
        command_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CommandReceipt:
        task_id = _text("task_id", task_id)
        source_system = _text("source_system", source_system)
        command, key = self._command_args(command_id, idempotency_key, f"task:{task_id}")
        scope = f"task:{task_id}"
        semantic = {"operation": "register_task", "task_id": task_id, "source_system": source_system}
        fingerprint = _hash(semantic)
        with self._transaction() as (conn, now, handoff_token):
            existing = self._existing_command(conn, scope=scope, key=key, fingerprint=fingerprint, now=now)
            if existing:
                return existing
            row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is not None and row["source_system"] != source_system:
                raise RuntimeConflict(f"task reference already belongs to {row['source_system']}")
            if row is None:
                conn.execute(
                    "INSERT INTO runtime_tasks(task_id,source_system,created_at) VALUES(?,?,?)",
                    (task_id, source_system, now),
                )
                self._append_event(
                    conn,
                    stream_type="task",
                    stream_id=task_id,
                    event_type="TaskReferenceRegistered",
                    payload={"task_id": task_id, "source_system": source_system},
                    scope=scope,
                    key=key,
                    now=now,
                )
            return self._save_receipt(
                conn, command_id=command, scope=scope, key=key,
                fingerprint=fingerprint, result={"task_id": task_id}, now=now
            )

    def register_task(
        self,
        task_id: str,
        *,
        source_system: str = "runtime_control",
        command_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CommandReceipt:
        """Short internal alias; registration remains explicit and V1-isolated."""
        return self.register_task_reference(
            task_id,
            source_system=source_system,
            command_id=command_id,
            idempotency_key=idempotency_key,
        )

    def register_execution(
        self,
        task_id: str,
        execution_id: str,
        *,
        request_snapshot: Mapping[str, Any] | None = None,
        execution_policy: Mapping[str, Any] | None = None,
        route_constraints: Mapping[str, Any] | None = None,
        command_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CommandReceipt:
        task_id, execution_id = _text("task_id", task_id), _text("execution_id", execution_id)
        request = _mapping(request_snapshot)
        policy = _mapping(execution_policy)
        route = _mapping(route_constraints)
        command, key = self._command_args(command_id, idempotency_key, f"execution:{execution_id}")
        scope = f"execution:{execution_id}"
        semantic = {
            "operation": "register_execution", "task_id": task_id, "execution_id": execution_id,
            "request_snapshot": request, "execution_policy": policy, "route_constraints": route,
        }
        fingerprint = _hash(semantic)
        with self._transaction() as (conn, now, handoff_token):
            existing = self._existing_command(conn, scope=scope, key=key, fingerprint=fingerprint, now=now)
            if existing:
                return existing
            if conn.execute("SELECT 1 FROM runtime_tasks WHERE task_id=?", (task_id,)).fetchone() is None:
                raise RuntimeNotFound(f"unknown runtime task: {task_id}")
            if conn.execute("SELECT 1 FROM runtime_executions WHERE execution_id=?", (execution_id,)).fetchone() is not None:
                raise RuntimeConflict(f"execution already exists: {execution_id}")
            conn.execute(
                """INSERT INTO runtime_executions(
                    execution_id,task_id,request_json,policy_json,route_json,lifecycle,created_at
                ) VALUES(?,?,?,?,?,'REQUESTED',?)""",
                (execution_id, task_id, _canonical(request), _canonical(policy), _canonical(route), now),
            )
            self._append_event(
                conn, stream_type="execution", stream_id=execution_id,
                event_type="ExecutionRegistered",
                payload={"execution_id": execution_id, "task_id": task_id},
                scope=scope, key=key, now=now,
            )
            return self._save_receipt(
                conn, command_id=command, scope=scope, key=key,
                fingerprint=fingerprint, result={"execution_id": execution_id, "task_id": task_id}, now=now
            )

    def register_attempt(
        self,
        execution_id: str,
        attempt_id: str,
        retry_index: int,
        *,
        provider_session_id: str | None = None,
        command_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CommandReceipt:
        execution_id, attempt_id = _text("execution_id", execution_id), _text("attempt_id", attempt_id)
        if retry_index < 0:
            raise ValueError("retry_index must not be negative")
        if provider_session_id is not None:
            provider_session_id = _text("provider_session_id", provider_session_id)
        command, key = self._command_args(command_id, idempotency_key, f"attempt:{attempt_id}")
        scope = f"attempt:{attempt_id}"
        semantic = {
            "operation": "register_attempt", "execution_id": execution_id,
            "attempt_id": attempt_id, "retry_index": retry_index,
            "provider_session_id": provider_session_id,
        }
        fingerprint = _hash(semantic)
        with self._transaction() as (conn, now, handoff_token):
            existing = self._existing_command(conn, scope=scope, key=key, fingerprint=fingerprint, now=now)
            if existing:
                return existing
            execution = conn.execute(
                "SELECT lifecycle FROM runtime_executions WHERE execution_id=?", (execution_id,)
            ).fetchone()
            if execution is None:
                raise RuntimeNotFound(f"unknown runtime execution: {execution_id}")
            if execution["lifecycle"] == "TERMINAL":
                raise InvalidTransition("terminal execution cannot receive an attempt")
            if conn.execute("SELECT 1 FROM runtime_attempts WHERE attempt_id=?", (attempt_id,)).fetchone() is not None:
                raise RuntimeConflict(f"attempt already exists: {attempt_id}")
            if conn.execute(
                "SELECT 1 FROM runtime_attempts WHERE execution_id=? AND retry_index=?",
                (execution_id, retry_index),
            ).fetchone() is not None:
                raise RuntimeConflict("retry_index already exists for execution")
            conn.execute(
                """INSERT INTO runtime_attempts(
                    attempt_id,execution_id,retry_index,provider_session_id,lifecycle,created_at
                ) VALUES(?,?,?,?,'PENDING',?)""",
                (attempt_id, execution_id, retry_index, provider_session_id, now),
            )
            self._append_event(
                conn, stream_type="attempt", stream_id=attempt_id,
                event_type="AttemptRegistered",
                payload={"attempt_id": attempt_id, "execution_id": execution_id, "retry_index": retry_index},
                scope=scope, key=key, now=now,
            )
            return self._save_receipt(
                conn, command_id=command, scope=scope, key=key,
                fingerprint=fingerprint, result={"attempt_id": attempt_id, "execution_id": execution_id}, now=now
            )

    def register_worker(
        self,
        worker_id: str,
        *,
        worker_kind: str = "runtime",
        host_reference: str = "unknown",
        capabilities: tuple[str, ...] | list[str] = (),
        capacity: int = 1,
        command_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CommandReceipt:
        worker_id = _text("worker_id", worker_id)
        worker_kind, host_reference = _text("worker_kind", worker_kind), _text("host_reference", host_reference)
        capabilities = tuple(_text("capability", value) for value in capabilities)
        if len(set(capabilities)) != len(capabilities):
            raise ValueError("capabilities must not contain duplicates")
        if capacity < 1:
            raise ValueError("capacity must be positive")
        command, key = self._command_args(command_id, idempotency_key, f"worker:{worker_id}")
        scope = f"worker:{worker_id}"
        semantic = {
            "operation": "register_worker", "worker_id": worker_id,
            "worker_kind": worker_kind, "host_reference": host_reference,
            "capabilities": list(capabilities), "capacity": capacity,
        }
        fingerprint = _hash(semantic)
        with self._transaction() as (conn, now, handoff_token):
            existing = self._existing_command(conn, scope=scope, key=key, fingerprint=fingerprint, now=now)
            if existing:
                return existing
            row = conn.execute("SELECT * FROM runtime_workers WHERE worker_id=?", (worker_id,)).fetchone()
            if row is not None:
                raise RuntimeConflict(f"worker already exists: {worker_id}")
            conn.execute(
                """INSERT INTO runtime_workers(
                    worker_id,worker_kind,host_reference,capabilities_json,capacity,lifecycle
                ) VALUES(?,?,?,?,?,'REGISTERED')""",
                (worker_id, worker_kind, host_reference, _canonical(list(capabilities)), capacity),
            )
            self._append_event(
                conn, stream_type="worker", stream_id=worker_id,
                event_type="WorkerRegistered",
                payload={"worker_id": worker_id, "capacity": capacity, "worker_version": 0},
                scope=scope, key=key, now=now,
            )
            return self._save_receipt(
                conn, command_id=command, scope=scope, key=key,
                fingerprint=fingerprint, result={"worker_id": worker_id}, now=now
            )

    def register_incarnation(
        self,
        worker_id: str,
        *,
        incarnation_id: str | None = None,
        generation: int | None = None,
        command_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CommandReceipt:
        worker_id = _text("worker_id", worker_id)
        if incarnation_id is None and (command_id is not None or idempotency_key is not None):
            command, key = self._command_args(command_id, idempotency_key, f"incarnation-command:{worker_id}")
            incarnation_id = "inc_" + _hash({"worker_id": worker_id, "key": key})[:24]
        else:
            incarnation_id = _text("incarnation_id", incarnation_id) if incarnation_id else _new_id("inc")
            command, key = self._command_args(command_id, idempotency_key, f"incarnation:{incarnation_id}")
        scope = f"incarnation:{incarnation_id}"
        with self._transaction() as (conn, now, handoff_token):
            worker = conn.execute("SELECT * FROM runtime_workers WHERE worker_id=?", (worker_id,)).fetchone()
            if worker is None:
                raise RuntimeNotFound(f"unknown runtime worker: {worker_id}")
            stored = conn.execute(
                "SELECT * FROM runtime_command_receipts WHERE idempotency_scope=? AND idempotency_key=?",
                (scope, key),
            ).fetchone()
            if stored is not None and generation is None:
                stored_result = json.loads(stored["result_json"])
                generation = int(stored_result["generation"])
            if generation is None:
                row = conn.execute(
                    "SELECT COALESCE(MAX(generation),0)+1 AS generation FROM runtime_worker_incarnations WHERE worker_id=?",
                    (worker_id,),
                ).fetchone()
                generation = int(row["generation"])
            if generation < 1:
                raise ValueError("generation must be positive")
            semantic = {"operation": "register_incarnation", "worker_id": worker_id, "incarnation_id": incarnation_id, "generation": generation}
            fingerprint = _hash(semantic)
            existing = self._existing_command(conn, scope=scope, key=key, fingerprint=fingerprint, now=now)
            if existing:
                return existing
            if conn.execute("SELECT 1 FROM runtime_worker_incarnations WHERE incarnation_id=?", (incarnation_id,)).fetchone() is not None:
                raise RuntimeConflict(f"incarnation already exists: {incarnation_id}")
            old = conn.execute(
                "SELECT incarnation_id,version FROM runtime_worker_incarnations WHERE worker_id=? AND lifecycle='ACTIVE'",
                (worker_id,),
            ).fetchone()
            if old is not None:
                conn.execute(
                    "UPDATE runtime_worker_incarnations SET lifecycle='SUPERSEDED',version=version+1 WHERE incarnation_id=?",
                    (old["incarnation_id"],),
                )
                orphaned = conn.execute(
                    "SELECT * FROM runtime_assignments WHERE incarnation_id=? AND lifecycle='ACTIVE'",
                    (old["incarnation_id"],),
                ).fetchall()
                for assignment in orphaned:
                    conn.execute(
                        "UPDATE runtime_assignments SET lifecycle='ORPHANED',version=version+1 WHERE assignment_id=?",
                        (assignment["assignment_id"],),
                    )
                    conn.execute(
                        """UPDATE runtime_allocations
                           SET lifecycle='QUARANTINED',release_reason=?,version=version+1
                           WHERE assignment_id=? AND lifecycle='ACTIVE'""",
                        ("worker_incarnation_superseded", assignment["assignment_id"]),
                    )
                    conn.execute(
                        """INSERT INTO runtime_recovery_work(assignment_id,state,reason,updated_at)
                           VALUES(?,'PENDING','worker incarnation superseded',?)
                           ON CONFLICT(assignment_id) DO UPDATE SET state='PENDING',reason=excluded.reason,updated_at=excluded.updated_at""",
                        (assignment["assignment_id"], now),
                    )
                    self._append_event(
                        conn, stream_type="assignment", stream_id=assignment["assignment_id"],
                        event_type="AssignmentOrphaned",
                        payload=self._assignment_event_payload(
                            conn,
                            assignment["assignment_id"],
                            reason="worker_incarnation_superseded",
                        ),
                        scope=scope, key=f"{key}:{assignment['assignment_id']}", now=now,
                    )
            conn.execute(
                """INSERT INTO runtime_worker_incarnations(
                    incarnation_id,worker_id,generation,lifecycle,started_at
                ) VALUES(?,?,?,'ACTIVE',?)""",
                (incarnation_id, worker_id, generation, now),
            )
            conn.execute(
                """UPDATE runtime_workers SET current_incarnation_id=?,version=version+1
                   WHERE worker_id=?""",
                (incarnation_id, worker_id),
            )
            self._append_event(
                conn, stream_type="worker", stream_id=worker_id,
                event_type="WorkerIncarnationRegistered",
                payload={
                    "worker_id": worker_id,
                    "incarnation_id": incarnation_id,
                    "generation": generation,
                    "worker_version": int(conn.execute(
                        "SELECT version FROM runtime_workers WHERE worker_id=?", (worker_id,)
                    ).fetchone()["version"]),
                    "incarnation_version": 0,
                    "superseded_incarnation_id": old["incarnation_id"] if old is not None else None,
                    "superseded_incarnation_version": (
                        int(old["version"]) + 1 if old is not None else None
                    ),
                },
                scope=scope, key=key, now=now,
            )
            return self._save_receipt(
                conn, command_id=command, scope=scope, key=key,
                fingerprint=fingerprint, result={"worker_id": worker_id, "incarnation_id": incarnation_id, "generation": generation}, now=now
            )

    def _active_worker(self, conn: sqlite3.Connection, worker_id: str, incarnation_id: str) -> sqlite3.Row:
        row = conn.execute(
            """SELECT w.*,i.lifecycle AS incarnation_lifecycle,i.generation
               FROM runtime_workers w JOIN runtime_worker_incarnations i
                 ON i.incarnation_id=w.current_incarnation_id
               WHERE w.worker_id=? AND i.incarnation_id=?""",
            (worker_id, incarnation_id),
        ).fetchone()
        if row is None or row["lifecycle"] != "REGISTERED" or row["incarnation_lifecycle"] != "ACTIVE":
            raise StaleMutation("worker incarnation is not current and active")
        return row

    def heartbeat_worker(
        self,
        worker_id: str,
        incarnation_id: str,
        *,
        worker_reported_at: str | None = None,
        command_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CommandReceipt:
        worker_id, incarnation_id = _text("worker_id", worker_id), _text("incarnation_id", incarnation_id)
        command, key = self._command_args(command_id, idempotency_key, f"heartbeat:{worker_id}:{incarnation_id}:{self.clock.now().isoformat()}")
        scope = f"worker-incarnation:{incarnation_id}"
        semantic = {"operation": "heartbeat", "worker_id": worker_id, "incarnation_id": incarnation_id, "worker_reported_at": worker_reported_at}
        fingerprint = _hash(semantic)
        with self._transaction() as (conn, now, handoff_token):
            existing = self._existing_command(conn, scope=scope, key=key, fingerprint=fingerprint, now=now)
            if existing:
                return existing
            self._active_worker(conn, worker_id, incarnation_id)
            conn.execute(
                "UPDATE runtime_worker_incarnations SET last_heartbeat_at=?,version=version+1 WHERE incarnation_id=?",
                (now, incarnation_id),
            )
            conn.execute(
                "UPDATE runtime_workers SET last_heartbeat_at=?,version=version+1 WHERE worker_id=?",
                (now, worker_id),
            )
            versions = conn.execute(
                """SELECT w.version AS worker_version,i.version AS incarnation_version
                   FROM runtime_workers w
                   JOIN runtime_worker_incarnations i ON i.worker_id=w.worker_id
                  WHERE w.worker_id=? AND i.incarnation_id=?""",
                (worker_id, incarnation_id),
            ).fetchone()
            assert versions is not None
            self._append_event(
                conn, stream_type="incarnation", stream_id=incarnation_id,
                event_type="WorkerHeartbeatRecorded",
                payload={
                    "worker_id": worker_id,
                    "incarnation_id": incarnation_id,
                    "worker_reported_at": worker_reported_at,
                    "heartbeat_at": now,
                    "worker_version": int(versions["worker_version"]),
                    "incarnation_version": int(versions["incarnation_version"]),
                },
                scope=scope, key=key, now=now,
            )
            return self._save_receipt(
                conn, command_id=command, scope=scope, key=key, fingerprint=fingerprint,
                result={"worker_id": worker_id, "incarnation_id": incarnation_id, "heartbeat_at": now}, now=now
            )

    def assign_attempt(
        self,
        attempt_id: str,
        worker_id: str,
        incarnation_id: str,
        resource_key: str,
        *,
        lease_seconds: int = 60,
        assignment_id: str | None = None,
        command_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CommandReceipt:
        attempt_id, worker_id = _text("attempt_id", attempt_id), _text("worker_id", worker_id)
        incarnation_id, resource_key = _text("incarnation_id", incarnation_id), _text("resource_key", resource_key)
        if lease_seconds < 1 or lease_seconds > self.max_lease_seconds:
            raise ValueError("lease_seconds is outside the configured bound")
        command, key = self._command_args(command_id, idempotency_key, f"assign:{attempt_id}:{resource_key}:{worker_id}:{incarnation_id}")
        scope = f"assignment:{attempt_id}"
        assignment_id = _text("assignment_id", assignment_id) if assignment_id else "asn_" + _hash({"scope": scope, "key": key})[:24]
        semantic = {
            "operation": "assign_attempt", "attempt_id": attempt_id, "worker_id": worker_id,
            "incarnation_id": incarnation_id, "resource_key": resource_key,
            "lease_seconds": lease_seconds, "assignment_id": assignment_id,
        }
        fingerprint = _hash(semantic)
        with self._transaction() as (conn, now, handoff_token):
            existing = self._existing_command(conn, scope=scope, key=key, fingerprint=fingerprint, now=now)
            if existing:
                return existing
            worker = self._active_worker(conn, worker_id, incarnation_id)
            attempt = conn.execute("SELECT * FROM runtime_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if attempt is None:
                raise RuntimeNotFound(f"unknown attempt: {attempt_id}")
            held = conn.execute(
                "SELECT assignment_id,resource_key FROM runtime_assignments WHERE attempt_id=? AND lifecycle IN ('ACTIVE','EXPIRED','ORPHANED')",
                (attempt_id,),
            ).fetchone()
            if held is not None:
                if held["resource_key"] == resource_key:
                    raise ResourceBusy(f"attempt resource remains held or quarantined: {resource_key}")
                raise RuntimeConflict(f"attempt already has unresolved assignment: {held['assignment_id']}")
            if attempt["lifecycle"] != "PENDING":
                raise InvalidTransition(f"attempt {attempt_id} is not pending")
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM runtime_assignments WHERE worker_id=? AND lifecycle IN ('ACTIVE','EXPIRED','ORPHANED')",
                (worker_id,),
            ).fetchone()["count"]
            if int(count) >= int(worker["capacity"]):
                raise CapacityExceeded(f"worker capacity exhausted: {worker_id}")
            held_resource = conn.execute(
                "SELECT allocation_id FROM runtime_allocations WHERE resource_key=? AND lifecycle IN ('ACTIVE','QUARANTINED')",
                (resource_key,),
            ).fetchone()
            if held_resource is not None:
                raise ResourceBusy(f"resource is held or quarantined: {resource_key}")
            conn.execute(
                "INSERT INTO runtime_resource_counters(resource_key,current_epoch) VALUES(?,0) ON CONFLICT(resource_key) DO NOTHING",
                (resource_key,),
            )
            conn.execute(
                "UPDATE runtime_resource_counters SET current_epoch=current_epoch+1 WHERE resource_key=?",
                (resource_key,),
            )
            epoch = int(conn.execute(
                "SELECT current_epoch FROM runtime_resource_counters WHERE resource_key=?", (resource_key,)
            ).fetchone()["current_epoch"])
            expires = encode_time(decode_time(now) + _datetime.timedelta(seconds=lease_seconds))
            allocation_id = _new_id("alloc")
            conn.execute(
                """INSERT INTO runtime_assignments(
                    assignment_id,attempt_id,worker_id,incarnation_id,resource_key,resource_epoch,
                    lifecycle,lease_expires_at,created_at
                ) VALUES(?,?,?,?,?,?, 'ACTIVE',?,?)""",
                (assignment_id, attempt_id, worker_id, incarnation_id, resource_key, epoch, expires, now),
            )
            conn.execute(
                """INSERT INTO runtime_allocations(
                    allocation_id,assignment_id,attempt_id,worker_id,incarnation_id,resource_key,
                    resource_epoch,lifecycle,expires_at
                ) VALUES(?,?,?,?,?,?,?,'ACTIVE',?)""",
                (allocation_id, assignment_id, attempt_id, worker_id, incarnation_id, resource_key, epoch, expires),
            )
            conn.execute(
                "UPDATE runtime_attempts SET lifecycle='ASSIGNED',version=version+1 WHERE attempt_id=? AND version=?",
                (attempt_id, attempt["version"]),
            )
            conn.execute(
                """INSERT INTO runtime_protected_resources(resource_key,fencing_epoch,value_json,version)
                   VALUES(?,?,NULL,0)
                   ON CONFLICT(resource_key) DO UPDATE SET fencing_epoch=excluded.fencing_epoch""",
                (resource_key, epoch),
            )
            self._append_event(
                conn, stream_type="assignment", stream_id=assignment_id,
                event_type="AssignmentGranted",
                payload=self._assignment_event_payload(conn, assignment_id),
                scope=scope, key=key, now=now,
            )
            return self._save_receipt(
                conn, command_id=command, scope=scope, key=key, fingerprint=fingerprint,
                result={
                    "assignment_id": assignment_id, "allocation_id": allocation_id,
                    "attempt_id": attempt_id, "worker_id": worker_id,
                    "incarnation_id": incarnation_id, "resource_key": resource_key,
                    "resource_epoch": epoch, "lease_expires_at": expires,
                }, now=now
            )

    def _owner_rows(
        self,
        conn: sqlite3.Connection,
        assignment_id: str,
        worker_id: str,
        incarnation_id: str,
        attempt_id: str,
        resource_key: str,
        resource_epoch: int,
        now: str,
        *,
        handoff_token: str | None,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        if handoff_token is None:
            raise RuntimeAuthorizationError(
                "assignment owner authorization requires a durable handoff"
            )
        handoff = conn.execute(
            "SELECT handoff_token FROM runtime_safety_handoffs WHERE handoff_key=?",
            (assignment_id,),
        ).fetchone()
        if handoff is None:
            raise SafetyDecisionPending(
                f"safety handoff is missing for assignment {assignment_id}"
            )
        if handoff["handoff_token"] != handoff_token:
            raise SafetyDecisionPending(
                f"safety handoff is owned by another transaction for assignment {assignment_id}"
            )
        row = conn.execute(
            "SELECT * FROM runtime_assignments WHERE assignment_id=?", (assignment_id,)
        ).fetchone()
        if row is None:
            raise RuntimeNotFound(f"unknown assignment: {assignment_id}")
        exact = (
            row["worker_id"] == worker_id
            and row["incarnation_id"] == incarnation_id
            and row["attempt_id"] == attempt_id
            and row["resource_key"] == resource_key
            and int(row["resource_epoch"]) == int(resource_epoch)
        )
        if not exact or row["lifecycle"] != "ACTIVE":
            raise StaleMutation("assignment ownership tuple is no longer current")
        if decode_time(now) >= decode_time(row["lease_expires_at"]):
            raise LeaseExpired("assignment lease is expired")
        allocation = conn.execute(
            """SELECT * FROM runtime_allocations
               WHERE assignment_id=? AND lifecycle='ACTIVE'""",
            (assignment_id,),
        ).fetchone()
        if allocation is None or any(
            allocation[name] != value
            for name, value in (
                ("attempt_id", attempt_id), ("worker_id", worker_id),
                ("incarnation_id", incarnation_id), ("resource_key", resource_key),
            )
        ) or int(allocation["resource_epoch"]) != int(resource_epoch):
            raise StaleMutation("resource allocation ownership tuple is no longer current")
        self._active_worker(conn, worker_id, incarnation_id)
        return row, allocation

    def renew_assignment(
        self,
        assignment_id: str,
        worker_id: str,
        incarnation_id: str,
        attempt_id: str,
        resource_key: str,
        resource_epoch: int,
        *,
        lease_seconds: int = 60,
        expected_version: int | None = None,
        command_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CommandReceipt:
        values = tuple(_text(name, value) for name, value in (
            ("assignment_id", assignment_id), ("worker_id", worker_id),
            ("incarnation_id", incarnation_id), ("attempt_id", attempt_id), ("resource_key", resource_key),
        ))
        assignment_id, worker_id, incarnation_id, attempt_id, resource_key = values
        if lease_seconds < 1 or lease_seconds > self.max_lease_seconds:
            raise ValueError("lease_seconds is outside the configured bound")
        command, key = self._command_args(command_id, idempotency_key, f"renew:{assignment_id}:{resource_epoch}:{expected_version}")
        scope = f"assignment:{assignment_id}"
        semantic = {
            "operation": "renew_assignment", "assignment_id": assignment_id, "worker_id": worker_id,
            "incarnation_id": incarnation_id, "attempt_id": attempt_id, "resource_key": resource_key,
            "resource_epoch": resource_epoch, "lease_seconds": lease_seconds, "expected_version": expected_version,
        }
        fingerprint = _hash(semantic)
        with self._transaction(safety_key=assignment_id) as (conn, now, handoff_token):
            existing = self._existing_command(conn, scope=scope, key=key, fingerprint=fingerprint, now=now)
            if existing:
                return existing
            row, _ = self._owner_rows(
                conn,
                assignment_id,
                worker_id,
                incarnation_id,
                attempt_id,
                resource_key,
                resource_epoch,
                now,
                handoff_token=handoff_token,
            )
            if expected_version is not None and int(row["version"]) != expected_version:
                raise RuntimeVersionConflict(f"expected assignment version {expected_version}, actual {row['version']}")
            expires = encode_time(decode_time(now) + _datetime.timedelta(seconds=lease_seconds))
            updated = conn.execute(
                """UPDATE runtime_assignments SET lease_expires_at=?,version=version+1
                   WHERE assignment_id=? AND lifecycle='ACTIVE' AND version=?""",
                (expires, assignment_id, row["version"]),
            ).rowcount
            if updated != 1:
                raise RuntimeVersionConflict("assignment changed while renewing")
            conn.execute(
                "UPDATE runtime_allocations SET expires_at=?,version=version+1 WHERE assignment_id=? AND lifecycle='ACTIVE'",
                (expires, assignment_id),
            )
            self._append_event(
                conn, stream_type="assignment", stream_id=assignment_id,
                event_type="AssignmentRenewed",
                payload=self._assignment_event_payload(conn, assignment_id),
                scope=scope, key=key, now=now,
            )
            return self._save_receipt(
                conn, command_id=command, scope=scope, key=key, fingerprint=fingerprint,
                result={"assignment_id": assignment_id, "lease_expires_at": expires, "resource_epoch": resource_epoch}, now=now
            )

    def release_assignment(
        self,
        assignment_id: str,
        worker_id: str,
        incarnation_id: str,
        attempt_id: str,
        resource_key: str,
        resource_epoch: int,
        *,
        reason: str = "released",
        expected_version: int | None = None,
        command_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CommandReceipt:
        return self._finish_assignment(
            "release_assignment", "AssignmentReleased", "RELEASED", True,
            assignment_id, worker_id, incarnation_id, attempt_id, resource_key, resource_epoch,
            reason=reason, expected_version=expected_version, command_id=command_id, idempotency_key=idempotency_key,
        )

    def revoke_assignment(
        self,
        assignment_id: str,
        worker_id: str,
        incarnation_id: str,
        attempt_id: str,
        resource_key: str,
        resource_epoch: int,
        *,
        reason: str = "revoked",
        expected_version: int | None = None,
        command_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CommandReceipt:
        return self._finish_assignment(
            "revoke_assignment", "AssignmentRevoked", "REVOKED", False,
            assignment_id, worker_id, incarnation_id, attempt_id, resource_key, resource_epoch,
            reason=reason, expected_version=expected_version, command_id=command_id, idempotency_key=idempotency_key,
        )

    def _finish_assignment(
        self,
        operation: str,
        event_type: str,
        lifecycle: str,
        terminal_attempt: bool,
        assignment_id: str,
        worker_id: str,
        incarnation_id: str,
        attempt_id: str,
        resource_key: str,
        resource_epoch: int,
        *,
        reason: str,
        expected_version: int | None,
        command_id: str | None,
        idempotency_key: str | None,
    ) -> CommandReceipt:
        values = tuple(_text(name, value) for name, value in (
            ("assignment_id", assignment_id), ("worker_id", worker_id),
            ("incarnation_id", incarnation_id), ("attempt_id", attempt_id), ("resource_key", resource_key),
        ))
        assignment_id, worker_id, incarnation_id, attempt_id, resource_key = values
        reason = _text("reason", reason)
        command, key = self._command_args(command_id, idempotency_key, f"{operation}:{assignment_id}:{resource_epoch}:{expected_version}")
        scope = f"assignment:{assignment_id}"
        semantic = {
            "operation": operation, "assignment_id": assignment_id, "worker_id": worker_id,
            "incarnation_id": incarnation_id, "attempt_id": attempt_id, "resource_key": resource_key,
            "resource_epoch": resource_epoch, "reason": reason, "expected_version": expected_version,
        }
        fingerprint = _hash(semantic)
        with self._transaction(safety_key=assignment_id) as (conn, now, handoff_token):
            existing = self._existing_command(conn, scope=scope, key=key, fingerprint=fingerprint, now=now)
            if existing:
                return existing
            row, _ = self._owner_rows(
                conn,
                assignment_id,
                worker_id,
                incarnation_id,
                attempt_id,
                resource_key,
                resource_epoch,
                now,
                handoff_token=handoff_token,
            )
            if expected_version is not None and int(row["version"]) != expected_version:
                raise RuntimeVersionConflict(f"expected assignment version {expected_version}, actual {row['version']}")
            conn.execute(
                "UPDATE runtime_assignments SET lifecycle=?,version=version+1,released_at=? WHERE assignment_id=? AND version=?",
                (lifecycle, now, assignment_id, row["version"]),
            )
            conn.execute(
                "UPDATE runtime_allocations SET lifecycle='RELEASED',release_reason=?,version=version+1 WHERE assignment_id=? AND lifecycle='ACTIVE'",
                (reason, assignment_id),
            )
            conn.execute(
                "UPDATE runtime_attempts SET lifecycle=?,version=version+1 WHERE attempt_id=?",
                ("RELEASED" if terminal_attempt else "PENDING", attempt_id),
            )
            self._append_event(
                conn, stream_type="assignment", stream_id=assignment_id,
                event_type=event_type,
                payload=self._assignment_event_payload(conn, assignment_id, reason=reason),
                scope=scope, key=key, now=now,
            )
            return self._save_receipt(
                conn, command_id=command, scope=scope, key=key, fingerprint=fingerprint,
                result={"assignment_id": assignment_id, "resource_key": resource_key, "resource_epoch": resource_epoch, "lifecycle": lifecycle}, now=now
            )

    def reconcile_expired_once(self, *, limit: int = 100, command_id: str | None = None) -> tuple[str, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        command_prefix = _text("command_id", command_id) if command_id is not None else "expiry"
        with self._transaction() as (conn, now, handoff_token):
            batch_scope = "runtime:expiry-reconciliation"
            batch_fingerprint = _hash({"operation": "reconcile_expired_once", "limit": limit})
            if command_id is not None:
                existing_batch = self._existing_command(
                    conn,
                    scope=batch_scope,
                    key=command_prefix,
                    fingerprint=batch_fingerprint,
                    now=now,
                )
                if existing_batch:
                    return tuple(existing_batch.result["assignment_ids"])
            rows = conn.execute(
                """SELECT * FROM runtime_assignments
                   WHERE lifecycle='ACTIVE' AND lease_expires_at<=?
                   ORDER BY lease_expires_at,assignment_id LIMIT ?""",
                (now, limit),
            ).fetchall()
            expired: list[str] = []
            for row in rows:
                assignment_id = row["assignment_id"]
                key = f"{command_prefix}:{assignment_id}:{row['lease_expires_at']}"
                scope = f"assignment:{assignment_id}"
                semantic = {"operation": "reconcile_expiry", "assignment_id": assignment_id, "lease_expires_at": row["lease_expires_at"]}
                fingerprint = _hash(semantic)
                existing = self._existing_command(conn, scope=scope, key=key, fingerprint=fingerprint, now=now)
                if existing:
                    expired.append(assignment_id)
                    continue
                conn.execute(
                    "UPDATE runtime_assignments SET lifecycle='EXPIRED',version=version+1 WHERE assignment_id=? AND lifecycle='ACTIVE'",
                    (assignment_id,),
                )
                conn.execute(
                    "UPDATE runtime_allocations SET lifecycle='QUARANTINED',release_reason='lease_expired',version=version+1 WHERE assignment_id=? AND lifecycle='ACTIVE'",
                    (assignment_id,),
                )
                conn.execute(
                    """INSERT INTO runtime_recovery_work(assignment_id,state,reason,updated_at)
                       VALUES(?,'PENDING','lease expired; process death not proven',?)
                       ON CONFLICT(assignment_id) DO UPDATE SET state='PENDING',reason=excluded.reason,updated_at=excluded.updated_at""",
                    (assignment_id, now),
                )
                self._append_event(
                    conn, stream_type="assignment", stream_id=assignment_id,
                    event_type="AssignmentExpired",
                    payload=self._assignment_event_payload(conn, assignment_id),
                    scope=scope, key=key, now=now,
                )
                self._save_receipt(
                    conn, command_id=key, scope=scope, key=key, fingerprint=fingerprint,
                    result={"assignment_id": assignment_id, "lifecycle": "EXPIRED"}, now=now,
                )
                expired.append(assignment_id)
            if command_id is not None:
                self._save_receipt(
                    conn,
                    command_id=command_prefix,
                    scope=batch_scope,
                    key=command_prefix,
                    fingerprint=batch_fingerprint,
                    result={"assignment_ids": expired},
                    now=now,
                )
            return tuple(expired)

    def recover_assignment(
        self,
        assignment_id: str,
        *,
        old_process_stopped: bool,
        side_effect_fence_verified: bool,
        command_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CommandReceipt:
        assignment_id = _text("assignment_id", assignment_id)
        if not old_process_stopped or not side_effect_fence_verified:
            raise RecoveryBlocked("old process stop and side-effect fence evidence are required")
        command, key = self._command_args(command_id, idempotency_key, f"recover:{assignment_id}")
        scope = f"assignment:{assignment_id}"
        semantic = {"operation": "recover_assignment", "assignment_id": assignment_id, "old_process_stopped": True, "side_effect_fence_verified": True}
        fingerprint = _hash(semantic)
        with self._transaction() as (conn, now, handoff_token):
            existing = self._existing_command(conn, scope=scope, key=key, fingerprint=fingerprint, now=now)
            if existing:
                return existing
            row = conn.execute("SELECT * FROM runtime_assignments WHERE assignment_id=?", (assignment_id,)).fetchone()
            if row is None:
                raise RuntimeNotFound(f"unknown assignment: {assignment_id}")
            if row["lifecycle"] not in {"EXPIRED", "ORPHANED"}:
                raise InvalidTransition("only expired or orphaned assignments can be recovered")
            attempt = conn.execute("SELECT * FROM runtime_attempts WHERE attempt_id=?", (row["attempt_id"],)).fetchone()
            if attempt is None or attempt["lifecycle"] == "TERMINAL":
                raise InvalidTransition("terminal attempt cannot be recovered")
            conn.execute(
                "UPDATE runtime_assignments SET lifecycle='RECOVERED',version=version+1,released_at=? WHERE assignment_id=?",
                (now, assignment_id),
            )
            conn.execute(
                "UPDATE runtime_allocations SET lifecycle='RELEASED',release_reason='controlled_recovery',version=version+1 WHERE assignment_id=? AND lifecycle='QUARANTINED'",
                (assignment_id,),
            )
            conn.execute(
                "UPDATE runtime_attempts SET lifecycle='PENDING',version=version+1 WHERE attempt_id=?",
                (row["attempt_id"],),
            )
            conn.execute(
                "UPDATE runtime_recovery_work SET state='DONE',attempts=attempts+1,updated_at=? WHERE assignment_id=?",
                (now, assignment_id),
            )
            self._append_event(
                conn, stream_type="assignment", stream_id=assignment_id,
                event_type="AssignmentRecovered",
                payload=self._assignment_event_payload(conn, assignment_id),
                scope=scope, key=key, now=now,
            )
            return self._save_receipt(
                conn, command_id=command, scope=scope, key=key, fingerprint=fingerprint,
                result={"assignment_id": assignment_id, "attempt_id": row["attempt_id"], "lifecycle": "RECOVERED"}, now=now
            )

    def record_attempt_evidence(
        self,
        assignment_id: str,
        worker_id: str,
        incarnation_id: str,
        attempt_id: str,
        resource_key: str,
        resource_epoch: int,
        evidence: Mapping[str, Any],
        *,
        evidence_id: str | None = None,
        command_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CommandReceipt:
        evidence = _mapping(evidence)
        encoded = _canonical(evidence)
        if len(encoded.encode("utf-8")) > self.max_evidence_payload_bytes:
            raise PayloadSizeExceeded("attempt evidence exceeds configured byte limit")
        scope = f"attempt:{attempt_id}"
        request_semantic = {
            "operation": "record_attempt_evidence",
            "assignment_id": assignment_id, "worker_id": worker_id, "incarnation_id": incarnation_id,
            "attempt_id": attempt_id, "resource_key": resource_key, "resource_epoch": resource_epoch,
            "evidence": evidence,
        }
        if evidence_id is not None:
            evidence_id = _text("evidence_id", evidence_id)
            default_key = f"evidence:{evidence_id}"
        else:
            default_key = f"evidence:auto:{_hash(request_semantic)}"
        command, key = self._command_args(command_id, idempotency_key, default_key)
        with self._transaction(safety_key=assignment_id) as (conn, now, handoff_token):
            stored = conn.execute(
                "SELECT * FROM runtime_command_receipts WHERE idempotency_scope=? AND idempotency_key=?",
                (scope, key),
            ).fetchone()
            if evidence_id is None and stored is not None:
                stored_result = json.loads(stored["result_json"])
                evidence_id = _text("evidence_id", stored_result.get("evidence_id"))
            if evidence_id is None:
                evidence_id = _new_id("evidence")
            semantic = {**request_semantic, "evidence_id": evidence_id}
            fingerprint = _hash(semantic)
            existing = self._existing_command(conn, scope=scope, key=key, fingerprint=fingerprint, now=now)
            if existing:
                return existing
            self._owner_rows(
                conn,
                assignment_id,
                worker_id,
                incarnation_id,
                attempt_id,
                resource_key,
                resource_epoch,
                now,
                handoff_token=handoff_token,
            )
            if conn.execute("SELECT 1 FROM runtime_evidence WHERE evidence_id=?", (evidence_id,)).fetchone() is not None:
                raise RuntimeConflict(f"evidence already exists: {evidence_id}")
            conn.execute(
                "INSERT INTO runtime_evidence(evidence_id,assignment_id,attempt_id,payload_json,recorded_at) VALUES(?,?,?,?,?)",
                (evidence_id, assignment_id, attempt_id, encoded, now),
            )
            self._append_event(
                conn, stream_type="attempt", stream_id=attempt_id,
                event_type="AttemptEvidenceRecorded",
                payload={"evidence_id": evidence_id, "assignment_id": assignment_id, "attempt_id": attempt_id},
                scope=scope, key=key, now=now,
            )
            return self._save_receipt(
                conn, command_id=command, scope=scope, key=key, fingerprint=fingerprint,
                result={"evidence_id": evidence_id, "assignment_id": assignment_id, "attempt_id": attempt_id}, now=now
            )

    def mutate_protected_resource(
        self,
        assignment_id: str,
        worker_id: str,
        incarnation_id: str,
        attempt_id: str,
        resource_key: str,
        resource_epoch: int,
        expected_version: int,
        value: Mapping[str, Any],
        *,
        command_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CommandReceipt:
        value = _mapping(value)
        command, key = self._command_args(command_id, idempotency_key, f"resource-mutation:{assignment_id}:{expected_version}")
        scope = f"resource:{resource_key}"
        semantic = {
            "operation": "mutate_protected_resource", "assignment_id": assignment_id,
            "worker_id": worker_id, "incarnation_id": incarnation_id, "attempt_id": attempt_id,
            "resource_key": resource_key, "resource_epoch": resource_epoch,
            "expected_version": expected_version, "value": value,
        }
        fingerprint = _hash(semantic)
        with self._transaction(safety_key=assignment_id) as (conn, now, handoff_token):
            existing = self._existing_command(conn, scope=scope, key=key, fingerprint=fingerprint, now=now)
            if existing:
                return existing
            self._owner_rows(
                conn,
                assignment_id,
                worker_id,
                incarnation_id,
                attempt_id,
                resource_key,
                resource_epoch,
                now,
                handoff_token=handoff_token,
            )
            updated = conn.execute(
                """UPDATE runtime_protected_resources SET value_json=?,version=version+1
                   WHERE resource_key=? AND fencing_epoch=? AND version=?""",
                (_canonical(value), resource_key, resource_epoch, expected_version),
            ).rowcount
            if updated != 1:
                raise RuntimeVersionConflict("protected resource version or fence is stale")
            self._append_event(
                conn, stream_type="resource", stream_id=resource_key,
                event_type="ProtectedResourceMutated",
                payload={"resource_key": resource_key, "resource_epoch": resource_epoch, "expected_version": expected_version},
                scope=scope, key=key, now=now,
            )
            return self._save_receipt(
                conn, command_id=command, scope=scope, key=key, fingerprint=fingerprint,
                result={"resource_key": resource_key, "resource_epoch": resource_epoch, "version": expected_version + 1}, now=now
            )

    def get_worker(self, worker_id: str) -> WorkerRecord:
        with self._read_connection() as conn:
            row = conn.execute("SELECT * FROM runtime_workers WHERE worker_id=?", (worker_id,)).fetchone()
            if row is None:
                raise RuntimeNotFound(f"unknown worker: {worker_id}")
            return self._worker_from(row)

    def get_incarnation(self, incarnation_id: str) -> IncarnationRecord:
        with self._read_connection() as conn:
            row = conn.execute("SELECT * FROM runtime_worker_incarnations WHERE incarnation_id=?", (incarnation_id,)).fetchone()
            if row is None:
                raise RuntimeNotFound(f"unknown incarnation: {incarnation_id}")
            return self._incarnation_from(row)

    def get_execution(self, execution_id: str) -> ExecutionRecord:
        with self._read_connection() as conn:
            row = conn.execute("SELECT * FROM runtime_executions WHERE execution_id=?", (execution_id,)).fetchone()
            if row is None:
                raise RuntimeNotFound(f"unknown execution: {execution_id}")
            return self._execution_from(row)

    def get_attempt(self, attempt_id: str) -> AttemptRecord:
        with self._read_connection() as conn:
            row = conn.execute("SELECT * FROM runtime_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None:
                raise RuntimeNotFound(f"unknown attempt: {attempt_id}")
            return self._attempt_from(row)

    def get_assignment(self, assignment_id: str) -> AssignmentRecord:
        with self._read_connection() as conn:
            row = conn.execute(
                """SELECT a.*, al.allocation_id FROM runtime_assignments a
                   LEFT JOIN runtime_allocations al ON al.assignment_id=a.assignment_id
                   WHERE a.assignment_id=?""",
                (assignment_id,),
            ).fetchone()
            if row is None:
                raise RuntimeNotFound(f"unknown assignment: {assignment_id}")
            return self._assignment_from(row)

    def get_allocation(self, allocation_id: str) -> AllocationRecord:
        with self._read_connection() as conn:
            row = conn.execute("SELECT * FROM runtime_allocations WHERE allocation_id=?", (allocation_id,)).fetchone()
            if row is None:
                raise RuntimeNotFound(f"unknown allocation: {allocation_id}")
            return self._allocation_from(row)

    def get_recovery_status(self, assignment_id: str) -> RecoveryRecord | None:
        with self._read_connection() as conn:
            row = conn.execute("SELECT * FROM runtime_recovery_work WHERE assignment_id=?", (assignment_id,)).fetchone()
            if row is None:
                return None
            return RecoveryRecord(
                assignment_id=row["assignment_id"], state=row["state"], reason=row["reason"],
                attempts=int(row["attempts"]), updated_at=row["updated_at"],
            )

    def get_protected_resource(self, resource_key: str) -> ProtectedResourceRecord:
        with self._read_connection() as conn:
            row = conn.execute("SELECT * FROM runtime_protected_resources WHERE resource_key=?", (resource_key,)).fetchone()
            if row is None:
                raise RuntimeNotFound(f"unknown protected resource: {resource_key}")
            return ProtectedResourceRecord(
                resource_key=row["resource_key"], fencing_epoch=int(row["fencing_epoch"]),
                value=json.loads(row["value_json"]) if row["value_json"] is not None else None,
                version=int(row["version"]),
            )

    @contextlib.contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        self._require_schema_path()
        conn = self._connect(read_only=True)
        try:
            self._require_schema(conn)
            yield conn
        finally:
            conn.close()

    @classmethod
    def _encode_event_cursor(cls, global_position: int) -> str:
        raw = f"runtime-events:v{cls.CURSOR_VERSION}:{global_position}".encode("ascii")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def _decode_event_cursor(cls, cursor: str | None) -> int:
        if cursor is None:
            return 0
        cursor = _text("cursor", cursor)
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            raw = base64.b64decode(padded, altchars=b"-_", validate=True).decode("ascii")
            prefix, version, position = raw.split(":", 2)
            if prefix != "runtime-events" or version != f"v{cls.CURSOR_VERSION}":
                raise ValueError
            decoded = int(position)
            if decoded < 0 or cls._encode_event_cursor(decoded) != cursor:
                raise ValueError
            return decoded
        except (UnicodeError, ValueError) as exc:
            raise RuntimeConflict("invalid runtime event cursor") from exc

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> RuntimeEventRecord:
        return RuntimeEventRecord(
            event_id=row["event_id"],
            stream_type=row["stream_type"],
            stream_id=row["stream_id"],
            sequence=int(row["sequence"]),
            event_family=row["event_family"],
            event_type=row["event_type"],
            schema_version=int(row["schema_version"]),
            occurred_at=row["occurred_at"],
            recorded_at=row["recorded_at"],
            payload=json.loads(row["payload_json"]),
            payload_hash=row["payload_hash"],
            global_position=int(row["global_position"]),
        )

    def _read_event_rows(
        self,
        conn: sqlite3.Connection,
        *,
        where_sql: str,
        args: tuple[Any, ...],
        order_sql: str,
        limit: int,
        max_payload_bytes: int,
    ) -> tuple[RuntimeEventRecord, ...]:
        metadata_cursor = conn.execute(
            f"""SELECT p.global_position,e.event_id,e.stream_type,e.stream_id,e.sequence,
                       e.event_family,e.event_type,e.schema_version,e.occurred_at,e.recorded_at,
                       e.payload_hash,LENGTH(CAST(e.payload_json AS BLOB)) AS payload_bytes
                  FROM runtime_event_positions p
                  JOIN runtime_events e ON e.event_id=p.event_id
                 WHERE {where_sql} ORDER BY {order_sql} LIMIT ?""",
            (*args, limit),
        )
        metadata: list[sqlite3.Row] = []
        total = 0
        try:
            for row in metadata_cursor:
                size = int(row["payload_bytes"])
                if size > max_payload_bytes:
                    raise PayloadSizeExceeded(
                        f"runtime event {row['event_id']} exceeds read budget"
                    )
                total += size
                if total > max_payload_bytes:
                    raise PayloadSizeExceeded("runtime event page exceeds read budget")
                metadata.append(row)
        finally:
            metadata_cursor.close()
        if not metadata:
            return ()
        positions = tuple(int(row["global_position"]) for row in metadata)
        placeholders = ",".join("?" for _ in positions)
        body_cursor = conn.execute(
            f"""SELECT p.global_position,e.* FROM runtime_event_positions p
                  JOIN runtime_events e ON e.event_id=p.event_id
                 WHERE p.global_position IN ({placeholders}) ORDER BY {order_sql}""",
            positions,
        )
        try:
            return tuple(self._event_from_row(row) for row in body_cursor)
        finally:
            body_cursor.close()

    def read_events(
        self,
        *,
        stream_type: str | None = None,
        stream_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
        max_payload_bytes: int = 2_000_000,
    ) -> tuple[RuntimeEventRecord, ...]:
        """Read one aggregate stream by sequence, or the first global page.

        Cross-stream continuation must use :meth:`scan_events`; a stream sequence
        is never accepted as a global storage checkpoint.
        """
        if limit < 1 or max_payload_bytes < 1:
            raise ValueError("limit and max_payload_bytes must be positive")
        if (stream_type is None) != (stream_id is None):
            raise ValueError("stream_type and stream_id must be provided together")
        if stream_type is None:
            if after_sequence != 0:
                raise ValueError("cross-stream continuation requires scan_events cursor")
            return self.scan_events(
                limit=limit,
                max_payload_bytes=max_payload_bytes,
            ).events
        with self._read_connection() as conn:
            return self._read_event_rows(
                conn,
                where_sql="e.stream_type=? AND e.stream_id=? AND e.sequence>?",
                args=(stream_type, stream_id, after_sequence),
                order_sql="e.sequence",
                limit=limit,
                max_payload_bytes=max_payload_bytes,
            )

    def scan_events(
        self,
        *,
        cursor: str | None = None,
        limit: int = 100,
        max_payload_bytes: int = 2_000_000,
    ) -> RuntimeEventPage:
        """Read a lossless bounded page across streams by opaque storage cursor."""
        if limit < 1 or max_payload_bytes < 1:
            raise ValueError("limit and max_payload_bytes must be positive")
        after_position = self._decode_event_cursor(cursor)
        with self._read_connection() as conn:
            events = self._read_event_rows(
                conn,
                where_sql="p.global_position>?",
                args=(after_position,),
                order_sql="p.global_position",
                limit=limit,
                max_payload_bytes=max_payload_bytes,
            )
        checkpoint = events[-1].global_position if events else after_position
        assert checkpoint is not None
        return RuntimeEventPage(
            events=events,
            next_cursor=self._encode_event_cursor(checkpoint),
        )

    def count_events(self) -> int:
        with self._read_connection() as conn:
            return int(conn.execute("SELECT COUNT(*) AS count FROM runtime_events").fetchone()["count"])

    def list_outbox(self, states: tuple[str, ...] = ("PENDING", "RETRY")) -> tuple[dict[str, Any], ...]:
        with self._read_connection() as conn:
            placeholders = ",".join("?" for _ in states)
            rows = conn.execute(
                f"SELECT * FROM runtime_outbox WHERE state IN ({placeholders}) ORDER BY rowid",
                states,
            ).fetchall()
            return tuple(dict(row) for row in rows)

    def replay_events(self, events: tuple[RuntimeEventRecord, ...] | list[RuntimeEventRecord]) -> dict[str, Any]:
        if events and events[0].stream_type == "worker":
            worker_id = events[0].stream_id
            incarnation_ids = {
                event.payload.to_dict().get("incarnation_id")
                for event in events
                if isinstance(event.payload.to_dict().get("incarnation_id"), str)
            }
            related: list[RuntimeEventRecord] = []
            if incarnation_ids:
                placeholders = ",".join("?" for _ in incarnation_ids)
                with self._read_connection() as conn:
                    rows = conn.execute(
                        f"""SELECT p.global_position,e.*
                              FROM runtime_event_positions p
                              JOIN runtime_events e ON e.event_id=p.event_id
                             WHERE e.stream_type='incarnation'
                               AND e.stream_id IN ({placeholders})
                             ORDER BY p.global_position""",
                        tuple(sorted(incarnation_ids)),
                    ).fetchall()
                    related = [self._event_from_row(row) for row in rows]
            return replay_worker_events(events, tuple(related))
        return replay_runtime_events(events)
