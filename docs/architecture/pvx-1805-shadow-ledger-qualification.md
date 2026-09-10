# PVX-1805 Shadow Event Ledger Qualification

## 1. Scope and Source

```ini
ISSUE=PVX-1805
REPOSITORY=/home/pvxlabs/dev/clinx
BRANCH=main
BASE_COMMIT=75c0a95f43078a9f939151e145c7062c560c19a7
PYTHON_VERSION=3.12.3
SQLITE_RUNTIME_VERSION=3.45.1
SQLITE_MODULE_VERSION=2.6.0
```

Observed base commit:

```text
75c0a95f43078a9f939151e145c7062c560c19a7
2026-09-10T12:06:57+00:00
docs: clarify CLINX product positioning
```

The PVX-1804 prerequisite source was present in the current working tree as
untracked `domain/`, `test_domain.py`, and `docs/architecture/` content. It was
not present in `BASE_COMMIT`; no PVX-1804 commit was invented. The source was
read and its baseline was rerun before PVX-1805 implementation.

Initial prerequisite results from this checkout:

```text
python3 -m pytest -q --ignore=test_domain.py
297 passed, 48 subtests passed in 5.18s

python3 -m pytest -q test_domain.py
9 passed, 10 subtests passed in 0.05s

python3 -m pytest -q
306 passed, 58 subtests passed in 5.22s
```

No real provider, host command, production database, managed service, Linear
write, deployment, merge, or follow-on issue was started.

## 2. Delivered Changes

### Architecture and qualification

- `docs/architecture/ADR-002-shadow-event-ledger.md`
- `docs/architecture/pvx-1805-shadow-ledger-qualification.md`

### Domain contract correction required by the ledger

- `domain/event.py`: completed the reusable event envelope with `recorded_at`,
  structured actor, scoped idempotency, and canonical payload hash.
- `domain/_model.py`: made direct JSON documents reject invalid and non-finite
  JSON values consistently.
- `domain/__init__.py`: exported `EventActor`.
- `test_domain.py`: added the failing envelope/snapshot/JSON tests before the
  minimal domain correction.

### Ledger implementation

- `shadow_ledger/errors.py`
- `shadow_ledger/models.py`
- `shadow_ledger/schema.py`
- `shadow_ledger/store.py`
- `shadow_ledger/replay.py`
- `shadow_ledger/__init__.py`
- `shadow_ledger/__main__.py`

### V1 observation and tests

- `task_registry.py`: default-off constructor switch, explicit schema setup,
  same-connection transaction participation, observation hooks, and explicit
  baseline import. No V1 table or lifecycle decision was replaced.
- `test_shadow_ledger.py`: temporary-database contract, fault, concurrency,
  crash, inbox/outbox, replay, CLI, and real-registry fixture tests.

No dependency was added. All implementation uses the Python standard library.

## 3. Enable and Disable Contract

Default construction remains:

```python
TaskRegistry(path)
```

This leaves shadow mode off, does not import the ledger at runtime, does not
create `v2_` schema, and follows the existing V1 write behavior.

Explicit local enablement is:

```python
TaskRegistry(path, shadow_events=True)
```

It applies the versioned additive shadow schema to the same SQLite file before
instrumented writes. A standalone fixture may explicitly call:

```python
EventStore(path).initialize()
```

Returning to `TaskRegistry(path)` disables new shadow observations without
deleting history. There is no destructive schema rollback and no existing
production configuration or MCP option that enables shadow mode.

## 4. Schema

Migration `1: shadow_event_ledger_foundation` creates:

```text
v2_schema_migrations
v2_legacy_identity_map
v2_aggregate_versions
v2_events
v2_event_inbox
v2_event_outbox
v2_projection_checkpoints
```

The migration is ordered, repeatable, and additive. Unique constraints protect
stream versions and scoped idempotency. Database triggers reject normal event
updates and deletes. Inbox, outbox, versions, mappings, and checkpoints have
their own contracts and do not mutate immutable event payloads.

