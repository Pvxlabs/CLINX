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

The independent review and remediation baseline for this qualification is
`b09a36eeca15f693df7031b4f34f635f68ef99ee`. The historical source report above
is retained for lineage; its original test counts are not substituted for the
current checkout results in the remediation addendum below.

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

## 13. PVX-1805 Remediation Addendum: SL-01 to SL-04

This addendum records the bounded correction requested by the independent
review. It does not change the V1 authority boundary or promote the shadow
ledger to a runtime decision maker.

```ini
REVIEW_BASE_COMMIT=b09a36eeca15f693df7031b4f34f635f68ef99ee
FINAL_COMMIT=RECORDED_IN_FINAL_REPORT_AFTER_COMMIT
V1_AUTHORITY=ON
V2_EVENT_AUTHORITY=OFF
SHADOW_DEFAULT=OFF
PUBLIC_MCP_CONTRACT=UNCHANGED
```

### SL-01 — Snapshot field fidelity

**Defect and root cause.** The original baseline importer used one `stage`
value for both lifecycle state and current stage, and only read a provider turn
from an existing result row. That lost an already persisted active turn and
could misstate `CODEX_RUNNING / provider_wait` as `provider_wait / provider_wait`.

**Fix location.** `task_registry.py::import_shadow_baseline()` now reads
`execution_state`, `current_stage`, and `turn_id` independently when an exact
active execution and its task projection are provably correlated. Retained
history uses only execution-owned evidence; fields absent from that history are
`UNKNOWN` with field-level provenance. A newer execution on the same task is
never used to fill an older execution's facts. `result_status` and
`resource_key` are also imported explicitly.

**Test mapping.**

- `test_snapshot_baseline_preserves_active_state_stage_and_turn_without_result`
- `test_retained_old_execution_does_not_borrow_new_task_projection`
- `test_existing_exact_execution_requires_explicit_unknown_history_baseline`
- `test_legacy_snapshot_then_new_cycle_replays_without_invented_identity`

**Before/after.** Before remediation, the defect was source-confirmed by the
review and the active/no-result candidate was not independently run against the
review checkout. After remediation, the real `TaskRegistry` plus temporary
SQLite regression passes and preserves all three fields and provenance.

**Compatibility and remaining boundary.** The importer remains a declared
current snapshot, not reconstruction of pre-baseline history. Missing retained
fields stay `UNKNOWN`; no historical event is fabricated or rewritten.

### SL-02 — Legacy task stream multi-cycle replay

**Defect and root cause.** The original reducer treated every
`V1UnattributedExecutionClaimObserved` as a stream bootstrap, so a legal
`claim -> release -> claim -> release` sequence failed at the second claim.

**Fix location.** `shadow_ledger/replay.py::ReplayReducer._apply()` now
distinguishes first-stream bootstrap from a later unattributed acquisition.
Later claims require an `UNATTRIBUTED` stream with no held lease and establish
a new held resource cycle. Releases require a held lease and a matching
resource when one is known. No execution, attempt, or provider-session ID is
created.

**Test mapping.**

- `test_legacy_task_stream_replays_two_real_resource_cycles`
- `test_legacy_snapshot_then_new_cycle_replays_without_invented_identity`
- `test_replay_rejects_duplicate_bootstrap_after_valid_baseline`
- `test_replay_rejects_version_gap_after_valid_baseline`
- `test_legacy_release_requires_held_lease_and_matching_resource`
- `test_replay_rejects_wrong_attribution_and_turn_ownership`

**Before/after.** The review's red-phase reproduction produced
`ReplayError: baseline event must be first in a stream` for the second legacy
claim. The corrected real-registry and reducer fixtures replay both resource
cycles and still reject duplicate bootstrap, version gaps, wrong ownership,
and invalid releases.

**Compatibility and remaining boundary.** Existing task-level unattributed
history remains legal and identity-free. The reducer does not infer causality
between unrelated streams or repair malformed historical order.

### SL-03 — Result status and resource identity comparison

