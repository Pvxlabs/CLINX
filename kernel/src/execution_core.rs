//! Frozen execution-core semantics. Pure functions; evidence is supplied, never authenticated.
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::OnceLock,
};

pub const FAMILY: &str = "CLINX_EXECUTION_CORE_V1";
pub const VERSION: &str = "1.0.0";
pub const CONTRACT_BYTES: &str = include_str!("../../fixtures/execution_core_v1.json");
pub fn contract() -> &'static Value {
    static CONTRACT: OnceLock<Value> = OnceLock::new();
    CONTRACT.get_or_init(|| serde_json::from_str(CONTRACT_BYTES).expect("frozen contract JSON"))
}
// Python's frozen canonical JSON uses a different exponent threshold from serde_json.
// Keep UTF-8, sorted keys, typed integers and Python float spelling for semantic hashes.
pub fn canonical(v: &Value) -> String {
    match v {
        Value::Array(a) => format!(
            "[{}]",
            a.iter().map(canonical).collect::<Vec<_>>().join(",")
        ),
        Value::Object(o) => format!(
            "{{{}}}",
            o.iter()
                .map(|(k, v)| format!("{}:{}", serde_json::to_string(k).unwrap(), canonical(v)))
                .collect::<Vec<_>>()
                .join(",")
        ),
        Value::Number(n) if n.is_f64() => {
            let s = n.to_string();
            let (sign, s) = if let Some(s) = s.strip_prefix('-') {
                ("-", s)
            } else {
                ("", s.as_str())
            };
            let parts: Vec<_> = s.split(['e', 'E']).collect();
            let power = parts
                .get(1)
                .and_then(|p| p.parse::<i32>().ok())
                .unwrap_or(0);
            let mantissa = parts[0];
            let before = mantissa.find('.').unwrap_or(mantissa.len()) as i32;
            let digits: String = mantissa.chars().filter(|c| c.is_ascii_digit()).collect();
            let zeroes = digits.chars().take_while(|c| *c == '0').count();
            let digits = digits[zeroes..].trim_end_matches('0');
            if digits.is_empty() {
                return format!("{sign}0.0");
            }
            let exponent = before - zeroes as i32 - 1 + power;
            if !(-4..16).contains(&exponent) {
                let fraction = if digits.len() > 1 {
                    format!(".{}", &digits[1..])
                } else {
                    String::new()
                };
                format!(
                    "{sign}{}{fraction}e{}{:02}",
                    &digits[..1],
                    if exponent < 0 { "-" } else { "+" },
                    exponent.abs()
                )
            } else {
                let position = exponent + 1;
                if position <= 0 {
                    format!("{sign}0.{}{}", "0".repeat((-position) as usize), digits)
                } else if position as usize >= digits.len() {
                    format!(
                        "{sign}{digits}{}.0",
                        "0".repeat(position as usize - digits.len())
                    )
                } else {
                    format!(
                        "{sign}{}.{}",
                        &digits[..position as usize],
                        &digits[position as usize..]
                    )
                }
            }
        }
        _ => serde_json::to_string(v).unwrap(),
    }
}
pub fn digest(v: &Value) -> String {
    format!("{:x}", Sha256::digest(canonical(v).as_bytes()))
}
fn fields(s: &str) -> BTreeSet<&str> {
    s.split_whitespace().collect()
}
pub fn keys(v: &Value, s: &str) -> bool {
    v.as_object()
        .is_some_and(|o| o.keys().map(String::as_str).collect::<BTreeSet<_>>() == fields(s))
}
// Match the frozen Python verifier's JSON equality (including its numeric comparisons).
fn py_eq(a: &Value, b: &Value) -> bool {
    enum Numeric {
        Integer(i128),
        Float(f64),
    }
    let numeric = |v: &Value| match v {
        Value::Bool(b) => Some(Numeric::Integer(i128::from(*b))),
        Value::Number(n) if n.is_f64() => n.as_f64().map(Numeric::Float),
        Value::Number(n) => n
            .as_i64()
            .map(i128::from)
            .or_else(|| n.as_u64().map(i128::from))
            .map(Numeric::Integer),
        _ => None,
    };
    match (a, b) {
        (Value::Object(a), Value::Object(b)) => {
            a.len() == b.len() && a.iter().all(|(k, v)| b.get(k).is_some_and(|w| py_eq(v, w)))
        }
        (Value::Array(a), Value::Array(b)) => {
            a.len() == b.len() && a.iter().zip(b).all(|(v, w)| py_eq(v, w))
        }
        _ => match (numeric(a), numeric(b)) {
            (Some(Numeric::Integer(a)), Some(Numeric::Integer(b))) => a == b,
            (Some(Numeric::Float(a)), Some(Numeric::Float(b))) => a == b,
            (Some(Numeric::Float(f)), Some(Numeric::Integer(i)))
            | (Some(Numeric::Integer(i)), Some(Numeric::Float(f))) => {
                f.fract() == 0.0 && f as i128 == i
            }
            _ => a == b,
        },
    }
}
fn text(v: &Value) -> bool {
    v.as_str().is_some_and(|s| !s.is_empty())
}
fn int(v: &Value) -> Option<i64> {
    v.as_i64()
}
fn num(v: &Value) -> Option<f64> {
    v.as_f64().filter(|n| n.is_finite())
}
fn truth(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
        Value::Number(n) => n.as_f64() != Some(0.0),
    }
}
const BINDING: &str =
    "execution_id attempt_id provider_session_id thread_id turn_id operation_id generation";
