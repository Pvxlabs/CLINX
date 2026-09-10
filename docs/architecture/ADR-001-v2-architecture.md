# ADR-001: CLINX V2 Architecture Baseline

- **Status:** Accepted as architecture baseline
- **Date:** 2026-09-10
- **Decision owners:** CLINX maintainers
- **Scope:** Architecture and migration direction only
- **Related review:** [CLINX V2 Architecture Review](./v2-review.md)

## 1. Decision

CLINX will evolve into an:

```text
AI Engineering Control Plane
```

It will not remain merely a:

```text
coding agent dispatcher
```

The V2 architecture will preserve CLINX V1's identity, policy, evidence, host-execution, finalization, and public-boundary safeguards while changing the durable unit of execution ownership.

The core lifecycle will be modeled as:

```text
Project / Workspace
        |
       Task
        |
    Execution
        |
      Attempt
        |
ProviderSession + RuntimeWorker + ResourceAllocation
        |
      Event
        |
   Projection
```

The primary distinctions are:

- A `Task` is durable human intent and engineering lifecycle.
- An `Execution` is one requested run of a task.
- An `Attempt` is one concrete provider and worker realization of an execution.
- A `ProviderSession` contains provider-specific continuity but is not execution identity.
- A `RuntimeWorker` owns a live attempt only while holding current, renewable, fenced authority.
- An `Event` is an immutable lifecycle fact and the durable history of authoritative state transitions.
- A `Projection` is a rebuildable view and never command or lifecycle authority.

This decision addresses the V1 limitations that prevent reliable long-running control-plane operation:

- task-scoped execution singleton;
- provider session coupled to task identity;
- no durable event history;
- no durable worker ownership, heartbeat, or fencing;
- recovery dependent on startup, status reads, or another caller action.

## 2. Domain Model

### Project

| Attribute | Decision |
|---|---|
| **Responsibility** | Defines stable engineering-system identity, repository or product scope, default execution policy, allowed providers, and project-level capacity defaults. |
| **Ownership** | Owned by the project/catalog authority. It owns project configuration, not active runtime work. |
| **Lifecycle authority** | Project administration commands create, activate, suspend, or retire a project. Execution workers cannot mutate project lifecycle. |
| **Persistence requirement** | Durable. Identity, policy versions, provider constraints, and lifecycle status must survive process restart and remain auditable. |

### Workspace

| Attribute | Decision |
|---|---|
| **Responsibility** | Represents a concrete execution scope such as a host, worktree, checkout, runtime namespace, or bounded capacity domain. Defines canonical resource identity and allocation constraints. |
| **Ownership** | Owned by the workspace/resource authority. It does not own human intent or execution outcome. |
| **Lifecycle authority** | Workspace administration and resource coordination control registration, availability, draining, and retirement. |
| **Persistence requirement** | Durable. Canonical paths, host bindings, capabilities, capacity, policy references, and availability state must be persisted. |

### Task

| Attribute | Decision |
|---|---|
| **Responsibility** | Holds human intent, engineering objective, project/workspace context, and long-lived engineering lifecycle. A task may have many executions. |
| **Ownership** | Owned by the task aggregate and command service. It does not own provider threads, provider turns, workers, host processes, or worktree leases. |
| **Lifecycle authority** | Human or authorized command actions control task lifecycle. Execution outcomes may inform task state through explicit task transitions but cannot silently redefine task intent. |
| **Persistence requirement** | Durable. Intent, lifecycle state, actor identity, relationships, and transition versions must survive all runtime failures. |

### Execution

| Attribute | Decision |
|---|---|
| **Responsibility** | Represents one requested run of a task. Owns the immutable request snapshot, route constraints, execution policy, deadline, admission state, attempt strategy, and terminal outcome. |
| **Ownership** | Owned by the execution aggregate. It may coordinate multiple attempts but does not own provider protocol details or live worker processes. |
| **Lifecycle authority** | The command service may request or cancel an execution; the scheduler may admit it; the finalizer is the sole authority for evidence-based terminal outcome. |
| **Persistence requirement** | Durable. Request, policy, route, state version, attempt relationships, final result, and terminal evidence references must be persisted. |

### Attempt

