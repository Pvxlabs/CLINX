# PVX-1812: managed completion delivery repair candidate

## Scope and status

This candidate is based on `1db732a841af906153e2333ece2c85d4450b3daa`.
It repairs completion delivery for explicitly enrolled managed Host executions.
It is not a production deployment, a V2 worker authority cutover, or Rust adoption.
P620 was unreachable during the implementation. No service, production database,
Provider, existing task, or lease was modified by the reviewer.

The original PVX-1812 execution must be recovered on P620 using exact existing
evidence. A new real pilot execution and continuation are still required.
Scripted wire tests below exercise real CLINX classes and real temporary SQLite;
they are not real Codex/Host E2E qualification.

## Implementation

`clinx_completion_handoffs` is an additive local table, not another lifecycle
authority. An immutable execution/task/thread/turn identity is registered after
the accepted turn identity is persisted and before supervision starts. The
supervisor can notify this journal and close; its in-memory event list no longer
owns completion progress. Bounded service/MCP observation consumes enrolled jobs
and calls the existing Finalizer through exact reconciliation. The observer uses
its own connection; it never re-enters the supervisor transport. Process restart
resumes persisted pending deliveries, without scanning or reclaiming other leases.

The observed event is a wakeup, not permission to fabricate a terminal result.
Exact provider history is re-read. A new observation connection may have a new
generation; durable execution-owned identity, not a vanished socket generation,
is the basis for restart recovery. Missing/mismatched identifiers fail closed.

The dispatcher persists RUNNING/turn identity before starting supervision. The
public prepared-start path and all exact finalization paths share a local
cross-thread/cross-process per-execution lock, so late start bookkeeping cannot
overwrite a fast completed result. The OS releases process locks on process exit.
Locks are serialization, never substitutes for ownership validation; lock files
must not be deleted while the database is in use.

Existing-result recovery can finish a terminal transition/lease cleanup left
incomplete by a process exit. Lease release checks the exact task and execution.
After an accepted turn, failure to register a handoff retains an uncertain lease
instead of misclassifying the error as pre-turn failure. This narrow pre-enrollment
failure still requires explicit exact recovery; no automatic bulk enrollment or
unknown-side-effect retry is added.

Automatic completion and the explicit recovery command use scoped reconciliation
without the legacy global stale-lease sweep. A new observation connection failing
to read is observation uncertainty, not proof that the original Provider stopped.
The legacy default reconciliation mode is retained outside this repair's scope.

## Maintenance entry

The operator-only local command is:

```text
python3 bridge.py --config <actual-runtime-config> recover-execution <exact-execution-ref>
```

It neither starts a Provider turn nor constructs HostExecutor (whose legacy
constructor performs a host reconciliation sweep). It uses existing exact
provider/registry evidence and the Finalizer, not manual SQL result insertion.
The CLI does not load a Linear client: its successful local recovery does not
claim that external projection was written. Normal configured runtime projection
or a separately verified projection retry remains necessary. Capture the
runtime database/config identity before invoking any write-capable recovery.

## Host contract boundary

New dynamic tool schemas expose only this execution policy's operation classes
and capabilities. Handler validation rejects malformed or unapproved inputs
before executing a Host operation. The managed prompt identifies the Provider as
an already-approved execution worker, not an outer CLINX operator.

This hardens the dynamic Host path, but does **not** establish per-Provider MCP
catalog isolation. Hiding/disabling nested CLINX control-plane tools in the actual
Provider configuration still requires deployment-environment verification. Global
approval policy, capability grants, and the outer operator MCP catalog are not
weakened or silently changed. Old loaded Provider thread schemas are not rewritten.

## Evidence and limitations

The baseline two-case completion test fails without an explicit status query;
the same tests pass with this candidate. Repository-resident tests cover normal
and early completion, actual public prepared start, a new execution and
continuation, restart of persisted deliveries, real subprocess exit during
finalization, exact lease preservation on registration failure, duplicate
concurrent delivery, wrong identities, observer disconnect, Linear failure,
and scope isolation. Raw outputs and tested commit/tree are delivered separately.

In the reviewer Python 3.13 environment, the existing tunnel tests require
`/usr/bin/python3.12`, which is absent; they are not marked as passing there.
Full Python and Rust checks must be recorded by the isolated Linux CI run before
this candidate is described as repository-qualified. Do not reuse historical
suite counts as current results.

Still NOT_RUN on P620: deployed process identity, original PVX-1812 recovery,
real Provider/Host new execution, real continuation, and actual nested-control-plane
catalog restriction. There is no production-readiness or full runtime-qualification
claim. Main integration and deployment remain separate from a candidate branch.