const OWNER: &str =
    "execution_id attempt_id assignment_id worker_id incarnation_id resource_key resource_epoch";
const FACTS: &str = "unresolved_execution_ids commit_bundle binding observation now stored_owner presented_owner safety_pending stored_versions expected_versions expires_at allocation_state assignment_state previous_epoch used_units capacity_units capacity unresolved_allocation_ids current_incarnation presented_incarnation old_writer_stopped_ref side_effect_fenced_ref dispatch_certainty non_dispatch_proof retry_policy delivery_ledger_complete deliveries result committed_outcome finalization_phase cancel_requested lease_execution_id lease_attempt_id previous_incarnation previous_generation next_generation old_assignments_quarantined extensions";
fn identity(v: &Value, names: &str) -> bool {
    keys(v, names)
        && v.as_object().unwrap().iter().all(|(k, v)| {
            if k == "generation" || k == "resource_epoch" {
                int(v).is_some_and(|n| n > 0)
            } else {
                text(v)
            }
        })
}
fn bundle(f: &Value, names: &str) -> bool {
    f["commit_bundle"].as_array().is_some_and(|a| {
        a.len() == fields(names).len()
            && a.iter().all(Value::is_string)
            && a.iter()
                .map(|x| x.as_str().unwrap())
                .collect::<BTreeSet<_>>()
                == fields(names)
    })
}
fn verdict(b: bool) -> &'static str {
    if b { "APPLY" } else { "REJECT" }
}
fn known(b: bool) -> &'static str {
    if b { "APPLY" } else { "HOLD_UNKNOWN" }
}
fn outcome(s: &Value) -> bool {
    matches!(
        s.as_str(),
        Some("SUCCEEDED" | "BLOCKED" | "FAILED" | "CANCELLED")
    )
}
pub fn guard(name: &str, f: &Value, target: &Value) -> &'static str {
    match name {
        "children_resolved" => known(f["unresolved_execution_ids"] == json!([])),
        "admission" => verdict(bundle(
            f,
            "execution request prepared owner event receipt outbox",
        )),
        "binding" => {
            let mut b = f["binding"].clone();
            if b.is_object() && b["turn_id"].is_null() && b.get("turn_id").is_some() {
                b["turn_id"] = json!("not-yet-dispatched");
            }
            known(identity(&b, BINDING))
        }
        "provider_live" | "provider_terminal" => {
            let o = &f["observation"];
            if guard("binding", f, target) != "APPLY"
                || !o.is_object()
                || !identity(&f["binding"], BINDING)
                || !py_eq(&o["binding"], &f["binding"])
                || name == "provider_terminal" && o["complete"] != true
            {
                return "HOLD_UNKNOWN";
            }
            let (Some(now), Some(observed)) = (num(&f["now"]), num(&o["observed_at"])) else {
                return "HOLD_UNKNOWN";
            };
            if !(0.0..=120.0).contains(&(now - observed)) {
                return "HOLD_UNKNOWN";
            }
            known(if name == "provider_live" {
                o["liveness"] == "LIVE" && py_eq(&o["active_claims"], &json!(1))
            } else {
                o["liveness"] == "TERMINAL" && py_eq(&o["active_claims"], &json!(0))
            })
        }
        "fence" => {
            if !identity(&f["stored_owner"], OWNER) {
                return "HOLD_UNKNOWN";
            }
            if !py_eq(&f["presented_owner"], &f["stored_owner"]) || f["safety_pending"] != false {
                return "REJECT";
            }
            let v = &f["stored_versions"];
            if !keys(v, "assignment allocation resource")
                || !v
                    .as_object()
                    .unwrap()
                    .values()
                    .all(|x| int(x).is_some_and(|n| n >= 0))
                || !py_eq(&f["expected_versions"], v)
            {
                return "REJECT";
            }
            let (Some(now), Some(expires)) = (num(&f["now"]), num(&f["expires_at"])) else {
                return "HOLD_UNKNOWN";
            };
            verdict(
                now < expires
                    && matches!(f["allocation_state"].as_str(), Some("HELD" | "ACTIVE"))
                    && f["assignment_state"] == "ACTIVE",
            )
        }
        "grant" => {
            if !identity(&f["presented_owner"], OWNER) {
                return "REJECT";
            }
            let (Some(p), Some(u), Some(n), Some(c)) = (
                int(&f["previous_epoch"]),
                int(&f["used_units"]),
                int(&f["capacity_units"]),
                int(&f["capacity"]),
            ) else {
                return "REJECT";
            };
            if p < 0
                || p.checked_add(1) != int(&f["presented_owner"]["resource_epoch"])
                || u < 0
                || n < 1
                || u.checked_add(n).is_none_or(|sum| c < sum)
                || f["unresolved_allocation_ids"] != json!([])
            {
                return "REJECT";
            }
            guard("fence", f, target)
        }
        "expired" => match (num(&f["now"]), num(&f["expires_at"])) {
            (Some(n), Some(e)) => verdict(n >= e),
            _ => "HOLD_UNKNOWN",
        },
        "incarnation" => verdict(
            text(&f["current_incarnation"])
                && f["presented_incarnation"] == f["current_incarnation"],
        ),
        "new_incarnation" => verdict(
            int(&f["previous_generation"])
                .is_some_and(|p| p > 0 && p.checked_add(1) == int(&f["next_generation"]))
                && text(&f["presented_incarnation"])
                && f["presented_incarnation"] != f["previous_incarnation"]
                && f["old_assignments_quarantined"] == true,
        ),
        "stopped" => {
            known(text(&f["old_writer_stopped_ref"]) && text(&f["side_effect_fenced_ref"]))
        }
        "not_dispatched" => {
            let p = &f["non_dispatch_proof"];
            known(
                f["dispatch_certainty"] == "NOT_DISPATCHED"
                    && p.is_object()
                    && guard("binding", f, target) == "APPLY"
                    && p["source"] == "dispatch_boundary"
                    && p["boundary"] == "PRE_SEND"
                    && truth(&p["evidence_ref"])
                    && p["execution_id"] == f["binding"]["execution_id"]
                    && p["attempt_id"] == f["binding"]["attempt_id"],
            )
        }
        "retry" => {
            for g in ["not_dispatched", "stopped"] {
                let v = guard(g, f, target);
                if v != "APPLY" {
                    return v;
                }
            }
            let p = &f["retry_policy"];
            verdict(
                p["allowed"] == true
                    && int(&p["remaining"]).is_some_and(|n| n > 0)
                    && p["cancel_requested"] == false
                    && matches!((num(&f["now"]),num(&p["deadline_at"])),(Some(n),Some(d)) if n<d),
            )
        }
        "deliveries" => {
            let Some(rows) = f["deliveries"].as_array() else {
                return "HOLD_UNKNOWN";
            };
            if f["delivery_ledger_complete"] != true || guard("binding", f, target) != "APPLY" {
                return "HOLD_UNKNOWN";
            }
            let mut seen = BTreeSet::new();
            for r in rows {
                if !r.is_object()
                    || !truth(&r["call_id"])
                    || r["call_id"].is_array()
                    || r["call_id"].is_object()
                    || !seen.insert(r["call_id"].to_string())
                {
                    return "REJECT";
                }
                if ["execution_id", "attempt_id"]
                    .iter()
                    .any(|k| r[k] != f["binding"][k])
                {
                    return "REJECT";
                }
                let res = &r["resolution"];
                let out = f
                    .get("committed_outcome")
                    .or_else(|| f["result"].get("outcome"))
                    .unwrap_or(target);
                if r["delivery_state"] == "FAILED"
                    && res.is_object()
                    && res["method"] == "EXISTING_AUTHORIZED_RECONCILIATION"
                    && truth(&res["evidence_ref"])
                    && r["host_completion"] == "KNOWN"
                    && matches!(out.as_str(), Some("BLOCKED" | "FAILED" | "CANCELLED"))
                    && ["execution_id", "attempt_id", "host_ref"]
                        .iter()
                        .all(|k| res[k] == r[k])
                {
                    continue;
                }
                if r["host_completion"] != "KNOWN"
                    || !truth(&r["host_ref"])
                    || r["delivery_state"] != "DELIVERED"
                    || r["ack_host_ref"] != r["host_ref"]
                    || r["response_sent"] != true
                {
                    return "HOLD_UNKNOWN";
                }
            }
            "APPLY"
        }
        "finalizer" | "release" => {
            let pre = f["dispatch_certainty"] == "NOT_DISPATCHED";
            for g in [
                if pre {
                    "not_dispatched"
                } else {
                    "provider_terminal"
                },
                "deliveries",
            ] {
                let v = guard(g, f, target);
                if v != "APPLY" {
                    return v;
                }
            }
            let r = &f["result"];
            if !r.is_object()
                || !truth(&r["result_ref"])
                || ["execution_id", "attempt_id"]
                    .iter()
                    .any(|k| r[k] != f["binding"][k])
            {
                return "REJECT";
            }
            let out = if name == "finalizer" {
                target
            } else {
                &f["committed_outcome"]
            };
            if !outcome(out) {
                return "REJECT";
            }
            if r["outcome"] != *out
                || pre && out == "SUCCEEDED"
                || out == "CANCELLED" && f["cancel_requested"] != true
                || f["finalization_phase"] != "TERMINAL_COMMITTED"
            {
                return "HOLD_UNKNOWN";
            }
            if !bundle(f, "result terminal event receipt outbox") {
                return "REJECT";
            }
            if name == "release" {
                if f["lease_execution_id"] != f["binding"]["execution_id"]
                    || f["lease_attempt_id"] != f["binding"]["attempt_id"]
                {
                    return "REJECT";
                }
                return guard("stopped", f, target);
            }
            "APPLY"
        }
        "event_bundle" => verdict(bundle(f, "state event receipt outbox")),
        _ => "REJECT",
    }
}

