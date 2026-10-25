//! Exact node observation routing, native cursor envelopes and read projection.
//! Callback IO uses existing identities; no failover or replay of execution input.
use crate::{
    node_scope::{self, PROTOCOL, PROVIDER, Scope},
    node_tls::Result,
    read_projection::{text, truth},
};
use base64::{
    Engine,
    engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD},
};
use hmac::{Hmac, Mac};
use serde_json::{Value, json};
use sha2::Sha256;
use std::{
    collections::{BTreeSet, VecDeque},
    sync::{Arc, Mutex, mpsc},
    thread,
    time::{Duration, Instant},
};
use unicode_casefold::UnicodeCaseFold;
fn key(a: &Value) -> Result<Vec<u8>> {
    let s = text(&a["secret_hex"]);
    if !s.len().is_multiple_of(2) || !s.is_ascii() {
        return Err("INVALID_CONTEXT_CURSOR".into());
    }
    (0..s.len())
        .step_by(2)
        .map(|n| u8::from_str_radix(&s[n..n + 2], 16).map_err(|_| "INVALID_CONTEXT_CURSOR".into()))
        .collect()
}
fn cursor_body(a: &Value, version: i64, payload: Value) -> Value {
    json!([
        version,
        a["node_id"],
        a["user_scope"],
        a["provider"],
        a["native_thread_id"],
        a["source"],
        a["source_version"],
        payload
    ])
}
fn cursor_encode(a: &Value, provider: bool) -> Result<Value> {
    let p = if provider {
        &a["provider_cursor"]
    } else {
        &a["offset"]
    };
    if provider {
        if !p
            .as_str()
            .is_some_and(|s| !s.is_empty() && s.chars().count() <= 4096)
        {
            return Err("INVALID_PROVIDER_CURSOR".into());
        }
    } else if !(p.as_u64().is_some() || p.is_boolean()) {
        return Err("INVALID_CURSOR_OFFSET".into());
    }
    let mut wire = cursor_body(a, if provider { 2 } else { 1 }, p.clone())
        .to_string()
        .into_bytes();
    let key = key(a)?;
    if !key.is_empty() {
        let mut mac = Hmac::<Sha256>::new_from_slice(&key).map_err(|_| "INVALID_CONTEXT_CURSOR")?;
        mac.update(&wire);
        let signature = mac
            .finalize()
            .into_bytes()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>();
        wire.push(b'.');
        wire.extend(signature.as_bytes());
    }
    Ok(json!(URL_SAFE_NO_PAD.encode(wire)))
}
fn cursor_decode(a: &Value, provider: bool) -> Result<Value> {
    let cursor = a["cursor"]
        .as_str()
        .filter(|s| s.chars().count() <= if provider { 8192 } else { 4096 })
        .ok_or("INVALID_CONTEXT_CURSOR")?;
    let mut raw = URL_SAFE_NO_PAD
        .decode(cursor.trim_end_matches('='))
        .or_else(|_| STANDARD.decode(cursor))
        .map_err(|_| "INVALID_CONTEXT_CURSOR")?;
    let key = key(a)?;
    if !key.is_empty() {
        let at = raw
            .iter()
            .rposition(|b| *b == b'.')
            .ok_or("INVALID_CONTEXT_CURSOR")?;
        let signature = &raw[at + 1..];
        if signature.len() != 64
            || !signature
                .iter()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
        {
            return Err("INVALID_CONTEXT_CURSOR".into());
        }
        let sig = (0..64)
            .step_by(2)
            .map(|n| {
                u8::from_str_radix(std::str::from_utf8(&signature[n..n + 2]).unwrap(), 16).unwrap()
            })
            .collect::<Vec<_>>();
        let mut mac = Hmac::<Sha256>::new_from_slice(&key).map_err(|_| "INVALID_CONTEXT_CURSOR")?;
        mac.update(&raw[..at]);
        mac.verify_slice(&sig)
            .map_err(|_| "INVALID_CONTEXT_CURSOR")?;
        raw.truncate(at);
    }
    let v: Value = serde_json::from_slice(&raw).map_err(|_| "INVALID_CONTEXT_CURSOR")?;
    let rows = v
        .as_array()
        .filter(|v| v.len() == 8)
        .ok_or("INVALID_CONTEXT_CURSOR")?;
    let version = rows[0].as_i64().ok_or("INVALID_CONTEXT_CURSOR")?;
    let expected = cursor_body(a, version, rows[7].clone());
    if expected != v {
        return Err("INVALID_CONTEXT_CURSOR".into());
    }
    if version == 1 && rows[7].as_u64().is_some() {
        return Ok(if provider {
            Value::Null
        } else {
            rows[7].clone()
        });
    }
    if provider && version == 2 && rows[7].as_str().is_some_and(|s| !s.is_empty()) {
        return Ok(rows[7].clone());
    }
    Err("INVALID_CONTEXT_CURSOR".into())
}
pub fn command(op: &str, a: &Value) -> Result<Value> {
    match op {
        "cursor.encode" => cursor_encode(a, false),
        "cursor.decode" => cursor_decode(a, false),
        "cursor.provider_encode" => cursor_encode(a, true),
        "cursor.provider_decode" => cursor_decode(a, true),
        "router.found" => Ok(json!(found(&a["result"], &a["thread_id"]))),
        "router.answered" => Ok(json!(answered(&a["result"], &a["thread_id"]))),
        _ => Err("NODE_ROUTER_OPERATION_UNKNOWN".into()),
    }
}
fn found(r: &Value, tid: &Value) -> bool {
    r["queried_thread_id"] == *tid
        && matches!(
            text(&r["lookup_status"]),
            "RESOLVED" | "THREAD_UNBOUND" | "AVAILABLE" | "FOUND"
        )
        && r["provider_existence"] != "NOT_FOUND"
        && !truth(&r["error_code"])
}
fn answered(r: &Value, tid: &Value) -> bool {
    found(r, tid)
        || (r["queried_thread_id"] == *tid
            && r["lookup_status"] == "THREAD_NOT_FOUND"
            && r["provider_existence"] == "NOT_FOUND"
            && (r["error_code"].is_null() || r["error_code"] == "THREAD_NOT_FOUND"))
}
fn permitted(record: &Value, user: &Value) -> bool {
    record["user_scope"] == *user && record["authorized"] == true && record["trusted"] == true
}
fn local(record: &Value, a: &Value) -> bool {
    record["node_id"] == a["local_node_id"]
        && a["local_readers"]
            .as_array()
            .is_some_and(|v| v.contains(&record["node_id"]))
}
fn target(a: &Value, call: &impl Fn(&str, Value) -> Result<Value>) -> Result<Value> {
    if truth(&a["node_id"]) {
        let r = call("registry.get", json!({"node_id":a["node_id"]}))?;
        return if r.is_null() {
            Err("UNKNOWN_NODE".into())
        } else {
            Ok(r)
        };
    }
    if truth(&a["host"]) {
        let host = text(&a["host"]);
        let records = call(
            "registry.list_nodes",
            json!({"user_scope":a["user_scope"],"authorized_only":true}),
        )?;
        let matches = records
            .as_array()
            .ok_or("NODE_RESPONSE_INVALID")?
            .iter()
            .filter(|r| {
                [&r["node_id"], &r["display_name"]]
                    .into_iter()
                    .chain(r["route_ids"].as_array().into_iter().flatten())
                    .any(|v| text(v).case_fold().eq(host.case_fold()))
            })
            .cloned()
            .collect::<Vec<_>>();
        return match matches.len() {
            0 => Err("UNKNOWN_THREAD_HOST".into()),
            1 => Ok(matches[0].clone()),
            _ => Err("THREAD_HOST_CONFLICT".into()),
        };
    }
    Err("NODE_TARGET_REQUIRED".into())
}
fn merge(mut v: Value, extra: Value) -> Result<Value> {
    let out = v.as_object_mut().ok_or("NODE_RESPONSE_INVALID")?;
    out.extend(extra.as_object().unwrap().clone());
    Ok(v)
}
fn ask(
    record: &Value,
    a: &Value,
    request: &Value,
    call: &(impl Fn(&str, Value) -> Result<Value> + Sync),
) -> (Value, Value, bool) {
    if (node_scope::stale(record) || record["state"] == "OFFLINE") && !local(record, a) {
        return (record.clone(), Value::Null, false);
    }
    let result = (|| -> Result<Value> {
        let available = call("router.reader", json!({"record":record}))?;
        if available != true {
            return Err("NODE_READER_UNAVAILABLE".into());
        }
        let response = call("router.read", json!({"record":record,"request":request}))?;
        if let Some(result) = response.get("result") {
            if answered(result, &request["thread_id"]) {
                call("registry.touch", json!({"node_id":record["node_id"]}))?;
            }
            Ok(result.clone())
        } else {
            Err("NODE_READ_UNAVAILABLE".into())
        }
    })();
    match result {
        Ok(r) => {
            let good = answered(&r, &request["thread_id"]);
            (record.clone(), r, good)
        }
        Err(_) => (record.clone(), Value::Null, false),
    }
}
fn read(a: &Value, call: &(impl Fn(&str, Value) -> Result<Value> + Sync)) -> Result<Value> {
    call("registry.refresh_states", json!({}))?;
    let mut request = json!({"operation":"session.read","protocol_version":PROTOCOL,"thread_id":a["thread_id"],"user_scope":a["user_scope"],"provider":a.get("provider").cloned().unwrap_or(json!(PROVIDER)),"recent_turns":a.get("recent_turns").cloned().unwrap_or(json!(8)),"max_bytes":a.get("max_bytes").cloned().unwrap_or(json!(32000))});
    if !a["cursor"].is_null() {
        request["cursor"] = a["cursor"].clone();
    }
    if truth(&a["node_id"]) || truth(&a["host"]) {
        let r = target(a, call)?;
        let id = &r["node_id"];
        if !permitted(&r, &a["user_scope"]) {
            return Ok(
                json!({"error_code":"SHARING_SCOPE_DENIED","lookup_status":"SHARING_SCOPE_DENIED","coverage":{"requested_node_id":id}}),
            );
        }
        let reader = call("router.reader", json!({"record":r}))?;
        if reader != true || ((node_scope::stale(&r) || r["state"] == "OFFLINE") && !local(&r, a)) {
            return Ok(
                json!({"error_code":"NODE_OFFLINE","lookup_status":"NODE_OFFLINE","coverage":{"requested_node_id":id,"stale":node_scope::stale(&r)}}),
            );
        }
        let response = call("router.read", json!({"record":r,"request":request}))?;
        let Some(result) = response.get("result") else {
            return Ok(
                json!({"error_code":"NODE_UNAVAILABLE","lookup_status":"NODE_UNAVAILABLE","coverage":{"requested_node_id":id}}),
            );
        };
        let complete = answered(result, &a["thread_id"]);
        if complete {
            call("registry.touch", json!({"node_id":id}))?;
        }
        return merge(
            result.clone(),
            json!({"source_node_id":id,"coverage":{"requested_node_id":id,"complete":complete,"nodes_queried":[id]}}),
        );
    }
    let records = call(
        "registry.list_nodes",
        json!({"user_scope":a["user_scope"],"authorized_only":true}),
    )?;
    let records = records.as_array().ok_or("NODE_RESPONSE_INVALID")?;
    if records.is_empty() {
        return Ok(
            json!({"error_code":"THREAD_LOOKUP_UNAVAILABLE","lookup_status":"THREAD_LOOKUP_UNAVAILABLE","coverage":{"complete":false,"nodes_queried":[],"reason":"NO_AUTHORIZED_NODES"}}),
        );
    }
    let queue = Arc::new(Mutex::new(VecDeque::from(records.clone())));
    let (tx, rx) = mpsc::channel();
    let workers = (a["max_workers"].as_u64().unwrap_or(4) as usize)
        .clamp(1, 16)
        .min(records.len());
    let timeout = a["timeout_seconds"]
        .as_f64()
        .filter(|v| v.is_finite())
        .unwrap_or(2.0)
        .max(0.05);
    let deadline = Instant::now() + Duration::from_secs_f64(timeout);
    let mut results = Vec::new();
    thread::scope(|scope| {
        for _ in 0..workers {
            let queue = queue.clone();
            let tx = tx.clone();
            let request = &request;
            scope.spawn(move || {
                loop {
                    let record = queue.lock().unwrap().pop_front();
                    let Some(record) = record else {
                        break;
                    };
                    if tx.send(ask(&record, a, request, call)).is_err() {
                        break;
                    }
                }
            });
        }
        drop(tx);
        while results.len() < records.len() {
            let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
                break;
            };
            match rx.recv_timeout(remaining) {
                Ok(r) => results.push(r),
                Err(_) => break,
            }
        }
        if results.len() < records.len() {
            queue.lock().unwrap().clear();
        }
    });
    let queried = results
        .iter()
        .map(|(r, _, _)| text(&r["node_id"]).to_owned())
        .collect::<BTreeSet<_>>();
    let authorized = records
        .iter()
        .map(|r| text(&r["node_id"]).to_owned())
        .collect::<BTreeSet<_>>();
    let mut unavailable = results
        .iter()
        .filter(|(_, r, good)| r.is_null() || !*good)
        .map(|(r, _, _)| text(&r["node_id"]).to_owned())
        .collect::<BTreeSet<_>>();
    unavailable.extend(authorized.difference(&queried).cloned());
    let coverage = json!({"complete":unavailable.is_empty()&&results.len()==records.len(),"nodes_queried":queried,"nodes_unavailable":unavailable,"nodes_authorized":authorized});
    let found = results
        .iter()
        .filter(|(_, r, _)| !r.is_null() && found(r, &a["thread_id"]))
        .collect::<Vec<_>>();
    if found.len() > 1 {
        return Ok(
            json!({"error_code":"THREAD_IDENTITY_CONFLICT","lookup_status":"THREAD_IDENTITY_CONFLICT","unavailable_reason":"Exact thread matched multiple authorized nodes","coverage":coverage,"matching_nodes":found.iter().map(|(r,_,_)|text(&r["node_id"])).collect::<BTreeSet<_>>(),"read_only":true}),
        );
    }
    if let Some((record, result, _)) = found.first() {
        return merge(
            result.clone(),
            json!({"source_node_id":record["node_id"],"coverage":coverage,"read_only":true}),
        );
    }
    if !unavailable.is_empty() {
        return Ok(
            json!({"error_code":"THREAD_SEARCH_INCOMPLETE","lookup_status":"THREAD_SEARCH_INCOMPLETE","unavailable_reason":"Some authorized nodes were offline, timed out or unavailable","coverage":coverage,"read_only":true}),
        );
    }
    Ok(
        json!({"error_code":"THREAD_NOT_FOUND","lookup_status":"THREAD_NOT_FOUND","absence_scope":"AUTHORIZED_NODE_SET","coverage":coverage,"read_only":true}),
    )
}
fn execute(a: &Value, call: &impl Fn(&str, Value) -> Result<Value>) -> Result<Value> {
    call("registry.refresh_states", json!({}))?;
    let record = target(a, call)?;
    let id = &record["node_id"];
    let op = text(&a["operation"]);
    let request = &a["request"];
    let fail = |code| json!({"error_code":code,"execution_started":false,"node_id":id});
    if !permitted(&record, &a["user_scope"]) {
        return Ok(fail("SHARING_SCOPE_DENIED"));
    }
    let scope = call(
        "registry.scope",
        json!({"node_id":id,"user_scope":a["user_scope"]}),
    )?;
    let permission = if matches!(op, "execution.context" | "execution.status") {
        "session.read"
    } else {
        op
    };
    if scope.is_null() || !Scope::parse(&scope)?.allows(permission) {
        return Ok(fail("SHARING_SCOPE_DENIED"));
    }
    let local_read = matches!(op, "execution.context" | "execution.status")
        && local(&record, a)
        && a["executors"].as_array().is_some_and(|v| v.contains(id));
    if (node_scope::stale(&record) || record["state"] != "ONLINE") && !local_read {
        return Ok(fail("NODE_OFFLINE"));
    }
    if call("router.executor", json!({"record":record}))? != true {
        return Ok(fail("NODE_EXECUTION_UNAVAILABLE"));
    }
    let request = merge(
        request.clone(),
        json!({"node_id":id,"user_scope":a["user_scope"]}),
    )?;
    let reply = call(
        "router.execute",
        json!({"node_id":id,"operation":op,"request":request}),
    )?;
    if let Some(v) = reply.get("result") {
        let result = merge(v.clone(), json!({"node_id":id}))?;
        let expected = &a["request"]["canonical_identity"];
        if truth(expected)
            && !truth(&result["error_code"])
            && result["task_ref"] != expected["task_id"]
        {
            return Ok(
                json!({"error_code":"CANONICAL_HANDOFF_ACK_CONFLICT","node_id":id,"execution_started":false,"operation_state":"UNKNOWN","side_effect":"UNKNOWN","retry":"RECONCILIATION_REQUIRED"}),
            );
        }
        if !truth(&result["error_code"]) {
            if local_read {
                call("registry.touch", json!({"node_id":id}))?;
            }
            for k in ["task_ref", "prepared_execution_ref", "execution_ref"] {
                if truth(&result[k]) {
                    call(
                        "registry.bind_reference",
                        json!({"reference":result[k],"node_id":id,"user_scope":a["user_scope"]}),
                    )?;
                }
            }
        }
        return Ok(result);
    }
    if matches!(
        op,
        "node.status"
            | "session.read"
            | "session.status"
            | "execution.context"
            | "execution.status"
    ) {
        return Ok(
            json!({"error_code":"NODE_READ_UNAVAILABLE","read_only":true,"operation_state":"READ_FAILED","side_effect":"NONE","retry":"READ_ONLY_RETRY","failure_stage":"rpc_transport","error_type":reply["error_type"],"node_id":id}),
        );
    }
    Ok(
        json!({"error_code":"EXECUTION_OUTCOME_UNKNOWN","operation_state":"UNKNOWN","side_effect":"UNKNOWN","retry":"RECONCILIATION_REQUIRED","node_id":id}),
    )
}
fn service_projection(
    a: &Value,
    status: bool,
    call: &impl Fn(&str, Value) -> Result<Value>,
) -> Result<Value> {
    let request = &a["request"];
    let record = &a["record"];
    let scope = Scope::parse(&a["scope"])?;
    crate::node_service::authorize(
        record,
        &scope,
        if status {
            "session.status"
        } else {
            "session.read"
        },
        request,
    )?;
    // Preserve SessionIdentity normalization before calling native history.
    // Status forwards the original provider thread key without a cursor identity.
    let identity_text = |value: &Value, limit: usize| -> Result<Value> {
        let raw = value.as_str().ok_or("ValueError")?;
        if raw.trim().is_empty()
            || raw.chars().count() > limit
            || raw.chars().any(|c| c < ' ' || c == '\u{7f}')
        {
            return Err("ValueError".into());
        }
        Ok(json!(raw.trim()))
    };
    let tid = if status {
        request.get("thread_id").ok_or("KeyError")?.clone()
    } else {
        identity_text(&request["thread_id"], 256)?
    };
    let provider = request.get("provider").cloned().unwrap_or(json!(PROVIDER));
    let source = request.get("source").cloned().unwrap_or(json!("native"));
    let version = request.get("source_version").cloned().unwrap_or(json!("1"));
    let version = if status {
        version
    } else {
        identity_text(&version, 512)?
    };
    let mut binding = json!({"node_id":record["node_id"],"user_scope":scope.user_scope,"provider":provider,"native_thread_id":tid,"source":source,"source_version":version,"secret_hex":a["secret_hex"]});
    let mut kwargs = json!({});
    for key in ["recent_turns", "max_bytes"] {
        if let Some(v) = request.get(key) {
            kwargs[key] = v.clone();
        }
    }
    if !status && truth(&request["cursor"]) {
        binding["cursor"] = request["cursor"].clone();
        let cursor = cursor_decode(&binding, true)?;
        if !cursor.is_null() {
            kwargs["cursor"] = cursor;
        }
    }
    let mut result = call(
        if status {
            "service.status"
        } else {
            "service.read"
        },
        json!({"thread_id":tid,"kwargs":kwargs}),
    )?;
    let snapshot = call("service.snapshot", json!({}))?;
    if !status && result.get("queried_thread_id").is_none() {
        result["queried_thread_id"] = json!(tid);
    }
    result = merge(
        result,
        json!({"node_id":snapshot["record"]["node_id"],"user_scope":snapshot["scope"]["user_scope"],"provider":provider,"source_node_id":snapshot["record"]["node_id"],"source_observed_at":crate::node_tls::now(),"read_only":true}),
    )?;
    if !status {
        if truth(&result["next_cursor"]) {
            binding["provider_cursor"] = result["next_cursor"].clone();
            result["next_cursor"] = cursor_encode(&binding, true)?;
        }
        call(
            "registry.update_thread",
            json!({"identity":{"node_id":record["node_id"],"user_scope":scope.user_scope,"provider":provider,"native_thread_id":tid,"source_version":version},"source":source,"status":{"lookup_status":result["lookup_status"],"context_status":result["context_status"]}}),
        )?;
    }
    Ok(result)
}

