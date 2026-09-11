# PVX-1807 - Provider Adapter Remediation Qualification

## Scope and authority

This qualification was executed in the isolated development copy
`/home/pvxlabs/dev/clinx-pvx1807-remediation` under the direct operator
instruction recorded as:

```text
AUTHORITY_SOURCE=USER_DIRECT_OPERATOR_INSTRUCTION
EXECUTION_SCOPE=ISOLATED_DEVELOPMENT_COPY
CLINX_MANAGED_EXECUTION_AUTHORITY=NOT_CLAIMED
HISTORICAL_TASK_MUTATION=NOT_PERFORMED
ISOLATED_DEVELOPMENT_AUTHORITY_VERIFIED=PASS
```

The copy is an independent clone with independent `.git` metadata. It was
created from the fixed remediation BASE
`a85677228dea28e20339b25ee22d5ea9f3b04cfe` on feature branch
`pvx-1807-remediation`. The original `/home/pvxlabs/dev/clinx` checkout was
left clean on `main` at that same BASE; no fetch, checkout, reset, restore,
clean, merge, cherry-pick, or metadata write was run there. No repository-local
`AGENTS.md` exists; the applicable `/home/pvxlabs/AGENTS.md` machine policy was
observed.

PVX-1800 task `task_322fd95b8e8a480387460c24e763d0a7` and its lease were not
cancelled, recovered, released, deleted, or modified. No CLINX runtime grant,
canonical registration, runtime database record, provider takeover, physical
worktree, service, tunnel, production database, or live provider was used.

The supplied P620 red-phase evidence (`11 failed, 1 passed`) remains historical
evidence for the original full-repository counterexample. It is not relabeled
as this repaired copy's result. The isolated candidate PA matrix below was
also run against the pre-remediation BASE in a disposable red clone before
the green run; failures are recorded separately from the final results.

## Remediation contracts

| Gate | Contract and green evidence |
| --- | --- |
| `PA-01` | Provider calls use the opaque provider thread handle, including exact interrupt routing. Reconnect creates generation `2`, resumes the same handle, and rebinds the same execution/attempt/operation identity. Generation-1 events are stale and non-authoritative. |
| `PA-02` | `_RecordingTransport` keeps per-request send evidence, so nested server requests cannot overwrite `turn/start` state. A sent-but-unanswered start is `SideEffectUnknown`; the whole adapter connection is quarantined and no second provider request is admitted. |
| `PA-03` | Normalization checks complete session/thread/turn/operation/generation context. A known terminal event is exact once; later matching evidence is `historical`. Unknown terminal statuses are not terminal. |
| `PA-04` | Dynamic tool handling is bound to namespace/name, provider thread, current turn, operation closure, and connection generation. Continuations and reconnects use the current turn binding. |
| `PA-05` | A zero-timeout empty poll is `observed` with no events and leaves the adapter `connected`. A closed transport is `transport_loss` and `disconnected`; explicit cleanup is idempotent and `closed`. Native-id and terminal-operation history are bounded. |

The adapter still exposes provider evidence only. It has no Task/Execution/
Attempt finalizer, lease release, runtime-control write, shadow-ledger write,
Linear write, or production authority path.

## Files and source evidence

The actual files imported by the isolated tests were all under
`/home/pvxlabs/dev/clinx-pvx1807-remediation`:

| File | SHA-256 |
| --- | --- |
| `provider_adapters/contracts.py` | `6d314b6a7ec363b7b5ada308e5198042420bddf2344410998a9217ddedfef7b6` |
| `provider_adapters/codex.py` | `4c1e513fa73e32ba000c7de58047289a5052a71755955cc6feaaeb165fa9fd8f` |
| `provider_adapters/fixtures.py` | `b7ffad678bc21a305a79e285fd2754e9cc78c40e1f93642f2e3c7cac8292c9bf` |
| `provider_adapters/protocol.py` | `54fc5137392569cf52031e9cac531bd78d7d6c4e022af6018c5871dfdf9cf584` |
| `app_server.py` | `f554b8f64c8fb8182be6014f294b3a855c7b7c51fac352f14eb151ce9ef63c77` |
| `test_provider_adapters.py` | `be8841e14f0ee15f977d3be5958894466092cd6abfe85ce7d75cab94a56bed76` |

The candidate tests were kept outside the repository at
`/tmp/pvx1807-remediation-tests/test_pa_matrix.py`. They imported the real
`CodexAppServerClient` and the strict `ScriptedTransport`; only the transport
was scripted. The client, adapter, normalizer, and transport were not all
mocked.

## Commands and results

All commands below were run from the isolated copy with
`PYTHONPATH=/home/pvxlabs/dev/clinx-pvx1807-remediation` where shown:

```text
python3 -m pytest -q /tmp/pvx1807-remediation-tests/test_pa_matrix.py
5 passed

PYTHONPATH=/tmp/clinx-pvx1807-red:/tmp/pvx1807-remediation-tests python3 -m pytest -q /tmp/pvx1807-remediation-tests/test_pa_matrix.py
5 failed (pre-remediation BASE red clone; expected PA counterexamples)

python3 -m pytest -q test_provider_adapters.py
10 passed

python3 -m pytest -q test_m13b.py test_bridge.py test_pvx1805_v1_compatibility.py
134 passed, 12 subtests passed

python3 -m pytest -q test_domain.py test_shadow_ledger.py test_pvx1805_v1_compatibility.py
69 passed, 18 subtests passed

python3 -m pytest -q test_pvx1806_remediation.py test_runtime_control.py test_runtime_wiring.py
108 passed

python3 -m pytest -q
481 passed, 66 subtests passed

python3 -m compileall -q provider_adapters app_server.py
PASS

python3 -m compileall -q .
PASS

git diff --check
PASS
```

The full suite includes the Domain, Shadow, PVX-1805 compatibility, and
PVX-1806 runtime/guard regressions. The focused adapter/client, V1 default
path, and compatibility commands were run before the full suite. No tests
were skipped, deleted, weakened, or double-counted.

The implementation was committed normally as
`7a0445e74c2540fd776366784354b4638111112e` and pushed to feature branch
`pvx-1807-remediation`. The HTTPS `origin` URL remained unchanged; the push
used the machine's existing SSH Git authentication after HTTPS reported that
no non-interactive username credential was available. No force push was used.

The disposable pre-remediation red clone was run with the same candidate file
and produced failures in the intended PA cases: provider-handle interrupt
routing, generation rebind, nested-request side-effect classification,
historical terminal isolation, and empty-poll/closed-transport distinction.
Those failures are red evidence only; the final green result is the isolated
copy result above.

## Qualification gates

| Gate | Status | Evidence or boundary |
| --- | --- | --- |
| `ISOLATED_DEVELOPMENT_AUTHORITY_VERIFIED` | `PASS` | Direct operator instruction, independent clone and metadata, fixed BASE, applicable AGENTS policy checked. |
| `PA-01_PROVIDER_HANDLE_AND_RECONNECT_CONTINUITY` | `PASS` | Candidate matrix; opaque `thread-a` routing, generation increment, same execution/attempt/operation, stale event rejection. |
| `PA-02_REQUEST_SCOPED_SIDE_EFFECT_AND_ADMISSION` | `PASS` | Candidate matrix; nested tool request cannot erase sent `turn/start` evidence and no duplicate start is sent. |
| `PA-03_EVENT_HISTORY_CONTEXT_AND_TERMINAL_SCHEMA` | `PASS` | Candidate matrix; complete identity checks, `historical` status, and unknown terminal status behavior. |
| `PA-04_CONTINUOUS_TURN_HANDLER_BINDING` | `PASS` | Candidate matrix plus adapter test; namespace/thread/turn and operation/generation closure are enforced. |
| `PA-05_POLL_DISCONNECT_AND_LIFECYCLE_BOUNDS` | `PASS` | Candidate matrix; empty poll, hard loss, `connected`/`disconnected`/`closed`, bounded histories. |
| `CODEX_REAL_CLIENT_ADAPTER_CONFORMANCE` | `PASS` | Strict scripted transport through the real `CodexAppServerClient`; no client/transport/normalizer triple mock. |
| `SYNTHETIC_PROVIDER_CONFORMANCE` | `PASS` | Existing synthetic provider contract test retains explicit unsupported interrupt capability. |
| `V1_DEFAULT_PATH_COMPATIBILITY` | `PASS` | `test_bridge.py`, `test_m13b.py`, and full suite pass; adapter remains explicit and default-off. |
| `PVX1805_COMPATIBILITY` | `PASS` | `test_pvx1805_v1_compatibility.py` and full suite pass. |
| `PVX1806_RUNTIME_GUARD_REGRESSION` | `PASS` | Runtime-control, worker, wiring, and full suite pass. |
| `FULL_SUITE` | `PASS` | `481 passed, 66 subtests passed`. |
| `LINEAR_SYNC` | `NOT_PERFORMED` | No Linear write interface was available or used; no progress/status sync was fabricated. |

