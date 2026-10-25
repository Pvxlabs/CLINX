//! Node-side authenticated owner/Centre intersection before any dispatch.
use crate::{
    node_scope::{self, PROTOCOL},
    node_tls::{self, Result},
};
use serde_json::{Value, json};
use std::path::Path;
pub fn handle(a: &Value, mut call: impl FnMut(&str, Value) -> Result<Value>) -> Result<Value> {
    let root = Path::new(a["state"].as_str().ok_or("IDENTITY_BOOTSTRAP_REQUIRED")?);
    let der =
        node_tls::decode_certificate(a["certificate_hex"].as_str().ok_or("INVALID_CERTIFICATE")?)?;
    let peer = node_tls::authenticate(root, &der, None, true)?;
    if peer["node_id"] != a["centre_id"] {
        return Err("CENTRE_IDENTITY_MISMATCH".into());
    }
    let approvals = node_tls::read_state(root, "sharing.json")?.unwrap_or(json!({"scopes":{}}));
    if !approvals["scopes"].is_object() {
        return Err("INVALID_SHARING_STORE".into());
    }
    let id = a["centre_id"].as_str().ok_or("CENTRE_IDENTITY_MISMATCH")?;
    let local = node_scope::optional(&approvals["scopes"][id])?;
    let centre = node_scope::optional(&a["active_scope"])?;
    if local.as_ref().is_none_or(|s| s.revoked) || centre.as_ref().is_none_or(|s| s.revoked) {
        return Err("SHARING_SCOPE_DENIED".into());
    }
    let effective = node_scope::intersect(local, centre)?;
    call("scope.apply", effective.value())?;
    let r = &a["request"];
    if matches!(
        r["operation"].as_str(),
        Some("provider.upload" | "provider.read")
    ) {
        if a["provider_relay"] != true
            || r["protocol_version"] != PROTOCOL
            || r["node_id"] != a["node_id"]
        {
            return Err("PROVIDER_RELAY_UNAVAILABLE".into());
        }
        call(
            "provider.handle",
            json!({"request":r,"scope":effective.value()}),
        )
    } else {
        call("node.handle", r.clone())
    }
}
