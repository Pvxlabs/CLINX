# Rust Node/Centre runtime

`clinx-node-daemon` owns the Helper and Centre process lifecycle. The macOS
`CLINXNodeService` launcher directly executes its bundled Rust binary; the Swift
device UI invokes `clinx-node --discovery-gui`. Neither normal path starts Python.
The historical Python entrypoint names only locate and `exec` the same binary;
the Helper shim's explicit observation factory remains a qualification API.

The daemon assembles existing contracts rather than adding a business authority:

- The Centre accepts authenticated mTLS connections, reloads durable trust and
  sharing scopes for each request, binds the registered Node/user/fingerprint,
  and forwards the exact request identity to the local Rust Authority IPC.
- The Helper intersects local sharing with the Centre grant for every request.
  Node selectors, canonical adoption, request receipts and duplicate recovery
  reuse the Rust Node API and service contracts. The business state writer is
  still Rust Authority. The Node SQLite file stores protocol receipts, not a
  second task state machine.
- Provider execution and evidence travel through the existing paired relay and
  `clinx-provider`. Provider evidence remains associated with its exact
  task/execution/thread/turn identity; an upload ACK proves no business outcome.
- The observation source, outbox collection and supervision tick run inside
  Rust. An unavailable execution configuration does not remove an independently
  granted session-read capability. A malformed shared Provider config still
  fails startup, preserving the existing configuration boundary.
- SIGTERM/SIGINT revoke the active in-memory route and close listeners/outboxes.
  Tick errors take the same cleanup path. Interactive pairing uses terminal
  echo suppression and restores it on cancellation; the GUI PIN remains on its
  separate stdin line.

The existing Centre CLI flags remain accepted (`bootstrap`, `share`,
`configure-node`, `pair-listen`, `pair-connect`, `serve`). The packaged Centre
launcher selects `--rust-projection`; it no longer supplies a legacy canonical
SQLite database. Explicit local read-only legacy source parameters remain a
compatibility/qualification option and are rejected when Rust Authority is
selected, as before.

`fixtures/test_rust_node_daemon.py` starts two independent Rust processes and
performs real OPAQUE pairing, mTLS registration, Helper reads, reverse routing,
Provider WebSocket evidence reads, scope revocation, concurrent duplicate
claims, payload conflicts, lost-reply recovery after restart, and exact cancel
routing. Its Authority endpoint and desktop Provider are explicitly recording
fixtures. Core transaction and Provider semantics are covered separately by
the unchanged existing Rust/qualification suites. This is isolated Linux
qualification, not evidence of a deployed Mac/P620 execution. macOS toolchain,
bundle signing and real cross-device qualification remain separate checks.
