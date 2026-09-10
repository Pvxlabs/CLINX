# PVX-1806 RuntimeWorker Foundation Qualification

## 1. Scope and Baseline

```ini
ISSUE=PVX-1806
REPOSITORY=/home/pvxlabs/dev/clinx
BASE_COMMIT=73351028002f5dd4249c3b67f90f38a906cd88b0
BRANCH=main
PYTHON_VERSION=3.12.3
SQLITE_RUNTIME_VERSION=3.45.1
V1_LIVE_AUTHORITY=ON
V2_LIVE_AUTHORITY_CUTOVER=NOT_PERFORMED
WORKER_RUNTIME_DEFAULT=OFF
SHADOW_DEFAULT=OFF
PUBLIC_MCP_CONTRACT=UNCHANGED
```

The detailed PVX-1806 Linear description and the supplied ten-section SPEC
are the source for this qualification. The accepted PVX-1805 implementation
was rerun before this work and produced `366 passed, 66 subtests passed` in
the current checkout. That number is not used as a substitute for the final
post-change run.

This issue adds an internal foundation only. It does not import V1 tasks,
executions, leases, provider sessions, or worktrees automatically. No live
provider, manager service, production database, shadow stream, or public MCP
path was started.

## 2. Delivered Boundary

The new `runtime_control/` package is independent from `TaskRegistry` and is
initialized only by:

```python
store = RuntimeControlStore(path, clock=clock)
store.initialize()
```

The constructor does not create a file or schema. `TaskRegistry(path)` does
not call this initializer and does not create `runtime_schema_migrations`.
When explicitly initialized on an existing V1 database, the migration is
additive: it creates only `runtime_*` tables and does not backfill V1 rows.
`runtime_tasks` must be registered explicitly, so a NULL or otherwise
unproved V1 execution reference cannot manufacture an Attempt or a provider
session.

The implementation is standard-library Python and SQLite only. The primary
files are:

- `runtime_control/schema.py`: versioned additive schema and constraints;
- `runtime_control/store.py`: transaction, ownership, fencing, recovery,
  event, receipt, outbox, and bounded-read operations;
- `runtime_control/models.py` and `clock.py`: typed snapshots and injectable
  coordinator time;
- `runtime_control/errors.py`: explicit contract failures;
- `runtime_control/qualification.py`: temporary-DB fixed-seed qualification;
- `test_runtime_control.py`: real SQLite, reopen, fault, and multiprocess
  tests;
- `docs/architecture/ADR-003-runtime-worker-ownership.md`: decision record.

## 3. Durable Identity Relationship

```text
runtime_tasks.task_id
        |
runtime_executions.execution_id  (FK task_id)
        |
runtime_attempts.attempt_id      (FK execution_id, unique retry index)
        |
runtime_assignments.assignment_id (FK attempt, worker, incarnation)
        |                         \
runtime_allocations.allocation_id  runtime_worker_incarnations.incarnation_id
        |                            |
resource_key + monotonic epoch      runtime_workers.worker_id
```

An assignment carries the exact attempt, stable worker, process incarnation,
resource key, resource epoch, lease expiry, and aggregate version. One partial
unique index allows only one `ACTIVE` assignment per attempt. A second partial
unique index allows only one `ACTIVE` or `QUARANTINED` allocation per resource.
SQLite foreign keys are enabled on every connection.

The three concepts remain distinct:

| Situation | Durable identity result |
|---|---|
| Same provider realization after controlled ownership recovery | Same Attempt, new Assignment and higher ResourceAllocation epoch. |
| Re-execution/retry | New Attempt under the same Execution with a new retry index. |
| New user run | New Execution and explicit Attempt registration. |
| Provider continuity | Optional opaque `provider_session_id` on an Attempt; never an Execution or Assignment identity. |

## 4. Authority and Transition Matrix

| State/command | Authority | Allowed result |
|---|---|---|
| Task/Execution/Attempt registration | Explicit V2 coordinator | Durable relation only; no V1 mutation. |
| Worker registration | Coordinator | Stable `worker_id`; no process identity implied. |
| Incarnation registration | Coordinator | New generation; old incarnation becomes `SUPERSEDED`; active assignments become `ORPHANED` and resources `QUARANTINED`. |
| Heartbeat | Current worker boundary | Heartbeat telemetry only. It cannot create an incarnation, extend every assignment, or revive old authority. |
| Assignment grant | Coordinator | Requires pending Attempt, active incarnation, capacity, free resource, and a new epoch. |
| Assignment renew | Current exact owner | Requires full owner tuple, valid lease, and expected version. `now >= expires_at` rejects renewal. |
| Expiry reconciliation | Explicit coordinator command | Marks assignment `EXPIRED`, creates durable recovery work, and quarantines the resource. It does not prove process death. |
| Controlled recovery | Coordinator with explicit evidence | Requires old process stopped and side-effect fence verified; releases quarantine and makes a non-terminal Attempt pending. Missing evidence raises `RecoveryBlocked`. |
| Release/revoke | Exact current owner/coordinator boundary | Invalidates only the exact assignment. A stale release cannot touch a later owner. Release never resets the resource counter. |
| Evidence | Current exact owner | Stores attempt evidence only; it cannot finalize Task or Execution. |
| Protected mutation | Resource adapter at actual SQLite write | Updates a real protected row only after the full fence and expected version validate in the same transaction. |
| Final terminal outcome | Existing V1 Finalizer | Unchanged and outside this package. |

