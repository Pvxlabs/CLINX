# ADR-002: CLINX V2 Shadow Event Ledger

- **Status:** Accepted for PVX-1805 implementation
- **Date:** 2026-09-10
- **Authority:** V1 remains authoritative
- **Related:** [ADR-001](./ADR-001-v2-architecture.md)

## 1. Decision

CLINX will add a durable shadow event ledger that records facts already
persisted by selected V1 `TaskRegistry` mutations. The ledger is evidence for
contract validation, replay, and later migration design. It is not an execution
state machine and cannot decide or change V1 outcomes.

The fixed authority boundary is:

```ini
V1_AUTHORITY=ON
V2_EVENT_AUTHORITY=OFF
SHADOW_DEFAULT=OFF
PUBLIC_MCP_CONTRACT=UNCHANGED
```

ADR-001 remains unchanged: the finalizer owns evidence-based terminal
decisions, workers do not gain durable takeover authority in this phase, and
projections are never command authority.

## 2. Event Envelope

PVX-1805 reuses `domain.Event`. A persisted event contains:

| Field | Contract |
|---|---|
| `event_id` | Opaque, unique ledger identity generated once on first append. |
| `aggregate_type` | Stream kind, currently `task` or `execution`. |
| `aggregate_id` | Stable mapped V2 shadow identity. |
| `aggregate_version` | Per-stream sequence starting at one. |
| `event_type` | Past-tense V1 observed fact, prefixed `V1`. |
| `schema_version` | Positive event payload schema version. PVX-1805 supports version 1. |
| `occurred_at` | Best available source-fact time. It is not stream order. |
| `recorded_at` | Ledger insertion time. It is not stream order. |
| `actor` | Structured `actor_type` and `actor_id`; shadow hooks use the V1 registry boundary. |
| `causation_id` | Optional fact that caused this observation. |
| `correlation_id` | Optional request or exact V1 execution correlation. |
| `idempotency_scope` | Namespace in which the key is unique. |
| `idempotency_key` | Stable exact-retry key within its scope. |
| `payload` | Canonical immutable JSON object. |
| `payload_hash` | SHA-256 of canonical payload JSON. |

Raw provider protocol identifiers remain in restricted V1 evidence or adapter
boundaries. Public projections are unchanged. A provider reference is included
in a shadow payload only when V1 has already persisted that exact correlation
and the ledger is read directly through its restricted local interface.

## 3. Stream Identity and Ordering

Each `(aggregate_type, aggregate_id)` pair is one stream. Its
`aggregate_version` is the only domain ordering contract. A unique database
constraint protects `(aggregate_type, aggregate_id, aggregate_version)`.

The integer `cursor` on `v2_events` is only a database pagination cursor. It is
useful for bounded scans and projection checkpoints but does not establish a
global business or causal order across streams.

Neither `occurred_at`, `recorded_at`, V1 `updated_at`, nor the database cursor
may be used to infer cross-stream causality. PVX-1805 does not claim a global
business total order.

Append requires `expected_version`. Inside the caller's transaction, the store
compares and advances `v2_aggregate_versions` using a SQL compare-and-swap.
It never computes `MAX(version) + 1` outside the transaction and never relies
on a Python process lock.

## 4. Identity Mapping

`v2_legacy_identity_map` persists a stable mapping from:

```text
(source_system, source_type, source_identity)
    -> (target_type, target_id, attribution_state)
```

Mapped identifiers are deterministic hashes of the source tuple, then checked
against the durable mapping. A retry resolves to the same target identity.
Any conflicting durable mapping fails closed.

An exact non-empty V1 `execution_ref` may map to an `execution` stream. The
source task identity is retained in event payloads and maps separately to a
`task` identity.

Legacy `NULL execution_ref` rows do not receive invented execution, attempt, or
provider-session identities. They may emit a task-stream observation with
`attribution=UNATTRIBUTED` and the exact known V1 task source identity.

The recorder never resolves an execution by looking up the task's latest
result. Result observations require their exact persisted `execution_ref`.

## 5. Idempotency

`(idempotency_scope, idempotency_key)` is unique. The semantic fingerprint
includes:

- aggregate type and ID;
- event type and schema version;
- actor;
- causation and correlation IDs;
- canonical payload;
- requested outbox destination, idempotency identity, and canonical payload.

The fingerprint excludes service-generated `event_id`, assigned
`aggregate_version`, `recorded_at`, `payload_hash`, database cursor, outbox ID,
and outbox timestamps. `occurred_at` is also excluded because a caller may omit
it and the recorder then generates it. Source chronology that affects meaning
must be carried explicitly in the canonical payload.

Append checks scoped idempotency before `expected_version`. Therefore, after a
successful commit followed by a lost response, an exact retry returns the
original event even though its original `expected_version` is now stale.