pub fn transition(r: &Value) -> &'static str {
    let Some(o) = r.as_object() else {
        return "REJECT";
    };
    let req = fields("object id from to action actor version next_version facts");
    if !req.iter().all(|k| o.contains_key(*k))
        || o.keys()
            .any(|k| !req.contains(k.as_str()) && k != "extensions")
    {
        return "REJECT";
    }
    // Both counters must be typed, in-range Lab integers before arithmetic.
    // Never compare Options: overflow and invalid input must not equal None.
    let (Some(version), Some(next_version)) = (int(&r["version"]), int(&r["next_version"])) else {
        return "REJECT";
    };
    if !text(&r["id"]) || version < 0 || next_version < 1 {
        return "REJECT";
    }
    let Some(incremented) = version.checked_add(1) else {
        return "REJECT";
    };
    if incremented != next_version {
        return "REJECT";
    }
    let c = contract();
    let Some(obj) = r["object"].as_str().and_then(|s| c["objects"].get(s)) else {
        return "REJECT";
    };
    let edges: Vec<_> = obj["transitions"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|e| {
            ["from", "to", "action", "actor"]
                .iter()
                .all(|k| e[k] == r[k])
        })
        .collect();
    let f = &r["facts"];
    if edges.len() != 1
        || !f.is_object()
        || f.as_object()
            .unwrap()
            .keys()
            .any(|k| !fields(FACTS).contains(k.as_str()))
    {
        return "REJECT";
    }
    let b = &f["binding"];
    if b.is_object() {
        let k = match r["object"].as_str() {
            Some("Execution") => Some("execution_id"),
            Some("Attempt") => Some("attempt_id"),
            Some("ProviderSession") => Some("provider_session_id"),
            _ => None,
        };
        if k.is_some_and(|k| b[k] != r["id"])
            || f["stored_owner"].is_object()
                && ["execution_id", "attempt_id"]
                    .iter()
                    .any(|k| f["stored_owner"][k] != b[k])
        {
            return "REJECT";
        }
    }
    let vs: Vec<_> = edges[0]["guards"]
        .as_array()
        .unwrap()
        .iter()
        .map(|g| guard(g.as_str().unwrap(), f, &r["to"]))
        .collect();
    if vs.contains(&"REJECT") {
        "REJECT"
    } else if vs.contains(&"HOLD_UNKNOWN") {
        "HOLD_UNKNOWN"
    } else {
        "APPLY"
    }
}

