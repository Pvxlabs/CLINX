"""Python coordinator/reference for the isolated Rust Runtime protocol.

State semantics delegate to the unmodified frozen verifier, never the Rust result.
All rows here are deterministic supplied evidence, not live observations.
"""
from __future__ import annotations

import copy
import datetime
import json
import os
import subprocess
from pathlib import Path

from scripts import check_execution_core_contract as v

PROTOCOL = "CLINX_RUNTIME_LAB_V1"
CONTRACT = v.load_contract()
FULL = set("event_id family contract_version schema_version aggregate_type aggregate_id sequence "
           "aggregate_version actor causation_id correlation_id transaction_id idempotency_scope "
           "idempotency_key payload payload_hash occurred_at recorded_at".split())
# The frozen verifier requires exact int (not bool); this Lab protocol's existing
# Rust counters are signed 64-bit. Keep that transport limit out of the V1 oracle.
MAX_LAB_INTEGER = (1 << 63) - 1


def lab_integer(value, minimum):
    return type(value) is int and minimum <= value <= MAX_LAB_INTEGER


def validate_record_versions(record):
    if (not isinstance(record, dict) or not lab_integer(record.get("version"), 0)
            or not lab_integer(record.get("next_version"), 1)):
        raise v.Invalid("REJECT")


def validate_event_counters(e):
    if (not isinstance(e, dict) or type(e.get("schema_version")) is not int
            or e["schema_version"] != 1 or not lab_integer(e.get("sequence"), 1)):
        raise v.Invalid("REJECT")
    validate_record_versions(e.get("payload"))


def offline(e):
    if set(e) != FULL:
        raise v.Invalid("REJECT")
    validate_event_counters(e)
    if not lab_integer(e["aggregate_version"], 1):
        raise v.Invalid("REJECT")
    for field in ("occurred_at", "recorded_at"):
        try:
            parsed = datetime.datetime.strptime(e[field], "%Y-%m-%dT%H:%M:%S.%fZ")
            if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != e[field]:
                raise ValueError("noncanonical UTC")
        except (ValueError, TypeError):
            raise v.Invalid("REJECT") from None
    if (any(not isinstance(e[k], str) or not e[k]
            for k in ("causation_id", "correlation_id", "transaction_id"))
            or e["aggregate_type"] != e["payload"]["object"]
            or e["aggregate_id"] != e["payload"]["id"]
            or e["aggregate_version"] != e["payload"]["next_version"]
            or e["actor"] != e["payload"]["actor"] or e["recorded_at"] < e["occurred_at"]):
        raise v.Invalid("REJECT")
    return dict(family=e["family"], contract_version=e["contract_version"],
                schema_version=e["schema_version"], event_id=e["event_id"],
                stream_id=[e["aggregate_type"], e["aggregate_id"]], sequence=e["sequence"],
                payload=e["payload"], payload_hash=e["payload_hash"],
                scope=e["idempotency_scope"], key=e["idempotency_key"])


def projection(events):
    for e in events:
        validate_event_counters(e)
    streams = v.replay(CONTRACT, events)
    return [dict(object=obj, id=oid, sequence=seq, version=version, lifecycle=state)
            for (obj, oid), (seq, version, state) in sorted(streams.items())]


def row_key(row):
    return v.canonical([row.get(k) for k in ("execution_id", "attempt_id", "call_id")])


