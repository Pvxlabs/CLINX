# Native Rust business authority

Status on 2026-10-23: **dedicated services installed and qualified before cutover;
production cutover BLOCKED**. The production release remains
`owner-shared-07da5fe20c4119dee1b4`, with its Python Owner and original Tunnel
launcher active. The installed Rust candidate is `rust-a4979396ab76a29385d4`.
A candidate that commits real work is not yet the sole production authority.

The workspace is `/home/pvxlabs/dev/CLINX-RUST`, branch `codex/clinx-rust`, HEAD
`5a870aecd5ea5ba12edb93eb45a34339568a64e4` plus the saved dirty source manifest.
No commit, push, reset, clean, stash, parallel checkout, or rollback implementation
was performed. Existing production artifacts and configurations remain retained.

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
`/var/lib/clinx-rust/authority/authority.sqlite3`; it contains acceptance work,
with no legacy task, lease or running-ledger import. Node identity, peer trust
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
request parameters, with matching public response schemas. The active production
Tunnel launcher has not been changed to this staged command.

Centre runs on the qualification loopback port 18774 with `--rust-projection`.
It reads managed task/turn projections through Rust and writes only existing Node
observation/index/outbox projections. It neither opens the legacy task database
nor invokes the Python canonical lifecycle. The production Centre remains on 18773.

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

## Remaining production cutover gate

The final candidate is running under all four dedicated identities, with the
acceptance ledger retained. The real external MCP connector still reports
`owner-shared-07da5fe20c4119dee1b4`; Python Owner PID 119760 remains active.
No legacy production service or public entry point has been switched.

Current concrete blockers are:

1. **RESOLVED, 2026-10-23 write-access correction.** The migration had incorrectly
   applied ProtectSystem=strict / ProtectHome=read-only to execution services and
   replaced the original Codex filesystem user with an unrelated UID. Provider
   and Dispatcher no longer impose filesystem-wide read-only mounts. Provider
   and Host execution use pvxlabs (UID 1001), preserving original file access
   without a project allowlist or recursive chmod/chown/ACL changes. The Rust
   writer remains UID 993. A real Provider task modified an existing 0600 file,
   created another file, successfully added/removed its Git index entry, and
   finished SUCCEEDED/RELEASED. The deployed Host helper also modified an owner-only
   file under the original development tree. HDL, CLINX-RUST, ORION worktree,
   /lab and the /data qualification directory are writable in the actual Provider
   namespace. Raw DB, writer FD, authority socket and path-alias access denial
   passed again for the corrected execution identity.
   Evidence: /lab/runtime-worktrees/p620/clinx-rust-business-20261022T165524Z/write-access-20261023T155501Z/.
   This supersedes the old read-only blocker and the earlier worker-UID description;
   it does not assert production cutover or full legacy process revocation.
2. Existing Python routing supports workspace-bounded dynamic projects (HDL is
   in use); the frozen candidate authority policy currently contains only orion
   and clinx. Preserve the existing bounded resolution semantics in Rust and
   seal a complete production policy before initializing clean production state.
   Editing policy.json beside the existing ledger is correctly rejected with
   AUTHORITY_POLICY_MISMATCH; do not bypass the stored policy check.
3. Exact Desktop observations found unrelated HDL and ORION turns inProgress,
   despite the other Provider reporting notLoaded/interrupted for the same IDs.
   Drain using the existing Owner admission/shutdown contract and obtain exact
   completion evidence from the actual endpoint before full legacy cgroup kill.
   The final readback still has HDL turn
   `01a16953-29bd-7c83-b88f-f82d0ddfb5b6` inProgress; ORION is no longer in
   the old registry active set. Rust also directly returned PROJECT_SCOPE_DENIED
   for HDL preparation; this is an observed admission failure, not conjecture.
   Do not infer safety from the alternate endpoint or stale ledger flags.

After these are closed: independently fsync the migration result, disable old
restart paths, inventory every old writable FD, kill/reap the exact old cgroups,
verify only Rust holds new DB/WAL/SHM write descriptors, and switch the existing
Tunnel launcher and Centre port together. Then perform acceptance through the
real external connector. No extra control service or rollback implementation
is required or implemented.

The final receipt keeps `safe_to_retire_old_control_plane=false`, production
cutover BLOCKED, whole legacy revocation NOT_VERIFIED, and post-cutover real
acceptance NOT_VERIFIED. Candidate success must not be reported as production
unique-writer success.
