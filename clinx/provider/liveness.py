"""Interpret exact Provider observations; owns no durable state."""

ACTIVE = {"inprogress", "running", "active"}
TERMINAL = {"completed", "succeeded", "success", "failed", "error", "systemerror",
            "errored", "timeout", "timedout", "disconnected", "connectionlost",
            "connectionclosed", "cancelled", "canceled", "interrupted", "aborted"}

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
        elif (rows and not unavailable and len(exact) == len(rows)
              and all(r['state'] in {'notLoaded', 'unloaded'} and
                      normalized(r.get('turn_status')) == 'completed' for r in exact)):
            # All configured peers agree on this exact durable completion.
            # Unloading a finished thread is not loss of its terminal evidence.
            # Interrupted projections, missing turns and unreachable peers do
            # not qualify; an active claim already took precedence above.
            owner, liveness, reason = exact[0], 'TERMINAL', 'EXACT_COMPLETED_TURN_UNLOADED'
    # Absence of a socket is not proof its daemon has no active owner.
    safe = bool(liveness == "TERMINAL" and not unavailable and not active_claims and not conflict)
    return dict(provider_liveness=liveness, transport_health=health, liveness_reason=reason,
                owner_endpoint=owner["endpoint"] if owner else None, ownership_conflict=conflict,
                release_safe=safe, observations=rows)
