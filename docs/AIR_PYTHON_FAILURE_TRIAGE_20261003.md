# Python failure triage — 2026-10-03 (Air)

Baseline comparison: the selected baseline worktree at main (f88ffe0b3b20cf4361ba3899bd341ae0ea4a42c4) reproduced the same platform/runtime failures before candidate-only source changes were considered. No assertion was deleted and no skip was added.

## Full-suite result after isolated OPAQUE setup

Command: TMPDIR=/tmp .venv/bin/python -m pytest -q

Result: 39 failed, 956 passed, 9 skipped, 15 warnings, 118 subtests passed (.validation/air-node-delivery/full-pytest-opaque-20261003.log).

The earlier 60 failures included 21 production pairing tests. Building/installing the local arm64 abi3 OPAQUE wheel (.validation/air-node-delivery/opaque-wheels/) moved all production pairing and Monitor pairing tests to passing; those were an isolated missing dependency, not a candidate regression.

| Group | Tests | Evidence | Classification | Current action |
|---|---:|---|---|---|
| macOS temporary path spelling | 1 bridge + 4 workspace/path assertions | /private/tmp/... returned by tempfile, expected /tmp/... | Air platform path-normalization mismatch, reproduced on baseline | no production change; preserve as platform-specific |
| P620 Host/service probes | 1 execution-authority + 4 host-contract + m10/m13b/m5/m6 | missing local LOCAL_HOST_PROCESS, no P620 target/service contract in Air test process | requires P620 runtime/host fixture; not a Monitor/node regression | defer to P620 isolated environment |
| Rust kernel | 19 kernel/pvx1808 tests | release binary absent; cargo build --release --locked --manifest-path kernel/Cargo.toml stops because clinx-decision-kernel requires rustc 1.98 while Air has rustc 1.94.1 | toolchain blocker | do not claim kernel conformance |
| local discovery edge cases | 3 | one BrokenPipe during test teardown, one bind to 127.0.0.2 unavailable on this macOS host, one CLI child reports DISCOVERY_DEPENDENCIES_MISSING because it intentionally uses /usr/bin/python3 | platform/launcher fixture issues; production OPAQUE path passes | no assertion relaxation |
| tunnel child | 5 | tests pin /usr/bin/python3.12, absent on Air; child exits 127 | interpreter-path fixture blocker | P620/Linux or supported Python 3.12 runtime required |
| trusted workspace path | 1 | macOS /System/Volumes/Data prefix differs from Linux expected root | platform path semantics | no production change |

The remaining failures are not evidence that the Monitor or node protocol changes regressed. They are retained as unresolved environment/platform items and are not silently marked irrelevant.
