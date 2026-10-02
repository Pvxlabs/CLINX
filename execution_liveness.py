"""Exact execution liveness, independent of transport and lifecycle projections.

The read plane never probes, migrates, finalizes or releases a lease. Host
activity is sourced from the existing provider-acknowledged delivery ledger.
"""
import datetime as dt
import json
import time

LIVENESS_GRACE_SECONDS = 120.0
ACTIVE = {"inprogress", "running", "active"}
TERMINAL = {"completed", "succeeded", "success", "failed", "error", "systemerror",
            "errored", "timeout", "timedout", "disconnected", "connectionlost",
            "connectionclosed", "cancelled", "canceled", "interrupted", "aborted"}
SCHEMA = """CREATE TABLE IF NOT EXISTS execution_liveness (
    execution_ref TEXT PRIMARY KEY, thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
    provider_liveness TEXT NOT NULL, transport_health TEXT NOT NULL,
    liveness_reason TEXT NOT NULL, owner_endpoint TEXT, ownership_conflict INTEGER NOT NULL,
    last_provider_activity_at REAL, last_live_owner_at REAL, unknown_since REAL,
    observed_at REAL NOT NULL, release_safe INTEGER NOT NULL, observations_json TEXT NOT NULL
)"""


def normalized(value):
    if isinstance(value, dict):
        value = value.get("type") or value.get("status") or value.get("state")
    return str(value or "UNKNOWN").replace("_", "").replace("-", "").casefold()


def classify(observations, thread_id, turn_id):
    """A loaded exact-turn owner outranks an unloaded daemon's history.

    Active thread claims on another turn still prohibit terminalization/release.
    An inaccessible peer degrades transport but cannot cancel a proved owner.
    """
    rows = observations
    unavailable = [r for r in rows if r["state"] in {"UNKNOWN", "OFFLINE"} or r.get("transport_error")]
    health = ("UNAVAILABLE" if not rows or len(unavailable) == len(rows) else
              "DEGRADED" if unavailable else "HEALTHY")
    active_claims = [r for r in rows if normalized(r["state"]) in ACTIVE]
    exact = [r for r in rows if r.get("thread_id") == thread_id and
             r.get("turn_id") == turn_id and turn_id]
    live = [r for r in exact if r in active_claims and normalized(r.get("turn_status")) in ACTIVE]
    conflict = len(active_claims) > 1
    owner = None
    liveness, reason = "UNKNOWN", "OWNER_UNCONFIRMED"
    if conflict:
        reason = "OWNERSHIP_CONFLICT"
    elif len(live) == 1:
        owner, liveness, reason = live[0], "LIVE", "EXACT_OWNER_ACTIVE"
    elif active_claims:
        reason = "ACTIVE_OWNER_EXACT_TURN_UNCONFIRMED"
    else:
        loaded = [r for r in rows if r["state"] not in {"UNKNOWN", "OFFLINE", "notLoaded", "unloaded"}]
        terminals = [r for r in exact if r["state"] == "idle" and
                     normalized(r.get("turn_status")) in TERMINAL]
        if len(loaded) == 1 and len(terminals) == 1:
            owner, liveness, reason = terminals[0], "TERMINAL", "EXACT_OWNER_TERMINAL"
    # Absence of a socket is not proof its daemon has no active owner.
    safe = bool(liveness == "TERMINAL" and not unavailable and not active_claims and not conflict)
    return dict(provider_liveness=liveness, transport_health=health, liveness_reason=reason,
                owner_endpoint=owner["endpoint"] if owner else None, ownership_conflict=conflict,
                release_safe=safe, observations=rows)


def host_delivery_at(conn, execution_ref):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='host_tool_deliveries'").fetchone():
        return None
    return conn.execute("""SELECT MAX(acknowledged_at) FROM host_tool_deliveries
        WHERE execution_ref=? AND delivery_state='DELIVERED' AND host_execution_ref IS NOT NULL""",
        (execution_ref,)).fetchone()[0]


def read_row(conn, execution_ref):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='execution_liveness'").fetchone():
        return None
    row = conn.execute("SELECT * FROM execution_liveness WHERE execution_ref=?", (execution_ref,)).fetchone()
    return dict(row) if row else None