fn authority_request(a: &Value, call: &impl Fn(&str, Value) -> Result<Value>) -> Result<Value> {
    let command = text(&a["operation"])
        .strip_prefix("business.")
        .ok_or("UNSUPPORTED_OPERATION")?;
    if !node_scope::optional(&a["scope"])?.is_some_and(|s| s.allows("execution.authority")) {
        return Err("SHARING_SCOPE_DENIED".into());
    }
    let request = json!({"operation":"authority.request","protocol_version":PROTOCOL,
        "node_id":a["node_id"],"sharing_scope":a["scope"],"request_id":a["request_id"],
        "command":command,"payload":a["payload"]});
    // Match the deployed Python frame budget (json.dumps ensure_ascii=True).
    let wire = request.to_string();
    let length = wire
        .chars()
        .map(|c| {
            if c.is_ascii() && c != '\u{7f}' {
                1
            } else {
                6 * c.len_utf16()
            }
        })
        .sum::<usize>();
    if length + 1 > 256 * 1024 {
        return Err("REQUEST_TOO_LARGE".into());
    }
    let response = call("authority.request", request).map_err(|_| "DELIVERY_UNKNOWN")?;
    if truth(&response["error_code"]) {
        return Err(text(&response["error_code"]).into());
    }
    if response["request_id"] != a["request_id"] || !response["result"].is_object() {
        return Err("DELIVERY_UNKNOWN".into());
    }
    Ok(response["result"].clone())
}

