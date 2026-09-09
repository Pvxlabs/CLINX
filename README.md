# linear-local-codex-bridge Dispatcher V1 / M6

A deliberately thin local actuator for this workflow:

```text
ChatGPT
   ↕
Linear
   ↓  Todo + local-codex
linear-local-codex-bridge
   ↓  local P620 app-server proxy (SSH remains an explicit remote option)
Disposable durable Codex thread
   ↕  Linear MCP
Linear
   ↕
ChatGPT review
```

## M6 design

The bridge is **not an agent** and does not interpret issue content.

It only:

1. Polls Linear for `Todo` issues carrying `local-codex`.
2. Resolves a configured project identity: cwd, repository origin, and branch.
3. Moves the issue to `In Progress` as the visible Linear claim.
4. Resolves a project through a bounded workspace registry.
5. Resolves a ChatGPT-selected task action into a durable task record and one
   canonical conversation binding.
6. Reads back the exact thread and verifies thread/session/project/repository
   identity, direct-input capability, and compatible app-server version.
7. Sends `turn/start` to that exact thread through the P620 app-server transport.

M6 keeps the M5 and small M2 completion-marker paths for compatibility. The
dispatcher itself remains asynchronous after `turn/start`. The old `codex exec`
helper is retained only as a marked legacy compatibility path and is not the
V1 default. `EXECUTION_MODE=fast` is accepted and persisted as a task choice;
M6 does not claim an app-server-native fast capability that has not been
verified.

The bridge never marks an issue `Done`.

## M11 ChatGPT integration and execution handoff surface

CLINX exposes the M11 context plane through the dependency-free stdio adapter
in `mcp_server.py`:

```bash
python3 mcp_server.py --config bridge.toml --stdio
```

The default adapter exposes seven bounded read-only tools plus explicit start
and cancellation actions:
`clinx_find_task`, `clinx_get_context`, `clinx_get_topic_status`,
`clinx_get_status`, `clinx_list_projects`, `clinx_get_capabilities`, and
`clinx_prepare_execution`.  They reuse the task registry,
ConversationBinding, and the M8 bounded context reader.  Archived and
historically adopted tasks remain discoverable through the human query path.

`clinx_start_execution` requires
the exact boolean `approved=true`, an opaque prepared execution reference, and
the persisted integrity checks for the selected logical and executable
provider models.  Recoverable dispatch failures remain `RECOVERY_REQUIRED`;
identity guard failures remain `BLOCKED`.

`clinx_cancel_execution` accepts only an opaque CLINX `execution_ref` and
interrupts the exact active CLINX-managed turn. It never accepts a PID, shell,
thread, turn, or cwd value; cancellation is durable and idempotent.

Project topic status is read with an exact project identity and bounded Codex
history.  It combines matching registered tasks with unadopted conversations,
excludes conversations already bound to a task, and never starts a thread or
turn:

```bash
python3 bridge.py --config bridge.toml tasks topic \
  --host P620 --project ORION --topic "DATA NODE"
```

`clinx_get_capabilities` describes the current boundary.  `clinx_prepare_execution`
is a read-only preparation step: it requires the exact boolean
`approved=true`, resolves a new or existing task through CLINX, validates the
configured project identity, selects model/reasoning/execution mode, and
returns a parser-compatible Linear handoff.  It does not create a Task, write
Linear, connect to Codex, or call `turn/start`.

The default catalog does not expose `clinx_execute`.  That legacy execution
adapter is retained only as an internal/experimental break-glass compatibility
path and is available only when a local process is explicitly started with
`--allow-execute`; it is never the normal command-plane path.
The normal M11 flow is:

```text
ChatGPT human intent
  -> CLINX task discovery / context
  -> explicit approved=true
  -> clinx_prepare_execution
  -> clinx_start_execution owns the command and Codex dispatch
  -> Linear mirrors the CLINX Task once and records audit comments/status
```

CLINX is the authoritative context, command, and execution-history plane;
Linear is the human-readable audit and notification projection, and Codex is
the provider execution plane. Human-facing calls require no task UUID,
thread ID, session ID, turn ID, cwd, or repository-origin value.  Those remain
inside CLINX's registry and dispatcher.  The read-only context MCP never starts
a Codex thread or turn.