## Required runtime boundaries

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
MAIN_BRANCH_PUSH=NOT_PERFORMED
NEXT_PHASE_STARTED=NO
HISTORICAL_TASK_MUTATION=NOT_PERFORMED
```

Remaining limits are intentional: no live provider, deployed service, tunnel,
cross-process recovery, durable provider event subscription, physical process
fencing, second real provider, V2 cutover, production schema, production
deployment, or canonical provider E2E was run. Whether this branch is merged
to `main` is a separate review decision.

## Tool Dispatch Continuation: BASE 44effc05

This continuation uses the same user-direct isolated-development authority:

```text
AUTHORITY_SOURCE=USER_DIRECT_OPERATOR_INSTRUCTION
EXECUTION_SCOPE=ISOLATED_DEVELOPMENT_COPY
CLINX_MANAGED_EXECUTION_AUTHORITY=NOT_CLAIMED
HISTORICAL_TASK_MUTATION=NOT_PERFORMED
ISOLATED_DEVELOPMENT_AUTHORITY_VERIFIED=PASS
BASE=44effc05d86417d110499dd286e5a17035c98a40
MAIN=a85677228dea28e20339b25ee22d5ea9f3b04cfe
```

The review attachments are evidence and test instructions, not an execution
authority. The original checkout `/home/pvxlabs/dev/clinx`, PVX-1800 task and
lease, production databases, management services, and real Providers were
outside this scope.

### PA-04 contract

The adapter constructs the real `CodexAppServerClient` with
`strict_dynamic_tool_binding=True`. A trusted binding is formed only after the
`turn/start` response returns the exact provider turn, while namespace, tool
name, provider thread, operation closure, and connection generation are
already captured by the adapter configuration. A valid pre-response request is
stored in a bounded staging queue and is executed only after an exact turn
match. A request whose turn differs from the response, belongs to a retired
turn, uses a second unconfirmed turn, or fails namespace/name/thread/optional
operation/generation validation receives a visible unsuccessful result and
does not invoke the handler. The queue is finite; a provider that waits for a
tool response before returning `turn/start` therefore yields a bounded
unresolved outcome rather than an unbounded wait or speculative callback.

V1 callers that construct `CodexAppServerClient` without the strict flag keep
the established provisional behavior and response shape. This is an explicit
compatibility boundary; the adapter path is the only caller opting into the
pre-callback exact-binding contract.

Tool request admission is separate from response delivery. Within the live
client/binding scope, each typed request id moves through
`admitted -> staged -> executing -> response_pending -> responded`. Integer
`1` and string `"1"` are different ids. A method/params fingerprint is kept;
reusing an id with different semantics is a protocol conflict. Admission is
recorded before invoking the handler. Handler output, including handler
failure, is cached before sending a response; a transport failure leaves the
record pending and permits response redelivery only. It never re-enters the
handler. A finite capacity rejects new ids explicitly instead of evicting
active replay protection. Retirement clears current records, fences retired
turn ids in a bounded window, and `close()` removes the handler. This is a
same-process/live-binding guarantee, not a cross-process or network
exactly-once claim.

### Red/green evidence and source

Before edits, the supplied complete candidate file was extracted to the
isolated temporary directory `/tmp/pvx1807-tool-boundary.lF8LPT` and run with
`PYTHONPATH=/home/pvxlabs/dev/clinx-pvx1807-remediation`:

```text
python3 -m pytest -q /tmp/pvx1807-tool-boundary.lF8LPT/test_pvx1807_tool_boundary_candidates.py
4 failed, 1 passed
```

The same candidate run after the fix was `5 passed`. The red failures were the
two pre-response callback crossovers, callback replay after response-send
failure, and callback replay after request-capacity eviction. No real Host or
shell command ran; handlers only recorded arguments.

Actual imported source hashes after the fix:

| File | SHA-256 |
| --- | --- |
| `app_server.py` | `21aa1988d74e6c0755cef1e217bec23858f729507ec11eaf05cf4e9678808c05` |
| `provider_adapters/codex.py` | `9888c583b17a88538c5635128247283be955d1ce0ae063a4ced4bf0238105c4a` |
| `provider_adapters/contracts.py` | `6d314b6a7ec363b7b5ada308e5198042420bddf2344410998a9217ddedfef7b6` |
| `test_pvx1807_remediation.py` | `3709a193d5c9f9e7045d5ec28bfc66a1129ec05648e582b3dff060c9254e149f` |

### Commands and results

All commands were run from the isolated repository. `PYTHONNOUSERSITE=1` was
used for the package runs; no absolute `PYTHONPATH` to the original checkout
was used.

```text
PYTHONNOUSERSITE=1 PYTHONPATH=. python3 -m pytest -q test_pvx1807_remediation.py
15 passed

PYTHONNOUSERSITE=1 PYTHONPATH=. python3 -m pytest -q \
  test_provider_adapters.py test_m13b.py test_bridge.py
125 passed

PYTHONNOUSERSITE=1 PYTHONPATH=. python3 -m pytest -q \
  test_domain.py test_shadow_ledger.py test_pvx1805_v1_compatibility.py
69 passed

PYTHONNOUSERSITE=1 PYTHONPATH=. python3 -m pytest -q \
  test_pvx1806_remediation.py test_runtime_control.py test_runtime_wiring.py
108 passed

PYTHONNOUSERSITE=1 PYTHONPATH=. python3 -m pytest -q
496 passed

