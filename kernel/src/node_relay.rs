//! Disposable Provider chunk buffers. All submission decisions live in Rust.
//! Sealing precedes I/O; a lost callback/reply never resubmits Provider input.
use crate::{node_scope::Scope, node_tls::Result};
use base64::{Engine, engine::general_purpose::STANDARD};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    sync::Mutex,
    time::{Duration, Instant},
};
pub const MAX_REQUEST: usize = 1_048_576;
pub const MAX_RESPONSE: usize = 16 * 1024 * 1024;
pub const CHUNK: usize = 48 * 1024;
fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
enum State {
    Uploading(Vec<u8>),
    InFlight,
    Ready(Vec<u8>, String),
    Consumed,
}
struct Row {
    context: String,
    count: u64,
    digest: String,
    next: u64,
    state: State,
    deadline: Instant,
}
#[derive(Default)]
pub struct Relay {
    rows: Mutex<BTreeMap<String, Row>>,
}
impl Relay {
    pub fn count(&self) -> Result<usize> {
        Ok(self
            .rows
            .lock()
            .map_err(|_| "PROVIDER_BUFFER_UNAVAILABLE")?
            .len())
    }
    pub fn handle(
        &self,
        a: &Value,
        exchange: impl FnOnce(&Value, &Value) -> Result<Value>,
    ) -> Result<Value> {
        let r = &a["request"];
        let context = &r["context"];
        let scope = Scope::parse(&a["scope"])?;
        if !context.is_object()
            || context["host"] != a["node_id"]
            || context["user_scope"] != scope.user_scope
            || !scope.allows("execution.authority")
            || !scope.project(&context["cwd"])
        {
            return Err("PROVIDER_SCOPE_DENIED".into());
        }
        let id = r["request_id"]
            .as_str()
            .filter(|s| {
                s.len() == 32
                    && s.bytes()
                        .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
            })
            .ok_or("INVALID_PROVIDER_REQUEST_ID")?;
        // The private binding carries the original JSON encoding, preserving the
        // deployed context identity hash even for non-ASCII keys and key order.
        let wire = a["context_wire"]
            .as_str()
            .ok_or("PROVIDER_REQUEST_IDENTITY_CONFLICT")?;
        if serde_json::from_str::<Value>(wire).ok().as_ref() != Some(context) {
            return Err("PROVIDER_REQUEST_IDENTITY_CONFLICT".into());
        }
        let context_hash = hash(wire.as_bytes());
        let data;
        let generation;
        {
            let mut rows = self
                .rows
                .lock()
                .map_err(|_| "PROVIDER_BUFFER_UNAVAILABLE")?;
            let now = Instant::now();
            rows.retain(|_, row| row.deadline > now);
            match r["operation"].as_str() {
                Some("provider.upload") => {
                    let index = r["index"].as_u64().ok_or("INVALID_PROVIDER_CHUNK")?;
                    let count = r["count"]
                        .as_u64()
                        .filter(|n| *n > 0 && *n <= 22 && index < *n)
                        .ok_or("INVALID_PROVIDER_CHUNK")?;
                    let digest = r["digest"]
                        .as_str()
                        .filter(|s| s.chars().count() == 64)
                        .ok_or("INVALID_PROVIDER_CHUNK")?;
                    let chunk = STANDARD
                        .decode(r["data"].as_str().ok_or("INVALID_PROVIDER_CHUNK")?)
                        .map_err(|_| "INVALID_PROVIDER_CHUNK")?;
                    if chunk.len() > CHUNK {
                        return Err("INVALID_PROVIDER_CHUNK".into());
                    }
                    if !rows.contains_key(id) {
                        if index != 0
                            || rows.len() >= 4096
                            || rows
                                .values()
                                .filter(|r| !matches!(r.state, State::Consumed))
                                .count()
                                >= 16
                        {
                            return Err("PROVIDER_BUFFER_UNAVAILABLE".into());
                        }
                        rows.insert(
                            id.into(),
                            Row {
                                context: context_hash.clone(),
                                count,
                                digest: digest.into(),
                                next: 0,
                                state: State::Uploading(Vec::new()),
                                deadline: now + Duration::from_secs(120),
                            },
                        );
                    }
                    let row = rows.get_mut(id).unwrap();
                    if row.context != context_hash
                        || row.digest != digest
                        || row.count != count
                        || row.next != index
                    {
                        return Err("PROVIDER_REQUEST_IDENTITY_CONFLICT".into());
                    }
                    let State::Uploading(bytes) = &mut row.state else {
                        return Err("PROVIDER_REQUEST_IDENTITY_CONFLICT".into());
                    };
                    bytes.extend(chunk);
                    row.next += 1;
                    if bytes.len() > MAX_REQUEST {
                        return Err("PROVIDER_REQUEST_TOO_LARGE".into());
                    }
                    if index + 1 < count {
                        return Ok(json!({"request_id":id,"next_index":row.next}));
                    }
                    if hash(bytes) != digest {
                        return Err("PROVIDER_CHUNK_DIGEST_CONFLICT".into());
                    }
                    data = std::mem::take(bytes);
                    generation = row.deadline;
                    row.state = State::InFlight;
                }
                Some("provider.read") => {
                    let row = rows
                        .get_mut(id)
                        .filter(|r| r.context == context_hash)
                        .ok_or("PROVIDER_REPLY_UNAVAILABLE")?;
                    let State::Ready(reply, digest) = &row.state else {
                        return Err("PROVIDER_DELIVERY_UNKNOWN".into());
                    };
                    let index = r["index"]
                        .as_u64()
                        .and_then(|n| usize::try_from(n).ok())
                        .filter(|i| i.checked_mul(CHUNK).is_some_and(|n| n < reply.len().max(1)))
                        .ok_or("INVALID_PROVIDER_CHUNK")?;
                    let end = ((index + 1) * CHUNK).min(reply.len());
                    let result = json!({"request_id":id,"index":index,"size":reply.len(),"digest":digest,"data":STANDARD.encode(&reply[index*CHUNK..end])});
                    if end == reply.len() {
                        row.state = State::Consumed;
                    }
                    return Ok(result);
                }
                _ => return Err("UNKNOWN_PROVIDER_OPERATION".into()),
            }
        }
        let result = serde_json::from_slice::<Value>(&data)
            .map_err(|_| "PROVIDER_DELIVERY_UNKNOWN".to_owned())
            .and_then(|body| exchange(context, &body));
        let reply = match result {
            Ok(result) => json!({"result":result}),
            Err(e) => json!({"error":e}),
        };
        let mut reply = serde_json::to_vec(&reply).map_err(|_| "PROVIDER_DELIVERY_UNKNOWN")?;
        if reply.len() > MAX_RESPONSE {
            reply = serde_json::to_vec(&json!({"error":"PROVIDER_RESPONSE_TOO_LARGE"})).unwrap();
        }
        let digest = hash(&reply);
        let size = reply.len();
        let mut rows = self
            .rows
            .lock()
            .map_err(|_| "PROVIDER_BUFFER_UNAVAILABLE")?;
        // A TTL-expired generation cannot publish into a reused request ID.
        // At >120 seconds the delivery is unknown, never an invitation to resend.
        let row = rows
            .get_mut(id)
            .filter(|r| {
                r.context == context_hash
                    && r.deadline == generation
                    && matches!(r.state, State::InFlight)
            })
            .ok_or("PROVIDER_DELIVERY_UNKNOWN")?;
        row.state = State::Ready(reply, digest.clone());
        Ok(json!({"request_id":id,"accepted":true,"size":size,"digest":digest}))
    }
}
pub fn exchange(a: &Value) -> Result<Value> {
    let timeout = a["timeout"]
        .as_f64()
        .and_then(|n| Duration::try_from_secs_f64(n).ok())
        .filter(|n| !n.is_zero())
        .ok_or("TIMEOUT_INVALID")?;
    crate::node_provider::exchange(
        std::path::Path::new(
            a["endpoint"]
                .as_str()
                .ok_or("EXACT_DESKTOP_ENDPOINT_REQUIRED")?,
        ),
        std::path::Path::new(
            a["program"]
                .as_str()
                .ok_or("RUST_PROVIDER_BINARY_REQUIRED")?,
        ),
        &a["context"],
        &a["body"],
        timeout,
    )
}
