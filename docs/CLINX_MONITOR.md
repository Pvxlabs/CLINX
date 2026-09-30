# CLINX Monitor — Phase 0/1 specification and observer contract

Contract: `CLINX_OBSERVER_V1` / wire `schema_version: "1"`.
Scope: native macOS Swift/SwiftUI/MenuBarExtra observer; P620 remains the only CLINX
authority. This change implements specification + a separately launched read-only
backend, and supplies Swift reference models/client. It does not ship a compiled Mac
app, deploy a listener, restart CLINX, or operate any ORION task.

## Authority audit

| Existing source | Authoritative facts / caveat | Observer decision |
| --- | --- | --- |
| `task_registry.py:TaskRegistry`, `tasks` | Durable task identity, current state/stage/blocker, timestamps, running/retry flags. Constructor initializes/migrates; regular connections allow writes. | Read the same existing SQLite DB using URI `mode=ro` + `query_only=ON`. Do not instantiate TaskRegistry in the observer. |
| `executions`, `execution_history` | Active lease row and retained execution ownership, exact turn/model/route/policy; legacy ownership may be null. | Active row first; otherwise newest retained row only if its non-null turn matches the current task turn. No task-route backfill into execution identity. |
| `m9_integration.py:ClinxIntegration.get_status` | Reclaims stale leases and can reconcile provider completion on a status read. | Never call it from observer HTTP. Read-only advertising alone is insufficient. |
| `prepared_executions` | Persisted reasoning effort for resulting execution/task; prompts and thread bindings are private. | Select only reasoning effort by exact resulting execution + resulting task. |
| `execution_results` / `completion_runtime.py` | Structured PASS/BLOCKED result, exact task/execution/turn, writeback state. | Exact join only. A previous execution's PASS never makes a new run PASS. No raw_result or raw changed file paths. |
| `host_executor.py`, `host_executions` | Persisted structured operation/result evidence; argv/cwd/stdout/stderr may contain secrets. | Explicit metadata-only allowlist, exact task/execution filter. No command/process invocation. |
| `shadow_ledger` / `v2_events` | Existing optional append-only V1 observations, ordered durable cursor; not universal history. | Select allowlisted V1 observation kinds and source_task_id. No synthetic poll events, no new ledger, no enabling shadow writes. Coverage stays PARTIAL or UNAVAILABLE. |
| `mcp_server.py`, `app_server.py` tunnel / `bridge.py` | MCP exposes read and mutation surfaces; app-server transport can carry provider traffic. Neither is a verified Monitor Tailscale identity boundary. | Separate loopback GET-only HTTP adapter. Do not proxy MCP tools or app-server/tunnel frames to Mac. |

`observer_server.py` owns serialization only. The live registry, execution semantics,
leases, completion logic, transport, host execution and ORION records are unchanged.
Unit tests instantiate TaskRegistry only in disposable fixture directories.

## API and transport

| Request | Response | Bounds |
| --- | --- | --- |
| GET /v1/health | health | Source readability, observer capability; authority process liveness UNKNOWN |
| GET /v1/tasks?state=active | task page | 50 items, optional offset 0..100000, has_more/next_offset |
| GET /v1/tasks?state=recent | task page | Same; newest updated_at then task_ref |
| GET /v1/tasks/{task_ref} | task snapshot | Opaque reference: 1..128 ASCII alphanumeric / underscore / hyphen |
| GET /v1/tasks/{task_ref}/events?after={cursor} | event page | 100 items, ascending durable cursor |
| SSE | Not implemented in Phase 1 | Mac uses polling |

All routes, including health, require one Authorization header. No other routes or
methods exist. Authenticated non-GET methods return 405 with `Allow: GET`; missing or
invalid auth returns 401, including on unknown routes. Unknown task/route 404;
malformed or duplicate query / cursor 400; cursor above current ledger high-water 409
`CURSOR_RESET_REQUIRED`; source/schema/busy failure 503. Missing/revoked server
credential returns 503 `AUTH_UNAVAILABLE`. Errors contain only schema_version/error,
never exceptions, paths, SQL, headers or source records. HTTP request bodies are
rejected (400), including chunked bodies. There is no CORS grant, filesystem/log/shell
endpoint, artifact download, hidden reasoning, raw terminal, prompt or conversation API.

