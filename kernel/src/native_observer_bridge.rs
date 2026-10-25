//! User-owned native observation checkpoint and remote read policy.
//! Uses existing read-only callbacks; never creates tasks or starts a Provider.
use crate::{node_scope::Scope, node_tls::Result, read_projection::*};
#[path = "observer_projection.rs"]
mod observer_projection;
use base64::{
    Engine,
    engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD},
};
use hmac::{Hmac, Mac};
use regex::Regex;
use serde_json::{Value, json};
use sha2::Sha256;
use std::{
    fs::File,
    io::Read,
    os::unix::fs::MetadataExt,
    path::Path,
    sync::{LazyLock, Mutex},
    time::{Duration, Instant},
};
const CHECKPOINT_LIMIT: usize = 12000;
const BODY_LIMIT: usize = 1024 * 1024;
static THREAD: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$").unwrap());
#[derive(Default)]
pub struct Runtime {
    key: Mutex<Option<Vec<u8>>>,
    unavailable_until: Mutex<Option<Instant>>,
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
fn unhex(value: &str) -> Option<Vec<u8>> {
    if !value.len().is_multiple_of(2)
        || !value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return None;
    }
    (0..value.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&value[i..i + 2], 16).ok())
        .collect()
}
fn scope(call: &mut impl FnMut(&str, Value) -> Result<Value>) -> Result<Value> {
    let facts = call("native_observer.facts", json!({}))?;
    if !truth(&facts["local_identity"]) || !truth(&facts["native_home"]) || facts["scope"].is_null()
    {
        return Err("SHARING_SCOPE_DENIED".into());
    }
    Scope::parse(&facts["scope"])?;
    Ok(facts["scope"].clone())
}
fn generation(call: &mut impl FnMut(&str, Value) -> Result<Value>) -> Result<Value> {
    call("native_observer.generation", json!({}))
}
fn inventory(a: &Value, call: &mut impl FnMut(&str, Value) -> Result<Value>) -> Result<Value> {
    let grant = scope(call)?;
    let audit = a["audit"].as_bool().ok_or("INVALID_QUERY")?;
    let after = &a["after"];
    if !after.is_null() {
        let valid = if audit {
            after.as_str().is_some_and(|s| s.chars().count() <= 128)
        } else {
            after.as_array().is_some_and(|v| {
                v.len() == 2
                    && v[0].as_u64().is_some()
                    && v[1].as_str().is_some_and(|s| s.chars().count() <= 128)
            })
        };
        if !valid {
            return Err("INVALID_QUERY".into());
        }
    }
    let result = call(
        "native_observer.inventory",
        json!({"scope":grant,"after":after,"audit":audit}),
    )?;
    if scope(call)? != grant {
        return Err("SHARING_SCOPE_DENIED".into());
    }
    Ok(
        json!({"read_only":true,"rows":result[0],"after":result[1],"coverage":result[2],"generation":generation(call)?}),
    )
}
impl Runtime {
    fn key(&self) -> Result<Vec<u8>> {
        let mut key = self.key.lock().map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")?;
        if key.is_none() {
            let mut bytes = vec![0; 32];
            File::open("/dev/urandom")
                .and_then(|mut f| f.read_exact(&mut bytes))
                .map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")?;
            *key = Some(bytes);
        }
        Ok(key.as_ref().unwrap().clone())
    }
    fn decode(&self, value: &Value, binding: &Value) -> Result<Value> {
        let key = self.key()?;
        let parse = (|| -> Option<Value> {
            let token = value
                .as_str()
                .filter(|s| s.chars().count() <= CHECKPOINT_LIMIT)?;
            let (wire, signature) = token.split_once('.')?;
            if signature.len() != 64 {
                return None;
            }
            let signature = unhex(signature)?;
            let raw = URL_SAFE_NO_PAD
                .decode(wire.trim_end_matches('='))
                .or_else(|_| STANDARD.decode(wire))
                .ok()?;
            let mut h = Hmac::<Sha256>::new_from_slice(&key).ok()?;
            h.update(&raw);
            h.verify_slice(&signature).ok()?;
            let saved: Value = serde_json::from_slice(&raw).ok()?;
            if saved["binding"] != *binding
                || saved["expires"].as_f64()? < now()
                || !saved["state"].is_object()
            {
                return None;
            }
            Some(saved["state"].clone())
        })();
        parse.ok_or("NATIVE_COLLECTOR_CURSOR_RESET".into())
    }
    fn encode(&self, binding: Value, state: Value) -> Result<String> {
        let data = compact(&json!({"binding":binding,"state":state,"expires":now()+3600.0}));
        let mut h = Hmac::<Sha256>::new_from_slice(&self.key()?)
            .map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")?;
        h.update(data.as_bytes());
        let token = format!(
            "{}.{}",
            URL_SAFE_NO_PAD.encode(data),
            hex(&h.finalize().into_bytes())
        );
        if token.len() > CHECKPOINT_LIMIT {
            return Err("NATIVE_HISTORY_BUDGET_EXHAUSTED".into());
        }
        Ok(token)
    }
    fn sample(
        &self,
        a: &Value,
        call: &mut impl FnMut(&str, Value) -> Result<Value>,
    ) -> Result<Value> {
        let grant = scope(call)?;
        let tid = &a["tid"];
        let metadata = call("native_observer.metadata", json!({"tid":tid}))?;
        if !truth(&metadata) {
            return Err("NATIVE_THREAD_NOT_FOUND".into());
        }
        if !Scope::parse(&grant)?.project(&metadata["cwd"]) {
            return Err("PROJECT_SCOPE_DENIED".into());
        }
        let binding = json!([tid, generation(call)?, grant]);
        let state = if a["checkpoint"].is_null() {
            json!({})
        } else {
            self.decode(&a["checkpoint"], &binding)?
        };
        let sampled = call(
            "native_observer.sample",
            json!({"metadata":metadata,"state":state}),
        )?;
        if scope(call)? != grant || json!([tid, generation(call)?, grant]) != binding {
            return Err("SHARING_SCOPE_DENIED".into());
        }
        let mut events = sampled[0].as_array().ok_or("INVALID_OBSERVATION")?.clone();
        let state = sampled[1].clone();
        if !state.is_object() {
            return Err("INVALID_OBSERVATION".into());
        }
        for event in &mut events {
            if !event.is_object() {
                return Err("INVALID_OBSERVATION".into());
            }
            event["title"] = json!(truncate(
                event["title"].as_str().ok_or("INVALID_OBSERVATION")?,
                256
            ));
        }
        Ok(
            json!({"read_only":true,"thread_id":tid,"project":metadata["cwd"],"events":events,"checkpoint":self.encode(binding,state.clone())?,"user_message_observed":truth(&state["user_message_observed"])}),
        )
    }
    fn remote_read(
        &self,
        a: &Value,
        call: &mut impl FnMut(&str, Value) -> Result<Value>,
    ) -> Result<Value> {
        if self
            .unavailable_until
            .lock()
            .map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")?
            .is_some_and(|t| Instant::now() < t)
        {
            return Err("NATIVE_OBSERVER_UNAVAILABLE".into());
        }
        let response = call(
            "native_observer.http",
            json!({"path":a["path"],"params":a["params"]}),
        )?;
        let parsed = (|| -> Result<Value> {
            if response["transport_error"] == true {
                return Err("NATIVE_OBSERVER_UNAVAILABLE".into());
            }
            let body = response["body"]
                .as_str()
                .ok_or("NATIVE_OBSERVER_UNAVAILABLE")?;
            if body.len() > BODY_LIMIT {
                return Err("NATIVE_OBSERVER_UNAVAILABLE".into());
            }
            let value: Value =
                serde_json::from_str(body).map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")?;
            if response["status"] != 200 {
                return Err(match text(&value["error"]) {
                    s @ ("NATIVE_COLLECTOR_CURSOR_RESET"
                    | "SHARING_SCOPE_DENIED"
                    | "PROJECT_SCOPE_DENIED"
                    | "NATIVE_THREAD_NOT_FOUND") => s,
                    _ => "NATIVE_OBSERVER_UNAVAILABLE",
                }
                .into());
            }
            if !value.is_object() || value["read_only"] != true {
                return Err("NATIVE_OBSERVER_UNAVAILABLE".into());
            }
            Ok(value)
        })();
        if matches!(&parsed,Err(e)if e=="NATIVE_OBSERVER_UNAVAILABLE") {
            *self
                .unavailable_until
                .lock()
                .map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")? =
                Some(Instant::now() + Duration::from_secs(10));
        }
        parsed
    }
    pub fn invoke(
        &self,
        op: &str,
        a: &Value,
        mut call: impl FnMut(&str, Value) -> Result<Value>,
    ) -> Result<Value> {
        match op {
            op if op.starts_with("native_observer.legacy_") => {
                observer_projection::invoke(op, a, &mut call)
            }
            "native_observer.managed_context" => managed_context(a, &mut call),
            "native_observer.managed_identity" => managed_identity(&a["item"]),
            "native_observer.managed_recheck" => managed_recheck(a, &mut call),
            "native_observer.monitor_health" => monitor(a, true, &mut call),
            "native_observer.monitor_list" => monitor(a, false, &mut call),
            "native_observer.monitor_mcp" => monitor_mcp(a),
            "native_observer.desktop_limits" => desktop_limits(a),
            "native_observer.desktop_read" => desktop_read(a, &mut call),
            "native_observer.desktop_observe" => desktop_observe(a, &mut call),
            op if op.starts_with("native_observer.managed_source_") => {
                managed_source(op, a, &mut call)
            }
            "native_observer.activity_read" => activity(a, &hex(&self.key()?)),
            "native_observer.activity_generation" => Ok(json!(hex(&self.key()?))),
            "native_observer.activity_encode" => Ok(json!(activity_encode(
                &a["scope"],
                &a["mode"],
                &a["position"]
            )?)),
            "native_observer.activity_decode" => {
                activity_decode(&a["value"], &a["scope"], &a["mode"])
            }
            "native_observer.activity_text" => Ok(json!(activity_text(&a["value"]))),
            "native_observer.scope" => scope(&mut call),
            "native_observer.binding" => Ok(json!([a["tid"], generation(&mut call)?, a["scope"]])),
            "native_observer.inventory" => inventory(a, &mut call),
            "native_observer.sample" => self.sample(a, &mut call),
            "native_observer.key" => {
                if let Some(v) = a.get("value") {
                    let key = unhex(v.as_str().ok_or("INVALID_QUERY")?).ok_or("INVALID_QUERY")?;
                    *self.key.lock().map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")? = Some(key);
                }
                Ok(json!(hex(&self.key()?)))
            }
            "native_observer.route" => {
                let route = text(&a["route"]);
                let query = a["query"].as_object().ok_or("INVALID_QUERY")?;
                if route == "inventory" {
                    if query
                        .keys()
                        .any(|k| !matches!(k.as_str(), "audit" | "after"))
                    {
                        return Err("INVALID_QUERY".into());
                    }
                    let audit = query.get("audit").map(text).unwrap_or("0");
                    if !matches!(audit, "0" | "1") {
                        return Err("INVALID_QUERY".into());
                    }
                    let after = query
                        .get("after")
                        .map(|v| serde_json::from_str(text(v)))
                        .transpose()
                        .map_err(|_| "INVALID_QUERY")?
                        .unwrap_or(Value::Null);
                    inventory(&json!({"after":after,"audit":audit=="1"}), &mut call)
                } else {
                    let tid = route.strip_prefix("sample/").ok_or("INVALID_QUERY")?;
                    if !THREAD.is_match(tid) {
                        return Err("INVALID_THREAD_ID".into());
                    }
                    if query.keys().any(|k| k != "checkpoint") {
                        return Err("INVALID_QUERY".into());
                    }
                    self.sample(
                        &json!({"tid":tid,"checkpoint":query.get("checkpoint")}),
                        &mut call,
                    )
                }
            }
            "native_observer.remote_read" => self.remote_read(a, &mut call),
            "native_observer.remote_inventory" => remote_inventory(a, &mut call),
            "native_observer.remote_turns" => remote_turns(a, &mut call),
            _ => Err("NATIVE_OBSERVER_OPERATION_UNKNOWN".into()),
        }
    }
}
fn remote_inventory(
    a: &Value,
    call: &mut impl FnMut(&str, Value) -> Result<Value>,
) -> Result<Value> {
    let scope = Scope::parse(&a["scope"])?;
    let audit = a["audit"].as_bool().ok_or("INVALID_QUERY")?;
    let mut after = a["after"].clone();
    if after.is_null() && !audit {
        after = json!([(now() as i64 - 300).max(0), ""]);
    }
    let mut params = json!({"audit":i32::from(audit)});
    if !after.is_null() {
        params["after"] = json!(compact(&after));
    }
    let result = call(
        "native_observer.read",
        json!({"path":"inventory","params":params}),
    )?;
    let rows = result["rows"]
        .as_array()
        .filter(|r| r.len() <= 16)
        .ok_or("INVALID_OBSERVATION")?;
    Ok(json!([
        rows.iter()
            .filter(|r| scope.project(&r["cwd"]))
            .collect::<Vec<_>>(),
        result["after"],
        result["coverage"]
    ]))
}
fn remote_turns(a: &Value, call: &mut impl FnMut(&str, Value) -> Result<Value>) -> Result<Value> {
    let metadata = &a["metadata"];
    let tid = text(&metadata["id"]);
    if !THREAD.is_match(tid) {
        return Err("INVALID_THREAD_ID".into());
    }
    let mut state = a["state"].clone();
    if !state.is_object() {
        return Err("INVALID_OBSERVATION".into());
    }
    let mut params = json!({});
    if truth(&state["native_checkpoint"]) {
        params["checkpoint"] = state["native_checkpoint"].clone();
    }
    let path = format!("sample/{tid}");
    let result = match call("native_observer.read", json!({"path":path,"params":params})) {
        Err(e) if e == "NATIVE_COLLECTOR_CURSOR_RESET" && params.get("checkpoint").is_some() => {
            call("native_observer.read", json!({"path":path,"params":{}}))?
        }
        r => r?,
    };
    if result["thread_id"] != tid || result["project"] != metadata["cwd"] {
        return Err("NATIVE_OBSERVATION_IDENTITY_CONFLICT".into());
    }
    let events = result["events"]
        .as_array()
        .filter(|v| (1..=72).contains(&v.len()))
        .ok_or("INVALID_OBSERVATION")?;
    if events.iter().any(|e| {
        e["native_thread_id"] != tid
            || e["project"] != metadata["cwd"]
            || !e["task_ref"].is_null()
            || e["binding_evidence"] != "THREAD_UNBOUND"
            || !e["turn"]["execution_ref"].is_null()
    }) {
        return Err("NATIVE_OBSERVATION_IDENTITY_CONFLICT".into());
    }
    state["native_checkpoint"] = result["checkpoint"].clone();
    state["user_message_observed"] = json!(result["user_message_observed"] == true);
    Ok(json!([
        events.iter().map(|e| e["turn"].clone()).collect::<Vec<_>>(),
        events.last().unwrap()["coverage"],
        state
    ]))
}

