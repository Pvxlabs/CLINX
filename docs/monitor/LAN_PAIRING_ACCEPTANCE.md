# LAN Pairing acceptance — 2026-10-02

Status: implementation and local verification complete; full physical-device
acceptance pending same-LAN network conditions.

## Verified

- Native OPAQUE wheel built, installed and imported on this arm64 iMac. Xcode 27
  initially produced an unloadable LINKEDIT string pool. Building with release debug
  information retained and no stripping resolves the loader failure; the setup script
  fixes those flags. This matches the failure reported in
  [rust-lang/rust #157750](https://github.com/rust-lang/rust/issues/157750).
- 42 Python tests passed: production PAKE/TLS, exact peer identity, one-use/expiry/
  failure budgets, persisted reconnect, rejected key substitution, bootstrap opt-in,
  private configuration permissions, rejected malformed endpoint, and revoked peers.
- 65 Swift tests passed, including exact bootstrap identity/read-only checks, URL and
  credential rejection, saved device account restoration, disconnect/manual-route
  preservation, and discarding an old device's in-flight response after disconnect.
- Release `.app` build and stable local certificate signature verification passed.
- Real signed macOS App Settings screenshots inspected through native Computer Use:
  Light Devices empty state and discovered-device row; native pairing sheet and focus;
  Return submission, rejection message, Esc cancellation; final Dark Devices and
  corrected four-digit field. The placeholder clipping found in the first screenshot
  was corrected and the final build was captured again.
- Nearby row came from a real mDNS/TLS test listener on the iMac LAN interface,
  `CLINX Pairing Test`, using a temporary identity, closed pairing window and no
  Observer credential. This is a same-machine transport/UI test, not a second host.
- Existing Connection tab now checks Keychain metadata in the background. A native
  sample previously confirmed its on-appear secret read blocked the main thread.
- Final normal launch read the saved Keychain credential successfully: main window
  showed `P620, Connected`, a real running task with a fresh snapshot, and Devices
  showed `Manual connection / Connected`. Temporary capture launch arguments were
  removed and the original saved endpoint was preserved.

## Pending and limits

- iMac LAN: `192.168.1.87`; P620 LAN: `10.137.1.10`. Current route is through VPN;
  mDNS does not cross those subnets. User has been asked which same-LAN host can be
  used. No discovery of P620, physical cross-device pairing or sleep recovery claimed.
- Successful new-pair → Keychain → real task-list UI flow remains pending. Backend
  same-machine tests prove successful OPAQUE/mTLS/bootstrap but do not substitute for
  that human/device acceptance.
- Keychain authorization initially blocked readback. It cleared before final
  normal-launch verification; no further authorization is pending for the existing
  manual P620 connection. Automation did not access SecurityAgent.
- Wider DEV-SPAKE2 regression attempt could not install optional `spake2` because
  PyPI returned `No route to host`; the production/bootstrap test suite above passed.
- Source publication was authorized on 2026-10-02 after Settings layout review.
  P620 pairing runtime has not been activated. Dependency setup is a local developer installation, not a standalone
  notarized distribution.

## Independent runtime observation

During UI acceptance the existing P620 Observer hit systemd `start-limit-hit`
following native-history mount refreshes. `reset-failed` and `start` restored this
read-only service; subsequent service state was active and unauthenticated private
HTTPS health returned 401 as expected. No Core/Provider service was switched.
Root-cause remediation of repeated guard restarts is separate from the pairing UI.

## Linear

- PVX-1875 — M1 contract: Done.
- PVX-1876 — M2 native/runtime/bootstrap: Done, within local test evidence above.
- PVX-1877 — M3 SwiftUI: In Review.
- PVX-1878 — M4 physical iMac/P620 acceptance: pending prerequisites.

## Settings refinement accepted for commit — 2026-10-02

- Removed the repeated window title. Four equal-width (108pt) settings selectors
  retain icons, labels and selected-state accessibility.
- Devices and Connection share a 24pt inset, aligned introduction/status card and
  footer. Connection uses stacked endpoint/credential sections with equal action
  widths; Save and Replace remain visible, removal lives in the credential menu.
- Removed the separate connection-test button and duplicate last-success row;
  existing live connection status and sync recency remain visible.
- Real Dark macOS screenshots verified alignment and corrected Replace truncation.
  The final Settings refinement does not claim a new Light screenshot pass.
- 65 Swift tests passed; final Release build and stable-signature verification passed.
  Physical same-LAN pairing acceptance remains pending as recorded above.