Responses use JSON, no-store, nosniff, Connection: close. Request target <=2048
characters; query <=4 fields; response <=2 MiB (otherwise 503). SQLite busy timeout
250ms; VM work budget 10 million instructions per snapshot; HTTP socket timeout 5s;
16 concurrent requests and a 16-request listen queue. Text bounds: title 512,
blocker 2048, result text 4096 per field, identifiers/stage/route text 256. Polling
does not retain a DB transaction between requests. Existing SQLite WAL must be
readable by the observer OS account; do not use immutable=1 on a live WAL DB.

Active includes unresolved states (QUEUED, running, BLOCKED, FAILED, RECOVERY_REQUIRED,
unknown); Recent includes COMPLETED, IN_REVIEW, CANCELLED, STOPPED. A blocked task stays
visible in Active until authority changes it. Recent is bounded by page size, not
an invented age cutoff. Offset pagination can shift during concurrent writes;
deduplicate by task_ref and refresh first page on the next poll. It is not an
export/snapshot guarantee across multiple requests.

## Schema v1 and evidence semantics

Machine contract: [observer-v1.schema.json](monitor/observer-v1.schema.json).
Wire examples: [api-examples.json](monitor/api-examples.json), explicitly synthetic.
Swift: [ObserverModels.swift](monitor/ObserverModels.swift) and
[ObserverClient.swift](monitor/ObserverClient.swift).

| Field | Source / interpretation |
| --- | --- |
| task_ref, project, title, host | Current task record. No repository credentials, cwd, prompt or summary. |
| state, stage | Current task projection; preserve unknown future strings. |
| execution_ref, execution_state, execution_stage | Exact selected execution, null/UNKNOWN when absent or unowned. Kept separate from task projection. |
| model.logical/resolved, reasoning | Selected execution models; reasoning is effort setting only. Never infer from defaults or expose model reasoning text. |
| current_activity | Persisted stage label and last_progress_at; not terminal streaming or inferred natural-language activity. |
| blocker | Bounded/redacted persisted current_blocker, or null. Missing blocker text does not erase a BLOCKED state. |
| timestamps | ISO 8601 UTC/offset strings; invalid/naive/missing values null. observed_at is observer read time, not authority heartbeat. |
| elapsed_seconds | Exact elapsed duration from persisted acquired_at to result/release boundary, or observed_at for an active nonterminal execution; absent/invalid/reversed endpoints null. No ETA. |
| completed_at | Persisted terminal boundary observation (lease release or structured result receipt), not proof of process exit time. |
| codex_running, retry_required | Persisted task flags. Neither proves live process liveness nor grants a retry operation. |
| mutation_boundary | Observer read_only true, allowed_actions empty. Selected execution's recorded classes/surface are display-only; UNKNOWN if absent. |
| routing | Only host/surface/provider/transport stable identifier and status from the execution route. No conversation, binding, machine_id, authority scopes or worktree paths. |
| phases[] | Empty in this backend because no canonical persisted unit denominator is available. Future entries require phase_ref/title/state/evidence_ref and optional completed_units/total_units/unit. |
| progress_percent | Always null in Phase 1. Future non-null values require persisted completed units and a positive finite total for the same scope, units and execution, 0 <= completed <= total. Compute 100*completed/total; never wall-clock, model opinion, phase count, tool-call count or weighted guesses. Unknown/inconsistent denominator => null. |
| recent_events | Newest 20 persisted allowlisted observations (descending), has_more + next_cursor + coverage. |
| host_operations | Newest 20 exact-execution records; opaque ref, host, surface, class, capability, operation name, start/end/duration/exit/state/timeout. No argv, cwd, target command, output, output digest, executor instance or cancellation capability. |
| final_result | Null unless task + execution + non-null turn match. Structured status/summary/validation/blockers/next_state/received_at, truncation/redaction flags. changed_files null; raw_result omitted. A result is producer-reported evidence, not new acceptance by Monitor. |
| artifacts[] | Empty / NO_SAFE_ARTIFACT_REGISTRY. Never scan paths from free text. Reserved metadata: opaque ref, safe basename, media_type, size, sha256, created_at, owning execution_ref, OBSERVER_SAFE classification. No filesystem path/URL/open action. |
| menu_state | Derived display only, never persisted back. See mapping below. |