| Attribute | Decision |
|---|---|
| **Responsibility** | Represents one concrete realization of an execution using one provider session and one runtime-worker assignment. Owns retry identity, assignment identity, provider correlation, attempt progress, and attempt outcome. |
| **Ownership** | Created by the scheduler and live-owned by exactly one fenced runtime-worker assignment at a time. |
| **Lifecycle authority** | The scheduler creates, retries, supersedes, or cancels attempts according to execution policy. A current fenced worker may advance live attempt state. The execution finalizer decides how attempt evidence affects the execution outcome. |
| **Persistence requirement** | Durable. Attempt number, provider session, worker assignment, fencing epoch, progress cursor, evidence references, and terminal state must be persisted. |

### ProviderSession

| Attribute | Decision |
|---|---|
| **Responsibility** | Encapsulates provider-specific state such as session, thread, turn, conversation, resume token, capability snapshot, provider version, and event cursor. |
| **Ownership** | Owned by the provider adapter. Core services reference it through opaque CLINX identifiers. |
| **Lifecycle authority** | The provider adapter creates, binds, resumes, pauses, migrates, marks unavailable, and closes sessions under an attempt command. |
| **Persistence requirement** | Durable. Provider continuity and correlation metadata must survive control-plane and worker restarts, with sensitive fields restricted from public projections. |

### RuntimeWorker

| Attribute | Decision |
|---|---|
| **Responsibility** | Represents a live execution worker and its capabilities, host binding, heartbeat, assignments, and ownership epochs. It supervises provider and bounded host activity for assigned attempts. |
| **Ownership** | Owned by the worker registry and scheduler. A worker owns live execution only through a current assignment lease. |
| **Lifecycle authority** | The worker registry controls registration, health, draining, offline state, and epoch changes. Workers may renew their own heartbeat and current assignments but cannot self-grant stale authority. |
| **Persistence requirement** | Durable identity and assignment state; renewable heartbeat and lease state; monotonically increasing fencing epoch. Process-local state alone is insufficient. |

### ResourceAllocation

| Attribute | Decision |
|---|---|
| **Responsibility** | Represents exclusive or capacity-bounded access to a worktree, host slot, provider slot, runtime namespace, or other constrained resource. |
| **Ownership** | Owned by the resource coordinator and assigned to an attempt. It is not an implicit side effect of task state. |
| **Lifecycle authority** | The resource coordinator grants, renews, fences, expires, and releases allocations. Runtime mutations must present the current allocation epoch. |
| **Persistence requirement** | Durable. Resource key, owner attempt, worker, epoch, expiry, renewals, state, release reason, and evidence reference must be persisted. |

### Event

| Attribute | Decision |
|---|---|
| **Responsibility** | Records immutable lifecycle facts, causal order, actor identity, correlation, idempotency, and schema version. Events provide the authoritative transition history. |
| **Ownership** | Owned by the event store. Producers may append only events authorized by the relevant aggregate version and, for live work, the current fencing token. |
| **Lifecycle authority** | Events are append-only. They are never edited into a different fact. Corrections are expressed as new events. Aggregate transition rules decide which events are valid. |
| **Persistence requirement** | Durable and replayable. Event ID, aggregate identity/version, type, timestamps, actor, causation/correlation IDs, idempotency key, payload version, and integrity metadata must be retained. |

### Projection

| Attribute | Decision |
|---|---|
| **Responsibility** | Provides rebuildable read models for task status, execution status, attempts, operator views, MCP responses, Linear delivery state, metrics, and search. |
| **Ownership** | Owned by projection workers or read-model services. A projection is derived from authoritative events and aggregate records. |
| **Lifecycle authority** | Projection workers may advance checkpoints, retry failed application, or declare lag/degradation. A projection cannot complete, fail, cancel, reopen, or assign work. |
| **Persistence requirement** | Durable for efficient reads and restartable consumption, but disposable and rebuildable from the event history. Projection checkpoints and failures must be persisted. |

## 3. Ownership Rules

### Task

`Task` owns:

- human intent;
- the engineering objective;
- project and workspace context;
- the long-lived engineering lifecycle.

`Task` does not own:

- a provider thread, session, conversation, or turn;
- a runtime worker;
- a host process;
- a resource allocation;
- a single current execution as an identity shortcut.

### Execution

`Execution` owns:

- one requested run;
- the immutable request snapshot;
- execution and retry policy;
- route and authority constraints;
- admission and cancellation intent;
- selection of winning or superseded attempts;
- the terminal outcome.

The finalizer remains the terminal decision authority. Provider completion alone, worker exit alone, or projection state alone is insufficient to finalize an execution.

### Attempt

`Attempt` owns:

- one provider realization of an execution;
- one retry or failover identity;
- provider correlation for that realization;
- worker assignment and fencing epoch;
- attempt-local progress and evidence references;
- attempt outcome.

An execution may have multiple sequential attempts and, only when policy explicitly permits it, multiple concurrent attempts. Attempt terminal state does not automatically determine execution terminal state.

### ProviderSession

`ProviderSession` owns provider-specific continuity and protocol state.

It must not become execution identity. Replacing, migrating, resuming, or closing a provider session must not replace the `Task`, `Execution`, or `Attempt` identity it supports.

Provider-specific fields must remain behind adapters and restricted persistence boundaries. The core state machine must not require every provider to implement Codex-style thread/session/turn semantics.

### RuntimeWorker

`RuntimeWorker` owns:

- live supervision of assigned attempts;
- heartbeat publication;
- assignment renewal;
- the current fencing epoch presented with mutations;
- provider and host-process observation for that assignment.

Worker authority is leased, renewable, and revocable. After lease expiry or reassignment, stale worker writes must fail closed even if the old process remains alive.

### ResourceAllocation

`ResourceAllocation` owns temporary access authority for constrained runtime resources. It binds resource identity to an attempt, worker, lease interval, and fencing epoch.

Allocation release is explicit and auditable. Terminal execution state should request release, but release completion is tracked independently so resource cleanup can be retried without changing the terminal decision.

### Event

`Event` owns lifecycle history.

All authoritative transitions must produce an event with aggregate version and causal metadata. Provider notifications must be durably accepted and deduplicated before normalization. Late or duplicated events remain evidence but cannot overwrite newer authority without a valid transition command.

### Projection

`Projection` must never become authority.

In particular:

- MCP status is a bounded projection, not execution ownership;
- Linear is a downstream audit projection, not terminal authority;
- operator dashboards are observations, not state machines;
- projection lag must be visible and must not be interpreted as execution failure or success;
- projection rebuild or outage must not alter command acceptance or terminal truth.

## 4. Lifecycle Authority

The target command and state path is:

```text
Authorized command
      |
Task / Execution aggregate validation
      |
Scheduler admission and Attempt creation
      |
ResourceAllocation + RuntimeWorker assignment
      |
ProviderSession and bounded host execution
      |
Durable normalized evidence Events
      |
Finalizer terminal decision
      |
Resource release workflow
      |
Rebuildable projections and downstream delivery
```

Authority is divided as follows:

| Action | Authority |
|---|---|
| Create or change human intent | Task command service |
| Request or cancel a run | Execution command service |
| Admit work and create an attempt | Scheduler |
| Allocate constrained resources | Resource coordinator |
| Advance a live attempt | Current fenced RuntimeWorker |
| Manage provider continuity | Provider adapter |
| Decide execution terminal outcome | Finalizer using normalized provider and host evidence |
| Release runtime resources | Resource coordinator and cleanup workflow |
| Publish MCP, operator, metrics, or Linear state | Projection workers |

State transitions must be deterministic and idempotent:

1. Validate the command against the current aggregate version.
2. Validate actor authority and any required assignment/allocation fence.
3. Append the event and update the aggregate current-state record atomically.
4. Place downstream work in an outbox in the same transaction.
5. Let consumers apply projections using durable cursors and idempotency keys.
6. Reject stale, duplicated, invalidly ordered, or incorrectly correlated mutations.

## 5. Persistence Baseline

V2 persistence must distinguish authoritative history, current aggregate state, immutable evidence, and rebuildable read models.

The baseline logical stores are:

```text
projects
workspaces
tasks
executions
attempts
provider_sessions
runtime_workers
resource_allocations

events
event_inbox
event_outbox
aggregate_versions

provider_evidence
host_evidence
execution_results

projections
projection_checkpoints
projection_failures
```

Requirements:

- Aggregate transitions use optimistic aggregate versions.
- Worker and resource mutations require current fencing epochs.
- Provider observations enter a durable inbox before normalization.
- External deliveries use a transactional outbox.
- Immutable evidence remains separate from mutable projections.
- Provider secrets and private protocol identifiers remain inaccessible to public MCP projections.
- Current-state tables may optimize reads but must be reconcilable with the event history.
- SQLite may remain an initial single-node implementation only behind storage contracts that permit a later move to Postgres and a durable queue.

## 6. Migration Principles

### Preserve

The migration must preserve:

- the V1 public MCP contract and its opaque, bounded identity surface;
- the finalizer evidence model and evidence-first terminal decisions;
- exact task, execution, provider-correlation, route, policy, and host-evidence boundaries;
- bounded host execution and fail-closed authority checks;
- Linear as an idempotent downstream audit projection;
- existing qualification requirements and evidence standards.

### Avoid

The migration must avoid:

- a big bang rewrite;
- replacing V1 before V2 replay and compatibility are proven;
- a Rust rewrite before the domain model and state contracts stabilize;
- provider-specific coupling in core entities or transitions;
- using projections as command or lifecycle authority;
- treating process-local ownership as durable recovery state;
- weakening qualification because a new implementation language or storage engine is introduced.

### Incremental Boundary

Migration will proceed through compatibility layers:

1. Freeze and document V1 public, identity, finalizer, and qualification contracts.
2. Add V2 identifiers and shadow events while V1 remains authoritative.
3. Prove event replay reconstructs existing task and execution views.
4. Separate execution from attempt and provider session persistence.
5. Introduce durable worker assignment, heartbeat, allocations, and fencing.
6. Extract provider adapters and qualify at least one non-Codex test adapter against the same contract.
7. Move MCP, operator, and Linear reads to V2 projections behind the existing public contract.
8. Retire V1 singleton assumptions only after replay, recovery, fencing, late-event, and projection-outage qualification passes.

During migration, divergence between V1 state and V2 shadow records must fail visibly. It must not be silently reconciled by choosing the more favorable state.

## 7. Future Runtime Direction

The intended long-term layering is:

```text
Python / TypeScript Control Plane

              |

             RPC

              |

      Rust Runtime Kernel
```

The Python/TypeScript control plane is expected to own human-facing commands, policy authoring, project/workspace administration, provider integration orchestration, MCP compatibility, operator interfaces, and projections.

Candidate Rust runtime-kernel responsibilities are:

- execution state machine;
- scheduler;
- worker supervisor;
- event engine.

This ADR does not commit CLINX to a Rust production migration. Rust adoption requires prototype validation of:

- deterministic transition behavior and idempotency;
- crash and restart recovery;
- worker fencing and stale-write rejection;
- provider adapter RPC semantics;
- event replay compatibility;
- operational observability and debugging;
- deployment and maintenance cost relative to the existing Python runtime.

The prototype must validate a stable language-neutral contract. Production migration is a later ADR and requires evidence that the kernel improves correctness or operability without weakening existing safety and qualification boundaries.

## 8. Consequences

### Positive

- Multiple executions can belong to one task without identity collision.
- Retries, failover, and bounded concurrent attempts gain explicit identities and policies.
- Provider continuity can be replaced or recovered independently from task and execution identity.
- Worker loss becomes a durable lease and fencing problem rather than an inference from process-local state.
- Lifecycle history becomes replayable and auditable.
- MCP, Linear, metrics, and operator views can fail or lag without becoming execution authority.
- A provider-neutral core can support multiple agent providers through adapters.

### Costs and Risks

- Dual-write and replay validation increase migration complexity.
- Event schemas require versioning and long-term compatibility discipline.
- Worker leases, fencing, inboxes, and outboxes introduce more operational state.
- Multiple attempts require explicit winner, cancellation, evidence, and cost policies.
- SQLite may become a throughput or availability constraint before a multi-node store is introduced.
- A language boundary to a future Rust kernel adds RPC, deployment, and observability complexity and must be justified by prototype evidence.

## 9. Non-Goals

This ADR does **not**:

- replace CLINX V1 immediately;
- rewrite all CLINX code;
- introduce a Rust production runtime;
- change existing MCP contracts;
- change existing qualification requirements;
- modify current runtime behavior;
- select a final RPC protocol, database, queue, or deployment topology;
- authorize concurrent execution before allocation, fencing, finalization, and recovery semantics are qualified.

## 10. Acceptance Record

```ini
ADR_CREATED=YES
DOMAIN_MODEL_DEFINED=YES
OWNERSHIP_RULES_DEFINED=YES
MIGRATION_BOUNDARY_DEFINED=YES
NO_RUNTIME_CHANGE=YES
```

The repository's existing test suite remains the regression baseline:

```text
297 passed
48 subtests passed
```

Passing the existing suite confirms that this documentation-only change does not alter checked-in runtime behavior. It does not by itself qualify the proposed V2 scheduler, event store, worker recovery, provider adapters, or Rust runtime candidates.
