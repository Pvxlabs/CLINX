//! Exact predecessor checkpoints and once-only successor creation in the same
//! authority transaction as task/execution binding. No latest-turn selection.
use super::*;

/// Canonical identity is a routing assertion, never Provider outcome evidence.
/// Node RPC verifies its paired owner; the authority still requires a fresh
/// Transport observation before creating any task or predecessor checkpoint.
pub(super) fn adoption_identity(p: &Value) -> Result<Option<Value>> {
    let Some(identity) = p.get("canonical_identity") else {
        return Ok(None);
    };
    if !core::keys(
        identity,
        "node_id provider thread_id user_scope task_id host origin first_provenance_json title cwd",
    ) {
        return Err("CANONICAL_HANDOFF_IDENTITY_CONFLICT".into());
    }
    let thread = string(identity, "thread_id")?;
    let valid_thread = thread.len() == 36
        && thread.bytes().enumerate().all(|(i, b)| {
            if [8, 13, 18, 23].contains(&i) {
                b == b'-'
            } else {
                b.is_ascii_digit() || (b'a'..=b'f').contains(&b)
            }
        });
    let node = string(identity, "node_id")?;
    let proof: Value = serde_json::from_str(string(identity, "first_provenance_json")?)
        .map_err(|_| "CANONICAL_HANDOFF_IDENTITY_CONFLICT")?;
    string(identity, "user_scope")?;
    string(&proof, "node_fingerprint")?;
    string(&proof, "observation_id")?;
    string(&proof, "digest")?;
    if !valid_thread
        || identity["host"] != node
        || p["host"] != node
        || identity["provider"] != "codex_app_server"
        || identity["origin"] != "CODEX_NATIVE"
        || p["thread_id"] != thread
        || proof["node"] != node
        || identity["cwd"] != p["workspace_contract"]["cwd"]
        || identity["task_id"] != canonical_task_id(node, thread)
    {
        return Err("CANONICAL_HANDOFF_IDENTITY_CONFLICT".into());
    }
    string(p, "exact_turn_id").map_err(|_| "EXACT_PREDECESSOR_TURN_REQUIRED")?;
    Ok(Some(
        json!({"identity":identity,"predecessor_turn_id":p["exact_turn_id"]}),
    ))
}

fn canonical_task_id(node: &str, thread: &str) -> String {
    use sha1::{Digest, Sha1};
    // UUIDv5 URL namespace, identical to the deployed Python identity contract.
    let mut hash = Sha1::new();
    hash.update([
        0x6b, 0xa7, 0xb8, 0x11, 0x9d, 0xad, 0x11, 0xd1, 0x80, 0xb4, 0x00, 0xc0, 0x4f, 0xd4, 0x30,
        0xc8,
    ]);
    hash.update(core::canonical(&json!([node, "codex_app_server", thread])));
    let mut bytes = hash.finalize();
    bytes[6] = (bytes[6] & 0x0f) | 0x50;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    format!(
        "task_{}",
        bytes[..16]
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>()
    )
}

pub(super) fn needed(task: &Value, prepared: &Value) -> bool {
    task["thread_id"].is_string()
        && prepared["execution_policy"]["execution_surface"] == "HOST_EXECUTOR"
        && task["provider_policy_hash"] != prepared["policy_hash"]
}