- Same scope/key and same fingerprint returns the original event and outbox.
- Same scope/key and different semantics raises `IdempotencyConflict`.
- A new key with stale `expected_version` raises `AggregateVersionConflict`.
- SQLite lock contention raises a distinct `LedgerBusy` error.
- Invalid envelope or schema raises `LedgerValidationError`.

PVX-1805 performs no automatic write retries. Callers may make bounded retries
only when their idempotency key and semantic request remain unchanged.

## 6. Storage and Migrations

The implementation uses Python standard-library `sqlite3` and adds only
isolated `v2_` tables:

```text
v2_schema_migrations
v2_legacy_identity_map
v2_aggregate_versions
v2_events
v2_event_inbox
v2_event_outbox
v2_projection_checkpoints
```

Migrations are explicit, ordered, versioned, and idempotent. They run only when
shadow mode is explicitly enabled or the standalone ledger is explicitly
initialized. Disabling shadow mode is non-destructive: existing history is
retained and V1 continues without new shadow writes.

No V1 table, column, index, check constraint, execution singleton, or lease
contract is deleted or rebuilt.

Database triggers reject normal `UPDATE` and `DELETE` operations on
`v2_events`. This is an application/database-schema protection boundary, not a
claim that a database administrator cannot remove the triggers or rewrite the
file.

## 7. Transaction Ownership

`EventStore` exposes a narrow unit-of-work API:

- A store-owned unit opens one connection, starts `BEGIN IMMEDIATE`, commits
  on success, and rolls back on failure.
- A caller-owned unit passes an already active connection. The store neither
  begins, commits, nor rolls back that outer transaction.
- Calling a transaction-participating append without an active transaction is
  rejected.
- Schema migration owns its separate setup transaction and is never invoked
  inside a V1 mutation transaction.

For an instrumented V1 mutation, `TaskRegistry` owns one connection and one
`BEGIN IMMEDIATE` transaction containing:

```text
V1 mutation
    +
shadow event append
    +
aggregate-version CAS
    +
shadow outbox insert
```

Any shadow failure is visible and rolls back that local V1 write unit. The
store does not swallow the error and does not commit the outer transaction.

Provider calls, Linear calls, and host command execution remain outside these
database transactions. This local atomicity does not remove every crash window
across V1 finalization, which currently spans several registry calls.

## 8. V1 Observation Hooks

Only facts already written by V1 are observed:

| Source mutation | Observed event | Stream | Same transaction | Replay fields |
|---|---|---|---|---|
| Task/execution claim and worktree lease insert | `V1ExecutionClaimObserved` or `V1UnattributedExecutionClaimObserved` | Exact execution, otherwise task | Yes | task/execution identity, lifecycle, stage, lease |
| Changed provider progress/correlation persisted by `set_execution_state` | `V1ExecutionProgressObserved` | Exact execution, otherwise task | Yes | lifecycle, stage, turn correlation, attribution |
| Host evidence row inserted | `V1HostExecutionStartedObserved` | Exact execution | Yes | host evidence reference and running state |
| Host evidence row completed | `V1HostEvidenceObserved` | Exact execution | Yes | evidence reference, result state, evidence hashes |
| Exact execution result inserted | `V1ExecutionResultPersistedObserved` | Exact execution | Yes | exact result reference and status |
| Terminal state persisted | `V1TerminalStateObserved` | Exact execution, otherwise task | Yes | lifecycle and terminal evidence fields |
| Worktree lease deleted | `V1LeaseReleasedObserved` or task-level unattributed equivalent | Exact execution, otherwise task | Yes | lease state and resource key |

No-op state writes emit no event. Repeated status reads and reconciliations that
make no V1 state change emit no progress event. Events use `Observed`, not V2
command names such as `ExecutionFinalized`, because the recorder has no
terminal authority.

Existing history may be imported only through
`V1SnapshotBaselineImported`. Its payload declares the source snapshot time,
known fields, attribution, and `history_before_baseline=UNKNOWN`. It does not
claim to reconstruct preceding transitions.

Replay comparison is fixed before execution to these fields:

```text
aggregate_type
task_id
execution_id
source_task_id
source_execution_ref
execution_state
current_stage
exact_result_ref
result_status
turn_id
lease_state
resource_key
stream_version
attribution
```

The following are explicitly excluded from PVX-1805 equivalence: timestamps,
database cursor, route/policy payloads, raw provider identifiers, raw host
output, Linear projection state, and history before the declared baseline.
Exclusion means `NOT_COVERED`, not inferred equality.

## 9. Inbox and Outbox

Inbox deduplication uses `(source, dedup_key)`, preferring a provider-native
event ID or cursor. Payload hashes detect conflicting reuse of that identity;
payload hash alone never deduplicates receipts because identical payloads may
represent separate valid observations.