def validate_ledger(events, old_len, deliveries):
    owners, workers, epochs, terminals, allocations = {}, {}, {}, {}, {}
    for index, e in enumerate(events):
        r, f = e["payload"], e["payload"]["facts"]
        owner = f.get("stored_owner")
        if isinstance(owner, dict):
            eid = owner["execution_id"]
            if eid in owners and owners[eid] != owner:
                raise v.Invalid("REJECT")
            if owner["worker_id"] in workers and workers[owner["worker_id"]] != owner["incarnation_id"]:
                raise v.Invalid("REJECT")
            owners[eid] = owner
            if r["object"] == "ResourceAllocation":
                allocations[r["id"]] = owner
        if r["object"] == "RuntimeWorker":
            if r["action"] == "register_new_incarnation":
                if r["id"] in workers and workers[r["id"]] != f["previous_incarnation"]:
                    raise v.Invalid("REJECT")
                workers[r["id"]] = f["presented_incarnation"]
            elif "current_incarnation" in f:
                if r["id"] in workers and workers[r["id"]] != f["current_incarnation"]:
                    raise v.Invalid("REJECT")
                workers[r["id"]] = f["current_incarnation"]
        if r["object"] == "ResourceAllocation" and r["action"] == "grant":
            owner = f["presented_owner"]
            resource = owner["resource_key"]
            if resource in epochs and f["previous_epoch"] != epochs[resource]:
                raise v.Invalid("REJECT")
            epochs[resource] = owner["resource_epoch"]
        if r["object"] == "Execution" and r["to"] in {"SUCCEEDED", "BLOCKED", "FAILED", "CANCELLED"}:
            if index >= old_len:
                for row in deliveries.values():
                    if row["execution_id"] != r["id"]:
                        continue
                    matched = next((d for d in f["deliveries"] if d["call_id"] == row["call_id"]), None)
                    if matched is None or any(matched.get(k) != row.get(k) for k in (
                            "execution_id", "attempt_id", "host_ref", "host_completion",
                            "delivery_state", "response_sent")):
                        raise v.Invalid("HOLD_UNKNOWN")
            terminals[r["id"]] = r
        if r["object"] == "ResourceAllocation" and r["action"] in {"release", "commit_release"}:
            terminal = terminals.get(f["lease_execution_id"])
            if (terminal is None or terminal["to"] != f["committed_outcome"]
                    or terminal["facts"]["binding"]["attempt_id"] != f["lease_attempt_id"]):
                raise v.Invalid("HOLD_UNKNOWN")
            owner = allocations.get(r["id"])
            if owner and (owner["execution_id"] != f["lease_execution_id"]
                          or owner["attempt_id"] != f["lease_attempt_id"]):
                raise v.Invalid("REJECT")