**Defect and root cause.** The original ADR/code contract omitted
`result_status` and `resource_key` from typed replay state, summary hashing, and
field comparison. PASS versus BLOCKED or resource A versus resource B could
therefore compare as equal.

**Fix location.** `ReplayResult`, `ReplayReducer`, `COMPARISON_FIELDS`, and
`ReplayService.compare()` now carry and compare both fields. Expected input must
contain every comparison field. Exact execution, turn, and attribution
correlation conflicts fail closed. Historical schema-1 events lacking the new
facts replay as explicit `UNKNOWN` values.

**Test mapping.**

- `test_comparison_requires_result_status_and_resource_key_from_v1_fixture`
- `test_replay_rejects_wrong_attribution_and_turn_ownership`
- `test_old_execution_result_cannot_apply_to_new_execution_stream`
- `test_replay_rejects_unknown_schema_gap_missing_baseline_and_correlation`
- `test_append_only_triggers_reject_update_and_delete`
- `test_offline_cli_replays_and_compares_without_writing`

**Before/after.** The review's isolated reproduction showed identical replay
state, identical summary hash, and `compare=PASS` when either field changed.
After remediation, independent V1 fixture values detect both differences,
missing expected fields raise `ReplayError`, and exact identity/turn conflicts
are rejected. The append-only triggers remain active; committed events are not
edited.

**Compatibility and remaining boundary.** Old legal events are not migrated in
place. A comparison involving them must declare `UNKNOWN`, and fields outside
`COMPARISON_FIELDS` remain `NOT_COVERED`.

### SL-04 — Read and write size boundaries

**Defect and root cause.** The original reader selected and `fetchall()`'d full
event rows before checking the UTF-8 payload budget, and new writes had no
single-payload admission limit.

**Fix location.** `shadow_ledger/store.py::EventStore._append()` checks the
canonical UTF-8 payload size after exact-idempotency lookup and before storing a
new event, raising `PayloadSizeExceeded`. `read_events()` first selects cursor
and SQLite BLOB byte lengths, validates single and cumulative page budgets, and
only then selects full event bodies. It explicitly closes cursors and
connections on success and error.

**Test mapping.**

- `test_append_rejects_payload_above_write_budget_with_distinct_error`
- `test_append_accepts_payload_exactly_at_utf8_write_budget`
- `test_exact_retry_of_historical_large_payload_survives_lower_write_budget`
- `test_read_preflights_existing_oversized_payload_before_full_select`
- `test_read_budget_is_utf8_exact_and_rejects_one_byte_below`
- `test_read_cumulative_budget_does_not_skip_unread_events`
- `test_read_exception_closes_connection_and_cursor_resources`

**Before/after.** Before remediation, the review confirmed the full-row
materialization ordering from source inspection; no production OOM was claimed.
After remediation, single-record, cumulative, multibyte, exact-budget,
pagination, pre-materialization, and cleanup tests pass.

**Compatibility and remaining boundary.** Existing oversized records are not
deleted or truncated. They are rejected before payload materialization when a
page budget cannot admit them. Exact retries of historical events remain
idempotent after a write limit is lowered. This is a bounded local SQLite read
contract, not a distributed storage capacity or power-loss guarantee.

## 14. Remediation Test Evidence

These are the actual results from the current checkout after the remediation
patches. The original report's `338 passed, 66 subtests passed` is historical;
the additional negative and boundary tests account for the new total.

```text
python3 -m pytest -q --ignore=test_domain.py --ignore=test_shadow_ledger.py
297 passed, 48 subtests passed in 5.15s

python3 -m pytest -q test_domain.py
10 passed, 10 subtests passed in 0.05s

python3 -m pytest -q test_shadow_ledger.py
50 passed, 8 subtests passed in 0.73s

python3 -m pytest -q
357 passed, 66 subtests passed in 5.99s

python3 -m compileall -q domain shadow_ledger task_registry.py test_domain.py test_shadow_ledger.py
PASS

git diff --check
PASS
```

