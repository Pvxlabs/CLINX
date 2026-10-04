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
Active cancellation is forwarded to this same owner so it reaches the actual
HostExecutor process registry. Cancellation remains available while draining;
terminal cancellation readback remains idempotent across owner restart.

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

The final required Host/authority/runtime regression is **440 passed, 51 subtests
passed**, recorded in
`.validation/host-tool-delivery-20261004/regression-qualified.log`. Coverage
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

## Final runtime acceptance and delivery

Final runtime source: `809fb6b19d3ae7bb9f9bdcd83450ad0ff67f045a`.
Release: `/home/pvxlabs/.local/lib/clinx-control/releases/host-delivery-809fb6b1`.
The source tree, archive, MCP schema, control modules and kernel binary hashes
are in `runtime-build.json`; effective process argv/cgroup and matching file
hashes are in `qualified-final-readback.json`. The persistent owner is PID
590006; both tunnel MCP children use this same release. Node centre PID 2248985
and the existing bridge service PID 1043359 did not change.

The first candidate owner refused startup because the existing task DB parent
was group-writable. That failure occurred before switching MCP children and is
retained in `activation-attempt-1.log`. The repaired endpoint is under the private
user runtime directory, keyed by the canonical database hash. Existing DB
directory permissions were not changed. The earlier release and failed build
manifest are retained; they are not represented as successful activation.

One formal connector start immediately after the final tunnel restart returned
`Transport closed`. Exact readback showed preparation still PREPARED, no execution
and no owner claim. It remains unstarted and was not replayed. After a successful
formal read verified connector recovery, a fresh preparation was used for the
final acceptance. `connector-restart-unstarted.json` preserves this separate
transport failure; it is not a Host delivery PASS.

Local canonical `prepare_execution` produced the sealed references because the
existing node registry has both `p620` and a stale `p620-smoke` route using the
same host alias. Formal prepare with explicit host therefore reports
`THREAD_HOST_CONFLICT`. No node registry entry, project permission or operation
scope was edited. The read-only start and both terminal status reads below used
the installed formal CLINX tools. Mutation start used the installed `launch-mcp`
JSON-RPC entrypoint and deliberately terminated its requester. Preparation did
not execute work or replace the start/Host authority checks. End-to-end formal
prepare through the ambiguous host alias remains outside this delivery PASS.

| Final gate | Read-only | Isolated mutation |
| --- | --- | --- |
| Execution | `exec_c08600a6677d4ec4a346e6d6c7685916` | `exec_2c70932b5cc24e48888ae53582a3a0cc` |
| Task | `task_6b199302aedd440c9db935a21ee28f0f` | `task_6f2deb2abf4542fdb245ba7d84382ba1` |
| Host | `hostexec_c2603f9830c14ac586d2402d5c5a2a3f` | `hostexec_5236304d8782460ab9172715e16e82db` |
| Operation | `GIT.status` | `LOCAL_HOST_PROCESS.development_command`, counter in no-origin fixture |
| Requester | formal long-lived connector | PID 611083, exit -15 immediately after DISPATCHED |
| Owner | PID 590006, final release | same persistent PID 590006 |
| Call / delivery | `SUCCEEDED / DELIVERED`, count 1 | `SUCCEEDED / DELIVERED`, count 1 |
| Worker final | `COMPLETED / PASS` | `COMPLETED / PASS`, `COUNTER=1` |
| Result projection | `WRITTEN` | `WRITTEN` |

The mutation was observed `CODEX_RUNNING` after requester death, then terminal.
Re-submitting its exact prepared reference through the formal tool returned the
cached dispatch receipt. Re-admitting its recorded call ID at the ledger boundary
raised `ToolCallReplayRejected`; the ledger and Host receipt stayed unchanged and
the counter remained 1. This is an explicit duplicate-delivery test, not a retry
of an unknown side effect. Unit regressions also exercise duplicate Provider
requests and reconnects against actual fixture Host processes.

Both formal terminal status payloads exactly equal the corresponding SQLite
delivery rows. Host receipt, call ID, task, thread and turn match in both
directions; the ledger listener PID is the persistent owner rather than the
terminated requester. Host `SUCCEEDED` and provider `DELIVERED` remain separate
dimensions; neither is substituted for the other's acknowledgement.

Final readback shows zero active execution rows and zero worktree leases. The
historical failure rows, result and completion handoff retain SHA-256
`4f85875ab74601c30c55fa1e655fa0cf9d9cf676cb490bcb109f4cfd37b116ec`.
The version-1 future policy and exact derived Git target scope are unchanged.
An earlier candidate's mutation result had Linear writeback FAILED; that receipt
is preserved locally. The final candidate's two writebacks are independently
WRITTEN, without rewriting the earlier result.

## Required status fields

| Field | Final result |
| --- | --- |
| ROOT_CAUSE | CONFIRMED: requesting MCP process owned the live completion/dynamic-tool listener and was explicitly terminated |
| FAILURE_BOUNDARY | Provider invocation → dead listener, before ledger admission/Host dispatch; baseline A PASS, B reproduced |
| DURABLE_CALL_IDENTITY | PASS: execution/call primary identity, bound task/thread/turn/operation/target/owner; durable pre-validation rejection |
| DELIVERY_STATE_MACHINE | PASS: persisted call phases and independent exact-origin Provider acknowledgement |
| LEDGER_SEMANTICS | PASS for repaired owner path; original empty ledger retained as historical missing-ingress evidence |
| RESULT_CONTINUATION | PASS: both final workers used real Host results and completed |
| IDEMPOTENCY | PASS: duplicate start/call does not create a second Host process or increment counter |
| AUTHORITY_REGRESSION | PASS: required regression selection, version 1 and exact `gitwt_21b60628c2544672903c7394a57348f0` scope preserved |
| RUNTIME_BUILD | PASS: immutable source `809fb6b19d3ae7bb9f9bdcd83450ad0ff67f045a`, descendant of `1a7f93e8` |
| RUNTIME_ACTIVATION | PASS: final owner plus both MCP children verified; unrelated services retained |
| READ_ONLY_REAL_ACCEPTANCE | PASS: `exec_c08600a6677d4ec4a346e6d6c7685916` |
| MUTATION_REAL_ACCEPTANCE | PASS: `exec_2c70932b5cc24e48888ae53582a3a0cc`, requester terminated, counter 1 |
| PERSISTED_DELIVERY_READBACK | PASS: both formal status payloads exactly equal persisted delivery and Host identity |
| ROLLBACK | Saved and documented; restoring the previous runtime after final activation was NOT_RUN |
| BRANCH | `codex/host-delivery-fix-20261004` |
| HEAD | Runtime source above; documentation/evidence commit identity is recorded after commit in local `git-delivery.json` and the final handoff |
| PUSH | Normal branch push; exact remote commit verification is recorded in local `git-delivery.json` after push |
| FINAL_STATUS | PASS for the requested Host delivery repair and acceptance gates |

This PASS does not claim a green broad default discovery suite, formal prepare
through the existing ambiguous host alias, forced-owner-crash automatic adoption,
or any Network Observation/macOS/Air/iMac/ORION deployment. Failed, unstarted,
skipped and unrun evidence above is retained with its actual status.