fn remote_request(a: &Value, call: &impl Fn(&str, Value) -> Result<Value>) -> Result<Value> {
    let record = &a["record"];
    let request = &a["request"];
    call("remote.peer", json!({"node_id":a["node_id"]}))?;
    let registered = if record.is_null() {
        Value::Null
    } else {
        call(
            "remote.scope",
            json!({"node_id":a["node_id"],"user_scope":record["user_scope"]}),
        )?
    };
    let effective = node_scope::intersect(
        node_scope::optional(&registered)?,
        node_scope::optional(&a["scope"])?,
    )?;
    let op = text(&request["operation"]);
    let permission = if matches!(op, "execution.context" | "execution.status") {
        "session.read"
    } else {
        op
    };
    let provider = request.get("provider").cloned().unwrap_or(json!(PROVIDER));
    let mut provider_scope = effective.clone();
    provider_scope.providers = if effective.providers.iter().any(|p| provider == *p) {
        vec![PROVIDER.into()]
    } else {
        vec![]
    };
    if !provider_scope.allows(permission) {
        return Err("SHARING_SCOPE_DENIED".into());
    }
    if record.is_null()
        || !truth(&record["endpoint"])
        || node_scope::stale(record)
        || record["state"] == "OFFLINE"
    {
        return Err("NODE_OFFLINE".into());
    }
    if request["user_scope"] != effective.user_scope {
        return Err("USER_SCOPE_DENIED".into());
    }
    call(
        "remote.request",
        json!({"node_id":a["node_id"],"endpoint":record["endpoint"],
        "request":merge(request.clone(),json!({"node_id":a["node_id"]}))?}),
    )
}

