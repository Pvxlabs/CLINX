# Activity v1 acceptance — 2026-10-02

Scope/contract: ACTIVITY_V1_SPEC.md. Linear: PVX-1871 / PVX-1872 / PVX-1873.

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