## 5. Fencing and Idempotency Contract

Protected mutation validates this tuple before the SQL `UPDATE`:

```text
worker_id
incarnation_id
assignment_id
attempt_id
resource_key
resource_epoch
unexpired coordinator lease
expected protected-resource version
```

The validation and update are in one `BEGIN IMMEDIATE` transaction. A stale
incarnation, assignment, epoch, lease, or version fails closed. The fixture
does not merely call a token validator: `mutate_protected_resource()` updates
`runtime_protected_resources` under the actual fence.

Resource epochs start at one for a key and increase only when a new allocation
is granted. Release, worker replacement, and coordinator reopen do not reset
the counter. Expired or orphaned allocations are quarantined until controlled
recovery confirms that old side effects are stopped and fenced.

Every ownership mutation commits its state changes, versioned
`RUNTIME_WORKER_V1` event, command receipt, and local outbox row together.
Same-scope/same-key retries compare a semantic fingerprint and return the
original committed result without allocating or incrementing again. A
different semantic command under the same key raises
`RuntimeIdempotencyConflict`. A receipt exposes `original_committed_result`
separately from a fresh `current_authority_valid` check; an old successful
receipt cannot revive permission.

Runtime events use their own family and append-only triggers. They are not
sent through the V1 `V1*Observed` shadow hook, and no PVX-1805 event is
rewritten.

## 6. Clock and Recovery Assumptions

Lease timestamps come only from the coordinator `Clock`. Worker-reported time
is retained as telemetry in heartbeat event payloads and never decides lease
validity. `ManualClock` provides deterministic tests. A backwards coordinator
clock relative to persisted `runtime_clock_state` raises `ClockAnomaly`
instead of making an old lease valid again. A forward jump only makes leases
eligible for explicit reconciliation; it does not prove physical process
death.

`reconcile_expired_once(limit=...)` is bounded and durable. Reopening a new
`RuntimeControlStore` can read the same assignment/recovery rows without the
original process or a status read being present. Recovery is explicit. A
terminal Attempt is never reopened and no external command is automatically
rerun.

## 7. Qualification Matrix

The focused suite uses temporary SQLite databases, separate connections, and
two forked processes for the race. The bounded qualification entry is:

```bash
python3 -m runtime_control.qualification --seed 1806 --operations 200
```

The fixture scope is 8 workers, 32 attempts, 200 operations, and an
independent in-memory reference for expected resource epochs. It is a bounded
state-sequence sample, not a production capacity or throughput claim.

| Contract | Evidence |
|---|---|
| Durable execution/attempt references | Foreign-key chain, reopen test, missing-parent rejection. |
| Worker incarnation isolation | New generation supersedes old; old heartbeat and owner writes fail; old allocation is quarantined. |
| Single current assignment | Partial unique index and two-process same-attempt race. |
| Capacity admission | Durable worker capacity rejects a second assignment. |
| Atomic ownership/event commit | Faults after event, outbox, or receipt roll back all local rows; post-commit response loss is retryable. |
| Command idempotency | Same command returns the original assignment; semantic reuse conflicts; stale receipt reports authority false. |
| Expired lease rejection | Exact boundary `now == expires_at` rejects renew; reconciliation is explicit. |
| Resource epoch monotonicity | Release and regrant produce epochs 1 then 2; protected resource keeps epoch. |
| Stale mutation/release rejection | Old release, evidence, and protected write cannot alter the later owner. |
| Recovery restart/idempotency | Durable recovery row survives reopen; blocked recovery keeps resource quarantined; successful recovery is command-idempotent. |
| Database mutation fencing | Real protected SQLite row update validates the full owner tuple and row version. |
| Clock boundary behavior | Manual clock, exact expiry, worker future timestamp ignored, backward coordinator clock rejected. |
| Replay equivalence | Runtime family, sequence, stream identity, and independent expected event family are checked. |
| V1 compatibility | `TaskRegistry(path)` remains default-only; explicit runtime initialization on an existing V1 DB is additive and isolated. |

## 8. Commands and Results

