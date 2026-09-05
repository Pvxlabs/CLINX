# linear-local-codex-bridge Dispatcher V1 / M0

A deliberately thin local actuator for this workflow:

```text
ChatGPT
   ↕
Linear
   ↓  Todo + local-codex
linear-local-codex-bridge
   ↓  SSH p620 → Codex app-server
Disposable durable Codex thread
   ↕  Linear MCP
Linear
   ↕
ChatGPT review
```

## M0 design

The bridge is **not an agent** and does not interpret issue content.

It only:

1. Polls Linear for `Todo` issues carrying `local-codex`.
2. Maps the Linear Project to a configured local Git repository.
3. Moves the issue to `In Progress` as the visible single-worker lease.
4. Resolves the explicit target alias and reads its exact durable thread.
5. Verifies thread/session/project/repository identity and direct-input capability.
6. Sends `turn/start` to that exact thread through the P620 app-server transport.

M0 stops after `turn/start`. It does not wait for completion or write final
results back to Linear. The old `codex exec` helper is retained only as a
marked legacy compatibility path and is not the V1 default.

The bridge never marks an issue `Done`.

## Requirements

- macOS or Linux
- Python 3.11+
- Git
- Codex app-server/proxy transport available through the configured SSH alias
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

## First automatic test

Do **not** open Codex interactively and do **not** type an issue prompt.

After filling every placeholder in the disposable `[targets.pilot]` entry and
verifying the remote app-server command, run exactly:

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
SSH p620
    ↓
thread/read + identity guard
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

v0.1 intentionally stays foreground. A LaunchAgent/systemd wrapper belongs in the next step after the protocol is proven.

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
