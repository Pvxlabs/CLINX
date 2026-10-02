# Worker continuation and native visibility qualification

Evidence directory: `/data/artifacts/clinx-continuation-20261002/` (Shanghai date;
wire timestamps remain UTC). This is Core qualification, not Observer deployment.

## Confirmed causes and corrections

The five calls in `exec_caf8f4564d5f40ee9b7b39160e8bcb34` exited 0 and were ACKed
on 2026-10-01 15:35:40.174142–15:35:40.258660 UTC. The Worker wrote BLOCKED at
15:36:14.684408 UTC. Its model-visible response snapshot still said
`AWAITING_PROVIDER_ACK`; the prompt told it to stop/reconcile that snapshot.
This was not a lost ACK or a perpetually pending ledger.

The task title never reached native `name`; 17,562 characters of machine contract
and request became the preview/title. The request was preserved at offset 14,771.
`source=vscode` is not a hidden-thread flag, and `threadSource` is analytics metadata.
`has_user_event=0` alone is not corruption. `3484eb1` descends from `4d5b590`.
No historical result, rollout content, or private Codex database was rewritten.

The repair separates three facts:

- A completed Host result reaches the Worker with its exit code and
  `execution_can_continue=true`. `retry_allowed=false` applies to the original operation.
- The send-time `delivery_state=PENDING` is explicitly a `RESPONSE_SNAPSHOT`.
  Only correlated Provider `item/completed` establishes DELIVERED in the ledger.
- A subsequent call checks durable evidence. Calls in the same batch queue while
  the same live owner awaits ACK; the existing reader continues consuming events.
  The queue is bounded and uses the existing request deadline. Unknown completion,
  disconnect, failure, replacement owner, and replay remain fail closed.

Normal completion is no longer prematurely recorded as delivery failure.
A nonzero exit and a pre-dispatch rejection can both be reliably delivered.
The finalizer still respects a business BLOCKED result after successful delivery.

## Read plane and native content

All `get_status` selectors now read without reclaim/reconcile/finalize. A task read
selects its exact current execution and returns the same Host/ledger evidence as
that execution read. `provider_delivery` reports durable ACK separately from
Worker text; `worker_pending_is_stale_snapshot` exposes the observed old mismatch.
Historical thread selection remains isolated and cannot borrow current task evidence.

New and continued tasks call `thread/name/set`. The visible user content is the
real task title followed by the complete user request. The execution/result contract
uses native `developerInstructions`; the operation catalog appears once in the
dynamic tool description. Sealed policy and Host authority checks remain unchanged.

Only the preserved Observer thread
`01a0f81b-1c84-7b20-aaeb-f452be41bbae` received a metadata-only name repair:
`CLINX Observer Mac 实时连接验收`. Its original prompt and BLOCKED result remain intact.
Native name search, Desktop app inventory/read, and the actual CLI picker plus
transcript preview find that title without a UUID. The user's Desktop UI still
does not find it; the API result is not rendered visibility acceptance.
Old previews intentionally retain historical content.

## Client and event evidence

P620 CLI is 0.156.1, `CODEX_HOME=/home/pvxlabs/.codex`, cwd
`/home/pvxlabs/dev/clinx`, provider `custom`, no selected profile. Desktop's app
inventory identifies this chat and the fault thread on `remote-ssh-discovered:p620`;
it also distinguishes local Mac chats. The public daemon has this development chat
loaded, while the dedicated Provider owns managed execution threads.

Both endpoints find the fault thread with default `thread/list` and
`useStateDbOnly=true`. Filtering the old remediation cwd or provider `openai` hides it.
Native title search fails before the metadata repair and succeeds afterward.
The prior successful managed closure thread (`01a0f60f-d614-7d01-b411-5babc7ab967e`)
has the same vscode/custom source/provider and a null name with 16,207 user characters.
Older managed threads still record the remediation cwd. This native Desktop chat
uses vscode/custom too, but has a real name and 254 user characters. These comparisons
disprove source-only visibility claims; they do not prove historical rendered UI use.
Neither `source`, `threadSource`, nor `has_user_event` was changed.

The user reported "没见到。" and explicitly confirmed the search was in remote
P620's `Clinx`, not the separate local Mac `CLINX` project. The app's project
inventory maps remote project `10154534-a419-4b48-ac43-a0bf8b0989bf` to
`remote-ssh-discovered:p620` and `/home/pvxlabs/dev/clinx`; the fault thread is
returned with that same project ID. The remote project is pinned and no host is
reported unavailable. The local project instead maps to
`/Users/tinzleung/Developer/CLINX`. This rules out the local/remote project mix-up
for this observation and finds no stale remediation path in the current project
mapping. It does not identify the rendered client's filter or cache behavior.
P620's public daemon log did not expose the actual UI `thread/list` parameters.
Evidence: `desktop-project-followup.json`. Desktop visibility is
`NOT_RESTORED_USER_REPORTED`; the exact client-side cause remains UNKNOWN.

A two-daemon isolated qualification shares one temporary CODEX_HOME. Read-only
observers never call resume and never answer another owner's tools:

| Connection | Active status | Native notifications |
| --- | --- | --- |
| Owner endpoint | `active` | thread/started, thread/name/updated, thread/status/changed |
| Other endpoint, same persisted home | `notLoaded` | no execution thread notifications |

Neither observer receives per-item events. The 0.156.1 exported schema has
`thread/unsubscribe` but no standalone read-only `thread/subscribe` method.
Restoring the shared daemon or resuming an active thread merely for monitoring is
not an accepted substitute: it risks writer/response correlation and policy changes.
Actual Desktop rendered discovery failed the user's check. Rendered history and
item-stream visibility remain UNVERIFIED; native cross-daemon item streaming was
not restored.

The isolated identity test initializes one server with two distinct client names.
Both userAgents retain the first name as their prefix; only the parenthesized client
identity changes. This explains `clinx-delivery-check` on the dedicated process.
Live qualifications now always launch a disposable endpoint with temporary Codex
state; they cannot initialize or restart the shared Provider. Existing machine
config/auth is referenced without copying credentials. At a safe production switch,
the configured `linear-local-codex-bridge` must initialize the fresh dedicated process first.

## Validation

- Standard full regression: `python3 -m pytest -q`; 756 passed, 5 opt-in live tests
  skipped, 112 subtests passed on the final source.
- Deterministic fixtures exercise delayed ACKs, five batched and sequential calls,
  next legal calls, nonzero exits, pre-dispatch rejection, lost ACK deadline,
  wrong thread/turn/call/owner, replay/new call/reconnect, and immutable old BLOCKED.
- Real isolated normal task: six reads, then two reads on a second execution of
  the same native thread; both PASS with every receipt DELIVERED. User items and
  names are verified via native APIs; no ACK workaround is present in the user prompt.
- Real isolated injected failure: `HOST_EXECUTION_COUNT=1` and counter=1 after
  same-call, new-call and reconnect replay attempts; final business result BLOCKED.
- Real Host contract: intentional pre-dispatch failures followed by valid reads;
  normal sequence also exercises Git and registered service reads. A first clone
  failed DNS resolution before dispatch; its evidence is retained and the separate
  successful successor is in `host-live-v2`.

See `live-v5`, `live-v3`, `host-live-v1`, `host-live-v2`, and their top-level logs.
No isolated acceptance is labeled as a shared connector deployment PASS.

## Shared activation and remaining work

At switch preflight, `exec_d5cb9abec1b340e99a6854b88eef0fc1` has a real
`inProgress` Provider turn. Its owner PID is 1920093, the MCP child of
`clinx-tunnel.service`; the dedicated Provider is also shared with that active task.
No service was restarted and no ORION task/runtime was changed. The normal outer
connector still exposes the pre-fix read schema. Source qualification does not
prove runtime activation.

The final follow-up at 2026-10-01 16:54:07 UTC again read the dedicated Provider's
turn as `inProgress` and thread as `active`, with registry state `CODEX_RUNNING`.
There were zero currently running Host commands and no PENDING delivery rows, but
that does not make an active Worker safe to interrupt. Its owner was still PID
1920093 beneath `clinx-tunnel.service`. See `switch-preflight-followup.json`.
No service switch or formal outer-connector smoke was attempted.

After that execution is independently terminal, recheck active turns, running Host
processes and pending deliveries. From the independent native development session,
reload only the necessary CLINX services, initialize the dedicated Provider with
its configured identity, and read back process sources. Then use the ordinary outer
connector to prepare/start a short named read-only smoke and a separate continuation;
correlate receipts, ACKs, Worker results and writeback. Do not replay the Observer's
five historical calls or invent an ACK reconciliation blocker.

The actual Host contract registers only `clinx.service` and `clinx-tunnel.service`.
Observer continuation needs a narrow registration for `clinx-observer.service`,
its configured loopback/Tailnet health targets, and any explicitly required Serve
mutation operation. Existing `OUTBOUND_NETWORK` exposes only the public HTTPS target;
TAILSCALE exposes reads, not arbitrary Serve configuration. Authenticated Observer
probes need a credential-safe registered operation, never a token in prompt/argv/output.
These are follow-up changes, not authority added by this repair. Do not start an
Observer execution that is guaranteed to fail on missing targets.

Current completion boundary:
`HOLD_SHARED_ACTIVATION_AND_DESKTOP_VISIBILITY`.
`CLINX_NATIVE_VISIBILITY_AND_CONTINUATION_RESTORED` is not yet established.

## Source delivery boundary

The canonical checkout remains `/home/pvxlabs/dev/clinx` on local `main`. Remote
`origin/main` gained seven Monitor-only commits during this task. Integrating those
into the dirty canonical Monitor tree would touch the user's unrelated work, so the
Core commit is published on `codex/worker-continuation-20261002` for review; remote
main is not overwritten. All 13 pre-existing dirty files are byte-for-byte preserved.
The temporary worktree was removed. A branch push is not mainline integration or
shared runtime activation.