This repository does not open an HTTP listener or publish an unauthenticated
endpoint.  ChatGPT cannot connect directly to a private local stdio process;
remote discovery requires an authenticated, TLS-terminated, officially
supported MCP boundary supplied by the deployment environment.  The preferred
topology is a loopback-bound CLINX stdio process wrapped by the official Secure
MCP Tunnel.  The local adapter alone cannot make the service remotely
discoverable, and tunnel provisioning plus ChatGPT custom-App registration
remain operator/deployment actions.

The M9 planes are intentionally separate:

```text
CLINX    = context + command + authoritative execution/history plane
Linear   = audit + human notification projection only
Codex    = execution plane
```

Reference research informs the boundary but does not replace CLINX ownership:

```text
codex-from-chatgpt  -> remote MCP transport and tunnel topology
codex-mcp-bridge    -> Desktop/session and writer-ownership patterns
OpenAI Codex        -> app-server protocol authority
CLINX               -> task lifecycle and authoritative context
```

Desktop-created versus CLINX-created thread restorability, native relay, and
Desktop writer ownership remain a separate qualification.  They do not block
the M9 read-only context plane and no Desktop private state is modified here.

The public surface intentionally accepts task and project references rather
than Codex thread, session, turn, cwd, repository-origin, or credential fields.
Those identities remain inside the CLINX registry and dispatcher.

## M13-A host and execution-surface semantics

M13-A makes execution location and routing explicit without adding an execution
capability. The canonical model is:

```text
Execution
  -> Host
  -> Execution Surface
  -> Provider
  -> Transport
  -> Workspace / Worktree
```

The layers have different meanings:

- **Host** is a normalized computation-environment identity. Its stable
  identifier, display name, machine ID, and alias are separate fields. A
  hostname or IP address is identity data only; it is never an authority, an
  SSH target, a permission, or a worktree.
- **Execution Surface** is the bounded interface exposed by the host, currently
  the Codex app-server turn surface. It is a capability boundary, not a
  physical machine and not a generic executor.
- **Provider** is the backend that performs the bounded task, currently the
  Codex app-server provider. **Transport** is only the communication path to
  that provider, currently local stdio or explicitly configured SSH stdio.
  Transport failure is not proof that a turn reached a terminal state.
- **Workspace / Worktree** binds the execution to the configured workspace,
  project, repository identity, branch, and deterministic worktree lease.
- **Conversation** identifies the exact verified provider conversation. Its
  private thread/session/turn values remain internal; public projections expose
  only bound/unbound status.
- **Authority** is explicit and bounded (`clinx_task_bounded` with bounded
  scopes). Host, surface, provider, transport, capability, identity, and
  network permission never become authority by implication.
- **Network Policy** defaults to `network_access=false`. `true` is an explicit
  provider-turn network opt-in; it does not mean remote execution and does not
  change the host or transport.

Every Task, prepared execution, and opaque Execution persists the canonical
routing identity. The execution row is the authoritative routing claim for
cancel, status reconciliation, and continuation. Continuation therefore
reuses the creation-time host, surface, provider, transport, workspace, and
authority instead of selecting the current default route. Cancellation and
reconciliation use the same exact route; if it is missing, unknown, or
non-executable they fail closed and preserve the active lease for recovery.

Pre-M13 rows are normalized deterministically and bounded. Legacy tasks and
prepared executions receive an explicit incomplete/unknown route where the old
data cannot prove a value. Historical execution rows are not backfilled from a
task route or the current machine, because doing so would rewrite historical
identity. A legacy null-reference lease may be released only after bounded
terminal provider evidence; an unavailable route remains recovery required. No
normalization silently guesses a remote target or falls back to the current
transport.

The public MCP catalog remains nine tools and still excludes `clinx_execute`.
Task, context, topic, preparation, status, and execution outputs may expose a
redacted routing projection containing stable semantic identities, separation
flags, and network mode, but never credentials, raw shell capability, cwd,
repository origin, or provider thread/session/turn identifiers. Linear remains
an audit projection and is not a command plane.

## Requirements

