# Monitor LAN Pairing v1

Status: implementation contract, 2026-10-02.

## Objective and scope

Settings → Devices discovers nearby CLINX hosts, pairs using the existing production
OPAQUE protocol, obtains an explicitly shared read-only Observer connection, verifies
it, and displays real tasks without manually entering an address or bearer token.
User confirmed the complete flow in this conversation. Existing main-window geometry,
Activity performance, local archives, manual connection and execution semantics remain.

## Milestones and dependencies

- M1 / PVX-1875: scope, UX and connection contract (this document).
- M2 / PVX-1876: real macOS arm64 pairing runtime; authenticated Observer bootstrap;
  bounded local GUI adapter; protocol and credential tests.
- M3 / PVX-1877: Devices settings, pairing sheet, connection validation and recovery.
- M4 / PVX-1878: signed App, real iMac/P620 discovery/pair/relaunch and screenshots.

Linear milestone: CLINX Monitor — 局域网发现、配对与只读连接, in CLINX V2.
Complete and validate each phase before advancing. No commit/push until requested.

## UX contract

- Dedicated Devices tab in native Settings; current app semantic typography/colors.
- Current connection summary, Paired devices, Nearby devices. Stable node identity
  keys, names, online/offline/connecting/connected states, refresh and empty states.
- Unpaired device → Pair and connect → focused four-digit secure input, device name,
  explanation that the code comes from that device, Cancel and Connect.
- Pairing is asynchronous. Failure preserves the current working Monitor connection.
  Explain invalid/expired code, unavailable pairing backend, identity mismatch,
  absent host sharing and unavailable Observer distinctly where protocol allows.
- Paired online device → Connect without PIN. Persist selected device and reconnect
  after App relaunch. Offline records remain visible with Retry when rediscovered.
- Forget removes this Mac's stored device trust and connection credential; explain
  that it does not revoke copies on the other host. Confirm before forgetting.
- Retain Connection as the manual advanced route. No automatic connection to an
  arbitrary discovered device, no silent switch between trusted hosts.

## Connection and authority contract

- Existing discovery advertisements remain untrusted; no tokens in mDNS.
- Existing PAKE, identity binding, TLS, trust persistence, attempt limits and expiry
  remain the authority for pairing. No DEV fallback.
- Host explicitly opts into Monitor bootstrap using a private configuration file
  referencing its existing Observer credential environment file and HTTPS endpoint.
  Default device listener continues to offer identity sessions only.
- A separate `observer_connection` operation requires production trusted mTLS identity.
  It returns only this host's configured HTTPS endpoint and read-only Observer bearer.
  This is explicitly granted read access, never shell/Host Executor/task mutation.
- Observer remains its existing private HTTPS service (currently P620 Tailnet HTTPS).
  LAN discovery/pairing does not imply that its HTTPS route works without the existing
  network route. Surface reachability failures; no TLS bypass or new public endpoint.
- Mac validates `/v1/health` and schema/read-only status using candidate credentials
  before replacing current settings. Store credentials only in Keychain, per device.
- GUI runtime uses inherited private process channels, never argv/env/files for PIN.
  The CLI's TTY-only PIN input remains unchanged. A dedicated GUI adapter may read a
  bounded one-use PIN from its parent process channel; no transcript or debug logging.
- Bundle the matching adapter source; install pinned Python/native dependencies in a
  dedicated local application-support runtime. Show actionable unavailable-runtime
  state rather than claim pairing is ready. Runtime installation is a build/setup step,
  never an arbitrary command received from a discovered host.

## Acceptance

1. Production OPAQUE build/import and handshake on real macOS arm64.
2. Unpaired, revoked, DEV and mismatched identities cannot retrieve bootstrap secrets.
3. Host sharing disabled returns no connection data; malformed endpoints are rejected.
4. GUI PIN/credentials never enter argv, logs, UserDefaults, evidence or fixtures.
5. Discover real P620; pair; verify health; show real tasks; relaunch without PIN.
6. Failed pair/health preserves current connection. Forget clears selected trust and
   associated credential; unrelated manual connection remains available.
7. Real light/dark Settings screenshots and main-window readback; targeted Swift/Python
   tests; no scroll/performance regression. Do not report unmeasured checks as PASS.

## Explicit exclusions

New execution permissions; CLINX Core/ORION changes; public discovery; relay/mesh;
new cryptographic algorithms; broad UI redesign; remote automated pairing-window opening
from an unauthenticated client; revoking other Macs' shared Observer bearer copies.
