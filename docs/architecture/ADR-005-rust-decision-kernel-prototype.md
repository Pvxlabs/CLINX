# ADR-005: Rust Decision Kernel Prototype

- **Status:** Accepted for the PVX-1808 prototype qualification scope
- **Date:** 2026-09-11
- **Related decisions:** [ADR-001](./ADR-001-v2-architecture.md), [ADR-002](./ADR-002-shadow-event-ledger.md), [ADR-003](./ADR-003-runtime-worker-ownership.md), [ADR-004](./ADR-004-provider-adapter-contract.md)
- **Default:** explicit qualification only; `RUST_PRODUCTION_RUNTIME=NOT_IMPLEMENTED`

## Scope and authority

PVX-1808 is a local, isolated-development prototype in
`/home/pvxlabs/dev/clinx-pvx1807-remediation`, based on
`9a21bf19399e9c305f7fb1487feadc73a11da047` and branch
`pvx-1808-kernel-prototype`. The direct operator instruction authorizes the
implementation, qualification, ordinary commit, and feature-branch push in
that checkout. It does not authorize a CLINX managed execution grant, a
production change, a service restart, a provider takeover, a database
migration, a `main` push, a merge, or a change to the PVX-1800 task or lease.

The prototype has two capabilities only:

1. `evaluate_ownership`: a pure, non-authoritative ownership snapshot
   decision.
2. `replay_assignment`: a pure replay of the `RUNTIME_WORKER_V1` assignment
   event slice.

The Rust result is evidence for evaluation. `ALLOW_CANDIDATE` is not a
capability token, a lease, a guard, or permission to perform a real write.
The existing Python runtime-control transaction, clock sampling, CAS, guard,
and write-point fencing remain authoritative.

## Decision

Keep a small Rust library and two local CLI binaries behind a versioned NDJSON
stdio protocol. Python remains the reference and qualification driver. The
Rust process receives a complete self-contained request, performs no external
I/O, and returns a correlated `NON_AUTHORITATIVE` result or a stable error.
The evaluator is never imported by the normal V1, bridge, MCP, Finalizer,
Provider adapter, TaskRegistry, or worker paths.

The protocol is `CLINX_KERNEL_V1`. It is represented by the checked-in schema
at `fixtures/kernel_v1/protocol.schema.json`, Python validation in
`kernel_lab/protocol.py`, and Rust validation in `kernel/src/lib.rs`.

## Contract matrix

| Contract field or rule | Existing source responsibility | Experiment interface | Positive/negative evidence | Python entry | Rust entry | Explicitly not covered |
| --- | --- | --- | --- | --- | --- | --- |
| Execution and attempt identity | `runtime_control.models`, `store.py`, and the ownership checks in the runtime-control boundary | `snapshot.execution.*`, `snapshot.attempt.*`, `request.execution_id`, `request.attempt_id` | Ownership golden cases for matching and cross-execution identities; SQLite traces | `evaluate_ownership_reference` | `evaluate_ownership` | Creating, retrying, or terminalizing executions and attempts |
| Worker and incarnation identity | `runtime_control.store.py` worker/incarnation records | Worker identity, lifecycle, and current incarnation fields | Wrong worker, stale incarnation, inactive worker/incarnation cases | `evaluate_ownership_reference` | `evaluate_ownership` | Worker registration, heartbeat, or process supervision |
| Assignment and allocation identity | `runtime_control.store.py` assignment/allocation records | Assignment/allocation IDs, owner tuple, resource key, and lifecycle | Wrong assignment/resource, released/quarantined allocation, same-task multi-execution isolation | `evaluate_ownership_reference` | `evaluate_ownership` | Issuing or releasing a live guard or lease |
| Epoch and expected versions | Runtime-control fencing and CAS checks | Assignment, allocation, resource epochs and expected versions | Old epoch, assignment/allocation/resource version mismatch | `evaluate_ownership_reference` | `evaluate_ownership` | Replacing the authoritative transaction or CAS |
| Trusted time and expiry | `runtime_control.clock.py` and store-side expiry handling | Coordinator-trusted `now` and watermark, exact UTC microseconds | `now < expires_at`, equality, and after-expiry cases | `evaluate_ownership_reference` | `evaluate_ownership` | Reading the system clock or advancing a safety watermark |
| Safety handoff | Runtime-control safety handoff state | Verified handoff plus exact owner tuple and epoch | Pending, absent, and mismatched handoff cases | `evaluate_ownership_reference` | `evaluate_ownership` | Performing physical process fencing or recovery |
| Receipt semantics | Runtime-control receipt/idempotency boundary | Receipt is explicitly non-authoritative input context | Old success receipt cannot restore current ownership | `evaluate_ownership_reference` | `evaluate_ownership` | Replaying commands or mutating receipts |
| Assignment event family/schema | `runtime_control.replay.py`, `ADR-003` | `event_family=RUNTIME_WORKER_V1`, `schema_version=1` | Unknown family/schema and malformed envelopes fail closed | `replay_assignment_reference` | `replay_assignment` | Other runtime event families and full event-engine replay |
| Assignment stream order | Runtime event sequence validation | Assignment stream ID and contiguous sequence | Version gaps, mixed streams, duplicate fields, bad order | `replay_assignment_reference` | `replay_assignment` | A business total order across independent streams |
| Payload integrity and timestamps | Runtime event store and existing canonical payload hash | Canonical JSON payload hash, occurred/recorded UTC timestamps | Bad hash, invalid timestamp, recorded-before-occurred | `replay_assignment_reference` | `replay_assignment` | Rehashing or rewriting historical event bytes |
| Assignment transitions | `runtime_control.replay.py` state reducer | Seven event types and state snapshots | Current golden, legacy traces, negative fixtures, SQLite traces | `replay_assignment_reference` | `replay_assignment` | Worker-associated streams, Provider wire events, projections |
| Process and request bounds | New qualification boundary, not runtime authority | Bounded NDJSON request/response and child process | 19 conformance tests cover malformed input, limits, correlation, exit, timeout, and restart | `KernelSession` / `run_once` | `clinx-kernel-eval` | A daemon, service, queue, FFI, gRPC, or exactly-once network protocol |