No schema field is an authority override. A future optional additive field may be
ignored by Mac; changes to field meaning/types require a new API version. Unknown
strings must be displayed as uncertainty. The checked-in JSON Schema uses strict
objects to detect accidental data leakage in producer tests; consumer decoding
ignores unknown additive keys.

Strings are plain text, never rendered as HTML/Markdown links or commands. Common
bearer/key/password assignment patterns are redacted; the configured observer
credential is scrubbed from serialized HTTP bodies. This is not a universal DLP
engine: task titles and structured result prose can contain sensitive user text.
Tailnet ACL + credential authorize read access to all visible registry tasks.
Per-project/per-user authorization and arbitrary artifact serving are out of scope.

## Event cursor and retention

Events come solely from existing v2_events rows representing V1 task/execution/host
facts. Returned fields are event_ref, kind, source execution reference and persisted
occurred/recorded timestamps; raw payloads and actor data are omitted. Recognized kinds
are explicitly enumerated in the backend. Corrupt JSON and unknown future event kinds
are omitted. Baseline imports are observations, not reconstructed historic activity.

The cursor is an opaque, task-bound token containing a task hash prefix and ledger
position. It is not an authorization token. Clients must not compare or increment it.
Start without after, consume has_more pages, then persist next_cursor in memory per
task and endpoint. A detail snapshot's next_cursor resumes after the snapshot's
high-water. On 409 discard that task cursor and reload snapshot/history. A missing
ledger returns empty UNAVAILABLE, never fabricated events. A present optional ledger
returns PARTIAL even if the result is empty. No promise of exhaustive history.

The observer performs no retention, deletion or migrations. DB replacement/reset
requires clients to discard cursors (reconnect to a newly provisioned endpoint);
the current cursor detects high-water rollback but cannot detect replacement by a
different DB with a greater cursor. This is an explicit Phase 1 limitation. Durable
notification deduplication keys include endpoint + task_ref + execution_ref +
event_ref/result.received_at, not arrival time.

## Network and credentials

Chosen deployment topology (not activated by this change):

Mac URLSession -> HTTPS on private Tailscale Serve endpoint -> 127.0.0.1:8766 observer
-> canonical P620 SQLite registry opened read-only.

Do not bind 0.0.0.0, a public interface or an unauthenticated raw TCP port. Listener
host is hard-coded loopback. Do not use Tailscale Funnel. Tailnet ACL/grants should
allow only the designated Mac/user to this Serve HTTPS endpoint; HTTPS certificate
validation remains enabled, no ATS exception and no custom trust bypass. Disable
request/header/body logging on any reverse proxy. Validate tailnet exposure and
unauthorized-device denial before enabling a production endpoint.

Tailscale identity would be preferred if the server could verify the peer via a
trusted local tailscaled identity channel. The current stdlib HTTP/MCP/tunnel stack
has no such verified contract; forwarded identity headers are not trusted.
Therefore bearer authentication is mandatory even behind Serve. Never interpret
Tailscale-User/Login/X-Forwarded-* as authorization.

Provision a new random >=32-byte credential encoded URL-safe, stored on P620 only in
an observer-specific protected environment source (0600). Required env:
CLINX_OBSERVER_TOKEN, CLINX_OBSERVER_DB (existing canonical DB), optional
CLINX_OBSERVER_PORT (default 8766). Do not pass credentials on argv, query strings,
MCP payloads, source control, issue comments, screenshots or test logs. Do not reuse
the operator or provider credential. Launch `python3 observer_server.py` as a
separate low-privilege service with read access to the registry, no authority write
permission, no provider credentials and no CLINX operator tooling. It must not
replace/restart the existing CLINX service. OS read-only mounts/permissions are the
deployment gate; application mode=ro/query_only provide defense in depth.