The current checkout evidence is recorded after the implementation run below;
historical PVX-1805 counts are not copied into this section.

```text
python3 -m pytest -q test_runtime_control.py
python3 -m runtime_control.qualification --seed 1806 --operations 200
python3 -m compileall -q runtime_control
git diff --check
```

The final report records the exact output for the full suite, domain suite,
shadow suite, V1 suite, runtime suite, CLI checks, and bounded qualification.

The foundation-delivery results below are preserved from the `fca699e9`
round; they are historical evidence and are not relabeled as the corrective
run:

```text
python3 -m pytest -q --ignore=test_domain.py --ignore=test_shadow_ledger.py --ignore=test_runtime_control.py
306 passed, 48 subtests passed

python3 -m pytest -q test_domain.py
10 passed, 10 subtests passed

python3 -m pytest -q test_shadow_ledger.py
50 passed, 8 subtests passed

python3 -m pytest -q test_runtime_control.py
20 passed

python3 -m pytest -q
386 passed, 66 subtests passed

python3 -m runtime_control.qualification --seed 1806 --operations 200
qualification=PASS, workers=8, attempts=32, operations=200,
successful_assignments=32, successful_releases=32,
assignment_streams_replayed=32, event_count=145

python3 -m compileall -q domain shadow_ledger runtime_control task_registry.py test_domain.py test_shadow_ledger.py test_runtime_control.py
PASS

git diff --check
PASS
```

The existing PVX-1805 compatibility candidate also ran directly:
`python3 -m pytest -q test_pvx1805_v1_compatibility.py` produced `9 passed`.
The V1, Domain, Shadow, and full counts are current checkout evidence; the
older PVX-1805 report's `297/48` and `31/8` figures remain historical lineage.
The existing shadow CLI help, replay, and compare commands were run on a
temporary fixture: replay completed and compare returned `status=PASS` over
the fixed 14-field contract. No production database or service was involved.

The SPEC gate status for this checkout is:

```ini
DURABLE_EXECUTION_ATTEMPT_REFERENCES=PASS
WORKER_INCARNATION_ISOLATION=PASS
SINGLE_CURRENT_ASSIGNMENT=PASS
CAPACITY_ADMISSION=PASS
ATOMIC_OWNERSHIP_EVENT_COMMIT=PASS
COMMAND_IDEMPOTENCY=PASS
EXPIRED_LEASE_REJECTED=PASS
RESOURCE_EPOCH_MONOTONIC=PASS
STALE_MUTATION_AND_RELEASE_REJECTED=PASS
RECOVERY_RESTART_IDEMPOTENCY=PASS
DB_MUTATION_FENCING=PASS
CLOCK_BOUNDARY_BEHAVIOR=PASS
REPLAY_EQUIVALENCE=PASS
V1_COMPATIBILITY=PASS
PVX1805_REGRESSION=PASS
FULL_SUITE=PASS
```

## 9. Unqualified Boundaries

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

Final follow-up verification in this checkout:

```text
python3 -m pytest -q
428 passed, 66 subtests passed in 7.76s

python3 -m runtime_control.qualification --seed 1806 --operations 200
qualification=PASS; original=200/63/137; transition=10/7/2/1;
worker_version_matrix heartbeats=2, replayed Worker/incarnation=4/2

python3 -m compileall -q domain shadow_ledger runtime_control task_registry.py \
  test_domain.py test_shadow_ledger.py test_runtime_control.py \
  test_pvx1806_remediation.py
PASS

git diff --check
PASS
```

The final commits are ordinary `main` commits `ba5266ad21ef57fe02aff8742138be56aa70a3e7`
and `09bccb53d83b5eb364c03122efbb966c407c831e7`; `origin/main` was read back
at the latter SHA and the worktree was clean. No production database migration,
deployment, service restart, provider takeover, or next phase was performed.

Not qualified here: physical process or shell fencing, real provider failover,
live worktree takeover, distributed SQLite, power-loss durability, scheduler
fairness, quotas, long-term retention, production throughput, Rust, RPC,
systemd service operation, Linear delivery, V2 terminal finalization, and any
PVX-1807 work.

## 10. fca699e9 Independent Review Remediation

### 10.1 Baseline and actual fixtures

```ini
REVIEW_BASE=fca699e9dc19f4791be63702981918471b6f9af5
BRANCH=main
START_WORKTREE=CLEAN
START_REMOTE_MAIN=fca699e9dc19f4791be63702981918471b6f9af5
LINEAR_STATE=In Review
LINEAR_LATEST_REVIEW=fca699e9 independent review - CHANGES_REQUESTED
PYTHON_VERSION=3.12.3
SQLITE_RUNTIME_VERSION=3.45.1
```

