# CLINX Monitor macOS app

This is the Phase 2 macOS 13+ SwiftUI MenuBarExtra client. `Package.swift` is the
project manifest; Xcode can open this package. `Sources/CLINXMonitor` contains the
shipping model, HTTPS client, view model and UI. The Swift files in `docs/monitor`
remain the Phase 0/1 handoff snapshot and are not the app build source.

On a Mac with current Xcode Command Line Tools:

```sh
cd MonitorApp
swift test
./Scripts/build-app.sh
open '.build/CLINX Monitor.app'
```

The build script makes an unsigned local `.app` bundle. Distribution signing and
notarization have not been performed. The UI asks for the private HTTPS Observer
base URL and an observer-specific bearer credential. The endpoint is stored in
local preferences; the credential is stored in this Mac's non-synchronizing
Keychain. Do not put a credential in a URL, shell argument, screenshot or issue.

The client calls only the four ADR-006 GET routes. It has no execution mutation
controls. Opening a task shows the current execution and its allowlisted event
evidence; event history can be `PARTIAL` or `UNAVAILABLE`. Progress remains unknown
when the Observer has no persisted denominator. A task result `PASS` means only an
exact structured result for that task/execution; it is not deployment acceptance.

The fixture in `Tests/CLINXMonitorTests/Fixtures` is synthetic. Acceptance on a real
Mac still requires building and launching the app, connecting to an authorized
Observer, and checking UI, Keychain, TLS and tailnet ACL behavior with real evidence.
