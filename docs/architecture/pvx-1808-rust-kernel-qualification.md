# PVX-1808 Rust Decision Kernel Qualification

## Result

`ADOPTION_RECOMMENDATION=CONTINUE_EVALUATION`

This result qualifies the bounded, non-authoritative prototype only. It does
not approve a production language migration, authority cutover, provider
takeover, or default runtime integration.

## Scope, baseline, and authority

| Item | Value |
| --- | --- |
| Checkout | `/home/pvxlabs/dev/clinx-pvx1807-remediation` |
| Branch | `pvx-1808-kernel-prototype` |
| BASE | `9a21bf19399e9c305f7fb1487feadc73a11da047` |
| BASE TREE | `2c15902c8ec9ea77570d0746b66242a98065ce64` |
| Contract | `CLINX_KERNEL_V1` |
| Scope | Non-authoritative ownership decision and assignment replay |
| Authority source | Direct operator instruction for isolated development copy |
| Original checkout | `/home/pvxlabs/dev/clinx`, not modified |
| PVX-1800 task/lease | Not read or modified |
| Production/managed execution | Not used |

The supplied SPEC and pasted request were treated as technical execution
instructions for this checkout, not as production or managed-execution
authority. The implementation stayed within `kernel/`, `kernel_lab/`,
`fixtures/kernel_v1/`, `test_kernel_conformance.py`, `.gitignore`, and the two
new architecture documents.

## Delivered implementation

| Area | Files | Boundary |
| --- | --- | --- |
| Rust library and evaluation CLI | [`kernel/src/lib.rs`](../../kernel/src/lib.rs), [`kernel/src/bin/clinx-kernel-eval.rs`](../../kernel/src/bin/clinx-kernel-eval.rs) | Pure typed ownership decision and assignment replay |
| Rust controlled benchmark | [`kernel/src/bin/clinx-kernel-bench.rs`](../../kernel/src/bin/clinx-kernel-bench.rs) | Local measurement only |
| Python protocol and session driver | [`kernel_lab/protocol.py`](../../kernel_lab/protocol.py) | Bounded NDJSON subprocess boundary |
| Python reference and SQLite traces | [`kernel_lab/reference.py`](../../kernel_lab/reference.py), [`kernel_lab/traces.py`](../../kernel_lab/traces.py) | Existing reducer comparison and real temporary store traces |
| Qualification and cost measurement | [`kernel_lab/qualification.py`](../../kernel_lab/qualification.py), [`kernel_lab/benchmark.py`](../../kernel_lab/benchmark.py) | Fail-closed semantic gates and separated workloads |
| Versioned fixtures | [`fixtures/kernel_v1/protocol.schema.json`](../../fixtures/kernel_v1/protocol.schema.json), `assignment_golden.json`, `ownership_golden.json`, `legacy_events.json`, `replay_negative.json` | Repository-resident contract evidence |
| Conformance tests | [`test_kernel_conformance.py`](../../test_kernel_conformance.py) | Actual Python reference versus actual Rust binary |

## Semantic contract and evidence

### Ownership

`evaluate_ownership` consumes a coordinator-trusted snapshot and a complete
request. It checks execution/attempt, worker/incarnation, assignment,
allocation/resource, epoch, expected versions, lifecycle, lease expiry,
trusted time/watermark, and verified safety handoff. Missing or `UNKNOWN`
fields return `INSUFFICIENT_EVIDENCE`; mismatches return
`REJECT_CANDIDATE`; only an exact live match returns `ALLOW_CANDIDATE`.
Results always carry `authority=NON_AUTHORITATIVE`.

The ownership fixture contains 19 manually expected cases. All 19 matched the
independent Python reference, the actual Rust release binary, and the frozen
expected result: `19/19 PASS`. The equality boundary is covered explicitly:
`now == expires_at` is not live. The cases also cover wrong worker and
incarnation, stale epochs and versions, inactive resources, pending or
mismatched safety handoff, incomplete evidence, old receipts, and multiple
executions for one task.

### Assignment replay

`replay_assignment` implements only the `RUNTIME_WORKER_V1` assignment slice:

```text
AssignmentGranted
AssignmentRenewed
AssignmentReleased
AssignmentRevoked
AssignmentExpired
AssignmentOrphaned
AssignmentRecovered
```

Replay validates event envelope fields, event family/schema, assignment stream
identity, contiguous sequence, timestamps, canonical payload hash, identity,
versions, dependent lifecycles, and legal predecessors. Every prefix is
compared, not only the final state. The implementation keeps assignment,
allocation, attempt, and recovery versions distinct; it does not invent a
cross-stream business order.

