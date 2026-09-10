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
| Event | Records versioned runtime lifecycle facts. | Append-only transaction participant. | Durable event family/type, stream sequence, payload, and integrity metadata. |
| Command receipt | Records scoped idempotency and original committed result. | The command transaction. | Durable semantic fingerprint and original result; current authority is rechecked on retry. |
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
| Evidence mutation | Current worker through an internal boundary | Exact current ownership tuple and valid lease | Stores attempt-local evidence only; it cannot finalize Task or Execution. |
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

Assignment, allocation, state changes, runtime event, command receipt, and
outbox row are one local transaction. Faults before commit roll back all of
them. A response lost after commit is recovered by the exact command retry.

## 5. Time and Recovery

The coordinator clock supplies lease decisions and absolute `expires_at`.
Worker-reported timestamps are telemetry only. Tests use an injected clock;
the system rejects a coordinator clock moving backwards relative to its last
persisted decision instead of revalidating old leases. A forward jump makes
leases eligible for expiry, but still does not prove physical process death.

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

Runtime event streams are append-only and sequence-checked. Payloads are
canonical UTF-8 JSON with a bounded write size. Event reads preflight byte
lengths before materializing bodies and are bounded by page size and byte
budget. Unknown runtime schema versions, gaps, wrong stream identity, and
conflicting idempotency are rejected.

Closing runtime control leaves its rows and events intact. Reinitialization is
idempotent and additive. No production database migration is performed by
this decision.

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
V2_LIVE_AUTHORITY_CUTOVER=OFF
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