The supplied review archive was extracted to `/tmp` and its unmodified
`test_pvx1806_review_candidates.py` was imported against the real checkout.
All databases and protected resources were pytest temporary SQLite fixtures.
No production database, provider, service, physical worktree, or background
runtime was opened.

Red phase:

```text
PYTHONPATH=/home/pvxlabs/dev/clinx python3 -m pytest -vv \
  /tmp/<review>/test_pvx1806_review_candidates.py
6 failed in 0.20s
```

The six cases reproduced: rejected-expiry clock rollback, ordinary generated
evidence retry, generated evidence retry after a lost response, cross-scope
client-key collision, orphan replay divergence, and cross-stream page loss.
The first attempted command used unavailable `python` and executed no tests;
it is not counted as red evidence.

For the green review-candidate run, only RC-04 was adapted as authorized: the
old unfiltered `after_sequence` continuation was replaced with
`scan_events(cursor=...)`, while its complete ordered event-ID comparison and
the other five cases were preserved.

```text
PYTHONPATH=/home/pvxlabs/dev/clinx python3 -m pytest -q \
  /tmp/<review>/test_pvx1806_review_candidates.py
6 passed in 0.14s
```

### 10.2 RC-01 safety observation boundary

Trusted coordinator time is committed in its own short `BEGIN IMMEDIATE`
transaction before an authorization transaction starts. Protected mutation and
renewal then commit a per-assignment `runtime_safety_handoffs` row before
waiting for the ownership write lock. The guard records the preflight
observation and lease bound and makes an unresolved handoff fail closed. After
the lock is acquired, the coordinator samples time again; this post-lock sample
is the authorization linearization point and `now >= expires_at` rejects.

Accepted state/event/receipt/outbox changes, the post-lock watermark, and guard
deletion commit atomically. A rejected or failed business transaction rolls
back all business rows, then a separate safety transaction commits the fresh
observation before removing the guard. If that safety transaction is busy or
fails, or the process exits first, the guard remains durable and subsequent
authorization returns `SafetyDecisionPending`; no old watermark can
reauthorize the assignment. `recover_safety_handoff()` requires explicit stop
and side-effect-fence evidence and will not clear a guard before its recorded
lease bound. Tests cover both lock positions, valid/exact/expired boundaries,
renew and protected mutation, independent connections, rejection, rollback,
busy and commit-failure injection, response loss, reopen, and a real child
process exit. The safety transaction contains no business state, event, receipt,
or outbox record.

### 10.3 RC-02 scoped identity and migration

Runtime schema migration 2 separates `receipt_id` (global storage identity)
from `command_id` (caller-visible compatibility value). Uniqueness and lookup
remain `(idempotency_scope, idempotency_key)`. The existing rule that
simultaneously supplied `command_id` and `idempotency_key` must match is
unchanged. Consequently `task-a/create` and `task-b/create` have distinct
receipt rows but each returns `command_id=create`; a semantic change inside
either scope is still rejected.

For omitted `evidence_id`, an existing scoped receipt is read before an ID is
chosen, and its committed evidence ID participates in fingerprint validation.
With no receipt, generation occurs once under the serialized transaction.
Explicit evidence IDs remain optional and semantic. Tests cover ordinary and
post-commit-response-loss retries, reopen, explicit/generated IDs, payload and
owner conflicts, invalidated authority, and a deterministic two-process
duplicate race that leaves one evidence row and one event.

`reconcile_expired_once(command_id=...)` also records one batch receipt. A
post-commit lost response therefore returns the original assignment-ID tuple on
retry rather than an empty rescan; reusing that batch key with a different
limit remains a semantic conflict.

The upgrade fixture constructs the preserved migration-1 schema and real old
task/event/outbox/receipt rows, then runs `initialize()`. The old event bytes
and hash remain identical, the old receipt becomes
`receipt_id=legacy command_id`, a new scope can reuse that client key, and
migration versions are exactly `(1, 2, 3)`. Migration 3 adds only the
`runtime_safety_handoffs` guard table; it does not rewrite events or receipts.
The reviewed migration-1 schema at the baseline had Git blob
`de2e2d6414dac5266b59346bee0484bbcc844dda`.

### 10.4 RC-03 event/reducer coverage

Assignment producers persist a complete versioned state snapshot in the same
transaction as each event. Replay uses only immutable event input and validates
family, schema, payload hash, timestamps, stream identity, continuous sequence,
event type, required fields, immutable owner tuple, legal transition, dependent
lifecycle, and exact version changes.