PYTHONNOUSERSITE=1 PYTHONPATH=. python3 -m compileall -q .
PASS
git diff --check
PASS
```

The full-suite result is the actual count for this checkout. The historical
`493 passed / 66 subtests` and earlier `481 / 66` values remain comparison
evidence only; they are not reused or double-counted. No test was skipped,
deleted, weakened, or replaced by the reviewer harness.

### Acceptance gates

| Gate | Status | Evidence |
| --- | --- | --- |
| `EXACT_BINDING_BEFORE_CALLBACK` | `PASS` | Strict adapter staging and exact post-response attach; resident mismatch and continuous-turn tests |
| `UNKNOWN_PRE_RESPONSE_TURN_HAS_NO_SIDE_EFFECT` | `PASS` | `test_pa04_pre_response_turn_mismatch_has_no_callback_side_effect`; candidate red/green run |
| `OLDER_THAN_PREVIOUS_TURN_ISOLATION` | `PASS` | Delayed turn-1 request is rejected before turn-3 handler; resident continuous-binding test |
| `TOOL_REQUEST_ADMISSION_BEFORE_EXECUTION` | `PASS` | `_ServerRequestRecord` is created before handler invocation |
| `RESPONSE_SEND_FAILURE_NO_HANDLER_REPLAY` | `PASS` | Response-failure resident test and candidate green run |
| `REQUEST_CAPACITY_PRESERVES_NO_REPLAY` | `PASS` | Explicit capacity error retains earlier typed request records |
| `REQUEST_ID_SCOPE_AND_CONFLICT` | `PASS` | Typed integer/string ids and changed-payload conflict resident test |
| `VALID_TOOL_PROGRESS` | `PASS` | Exact turn-1 pre-response request is staged then delivered once; existing adapter test |
| `REPOSITORY_RESIDENT_REGRESSION` | `PASS` | `test_pvx1807_remediation.py` is pytest-discoverable and ran 15 tests |
| `V1_DEFAULT_PATH_COMPATIBILITY` | `PASS` | `test_m13b.py`, `test_bridge.py`, and full suite |
| `PA01_PA02_PA03_PA05_REGRESSION` | `PASS` | Existing residual controls remain green in the 15-test remediation set and full suite |
| `PVX1805_PVX1806_REGRESSION` | `PASS` | PVX-1805 compatibility and PVX-1806/runtime groups passed |
| `FULL_SUITE` | `PASS` | `496 passed` |
| `LINEAR_SYNC` | `NOT_PERFORMED` | Latest review was read; final SHA/branch comment is performed only after delivery push |

The qualification remains scripted conformance and local evidence. It does
not claim canonical Provider E2E, live takeover, physical process fencing,
network exactly-once, or a V2 cutover. Runtime defaults remain unchanged:

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
CROSS_PROCESS_TOOL_EXACTLY_ONCE=NOT_QUALIFIED
MAIN_BRANCH_PUSH=NOT_PERFORMED
HISTORICAL_TASK_MUTATION=NOT_PERFORMED
NEXT_PHASE_STARTED=NO
```

## Wrapped transport lifecycle qualification: BASE e2dbb9a0

This follow-up was performed under the existing direct operator instruction in
the same isolated development copy. It is not a canonical CLINX runtime grant
and did not mutate the original checkout, PVX-1800 task/lease, runtime state,
or production systems.

```text
AUTHORITY_SOURCE=USER_DIRECT_OPERATOR_INSTRUCTION
EXECUTION_SCOPE=ISOLATED_DEVELOPMENT_COPY
CLINX_MANAGED_EXECUTION_AUTHORITY=NOT_CLAIMED
HISTORICAL_TASK_MUTATION=NOT_PERFORMED
BASE=e2dbb9a047cb2f27a2e85c612d13332de1204142
MAIN=a85677228dea28e20339b25ee22d5ea9f3b04cfe
```

### Lifecycle ownership and continuation

The lifecycle evidence chain is concrete transport -> `_RecordingTransport`
-> `CodexAppServerClient` -> `CodexProviderAdapter`. The shared
`transport_lifecycle_state` probe returns `available`, `unavailable`, or
`unknown` from already-exposed local fields. `_RecordingTransport` forwards
the inner result without synthesizing availability. Before every new staged
callback the client requires positive availability evidence, including after
redelivering a cached response. Known closed and unknown transports cannot
start new callback work; no business request, handler, timeout, or Host command
is used as a health probe.

On a known closed transport, an executed request keeps its cached response
state and the unexecuted tail remains reachable as `staged`. The adapter keeps
the operation in `_pending_dynamic_batches` and does not register it as
accepted. After controlled recovery of the same client and binding,
`resume_dynamic_tool_batch` retries delivery or flushes the tail exactly once
without resending `turn/start`. This does not transfer the guarantee to a new
client or process and does not establish network exactly-once or physical
fencing.

### Red and green evidence