Append-only protection is bounded to the shipped schema and application path;
it is not protection against a database administrator rewriting the file or
removing a trigger.

## 5. Hook Coverage Matrix

| V1 mutation | Shadow event | Stream attribution | Atomic unit | Replay field |
|---|---|---|---|---|
| Execution and worktree claim | `V1ExecutionClaimObserved` | Exact execution ref | V1 rows + event CAS + outbox | identity, state, stage, lease |
| Legacy NULL-ref claim | `V1UnattributedExecutionClaimObserved` | Task, `UNATTRIBUTED` | V1 rows + event CAS + outbox | task identity, state, lease |
| Changed persisted progress/turn | `V1ExecutionProgressObserved` | Exact execution when known | task/execution row + event CAS + outbox | state, stage, turn |
| Host row insert | `V1HostExecutionStartedObserved` | Exact execution | host row + event CAS + outbox | evidence ref, running state |
| Host row completion | `V1HostEvidenceObserved` | Exact execution | host row + event CAS + outbox | evidence ref, result/hashes |
| Exact result insert | `V1ExecutionResultPersistedObserved` | Exact execution | result row + event CAS + outbox | result ref, status, turn |
| Terminal task state | `V1TerminalStateObserved` | Exact execution when known | task/execution row + event CAS + outbox | terminal state/stage |
| Lease deletion | `V1LeaseReleasedObserved` | Exact execution | lease/history/delete + event CAS + outbox | released state/resource |
| Legacy lease deletion | `V1UnattributedLeaseReleasedObserved` | Task, `UNATTRIBUTED` | lease/delete + event CAS + outbox | released state/resource |
| Explicit current snapshot | `V1SnapshotBaselineImported` | Caller-supplied exact ref or task | baseline event CAS + outbox | known current fields, prior history unknown |

No-op state writes emit no event. A repeated legacy terminal reconciliation
with no remaining V1 mutation emitted no additional progress or release event
in the fixture.

## 6. Identity and Replay

V1 identities are mapped through durable
`(source_system, source_type, source_identity)` rows. Target IDs are stable
deterministic hashes checked against the stored mapping. An exact non-empty
`execution_ref` is required for an execution stream and exact result event.

Legacy `NULL execution_ref` data remains task-level and `UNATTRIBUTED`. The
implementation does not invent an execution, attempt, or provider-session ID.
It never uses a task's latest result to assign an older execution result.

Replay is pure over bounded event pages and an explicit first claim/baseline.
It rejects unknown schema, version gaps/order errors, missing baseline,
correlation conflict, repeated baseline, and conflicting exact result identity.
It never writes V1, queries a provider, calls Linear, releases a lease, or
re-executes a command.

Compared fields are fixed in ADR-002 and code. Timestamps, database cursor,
route/policy payloads, raw provider identity, raw host output, Linear state, and
pre-baseline history are excluded and remain outside this qualification.

## 7. Transaction and Fault Evidence

For shadow-enabled observation hooks, `TaskRegistry` owns one SQLite connection
and explicit transaction. `EventStore` joins that active transaction and does
not issue nested `BEGIN`, `COMMIT`, or `ROLLBACK`.

Injected failures at these boundaries rolled back the V1 execution claim,
worktree lease, identity mapping, aggregate version, event, and outbox:

```text
before_event_append
after_event_append
after_outbox_insert
```

The external-connection unit-of-work test also rolled back after returning from
the ledger and proved the ledger had not committed its caller's transaction.

Software process-exit fixtures established:

- Exit before commit left zero event, version advancement, or outbox row.
- Exit after commit but before a usable response retained one complete event
  and outbox; exact retry returned that first event without another version.

These are process-crash tests, not power-loss durability certification.
Atomicity applies to each instrumented local write unit. The existing V1
finalizer still spans multiple registry calls, so PVX-1805 does not claim that
the whole finalization chain has become one crash-atomic transaction.