class Reference:
    def __init__(self):
        self.events = []
        self.deliveries = {}
        self.checkpoints = {}
        self.receipts = {}

    def snapshot(self):
        return dict(projection=projection([offline(e) for e in self.events]),
                    deliveries=copy.deepcopy(self.deliveries),
                    checkpoints=copy.deepcopy(self.checkpoints), event_count=len(self.events))

    def execute(self, request):
        try:
            if (set(request) != {"protocol_version", "request_id", "operation", "payload"}
                    or request["protocol_version"] != PROTOCOL
                    or not isinstance(request["request_id"], str)
                    or not 1 <= len(request["request_id"].encode()) <= 128):
                raise v.Invalid("REJECT")
            next_state = copy.deepcopy(self)
            result = next_state._apply(request)
            self.__dict__.update(next_state.__dict__)
            return result
        except (v.Invalid, KeyError, TypeError, ValueError) as e:
            return {"verdict": "HOLD_UNKNOWN" if str(e) == "HOLD_UNKNOWN" else "REJECT"}

    def _apply(self, request):
        op, p, rid = (request[k] for k in ("operation", "payload", "request_id"))
        expected = {"evaluate": {"record"}, "replay": {"events"}, "ack": {"row", "ack"},
                    "receipt": {"original", "request"}, "inspect": set(), "recovery": {"record"},
                    "append": {"events"}, "record_delivery": {"row"}, "late_ack": {"ack"},
                    "checkpoint": {"record"}}
        if op not in expected or set(p) != expected[op]:
            raise v.Invalid("REJECT")
        if op in {"evaluate", "checkpoint"}:
            validate_record_versions(p["record"])
        if op == "evaluate":
            return {"verdict": v.check_transition(CONTRACT, p["record"])}
        if op == "replay":
            if len(p["events"]) > 1024:
                raise v.Invalid("REJECT")
            return {"verdict": "APPLY", "projection": projection(p["events"])}
        if op == "ack":
            return {"verdict": v.reconcile_ack(p["row"], p["ack"])}
        if op == "receipt":
            return {"verdict": v.receipt_retry(p["original"], p["request"])}
        if op == "inspect":
            return {"verdict": "APPLY", "snapshot": self.snapshot()}
        if op == "recovery":
            r = p["record"]
            try:
                validate_record_versions(r)
                verdict = v.check_transition(CONTRACT, r)
            except v.Invalid:
                verdict = "REJECT"
            classification = verdict if verdict != "APPLY" else (
                "EXACT_RELEASE_ONLY" if r["object"] == "ResourceAllocation"
                else "COMMIT_ORIGINAL_TERMINAL_ONLY")
            return dict(verdict=verdict, classification=classification)
        h = v.digest(dict(operation=op, payload=p))
        if rid in self.receipts:
            prior_hash, result = self.receipts[rid]
            if prior_hash != h:
                raise v.Invalid("REJECT")
            return dict(verdict="DUPLICATE", receipt=copy.deepcopy(result))
        verdict = "APPLY"
        if op == "append":
            events = p["events"]
            if not events or len(events) + len(self.events) > 1024:
                raise v.Invalid("REJECT")
            next_events = copy.deepcopy(self.events)
            for e in events:
                offline(e)
                old = next((r for r in next_events if r["event_id"] == e["event_id"]), None)
                if old is not None:
                    if v.canonical(old) != v.canonical(e):
                        raise v.Invalid("REJECT")
                    continue
                prior = self.checkpoints.get(e["aggregate_id"])
                if (prior and e["aggregate_type"] == "Execution"
                        and "result" in e["payload"]["facts"]
                        and e["payload"]["facts"]["result"] != prior["facts"]["result"]):
                    raise v.Invalid("HOLD_UNKNOWN")
                next_events.append(copy.deepcopy(e))
            projection([offline(e) for e in next_events])
            validate_ledger(next_events, len(self.events), self.deliveries)
            if len(next_events) == len(self.events):
                verdict = "DUPLICATE"
            self.events = next_events
        elif op == "record_delivery":
            row = p["row"]
            if (v.reconcile_ack(row, {**row, "success": True}) != "APPLY"
                    or row["delivery_state"] not in {"PENDING", "FAILED"}):
                raise v.Invalid("REJECT")
            k = row_key(row)
            if k in self.deliveries:
                if row != self.deliveries[k]:
                    raise v.Invalid("REJECT")
                verdict = "DUPLICATE"
            else:
                self.deliveries[k] = copy.deepcopy(row)
        elif op == "late_ack":
            row = self.deliveries[row_key(p["ack"])]
            verdict = v.reconcile_ack(row, p["ack"])
            if verdict not in {"APPLY", "DUPLICATE"}:
                raise v.Invalid(verdict)
            if verdict == "APPLY":
                row.update(delivery_state="DELIVERED", ack_host_ref=row["host_ref"])
        elif op == "checkpoint":
            r = p["record"]
            f = r["facts"]
            if (r["object"] != "Execution" or r["from"] != "FINALIZING"
                    or r["actor"] != "finalizer" or f["finalization_phase"] != "RESULT_COMMITTED"
                    or v.check_transition(CONTRACT, r) != "HOLD_UNKNOWN"):
                raise v.Invalid("REJECT")
            result = f["result"]
            if (v.guard("binding", f, r["to"]) != "APPLY" or not result.get("result_ref")
                    or result.get("execution_id") != f["binding"]["execution_id"]
                    or result.get("attempt_id") != f["binding"]["attempt_id"]
                    or result.get("outcome") != r["to"]):
                raise v.Invalid("HOLD_UNKNOWN")
            if not any(s["object"] == "Execution" and s["id"] == r["id"]
                       and s["lifecycle"] == r["from"] and s["version"] == r["version"]
                       for s in self.snapshot()["projection"]):
                raise v.Invalid("REJECT")
            prior = self.checkpoints.get(r["id"])
            if prior and prior["facts"]["result"] != f["result"]:
                raise v.Invalid("HOLD_UNKNOWN")
            self.checkpoints[r["id"]] = copy.deepcopy(r)
            verdict = "HOLD_UNKNOWN"
        result = dict(verdict=verdict, snapshot=self.snapshot())
        self.receipts[rid] = h, copy.deepcopy(result)
        return result


def run(binary: Path, directory: Path, requests: list, *, initialize=False, crash=None):
    """Bounded one-shot lab coordinator. Never retries a failed mutation automatically."""
    data = "".join(v.canonical(r) + "\n" for r in requests)
    if len(data.encode()) > 8 * 1024 * 1024 or len(requests) > 4096:
        raise ValueError("lab batch limit")
    env = os.environ.copy()
    env.pop("CLINX_RUNTIME_LAB_CRASH", None)
    if crash:
        env["CLINX_RUNTIME_LAB_CRASH"] = crash
    command = [str(binary), "--lab-dir", str(directory)] + (["--init"] if initialize else [])
    process = subprocess.run(command, input=data, text=True, capture_output=True, timeout=30, env=env)
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    for r, response in zip(requests, responses):
        if (response["protocol_version"] != PROTOCOL or response["authority"] != "NON_AUTHORITATIVE"
                or response["request_id"] != r["request_id"]):
            raise ValueError("invalid Rust response identity")
    if process.returncode == 0 and len(responses) != len(requests):
        raise ValueError("missing Rust response")
    return process, [r["result"] for r in responses]
