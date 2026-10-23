# Native Rust business authority

Status on 2026-10-24 (Asia/Shanghai): **production Rust authority PASS**.
The selected immutable release is `rust-834b236abff41aa9e117`. The existing
Tunnel launcher now forwards stdio to `/run/clinx-rust-api/mcp.sock`; Centre
publishes the Rust projection on `0.0.0.0:18773`. Python Owner PID 119760 exited
after its exact active ORION turn completed. The old Owner, Dispatcher, Provider
and Centre units are stopped and masked. No legacy history or Lease was imported.

The source build used `/home/pvxlabs/dev/CLINX-RUST`, `codex/clinx-rust`, based
on integration `a7c915a8df7e9585eac5d46c152306d98d6618b0` plus the recorded
cutover patch. Final source integration and deployed file hashes are recorded in
the cutover provenance receipt; the immutable release manifest retains its actual
build-time base HEAD. No push, reset, clean, stash or rollback implementation was
performed. Original Codex development-directory access is preserved.

Current evidence root:
`/lab/runtime-worktrees/p620/clinx-rust-business-20261022T165524Z/cutover-20261023T164657Z/`.
The independently fsynced retirement and switch receipts are also under
`/var/lib/clinx-rust/migration-receipts/`.

## One durable Rust authority

`authority_store.rs` owns SQLite. Business commands in `business.rs` share its
transaction, receipt, immutable command chain, outbox, lease and finalization.
The daemon commits directly and does not call the Python Owner. The frozen V1
core, runtime lab and fixture hashes are unchanged.

Implemented business paths include preparation, admission, once-only dispatch,
clientUserMessageId readback, cancellation, native observation, success/failure
finalization, restart validation, and resource release. Linux BOOTTIME and boot
identity fence lease evidence. Unknown delivery never authorizes replay or release.

`authority_state.rs` adds an explicitly versioned `CLINX_PRE_SEND_CLOSURE_V1`
operation. It closes only the exact admission prefix with no native send or
checkpoint. FAILED/CANCELLED, attempt terminal, allocation release, receipt,
outbox and lease release commit together. It creates no synthetic native binding.
SENDING cannot be reclassified as a pre-send failure.

Native adoption on configured, visible endpoints creates a new Rust task without
importing Python task history. Exact observation challenges, workspace seals and
unique bindings gate adoption. Policy preparation/application use reviewed hashes,
CAS and active-task exclusion. Prepared executions are invalidated by policy changes.
Exact successor handoff and derived Git target validation now also use Rust
transactions, as qualified below. Legacy policies are not silently inherited.

The Host dynamic-tool ledger now lives inside the Rust execution. Receipt,
once-only take/begin permission, process completion, response delivery, native
ACK readback and Finalizer checks all use Rust commands. Python's HostExecutor
retains its command validation and subprocess runner without Python ledger writes.
Unknown Host side effects remain HELD. A lost ACK does not authorize reexecution.

## Dedicated identities and caller wiring

| Identity | UID | Responsibility |
| --- | ---: | --- |
| clinx-authority | 993 | Rust SQLite writer and authority socket |
| clinx-api | 990 | Existing MCP and Centre adapters |
| clinx-transport | 988 | Existing Provider transport and Host supervision |
| pvxlabs | 1001 | Codex Provider and Host subprocesses using the original filesystem identity |

Root-owned release, policy and service files are under `/var/lib/clinx-rust` and
`/etc/systemd/system`. The only new business state is
`/var/lib/clinx-rust/authority/authority.sqlite3`; it was initialized clean for production,
with no legacy task, lease or running-ledger import. Candidate qualification
ledgers were archived intact before initialization. Node identity, peer trust
and sharing configuration were copied; legacy Node databases were not imported.

The authority uses SO_PEERCRED for API/transport roles. Production mode rejects
raw commit/claim/renew operations. `CLINX_AUTHORITY=rust` prohibits construction
of a Python TaskRegistry writer and has no Python fallback.

The production Rust dispatcher requires the separate Provider endpoint. The
Provider and Host processes use the original operator filesystem identity;
they do not use the authority UID or authority IPC group. The Provider runs
with no capabilities. Its native socket's fixed `/tmp`
path is bound into the private namespaces from the dedicated runtime directory.
A separate provider IPC group permits only API/transport access to that endpoint.

The root-owned, fixed-identity `host-worker` launcher is executable only by the
transport group. It closes inherited FDs, clears the environment, permanently
drops to the original Codex user and groups fixed in root-owned worker-user
configuration, and sets no_new_privs. The
transport's capability bounding set contains only SETUID/SETGID for this drop.
The worker cannot open the state, authority socket, MCP entry socket, writer proc
FDs or Docker socket; canonical-path and symlink-alias access both fail.

The installed MCP socket activates the existing `mcp_server.py` as clinx-api.
`rust_stdio.py` forwards bytes from the existing stdio transport without decisions.
The Rust catalog includes target registration/revocation, policy control and Host
request parameters, with matching public response schemas. The existing production
Tunnel launcher now invokes this command; it does not load the old Owner environment.