def iso(value):
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat() if value is not None else None


def public_status(conn, execution_ref, *, now=None):
    now = time.time() if now is None else now
    row = read_row(conn, execution_ref)
    result = dict(provider_liveness="UNKNOWN", transport_health="UNAVAILABLE",
                  codex_running=False, last_provider_activity_at=None,
                  last_host_delivery_at=iso(host_delivery_at(conn, execution_ref)),
                  last_live_owner_at=None, liveness_reason="NO_EXACT_OWNER_OBSERVATION",
                  ownership_conflict=False, liveness_observed_at=None,
                  liveness_grace_seconds=LIVENESS_GRACE_SECONDS)
    if row:
        fresh = now - row["observed_at"] <= LIVENESS_GRACE_SECONDS
        live = row["provider_liveness"] == "LIVE" and fresh
        result.update(provider_liveness=row["provider_liveness"] if fresh or row["provider_liveness"] == "TERMINAL" else "UNKNOWN",
                      transport_health=row["transport_health"], codex_running=live,
                      last_provider_activity_at=iso(row["last_provider_activity_at"]),
                      last_live_owner_at=iso(row["last_live_owner_at"]),
                      liveness_observed_at=iso(row["observed_at"]),
                      ownership_conflict=bool(row["ownership_conflict"]),
                      liveness_reason=row["liveness_reason"] if fresh or row["provider_liveness"] == "TERMINAL" else "OWNER_OBSERVATION_EXPIRED")
    return result


def record(registry, execution_ref, thread_id, turn_id, evidence, *, now=None):
    """Persist a current exact identity only; unknown observations are not activity."""
    from task_registry import TaskRegistryError
    now = time.time() if now is None else now
    execution = registry.get_execution_record(execution_ref)
    route = registry.get_execution_routing_identity(execution_ref)
    if (execution is None or execution.get("execution_owned_turn") != turn_id or
            route is None or route.conversation.binding != thread_id):
        raise TaskRegistryError("liveness evidence does not own exact execution/thread/turn")
    task = registry.get_task(execution["task_id"])
    if task.turn_id != turn_id:
        raise TaskRegistryError("liveness evidence does not own current task turn")
    with registry._connect() as conn:
        old = read_row(conn, execution_ref) or {}
        if old and (old["thread_id"] not in {"", thread_id} or old["turn_id"] not in {"", turn_id}):
            raise TaskRegistryError("liveness identity is immutable once known")
        known = evidence["provider_liveness"] in {"LIVE", "TERMINAL"}
        activity = now if known else old.get("last_provider_activity_at")
        live_at = now if evidence["provider_liveness"] == "LIVE" else old.get("last_live_owner_at")
        unknown_since = None if known else old.get("unknown_since") or now
        last_host = host_delivery_at(conn, execution_ref)
        anchor = max(v for v in (unknown_since, activity, live_at, last_host, now if known else None) if v is not None)
        expired = not known and now - anchor >= LIVENESS_GRACE_SECONDS
        state = ("CODEX_RUNNING" if evidence["provider_liveness"] == "LIVE" else
                 "RECOVERY_REQUIRED" if expired and evidence["transport_health"] != "HEALTHY" and
                 not evidence["ownership_conflict"] else "TRANSPORT_UNCERTAIN")
        conn.execute("""INSERT INTO execution_liveness VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(execution_ref) DO UPDATE SET
            thread_id=excluded.thread_id,turn_id=excluded.turn_id,
            provider_liveness=excluded.provider_liveness,transport_health=excluded.transport_health,
            liveness_reason=excluded.liveness_reason,owner_endpoint=excluded.owner_endpoint,
            ownership_conflict=excluded.ownership_conflict,
            last_provider_activity_at=excluded.last_provider_activity_at,
            last_live_owner_at=excluded.last_live_owner_at,unknown_since=excluded.unknown_since,
            observed_at=excluded.observed_at,release_safe=excluded.release_safe,
            observations_json=excluded.observations_json""",
            (execution_ref, thread_id, turn_id, evidence["provider_liveness"], evidence["transport_health"],
             evidence["liveness_reason"], evidence["owner_endpoint"], int(evidence["ownership_conflict"]),
             activity, live_at, unknown_since, now, int(evidence["release_safe"]),
             json.dumps(evidence["observations"], sort_keys=True)))
    if evidence["provider_liveness"] != "TERMINAL" or not evidence["release_safe"]:
        if task.execution_state in {"CANCEL_REQUESTED", "CANCELLATION_PENDING"}:
            state = "CANCELLATION_PENDING"
        registry.set_execution_state(execution["task_id"], state, current_stage=state,
            current_blocker=None if evidence["provider_liveness"] == "LIVE" else evidence["liveness_reason"],
            codex_running=evidence["provider_liveness"] == "LIVE",
            retry_required=evidence["provider_liveness"] != "LIVE",
            failure_stage=None if evidence["provider_liveness"] == "LIVE" else "reconciliation",
            failure_code=None if evidence["provider_liveness"] == "LIVE" else evidence["liveness_reason"],
            failure_evidence=None if evidence["provider_liveness"] == "LIVE" else evidence["liveness_reason"],
            _shadow_execution_ref=execution_ref)
        with registry._connect() as conn:
            conn.execute("""UPDATE executions SET stage=? WHERE execution_ref=? AND EXISTS
                (SELECT 1 FROM worktree_leases WHERE execution_ref=?)""",
                (state, execution_ref, execution_ref))
    with registry._connect() as conn:
        return dict(state=state, authoritative=False, **public_status(conn, execution_ref, now=now))