The attachment ZIP contained the named candidate, extracted to an isolated
temporary directory. The actual imported source paths at BASE were
`/home/pvxlabs/dev/clinx-pvx1807-remediation/app_server.py` and
`/home/pvxlabs/dev/clinx-pvx1807-remediation/provider_adapters/codex.py`.

| Source | SHA-256 at BASE | SHA-256 after fix |
| --- | --- | --- |
| `app_server.py` | `f2ab41c2af940f45e1047cb2894e7e51135d1393dabd6085cf4d04e5632c0b26` | `9674fe045e8658d79bdd2bfd88bac9bf0cb2e9ffad407155948daabe8dfe10c5` |
| `provider_adapters/codex.py` | `6fc98e4e6e0c9d4d954e86e9bf1093aac1174e7db132b93d8da4e251b49f5775` | `0e3a7eb0716b2dcd022fd4be12540c9bb3f99d713e86c5fe801d5fbbb73df3af` |

Before implementation:

```text
PYTHONNOUSERSITE=1 PYTHONPATH=/home/pvxlabs/dev/clinx-pvx1807-remediation \
  python3 -m pytest -q \
  /tmp/pvx1807-transport-state-review.ESF4tc/test_pvx1807_transport_state_candidates.py
2 failed, 2 passed
```

Both failures were wrapper paths: one called `r1` after close before the first
callback, and one called `r2` after cached `r1` delivery closed the inner
transport. Both direct-client controls passed. After implementation, the same
command produced `4 passed`.

### Repository regression mapping

| Contract | Repository evidence | Result |
| --- | --- | --- |
| Wrapped close before first callback | `test_pa04_wrapped_transport_lifecycle_blocks_callbacks_and_preserves_tail[before_first_callback]` | PASS |
| Wrapped close after cached response | `test_pa04_wrapped_transport_lifecycle_blocks_callbacks_and_preserves_tail[after_cached_response]` | PASS |
| Direct/wrapped equivalence | `test_pa04_direct_transport_lifecycle_matches_wrapped_contract` | PASS (2 parameter cases) |
| Unknown evidence is not authorization | `test_pa04_unknown_transport_lifecycle_does_not_authorize_strict_callback` | PASS |
| Same-client continuation and no start resend | Wrapped lifecycle test plus `test_pa04_staged_batch_adapter_failure_has_explicit_continue_entry` | PASS |
| Existing batch, typed-id, capacity, cache, wrong-turn and retirement controls | `test_pvx1807_remediation.py` | PASS |

Actual regression commands and results:

```text
python3 -m pytest -q test_pvx1807_remediation.py
29 passed
python3 -m pytest -q test_pvx1807_remediation.py test_provider_adapters.py
39 passed
python3 -m pytest -q test_bridge.py test_pvx1805_v1_compatibility.py
74 passed
python3 -m pytest -q test_domain.py test_shadow_ledger.py
60 passed
python3 -m pytest -q test_pvx1806_remediation.py test_runtime_control.py test_runtime_wiring.py
108 passed
python3 -m pytest -q
510 passed, 66 subtests passed
```

The historic `505 passed / 66 subtests` result is retained only as the prior
baseline. No test was skipped, deleted, weakened, or counted as a substitute
for the full suite.

### Transport-state gates

| Gate | Status | Evidence |
| --- | --- | --- |
| `TRANSPORT_LIFECYCLE_SURVIVES_WRAPPING` | `PASS` | Explicit wrapper delegation and real adapter regression |
| `KNOWN_CLOSED_BEFORE_FIRST_CALLBACK_REJECTED` | `PASS` | Callback count remains zero and both requests remain staged |
| `KNOWN_CLOSED_AFTER_CACHED_RESPONSE_REJECTED` | `PASS` | Cached `r1` becomes responded; closed state blocks `r2` |
| `SAME_CLIENT_CONTINUATION_RETAINS_TAIL` | `PASS` | Controlled recovery completes the preserved tail |
| `NO_CALLBACK_REPLAY_OR_START_RESEND` | `PASS` | One callback per request and one wire `turn/start` |
| `VALID_TRANSPORT_PROGRESS` | `PASS` | Existing normal and temporary-response-failure batch controls |
| `DIRECT_AND_ADAPTER_CONTRACT_EQUIVALENCE` | `PASS` | Two direct and two wrapped position cases pass |
| `REPOSITORY_RESIDENT_REGRESSION` | `PASS` | Default pytest discovers `test_pvx1807_remediation.py` |
| `V1_DEFAULT_PATH_COMPATIBILITY` | `PASS` | Bridge/V1 compatibility group: `74 passed` |
| `PVX1805_PVX1806_REGRESSION` | `PASS` | PVX-1805 plus PVX-1806/runtime groups pass |
| `FULL_SUITE` | `PASS` | `510 passed, 66 subtests passed` |
| `LINEAR_SYNC` | `PASS` | Delivery comment `21817f24-8d9b-4db3-b60c-8bab073d749e`; issue remains `In Review` |