| Command/event | Persisted and replay-compared fields |
|---|---|
| Grant | assignment/allocation IDs, attempt, worker, incarnation, resource, epoch, lease, lifecycle, versions |
| Renew | unchanged identity, new lease, assignment/allocation versions |
| Release/revoke | assignment lifecycle/version, allocation release state/version/reason, attempt lifecycle/version |
| Expire | assignment `EXPIRED`, allocation `QUARANTINED`, pending recovery work |
| Orphan | assignment `ORPHANED`, allocation `QUARANTINED`, replacement reason, pending recovery work |
| Recover | assignment `RECOVERED`, allocation `RELEASED`, attempt `PENDING`, recovery `DONE` and attempts |
| Worker incarnation replacement | current incarnation/generation, superseded lifecycle/version, worker version |

Legacy legal schema-1 assignment payloads are upcast by event-specific rules;
the old orphan allocation version behavior is retained only for those old
events. Unknown event family/schema/type, gaps, identity conflicts, hash
changes, and illegal lifecycle changes reject the replay. Unknown extension
fields are listed in `not_covered_fields` and omitted from projected state.
Non-assignment aggregate details not present in historical events remain
`UNKNOWN`/`NOT_COVERED`; the reducer does not query current tables to invent
them.

The bounded qualification preserves the original fixed-seed 8-worker,
32-attempt, 200-operation sample and adds a separate deterministic transition
matrix with clock advancement, response loss/retry, exact expiry, recovery,
reassignment, and incarnation replacement. It reports attempted, effective,
no-op, and rejected operations separately:

```text
original sample: attempted=200, effective=63, no_op=137,
  successful_assignments=32, successful_releases=32,
  assignment_streams_replayed=32, event_count=145
transition matrix: attempted=10, effective=7, no_op=2, rejected=1,
  assignment_streams_compared=2, incarnation_replacement_compared=true,
  response_loss_retry_compared=true
```

### 10.5 RC-04 cursor contract

`read_events(stream_type, stream_id, after_sequence)` is the single-stream
contract. Both stream identifiers are required; using a non-zero
`after_sequence` without them fails explicitly. `scan_events(cursor)` is the
bounded cross-stream contract. Its opaque v1 token advances over a durable
append-only `global_position`, which is assigned by an event-insert trigger and
backfilled for old events without changing the event rows. It is storage order,
not cross-aggregate causality.

Tests cover A1/A2/B1 interleaving, a newly appended stream at sequence 1,
changing page sizes, an empty page with an unchanged checkpoint, reopen with
the same token, malformed tokens, and byte-budget rejection followed by a
successful retry from the original cursor. No query raises the row limit or
materializes the complete history to manufacture a pass.

### 10.6 Corrective-run results

```text
python3 -m pytest -q test_runtime_control.py test_pvx1806_remediation.py
44 passed in 1.27s

python3 -m pytest -q --ignore=test_domain.py --ignore=test_shadow_ledger.py \
  --ignore=test_runtime_control.py --ignore=test_pvx1806_remediation.py
306 passed, 48 subtests passed in 5.48s

python3 -m pytest -q test_domain.py
10 passed, 10 subtests passed in 0.05s

python3 -m pytest -q test_shadow_ledger.py
50 passed, 8 subtests passed in 0.81s

python3 -m pytest -q test_pvx1805_v1_compatibility.py
9 passed in 0.31s

python3 -m pytest -q
410 passed, 66 subtests passed in 7.14s

python3 -m runtime_control.qualification --seed 1806 --operations 200
qualification=PASS (bounded counts listed above)

python3 -m runtime_control.qualification --help
PASS

python3 -m compileall -q domain shadow_ledger runtime_control task_registry.py \
  test_domain.py test_shadow_ledger.py test_runtime_control.py \
  test_pvx1806_remediation.py
PASS

git diff --check
PASS
```

The V1 subtotal is `306 passed / 48 subtests`; its nine PVX-1805
compatibility tests are already included and are not added again. The separate
`9 passed` line is a focused rerun, not a contribution to `410` beyond its
existing inclusion.

```ini
EXPIRY_DECISION_SURVIVES_REJECTION=PASS
CLOCK_ROLLBACK_CANNOT_REVIVE_AUTHORITY=PASS
SCOPED_COMMAND_IDEMPOTENCY=PASS
GENERATED_EVIDENCE_ID_RETRY=PASS
RESPONSE_LOSS_RECEIPT_RECOVERY=PASS
ORPHAN_REPLAY_EQUIVALENCE=PASS
RUNTIME_STATE_REPLAY_MATRIX=PASS
LOSSLESS_MULTISTREAM_PAGINATION=PASS
RUNTIME_SCHEMA_UPGRADE_COMPATIBILITY=PASS
ATOMIC_OWNERSHIP_EVENT_RECEIPT=PASS
STALE_OWNER_REJECTION=PASS
PVX1805_REGRESSION=PASS
FULL_SUITE=PASS

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

The final Git commit, push, remote readback, and post-commit worktree state are
reported by the enclosing task because a commit cannot contain its own SHA.

### 10.7 Follow-up corrective run (7335102)

This follow-up is limited to the two residual contracts from the independent
7335102 review: authorization time after a contended ownership lock, and
heartbeat-aware Worker/incarnation replay. RC-02, RC-04, AssignmentOrphaned,
and the v1-to-v2 receipt migration remain unchanged.

The supplied candidate file was first run against this checkout with its
unmodified barriers and real temporary SQLite fixtures:

```text
PYTHONPATH=/home/pvxlabs/dev/clinx python3 -m pytest -q \
  /tmp/pvx1806-followup-UnHYbt/test_pvx1806_followup_candidates.py
