# Testing CLINX

All pytest modules, the runner, configuration and suite index are concentrated
in the local `tests/` directory. This directory is ignored and not tracked by Git;
a new GitHub clone does not contain it. The commands here apply when that local
suite is present. The retained tests still use historical flat module names.
Use `tools.qualification.run_local_tests` below: it creates a temporary copy,
relocates imports, patch targets and source paths using
`architecture/module-moves.json`, and runs with private HOME/XDG/TMPDIR state.
It preserves the original files, assertions, failures and pytest exit status;
each run records source hashes, a log and JUnit XML. There are no runtime import
shims. Directly running the old suite against the new package layout is not the
current regression command.

Install Python 3.12 and the pinned dependencies as described in
[CONTRIBUTING](../CONTRIBUTING.md). `requirements-dev.txt` includes the existing
discovery development pins; there is one source of package versions.
The local `tests/README.md` index maps suites and naming conventions.

## Current Rust adapter and compatibility checks

The current Provider/workspace migration is qualified by
[Rust Provider migration verification](RUST_PROVIDER_RUNTIME_MIGRATION.md): 71 Rust tests
and 73 tracked fixtures, including isolated paired Provider metadata calls, real
Host subprocesses and exact Provider ACK/restart evidence. Build into a task-owned LAB directory, then use:

```sh
cargo build --release --locked --manifest-path kernel/Cargo.toml --target-dir <task-target>
CLINX_TEST_RUST_BIN_DIR=<task-target>/release \
CLINX_MCP_RELAY=<task-target>/release/clinx-mcp-relay \
CLINX_PROVIDER_BINARY=<task-target>/release/clinx-provider \
  <qualification-python> -B -m pytest fixtures -q --tb=short -p no:cacheprovider \
  --basetemp=/data/artifacts/<task>/pytest
```

Use a short task path for Unix socket fixtures. These tests never need a
production Provider, task, database or service restart. The retained local suite
has 109 collection errors on both sides of this migration; its 96 collectable
tests remain 69 passed / 25 failed / 2 errors, with identical test identities and
outcomes. The dated records below describe earlier baselines.

The Python Authority, runtime_control and prototype Provider implementations were
retired in [cleanup phase 2](cleanup/RUST_MIGRATION_CLEANUP_PHASE2.md). Their local
test files and the local suite index were preserved byte for byte. Consequently,
`basic`, `owner` and `recovery` still select retired suites, and full collection
reported nine import errors (1,714 collected, versus 1,967 before retirement).
These profiles are historical evidence, not current passing gates. Do not add a
skip hook, weaken assertions or delete the retained tests to hide this result.
The phase 2 record lists all affected modules and the exact baseline comparison.

[Cleanup phase 3](cleanup/RUST_MIGRATION_CLEANUP_PHASE3.md) also retires the
standalone Linear `Bridge`/V1 Dispatcher, its contract parser and `run/once/doctor`
commands. Their local tests are preserved and now fail on the removed symbols
or commands. The 20-module comparison changed from 492 passed / 21 failed to
454 passed / 66 failed; all 39 changed JUnit node outcomes refer to these retired
interfaces, and other outcomes are unchanged. This is not a passing full suite.
Fresh Rust builds, copied-package request/routing tests and the isolated Rust
MCP/Dispatcher/recovery chain are recorded separately in the phase 3 report.

[Cleanup phase 4](cleanup/RUST_MIGRATION_CLEANUP_PHASE4.md) removes the Python
Owner, Centre automatic enrollment, duplicate domain models, shadow replay and
the private consultation/Owner entry. Full collection now reports 1,491 tests
and 24 import errors, including 15 newly affected historical suites. All 124
local test/support files retain their original SHA-256; none were skipped or
edited. The 18 retained modules have the same 439 JUnit node outcomes as their
baseline subset: 386 passed / 66 failed / 2 skipped / 73 subtests passed.
That subset is not a passing full suite. The report lists the excluded retired
module tests and separately records 7 no-Owner recovery checks, 10 tracked Rust
node contract checks, a baseline-created publication outbox upgrade, and the
fresh Rust artifact chain.

