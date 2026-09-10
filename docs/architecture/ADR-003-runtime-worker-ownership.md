# ADR-003: Durable RuntimeWorker Ownership and Fenced Assignment

- **Status:** Accepted for the PVX-1806 foundation
- **Date:** 2026-09-10
- **Decision owners:** CLINX maintainers
- **Scope:** Internal, explicitly initialized runtime-control foundation
- **Related decisions:** [ADR-001](./ADR-001-v2-architecture.md), [ADR-002](./ADR-002-shadow-event-ledger.md)

## 1. Decision

CLINX will add a small, independent `runtime_control` persistence boundary for
durable worker ownership. It is a foundation for the V2 AI Engineering Control
Plane, not a second authority for the V1 dispatcher. The foundation records
explicit V2 registrations, fenced assignments, resource epochs, runtime event
history, command receipts, recovery work, and a local outbox in SQLite.

The package is default-off. `RuntimeControlStore.initialize()` is the only
operation that creates its additive schema. Constructing `TaskRegistry(path)`
or serving the existing MCP contract does not initialize or use these tables.
No V1 execution, lease, provider process, worktree, or finalizer state is
automatically imported.

The durable relation is:

```text
runtime task reference
        |
     execution registration
        |
      attempt
        |
     assignment ---- runtime worker incarnation
        |
   resource allocation ---- resource fencing counter
        |
   runtime event + command receipt + local outbox
```

An assignment is valid only when its worker incarnation, attempt, resource,
epoch, lease, and expected aggregate version all validate in the same SQLite
transaction as the protected write. Database fencing is qualified here only
for controlled SQLite fixtures. It does not prove that an old process, shell,
container, or filesystem client has stopped.

## 2. Durable Entities and Ownership

| Entity | Responsibility | Lifecycle authority | Persistence requirement |
|---|---|---|---|
| Task reference | Names an explicitly registered V2 task relationship. | V2 registration command; it does not mutate V1 Task. | Durable key and source label; no inferred legacy identity. |
| Execution registration | Stores one requested V2 run and immutable request/policy snapshots. | V2 command boundary; it does not finalize V1 or V2 terminal outcomes. | Durable row, request JSON, policy JSON, lifecycle, and version. |
| Attempt | Names one concrete execution realization and retry index. | V2 attempt registration and controlled recovery rules. | Durable execution FK, unique retry identity, lifecycle, and provider-session reference if known. |
| RuntimeWorker | Names stable worker identity and capability/capacity metadata. | Worker registry/coordinator. | Durable worker identity, capacity, lifecycle, and version. |
| Incarnation | Names one process generation for a stable worker. | Coordinator registration; worker heartbeat cannot create or revive it. | Durable generation, lease/heartbeat telemetry, and status. |
| Assignment | Grants one attempt to one worker incarnation for a bounded lease. | Coordinator assignment, renew, revoke, expiry, and recovery commands. | Durable owner tuple, lease, version, state, and immutable assignment identity. |
| ResourceAllocation | Grants a resource key at a monotonic fencing epoch. | Coordinator/resource authority. | Durable allocation, owner tuple, epoch, expiry, release/quarantine state. |
| Event | Records versioned runtime lifecycle facts. | Append-only transaction participant. | Durable event family/type, per-stream sequence, non-causal global storage position, payload, and integrity metadata. |
| Command receipt | Records scoped idempotency and original committed result. | The command transaction. | Globally unique `receipt_id`, client-compatible `command_id`, scoped key, semantic fingerprint, and original result; current authority is rechecked on retry. |
| Recovery work | Records that an expired/orphaned owner needs explicit reconciliation. | Explicit reconciliation and recovery commands. | Durable state; no status read triggers recovery. |

The package deliberately does not create a `ProviderSession` record. A
provider session remains a provider-specific continuity object and is optional
until exact provider correlation exists. A session reference can be attached
to an attempt, but it never becomes an execution or assignment identity.

## 3. Authority and Transition Rules

