//! Existing Node routing index and request ledger. Never a task-state database.
//! Transactions retain the deployed schema and exact historical payload hashes.
use crate::node_tls::{Result, now};
use rusqlite::{Connection, OptionalExtension, Row, params, types::ValueRef};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{sync::Mutex, time::Duration};
#[derive(Default)]
pub struct Registry {
    connection: Mutex<Option<Connection>>,
}
fn db<T>(r: rusqlite::Result<T>) -> Result<T> {
    r.map_err(|_| "NODE_REGISTRY_UNAVAILABLE".into())
}
fn s<'a>(v: &'a Value, k: &str) -> Result<&'a str> {
    v[k].as_str().ok_or("NODE_REGISTRY_ARGUMENT_INVALID".into())
}
fn encoded(v: &Value) -> String {
    serde_json::to_string(v).unwrap()
}
fn row(r: &Row) -> rusqlite::Result<Value> {
    let mut out = serde_json::Map::new();
    for i in 0..r.as_ref().column_count() {
        let k = r.as_ref().column_name(i)?;
        let v = match r.get_ref(i)? {
            ValueRef::Null => Value::Null,
            ValueRef::Integer(n) => json!(n),
            ValueRef::Real(n) => json!(n),
            ValueRef::Text(t) => {
                json!(std::str::from_utf8(t).map_err(|_| rusqlite::Error::InvalidQuery)?)
            }
            _ => return Err(rusqlite::Error::InvalidQuery),
        };
        out.insert(k.into(), v);
    }
    Ok(Value::Object(out))
}
fn record(mut v: Value) -> Result<Value> {
    for (old, new) in [
        ("providers_json", "providers"),
        ("capabilities_json", "capabilities"),
        ("route_ids_json", "route_ids"),
    ] {
        v[new] = serde_json::from_str(s(&v, old)?).map_err(|_| "INVALID_NODE_RECORD")?;
        v.as_object_mut().unwrap().remove(old);
    }
    for k in ["trusted", "authorized", "version_compatible"] {
        v[k] = json!(v[k] == 1);
    }
    Ok(v)
}
fn error(code: &str, details: Value) -> Value {
    json!({"registry_error":{"error_code":code,"details":details}})
}
fn write_scope(c: &Connection, id: &str, scope: &Value) -> Result<()> {
    let scope = crate::node_scope::Scope::parse(scope)?;
    db(c.execute("INSERT INTO sharing_scopes VALUES (?,?,?,?,?,?,?) ON CONFLICT(node_id,user_scope) DO UPDATE SET read_sessions=excluded.read_sessions,execute_tasks=excluded.execute_tasks,providers_json=excluded.providers_json,revoked=excluded.revoked,updated_at=excluded.updated_at",params![id,scope.user_scope,scope.read_sessions,scope.execute_tasks,encoded(&json!(scope.providers)),scope.revoked,scope.updated_at]))?;
    db(c.execute(
        "INSERT OR REPLACE INTO observation_projects VALUES (?,?,?)",
        params![id, scope.user_scope, encoded(&json!(scope.projects))],
    ))?;
    Ok(())
}
impl Registry {
    /// Observer identity reads never initialize schema or take a writer lock.
    pub fn read(a: &Value) -> Result<Value> {
        let op = s(a, "method")?;
        if !matches!(
            op,
            "registry.get" | "registry.list_nodes" | "registry.scope"
        ) {
            return Err("OBSERVATION_READ_ONLY".into());
        }
        let c = crate::read_projection::readonly(
            std::path::Path::new(s(a, "path")?),
            "NODE_REGISTRY_UNAVAILABLE",
            false,
        )?;
        Self::execute(&c, op, &a["arguments"])
    }
    pub fn invoke(&self, op: &str, a: &Value) -> Result<Value> {
        let mut lock = self
            .connection
            .lock()
            .map_err(|_| "NODE_REGISTRY_UNAVAILABLE")?;
        if op == "registry.init" {
            if lock.is_some() {
                return Err("NODE_REGISTRY_ALREADY_OPEN".into());
            }
            let c = db(Connection::open(s(a, "path")?))?;
            db(c.busy_timeout(Duration::from_secs(2)))?;
            db(c.execute_batch("PRAGMA journal_mode=WAL;"))?;
            db(c.execute_batch(include_str!("node_registry.sql")))?;
            *lock = Some(c);
            return Ok(Value::Null);
        }
        let c = lock.as_mut().ok_or("NODE_REGISTRY_CLOSED")?;
        // BEGIN IMMEDIATE makes identity, request and fence checks atomic across
        // distinct Node workers sharing the same existing index.
        let tx = db(c.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate))?;
        let result = Self::execute(&tx, op, a)?;
        db(tx.commit())?;
        Ok(result)
    }
    fn execute(c: &Connection, op: &str, a: &Value) -> Result<Value> {
        match op {
            "registry.register" => {
                let r = &a["record"];
                let id = s(r, "node_id")?;
                let fp = s(r, "public_key_fingerprint")?;
                if !fp.is_empty() {
                    let other:Option<String>=db(c.query_row("SELECT node_id FROM nodes WHERE public_key_fingerprint=? AND node_id<>?",params![fp,id],|r|r.get(0)).optional())?;
                    if let Some(other) = other {
                        return Ok(error(
                            "DUPLICATE_NODE_IDENTITY",
                            json!({"existing_node_id":other}),
                        ));
                    }
                }
                let old: Option<String> = db(c
                    .query_row(
                        "SELECT public_key_fingerprint FROM nodes WHERE node_id=?",
                        [id],
                        |r| r.get(0),
                    )
                    .optional())?;
                if old.as_deref().is_some_and(|p| p != fp) {
                    return Ok(error("NODE_IDENTITY_CONFLICT", json!({})));
                }
                db(c.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET user_scope=excluded.user_scope,protocol_version=excluded.protocol_version,providers_json=excluded.providers_json,capabilities_json=excluded.capabilities_json,route_ids_json=excluded.route_ids_json,display_name=excluded.display_name,endpoint=excluded.endpoint,state=excluded.state,last_seen=excluded.last_seen,stale_after_seconds=excluded.stale_after_seconds,trusted=excluded.trusted,authorized=excluded.authorized,version_compatible=excluded.version_compatible,identity_generation=excluded.identity_generation",params![id,s(r,"user_scope")?,fp,s(r,"protocol_version")?,encoded(&r["providers"]),encoded(&r["capabilities"]),encoded(&r["route_ids"]),s(r,"display_name")?,r["endpoint"].as_str(),s(r,"state")?,r["last_seen"].as_str(),r["stale_after_seconds"].as_i64(),r["trusted"]==true,r["authorized"]==true,r["version_compatible"]==true,r["identity_generation"].as_i64()]))?;
                if !a["scope"].is_null() {
                    write_scope(c, id, &a["scope"])?;
                }
                Ok(r.clone())
            }
            "registry.get" => db(c
                .query_row(
                    "SELECT * FROM nodes WHERE node_id=?",
                    [s(a, "node_id")?],
                    row,
                )
                .optional())?
            .map(record)
            .transpose()
            .map(|v| v.unwrap_or(Value::Null)),
            "registry.list_nodes" => {
                let mut stmt=db(c.prepare("SELECT * FROM nodes WHERE (? IS NULL OR user_scope=?) AND (?=0 OR (authorized=1 AND trusted=1)) ORDER BY node_id"))?;
                let rows = db(stmt.query_map(
                    params![
                        a["user_scope"].as_str(),
                        a["user_scope"].as_str(),
                        a["authorized_only"] == true
                    ],
                    row,
                ))?;
                let values = rows.map(|r| record(db(r)?)).collect::<Result<Vec<_>>>()?;
                Ok(json!(values))
            }
            "registry.scope" => {
                let id = s(a, "node_id")?;
                let user = s(a, "user_scope")?;
                let r = db(c
                    .query_row(
                        "SELECT * FROM sharing_scopes WHERE node_id=? AND user_scope=?",
                        params![id, user],
                        row,
                    )
                    .optional())?;
                let Some(r) = r else {
                    return Ok(Value::Null);
                };
                let projects:Option<String>=db(c.query_row("SELECT projects_json FROM observation_projects WHERE node_id=? AND user_scope=?",params![id,user],|r|r.get(0)).optional())?;
                Ok(
                    json!({"user_scope":r["user_scope"],"read_sessions":r["read_sessions"]==1,"execute_tasks":r["execute_tasks"]==1,"revoked":r["revoked"]==1,"providers":serde_json::from_str::<Value>(s(&r,"providers_json")?).map_err(|_|"INVALID_SHARING_SCOPE")?,"projects":serde_json::from_str::<Value>(projects.as_deref().unwrap_or("[\"*\"]")).map_err(|_|"INVALID_SHARING_SCOPE")?,"updated_at":r["updated_at"]}),
                )
            }
            "registry.authorize" => {
                let id = s(a, "node_id")?;
                let scope = &a["scope"];
                if !db(c
                    .query_row("SELECT 1 FROM nodes WHERE node_id=?", [id], |r| {
                        r.get::<_, i64>(0)
                    })
                    .optional())?
                .is_some()
                {
                    return Ok(error("UNKNOWN_NODE", json!({})));
                }
                write_scope(c, id, scope)?;
                db(c.execute(
                    "UPDATE nodes SET user_scope=?,authorized=?,state=? WHERE node_id=?",
                    params![
                        s(scope, "user_scope")?,
                        scope["revoked"] != true,
                        if scope["revoked"] == true {
                            "REVOKED"
                        } else {
                            "ONLINE"
                        },
                        id
                    ],
                ))?;
                Ok(Value::Null)
            }
            "registry.revoke" => {
                let id = s(a, "node_id")?;
                db(c.execute(
                    "UPDATE nodes SET authorized=0,state='REVOKED' WHERE node_id=?",
                    [id],
                ))?;
                db(c.execute("UPDATE sharing_scopes SET revoked=1,updated_at=? WHERE node_id=? AND user_scope=?",params![now(),id,s(a,"user_scope")?]))?;
                Ok(Value::Null)
            }
            "registry.touch" => {
                let state = s(a, "state")?;
                if !matches!(state, "ONLINE" | "DEGRADED" | "OFFLINE" | "UNKNOWN") {
                    return Err("INVALID_NODE_STATE".into());
                }
                db(c.execute(
                    "UPDATE nodes SET state=?,last_seen=? WHERE node_id=?",
                    params![state, now(), s(a, "node_id")?],
                ))?;
                Ok(Value::Null)
            }
            "registry.update_endpoint" => {
                db(c.execute(
                    "UPDATE nodes SET endpoint=? WHERE node_id=?",
                    params![s(a, "endpoint")?, s(a, "node_id")?],
                ))?;
                Ok(Value::Null)
            }
            "registry.refresh_states" => {
                let mut stmt = db(c.prepare("SELECT * FROM nodes"))?;
                let rows = db(stmt.query_map([], row))?;
                for r in rows {
                    let r = db(r)?;
                    if r["state"] != "REVOKED"
                        && r["state"] != "VERSION_INCOMPATIBLE"
                        && crate::node_scope::stale(&r)
                    {
                        db(c.execute(
                            "UPDATE nodes SET state='OFFLINE' WHERE node_id=?",
                            [s(&r, "node_id")?],
                        ))?;
                    }
                }
                Ok(Value::Null)
            }
            "registry.bind_reference" => {
                let reference = s(a, "reference")?;
                let id = s(a, "node_id")?;
                let user = s(a, "user_scope")?;
                let old: Option<(String, String)> = db(c
                    .query_row(
                        "SELECT node_id,user_scope FROM reference_routes WHERE reference=?",
                        [reference],
                        |r| Ok((r.get(0)?, r.get(1)?)),
                    )
                    .optional())?;
                if old.is_some_and(|v| v != (id.into(), user.into())) {
                    return Ok(error("REFERENCE_ROUTE_CONFLICT", json!({})));
                }
                db(c.execute(
                    "INSERT OR IGNORE INTO reference_routes VALUES (?,?,?)",
                    params![reference, id, user],
                ))?;
                Ok(Value::Null)
            }
            "registry.reference_node" => {
                let r: Option<(String, String)> = db(c
                    .query_row(
                        "SELECT node_id,user_scope FROM reference_routes WHERE reference=?",
                        [s(a, "reference")?],
                        |r| Ok((r.get(0)?, r.get(1)?)),
                    )
                    .optional())?;
                if let Some((id, user)) = r {
                    if user != s(a, "user_scope")? {
                        return Ok(error("USER_SCOPE_DENIED", json!({})));
                    }
                    Ok(json!(id))
                } else {
                    Ok(Value::Null)
                }
            }
            "registry.claim_request" => {
                let wire = s(a, "payload_wire")?;
                let parsed: Value =
                    serde_json::from_str(wire).map_err(|_| "NODE_REGISTRY_ARGUMENT_INVALID")?;
                if parsed != a["payload"] {
                    return Err("NODE_REGISTRY_ARGUMENT_INVALID".into());
                }
                let digest = format!("{:x}", Sha256::digest(wire.as_bytes()));
                let id = s(a, "node_id")?;
                let request = s(a, "request_id")?;
                let op = s(a, "operation")?;
                let old = db(c
                    .query_row(
                        "SELECT * FROM request_ledger WHERE node_id=? AND request_id=?",
                        params![id, request],
                        row,
                    )
                    .optional())?;
                if let Some(r) = old {
                    if r["operation"] != op || r["payload_hash"] != digest {
                        return Ok(error("IDEMPOTENCY_KEY_REUSE", json!({})));
                    }
                    let response = if let Some(s) = r["response_json"].as_str() {
                        serde_json::from_str(s).map_err(|_| "NODE_REGISTRY_UNAVAILABLE")?
                    } else {
                        Value::Null
                    };
                    return Ok(json!([r["state"], response]));
                }
                db(c.execute(
                    "INSERT INTO request_ledger VALUES (?,?,?,?,?,?,?)",
                    params![
                        id,
                        request,
                        op,
                        digest,
                        "IN_FLIGHT",
                        Option::<String>::None,
                        now()
                    ],
                ))?;
                Ok(json!(["NEW", null]))
            }
            "registry.finish_request" => {
                db(c.execute("UPDATE request_ledger SET state=?,response_json=?,updated_at=? WHERE node_id=? AND request_id=?",params![s(a,"state")?,encoded(&a["response"]),now(),s(a,"node_id")?,s(a,"request_id")?]))?;
                Ok(Value::Null)
            }
            "registry.acquire_fence" | "registry.release_fence" => {
                let i = &a["identity"];
                let id = s(i, "node_id")?;
                let user = s(i, "user_scope")?;
                let provider = s(i, "provider")?;
                let tid = s(i, "native_thread_id")?;
                let exec = s(a, "execution_ref")?;
                if op == "registry.release_fence" {
                    db(c.execute("UPDATE route_fences SET owner_execution=NULL WHERE node_id=? AND user_scope=? AND provider=? AND native_thread_id=? AND owner_execution=?",params![id,user,provider,tid,exec]))?;
                    return Ok(Value::Null);
                }
                let old:Option<(i64,Option<String>)>=db(c.query_row("SELECT fence,owner_execution FROM route_fences WHERE node_id=? AND user_scope=? AND provider=? AND native_thread_id=?",params![id,user,provider,tid],|r|Ok((r.get(0)?,r.get(1)?))).optional())?;
                if let Some((f, Some(owner))) = &old
                    && owner != exec
                {
                    return Ok(error(
                        "SINGLE_WRITER_BUSY",
                        json!({"owner_execution":owner,"fence":f}),
                    ));
                }
                let f = old.map(|v| v.0).unwrap_or(0) + 1;
                db(c.execute("INSERT INTO route_fences VALUES (?,?,?,?,?,?) ON CONFLICT(node_id,user_scope,provider,native_thread_id) DO UPDATE SET fence=excluded.fence,owner_execution=excluded.owner_execution",params![id,user,provider,tid,f,exec]))?;
                Ok(json!(f))
            }
            "registry.update_thread" => {
                let i = &a["identity"];
                db(c.execute("INSERT INTO thread_index VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(node_id,user_scope,provider,native_thread_id,source) DO UPDATE SET source_version=excluded.source_version,last_seen=excluded.last_seen,status_json=excluded.status_json",params![s(i,"node_id")?,s(i,"user_scope")?,s(i,"provider")?,s(i,"native_thread_id")?,s(a,"source")?,s(i,"source_version")?,a["observed_at"].as_str().unwrap_or(&now()),encoded(&a["status"])]))?;
                Ok(Value::Null)
            }
            "registry.lookup_thread" => {
                let mut stmt=db(c.prepare("SELECT * FROM thread_index WHERE native_thread_id=? AND user_scope=? AND provider=? ORDER BY node_id,source"))?;
                let rows = db(stmt.query_map(
                    params![
                        s(a, "native_thread_id")?,
                        s(a, "user_scope")?,
                        s(a, "provider")?
                    ],
                    row,
                ))?;
                let mut result = Vec::new();
                for r in rows {
                    let mut r = db(r)?;
                    r["status"] = serde_json::from_str(s(&r, "status_json")?)
                        .map_err(|_| "NODE_REGISTRY_UNAVAILABLE")?;
                    result.push(r);
                }
                Ok(json!(result))
            }
            _ => Err("NODE_REGISTRY_OPERATION_UNKNOWN".into()),
        }
    }
}
