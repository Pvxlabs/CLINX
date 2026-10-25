//! Existing device identity/trust and one-shot pairing admission. No task
//! authority, new protocol, enrollment permission, or implicit PAKE fallback.
use crate::node_tls::{self, Result};
use base64::{Engine, engine::general_purpose::STANDARD};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs::File,
    io::{Read, Write},
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
const PROTOCOL: &str = "OPAQUE-3DH-RISTRETTO255-SHA512-ARGON2I-v1";
fn random<const N: usize>() -> Result<[u8; N]> {
    let mut v = [0; N];
    File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut v))
        .map_err(|_| "PAIRING_RANDOM_UNAVAILABLE")?;
    Ok(v)
}
fn nonce() -> Result<String> {
    Ok(random::<16>()?.iter().map(|b| format!("{b:02x}")).collect())
}
fn fingerprint(v: &Value) -> Result<String> {
    let key = STANDARD
        .decode(v.as_str().ok_or("INVALID_ENCODING")?)
        .map_err(|_| "INVALID_ENCODING")?;
    if key.len() != 32 {
        return Err("INVALID_PUBLIC_KEY".into());
    }
    Ok(format!("{:x}", Sha256::digest(key)))
}
fn public_record(v: &Value) -> Result<Value> {
    if !v.is_object()
        || [
            "node_id",
            "display_name",
            "public_key",
            "fingerprint",
            "protocol_version",
        ]
        .iter()
        .any(|k| v.get(k).is_none())
    {
        return Err("INVALID_IDENTITY".into());
    }
    node_tls::validate_identity(v)?;
    let fp = fingerprint(&v["public_key"])?;
    if v["fingerprint"] != fp {
        return Err("IDENTITY_OR_PROTOCOL_MISMATCH".into());
    }
    Ok(
        json!({"node_id":v["node_id"],"display_name":v["display_name"],"public_key":v["public_key"],"fingerprint":fp,"protocol_version":1}),
    )
}
fn certificate(v: &Value) -> Result<String> {
    node_tls::cert(v.as_str().ok_or("INVALID_CERTIFICATE")?)
        .and_then(|cert| node_tls::public(&cert))
        .map(|(key, _)| key)
        .map_err(|_| "INVALID_CERTIFICATE".into())
}
fn peers(v: &Value) -> Result<Value> {
    if v.is_null() {
        return Ok(json!({}));
    }
    let rows = v["peers"].as_object().ok_or("INVALID_PEER_STORE")?;
    for (id, peer) in rows {
        let public = public_record(peer)?;
        if public["node_id"] != *id
            || !matches!(peer["trust_state"].as_str(), Some("TRUSTED" | "REVOKED"))
        {
            return Err("INVALID_PEER_STORE".into());
        }
        if certificate(&peer["certificate"])? != public["public_key"] {
            return Err("PEER_KEY_MISMATCH".into());
        }
        if !matches!(
            peer["security_status"].as_str(),
            Some("DEV_ONLY" | "OPAQUE_V1")
        ) || (peer["security_status"] == "OPAQUE_V1" && peer["pairing_protocol"] != PROTOCOL)
        {
            return Err("INVALID_PEER_SECURITY_STATUS".into());
        }
    }
    Ok(json!(rows))
}
fn transition(op: &str, a: &Value, raw: &Value) -> Result<Value> {
    let mut all = peers(raw)?;
    let now = node_tls::now();
    let result = match op {
        "trust" => {
            let status = &a["security_status"];
            if status == "OPAQUE_V1"
                && (a["pairing_protocol"] != PROTOCOL || a["fresh_pairing"] != true)
            {
                return Err("FRESH_PRODUCTION_PAIRING_REQUIRED".into());
            }
            let public = public_record(&a["public"])?;
            if !matches!(status.as_str(), Some("DEV_ONLY" | "OPAQUE_V1")) {
                return Err("INVALID_PEER_SECURITY_STATUS".into());
            }
            if certificate(&a["certificate"])? != public["public_key"] {
                return Err("PEER_KEY_MISMATCH".into());
            }
            let id = public["node_id"].as_str().unwrap();
            let old = &all[id];
            if all
                .as_object()
                .unwrap()
                .iter()
                .any(|(key, peer)| key != id && peer["public_key"] == public["public_key"])
            {
                return Err("PUBLIC_KEY_ALREADY_BOUND".into());
            }
            if !old.is_null()
                && (old["public_key"] != public["public_key"] || old["trust_state"] == "REVOKED")
            {
                return Err("IDENTITY_MISMATCH".into());
            }
            if old["security_status"] == "OPAQUE_V1" && status == "DEV_ONLY" {
                return Err("TRUST_DOWNGRADE_REJECTED".into());
            }
            let mut peer = public.clone();
            let paired = if !old.is_null() && &old["security_status"] == status {
                old.get("paired_at").cloned().ok_or("INVALID_PEER_STORE")?
            } else {
                json!(now)
            };
            peer.as_object_mut().unwrap().extend(json!({"certificate":a["certificate"],"friendly_name":public["display_name"],"trust_state":"TRUSTED","security_status":status,"pairing_protocol":a["pairing_protocol"],"paired_at":paired,"updated_at":now,"last_authenticated_at":old["last_authenticated_at"]}).as_object().unwrap().clone());
            all[id] = peer;
            Value::Null
        }
        "authenticate" => {
            let id = a["node_id"].as_str().ok_or("UNTRUSTED_DEVICE")?;
            let peer = &mut all[id];
            if peer.is_null() || peer["trust_state"] != "TRUSTED" {
                return Err("UNTRUSTED_DEVICE".into());
            }
            if peer["public_key"] != a["public_key"] {
                return Err("IDENTITY_MISMATCH".into());
            }
            if peer["security_status"] == "DEV_ONLY" && a["allow_dev"] != true {
                return Err("SECURITY_BLOCKER_DEV_ONLY".into());
            }
            peer["last_authenticated_at"] = json!(now);
            peer["updated_at"] = json!(now);
            peer.clone()
        }
        "revoke" => {
            let id = a["node_id"].as_str().ok_or("UNKNOWN_DEVICE")?;
            if all.get(id).is_none() {
                return Err("UNKNOWN_DEVICE".into());
            }
            all[id]["trust_state"] = json!("REVOKED");
            all[id]["updated_at"] = json!(now);
            Value::Null
        }
        _ => return Err("DISCOVERY_OPERATION_UNKNOWN".into()),
    };
    Ok(json!({"state":{"schema_version":1,"peers":all},"value":result}))
}
fn write(root: &Path, value: &Value) -> Result<()> {
    let mut tmp = tempfile::NamedTempFile::new_in(root).map_err(|_| "STATE_WRITE_UNAVAILABLE")?;
    tmp.write_all(&serde_json::to_vec(value).map_err(|_| "INVALID_STATE")?)
        .and_then(|_| tmp.as_file().sync_all())
        .map_err(|_| "STATE_WRITE_UNAVAILABLE")?;
    tmp.persist(root.join("peers.json"))
        .map_err(|_| "STATE_WRITE_UNAVAILABLE")?;
    File::open(root)
        .and_then(|f| f.sync_all())
        .map_err(|_| "STATE_WRITE_UNAVAILABLE".into())
}
#[derive(Default)]
struct Window {
    code: Option<String>,
    id: Option<String>,
    expires: f64,
    next_attempt: f64,
    next_open: f64,
    busy: Option<String>,
    failures: u64,
}
impl Window {
    fn clear(&mut self) {
        self.code = None;
        self.id = None;
    }
    fn active(&mut self, now: f64) -> Result<()> {
        if now >= self.expires {
            self.clear();
        }
        if self.code.is_none() {
            return Err("PAIRING_CLOSED".into());
        }
        Ok(())
    }
    fn snapshot(&self) -> Value {
        json!({"code":self.code,"id":self.id,"failures":self.failures})
    }
}
pub struct Runtime {
    next: AtomicU64,
    windows: Mutex<BTreeMap<u64, Arc<Mutex<Window>>>>,
    start: Instant,
}
impl Default for Runtime {
    fn default() -> Self {
        Self {
            next: AtomicU64::new(0),
            windows: Mutex::default(),
            start: Instant::now(),
        }
    }
}
impl Runtime {
    pub fn invoke<F: FnMut(&str, Value) -> Result<Value>>(
        &self,
        operation: &str,
        a: &Value,
        mut callback: F,
    ) -> Result<Value> {
        match operation {
            "discovery.public" => return public_record(&a["value"]),
            "discovery.name" => {
                node_tls::validate_identity(
                    &json!({"node_id":"fixture","display_name":a["value"],"protocol_version":1}),
                )?;
                return Ok(a["value"].clone());
            }
            "discovery.node" => {
                node_tls::validate_identity(
                    &json!({"node_id":a["value"],"display_name":"fixture","protocol_version":1}),
                )?;
                return Ok(a["value"].clone());
            }
            "discovery.fingerprint" => return fingerprint(&a["value"]).map(|v| json!(v)),
            "discovery.certificate" => return certificate(&a["value"]).map(|v| json!(v)),
            "discovery.peers.validate" => return peers(&a["value"]),
            "discovery.peers.transition" => {
                return transition(
                    a["operation"]
                        .as_str()
                        .ok_or("DISCOVERY_OPERATION_UNKNOWN")?,
                    &a["arguments"],
                    &a["state"],
                );
            }
            op if op.starts_with("discovery.peers.") => {
                let root = Path::new(a["root"].as_str().ok_or("UNSAFE_STATE_DIRECTORY")?);
                node_tls::check(root, true)?;
                let op = op.strip_prefix("discovery.peers.").unwrap();
                // Atomic replacement gives a complete read snapshot. A read-only
                // observer must never create store.lock or take a writer lock.
                if op == "all" {
                    return peers(
                        &node_tls::read_unlocked(root, "peers.json")?.unwrap_or(Value::Null),
                    );
                }
                let _guard = node_tls::lock(root)?;
                let raw = node_tls::read_unlocked(root, "peers.json")?.unwrap_or(Value::Null);
                let value = transition(op, a, &raw)?;
                write(root, &value["state"])?;
                return Ok(value["value"].clone());
            }
            "discovery.window.create" => {
                let id = self.next.fetch_add(1, Ordering::Relaxed) + 1;
                self.windows
                    .lock()
                    .map_err(|_| "PAIRING_UNAVAILABLE")?
                    .insert(id, Arc::new(Mutex::new(Window::default())));
                return Ok(json!(id));
            }
            "discovery.window.delete" => {
                self.windows
                    .lock()
                    .map_err(|_| "PAIRING_UNAVAILABLE")?
                    .remove(&a["window"].as_u64().ok_or("PAIRING_CLOSED")?);
                return Ok(Value::Null);
            }
            _ => (),
        }
        let window = self
            .windows
            .lock()
            .map_err(|_| "PAIRING_UNAVAILABLE")?
            .get(&a["window"].as_u64().ok_or("PAIRING_CLOSED")?)
            .cloned()
            .ok_or("PAIRING_CLOSED")?;
        let mut state = window.lock().map_err(|_| "PAIRING_UNAVAILABLE")?;
        let now = if a["now"].is_null() {
            self.start.elapsed().as_secs_f64()
        } else {
            a["now"]
                .as_f64()
                .filter(|n| n.is_finite())
                .ok_or("PAIRING_CLOCK_INVALID")?
        };
        match operation {
            "discovery.window.close" => {
                state.clear();
                Ok(Value::Null)
            }
            "discovery.window.expire" => {
                if state.id.as_deref() == a["id"].as_str() {
                    state.clear();
                }
                Ok(Value::Null)
            }
            "discovery.window.snapshot" => {
                if now >= state.expires {
                    state.clear();
                }
                Ok(state.snapshot())
            }
            "discovery.window.active" => {
                state.active(now)?;
                Ok(Value::Null)
            }
            "discovery.window.open" => {
                if now < state.next_open || state.busy.is_some() {
                    return Err("PAIRING_RATE_LIMITED".into());
                }
                let lifetime = a["lifetime"]
                    .as_f64()
                    .filter(|n| n.is_finite() && *n >= 0.0)
                    .ok_or("PAIRING_LIFETIME_INVALID")?;
                let duration = Duration::try_from_secs_f64(lifetime)
                    .map_err(|_| "PAIRING_LIFETIME_INVALID")?;
                let n = loop {
                    let n = u16::from_be_bytes(random::<2>()?);
                    if n < 60000 {
                        break n % 10000;
                    }
                };
                let id = nonce()?;
                state.code = Some(format!("{n:04}"));
                state.id = Some(id.clone());
                state.expires = now + lifetime;
                state.next_open = state.expires;
                state.next_attempt = now;
                state.failures = 0;
                let weak = Arc::downgrade(&window);
                std::thread::spawn(move || {
                    std::thread::sleep(duration);
                    if let Some(window) = weak.upgrade()
                        && let Ok(mut state) = window.lock()
                        && state.id.as_deref() == Some(&id)
                    {
                        state.clear();
                    }
                });
                Ok(json!(state.code))
            }
            "discovery.window.reserve" => {
                state.active(now)?;
                if state.busy.is_some() {
                    return Err("PAIRING_BUSY".into());
                }
                if now < state.next_attempt {
                    return Err("PAIRING_RATE_LIMITED".into());
                }
                state.busy = Some(nonce()?);
                state.next_attempt = now + 1.0;
                Ok(json!([state.busy, state.id, state.code]))
            }
            "discovery.window.complete" => {
                if state.busy.is_none() || state.busy.as_deref() != a["reservation"].as_str() {
                    return Err("PAIRING_REPLAY".into());
                }
                state.busy = None;
                state.active(now)?;
                if a["success"] == true {
                    state.clear();
                    if a["commit"] == true {
                        callback("discovery.commit", json!({"token":a["callback_token"]}))?;
                    }
                } else {
                    state.failures += 1;
                    if state.failures
                        >= a["max_failures"].as_u64().ok_or("PAIRING_POLICY_INVALID")?
                    {
                        state.clear();
                    }
                }
                Ok(Value::Null)
            }
            _ => Err("DISCOVERY_OPERATION_UNKNOWN".into()),
        }
    }
}