The tracked node contract fixture also runs without the local test archive:

```sh
python -B -m pytest --import-mode=prepend -o pythonpath=. fixtures/test_rust_node_contract.py
```

For the existing public request and native routing fixtures:

```sh
python -B -m tools.qualification.run_local_tests --work-dir /data/artifacts/<unique-task>/adapters test_rust_native_routing.py test_rust_request_contract.py
python -B -m tools.qualification.run_local_tests --work-dir /data/artifacts/<unique-task>/collection --collect-only
python -m ruff check --no-cache --config ruff-discovery.toml <changed-python-files>
git diff --check
```

The two adapter modules passed 36 tests from an independently copied Rust source
package. They need no production service, account or real Codex daemon. Use
private HOME/XDG/TMPDIR state and no live opt-ins for offline regression work.
The current qualification runner supplies disposable state, disables bytecode/cache
writes and third-party plugin autoload, and preserves pytest's exit status.
Pytest 9.1 includes unittest subtest support; use the pinned dependency versions.

[Phase 5](cleanup/REPOSITORY_CONVERGENCE_PHASE5.md) records the package relocation
and unchanged baseline outcomes. Replace `<unique-task>` with a fresh task-owned
directory. The following historical profile examples explain prerequisites and
old evidence; they are not current passing gates and do not translate flat imports.

Useful retained compatibility modules include `test_execution_owner_routing.py`,
`test_execution_liveness.py`, `test_result_ingestion.py` and
`test_desktop_publication.py`. They exercise the ordinary local execution route
that is still enabled on iMac. They do not qualify a Python Authority writer.
`test_execution_owner.py`, `test_native_input_recovery.py` and
`test_shadow_ledger.py` still import deleted modules and are historical evidence,
not current passing gates. No import shims were added to hide their retirement.
The schema-only migration input belonged to the retired experiment; it is not
required to run the present Rust authority or device compatibility code.

Rust checks use a task-owned output directory, never an installed release:

```sh
cargo test --lib --locked --offline --manifest-path kernel/Cargo.toml --target-dir <task-build-dir>
cargo test --tests --locked --offline --manifest-path kernel/Cargo.toml --target-dir <task-build-dir>
cargo build --release --locked --offline --manifest-path kernel/Cargo.toml --bin clinx-runtime --bin clinx-mcp-relay --target-dir <task-build-dir>
CLINX_MCP_RELAY=<task-build-dir>/release/clinx-mcp-relay python -B -m pytest --import-mode=prepend -o pythonpath=. fixtures/test_mcp_relay.py fixtures/test_rust_node_contract.py
```

Historical Linux compatibility Owner fixtures may have left empty lock files under
`/run/user/<uid>/clinx-owner-<uid>/`. Do not sweep this shared directory.

## Recovery and environment-dependent suites

```sh
python tests/run_tests.py recovery
```

This historical profile includes a retired Authority transport module and cannot
currently collect in full. Its retained recovery tests include a transient systemd test that
needs a separately provisioned user manager/session bus. Without that prerequisite
it fails with `Failed to connect to bus`. Do not attach the runner to a production
manager to obtain a pass. The remaining recovery cases use temporary state.

| Suite | Additional requirements |
| --- | --- |
| Local `tests/test_m*.py`, remaining `tests/test_pvx*.py` | Exact paths; stable module names with shared fixture imports |
| Discovery, pairing and node/process suites | Development dependencies; production-protocol cases also need `native/opaque_pairing` built with maturin and its Cargo.lock; see [Local Discovery](LOCAL_DISCOVERY_V1.md) |
| Tunnel wire, first migration and stage1 wire | Pinned installed client/release artifacts referenced in source, isolated HTTP proxy/Owner; no portable artifact availability promise |
| Transport build/release tests | Fixture sources and controlled fake builds; actual builds need matching Go toolchain/upstream archive |
| `fixtures/bridge_transport_control_test.go` | Matching upstream harness through `scripts/qualify_bridge_transport.py`, not standalone pytest |
| Rust Owner client | `cargo test --locked --manifest-path kernel/Cargo.toml --target-dir <fresh-directory>` checks compilation; the old Owner profile also contains retired experiment suites and is not a passing gate |
| Native OPAQUE Rust adapter | Matching Python/PyO3/maturin toolchain; independent of pytest collection |
| `MonitorApp/Tests/CLINXMonitorTests` | macOS/Xcode; from MonitorApp run `swift test --scratch-path <fresh-directory>`; Python source checks are not GUI acceptance |

