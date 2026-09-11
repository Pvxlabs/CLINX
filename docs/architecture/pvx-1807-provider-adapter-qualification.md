# PVX-1807 - Provider Adapter Contract & Codex Conformance

## Scope and authority

This qualification covers the local checkout at `/home/pvxlabs/dev/clinx`.
The starting checkout was clean on `main` at `2f02131` (the stated PVX-1806
baseline). No repository-local execution-authority or lease-control entry was
available to read; no PVX-1800 lease was claimed, changed, cancelled, or
recovered. The requested work stayed in this ordinary writable checkout.

The adapter remains explicit and default-off:

```text
V1_LIVE_AUTHORITY=ON
V2_LIVE_AUTHORITY_CUTOVER=NOT_PERFORMED
PROVIDER_ADAPTER_LIVE_DEFAULT=OFF
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

No real Codex service, tunnel, user session, development command, host
command, or Linear write was used by the conformance tests.

## Delivered files

- `provider_adapters/contracts.py`: typed, JSON-shaped provider-neutral values.
- `provider_adapters/protocol.py`: structural adapter contract.
- `provider_adapters/codex.py`: explicit Codex adapter using the real
  `CodexAppServerClient`.
- `provider_adapters/fixtures.py`: deterministic scripted transport and a
  deliberately different synthetic provider.
- `test_provider_adapters.py`: conformance and negative-authority tests.
- `docs/architecture/ADR-004-provider-adapter-contract.md`: decision and
  current-entry mapping.
- `app_server.py`: bounded raw notification drain, additive to the existing V1
  client surface.

## Raw trace

The focused fixture scripts the following sequence. The adapter still creates
and invokes the production `CodexAppServerClient`; only the transport is
scripted.

```text
initialize request
  -> initialize response {serverInfo.version: "test"}
thread/start {cwd, model}
  -> {thread: {id: "thread-a"}}
turn/start {threadId: "thread-a", input, cwd, effort}
  -> {turn: {id: "turn-1"}}
turn/completed notification
  {threadId: "thread-a", turnId: "turn-1", status: "completed", eventId: "evt-1"}
  -> NormalizedEvent(correlation=exact, terminal_observed=true,
     operation=(execution-1, attempt-1, exact operation id))
```

The dynamic-tool case sends a server `item/tool/call` request with its request
id and exact namespace/thread/turn. The real client responds through its
existing handler, and the test verifies the request id and turn attachment.
No complete wire dictionary is required by normalized-event consumers.

## Capability and identity matrix

| Capability/identity | Codex adapter | Synthetic fixture |
| --- | --- | --- |
| Model/session discovery | `model/list` supported evidence, unknown on unavailable response | Session resume supported |
| Session handle | Opaque Codex thread id, separate CLINX id | Opaque room handle, separate CLINX id |
| Exact operation | Execution + Attempt + Session + Operation + generation | Same provider-neutral context |
| Dynamic tools | Supported through existing client validation | Explicit no-op fixture hook |
| Interrupt | Request supported; confirmation requires terminal event | Explicitly unsupported by default |
| Multiplexing | Unsupported on one adapter connection; `AdapterBusy` | N/A in fixture |

Error and observation mapping is explicit: transport loss, protocol error,
timeout, ambiguous side effect, correlation failure, unsupported capability,
cancel requested, and cancel confirmed are not collapsed into one `FAILED`
state.

## Qualification commands and results

Environment: Python 3.12.3, pytest from the repository environment, SQLite
3.45.1.

```text
python3 -m pytest -q test_provider_adapters.py
10 passed

python3 -m pytest -q test_m13b.py test_bridge.py test_pvx1805_v1_compatibility.py
124 passed, 12 subtests passed

python3 -m pytest -q
481 passed, 66 subtests passed

python3 -m compileall -q provider_adapters app_server.py
PASS

git diff --check
PASS
```

The full suite result is the current checkout result, not the historical
PVX-1806 execution report. The PVX-1805/V1 compatibility tests and PVX-1806
runtime/guard suites are included in the full run.

## Required qualification status

| Qualification | Status | Evidence or boundary |
| --- | --- | --- |
| `EXECUTION_AUTHORITY_VALID` | PASS | Clean, writable local checkout; no protected lease was touched. |
| `PROVIDER_NEUTRAL_CONTRACT` | PASS | Typed contract, serialization, capability tri-state, exact context, normalized event and typed errors. |
| `CODEX_REAL_CLIENT_ADAPTER_CONFORMANCE` | PASS | Scripted transport through the real `CodexAppServerClient`; raw trace above. Not live-provider E2E. |
| `SYNTHETIC_PROVIDER_CONFORMANCE` | PASS | Different session/event shape and explicitly unsupported interrupt capability. |
| `EXACT_SESSION_EXECUTION_CORRELATION` | PASS | Exact thread/turn/session/generation checks; mismatch and absent correlation remain non-authoritative. |
| `CONTINUATION_TOOL_ATTACHMENT_COMPATIBILITY` | PASS | Existing dynamic-tool namespace/thread/turn checks reused; V1 bridge tests pass. |
| `AMBIGUOUS_START_NO_DUPLICATE` | PASS | Sent-but-unanswered start becomes `SideEffectUnknown`; second start is rejected and not sent. |
| `CANCELLATION_OBSERVATION_SEMANTICS` | PASS | Interrupt returns request-delivered; interrupted terminal event is separate confirmation. |
| `CROSS_SESSION_EVENT_ISOLATION` | PASS | One active operation per connection, exact context checks, stale/mismatched events are non-authoritative. |
| `BOUNDED_OBSERVATION_AND_CLEANUP` | PASS | Max 100 events, bounded payloads, idempotent close, owned transport only. |
| `NO_ADAPTER_TERMINAL_AUTHORITY` | PASS | Adapter exposes evidence only and has no finalizer, lease, TaskRegistry, or Linear write path. |
| `V1_DEFAULT_PATH_COMPATIBILITY` | PASS | Adapter is not imported by V1 bridge/MCP; focused bridge/V1 tests pass. |
| `PVX1805_PVX1806_REGRESSION` | PASS | Included compatibility, runtime-control, shadow and domain tests pass in full run. |
| `FULL_SUITE` | PASS | `481 passed, 66 subtests passed`. |
| `LINEAR_SYNC` | NOT_PERFORMED | No Linear write interface was available/used; no status was fabricated. |

## Unqualified boundaries

The tests do not prove a live provider, deployed service, physical process
fencing, cross-process event delivery, durable provider recovery, or a second
real provider. The adapter does not write the existing runtime-control evidence
sink; therefore pending-guard/expired-token sink behavior remains owned and
qualified by PVX-1806 rather than reimplemented here. No production schema or
provider schema was created.
