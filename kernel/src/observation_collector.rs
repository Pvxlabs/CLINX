//! Durable node observation collection. Owns the existing spool and file lock;
//! never submits execution input or writes Task Authority storage.
use crate::{
    node_scope::{self, Scope},
    node_tls::Result,
    observation,
    read_projection::*,
};
use rusqlite::{Connection, params};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    fs::{self, File, OpenOptions},
    io::Read,
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::Path,
    sync::Mutex,
    time::Duration,
};
const PAGE: usize = 16;
const MAX_BATCH: usize = 32;
const OUTBOX: usize = 2048;
const SESSIONS: usize = 20_000;
fn db<T>(r: rusqlite::Result<T>) -> Result<T> {
    r.map_err(|_| "COLLECTOR_STORE_UNAVAILABLE".into())
}
fn query(c: &Connection, q: &str, a: &[rusqlite::types::Value]) -> Result<Vec<Value>> {
    rows(c, q, a, "COLLECTOR_STORE_UNAVAILABLE")
}
fn get(c: &Connection, key: &str, default: Value) -> Result<Value> {
    let rows = query(
        c,
        "SELECT value FROM meta WHERE key=?",
        &[key.to_owned().into()],
    )?;
    match rows.first() {
        None => Ok(default),
        Some(r) if key == "stream" => Ok(r["value"].clone()),
        Some(r) => serde_json::from_str(text(&r["value"]))
            .map_err(|_| "COLLECTOR_STORE_UNAVAILABLE".into()),
    }
}
fn put(c: &Connection, key: &str, value: &Value) -> Result<()> {
    db(c.execute(
        "INSERT OR REPLACE INTO meta VALUES (?,?)",
        params![key, compact(value)],
    ))?;
    Ok(())
}
fn count(c: &Connection, table: &str) -> Result<usize> {
    Ok(
        query(c, &format!("SELECT count(*) AS n FROM {table}"), &[])?[0]["n"]
            .as_u64()
            .unwrap_or(0) as usize,
    )
}
fn stream_id() -> Result<String> {
    let mut b = [0u8; 16];
    File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut b))
        .map_err(|_| "COLLECTOR_STORE_UNAVAILABLE")?;
    b[6] = (b[6] & 15) | 64;
    b[8] = (b[8] & 63) | 128;
    let h = b.iter().map(|b| format!("{b:02x}")).collect::<String>();
    Ok(format!(
        "{}-{}-{}-{}-{}",
        &h[..8],
        &h[8..12],
        &h[12..16],
        &h[16..20],
        &h[20..]
    ))
}
struct Collector {
    connection: Connection,
    _lock: File,
    status: String,
    sampled: u64,
}
impl Collector {
    fn open(a: &Value) -> Result<Self> {
        let p = Path::new(a["path"].as_str().ok_or("COLLECTOR_PATH_REQUIRED")?);
        if let Some(parent) = p.parent() {
            fs::create_dir_all(parent).map_err(|_| "COLLECTOR_STORE_UNAVAILABLE")?;
        }
        let mut lock = p.to_path_buf();
        lock.set_extension("lock");
        let lock = OpenOptions::new()
            .create(true)
            .append(true)
            .mode(0o600)
            .open(lock)
            .map_err(|_| "COLLECTOR_STORE_UNAVAILABLE")?;
        rustix::fs::flock(&lock, rustix::fs::FlockOperation::NonBlockingLockExclusive)
            .map_err(|_| "COLLECTOR_ALREADY_RUNNING")?;
        let c = db(Connection::open(p))?;
        db(c.busy_timeout(Duration::from_secs(2)))?;
        db(c.execute_batch("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,metadata TEXT NOT NULL,state TEXT NOT NULL,checked REAL NOT NULL);
CREATE TABLE IF NOT EXISTS outbox(seq INTEGER PRIMARY KEY,payload TEXT NOT NULL);"))?;
        db(c.execute(
            "INSERT OR IGNORE INTO meta VALUES ('stream',?)",
            [stream_id()?],
        ))?;
        db(c.execute("INSERT OR IGNORE INTO meta VALUES ('seq','0')", []))?;
        fs::set_permissions(p, fs::Permissions::from_mode(0o600))
            .map_err(|_| "COLLECTOR_STORE_UNAVAILABLE")?;
        Ok(Self {
            connection: c,
            _lock: lock,
            status: "BOOTSTRAP".into(),
            sampled: 0,
        })
    }
    fn state(&self) -> Value {
        json!({"status":self.status,"sampled":self.sampled})
    }
    fn tick(
        &mut self,
        a: &Value,
        mut call: impl FnMut(&str, Value) -> Result<Value>,
    ) -> Result<u64> {
        let raw = call("collector.scope", json!({}))?;
        let local = node_scope::optional(&raw)?;
        let c = &self.connection;
        if local
            .as_ref()
            .is_none_or(|s| !s.allows("observation.upload"))
        {
            let stored = get(c, "effective_scope", Value::Null)?;
            let denied = if let Some(s) = local {
                s.value()
            } else if stored.is_object() {
                json!({"user_scope":stored["user_scope"],"revoked":true})
            } else {
                Value::Null
            };
            if !denied.is_null() {
                call("collector.heartbeat", json!({"scope":denied}))?;
            }
            self.status = "SHARING_SCOPE_DENIED".into();
            return Ok(2);
        }
        let local = local.unwrap();
        let reply = call("collector.heartbeat", json!({"scope":local.value()}))?;
        let online = reply["offline"] != true;
        let effective = if online {
            if reply["registered"] != true {
                put(c, "effective_scope", &Value::Null)?;
                self.status = reply["error_code"].as_str().unwrap_or("OFFLINE").into();
                return Ok(12);
            }
            let scope = Scope::parse(&reply["scope"])?;
            put(c, "effective_scope", &scope.value())?;
            scope
        } else {
            let saved = get(c, "effective_scope", Value::Null)?;
            if !truth(&saved) {
                self.status = "AWAITING_BILATERAL_APPROVAL".into();
                return Ok(12);
            }
            Scope::parse(&saved)?
        };
        let scope = node_scope::intersect(Some(local), Some(effective))?;
        if !scope.allows("observation.upload") {
            self.status = "SHARING_SCOPE_DENIED".into();
            return Ok(12);
        }
        let candidates = query(
            c,
            "SELECT * FROM outbox ORDER BY seq LIMIT ?",
            &[(MAX_BATCH as i64).into()],
        )?;
        let mut queued = Vec::new();
        let mut batch_bytes = 0;
        for row in candidates {
            let size = text(&row["payload"]).len() + 64;
            if !queued.is_empty() && batch_bytes + size > 150_000 {
                break;
            }
            batch_bytes += size;
            queued.push(row);
        }
        let mut online = online;
        if !queued.is_empty() && online {
            let stream = get(c, "stream", Value::Null)?;
            let events = queued
                .iter()
                .map(|r| -> Result<Value> {
                    let mut p: Value = serde_json::from_str(text(&r["payload"]))
                        .map_err(|_| "COLLECTOR_STORE_UNAVAILABLE")?;
                    if scope.project(&p["project"]) {
                        p["seq"] = r["seq"].clone();
                        Ok(p)
                    } else {
                        Ok(json!({"seq":r["seq"],"scope_redacted":true}))
                    }
                })
                .collect::<Result<Vec<_>>>()?;
            let response = call(
                "collector.upload",
                json!({"scope":scope.value(),"batch":{"stream_id":stream,"events":events,"previous_stream_id":a["previous_stream_id"]}}),
            )?;
            if response["offline"] == true {
                online = false;
            } else {
                if response["accepted"] != true {
                    self.status = response["error_code"]
                        .as_str()
                        .unwrap_or("UPLOAD_UNAVAILABLE")
                        .into();
                    return Ok(2);
                }
                let ack = response["ack_seq"]
                    .as_u64()
                    .ok_or("OBSERVATION_ACK_CONFLICT")?;
                let last = queued.last().unwrap()["seq"]
                    .as_u64()
                    .ok_or("COLLECTOR_STORE_UNAVAILABLE")?;
                if ack > last || response.get("stream_id").is_some_and(|s| s != &stream) {
                    return Err("OBSERVATION_ACK_CONFLICT".into());
                }
                db(c.execute("DELETE FROM outbox WHERE seq<=?", [ack]))?;
            }
        }
        let limit = a["outbox_limit"].as_u64().unwrap_or(OUTBOX as u64) as usize;
        if count(c, "outbox")? >= limit.saturating_sub(128) {
            self.status = "OBSERVATION_BACKPRESSURE".into();
            return Ok(2);
        }
        let generation = call("collector.generation", json!({}))?;
        db(c.execute_batch("BEGIN IMMEDIATE"))?;
        let sampled = (|| -> Result<(Value, String, Option<u64>)> {
            if get(c, "generation", Value::Null)? != generation {
                put(c, "index_after", &Value::Null)?;
                put(c, "audit_after", &Value::Null)?;
                put(c, "generation", &generation)?;
                db(c.execute("UPDATE sessions SET state='{}'", []))?;
                self.status = "SOURCE_REBUILT".into();
            }
            let inventory = call(
                "collector.inventory",
                json!({"scope":scope.value(),"after":get(c,"index_after",Value::Null)?,"audit":false}),
            )?;
            let metadata = inventory[0]
                .as_array()
                .ok_or("INVALID_OBSERVATION")?
                .clone();
            let after = &inventory[1];
            let coverage = inventory[2]
                .as_str()
                .ok_or("INVALID_OBSERVATION")?
                .to_owned();
            if truth(after) {
                put(c, "index_after", after)?;
            } else {
                let previous = get(c, "index_after", Value::Null)?;
                let next = previous[0]
                    .as_f64()
                    .map(|n| json!([(n - 60.0).max(0.0) as i64, ""]))
                    .unwrap_or(Value::Null);
                put(c, "index_after", &next)?;
            }
            let audit = call(
                "collector.inventory",
                json!({"scope":scope.value(),"after":get(c,"audit_after",Value::Null)?,"audit":true}),
            )?;
            put(c, "audit_after", &audit[1])?;
            let managed = call(
                "collector.canonical_inventory",
                json!({"scope":scope.value(),"after":get(c,"canonical_after",json!(""))?}),
            )?;
            let managed = managed.as_array().ok_or("INVALID_OBSERVATION")?;
            put(
                c,
                "canonical_after",
                &managed.last().map(|m| m["id"].clone()).unwrap_or(json!("")),
            )?;
            let mut candidates: Vec<Value> = Vec::new();
            for m in managed
                .iter()
                .chain(audit[0].as_array().ok_or("INVALID_OBSERVATION")?)
                .chain(&metadata)
            {
                if let Some(i) = candidates.iter().position(|v| v["id"] == m["id"]) {
                    candidates[i] = m.clone();
                } else {
                    candidates.push(m.clone());
                }
            }
            for q in [
                "SELECT metadata FROM sessions WHERE json_extract(state,'$.native_observation_error') IS NOT NULL OR (json_extract(state,'$.reported_coverage') IS NULL AND json_extract(state,'$.hashes.metadata') IS NOT NULL) ORDER BY checked LIMIT 8",
                "SELECT metadata FROM sessions ORDER BY coalesce(json_extract(state,'$.active'),0) DESC,checked LIMIT 8",
            ] {
                for r in query(c, q, &[])? {
                    let m: Value = serde_json::from_str(text(&r["metadata"]))
                        .map_err(|_| "COLLECTOR_STORE_UNAVAILABLE")?;
                    if scope.project(&m["cwd"]) && !candidates.iter().any(|v| v["id"] == m["id"]) {
                        candidates.push(m);
                    }
                }
            }
            for m in candidates.iter().take(PAGE * 3 + 16) {
                if count(c, "outbox")? >= limit.saturating_sub(128) {
                    self.status = "OBSERVATION_BACKPRESSURE".into();
                    break;
                }
                if !scope.project(&m["cwd"]) {
                    continue;
                }
                let tid = m["id"].as_str().ok_or("INVALID_OBSERVATION")?;
                let prior = query(
                    c,
                    "SELECT state FROM sessions WHERE id=?",
                    &[tid.to_owned().into()],
                )?;
                if prior.is_empty() && count(c, "sessions")? >= SESSIONS {
                    self.status = "SESSION_RETENTION_LIMIT".into();
                    return Ok((json!(metadata), coverage, Some(2)));
                }
                let state = prior
                    .first()
                    .map(|r| serde_json::from_str(text(&r["state"])))
                    .transpose()
                    .map_err(|_| "COLLECTOR_STORE_UNAVAILABLE")?
                    .unwrap_or(json!({}));
                let sample = call("collector.sample", json!({"metadata":m,"state":state}))?;
                let mut state = sample[1].clone();
                let events = sample[0].as_array().ok_or("INVALID_OBSERVATION")?;
                if !state.is_object() {
                    return Err("INVALID_OBSERVATION".into());
                }
                let signature = json!(
                    events
                        .iter()
                        .map(|e| e["coverage"].as_str().unwrap_or("UNKNOWN").to_owned())
                        .collect::<BTreeSet<_>>()
                );
                let mut hashes = if state["reported_coverage"] == signature {
                    state["hashes"]
                        .as_object()
                        .map(|m| {
                            m.iter()
                                .map(|(k, v)| (k.clone(), v.clone()))
                                .collect::<Vec<_>>()
                        })
                        .unwrap_or_default()
                } else {
                    Vec::new()
                };
                state["reported_coverage"] = signature;
                for event in events {
                    let mut event = event.clone();
                    let title = observation::clean(&event["title"], 100)?;
                    event["title"] = if truth(&title) { title } else { json!(tid) };
                    for (field, n) in [("summary", 3000), ("progress", 1500)] {
                        if truth(&event["turn"][field]) {
                            event["turn"][field] = observation::clean(&event["turn"][field], n)?;
                        }
                    }
                    let key = ["execution_ref", "turn_id"]
                        .iter()
                        .map(|k| &event["turn"][k])
                        .find(|v| truth(v))
                        .map(|v| text(v).to_owned())
                        .unwrap_or("metadata".into());
                    let digest = json!(sha(compact(&event).as_bytes()));
                    if hashes.iter().any(|(k, v)| k == &key && v == &digest) {
                        continue;
                    }
                    if count(c, "outbox")? >= limit {
                        return Err("OBSERVATION_BACKPRESSURE".into());
                    }
                    let seq = get(c, "seq", json!(0))?
                        .as_u64()
                        .ok_or("COLLECTOR_STORE_UNAVAILABLE")?
                        + 1;
                    db(c.execute(
                        "INSERT INTO outbox VALUES (?,?)",
                        params![seq, compact(&event)],
                    ))?;
                    put(c, "seq", &json!(seq))?;
                    if let Some((_, v)) = hashes.iter_mut().find(|(k, _)| k == &key) {
                        *v = digest;
                    } else {
                        hashes.push((key, digest));
                    }
                }
                state["hashes"] = hashes
                    .into_iter()
                    .rev()
                    .take(128)
                    .collect::<serde_json::Map<_, _>>()
                    .into();
                db(c.execute(
                    "INSERT OR REPLACE INTO sessions VALUES (?,?,?,?)",
                    params![tid, compact(m), compact(&state), now()],
                ))?;
                self.sampled += 1;
            }
            Ok((json!(metadata), coverage, None))
        })();
        let (metadata, coverage, early) = match sampled {
            Ok(v) => {
                if let Err(error) = db(c.execute_batch("COMMIT")) {
                    // BUSY leaves a SQLite transaction open. Release the
                    // uncommitted checkpoint/outbox together before next tick.
                    let _ = c.execute_batch("ROLLBACK");
                    return Err(error);
                }
                v
            }
            Err(e) => {
                let _ = c.execute_batch("ROLLBACK");
                return Err(e);
            }
        };
        if let Some(delay) = early {
            return Ok(delay);
        }
        self.status = if online {
            coverage
        } else {
            "OFFLINE_BUFFERING".into()
        };
        let seq = get(c, "seq", json!(0))?;
        let changed = seq.as_u64() > get(c, "previous_tick_seq", json!(0))?.as_u64();
        put(c, "previous_tick_seq", &seq)?;
        let running = query(
            c,
            "SELECT state FROM sessions ORDER BY checked DESC LIMIT 56",
            &[],
        )?
        .iter()
        .any(|r| {
            serde_json::from_str::<Value>(text(&r["state"]))
                .ok()
                .is_some_and(|v| truth(&v["active"]))
        });
        Ok(
            if changed
                || !queued.is_empty()
                || running
                || metadata.as_array().is_some_and(|v| v.len() == PAGE)
            {
                2
            } else {
                12
            },
        )
    }
}
#[derive(Default)]
pub struct Runtime(Mutex<Option<Collector>>);
impl Runtime {
    pub fn invoke(
        &self,
        op: &str,
        a: &Value,
        call: impl FnMut(&str, Value) -> Result<Value>,
    ) -> Result<Value> {
        let mut guard = self.0.lock().map_err(|_| "COLLECTOR_STORE_UNAVAILABLE")?;
        if op == "collector.init" {
            if guard.is_some() {
                return Err("COLLECTOR_ALREADY_RUNNING".into());
            }
            *guard = Some(Collector::open(a)?);
            return Ok(guard.as_ref().unwrap().state());
        }
        if op == "collector.close" {
            *guard = None;
            return Ok(Value::Null);
        }
        let c = guard.as_mut().ok_or("COLLECTOR_NOT_OPEN")?;
        match op {
            "collector.state" => Ok(c.state()),
            "collector.get" => get(&c.connection, text(&a["key"]), a["default"].clone()),
            "collector.put" => {
                put(&c.connection, text(&a["key"]), &a["value"])?;
                Ok(Value::Null)
            }
            "collector.tick" => {
                let delay = c.tick(a, call)?;
                Ok(json!({"delay":delay,"status":c.status,"sampled":c.sampled}))
            }
            _ => Err("COLLECTOR_OPERATION_UNKNOWN".into()),
        }
    }
}