fn helper_read(a: &Value, call: &impl Fn(&str, Value) -> Result<Value>) -> Result<Value> {
    let tid = text(&a["thread_id"]);
    let history = crate::native_history::History::new(text(&a["native_home"]));
    let unavailable = |code: String| {
        json!({"queried_thread_id":tid,"lookup_status":code,
        "error_code":code,"unavailable_reason":code,"provider_existence":"UNKNOWN","context_status":"CONTEXT_UNAVAILABLE"})
    };
    let metadata = match history.metadata(tid) {
        Ok(v) => v,
        Err(e) => return Ok(unavailable(e)),
    };
    if metadata.is_null() {
        return Ok(
            json!({"queried_thread_id":tid,"lookup_status":"THREAD_NOT_FOUND",
            "provider_existence":"NOT_FOUND","absence_scope":"CURRENT_USER_NATIVE_INDEX","context_status":"CONTEXT_UNAVAILABLE"}),
        );
    }
    let scope = call("helper.scopes", json!({}))?;
    let scope = node_scope::intersect(
        node_scope::optional(&scope["local"])?,
        node_scope::optional(&scope["active"])?,
    )?;
    let cwd = metadata.get("cwd").cloned().unwrap_or(json!(""));
    if !scope.project(&cwd) {
        return Ok(json!({"error_code":"PROJECT_SCOPE_DENIED","read_only":true}));
    }
    if a["execution"] == true {
        return call(
            "helper.context",
            merge(
                a["kwargs"].clone(),
                json!({"thread_id":tid,"host":a["node_id"]}),
            )?,
        );
    }
    let kwargs = &a["kwargs"];
    let integer = |value: &Value| -> Result<usize> {
        let n = value
            .as_i64()
            .or_else(|| value.as_bool().map(i64::from))
            .or_else(|| {
                value
                    .as_f64()
                    .filter(|v| v.is_finite())
                    .map(|v| v.trunc() as i64)
            })
            .or_else(|| value.as_str().and_then(|v| v.trim().parse::<i64>().ok()))
            .ok_or("ValueError")?;
        Ok(n.max(0) as usize)
    };
    let context = match history.context(
        tid,
        text(&a["node_id"]),
        &metadata,
        crate::native_history::ContextLimits {
            recent: integer(kwargs.get("recent_turns").unwrap_or(&json!(8)))?,
            budget: integer(kwargs.get("max_bytes").unwrap_or(&json!(32000)))?,
        },
        &Value::Null,
        &kwargs["cursor"],
    ) {
        Ok(v) => v,
        Err(e) => return Ok(unavailable(e)),
    };
    let name = [&metadata["name"], &metadata["title"]]
        .into_iter()
        .find(|v| truth(v))
        .cloned()
        .unwrap_or(json!(""));
    merge(
        json!({"queried_thread_id":tid,"lookup_status":"THREAD_UNBOUND","binding_status":"NOT_FOUND",
        "provider_existence":"CONFIRMED","host":a["node_id"],"native_thread":{"thread_id":tid,"cwd":metadata["cwd"],
        "history_mode":metadata["history_mode"],"archived":metadata["archived"],"created_at":metadata["created_at"],
        "updated_at":metadata["updated_at"],"name":name},"native_status":history.status(tid,&metadata),
        "provider_observation":{"state":"UNKNOWN","reason":"Live provider observation is separate from read-only history"}}),
        context,
    )
}

