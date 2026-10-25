# CLINX macOS app

macOS 13+ SwiftUI read-only client for the private CLINX Observer. `Package.swift` is the
project manifest; Xcode can open this package. `Sources/CLINXMonitor` contains the shipping
model, HTTPS client, view model and UI. The Swift files in `docs/monitor` remain the
Phase 0/1 handoff snapshot and are not the app build source.

The [native runtime manifest](../architecture/native-runtime.json) declares three signed
Rust helpers: `clinx-node`, `clinx-provider` and `clinx-node-daemon`. Swift discovery
calls `clinx-node --discovery-gui` through bounded private pipes; `CLINXNodeService`
execs `clinx-node-daemon helper`. The App contains no Python sources, interpreter
or OPAQUE wheel. Incremental builds remove the old build-owned runtime trees and
preserve Swift assets. Device state remains in the original private state directory.
See [the architecture boundary](../docs/REPOSITORY_ARCHITECTURE.md).

## Build and run (macOS)

```sh
cd MonitorApp
swift test
./Scripts/build-app.sh
open '.build/CLINX.app'
```

The build script makes a locally signed `.app` bundle and renders the app icon from the
design spec (`Scripts/make-app-icon.swift`, Figma page 17 “App Icon”). Distribution signing
and notarization have not been performed.

## UI/UX (Phase 2 redesign)

The window is a three-pane desktop shell — **Sidebar · Execution list · Inspector** — under
one compact toolbar. The archived UI design report maps the screens to their Figma
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

### Menu bar

The CLINX ring in the macOS menu bar shows the Observer connection and the same
archive-aware, filtered category counts as the window sidebar. Open CLINX or a
category brings the existing window forward, or reopens it after closing. Refresh,
Settings and Quit are also available. Closing the window keeps the read-only
Observer polling for the menu bar; Quit ends the app. The monochrome template mark
adapts to the system menu bar appearance.

Clicking the Dock icon uses the same restore action, including when only Settings is
visible. Both actions reuse the current main window, so repeated clicks do not create duplicates.
The bundle and display name are `CLINX`; the existing bundle identifier, signing identity,
Keychain service and application-support directories retain their established values.

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
`UNAVAILABLE`. The stat shows the known execution state when no canonical work denominator is
persisted. A real phase count or canonical percentage is displayed as progress; a completed
native turn is labelled “Turn completed” and does not imply business qualification. A task result `PASS` means only an exact structured result for that
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

The task list and inspector header show the workspace name; hover reveals the full
path and the list context menu can copy it. The header keeps status, workspace and
host without duplicating the inspector's metadata. OVERVIEW uses uppercase field
labels and a shared 12pt base size (including monospaced IDs). Click TASK ID or
THREAD ID to copy the complete value; Copied / Copy failed confirms the outcome.
Activity summaries belong to the Activity tab. A missing exact turn model is
UNKNOWN; configured-only model names are labelled as configured. New observation
sources export the model only from the indexed native turn's own turn_context.

The Activity tab follows Raw snapshot. While visible it polls the exact execution's
public persisted Codex feedback approximately every two seconds. The initial page
contains the latest 40 messages; Load earlier feedback pages backwards. Revisions
replace the same stable message ID. Scrolling upward pauses following and exposes
Jump to latest / New activity. Tool activity uses the existing exact Host metadata;
raw command output is not transported. Structured results remain separate from public
feedback. A missing native source/turn or old Observer version is explicitly shown.
Synthetic sources do not claim native feedback. Synced means last successful read,
not provider liveness or the generation time of the last message.

See [the Activity contract](../docs/monitor/ACTIVITY_V1_SPEC.md) and
[historical validation archive](../docs/README.md#历史资料恢复). The Observer unit exposes only the native state/history DB and WAL/SHM
companions as read-only mounts. The companion `clinx-observer-history-guard.timer`
checks their identities every five seconds and refreshes only an active standalone
Observer when Codex replaces a file. Observer restart invalidates Activity cursors
so the client reloads recovered feedback. The Provider and CLINX Core keep running.
# Nearby devices and pairing

Settings → **Devices** discovers CLINX hosts on the same local network. Select
**Pair…**, enter the code displayed by that host, and choose **Pair and connect**.
After production OPAQUE authentication the App requests the host's explicitly
shared read-only Observer configuration, verifies its health, and saves a
device-specific credential in Keychain. Relaunch uses that saved connection;
**Connect** on a paired device needs no new code. **Disconnect** stops using it;
**Forget device…** removes this Mac's trust and saved credential.

The existing **Connection** tab remains the manual configuration route. Pairing or
health failures do not replace the current connection. Pairing success followed by
a service failure offers **Connect Monitor** without making you pair again.

Local developer setup requires a native macOS Rust toolchain and Xcode command-line
tools. Run `sh MonitorApp/Scripts/build-app.sh`. The historical
`setup-discovery-runtime.sh` command now delegates to that App build; it does not
install a per-user Python environment. Existing device identities and peer files
remain compatible. Native Mac build/signing and LAN discovery must be qualified on
macOS before distributing the new artifact; Linux wire tests do not establish this.

Host opt-in uses an owner-only (0600) JSON file, for example:

```json
{
  "share_read_only_observer": true,
  "endpoint": "https://your-host.your-tailnet.ts.net:8449",
  "credential_file": "/home/your-user/.config/clinx-observer/observer.env"
}
```

In the host's configured CLINX environment, run:

```sh
clinx pair accept --observer-config ~/.config/clinx-observer/monitor-pairing.json
```

This displays a local one-use, 60-second code. Leave the process running for trusted
reconnections, or use `clinx serve --observer-config …` after the pairing process
exits. Only one listener can own the device identity at a time. Later pairing needs
a new local `pair accept` window. A normal listener without this option grants no
Monitor access. Shared read-only bearer credentials are not per-peer revocable on
the host: rotate the Observer bearer to revoke previously issued copies.

Discovery and initial pairing use LAN multicast/TLS. The Observer retains its
configured private HTTPS route (currently Tailnet HTTPS on P620), which must also
be reachable. Discovery does not cross routed VPN subnets. See
[the implementation contract](../docs/monitor/LAN_PAIRING_SPEC.md) and
[historical acceptance archive](../docs/README.md#历史资料恢复).

Helper 执行业务 API 由同源码构建的 Rust `clinx-node` 承担，Provider 协议由
`clinx-provider` 承担；两者在本机 Mac 架构编译、放入 `Contents/Helpers/` 并分别签名。
`CLINXNodeService` 固定 bundle 内路径；缺失二进制时不回退到 Python 业务实现。
Python 仍负责既有配对、mTLS、发现和观察网络运行时。迁移验证与未验证边界见
[Node API 迁移](../docs/RUST_NODE_API_MIGRATION.md)。
