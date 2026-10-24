"""Repair only a stale coarse projection from exact committed completion truth."""

import json

from clinx.compat.task_registry import TaskRegistryError, _now

KIND = "COMPLETED_TASK_PROJECTION_V1"


def reconcile(registry, execution_ref, *, check_only=False):
    with registry._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM execution_history WHERE execution_ref=?", (execution_ref,)
        ).fetchone()
        if (
            not row
            or row["stage"] != "COMPLETED"
            or row["execution_state"] != "COMPLETED"
            or not row["released_at"]
        ):
            raise TaskRegistryError("TASK_PROJECTION_COMPLETION_UNPROVEN")
        e = dict(row)
        t = dict(
            conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (e["task_id"],)
            ).fetchone()
        )
        r = conn.execute(
            "SELECT * FROM execution_results WHERE execution_ref=?", (execution_ref,)
        ).fetchone()
        latest = conn.execute(
            "SELECT execution_ref FROM execution_history WHERE task_id=? ORDER BY acquired_at DESC,rowid DESC LIMIT 1",
            (e["task_id"],),
        ).fetchone()
        binding = conn.execute(
            "SELECT * FROM conversation_bindings WHERE task_id=?", (e["task_id"],)
        ).fetchone()
        captured = json.loads(e["routing_identity_json"])
        if (
            not r
            or r["task_id"] != t["task_id"]
            or r["turn_id"] != e["turn_id"]
            or r["status"] not in {"PASS", "COMPLETED"}
            or latest["execution_ref"] != execution_ref
            or t["execution_state"] != "COMPLETED"
            or t["current_stage"] != "COMPLETED"
            or t["turn_id"] != e["turn_id"]
            or t["codex_running"]
            or t["retry_required"]
            or not binding
            or binding["thread_id"] != captured["conversation"]["binding"]
            or t["status"] not in {"ACTIVE", "COMPLETED"}
        ):
            raise TaskRegistryError("TASK_PROJECTION_IDENTITY_CONFLICT")
        if (
            conn.execute(
                "SELECT 1 FROM executions WHERE task_id=?", (t["task_id"],)
            ).fetchone()
            or conn.execute(
                "SELECT 1 FROM worktree_leases WHERE task_id=? OR worktree_key=?",
                (t["task_id"], e["worktree_key"]),
            ).fetchone()
        ):
            raise TaskRegistryError("TASK_PROJECTION_ACTIVE_OWNER")
        response = dict(
            execution_ref=execution_ref,
            task_ref=t["task_id"],
            execution_state="COMPLETED",
            status="COMPLETED",
            business_outcome="NOT_EVALUATED",
            idempotent=t["status"] == "COMPLETED",
            check_only=check_only,
            execution_started=False,
            lease_mutated=False,
        )
        if check_only or t["status"] == "COMPLETED":
            return response
        conn.execute("""CREATE TABLE IF NOT EXISTS execution_recovery_audit (
            execution_ref TEXT PRIMARY KEY,recovered_at TEXT NOT NULL,
            previous_execution_json TEXT NOT NULL,previous_task_json TEXT NOT NULL,
            owner_evidence_json TEXT NOT NULL)""")
        if conn.execute(
            "SELECT 1 FROM execution_recovery_audit WHERE execution_ref=?",
            (execution_ref,),
        ).fetchone():
            raise TaskRegistryError("TASK_PROJECTION_AUDIT_CONFLICT")
        conn.execute(
            "INSERT INTO execution_recovery_audit VALUES (?,?,?,?,?)",
            (
                execution_ref,
                _now(),
                json.dumps(e, sort_keys=True),
                json.dumps(t, sort_keys=True),
                json.dumps(
                    dict(
                        kind=KIND, source="COMMITTED_EXACT_COMPLETION", result=dict(r)
                    ),
                    sort_keys=True,
                ),
            ),
        )
        conn.execute(
            "UPDATE tasks SET status='COMPLETED',updated_at=? WHERE task_id=? AND status='ACTIVE' AND turn_id=? AND execution_state='COMPLETED'",
            (_now(), t["task_id"], e["turn_id"]),
        )
        return response
