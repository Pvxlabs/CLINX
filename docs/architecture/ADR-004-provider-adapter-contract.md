# ADR-004: Provider Adapter Contract and Codex Conformance

- **Status:** Accepted for the PVX-1807 development qualification scope
- **Date:** 2026-09-10
- **Related decisions:** [ADR-001](./ADR-001-v2-architecture.md), [ADR-002](./ADR-002-shadow-event-ledger.md), [ADR-003](./ADR-003-runtime-worker-ownership.md)
- **Default:** explicit construction only; `PROVIDER_ADAPTER_LIVE_DEFAULT=OFF`

## Decision

CLINX has an internal, provider-neutral observation boundary in
`provider_adapters/`. It isolates provider protocol identities from the
existing Task, Execution, Attempt, lease, and Finalizer authorities. The first
implementation is `CodexProviderAdapter`, which uses the existing
`CodexAppServerClient` and its configured transport. It does not replace the
V1 bridge, MCP server, TaskRegistry, Finalizer, or runtime-control store.

The package is not imported by the V1 dispatch path. A caller must explicitly
construct an adapter and provide a transport (or a transport factory). No
service configuration, worker startup, database migration, or live provider
takeover is implied by importing or testing this package.

## Contract

The contract is expressed by `ProviderAdapter` and the serializable values in
`contracts.py`:

| Value | Required identity/semantics |
| --- | --- |
| `Capabilities` | Each named capability is `supported`, `unsupported`, or `unknown`, with evidence source, provider version, and bounded details. Missing capability lookup returns `unknown`; it never silently falls back. |
| `ProviderSessionRef` | Stable CLINX session id, provider id, opaque provider handle, and connection generation. The provider handle is continuity data, not an authorization credential. |
| `OperationContext` | Exact `execution_id`, `attempt_id`, session id, operation id, connection generation, and optional already-validated assignment id. Latest task result, timestamps, and transcript text are not correlation mechanisms. |
| `NormalizedEvent` | Adapter receipt id, provider source, generation, event kind/type, exact/unknown/mismatch/stale correlation, local sequence or provider cursor, terminal observation, cancellation observation, bounded error/evidence fields, duplicate marker, and bounded JSON payload. |
| `Outcome` / adapter errors | `accepted`, `observed`, `unsupported`, `protocol_error`, `transport_loss`, `timeout`, `cancel_requested`, `cancel_confirmed`, `correlation_failure`, `side_effect_outcome_unknown`, and `unknown_outcome` remain distinct. |

`NormalizedEvent.terminal_observed` is provider evidence only. It cannot write
a terminal Task/Execution state, release a V1 worktree lease, mutate
runtime-control ownership, or update Linear.

## Existing Entry Mapping

| Existing caller or side effect | Existing method | Adapter operation | State owner and compatibility evidence |
| --- | --- | --- | --- |
| V1 bridge initialization | `CodexAppServerClient.initialize` | Adapter connection initialization | Codex client/transport; `test_bridge.py` and adapter scripted trace |
| New durable provider thread | `thread_start` | `create_session` | ProviderSessionRef only; no CLINX execution mutation |
| Existing thread attachment | `thread_resume` | `resume_session` | Exact opaque handle and session generation; bridge migration/resume tests remain unchanged |
| Managed prompt | `turn_start` | `start_turn` / `continue_turn` | Attempt/Execution owner remains the caller; adapter returns accepted provider reference |
| Dynamic host tool | `configure_dynamic_tool`, `attach_dynamic_tool_turn`, server `item/tool/call` | `configure_dynamic_tool`; normalized `tool_request` | Existing Codex client validates namespace/thread/turn and preserves request id |
| Provider notification | client event drain | `observe` then `normalize_event` | Adapter emits evidence; Finalizer remains the only V1 terminal authority |
| Exact cancellation | `turn_interrupt(thread_id, turn_id)` | `interrupt` | First response means request delivered, not confirmed; terminal interrupted event is separate evidence |
| Connection cleanup | transport/client `close` | `close` | Idempotent; only the adapter-owned connection is closed |

All network, provider start/interrupt, and host command work remains outside
SQLite write transactions. This package adds no durable queue, event ledger,
provider table, or background worker.

## Codex Adapter Rules

`CodexProviderAdapter` constructs the real `CodexAppServerClient`; tests inject
only the transport. WebSocket framing, process supervision, JSON-RPC request
matching, sandbox policy construction, and dynamic-tool response formatting
remain in `app_server.py`.

