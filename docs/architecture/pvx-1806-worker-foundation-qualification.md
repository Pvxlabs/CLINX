# PVX-1806 RuntimeWorker Foundation Qualification

## 1. Scope and Baseline

```ini
ISSUE=PVX-1806
REPOSITORY=/home/pvxlabs/dev/clinx
BASE_COMMIT=5360ad5ea5f8b0ae4291ca1b46ef077dc4cf5b34
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

Current checkout results after the implementation are:

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

Not qualified here: physical process or shell fencing, real provider failover,
live worktree takeover, distributed SQLite, power-loss durability, scheduler
fairness, quotas, long-term retention, production throughput, Rust, RPC,
systemd service operation, Linear delivery, V2 terminal finalization, and any
PVX-1807 work.