The Rust authority retains its frozen execution contract and shared Rust state.
Historical Python Authority tests remain local for evidence recovery; their
implementation has been removed from current source. See
[Rust authority status and acceptance](RUST_AUTHORITY.md) and
[historical source/evidence recovery](README.md#历史资料恢复).
A lab or adapter PASS does not establish production business acceptance.

Transport packaging/wire tests have an additional artifact-location contract:
create a fresh task-owned directory under `/data/artifacts`, then run:

```sh
python tests/run_tests.py transport --temp-parent /data/artifacts/your-task
```

The runner creates and removes its own subdirectory there. Tests use isolated
services and fake credentials. Some cases read pinned release archives or client
binaries, so this profile requires those exact artifacts and is not the portable
basic profile. `tests/test_forwarding_deploy.py` currently uses a pinned historical
package whose home-path contract conflicts with an isolated HOME; its seven
fixture errors remain visible. Do not weaken the package's exact-path checks or
point tests at live configuration to make them pass.

## Real Provider and end-to-end acceptance

These require disposable configured workspaces, real endpoints and any required
credentials. The profile runner does not forward live opt-ins. Read each file's
setup before a deliberate live run; setting a flag alone is insufficient.

| File | Existing opt-in |
| --- | --- |
| `tests/test_acceptance_result_live.py` | `CLINX_LIVE_RESULT_CONTRACT=1` |
| `tests/test_execution_authority_live.py`, `tests/test_host_provider_isolation_live.py` | `CLINX_LIVE_AUTHORITY=1` |
| `tests/test_host_contract_v2_live.py` | `CLINX_LIVE_HOST_CONTRACT_V2=1` |
| `tests/test_native_interop_live.py` | `CLINX_LIVE_NATIVE_INTEROP=1` |
| `tests/test_tool_delivery_live.py` | `CLINX_LIVE_DELIVERY_ACCEPTANCE=1`; additional flags in source |
| `tests/test_worker_continuation_live.py` | `CLINX_LIVE_CONTINUATION=1` |

See [native forwarding](NATIVE_TASK_FORWARDING.md) and
[execution authority](EXECUTION_AUTHORITY.md). Existing live skips mean NOT_RUN.
Collection proves discovery/imports; it does not measure line/branch coverage,
production capacity, real device behavior or business acceptance.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Missing `zeroconf` / `jsonschema` | Install `requirements-dev.txt` using the interpreter that runs pytest |
| `setup(tmp_path)` receives a module | Use pinned pytest; older system pytest invokes legacy nose setup. Changing HOME can expose an older system copy |
| Missing Python Owner / `CLINX_OWNER_CLIENT_BINARY` | Python Owner fixtures are historical and do not collect on current source; use the recorded source ref in an isolated fixture if recovering old evidence, never start an Owner against production |
| Missing `CLINX_PRODUCTION_SCHEMA` | Supply `owner --schema <schema-only.json>`; absence is retained as an explicit prerequisite failure |
| `PRODUCTION_PAKE_UNAVAILABLE` | Build the native OPAQUE adapter in the test environment; SPAKE2 DEV_ONLY is a different backend |
| Socket-directory permissions | Confirm private same-user endpoint and temporary DB identity; do not chmod/delete a production socket |
| `systemd-run` cannot connect to bus | Provide an isolated test user manager; retain the failure if unavailable |
| Ruff findings in legacy files | Use `ruff-discovery.toml`, compare to the saved baseline and avoid unrelated formatting changes |
| Collection succeeds but tests fail | Report exact failed node IDs and unmet prerequisites; do not weaken assertions or hide failures |
