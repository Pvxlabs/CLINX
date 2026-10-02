# Activity v1 acceptance — 2026-10-02

Scope/contract: ACTIVITY_V1_SPEC.md. Linear: PVX-1871 / PVX-1872 / PVX-1873.

## 2026-10-02 stale history mount correction

- Exact reported execution: `exec_e47dc3d423ca40458cd77e0b95a7e141`.
  Direct native read returned 16 public messages while the running Observer HTTP
  returned AVAILABLE with zero items. Both native databases had the same inode in
  the host and service, but all four WAL/SHM companions differed. This isolates the
  stale individual bind mounts rather than a missing execution/thread/turn binding.
- Restarting only Observer recovered 17 messages, including the user's two quoted
  feedback paragraphs. The existing Mac still rendered empty even after switching
  tasks; the Activity state owner was therefore separated from its Equatable shell.
  After user-completed Keychain authorization, the rebuilt Mac app rendered the
  recovered public feedback and final response (real screenshot, 17:39 Asia/Shanghai).
- Added a host-side five-second timer that stats the six fixed native/mounted paths.
  Only differing stable identities trigger `try-restart` of the active Observer;
  missing primary databases, changing sources and stopped/replaced processes defer.
  It has no listener or message-content reads. Its host user namespace is necessary
  for `/proc/<pid>/root` inspection; the HTTP service retains all existing sandboxing.
- Added reader-lifetime cursor scope. A cursor captured before activation returned
  409 after activation; a fresh authenticated read recovered 19 messages. Six native
  read-only mounts and hidden `auth.json` were verified. Core/Provider/Tunnel PIDs
  remained 289423 / 1829817 / 289422.
- Isolated real systemd bind-mount fixture: ordinary content write caused no restart;
  replacing WAL changed fixture PID 1458426 to 1458438 and refreshed the mount; a
  stopped fixture was not started. No native data was modified for this test.
- 33 Python tests and 9 subtests passed; 61 Swift tests passed. Release build and
  stable signing passed. Logs: `/tmp/clinx-activity-refresh-swift.log` and
  `/tmp/clinx-activity-refresh-build.log`. The initial Keychain wait was resolved
  by the user; no credential or authorization policy was changed by this fix.
- Real UI readback after authorization: P620 Connected, the exact reported
  execution selected, and the 17:03/17:07 feedback plus 17:26 final response visible.
  HTTP returned 20 public items with the last marked result. Scrolling upward
  showed Jump to latest; scrollbar position stayed 0.6993939393939393 while Synced
  advanced from 17:39:56 to 17:40:32. Jump to latest returned to the final response
  and dismissed the button. Real screenshot and accessibility readback passed.
  The Provider turn had ended before this acceptance window, so newly generated
  message-to-screen latency remains unmeasured; do not treat polling as that proof.
- Activated Observer release:
  `~/.local/lib/clinx-observer/releases/activity-refresh-9dad52e70c22`.
  Server SHA256: `9dad52e70c22bf58d36b2f4c4dfcaea258325ce5f71fa840059b1510ef9b662c`.
  Guard SHA256: `e3a64e385ddff54315a6727c5fb159cc1a5028f6d0dfd535702f3f6a5f370baf`.
  Guard timer is enabled/active and the guard exits successfully. The installed
  Observer unit and credential are unchanged.
- Rollback: disable/stop only `clinx-observer-history-guard.timer`, remove its two
  new unit files, restore `current` to the path recorded in
  `~/.local/lib/clinx-observer/rollbacks/before-activity-refresh-9dad52e70c22/previous-release.txt`,
  daemon-reload and restart only Observer. The old release remains intact.
- These corrective changes have not been committed or pushed.

## Automated evidence

- Python Observer + Activity + JSON schema: 27 passed, 9 subtests passed.
  Command: /tmp/clinx-activity-venv/bin/python -m pytest -q test_observer_activity.py test_observer_server.py test_observer_schema.py
  Log: /tmp/clinx-activity-python-tests.log. Initial system Python lacked pytest/jsonschema;
  dependencies were installed only into the disposable /tmp/clinx-activity-venv.
- Swift: 57 tests passed, including four Activity tests for revision merge/replay,
  old-page cursor separation, execution mismatch, and late response after tab closure.
  Command: swift test --package-path MonitorApp. Log: /tmp/clinx-activity-swift-tests.log.
- Release build and stable certificate signature verification passed.
  Command: MonitorApp/Scripts/build-app.sh. Log: /tmp/clinx-activity-build.log.
- git diff --check passed. No commit/push for Activity yet.

## P620 actual read / activation

- Exact execution: exec_36780362f2e34f949fc52d6f3cd742c0.
- Native source: paginated state/history index; 12 public feedback/final-response items.
- Candidate direct read 4.8ms; activated Observer HTTP read 7.1ms; delta query with
  returned cursor gave zero new messages (no duplicates). These are read timings,
  NOT provider-generation-to-Mac latency.
