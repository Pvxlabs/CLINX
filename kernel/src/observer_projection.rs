//! Historical v1 Observer read compatibility. No migrations or write statements.
//! Normal source reads and projection policy are Rust; optional callbacks retain
//! the public read-only snapshot inspection surface used by qualification.
use crate::{node_scope::Scope, node_tls::Result, read_projection::*};
use regex::Regex;
use serde_json::{Value, json};
use std::{path::Path, sync::LazyLock, time::Duration};
const SOURCE: &str = "LEGACY_SOURCE_UNAVAILABLE";
const TASK: &str = "task_id,project_alias,title,host,status,execution_state,current_stage,current_blocker,created_at,updated_at,last_progress_at,codex_running,retry_required,turn_id";
const EXEC: &str = "execution_ref,task_id,logical_model,resolved_model,stage,execution_state,turn_id,acquired_at,routing_identity_json,execution_policy_json";
const OPS: &str = "host_execution_ref,execution_ref,host,surface,operation_class,capability,operation,started_at,completed_at,duration_ms,exit_code,result_state,timed_out";
const EVENTS: [&str; 10] = [
    "V1SnapshotBaselineImported",
    "V1TerminalStateObserved",
    "V1ExecutionProgressObserved",
    "V1ExecutionResultPersistedObserved",
    "V1HostExecutionStartedObserved",
    "V1HostEvidenceObserved",
    "V1LeaseReleasedObserved",
    "V1UnattributedLeaseReleasedObserved",
    "V1ExecutionClaimObserved",
    "V1UnattributedExecutionClaimObserved",
];
fn terminal(s: &str) -> bool {
    matches!(
        s,
        "COMPLETED"
            | "IN_REVIEW"
            | "BLOCKED"
            | "FAILED"
            | "RECOVERY_REQUIRED"
            | "CANCELLED"
            | "STOPPED"
    )
}
fn menu(state: &Value, running: &Value, retry: &Value, result: &Value) -> Value {
    json!(if matches!(
        text(state),
        "BLOCKED" | "FAILED" | "RECOVERY_REQUIRED" | "TRANSPORT_UNCERTAIN"
    ) || truth(retry)
        || result["status"] == "BLOCKED"
    {
        "BLOCKED"
    } else if matches!(
        text(state),
        "CLAIMED"
            | "DISPATCHING"
            | "TURN_STARTED"
            | "CODEX_RUNNING"
            | "FINALIZING"
            | "CANCEL_REQUESTED"
            | "CANCELLATION_PENDING"
    ) || truth(running)
    {
        "RUNNING"
    } else if matches!(text(state), "COMPLETED" | "IN_REVIEW") && result["status"] == "PASS" {
        "PASS"
    } else if matches!(
        text(state),
        "QUEUED" | "STOPPED" | "CANCELLED" | "COMPLETED" | "IN_REVIEW"
    ) {
        "IDLE"
    } else {
        "BLOCKED"
    })
}
static REDACT: LazyLock<Vec<(Regex, &str)>> = LazyLock::new(|| {
    vec![
        (Regex::new(r"[\x00-\x08\x0b-\x1f\x7f]").unwrap(), ""),
        (
            Regex::new(r"(?i)bearer\s+[^\s,;]+").unwrap(),
            "Bearer [REDACTED]",
        ),
        (
            Regex::new(r"(?i)\b(password|secret|api[_-]?key|token)\s*[:=]\s*[^\s,;]+").unwrap(),
            "${1}=[REDACTED]",
        ),
    ]
});
fn safe(v: &Value, limit: usize) -> Value {
    let Some(v) = v.as_str() else {
        return Value::Null;
    };
    let mut s = truncate(v, limit);
    for (p, r) in REDACT.iter() {
        s = p.replace_all(&s, *r).into_owned();
    }
    json!(s)
}
fn object(v: &Value) -> Value {
    serde_json::from_str::<Value>(text(v))
        .ok()
        .filter(Value::is_object)
        .unwrap_or(json!({}))
}
fn timestamp(v: &Value) -> Value {
    let Ok(t) = chrono::DateTime::parse_from_rfc3339(&text(v).replace(' ', "T")) else {
        return Value::Null;
    };
    json!(t.to_rfc3339_opts(
        if t.timestamp_subsec_micros() == 0 {
            chrono::SecondsFormat::Secs
        } else {
            chrono::SecondsFormat::Micros
        },
        false
    ))
}
fn elapsed(start: &Value, end: &Value) -> Value {
    let parse = |v: &Value| -> Option<i64> {
        let s = text(v).replace(' ', "T");
        chrono::DateTime::parse_from_rfc3339(&s)
            .ok()
            .map(|t| t.timestamp_micros())
    };
    match (parse(start), parse(end)) {
        (Some(a), Some(b)) if (b - a) / 1_000_000 >= 0 => json!((b - a) / 1_000_000),
        _ => Value::Null,
    }
}
fn event_cursor(task: &Value, cursor: &Value) -> Value {
    json!(format!(
        "{}.{}",
        &sha(text(task).as_bytes())[..16],
        if text(cursor).is_empty() {
            cursor.to_string()
        } else {
            text(cursor).to_owned()
        }
    ))
}
fn cursor(task: &Value, after: &Value) -> Result<i64> {
    if after.is_null() {
        return Ok(0);
    }
    let v = after.as_str().ok_or("INVALID_CURSOR")?;
    let (prefix, n) = v.split_once('.').ok_or("INVALID_CURSOR")?;
    if prefix != &sha(text(task).as_bytes())[..16]
        || n.is_empty()
        || n.len() > 19
        || !n.bytes().all(|b| b.is_ascii_digit())
    {
        return Err("INVALID_CURSOR".into());
    }
    n.parse().map_err(|_| "INVALID_CURSOR".into())
}
struct Database<'a, F: FnMut(&str, Value) -> Result<Value>> {
    conn: Option<rusqlite::Connection>,
    call: &'a mut F,
}
impl<'a, F: FnMut(&str, Value) -> Result<Value>> Database<'a, F> {
    fn query(&mut self, query: &str, args: &[Value]) -> Result<Vec<Value>> {
        if let Some(conn) = &self.conn {
            rows(
                conn,
                query,
                &args.iter().map(sql).collect::<Vec<_>>(),
                SOURCE,
            )
        } else {
            (self.call)("legacy.sql", json!({"sql":query,"arguments":args}))?
                .as_array()
                .cloned()
                .ok_or(SOURCE.into())
        }
    }
    fn table(&mut self, name: &str) -> Result<bool> {
        Ok(!self
            .query("SELECT 1 FROM sqlite_master WHERE name=?", &[json!(name)])?
            .is_empty())
    }
    fn visible(&mut self, task: &Value) -> Result<bool> {
        if !self.table("native_task_bindings")? {
            return Ok(true);
        }
        let bindings = self.query(
            "SELECT * FROM native_task_bindings WHERE task_id=? AND origin='CODEX_NATIVE' LIMIT 2",
            std::slice::from_ref(task),
        )?;
        if bindings.is_empty()
            || !self
                .query(
                    "SELECT 1 FROM conversation_bindings WHERE task_id=?",
                    std::slice::from_ref(task),
                )?
                .is_empty()
        {
            return Ok(true);
        }
        for b in bindings {
            let facts = (self.call)("legacy.grant", json!({"node_id":b["node_id"]}))?;
            if facts["scope"].is_null() || facts["record"].is_null() {
                continue;
            }
            let scope = Scope::parse(&facts["scope"])?;
            let provenance = object(&b["first_provenance_json"]);
            if facts["record"]["public_key_fingerprint"] == provenance["node_fingerprint"]
                && b["user_scope"] == facts["user_scope"]
                && !scope.revoked
                && scope.read_sessions
                && scope.providers.iter().any(|p| b["provider"] == p.as_str())
                && scope.project(&b["project"])
            {
                return Ok(true);
            }
        }
        Ok(false)
    }
    fn task(&mut self, task: &Value) -> Result<Value> {
        let rows = self.query(
            &format!("SELECT {TASK} FROM tasks WHERE task_id=?"),
            std::slice::from_ref(task),
        )?;
        let row = rows.first().ok_or("TASK_NOT_FOUND")?;
        if !self.visible(task)? {
            return Err("TASK_NOT_FOUND".into());
        }
        Ok(row.clone())
    }
    fn events(&mut self, task: &Value, after: &Value, recent: bool) -> Result<Value> {
        let cursor = cursor(task, after)?;
        if !self.table("v2_events")? {
            if cursor != 0 {
                return Err("CURSOR_RESET_REQUIRED".into());
            }
            return Ok(
                json!({"items":[],"next_cursor":event_cursor(task,&json!(0)),"has_more":false,"coverage":"UNAVAILABLE"}),
            );
        }
        let high =
            self.query("SELECT coalesce(max(cursor),0) AS high FROM v2_events", &[])?[0]["high"]
                .as_i64()
                .ok_or(SOURCE)?;
        if cursor > high {
            return Err("CURSOR_RESET_REQUIRED".into());
        }
        let limit = if recent { 20 } else { 100 };
        let mut args = vec![json!(cursor), json!(high), task.clone()];
        args.extend(EVENTS.iter().map(|s| json!(s)));
        args.push(json!(limit + 1));
        let query = format!(
            "SELECT cursor,event_id,event_type,occurred_at,recorded_at,json_extract(CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{{}}' END,'$.source_execution_ref') AS execution_ref FROM v2_events WHERE cursor>? AND cursor<=? AND json_extract(CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{{}}' END,'$.source_task_id')=? AND event_type IN ({}) ORDER BY cursor {} LIMIT ?",
            vec!["?"; EVENTS.len()].join(","),
            if recent { "DESC" } else { "ASC" }
        );
        let mut selected = self.query(&query, &args)?;
        let more = selected.len() > limit;
        selected.truncate(limit);
        let next = if more && !recent {
            selected.last().unwrap()["cursor"].clone()
        } else {
            json!(high)
        };
        Ok(
            json!({"items":selected.iter().map(|r|json!({"cursor":event_cursor(task,&r["cursor"]),"event_ref":safe(&r["event_id"],256),"kind":r["event_type"],"execution_ref":safe(&r["execution_ref"],256),"occurred_at":timestamp(&r["occurred_at"]),"recorded_at":timestamp(&r["recorded_at"])})).collect::<Vec<_>>(),"next_cursor":event_cursor(task,&next),"has_more":more,"coverage":"PARTIAL"}),
        )
    }
    fn project(&mut self, task: &Value, observed: &Value, a: &Value) -> Result<Value> {
        let tref = &task["task_id"];
        let current = self.query(
            &format!("SELECT {EXEC},NULL AS released_at FROM executions WHERE task_id=?"),
            std::slice::from_ref(tref),
        )?;
        let active = !current.is_empty();
        let execution = if active {
            current[0].clone()
        } else {
            let rows=self.query(&format!("SELECT {EXEC},released_at FROM execution_history WHERE task_id=? ORDER BY acquired_at DESC,execution_ref DESC LIMIT 1"),std::slice::from_ref(tref))?;
            rows.first()
                .filter(|r| truth(&task["turn_id"]) && r["turn_id"] == task["turn_id"])
                .cloned()
                .unwrap_or(json!({}))
        };
        let eref = &execution["execution_ref"];
        let bindings = self.query(
            "SELECT thread_id FROM conversation_bindings WHERE task_id=?",
            std::slice::from_ref(tref),
        )?;
        let mut binding = bindings.first().cloned();
        let mut native = Value::Null;
        if binding.is_none() && self.table("native_task_bindings")? {
            let rows = self.query(
                "SELECT node_id,thread_id FROM native_task_bindings WHERE task_id=? LIMIT 2",
                std::slice::from_ref(tref),
            )?;
            if rows.len() == 1 {
                native = rows[0].clone();
                binding = Some(native.clone());
            }
        }
        let mut conversation = if let Some(b) = &binding {
            json!({"node_id":safe(if native.is_null(){&task["host"]}else{&native["node_id"]},256),"provider":"codex_app_server","thread_id":safe(&b["thread_id"],256)})
        } else {
            Value::Null
        };
        let route = object(&execution["routing_identity_json"]);
        let thread = if truth(&execution) {
            route["conversation"]["binding"].clone()
        } else {
            binding
                .as_ref()
                .map(|b| b["thread_id"].clone())
                .unwrap_or(Value::Null)
        };
        let uri = (self.call)(
            "legacy.codex_uri",
            json!({"thread_id":thread,"host_id":a["codex_host_ids"].get(text(&task["host"]))}),
        )?;
        if !conversation.is_null() && truth(&execution) {
            if thread.is_string() && !matches!(text(&thread), "UNBOUND" | "UNKNOWN" | "BOUND") {
                conversation["thread_id"] = thread;
            } else {
                conversation = Value::Null;
            }
        }
        let mut result = Value::Null;
        if truth(eref) && truth(&execution["turn_id"]) && execution["turn_id"] == task["turn_id"] {
            let rows=self.query("SELECT status,summary,validation,blockers,next_state,received_at FROM execution_results WHERE task_id=? AND execution_ref=? AND turn_id=?",&[tref.clone(),eref.clone(),execution["turn_id"].clone()])?;
            if let Some(r) = rows.first() {
                let mut out = serde_json::Map::new();
                for k in ["status", "summary", "validation", "blockers", "next_state"] {
                    out.insert(k.into(), safe(&r[k], 4096));
                }
                out.extend(json!({"execution_ref":eref,"received_at":timestamp(&r["received_at"]),"changed_files":null,"text_truncated":(["summary","validation","blockers"].iter().any(|k|text(&r[*k]).chars().count()>4096)),"redaction":"PATHS_AND_RAW_RESULT_OMITTED"}).as_object().unwrap().clone());
                result = Value::Object(out);
            }
        }
        let mut routing = serde_json::Map::new();
        for k in ["host", "surface", "provider", "transport"] {
            let status = safe(&route[k]["status"], 256);
            routing.insert(k.into(),json!({"identifier":safe(&route[k]["stable_identifier"],256),"status":if truth(&status){status}else{json!("UNKNOWN")}}));
        }
        let policy = object(&execution["execution_policy_json"]);
        let classes = policy["operation_classes"]
            .as_array()
            .map(|v| {
                json!(
                    v.iter()
                        .take(16)
                        .filter(|v| v.is_string())
                        .map(|v| safe(v, 256))
                        .collect::<Vec<_>>()
                )
            })
            .unwrap_or(Value::Null);
        let mut reasoning = Value::Null;
        let mut operations = Vec::new();
        if truth(eref) {
            let rows=self.query("SELECT reasoning_effort FROM prepared_executions WHERE resulting_execution_ref=? AND resulting_task_id=? ORDER BY created_at DESC LIMIT 1",&[eref.clone(),tref.clone()])?;
            reasoning = rows
                .first()
                .map(|r| safe(&r["reasoning_effort"], 256))
                .unwrap_or(Value::Null);
            operations=self.query(&format!("SELECT {OPS} FROM host_executions WHERE task_id=? AND execution_ref=? ORDER BY started_at DESC,host_execution_ref DESC LIMIT ?"),&[tref.clone(),eref.clone(),json!(21)])?;
        }
        let more = operations.len() > 20;
        operations.truncate(20);
        for op in &mut operations {
            for v in op.as_object_mut().ok_or(SOURCE)?.values_mut() {
                if v.is_string() {
                    *v = safe(v, 256);
                }
            }
            op["timed_out"] = json!(truth(&op["timed_out"]));
            op["started_at"] = timestamp(&op["started_at"]);
            op["completed_at"] = timestamp(&op["completed_at"]);
        }
        let field = |v: &Value| {
            let v = safe(v, 256);
            if truth(&v) { v } else { json!("UNKNOWN") }
        };
        let state = field(&task["execution_state"]);
        let stage = field(&task["current_stage"]);
        let mut end = timestamp(&execution["released_at"]);
        if end.is_null() && !result.is_null() {
            end = result["received_at"].clone();
        }
        if end.is_null() && active && !terminal(text(&state)) {
            end = observed.clone();
        }
        let start = timestamp(&execution["acquired_at"]);
        let blocker = safe(&task["current_blocker"], 2048);
        Ok(
            json!({"schema_version":"1","task_ref":tref,"execution_ref":eref,"project":safe(&task["project_alias"],256),"title":safe(&task["title"],512),"host":safe(&task["host"],256),"state":state,"stage":stage,
            "native_conversation":conversation,"codex_uri":uri,"execution_state":field(&execution["execution_state"]),"execution_stage":field(&execution["stage"]),
            "model":{"logical":safe(&execution["logical_model"],256),"resolved":safe(&execution["resolved_model"],256)},"reasoning":reasoning,
            "current_activity":{"kind":"PERSISTED_STAGE","label":stage,"observed_at":timestamp(&task["last_progress_at"])},"blocker":if truth(&blocker){json!({"code":"PERSISTED_BLOCKER","message":blocker})}else{Value::Null},
            "timestamps":{"created_at":timestamp(&task["created_at"]),"updated_at":timestamp(&task["updated_at"]),"last_progress_at":timestamp(&task["last_progress_at"]),"started_at":start,"completed_at":if terminal(text(&state)){timestamp(&end)}else{Value::Null},"observed_at":observed},
            "elapsed_seconds":elapsed(&start,&end),"codex_running":truth(&task["codex_running"]),"retry_required":truth(&task["retry_required"]),
            "mutation_boundary":{"observer_read_only":true,"allowed_actions":[],"execution_operation_classes":classes,"execution_surface":safe(&policy["execution_surface"],256),"authority_status":if classes.is_null(){"UNKNOWN"}else{"PERSISTED"}},
            "routing":routing,"phases":[],"progress_percent":null,"progress_basis":"NO_PERSISTED_DENOMINATOR","recent_events":self.events(tref,&Value::Null,true)?,"host_operations":operations,"host_operations_has_more":more,
            "final_result":result,"artifacts":[],"artifacts_status":"NO_SAFE_ARTIFACT_REGISTRY","menu_state":menu(&state,&task["codex_running"],&task["retry_required"],&result)}),
        )
    }
}
pub fn invoke(
    op: &str,
    a: &Value,
    call: &mut impl FnMut(&str, Value) -> Result<Value>,
) -> Result<Value> {
    match op {
        "native_observer.legacy_safe_text" => {
            return Ok(safe(
                &a["value"],
                a["limit"].as_u64().unwrap_or(256) as usize,
            ));
        }
        "native_observer.legacy_object" => return Ok(object(&a["value"])),
        "native_observer.legacy_timestamp" => return Ok(timestamp(&a["value"])),
        "native_observer.legacy_elapsed" => return Ok(elapsed(&a["start"], &a["end"])),
        "native_observer.legacy_menu" => {
            return Ok(menu(&a["state"], &a["running"], &a["retry"], &a["result"]));
        }
        "native_observer.legacy_event_cursor" => {
            return Ok(event_cursor(&a["task_ref"], &a["cursor"]));
        }
        "native_observer.legacy_parse_cursor" => {
            return Ok(json!(cursor(&a["task_ref"], &a["after"])?));
        }
        _ => {}
    }
    let conn = if a["external_snapshot"] == true {
        None
    } else {
        let c = readonly(Path::new(text(&a["path"])), SOURCE, false)?;
        c.busy_timeout(Duration::from_millis(250))
            .map_err(|_| SOURCE)?;
        let mut steps = 0;
        c.progress_handler(
            1000,
            Some(move || {
                steps += 1000;
                steps > 10_000_000
            }),
        );
        Some(c)
    };
    let mut db = Database { conn, call };
    let tref = &a["task_ref"];
    let observed = json!(super::stamp());
    match op {
        "native_observer.legacy_health" => {
            db.query("SELECT task_id FROM tasks LIMIT 1", &[])?;
            Ok(
                json!({"schema_version":"1","status":"OK","read_only":true,"observed_at":observed,"authority_liveness":"UNKNOWN","auth_mode":"OBSERVER_BEARER","events_transport":"POLLING"}),
            )
        }
        "native_observer.legacy_visible" => Ok(json!(db.visible(tref)?)),
        "native_observer.legacy_task_row" => db.task(tref),
        "native_observer.legacy_events" => db.events(tref, &a["after"], truth(&a["recent"])),
        "native_observer.legacy_project" => db.project(&a["task"], &a["observed"], a),
        "native_observer.legacy_task" => {
            let task = db.task(tref)?;
            db.project(&task, &observed, a)
        }
        "native_observer.legacy_task_events" => {
            db.task(tref)?;
            let mut events = db.events(tref, &a["after"], false)?;
            events.as_object_mut().unwrap().extend(
                json!({"schema_version":"1","task_ref":tref,"observed_at":observed})
                    .as_object()
                    .unwrap()
                    .clone(),
            );
            Ok(events)
        }
        "native_observer.legacy_list" => {
            let predicate = if a["state"] == "active" {
                "NOT IN"
            } else {
                "IN"
            };
            let tasks=db.query(&format!("SELECT {TASK} FROM tasks WHERE execution_state {predicate} (?,?,?,?) ORDER BY updated_at DESC,task_id DESC LIMIT ? OFFSET ?"),&[json!("COMPLETED"),json!("IN_REVIEW"),json!("CANCELLED"),json!("STOPPED"),json!(51),a["offset"].clone()])?;
            let mut items = Vec::new();
            for task in tasks.iter().take(50) {
                if db.visible(&task["task_id"])? {
                    items.push(db.project(task, &observed, a)?);
                }
            }
            Ok(
                json!({"schema_version":"1","observed_at":observed,"items":items,"next_offset":if tasks.len()>50{json!(a["offset"].as_i64().unwrap_or(0)+50)}else{Value::Null},"has_more":tasks.len()>50}),
            )
        }
        "native_observer.legacy_activity_execution" => {
            let task = db.task(tref)?;
            let projected = db.project(&task, &observed, a)?;
            if projected["execution_ref"] != a["execution_ref"] {
                return Err("ACTIVITY_EXECUTION_CHANGED".into());
            }
            let rows=db.query("SELECT execution_ref,turn_id,routing_identity_json FROM executions WHERE task_id=? AND execution_ref=? UNION ALL SELECT execution_ref,turn_id,routing_identity_json FROM execution_history WHERE task_id=? AND execution_ref=?",&[tref.clone(),a["execution_ref"].clone(),tref.clone(),a["execution_ref"].clone()])?;
            if rows.len() != 1 {
                return Err("ACTIVITY_EXECUTION_CHANGED".into());
            }
            Ok(rows[0].clone())
        }
        _ => Err("NATIVE_OBSERVER_OPERATION_UNKNOWN".into()),
    }
}
