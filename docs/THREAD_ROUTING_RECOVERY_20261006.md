# P620 CLINX thread routing recovery, 2026-10-06

Exact native thread reads through the public CLINX connector returned
`THREAD_HOST_CONFLICT` before reading any thread or execution state. The running
MCP release was `network-observation-27bec157`, not the control `current` symlink.

## Causes and provenance

1. The MCP routing registry `tasks.sqlite3.nodes.sqlite3` retained an authorized
   `p620-smoke` node in user scope `tinzleung`, with `route_ids=["p620"]`, alongside
   the real `p620` node. It had no paired key fingerprint, endpoint, live process,
   indexed threads, request ledger, reference routes or execution fences. Its
   last recorded observation was 2026-10-04 10:03:58 UTC. A historical diagnostic
   at 10:33:58 UTC that day already showed the duplicate. This establishes that
   the residue predates today's incident; it does not identify its creator or
   the exact time the first user request failed.
2. The local MCP node was registered at process startup with a 120-second
   freshness threshold. Reads rejected stale nodes before calling the attached
   local reader, and only successful reads renewed the observation. Thus an
   idle local process could never recover by reading its native history.
3. `clinx-tunnel.service` and the transient `clinx-air-resume-canary.service`
   concurrently polled the same `clinx-p620` profile. Both loaded the old MCP
   code. Stopping an MCP child also stopped its tunnel; the canary continued
   serving the old route guard until explicitly stopped.

## Changes

- Host alias selection considers trusted, authorized nodes. Revoked records
  remain in the registry as evidence without occupying an executable route.
- An attached, in-process local reader can attempt a real read after idle.
  Its observation is renewed only after the callback returns. Remote transports,
  including a remote transport with the local node name, retain freshness checks.
- Retired only `p620-smoke` via the deployed `NodeRegistry.revoke` operation.
  No SQL edits to Task, Execution, result, lease or receipt records were made.
- Staged an immutable successor of the running MCP release, changing only
  `node_protocol.py`, and switched the existing runtime release pointer.
- Restored the enabled formal tunnel service and stopped the duplicate transient
  canary. The independent Provider and managed execution owner retained their PIDs.

## Validation

The isolated repository environment uses `requirements-discovery-dev.txt`,
`jsonschema` for existing tests, and the existing ABI-compatible OPAQUE wheel.
Tests use a separate HOME and fixture databases. The first run exposed a test
API typo and the missing native pairing dependency; its failed log was retained.
After fixing the test and installing the existing wheel into this test environment:

`84 passed, 13 subtests passed` across `test_thread_router_recovery.py`,
`test_node_protocol.py`, `test_node_control.py`, `test_thread_identity.py`,
and `test_native_interop.py`. Syntax and `git diff --check` passed.

Public connector readback of the running P620 chat now returns
`THREAD_UNBOUND`, `context_status=AVAILABLE`, `source_node_id=p620` and complete
requested-node coverage. `THREAD_UNBOUND` describes a native chat without an
adopted CLINX Task; it is not an execution failure. It does not establish
recovery of an unspecified historical execution. Historical execution state
must remain as recorded.

After 139.9 seconds without a local thread query, exceeding the original
120-second threshold, the public status read still returned available native
context from `p620` with complete requested-node coverage. An exact Desktop URI
read also succeeded. The existing bound thread
`01a106ea-2ec3-75d2-ac28-ad7d469b8eb6` resolved to its original Task and Execution,
retaining `execution_state=COMPLETED`. No execution was resumed or replayed.

Logical hashes of all Task, active/historical Execution, result, workspace lease,
Host execution, Host delivery and production resource lease tables matched the
preflight snapshot. No active managed Execution existed at switching time.
Desktop Commander also successfully executed the read-only `hostname` command
on the same P620 device after its environment-proxy recovery.

Immutable preflight, manifest, test logs, postflight and idle readback evidence:
`/home/pvxlabs/.local/state/clinx/maintenance/thread-routing-recovery-20261006/`.

The deployed protocol file SHA256 is
`2202222fa09212504078511569a8fa35b44f59f50c79ffca0be90a503577ebf1`.
The base source commit is `27bec157735d261a789c0c248f4125d807f04b02`.