- Release: /home/pvxlabs/.local/lib/clinx-observer/releases/activity-v1-002b10f28b6fad75
- SHA256: 002b10f28b6fad7568e2eac791ecfa5eb68ecbf8dd0889a84942f5c2bfbbc4b2
- Rollback: /home/pvxlabs/.local/lib/clinx-observer/rollbacks/before-activity-002b10f28b6fad75
- Previous release: /home/pvxlabs/.local/lib/clinx-observer/releases/56b4638f458a20ca08e6ccb87e2aa1d56f1e0bb3
- Previous server SHA256: cd66062d1da29520cb9ec34900e9d46dd9ba3a96964ce1a1f055510735a938b3
- Previous unit SHA256: 6af12fa00016a42f3ba32a7a9ad1ae178b686b1d64301c2b2f619c6ca5989c8b
- /proc/<observer-pid>/mountinfo confirmed all six native DB/WAL/SHM binds are ro.
  Core/provider processes were not restarted. No authority write or business execution.
- Rollback procedure: restore current symlink to previous release; copy saved observer.service
  to ~/.config/systemd/user/clinx-observer.service; systemctl --user daemon-reload;
  systemctl --user restart clinx-observer.service. Credential file stays in place.

## Real Mac screenshot / interaction

- First Release connected to P620 and displayed real execution feedback in Activity.
- Real task switch changed execution and cleared old feedback before loading the new feed.
- Scrolling up exposed Jump to latest; over 40s of polling preserved the same visible
  paragraph (10:41:12) rather than moving to the bottom. Jump to latest returned to the end.
- Screenshot: /tmp/clinx-activity-evidence/activity-scrolled.png.
- Screenshot review correction: format inline Markdown; label poll time Synced; make
  tool/result updates obey the same follow/pause behavior; avoid success icons for failed tools.
- Corrected Release built/signed and 57 tests passed again. User completed Keychain
  authorization; real Mac readback at 13:02–13:04 confirmed P620 Connected and advancing
  Synced timestamps. Final screenshots verify the Activity tab, inline Markdown, feedback
  and execution result. Jump to latest reached the final response/result.
- Final screenshots: /tmp/clinx-activity-evidence/activity-final-feedback.png and
  /tmp/clinx-activity-evidence/activity-final-latest.png.
- Final-build wheel automation returned windowNotFoundAtPosition. Accessibility ScrollToTop
  reached the beginning, but a later readback was at the end; that action does not establish
  the live-wheel pause contract. The user subsequently verified with the actual mouse:
  "顺畅，位置保持不变" after scrolling upward and waiting through refreshes. Final-build
  scroll/pause acceptance therefore passes on human interaction evidence.

## Remaining gates / limits

- Observe one newly produced real message to measure provider-persistence-to-Mac delay.
  Current real task completed before the final acceptance window; no task was started
  merely to generate measurements. Nominal poll interval is 2 seconds plus request latency.
- Corrected Release scrolling feel and reading-position retention confirmed by the user.
- Only paginated native history is supported; legacy/missing/unbound turns are explicitly unavailable.
- Tool display includes the existing latest 20 Host metadata records, not raw tool outputs.
- If Codex recreates a DB/WAL/SHM file, restart the standalone Observer to remount current files.

## Performance correction — 13:11

- User reported residual stutter after initial acceptance. Code inspection found Markdown
  parsing in lazy row body and four ObservableObject publications per unchanged background
  poll (loading twice, notice, sync time), plus snapshot-only parent invalidation.
- Markdown is now prepared during decoding on the ObserverClient actor. Sync time has
  its own small observed label; unchanged polls do not publish transcript changes. Activity
  compares only displayed task content when its parent receives a fresh snapshot.
- Poll cadence remains approximately 2 seconds. Full feedback, pagination and scroll
  behavior are preserved; no Observer/Core/service changes in this correction.
- Swift 59 tests passed, including new tests for zero transcript publications on empty/replayed
  updates and Markdown formatting/wire-payload preservation. Log: /tmp/clinx-activity-perf-tests.log.
- Release build and stable signature verified; log: /tmp/clinx-activity-perf-build.log.
  Actual rebuilt app reconnected to P620; real feedback and advancing Synced timestamps verified.
  Screenshot: /tmp/clinx-activity-evidence/activity-performance-final.png.
- User exercised continuous scrolling through long feedback and confirmed: "明显顺畅了".
  This establishes a perceived improvement, not a measured frame-rate guarantee. Short runtime
  samples are saved at /tmp/clinx-activity-before.sample.txt and /tmp/clinx-activity-after.sample.txt;
  they are not matched scrolling traces, so no before/after CPU or frame-rate claim is made.
- No commit/push for Activity yet. The separate new-message latency gate remains unmeasured.
