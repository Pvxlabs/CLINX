# Testing CLINX

All pytest modules, the runner, configuration and suite index are concentrated
in the local `tests/` directory. This directory is ignored and not tracked by Git;
a new GitHub clone does not contain it. The commands here apply when that local
suite is present. Use `python -m pytest -c tests/pytest.ini tests` for direct pytest
invocation from the repository root; shared fixture imports are handled locally.

Install Python 3.12 and the pinned dependencies as described in
[CONTRIBUTING](../CONTRIBUTING.md). `requirements-dev.txt` includes the existing
discovery development pins; there is one source of package versions.
The local `tests/README.md` index maps suites and naming conventions.

## Portable basic tests

From the repository root, using your development environment's Python:

```sh
python tests/run_tests.py basic --list
python tests/run_tests.py basic --junitxml=/tmp/clinx-basic-results.xml
python tests/run_tests.py collect
python -m ruff check --no-cache --config ruff-discovery.toml tests/run_tests.py tests/test_consultation_owner.py
git diff --check
```

The basic profile runs core domain, Provider adapter, SQLite runtime-control,
shadow-ledger and Observer-schema checks. It needs no production CLINX service,
account, API key, installed release, Codex daemon or Rust binary. Its SQLite
registries and scripted transports are temporary. The profile is a reproducible
entrypoint, not a claim that every platform and integration suite has passed.

The runner creates private HOME/XDG/TMPDIR state, removes it after the run,
disables bytecode/pytest-cache writes and third-party plugin autoload, and passes
only PATH/locale/platform settings plus explicit inputs below. This is environment
isolation, not an OS sandbox. `--junitxml` saves results outside temporary state.
Pytest 9.1 includes unittest subtest support. The local `tests/pytest.ini` keeps the original
filename patterns and accepts `test.py`; it sets no selection filter, skip hook,
import-mode override or narrowed testpaths. Plain `python -m pytest` collects all
eligible tests, including suites with special prerequisites.

## Isolated Owner and authority

The current Rust Owner client needs Linux and Rust 1.98+. Build it from this
checkout with locked dependencies into a fresh directory:

```sh
CLINX_TEST_BUILD=$(mktemp -d)
cargo build --locked --manifest-path kernel/Cargo.toml --bin clinx-owner-client --target-dir "$CLINX_TEST_BUILD"
CLINX_OWNER_CLIENT_BINARY="$CLINX_TEST_BUILD/debug/clinx-owner-client" python tests/run_tests.py owner
```

On P620 create the build directory under your task-owned LAB path. Preserve logs
before removing only your build output; do not replace an installed release.
The profile starts isolated Owner threads, real Unix sockets and SQLite registries,
uses scripted Providers, and exercises Python/Rust transport, CAS, rollback, stale
epochs, uncertain ACKs, finalization and lease release. No production Owner is
needed. The consultation CLI test now starts its own temporary Owner;
its persistence/discovery assertions remain in their original collected case.
The additional integration case covers idempotency, uncertain send/resume and
refusal to write after Owner shutdown.

Two schema-migration cases require a schema-only `sqlite_master` JSON export.
One currently fails and the other skips when no schema is supplied; this remains
visible in the default Owner run. For a complete schema qualification:

```sh
CLINX_OWNER_CLIENT_BINARY="$CLINX_TEST_BUILD/debug/clinx-owner-client" python tests/run_tests.py owner --schema /absolute/path/to/schema-only.json
```

The JSON is a list of objects with `type`, `name` and `sql` strings, containing DDL
only. Use an independently approved schema export, never a production SQLite
file or production rows. The runner validates the JSON shape and forwards this
explicit path as `CLINX_PRODUCTION_SCHEMA`; it does not inherit that variable or
open a production database. A schema export proves compatibility with that schema
version, not with every future database.

On Linux the runtime derives socket directories from the temporary database
identity under `/run/user/<uid>/clinx-owner-<uid>/`; stopped test Owners may leave
empty lock files there. Do not sweep this shared directory.

## Recovery and environment-dependent suites

```sh
python tests/run_tests.py recovery
```

This retains failure/recovery semantics, including a transient systemd test that
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
| Rust Owner client | `cargo test --locked --manifest-path kernel/Cargo.toml --target-dir <fresh-directory>` checks compilation; real socket behavior is covered by the Owner pytest profile |
| Native OPAQUE Rust adapter | Matching Python/PyO3/maturin toolchain; independent of pytest collection |
| `MonitorApp/Tests/CLINXMonitorTests` | macOS/Xcode; from MonitorApp run `swift test --scratch-path <fresh-directory>`; Python source checks are not GUI acceptance |

Historical Decision Kernel, Runtime Lab and maintenance-authorization experiments
have been retired with their exclusive tests and qualification wrappers. Core
Owner, authority, SQLite, transport, idempotency and recovery assertions remain.
[Historical source/evidence recovery](README.md#历史资料恢复) is independent of
normal development installation.

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
| Missing Owner / `CLINX_OWNER_CLIENT_BINARY` | Build the client and start the fixture's own temporary Owner; inspect the full error response, never fall back to local production writes |
| Missing `CLINX_PRODUCTION_SCHEMA` | Supply `owner --schema <schema-only.json>`; absence is retained as an explicit prerequisite failure |
| `PRODUCTION_PAKE_UNAVAILABLE` | Build the native OPAQUE adapter in the test environment; SPAKE2 DEV_ONLY is a different backend |
| Socket-directory permissions | Confirm private same-user endpoint and temporary DB identity; do not chmod/delete a production socket |
| `systemd-run` cannot connect to bus | Provide an isolated test user manager; retain the failure if unavailable |
| Ruff findings in legacy files | Use `ruff-discovery.toml`, compare to the saved baseline and avoid unrelated formatting changes |
| Collection succeeds but tests fail | Report exact failed node IDs and unmet prerequisites; do not weaken assertions or hide failures |