pub fn reconcile_ack(row: &Value, ack: &Value) -> &'static str {
    let names = fields(
        "execution_id attempt_id owner_ref thread_id turn_id generation call_id namespace tool operation_id host_ref",
    );
    if names.iter().any(|k| row[k].is_null()) || !truth(&row["response_sent"]) {
        return "HOLD_UNKNOWN";
    }
    if row["reconciliation_state"] == "RESOLVED" && row["delivery_state"] != "DELIVERED" {
        return "REJECT";
    }
    if names.iter().any(|k| {
        row[k] != ack[k] || row[k].is_number() && int(&row[k]).is_some() != int(&ack[k]).is_some()
    }) {
        return "REJECT";
    }
    if ack["success"] != true {
        return "HOLD_UNKNOWN";
    }
    if row["delivery_state"] == "DELIVERED" {
        "DUPLICATE"
    } else {
        "APPLY"
    }
}

pub fn receipt_retry(original: &Value, request: &Value) -> &'static str {
    if ["scope", "key", "request_hash"]
        .iter()
        .any(|k| original.get(k).is_none())
        || ["scope", "key", "semantics"]
            .iter()
            .any(|k| request.get(k).is_none())
    {
        return "REJECT";
    }
    if original["scope"] == request["scope"]
        && original["key"] == request["key"]
        && original["request_hash"] == digest(&request["semantics"])
    {
        "DUPLICATE"
    } else {
        "REJECT"
    }
}

