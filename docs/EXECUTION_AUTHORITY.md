# Canonical Task execution authority

## Native task forwarding (2026-10-17)

CLINX forwards messages to native Codex conversations. The only execution
configuration accepted by the public connector is `model` and `reasoning_effort`.
Each is independent: omitted values inherit the conversation's configuration;
CLINX does not choose a default model or reasoning effort for forwarded work.

Pass the complete `codex://threads/...?...hostId=...` URI to
`clinx_prepare_execution`, preserving its original hostId. No preliminary search,
status read, separate adoption or policy review is needed. A known task_ref can
also select the conversation. The normal flow is prepare once, then start the
returned prepared_execution_ref. The user's instruction to send or continue the
task supplies approval; the caller should not ask for the same confirmation again.
Use get_prepared_request to resolve a lost response or a concrete ambiguity, not
as a routine step. The start receipt, not preparation, proves dispatch.

There is one public execution backend, `CODEX_NATIVE`. Ordinary new and continued
tasks use `NATIVE_FULL_ACCESS`: `sandboxPolicy={"type":"dangerFullAccess"}` and
`approvalPolicy="never"`, without `writableRoots` or network-disable settings.
This runtime behavior supersedes the initial 2026-10-17 no-override description;
see [native full access](NATIVE_FULL_ACCESS.md) and
[Desktop execution](DESKTOP_NATIVE_EXECUTION.md). Existing developer instructions
are preserved; CLINX does not impose a reply template.
The old network, surface, normal/fast, capability and operation-class selectors
are removed from connector discovery. Historical stored policy/route fields
remain for receipt integrity; they do not describe current native permissions.
Legacy network arguments cannot change a conversation's policy or select a new
Provider thread. Old Host capability and authority tools are no longer exposed
by the public connector; internal history/reconciliation compatibility remains.

An existing conversation is resumed directly for public native forwarding, even
when legacy CLINX policy versions have no Provider migration lineage. Internal
policy metadata does not require a new native thread. Forwarding does not create
a policy successor or inject `CLINX HISTORY REFERENCES` into the task message.
Existing lineage remains historical evidence; it is not rewritten or replayed.
The legacy Host executor's dynamic-tool migrations remain internal compatibility.
Active execution ownership,
exact target identity, duplicate prevention and completion receipts are retained.
They coordinate delivery and do not authorize Git operations or repository paths.

The 2026-10-24 Rust native workspace binding resolves an exact Thread's live
`cwd` before preparation and admission, and verifies the transport's observed
cwd before sending. The resolved context records the repository root, shared Git
common directory (repository identity), worktree Git directory, branch and HEAD.
Project registration supplies a default path and identity discovery; legacy
`workspace_contract` branch/HEAD values are audit metadata. Changes to branch,
HEAD or cwd within the same repository do not reject a native execution. Unknown
repository identity, unreadable cwd and a different repository fail explicitly.
Historical status and exact input receipts do not depend on a current checkout.
Explicit Host execution uses the same workspace resolver while retaining operation
scope, exact handoff and receipt checks.

Rust leases are scoped by `host + task_ref`, rather than `host + cwd`. Independent
tasks can dispatch and run in the same directory while other executions are
RUNNING, unknown or awaiting an ACK. Each task retains its own attempt fence,
active execution, cancellation, finalization and receipt identities. Host command
identity deduplication remains; a shared resource key does not exclude other
executions. Git and filesystem operations report actual conflicts normally.
Existing persisted execution resources remain valid without database migration.
This is a source contract; local tests do not establish installed-release activation.

Production workspace metadata is read by a fixed, bounded probe through the
existing Codex Provider's `command/exec`, under the configured native worker UID.
Rust authenticates that Provider's peer identity and validates and records the
observed repository identity, cwd, branch and HEAD in its existing transaction.
It does not require the SQLite writer UID to read or list project directories.
The adapter creates no thread or turn and never writes task state. Provider
failure, malformed evidence and actual worker filesystem failures remain explicit
errors; production never falls back to the writer's filesystem identity. The
Authority retains its existing empty capability set, database ownership and
read-only filesystem confinement. Project ACLs are unchanged.

An exact execution status read can resolve its sealed preparation before an
execution record exists. An untouched `PREPARED` request without an owner claim
is reported as `PREPARED_WITHOUT_EXECUTION`, with the exact request evidence and
no inferred start failure. This observation does not identify a platform refusal
or authorize replay. A claimed owner or a `RUNNING` preparation retains dispatch
uncertainty; earlier task failures never supply the missing start error.