The Shadow Ledger suite includes the existing CAS, multi-process race,
idempotency conflict, crash-before/after-commit, rollback, inbox/outbox,
default-off, ON-to-OFF, append-only, CLI replay/compare, and real-registry
fixtures. These remain local temporary-database tests; canonical provider E2E,
production database migration, deployment, and authority cutover were not run.

## 15. Claim-window ownership correction

This section records the follow-up correction against `fd948f0`. It addresses
only the remaining SL-01 ownership gap; SL-02, SL-03, and SL-04 are retained
unchanged.

### Field ownership rule

An active `executions` row and matching worktree lease prove that an execution
owns a resource. They do not prove that the mutable `tasks` projection has
already been rebound to that execution. Exact baseline fields therefore use
these sources:

| Field | Exact source | Proof | Behavior without proof |
|---|---|---|---|
| `execution_state` | `executions.execution_state` / retained history | V1 state transition persisted beside the exact execution | `UNKNOWN`, provenance `NOT_PERSISTED_FOR_EXACT_EXECUTION` |
| `current_stage` | `executions.stage` / retained history | Execution-owned stage written at claim or progress | `UNKNOWN`; never copy Task stage |
| `turn_id` | execution-owned turn, then exact result turn | Exact execution/result correlation; both are checked for conflict | `UNKNOWN`; conflict is recorded as `CONFLICT` |
| `lease_state`, `resource_key` | matching lease/history row | Resource row matches task and exact execution | `UNKNOWN`; does not establish other field ownership |
| `result_status` | exact result row | Result references the exact task and execution | `UNKNOWN` when absent |

The claim event starts a new execution with `turn_id=NULL`. A provider thread or
session may be continued by a future adapter, but that continuity is not the
new execution's exact provider turn.

### Red/green evidence

Before the correction, the two real-registry temporary SQLite candidates failed:

```text
test_new_claim_snapshot_cannot_borrow_old_task_projection: FAIL
  execution_state/current_stage/turn_id = COMPLETED/COMPLETED/turn_old
test_shadow_new_claim_does_not_inherit_previous_execution_turn: FAIL
  claim turn_id = turn_old
```

The review probe independently observed the same values after the new execution
row and lease were committed. After the correction, the tests pass and the new
execution baseline is `CLAIMED / CLAIMED / NULL` (with execution-row
provenance), while its claim event contains no prior turn.

The reopened-registry case passes: a separate `TaskRegistry` imports the
baseline after claim commit and reads durable execution-owned columns, not
process-local state. A valid active execution with no result also retains its
persisted state/stage/turn. An exact result retains its own result status and
turn; a deliberately contradictory result row produces `UNKNOWN` plus explicit
turn-conflict provenance. Retained A and active B remain isolated in both
directions.

### Implementation and compatibility boundary

`task_registry.py` adds nullable ownership columns to active and retained V1
execution rows. New claims initialize `execution_state=CLAIMED`,
`stage=CLAIMED`, and `turn_id=NULL`; `set_execution_state()` updates the exact
active row in the same transaction as the Task projection and shadow event.
Release/reclaim copies those columns into retained history. Existing rows are
not rewritten from the Task projection: on databases that predate these
columns, missing state or turn remains `UNKNOWN`. Existing immutable shadow
events are not edited or deleted. This is an internal compatibility migration
path only; no production database migration was executed.

The correction does not change V1 scheduling, Finalizer decisions, lease
authority, provider calls, public MCP contracts, or shadow default-off behavior.
It does not implement RuntimeWorker, Scheduler, Rust runtime, or an Execution/
Attempt storage migration.

### Follow-up test mapping

- `test_claim_window_baseline_does_not_borrow_retained_projection`
- `test_claim_observation_does_not_inherit_retained_turn`
- `test_snapshot_baseline_preserves_active_state_stage_and_turn_without_result`
- `test_exact_result_turn_is_retained_and_conflict_is_explicit`
- `test_retained_old_execution_does_not_borrow_new_task_projection`

The first two are the red/green core cases. The remaining tests cover durable
reopen, active no-result fidelity, exact result ownership/conflict, and retained
execution isolation. All use temporary SQLite fixtures and deterministic
context-manager boundaries; no provider, management service, or production
database was started.