The current grant-renew-release golden has all prefixes matched. Three legacy
traces (`expire -> recover`, `orphan -> recover`, and `revoke`) all matched
their explicit legacy expectations. Eleven negative fixtures all failed in
both implementations with the expected stable error code. The Python
reference error mapping was tightened during qualification so an unknown
state payload version is consistently `ASSIGNMENT_STATE_INVALID` in both
languages.

### SQLite trace layer

The trace harness uses the public `RuntimeControlStore` API with a real,
temporary SQLite database and an injected `ManualClock`. It reads the stored
assignment events and final persisted fields, then compares each stream prefix
with the Python reducer and actual Rust binary.

Seeds `1808`, `18081`, `18082`, and `18083` each ran 256 attempted operations.
Each seed produced:

```text
attempted=256
effective=8
rejected=217
no_op=31
assignment_streams=5
```

The active owner snapshot was accepted by both the ownership oracle and Rust;
all assignment streams matched their temporary SQLite persisted state. The
trace is a repeatable finite qualification workload, not a production load or
capacity certification.

## Protocol and process qualification

The actual Rust binary and Python driver use `CLINX_KERNEL_V1` NDJSON. The
protocol rejects duplicate fields, unknown envelope fields, bad JSON,
unsupported versions, unknown operations, unsafe integer values, and frames
over 1 MiB. The Python side bounds responses, stderr, event count, process
requests, and timeout. The child process writes protocol only to stdout and
bounded diagnostics to stderr.

`python3 -m pytest -q test_kernel_conformance.py` completed with:

```text
19 passed
```

Those tests include actual-binary malformed JSON, duplicate-field and
unknown-field rejection; unsupported protocol and operation; integer and
frame bounds; response/stderr bounds; truncated output; wrong response ID;
timeout; exit-before-response; exit-after-response; restart replay;
request correlation; batch isolation; and client-side frame bounds. No test
starts a real Provider, Host executor, management service, or production
worker.

## Rust and Python implementation checks

The following Rust checks passed during implementation. The document pass
does not change Rust sources; the Python/documentation post-checks are
recorded separately below.

```text
cargo fmt --all -- --check                         PASS
cargo check --locked                              PASS
cargo test --locked                               3 passed; doc tests passed
cargo clippy --all-targets --all-features --locked -- -D warnings
                                                   PASS
cargo build --release --locked                    PASS
python3 -m compileall -q .                        PASS
git diff --check                                  PASS
```

Release artifacts used by qualification:

```text
/home/pvxlabs/dev/clinx-pvx1807-remediation/kernel/target/release/clinx-kernel-eval
/home/pvxlabs/dev/clinx-pvx1807-remediation/kernel/target/release/clinx-kernel-bench
```

The release evaluator artifact was 893,224 bytes and the benchmark artifact
was 659,056 bytes in the recorded measurement.

## Existing regression

The current checkout's original test suite was rerun rather than relying on
the historical 510 passed / 66 subtests baseline. The final post-document
command and result are recorded below:

```text
python3 -m pytest -q test_kernel_conformance.py
19 passed

python3 -m pytest -q
529 passed, 66 subtests passed in 10.11s

python3 -m compileall -q .
PASS

git diff --check
PASS
```

The full suite includes the V1, Domain, Shadow, PVX-1805 compatibility,
PVX-1806 runtime/guard, and PVX-1807 adapter/conformance regressions. The
current count is evidence for this checkout only; historical counts remain
historical and were not reused to manufacture the result.

## Controlled cost measurement

The benchmark ran after semantic qualification, with one warmup and five
measured rounds per workload. Inputs and iteration counts were fixed. Times
are wall-clock `perf_counter_ns` measurements. The raw measurement was
captured at `/tmp/pvx1808-benchmark.json`; the table below records its values
in the repository report.

Environment:

```text
Python 3.12.3
rustc 1.98.1 (48a229cea 2026-09-01)
cargo 1.98.1 (797e8a9bc 2026-08-05)
Linux 6.8.0-139-generic x86_64 with glibc 2.39
24 CPUs reported by the host
```

| Workload | Samples | Median | P95 | Max | Unit/qualification meaning |
| --- | ---: | ---: | ---: | ---: | --- |
| Release compile | 5 | 26,534,629 | 27,223,020.2 | 27,343,499 | ns per `cargo build --release --locked` |
| Python ownership reference | 5 | 96,545 | 108,143.2 | 110,211 | ns per one reference decision |
| Python replay reference | 5 | 678,400 | 692,328.8 | 693,429 | ns per one fixture replay |
| Rust core | 5 | 13,108,379 | 14,038,561.6 | 14,251,219 | ns per 2,000 in-process core iterations |
| Rust serde | 5 | 25,358,885 | 26,853,755.6 | 27,195,294 | ns per 2,000 serde iterations |
| Python to Rust round trip | 5 | 2,622,836 | 2,694,443.0 | 2,711,215 | ns per one subprocess request |
| Cold start | 5 | 2,344,864 | 2,554,517.2 | 2,563,242 | ns per new-process replay request |
| Hot batch, 16 requests | 5 | 3,268,754 | 3,832,752.4 | 3,862,822 | ns for one process and 16 requests |
| Python serialization/parsing | 5 | 33,284 | 37,618.2 | 38,634 | ns per bounded encode/decode |
| Rust RSS | 5 | 2,112 | n/a | 2,112 | KB, `/usr/bin/time` max RSS |
| Python child `ru_maxrss` | 1 aggregate | 29,568 | n/a | 29,568 | KB, process children aggregate |