The qualification remains local scripted conformance, not canonical Provider
E2E. V1 remains the only live authority; provider adapter, worker runtime, and
shadow defaults remain off. No production migration, deployment, real
Provider takeover, cross-process exactly-once qualification, main push,
historical task mutation, or next phase was performed.

## Residual Continuation: BASE 9521720

This section records the follow-up remediation requested for the residual
contracts. It was performed in the same user-authorized isolated development
copy, not through a CLINX managed execution grant. The original checkout,
PVX-1800 task, and its lease were left untouched.

```text
AUTHORITY_SOURCE=USER_DIRECT_OPERATOR_INSTRUCTION
EXECUTION_SCOPE=ISOLATED_DEVELOPMENT_COPY
CLINX_MANAGED_EXECUTION_AUTHORITY=NOT_CLAIMED
HISTORICAL_TASK_MUTATION=NOT_PERFORMED
ISOLATED_DEVELOPMENT_AUTHORITY_VERIFIED=PASS
BASE=9521720a40842b8e3f44f11f05cba6c08a80fa20
MAIN=a85677228dea28e20339b25ee22d5ea9f3b04cfe
```

### Red and Green Evidence

The two supplied reviewer candidate files were rerun against the actual
isolated repository before edits. The reconstruction values in the attached
review are retained as historical context only. Red results were:

```text
python3 -m pytest -q test_pvx1807_review_candidates.py
8 passed, 4 failed
python3 -m pytest -q test_pvx1807_additional_candidates.py
3 failed
```

The failures reproduced the possible-send retry, historical observer drain,
substituted context authorization, old-handler crossover, operation-history
replay, and terminal-before-EOF evidence loss. After remediation, the same
15 candidates passed:

```text
PYTHONPATH=/home/pvxlabs/dev/clinx-pvx1807-remediation python3 -m pytest -q \
  /tmp/<isolated-review-candidates>/test_pvx1807_review_candidates.py \
  /tmp/<isolated-review-candidates>/test_pvx1807_additional_candidates.py
15 passed
```

The temporary path above describes the command shape without making it a
repository dependency. Formal tests are resident in
`test_pvx1807_remediation.py` and passed `12 passed` from a normal checkout.
They use the real `CodexAppServerClient`, the real adapter and normalizer, and
a strict scripted transport; only the transport is deterministic fixture code.

| PA contract | Original counterexample | Repository test mapping | Red evidence | Green evidence |
| --- | --- | --- | --- | --- |
| `PA-01` retained | Handle/generation continuity regression from prior review | `test_pa01_handle_routing_and_generation_continuity` | Prior branch counterexample retained | Opaque handle interrupt and generation continuity pass |
| `PA-02` | Possible partial write retried after reconnect; nested request/malformed reply erased evidence | `test_pa02_possible_send_is_quarantined_across_reconnect_and_new_id`, `test_pa02_definitely_unsent_and_remote_rejection_remain_recoverable`, `test_pa02_malformed_post_send_reply_and_nested_request_stay_unknown` | `8/4` candidate red plus `3` additional failures | Resident PA-02 tests pass; wire start count remains one for uncertain outcomes |
| `PA-03` | Historical observer consumed B terminal; foreign execution/attempt context became exact | `test_pa03_historical_observer_cannot_consume_active_event`, `test_pa03_full_context_rejection_has_no_state_effect_then_valid_observe`, `test_pa03_unknown_status_does_not_release_active_operation` | Candidate observer/context failures | Resident tests pass; B terminal remains available and invalid input has no state effect |
| `PA-04` | New handler received old turn; legal new turn was rejected | `test_pa04_continuous_tool_binding_rejects_old_turn_and_runs_new_once`, `test_pa04_duplicate_tool_request_id_gets_one_response_and_one_call` | Candidate handler crossover failure | Resident tests pass; old turn is rejected and new request executes once |
| `PA-05` | History eviction enabled old-id replay; terminal before EOF was lost; method history grew unbounded | `test_pa05_capacity_rejection_preserves_no_replay`, `test_pa05_consumed_terminal_survives_hard_eof`, `test_pa05_lifetime_histories_are_bounded_and_empty_poll_is_not_eof` | `3` additional failures | Resident tests pass; capacity rejects, EOF carries evidence, histories are bounded |

### Source and Regression Commands

The actual imported source files before edits were under the isolated copy:

| File | SHA-256 at BASE 9521720 | SHA-256 after remediation |
| --- | --- | --- |
| `app_server.py` | `f554b8f64c8fb8182be6014f294b3a855c7b7c51fac352f14eb151ce9ef63c77` | `d7580100bcf3442f6e9f19f868e12a4bd44db5bed9e013d801f168d5076171d9` |
| `provider_adapters/codex.py` | `4c1e513fa73e32ba000c7de58047289a5052a71755955cc6feaaeb165fa9fd8f` | `cb7eb7e8ae41af8f32a9e8b277c287abd3d6e1218adbdeede0bcac1828d59571` |
| `provider_adapters/contracts.py` | `6d314b6a7ec363b7b5ada308e5198042420bddf2344410998a9217ddedfef7b6` | unchanged |
| `test_provider_adapters.py` | `be8841e14f0ee15f977d3be5958894466092cd6abfe85ce7d75cab94a56bed76` | unchanged |