## Ownership decision contract

The decision function validates all required fields before comparing any
identity. Missing or `UNKNOWN` evidence returns `INSUFFICIENT_EVIDENCE` with
`EVIDENCE_INCOMPLETE`; it is not repaired from a task latest result, current
database row, receipt, or newly created execution. Once fields are complete,
the deterministic priority is:

1. execution identity and execution lifecycle;
2. attempt-to-execution identity and attempt lifecycle;
3. worker identity and worker lifecycle;
4. current worker incarnation and incarnation identity/lifecycle;
5. assignment identity, lifecycle, and resource epoch;
6. allocation identity, lifecycle, and resource epoch;
7. protected resource identity, epoch, and expected versions;
8. lease equality and trusted-time boundary;
9. safety-handoff state and owner tuple.

The exact equality boundary is intentional: `now == expires_at` is rejected.
The function reads no clock, database, receipt, provider, or filesystem and
does not create, clean, renew, release, or fence anything.

## Assignment replay contract

The assignment slice preserves the transition table already defined by
ADR-003:

| Event | Legal prior state | Assignment | Allocation | Attempt | Recovery |
| --- | --- | --- | --- | --- | --- |
| `AssignmentGranted` | none | `ACTIVE` | `ACTIVE` | `ASSIGNED` | none |
| `AssignmentRenewed` | `ACTIVE` | `ACTIVE`, new lease/version | `ACTIVE`, new lease/version | unchanged | none |
| `AssignmentReleased` | `ACTIVE` | `RELEASED` | `RELEASED` | `RELEASED` | none |
| `AssignmentRevoked` | `ACTIVE` | `REVOKED` | `RELEASED` | `PENDING` | none |
| `AssignmentExpired` | `ACTIVE` | `EXPIRED` | `QUARANTINED` | `ASSIGNED` | `PENDING` |
| `AssignmentOrphaned` | `ACTIVE` | `ORPHANED` | `QUARANTINED` | `ASSIGNED` | `PENDING` |
| `AssignmentRecovered` | `EXPIRED` or `ORPHANED` | `RECOVERED` | `RELEASED` | `PENDING` | `DONE` |

Each prefix is checked, not only the final state. The reducer validates the
family, schema, assignment stream, contiguous sequence, canonical payload
hash, timestamps, identity, versions, dependent lifecycles, and legal
predecessor. Legacy legal events retain the existing explicit upcast behavior;
missing information remains unknown rather than being inferred. Invalid
transitions and unsupported schemas fail closed.

## Language and process boundary