| Operation | Caller boundary | Required evidence | State effect |
|---|---|---|---|
| Register task/execution/attempt | V2 coordinator | Existing parent registration and unique identity | Adds a durable V2 relation; never imports a V1 row implicitly. |
| Register worker/incarnation | Coordinator | Stable worker identity and a new generation | Registers or supersedes an incarnation. Old assignments are quarantined, not silently transferred. |
| Heartbeat | Worker through an internal authenticated boundary | Exact worker and active incarnation | Updates heartbeat telemetry only; it cannot grant, extend, or recover assignments. |
| Assign attempt | Coordinator | Pending attempt, active incarnation, capacity, free resource, command key | Atomically allocates a new resource epoch, creates the current assignment, event, receipt, and outbox. |
| Renew assignment | Current worker/coordinator | Exact assignment/attempt/worker/incarnation/resource/epoch, unexpired lease, expected version | Extends only that assignment lease. `now >= expires_at` is expired. |
| Reconcile expiry | Coordinator | Coordinator clock and bounded scan | Marks expired ownership and quarantines the resource; it does not prove process death. |
| Recover assignment | Coordinator with explicit fixture evidence | Expired/orphaned assignment, old process stopped, side-effect fence verified | Releases quarantine and makes a non-terminal attempt eligible for a controlled reassignment. |
| Revoke/release | Coordinator/current owner as allowed by internal policy | Exact current ownership tuple and expected version | Invalidates the old assignment; release never resets the resource epoch. |
| Evidence mutation | Current worker through an internal boundary | Exact current ownership tuple, valid lease, and assignment-owner handoff | Stores attempt-local evidence only; it cannot finalize Task or Execution. |
| Protected mutation | Resource adapter at the actual SQLite write | Full owner tuple, epoch, lease, expected version | Updates only when the same transaction validates the current fence. |

There is no external authentication protocol in this package. A caller-supplied
string such as `actor="coordinator"` is metadata, not authorization. Production
integration must place an authenticated command boundary around these internal
methods before live use.

## 4. Fencing, Capacity, and Idempotency

Stable `worker_id` and `incarnation_id` are separate identities. A new
incarnation cannot make the old incarnation valid. Assignment validity is the
conjunction of:

```text
worker_id + active incarnation_id + assignment_id + attempt_id
+ resource_key + resource_epoch + unexpired lease + expected version
```

SQLite foreign keys, partial unique indexes, `BEGIN IMMEDIATE`, and compare-
and-swap version updates enforce the relation without process-local locks.
One attempt has at most one current assignment. An active/quarantined resource
cannot be admitted twice, and worker active assignment count cannot exceed the
persisted capacity. Resource counters are incremented only on a new allocation
and never reset on release, worker change, or database reopen.

Each mutation accepts a scoped command key. A retry with the same key and the
same semantic fingerprint returns the original committed receipt and does not
create a second allocation, event, or epoch. A same-key semantic mismatch is a
conflict. A historical receipt includes `original_committed_result`, but its
`current_authority_valid` value is recomputed from current rows; replaying a
receipt cannot restore an expired or revoked permission.

Receipt lookup is not an authorization or recovery path: a new
owner-authorized command must still obtain the assignment handoff described
below.

`command_id` remains the caller-facing compatibility parameter and is returned
unchanged in `CommandReceipt`. It is not a globally unique storage key. Receipt
lookup and conflict detection use `(idempotency_scope, idempotency_key)`;
`receipt_id` is the separate global storage identity. Supplying both
`command_id` and `idempotency_key` still requires equal values. This lets two
scopes use the same client key without weakening conflicts inside one scope.

`record_attempt_evidence()` keeps `evidence_id` optional. With an existing
scoped receipt, a retry first restores the committed generated ID from that
receipt and then verifies the complete semantic fingerprint. With no receipt,
the ID is generated inside the serialized business transaction. If neither a
client key nor an explicit evidence ID is supplied, the default scoped key is
derived from the request semantics. Explicit IDs remain part of the semantic
fingerprint. Payload or owner changes under the same scoped key still conflict.
An overlapping same-key call may be rejected while the first call's assignment
guard is pending; an exact retry after the winner commits recovers the original
receipt and generated evidence ID.

Assignment, allocation, state changes, runtime event, command receipt, and
outbox row are one local transaction. Faults before commit roll back all of
them. A response lost after commit is recovered by the exact command retry.

## 5. Time and Recovery