## 8. Concurrency and Bounded Samples

- Two independent connections using the same stale `expected_version` allowed
  one commit and rejected the other.
- A real two-process race produced exactly one committed append and one
  `AggregateVersionConflict`, with one event and one outbox row.
- A held SQLite write lock produced `LedgerBusy`, distinct from version
  conflict and schema validation errors.
- A bounded batch used 21 streams and 21 events, pages of 7, and a durable
  projection checkpoint.
- A real registry lifecycle produced 7 ordered events from claim through lease
  release and replayed in pages of 2.
- The CLI replayed and compared a 5-event fixture in pages of 2 without adding
  an event.

These samples validate local contracts only. They are not a production
throughput, concurrency, latency, or capacity benchmark.

## 9. Inbox and Outbox Boundary

Inbox receipts use source plus native dedup key, with optional provider cursor.
The same payload under a different native key remains a separate receipt.
Conflicting reuse of a native key fails. Normalized event append, outbox insert,
and inbox `PROCESSED` state are one transaction; an injected interruption left
the receipt `RECEIVED` and was recoverable. Uncorrelated input can be retained
as `QUARANTINED` without guessing an attempt.

The local fake outbox consumer recorded a failure as `RETRY` without data loss,
then recorded a successful retry as `APPLIED`. This establishes storage and
local delivery-state contracts only.

```ini
CONTINUOUS_PROVIDER_COLLECTION=NOT_RUN
PRODUCTION_LINEAR_ASYNC_DELIVERY=NOT_RUN
EXTERNAL_SIDE_EFFECT_EXACTLY_ONCE=NOT_CLAIMED
```

## 10. Final Test Evidence

```text
python3 -m pytest -q --ignore=test_domain.py --ignore=test_shadow_ledger.py
297 passed, 48 subtests passed in 5.07s

python3 -m pytest -q test_domain.py
10 passed, 10 subtests passed in 0.05s

python3 -m pytest -q test_shadow_ledger.py
31 passed, 8 subtests passed in 0.52s

python3 -m pytest -q
338 passed, 66 subtests passed in 5.66s

python3 -m compileall -q domain shadow_ledger task_registry.py test_domain.py test_shadow_ledger.py
PASS

git diff --check
PASS
```

## 11. Final Status

```ini
SOURCE_PREREQUISITES_VERIFIED=PASS
V1_REGRESSION=PASS
DOMAIN_REGRESSION=PASS
SHADOW_DEFAULT_OFF=PASS
EVENT_APPEND_ONLY=PASS
ATOMIC_LOCAL_WRITE=PASS
EXACT_IDEMPOTENCY=PASS
IDEMPOTENCY_CONFLICT_REJECTED=PASS
CONCURRENT_APPEND_CAS=PASS
CRASH_RECOVERY=PASS
EXACT_EXECUTION_CORRELATION=PASS
REPLAY_EQUIVALENCE=PASS
INBOX_OUTBOX_LOCAL_CONTRACT=PASS

V1_AUTHORITY=ON
V2_AUTHORITY_CUTOVER=NOT_PERFORMED
PRODUCTION_DB_MIGRATION=NOT_PERFORMED
PRODUCTION_DEPLOY=NOT_PERFORMED
CANONICAL_PROVIDER_E2E=NOT_RUN
```

## 12. Uncovered Boundaries

- No production database migration or shadow enablement.
- No real provider continuous event ingestion or reconnect behavior.
- No production Linear outbox consumer.
- No scheduler, worker takeover, fencing enforcement, or resource coordinator.
- No distributed/multi-node event ordering or storage qualification.
- No whole-finalizer crash atomicity.
- No power-loss, filesystem-corruption, backup/restore, or disaster-recovery test.
- No Rust runtime or RPC boundary.
- No MCP schema or response change.