4 failed, 1 passed in 0.16s
```

The two red RC-01 cases held the observation lock and the business lock on
independent connections, advanced the injected clock from T9 to T11 while the
writer was blocked, and observed the protected row incorrectly change from
version 0 to 1. The two red RC-03 cases used the real registration and
heartbeat APIs: the database changed Worker/incarnation versions from `1/0` to
`2/1`, while the registration-only reducer returned `1/0`.

After the corrective changes, the same candidate assertions were green:

```text
PYTHONPATH=/home/pvxlabs/dev/clinx python3 -m pytest -q \
  /tmp/pvx1806-followup-UnHYbt/test_pvx1806_followup_candidates.py
5 passed in 0.14s
```

#### Authorization lock matrix (pre-handoff historical run)

`_transaction()` keeps the initial durable preflight observation, acquires the
business `BEGIN IMMEDIATE` lock, and samples the injected coordinator clock
after that lock is acquired. `now >= expires_at` is rejected by the shared
owner validator for protected mutation and renew. The post-lock sample is
updated in the business transaction on success; on any validation or injected
business rollback it was persisted by an independent safety transaction after
rollback. This historical run established the lock-time boundary; the durable
guard and fail-closed handoff that close its rollback window are recorded in
10.8. The failed business write leaves no state/event/receipt/outbox, while the
safety watermark survives reopen and clock rollback.

The repository matrix covers both lock positions, all three boundaries, and
both protected operations:

```text
2 lock positions x (valid, exact, expired) x (mutation, renew) = 12 cases
valid waits commit; exact/expired waits reject; protected version remains 0
for every rejected case; watermark is retained on an independent connection
```

#### Worker/incarnation replay matrix

Worker registration and replacement events now carry their post-mutation
versions. Heartbeat events carry both post-mutation versions and heartbeat
time in the immutable incarnation stream. `replay_worker_events()` validates
each stream independently, merges related streams by durable global position,
and checks identity, sequence, event type, and exact version increments. The
store convenience method loads only those immutable related event rows; it
does not query current runtime tables to fill a projection.

| Scenario | Durable Worker/incarnation | Replay result |
|---|---:|---:|
| Registration, no heartbeat | `1 / 0` | `1 / 0` exact |
| One heartbeat | `2 / 1` | `2 / 1` exact |
| Three heartbeats | `4 / 3` | `4 / 3` exact |
| Heartbeat then replacement | current `3 / 0`, old `2` superseded | exact current and superseded versions |
| Legacy events without version fields | unavailable | `UNKNOWN` with `NOT_COVERED` provenance |

Single worker-stream input is reported as `view=registration_only`; only the
worker-plus-incarnation entry point claims aggregate equivalence. Unknown or
malformed version evidence fails replay rather than copying a lifecycle or
registration count.

The bounded qualification's original sample remains `200 attempted / 63
effective / 137 no-op`; its transition matrix remains separately accounted as
`10 attempted / 7 effective / 2 no-op / 1 rejected`. The worker follow-up is
reported in a separate `worker_version_matrix` and does not inflate those
historical operation counts.

Follow-up gates:

```ini
AUTHORIZATION_TIME_FRESH_AFTER_LOCK=PASS
EXPIRY_BOUNDARY_UNDER_LOCK_CONTENTION=PASS
REJECTED_FRESH_TIME_SURVIVES_ROLLBACK=PASS
MULTI_COORDINATOR_TIME_ORDERING=PASS
WORKER_VERSION_AFTER_HEARTBEAT=PASS
INCARNATION_VERSION_AFTER_HEARTBEAT=PASS
HEARTBEAT_REPLACEMENT_REPLAY=PASS
LEGACY_REPLAY_UNKNOWN_HONESTY=PASS
RC02_RC04_REGRESSION=PASS
ASSIGNMENT_REPLAY_REGRESSION=PASS
RUNTIME_SCHEMA_COMPATIBILITY=PASS
PVX1805_REGRESSION=PASS
FULL_SUITE=PASS

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

### 10.8 Safety handoff corrective run (46862ba)

