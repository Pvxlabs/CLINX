//! Durable observation projection; never a Task Authority. Deployed schema,
//! stream CAS, privacy filters and cursor identities are preserved.
use crate::{
    native_history as history,
    node_scope::{self, PROVIDER, Scope},
    node_tls::Result,
    read_projection::*,
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use regex::Regex;
use rusqlite::{Connection, params};
use serde_json::{Value, json};
use std::{fs, path::Path, sync::LazyLock};
pub const VERSION: &str = "clinx-observation-v1";
fn db<T>(v: rusqlite::Result<T>) -> Result<T> {
    v.map_err(|_| "OBSERVATION_UNAVAILABLE".into())
}
fn query(c: &Connection, q: &str, a: &[rusqlite::types::Value]) -> Result<Vec<Value>> {
    rows(c, q, a, "OBSERVATION_UNAVAILABLE")
}
fn first(c: &Connection, q: &str, a: &[rusqlite::types::Value]) -> Result<Value> {
    Ok(query(c, q, a)?.into_iter().next().unwrap_or(Value::Null))
}
fn parsed(v: &Value, k: &str) -> Result<Value> {
    serde_json::from_str(text(&v[k])).map_err(|_| "OBSERVATION_UNAVAILABLE".into())
}
fn maybe(v: &Value, default: &str) -> String {
    if truth(v) {
        v.as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| v.to_string())
    } else {
        default.into()
    }
}
fn display(v: &Value, default: &str) -> Result<String> {
    if truth(v) {
        v.as_str()
            .map(str::to_owned)
            .ok_or("INVALID_OBSERVATION".into())
    } else {
        Ok(default.into())
    }
}
fn bounded(v: &Value, optional: bool) -> Result<Value> {
    if optional && v.is_null() {
        return Ok(Value::Null);
    }
    if v.as_str()
        .is_some_and(|s| !s.is_empty() && s.chars().count() <= 256 && !s.chars().any(|c| c < ' '))
    {
        Ok(v.clone())
    } else {
        Err("INVALID_OBSERVATION".into())
    }
}
pub fn clean(v: &Value, limit: usize) -> Result<Value> {
    if v.is_null() {
        return Ok(Value::Null);
    }
    let s = v.as_str().ok_or("INVALID_OBSERVATION")?;
    static PRIVATE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(
            r"-----BEGIN [^\-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^\-]*PRIVATE KEY-----|$)",
        )
        .unwrap()
    });
    static SECRET: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"(?i)\b(token|credential|password|secret)\s*[:=]\s*[^\s,;]+").unwrap()
    });
    let s = PRIVATE.replace_all(s, "[REDACTED]");
    let s = SECRET.replace_all(&s, "${1}=[REDACTED]");
    Ok(json!(
        history::message(&json!({"type":"agentMessage","text":truncate(&s,limit*2)}))
            .map(|(_, s)| truncate(&s, limit))
            .unwrap_or_default()
    ))
}
pub fn observation_id(node: &str, user: &str, provider: &str, thread: &str) -> String {
    format!(
        "obs_{}",
        &sha(compact(&json!([node, user, provider, thread])).as_bytes())[..40]
    )
}
pub fn validate(event: &Value, node: &str, scope: &Scope) -> Result<Value> {
    if !event.is_object() {
        return Err("INVALID_OBSERVATION".into());
    }
    let thread = bounded(&event["native_thread_id"], false)?;
    let provider = event.get("provider").cloned().unwrap_or(json!(PROVIDER));
    let project = &event["project"];
    if !project.as_str().is_some_and(|s| s.chars().count() <= 1024) || !scope.project(project) {
        return Err("PROJECT_SCOPE_DENIED".into());
    }
    if !scope.allows("observation.upload") || !scope.providers.iter().any(|p| provider == *p) {
        return Err("SHARING_SCOPE_DENIED".into());
    }
    let turn = if truth(&event["turn"]) {
        event["turn"].clone()
    } else {
        json!({})
    };
    if !turn.is_object() {
        return Err("INVALID_OBSERVATION".into());
    }
    let mut proof = turn["live_observation"].clone();
    if !proof.is_null() {
        if !proof.is_object()
            || proof["thread_id"] != thread
            || !truth(&turn["turn_id"])
            || proof["turn_id"] != turn["turn_id"]
            || !proof["observed_at"].is_number()
            || !matches!(proof["liveness"].as_str(), Some("LIVE" | "UNKNOWN"))
        {
            return Err("INVALID_OBSERVATION_PROOF".into());
        }
        proof = json!({"thread_id":thread,"turn_id":proof["turn_id"],"observed_at":proof["observed_at"],"liveness":proof["liveness"],"reason":clean(&proof["reason"],80)?});
    }
    let ordinal = turn.get("ordinal").cloned().unwrap_or(json!(0));
    if ordinal.as_u64().is_none() {
        return Err("INVALID_OBSERVATION".into());
    }
    let canonical = &turn["execution_state"];
    if !canonical.is_null() && !canonical.as_str().is_some_and(|s| s.chars().count() <= 64) {
        return Err("INVALID_OBSERVATION".into());
    }
    let task = bounded(&event["task_ref"], true)?;
    let eref = bounded(&turn["execution_ref"], true)?;
    if (truth(&task) || truth(&eref)) && event["binding_evidence"] != "CANONICAL_ROUTE" {
        return Err("OBSERVATION_BINDING_UNPROVEN".into());
    }
    let result = &turn["business_result"];
    if !result.is_null() && !matches!(result.as_str(), Some("PASS" | "BLOCKED")) {
        return Err("INVALID_OBSERVATION".into());
    }
    if !result.is_null() && !(truth(&task) && truth(&eref)) {
        return Err("OBSERVATION_BINDING_UNPROVEN".into());
    }
    let model = &turn["model"];
    if !model.is_null() && (history::model(model).is_null() || !truth(&turn["turn_id"])) {
        return Err("INVALID_NATIVE_TURN_MODEL".into());
    }
    let artifacts = turn.get("artifacts").cloned().unwrap_or(json!([]));
    let artifacts = artifacts
        .as_array()
        .filter(|a| a.len() <= 16)
        .ok_or("INVALID_OBSERVATION")?;
    let admission = if truth(&event["native_admission"]) {
        event["native_admission"].clone()
    } else {
        json!({})
    };
    if !admission.is_object() {
        return Err("INVALID_OBSERVATION".into());
    }
    let title = display(&event["title"], text(&thread))?;
    let title = title.lines().next().unwrap_or("");
    let mut payload = json!({"observation_id":observation_id(node,&scope.user_scope,text(&provider),text(&thread)),"node_id":node,"user_scope":scope.user_scope,"provider":provider,"native_thread_id":thread,"project":project,"title":clean(&json!(title),100)?,"source":if truth(&task){"CLINX_MANAGED"}else{"CODEX_NATIVE"},"source_client":clean(&json!(display(&event["source_client"],"UNKNOWN")?),40)?,"task_ref":task,"binding_evidence":if truth(&task){"CANONICAL_ROUTE"}else{"THREAD_UNBOUND"},"source_generation":bounded(event.get("source_generation").unwrap_or(&json!("unknown")),false)?,"coverage":clean(&json!(display(&event["coverage"],"PARTIAL")?),80)?,"source_updated_at":clean(&json!(maybe(&event["source_updated_at"],"")),64)?,"turn":{"turn_id":bounded(&turn["turn_id"],true)?,"ordinal":ordinal,"native_state":history::normalize(&turn["native_state"]),"execution_ref":eref,"execution_state":canonical,"business_result":result,"result_processing":clean(&json!(display(&turn["result_processing"],"UNKNOWN")?),80)?,"started_at":clean(&json!(maybe(&turn["started_at"],"")),64)?,"completed_at":clean(&json!(maybe(&turn["completed_at"],"")),64)?,"progress":clean(&turn["progress"],1500)?,"summary":clean(&turn["summary"],3000)?,"artifacts":artifacts.iter().map(|v|clean(v,256)).collect::<Result<Vec<_>>>()?}});
    if event.get("native_admission").is_some() {
        payload["native_admission"] = json!({"has_user_event":admission["has_user_event"]==true,"user_message_observed":admission["user_message_observed"]==true,"created_at":clean(&json!(maybe(&admission["created_at"],"")),64)?,"archived":admission["archived"]==true});
    }
    if !model.is_null() {
        payload["turn"]["model"] = model.clone();
    }
    if !proof.is_null() {
        payload["turn"]["live_observation"] = proof;
    }
    if turn.get("source").is_some() {
        payload["turn"]["source"] = json!(if truth(&payload["turn"]["execution_ref"]) {
            "CLINX_MANAGED"
        } else {
            "CODEX_NATIVE"
        });
    }
    Ok(payload)
}
pub fn init(path: &Path) -> Result<Value> {
    use std::os::unix::fs::PermissionsExt;
    fs::create_dir_all(path.parent().ok_or("OBSERVATION_UNAVAILABLE")?)
        .map_err(|_| "OBSERVATION_UNAVAILABLE")?;
    let c = db(Connection::open(path))?;
    db(c.busy_timeout(std::time::Duration::from_secs(2)))?;
    db(c.execute_batch("PRAGMA journal_mode=WAL;"))?;
    db(c.execute_batch(include_str!("observation.sql")))?;
    let cols = columns(&c, "events")?;
    for k in ["observation", "payload"] {
        if !cols.iter().any(|s| s == k) {
            db(c.execute_batch(&format!("ALTER TABLE events ADD COLUMN {k} TEXT")))?;
        }
    }
    db(c.execute_batch("CREATE INDEX IF NOT EXISTS observation_activity ON events(observation,received DESC,stream,seq);"))?;
    let mut random = [0u8; 32];
    FileReader::random(&mut random)?;
    let secret = random
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>();
    db(c.execute(
        "INSERT OR IGNORE INTO settings VALUES ('cursor_secret',?)",
        [secret],
    ))?;
    db(c.execute(
        "INSERT OR IGNORE INTO settings VALUES ('retention_evicted','0')",
        [],
    ))?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))
        .map_err(|_| "OBSERVATION_UNAVAILABLE")?;
    Ok(Value::Null)
}
struct FileReader;
impl FileReader {
    fn random(out: &mut [u8]) -> Result<()> {
        use std::io::Read;
        fs::File::open("/dev/urandom")
            .and_then(|mut f| f.read_exact(out))
            .map_err(|_| "OBSERVATION_UNAVAILABLE".into())
    }
}
pub fn accept(a: &Value) -> Result<Value> {
    let node = text(&a["node"]);
    let scope = Scope::parse(&a["scope"])?;
    let request = &a["request"];
    let stream = bounded(&request["stream_id"], false)?;
    let events = request["events"]
        .as_array()
        .filter(|a| !a.is_empty() && a.len() <= 32)
        .ok_or("INVALID_OBSERVATION_BATCH")?;
    if compact(&request["events"]).len() > 180000 {
        return Err("INVALID_OBSERVATION_BATCH".into());
    }
    let mut validated = Vec::new();
    let mut last = 0;
    for event in events {
        let incoming = event["seq"]
            .as_i64()
            .filter(|n| *n >= 1)
            .ok_or("INVALID_OBSERVATION_SEQUENCE")?;
        let payload = if event["scope_redacted"] == true {
            if event
                .as_object()
                .is_none_or(|m| m.len() != 2 || !m.contains_key("seq"))
            {
                return Err("INVALID_OBSERVATION".into());
            }
            Value::Null
        } else {
            validate(event, node, &scope)?
        };
        let digest = sha(compact(&payload).as_bytes());
        if incoming <= last {
            return Err("OBSERVATION_SEQUENCE_ORDER".into());
        }
        last = incoming;
        validated.push((incoming, payload, digest));
    }
    let mut c = db(Connection::open(text(&a["path"])))?;
    db(c.busy_timeout(std::time::Duration::from_secs(2)))?;
    let tx = db(c.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate))?;
    let received = now();
    let state = first(
        &tx,
        "SELECT * FROM streams WHERE node=? AND user=?",
        &[node.to_owned().into(), scope.user_scope.clone().into()],
    )?;
    let mut seq = if state["stream"] == stream {
        state["seq"].as_i64().unwrap_or(0)
    } else {
        0
    };
    let mut gap = state["gap"].clone();
    if !state.is_null() && state["stream"] != stream {
        if request["previous_stream_id"] != state["stream"] {
            return Err("OBSERVATION_SOURCE_CONFLICT".into());
        }
        gap = json!("SOURCE_REBUILT");
    }
    let mut expected = seq + 1;
    for (incoming, _, _) in &validated {
        if *incoming > seq {
            if *incoming != expected {
                if !state.is_null() {
                    db(tx.execute(
                        "UPDATE streams SET gap=? WHERE node=? AND user=?",
                        params!["SEQUENCE_GAP", node, scope.user_scope],
                    ))?;
                }
                db(tx.commit())?;
                return Ok(
                    json!({"accepted":false,"error_code":"OBSERVATION_GAP","expected_seq":expected,"ack_seq":seq,"stream_id":stream}),
                );
            }
            expected += 1;
        }
    }
    for (incoming, original, digest) in &validated {
        if *incoming <= seq {
            let old = first(
                &tx,
                "SELECT digest FROM events WHERE node=? AND user=? AND stream=? AND seq=?",
                &[
                    node.to_owned().into(),
                    scope.user_scope.clone().into(),
                    sql(&stream),
                    (*incoming).into(),
                ],
            )?;
            if !old.is_null() && old["digest"] != *digest {
                return Err("OBSERVATION_SEQUENCE_CONFLICT".into());
            }
            continue;
        }
        let mut payload = original.clone();
        if payload.is_null() {
            db(tx.execute(
                "INSERT INTO events(node,user,stream,seq,digest,received) VALUES (?,?,?,?,?,?)",
                params![
                    node,
                    scope.user_scope,
                    text(&stream),
                    incoming,
                    digest,
                    received
                ],
            ))?;
            seq = *incoming;
            gap = json!("SCOPE_WITHHELD");
            continue;
        }
        let oid = payload["observation_id"].clone();
        let event_payload = compact(&payload);
        let mut turn = payload["turn"].clone();
        let previous = first(&tx, "SELECT * FROM items WHERE id=?", &[sql(&oid)])?;
        if !previous.is_null() {
            let prior = parsed(&previous, "payload")?;
            if truth(&prior["task_ref"])
                && truth(&payload["task_ref"])
                && prior["task_ref"] != payload["task_ref"]
            {
                return Err("OBSERVATION_IDENTITY_CONFLICT".into());
            }
            payload["project"] = prior["project"].clone();
            if prior["source_generation"] != payload["source_generation"] {
                gap = json!("SOURCE_REBUILT");
            }
            if truth(&prior["task_ref"]) && !truth(&payload["task_ref"]) {
                payload["task_ref"] = prior["task_ref"].clone();
                payload["source"] = json!("CLINX_MANAGED");
                payload["binding_evidence"] = json!("CANONICAL_ROUTE");
            }
        }
        let key = if truth(&turn["turn_id"]) {
            turn["turn_id"].clone()
        } else if truth(&turn["execution_ref"]) {
            turn["execution_ref"].clone()
        } else {
            json!("metadata")
        };
        let old = first(
            &tx,
            "SELECT payload FROM turns WHERE observation=? AND turn_key=?",
            &[sql(&oid), sql(&key)],
        )?;
        if !old.is_null() {
            let old = parsed(&old, "payload")?;
            if truth(&old["execution_ref"])
                && truth(&turn["execution_ref"])
                && old["execution_ref"] != turn["execution_ref"]
            {
                return Err("OBSERVATION_IDENTITY_CONFLICT".into());
            }
            if history::terminal(text(&old["native_state"]))
                && !history::terminal(text(&turn["native_state"]))
            {
                turn["native_state"] = old["native_state"].clone();
                turn["completed_at"] = old["completed_at"].clone();
            }
            for k in [
                "business_result",
                "summary",
                "execution_ref",
                "execution_state",
            ] {
                if truth(&old[k]) && !truth(&turn[k]) {
                    turn[k] = old[k].clone();
                }
            }
        }
        db(tx.execute(
            "INSERT OR REPLACE INTO turns VALUES (?,?,?,?,?)",
            params![
                text(&oid),
                text(&key),
                turn["ordinal"].as_i64(),
                received,
                compact(&turn)
            ],
        ))?;
        payload["turn"] =
            if previous["ordinal"].as_i64().unwrap_or(-1) > turn["ordinal"].as_i64().unwrap_or(0) {
                parsed(&previous, "payload")?["turn"].clone()
            } else {
                turn
            };
        db(tx.execute(
            "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?)",
            params![
                text(&oid),
                node,
                scope.user_scope,
                text(&payload["project"]),
                text(&payload["provider"]),
                text(&payload["native_thread_id"]),
                payload["task_ref"].as_str(),
                received,
                payload["turn"]["ordinal"].as_i64(),
                compact(&payload)
            ],
        ))?;
        db(tx.execute("INSERT INTO events(node,user,stream,seq,digest,received,observation,payload) VALUES (?,?,?,?,?,?,?,?)",params![node,scope.user_scope,text(&stream),incoming,digest,received,text(&oid),event_payload]))?;
        seq = *incoming;
    }
    let generation = validated
        .iter()
        .rev()
        .find(|(_, p, _)| !p.is_null())
        .map(|(_, p, _)| p["source_generation"].clone())
        .unwrap_or_else(|| {
            if state.is_null() {
                json!("redacted")
            } else {
                state["source_generation"].clone()
            }
        });
    db(tx.execute(
        "INSERT OR REPLACE INTO streams VALUES (?,?,?,?,?,?,?)",
        params![
            node,
            scope.user_scope,
            text(&stream),
            seq,
            text(&generation),
            gap.as_str(),
            received
        ],
    ))?;
    for (table, maximum) in [("items", 20000), ("turns", 50000), ("events", 50000)] {
        let count: i64 =
            db(tx.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0)))?;
        let extra = (count - maximum).max(0);
        if extra > 0 {
            db(tx.execute(&format!("DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} ORDER BY received LIMIT ?)"),[extra]))?;
            db(tx.execute(
                "UPDATE settings SET value=CAST(value AS INTEGER)+? WHERE key='retention_evicted'",
                [extra],
            ))?;
        }
    }
    db(tx.execute(
        "DELETE FROM turns WHERE observation NOT IN (SELECT id FROM items)",
        [],
    ))?;
    db(tx.commit())?;
    Ok(json!({"accepted":true,"ack_seq":seq,"stream_id":stream,"received_at":received,"gap":gap}))
}
pub fn binding(a: &Value) -> Result<Value> {
    let path = text(&a["path"]);
    if path.is_empty() || !Path::new(path).exists() {
        return Ok(Value::Null);
    }
    let c = readonly(Path::new(path), "OBSERVATION_UNAVAILABLE", false)?;
    if first(
        &c,
        "SELECT 1 AS found FROM sqlite_master WHERE name='native_task_bindings'",
        &[],
    )?
    .is_null()
    {
        return Ok(Value::Null);
    }
    let mut predicates = Vec::new();
    let mut args = Vec::new();
    for (column, key) in [
        ("node_id", "node"),
        ("host", "host"),
        ("provider", "provider"),
        ("thread_id", "thread"),
        ("task_id", "task"),
        ("user_scope", "user"),
    ] {
        if !a[key].is_null() {
            predicates.push(format!("{column}=?"));
            args.push(sql(&a[key]));
        }
    }
    let rows = query(
        &c,
        &format!(
            "SELECT * FROM native_task_bindings{} LIMIT 2",
            if predicates.is_empty() {
                String::new()
            } else {
                format!(" WHERE {}", predicates.join(" AND "))
            }
        ),
        &args,
    )?;
    if rows.len() > 1 {
        return Err("NATIVE_IDENTITY_AMBIGUOUS".into());
    }
    Ok(rows.into_iter().next().unwrap_or(Value::Null))
}
struct Directory<'a, F: FnMut(&str, Value) -> Result<Value>> {
    a: &'a Value,
    call: F,
}
impl<F: FnMut(&str, Value) -> Result<Value>> Directory<'_, F> {
    fn fact(&mut self, node: &str) -> Result<Value> {
        (self.call)("directory.node", json!({"node_id":node}))
    }
    fn nodes(&mut self) -> Result<Vec<Value>> {
        (self.call)("directory.nodes", json!({}))?
            .as_array()
            .cloned()
            .ok_or("OBSERVATION_UNAVAILABLE".into())
    }
    fn scope(&mut self, node: &str) -> Result<Option<Scope>> {
        let f = self.fact(node)?;
        let record = &f["record"];
        let local = self.a["local_identity"]["node_id"] == node
            && !record.is_null()
            && record["public_key_fingerprint"] == self.a["local_identity"]["fingerprint"];
        let paired =
            f["peer"]["trust_state"] == "TRUSTED" && f["peer"]["security_status"] == "OPAQUE_V1";
        if !(local || paired)
            || record.is_null()
            || record["authorized"] != true
            || record["trusted"] != true
            || record["version_compatible"] != true
        {
            return Ok(None);
        }
        let s = node_scope::intersect(
            node_scope::optional(&f["scope"])?,
            node_scope::optional(&f["approval"])?,
        )?;
        if s.user_scope == text(&self.a["user_scope"]) && s.allows("session.read") {
            Ok(Some(s))
        } else {
            Ok(None)
        }
    }
    fn allowed(&mut self, p: &Value) -> Result<bool> {
        Ok(self.scope(text(&p["node_id"]))?.is_some_and(|s| {
            s.project(&p["project"]) && s.providers.iter().any(|v| p["provider"] == *v)
        }))
    }
    fn fingerprint(&mut self) -> Result<String> {
        let mut approvals = serde_json::Map::new();
        for r in self.nodes()? {
            let id = text(&r["node_id"]);
            approvals.insert(
                id.into(),
                self.scope(id)?.map(|s| s.value()).unwrap_or(Value::Null),
            );
        }
        Ok(sha(compact(&Value::Object(approvals)).as_bytes()))
    }
    fn cursor(
        &mut self,
        c: &Connection,
        context: &Value,
        position: Value,
        cursor: &Value,
    ) -> Result<Value> {
        use hmac::{Hmac, Mac};
        use sha2::Sha256;
        let secret = first(
            c,
            "SELECT value FROM settings WHERE key='cursor_secret'",
            &[],
        )?["value"]
            .clone();
        let scope = json!([
            VERSION,
            self.a["user_scope"],
            self.fingerprint()?,
            sha(compact(context).as_bytes())
        ]);
        if !cursor.is_null() {
            let result = (|| -> Option<Value> {
                let token = cursor.as_str().filter(|s| s.len() <= 2048)?;
                let (raw, signature) = token.split_once('.')?;
                let data = URL_SAFE_NO_PAD.decode(raw.trim_end_matches('=')).ok()?;
                if signature.len() != 64 || !signature.bytes().all(|b| b.is_ascii_hexdigit()) {
                    return None;
                }
                let signature = (0..64)
                    .step_by(2)
                    .map(|n| u8::from_str_radix(&signature[n..n + 2], 16))
                    .collect::<std::result::Result<Vec<_>, _>>()
                    .ok()?;
                let mut h = Hmac::<Sha256>::new_from_slice(text(&secret).as_bytes()).ok()?;
                h.update(&data);
                h.verify_slice(&signature).ok()?;
                let decoded: Value = serde_json::from_slice(&data).ok()?;
                if decoded["scope"] != scope || decoded["expires"].as_f64()? < now() {
                    return None;
                }
                decoded.get("position").cloned()
            })();
            return result.ok_or("INVALID_OBSERVATION_CURSOR".into());
        }
        let data = compact(&json!({"scope":scope,"position":position,"expires":now()+3600.0}));
        let mut h = Hmac::<Sha256>::new_from_slice(text(&secret).as_bytes())
            .map_err(|_| "OBSERVATION_UNAVAILABLE")?;
        h.update(data.as_bytes());
        let signature = h
            .finalize()
            .into_bytes()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>();
        Ok(json!(format!(
            "{}.{signature}",
            URL_SAFE_NO_PAD.encode(data)
        )))
    }
    fn project(&mut self, c: &Connection, p: &Value, received: f64) -> Result<Value> {
        let mut p = p.clone();
        p["turn"]["source"] = json!(if truth(&p["turn"]["execution_ref"]) {
            "CLINX_MANAGED"
        } else {
            "CODEX_NATIVE"
        });
        let r = self.fact(text(&p["node_id"]))?["record"].clone();
        if truth(&self.a["canonical_path"]) {
            let association = binding(
                &json!({"path":self.a["canonical_path"],"node":p["node_id"],"provider":p["provider"],"thread":p["native_thread_id"],"user":self.a["user_scope"]}),
            )?;
            if !association.is_null()
                && !r.is_null()
                && parsed(&association, "first_provenance_json")?["node_fingerprint"]
                    == r["public_key_fingerprint"]
            {
                p["task_ref"] = association["task_id"].clone();
                p["source"] = association["origin"].clone();
                p["binding_evidence"] = json!(if association["origin"] == "CODEX_NATIVE" {
                    "NATIVE_REGISTERED"
                } else {
                    "CANONICAL_ROUTE"
                });
            } else if truth(&p["task_ref"]) {
                p["task_ref"] = Value::Null;
                p["binding_evidence"] = json!("CANONICAL_UNVERIFIED");
            }
        }
        let scope = self.scope(text(&p["node_id"]))?;
        let available = !r.is_null() && !node_scope::stale(&r) && r["state"] == "ONLINE";
        let duplicates = query(
            c,
            "SELECT payload FROM items WHERE user=? AND provider=? AND thread=? AND node<>? LIMIT 65",
            &[
                sql(&self.a["user_scope"]),
                sql(&p["provider"]),
                sql(&p["native_thread_id"]),
                sql(&p["node_id"]),
            ],
        )?;
        let mut conflict = false;
        for row in duplicates {
            if self.allowed(&parsed(&row, "payload")?)? {
                conflict = true;
                break;
            }
        }
        if conflict {
            p["coverage"] = json!("MULTI_SOURCE_THREAD_IDENTITY");
        }
        let t = p["turn"].clone();
        let proof = &t["live_observation"];
        let age = now() - proof["observed_at"].as_f64().unwrap_or(0.0);
        let proven = available
            && !conflict
            && t["native_state"] == "RUNNING"
            && proof["liveness"] == "LIVE"
            && proof["thread_id"] == p["native_thread_id"]
            && proof["turn_id"] == t["turn_id"]
            && truth(&t["turn_id"])
            && (0.0..30.0).contains(&age);
        let execute = scope.as_ref().is_some_and(|s| s.execute_tasks);
        let capable = r["capabilities"]
            .as_array()
            .is_some_and(|a| a.iter().any(|s| s == "execution.prepare"));
        let enabled = p["binding_evidence"] != "NATIVE_REGISTERED"
            && available
            && execute
            && truth(&p["task_ref"])
            && capable
            && !conflict;
        let reason = if p["binding_evidence"] == "NATIVE_REGISTERED" {
            "NATIVE_CONTROL_HANDOFF_REQUIRED"
        } else if conflict {
            "THREAD_IDENTITY_CONFLICT"
        } else if !available {
            "NODE_OFFLINE"
        } else if !execute {
            "READ_SESSIONS_ONLY"
        } else if !capable {
            "PROVIDER_CONTROL_UNAVAILABLE"
        } else if !truth(&p["task_ref"]) {
            "EXPLICIT_ADOPTION_REQUIRED"
        } else {
            "CANONICAL_AUTHORIZATION_REQUIRED"
        };
        p["display_state"] = if truth(&p["native_admission"]["archived"]) {
            json!("ARCHIVED")
        } else if proven {
            json!("RUNNING")
        } else if t["native_state"] == "RUNNING" {
            json!("UNKNOWN")
        } else {
            t["native_state"].clone()
        };
        let route = r["route_ids"]
            .as_array()
            .and_then(|v| v.first())
            .and_then(Value::as_str);
        p["codex_uri"] = json!(format!(
            "codex://threads/{}{}",
            text(&p["native_thread_id"]),
            route
                .map(|s| format!("?hostId={}", quote(s)))
                .unwrap_or_default()
        ));
        p["schema_version"] = json!(VERSION);
        p["received_at"] = json!(received);
        p["device_name"] = if r.is_null() {
            p["node_id"].clone()
        } else {
            r["display_name"].clone()
        };
        p["freshness"] = json!(if available && now() - received < 30.0 {
            "RECENT"
        } else if available {
            "STALE"
        } else {
            "OFFLINE"
        });
        p["liveness"] = json!(if proven { "PROVEN" } else { "NOT_PROVEN" });
        p["progress_percent"] = Value::Null;
        p["control"] = json!({"node_id":p["node_id"],"native_thread_id":p["native_thread_id"],"task_ref":p["task_ref"],"execution_ref":t["execution_ref"],"entrypoint":if truth(&p["task_ref"]){"clinx_prepare_execution"}else{"clinx_adopt_conversation"},"enabled":enabled,"reason":reason,"allowed_actions":[]});
        Ok(p)
    }
    fn list(&mut self, c: &Connection, r: &Value) -> Result<Value> {
        let limit = limit(r, 50)?;
        if !r["kind"].is_null() && !matches!(r["kind"].as_str(), Some("managed" | "external")) {
            return Err("INVALID_OBSERVATION_QUERY".into());
        }
        let mut filters = json!(["recent-v1", r["node"], r["project"], r["state"], r["kind"]]);
        if !r["native_thread_id"].is_null() {
            bounded(&r["native_thread_id"], false)?;
            filters
                .as_array_mut()
                .unwrap()
                .push(r["native_thread_id"].clone());
        }
        let cursor = &r["cursor"];
        let after = if truth(cursor) {
            self.cursor(c, &filters, Value::Null, cursor)?
        } else {
            json!([i64::MAX, "~"])
        };
        position(&after, 2)?;
        let mut predicates = vec!["user=?", "(ordinal < ? OR (ordinal = ? AND id > ?))"];
        let mut args = vec![
            sql(&self.a["user_scope"]),
            sql(&after[0]),
            sql(&after[0]),
            if truth(cursor) {
                sql(&after[1])
            } else {
                String::new().into()
            },
        ];
        if truth(&r["node"]) {
            predicates.push("node=?");
            args.push(sql(&r["node"]));
        }
        if truth(&r["native_thread_id"]) {
            predicates.push("thread=?");
            args.push(sql(&r["native_thread_id"]));
        }
        let rows = query(
            c,
            &format!(
                "SELECT * FROM items WHERE {} ORDER BY ordinal DESC,id LIMIT 501",
                predicates.join(" AND ")
            ),
            &args,
        )?;
        let mut items = Vec::new();
        let mut scanned = 0;
        let mut last = after;
        for row in rows.iter().take(500) {
            if items.len() >= limit {
                break;
            }
            scanned += 1;
            last = json!([row["ordinal"], row["id"]]);
            let p = parsed(row, "payload")?;
            if !self.allowed(&p)? {
                continue;
            }
            if truth(&r["project"]) && p["project"] != r["project"] {
                continue;
            }
            let projected = self.project(c, &p, row["received"].as_f64().unwrap_or(0.0))?;
            if truth(&r["state"])
                && r["state"] != projected["display_state"]
                && r["state"] != projected["freshness"]
            {
                continue;
            }
            if truth(&r["kind"]) && (p["source"] == "CLINX_MANAGED") != (r["kind"] == "managed") {
                continue;
            }
            items.push(projected);
        }
        let more = rows.len() > scanned;
        let evicted = first(
            c,
            "SELECT value FROM settings WHERE key='retention_evicted'",
            &[],
        )?["value"]
            .as_str()
            .and_then(|s| s.parse::<u64>().ok())
            .unwrap_or(0);
        let streams = query(
            c,
            "SELECT * FROM streams WHERE user=?",
            &[sql(&self.a["user_scope"])],
        )?;
        let mut sources = Vec::new();
        for record in self.nodes()? {
            if self.scope(text(&record["node_id"]))?.is_none() {
                continue;
            }
            let stream = streams
                .iter()
                .find(|s| s["node"] == record["node_id"])
                .cloned()
                .unwrap_or(Value::Null);
            sources.push(json!({"node_id":record["node_id"],"display_name":record["display_name"],"state":if node_scope::stale(&record){json!("OFFLINE")}else{record["state"].clone()},"coverage":if stream.is_null(){"AWAITING_BOOTSTRAP"}else{"RECEIVED"},"stream_id":stream["stream"],"ack_seq":stream["seq"],"source_generation":stream["source_generation"],"gap":stream["gap"],"last_upload_at":stream["received"]}));
        }
        for p in &items {
            if !self.allowed(p)? {
                return Err("SHARING_SCOPE_DENIED".into());
            }
        }
        Ok(
            json!({"schema_version":VERSION,"read_only":true,"items":items,"sources":sources,"next_cursor":if more{self.cursor(c,&filters,last,&Value::Null)?}else{Value::Null},"has_more":more,"coverage":{"codex_desktop":"PERSISTED_INDEX","codex_cli":"PERSISTED_INDEX","clinx":"CANONICAL_READ_ONLY","other_agents":"NOT_IMPLEMENTED","ephemeral_unpublished":"UNOBSERVABLE"},"retention_evicted":evicted,"observed_at":now()}),
        )
    }
    fn detail(&mut self, c: &Connection, r: &Value) -> Result<Value> {
        let limit = limit(r, 32)?;
        let id = &r["observation_id"];
        let row = first(
            c,
            "SELECT * FROM items WHERE id=? AND user=?",
            &[sql(id), sql(&self.a["user_scope"])],
        )?;
        if row.is_null() {
            return Err("OBSERVATION_NOT_FOUND".into());
        }
        let p = parsed(&row, "payload")?;
        if !self.allowed(&p)? {
            return Err("OBSERVATION_NOT_FOUND".into());
        }
        let context = json!(["history", id]);
        let before = if truth(&r["cursor"]) {
            self.cursor(c, &context, Value::Null, &r["cursor"])?
        } else {
            json!([i64::MAX, "\u{ffff}"])
        };
        position(&before, 2)?;
        let rows = query(
            c,
            "SELECT * FROM turns WHERE observation=? AND (ordinal,turn_key)<(?,?) ORDER BY ordinal DESC,turn_key DESC LIMIT ?",
            &[
                sql(id),
                sql(&before[0]),
                sql(&before[1]),
                ((limit + 1) as i64).into(),
            ],
        )?;
        let more = rows.len() > limit;
        let page = rows.iter().take(limit).collect::<Vec<_>>();
        let stream = first(
            c,
            "SELECT * FROM streams WHERE node=? AND user=?",
            &[sql(&p["node_id"]), sql(&self.a["user_scope"])],
        )?;
        let turns = page
            .iter()
            .map(|t| {
                let mut p = parsed(t, "payload")?;
                p["source"] = json!(if truth(&p["execution_ref"]) {
                    "CLINX_MANAGED"
                } else {
                    "CODEX_NATIVE"
                });
                Ok(p)
            })
            .collect::<Result<Vec<_>>>()?;
        let item = self.project(c, &p, row["received"].as_f64().unwrap_or(0.0))?;
        if !self.allowed(&p)? {
            return Err("SHARING_SCOPE_DENIED".into());
        }
        Ok(
            json!({"schema_version":VERSION,"item":item,"turns":turns,"has_more":more,"next_cursor":if more{let last=page.last().unwrap();self.cursor(c,&context,json!([last["ordinal"],last["turn_key"]]),&Value::Null)?}else{Value::Null},"gap":if stream.is_null(){json!("UNKNOWN")}else{stream["gap"].clone()},"content_status":"CACHED_PUBLIC_SUMMARIES","read_only":true}),
        )
    }
    fn activity(&mut self, c: &Connection, r: &Value) -> Result<Value> {
        self.detail(c, &json!({"observation_id":r["observation_id"],"limit":1}))?;
        let limit = limit(r, 50)?;
        let id = &r["observation_id"];
        let context = json!(["activity", id]);
        let before = if truth(&r["cursor"]) {
            self.cursor(c, &context, Value::Null, &r["cursor"])?
        } else {
            json!([1e20, "~", i64::MAX])
        };
        position(&before, 3)?;
        let rows = query(
            c,
            "SELECT * FROM events WHERE observation=? AND user=? AND payload IS NOT NULL AND (received,stream,seq)<(?,?,?) ORDER BY received DESC,stream DESC,seq DESC LIMIT ?",
            &[
                sql(id),
                sql(&self.a["user_scope"]),
                sql(&before[0]),
                sql(&before[1]),
                sql(&before[2]),
                ((limit + 1) as i64).into(),
            ],
        )?;
        let page = rows.iter().take(limit).collect::<Vec<_>>();
        let mut items = Vec::new();
        for row in &page {
            let p = parsed(row, "payload")?;
            let t = &p["turn"];
            items.push(json!({"event_id":format!("{}:{}",text(&row["stream"]),row["seq"]),"turn_id":t["turn_id"],"execution_ref":t["execution_ref"],"source_seq":row["seq"],"recorded_at":row["received"],"source":t.get("source").cloned().unwrap_or(json!(if truth(&t["execution_ref"]){"CLINX_MANAGED"}else{"CODEX_NATIVE"})),"provenance":{"node_id":row["node"],"stream_id":row["stream"],"seq":row["seq"],"digest":row["digest"]},"source_updated_at":p["source_updated_at"],"native_state":t["native_state"],"business_result":t["business_result"],"kind":if history::terminal(text(&t["native_state"]))||truth(&t["business_result"]){"result"}else{"progress"},"text":if truth(&t["summary"]){&t["summary"]}else{&t["progress"]},"artifacts":t["artifacts"]}));
        }
        self.detail(c, &json!({"observation_id":id,"limit":1}))?;
        let evicted = first(
            c,
            "SELECT value FROM settings WHERE key='retention_evicted'",
            &[],
        )?["value"]
            .as_str()
            .and_then(|s| s.parse::<u64>().ok())
            .unwrap_or(0);
        let more = rows.len() > limit;
        Ok(
            json!({"schema_version":VERSION,"observation_id":id,"items":items,"read_only":true,"coverage":if evicted>0{"RETENTION_GAP"}else{"RECEIVED_EVENTS_ONLY"},"next_cursor":if more{let last=page.last().unwrap();self.cursor(c,&context,json!([last["received"],last["stream"],last["seq"]]),&Value::Null)?}else{Value::Null},"has_more":more}),
        )
    }
    fn context(&mut self, c: &Connection, r: &Value) -> Result<Value> {
        let item =
            self.detail(c, &json!({"observation_id":r["observation_id"],"limit":1}))?["item"]
                .clone();
        let mut result = None;
        if self.a["local_identity"]["node_id"] == item["node_id"] && truth(&self.a["native_home"]) {
            let h = history::History::new(text(&self.a["native_home"]));
            let m = h.metadata(text(&item["native_thread_id"]))?;
            if !m.is_null() && m["cwd"] == item["project"] {
                let mut context = h.context(
                    text(&item["native_thread_id"]),
                    text(&item["node_id"]),
                    &m,
                    history::ContextLimits {
                        recent: 4,
                        budget: 16000,
                    },
                    &Value::Null,
                    &r["cursor"],
                )?;
                context["read_only"] = json!(true);
                result = Some(context);
            }
        }
        let mut result = if let Some(result) = result {
            result
        } else if self.a["remote"] != true || item["freshness"] == "OFFLINE" {
            json!({"schema_version":VERSION,"context_status":"UNCACHED_CONTENT_UNAVAILABLE","read_only":true})
        } else {
            let record = self.fact(text(&item["node_id"]))?["record"].clone();
            (self.call)(
                "directory.remote",
                json!({"record":record,"request":{"operation":"session.read","protocol_version":node_scope::PROTOCOL,"user_scope":self.a["user_scope"],"provider":item["provider"],"thread_id":item["native_thread_id"],"cursor":r["cursor"],"recent_turns":4,"max_bytes":16000}}),
            )?
        };
        // A remote read can overlap a newly committed projection. Recheck from
        // a fresh snapshot so the original transaction cannot hide that change.
        let current_snapshot = readonly(
            Path::new(text(&self.a["path"])),
            "OBSERVATION_UNAVAILABLE",
            false,
        )?;
        let current = self.detail(
            &current_snapshot,
            &json!({"observation_id":r["observation_id"],"limit":1}),
        )?["item"]
            .clone();
        for k in [
            "node_id",
            "project",
            "native_thread_id",
            "task_ref",
            "source",
            "binding_evidence",
        ] {
            if current[k] != item[k] {
                return Err("EXECUTION_TURN_IDENTITY_CONFLICT".into());
            }
        }
        if current["turn"] != item["turn"] {
            return Err("EXECUTION_TURN_IDENTITY_CONFLICT".into());
        }
        result["schema_version"] = json!(VERSION);
        Ok(result)
    }
}
fn quote(s: &str) -> String {
    s.bytes()
        .map(|b| {
            if b.is_ascii_alphanumeric() || b"-_.~".contains(&b) {
                (b as char).to_string()
            } else {
                format!("%{b:02X}")
            }
        })
        .collect()
}
fn limit(r: &Value, default: usize) -> Result<usize> {
    match r.get("limit") {
        None => Ok(default),
        Some(v) => v
            .as_u64()
            .filter(|n| (1..=100).contains(n))
            .map(|n| n as usize)
            .ok_or("INVALID_OBSERVATION_QUERY".into()),
    }
}
fn position(v: &Value, len: usize) -> Result<()> {
    if v.as_array().is_some_and(|a| a.len() == len) {
        Ok(())
    } else {
        Err("INVALID_OBSERVATION_CURSOR".into())
    }
}
pub fn invoke(
    op: &str,
    a: &Value,
    call: impl FnMut(&str, Value) -> Result<Value>,
) -> Result<Value> {
    match op {
        "observation.compact" => return Ok(json!(compact(&a["value"]))),
        "observation.clean" => {
            return clean(&a["value"], a["limit"].as_u64().unwrap_or(1000) as usize);
        }
        "observation.id" => {
            return Ok(json!(observation_id(
                text(&a["node"]),
                text(&a["user"]),
                text(&a["provider"]),
                text(&a["thread"])
            )));
        }
        "observation.validate" => {
            return validate(&a["event"], text(&a["node"]), &Scope::parse(&a["scope"])?);
        }
        "observation.init" => return init(Path::new(text(&a["path"]))),
        "observation.accept" => return accept(a),
        "observation.binding" => return binding(a),
        _ => {}
    }
    let mut d = Directory { a, call };
    if op == "observation.scope" {
        return Ok(d
            .scope(text(&a["node"]))?
            .map(|s| s.value())
            .unwrap_or(Value::Null));
    }
    let c = readonly(
        Path::new(text(&a["path"])),
        "OBSERVATION_UNAVAILABLE",
        false,
    )?;
    match op {
        "observation.list" => d.list(&c, &a["request"]),
        "observation.detail" => d.detail(&c, &a["request"]),
        "observation.activity" => d.activity(&c, &a["request"]),
        "observation.context" => d.context(&c, &a["request"]),
        _ => Err("OBSERVATION_OPERATION_UNKNOWN".into()),
    }
}
