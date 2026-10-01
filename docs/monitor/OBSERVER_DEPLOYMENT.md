# Observer private deployment contract

Status: implementation and runtime qualification in progress (PVX-1854/PVX-1855).
Baseline: `78f4ce3a0311e506d5deaee0e56396ee25249bbb`.
Authority: ADR-006, CLINX_MONITOR.md and observer-v1.schema.json.

## Objective and boundaries

Deploy only the independent GET-only Observer against the existing P620 registry.
Do not restart CLINX, its provider/tunnel, HostExecutor, QEX, DRS or ORION. Do not
construct TaskRegistry, migrate/copy the DB, invoke MCP, or mutate tasks/executions.
The old Monitor UI and automation tooling are outside this deployment milestone.

## Milestones and acceptance

1. Audit source and qualify a non-root user-service sandbox: source/registry
   read-only, other home credentials hidden, no authority sockets available.
2. Add this contract, a minimal unit and opt-in method/status-only audit. Pass
   Observer/schema tests, full Python regression and git diff --check; create a
   separate commit, non-force push, and read back origin/main.
3. Install that exact commit's Observer and unit independently, provision an
   Observer-only random bearer in a protected 0600 environment file, and qualify
   health/task pages/task execution detail/events against the real DB/schema.
   Missing/wrong bearer must return 401; authenticated non-GET must return 405.
   Demonstrate OS-level denial of DB writes without attempting a DB mutation.
4. With the effective Tailnet policy verified, enable one private
   HTTPS Serve route and verify Tailnet allow/non-Tailnet deny, normal TLS trust,
   bearer rotation/revocation and formal Monitor real-data requests. Count methods
   in a separate formal-app observation window; only GET is accepted as PASS.

## P620 service layout

The existing authority uses the pvxlabs user manager. The independent Observer
uses that user manager with unprivileged user/mount namespaces, not root:

- Unit: `~/.config/systemd/user/clinx-observer.service`.
- Source: `~/.local/lib/clinx-observer/current/observer_server.py`; `current` points
  to a release directory named by the deployed canonical SHA.
- Registry: `~/.local/state/clinx/tasks.sqlite3`, including live WAL/SHM. Mount the
  directory read-only to follow live WAL replacement; do not use immutable=1.
- Credential: `~/.config/clinx-observer/observer.env`, mode 0600, containing only
  CLINX_OBSERVER_TOKEN. It is loaded by the user manager before hiding the home
  directory. Never write the bearer into a unit, repository, logs, argv or URL.

ProtectHome=tmpfs exposes only the Observer source and registry read-only. The
user manager's /run/user directory and other users' credentials must be hidden;
restrict address families to IPv4 TCP (no AF_UNIX authority sockets), disable
privilege escalation. Qualification must inspect the effective sandbox and fail closed if the
kernel/user manager cannot enforce it. The Observer projection is the only API;
the service cannot access provider/operator credentials.

The sandbox reads the existing registry directory, including sibling files, but
the application opens only the configured canonical DB and exposes an allowlisted
projection. It has no filesystem endpoint. This does not introduce a database
column-level security boundary; a future separate database principal is out of scope.

## Network and authorization scope

Backend: `127.0.0.1:8766` only. Planned frontend:
`https://workstation-p620.tail691100.ts.net:8449` (TCP/HTTPS, private Serve).
8449 is dedicated to Observer; do not alter existing Serve routes 8443-8448.

The user explicitly authorized **all joined Tailnet devices** on 2026-10-01,
superseding ADR-006's original designated-Mac/user restriction for this deployment.
An independent Observer-only bearer remains mandatory for every GET. This is not
public or unauthenticated read access. No ACL policy mutation is authorized or
needed if the existing effective policy already admits joined Tailnet clients.

Acceptance identities: Mac Air IPv4 `100.126.61.35`, IPv6
`fd7a:115c:a1e0::da31:3d24`; P620 IPv4 `100.77.218.95`, IPv6
`fd7a:115c:a1e0::8331:da60`. Both are owned by the same Tailnet user; owner-wide
rules would not distinguish the Mac. The effective P620 filter currently admits
Tailnet sources on all ports. Keep that policy unchanged. Validate normal Mac
access, wrong/missing bearer denial, loopback-only backend and absence of LAN/public
8449 listeners or Funnel. Do not claim an unauthorized Tailnet-device denial: all
joined Tailnet devices are authorized to reach this private endpoint by user choice.

After local service/schema/security qualification, the route command is:
`tailscale serve --bg --https=8449 http://127.0.0.1:8766`.
Use the normal hostname-validating TLS client, then the shipping ObserverClient
and formal Monitor app. Credential provisioning uses protected stdin/SSH into
the existing native Keychain API; never clipboard automation or shell argv.

## Audit, rotation and rollback

CLINX_OBSERVER_AUDIT_METHODS=1 enables JSON lines containing only a fixed allowlist
of method names (unknown names become OTHER) and an integer status. No path,
query, header, body, credential, task content or user agent is logged. Use journal
time/InvocationID boundaries to separate negative-method probes from the formal
Monitor observation window. Do not treat probe requests as app traffic.

Rotate only this bearer, restart only clinx-observer.service, verify old=401 and
new=200, and replace the Mac Keychain item. Environment edits alone do not rotate
the running process. Revoke by stopping this Observer and/or withdrawing only its
8449 Serve route/grant, then removing the Keychain item. Never cancel a CLINX task.

Rollback stops/disables only clinx-observer.service and removes only the 8449
Serve route (`tailscale serve --https=8449 off`). Retain the pinned release and
protected credential source for operator-controlled recovery. Do not run Serve
reset, modify other services, or delete the authority registry.

## Progress

- Source/ADR audit complete; baseline latest main verified.
- P620 user namespace sandbox probe: real task count 93; O_RDWR open denied with
  errno 30 (EROFS); CLINX runtime credential path hidden. No DB write was attempted.
- Tailnet effective filter is currently broad allow; all-Tailnet scope explicitly authorized, so no ACL change is needed. Serve 8449
  remains inactive until local service qualification.
- Remaining milestone gates are not yet claimed PASS.
- Observer/schema qualification: 22 tests passed. Full Python regression: 619
  passed, 1 skipped, 102 subtests passed, zero failures on P620 in an independent
  qualification checkout. Build the locked release Rust kernel first and retain
  Git history: compatibility tests require both prerequisites. GitHub was
  unreachable from P620, so canonical source/history were transferred from Mac
  without changing the authority checkout. Unit verification passed.