This run addressed the remaining RC-01 handoff invariant only. The supplied
candidate harness was first executed against the pinned checkout with real
temporary SQLite databases and deterministic barriers:

```ini
BASE_COMMIT=46862ba2207330a7bf30f4abd6ef8f26626d4987
FINAL_COMMIT=WORKTREE_PENDING
BRANCH=main
```

```text
PYTHONPATH=/home/pvxlabs/dev/clinx python3 -m pytest -q \
  /tmp/pvx1806-handoff-YZEMQn/test_pvx1806_safety_handoff_candidates.py
1 passed, 3 failed in 0.14s
```

The three red cases showed a competing coordinator writing protected version
`0 -> 1` after business rollback, a real SQLite busy failure leaving the old
watermark usable, and a child exiting before the independent safety write. The
normal completion control persisted `T11` and rejected a `T9.5` writer with
`ClockAnomaly`.

After the fix the same four assertions are green:

```text
PYTHONPATH=/home/pvxlabs/dev/clinx python3 -m pytest -q \
  /tmp/pvx1806-handoff-YZEMQn/test_pvx1806_safety_handoff_candidates.py
4 passed in 0.13s
```

The durable protocol is a v3 additive `runtime_safety_handoffs` table keyed by
assignment. It is committed before the protected business lock and records the
preflight observation plus `blocked_until=lease_expires_at`. Success commits
business state/event/receipt/outbox, the post-lock watermark, and guard deletion
in one transaction. Failure rolls back every business row, then commits the
fresh safety observation before deleting the guard. Busy, commit failure, or
process exit leaves the guard present; subsequent authorization returns
`SafetyDecisionPending` (or a preserved safety failure) and cannot use the old
watermark. Explicit `recover_safety_handoff()` requires stop/fence evidence and
the recorded lease bound, so a clock rollback cannot clear protection.

The finite repository matrix covers both original lock positions, valid/exact/
expired boundaries, protected mutation and renewal (12 cases), plus normal
rejection, business rollback, response loss, real safety busy and commit
failure, reopen, and one forked child exit. Assertions inspect protected
value/version, assignment and epoch, clock watermark, guard, receipt/event/
outbox rows, and follow-up write eligibility. Registration, resource admission,
heartbeat, and V1 paths do not share this guard; evidence is intentionally
included because it is an owner-authorized write. Those non-owner paths retain
their existing concurrency/idempotency contracts.

Safety handoff gates:

```ini
AUTHORIZATION_TIME_FRESH_AFTER_LOCK=PASS
NO_REAUTHORIZATION_DURING_SAFETY_HANDOFF=PASS
SAFETY_PERSIST_FAILURE_FAILS_CLOSED=PASS
UNRESOLVED_SAFETY_DECISION_SURVIVES_PROCESS_EXIT=PASS
FAILED_BUSINESS_STATE_ROLLED_BACK=PASS
VALID_COMMAND_PROGRESS=PASS
RESPONSE_LOSS_IDEMPOTENCY=PASS
RUNTIME_SCHEMA_COMPATIBILITY=PASS
RC02_RC03_RC04_REGRESSION=PASS
PVX1805_REGRESSION=PASS
FULL_SUITE=PASS
```

The final local package run was `432 passed, 66 subtests passed`; the bounded
qualification CLI remained `PASS` with `200 attempted`, `63 effective`, and
`137 no-op` operations. The adapted RC-04 candidate (`scan_events(cursor=...)`)
and the follow-up/hand-off candidates all passed; the unadapted RC-04 probe
continues to be rejected because a cross-stream `after_sequence` is ambiguous.

The guard is an internal runtime-control mechanism only. Physical process
fencing, power-loss durability, provider takeover, production migration, and
V2 authority cutover remain outside this qualification.

### 10.9 Assignment-owner guard coverage corrective run (fad9679)

This corrective run closes the remaining RC-01 entry-point gap identified by
the `fad9679719da9a37412c2d3161e886cfd3c97bd8` continuation review. The review
and supplied SPEC are execution constraints and evidence sources; they are not
product files. The review harness was not added to the repository.

```ini
REVIEW_BASE=fad9679719da9a37412c2d3161e886cfd3c97bd8
FINAL_COMMIT=WORKTREE_PENDING
BRANCH=main
CLINX_EXECUTION=BLOCKED_EXISTING_PVX1800_WORKTREE_LEASE
```

The two required red cases were run against an isolated checkout of the
review baseline with real `RuntimeControlStore`, temporary SQLite, independent
connections, and `ManualClock`:

```text
RED_A pending guard bypass: guard_present=True,
  evidence=True, evidence_rows=1 (before=0), current_authority_valid=True
RED_B evidence-first rollback: evidence_failure="business rollback",
  guard_present=False, mutation=True, protected_version=1,
  value={"red":"revived"}
```