A normalized event, its outbox row, and the inbox `PROCESSED` update occur in
one transaction. Inputs lacking reliable execution/attempt correlation remain
visible as `QUARANTINED`; the normalizer does not guess an attempt.

Outbox rows include destination, destination-scoped idempotency identity,
canonical payload, status (`PENDING`, `RETRY`, `APPLIED`, or `FAILED`), attempt
count, and last error. A local consumer may record failure and retry. PVX-1805
does not start a daemon, switch production Linear, or promise exactly-once
external side effects.

## 10. Replay and Comparison

The offline reducer consumes bounded pages of events and an explicit baseline.
It depends only on event data and versioned reducers. It does not query a
provider, use current time or randomness, read current V1 results to fill gaps,
write V1, release a lease, invoke MCP/Linear, or trigger commands.

The fixed comparison fields are:

- `task_id` and exact mapped `execution_id` where attributable;
- supported V1 execution lifecycle state;
- current stage;
- exact execution result reference and status;
- known persisted attempt/turn correlation;
- lease state and resource key;
- final stream version;
- attribution and history-completeness markers.

The typed comparison contract is the following exact ordered set. The order is
part of the deterministic comparison output, while the values are the
field-level V1 evidence selected by the reducer:

```text
aggregate_type
task_id
execution_id
source_task_id
source_execution_ref
execution_state
current_stage
exact_result_ref
result_status
turn_id
lease_state
resource_key
stream_version
attribution
```

`result_status` and `resource_key` are not derived from a projection or from
the result reference alone. A PASS result and a BLOCKED result are different
observations, as are two lease states with different resource identities. The
reducer carries both values into `ReplayResult` and the summary hash, and
`ReplayService.compare()` reports a difference when either changes.

Expected comparison input is an explicit schema: every field in the set above
must be present. Missing expected fields fail with `ReplayError`; they are not
treated as an implicit `null` or as equality. Expected values must come from an
independent V1 snapshot or fixture rather than being generated from the
replayed result itself.

For schema-version-1 history written before these fields were observed,
replay preserves compatibility by materializing `result_status=UNKNOWN` and
`resource_key=UNKNOWN`. Existing immutable events are not rewritten, and
comparisons against historical traces must declare those unknowns explicitly.

Legacy task streams with `execution_ref=NULL` remain task-level and
`UNATTRIBUTED`. The first snapshot or claim bootstraps the stream; later
unattributed claims are new resource-acquisition observations only after the
previous lease cycle was released. A release must follow a held lease and,
when present, must match the held `resource_key`. Replay still rejects a
duplicate bootstrap, version gap, wrong attribution, or illegal duplicate
release, and it never invents an execution, attempt, or provider-session ID.

Explicit exclusions for PVX-1805 are:

- raw provider payload and unpersisted provider notification history;
- inferred attempt or provider-session identity;
- Linear writeback state;
- host stdout/stderr bodies;
- V1 fields not named in the hook matrix;
- history before an imported baseline;
- cross-stream total ordering.

Replay fails on missing baseline, unknown event schema/type, stream version gap,
wrong order, aggregate identity mismatch, or conflicting exact correlation. It
does not skip invalid events and return overall success. Comparison reports
differences per field. Replaying the same trace yields the same canonical state
and digest.

Reads are bounded by page count, page size, and per-page payload bytes.
Projection checkpoints store only a cursor/version position and mutable status;
they do not modify event payloads.

The append/read byte boundary is explicit. New event payloads are rejected with
`PayloadSizeExceeded` when their canonical UTF-8 JSON exceeds the configured
single-event write limit. Exact idempotent retries are checked before that
admission rule so an event accepted under an older limit remains retryable.
Reads first select cursor and UTF-8 byte-length metadata, reject a single
oversized record or an over-budget page, and only then materialize event
bodies. Existing oversized records therefore remain readable only under a
budget that admits them; they are not silently skipped. A failed preflight
does not advance the caller's cursor, and read connections/cursors are closed
on both success and error.

## 11. Covered and Uncovered Boundaries

PVX-1805 covers:

- isolated, versioned local SQLite schema;
- append-only event persistence;
- scoped exact idempotency and conflicts;
- per-stream CAS with independent connections/processes;
- same-connection local V1 mutation/event/outbox atomicity;
- durable inbox/outbox local contracts;
- bounded deterministic offline replay and field comparison;
- representative real `TaskRegistry` lifecycle instrumentation;
- software-process crash/retry fixtures using temporary databases.

PVX-1805 does not cover:

- V2 authority or scheduler decisions;
- runtime-worker takeover or fencing enforcement;
- a continuous provider event collector;
- production Linear outbox delivery;
- public MCP changes;
- production database migration or shadow enablement;
- production deployment;
- canonical provider/host end-to-end execution;
- power-loss durability certification;
- Rust or multi-node storage.