- macOS or Linux
- Python 3.11+
- Git
- Codex app-server/proxy transport available through the configured local or SSH boundary
- Linear MCP already configured in Codex
- A Linear personal API key for the bridge's narrow control-plane polling

No Python packages are required.

## One-time setup

### 1. Confirm the existing pilot repo

The current test expects:

```bash
test -d /tmp/linear-codex-pilot/.git
```

If it was removed, recreate it:

```bash
rm -rf /tmp/linear-codex-pilot
mkdir -p /tmp/linear-codex-pilot
cd /tmp/linear-codex-pilot
git init -b main
printf '# Pilot Repository\n' > README.md
git add README.md
git commit -m "chore: initialize Linear Codex pilot"
```

### 2. Confirm Linear MCP in Codex

```bash
codex mcp list
```

The output must contain `linear`.

If not:

```bash
codex mcp add linear --url https://mcp.linear.app/mcp
codex mcp login linear
```

### 3. Create a Linear personal API key

In Linear, open **Settings → Security & access** and create a personal API key.

Do not paste the key into Linear, Git, this README, or `bridge.toml`.

Export it only into the bridge process:

```bash
export LINEAR_API_KEY='lin_api_...'
```

### 4. Run doctor

From this directory:

```bash
python3 bridge.py --config bridge.toml doctor
```

Expected final line:

```text
DOCTOR = PASS
```

The doctor should also report `PVX-1508` as currently eligible.

## Human task handoff

The human-facing request contains only host, project, task intent, and optional
urgency. ChatGPT decides whether the request creates, continues, or reopens a
task, selects the model and reasoning effort, drafts the execution prompt, and
waits for the user's execution confirmation.

After confirmation, CLINX starts the prepared command with the canonical M12
machine handoff; the first real Task mirror creates one Linear audit issue:

```text
HOST=P620
PROJECT=pilot
TASK_ACTION=create
MODEL=gpt-5.6-luna
REASONING=high
EXECUTION_MODE=normal
TASK_TITLE=Long-running disposable pilot task
TASK_SUMMARY_UPDATE=Current short task summary
```

The mirror issue is reused for every continuation, reopen, recovery, and
execution. CLINX writes structured lifecycle comments, moves it to In Progress
only after a real `TURN_STARTED`, and moves successful completion to In Review;
Codex never marks it Done.

For continuation or explicit historical recovery, CLINX receives the hidden
task reference resolved by ChatGPT from the Task Registry:

```text
HOST=P620
PROJECT=pilot
TASK_ACTION=continue
TASK_REF=task_<opaque>
MODEL=gpt-5.6-luna
REASONING=high
EXECUTION_MODE=normal
```

Use `TASK_ACTION=reopen` with the hidden `TASK_REF` to reactivate a completed or
archived task and dispatch on its existing exact conversation binding. A
continuation never searches Codex threads and never falls back to a project
`current` thread, cwd match, latest thread, or new thread.

`TASK_REF` is machine identity, not a value the user supplies or remembers.
Thread IDs, session IDs, cwd, and repository identity remain internal audit
data and are omitted from the normal task discovery surface and M6 execution
comment.

## Task discovery

The read-only task query surface does not connect to Codex or dispatch work:

```bash
python3 bridge.py --config bridge.toml tasks list --host P620 --project pilot
python3 bridge.py --config bridge.toml tasks find --host P620 --project pilot \
  --query "disposable pilot"
python3 bridge.py --config bridge.toml tasks show task_<opaque>
```

The JSON response includes human metadata, status, hidden `task_ref`, whether a
binding exists, the latest readable Linear execution reference, and the stable
Linear task-index issue. It excludes thread ID, session ID, cwd, origin, and
branch. Archived tasks are excluded from normal list/find operations unless
`--include-archived` or `--status ARCHIVED` is explicit.

Each task has at most one task-specific Linear index issue. That durable index
record is separate from the per-run execution issues:

```text
Task index issue != execution issue

Task
  -> execution issue 1
  -> execution issue 2
  -> execution issue 3
```

The index mirrors only `TASK_REF`, task key, host, project, title, summary,
status, execution mode, binding presence, latest execution reference, and
timestamps. It never mirrors conversation IDs, full transcripts, or
credentials.

## Existing conversation adoption

