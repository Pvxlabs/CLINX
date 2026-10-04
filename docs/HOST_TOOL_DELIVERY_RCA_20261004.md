# Host dynamic tool delivery RCA — 2026-10-04

## Confirmed boundary

The historical failure is `exec_054234f6798d4027afa2ccdbcecfe263`, task
`task_dd43ad750800462a94e245d0ba201889`, turn
`01a1065f-b127-7e70-8d89-59627ed41592`. Its native dynamic call was
`exec-8ac274f6-85f2-48b7-bd5b-4c9b751a0f80`.

The operator corrected the original entrypoint description: ChatGPT called Remote
Desktop Commander, which ran a one-shot Python MCP client using `launch-mcp`.
Immediately after `clinx_start_execution` returned `DISPATCHED`, that client ran
`p.terminate(); p.wait(...)`. This was **not** a failure observed under the normal
long-lived ChatGPT connector child. The one-shot client had been used because the
ChatGPT session still cached the older authority tool schema.

The old implementation owned the Provider connection, dynamic tool listener and
completion supervisor inside the MCP process that performed start. Ending that
process removed all three, while the Provider turn continued. The worker emitted
the namespaced `tools.clinx__clinx_host_operation` request, waited on its code cell,
and eventually received a cancelled dynamic-call result. No live listener reached
the old ledger insertion point in `admit`, so both Host tables remained empty.
This establishes an ownership-lifetime failure before Host validation/dispatch;
it is not evidence of a slow GIT command or a broken namespace parser.

The original BLOCKED result and its empty ledger remain immutable. Its historical
`NO_HOST_CALLS` means the old ledger contained no calls; the native event evidence
demonstrates why that was insufficient to prove that the worker made no request.

## A/B evidence before the repair

| Experiment | Execution | Result |
| --- | --- | --- |
| A: installed formal connector, child PID 117326 retained | `exec_bc85e7240e9c4ec9bbd49e373200cff7` | COMPLETED/PASS; one delivered GIT.status call; Host `hostexec_ead5dfc12fd24499b4a9a9c904173a77` |
| B: one-shot MCP terminated after DISPATCHED | `exec_0fc6596fa2f740429c728060a2f73f2b` | worker submitted and waited; ledger count 0; MCP exit -15; failure reproduced |

B used an isolated task DB and no-origin test repo and was then cancelled through
the canonical cancellation API. The original convergence execution was not
retried, cancelled, reconciled or edited. The first B harness incorrectly expected
an active `thread_items_list` read to expose the pending dynamic item; its failed
assertion/log is retained. The corrected collector uses the persisted native
worker call and pending-cell outputs, plus the exact empty ledger.

## Repair

`execution_owner.py` provides one persistent local owner per canonical task DB.
MCP start forwards only the already sealed prepared reference, explicit approval
and exact database identity over a private same-user Unix socket. It does not
forward arbitrary prompts, operation authority, shell commands or target paths.
The owner uses the existing `ClinxIntegration`, canonical prepared-start lock,
TaskDispatcher, HostExecutor, leases and finalizer. Its ownership table is a
transport/dispatch receipt, not another authorization source.

The owner persists its claim before dispatch and its response before replying.
Requester EOF, SIGTERM or response loss does not end its Provider supervisor.
Duplicate start returns the persisted response without another Provider start.
A missing owner fails closed; an uncertain prior claim or lost prior active owner
requires recovery and never starts another turn automatically. No transient
process fallback exists. The singleton lock prevents a second owner replacing a
live socket. SIGTERM drains owned executions and rejects new starts; the systemd
unit sends the initial signal only to the owner and has no forced drain timeout.
Completion polling is scoped to that owner's enrolled execution references.
Starting an owner or an MCP reader does not adopt unrelated pending executions.

This addresses requester-process death. A forced owner crash/host power loss is
an explicit recovery boundary; this change does not claim automatic active-turn
adoption after SIGKILL and never replays uncertain Host work.

Ingress is now persisted at an owned exact-turn `item/started` or `item/tool/call`
before callback admission, schema/policy/target validation and side effects.
Unconfirmed strict-bind requests remain staged until their turn identity is
proven; foreign turns and read-only observers do not write or respond.