## 16. V1 ownership-column compatibility correction

This follow-up is based on the review of `b64b93d7c654f4357dc069590b3b83fc5a6374fc`
against parent `fd948f0cabc30b6f48d32d6cb20d76970f8d3630`. It is limited to the
compatibility regressions caused by the nullable execution ownership columns.
The claim-window isolation and SL-02/03/04 behavior remain in force.

### R1: orphan lease recovery

The regression was a SQLite named-column collision in
`reclaim_stale_worktree_leases()`: both `e.execution_state` and
`t.execution_state` were selected without aliases, while the predicate used
`row["execution_state"]`. For a terminal Task with `COMPLETED` and
`codex_running=0`, a missing execution row made the named value NULL. The
parent predicate reclaimed the lease; the reviewed commit returned zero and
left one lease.

The minimal fix is in `task_registry.py`: the query now names
`task_execution_state`, `task_codex_running`, `execution_owned_state`, and
`execution_owned_turn` separately. Task-terminal recovery uses only the Task
aliases. Retained history uses only execution-owned aliases. The original
conservative rule remains: a live execution is not reclaimed merely because
the Task is `RECOVERY_REQUIRED` or otherwise marked uncertain.

### R2: legacy running update

The parent-created database test proves that an old active execution has no
`executions.execution_state` or `executions.turn_id` columns. After the current
initializer adds both nullable columns, they are NULL. The reviewed setter
incorrectly treated the NULL owned turn as the only validation source and
rejected the parent-accepted same-state call:

```text
CODEX_RUNNING requires an exact turn_id
```

The correction is in `set_execution_state()`. If the active row is an old row
whose ownership state is still NULL, the existing Task turn may be used for
V1 setter validation. That fallback is deliberately not copied into the
execution-owned field or shadow event. A new claim has ownership state
`CLAIMED`, so an omitted turn is not accepted as a new exact turn and the
claim-window isolation remains intact.

### Red and green evidence

The supplied full-checkout candidate was executed locally through pytest
against the current repository before the fix:

```text
3 failed, 1 passed
R1 direct orphan reclaim: 0, lease remained 1
R1 startup orphan reclaim: lease remained 1
R2 parent-created DB upgrade: CODEX_RUNNING requires an exact turn_id
```

After the correction, the added real-registry temporary SQLite suite passes:

```text
python3 -m pytest -q test_pvx1805_v1_compatibility.py
9 passed
```

It covers parent-created and current-created databases, shadow OFF and ON,
direct and startup reclaim, repeated reclaim idempotency, active recovery
guarding, legacy running update after additive migration, explicit owned-turn
preservation, and shadow observation that remains NULL rather than fabricating
the legacy Task turn.

### Schema and source lineage

The default initializer still performs an automatic additive migration when
opening an older writable database. It adds nullable `execution_state` and
`turn_id` columns to `executions` and `execution_history`; it does not backfill
them from `tasks`. On a new database, the same columns are created in the
initial table definitions. A fresh database opened with
`shadow_events=False` creates no V2 ledger tables; opening with
`shadow_events=True` creates the local shadow tables after V1 initialization.
Reopening an existing shadow database with shadow disabled retains its history
but appends no new events. This code path was exercised only on temporary
SQLite databases. `PRODUCTION_DB_MIGRATION` remains `NOT_PERFORMED`.

The parent source was loaded from the local Git object
`fd948f0cabc30b6f48d32d6cb20d76970f8d3630` to create the old database; the
current checkout then opened that same database and performed the migration.
Existing events, execution rows, and leases were not rewritten or deleted to
make the compatibility test pass. Old owned fields that remain NULL are still
`UNKNOWN` for exact shadow purposes.

### Remaining limits

This correction does not define a general migration for databases whose
execution row is already partially corrupted, does not infer historical
ownership from timestamps, and does not add a startup drain or new V1 policy.
It does not alter Finalizer, scheduler, cancellation, resource authority,
provider calls, MCP contracts, or production deployment behavior.