The formal regression commands and actual results were:

```text
python3 -m pytest -q test_pvx1807_remediation.py
12 passed
python3 -m pytest -q test_provider_adapters.py test_bridge.py \
  test_pvx1805_v1_compatibility.py test_pvx1806_remediation.py \
  test_runtime_control.py test_runtime_wiring.py
192 passed, 12 subtests passed
python3 -m pytest -q
493 passed, 66 subtests passed
python3 -m compileall -q .
PASS
git diff --check
PASS
```

The historical `481 passed / 66 subtests` value is not reused as the current
result; the current full suite includes the 12 new resident tests. No tests
were skipped, removed, weakened, or counted twice.

### Acceptance Gates

| Gate | Status | Evidence |
| --- | --- | --- |
| `PA01_RECORDED_COUNTEREXAMPLES_REGRESSION` | `PASS` | Prior PA-01 tests plus resident handle/generation test |
| `POSSIBLY_SENT_START_REMAINS_QUARANTINED` | `PASS` | Resident PA-02 uncertain-send and malformed-reply tests |
| `RECONNECT_CANNOT_REPLAY_UNRESOLVED_START` | `PASS` | Reconnect and new-operation wire-count assertion |
| `HISTORICAL_OBSERVER_PRESERVES_ACTIVE_EVENTS` | `PASS` | Observer is rejected before drain while B is active |
| `FULL_CONTEXT_REJECTED_WITHOUT_STATE_EFFECT` | `PASS` | Invalid execution/attempt/assignment raises before sequence/dedup mutation |
| `CONTINUOUS_TOOL_BINDING_NO_OLD_HANDLER_CROSSOVER` | `PASS` | Old turn rejection, new turn exactly once, duplicate id suppression |
| `HISTORY_RETIREMENT_PRESERVES_NO_REPLAY` | `PASS` | Capacity exhaustion rejects rather than evicts admission identity |
| `CONSUMED_EVENTS_SURVIVE_HARD_EOF` | `PASS` | EOF exception carries normalized terminal evidence |
| `LIFETIME_STATE_BOUNDS` | `PASS` | Method/raw buffers, native ids, request ids, sessions and operations bounded |
| `REPOSITORY_RESIDENT_REMEDIATION_TESTS` | `PASS` | `test_pvx1807_remediation.py` is auto-discovered by pytest |
| `V1_DEFAULT_PATH_COMPATIBILITY` | `PASS` | Bridge, V1, and compatibility regression commands |
| `PVX1805_PVX1806_REGRESSION` | `PASS` | PVX-1805 and PVX-1806/runtime command |
| `FULL_SUITE` | `PASS` | `493 passed, 66 subtests passed` |
| `LINEAR_SYNC` | `PASS` | Linear comment `3cb780dd-f729-4b20-97b6-def9b397ff81` records BASE, feature branch, FINAL, remote readback, tests, and preserved boundaries; issue remains `In Review` |

The qualification remains scripted conformance and local evidence only. It
does not claim canonical provider E2E, live takeover, process fencing, or
external exactly-once delivery.

## Strict staging batch continuation: BASE ac8483d9

This continuation was performed under the same direct operator instruction in
the isolated development copy. It is not a canonical CLINX runtime grant and
does not mutate the original checkout, PVX-1800 task/lease, runtime state, or
production systems.

```text
AUTHORITY_SOURCE=USER_DIRECT_OPERATOR_INSTRUCTION
EXECUTION_SCOPE=ISOLATED_DEVELOPMENT_COPY
CLINX_MANAGED_EXECUTION_AUTHORITY=NOT_CLAIMED
HISTORICAL_TASK_MUTATION=NOT_PERFORMED
BASE=ac8483d909508fe404c2c7f1c3d94e4c3c7338a0
MAIN=a85677228dea28e20339b25ee22d5ea9f3b04cfe
```

### Batch invariant and interruption semantics

Strict staging admits valid same-turn requests before the `turn/start`
response, but does not invoke a handler until the response establishes the
exact binding. After attachment, the queue is flushed in arrival order with a
single current request in flight. The current request is removed only after a
response is cached. If response delivery fails, that request is
`response_pending` and its cached result is the only retryable work; the
unprocessed tail stays `staged` and remains reachable through a repeated
same-binding attach. A duplicate request can redeliver a cached result but
cannot invoke the handler again. Capacity remains finite and admission
identities are not evicted.