pub fn replay(events: &[Value]) -> Result<Value, String> {
    let c = contract();
    let mut streams: BTreeMap<(String, String), (i64, i64, Value)> = BTreeMap::new();
    let mut seen = BTreeMap::new();
    let mut ids = BTreeSet::new();
    let mut bindings: BTreeMap<(String, String), Value> = BTreeMap::new();
    let mut generations: BTreeMap<(String, String), i64> = BTreeMap::new();
    for e in events {
        if !keys(
            e,
            "family contract_version schema_version event_id stream_id sequence payload payload_hash scope key",
        ) || e["family"] != FAMILY
            || e["contract_version"] != VERSION
            || int(&e["schema_version"]) != Some(1)
            || ["event_id", "scope", "key"].iter().any(|k| !text(&e[k]))
        {
            return Err("REJECT".into());
        }
        if e["payload_hash"] != digest(&e["payload"]) {
            return Err("REJECT".into());
        }
        let eid = e["event_id"].as_str().unwrap();
        if let Some(prior) = seen.get(eid) {
            if canonical(prior) != canonical(e) {
                return Err("REJECT".into());
            }
            continue;
        }
        if !ids.insert((e["scope"].to_string(), e["key"].to_string())) {
            return Err("REJECT".into());
        }
        let r = &e["payload"];
        let (Some(name), Some(id)) = (r["object"].as_str(), r["id"].as_str()) else {
            return Err("REJECT".into());
        };
        let Some(obj) = c["objects"].get(name) else {
            return Err("REJECT".into());
        };
        let stream = (name.to_owned(), id.to_owned());
        let initial = if name == "Event" {
            json!("ABSENT")
        } else {
            obj["initial"].clone()
        };
        let (seq, version, state) = streams.get(&stream).cloned().unwrap_or((0, 0, initial));
        if e["stream_id"] != json!([name, id])
            || int(&e["sequence"]) != Some(seq + 1)
            || int(&r["version"]) != Some(version)
            || r["from"] != state
        {
            return Err("REJECT".into());
        }
        let v = transition(r);
        if v != "APPLY" {
            return Err(v.into());
        }
        let b = &r["facts"]["binding"];
        if truth(b) {
            let Some(g) = int(&b["generation"]) else {
                return Err("REJECT".into());
            };
            if bindings.get(&stream).is_some_and(|prior| {
                prior
                    .as_object()
                    .unwrap()
                    .iter()
                    .any(|(k, v)| k != "generation" && !v.is_null() && b[k] != *v)
            }) {
                return Err("REJECT".into());
            }
            if generations.get(&stream).is_some_and(|old| g < *old) {
                return Err("REJECT".into());
            }
            bindings.insert(stream.clone(), b.clone());
            generations.insert(stream.clone(), g);
        }
        streams.insert(
            stream,
            (seq + 1, int(&r["next_version"]).unwrap(), r["to"].clone()),
        );
        seen.insert(eid, e.clone());
    }
    Ok(Value::Array(streams.into_iter().map(|((object,id),(seq,version,state))|json!({"object":object,"id":id,"sequence":seq,"version":version,"lifecycle":state})).collect()))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn frozen_goldens() {
        let c = contract();
        assert_eq!(c["cases"].as_array().unwrap().len(), 35);
        for case in c["cases"].as_array().unwrap() {
            let mut r = case["record"].clone();
            let mut f = json!({});
            for name in r["facts_refs"].as_array().unwrap() {
                for (k, v) in c["contexts"][name.as_str().unwrap()].as_object().unwrap() {
                    f[k] = v.clone();
                }
            }
            for (k, v) in r["facts"].as_object().unwrap() {
                f[k] = v.clone();
            }
            r.as_object_mut().unwrap().remove("facts_refs");
            r["facts"] = f;
            assert_eq!(transition(&r), case["expected"], "{}", case["name"]);
        }
    }
    #[test]
    fn empty_replay_has_no_authority() {
        assert_eq!(replay(&[]).unwrap(), json!([]));
    }
    #[test]
    fn hash_utf8_sorted() {
        assert_eq!(
            digest(&json!({"z":1,"a":"中文"})),
            "72732b21080425bbd25c22f698ff1f85eb9d348ae95381d61f3fd3c04279198c"
        );
    }
    #[test]
    fn finite_float_roundtrip_and_exponent_spelling() {
        for raw in [
            "-9.71589973258006e+140",
            "-1.2628007997939483",
            "5.314627079144341e+195",
            "1e-05",
            "1e+16",
            "-0.0",
        ] {
            let value: Value = serde_json::from_str(raw).unwrap();
            assert_eq!(canonical(&value), raw);
        }
    }
    #[test]
    fn expiry_equal_boundary_and_pending_guard_are_rejected() {
        let mut f = contract()["contexts"]["fenced"].clone();
        assert_eq!(guard("fence", &f, &json!("RUNNING")), "APPLY");
        f["now"] = json!(110);
        assert_eq!(guard("fence", &f, &json!("RUNNING")), "REJECT");
        f["now"] = json!(100);
        f["safety_pending"] = json!(true);
        assert_eq!(guard("fence", &f, &json!("RUNNING")), "REJECT");
    }
    #[test]
    fn partial_result_never_finalizes_or_releases() {
        let mut f = contract()["contexts"]["release"].clone();
        f["finalization_phase"] = json!("RESULT_COMMITTED");
        assert_eq!(guard("finalizer", &f, &json!("SUCCEEDED")), "HOLD_UNKNOWN");
        assert_eq!(
            guard("release", &f, &json!("RELEASE_PENDING")),
            "HOLD_UNKNOWN"
        );
        f["finalization_phase"] = json!("TERMINAL_COMMITTED");
        f["commit_bundle"] = json!(["result", "terminal", "event"]);
        assert_eq!(guard("release", &f, &json!("RELEASE_PENDING")), "REJECT");
    }
    #[test]
    fn typed_ack_and_unknown_dispatch_do_not_create_authority() {
        let row = json!({"execution_id":"e1","attempt_id":"a1","owner_ref":"o1","thread_id":"t1","turn_id":"turn1","generation":3,"call_id":1,"namespace":"tools","tool":"host","operation_id":"op1","host_ref":"h1","response_sent":true,"delivery_state":"PENDING"});
        let original = row.clone();
        let mut ack = row.clone();
        ack["success"] = json!(true);
        assert_eq!(reconcile_ack(&row, &ack), "APPLY");
        ack["call_id"] = json!("1");
        assert_eq!(reconcile_ack(&row, &ack), "REJECT");
        assert_eq!(row, original);
        let mut f = contract()["contexts"]["retry"].clone();
        f["dispatch_certainty"] = json!("UNKNOWN");
        assert_eq!(guard("retry", &f, &json!("PENDING")), "HOLD_UNKNOWN");
    }
    #[test]
    fn large_integer_cas_is_not_rounded_through_float() {
        assert!(!py_eq(
            &json!(9007199254740992_i64),
            &json!(9007199254740993_i64)
        ));
        assert!(!py_eq(
            &json!(9007199254740992.0_f64),
            &json!(9007199254740993_i64)
        ));
        assert!(py_eq(&json!(2.0), &json!(2)));
    }
}