The Rust core and Rust serde rows are benchmark-binary measurements over
2,000 iterations; they are not directly comparable to one Python call or to
the process round-trip rows. The results do not support a production
throughput, P99, capacity, multi-machine consistency, or long-run stability
claim. No speedup threshold was assumed.

## Qualification gates

| Gate | Status | Evidence or boundary |
| --- | --- | --- |
| `SOURCE_BASELINE_VERIFIED` | `PASS` | Fixed BASE and BASE TREE verified in the authorized independent checkout. |
| `LANGUAGE_NEUTRAL_CONTRACT_VERSIONED` | `PASS` | `CLINX_KERNEL_V1` schema, bounded protocol, explicit integer/time/unknown-field rules, and both-language validation. |
| `RUST_BINARY_BUILT_AND_EXECUTED` | `PASS` | Locked release build, evaluator and benchmark artifacts, actual binary conformance and qualification runs. |
| `OWNERSHIP_DECISION_DIFFERENTIAL` | `PASS` | 19/19 ownership golden cases matched Python, Rust, and independent expected values. |
| `ASSIGNMENT_REPLAY_DIFFERENTIAL` | `PASS` | Every current and legacy replay prefix matched the Python reference and expected state. |
| `INDEPENDENT_GOLDEN_INVARIANTS` | `PASS` | Human-authored expected values were checked independently; no implementation updated a fixture. |
| `INVALID_INPUT_FAILS_CLOSED` | `PASS` | 11 negative fixtures plus malformed protocol, bounds, unknown schema, identity, hash, timestamp, and transition failures. |
| `LEGACY_EVENT_COMPATIBILITY` | `PASS` | Three legacy traces preserve explicit upcast/unknown behavior and match both implementations. |
| `MULTI_EXECUTION_IDENTITY_ISOLATION` | `PASS` | Ownership fixtures and SQLite traces reject cross-execution/attempt identity crossover. |
| `BOUNDED_PROTOCOL_AND_FAILURE_HANDLING` | `PASS` | 19 focused conformance tests cover correlation, limits, timeout, truncation, exits, stderr, restart, and batch isolation. |
| `PURE_KERNEL_NO_SIDE_EFFECTS` | `PASS` | Rust operations consume self-contained input only; no database, clock, provider, lease, guard, command, or filesystem authority path. |
| `PYTHON_DEFAULT_PATH_UNCHANGED` | `PASS` | New modules are explicit qualification dependencies; full existing suite remains green. |
| `PVX1805_PVX1806_PVX1807_REGRESSION` | `PASS` | Full suite includes the accepted V1, compatibility, runtime/guard, and provider-adapter regressions. |
| `REPOSITORY_RESIDENT_CONFORMANCE` | `PASS` | Fixtures, driver, reference, Rust source, and `test_kernel_conformance.py` are checked into the branch. |
| `COST_MEASUREMENT_RECORDED` | `PASS` | One warmup plus five rounds, fixed inputs, separated workloads, environment and RSS method recorded above. |
| `FULL_QUALIFICATION` | `PASS` | Semantic gates, actual SQLite traces, protocol tests, Rust checks, full Python regression, and cost measurement completed. |
| `LINEAR_SYNC` | `NOT_RUN` | No Linear mutation was performed; final delivery records `LINEAR_SYNC=NOT_PERFORMED`. |

## Fixed production boundary

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
LINEAR_SYNC=NOT_PERFORMED
```

## Residual non-goals and next evidence

The recommendation to continue evaluation is intentionally narrow. Before
any production adoption, a separate task must qualify real transaction/write
point fencing, process supervision and crash/restart boundaries, Provider live
E2E and canonical wire behavior, deployment and observability, load and tail
latency, multi-machine behavior, and ongoing maintenance cost. This branch
does not claim any of those conditions.

## Delivery record

The branch is limited to one ordinary commit and one feature-branch push. No
main push, force push, merge, deployment, service restart, PVX-1800 mutation,
or next phase was performed. The final commit, tree, clean status, and remote
readback are recorded in the delivery response after the post-document
verification completes.
