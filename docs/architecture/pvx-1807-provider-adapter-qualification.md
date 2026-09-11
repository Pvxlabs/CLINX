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
| `provider_adapters/contracts.py` | `2fe870d0c2294e249d3243a18ca013c290c735512e4030c68fe64bac7d551e84` |
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
