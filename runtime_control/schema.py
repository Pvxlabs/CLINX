"""Versioned additive SQLite schema for runtime control."""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


LATEST_SCHEMA_VERSION = 3

MIGRATIONS = (
    Migration(
        1,
        "runtime_worker_ownership_foundation",
        (
            """CREATE TABLE IF NOT EXISTS runtime_tasks (
                task_id TEXT PRIMARY KEY,
                source_system TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS runtime_executions (
                execution_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES runtime_tasks(task_id),
                request_json TEXT NOT NULL,
                policy_json TEXT NOT NULL,
                route_json TEXT NOT NULL,
                lifecycle TEXT NOT NULL CHECK(lifecycle IN ('REQUESTED','CANCELLED','TERMINAL')),
                version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS runtime_attempts (
                attempt_id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL REFERENCES runtime_executions(execution_id),
                retry_index INTEGER NOT NULL CHECK(retry_index >= 0),
                provider_session_id TEXT,
                lifecycle TEXT NOT NULL CHECK(lifecycle IN ('PENDING','ASSIGNED','RELEASED','TERMINAL')),
                version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
                created_at TEXT NOT NULL,
                UNIQUE(execution_id, retry_index)
            )""",
            """CREATE TABLE IF NOT EXISTS runtime_workers (
                worker_id TEXT PRIMARY KEY,
                worker_kind TEXT NOT NULL,
                host_reference TEXT NOT NULL,
                capabilities_json TEXT NOT NULL,
                capacity INTEGER NOT NULL CHECK(capacity > 0),
                lifecycle TEXT NOT NULL CHECK(lifecycle IN ('REGISTERED','DRAINING','RETIRED')),
                version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
                current_incarnation_id TEXT,
                last_heartbeat_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS runtime_worker_incarnations (
                incarnation_id TEXT PRIMARY KEY,
                worker_id TEXT NOT NULL REFERENCES runtime_workers(worker_id),
                generation INTEGER NOT NULL CHECK(generation > 0),
                lifecycle TEXT NOT NULL CHECK(lifecycle IN ('ACTIVE','SUPERSEDED','REVOKED')),
                version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
                started_at TEXT NOT NULL,
                last_heartbeat_at TEXT,
                UNIQUE(worker_id, generation)
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS runtime_one_active_incarnation
                ON runtime_worker_incarnations(worker_id) WHERE lifecycle='ACTIVE'""",
            """CREATE TABLE IF NOT EXISTS runtime_resource_counters (
                resource_key TEXT PRIMARY KEY,
                current_epoch INTEGER NOT NULL DEFAULT 0 CHECK(current_epoch >= 0)
            )""",
            """CREATE TABLE IF NOT EXISTS runtime_assignments (
                assignment_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL REFERENCES runtime_attempts(attempt_id),
                worker_id TEXT NOT NULL REFERENCES runtime_workers(worker_id),
                incarnation_id TEXT NOT NULL REFERENCES runtime_worker_incarnations(incarnation_id),
                resource_key TEXT NOT NULL,
                resource_epoch INTEGER NOT NULL CHECK(resource_epoch > 0),
                lifecycle TEXT NOT NULL CHECK(lifecycle IN ('ACTIVE','EXPIRED','ORPHANED','REVOKED','RECOVERED','RELEASED')),
                version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
                lease_expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                released_at TEXT
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS runtime_one_current_assignment
                ON runtime_assignments(attempt_id) WHERE lifecycle='ACTIVE'""",
            """CREATE TABLE IF NOT EXISTS runtime_allocations (
                allocation_id TEXT PRIMARY KEY,
                assignment_id TEXT NOT NULL REFERENCES runtime_assignments(assignment_id),
                attempt_id TEXT NOT NULL REFERENCES runtime_attempts(attempt_id),
                worker_id TEXT NOT NULL REFERENCES runtime_workers(worker_id),
                incarnation_id TEXT NOT NULL REFERENCES runtime_worker_incarnations(incarnation_id),
                resource_key TEXT NOT NULL,
                resource_epoch INTEGER NOT NULL CHECK(resource_epoch > 0),
                lifecycle TEXT NOT NULL CHECK(lifecycle IN ('ACTIVE','QUARANTINED','RELEASED')),
                version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
                expires_at TEXT NOT NULL,
                release_reason TEXT,
                UNIQUE(resource_key, resource_epoch)
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS runtime_one_held_allocation
                ON runtime_allocations(resource_key)
                WHERE lifecycle IN ('ACTIVE','QUARANTINED')""",
            """CREATE TABLE IF NOT EXISTS runtime_recovery_work (
                assignment_id TEXT PRIMARY KEY REFERENCES runtime_assignments(assignment_id),
                state TEXT NOT NULL CHECK(state IN ('PENDING','BLOCKED','DONE')),
                reason TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS runtime_evidence (
                evidence_id TEXT PRIMARY KEY,
                assignment_id TEXT NOT NULL REFERENCES runtime_assignments(assignment_id),
                attempt_id TEXT NOT NULL REFERENCES runtime_attempts(attempt_id),
                payload_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS runtime_protected_resources (
                resource_key TEXT PRIMARY KEY,
                fencing_epoch INTEGER NOT NULL CHECK(fencing_epoch >= 0),
                value_json TEXT,
                version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0)
            )""",
            """CREATE TABLE IF NOT EXISTS runtime_stream_versions (
                stream_type TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                current_sequence INTEGER NOT NULL DEFAULT 0 CHECK(current_sequence >= 0),
                PRIMARY KEY(stream_type, stream_id)
            )""",
            """CREATE TABLE IF NOT EXISTS runtime_events (
                event_id TEXT PRIMARY KEY,
                stream_type TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK(sequence > 0),
                event_family TEXT NOT NULL CHECK(event_family='RUNTIME_WORKER_V1'),
                event_type TEXT NOT NULL,
                schema_version INTEGER NOT NULL CHECK(schema_version = 1),
                occurred_at TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                idempotency_scope TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                UNIQUE(stream_type, stream_id, sequence),
                UNIQUE(idempotency_scope, idempotency_key)
            )""",
            """CREATE TABLE IF NOT EXISTS runtime_command_receipts (
                command_id TEXT PRIMARY KEY,
                idempotency_scope TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                semantic_fingerprint TEXT NOT NULL,
                result_json TEXT NOT NULL,
                committed_at TEXT NOT NULL,
                UNIQUE(idempotency_scope, idempotency_key)
            )""",
            """CREATE TABLE IF NOT EXISTS runtime_outbox (
                outbox_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL REFERENCES runtime_events(event_id),
                destination TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('PENDING','RETRY','APPLIED','FAILED')),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                last_error TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS runtime_clock_state (
                state_id INTEGER PRIMARY KEY CHECK(state_id=1),
                last_coordinator_time TEXT NOT NULL
            )""",
            """CREATE TRIGGER IF NOT EXISTS runtime_events_no_update
                BEFORE UPDATE ON runtime_events BEGIN
                    SELECT RAISE(ABORT, 'runtime event history is append-only');
                END""",
            """CREATE TRIGGER IF NOT EXISTS runtime_events_no_delete
                BEFORE DELETE ON runtime_events BEGIN
                    SELECT RAISE(ABORT, 'runtime event history is append-only');
                END""",
        ),
    ),
    Migration(
        2,
        "runtime_contract_remediation",
        (
            """CREATE TABLE runtime_command_receipts_v2 (
                receipt_id TEXT PRIMARY KEY,
                command_id TEXT NOT NULL,
                idempotency_scope TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                semantic_fingerprint TEXT NOT NULL,
                result_json TEXT NOT NULL,
                committed_at TEXT NOT NULL,
                UNIQUE(idempotency_scope, idempotency_key)
            )""",
            """INSERT INTO runtime_command_receipts_v2(
                receipt_id,command_id,idempotency_scope,idempotency_key,
                semantic_fingerprint,result_json,committed_at
            ) SELECT command_id,command_id,idempotency_scope,idempotency_key,
                     semantic_fingerprint,result_json,committed_at
                FROM runtime_command_receipts""",
            "DROP TABLE runtime_command_receipts",
            "ALTER TABLE runtime_command_receipts_v2 RENAME TO runtime_command_receipts",
            """CREATE TABLE runtime_event_positions (
                global_position INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE REFERENCES runtime_events(event_id)
            )""",
            """INSERT INTO runtime_event_positions(event_id)
                SELECT event_id FROM runtime_events ORDER BY rowid""",
            """CREATE TRIGGER runtime_events_assign_position
                AFTER INSERT ON runtime_events BEGIN
                    INSERT INTO runtime_event_positions(event_id) VALUES(NEW.event_id);
                END""",
            """CREATE TRIGGER runtime_event_positions_no_update
                BEFORE UPDATE ON runtime_event_positions BEGIN
                    SELECT RAISE(ABORT, 'runtime event positions are append-only');
                END""",
            """CREATE TRIGGER runtime_event_positions_no_delete
                BEFORE DELETE ON runtime_event_positions BEGIN
                    SELECT RAISE(ABORT, 'runtime event positions are append-only');
                END""",
        ),
    ),
    Migration(
        3,
        "runtime_safety_handoff_guard",
        (
            """CREATE TABLE runtime_safety_handoffs (
                handoff_key TEXT PRIMARY KEY,
                handoff_token TEXT NOT NULL UNIQUE,
                observed_at TEXT NOT NULL,
                blocked_until TEXT NOT NULL,
                created_at TEXT NOT NULL,
                CHECK(length(handoff_key) > 0),
                CHECK(length(handoff_token) > 0)
            )""",
        ),
    ),
)
