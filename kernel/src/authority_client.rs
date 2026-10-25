//! Python compatibility request validation; reuses the existing Rust Unix transport.
use serde_json::Value;
use std::{path::PathBuf, time::Duration};
pub fn request(a: &Value) -> Result<Value, String> {
    let operation = a["wire_operation"]
        .as_str()
        .ok_or("UNSUPPORTED_OPERATION")?;
    let allowed = matches!(
        operation,
        "claim" | "commit" | "renew" | "read_commit" | "health"
    ) || operation.strip_prefix("business.").is_some_and(|op| {
        matches!(
            op,
            "node_request"
                | "prepare"
                | "start"
                | "cancel"
                | "get_status"
                | "get_prepared"
                | "list_tasks"
                | "list_projects"
                | "read_receipt"
                | "work"
                | "take_dispatch"
                | "resume_dispatch"
                | "successor_begin"
                | "successor_take"
                | "successor_commit"
                | "successor_recover_empty"
                | "effective_policy"
                | "register_git_target"
                | "revoke_git_target"
                | "resolve_git_target"
                | "adoption_endpoints"
                | "bind_thread"
                | "authorize_send"
                | "fail_before_send"
                | "observation_inventory"
                | "observation_turns"
                | "adopt"
                | "adoption_begin"
                | "adoption_commit"
                | "prepare_policy"
                | "apply_policy"
                | "host_receive"
                | "host_take"
                | "host_begin"
                | "host_complete"
                | "host_sent"
                | "host_failed"
                | "host_ack"
                | "host_rows"
                | "host_reconcile_rejected"
                | "native_create_take"
                | "metadata_begin"
                | "metadata_commit"
                | "input_begin"
                | "input_commit"
                | "observe_begin"
                | "observe_commit"
                | "take_cancel"
                | "outbox"
                | "ack_outbox"
        )
    });
    if !allowed {
        return Err("UNSUPPORTED_OPERATION".into());
    }
    let id = a["request_id"]
        .as_str()
        .filter(|s| !s.is_empty() && s.chars().count() <= 128)
        .ok_or("INVALID_REQUEST_ID")?;
    let endpoint = PathBuf::from(
        a["endpoint"]
            .as_str()
            .ok_or("AUTHORITY_ENDPOINT_UNTRUSTED")?,
    );
    let uid = u32::try_from(
        a["authority_uid"]
            .as_u64()
            .ok_or("AUTHORITY_PEER_UNTRUSTED")?,
    )
    .map_err(|_| "AUTHORITY_PEER_UNTRUSTED")?;
    let timeout = a["timeout"]
        .as_f64()
        .filter(|v| v.is_finite() && *v > 0.)
        .ok_or("AUTHORITY_UNAVAILABLE")?;
    let timeout = Duration::try_from_secs_f64(timeout).map_err(|_| "AUTHORITY_UNAVAILABLE")?;
    // Same validation order as the compatibility client: bound the frame before IO.
    let wire=serde_json::to_vec(&serde_json::json!({"protocol_version":crate::authority_store::PROTOCOL,"operation":operation,"request_id":id,"payload":a["payload"]})).map_err(|_|"REQUEST_TOO_LARGE")?;
    if wire.len() + 1 > crate::MAX_FRAME_BYTES {
        return Err("REQUEST_TOO_LARGE".into());
    }
    crate::native_transport::UnixAuthority { endpoint, uid }
        .request(operation, a["payload"].clone(), id, timeout)
        .map_err(|e| {
            if e == "AUTHORITY_DELIVERY_UNKNOWN" {
                "DELIVERY_UNKNOWN".into()
            } else {
                e
            }
        })
}
