//! Observation source merge and historical read compatibility. No task writes.
use crate::{native_history, node_scope::Scope, node_tls::Result, read_projection::*};
use serde_json::{Value, json};
use std::{os::unix::fs::MetadataExt, path::Path};
fn legacy(a: &Value) -> Result<Option<rusqlite::Connection>> {
    let p = text(&a["canonical_path"]);
    if p.is_empty() || !Path::new(p).exists() {
        return Ok(None);
    }
    readonly(Path::new(p), "OBSERVATION_SOURCE_UNAVAILABLE", true).map(Some)
}
fn inventory(a: &Value) -> Result<Value> {
    let Some(c) = legacy(a)? else {
        return Ok(json!([]));
    };
    let scope = Scope::parse(&a["scope"])?;
    let rows = rows(
        &c,
        "SELECT b.thread_id,t.cwd,t.title,t.updated_at FROM conversation_bindings b JOIN tasks t ON t.task_id=b.task_id WHERE t.host=? AND b.thread_id>? ORDER BY b.thread_id LIMIT 16",
        &[sql(&a["owner_host"]), sql(&a["after"])],
        "OBSERVATION_SOURCE_UNAVAILABLE",
    )?;
    Ok(json!(rows.into_iter().filter(|r|scope.project(&r["cwd"])).map(|r|json!({"id":r["thread_id"],"cwd":r["cwd"],"title":r["title"],"updated_at":r["updated_at"],"history_mode":"canonical","rollout_path":""})).collect::<Vec<_>>()))
}
fn canonical(a: &Value) -> Result<Value> {
    let mut state = a["state"].clone();
    let Some(c) = legacy(a)? else {
        return Ok(json!([[], state]));
    };
    let mut turns = Vec::new();
    let mut foreign = false;
    for table in ["executions", "execution_history"] {
        let q = format!(
            "SELECT e.*,t.cwd,t.host FROM {table} e JOIN tasks t ON t.task_id=e.task_id WHERE json_extract(e.routing_identity_json,'$.conversation.binding')=?"
        );
        let recent = rows(
            &c,
            &(q.clone() + " ORDER BY e.acquired_at DESC,e.execution_ref DESC LIMIT 4"),
            &[sql(&a["tid"])],
            "OBSERVATION_SOURCE_UNAVAILABLE",
        )?;
        let key = format!("canonical_before_{table}");
        let before = state.get(&key).cloned().unwrap_or(json!(["9999", "~"]));
        let older = rows(
            &c,
            &(q + " AND (e.acquired_at,e.execution_ref)<(?,?) ORDER BY e.acquired_at DESC,e.execution_ref DESC LIMIT 4"),
            &[sql(&a["tid"]), sql(&before[0]), sql(&before[1])],
            "OBSERVATION_SOURCE_UNAVAILABLE",
        )?;
        if let Some(last) = older.last() {
            state[&key] = json!([last["acquired_at"], last["execution_ref"]]);
        }
        state[format!("canonical_complete_{table}")] = json!(older.len() < 4);
        let mut unique: Vec<Value> = Vec::new();
        for row in recent.into_iter().chain(older) {
            if let Some(i) = unique
                .iter()
                .position(|r| r["execution_ref"] == row["execution_ref"])
            {
                unique[i] = row;
            } else {
                unique.push(row);
            }
        }
        for row in unique {
            let route: Value = serde_json::from_str(text(&row["routing_identity_json"]))
                .map_err(|_| "OBSERVATION_SOURCE_UNAVAILABLE")?;
            if !route["host"]["stable_identifier"].is_null()
                && route["host"]["stable_identifier"] != a["owner_host"]
            {
                foreign = true;
                continue;
            }
            if row["cwd"] != a["project"] {
                continue;
            }
            let result=rows(&c,"SELECT status,summary,changed_files FROM execution_results WHERE execution_ref=? AND task_id=? AND turn_id=?",&[sql(&row["execution_ref"]),sql(&row["task_id"]),sql(&row["turn_id"])],"OBSERVATION_SOURCE_UNAVAILABLE")?.into_iter().next().unwrap_or(Value::Null);
            let artifacts = text(&result["changed_files"])
                .split(',')
                .map(str::trim)
                .filter(|s| !s.is_empty() && *s != "NONE")
                .take(16)
                .collect::<Vec<_>>();
            turns.push(json!({"task_ref":row["task_id"],"turn_id":row["turn_id"],"execution_ref":row["execution_ref"],"ordinal":native_history::order(&row["acquired_at"],0),"execution_state":row["execution_state"],"native_state":"UNKNOWN","result_processing":if truth(&row["failure_code"]){row["failure_code"].clone()}else{json!(if result.is_null(){"PENDING"}else{"RECEIVED"})},"business_result":if matches!(text(&result["status"]),"PASS"|"BLOCKED"){result["status"].clone()}else{Value::Null},"summary":result["summary"],"artifacts":artifacts,"started_at":row["acquired_at"],"completed_at":row["released_at"]}));
        }
    }
    if foreign {
        return Err("OBSERVATION_FOREIGN_EXECUTION_OWNER".into());
    }
    Ok(json!([turns, state]))
}
fn sample(a: &Value, mut call: impl FnMut(&str, Value) -> Result<Value>) -> Result<Value> {
    let m = &a["metadata"];
    let tid = &m["id"];
    let native = call(
        "source.native_turns",
        json!({"metadata":m,"state":a["state"]}),
    )?;
    let canonical = call(
        "source.canonical_turns",
        json!({"tid":tid,"project":m["cwd"],"state":native[2]}),
    )?;
    let mut state = canonical[1].clone();
    let mut coverage = native[1].clone();
    let mut turns = native[0].as_array().ok_or("INVALID_OBSERVATION")?.clone();
    let mut managed = canonical[0]
        .as_array()
        .ok_or("INVALID_OBSERVATION")?
        .clone();
    let mut identities = std::collections::BTreeSet::new();
    for turn in &managed {
        if truth(&turn["turn_id"]) && !identities.insert(compact(&turn["turn_id"])) {
            return Err("OBSERVATION_IDENTITY_CONFLICT".into());
        }
    }
    for turn in &mut managed {
        if let Some(i) = turns
            .iter()
            .rposition(|t| truth(&t["turn_id"]) && t["turn_id"] == turn["turn_id"])
        {
            let original = turns.remove(i);
            turn["native_state"] = original["native_state"].clone();
            turn["ordinal"] = original["ordinal"].clone();
            for k in ["live_observation", "model"] {
                if truth(&original[k]) {
                    turn[k] = original[k].clone();
                }
            }
            if !truth(&turn["summary"]) {
                turn["summary"] = original["summary"].clone();
            }
        }
    }
    let mut tasks = Vec::new();
    for t in &managed {
        if !tasks.contains(&t["task_ref"]) {
            tasks.push(t["task_ref"].clone());
        }
    }
    turns.extend(managed);
    if a["canonical_db"] == true
        && ["executions", "execution_history"].iter().any(|t| {
            state
                .get(format!("canonical_complete_{t}"))
                .is_some_and(|v| !truth(v))
        })
    {
        coverage = json!("HISTORY_BACKFILL");
    }
    if turns.is_empty() {
        turns.push(json!({"turn_id":null,"ordinal":0,"native_state":"UNKNOWN"}));
    }
    let latest = turns.iter().enumerate().fold(0, |best, (i, t)| {
        if t["ordinal"].as_i64().unwrap_or(0) > turns[best]["ordinal"].as_i64().unwrap_or(0) {
            i
        } else {
            best
        }
    });
    if a["live_enabled"] == true
        && truth(&turns[latest]["turn_id"])
        && turns[latest]["native_state"] == "RUNNING"
        && turns[latest].get("live_observation").is_none()
    {
        turns[latest]["live_observation"] = call(
            "source.observe_live",
            json!({"tid":tid,"project":m["cwd"],"turn_id":turns[latest]["turn_id"]}),
        )?;
    }
    state["active"] = json!(turns.iter().any(|t| t["native_state"] == "RUNNING"
        || matches!(
            text(&t["execution_state"]),
            "CODEX_RUNNING" | "FINALIZING" | "TURN_STARTED"
        )));
    if tasks.len() > 1 {
        return Err("OBSERVATION_IDENTITY_CONFLICT".into());
    }
    let task = tasks.first().cloned().unwrap_or(Value::Null);
    let generation = call("source.generation", json!({}))?;
    let events=turns.into_iter().map(|mut turn|{if let Some(o)=turn.as_object_mut(){o.remove("task_ref");}turn["source"]=json!(if truth(&turn["execution_ref"]){"CLINX_MANAGED"}else{"CODEX_NATIVE"});json!({"native_thread_id":tid,"provider":crate::node_scope::PROVIDER,"project":m["cwd"],"title":([&m["name"],&m["title"],tid].into_iter().find(|v|truth(v)).cloned().unwrap_or(Value::Null)),"source_client":m.get("source").cloned().unwrap_or(json!("UNKNOWN")),"source_generation":generation,"source_updated_at":m["updated_at"],"native_admission":{"has_user_event":truth(&m["has_user_event"]),"user_message_observed":truth(&state["user_message_observed"]),"created_at":m["created_at"],"archived":truth(&m["archived"])},"coverage":coverage,"task_ref":task,"binding_evidence":if truth(&task){"CANONICAL_ROUTE"}else{"THREAD_UNBOUND"},"turn":turn})}).collect::<Vec<_>>();
    Ok(json!([events, state]))
}
pub fn invoke(
    op: &str,
    a: &Value,
    call: impl FnMut(&str, Value) -> Result<Value>,
) -> Result<Value> {
    match op {
        "source.generation" => {
            let mut rows = Vec::new();
            for v in [&a["native_index"], &a["canonical_path"]] {
                let p = text(v);
                if let Ok(m) = Path::new(p).metadata() {
                    rows.push(json!([p, m.dev(), m.ino()]));
                }
            }
            Ok(json!(&sha(compact(&json!(rows)).as_bytes())[..32]))
        }
        "source.canonical_inventory" => inventory(a),
        "source.canonical_turns" => canonical(a),
        "source.sample" => sample(a, call),
        _ => Err("OBSERVATION_SOURCE_OPERATION_UNKNOWN".into()),
    }
}
