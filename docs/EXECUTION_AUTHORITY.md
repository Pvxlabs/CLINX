# Canonical Task execution authority

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
Native development remains the default. `network_access=true` enables native
network; omission on continuation preserves the current route's value. Every
managed turn sends its explicit network policy, including false.

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
[授权派发真实闭环验收](AUTHORITY_DISPATCH_ACCEPTANCE_20261007.md)。
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

`test_execution_authority.py` covers CAS/concurrent apply, immutable history,
expiry, explicit approval, unresolved delivery across executions, exact scopes,
real safe controller processes, two candidates and target/resource fencing.
`CLINX_LIVE_AUTHORITY=1 ... test_execution_authority_live.py` compares direct and
CLINX native filesystem/Git/HTTPS on the same user/workspace/profile, then uses the
same Task for DEV → reauthorization → production-like Host execution and history
readback. `test_tool_delivery_live.py` injects a real Provider delivery failure
after a safe local mutation and verifies the counter stays one under replay.
All live tests use disposable registries, Provider endpoints and local targets.
Final counts, exact source/release and production limitations belong in the
accompanying acceptance receipt, not an inferred overall PASS.