An unavailable transport never causes speculative callback execution. A
mismatched staged turn is returned as an explicit unsuccessful tool result;
if its delivery fails, the failure remains cached for typed-id redelivery.
The adapter's `resume_dynamic_tool_batch` is the operation-level continuation
entry: it redelivers pending cached results and flushes the tail before
registering the accepted operation, and never resends `turn/start`.
Retirement and close clear the active queue and fence old turn ids, so a late
request cannot reach a subsequent handler. The guarantee ends with the live
client/binding; no cross-process or network exactly-once behavior is claimed.

### Red/green evidence and repository mapping

The named external candidate file was not present in the supplied attachment
set or `/tmp`. An equivalent independent candidate was reconstructed under
`/tmp/pvx1807-staged-batch-candidates/` against the actual isolated package.
Before the fix it reproduced the stranded-tail counterexample:

```text
PYTHONPATH=/home/pvxlabs/dev/clinx-pvx1807-remediation python3 -m pytest -q \
  /tmp/pvx1807-staged-batch-candidates/test_pvx1807_staged_batch_candidates.py
1 failed
```

After the fix the same candidate passed:

```text
PYTHONPATH=/home/pvxlabs/dev/clinx-pvx1807-remediation python3 -m pytest -q \
  /tmp/pvx1807-staged-batch-candidates/test_pvx1807_staged_batch_candidates.py
1 passed
```

Formal tests are in `test_pvx1807_remediation.py` and use the real client and
adapter with only a scripted transport and recording-only handlers:

| Contract | Repository evidence | Result |
| --- | --- | --- |
| Single/two-request normal progress | `test_pa04_staged_single_and_double_request_progress` | PASS |
| First, middle, and last response failure | `test_pa04_staged_batch_failure_preserves_tail_and_response_cache` | PASS (3 parameter cases) |
| Mismatched-turn failure delivery | `test_pa04_staged_wrong_turn_failure_is_visible_and_retryable` | PASS |
| Adapter/client continuation path | `test_pa04_staged_batch_adapter_failure_has_explicit_continue_entry` (`resume_dynamic_tool_batch`) | PASS |
| Retirement/close fencing | `test_pa04_retire_and_close_fence_staged_requests_from_new_handlers` | PASS |
| Existing PA-04, PA-01/02/03/05 controls | `test_pvx1807_remediation.py` | PASS |

The resident remediation file ran `24 passed` in the focused command. The
historical `496 passed` value remains comparison evidence only and is not
reused as this round's full-suite result. The tests are ordinary pytest files;
no temporary helper, absolute old-checkout `PYTHONPATH`, skipped case, or
weakened assertion is required for a fresh checkout. The final ordinary
fresh-checkout command `python3 -m pytest -q` completed with `505 passed, 66
subtests passed`.

### Batch qualification gates

| Gate | Status | Evidence |
| --- | --- | --- |
| `STAGED_REQUEST_OWNERSHIP_CONSISTENCY` | `PASS` | Queue and typed registry remain aligned across interruption; resident batch tests |
| `BATCH_SEND_FAILURE_NO_STRANDED_REQUEST` | `PASS` | First/middle/last failure cases preserve the unprocessed tail |
| `RETRY_NO_CALLBACK_REPLAY` | `PASS` | Cached response redelivery and repeated attach keep one callback per id |
| `UNEXECUTED_REQUEST_PROGRESS_OR_EXPLICIT_REJECTION` | `PASS` | Tail completes after same-binding attach; wrong turn receives cached unsuccessful result |
| `ADAPTER_CLIENT_FAILURE_CONTRACT_MATCH` | `PASS` | Real adapter -> client -> scripted transport test surfaces `SideEffectUnknown` and explicit client continuation |
| `EXACT_BINDING_SAFETY_REGRESSION` | `PASS` | Existing strict pre-response and turn/namespace/thread/generation tests remain green |
| `REQUEST_SCOPE_CAPACITY_REGRESSION` | `PASS` | Existing typed-id conflict and finite-capacity tests remain green |
| `REPOSITORY_RESIDENT_REGRESSION` | `PASS` | `test_pvx1807_remediation.py` is pytest-discoverable |
| `V1_DEFAULT_PATH_COMPATIBILITY` | `PASS` | Existing bridge/V1 regression and full suite |
| `PVX1805_PVX1806_REGRESSION` | `PASS` | Existing compatibility and runtime/guard groups |
| `FULL_SUITE` | `PASS` | `python3 -m pytest -q`: `505 passed, 66 subtests passed` |
| `LINEAR_SYNC` | `PASS` | Delivery comment `2c08287e-3c31-41d1-ae3f-3c08708b7adb`; issue remains `In Review` |

Runtime boundaries remain unchanged:

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
CROSS_PROCESS_TOOL_EXACTLY_ONCE=NOT_QUALIFIED
MAIN_BRANCH_PUSH=NOT_PERFORMED
HISTORICAL_TASK_MUTATION=NOT_PERFORMED
NEXT_PHASE_STARTED=NO
```
