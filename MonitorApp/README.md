# CLINX Monitor macOS app

macOS 13+ SwiftUI read-only client for the private CLINX Observer. `Package.swift` is the
project manifest; Xcode can open this package. `Sources/CLINXMonitor` contains the shipping
model, HTTPS client, view model and UI. The Swift files in `docs/monitor` remain the
Phase 0/1 handoff snapshot and are not the app build source.

## Build and run (macOS)

```sh
cd MonitorApp
swift test
./Scripts/build-app.sh
open '.build/CLINX Monitor.app'
```

The build script makes an unsigned local `.app` bundle and renders the app icon from the
design spec (`Scripts/make-app-icon.swift`, Figma page 17 “App Icon”). Distribution signing
and notarization have not been performed.

## UI/UX (Phase 2 redesign)

The window is a three-pane desktop shell — **Sidebar · Execution list · Inspector** — under
one compact toolbar. `docs/monitor/UI_REDESIGN.md` maps every screen back to its Figma
source (`CLINX Monitor UI/UX Redesign`, Figma Make file key `gjtQIi4778Yoh5cvRv5gwS`),
including tokens, metrics and the seven-state status system.

Design authority and code authority are kept separate:

* **Figma** owns colour, spacing, typography, layout and component hierarchy.
* **This code** owns the Observer contract: models, four authenticated GET routes, offset
  and cursor pagination, freshness and error semantics, Keychain credential storage.

### Local build signing and Keychain authorization

Run `Scripts/setup-local-signing.sh` once on a development Mac, then use
`Scripts/build-app.sh`. The setup imports a non-extractable local code-signing key
into the default Keychain, with private-key access limited to `/usr/bin/codesign`.
It does not change system certificate trust. Temporary key material is removed.
Keep this identity across rebuilds; do not replace the resulting signature with
`codesign --sign -`. An existing development certificate can be selected through
`CLINX_SIGNING_IDENTITY` (certificate common name).

On the first launch after switching from ad-hoc signing, macOS may request access
to the existing Observer credential. Choose **Always Allow** in that system dialog
to authorize this stable app identity. The credential stays in Keychain; the app
never stores the Mac login password. A locked Keychain can still require unlocking.
This self-signed identity is for local development, not distribution/notarization.

### Keyboard shortcuts

| Keys | Action |
| --- | --- |
| `⌘R` | Refresh from the Observer |
| `⌘,` | Settings |
| `⌘1`–`⌘5` | Active · Blocked · Failed · Recent · Completed |
| `↑` `↓` | Move the row selection |
| `Esc` | Clear selection |
| right-click | Copy title / execution ID / summary |

### Read-only

The client calls the ADR-006 GET routes, including the Activity v1 addendum. It has **no** execution controls: the
only verbs are Copy, Show, Filter and Refresh. Opening a task shows the current
execution and its allowlisted event evidence; event history can be `PARTIAL` or
`UNAVAILABLE`. Progress stays “Progress unavailable” unless the Observer persists a
canonical denominator. A task result `PASS` means only an exact structured result for that
task/execution; it is not deployment acceptance.

### Synthetic acceptance

Settings → Appearance (or the toolbar scenario selector once enabled) switches the client
to the synthetic acceptance data set. Synthetic mode is always badged `SYNTHETIC DATA` with
the purple window edge and the scenario selector, and is served through the same read-only
`ObserverServing` contract. Live mode shows `P620 · LIVE`.

The fixture in `Tests/CLINXMonitorTests/Fixtures` remains the schema example set. Acceptance
on a real Mac still requires building and launching the app, connecting to an authorized
Observer, and checking UI, Keychain, TLS and tailnet ACL behavior with real evidence.


### Activity

The Activity tab follows Raw snapshot. While visible it polls the exact execution's
public persisted Codex feedback approximately every two seconds. The initial page
contains the latest 40 messages; Load earlier feedback pages backwards. Revisions
replace the same stable message ID. Scrolling upward pauses following and exposes
Jump to latest / New activity. Tool activity uses the existing exact Host metadata;
raw command output is not transported. Structured results remain separate from public
feedback. A missing native source/turn or old Observer version is explicitly shown.
Synthetic sources do not claim native feedback. Synced means last successful read,
not provider liveness or the generation time of the last message.

See ../docs/monitor/ACTIVITY_V1_SPEC.md and ACTIVITY_V1_ACCEPTANCE.md for contract and
validation. The Observer unit exposes only the native state/history DB and WAL/SHM
companions as read-only mounts. If Codex recreates these files, restart the standalone
Observer to refresh its file mounts; this does not restart the provider or CLINX Core.
