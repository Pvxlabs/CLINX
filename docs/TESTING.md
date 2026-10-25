# Current native regression and qualification

All current runtime and portable qualification tests are tracked Rust. A fresh clone
can run them without the ignored local Python `tests/`, pytest, PyO3 or a Python wheel.
Original migration cases and checks remain mapped in `fixtures/*retirement*.json` and
[the retirement report](RUST_PYTHON_RETIREMENT.md). Older count snapshots belong to their
respective dated migration reports.

## Isolated Linux run

Use Rust 1.98+, Git, a native C linker and OpenSSL CLI. Do not inherit production state
or set test endpoints to installed services. P620 builds belong under `/lab`; disposable
repositories belong under the task-owned `/data/artifacts` location. Use short temporary
paths for Unix sockets. The following illustrates environment variables for an already
created, private task directory; replace the placeholders with your task-owned paths.

```sh
export CARGO_TARGET_DIR=/lab/runtime-worktrees/p620/<task>/target
export HOME=/data/artifacts/<short-task>/home
export XDG_CONFIG_HOME=/data/artifacts/<short-task>/config
export XDG_STATE_HOME=/data/artifacts/<short-task>/state
export XDG_DATA_HOME=/data/artifacts/<short-task>/data
export XDG_RUNTIME_DIR=/data/artifacts/<short-task>/run
export TMPDIR=/data/artifacts/<short-task>/tmp
# Retain explicit existing RUSTUP_HOME/CARGO_HOME for the installed toolchain/cache.
cargo build --locked --manifest-path kernel/Cargo.toml --bins --examples
cargo test --locked --manifest-path kernel/Cargo.toml
cargo test --locked --manifest-path native/opaque_pairing/Cargo.toml
cargo build --locked --release --manifest-path kernel/Cargo.toml --bins --examples
"$CARGO_TARGET_DIR/release/clinx-source-audit" --check-structure
"$CARGO_TARGET_DIR/release/clinx-source-audit"
git diff --check
```

The examples `qualification_workspace` and `qualification_tunnel` are real private
child programs used by integration tests; build them in the same target/profile as the tests.
They replace embedded Python fixtures and do not enter runtime/App packages.

## Coverage

| Suite | Important obligations |
| --- | --- |
| Authority/native/Host | Atomic receipts, exact operation identity, concurrent attempts, send once, ACK uncertainty, cancellation, restart and no blind replay |
| Node/Device/Discovery | Authenticated principal/scope intersection, reverse Authority, paired Provider route, 6-way concurrent traffic, restart, revocation, independent OpenSSL in both directions, expired certificates, CLI/PTY PIN handling |
| Provider/workspace | Exact thread/turn/input and cwd/repository proof, identity mismatch, process/SSH/Unix/WebSocket protocol, backlog and duplicate responses |
| Collector/Observer/MCP | Independent SQL/JSON oracle, read-only bytes, checkpoints and duplicate ACK, cursor scope, auth/audit, concurrent writes, restart and permission errors |
| Build/install | Explicit release members/hash/marker, no inherited Python/development binaries, deterministic archives, CAS path/preflight, exact service/config templates and no service activation |
| Tunnel/relay | Persistent identity/budget, original deterministic fault clocks, ownership/TERM/KILL, partial byte streams, half-close, no replay, bounded metrics IO and passive diagnostics |
| OPAQUE | Mutual confirmation, single consumption, context mismatch and malformed finish; now tested directly in Rust |

## Platform and artifact limits

Native macOS UI/App building and signing require macOS/Xcode; Linux native-resource
assembly tests are not that acceptance. Physical LAN multicast and real Mac-to-P620
execution are separately authorized device qualifications. The Discovery smoke example
requires an explicit `--lan` option for physical multicast; isolated `--loopback` results
must not be called physical LAN PASS.

Administrator install, provision, maintenance and deployed isolation checks are not
run against production by this suite. Their filesystem/command boundaries are tested
using private fixtures. Historical Python-to-Rust cgroup/old-provider-unit migration
probes are retired scenarios, retained in the matching Git source and prior receipts;
this does not create new proof of production isolation.

The full pinned Go transport build requires the exact source and toolchain archives
accepted by `clinx-build build-transport-runtime`. Unit/dependency/archive tests do not
replace that two-directory reproducibility proof when the toolchain archive is unavailable.

## Historical suite and failure reporting

The ignored Python `tests/` archive remains byte-for-byte unchanged. It references retired
APIs and cannot serve as the current passing gate. Re-run comparisons only in private
state using the preserved original relocation harness and exact baseline checkout;
keep the original pytest counts, collection errors, skips and masked expected-exception
outcomes. Do not edit/skip historical tests to manufacture PASS.

Normal Clippy may pass with pre-existing warnings. Record `cargo fmt --check` and strict
warning failures separately, compare their exact diagnostic sites with the baseline,
and fix new issues introduced by your change. Source retirement does not excuse a
current Rust test failure.