def release_allowed(conn, execution_ref):
    row = read_row(conn, execution_ref)
    # Existing exact finalizers without a reconciler observation keep their
    # ownership contract. Once liveness has been observed it cannot be bypassed.
    return row is None or bool(row["release_safe"] and row["provider_liveness"] == "TERMINAL"
                               and time.time() - row["observed_at"] <= LIVENESS_GRACE_SECONDS)


def invalidate(registry, execution_ref, reason):
    """Loss of local identity invalidates live proof without inventing activity."""
    with registry._connect() as conn:
        conn.execute("""UPDATE execution_liveness SET provider_liveness='UNKNOWN',
            transport_health='UNAVAILABLE',liveness_reason=?,release_safe=0,
            unknown_since=COALESCE(unknown_since,?),observed_at=? WHERE execution_ref=?""",
            (reason, time.time(), time.time(), execution_ref))


def completion_activity(registry, identity):
    """An enrolled exact completion event is activity, never terminal authority."""
    with registry._connect() as conn:
        now = time.time()
        conn.execute("""INSERT INTO execution_liveness VALUES
            (?,?,?,'UNKNOWN','UNAVAILABLE','COMPLETION_REQUIRES_RECONCILIATION',NULL,0,?,NULL,?,?,0,'[]')
            ON CONFLICT(execution_ref) DO UPDATE SET last_provider_activity_at=excluded.last_provider_activity_at""",
            (identity.execution_ref, identity.thread_id, identity.turn_id, now, now, now))


def require_reobservation(registry, execution_ref):
    """Persist the recovery release barrier, including pre-v2 executions.

    Empty identity fields represent missing routing evidence and never prove
    liveness. Only record(), after checking the durable exact route, fills them.
    """
    execution = registry.get_execution_record(execution_ref)
    route = registry.get_execution_routing_identity(execution_ref)
    if execution is None:
        return
    tid = route.conversation.binding if route and route.conversation.status == "BOUND" else ""
    turn = execution.get("execution_owned_turn") or ""
    with registry._connect() as conn:
        now = time.time()
        conn.execute("""INSERT OR IGNORE INTO execution_liveness VALUES
            (?,?,?,'UNKNOWN','UNAVAILABLE','RECOVERY_REQUIRES_OWNER_OBSERVATION',NULL,0,NULL,NULL,?,?,0,'[]')""",
            (execution_ref, tid, turn, now, now))
        conn.execute("""UPDATE execution_liveness SET release_safe=0,
            liveness_reason=CASE WHEN provider_liveness='UNKNOWN' THEN liveness_reason ELSE 'RECOVERY_REQUIRES_OWNER_OBSERVATION' END,
            provider_liveness='UNKNOWN',unknown_since=COALESCE(unknown_since,?)
            WHERE execution_ref=?""", (now, execution_ref))