An already verified durable conversation can be migrated into the task registry
without starting a thread or sending a turn. This is an operator/debug command;
the exact thread ID is intentionally not part of the human-facing Linear task
contract:

```bash
python3 bridge.py --config bridge.toml adopt-thread \
  --read-only \
  --project pilot \
  --thread-id <verified-thread-id> \
  --title "Existing Conversation Adoption Qualification" \
  --summary "Identity-only migration into the CLINX Task Registry."
```

The command performs `initialize` and exact `thread/read`, validates durable
status, direct-input capability, project cwd, repository origin, branch, and
app-server compatibility, then atomically creates an ACTIVE task and its one
conversation binding. If the exact thread is unloaded, it may call
`thread/resume` only to establish direct-input readiness; it never calls
`thread/start` or `turn/start`. `--sync-index` additionally mirrors the task
into Linear using `LINEAR_API_KEY` from the process environment.

## Compatibility contract

The M5 handoff remains supported:

```text
HOST=p620
PROJECT=pilot
PROJECT_MODE=existing
TASK_MODE=new
MODEL=gpt-5.6-luna
REASONING=high
EXECUTION_MODE=normal
```

Use `TASK_MODE=continue` with the exact persisted `TASK_ID` to reuse the same
conversation binding. `TASK_ACTION=complete`, `TASK_ACTION=reopen`, and
`TASK_ACTION=archive` change task state without selecting another thread.

The earlier M3 contract remains supported:

```text
PROJECT=pilot
THREAD_MODE=existing
THREAD_ALIAS=current
MODEL=gpt-5.6-luna
REASONING=high
```

For a disposable new durable thread, use `THREAD_MODE=new` and omit
`THREAD_ALIAS`. The legacy `TARGET_ALIAS=pilot` contract remains supported for
compatibility. Project configuration, never issue text, supplies cwd, origin,
and branch.

Do **not** open Codex interactively and do **not** type an issue prompt.

The durable task registry is SQLite at
`~/.local/state/clinx/tasks.sqlite3` by default (or `[runtime].task_db_path`).
It stores task records, the canonical task-to-thread binding, active execution
leases, Linear execution references, and task-index mappings. Existing M5
registries are migrated incrementally without clearing task, binding, or
execution history.

After verifying that the selected project is disposable and the remote
app-server command is available, run exactly:

```bash
python3 bridge.py --config bridge.toml once
```

Expected lifecycle:

```text
PVX-1508 Todo
    ↓
bridge claims
    ↓
PVX-1508 In Progress
    ↓
workspace/project resolver + durable task registry + task index
    ↓
thread/start or exact binding + thread/read + identity guard
    ↓
turn/start on exact disposable thread
```

Then return to ChatGPT and say:

```text
审核 PVX-1508
```

No Codex terminal output needs to be copied into ChatGPT.

## Run continuously after the pilot

```bash
python3 bridge.py --config bridge.toml run
```

Stop with `Ctrl-C`.

The checked-in units under `systemd/` keep the P620 bridge and tunnel-client
lifecycle aligned. Install both units into the user systemd directory, then
restart `clinx.service`; the tunnel child is replaced with the same restart
and resolves the current `bin/clinx-context-mcp` launcher. The tunnel profile
and credentials remain outside this repository.

## Failure behavior

If the app-server cannot initialize, identity validation fails, or `turn/start`
returns an error, the bridge writes a failure comment when possible and leaves
the issue in `In Progress`.

This prevents runaway automatic retries.

After the root cause is corrected, ChatGPT or the operator can move the issue back to `Todo` to explicitly retry.

## Security boundary

- Bridge: Linear control-plane polling + state change + local process launch only.
- Codex app-server: receives the exact durable thread and turn request.
- Linear MCP: task context and execution evidence.
- Linear personal API key: stored only in bridge process environment.
- The legacy local exec settings are not used by Dispatcher V1.
- The bridge does not push, merge, deploy, or grant `Done`.

## Why polling in M0?

This is the fastest localhost pilot. Linear recommends webhooks for long-running realtime integrations and requires webhook consumers to be publicly reachable HTTPS endpoints. Once the local protocol is proven, the P620 version can switch the trigger layer to a webhook/tunnel without changing the Codex execution contract.
