# ADR-006: CLINX Monitor reads canonical persisted facts through a separate observer

Status: Accepted for Phase 0/1 implementation; deployment and Mac acceptance pending.
Date: 2026-09-30. Related: ADR-001/002/003, docs/CLINX_MONITOR.md.

## Context

P620 is the only CLINX authority. A native Mac MenuBarExtra needs small, stable,
read-only snapshots over Tailscale. Existing TaskRegistry construction can migrate
the DB; ClinxIntegration.get_status can reclaim leases/reconcile an exact provider
turn. Host evidence and structured results contain fields unsafe for a UI transport.
The optional shadow ledger is not complete historical coverage.

## Decision

Implement observer_server.py as a separate serialization adapter over the same SQLite
registry, using mode=ro, query_only and one bounded transaction per request. Do not
call TaskRegistry initialization, MCP, dispatcher, provider transport, reconciliation
or host operations. There is no new authority table, event writer, state store or
migration. Existing execution semantics remain untouched.

Expose exactly four authenticated GET route shapes, loopback only; use private
Tailscale Serve HTTPS and tailnet ACL at deployment. No verified Tailscale peer
identity adapter currently exists in the stack, so require an independent observer
bearer credential. Never trust client/forwarded identity headers. Mac uses Keychain.
Do not use Funnel or expose the listener publicly.

Use exact execution/task/turn attribution for results and execution-owned metadata;
unknown remains null/UNKNOWN. Serialize only a positive allowlist. Progress is null
until a persisted real denominator exists. Empty phases/artifacts explicitly mean
no canonical evidence source. Existing optional V1 event observations may be read
with PARTIAL/UNAVAILABLE coverage; never synthesize missing history.

Native Mac uses URLSession async/await polling (2s active / 15s idle), state/freshness
separation, bounded responses, no redirects, and no mutation capability.

## Consequences and alternatives

Direct MCP get_status reuse is rejected because it can mutate authority despite its
status-oriented name. Adding observer handlers to the running bridge is rejected
because it couples deployment to active work. Copying the registry or maintaining a
Mac state machine as authority is rejected because results can drift. Reading logs
or terminal content is rejected because it is not canonical structured evidence.

A separate read-only SQL adapter depends on the existing table schema. Future
authority migrations must run observer contract tests; source incompatibility fails
closed with 503. Read snapshots do not certify process liveness. Event history may
be partial; global cursor reset cannot fully detect database replacement. Offset
pagination is eventually refreshed, not a multi-request snapshot. Free text cannot
provide universal secret detection and is restricted to authenticated viewers.

OS-level DB write denial, TLS/ACL checks, bearer lifecycle drills, Mac compilation,
UI/energy testing and notifications are separate acceptance gates. This ADR grants
no permission to restart CLINX, operate ORION, expose a service, or perform task
Start/Cancel/Retry/Approve/Deploy.