Public tools explicitly declare `readOnlyHint`, `destructiveHint` and
`openWorldHint`. Preparing a request is an additive write. Starting a native turn
can modify files or deploy services, so its annotations are `false/true/true`.
These declarations describe side effects; they do not grant runtime permissions
or determine whether the client permits a particular call.

Native Codex messages are forwarded without an added title or response template.
CLINX imposes no headings, field list, machine footer, or success declaration on
the reply. The final message is stored unchanged and exposed as
`execution_result.response_text` with `response_format=markdown` in both exact
execution and thread queries. Connectors should preserve this Markdown.

Existing local chats with the old `CLINX MANAGED EXECUTION CONTRACT` receive a
one-time native developer history item that retires only its reply template.
Continuation recognizes the exact old developer block and a durable retirement
record; user quotations and unrelated custom instructions do not trigger it.
It preserves thread identity, all historical records, other instructions and
the original user prompt. No configuration or sandbox override is involved.
The native append occurs only after the ordinary idle-owner guard; an uncertain
append is never retried automatically. Fresh native chats need no migration.

The legacy Linear completion observer excludes executions with a dedicated
Owner record. Each Owner observes its own executions. Deployment must update
the resident Linear dispatcher too: an older global completion observer can
otherwise race the Owner and still apply the retired report parser.

For native replies `status=COMPLETED` and `status_scope=NATIVE_TURN` describe only
the observed completed turn; `business_outcome=NOT_EVALUATED` does not assert
engineering or production success. Definitive provider errors, uncertain owner
identity, incomplete source and unresolved tool delivery retain their existing
handling. Legacy Host receipts retain their strict parser. Historical records
are not reinterpreted or rewritten.

The existing result table accepts the additional lifecycle status `COMPLETED`.
An atomic, repeatable SQLite CHECK migration preserves all old rows, indexes
and triggers. Native reply text is not shortened to the legacy 16,000-character
receipt limit; bounded audit excerpts remain separate from the original text.

Deployment acceptance also covers completed threads that every configured
Provider has unloaded. If all reachable configured Providers report the same
exact thread/turn as `completed`, with no active claim or missing peer evidence,
CLINX can finalize from that durable result and release the lease. Interrupted,
missing, conflicting, and unreadable observations still retain ownership. This
does not resume or replay the completed turn.

`clinx_get_effective_authority` no longer evaluates operation requirements for
task forwarding: `requirements_evaluated=false`, `requirements_covered=null`,
and `missing_operations=[]`. These values do not claim that Codex has executed
the requested operations. The legacy Host catalog and policy APIs remain for
historical inspection and compatibility, not as prerequisites to forward work.

The sections below describe the historical Host executor contract. They still
apply to direct use of that executor, not to public task forwarding.

CLINX owns Task/Execution identity, approved scope, workspace/resource ownership,
lifecycle, evidence and reconciliation. Codex retains its native workspace tools.
Production controller validation remains owned by the target application.

## Root causes and corrected path

The previous prepare path selected Host for explicit capability requests, but
unstructured production goals could retain the native default. Continuation
rejected every policy change; the documented reauthorization API did not exist.
Legacy adoption could retain a null policy. Discovery's fixed `authority_granted`
fields described the absence of a grant from discovery, rather than effective
Task authorization. Configured production argv embedded candidate SHAs. Finally,
Codex 0.156.1 accepts dynamic tools at thread/start, but **not** thread/resume;
CLINX silently accepted and ignored a new tool schema on resume.

The supported path is now:

User-authorized structured requirements → prepare → immutable policy/route
snapshot → atomic workspace claim → native turn and/or registered Host operation
→ resource fence → durable Host receipt → delivery ledger → exact Provider ACK
→ existing Finalizer → result/history and lease release.

`requested_operations` contains exact `capability`, `operation`,
`operation_class`, and registered `target` (empty for project/local operations).
For a new Task, these facts select Host capability/classes and exact operation
scopes. Production mutation additionally requires `production_mutation_intent`.
Every new production Task requires these exact scopes, including legacy registered
commands: capability/class alone is rejected by both prepare and direct dispatch.
Historical unscoped policies remain readable and retain their sealed continuation
behavior; they never grant the new workflows without explicit reauthorization.
Native execution now uses `NATIVE_FULL_ACCESS`: `dangerFullAccess` with
`approvalPolicy=never`. CLINX adds no filesystem or network sandbox. Legacy
`network_access` inputs remain compatible but do not restrict new native turns.
Continuation upgrades old task policy through the existing versioned transition;
historical execution snapshots retain their original policy.