/// Per-Helper registration and collection scheduling. This never owns tasks.
pub struct HelperSupervisor {
    next_attempt: Option<Instant>,
    next_collection: Option<Instant>,
    heartbeat_seconds: u64,
    active: bool,
}
impl Default for HelperSupervisor {
    fn default() -> Self {
        Self {
            next_attempt: None,
            next_collection: None,
            heartbeat_seconds: 15,
            active: false,
        }
    }
}
impl HelperSupervisor {
    pub fn tick(
        &mut self,
        a: &Value,
        call: impl Fn(&str, Value) -> Result<Value>,
    ) -> Result<Value> {
        let io = |op: &str, args: Value| -> Result<Value> {
            let response = call(op, args)?;
            if let Some(error) = response.get("error_type") {
                return Err(text(error).into());
            }
            response
                .get("result")
                .cloned()
                .ok_or("NODE_CALLBACK_INVALID".into())
        };
        let now = Instant::now();
        if self.next_attempt.is_none_or(|deadline| now >= deadline) {
            let result = (|| -> Result<()> {
                let current = io("helper.approval", json!({}))?;
                let scope = node_scope::optional(&current)?;
                if scope
                    .as_ref()
                    .is_none_or(|s| s.revoked || !(s.read_sessions || s.execute_tasks))
                {
                    self.active = false;
                    io("helper.active", Value::Null)?;
                    if scope.is_some() {
                        io("helper.register", json!({"heartbeat":true,"scope":current}))?;
                    }
                    io(
                        "helper.health",
                        json!({"status":"blocked","reason":"SHARING_AUTHORIZATION_REVOKED"}),
                    )?;
                } else {
                    let reply = io(
                        "helper.register",
                        json!({"heartbeat":self.active,"scope":current}),
                    )?;
                    if !truth(&reply["registered"]) {
                        self.active = false;
                        io("helper.active", Value::Null)?;
                        io("helper.state", json!({"state":"OFFLINE"}))?;
                        io(
                            "helper.health",
                            json!({"status":"offline","reason":reply.get("error_code").cloned().unwrap_or(json!("REGISTRATION_REJECTED"))}),
                        )?;
                    } else {
                        if !reply["scope"].is_object() {
                            return Err("DeviceError".into());
                        }
                        let scope = Scope::parse(&reply["scope"]).map_err(|_| "ValueError")?;
                        io("helper.active", scope.value())?;
                        self.active = true;
                        io(
                            "helper.state",
                            json!({"state":"ONLINE","last_seen":reply["observed_at"]}),
                        )?;
                        let seconds = reply.get("heartbeat_seconds").cloned().unwrap_or(json!(15));
                        let seconds = seconds
                            .as_i64()
                            .or_else(|| {
                                seconds.as_f64().filter(|v| v.is_finite()).map(|v| v as i64)
                            })
                            .or_else(|| seconds.as_bool().map(i64::from))
                            .or_else(|| seconds.as_str().and_then(|s| s.trim().parse().ok()))
                            .ok_or("ValueError")?;
                        self.heartbeat_seconds = seconds.max(3) as u64;
                        io(
                            "helper.health",
                            json!({"status":"running","centre_id":a["centre_id"],"node_id":a["node_id"],"endpoint":a["endpoint"],"capabilities":a["capabilities"]}),
                        )?;
                    }
                }
                Ok(())
            })();
            if let Err(error) = result {
                self.active = false;
                io("helper.active", Value::Null)?;
                io("helper.state", json!({"state":"OFFLINE"}))?;
                io("helper.health", json!({"status":"offline","reason":error}))?;
            }
            self.next_attempt =
                Some(Instant::now() + Duration::from_secs(self.heartbeat_seconds.min(60)));
        }
        if self.next_collection.is_none_or(|deadline| now >= deadline) {
            let delay = match io("helper.collect", json!({})) {
                Ok(result) => {
                    io(
                        "helper.health",
                        json!({"status":if matches!(text(&result["status"]),"INDEXED"|"HISTORY_BACKFILL"){"running"}else{"degraded"},
                        "reason":result["status"],"node_id":a["node_id"],"sampled":result["sampled"]}),
                    )?;
                    result["delay"]
                        .as_f64()
                        .filter(|n| n.is_finite())
                        .ok_or("INVALID_COLLECTION_DELAY")?
                        .max(0.0)
                }
                Err(error) => {
                    io(
                        "helper.health",
                        json!({"status":"degraded","reason":"OBSERVATION_READ_FAILED","error_type":error}),
                    )?;
                    2.0
                }
            };
            self.next_collection = Some(Instant::now() + Duration::from_secs_f64(delay));
        }
        Ok(json!({"active":self.active}))
    }
}

pub fn handle(
    op: &str,
    a: &Value,
    call: impl Fn(&str, Value) -> Result<Value> + Sync,
) -> Result<Value> {
    match op {
        "router.helper_read" => helper_read(a, &call),
        "router.authority_request" => authority_request(a, &call),
        "router.remote_request" => remote_request(a, &call),
        "router.service_read" => service_projection(a, false, &call),
        "router.service_status" => service_projection(a, true, &call),
        "router.target" => target(a, &call),
        "router.read" => read(a, &call),
        "router.execute" => execute(a, &call),
        "router.nodes" => {
            call("registry.refresh_states", json!({}))?;
            let records = call("registry.list_nodes", json!({"user_scope":a["user_scope"]}))?;
            let mut out = Vec::new();
            for record in records.as_array().ok_or("NODE_RESPONSE_INVALID")? {
                let scope = call(
                    "registry.scope",
                    json!({"node_id":record["node_id"],"user_scope":a["user_scope"]}),
                )?;
                out.push(merge(record.clone(), json!({"scope":scope}))?);
            }
            Ok(json!(out))
        }
        _ => Err("NODE_ROUTER_OPERATION_UNKNOWN".into()),
    }
}