pub(super) fn command(
    c: &Connection,
    policy: &Policy,
    boot: &str,
    op: &str,
    p: &Value,
    changes: &mut Vec<Value>,
) -> Result<Value> {
    let eid = string(p, "execution_ref")?;
    let mut e = row(c, "execution", eid)?;
    worker_fence(c, &e, p, boot)?;
    if e["dispatch_state"] != "TAKEN" || e["cancel_requested"] == true {
        return Err("HANDOFF_EXECUTION_FENCED".into());
    }
    let task_id = string(&e, "task_ref")?.to_owned();
    let mut task = row(c, "task", &task_id)?;
    let prepared = row(c, "prepared", string(&e, "prepared_execution_ref")?)?;
    targets::validate(policy, Some(&task), &prepared["execution_policy"])?;
    if task["active_execution"] != eid || task["policy_hash"] != prepared["policy_hash"] {
        return Err("HANDOFF_TASK_CHANGED".into());
    }
    if op == "successor_recover_empty" {
        if !core::keys(
            p,
            "execution_ref epoch ticket thread_id endpoint read_error turns_error",
        ) || e["handoff"]["state"] != "COMMITTED"
            || p["thread_id"] != e["thread_id"]
            || p["endpoint"] != e["endpoint"]
            || e["turn_id"].is_string()
            || p["read_error"]["code"] != -32600
            || p["turns_error"]["code"] != -32600
            || p["read_error"]["message"]
                != format!("thread not loaded: {}", string(&e, "thread_id")?)
            || p["turns_error"]["message"]
                != format!(
                    "thread {} is not materialized yet; thread/turns/list is unavailable before first user message",
                    string(&e, "thread_id")?
                )
        {
            return Err("EMPTY_SUCCESSOR_NOT_PROVEN".into());
        }
        let mut abandoned = e["handoff"].clone();
        abandoned["state"] = json!("ABANDONED_BEFORE_INPUT");
        abandoned["provider_evidence"] = p.clone();
        if e["abandoned_successors"].is_null() {
            e["abandoned_successors"] = json!([]);
        }
        let prior = e["abandoned_successors"].as_array_mut().unwrap();
        if prior.len() >= 3 {
            return Err("SUCCESSOR_RECOVERY_LIMIT".into());
        }
        prior.push(abandoned.clone());
        task["lineage"]
            .as_array_mut()
            .ok_or("HANDOFF_LINEAGE_MISSING")?
            .push(abandoned);
        task["provider_policy_hash"] = Value::Null;
        e["thread_id"] = Value::Null;
        e["endpoint"] = Value::Null;
        e["handoff"]["state"] = json!("PREPARED");
        e["handoff"]["nonce"] = json!(random_id()?);
        e["handoff"]["successor_thread_id"] = Value::Null;
        e["handoff"]["replaces_empty_thread_id"] = p["thread_id"].clone();
        put(c, "task", &task_id, &task, changes)?;
        put(c, "execution", eid, &e, changes)?;
        return Ok(e["handoff"].clone());
    }
    if op == "successor_begin" {
        if !core::keys(p, "execution_ref epoch ticket") || !needed(&task, &prepared) {
            return Err("SUCCESSOR_NOT_REQUIRED".into());
        }
        if !e["handoff"].is_null() {
            return Ok(e["handoff"].clone());
        }
        let checkpoint = &task["native_checkpoint"];
        if checkpoint["thread_id"] != e["predecessor_thread_id"]
            || !checkpoint["turn"]["id"].is_string()
            || !policy
                .endpoints
                .iter()
                .any(|s| checkpoint["endpoint"] == *s)
        {
            return Err("EXACT_PREDECESSOR_TURN_REQUIRED".into());
        }
        e["handoff"] = json!({"state":"PREPARED","nonce":random_id()?,"source":checkpoint,
            "policy_hash":prepared["policy_hash"],"workspace_contract":prepared["workspace_contract"],
            "successor_thread_id":null});
    } else if op == "successor_take" {
        if !core::keys(p, "execution_ref epoch ticket nonce observations")
            || e["handoff"]["state"] != "PREPARED"
            || p["nonce"] != e["handoff"]["nonce"]
        {
            return Err("HANDOFF_PERMIT_UNAVAILABLE".into());
        }
        let observations = p["observations"]
            .as_array()
            .ok_or("INVALID_HANDOFF_OBSERVATION")?;
        if observations.len() != policy.endpoints.len() {
            return Err("OBSERVATION_ENDPOINT_SET_MISMATCH".into());
        }
        let mut seen = std::collections::BTreeSet::new();
        for observation in observations {
            if !core::keys(observation, "endpoint thread turn")
                || !policy
                    .endpoints
                    .iter()
                    .any(|s| observation["endpoint"] == *s)
                || !seen.insert(string(observation, "endpoint")?)
                || observation["thread"]["id"] != e["handoff"]["source"]["thread_id"]
                || observation["thread"]["cwd"] != prepared["workspace_contract"]["cwd"]
                || observation["thread"]["status"]["type"] != "idle"
                || observation["turn"] != e["handoff"]["source"]["turn"]
            {
                return Err("EXACT_HANDOFF_OBSERVATION_CONFLICT".into());
            }
        }
        e["handoff"]["state"] = json!("CREATING");
        e["handoff"]["observations"] = p["observations"].clone();
        put(c, "execution", eid, &e, changes)?;
        return Ok(json!({"send_permitted":true,"handoff":e["handoff"]}));
    } else if op == "successor_commit" {
        if !core::keys(p, "execution_ref epoch ticket nonce endpoint thread")
            || p["nonce"] != e["handoff"]["nonce"]
            || e["handoff"]["state"] != "CREATING"
            || !policy.endpoints.iter().any(|s| p["endpoint"] == *s)
            || p["thread"]["cwd"] != prepared["workspace_contract"]["cwd"]
            || p["thread"]["status"]["type"] != "idle"
            || p["thread"]["id"] == e["handoff"]["source"]["thread_id"]
        {
            return Err("SUCCESSOR_IDENTITY_CONFLICT".into());
        }
        let successor = string(&p["thread"], "id")?;
        if rows(c, "task")?.iter().any(|t| {
            t["thread_id"] == successor
                || t["lineage"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .any(|h| h["source"]["thread_id"] == successor)
        }) {
            return Err("SUCCESSOR_ALREADY_BOUND".into());
        }
        e["handoff"]["state"] = json!("COMMITTED");
        e["handoff"]["successor_thread_id"] = json!(successor);
        e["handoff"]["endpoint"] = p["endpoint"].clone();
        e["thread_id"] = json!(successor);
        e["endpoint"] = p["endpoint"].clone();
        task["thread_id"] = json!(successor);
        task["provider_policy_hash"] = prepared["policy_hash"].clone();
        if task["lineage"].is_null() {
            task["lineage"] = json!([]);
        }
        task["lineage"]
            .as_array_mut()
            .unwrap()
            .push(e["handoff"].clone());
        put(c, "task", &task_id, &task, changes)?;
    } else {
        return Err("INVALID_HANDOFF_OPERATION".into());
    }
    put(c, "execution", eid, &e, changes)?;
    Ok(e["handoff"].clone())
}
