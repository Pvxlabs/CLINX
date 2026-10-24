"""Versioned, additive SQLite migrations for the shadow ledger."""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS = (
    Migration(
        version=1,
        name="shadow_event_ledger_foundation",
        statements=(
            """CREATE TABLE IF NOT EXISTS v2_legacy_identity_map (
                   source_system TEXT NOT NULL,
                   source_type TEXT NOT NULL,
                   source_identity TEXT NOT NULL,
                   target_type TEXT NOT NULL,
                   target_id TEXT NOT NULL UNIQUE,
                   attribution_state TEXT NOT NULL
                       CHECK(attribution_state IN ('EXACT','UNATTRIBUTED')),
                   created_at TEXT NOT NULL,
                   PRIMARY KEY(source_system,source_type,source_identity)
               )""",
            """CREATE TABLE IF NOT EXISTS v2_aggregate_versions (
                   aggregate_type TEXT NOT NULL,
                   aggregate_id TEXT NOT NULL,
                   current_version INTEGER NOT NULL CHECK(current_version >= 0),
                   updated_at TEXT NOT NULL,
                   PRIMARY KEY(aggregate_type,aggregate_id)
               )""",
            """CREATE TABLE IF NOT EXISTS v2_events (
                   cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                   event_id TEXT NOT NULL UNIQUE,
                   aggregate_type TEXT NOT NULL,
                   aggregate_id TEXT NOT NULL,
                   aggregate_version INTEGER NOT NULL CHECK(aggregate_version > 0),
                   event_type TEXT NOT NULL,
                   schema_version INTEGER NOT NULL CHECK(schema_version > 0),
                   occurred_at TEXT NOT NULL,
                   recorded_at TEXT NOT NULL,
                   actor_json TEXT NOT NULL,
                   causation_id TEXT,
                   correlation_id TEXT,
                   idempotency_scope TEXT NOT NULL,
                   idempotency_key TEXT NOT NULL,
                   semantic_fingerprint TEXT NOT NULL,
                   payload_json TEXT NOT NULL,
                   payload_hash TEXT NOT NULL,
                   UNIQUE(aggregate_type,aggregate_id,aggregate_version),
                   UNIQUE(idempotency_scope,idempotency_key)
               )""",
            """CREATE INDEX IF NOT EXISTS v2_events_stream_cursor
                   ON v2_events(aggregate_type,aggregate_id,cursor)""",
            """CREATE TABLE IF NOT EXISTS v2_event_inbox (
                   cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                   receipt_id TEXT NOT NULL UNIQUE,
                   source TEXT NOT NULL,
                   dedup_key TEXT NOT NULL,
                   provider_cursor TEXT,
                   received_at TEXT NOT NULL,
                   payload_json TEXT NOT NULL,
                   payload_hash TEXT NOT NULL,
                   state TEXT NOT NULL
                       CHECK(state IN ('RECEIVED','PROCESSED','QUARANTINED')),
                   normalized_event_id TEXT REFERENCES v2_events(event_id),
                   quarantine_reason TEXT,
                   UNIQUE(source,dedup_key)
               )""",
            """CREATE TABLE IF NOT EXISTS v2_event_outbox (
                   cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                   outbox_id TEXT NOT NULL UNIQUE,
                   event_id TEXT NOT NULL REFERENCES v2_events(event_id),
                   destination TEXT NOT NULL,
                   idempotency_key TEXT NOT NULL,
                   payload_json TEXT NOT NULL,
                   state TEXT NOT NULL
                       CHECK(state IN ('PENDING','RETRY','APPLIED','FAILED')),
                   attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                   last_error TEXT,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   UNIQUE(destination,idempotency_key)
               )""",
            """CREATE INDEX IF NOT EXISTS v2_outbox_pending_cursor
                   ON v2_event_outbox(state,cursor)""",
            """CREATE TABLE IF NOT EXISTS v2_projection_checkpoints (
                   projection_name TEXT PRIMARY KEY,
                   last_cursor INTEGER NOT NULL CHECK(last_cursor >= 0),
                   state TEXT NOT NULL CHECK(state IN ('PENDING','APPLIED','DEGRADED')),
                   updated_at TEXT NOT NULL,
                   last_error TEXT
               )""",
            """CREATE TRIGGER IF NOT EXISTS v2_events_reject_update
                   BEFORE UPDATE ON v2_events
                   BEGIN
                       SELECT RAISE(ABORT, 'v2_events is append-only');
                   END""",
            """CREATE TRIGGER IF NOT EXISTS v2_events_reject_delete
                   BEFORE DELETE ON v2_events
                   BEGIN
                       SELECT RAISE(ABORT, 'v2_events is append-only');
                   END""",
        ),
    ),
)

LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version