`RED_A` demonstrates that a protected mutation's unresolved guard did not
block a new evidence write. `RED_B` demonstrates that evidence as the first
failed owner command left no guard, allowing a reopened coordinator at an
earlier time to update the protected row from version 0 to 1. These are
baseline counterexamples, not post-fix results.

The minimal correction makes the five existing owner-authorized write entries
use one assignment-scoped handoff protocol:

| Entry | Handoff and owner proof | Successful business effect |
|---|---|---|
| `mutate_protected_resource` | Durable token, exact assignment/allocation tuple, post-lock lease check | Protected value/version, event, receipt, outbox |
| `renew_assignment` | Same assignment guard and post-lock `now < expires_at` check | Assignment/allocation lease and versions |
| `release_assignment` | Shared `_finish_assignment` guard-aware owner boundary | Assignment/allocation/attempt release state |
| `revoke_assignment` | Shared `_finish_assignment` guard-aware owner boundary | Assignment/allocation/attempt revoke state |
| `record_attempt_evidence` | Same guard-aware boundary; generated ID remains scoped/idempotent | Evidence, event, receipt, outbox |

The guard is committed before the business `BEGIN IMMEDIATE` lock. A successful
command clears only its own token in the same business transaction as state,
event, receipt, outbox, and the newest watermark. A rejected or failed business
transaction rolls back all business rows; safety completion persists the fresh
observation before clearing the guard. If safety completion is busy, fails, or
the process exits, the guard remains durable and later owner writes fail closed
with `SafetyDecisionPending`. A guard on assignment A does not block an
independently valid assignment B. Missing, wrong, or another transaction's
token cannot pass `_owner_rows`; the token is an internal transaction
capability, not caller authentication.

The repository's deterministic matrix includes five successful entries, five
pending-guard rejection cases, and all 25 prior-entry × follow-up-entry
handoffs. Each rejection checks assignment/allocation, protected value/version,
evidence, event, receipt, outbox, and guard rows. It also covers the original
12 lock-wait cases, response loss, safety busy/commit-failure injection,
clock rollback, explicit recovery bounds, an evidence-first forked child exit,
reopen, receipt replay under a pending guard, independent assignment progress,
and the concurrent same-key case where a guard rejection is followed by an
exact receipt-recovery retry. The child exits after business rollback and
before safety publish; this is process-exit evidence only and does not qualify
power-loss durability or physical process fencing.

```text
PYTHONPATH=. python3 -m pytest -q test_pvx1806_remediation.py
85 passed in 1.79s

PYTHONPATH=. python3 -m pytest -q test_runtime_control.py test_pvx1806_remediation.py
105 passed in 2.58s

PYTHONPATH=. python3 -m pytest -q
471 passed, 66 subtests passed in 8.48s

PYTHONPATH=. python3 -m runtime_control.qualification --seed 1806 --operations 200
qualification=PASS; attempted=200; effective=63; no_op=137;
event_count=145; assignment_streams_replayed=32;
transition_attempted=10; transition_effective=7; transition_no_op=2;
transition_rejected=1; worker_version_replayed=4;
incarnation_version_replayed=2; legacy_missing_versions=UNKNOWN_NOT_COVERED
```

The focused guard tests add no schema migration and preserve runtime schema v3,
receipt/event history, RC-02/RC-03/RC-04, and PVX-1805 compatibility. The
full suite count is the actual current-checkout result; it is not the
historical `432 passed / 66 subtests` baseline and does not double-count the
nine PVX-1805 compatibility tests already included in the V1 subtotal.

Guard-coverage gates:

```ini
MANDATORY_ASSIGNMENT_AUTHORIZATION_BOUNDARY=PASS
ALL_FIVE_OWNER_WRITES_GUARDED=PASS
PENDING_GUARD_REJECTS_NEW_EVIDENCE=PASS
EVIDENCE_FIRST_FAILURE_CANNOT_REVIVE_AUTHORITY=PASS
CROSS_ENTRY_HANDOFF_MATRIX=PASS
TRANSACTION_GUARD_OWNERSHIP=PASS
RECEIPT_AUTHORITY_RESPECTS_PENDING_GUARD=PASS
GENERATED_EVIDENCE_ID_IDEMPOTENCY=PASS
RESPONSE_LOSS_IDEMPOTENCY=PASS
VALID_COMMAND_PROGRESS=PASS
RUNTIME_SCHEMA_COMPATIBILITY=PASS
RC02_RC03_RC04_REGRESSION=PASS
PVX1805_REGRESSION=PASS
FULL_SUITE=PASS
```

The final commit SHA, ordinary push, remote readback, and clean-worktree
state are reported by the enclosing task after documentation and verification
are complete. No production database, provider, manager service, physical
worktree, deployment, or next-phase work was started.
