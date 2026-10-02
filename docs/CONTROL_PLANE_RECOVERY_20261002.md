# P620 control-plane activation and exact cancellation recovery

Accepted and activated on 2026-10-02, approximately 16:37–16:40 Asia/Shanghai.
The user explicitly authorized the switch after confirming no task needed CLINX advancement.

## Incident and boundaries

`RCA DATA L25 实时架构与成本`:

- task: `task_d974fcd472034b95aaa6f8797f91e14d`
- execution: `exec_e47dc3d423ca40458cd77e0b95a7e141`
- thread: `01a0fa9b-13ce-7fd2-84d0-82cc87e1c429`
- turn: `01a0fb97-ae65-7f13-bfdd-f83d57ff01d4`

Read-only preflight confirmed native owner `active / inProgress`, while the other
configured daemon returned `notLoaded / interrupted` for the same exact turn.
The canonical task/history said CANCELLED, the lease was absent and the handoff
was DONE. The Monitor faithfully displayed that incorrect canonical projection.
The exact PID that originally wrote cancellation was not established.

The formal services used canonical checkout `38631af027daf27f30af2a58204781d989a99aad`.
A separate MCP PID 2093376 had survived since 2026-10-01 17:02:54 UTC under an old
`clinx-context-mcp --help` command. The already merged exact-owner liveness fix
was not active in these processes. The old services and that exact orphan were
stopped for the authorized switch; the Provider and Observer were not restarted.

No ORION code, new Provider turn, resume, interrupt, replay, or execution start was
performed. Restoring a Running projection proves exact owner liveness; it does
not assert that useful task progress or tool delivery occurred during maintenance.

## Narrow recovery change

`recover-execution REF --restore-cancelled` is explicit maintenance. Ordinary
recovery and read-only status do not reopen cancellation. The new flag requires a
fresh unique exact active owner, current task/thread/turn and route identity,
unchanged worktree identity, no competing execution/lease/newer history, no
structured result, and a matching completion handoff.

A SQLite transaction journals the old task and execution plus Provider evidence,
moves the original execution identity back from history, reacquires its original
lease, and enrolls its handoff as PENDING. The intermediate state is
RECOVERY_REQUIRED, not a fabricated Running claim. Normal reconciliation reads the
Provider again and establishes CODEX_RUNNING / LIVE. A crash after the transaction
retains the lease and a durable pending handoff. Old records remain available in
`execution_recovery_audit`; Activity retains one exact active/history identity.

## Installed runtime

- Base source: `3fb10b5e177e1bb8b7f8518d895ad26edeef6f09`.
- Additional source changes: `bridge.py`, `execution_liveness.py`.
- Release: `/home/pvxlabs/.local/lib/clinx-control/releases/3fb10b5-recovery-c24cd0060f90`.
- Source manifest digest: `c24cd0060f90ef484dfbaa0326128db71a1e333430bbdcc9e883344ef2aec8ff`.
- Archive SHA-256: `edcf4988e90a09526179118de3c28f6635825846fc1c5cd3e886181264e4665b`.
- `RELEASE.json` records every packaged file hash; readback reverified all files.
- `clinx.service.d/50-control-release.conf` points the dispatcher at `clinx-control/current`.
- Tunnel profile command points to `clinx-control/launch-mcp`; all other profile
  values were compared and preserved. The launcher retains the original minimal
  child environment and uses the existing canonical config, without copying secrets.
- The Observer file already exactly matched current source (`002b10f28b6fad75…`);
  it and its configuration were left unchanged.
- Canonical checkout HEAD and all 13 pre-existing dirty files were hash-verified
  unchanged. Installed runtime is separate from that development checkout.

Readback PIDs: dispatcher 289423, tunnel 289422, MCP child 289448, unchanged Provider
1829817, unchanged Observer 3688161. Both switched services were active with zero
restarts at verification. The public CLINX MCP `get_status` call successfully
returned CODEX_RUNNING, LIVE, HEALTHY and `read_only=true` after the switch.

## Validation and result

- 65 focused recovery/liveness/owner/completion tests passed locally and on P620.
- Expanded targeted regression: 254 passed, 24 subtests passed. The first Mac run
  had two `/var` versus `/private/var` fixture-path assertions; running with a real,
  resolved TMPDIR passed without product/test changes for that environment issue.
- Real exact record: CODEX_RUNNING; Provider LIVE / HEALTHY; release_safe=false;
  original execution/thread/turn and acquisition time preserved; one held lease;
  one audit entry; handoff PENDING.
- Automatic handoff attempts and liveness observation timestamps advanced after
  startup, confirming ongoing supervision rather than a one-time database edit.
- Real iMac screenshot: Active count 1, one selected Running task, Inspector Running,
  and P620 Connected. UI source and accepted icon/menu/archive changes were preserved.
- No new commit or push was performed for this activation.

Local evidence: `/tmp/clinx-control-activation/` (tests, release manifest/archive,
recovery output, and `monitor-running.png`).

## Backup and rollback boundary

P620 backup:
`/home/pvxlabs/.local/state/clinx/maintenance/20261002T082945Z-pre-liveness/`.
It contains the old source archive, service files, tunnel profile, remote dirty
patch/manifest, initial and quiesced SQLite backups, and final `readback.json`.
Both SQLite backups had SHA-256:
`169ef74cebdf2ed4fa0c778e6003c14276445eb3eb972d560fbf799d32dccb78`.

A controlled software rollback would stop the two switched services, restore the
saved tunnel profile, remove only the added `50-control-release.conf`, reload
systemd and restart the services. Old software has the observed ownership defect;
rollback must first account for the restored live execution. Do not restore the
entire backup database over later operational updates. The recovery audit and
backup preserve the exact earlier records for a separately validated data repair.