Centre runs on production port 18773 with `--rust-projection`.
It reads managed task/turn projections through Rust and writes only existing Node
observation/index/outbox projections. It neither opens the legacy task database
nor invokes the Python canonical lifecycle. The retired Python Centre no longer owns that port.

## Completed acceptance and evidence boundaries

Evidence root: `/data/artifacts/clinx-rust-continuation-20261023T122057Z/`.
The previous 27 Rust tests, 100 caller regressions, crash checks and 300-second
scripted soak are retained under `clinx-rust-business-20261022T165524Z`; they were
not blindly repeated. Added focused tests cover pre-send closure, unloaded
terminal evidence, adoption/policy CAS, Host delivery, public MCP schemas and
Centre projections. Actual subprocess SIGKILL checks cover pre-send atomicity.

Real Provider acceptance used native Codex models, not a scripted Provider:

* Success and Host user_identity completed under the dedicated service identities;
  Host stdout was `clinx-worker`, with a persisted native ACK and released allocation.
* Cancellation committed CANCELLED/RELEASED on the original turn.
* A real external interruption at the Provider, without a Rust cancellation
  request, produced FAILED/RELEASED through Rust's native failure Finalizer.
* SIGKILL of real transport and authority after acceptance but before input readback
  recovered the original turn. Native turn count remained one; duplicate start did
  not dispatch again.
* The real Provider soak completed six tasks over 302.86 seconds with healthy services.
* Actual MCP stdio-to-socket-to-Rust dispatch returned `RUST_MCP_SERVICE_OK`, released
  its resource and returned the same execution on duplicate start.
* Centre published Rust projections; its delivery outbox drained to zero.

The prior soak ran release `rust-b08c9a1e812ddae81f8a`. Its evidence is retained
for the unchanged workload; it is not a soak of the current closure changes.
The current native identity/Host/Git changes received targeted acceptance below.

Input history may lag acceptance by tens of seconds. Early UNKNOWN observations
are preserved and polled; they do not imply failed delivery. A preliminary 0.164
version-incompatibility diagnosis was corrected by final archived Rust readback:
that task had reached SUCCEEDED/RELEASED. See the superseding
`rejected-provider-0164-state-correction.json`. The candidate pins the recovered,
previously qualified 0.160 Provider image and retains both records.

The physical kernel prerequisite passed with a real inherited writable FD and
SIGSTOP process, exact cgroup.kill, waitpid/reaping, failed SIGCONT and denied path
aliases. Deployed new-service isolation passed separately. Current FD inventory
found only clinx-runtime holding the new authority DB/WAL/SHM for writing.
**This does not claim that production legacy workers have been revoked.**

Runtime-scoped rustfmt, locked offline clippy and relevant Python lint passed.
Repository-wide cargo fmt still reports an unchanged import-order difference in
`clinx-owner-client.rs`; that unrelated old client file was preserved.

## Exact identity and target closure, 2026-10-23

Evidence root:
`/lab/runtime-worktrees/p620/clinx-rust-business-20261022T165524Z/closure-20261023T141722Z`.

* `business_handoff.rs` implements exact predecessor checkpoints, once-only
  successor creation, atomic binding, lost-ACK recovery, and bounded abandonment
  of an unmaterialized empty successor before input permission. SENDING and
  unknown observations cannot trigger replacement. Execution context selects
  the requested execution's thread/turn, never the thread's latest turn.
* The migration-owned legacy native session was archived on its original
  endpoint, checked for remaining writable rollout FDs, copied with unchanged
  native IDs, resumed on the dedicated Provider and adopted by Rust. Its real
  continuation retained the original thread and recalled the source result.
  See `native-transfer-final.json` and `deployed-git-and-native.json`.
* Real Host execution `exec_46008eaab75a8fd455705a83c428093f` recovered the same
  thread `01a16941-f51e-7663-aeb7-aa98d5262f42` and turn
  `01a16942-0477-7f61-9bce-341e85b4b657`. One Host row is COMPLETED/DELIVERED,
  stdout is `clinx-worker`, the exact predecessor token is retained, and the
  allocation is RELEASED. The original timeout evidence remains intact;
  `real-successor-recovery-final.json` records the later success.
* Provider and authority restart on the final release was followed by real
  materialized-session resume with the original imported thread. The new
  execution SUCCEEDED/RELEASED. Duplicate start of the prior Host preparation
  returned its original execution; no Host operation was replayed.
  See `materialized-real-final.json`.
* `business_targets.rs` registers/revokes task-owned linked worktrees and checks
  repository, git-dir device/inode, branch, remote and root constraints. Policy
  prepare/apply and execution/Host admission revalidate authorization. A real
  linked target was registered, authorized and revoked; Rust then rejected
  preparation with GIT_TARGET_REVOKED. No push was executed.
