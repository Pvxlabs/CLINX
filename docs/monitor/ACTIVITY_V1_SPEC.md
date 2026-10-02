# CLINX Monitor Activity v1

Status: IMPLEMENTING. Authorized 2026-10-02: near-real-time persisted activity, approximately 2s polling.
Linear milestone: CLINX Monitor — Activity 执行动态 v1. Tasks: PVX-1871, PVX-1872, PVX-1873.

## Objective / boundaries

Add Activity after Raw snapshot. Show current execution's public assistant feedback,
existing Host operation summaries, and exact structured final result. Preserve Monitor
geometry, archive/navigation behavior and scroll performance. No execution actions,
Core lifecycle changes, ORION changes, provider subscriptions, raw tool outputs,
reasoning/analysis, new authority DB or history reconstruction. No token streaming.

## M1 — read contract (PVX-1871)

Authenticated read-only GET /v1/tasks/{task}/activity?execution_ref={execution}[&after=...|before=...].
The execution must equal the execution currently projected by Observer. Read its OWN
routing_identity_json conversation binding, host, and turn_id; require P620 local host
and Codex provider. Never borrow task's previous turn or current conversation binding.
Execution change returns 409; missing exact identity returns explicit UNAVAILABLE.
Only current-user native state_5.sqlite + thread_history_1.sqlite, mode=ro/query_only,
short read transaction, SQL budget. Current P620 uses paginated history. Unsupported
history modes fail visibly; no filesystem rollout scan or native provider calls.
Only agentMessage public text (commentary/final_answer or unclassified public message),
explicitly excluding analysis/reasoning. Stable item ID + creation order + revision.
Latest 40 initially; bounded older pages and revision-based incremental updates include
edits to existing items. Opaque cursors bind task/execution/thread/turn/source identity
and direction. 16 KiB text per item, <=40 items/page; visible truncation marker.
Secrets scrubbed before transport; no arbitrary nested content or raw protocol JSON.
Poll time is not proof of provider liveness. Source availability and message time remain
separate. Native projection lag is a limitation until measured with a running task.
Host activity/final result reuse existing exact execution snapshot fields in the Mac.

Acceptance: isolation across task/execution/thread/turn; latest/older/delta/update paging;
malformed/stale cursor; missing source and unsupported schema; secret redaction and
reasoning exclusion; bounded output; requests do not change authority/native DBs.

## M2 — macOS UI (PVX-1872)

Independent Activity state, polling only while tab visible, nominal 2 seconds between
requests. Merge by stable ID, replace revisions, keep order. Cancel and discard stale
responses on task/execution/source switch. Fetch older on demand. Do not poll hidden
history. Show unavailable/stale/disconnected states and retain last good content on
transient errors. Bottom follows new content; scrolling upward pauses following and
shows New activity / Jump to latest. Loading older preserves visible anchor.
Use lazy rows, no full snapshot JSON serialization or large diff work on main thread.

Acceptance: Swift tests for merge/update/isolation; existing Swift tests; build/sign;
real iMac screenshot/interaction for tab, refresh, bottom and scrolled-up behavior.

## M3 — deployment / real acceptance (PVX-1873)

Deploy ONLY standalone Observer and update its user unit with read-only binds for the
native index/history DB and their optional WAL/SHM files. Keep ProtectHome, no provider
sockets or authority write access. Preserve old immutable release and unit for rollback.
Validate exact real P620 task GET and iMac connection. Record latency only if new real
feedback is observed; don't create or resume business work to manufacture a measurement.
Commit/push and activation outcomes reported independently.

## Progress

- Planning: milestone and tasks created before implementation.
- Source discovery: current P620 schema supports exact paginated public agent items;
  screenshot execution has 10 public agent messages. Read-only source inspection only.
- M1: passed; Activity + existing Observer + JSON schema: 27 tests, 9 subtests passed.
- M2: passed; 57 Swift tests/build/sign passed. First real screenshot + scroll/task-switch validation passed. Authorization completed; corrected Release screenshots confirm Connected, feedback rendering and Jump to latest. User confirmed final-build manual scrolling is smooth and the reading position remains unchanged across refreshes.
- M3: Observer activated/read back with rollback retained. Real generation-to-display latency still unmeasured; see ACTIVITY_V1_ACCEPTANCE.md.
- Performance follow-up: user reported residual stutter after initial manual acceptance;
  M2 reopened for bounded client-only correction. Move Markdown preparation to decode on
  ObserverClient's actor, isolate sync-label observation, suppress empty-poll transcript
  publications and snapshot-only parent invalidation. Keep 2-second polling and content intact.
- Performance follow-up validated: 59 Swift tests, Release build/sign, actual P620 reconnection
  and screenshot passed. User confirmed continuous long-feedback scrolling is "明显顺畅了";
  M2 closed again. New-message end-to-end latency remains the separate M3 pending measurement.