Mac stores the credential as a non-synchronizing generic Keychain item, service
com.pvxlabs.clinx.monitor.observer, account p620-observer, WhenUnlockedThisDeviceOnly.
Use SecureField for entry, clear local state after save; no UserDefaults, clipboard
automation, telemetry or diagnostic dump. Reference client refuses redirects and
allows HTTPS base URLs only; requests are GET and bounded. A read-only app may edit
its own endpoint/credential/settings, never task/execution state.

Rotate: provision new credential securely on P620 and Mac; replace protected server
environment; restart only the observer process; verify old credential 401 and new
credential 200. No overlap/secondary credential in Phase 1. Process env does not
reload magically. Revoke immediately: stop only observer/withdraw its Serve route
or tailnet grant, remove/replace its credential source, then remove Mac Keychain
item. Existing in-flight responses may already have been delivered. None of these
steps invoke task Cancel or provider reconciliation. Rotation/revocation over a real
tailnet remain a deployment acceptance item.

## Native macOS contract and view hierarchy

Target macOS 13+ (MenuBarExtra), SwiftUI, Foundation URLSession async/await, Security
Keychain, UserNotifications; optional ServiceManagement.SMAppService Launch at Login.
No WebView and no MCP client. Swift models/client are contract references pending
macOS compile and integration verification.

```text
MonitorApp
  MenuBarExtra (symbol + localized accessibility state, .window style)
    MonitorPopover
      ConnectionHeader (endpoint display, last successful fetch, stale age)
      ActiveRecentPicker
      TaskList
        TaskRow (project/title, state, host, model, real progress or indeterminate)
      ObserverFooter (health / settings / quit)
    TaskDetail
      IdentityAndRouting
      StateAndActivity (persisted timestamp, effort setting)
      Phases (unknown/absent empty state; no invented checklist)
      EventTimeline (coverage badge + bounded load-more)
      HostOperations (metadata only, no command/log expansion)
      BlockerPanel
      FinalResultPanel (producer status, validation, truncation/redaction badges)
      SafeArtifactMetadata (empty until authority supports it)
      HealthAndFreshness
  Settings
    HTTPS endpoint, Keychain credential, notifications, appearance, launch at login
```

All rows/detail text support selection and accessibility; full task_ref may be
copied, but no file/command/result auto-open. No Start/Cancel/Retry/Approve/Deploy
button, keyboard shortcut, context menu, notification action, URL scheme handler or
hidden command. retry_required is descriptive text.

| Condition, evaluated in order per task | Menu state / meaning |
| --- | --- |
| Client has no accepted response, transport/auth/schema failure, or stale beyond threshold | DISCONNECTED overlay; retain last snapshot labelled stale, never retain a green success as current |
| state BLOCKED/FAILED/RECOVERY_REQUIRED/TRANSPORT_UNCERTAIN, retry_required, or exact BLOCKED result | BLOCKED |
| Running state set or persisted codex_running true | RUNNING |
| COMPLETED/IN_REVIEW plus exact PASS result | PASS (task result only; not deployment or real trading acceptance) |
| QUEUED/STOPPED/CANCELLED, or COMPLETED/IN_REVIEW without exact result | IDLE with textual state; cancellation is not success |
| Any unknown future state | BLOCKED with Unknown state explanation |

Aggregate priority: DISCONNECTED > BLOCKED > RUNNING > PASS > IDLE. Evaluate Active
first; if Active is empty, show newest Recent result; don't let an ancient blocked
task in Recent override a current run. With no tasks and a fresh health response,
show IDLE. Baseline load never generates PASS/BLOCKED notifications. Future state
strings remain visible; unknown schema_version stops update and shows DISCONNECTED.

Use an @MainActor observable store to publish immutable snapshots; keep network and
decode work off the main actor. One polling task and at most one in-flight request;
cancel prior loop on endpoint change, sleep, window lifecycle change or app termination.
Default Active refresh 2s while unresolved work exists; idle refresh 15s. Refresh
detail/events only when shown; do not drain unseen history every poll. Periodic health
request shares the same scheduler. Timer tolerance/jitter ~10%; no animation/timer
per row. Refresh after wake or network-path recovery, reset ephemeral cursors on
endpoint changes. Deduplicate page items and events. No raw task data disk cache.