* New focused tests cover wrong identity/epoch, successor lost ACK and crash,
  pre-send-only recovery, revoked/stale Git scopes and exact context. The last
  materialized-thread branch passed five parameterized cases. Existing 27/100,
  crash and 302.86-second real Provider evidence was retained, not rerun.
* The paired Codex 0.160 code-mode helper was recovered and pinned with its
  Provider binary. Mixing it with the current npm helper had produced a real
  IPC error; the final Host result confirms that error is resolved.

`qualify_rust_legacy_cgroup.py` placed an old-UID process holding a writable
sentinel FD inside a child of the **actual live clinx.service cgroup**. Writes
still succeeded after SIGSTOP/SIGCONT and inode/path replacement. Only
cgroup.kill and waitpid removed that ability; subsequent SIGCONT failed and
old-UID opens of the new authority DB and a symlink alias were denied.
`actual-legacy-cgroup-probe.json` is PASS for that child-cgroup experiment only.
Actual old production processes were preserved. This is not whole-service
legacy revocation and must not satisfy the final cutover gate by itself.

## Production cutover and postflight

The remaining gates were closed in this cutover:

* Rust resolves existing dynamic project names inside the configured workspace,
  including HDL. Prepare, admission, send authorization and native adoption use
  the same resolver and exact Git seal. Project listing includes registered and
  exactly resolved projects without adding directory permissions or a filesystem
  whitelist. The existing host/query filters work through the external connector.
* The candidate ledger was archived, and production state was initialized with
  zero tasks, leases, commits and commands under the sealed production policy.
  The policy mismatch check was retained; no SQL policy patch or history import
  occurred. Existing identity, pairing and sharing configuration was retained.
* The existing Owner stopped admissions and drained its actual Desktop exact turn.
  The active set was empty before whole-service revocation. The task result and
  qualification receipt were independently fsynced before cutting the remaining
  old control plane.
* A stopped old-UID worker holding a writable sentinel FD was placed directly in
  the actual production `clinx.service` cgroup. Its old FD remained writable after
  stop/resume and path replacement. All approved old service cgroups were then
  killed and reaped, not just a disposable child cgroup. The probe died by SIGKILL,
  waitpid completed, SIGCONT returned ESRCH, and both sentinels remained unchanged.
  No writable old-DB FD remained. Original-UID new DB, alias, writer FD and authority
  socket access were denied after retirement. Epoch rejection was checked separately;
  it is not the physical revocation proof.
* Existing user unit definitions were preserved as evidence and masked at their
  actual load paths. A runtime mask alone would not override those user definitions.
  Only the existing Tunnel was restored, with its old Provider dependency removed.
  The Rust units and MCP socket are included in multi-user.target for reboot startup.

The external CLINX connector returned authority=RUST and completed real preparation,
status and duplicate-start readback. A start ACK was deliberately discarded at the
production MCP socket. Exact preparation readback located the same execution.
SIGKILL of authority and Dispatcher recovered the original thread and turn; the
real Provider completed SUCCEEDED/RELEASED. Full exact native turn enumeration
confirmed one turn. A separate real Provider interruption finalized FAILED/RELEASED.
An approved running cancellation finalized CANCELLED/RELEASED. A deployed production
API cancellation before send finalized CANCELLED/RELEASED with no thread or turn.

The final release's live production MCP catalog accepted an explicit HOST_EXECUTOR
request. The real Provider called user_identity once as pvxlabs, with one persisted
COMPLETED/DELIVERED Host row and exit code zero. Rust finalized SUCCEEDED/RELEASED;
the external cloud connector read back that exact result. No scripted Provider was
used for these checks. Final readback found seven executions with all allocations
released, only the Rust PID holding writable new DB/WAL/SHM FDs, an ONLINE authorized
Centre source with live projections, and a healthy fresh Tunnel poll. The authority
had 677.72 seconds of uninterrupted uptime at that readback.

Reuse the previously passed 27 Rust tests, 100 caller regressions, crash evidence
and 302.86-second real Provider soak. New routing checks cover traversal/symlink
escape, wrong host/repository, restart, identity changes before send and release.
Rust test compilation and Clippy passed. The old crash-test Policy fixture's misplaced
field was corrected and the new optional workspace field was supplied; crash semantics
were not changed or weakened.

The only remaining client limitation is discovery in this existing chat: its loaded
CLINX tool schema is the old native-only catalog. It omits Host parameters and policy/
Git target tools even though live production tools/list exports them. Refresh or
reconnect CLINX before invoking those new operations from this chat. One additional
cloud request failed at https://chatgpt.com/backend-api/ps/mcp before a preparation
commit was observed; it was not blindly replayed as an execution. Native production
calls and exact readback through that connector passed. Full refreshed-chat discovery
is NOT_VERIFIED; live production MCP Host/authority acceptance is PASS.

No rollback compatibility implementation or legacy task/Lease migration was requested
or built. Preserved source, policy, qualification evidence and runtime records remain
available for diagnosis; enabling a second writer is not a recovery procedure.
