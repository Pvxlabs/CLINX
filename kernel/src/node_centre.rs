//! Authenticated reverse route. Principal is derived here, never from wire intent.
use crate::{
    node_scope::{self, PROTOCOL, Scope},
    node_tls::{self, Result},
};
use serde_json::{Value, json};
use std::path::Path;
pub fn handle(a: &Value, mut call: impl FnMut(&str, Value) -> Result<Value>) -> Result<Value> {
    let root = Path::new(a["state"].as_str().ok_or("IDENTITY_BOOTSTRAP_REQUIRED")?);
    let h = a["certificate_hex"].as_str().ok_or("INVALID_CERTIFICATE")?;
    let der = node_tls::decode_certificate(h)?;
    let peer = node_tls::authenticate(root, &der, None, true)?;
    let id = peer["node_id"].as_str().ok_or("IDENTITY_MISMATCH")?;
    let r = &a["request"];
    if r["node_id"] != id {
        return Err("NODE_IDENTITY_MISMATCH".into());
    }
    if r["protocol_version"] != PROTOCOL {
        return Err("NODE_VERSION_INCOMPATIBLE".into());
    }
    let op = r["operation"].as_str().ok_or("UNKNOWN_NODE_OPERATION")?;
    if !matches!(
        op,
        "node.register" | "node.heartbeat" | "observation.upload" | "authority.request"
    ) {
        return Err("UNKNOWN_NODE_OPERATION".into());
    }
    if !r["sharing_scope"].is_object() {
        return Err("SHARING_SCOPE_DENIED".into());
    }
    let local = Scope::parse(&r["sharing_scope"])?;
    let approvals = node_tls::read_state(root, "sharing.json")?.unwrap_or(json!({"scopes":{}}));
    if !approvals["scopes"].is_object() {
        return Err("INVALID_SHARING_STORE".into());
    }
    let mut effective =
        node_scope::intersect(Some(local), node_scope::optional(&approvals["scopes"][id])?)?;
    if effective.revoked || !(effective.read_sessions || effective.execute_tasks) {
        let record = call("registry.get", json!({"node_id":id}))?;
        if !record.is_null() {
            call(
                "registry.revoke",
                json!({"node_id":id,"user_scope":record["user_scope"]}),
            )?;
        }
        return Err("SHARING_SCOPE_DENIED".into());
    }
    if op == "authority.request" {
        let record = call("registry.get", json!({"node_id":id}))?;
        if record.is_null()
            || record["authorized"] != true
            || node_scope::stale(&record)
            || record["state"] == "OFFLINE"
            || record["public_key_fingerprint"] != peer["fingerprint"]
        {
            return Err("REGISTRATION_REQUIRED".into());
        }
        let registered = call(
            "registry.scope",
            json!({"node_id":id,"user_scope":record["user_scope"]}),
        )?;
        effective = node_scope::intersect(Some(effective), node_scope::optional(&registered)?)?;
        if !effective.allows("execution.authority") {
            return Err("SHARING_SCOPE_DENIED".into());
        }
        if a["authority"] != true {
            return Err("RUST_AUTHORITY_NOT_CONFIGURED".into());
        }
        let request_id = r["request_id"]
            .as_str()
            .filter(|s| !s.is_empty() && s.chars().count() <= 128)
            .ok_or("INVALID_REQUEST_ID")?;
        let envelope = json!({"scope":{"principal":{"node_id":id,"user_scope":effective.user_scope,"fingerprint":peer["fingerprint"]},"projects":effective.projects},"request_id":request_id,"operation":r["command"],"payload":r["payload"]});
        return Ok(match call("authority.node_request", envelope) {
            Ok(result) => json!({"request_id":request_id,"result":result}),
            Err(e) => json!({"request_id":request_id,"error_code":e}),
        });
    }
    if op == "observation.upload" {
        let record = call("registry.get", json!({"node_id":id}))?;
        if a["observations"] != true {
            return Err("OBSERVATION_NOT_CONFIGURED".into());
        }
        if record.is_null()
            || record["user_scope"] != effective.user_scope
            || record["authorized"] != true
        {
            return Err("REGISTRATION_REQUIRED".into());
        }
        if !effective.allows("observation.upload") {
            return Err("SHARING_SCOPE_DENIED".into());
        }
        return call(
            "observation.accept",
            json!({"node_id":id,"scope":effective.value(),"request":r}),
        );
    }
    let port = r["port"]
        .as_u64()
        .filter(|v| *v > 0 && *v <= 65535)
        .ok_or("INVALID_NODE_ENDPOINT")?;
    let host = a["address"][0].as_str().ok_or("INVALID_NODE_ENDPOINT")?;
    let endpoint = if host.contains(':') {
        format!("tls://[{host}]:{port}")
    } else {
        format!("tls://{host}:{port}")
    };
    let supplied = r.get("capabilities").cloned().unwrap_or(json!([]));
    let supplied = supplied
        .as_array()
        .filter(|l| l.iter().all(Value::is_string))
        .ok_or("INVALID_NODE_CAPABILITIES")?;
    let mut caps = std::collections::BTreeSet::new();
    for c in supplied {
        let c = c.as_str().unwrap();
        if effective.read_sessions
            && matches!(
                c,
                "session.read" | "session.status" | "execution.context" | "execution.status"
            )
            || effective.execute_tasks
                && matches!(
                    c,
                    "execution.adopt"
                        | "execution.prepare"
                        | "execution.start"
                        | "execution.status"
                        | "execution.context"
                        | "execution.cancel"
                        | "execution.reconcile"
                        | "execution.native_canonical_v1"
                )
        {
            caps.insert(c);
        }
    }
    let observed = node_tls::now();
    let stale = a["stale_after"].as_i64().ok_or("INVALID_STALE_AFTER")?;
    let record = json!({"node_id":id,"user_scope":effective.user_scope,"public_key_fingerprint":peer["fingerprint"],"display_name":peer["display_name"],"capabilities":caps,"endpoint":endpoint,"state":"ONLINE","last_seen":observed,"stale_after_seconds":stale});
    call(
        "registry.register",
        json!({"record":record,"scope":effective.value()}),
    )?;
    let identity =
        node_tls::read_state(root, "identity.json")?.ok_or("IDENTITY_BOOTSTRAP_REQUIRED")?;
    Ok(
        json!({"registered":true,"node_id":id,"centre_node_id":identity["node_id"],"scope":effective.value(),"heartbeat_seconds":(stale/3).max(1),"endpoint":endpoint,"observed_at":observed,"read_only":true}),
    )
}