Python owns the reference adapter and subprocess lifecycle. Rust owns only
typed deserialization, the two pure operations, and structured protocol
responses. The boundary uses finite JSON integers, exact strings, explicit
`UNKNOWN` handling, and canonical UTC timestamps with six fractional digits.
It rejects duplicate JSON object fields, unknown envelope fields, unsupported
protocol versions, unknown operations, non-finite JSON, out-of-range integers,
oversized frames, and oversized event input.

The limits are deliberately conservative and checked in both directions:

```text
MAX_FRAME_BYTES=1048576
MAX_RESPONSE_BYTES=1048576
MAX_STDERR_BYTES=65536
MAX_EVENTS=1024
MAX_JSON_DEPTH=32
MAX_REQUESTS_PER_PROCESS=4096
MAX_SAFE_INTEGER=9007199254740991
stdout=NDJSON protocol only
stderr=bounded diagnostics only
```

`request_id` is echoed exactly. A child timeout, early exit, truncated
newline, wrong response ID, oversized response, or oversized stderr is a
qualification error. Restarting and replaying the same self-contained input
is allowed for pure computation, but does not prove recovery or exactly-once
behavior for a real provider or external side effect.

## Qualification evidence

The qualification report in
[`pvx-1808-rust-kernel-qualification.md`](./pvx-1808-rust-kernel-qualification.md)
records the actual commands and results. The evidence has three independent
layers:

1. Human-authored golden and negative expectations in
   `fixtures/kernel_v1/`.
2. Python reference versus the actual release Rust binary, comparing the
   complete semantic result and every replay prefix.
3. Real temporary SQLite `RuntimeControlStore` traces, read back through its
   public APIs and compared against both reducers and persisted state.

The run used seeds `1808`, `18081`, `18082`, and `18083`, with 256 attempted
operations per seed. It retained attempted/effective/rejected/no-op counts
and assignment-stream identity rather than treating generated traces as a
capacity test. Protocol failures were exercised through real subprocesses;
no production provider, Host executor, service, or management process was
started.

## Migration boundary and non-goals

The decision is extraction, not cutover. The following remain outside this
ADR:

- authoritative live ownership, leases, guards, CAS, and write-point fencing;
- process supervision, crash recovery, and physical old-process fencing;
- Provider adapter live E2E, provider takeover, and canonical provider wire;
- Worker runtime, scheduler, Finalizer, MCP/Linear projections, or new daemon;
- production schema migration, deployment, service restart, or observability;
- cross-process exactly-once behavior, production throughput, P99, capacity,
  long-run stability, or multi-machine consistency;
- automatic adoption by any default import or runtime path.

Fixed boundary values for this prototype are:

```text
V1_LIVE_AUTHORITY=ON
RUST_PROTOTYPE_SCOPE=NON_AUTHORITATIVE_DECISION_AND_REPLAY
RUST_PRODUCTION_RUNTIME=NOT_IMPLEMENTED
RUST_AUTHORITY_CUTOVER=NOT_PERFORMED
PROVIDER_ADAPTER_LIVE_DEFAULT=OFF
WORKER_RUNTIME_DEFAULT=OFF
SHADOW_DEFAULT=OFF
PUBLIC_MCP_CONTRACT=UNCHANGED
PRODUCTION_DB_MIGRATION=NOT_PERFORMED
PRODUCTION_DEPLOY=NOT_PERFORMED
SERVICE_RESTART=NOT_PERFORMED
REAL_PROVIDER_TAKEOVER=NOT_RUN
CANONICAL_PROVIDER_E2E=NOT_RUN
PHYSICAL_PROCESS_FENCING=NOT_QUALIFIED
CROSS_PROCESS_TOOL_EXACTLY_ONCE=NOT_QUALIFIED
TRANSACTION_ATOMICITY_BY_RUST=NOT_QUALIFIED
MAIN_BRANCH_PUSH=NOT_PERFORMED
ORIGINAL_CHECKOUT_MUTATION=NOT_PERFORMED
HISTORICAL_TASK_MUTATION=NOT_PERFORMED
NEXT_PHASE_STARTED=NO
```

## Recommendation

`ADOPTION_RECOMMENDATION=CONTINUE_EVALUATION` is appropriate for the bounded
prototype because the contract is versioned, both operations have independent
golden and SQLite-backed differential evidence, and the existing regression
suite remains green. This is not approval for production migration. Any
future adoption decision still requires separate qualification of the live
transaction boundary, process fencing, supervision and restart behavior,
provider E2E, deployment/observability, load, and maintenance cost.