Durable identity is `(execution_ref, tool_call_id)`, bound to task, thread, turn,
connection/listener/generation, Provider endpoint/version/PID, operation and
target. A semantic payload hash detects changed requests. Missing provider call
IDs receive a deterministic local rejection ID and never dispatch. Host receipts
hold the reverse call/execution correlation.

The call phases are `RECEIVED → VALIDATING → VALIDATED → DISPATCHED → RUNNING →
SUCCEEDED/FAILED/UNKNOWN`, with `REJECTED` for proven pre-dispatch failure.
`validated_at` and `dispatched_at` preserve transition evidence. A separate
delivery dimension remains `PENDING/DELIVERED/FAILED`: Host success alone does not
prove the worker received the response. Exact Provider `item/completed` content
and owner identity provide the acknowledgement. Worker final text cannot replace
these persisted facts. New rejected calls cannot clear prior uncertainty.

## Validation boundaries

The required Host/authority/runtime regression selection and counts are recorded
in `.validation/host-tool-delivery-20261004/regression-release.log`. Coverage
includes real fixture Host processes, policy/target rejection, duplicate IDs,
disconnect/reconnect and no replay, completion/result races, terminal history,
timeout, UNKNOWN reconciliation, derived targets, native interop, node routing,
MCP discovery and `--no-recover-existing`. No assertion was deleted or weakened;
the tunnel catalog expectation adds the two already shipped node discovery tools.
The launcher selects declared repository discovery dependencies when present.

Broad discovery run before the final ingress-only adjustment: 1033 passed,
9 existing opt-in skips, 120 subtests passed, 3 failed. Two LAN TLS rejection
tests surface `ConnectionResetError` instead of their expected exception types;
both reproduce on an unmodified `1a7f93e8` archive. The third CLI subprocess test
passes with `CLINX_PYTHON` set to the isolated environment. These are not marked
PASS, skipped, or patched as part of the Host repair. Full default suite is
therefore not claimed green. The required regression selection is a separate gate.

Real pre-activation requester-exit qualification on final ownership code:

- READ_ONLY_HOST: `exec_af1ca9f771fe4766ab1caf32ca45d4f3`.
- DEVELOPMENT_MUTATION: `exec_f930969b37b4460085260b34a13a39b8`.
- Both requester processes exited -15 immediately after DISPATCHED; owner PID
  491467 completed both turns. Each has one SUCCEEDED/DELIVERED call and exact
  matching Host receipt. Counter remained 1 after duplicate start and duplicate
  durable call admission. Owner subsequently drained and exited 0.

`scripts/qualify_execution_owner.py` reproduces these checks with isolated state
and a disposable repository; private configs remain outside the repository.

## Runtime and operational acceptance

Source baseline is `1a7f93e81e25c3e25f499e251e111bc89a2b82b8`, preserving the
air-resume and derived Git authority changes. Main `f88ffe0` was inspected and is
not the runtime build source. Runtime build, activation, official connector
read-only/mutation acceptance and final identity readback are recorded below
after they run; they are not inferred from the isolated tests.

Activation preflight found zero active executions, worktree leases, running Host
process receipts and pending completions. The original node-centre.env and
launch-mcp are saved under
`~/.local/state/clinx/qualification/host-delivery-20261004/activation-rollback/`.
Only the persistent execution owner and MCP tunnel children are in scope.
Node centre, Network Observation, macOS apps, Air/iMac and ORION are not switched.

Rollback requires the same zero-active-ownership check. Drain/stop the owner,
restore the saved node-centre.env and launcher, then restart only the changed MCP
children. Do not restore an old task DB over new receipts or remove additive
ledger columns. The old release remains available. Runtime rollback reintroduces
the old requester-lifetime limitation; it is a rollback point, not a repaired
delivery path.

The original future policy remains version 1 with the exact
`GIT.push_current_branch` scope targeting
`gitwt_21b60628c2544672903c7394a57348f0`. No convergence continuation/push or
deployment is performed in this task.
