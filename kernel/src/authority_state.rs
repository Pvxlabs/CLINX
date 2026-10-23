//! Production pre-send closure extension. Frozen V1 is delegated unchanged.
//!
//! This command is generated inside the business transaction, never a public
//! worker operation. It closes an allocation whose entire core history is the
//! three admission events; a persisted dispatch event makes closure impossible.
use crate::{ProtocolRequest, execution_core as core, runtime_lab};
use serde_json::{Value, json};

#[derive(Default)]
pub(crate) struct State {
    frozen: runtime_lab::State,
    closure: Option<Value>,
}
impl State {
    pub fn snapshot(&self) -> Value {
        let mut snapshot = self.frozen.snapshot();
        if let Some(p) = &self.closure {
            for r in snapshot["projection"].as_array_mut().unwrap() {
                r["lifecycle"] = match r["object"].as_str().unwrap() {
                    "Execution" => p["outcome"].clone(),
                    "Attempt" => json!("TERMINAL"),
                    "ResourceAllocation" => json!("RELEASED"),
                    _ => unreachable!(),
                };
                r["sequence"] = json!(r["sequence"].as_i64().unwrap() + 1);
                r["version"] = json!(r["version"].as_i64().unwrap() + 1);
            }
            snapshot["pre_send_closure"] = p.clone();
        }
        snapshot
    }
    pub fn apply(&mut self, req: &ProtocolRequest) -> Result<(Value, bool), String> {
        if self.closure.is_some() {
            return Err("PRE_SEND_ALREADY_CLOSED".into());
        }
        if req.operation != "finalize_before_send" {
            return self.frozen.apply(req);
        }
        let p = &req.payload;
        let s = self.frozen.snapshot();
        let valid = || -> Option<()> {
            if !core::keys(
                p,
                "contract execution_id attempt_id allocation_id outcome reason authority_id command_id cancel_requested",
            ) || p["contract"] != "CLINX_PRE_SEND_CLOSURE_V1"
                || p["command_id"] != req.request_id
                || !matches!(p["outcome"].as_str(), Some("FAILED" | "CANCELLED"))
                || (p["outcome"] == "CANCELLED" && p["cancel_requested"] != true)
                || !p["cancel_requested"].is_boolean()
                || s["event_count"] != 3
                || s["deliveries"] != json!({})
                || s["checkpoints"] != json!({})
            {
                return None;
            }
            for key in [
                "execution_id",
                "attempt_id",
                "allocation_id",
                "reason",
                "authority_id",
                "command_id",
            ] {
                if p[key]
                    .as_str()
                    .is_none_or(|v| v.is_empty() || v.len() > 4096)
                {
                    return None;
                }
            }
            let rows = s["projection"].as_array()?;
            if rows.len() != 3 {
                return None;
            }
            for (object, field, state) in [
                ("Execution", "execution_id", "ADMITTED"),
                ("Attempt", "attempt_id", "ASSIGNED"),
                ("ResourceAllocation", "allocation_id", "HELD"),
            ] {
                if !rows.iter().any(|r| {
                    r["object"] == object
                        && r["id"] == p[field]
                        && r["lifecycle"] == state
                        && r["version"] == 1
                        && r["sequence"] == 1
                }) {
                    return None;
                }
            }
            Some(())
        };
        if valid().is_none() {
            return Err("PRE_SEND_NOT_PROVEN".into());
        }
        self.closure = Some(p.clone());
        Ok((
            json!({"verdict":"APPLY", "contract":"CLINX_PRE_SEND_CLOSURE_V1", "snapshot":self.snapshot()}),
            true,
        ))
    }
}
