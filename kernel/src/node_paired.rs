//! Paired remote Provider route with fresh bilateral scope on every chunk.
use crate::{
    node_relay::{CHUNK, MAX_REQUEST, MAX_RESPONSE},
    node_scope::{self, PROTOCOL, Scope},
    node_tls::{self, Result},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::path::Path;
fn hash(b: &[u8]) -> String {
    format!("{:x}", Sha256::digest(b))
}
fn approval(root: &Path, id: &str) -> Result<Option<Scope>> {
    let v = node_tls::read_state(root, "sharing.json")?.unwrap_or(json!({"scopes":{}}));
    if !v["scopes"].is_object() {
        return Err("INVALID_SHARING_STORE".into());
    }
    node_scope::optional(&v["scopes"][id])
}
pub fn exchange(
    a: &Value,
    mut callback: impl FnMut(&str, Value) -> Result<Value>,
) -> Result<Value> {
    let root = Path::new(a["state"].as_str().ok_or("IDENTITY_BOOTSTRAP_REQUIRED")?);
    let id = a["node_id"].as_str().ok_or("PROVIDER_SCOPE_DENIED")?;
    let record = callback("registry.get", json!({"node_id":id}))?;
    let peer = node_tls::peer(root, id)?;
    let user = record["user_scope"].clone();
    let registered = if !record.is_null() {
        callback("registry.scope", json!({"node_id":id,"user_scope":user}))?
    } else {
        Value::Null
    };
    let scope = node_scope::intersect(node_scope::optional(&registered)?, approval(root, id)?)?;
    let mut context = a["context"].clone();
    if record.is_null()
        || record["authorized"] != true
        || node_scope::stale(&record)
        || record["state"] == "OFFLINE"
        || record["public_key_fingerprint"] != peer["fingerprint"]
        || !scope.allows("execution.authority")
        || context["host"] != id
        || !scope.project(&context["cwd"])
    {
        return Err("PROVIDER_SCOPE_DENIED".into());
    }
    let principal =
        json!({"node_id":id,"user_scope":scope.user_scope,"fingerprint":peer["fingerprint"]});
    if !context["node_principal"].is_null()
        && context["node_principal"] != json!({})
        && context["node_principal"] != principal
    {
        return Err("PROVIDER_PRINCIPAL_CONFLICT".into());
    }
    context["user_scope"] = json!(scope.user_scope);
    let wire = a["body_wire"]
        .as_str()
        .ok_or("INVALID_PROVIDER_REQUEST")?
        .as_bytes();
    let _: Value = serde_json::from_slice(wire).map_err(|_| "INVALID_PROVIDER_REQUEST")?;
    if wire.is_empty() || wire.len() > MAX_REQUEST {
        return Err("PROVIDER_REQUEST_TOO_LARGE".into());
    }
    let mut random = [0u8; 16];
    std::io::Read::read_exact(
        &mut std::fs::File::open("/dev/urandom").map_err(|_| "PROVIDER_BUFFER_UNAVAILABLE")?,
        &mut random,
    )
    .map_err(|_| "PROVIDER_BUFFER_UNAVAILABLE")?;
    let reference = random
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>();
    let mut call = |op: &str, fields: Value| -> Result<Value> {
        let current = callback("registry.scope", json!({"node_id":id,"user_scope":user}))?;
        if node_scope::intersect(node_scope::optional(&current)?, approval(root, id)?)? != scope {
            return Err("PROVIDER_SCOPE_CHANGED".into());
        }
        let mut request = json!({"operation":op,"protocol_version":PROTOCOL,"node_id":id,"context":context,"request_id":reference});
        for (k, v) in fields.as_object().ok_or("INVALID_PROVIDER_REQUEST")? {
            request[k] = v.clone();
        }
        // call reloads trust, pins the exact peer key and never retries a send.
        let result = node_tls::call(
            root,
            id,
            record["endpoint"].as_str().ok_or("INVALID_NODE_ENDPOINT")?,
            35.0,
            &request,
        )?;
        if let Some(code) = result["error_code"].as_str() {
            return Err(code.to_owned());
        }
        if result["request_id"] != reference {
            return Err("PROVIDER_RESPONSE_IDENTITY_CONFLICT".into());
        }
        Ok(result)
    };
    let count = wire.len().div_ceil(CHUNK);
    let digest = hash(wire);
    let mut reply = Value::Null;
    for (index, chunk) in wire.chunks(CHUNK).enumerate() {
        reply = call(
            "provider.upload",
            json!({"index":index,"count":count,"digest":digest,"data":STANDARD.encode(chunk)}),
        )?;
    }
    let size = reply["size"]
        .as_u64()
        .filter(|n| *n > 0 && *n <= MAX_RESPONSE as u64)
        .ok_or("PROVIDER_DELIVERY_UNKNOWN")? as usize;
    if reply["accepted"] != true {
        return Err("PROVIDER_DELIVERY_UNKNOWN".into());
    }
    let mut data = Vec::new();
    for index in 0..size.div_ceil(CHUNK) {
        let part = call("provider.read", json!({"index":index}))?;
        if part["index"] != index || part["size"] != size || part["digest"] != reply["digest"] {
            return Err("PROVIDER_RESPONSE_IDENTITY_CONFLICT".into());
        }
        let chunk = STANDARD
            .decode(part["data"].as_str().ok_or("INVALID_PROVIDER_CHUNK")?)
            .map_err(|_| "INVALID_PROVIDER_CHUNK")?;
        if chunk.len() > CHUNK {
            return Err("PROVIDER_RESPONSE_TOO_LARGE".into());
        }
        data.extend(chunk);
    }
    if data.len() != size || reply["digest"] != hash(&data) {
        return Err("PROVIDER_CHUNK_DIGEST_CONFLICT".into());
    }
    serde_json::from_slice(&data).map_err(|_| "PROVIDER_DELIVERY_UNKNOWN".into())
}
