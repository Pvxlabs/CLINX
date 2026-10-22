# Owner client

This Rust package contains `clinx-owner-client`, a bounded NDJSON stdio client
for an existing Execution Owner Unix socket. It validates socket ownership and
peer UID, forwards deliver/commit/read_commit requests, and returns uncertainty
without an implicit retry. It does not open a database or become an Owner.

Linux and Rust 1.98+ are required for the current peer-credential implementation.
From the repository root, build into a fresh directory:

```sh
CLINX_TEST_BUILD=$(mktemp -d)
cargo build --locked --manifest-path kernel/Cargo.toml --bin clinx-owner-client --target-dir "$CLINX_TEST_BUILD"
CLINX_OWNER_CLIENT_BINARY="$CLINX_TEST_BUILD/debug/clinx-owner-client" python tests/run_tests.py owner
```

The pytest command above requires the local ignored `tests/` suite; a fresh
Git clone does not include it. See [testing](../docs/TESTING.md) for optional schema migration input and isolated
Owner fixtures. `cargo test` alone does not exercise the socket protocol; the
Python Owner/authority profile runs the real binary against temporary Owners.
The package name is retained for Cargo compatibility. The non-authoritative
Decision Kernel evaluator/bench and Runtime Lab prototypes have been retired;
source recovery is documented in [the archive index](../docs/README.md#历史资料恢复).
