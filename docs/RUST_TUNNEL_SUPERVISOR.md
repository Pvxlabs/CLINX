> 文档分类：HISTORICAL。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](CONTRACTS.md)与[架构](REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# Rust Tunnel supervisor

`clinx-tunnel run` now owns the complete Tunnel supervisor lifecycle. systemd
supervisor and health templates invoke that binary directly; no Python process
starts or supervises the normal runtime. `--supervisor-contract` returns
`RUST_TUNNEL_SUPERVISOR_V1`.

The Rust engine owns the exclusive lock, launch intent and persistent restart
budget, child session/process group, TERM/KILL drain, health sampling, stop
signals and restart loop. Receipts retain the existing atomic write + file and
directory fsync contract. Existing identity hashes remain compatible with
Python's sorted JSON encoding, including numeric policy values and Unicode
command arguments. A saved unknown spawn outcome or possibly live prior child
still refuses admission; the process never adopts or signals that prior child.
This supervisor neither replays tasks nor changes execution authority.

Health probes connect directly to `127.0.0.1` without DNS or proxies, refuse
redirects and URL credentials/query/fragment, bound the response to 1 MiB and
apply one monotonic deadline across connect, headers and body reads. A trickling
body cannot restart the deadline. Content length and chunked framing are
validated. Group termination retains separate TERM/KILL deadlines and requires
both child reaping and a drained process group before another generation.

`clinx.transport.tunnel_reliability` is a qualification compatibility API.
Its CLI uses `execv` to replace itself with `clinx-tunnel`. The old `Supervisor`
object exposes injectable clock/process IO so existing fault tests still run;
its run, launch and termination decisions execute the same Rust engine through
private stdin/stdout callbacks. It contains no parallel lifecycle state machine.
No compatibility API is needed or included in the standalone Tunnel release.

`scripts/build_tunnel_release.py` accepts `--supervisor-binary`, validates the
marker and freezes the exact executable bytes into the source-derived release
identity. It stages `clinx-tunnel`, policy and paired Rust systemd templates.
It does not stage `tunnel_reliability.py` or `clinx-node`. The Python config CAS
script remains a maintenance tool in the release/rollback capsule. Publication,
activation, deployment and live configuration changes are separate operations.

Qualification on 2026-10-25 used task-owned state under
`/data/artifacts/clinx-thin-runtime-20261025/tunnel`:

- Existing unmodified reliability/stage1 assertions: 42 passed after import/path
  relocation outside the repository. Includes durable budgets, lost launch
  receipt, prior live process, PID reuse, deterministic recovery bounds, health
  deadlines, TERM-resistant descendants and unconfirmed group drain.
- Native Rust executable integration: 5 passed. Covers exclusive ownership,
  real HTTP health and readback, restart budget persistence, unknown prior
  receipt, stop signals, descendant cleanup, unaffected bystanders, private
  directory validation, redirect refusal and trickling-response total deadline.
- New package/identity tests: 2 passed. Includes reproducible release archives,
  Rust-only service entries, standalone health with no Python in PATH and exact
  compatibility with an existing Python identity/budget receipt.
- Existing config CAS tests: 17 passed; the historical template assertion still
  fails at its expected `clinx-provider.service` reference, absent before this
  change. That original test is retained. Its later Python health command check
  also represents the retired entrypoint and is not rewritten to manufacture a
  pass; new package tests assert the replacement Rust command.

The transient systemd test operates only its UUID-named `clinx-p1-fixture-*`
unit. A task-owned wrapper forwards the explicit candidate binary environment
only to that fixture unit; it does not change the user manager environment.
`transient-systemd.json` and `systemd-final-readback.json` record exit 78 at the
budget, MainPID 0, LoadState not-found and inactive/dead after collection.

No production service, task, database or runtime data was changed. The release
is NOT_DEPLOYED. These isolated supervisor checks do not claim production SLO
or cross-device business acceptance.
