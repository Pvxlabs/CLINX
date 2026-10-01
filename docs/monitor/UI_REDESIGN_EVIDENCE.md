# CLINX Monitor UI/UX redesign — macOS run evidence

This file records the macOS evidence for the redesign implemented in
`MonitorApp/Sources/CLINXMonitor` (see `UI_REDESIGN.md` for the Figma → SwiftUI map).

## 1. Execution environment

| Item | Value |
| --- | --- |
| Host | `air` (Tailscale `100.126.61.35`), user `tinzleung` |
| OS | macOS 26.6.2 (arm64) |
| Toolchain | Xcode at `/Applications/Xcode.app/Contents/Developer`, Swift 6.4 |
| Build tree | `~/clinx-monitor-verify/MonitorApp` (clean copy of the committed revision) |
| Transport | SSH over Tailscale; no interactive login was created for this run |

## 2. Command ladder (clean copy of the committed revision)

```text
swift package resolve     OK
swift build               Build complete! (9.31s)
swift test                27 tests, 0 failures
swift build -c release    Build complete! (11.78s)
./Scripts/build-app.sh    -> .build/CLINX Monitor.app
launch (capture run)      16/16 window renders written, app terminated itself
```

`swift test` covers the 6 pre-existing tests plus 21 new view-model tests (status mapping,
freshness, progress, view filters, synthetic acceptance contract, keyboard selection,
pagination, read-only boundary). One real defect was found by those tests and fixed before
the screenshots: `FAILED` + `retry_required` was being rendered as `BLOCKED`.

## 3. How the screenshots were taken

A remote `screencapture` cannot be used: the SSH session has no Screen Recording permission
(`screencapture` returns “could not create image from display”, verified on the host).

Instead the app captures **its own window** through `NSWindow` + `cacheDisplay(in:to:)`,
which is an in-process render and needs no system permission. The harness
(`CaptureHarness.swift`) is inert unless `CLINX_CAPTURE_DIR` is set, is read-only, and only
drives the synthetic scenarios:

```sh
CLINX_CAPTURE_DIR=$HOME/clinx-capture \
  ".build/CLINX Monitor.app/Contents/MacOS/CLINXMonitor"
```

Each capture is the full window frame (title bar + toolbar + panes) at the design's exact
reference size, at 2× backing scale.

The Figma reference images were produced by rendering the design's own Make source
(`gjtQIi4778Yoh5cvRv5gwS`, `src/monitor/*`) in a local Vite project via headless Chromium at
the same sizes, so every comparison is a same-size, same-scale image pair.

| Evidence | Location |
| --- | --- |
| macOS window renders (16 PNG, 1440×900 / 1100×720 / 900×652 / 560×450 @2x) | `/home/pvxlabs/dev/clinx-ui-evidence-20261001/macos-window/` |
| Figma reference renders (15 PNG, same sizes) | `/home/pvxlabs/dev/clinx-ui-evidence-20261001/figma-reference/` |

## 4. Screen review

| Screen | Rendered | Notes from the reference comparison |
| --- | --- | --- |
| 04 Live / Healthy | `04-live-healthy.png` | CURRENT tag, no attention group, no blocker panel |
| 05 Active | `05-active.png` | Matching rows, 2px attention edges, grouped “Needs attention → Running” |
| 06 Blocked | `06-blocked.png` | BLOCKER panel: label + code, title, 2-line summary, facts row, evidence disclosure, read-only footer |
| 07 Failed | `07-failed.png` | FAILURE panel, red timeline event with exit code and duration |
| 08 Completed | `08-completed.png` | Completed pill, 14 / 14 · 100% green bar, DONE badges |
| 09 Stale | `09-stale.png` | 28px degraded strip, STALE tag in list + inspector, “Last known running” group, running→stale, 80% dim |
| 10 Offline | `10-offline.png` | LAST KNOWN tag, hollow-square indicator, last known data still visible |
| 11 Empty | `11-empty.png` | Connected + healthy, empty list and inspector states |
| 12 Synthetic (Long Content) | `12-synthetic-long.png` | Purple edge + hatched SYNTHETIC DATA badge + scenario selector; CJK/long titles clamp at 2 lines in the inspector and truncate in rows |
| 13 Settings | `13-settings.png` | Native Settings scene, Connection/Appearance/Shortcuts; credential fully masked; read-only footer |
| 14 Dark (blocked) | `14-dark-blocked.png` | Dark semantic tokens across panes, badges and status colours |
| 14b Dark (synthetic) | `14b-dark-synthetic.png` | Dark + synthetic identity |
| 15 Compact 1100×720 | `15-compact-1100.png` | Sidebar rail with counts; list 340 |
| 15b Compact 900×652 | `15b-compact-900.png` | Rail, list 300, inspector stacks Overview above Timeline, stat strip reflows to 2×2 |
| 16 Search interaction | `16-interaction-search.png` | Query “audit” narrows the list to 2 rows, count and pagination follow |
| 17 Keyboard selection | `17-interaction-keyboard-selection.png` | ↑/↓ moved the selection 3 rows; inspector follows the selected execution |

The 15th reference frame (`active-1440-light.png`) is the first render of the same screen and
is kept for provenance.

## 5. Correction loops

Three screenshot-review rounds were run; each item below was found in a rendered image and
verified fixed in the next capture:

1. **Round 1** — duplicate window title (custom toolbar title *and* native centred title);
   capture specs addressed executions by execution ID while the fixture indexes rows by task
   ref, so the inspector stayed empty; window frame came out 52pt short of the reference;
   timeline timestamps wrapped inside a 50pt rail.
2. **Round 2** — a blocked execution displayed a finished phase as “Plan 4/4 · 100%”; the
   terminal-state event rendered a green check on blocked/failed executions; the compact
   toolbar pushed search into the overflow menu; the 4-column stat strip truncated in a
   stacked inspector.
3. **Round 3** — “Offline” was truncated to “Offli…” in the toolbar cluster; the empty
   scenario still rendered an empty “Hosts” heading.

## 6. Known, deliberate deviations

* Row height 54 (documented spec) vs 52 in the design's Make implementation.
* Compact minimum window renders 900×652 rather than 900×640: the native title bar plus
  toolbar is 52pt, and the content minimum is 600pt.
* At 900pt width the authority segment of the connection cluster collapses (CURRENT/STALE/
  LAST KNOWN stays visible in the list header, and degraded/offline always shows the
  connectivity strip).
* Timeline evidence disclosure lives on the blocker/failure panel: the Observer wire model
  carries no per-event payload, so event rows show the canonical event reference instead.
* The design's in-window settings modal is the native Settings scene; screenshots of it are
  560×450 rather than a 1440×900 window.

## 7. Not covered by this evidence

* A real Observer connection (endpoint, Tailscale ACL, TLS, Keychain credential, live data):
  no real Observer endpoint was contacted from this Mac during the redesign run.
* Mouse-driven interactions that live in view-local state (filter popover, evidence
  disclosure, right-click copy menu) were exercised in code and unit tests, not photographed.