const ACTIVITY_LIMIT: usize = 40;
const ACTIVITY_TEXT_LIMIT: usize = 16384;
const HISTORY_UNAVAILABLE: &str = "NATIVE_HISTORY_UNAVAILABLE";
const CURSOR_RESET: &str = "ACTIVITY_CURSOR_RESET_REQUIRED";
// Preserve Python's deployed ASCII JSON cursor codec, including array spacing.
fn cursor_json(value: &Value) -> String {
    match value {
        Value::Array(rows) => format!(
            "[{}]",
            rows.iter().map(cursor_json).collect::<Vec<_>>().join(", ")
        ),
        Value::String(s) => serde_json::to_string(s)
            .unwrap()
            .chars()
            .map(|ch| {
                if ch.is_ascii() {
                    ch.to_string()
                } else {
                    ch.encode_utf16(&mut [0; 2])
                        .iter()
                        .map(|c| format!("\\u{c:04x}"))
                        .collect()
                }
            })
            .collect(),
        _ => value.to_string(),
    }
}
fn activity_encode(scope: &Value, mode: &Value, position: &Value) -> Result<String> {
    let p = position
        .as_array()
        .filter(|v| v.len() == 2)
        .ok_or(CURSOR_RESET)?;
    Ok(URL_SAFE_NO_PAD.encode(cursor_json(&json!([scope, mode, p[0], p[1]]))))
}
fn activity_decode(value: &Value, scope: &Value, mode: &Value) -> Result<Value> {
    let parsed = (|| -> Option<Value> {
        let token = value.as_str().filter(|s| s.chars().count() <= 1024)?;
        let bytes = URL_SAFE_NO_PAD
            .decode(token.trim_end_matches('='))
            .or_else(|_| STANDARD.decode(token))
            .ok()?;
        let row: Value = serde_json::from_slice(&bytes).ok()?;
        let v = row.as_array()?;
        if v.len() != 4
            || v[0] != *scope
            || v[1] != *mode
            || v[2].as_i64().is_none_or(|n| n < 0)
            || v[3].as_str().is_none_or(|s| s.chars().count() > 256)
        {
            return None;
        }
        Some(json!([v[2], v[3]]))
    })();
    parsed.ok_or(CURSOR_RESET.into())
}
static ACTIVITY_REDACTIONS: LazyLock<Vec<(Regex, &str)>> = LazyLock::new(|| {
    vec![
    (Regex::new(r"(?s)-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)").unwrap(), "[REDACTED]"),
    (Regex::new(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,})\b").unwrap(), "[REDACTED]"),
    (Regex::new(r#"(?i)\b(?:access[_-]?token|api[_-]?key|password|secret|token)\b["']?\s*[:=]\s*["']?[^\s,;"']+"#).unwrap(), "[REDACTED]"),
    (Regex::new(r"[\x00-\x08\x0b-\x1f\x7f]").unwrap(), ""),
    (Regex::new(r"(?i)bearer\s+[^\s,;]+").unwrap(), "Bearer [REDACTED]"),
    (Regex::new(r"(?i)\b(password|secret|api[_-]?key|token)\s*[:=]\s*[^\s,;]+").unwrap(), "${1}=[REDACTED]"),
]
});
fn activity_text(v: &Value) -> String {
    let mut s = text(v).to_owned();
    for (pattern, replacement) in ACTIVITY_REDACTIONS.iter() {
        s = pattern.replace_all(&s, *replacement).into_owned();
    }
    bytes(&s, ACTIVITY_TEXT_LIMIT)
}
fn activity_db(path: &Path) -> Result<rusqlite::Connection> {
    let c = readonly(path, HISTORY_UNAVAILABLE, true)?;
    c.busy_timeout(Duration::from_millis(250))
        .map_err(|_| HISTORY_UNAVAILABLE)?;
    Ok(c)
}
fn activity(a: &Value, generation: &str) -> Result<Value> {
    let execution = &a["execution"];
    let route: Value =
        serde_json::from_str(text(&execution["routing_identity_json"])).unwrap_or(Value::Null);
    let tid = &route["conversation"]["binding"];
    let turn = &execution["turn_id"];
    let mut page = json!({"schema_version":"1","task_ref":a["task_ref"],"execution_ref":execution["execution_ref"],
        "observed_at":chrono::DateTime::from_timestamp_micros((now()*1_000_000.0) as i64).unwrap().to_rfc3339_opts(chrono::SecondsFormat::Micros,false),
        "source":"CODEX_NATIVE_HISTORY","availability":"UNAVAILABLE","reason":null,"items":[],"next_cursor":null,"older_cursor":null,"has_more":false});
    let reason = if !THREAD.is_match(text(tid)) || !THREAD.is_match(text(turn)) {
        Some("EXACT_TURN_UNAVAILABLE")
    } else if route["host"]["stable_identifier"] != "p620" {
        Some("NATIVE_HOST_UNAVAILABLE")
    } else if !matches!(
        text(&route["provider"]["stable_identifier"]),
        "codex" | "codex_app_server"
    ) {
        Some("PROVIDER_UNSUPPORTED")
    } else {
        None
    };
    if let Some(reason) = reason {
        page["reason"] = json!(reason);
        return Ok(page);
    }
    let read = (|| -> Result<Value> {
        let root = Path::new(text(&a["root"]));
        let history = root.join("thread_history_1.sqlite");
        {
            let c = activity_db(&root.join("state_5.sqlite"))?;
            let records = rows(
                &c,
                "SELECT history_mode FROM threads WHERE id=?",
                &[sql(tid)],
                HISTORY_UNAVAILABLE,
            )?;
            let metadata = records.first().ok_or("NATIVE_THREAD_UNAVAILABLE")?;
            if metadata["history_mode"] != "paginated" {
                return Err("NATIVE_HISTORY_MODE_UNSUPPORTED".into());
            }
        }
        let ino = history
            .metadata()
            .map_err(|_| HISTORY_UNAVAILABLE)?
            .ino()
            .to_string();
        let identity = json!([
            a["task_ref"],
            execution["execution_ref"],
            tid,
            turn,
            a["root"],
            ino,
            generation
        ]);
        let scope = json!(sha(cursor_json(&identity).as_bytes())[..32].to_string());
        let after = truth(&a["after"]);
        let position = if after || truth(&a["before"]) {
            Some(activity_decode(
                if after { &a["after"] } else { &a["before"] },
                &scope,
                &json!(if after { "after" } else { "before" }),
            )?)
        } else {
            None
        };
        let c = activity_db(&history)?;
        if rows(
            &c,
            "SELECT 1 FROM thread_turns WHERE thread_id=? AND turn_id=?",
            &[sql(tid), sql(turn)],
            HISTORY_UNAVAILABLE,
        )?
        .is_empty()
        {
            return Err("NATIVE_TURN_UNAVAILABLE".into());
        }
        let high = rows(
            &c,
            "SELECT updated_at_ordinal,item_id FROM thread_items WHERE thread_id=? AND turn_id=? ORDER BY updated_at_ordinal DESC,item_id DESC LIMIT 1",
            &[sql(tid), sql(turn)],
            HISTORY_UNAVAILABLE,
        )?;
        let high = high
            .first()
            .map(|r| json!([r["updated_at_ordinal"], r["item_id"]]))
            .unwrap_or(json!([0, ""]));
        if after
            && position
                .as_ref()
                .is_some_and(|p| (p[0].as_i64(), text(&p[1])) > (high[0].as_i64(), text(&high[1])))
        {
            return Err(CURSOR_RESET.into());
        }
        let mut query = "SELECT item_id,rollout_ordinal,updated_at_ordinal,created_at_ms,substr(json_extract(item_json,'$.text'),1,?) AS text,length(CAST(json_extract(item_json,'$.text') AS BLOB)) AS byte_count,json_extract(item_json,'$.phase') AS phase FROM thread_items WHERE thread_id=? AND turn_id=? AND item_type='agentMessage' AND json_valid(item_json) AND coalesce(json_extract(item_json,'$.phase'),'') IN ('','commentary','final_answer') AND coalesce(json_extract(item_json,'$.channel'),'') IN ('','commentary','final')".to_owned();
        let mut args = vec![sql(&json!(ACTIVITY_TEXT_LIMIT + 1)), sql(tid), sql(turn)];
        if let Some(p) = position {
            query.push_str(if after {
                " AND (updated_at_ordinal,item_id) > (?,?)"
            } else {
                " AND (rollout_ordinal,item_id) < (?,?)"
            });
            args.extend([sql(&p[0]), sql(&p[1])]);
        }
        query.push_str(if after {
            " ORDER BY updated_at_ordinal ASC,item_id ASC LIMIT ?"
        } else {
            " ORDER BY rollout_ordinal DESC,item_id DESC LIMIT ?"
        });
        args.push(sql(&json!(ACTIVITY_LIMIT + 1)));
        let mut selected = rows(&c, &query, &args, HISTORY_UNAVAILABLE)?;
        let more = selected.len() > ACTIVITY_LIMIT;
        selected.truncate(ACTIVITY_LIMIT);
        let next = if after && more {
            let r = selected.last().unwrap();
            json!([r["updated_at_ordinal"], r["item_id"]])
        } else {
            high
        };
        let older = if more && !after {
            let r = selected.last().unwrap();
            json!(activity_encode(
                &scope,
                &json!("before"),
                &json!([r["rollout_ordinal"], r["item_id"]])
            )?)
        } else {
            Value::Null
        };
        let mut items = selected.iter().filter(|r| r["text"].as_str().is_some_and(|s| !s.trim().is_empty())).map(|r| json!({
            "id":r["item_id"],"ordinal":r["rollout_ordinal"],"revision":r["updated_at_ordinal"],"timestamp_ms":r["created_at_ms"],
            "kind":if r["phase"] == "final_answer" {"result"} else {"feedback"},"text":activity_text(&r["text"]),"truncated":r["byte_count"].as_u64().unwrap_or(0) > ACTIVITY_TEXT_LIMIT as u64})).collect::<Vec<_>>();
        items.sort_by(|l, r| {
            (l["ordinal"].as_i64(), text(&l["id"])).cmp(&(r["ordinal"].as_i64(), text(&r["id"])))
        });
        Ok(
            json!({"items":items,"availability":"AVAILABLE","has_more":more,"next_cursor":activity_encode(&scope,&json!("after"),&next)?,"older_cursor":older}),
        )
    })();
    match read {
        Ok(values) => {
            page.as_object_mut()
                .unwrap()
                .extend(values.as_object().unwrap().clone());
            Ok(page)
        }
        Err(e) if e == CURSOR_RESET => Err(e),
        Err(e) => {
            page["reason"] = json!(e);
            Ok(page)
        }
    }
}

fn managed_identity(item: &Value) -> Result<Value> {
    if !item.is_object() || !item["turn"].is_object() {
        return Err("EXECUTION_TURN_IDENTITY_CONFLICT".into());
    }
    Ok(json!([
        item["node_id"],
        item["project"],
        item["native_thread_id"],
        item["task_ref"],
        item["turn"]["execution_ref"],
        item["turn"]["turn_id"],
        item["source"],
        item["binding_evidence"]
    ]))
}
fn managed_recheck(
    a: &Value,
    call: &mut impl FnMut(&str, Value) -> Result<Value>,
) -> Result<Value> {
    let current = call(
        "managed.detail",
        json!({"observation_id":a["observation_id"],"limit":1}),
    )?;
    if managed_identity(&current["item"])? != managed_identity(&a["item"])? {
        return Err("EXECUTION_TURN_IDENTITY_CONFLICT".into());
    }
    Ok(Value::Null)
}
fn managed_context(
    a: &Value,
    call: &mut impl FnMut(&str, Value) -> Result<Value>,
) -> Result<Value> {
    let item = call(
        "managed.detail",
        json!({"observation_id":a["observation_id"],"limit":1}),
    )?["item"]
        .clone();
    if item["node_id"] != a["runtime_host"] {
        return call(
            "managed.remote_context",
            json!({"observation_id":a["observation_id"],"cursor":a["cursor"]}),
        );
    }
    let mut result;
    if item["source"] != "CLINX_MANAGED" {
        result = call(
            "managed.desktop_context",
            json!({"thread_id":item["native_thread_id"],"context":true,"recent_turns":4,"max_bytes":16000,"cursor":a["cursor"]}),
        )?;
        if result["project"] != item["project"] {
            return Err("THREAD_SCOPE_MISMATCH".into());
        }
    } else {
        let id = managed_identity(&item)?;
        if item["binding_evidence"] != "CANONICAL_ROUTE"
            || ![2, 3, 4, 5].into_iter().all(|n| truth(&id[n]))
        {
            return Err("EXACT_RUST_EXECUTION_REQUIRED".into());
        }
        result = call(
            "managed.exact_context",
            json!({"task_ref":id[3],"thread_id":id[2],"host":id[0],"execution_ref":id[4],"recent_turns":4,"max_bytes":16000,"cursor":a["cursor"]}),
        )?;
        if result["read_only"] != true
            || result["thread_id"] != id[2]
            || result["task_ref"] != id[3]
            || result["execution_ref"] != id[4]
            || result["execution_turn_ref"] != id[5]
            || result["context_scope"] != "EXACT_EXECUTION_TURN"
        {
            return Err("EXECUTION_TURN_IDENTITY_CONFLICT".into());
        }
    }
    managed_recheck(
        &json!({"observation_id":a["observation_id"],"item":item}),
        call,
    )?;
    let out = result
        .as_object_mut()
        .ok_or("EXECUTION_TURN_IDENTITY_CONFLICT")?;
    out.insert("schema_version".into(), json!("clinx-observation-v1"));
    out.insert("read_only".into(), json!(true));
    Ok(result)
}
fn stamp() -> String {
    chrono::DateTime::from_timestamp_micros((now() * 1_000_000.0) as i64)
        .unwrap()
        .to_rfc3339_opts(chrono::SecondsFormat::Micros, false)
}
fn monitor(
    _: &Value,
    health: bool,
    call: &mut impl FnMut(&str, Value) -> Result<Value>,
) -> Result<Value> {
    if health
        && call(
            "monitor.read",
            json!({"name":"clinx_get_capabilities","arguments":{}}),
        )?["authority"]
            != "RUST"
    {
        return Err("RUST_AUTHORITY_REQUIRED".into());
    }
    call(
        "monitor.read",
        json!({"name":"clinx_list_observations","arguments":{"limit":1}}),
    )?;
    Ok(if health {
        json!({"schema_version":"1","status":"OK","read_only":true,"observed_at":stamp(),"authority_liveness":"UNKNOWN","auth_mode":"OBSERVER_BEARER","events_transport":"POLLING","authority":"RUST","observation_source":"RUST_CENTRE_V2"})
    } else {
        json!({"schema_version":"1","observed_at":stamp(),"items":[],"next_offset":null,"has_more":false,"source":"RUST_CENTRE_V2","legacy_history":false})
    })
}
fn monitor_mcp(a: &Value) -> Result<Value> {
    use std::io::{BufRead, BufReader, Write};
    if !matches!(
        text(&a["name"]),
        "clinx_get_capabilities"
            | "clinx_list_observations"
            | "clinx_get_observation"
            | "clinx_get_observation_context"
            | "clinx_get_observation_activity"
    ) {
        return Err("OBSERVER_READ_ONLY".into());
    }
    let mut random = [0u8; 16];
    File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut random))
        .map_err(|_| "RUST_OBSERVATION_UNAVAILABLE")?;
    let id = hex(&random);
    let request = json!({"jsonrpc":"2.0","id":id,"method":"tools/call","params":{"name":a["name"],"arguments":a["arguments"]}});
    let read = (|| -> std::io::Result<Vec<u8>> {
        let mut peer = crate::native_transport::connect_local(
            Path::new(text(&a["endpoint"])),
            Duration::from_secs(15),
        )?;
        peer.set_read_timeout(Some(Duration::from_secs(15)))?;
        peer.set_write_timeout(Some(Duration::from_secs(15)))?;
        writeln!(peer, "{request}")?;
        let mut raw = Vec::new();
        BufReader::new(peer.take(2 * 1024 * 1024 + 1)).read_until(b'\n', &mut raw)?;
        Ok(raw)
    })();
    let raw = read.map_err(|_| "RUST_OBSERVATION_UNAVAILABLE")?;
    if raw.len() > 2 * 1024 * 1024 || raw.last() != Some(&b'\n') {
        return Err("RUST_OBSERVATION_UNAVAILABLE".into());
    }
    let reply: Value = serde_json::from_slice(&raw).map_err(|_| "RUST_OBSERVATION_UNAVAILABLE")?;
    if reply["id"] != id || truth(&reply["error"]) {
        return Err("RUST_OBSERVATION_UNAVAILABLE".into());
    }
    let result = &reply["result"];
    if truth(&result["isError"]) {
        return Err(result["structuredContent"]["error_code"]
            .as_str()
            .unwrap_or("RUST_OBSERVATION_UNAVAILABLE")
            .into());
    }
    let payload = &result["structuredContent"];
    if payload["read_only"] != true {
        return Err("RUST_OBSERVATION_UNAVAILABLE".into());
    }
    Ok(payload.clone())
}
fn managed_source(
    op: &str,
    a: &Value,
    call: &mut impl FnMut(&str, Value) -> Result<Value>,
) -> Result<Value> {
    let mut state = a["state"].clone();
    match op {
        "native_observer.managed_source_generation" => {
            if a["rust_only"] == true {
                Ok(json!(truncate(
                    call("managed.health", json!({}))?["authority_id"]
                        .as_str()
                        .ok_or("INVALID_OBSERVATION")?,
                    32
                )))
            } else {
                call("managed.base_generation", json!({}))
            }
        }
        "native_observer.managed_source_inventory" => {
            if a["remote"] == true {
                return match call("managed.native_inventory", a.clone()) {
                    Ok(r) => Ok(r),
                    Err(e) => Ok(json!([[], a["after"], e])),
                };
            }
            if a["rust_only"] == true {
                Ok(json!([[], null, "RUST_MANAGED_ONLY"]))
            } else {
                call("managed.base_inventory", a.clone())
            }
        }
        "native_observer.managed_source_native_turns" => {
            if a["remote"] == true && a["metadata"]["history_mode"] != "canonical" {
                if !state.is_object() {
                    return Err("INVALID_OBSERVATION".into());
                }
                match call("managed.native_turns", a.clone()) {
                    Ok(r) => {
                        state = r[2].clone();
                        state
                            .as_object_mut()
                            .ok_or("INVALID_OBSERVATION")?
                            .remove("native_observation_error");
                        Ok(json!([r[0], r[1], state]))
                    }
                    Err(e) => {
                        state["native_observation_error"] = json!(e);
                        Ok(json!([[], e, state]))
                    }
                }
            } else {
                call("managed.base_native_turns", a.clone())
            }
        }
        "native_observer.managed_source_canonical_inventory" => {
            let tasks = match call(
                "managed.command",
                json!({"operation":"observation_inventory","arguments":{"host":a["owner_host"],"after":a["after"]}}),
            ) {
                Err(e) if e == "SOURCE_AUTHORITY_ERROR:RESPONSE_CAPACITY_EXCEEDED" => {
                    return Ok(json!([[], true]));
                }
                r => r?,
            };
            let scope = Scope::parse(&a["scope"])?;
            let tasks = tasks["tasks"].as_array().ok_or("INVALID_OBSERVATION")?;
            Ok(json!([tasks.iter().filter(|t|scope.project(&t["workspace_contract"]["cwd"])).map(|t|json!({"id":t["thread_id"],"cwd":t["workspace_contract"]["cwd"],"name":t["title"],"history_mode":"canonical","rollout_path":""})).collect::<Vec<_>>(),false]))
        }
        "native_observer.managed_source_canonical_turns" => {
            if !state.is_object() {
                return Err("INVALID_OBSERVATION".into());
            }
            let pages = (|| -> Result<(Value, Value)> {
                let recent = call(
                    "managed.command",
                    json!({"operation":"observation_turns","arguments":{"host":a["owner_host"],"thread_id":a["tid"],"before":"~"}}),
                )?;
                let older = call(
                    "managed.command",
                    json!({"operation":"observation_turns","arguments":{"host":a["owner_host"],"thread_id":a["tid"],"before":state.get("rust_before").cloned().unwrap_or(json!("~"))}}),
                )?;
                Ok((recent, older))
            })();
            let (recent, older) = match pages {
                Err(e) if e == "SOURCE_AUTHORITY_ERROR:RESPONSE_CAPACITY_EXCEEDED" => {
                    state["rust_projection_unavailable"] = json!(true);
                    return Ok(json!([[], state]));
                }
                r => r?,
            };
            state
                .as_object_mut()
                .unwrap()
                .remove("rust_projection_unavailable");
            let recent = recent["executions"]
                .as_array()
                .ok_or("INVALID_OBSERVATION")?;
            let older = older["executions"]
                .as_array()
                .ok_or("INVALID_OBSERVATION")?;
            if let Some(last) = older.last() {
                state["rust_before"] = last["id"].clone();
            }
            state["rust_history_complete"] = json!(older.len() < 16);
            let mut unique: Vec<Value> = Vec::new();
            for row in recent.iter().chain(older) {
                if let Some(i) = unique.iter().position(|r| r["id"] == row["id"]) {
                    unique[i] = row.clone();
                } else {
                    unique.push(row.clone());
                }
            }
            Ok(json!([unique.iter().map(|e|json!({"task_ref":e["task_ref"],"execution_ref":e["execution_ref"],"turn_id":e["turn_id"],"ordinal":e["created_at_ordinal"],"execution_state":e["status"],"native_state":"UNKNOWN","result_processing":if truth(&e["result"]){"RECEIVED"}else{"PENDING"},"business_result":null,"summary":e["result"]["response_text"],"artifacts":[],"started_at":e["created_at"],"completed_at":null})).collect::<Vec<_>>(),state]))
        }
        "native_observer.managed_source_sample" => {
            let sampled = call("managed.base_sample", a.clone())?;
            let mut events = sampled[0].as_array().ok_or("INVALID_OBSERVATION")?.clone();
            state = sampled[1].clone();
            if !state.is_object() {
                return Err("INVALID_OBSERVATION".into());
            }
            for event in &mut events {
                if !event.is_object() {
                    return Err("INVALID_OBSERVATION".into());
                }
                if state["rust_history_complete"] == false {
                    event["coverage"] = json!("HISTORY_BACKFILL");
                }
                if truth(&a["managed_inventory_unavailable"])
                    || truth(&state["rust_projection_unavailable"])
                {
                    event["coverage"] = json!("RUST_MANAGED_PROJECTION_UNAVAILABLE");
                }
            }
            state["active"] = json!(
                truth(&state["active"])
                    || events.iter().any(|e| matches!(
                        text(&e["turn"]["execution_state"]),
                        "ADMITTED" | "RUNNING" | "RECOVERY_REQUIRED" | "FINALIZING"
                    ))
            );
            Ok(json!([events, state]))
        }
        _ => Err("NATIVE_OBSERVER_OPERATION_UNKNOWN".into()),
    }
}

