"""Bounded final-message evidence and CAS repair, owned by ExecutionFinalizer.

No provider starts, authority changes, lease reclaim, or business-result rewriting.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime, timezone

from task_registry import TaskRegistryError


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def bounded(text, limit=15000):
    # Reuse Host capture redaction; only final agent text reaches this module.
    from host_executor import HostExecutor
    text = HostExecutor._redact(text)
    if len(text) <= limit:
        return text
    return text[:limit // 2] + "\n[TRUNCATED; full source SHA256 retained]\n" + text[-limit // 2:]


def bounded_result(result):
    return dataclasses.replace(result, **{
        key: bounded(getattr(result, key))
        for key in ("summary", "changed_files", "validation", "blockers", "raw_result")
    })


def failure_stage(code):
    if code and (code.startswith("RESULT_DELIVERY_") or code == "HOST_SIDE_EFFECT_UNRESOLVED"):
        return "result_delivery"
    if code and code.startswith("RESULT_") and not code.startswith("RESULT_DELIVERY_"):
        return "result_ingestion"
    return "provider" if code else None



def decision_evidence(decision, evidence):
    if evidence and evidence.get("source_incomplete") and (
            decision.failure_code is None or failure_stage(decision.failure_code) == "result_ingestion"):
        from m9_integration import ExecutionFinalizer
        result = ExecutionFinalizer._canonical_blocked(
            summary="Result source is explicitly truncated; completion is not proven.",
            validation="Re-read the same final message without replaying the task.",
            blockers="RESULT_TRUNCATED: incomplete source.", terminal_state="BLOCKED")
        decision = dataclasses.replace(decision, result=result, terminal_state="BLOCKED",
            retry_required=False, failure_code="RESULT_TRUNCATED", failure_evidence="Result source is truncated")
    if evidence and failure_stage(decision.failure_code) == "result_ingestion":
        public = {k: evidence[k] for k in (
            "execution_ref", "task_id", "thread_id", "turn_id", "source", "message_id",
            "raw_sha256", "raw_bytes", "truncated", "parse_error")}
        return dataclasses.replace(decision, failure_evidence=json.dumps(public, ensure_ascii=False, sort_keys=True))
    return decision


def evidence_for(registry, execution_ref, task_id, turn_id, raw, source=None):
    from m9_integration import parse_codex_result, ResultParseError
    source = source or {}
    original = source.get("raw_text", raw) or ""
    route = registry.get_execution_routing_identity(execution_ref)
    binding = registry.get_binding(task_id)
    thread = route.conversation.binding if route else binding.thread_id if binding else None
    error = code = None
    try:
        parse_codex_result(raw or "")
    except ResultParseError as exc:
        code, error = exc.code, str(exc)
    if source.get("source_incomplete"):
        code, error = "RESULT_TRUNCATED", "Result source is explicitly truncated"
    def scalar(key):
        value = source.get(key)
        return bounded(str(value), 512) if isinstance(value, (str, int, float)) else None
    return dict(execution_ref=execution_ref, task_id=task_id, thread_id=thread, turn_id=turn_id,
                source=source.get("source", "finalizer_input"), message_id=scalar("message_id"),
                phase=scalar("phase"), created_at=scalar("created_at"),
                source_incomplete=bool(source.get("source_incomplete")),
                raw_sha256=digest(original), raw_bytes=len(original.encode("utf-8")),
                raw_excerpt=bounded(original), truncated=len(original) > 15000,
                failure_stage="result_ingestion" if code else None,
                failure_code=code, parse_error=error)


def record_ingestion(registry, execution_ref, task_id, turn_id, raw, source=None):
    evidence = evidence_for(registry, execution_ref, task_id, turn_id, raw, source)
    body = json.dumps(evidence, sort_keys=True, ensure_ascii=False)
    with registry._connect() as conn:
        conn.execute("INSERT OR IGNORE INTO result_ingestions VALUES (?,?,?,?,?)",
                     (execution_ref, digest(body), body, evidence["failure_code"],
                      datetime.now(timezone.utc).isoformat()))
    return evidence


def check_repair(conn, identity, expected):
    """Check identity, old digest, ownership and side effects under one writer lock."""
    from execution_semantics import parse_routing_identity
    from tool_delivery import requires_reconciliation
    ref, task_id, thread, turn = (identity[k] for k in ("execution_ref", "task_id", "thread_id", "turn_id"))
    if any(identity[k] != expected[k] for k in ("task_id", "thread_id", "turn_id")):
        raise TaskRegistryError("RESULT_RECONCILIATION_IDENTITY_CONFLICT")
    row = conn.execute("SELECT * FROM execution_history WHERE execution_ref=?", (ref,)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM executions WHERE execution_ref=?", (ref,)).fetchone()
    task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    binding = conn.execute("SELECT thread_id FROM conversation_bindings WHERE task_id=?", (task_id,)).fetchone()
    old = conn.execute("SELECT * FROM execution_results WHERE execution_ref=?", (ref,)).fetchone()
    if (row is None or old is None or task is None or binding is None
            or row["task_id"] != task_id or old["task_id"] != task_id
            or row["turn_id"] != turn or old["turn_id"] != turn or task["turn_id"] != turn
            or binding["thread_id"] != thread or task["status"] == "ARCHIVED"):
        raise TaskRegistryError("RESULT_RECONCILIATION_IDENTITY_CONFLICT")
    route = parse_routing_identity(row["routing_identity_json"])
    # The sealed route and current task must still name the original worktree.
    from task_registry import TaskRegistry
    key = TaskRegistry.worktree_key(host=task["host"], cwd=task["cwd"],
                                    repository_origin=task["repository_origin"])
    if (route is None or route.conversation.binding != thread or row["worktree_key"] != key
            or route.workspace.worktree_key != key):
        raise TaskRegistryError("RESULT_RECONCILIATION_ROUTE_CONFLICT")
    if (row["stage"] not in {"BLOCKED", "COMPLETED", "FAILED"}
            or task["execution_state"] not in {"BLOCKED", "COMPLETED", "FAILED"}
            or task["codex_running"]
            or conn.execute("SELECT 1 FROM executions WHERE task_id=? AND execution_ref<>?",
                            (task_id, ref)).fetchone()
            or conn.execute("SELECT 1 FROM execution_history WHERE task_id=? AND execution_ref<>? AND acquired_at>=?",
                            (task_id, ref, row["acquired_at"])).fetchone()
            or conn.execute("SELECT 1 FROM worktree_leases WHERE worktree_key=? OR task_id=? OR execution_ref=?",
                            (row["worktree_key"], task_id, ref)).fetchone()):
        raise TaskRegistryError("RESULT_RECONCILIATION_OWNER_CONFLICT")
    # Tool ledger schema is installed by TaskRegistry. No delivery changes here.
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='host_tool_deliveries'").fetchone()
    deliveries = (conn.execute("SELECT * FROM host_tool_deliveries WHERE execution_ref=?", (ref,)).fetchall()
                  if exists else [])
    if any(requires_reconciliation(dict(item)) for item in deliveries):
        raise TaskRegistryError("RESULT_RECONCILIATION_SIDE_EFFECT_UNRESOLVED")
    host = conn.execute("SELECT 1 FROM host_executions WHERE execution_ref=? AND (completed_at IS NULL OR result_state IN ('UNKNOWN','RUNNING'))",
                        (ref,)).fetchone()
    if host:
        raise TaskRegistryError("RESULT_RECONCILIATION_SIDE_EFFECT_UNRESOLVED")
    current_hash = digest(old["raw_result"])
    previous = conn.execute("""SELECT * FROM result_reconciliation_audit
        WHERE execution_ref=? AND expected_result_sha256=? AND source_sha256=?""",
        (ref, expected["result_sha256"], expected["source_sha256"])).fetchone()
    if previous and previous["new_result_sha256"] == current_hash:
        return dict(row), dict(task), dict(old), True
    if current_hash != expected["result_sha256"]:
        raise TaskRegistryError("RESULT_RECONCILIATION_CAS_CONFLICT")
    # Restrict repairs to ingestion failures and the exact legacy synthesis.
    legacy = old["summary"] in {
        "Provider terminal output omitted the result marker and no successful host execution evidence was recorded.",
        "Host execution evidence succeeded; provider terminal output omitted the result marker.",
    }
    if not legacy and row["failure_stage"] != "result_ingestion":
        raise TaskRegistryError("RESULT_RECONCILIATION_NOT_INGESTION_FAILURE")
    return dict(row), dict(task), dict(old), False


def repair_result(finalizer, *, identity, expected, raw, source, provider_outcome, provider_status):
    """Called only after the existing dispatcher has re-read the terminal owner."""
    registry = finalizer.registry
    evidence = evidence_for(registry, identity["execution_ref"], identity["task_id"],
                            identity["turn_id"], raw, source)
    if (source is None or source.get("source") != "provider_final"
            or evidence["raw_sha256"] != expected["source_sha256"]
            or (expected.get("message_id") is not None and evidence["message_id"] != expected["message_id"])):
        raise TaskRegistryError("RESULT_RECONCILIATION_SOURCE_CONFLICT")
    # _decision preserves strict parsing and definitive provider failure.
    decision = finalizer._decision(identity["execution_ref"], raw,
        provider_outcome=provider_outcome, provider_status=provider_status, reconcile_delivery=False)
    if decision.terminal_state == "RECOVERY_REQUIRED":
        raise TaskRegistryError("RESULT_RECONCILIATION_SIDE_EFFECT_UNRESOLVED")
    decision = decision_evidence(decision, evidence)
    result = bounded_result(decision.result)
    stamp = datetime.now(timezone.utc).isoformat()
    with registry._shadow_write_connection() as conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        row, task, old, duplicate = check_repair(conn, identity, expected)
        from execution_liveness import read_row, release_allowed
        live = read_row(conn, identity["execution_ref"])
        if live is None or not release_allowed(conn, identity["execution_ref"]):
            raise TaskRegistryError("RESULT_RECONCILIATION_TERMINAL_NOT_PROVEN")
        if duplicate:
            return dict(state=row["stage"], authoritative=True, finalized=True,
                        completion_delivery="DONE", result_reconciliation="ALREADY_APPLIED", retry_required=False)
        stage = failure_stage(decision.failure_code)
        conn.execute("""INSERT INTO result_reconciliation_audit
            (execution_ref,expected_result_sha256,source_sha256,new_result_sha256,
             previous_result_json,previous_execution_json,previous_task_json,evidence_json,reconciled_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (identity["execution_ref"], expected["result_sha256"], expected["source_sha256"],
             digest(result.raw_result), json.dumps(old, sort_keys=True), json.dumps(row, sort_keys=True),
             json.dumps(task, sort_keys=True), json.dumps(evidence, sort_keys=True), stamp))
        conn.execute("""UPDATE execution_results SET status=?,summary=?,changed_files=?,validation=?,
            blockers=?,next_state=?,raw_result=?,received_at=?,writeback_state='PENDING',
            writeback_body_hash=NULL,written_at=NULL WHERE execution_ref=?""",
            (result.status,result.summary,result.changed_files,result.validation,result.blockers,
             result.next_state,result.raw_result,stamp,identity["execution_ref"]))
        for table in ("executions", "execution_history"):
            conn.execute(f"""UPDATE {table} SET stage=?,execution_state=?,failure_stage=?,failure_code=?,
                failure_evidence=? WHERE execution_ref=?""", (decision.terminal_state,decision.terminal_state,
                stage,decision.failure_code,decision.failure_evidence,identity["execution_ref"]))
        conn.execute("""UPDATE tasks SET execution_state=?,current_stage=?,current_blocker=?,
            codex_running=0,retry_required=0,failure_stage=?,failure_code=?,failure_evidence=?,
            updated_at=?,last_progress_at=? WHERE task_id=?""",
            (decision.terminal_state,decision.terminal_state,result.blockers if result.status=="BLOCKED" else None,
             stage,decision.failure_code,decision.failure_evidence,stamp,stamp,identity["task_id"]))
        registry._append_shadow_observation(conn, task_id=identity["task_id"], execution_ref=identity["execution_ref"],
            event_type="V1ExecutionResultPersistedObserved", observed_at=stamp,
            payload={"exact_result_ref":identity["execution_ref"], "turn_id":identity["turn_id"],
                     "result_status":result.status,"evidence_ref":identity["execution_ref"]})
        body = json.dumps(evidence, sort_keys=True, ensure_ascii=False)
        conn.execute("INSERT OR IGNORE INTO result_ingestions VALUES (?,?,?,?,?)",
                     (identity["execution_ref"],digest(body),body,evidence["failure_code"],stamp))
    return dict(state=decision.terminal_state, authoritative=True, finalized=True, completion_delivery="DONE",
                result_reconciliation="APPLIED", failure_stage=stage, failure_code=decision.failure_code,
                failure_evidence=decision.failure_evidence, retry_required=False, retry_scope="RESULT_RECONCILIATION")