One adapter owns one connection and allows one active operation at a time. A
second session on the same connection is allowed to exist as a reference, but
starting a second active turn returns `AdapterBusy`. Callers needing concurrent
provider sessions must use separate adapter connections. This is a bounded
serialization contract, not an arbitrary multiplexing claim.

The adapter increments a connection generation when a connection is created.
Reconnect creates a new generation and resumes the exact opaque provider
handle; it does not create a new Execution or Attempt. Events from a stale
generation remain diagnostic evidence and are never authority-eligible.

The client now exposes a bounded `drain_events()` surface. Existing V1 callers
continue to use their method-name event list and do not change behavior. The
adapter limits event batches to 100 and payloads to a configured byte bound;
oversized payloads become a hash-and-size truncation record. Native event ids
are marked as duplicate on redelivery. Events without native ids receive a new
receipt for every legal delivery, so equal payloads are not merged by hash.

## Failure and Retry Semantics

| Situation | Result | Retry rule |
| --- | --- | --- |
| Send fails before `turn/start` reaches the transport | `TransportLoss` | Caller may make an explicit new decision. |
| Request send succeeds but response is lost | `SideEffectUnknown` and operation is quarantined locally | No automatic second start; recovery/resume evidence is required. |
| Provider rejects or returns malformed protocol | `ProtocolError` | No provider-side exactly-once claim. |
| Observation deadline expires | `TimeoutError` | Timeout is not a stopped-provider or terminal-execution verdict. |
| Interrupt response arrives | `CANCEL_REQUESTED` | Observe an exact terminal event before treating cancellation as confirmed. |
| Duplicate interrupt | `CANCEL_REQUESTED` with `request_already_delivered` | No second interrupt is sent. |
| Late/mismatched/old-generation event | Normalized non-authoritative evidence | It cannot advance another operation or execution. |

The adapter never infers exact ownership from a latest turn, latest result,
timestamp order, or transcript text. Missing thread/turn correlation is
explicitly `unknown`; mismatch and stale generation are separate statuses.

## PVX-1807 Remediation Addendum

This remediation was performed under an explicit operator instruction:

```text
AUTHORITY_SOURCE=USER_DIRECT_OPERATOR_INSTRUCTION
EXECUTION_SCOPE=ISOLATED_DEVELOPMENT_COPY
CLINX_MANAGED_EXECUTION_AUTHORITY=NOT_CLAIMED
HISTORICAL_TASK_MUTATION=NOT_PERFORMED
```

The authorized copy is `/home/pvxlabs/dev/clinx-pvx1807-remediation`, created
from fixed BASE `a85677228dea28e20339b25ee22d5ea9f3b04cfe` on branch
`pvx-1807-remediation`. The original `/home/pvxlabs/dev/clinx` checkout was
kept read-only for this task. No CLINX managed execution grant, canonical
registration, runtime lease, or physical worktree takeover was inferred or
created. Historical PVX-1800 task `task_322fd95b8e8a480387460c24e763d0a7`
and its lease were not read, changed, cancelled, recovered, or released.

The following remediation contracts are now explicit:

| Contract | Remediation boundary |
| --- | --- |
| `PA-01` | Every provider call routes the opaque provider handle. Reconnect creates a new connection generation, resumes that exact handle, and rebinds the same execution/attempt/operation identity. Stale-generation evidence cannot advance the current operation. |
| `PA-02` | Transport send completion is tracked per RPC request, including when a server tool request is handled in the middle of an RPC. A sent-but-unanswered start is `side_effect_outcome_unknown`, quarantines the whole adapter connection, and is never automatically resent. |
| `PA-03` | Events are checked against the complete session/thread/turn/operation/generation context. Current exact terminal evidence moves the operation to historical-only observation; later matching events are `historical` and non-authoritative. Only the known terminal status schema is terminal. |
| `PA-04` | Dynamic-tool handlers are bound to namespace, name, provider thread, current turn, operation closure, and connection generation. Continuations and reconnects do not reuse a prior turn binding. |
| `PA-05` | Empty non-blocking polls remain `observed` with no events. A closed transport becomes `transport_loss` and a bounded `disconnected` lifecycle state; explicit `close()` is idempotent and `closed`. Event/native-id and operation history are bounded. |

The adapter continues to be an evidence source only. These guards do not write
Task, Execution, Attempt, lease, runtime-control, shadow-ledger, or Linear
state and do not change the public MCP contract.

## Deliberate Non-Goals

Cross-process provider recovery, durable subscriptions, external exactly-once
delivery, authenticated command boundaries, a second real provider, a new
scheduler/RPC service, production outbox consumption, V2 live cutover, and
physical process fencing are outside PVX-1807. The synthetic adapter fixture
proves the abstraction only; it is not a second provider integration.