The coordinator clock supplies lease decisions and absolute `expires_at`.
Worker-reported timestamps are telemetry only. Tests use an injected clock.
Every owner-authorized write — protected mutation, renewal, release, revoke,
and attempt evidence — first commits a trusted preflight observation to
`runtime_clock_state`, then creates a durable, per-assignment
`runtime_safety_handoffs` row in a short safety transaction before acquiring
the ownership write lock. The row is a fail-closed guard, not a business
event; it records the observation and the lease expiry bound that must be
crossed before orphan recovery is allowed. The business transaction acquires
its lock and samples the clock again. That post-lock sample is the
authorization linearization point and must satisfy `now < expires_at`.

The shared `_owner_rows(..., handoff_token=...)` boundary is mandatory for all
five owner-authorized writes. It verifies that the current transaction's
durable token belongs to the requested assignment before checking the owner
tuple, incarnation, epoch, allocation, version, and lease. A pending guard
owned by another command is a `SafetyDecisionPending` decision: it cannot be
deleted, replaced, or treated as proof of current authority. A guard on an
unrelated assignment is not consulted, so an independently valid assignment
can continue. The token is an internal transaction capability, not an
external authentication credential.

On success, state, event, receipt, outbox, the newest watermark, and deletion of
the matching handoff guard commit together. On a rejected decision or business
fault, the business transaction rolls back completely; a separate safety
transaction first commits the post-lock observation and only then removes the
guard. During that two-transaction handoff, a new call sees the durable guard
and cannot use the old watermark. If safety persistence is busy, fails, or the
process exits, the guard remains and `SafetyDecisionPending` (or the original
safety failure) fails authorization closed. The original business reason is
retained as exception context; no failed mutation state, event, receipt, or
outbox row is committed. Evidence is not a diagnostic exception to the owner
contract: it has the same safety boundary while its committed receipt remains
a historical result rather than a renewed permission.

`recover_safety_handoff()` is an explicit, evidence-gated recovery path. It
requires old-process stop and side-effect-fence evidence and refuses to clear a
guard before its recorded lease bound. A clock rollback raises `ClockAnomaly`
instead of revalidating old leases. Thus lock contention cannot authorize with
a pre-lock time, and a forward jump makes leases ineligible at the exact
`now >= expires_at` boundary without proving physical process death.

Expiry reconciliation is explicit and bounded. It persists recovery work and
quarantines the resource. A reopened coordinator can continue reconciliation
from SQLite; reading status does not trigger recovery. Reassignment of an
existing, non-terminal realization keeps the same attempt identity. A new
attempt or new execution must be registered explicitly. A terminal attempt is
never reopened or overwritten.

When old process state is unknown or a side-effect fence cannot be verified,
the resource remains quarantined and recovery is blocked. This decision does
not qualify physical process fencing, real provider failover, or safe reuse of
a V1 worktree.

## 6. Event Compatibility and Storage

Runtime events use a distinct versioned family, `RUNTIME_WORKER_V1`; they are
not sent through the V1 `V1*Observed` shadow validation path. Runtime tables
are additive and use their own `runtime_schema_migrations` version ledger so
initializing runtime control never edits or deletes PVX-1805 history. Existing
`v2_` shadow schema, append-only triggers, replay, bounded reads, and MCP
behavior remain unchanged.

Runtime event streams are append-only and sequence-checked. Assignment events
now carry a validated state snapshot for assignment, allocation, attempt, and
recovery fields. Worker registration and incarnation events carry their
post-mutation aggregate versions; `WorkerHeartbeatRecorded` carries the
post-mutation Worker and incarnation versions and remains in the incarnation
stream. The reducer selects transitions by `(event_type, schema_version)`,
verifies the payload hash, identity, sequence, required fields, dependent
lifecycles, and exact version advances, and never consults current database
rows. A worker replay therefore accepts a worker registration stream plus its
immutable related incarnation streams, merged by durable event position. A
worker stream by itself is explicitly a `registration_only` partial view, not
an aggregate-version claim. Legacy worker events without version fields return
`UNKNOWN`/`NOT_COVERED` provenance rather than inferring versions from
registration counts. Legal foundation events without the assignment snapshot
are handled by explicit v1 upcasts. Unknown types/schemas and illegal
transitions fail; unknown extension fields are reported as `NOT_COVERED` and
are not copied into authoritative projected state.

The worker replay coverage is intentionally field-specific:

| Mutation | Immutable evidence | Replay comparison |
|---|---|---|
| Worker registration | `worker_version=0`, identity, capacity | Worker identity/lifecycle/version |
| Incarnation registration | worker/incarnation identity, generation, new version, superseded identity/version | Current incarnation, generation, Worker version, superseded lifecycle/version |
| Heartbeat | incarnation stream sequence, heartbeat time, post-mutation Worker and incarnation versions | Heartbeat time and both aggregate versions |
| Legacy worker event | registration fields only | Registration-only view; missing versions are `UNKNOWN`/`NOT_COVERED` |

Stream sequence is a per-stream append order. Aggregate version is a durable
post-mutation counter and is never derived from the number of registration
events. The composite replay validates every related stream independently and
merges only by immutable storage position; that position is not a business
causal order.

| Assignment event | Legal prior lifecycle | Result | Allocation | Attempt | Recovery |
|---|---|---|---|---|---|
| `AssignmentGranted` | none | `ACTIVE` | `ACTIVE` | `ASSIGNED` | none |
| `AssignmentRenewed` | `ACTIVE` | `ACTIVE`, new lease/version | `ACTIVE`, new lease/version | unchanged | none |
| `AssignmentReleased` | `ACTIVE` | `RELEASED` | `RELEASED` | `RELEASED` | none |
| `AssignmentRevoked` | `ACTIVE` | `REVOKED` | `RELEASED` | `PENDING` | none |
| `AssignmentExpired` | `ACTIVE` | `EXPIRED` | `QUARANTINED` | `ASSIGNED` | `PENDING` |
| `AssignmentOrphaned` | `ACTIVE` | `ORPHANED` | `QUARANTINED` | `ASSIGNED` | `PENDING` |
| `AssignmentRecovered` | `EXPIRED` or `ORPHANED` | `RECOVERED` | `RELEASED` | `PENDING` | `DONE` |

Per-stream continuation and cross-stream scanning are different contracts.
`read_events(stream_type=..., stream_id=..., after_sequence=...)` uses only the
aggregate sequence and requires both stream identifiers. Cross-stream callers
use `scan_events(cursor=...)`, whose opaque token represents an append-only
`runtime_event_positions.global_position`. That position is storage order, not
business causality. An unfiltered non-zero `after_sequence` is rejected.
Payload byte preflight occurs before body materialization; rejection returns no
advanced token, so the same cursor can be retried with an adequate budget.

Runtime schema migration 2 rebuilds only the receipt table, copying every old
row with `receipt_id=old command_id` while preserving client-visible
`command_id`, scope, fingerprint, result, and timestamp. New receipts use an
independent generated `receipt_id`. It also backfills event positions in
existing `runtime_events.rowid` order without rewriting the event rows, then
assigns future positions with an `AFTER INSERT` trigger in the event
transaction. Migration 3 adds the assignment-scoped
`runtime_safety_handoffs` guard table without touching historical events or
receipts. Closing runtime control leaves all rows intact; reinitialization is
idempotent. No production database migration is performed by this decision.

## 7. Migration and Non-Goals

Migration is intentionally incremental:

1. Validate the standalone schema, worker/assignment state machine, and
   SQLite fencing with temporary fixtures.
2. Add an authenticated coordinator boundary and provider adapter contracts in
   later decisions, without importing V1 ownership implicitly.
3. Only after separate qualification may a future issue consider controlled
   integration with V2 executions or resources.

This ADR does **not**:

- change the V1 `TaskRegistry`, Finalizer, dispatcher, lease authority, or MCP
  contract;
- enable shadow events or runtime workers by default;
- migrate a production database or take over a live provider/worktree;
- implement a scheduler, fair queue, provider failover, Rust, RPC, systemd
  service, or physical process fence;
- automatically start PVX-1807 or any later phase.

The qualification must keep these boundaries explicit:

```ini
V1_LIVE_AUTHORITY=ON
V2_LIVE_AUTHORITY_CUTOVER=NOT_PERFORMED
WORKER_RUNTIME_DEFAULT=OFF
SHADOW_DEFAULT=OFF
PUBLIC_MCP_CONTRACT=UNCHANGED
PRODUCTION_DB_MIGRATION=NOT_PERFORMED
PRODUCTION_DEPLOY=NOT_PERFORMED
REAL_PROVIDER_TAKEOVER=NOT_RUN
PHYSICAL_PROCESS_FENCING=NOT_QUALIFIED
CANONICAL_PROVIDER_E2E=NOT_RUN
NEXT_PHASE_STARTED=NO
```