Freshness: display server observed_at and local lastSuccessfulFetch; compute connection
age using monotonic client time so clock skew does not fabricate disconnection.
Show stale if no success for max(3*current interval, 10s): active 10s / idle 45s.
Task last_progress_at is a separate timestamp and may legitimately remain old.
On a failed request mark DISCONNECTED immediately; retry 2/4/8/15/30/60s with jitter,
max 60s. Stop rapid retry on 401 until credential changes; app wake/manual local
refresh may probe once. Use low-power 15s minimum and pause while system sleeps.
Keep last error category only; never log headers/body/credential. SSE is a future
transport option, not an acceptance claim.

Notifications: opt-in permission; generic privacy-safe title/body by default; emit
once per newly observed exact-execution BLOCKED/PASS transition, and once after a
connection outage remains beyond stale threshold. Reconnected notification at most
once per outage. Persist only deduplication keys if needed, never payloads or secret.
Avoid replay storms after launch, pagination, wake, auth rotation, endpoint changes.
Notification click opens read-only detail; no custom mutation actions.

Use semantic colors and SF Symbols with text labels in light/dark/high contrast;
honor reduced motion, Dynamic Type and VoiceOver. Color is not sole status encoding.
Optional Launch at Login must be user-selected, use SMAppService and reflect its
actual authorization state; never alter any P620 service.

## Acceptance matrix

| Gate | Evidence / current boundary |
| --- | --- |
| Phase 0 canonical contract and authority audit | This spec, ADR-006, schema, examples, Swift references and handoff |
| Four GET routes / auth / non-GET denial | test_observer_server.py API + actual loopback HTTP fixture tests |
| No authority mutation or reconciliation | mode=ro/query_only write rejection, unchanged DB bytes/logical dump; observer imports no live dispatcher/TaskRegistry |
| Exact task/execution/turn isolation | Old execution PASS and mismatched turn excluded, future state never PASS |
| Unknown/progress/units | No denominator => null, empty phases; missing artifacts => explicit empty status |
| Safe serialization / bounds | Allowlist tests include secrets in argv/stdout/stderr/bindings/paths, text bounds and JSON Schema examples |
| Events / cursor | Stable ordering, task scope, bounded pages, missing ledger UNAVAILABLE, partial history, reset 409 |
| Canonical regression | Existing repository unit suites; exact command/outcome recorded in FINAL_REPORT |
| Mac JSON decoding / Swift compilation | NOT_RUN on P620: Swift/macOS SDK unavailable |
| Native MenuBarExtra UI / light/dark / notifications / Keychain / energy | NOT_RUN; Phase 2 Mac implementation gate, not implied by reference files |
| Real Tailscale ACL, TLS, bearer rotate/revoke, external port audit | NOT_RUN; no listener deployment authorized by this implementation |
| Running ORION task non-interference | No live registry/provider/status/reconciliation calls; no live CLINX restart or deployment |
| Commit/push/canonical main clean 0/0 | Readback evidence in FINAL_REPORT |
| Requested /data/artifacts export | Requires registered target; current execution returned TARGET_NOT_REGISTERED |

## Verification and handoff

Run `python3 -m unittest -q test_observer_server` for dependency-free backend tests.
Run `python3 -m unittest -q test_observer_schema` when jsonschema is installed for
Draft 2020-12 schema + synthetic examples validation. Full regression uses
`python3 -m unittest discover -q`. Tests create disposable local fixture registries
and a random loopback port; they do not read the configured live registry.

Repository reports:
[FINAL_REPORT.md](monitor/FINAL_REPORT.md),
[CLAUDE_CODE_HANDOFF.md](monitor/CLAUDE_CODE_HANDOFF.md).
Requested export location is /data/artifacts/clinx-monitor-spec-20261001/ with those
same filenames; export failure is explicit and must not be recorded as PASS.
