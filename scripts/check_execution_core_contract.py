"""Offline Stage-2 contract verifier. No dispatch, DB, clock or runtime authority."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

CONTRACT = Path(__file__).resolve().parents[1] / "fixtures/execution_core_v1.json"
FAMILY = "CLINX_EXECUTION_CORE_V1"
BINDING = {
    "execution_id", "attempt_id", "provider_session_id", "thread_id", "turn_id",
    "operation_id", "generation",
}
OWNER = {
    "execution_id", "attempt_id", "assignment_id", "worker_id", "incarnation_id",
    "resource_key", "resource_epoch",
}
GUARDS = {
    "children_resolved", "admission", "fence", "binding", "provider_live",
    "provider_terminal", "not_dispatched", "deliveries", "retry", "finalizer",
    "release", "expired", "stopped", "incarnation", "event_bundle", "grant",
    "new_incarnation",
}
FACT_FIELDS = {
    "unresolved_execution_ids", "commit_bundle", "binding", "observation", "now",
    "stored_owner", "presented_owner", "safety_pending", "stored_versions",
    "expected_versions", "expires_at", "allocation_state", "assignment_state",
    "previous_epoch", "used_units", "capacity_units", "capacity", "unresolved_allocation_ids",
    "current_incarnation", "presented_incarnation", "old_writer_stopped_ref",
    "side_effect_fenced_ref", "dispatch_certainty", "non_dispatch_proof", "retry_policy",
    "delivery_ledger_complete", "deliveries", "result", "committed_outcome",
    "finalization_phase", "cancel_requested", "lease_execution_id", "lease_attempt_id",
    "previous_incarnation", "previous_generation", "next_generation",
    "old_assignments_quarantined", "extensions",
}


def bundle(facts, fields):
    values = facts.get("commit_bundle")
    return (isinstance(values, list) and all(isinstance(v, str) for v in values)
            and len(values) == len(fields) and set(values) == fields)


class Invalid(ValueError):
    """A malformed or conflicting contract fact; never silently skipped."""


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def identity(value, fields):
    return (isinstance(value, dict) and set(value) == fields
            and all(type(v) is int and v > 0 if k in {"generation", "resource_epoch"}
                    else isinstance(v, str) and bool(v) for k, v in value.items()))


def load_contract():
    contract = json.loads(CONTRACT.read_text())
    if type(contract["schema_version"]) is not int:
        raise Invalid("schema version must be an integer")
    if (contract["family"], contract["contract_version"], contract["schema_version"]) != (
        FAMILY, "1.0.0", 1
    ):
        raise Invalid("unsupported contract")
    expected = {"Task", "Execution", "Attempt", "ProviderSession", "RuntimeWorker",
                "ResourceAllocation", "Event"}
    if set(contract["objects"]) != expected:
        raise Invalid("object coverage")
    for name, obj in contract["objects"].items():
        states = set(obj["states"])
        if len(states) != len(obj["states"]) or not set(obj["terminal"]) <= states:
            raise Invalid("invalid states")
        keys = set()
        for edge in obj["transitions"]:
            key = edge["from"], edge["action"], edge["to"]
            if key in keys or not set(edge["guards"]) <= GUARDS:
                raise Invalid("ambiguous transition or unknown guard")
            keys.add(key)
            if (edge["to"] not in states or edge["from"] in obj["terminal"]
                    or edge["from"] not in states | ({"ABSENT"} if name == "Event" else set())):
                raise Invalid("invalid transition endpoint")
    return contract


def guard(name, f, target):
    """Return APPLY, REJECT or HOLD_UNKNOWN from explicit, supplied ledger facts.

    The verifier checks consistency, not authenticity of the supplied evidence.
    Missing evidence never implies a negative side-effect fact.
    """
    if name == "children_resolved":
        return "APPLY" if f.get("unresolved_execution_ids") == [] else "HOLD_UNKNOWN"
    if name == "admission":
        return ("APPLY" if bundle(f, {
            "execution", "request", "prepared", "owner", "event", "receipt", "outbox"
        }) else "REJECT")
    if name == "binding":
        binding = f.get("binding")
        if isinstance(binding, dict) and binding.get("turn_id") is None:
            binding = {**binding, "turn_id": "not-yet-dispatched"}
        return "APPLY" if identity(binding, BINDING) else "HOLD_UNKNOWN"
    if name in {"provider_live", "provider_terminal"}:
        observation = f.get("observation")
        if guard("binding", f, target) != "APPLY" or not isinstance(observation, dict):
            return "HOLD_UNKNOWN"
        if (not identity(f["binding"], BINDING)
                or observation.get("binding") != f["binding"]
                or name == "provider_terminal" and observation.get("complete") is not True):
            return "HOLD_UNKNOWN"
        now, observed = f.get("now"), observation.get("observed_at")
        if not number(now) or not number(observed) or not 0 <= now - observed <= 120:
            return "HOLD_UNKNOWN"
        expected = ("LIVE", 1) if name == "provider_live" else ("TERMINAL", 0)
        actual = observation.get("liveness"), observation.get("active_claims")
        return "APPLY" if actual == expected else "HOLD_UNKNOWN"
    if name == "fence":
        if not identity(f.get("stored_owner"), OWNER):
            return "HOLD_UNKNOWN"
        if f.get("presented_owner") != f["stored_owner"]:
            return "REJECT"
        if f.get("safety_pending") is not False:
            return "REJECT"
        versions = f.get("stored_versions")
        if (not isinstance(versions, dict) or set(versions) != {
            "assignment", "allocation", "resource"
        } or any(type(v) is not int or v < 0 for v in versions.values())
                or f.get("expected_versions") != versions):
            return "REJECT"
        now, expires = f.get("now"), f.get("expires_at")
        if not number(now) or not number(expires):
            return "HOLD_UNKNOWN"
        if (now >= expires or f.get("allocation_state") not in {"HELD", "ACTIVE"}
                or f.get("assignment_state") != "ACTIVE"):
            return "REJECT"
        return "APPLY"
    if name == "grant":
        if not identity(f.get("presented_owner"), OWNER):
            return "REJECT"
        previous = f.get("previous_epoch")
        used, units, capacity = (f.get(k) for k in ("used_units", "capacity_units", "capacity"))
        if (type(previous) is not int or previous < 0
                or f["presented_owner"]["resource_epoch"] != previous + 1
                or any(type(v) is not int for v in (used, units, capacity))
                or used < 0 or units < 1 or capacity < used + units
                or f.get("unresolved_allocation_ids") != []):
            return "REJECT"
        return guard("fence", f, target)
    if name == "expired":
        if not number(f.get("now")) or not number(f.get("expires_at")):
            return "HOLD_UNKNOWN"
        return "APPLY" if f["now"] >= f["expires_at"] else "REJECT"
    if name == "incarnation":
        current = f.get("current_incarnation")
        return ("APPLY" if isinstance(current, str) and current
                and f.get("presented_incarnation") == current else "REJECT")
    if name == "new_incarnation":
        previous, next_generation = f.get("previous_generation"), f.get("next_generation")
        return ("APPLY" if type(previous) is int and previous > 0
                and type(next_generation) is int and next_generation == previous + 1
                and isinstance(f.get("presented_incarnation"), str)
                and f["presented_incarnation"]
                and f["presented_incarnation"] != f.get("previous_incarnation")
                and f.get("old_assignments_quarantined") is True else "REJECT")
    if name == "stopped":
        return ("APPLY" if all(isinstance(f.get(k), str) and f[k] for k in
                               ("old_writer_stopped_ref", "side_effect_fenced_ref"))
                else "HOLD_UNKNOWN")
    if name == "not_dispatched":
        proof = f.get("non_dispatch_proof")
        if f.get("dispatch_certainty") != "NOT_DISPATCHED" or not isinstance(proof, dict):
            return "HOLD_UNKNOWN"
        if guard("binding", f, target) != "APPLY":
            return "HOLD_UNKNOWN"
        return ("APPLY" if proof.get("source") == "dispatch_boundary"
                and proof.get("boundary") == "PRE_SEND" and proof.get("evidence_ref")
                and all(proof.get(k) == f["binding"][k] for k in ("execution_id", "attempt_id"))
                else "HOLD_UNKNOWN")
    if name == "retry":
        for check in ("not_dispatched", "stopped"):
            verdict = guard(check, f, target)
            if verdict != "APPLY":
                return verdict
        policy = f.get("retry_policy", {})
        return ("APPLY" if policy.get("allowed") is True
                and type(policy.get("remaining")) is int and policy["remaining"] > 0
                and policy.get("cancel_requested") is False and number(f.get("now"))
                and number(policy.get("deadline_at")) and f["now"] < policy["deadline_at"]
                else "REJECT")
    if name == "deliveries":
        if f.get("delivery_ledger_complete") is not True or not isinstance(
            f.get("deliveries"), list
        ) or guard("binding", f, target) != "APPLY":
            return "HOLD_UNKNOWN"
        seen = set()
        for row in f["deliveries"]:
            if not isinstance(row, dict) or not row.get("call_id") or row["call_id"] in seen:
                return "REJECT"
            seen.add(row["call_id"])
            if any(row.get(k) != f["binding"][k] for k in ("execution_id", "attempt_id")):
                return "REJECT"
            resolution = row.get("resolution")
            if row.get("delivery_state") == "FAILED" and isinstance(resolution, dict):
                outcome = f.get("committed_outcome", f.get("result", {}).get("outcome", target))
                if (resolution.get("method") == "EXISTING_AUTHORIZED_RECONCILIATION"
                        and resolution.get("evidence_ref") and row.get("host_completion") == "KNOWN"
                        and outcome in {"BLOCKED", "FAILED", "CANCELLED"}
                        and all(resolution.get(k) == row.get(k) for k in
                                ("execution_id", "attempt_id", "host_ref"))):
                    continue  # Retain FAILED; resolution is not a fabricated ACK.
            if (row.get("host_completion") != "KNOWN" or not row.get("host_ref")
                    or row.get("delivery_state") != "DELIVERED"
                    or row.get("ack_host_ref") != row["host_ref"]
                    or row.get("response_sent") is not True):
                return "HOLD_UNKNOWN"
        return "APPLY"
    if name in {"finalizer", "release"}:
        # Pre-send rejection has no provider terminal/Host ACK to observe.
        pre_send = f.get("dispatch_certainty") == "NOT_DISPATCHED"
        checks = ("not_dispatched", "deliveries") if pre_send else (
            "provider_terminal", "deliveries"
        )
        for check in checks:
            verdict = guard(check, f, target)
            if verdict != "APPLY":
                return verdict
        result = f.get("result", {})
        if (not result.get("result_ref") or any(result.get(k) != f["binding"][k]
                                               for k in ("execution_id", "attempt_id"))):
            return "REJECT"
        outcome = target if name == "finalizer" else f.get("committed_outcome")
        if outcome not in {"SUCCEEDED", "BLOCKED", "FAILED", "CANCELLED"}:
            return "REJECT"
        if result.get("outcome") != outcome or pre_send and outcome == "SUCCEEDED":
            return "HOLD_UNKNOWN"
        if outcome == "CANCELLED" and f.get("cancel_requested") is not True:
            return "HOLD_UNKNOWN"
        if f.get("finalization_phase") != "TERMINAL_COMMITTED":
            return "HOLD_UNKNOWN"
        if not bundle(f, {"result", "terminal", "event", "receipt", "outbox"}):
            return "REJECT"
        if name == "release":
            if any(f.get("lease_" + k) != f["binding"][k]
                   for k in ("execution_id", "attempt_id")):
                return "REJECT"
            return guard("stopped", f, target)
        return "APPLY"
    if name == "event_bundle":
        return ("APPLY" if bundle(f, {
            "state", "event", "receipt", "outbox"
        }) else "REJECT")
    raise Invalid("unknown guard")


def materialize(contract, record):
    result = copy.deepcopy(record)
    facts = {}
    for ref in result.pop("facts_refs", []):
        facts.update(copy.deepcopy(contract["contexts"][ref]))
    facts.update(result.get("facts", {}))
    result["facts"] = facts
    return result


def check_transition(contract, record):
    required = {"object", "id", "from", "to", "action", "actor", "version",
                "next_version", "facts"}
    if set(record) - required - {"extensions"} or not required <= set(record):
        return "REJECT"
    if (type(record["version"]) is not int or record["version"] < 0
            or type(record["next_version"]) is not int
            or record["next_version"] != record["version"] + 1
            or not isinstance(record["id"], str) or not record["id"]):
        return "REJECT"
    obj = contract["objects"].get(record["object"])
    if not obj:
        return "REJECT"
    edges = [e for e in obj["transitions"] if all(e[k] == record[k]
                                                 for k in ("from", "to", "action", "actor"))]
    if len(edges) != 1 or not isinstance(record["facts"], dict):
        return "REJECT"
    facts = record["facts"]
    if set(facts) - FACT_FIELDS:
        return "REJECT"
    binding = facts.get("binding")
    if isinstance(binding, dict):
        key = {"Execution": "execution_id", "Attempt": "attempt_id",
               "ProviderSession": "provider_session_id"}.get(record["object"])
        if key and binding.get(key) != record["id"]:
            return "REJECT"
        owner = facts.get("stored_owner")
        if isinstance(owner, dict) and any(owner.get(k) != binding.get(k)
                                           for k in ("execution_id", "attempt_id")):
            return "REJECT"
    try:
        verdicts = [guard(g, facts, record["to"]) for g in edges[0]["guards"]]
    except (KeyError, TypeError, ValueError):
        return "REJECT"
    return "REJECT" if "REJECT" in verdicts else (
        "HOLD_UNKNOWN" if "HOLD_UNKNOWN" in verdicts else "APPLY"
    )


def receipt_retry(original, request):
    """Historical receipt lookup only; never returns a live authority flag."""
    if original["scope"] != request["scope"] or original["key"] != request["key"]:
        return "REJECT"
    return "DUPLICATE" if original["request_hash"] == digest(request["semantics"]) else "REJECT"


def reconcile_ack(row, ack):
    """Check a late ACK against its original durable invocation, without re-dispatch."""
    fields = {"execution_id", "attempt_id", "owner_ref", "thread_id", "turn_id",
              "generation", "call_id", "namespace", "tool", "operation_id", "host_ref"}
    if not all(row.get(k) is not None for k in fields) or not row.get("response_sent"):
        return "HOLD_UNKNOWN"
    if row.get("reconciliation_state") == "RESOLVED" and row.get("delivery_state") != "DELIVERED":
        return "REJECT"  # Keep the original failure; append diagnostic evidence separately.
    if any(type(row[k]) is not type(ack.get(k)) or row[k] != ack[k] for k in fields):
        return "REJECT"
    if ack.get("success") is not True:
        return "HOLD_UNKNOWN"
    return "DUPLICATE" if row.get("delivery_state") == "DELIVERED" else "APPLY"


def replay(contract, events):
    """Validate complete v1 traces; no external effect or recovery is performed.

    Payloads are check_transition records. Baselines are the declared initial state,
    version 0. Partial historical streams require their original family's reader.
    """
    streams, seen, keys, bindings, generations = {}, {}, {}, {}, {}
    allowed = {"family", "contract_version", "schema_version", "event_id", "stream_id",
               "sequence", "payload", "payload_hash", "scope", "key"}
    for event in events:
        if set(event) != allowed or (event["family"], event["contract_version"],
                                    event["schema_version"]) != (FAMILY, "1.0.0", 1):
            raise Invalid("unsupported event envelope")
        if (type(event["schema_version"]) is not int
                or not all(isinstance(event[k], str) and event[k]
                           for k in ("event_id", "scope", "key"))):
            raise Invalid("invalid event identity or schema type")
        if digest(event["payload"]) != event["payload_hash"]:
            raise Invalid("payload hash mismatch")
        eid = event["event_id"]
        if eid in seen:
            if seen[eid] != canonical(event):
                raise Invalid("event id conflict")
            continue
        key = event["scope"], event["key"]
        if key in keys:
            raise Invalid("idempotency key reused by another event")
        r = event["payload"]
        stream = r["object"], r["id"]
        if event["stream_id"] != list(stream):
            raise Invalid("stream identity mismatch")
        obj = contract["objects"].get(r["object"])
        if obj is None:
            raise Invalid("unsupported object")
        initial = "ABSENT" if r["object"] == "Event" else obj["initial"]
        seq, version, state = streams.get(stream, (0, 0, initial))
        if (type(event["sequence"]) is not int or event["sequence"] != seq + 1
                or r["version"] != version or r["from"] != state):
            raise Invalid("gap, order, baseline or version mismatch")
        verdict = check_transition(contract, r)
        if verdict != "APPLY":
            raise Invalid(verdict)
        binding = r["facts"].get("binding")
        if binding:
            # This v1 slice has one unresolved attempt, and never changes the
            # execution/attempt/session/thread/turn/operation on reconnect.
            stable = {k: value for k, value in binding.items() if k != "generation"}
            prior = bindings.get(stream)
            if prior:
                for k, value in prior.items():
                    if value is not None and stable.get(k) != value:
                        raise Invalid("original execution binding changed")
            generation = binding.get("generation")
            if generation < generations.get(stream, generation):
                raise Invalid("provider generation went backward")
            bindings[stream] = stable
            generations[stream] = generation
        streams[stream] = event["sequence"], r["next_version"], r["to"]
        seen[eid], keys[key] = canonical(event), eid
    return streams


def main():
    contract = load_contract()
    for case in contract["cases"]:
        actual = check_transition(contract, materialize(contract, case["record"]))
        if actual != case["expected"]:
            raise Invalid(f"{case['name']}: expected {case['expected']}, got {actual}")
    print(f"PASS {FAMILY} 1.0.0: {len(contract['objects'])} objects, "
          f"{len(contract['cases'])} golden cases")


if __name__ == "__main__":
    main()