Preparation rejects an unsupported required operation, wrong class/target,
unavailable workflow executable or insufficient Task authority before starting a
Provider turn. Natural-language keywords are not authorization. Callers must
translate the user's goal into these structured requirements; a prose deployment
request alone does not grant production permission.

## Same Task reauthorization

Public MCP tools (also available through `ClinxIntegration`):

1. `clinx_get_effective_authority(task_ref, requested_operations?)` returns the future policy, generation
   and `policy_hash`, including legacy route/network authority in the CAS identity.
   Structured requirements return `requirements_covered` and exact
   `missing_operations`. Covered scopes are reused without reauthorization.
   A prose goal alone cannot establish whether future publishing is authorized.
2. `clinx_prepare_policy_reauthorization(approved=true, task_ref,
   expected_policy_hash, target_policy, reason, network_access?)` records an
   immutable, 15-minute request. Host policy changes require exact operation scopes.
3. `clinx_apply_policy_reauthorization(approved=true,
   prepared_reauthorization_ref)` atomically compares identity and appends the
   approved version. Repeat apply returns the original receipt without starting work.
4. A separate prepare/start continues or reopens that same canonical Task.

`clinx_get_prepared_request(request_ref)` reads either a `reauth_...` or
`prepared_...` record without applying, consuming, starting or probing a Provider.
It returns the target node/project/repository, previous/current/proposed policy,
policy differences, content hash, timestamps and validity. Policy requests expire
after 15 minutes; execution preparations have no time expiry and are checked
against their sealed policy/route instead. Applied requests retain their receipt
after expiry. Raw prompts, credentials and operator environment are omitted;
`prompt_sha256` identifies execution content without returning it.

Before applying, inspect this readback. Pass its `request_hash` as the optional
`expected_request_hash`, and the reviewed task as `expected_task_ref`, to
`clinx_apply_policy_reauthorization`. Both assertions are checked inside the
existing CAS transaction, including on idempotent repeats. The same review
assertions are accepted by `clinx_start_execution`; they cannot override policy,
target, prompt or authority. Existing clients remain compatible. `approved=true`
records the operator assertion only; it does not override client-side approvals.

After a lost apply response, read the SAME request and check
`request_state=APPLIED`, `applied_policy_version` and the effective policy hash.
After a lost start response, read the SAME prepared execution. The stable
`execution_ref`, exact execution-owned thread/turn and persistent owner state
identify what to inspect with `clinx_get_status(execution_ref=...)`. Neither a
task's previous completed turn nor a QUEUED projection proves this execution ran.
`provider_running=NOT_OBSERVED` on prepared readback is intentional; live status
is a separate observation. Unknown dispatch errors retain a consumed request,
and never restore it to PREPARED merely because the response was lost.

Failure results include `failure_stage`, `failure_source`, `failure_code`,
`correlation`, `side_effect_certainty`, `outcome_certainty`, `retry_allowed` and
`recovery_action`. A server receipt states `server_received=true` and
`caller_approval=NOT_OBSERVED`; CLINX cannot manufacture a client approval or
explain a request rejected before reaching it. Missing logs/executions do not
prove an external safety rejection. Preserve original client errors separately.

Exact thread examples (the status tool does NOT accept `max_bytes`):

```json
{"thread_id":"01a11492-d1ae-7872-ab01-ca022e9cda3d","host":"p620"}
```

```json
{"codex_uri":"codex://threads/01a11492-d1ae-7872-ab01-ca022e9cda3d?hostId=remote-ssh-discovered%3Ap620"}
```

Use either selector, not both. A full URI is never a `thread_id`.
`THREAD_UNBOUND` can coexist with a native COMPLETED turn; native historical
threads do not require automatic adoption. The public readback tool adds no
App pages or interaction flows, and existing write-operation annotations remain.

