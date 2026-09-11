# PVX-1808 Rust Decision Kernel Qualification

## Result

`IMPLEMENTATION_QUALIFICATION=PASS`

`ADOPTION_RECOMMENDATION=DEFER`

The implementation result applies only to the isolated, non-authoritative
decision/replay prototype. It is not approval for Rust authority, a production
language migration, provider takeover, default runtime integration, or a
change to the V1 live authority.

## Scope and Baseline

| Item | Value |
| --- | --- |
| Checkout | `/home/pvxlabs/dev/clinx-pvx1807-remediation` |
| Branch | `pvx-1808-kernel-prototype` |
| BASE | `3f5f0320eb579d6df2f4cd90def131078838d413` |
| BASE tree | `60b81ea140b587e6cab871a8b0933e167c47c191` |
| Main preservation anchor | `9a21bf19399e9c305f7fb1487feadc73a11da047` |
| Contract | `CLINX_KERNEL_V1` |
| Authority | V1 Python runtime-control remains the only live authority |
| Rust result | `NON_AUTHORITATIVE` decision/replay evidence only |
| Original checkout | `/home/pvxlabs/dev/clinx`, not modified |
| Production DB / service / Provider / Host | Not accessed, restarted, started, or mutated |

The attached SPEC and pasted request were treated as technical instructions
for this isolated checkout. They did not grant managed execution, production
access, authority cutover, or permission to modify the original checkout.

## Reproduction And Correction

The review reproduction was executed against the repository-resident files
and the actual release binary after the initial implementation:

```text
python3 -m pytest -q test_pvx1808_protocol_candidates.py
3 failed, 1 passed                         RED

python3 -m pytest -q test_pvx1808_ownership_candidates.py
3 failed, 1 passed                         RED
```

The protocol failures were deadline coverage for blocked stdin, stale
generation/buffer reuse after close, and Python acceptance of `NaN`. The
ownership failures were character-count versus UTF-8-byte limits, invalid
lifecycle acceptance, and zero active ownership epochs.

The corrected repository-resident tests are now:

```text
python3 -m pytest -q test_pvx1808_protocol_candidates.py
4 passed                                  GREEN

python3 -m pytest -q test_pvx1808_ownership_candidates.py
5 passed                                  GREEN

python3 -m pytest -q test_pvx1808_benchmark_metrics.py
1 passed                                  GREEN
```

K1 uses one monotonic deadline for non-blocking stdin writes, pipe delivery,
stdout/stderr draining, and response reading. Timeout, protocol failure, and
child failure perform bounded cleanup and permanently close the contaminated
session. `restart()` explicitly creates a new process generation and clears
buffers, request associations, and request count. Python rejects non-finite
JSON, duplicate fields, invalid JSON, excessive nesting, and operation result
shapes. Rust reads through bounded `BufRead::fill_buf`/`consume` chunks into a
frame bounded at read time; delimiter handling preserves the next buffered
frame. An oversized line receives one `FRAME_TOO_LARGE` response and the
finite evaluator process exits.

K2 uses the runtime schema lifecycle sets, UTF-8 byte limits, positive
ownership epochs, and separate non-negative versions/counters in both
languages. `UNKNOWN` and `NULL` remain `INSUFFICIENT_EVIDENCE`; an unrecognized
lifecycle is `INVALID_INPUT`; legal non-live lifecycle values are structured
rejections. The trace harness calls the actual temporary SQLite
`RuntimeControlStore`, reads actual entities and assignment events, creates a
real pending safety handoff, completes and clears it at the existing
runtime-control boundary, and reads back the coordinator watermark. Its
`VERIFIED` snapshot state is derived from that completion, not hand-filled.
Each trace operation declares the expected `success`, `rejected`, or `no_op`
outcome and its allowed runtime error category. Unexpected exceptions escape.

K3 separates the Rust benchmark's returned in-process `elapsed_ns` from the
outer subprocess wall time and keeps `iterations`. It separately measures
`core`, `parse_evaluate_serialize`, and `serde`, and separately records cold
process requests and a pre-started, warmed session batch. Repeated unchanged
builds are labeled `UP_TO_DATE_BUILD`; one clean build uses a temporary
`CARGO_TARGET_DIR`; incremental build is explicitly `NOT_MEASURED`. Python
and Rust RSS use the same ownership JSON workload and one fresh child process
under `/usr/bin/time %M`. Raw samples are checked in rather than retained only
in `/tmp`:

```text
fixtures/kernel_v1/benchmark_samples.json
```

## Contract And Evidence Mapping

The finite evidence path is:

```text
existing runtime-control validation
  -> runtime snapshot fields and actual transaction readback
  -> independent expected decision
  -> ownership/replay golden expectation
  -> Python reference
  -> actual Rust release binary
```

Existing `runtime_control` rules were not altered to make Rust agree. The
assignment replay path continues to compare every prefix against the existing
reducer, temporary SQLite persisted state, and independent expected values.
Legacy events and historical hashes remain in place.

The Rust binary was imported only by qualification modules and tests. No
normal V1, bridge, MCP, Finalizer, Provider adapter, TaskRegistry, or worker
runtime path imports it. `ALLOW_CANDIDATE` cannot grant a lease, guard,
write-point permission, or recovery authority.

## Qualification Evidence

### Semantic and Runtime Evidence

```text
19/19 ownership golden cases matched independent expected values,
Python reference, and actual Rust release binary.
11/11 replay negative fixtures rejected with the same stable error code.
Current and all three legacy assignment traces matched every prefix.
Seeds: 1808, 18081, 18082, 18083.
Operations per seed: 256 attempted, each operation declaration retained.
Per seed: 7 success, 218 rejected, 31 no-op, 26 total runtime events.
Handoff evidence: pending row read back, committed and cleared.
Watermark evidence: SQLite readback, not watermark=now fixture synthesis.
```

### Rust And Python Checks

```text
cargo fmt --all -- --check                                      PASS
cargo check --locked                                          PASS
cargo test --locked                                           PASS
cargo clippy --all-targets --all-features --locked -- -D warnings PASS
cargo build --release --locked                                 PASS
python3 -m compileall -q .                                     PASS
git diff --check                                               PASS
```

Rust test detail: library 3 tests, evaluator frame-reader 4 tests, benchmark
binary test target has no unit tests, and doc tests passed. The evaluator frame
reader tests cover the inclusive delimiter boundary, oversized content without
frame growth, partial EOF, and preservation of the next frame.

### Regression

```text
python3 -m pytest -q test_kernel_conformance.py                 20 passed
python3 -m pytest -q                                            540 passed, 66 subtests passed
```

The full result is from this checkout and includes the original repository
regressions plus the eleven new protocol, ownership, runtime-trace, and
benchmark provenance tests. The historical `529 passed / 66 subtests` count was not
reused or recomputed as the final result.

### Corrected Cost Measurement

Source and raw result: `fixtures/kernel_v1/benchmark_samples.json`. The run
used Python 3.12.3, rustc 1.98.1, cargo 1.98.1, Linux 6.8.0-139-generic,
x86_64, and five measured rounds after one warmup for each timed workload.
The following are summary medians from the checked-in raw sample set:

| Workload | Iterations | Returned/core median ns | Outer wall median ns | Meaning |
| --- | ---: | ---: | ---: | --- |
| Rust `core` | 2,000 | 12,396,647 | 13,255,502 | Evaluate already-parsed input; returned field is in-process |
| Rust `parse_evaluate_serialize` | 2,000 | 24,890,869 | 25,786,964 | Full parse, evaluate, serialize loop |
| Rust `serde` | 2,000 | 12,984,821 | 14,073,870 | Parse and serialize only |

| Workload | Median ns | P95 ns | Max ns |
| --- | ---: | ---: | ---: |
| Python ownership reference | 170,052 | 202,583.6 | 203,305 |
| Python replay reference | 508,802 | 601,109.2 | 618,440 |
| Python to Rust round trip | 2,370,123 | 2,418,195.4 | 2,419,025 |
| Cold process request | 2,464,602 | 2,588,447.8 | 2,596,080 |
| Warm session batch, 16 requests | 2,293,838 | 2,346,559.8 | 2,347,289 |
| Python bounded encode/decode | 187,425 | 197,628.2 | 199,067 |

Build labels and memory:

```text
UP_TO_DATE_BUILD: five measured rounds, median 25,426,784 ns
CLEAN_BUILD: one temporary-target sample, 3,921,177,579 ns
INCREMENTAL_BUILD: NOT_MEASURED
RSS: same ownership workload, one fresh child under /usr/bin/time %M
Rust RSS median: 2,112 KB
Python RSS median: 20,736 KB
```

The Rust rows are 2,000-iteration benchmark-binary measurements and are not
comparable to one Python call or a subprocess request. No speedup threshold
was assumed. These measurements do not support production throughput, P99,
capacity, multi-machine, or long-run stability conclusions. The previous
measurement record remains historical evidence with its old core/wall labels;
this raw file and table are the K3 corrected record.

## Acceptance Gates

| Gate | Status | Evidence / boundary |
| --- | --- | --- |
| `END_TO_END_IO_DEADLINE` | `PASS` | Blocked stdin/write-backpressure subprocess plus bounded cleanup regression. |
| `PROCESS_GENERATION_BUFFER_ISOLATION` | `PASS` | Explicit restart generation and stale-buffer discard regression. |
| `FINITE_JSON_RESPONSE_VALIDATION` | `PASS` | NaN, duplicate, malformed, depth, unknown-field, and result-shape checks. |
| `RUST_PREALLOCATION_FRAME_BOUND` | `PASS` | Bounded `BufRead` frame reader and four Rust boundary tests. |
| `OWNERSHIP_INVALID_STATE_AND_EPOCH_REJECTION` | `PASS` | Schema lifecycle sets, invalid state, positive epoch, and stable codes. |
| `UNICODE_BOUNDARY_DIFFERENTIAL` | `PASS` | UTF-8 byte boundary candidate against Python and Rust. |
| `EXISTING_RUNTIME_OWNERSHIP_REFERENCE` | `PASS` | Actual temporary SQLite store, runtime transaction boundary, and watermark readback. |
| `INDEPENDENT_NEGATIVE_INVARIANTS` | `PASS` | 11 authored negative replay fixtures plus ownership negatives. |
| `TRACE_UNEXPECTED_ERRORS_FAIL_QUALIFICATION` | `PASS` | Only declared `RuntimeControlError` is caught; unexpected exceptions escape. |
| `ASSIGNMENT_REPLAY_REGRESSION` | `PASS` | Current, legacy, golden, SQLite, and every-prefix comparisons. |
| `BENCHMARK_METRIC_PROVENANCE` | `PASS` | Returned `elapsed_ns`/`iterations`, raw samples, hashes, bounds, commands, env. |
| `COLD_HOT_BUILD_RSS_LABELS` | `PASS` | Cold/hot split, build labels, temporary clean target, same-workload RSS. |
| `REPOSITORY_RESIDENT_REGRESSION` | `PASS` | New tests and corrected raw measurement fixture are discoverable in repo. |
| `PYTHON_DEFAULT_PATH_UNCHANGED` | `PASS` | Full regression remains green; Rust is explicit qualification-only code. |
| `PVX1805_PVX1806_PVX1807_REGRESSION` | `PASS` | Full Python suite: 540 passed and 66 subtests passed. |
| `FULL_QUALIFICATION` | `PASS` | Rust checks, semantic, protocol, ownership, trace, regression, and K3 run. |
| `LINEAR_SYNC` | `PENDING_DELIVERY` | Comment is appended after feature branch push; issue remains `In Review`. |

## Production Boundary And Adoption

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
MAIN_BRANCH_PUSH=NOT_PERFORMED
ORIGINAL_CHECKOUT_MUTATION=NOT_PERFORMED
HISTORICAL_TASK_MUTATION=NOT_PERFORMED
NEXT_PHASE_STARTED=NO
```

`ADOPTION_RECOMMENDATION=DEFER` is independent of the implementation gate.
The prototype is sufficiently corrected for bounded continued evaluation,
but it is not evidence for production adoption. A later decision would need
live transaction/write-point fencing, process supervision and physical
fencing, Provider E2E, deployment/observability, load/tail behavior, and
maintenance evidence. No adapter, worker runtime, or shadow path is enabled by
this qualification.

## Delivery

The final ordinary commit SHA, final tree, clean worktree, feature-branch
remote readback, and Linear comment ID are recorded in the delivery response
after the final verification and push. No main push, force push, merge,
deployment, service restart, Provider/Host start, production DB access, or
PVX-1800 operation is part of this task.
