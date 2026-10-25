# Contributing to CLINX

Read applicable `AGENTS.md` and inspect the checkout before editing. Preserve unrelated
changes. The current runtime, compatibility contracts, build/maintenance tools and
portable qualification suite use Rust; macOS UI uses Swift. Python is not a build or
test prerequisite for current source.

Use Rust 1.98 or later, Git, a C linker and OpenSSL CLI for the independent wire tests.
On P620 use a task-owned target under `/lab/runtime-worktrees/p620` and private,
short temporary paths under `/data/artifacts`; Unix socket paths have a finite limit.
See [Testing](docs/TESTING.md) for exact commands and platform limits.

Build the test examples before running the suite. They are independent child programs
used by real process/Provider/transport qualifications; they are never runtime release members.

```sh
cargo build --locked --manifest-path kernel/Cargo.toml --bins --examples
cargo test --locked --manifest-path kernel/Cargo.toml
cargo test --locked --manifest-path native/opaque_pairing/Cargo.toml
cargo build --locked --release --manifest-path kernel/Cargo.toml --bins --examples
"$CARGO_TARGET_DIR/release/clinx-source-audit" --check-structure
"$CARGO_TARGET_DIR/release/clinx-source-audit"
git diff --check
```

Set `CARGO_TARGET_DIR` to your task-owned build directory before these commands.
Use private HOME/XDG/TMPDIR for process tests; never substitute production sockets,
identities, databases or Provider endpoints. A test must not replay an unknown request
or fabricate terminal evidence to make teardown pass.

Place native tests in `kernel/tests/`; preserve unique identity, request, concurrency,
WAL, ACK, cancellation, recovery, process ownership and failure-boundary assertions.
Frozen JSON/SQL oracles belong in `fixtures/`. Keep independent expectations separate
from the implementation under test. Do not convert failures into skips or weaken assertions.

The ignored local Python `tests/` is a historical archive. Do not edit or force-add it.
Recovery of an old test uses its exact Git source and pinned dependencies in a disposable,
task-owned checkout. The historical relocation runner and Python requirements are no
longer current source dependencies. See the [retirement evidence](docs/RUST_PYTHON_RETIREMENT.md).

Report current PASS separately from existing formatting/lint failures, historical API
failures, unavailable external artifacts, native macOS build/signing and real-device
acceptance. A successful private test or source commit does not prove a deployed release.

Remove only your own disposable checkouts and regenerable build outputs after retaining
logs, hashes and recovery bundles. Do not remove installed releases, credentials,
DB/WAL/SHM files, rollback dependencies, pre-existing caches or runtime data.