本次源码、P620 制品及 A/B/C 验收证据见
[历史验收归档](README.md#历史资料恢复)中的 `AUTHORITY_DISPATCH_ACCEPTANCE_20261007.md`。
其中 B 和复用既有授权的 ChatGPT 实际入口 C 均已通过。
本轮 C 没有重新验证新增权限 apply，不保证所有未来权限申请都会获准。

The audit records previous/new policy and route, requested/approved scope, reason,
channel, actor limitation, timestamps and generation. The current MCP protocol
proves an explicit operator `approved=true` assertion, not an individual human's
identity: actor is `MCP_OPERATOR_SUBJECT_UNAVAILABLE`, authority source is
`EXPLICIT_OPERATOR_APPROVED_TRUE`. No human signature/approval receipt is invented.
These control-plane calls are outer operator actions; managed workers receive
only their execution's operation grant and must not reauthorize themselves.

Apply observes the configured Provider owner without resuming it, and rejects
active execution/lease ownership, conflicts, incomplete ownership observation,
unresolved Host effects, unknown completion and unconfirmed result delivery.
CAS runs inside the same SQLite transaction as the version append. Historical
Execution policies, routes, results and receipts are never rewritten. The Task's
future policy changes without changing its current result or lifecycle. Stale
prepared executions and direct dispatch policy overrides are rejected. The claim
transaction rechecks the snapshot to close prepare/apply/start races.

`policy_reauthorizations`, `task_policy_versions`, and policy conversation lineage
are append-only. Generation zero snapshots the prior policy, including `{}`.
Changing permission back to an earlier value does not reuse its CAS generation.

## Provider binding and history

Codex 0.156.1 protocol schema was generated from the installed binary to verify
that resume/fork do not accept `dynamicTools`. The first future Execution after a
policy change therefore binds a new Provider thread under its existing Task and
workspace lease, before any turn starts. Its developer instructions and dynamic
schema carry the new policy. No competing Task or bootstrap turn is created.
The predecessor thread/session and policy generation are recorded immutably;
exact thread lookup retains historical Execution selection. The current prompt
receives the prior checkpoint and exact read-only history reference.

Existing executions are not rebound, resumed or interrupted by reauthorization.
If provider binding/turn dispatch becomes uncertain, existing recovery/finalizer
rules apply; a missing receipt never authorizes replay. Automatic raw database
changes and manual replacement of provider history are not supported.

## Registered production workflows

`host_executor.workflows` registers one fixed controller command per
workflow/target/action. The caller supplies only schema-validated identifiers.
The operation is `workflow:<identity>:<action>` under `LOCAL_HOST_PROCESS`.
Its arguments are exactly `target` and `parameters`.

Parameter formats: full 40-character lowercase source SHA, 64-character SHA-256,
bounded safe identity, or explicit enum. The executable, flags, hosts and roots
are registered configuration. A fixed absolute path template may incorporate
safe single-component identities; callers cannot supply paths, slashes, shell,
argv, executables, hosts or arbitrary flags. Execution uses `shell=False`.
Controller code owns candidate ancestry, exact artifact/manifest/plan validation,
migrations, requested component scope, business idempotency and recovery.

Current DATA registrations:

| Workflow/action | Target | Class | Existing controller |
| --- | --- | --- | --- |
| `ORION_DEPLOY:apply` | `orion-data` | `PRODUCTION_MUTATION` | `orion-data-node deploy` |
| `ORION_DEPLOY:operation` | `orion-data` | `PRODUCTION_READ_ONLY` | `orion-data-node operation` |
| `ORION_CONTROLLER_ADOPT:apply` | `orion-data` | `PRODUCTION_MUTATION` | `orion-deploy-controller-adopt --target data` |

Deploy accepts `source_sha`, `scope=DATA`, `artifact_set` and `request_id`.
`artifact_set` is one safe directory identity under the existing registered
`/opt/orion-data-node/release` root; the controller reads its `data-node` subdirectory
and unchanged `plan.json`. No new artifact staging layout is required. Those inputs must
already be staged by the authorized ORION artifact workflow. Registration does
not claim that artifacts are present or qualified, and does not stage/build them.
The complete plan is passed unchanged to the DATA controller, which selects its
DATA ownership; CLINX does not trim plans or enable CORE/0146.
Legacy SHA-specific commands remain compatible but are unnecessary for the new
workflow. Controller adoption is not application deployment.

All workflows for one target share a durable resource fence. Atomic Host start
acquires an increasing epoch under the active execution's canonical workspace
lease. A competing workspace cannot acquire it while the prior process outcome
or delivery is unknown. Known completion and exact ACK permit a new legal call;
the same recorded tool call never executes twice. This is the CLINX admission
fence, not a replacement for the controller's target-owned deployment lock.
Unresolved effects also block a new Execution on the same Task, even without a
policy change. Existing controller idempotency remains essential across calls.

## Discovery, status and exposure

Capability discovery distinguishes probes, implemented operations, exact target
registrations and effective Task grants. Legacy `authority_granted=false` and
path `production_authority_granted=false` remain compatibility fields with
explicit meanings: discovery/path access grants nothing. They are not an
answer about a selected Task. Use `clinx_get_effective_authority` for that answer.
Per-operation and per-target grant fields are separate from `runtime_health` and
`client_exposed`; unobserved layers stay `NOT_PROBED` / `NOT_OBSERVED`.
The responding control process reports its PID and loaded module source root;
this does not infer runtime identity from repository HEAD.

The public MCP registry advertises all three authority tools, and dynamic Host
schemas derive from the same operation catalog used for validation. A connector
can cache/filter that registry outside this repository. A successful backend
`tools/list` is not an outer-client PASS. If the outer client still exposes the
old set, Refresh the CLINX connector's metadata and open a fresh conversation,
then call effective authority and prepare/apply through the exposed tools.
No alternate SSH/database channel should be used to bypass that boundary.

Authority/preflight rejection returns one actionable root blocker with
`evaluation_scope=PROPOSED_EXECUTION_ONLY_PRIOR_EVIDENCE_UNCHANGED`. Unperformed
qualification/deploy/readback/observation are `NOT_RUN`; capacity is `UNVERIFIED`.
An independent prior qualification FAIL remains in its original evidence. Neither
reauthorization nor runtime activation changes those facts to PASS.

## Validation

`tests/test_execution_authority.py` covers CAS/concurrent apply, immutable history,
expiry, explicit approval, unresolved delivery across executions, exact scopes,
real safe controller processes, two candidates and target/resource fencing.
`CLINX_LIVE_AUTHORITY=1 ... tests/test_execution_authority_live.py` compares direct and
CLINX native filesystem/Git/HTTPS on the same user/workspace/profile, then uses the
same Task for DEV → reauthorization → production-like Host execution and history
readback. `tests/test_tool_delivery_live.py` injects a real Provider delivery failure
after a safe local mutation and verifies the counter stays one under replay.
All live tests use disposable registries, Provider endpoints and local targets.
Final counts, exact source/release and production limitations belong in the
accompanying acceptance receipt, not an inferred overall PASS.

## 2026-10-12 execution continuity repair

Ordinary DEV remains native by default. An explicit Host execution also retains
its already-authorized Codex workspace tools within the same sandbox, approval
and network policy. The Host operation grant applies to controlled Host access;
it neither disables workspace development nor lets native tools bypass production
scopes, Host authority or a no-replay requirement.

A Host continuation must use the configured dedicated Provider. Selecting the
idle Desktop owner is unsafe for dynamic tools: shared clients receive the same
call and can race the owning Host handler with an unsupported-namespace response.
The 2026-10-18 owner guard also rejects a Host successor transition while the
selected owner is the shared Desktop daemon. An idle observation is not an
exclusive Canonical Task handoff; a successor Thread alone cannot fence Desktop.
Active, conflicting or unknown ownership still prevents continuation. No
unsubscribe, takeover or Desktop data write is performed.
The predecessor remains readable by its exact thread/Execution identity.
`HOST_PROVIDER_ISOLATION` uses the existing conversation lineage, now retaining
multiple predecessors; an endpoint transition does not increment policy generation.
A policy change continues to use the existing prepare/apply reauthorization APIs.

### Shared native writer constraint and uncertain recovery

The ordinary native-input forwarding path was repaired on 2026-10-20. Desktop
remains the native owner: CLINX forwards the authorized message to the same
daemon and confirms its exact `clientUserMessageId` in a native `userMessage`
event on the returned Turn. No exclusive Task handoff, successor Thread, or
manual adoption is required. The shared-writer refusal described below applies
to exclusive Host dynamic-tool execution, not ordinary native task messages.
See [native task forwarding](NATIVE_TASK_FORWARDING.md).

The official `turn/start` protocol has no compare-and-start admission token.
Two clients of one daemon can receive the same Turn ID; a successful response
alone does not prove a new exclusive execution. Exclusive shared-daemon creation,
resume and start therefore fail before RPC with `side_effect=NONE`, including
Host routes and configured socket aliases. Ordinary messages use input-confirmed
native forwarding instead. Read-only observation and authorized
exact-Turn cancellation remain available through their existing interfaces.

The dedicated Provider is an operational boundary, not proof against arbitrary
external writers. The supported managed execution condition is that all writers
on that Provider participate in the existing CLINX owner/lease/fencing protocol;
Desktop must not simultaneously control the same Canonical Task. Socket naming,
an idle probe, or a second daemon does not establish that condition. Uncoordinated
external clients and the observation-to-start window remain outside an atomic
guarantee. Do not automatically unload Desktop or create a successor to evade
a shared-writer rejection.

For a pre-wire shared-writer rejection, no execution was dispatched; keep the
Task/Thread association and use native Desktop continuation, or an independently
qualified exclusive execution environment. Retrying the same shared route does
not qualify it. For a timeout or lost start ACK, keep the consumed prepared
request and retained lease: `UNKNOWN` / `RECOVERY_REQUIRED` means no replay and
no guessed Turn ID. A process restart does not authorize a new start. Use the
existing exact execution/liveness reconciliation only when its persisted
Task/Thread/Turn identity and fresh owner evidence are available. Without the
exact Turn identity, stop for evidence review; do not adopt the latest Desktop
turn or mark the execution successful. Cancellation requires the exact observed
turn and explicit existing scope; an interrupt ACK remains pending until the
native terminal event is observed. An unavailable daemon never proves that a
writer stopped or that a lease may be released.

Known successful DEV receipt reconciliation uses the existing
`recover-execution --reconcile-delivery --tool-call-id ... --delivery-proof-json ...` maintenance path / `reconcile_host_delivery`.
In addition to the exact task/thread/turn/call/Host refs, argv and Provider failure
hash, a mutating DEV receipt requires `completion_review` with `approved=true`,
`disposition=ACCEPT_RECORDED_COMPLETION_NO_REPLAY`, a reviewed reason, and the
persisted `stdout_sha256` / `stderr_sha256`. This is an outer operator assertion,
not a generated human signature or an ACK. The caller must inspect the actual
operation and effects. Production mutation, unknown completion, timeout, missing
receipt, conflicting identity and unreviewed effects remain ineligible.
Fresh exact terminal Provider ownership is required before releasing the old
lease. Other unresolved deliveries continue to block release. Accepted reviews
are idempotent and audited; the Host command is never rerun, delivery remains
FAILED and the original BLOCKED result stays immutable.

Validation in the isolated source worktree:

- Real Provider Native → Host → Native on one Task: PASS. Native code creation,
  unittest execution and native workspace tools during Host mode all ran.
- Two real disposable Providers: PASS. An idle shared predecessor received a
  dedicated successor, then resumed that successor; three Host receipts each
  received exact DELIVERED acknowledgement, with no competing Task.
- Real injected delivery failure: PASS for recovery/no-replay. The counter was
  incremented once, DEV receipt review released the lease, original BLOCKED and
  FAILED delivery were retained. The first test attempt hit an import error in
  the recovery helper; it was corrected and a new disposable execution passed.
- Extended regression: 287 passed / 44 subtests passed, one pre-existing catalog
  count failure (`15` expected, `16` current). This is not an all-suite PASS.
  Final focused recovery/authority/lineage checks: 55 passed, including active,
  unknown and stale owner rejection.

The real incident `exec_ec5ffc7143de419f8a788404fd2981ad` was reconciled through
this maintenance path after a read-only snapshot/dry run and a fresh terminal
owner observation. Host `hostexec_cee39eb6a4cb4d9ba480541c1152f636` executed once
and exited 0; delivery was rejected with `Unsupported dynamic tool namespace: clinx`
on the Desktop endpoint. Its DEV-class argv only read load, Git metadata and
existing files. Reconciliation is RESOLVED and the old lease is released;
execution/result remains BLOCKED, delivery remains FAILED, Owner and sealed
policy are unchanged. No ORION continuation or business operation was started.
The identity of the external client that supplied the rejection remains unknown.

Evidence: `/data/artifacts/clinx-execution-continuity-20261012/` (live acceptance,
regression logs and exact incident reconciliation readback). The source includes
the preceding native URI/discovery repair. The installed CLINX release has not
been switched; isolated source acceptance and incident maintenance do not prove
that future requests on the old resident runtime use the repair.

The controlled release integration selects only `c05732a` onto GitHub main
`fd6ae02`, preserving that main's Air Monitor terminal/observation fixes byte for
byte. It does not include the preceding `ea18bc7` UI/URI change. The stale catalog
size assertion is replaced by an independent exact 16-tool allowlist, including
the already-shipped `clinx_get_prepared_request`; no runtime tool is added.
Release activation and formal connector results are recorded separately in
`/data/artifacts/clinx-control-release-20261012/`.