fn desktop_limits(a: &Value) -> Result<Value> {
    let recent = a
        .get("recent_turns")
        .filter(|v| !v.is_null())
        .cloned()
        .unwrap_or(json!(8));
    let max = a
        .get("max_bytes")
        .filter(|v| !v.is_null())
        .cloned()
        .unwrap_or(json!(32000));
    if !recent.as_i64().is_some_and(|n| (1..=20).contains(&n))
        || !max.as_i64().is_some_and(|n| (1024..=128000).contains(&n))
    {
        return Err("INVALID_CONTEXT_LIMIT".into());
    }
    Ok(json!([recent, max]))
}
fn desktop_read(a: &Value, call: &mut impl FnMut(&str, Value) -> Result<Value>) -> Result<Value> {
    if !truth(&a["credential"]) {
        return Err("NATIVE_OBSERVER_NOT_CONFIGURED".into());
    }
    let response = call("desktop.http", a.clone())?;
    if response["transport_error"] == true {
        return Err("NATIVE_OBSERVER_UNAVAILABLE".into());
    }
    let body = response["body"]
        .as_str()
        .ok_or("NATIVE_OBSERVER_UNAVAILABLE")?;
    if response["status"] == 200 && body.len() > 1_048_576 {
        return Err("CONTEXT_CAPACITY_EXCEEDED".into());
    }
    let result: Value = serde_json::from_str(body).map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")?;
    if response["status"] != 200 {
        return Err(match text(&result["error"]) {
            code @ ("PROJECT_SCOPE_DENIED"
            | "SHARING_SCOPE_DENIED"
            | "NATIVE_THREAD_NOT_FOUND"
            | "INVALID_NATIVE_CONTEXT_CURSOR"
            | "INVALID_QUERY") => code,
            _ => "NATIVE_OBSERVER_UNAVAILABLE",
        }
        .into());
    }
    if result["queried_thread_id"] != a["thread_id"] || result["read_only"] != true {
        return Err("NATIVE_OBSERVATION_IDENTITY_CONFLICT".into());
    }
    Ok(result)
}
fn desktop_observe(
    a: &Value,
    call: &mut impl FnMut(&str, Value) -> Result<Value>,
) -> Result<Value> {
    let tid = &a["thread_id"];
    if !THREAD.is_match(text(tid)) {
        return Err("INVALID_THREAD_ID".into());
    }
    let limits = desktop_limits(a)?;
    let facts = call("desktop.facts", json!({}))?;
    if !truth(&facts["local_identity"]) || !truth(&facts["native_home"]) || facts["scope"].is_null()
    {
        return Err("SHARING_SCOPE_DENIED".into());
    }
    let node = &facts["local_identity"]["node_id"];
    let scope = Scope::parse(&facts["scope"])?;
    let metadata = call("desktop.metadata", json!({"thread_id":tid}))?;
    if !truth(&metadata) {
        return Err("NATIVE_THREAD_NOT_FOUND".into());
    }
    if !scope.project(&metadata["cwd"]) {
        return Err("PROJECT_SCOPE_DENIED".into());
    }
    let status = call(
        "desktop.status",
        json!({"thread_id":tid,"metadata":metadata}),
    )?;
    let mut result = json!({"queried_thread_id":tid,"thread_id":tid,"task_ref":null,"execution_ref":null,"host":node,"node_id":node,"project":metadata["cwd"],
        "lookup_status":"THREAD_UNBOUND","binding_status":"NOT_FOUND","execution_state":null,"read_only":true,"selection_scope":"QUERIED_THREAD","status_source":"NATIVE_OBSERVATION",
        "native_status":status,"observed_at":stamp(),"native_status_source":"NATIVE_PERSISTED_HISTORY","native_turn_state":status["state"],
        "provider_observation":{"state":"UNKNOWN","source":"LIVE_THREAD_READ","writer_claimed":false}});
    if truth(&a["endpoint"]) {
        let live = call(
            "desktop.live",
            json!({"thread_id":tid,"endpoint":a["endpoint"]}),
        )?;
        if live["unavailable"] == true {
            result["provider_observation"]["reason"] = json!("LIVE_THREAD_READ_UNAVAILABLE");
        } else {
            let thread = &live["thread"];
            if thread["id"] != *tid || thread["cwd"] != metadata["cwd"] {
                return Err("NATIVE_OBSERVATION_IDENTITY_CONFLICT".into());
            }
            result["provider_observation"]["state"] = thread["status"]
                .get("type")
                .cloned()
                .unwrap_or(json!("UNKNOWN"));
            result["provider_observation"]["observed_at"] = json!(stamp());
            result["native_thread"] =
                json!({"id":tid,"cwd":thread["cwd"],"status":thread["status"]});
        }
    }
    let mut live = result["provider_observation"]["state"].clone();
    if !matches!(text(&live), "active" | "idle") && result["native_turn_state"] == "RUNNING" {
        let args = json!({"thread_id":tid,"turn_id":status["turn_id"],"project":metadata["cwd"],"endpoint":a["endpoint"]});
        let mut proof = call("desktop.writer", args.clone())?;
        if proof["liveness"] != "LIVE" && truth(&a["endpoint"]) {
            proof = call("desktop.probe", args)?;
        }
        proof
            .as_object_mut()
            .ok_or("NATIVE_OBSERVATION_IDENTITY_CONFLICT")?
            .insert("observed_at".into(), json!(stamp()));
        if proof["liveness"] == "LIVE" {
            live = json!("active");
        }
        result["native_writer_observation"] = proof;
    }
    result["native_display_state"] = json!(match text(&live) {
        "active" => "RUNNING",
        "idle" => "IDLE",
        _ => "UNKNOWN",
    });
    result["codex_running"] = json!(live == "active");
    if truth(&a["context"]) {
        let context = call(
            "desktop.context",
            json!({"thread_id":tid,"node_id":node,"metadata":metadata,"recent_turns":limits[0],"max_bytes":limits[1],"cursor":a["cursor"]}),
        )?;
        result.as_object_mut().unwrap().extend(
            context
                .as_object()
                .ok_or("NATIVE_OBSERVATION_IDENTITY_CONFLICT")?
                .clone(),
        );
    }
    let current = call("desktop.scope", json!({"node_id":node}))?;
    if current.is_null() || !Scope::parse(&current)?.project(&metadata["cwd"]) {
        return Err("SHARING_SCOPE_DENIED".into());
    }
    Ok(result)
}
