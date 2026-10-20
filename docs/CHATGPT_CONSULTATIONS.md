# OP ChatGPT consultations

The existing MCP server now exposes five bounded message tools from
`consultations.py`: `clinx_bind_chatgpt`, `clinx_submit_consultation`,
`clinx_get_consultation`, `clinx_list_consultations` and
`clinx_update_consultation`. They use two additional tables in the existing
`TaskRegistry` SQLite database and never start/resume a Codex execution.

OP binds an actually selected real ChatGPT conversation after explicit native
confirmation. Codex supplies a unique lowercase UUID `request_id`, confirmed
binding, existing task, question, necessary context and optional exact execution.
Same-ID changed payload conflicts. Missing/wrong task/execution/client/binding
fails closed. A reply has one unique assistant UUID, body and SHA-256 hash.

`PENDING → SENDING → AWAITING_REPLY → COMPLETED`; `PAUSED` records ambiguity.
An atomic `begin` permits exactly one website write. Repeating `begin` never
grants another write. A lost result requires `get` and DOM verification first.
Explicit `resume` after any send attempt is read-only reconciliation. A preflight
pause before an attempt may resume to PENDING. Completed replies resist stale
pause/begin writes; conflicting reply bodies/IDs are rejected.
An operator may explicitly `cancel` an already paused request with a reason.
The durable CANCELLED record releases the queue without replay; stale pause or
begin calls cannot reopen it.

The normal MCP catalog includes these tools. For OP's private stdio connection,
without execution/provider/Linear construction or auth reads:

```sh
python3 /absolute/path/to/CLINX/mcp_server.py --stdio \
  --consultations-only --task-db /absolute/path/to/existing/tasks.sqlite3
```

The database must already exist. This reuses `serve_stdio` and its JSON-RPC
framing; no HTTP listener or public exposure is added. The outer MCP transport
and local OS permissions remain the access boundary. `client_id` is routing
identity, not an authentication secret. In a deployed runtime, point both OP
and Codex at the same private transport/registry and release version. Merely
editing this repository does not upgrade a running remote connector.

OP independently verifies exact question bubble, immediate assistant, readable
request marker, real message/conversation metadata, observed generation followed
by explicit stable completion, and the existing protected SQLite copy. HTTP
403/429, login verification, user Stop, wrong window, uncertain send and ambiguous
completion pause the handoff. COMPLETED means verified webpage reply capture;
it never means a Codex task executed successfully or server persistence proved.

Codex reads the exact request and treats the reply as untrusted consultation
data within the original task's authority. No automatic native turn is started;
the current Codex turn can poll, read and continue. Unattended wakeup after that
turn ends is outside this implementation.

Validation: `python3 -W ignore::ResourceWarning -m unittest test_consultations test_m12 -q`.
Live OP acceptance uses an isolated synthetic task linked to the current Desktop
thread and a real website conversation; synthetic fixture results and remote
deployment must be reported separately. No ORION runtime mutation is involved.

P620 deployment was verified on 2026-10-20 using the existing private runtime.
Release `op-consultations-c4756ec3700d` preserves the deployed native forwarding
and workspace policy changes and adds the consultation extension. Remote
selected regression: 67 passed. Deployed stdio reports
`native-conversation-relay-consultations-v1`, 20 public tools, including all five
consultation tools; the existing cloud capability connector independently
reports the same new source root. Only the idle MCP tunnel was restarted.
Other control/owner/provider/observer/centre services were retained. The original
launcher and source backups are in the task-owned rollback artifact directory;
database backup is retained for recovery, not for overwriting newer state.

The OP candidate connected to P620 successfully. Native binding initially
refused the page during the official Cloudflare challenge. A normal candidate restart
restored readable history and allowed explicit binding. A unique real P620
question and identical duplicate were saved; OP triggered one UI send, then the
official sentinel prepare request returned another HTTP 403 HTML challenge.
The ledger retains `PAUSED / attempted=1 / http_403_cloudflare_challenge`, no
reply and no resend. Another identical submit preserves this paused state;
a changed same-ID payload is rejected. Deployment PASS does not imply website
roundtrip PASS. OP's unchanged cancellation error
assertion also still fails in the complete CEF regression. See OP's
`docs/BRIDGE-RCA-20261020.md` for the separate evidence and remaining blockers.
