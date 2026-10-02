# Exact native completion ingestion — 2026-10-02

## Incident

Task `RCA DATA L25 实时架构与成本` / `task_d974fcd472034b95aaa6f8797f91e14d`:

- Old execution: `exec_ab8a145d457f43f7b2cafbb9e0341fa0`.
- Thread: `01a0fa9b-13ce-7fd2-84d0-82cc87e1c429`.
- Old turn: `01a0fc17-45dd-7d50-b28a-4fb4370f15cc`.

Read-only observations proved the exact old turn completed on both configured
endpoints, with a unique native idle owner. Reconciliation then requested 100 items;
that response raised UnicodeDecodeError, surfaced as malformed websocket JSON.
Requests for 1 or 10 newest items succeeded. The failed detail read replaced valid
terminal liveness with PROVIDER_UNAVAILABLE and left completion delivery pending.

The exact final agent item uses `phase=final_answer` and declares a standalone
fenced `FINAL_STATUS=PASS（本轮本地范围）`. It retains production/CORE NOT_RUN limits.
It has no CLINX_EXECUTION_RESULT header. During investigation the existing runtime
finalized the old execution as BLOCKED for a missing marker, and a successor
execution started. Terminal state and reported qualification must be kept distinct;
no generic prose PASS, command output or task completion alone qualifies as success.

## Code change

- Fetch newest exact-turn items singly (maximum 20 bounded pages), stopping at the
  exact final Agent item. Reject a mismatched turn; skip commentary and tool output.
- Accept native final phases `final` and `final_answer`.
- Normalize only a unique, standalone fenced FINAL_STATUS=PASS declaration with an
  optional parenthesized scope. Existing strict contracts keep precedence; malformed
  strict contracts, ambiguous/duplicate statuses and ordinary prose are not promoted.
- Preserve the complete original report before the normalized result, with scope
  and NOT_RUN limitations. Changed files remain UNKNOWN rather than fabricated.
- Exact owner/turn re-observation, host-delivery reconciliation, finalizer ownership
  and lease-release barriers are unchanged.

## Verification

- P620 candidate: 169 tests passed, 18 subtests passed across native completion,
  liveness, owner routing, durable completion, result protocol, lifecycle, thread
  identity and delivery suites.
- Initial Mac broad run: 168 passed / 18 subtests; one setup failure because the
  dedicated discovery Python environment lacks jsonschema. P620 has the dependency
  and the entire same selected suite passed. No dependency was installed for this fix.
- Real provider read with the candidate normalized the exact old final message to
  PASS and retained both its local-scope qualifier and production NOT_RUN declaration.
- Historical repair was dry-run on a SQLite backup, then compare-and-swap applied.
  Only the old execution_results and execution_history rows changed, plus one audit
  journal entry. Snapshots of every other table/row were verified unchanged within
  the write transaction. Current task, successor execution and all leases were kept.
- Public CLINX get_status readback: old execution COMPLETED, result PASS, blockers
  NONE; original received_at preserved. Full original result/history are retained in
  execution_result_corrections. External audit writeback is PENDING; no older Linear
  status was applied over the successor's current task.
- Native macOS UI inspection was unavailable (Computer Use timed out); no fresh
  screenshot PASS is claimed for this backend fix.

## Activation and rollback

Candidate: `/home/pvxlabs/.local/lib/clinx-control/releases/native-completion-fdb3e107032c`.
Bridge SHA-256: `fdb3e107032c570114a0bfbce32c59626993dde7bd8a0bf7b0a15b3d8be2b0b4`.
Manifest digest: `7e29e16bee5acd7fbc1d111966ecf46f3632fd6c12603d7247df9c9b467f3417`.
Rollback: `/home/pvxlabs/.local/lib/clinx-control/releases/3fb10b5-recovery-c24cd0060f90`.
Only bridge.py differs in the candidate runtime; other packaged files were hash-verified.
Activation initially deferred, then explicitly authorized by the user on 2026-10-02.
Activated at 20:18:26 Asia/Shanghai (12:18:26 UTC) by atomically switching the
current symlink and restarting only clinx.service and clinx-tunnel.service.
All 229 manifest file hashes were verified before activation. Both services are
active with zero automatic restarts; Provider, Observer and the three active ORION
user services retained their exact PIDs and start times. Public MCP readback after
activation still reports the old execution COMPLETED / PASS. Rollback retained.

The successor exec_0f02961ee4f34731a13f1d6da7b53f6d finalized before this switch
(at 12:14:43 UTC) with an explicit native BLOCKED report for missing production
authority/qualification. Its current BLOCKED projection is distinct from the repaired
historical execution and was not overwritten.

The existing signed Monitor 0.3.0 (build 3) bundle was installed byte-for-byte at
/Applications/CLINX Monitor.app, preserving its local signing identity and settings.
Signature verification passed. The running executable path resolves to Applications;
real window inspection shows P620 Connected and Activity feedback. This is a local
installation, not a Developer ID signed/notarized distribution release.
Provider and ORION processes must remain running across any control-plane switch.

Backup: `/home/pvxlabs/.local/state/clinx/maintenance/completion-20261002/before-1790942875246989152.sqlite3`.
SHA-256: `a6f1f85eaca71206bff40d1a5e6a3c865ed74ac387d03887d86a86aa890faeff`.
Repair script: `docs/evidence/native-completion-20261002/repair_incident.py`.
The script is incident-specific, default dry-run, and refuses changed/other records.
A software rollback must not restore the whole backup database over new task data.
