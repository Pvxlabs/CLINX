//! Read-only native Codex index/history. Exact identity, bounded SQL/file reads,
//! privacy allowlists and source-bound cursors. Never starts or claims a writer.
use crate::{node_tls::Result, read_projection::*};
use base64::{
    Engine,
    engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD},
};
use regex::Regex;
use rusqlite::Connection;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs::File,
    io::{BufRead, BufReader, Read, Seek, SeekFrom},
    path::{Path, PathBuf},
    sync::LazyLock,
};
pub const SCAN_BYTES: usize = 512 * 1024;
pub const LEGACY_REPLY_RETIREMENT: &str = concat!(
    "CLINX_LEGACY_REPLY_FORMAT_RETIRED_V1\n",
    "The reply-format requirements in the earlier CLINX MANAGED EXECUTION CONTRACT are retired: ",
    "CLINX no longer owns or constrains the final response format. The CLINX_EXECUTION_RESULT marker ",
    "and STATUS, SUMMARY, CHANGED_FILES, VALIDATION, BLOCKERS, NEXT_STATE fields are no longer required. ",
    "This only retires that CLINX-generated output template; all other instructions remain in effect."
);
pub fn normalize(v: &Value) -> &'static str {
    let v = if v.is_object() {
        ["type", "status", "state"]
            .iter()
            .map(|k| &v[k])
            .find(|v| truth(v))
            .unwrap_or(&Value::Null)
    } else {
        v
    };
    let key = text(v).replace(['_', '-'], "").to_lowercase();
    match key.as_str() {
        "inprogress" | "running" | "active" | "started" | "processing" => "RUNNING",
        "paused" => "PAUSED",
        "completed" | "succeeded" | "success" => "COMPLETED",
        "failed" | "error" | "errored" | "systemerror" => "FAILED",
        "timeout" | "timedout" => "TIMED_OUT",
        "cancelled" | "canceled" => "CANCELLED",
        "interrupted" | "aborted" => "INTERRUPTED",
        "disconnected" | "connectionlost" | "connectionclosed" => "DISCONNECTED",
        _ => "UNKNOWN",
    }
}
pub fn terminal(s: &str) -> bool {
    matches!(s, "COMPLETED" | "FAILED" | "CANCELLED" | "INTERRUPTED")
}
pub fn model(v: &Value) -> Value {
    static R: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$").unwrap());
    if v.as_str().is_some_and(|s| R.is_match(s)) {
        v.clone()
    } else {
        Value::Null
    }
}
static REDACT: LazyLock<Vec<Regex>> = LazyLock::new(|| {
    [
    r"(?i)\b(authorization\s*[:=]\s*bearer\s+|(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*)[^\s,;]+",
    r"\b(?:sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,})\b",
    r"(?s)-----BEGIN [^\-]*PRIVATE KEY-----.*?-----END [^\-]*PRIVATE KEY-----",
    r"\blin_api_[A-Za-z0-9]+\b",
    r"(?is)<in-app-browser-context\b[^>]*>.*?</in-app-browser-context>"
].into_iter().map(|p|Regex::new(p).unwrap()).collect()
});
pub fn message(v: &Value) -> Option<(String, String)> {
    if ["analysis", "reasoning"]
        .iter()
        .any(|s| v["channel"] == *s || v["phase"] == *s)
    {
        return None;
    }
    let (role, s) = match text(&v["type"]) {
        "agentMessage" => ("assistant", v["text"].as_str()?.into()),
        "userMessage" | "message"
            if v["type"] == "userMessage"
                || matches!(v["role"].as_str(), Some("user" | "assistant")) =>
        {
            let content = v["content"].as_array()?;
            let content_text = content
                .iter()
                .filter(|c| {
                    matches!(
                        c["type"].as_str(),
                        Some("text" | "input_text" | "output_text")
                    )
                })
                .filter_map(|c| c["text"].as_str())
                .collect::<Vec<_>>()
                .join("\n");
            (
                if v["type"] == "userMessage" {
                    "user"
                } else {
                    text(&v["role"])
                },
                content_text,
            )
        }
        _ => return None,
    };
    if s.trim().is_empty() {
        return None;
    }
    let mut s = REDACT[0].replace_all(&s, "${1}[REDACTED]").into_owned();
    for r in &REDACT[1..4] {
        s = r.replace_all(&s, "[REDACTED]").into_owned();
    }
    s = REDACT[4].replace_all(&s, "").replace('\0', "");
    Some((role.into(), s))
}
pub fn cursor_encode(tid: &str, host: &str, mode: &str, before: &Value) -> Value {
    // JSON whitespace is irrelevant to opaque cursors; old Python tokens decode.
    json!(URL_SAFE_NO_PAD.encode(json!([1, tid, host, mode, before]).to_string()))
}
pub fn cursor_decode(v: &Value, tid: &str, host: &str, mode: &str) -> Result<Option<i64>> {
    if v.is_null() {
        return Ok(None);
    }
    let parsed = (|| -> Option<i64> {
        let s = v.as_str().filter(|s| s.len() <= 1024)?;
        let bytes = URL_SAFE_NO_PAD
            .decode(s.trim_end_matches('='))
            .or_else(|_| STANDARD.decode(s))
            .ok()?;
        let value: Value = serde_json::from_slice(&bytes).ok()?;
        let a = value.as_array()?;
        if a.len() != 5 || a[..4] != [json!(1), json!(tid), json!(host), json!(mode)] {
            return None;
        }
        a[4].as_i64().filter(|n| *n >= 0)
    })();
    parsed.map(Some).ok_or("INVALID_CONTEXT_CURSOR".into())
}
#[derive(Clone, Copy)]
pub struct ContextLimits {
    pub recent: usize,
    pub budget: usize,
}
struct Pagination {
    next: Value,
    incomplete: bool,
}
fn context_result(
    tid: &str,
    entries: &[Value],
    source: &str,
    max_bytes: usize,
    turns: Value,
    page: Pagination,
    evidence: Value,
) -> Value {
    let Pagination { next, incomplete } = page;
    let mut latest = BTreeMap::new();
    for row in entries {
        if let Some((role, s)) = message(&row["item"]) {
            latest.insert(role, (s, row["turn_id"].clone(), row["timestamp"].clone()));
        }
    }
    let budget = max_bytes / latest.len().max(1);
    let mut texts = BTreeMap::new();
    let mut refs = serde_json::Map::new();
    let mut truncated = incomplete;
    for (role, (s, turn, timestamp)) in latest {
        truncated |= s.len() > budget;
        texts.insert(role.clone(), bytes(&s, budget));
        refs.insert(role, json!({"turn_id":turn,"timestamp":timestamp}));
    }
    let mut provenance = json!({"queried_thread_id":tid,"context_turn_refs":turns,"message_refs":refs,"execution_attribution":"NOT_CLAIMED"});
    if let Some(m) = evidence.as_object() {
        for (k, v) in m {
            provenance[k] = v.clone();
        }
    }
    json!({"context_status":if texts.is_empty(){"CONTEXT_UNAVAILABLE"}else{"AVAILABLE"},"context_source":source,"context_scope":"THREAD_RECENT","context_range":"cursor-page","context_truncated":truncated||!next.is_null(),"next_cursor":next,"last_user_intent":texts.get("user"),"last_codex_result":texts.get("assistant"),"provenance":provenance})
}
pub struct History {
    pub root: PathBuf,
}
fn readable_index(path: &Path, unavailable: &str) -> Result<Connection> {
    // Preserve the native read API's permission error separately from an
    // absent/incompatible database. access uses the same real UID as os.access.
    if path.exists() && rustix::fs::access(path, rustix::fs::Access::READ_OK).is_err() {
        return Err("NATIVE_ACCESS_DENIED".into());
    }
    readonly(path, unavailable, true)
}
impl History {
    pub fn new(root: &str) -> Self {
        Self {
            root: resolve(Path::new(root)),
        }
    }
    pub fn metadata(&self, tid: &str) -> Result<Value> {
        let p = self.root.join("state_5.sqlite");
        if !p.exists() {
            return Err("NATIVE_INDEX_UNAVAILABLE".into());
        }
        let c = readable_index(&p, "NATIVE_INDEX_UNAVAILABLE")?;
        let cols = columns(&c, "threads")?;
        if !["id", "cwd", "rollout_path"]
            .iter()
            .all(|k| cols.iter().any(|c| c == k))
        {
            return Err("NATIVE_SCHEMA_UNSUPPORTED".into());
        }
        let mut fields = vec!["id".into(), "cwd".into(), "rollout_path".into()];
        for k in [
            "name",
            "title",
            "history_mode",
            "archived",
            "created_at",
            "updated_at",
            "cli_version",
            "project_id",
        ] {
            if cols.iter().any(|s| s == k) {
                fields.push(if matches!(k, "name" | "title") {
                    format!("substr({k},1,512) AS {k}")
                } else {
                    k.into()
                });
            }
        }
        Ok(rows(
            &c,
            &format!("SELECT {} FROM threads WHERE id=?", fields.join(",")),
            &[tid.to_owned().into()],
            "NATIVE_INDEX_UNAVAILABLE",
        )?
        .into_iter()
        .next()
        .unwrap_or(Value::Null))
    }
    fn header(path: &Path) -> Result<Value> {
        let file = File::open(path).map_err(|_| "NATIVE_IDENTITY_CONFLICT")?;
        let mut bytes = Vec::new();
        BufReader::new(file)
            .take(131072)
            .read_until(b'\n', &mut bytes)
            .map_err(|_| "NATIVE_IDENTITY_CONFLICT")?;
        serde_json::from_slice(&bytes).map_err(|_| "NATIVE_IDENTITY_CONFLICT".into())
    }
    pub fn path(&self, tid: &str, m: &Value) -> Result<PathBuf> {
        let p = resolve(Path::new(
            m["rollout_path"]
                .as_str()
                .ok_or("NATIVE_IDENTITY_CONFLICT")?,
        ));
        let name = p
            .file_name()
            .and_then(|s| s.to_str())
            .ok_or("NATIVE_IDENTITY_CONFLICT")?;
        let fork = Regex::new(&format!(
            r".*-{}_[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}\.jsonl$",
            regex::escape(tid)
        ))
        .map_err(|_| "NATIVE_IDENTITY_CONFLICT")?
        .is_match(name);
        if !p.starts_with(&self.root) || !(name.ends_with(&format!("-{tid}.jsonl")) || fork) {
            return Err("NATIVE_IDENTITY_CONFLICT".into());
        }
        if fork {
            let h = Self::header(&p)?;
            let meta = &h["payload"];
            if h["type"] != "session_meta" || meta["id"] != tid || !meta["cwd"].is_string() {
                return Err("NATIVE_IDENTITY_CONFLICT".into());
            }
            if resolve(Path::new(text(&meta["cwd"]))) != resolve(Path::new(text(&m["cwd"])))
                && !self.observed_workspace(&p, text(&m["cwd"]))?
            {
                return Err("NATIVE_IDENTITY_CONFLICT".into());
            }
        }
        Ok(p)
    }
    fn observed_workspace(&self, path: &Path, cwd: &str) -> Result<bool> {
        let mut f = File::open(path).map_err(|_| "NATIVE_IDENTITY_CONFLICT")?;
        let size = f.metadata().map_err(|_| "NATIVE_IDENTITY_CONFLICT")?.len();
        let offsets = if size > SCAN_BYTES as u64 {
            vec![0, size - SCAN_BYTES as u64]
        } else {
            vec![0]
        };
        for offset in offsets {
            f.seek(SeekFrom::Start(offset))
                .map_err(|_| "NATIVE_IDENTITY_CONFLICT")?;
            let mut r = BufReader::new(&mut f);
            if offset > 0 {
                let mut ignored = Vec::new();
                r.by_ref()
                    .take(SCAN_BYTES as u64)
                    .read_until(b'\n', &mut ignored)
                    .map_err(|_| "NATIVE_IDENTITY_CONFLICT")?;
            }
            let mut data = Vec::new();
            r.take(SCAN_BYTES as u64)
                .read_to_end(&mut data)
                .map_err(|_| "NATIVE_IDENTITY_CONFLICT")?;
            for line in data.split(|b| *b == b'\n') {
                if let Ok(v) = serde_json::from_slice::<Value>(line)
                    && v["type"] == "turn_context"
                    && v["payload"]["cwd"]
                        .as_str()
                        .is_some_and(|s| resolve(Path::new(s)) == resolve(Path::new(cwd)))
                {
                    return Ok(true);
                }
            }
        }
        Ok(false)
    }
    pub fn identity(&self, tid: &str, m: &Value) -> Result<Value> {
        let h = Self::header(&self.path(tid, m)?)?;
        let meta = &h["payload"];
        if h["type"] != "session_meta"
            || meta["id"] != tid
            || resolve(Path::new(text(&meta["cwd"]))) != resolve(Path::new(text(&m["cwd"])))
        {
            return Err("NATIVE_IDENTITY_CONFLICT".into());
        }
        Ok(
            json!({"id":tid,"sessionId":meta["id"],"cwd":m["cwd"],"projectId":m["project_id"],"ephemeral":false,"status":{"type":"UNKNOWN"},"cliVersion":if truth(&m["cli_version"]){&m["cli_version"]}else{&meta["cli_version"]},"name":if truth(&m["name"]){&m["name"]}else{&m["title"]},"identity_source":"CODEX_STATE_AND_SESSION_HEADER"}),
        )
    }
    pub fn pending(&self, tid: &str, m: &Value) -> Result<Value> {
        self.identity(tid, m)?;
        let f = File::open(self.path(tid, m)?).map_err(|_| "NATIVE_HISTORY_UNAVAILABLE")?;
        let mut reader = BufReader::new(f);
        let mut pending = false;
        loop {
            let mut line = Vec::new();
            reader
                .by_ref()
                .take(1024 * 1024)
                .read_until(b'\n', &mut line)
                .map_err(|_| "NATIVE_HISTORY_UNAVAILABLE")?;
            if line.is_empty() {
                break;
            }
            if !line.ends_with(b"\n") {
                while !line.is_empty() && !line.ends_with(b"\n") {
                    line.clear();
                    reader
                        .by_ref()
                        .take(1024 * 1024)
                        .read_until(b'\n', &mut line)
                        .map_err(|_| "NATIVE_HISTORY_UNAVAILABLE")?;
                }
                continue;
            }
            let Ok(v) = serde_json::from_slice::<Value>(&line) else {
                continue;
            };
            let p = &v["payload"];
            if v["type"] != "response_item" || p["role"] != "developer" {
                continue;
            }
            let text = p["content"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|c| matches!(c["type"].as_str(), Some("input_text" | "text")))
                .filter_map(|c| c["text"].as_str())
                .collect::<String>();
            if text == LEGACY_REPLY_RETIREMENT {
                pending = false;
            } else if text.starts_with("CLINX MANAGED EXECUTION CONTRACT\n")
                && text.contains("CLINX owns the final response format")
                && text.contains("CLINX_EXECUTION_RESULT")
                && text.contains("END CLINX MANAGED EXECUTION CONTRACT")
            {
                pending = true;
            }
        }
        Ok(json!(pending))
    }
    pub fn projection(&self, tid: &str, m: &Value, c: &Connection) -> Result<String> {
        let p = self.path(tid, m)?;
        let name = p.file_name().unwrap().to_string_lossy();
        if name.ends_with(&format!("-{tid}.jsonl")) {
            return Ok(tid.into());
        }
        let fork = p
            .file_stem()
            .unwrap()
            .to_string_lossy()
            .rsplit('_')
            .next()
            .unwrap()
            .to_owned();
        if rows(c,"SELECT next_rollout_byte_offset FROM thread_history_projection_state WHERE thread_id=?",&[fork.clone().into()],"NATIVE_SCHEMA_UNSUPPORTED")?.is_empty(){return Ok(tid.into());}
        if let Some(turn)=rows(c,"SELECT turn_id,rollout_byte_offset FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1",&[fork.clone().into()],"NATIVE_SCHEMA_UNSUPPORTED")?.first(){
            let offset=turn["rollout_byte_offset"].as_u64().ok_or("NATIVE_IDENTITY_CONFLICT")?;
            let mut f=File::open(&p).map_err(|_|"NATIVE_IDENTITY_CONFLICT")?;
            if offset>=f.metadata().map_err(|_|"NATIVE_IDENTITY_CONFLICT")?.len(){return Err("NATIVE_IDENTITY_CONFLICT".into());}
            f.seek(SeekFrom::Start(offset)).map_err(|_|"NATIVE_IDENTITY_CONFLICT")?;
            let mut line=Vec::new();BufReader::new(f).take(131072).read_until(b'\n',&mut line).map_err(|_|"NATIVE_IDENTITY_CONFLICT")?;
            let row:Value=serde_json::from_slice(&line).map_err(|_|"NATIVE_IDENTITY_CONFLICT")?;
            if row["type"]!="event_msg"||row["payload"]["type"]!="task_started"||row["payload"]["turn_id"]!=turn["turn_id"]{return Err("NATIVE_IDENTITY_CONFLICT".into());}
        }
        Ok(fork)
    }
    pub fn status(&self, tid: &str, m: &Value) -> Value {
        let mut base = json!({"source":"NATIVE_PERSISTED_HISTORY","status":null,"state":"UNKNOWN","terminal":false});
        if m["history_mode"] != "paginated" {
            base["reason"] = json!("NATIVE_HISTORY_MODE_UNSUPPORTED");
            return base;
        }
        let result = (|| -> Result<Value> {
            let c = readonly(
                &self.root.join("thread_history_1.sqlite"),
                "NATIVE_HISTORY_UNAVAILABLE",
                true,
            )?;
            let history_tid = self.projection(tid, m, &c)?;
            let mut row=rows(&c,"SELECT turn_id,status,started_at,completed_at FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1",&[history_tid.clone().into()],"NATIVE_HISTORY_UNAVAILABLE")?.into_iter().next().ok_or("NATIVE_TURN_UNAVAILABLE")?;
            row["state"] = json!(normalize(&row["status"]));
            row["terminal"] = json!(terminal(normalize(&row["status"])));
            row["source"] = base["source"].clone();
            row["history_thread_id"] = json!(history_tid);
            Ok(row)
        })();
        match result {
            Ok(v) => v,
            Err(e) => {
                base["reason"] = json!(e);
                base
            }
        }
    }
    pub fn turn_model(&self, tid: &str, m: &Value, turn: &str, offset: &Value) -> Value {
        let result = (|| -> Result<Value> {
            let offset = offset.as_u64().ok_or("MODEL_UNAVAILABLE")?;
            if turn.is_empty() {
                return Err("MODEL_UNAVAILABLE".into());
            }
            let mut f = File::open(self.path(tid, m)?).map_err(|_| "MODEL_UNAVAILABLE")?;
            if offset >= f.metadata().map_err(|_| "MODEL_UNAVAILABLE")?.len() {
                return Err("MODEL_UNAVAILABLE".into());
            }
            f.seek(SeekFrom::Start(offset))
                .map_err(|_| "MODEL_UNAVAILABLE")?;
            let mut reader = BufReader::new(f).take(128 * 1024);
            let mut anchored = false;
            loop {
                let mut line = Vec::new();
                reader
                    .read_until(b'\n', &mut line)
                    .map_err(|_| "MODEL_UNAVAILABLE")?;
                if line.is_empty() || !line.ends_with(b"\n") {
                    return Err("MODEL_UNAVAILABLE".into());
                }
                let row: Value = serde_json::from_slice(&line).map_err(|_| "MODEL_UNAVAILABLE")?;
                let p = &row["payload"];
                if !p.is_object() {
                    return Err("MODEL_UNAVAILABLE".into());
                }
                let start = row["type"] == "event_msg" && p["type"] == "task_started";
                let context = row["type"] == "turn_context";
                if (!anchored || start || context) && (!(start || context) || p["turn_id"] != turn)
                {
                    return Err("MODEL_UNAVAILABLE".into());
                }
                anchored = true;
                if context {
                    if p.get("cwd").unwrap_or(&m["cwd"]) != &m["cwd"] {
                        return Err("MODEL_UNAVAILABLE".into());
                    }
                    return Ok(model(&p["model"]));
                }
            }
        })();
        result.unwrap_or(Value::Null)
    }
    pub fn context(
        &self,
        tid: &str,
        host: &str,
        m: &Value,
        limits: ContextLimits,
        anchor: &Value,
        cursor: &Value,
    ) -> Result<Value> {
        let ContextLimits { recent, budget } = limits;
        if !(1..=20).contains(&recent) || !(1024..=128000).contains(&budget) {
            return Err("INVALID_CONTEXT_LIMIT".into());
        }
        if m["history_mode"] == "paginated" {
            self.paginated(tid, host, m, limits, anchor, cursor)
        } else {
            self.legacy(tid, host, m, limits, anchor, cursor)
        }
    }
    fn paginated(
        &self,
        tid: &str,
        host: &str,
        m: &Value,
        limits: ContextLimits,
        anchor: &Value,
        cursor: &Value,
    ) -> Result<Value> {
        let ContextLimits { recent, budget } = limits;
        let before = cursor_decode(cursor, tid, host, "paginated")?;
        let c = readable_index(
            &self.root.join("thread_history_1.sqlite"),
            "NATIVE_HISTORY_UNAVAILABLE",
        )?;
        let history_tid = self.projection(tid, m, &c)?;
        let mut predicate = "thread_id=?".to_owned();
        let mut args = vec![history_tid.clone().into()];
        if truth(anchor) {
            predicate.push_str(" AND turn_id=?");
            args.push(sql(anchor));
        }
        if let Some(n) = before {
            predicate.push_str(" AND rollout_ordinal<?");
            args.push(n.into());
        }
        args.push(((recent + 1) as i64).into());
        let mut turns = rows(
            &c,
            &format!(
                "SELECT turn_id,rollout_ordinal,status,started_at,completed_at FROM thread_turns WHERE {predicate} ORDER BY rollout_ordinal DESC LIMIT ?"
            ),
            &args,
            "NATIVE_SCHEMA_UNSUPPORTED",
        )?;
        let more = turns.len() > recent;
        turns.truncate(recent);
        let mut entries = Vec::new();
        let mut parts_truncated = false;
        for turn in turns.iter().rev() {
            for kind in ["userMessage", "agentMessage"] {
                let records = rows(
                    &c,
                    "SELECT item_id,rollout_ordinal,created_at_ms,substr(json_extract(item_json,'$.text'),1,?) AS text,substr(json_extract(item_json,'$.phase'),1,32) AS phase,substr(json_extract(item_json,'$.channel'),1,32) AS channel,json_array_length(item_json,'$.content') AS content_parts,CASE WHEN item_type='userMessage' THEN (SELECT substr(group_concat(substr(json_extract(value,'$.text'),1,?), char(10)),1,?) FROM (SELECT value FROM json_each(item_json,'$.content') LIMIT 32) WHERE json_extract(value,'$.type') IN ('text','input_text')) END AS content FROM thread_items WHERE thread_id=? AND turn_id=? AND item_type=? AND coalesce(json_extract(item_json,'$.phase'),'') NOT IN ('analysis','reasoning') AND coalesce(json_extract(item_json,'$.channel'),'') NOT IN ('analysis','reasoning') ORDER BY rollout_ordinal DESC LIMIT 1",
                    &[
                        (budget as i64 + 1).into(),
                        (budget as i64 + 1).into(),
                        (budget as i64 + 1).into(),
                        history_tid.clone().into(),
                        sql(&turn["turn_id"]),
                        kind.to_owned().into(),
                    ],
                    "NATIVE_SCHEMA_UNSUPPORTED",
                )?;
                for row in records {
                    parts_truncated |= row["content_parts"].as_i64().unwrap_or(0) > 32;
                    entries.push(json!({"item":{"type":kind,"text":row["text"],"phase":row["phase"],"channel":row["channel"],"content":[{"type":"text","text":row["content"].as_str().unwrap_or("")}]},"turn_id":turn["turn_id"],"timestamp":row["created_at_ms"]}));
                }
            }
        }
        let projection = rows(
            &c,
            "SELECT next_rollout_byte_offset FROM thread_history_projection_state WHERE thread_id=?",
            &[history_tid.clone().into()],
            "NATIVE_SCHEMA_UNSUPPORTED",
        )?;
        let offset = projection
            .first()
            .map(|r| r["next_rollout_byte_offset"].clone())
            .unwrap_or(Value::Null);
        let path = self.path(tid, m)?;
        let size = path.metadata().ok().map(|s| s.len());
        let incomplete = offset.as_u64().zip(size).is_none_or(|(a, b)| a < b);
        Ok(context_result(
            tid,
            &entries,
            "CODEX_NATIVE_HISTORY",
            budget,
            json!(
                turns
                    .iter()
                    .rev()
                    .map(|t| &t["turn_id"])
                    .collect::<Vec<_>>()
            ),
            Pagination {
                next: if more {
                    cursor_encode(
                        tid,
                        host,
                        "paginated",
                        &turns.last().unwrap()["rollout_ordinal"],
                    )
                } else {
                    Value::Null
                },
                incomplete: incomplete || parts_truncated,
            },
            json!({"indexed_source":"CODEX_THREAD_HISTORY_PRIMARY_KEY","native_turns":turns,"history_thread_id":history_tid,"projection_byte_offset":offset,"rollout_bytes":size,"projection_incomplete":incomplete,"sql_step_budget":SQL_STEPS,"body_byte_budget":budget,"content_parts_limit":32,"content_parts_truncated":parts_truncated}),
        ))
    }
    fn legacy(
        &self,
        tid: &str,
        host: &str,
        m: &Value,
        limits: ContextLimits,
        anchor: &Value,
        cursor: &Value,
    ) -> Result<Value> {
        let ContextLimits { recent, budget } = limits;
        let before = cursor_decode(cursor, tid, host, "legacy")?;
        let path = self.path(tid, m)?;
        self.identity(tid, m)?;
        let mut reader =
            BufReader::new(File::open(&path).map_err(|_| "NATIVE_HISTORY_UNAVAILABLE")?);
        let mut header = Vec::new();
        reader
            .by_ref()
            .take(131072)
            .read_until(b'\n', &mut header)
            .map_err(|_| "NATIVE_HISTORY_UNAVAILABLE")?;
        let start = reader
            .stream_position()
            .map_err(|_| "NATIVE_HISTORY_UNAVAILABLE")?;
        let size = path
            .metadata()
            .map_err(|_| "NATIVE_HISTORY_UNAVAILABLE")?
            .len();
        let end = (before.map(|n| n as u64).unwrap_or(size)).min(size);
        if end < start {
            return Err("INVALID_CONTEXT_CURSOR".into());
        }
        let offset = start.max(end.saturating_sub(SCAN_BYTES as u64));
        reader
            .seek(SeekFrom::Start(offset))
            .map_err(|_| "NATIVE_HISTORY_UNAVAILABLE")?;
        let mut data = Vec::new();
        reader
            .take(end - offset)
            .read_to_end(&mut data)
            .map_err(|_| "NATIVE_HISTORY_UNAVAILABLE")?;
        let mut next_offset = offset;
        let mut page_start = offset;
        if offset > start {
            if let Some(i) = data.iter().position(|b| *b == b'\n') {
                page_start = offset + i as u64 + 1;
                next_offset = page_start.min(end - 1);
                data.drain(..=i);
            } else {
                data.clear();
            }
        }
        if before.is_some() && !data.ends_with(b"\n") {
            if let Some(i) = data.iter().rposition(|b| *b == b'\n') {
                data.truncate(i);
            } else {
                data.clear();
            }
        }
        let mut entries = Vec::new();
        let mut turns = Vec::new();
        let mut current = Value::Null;
        let mut turn_offsets = BTreeMap::new();
        let mut pos = page_start;
        for line in data.split_inclusive(|b| *b == b'\n') {
            let record_offset = pos;
            pos += line.len() as u64;
            let Ok(row) = serde_json::from_slice::<Value>(line) else {
                continue;
            };
            let p = &row["payload"];
            if row["type"] == "turn_context" {
                current = p["turn_id"].clone();
                if truth(&current) && !turns.contains(&current) {
                    turns.push(current.clone());
                    turn_offsets.insert(text(&current).to_owned(), record_offset);
                }
            }
            if row["type"] == "response_item" && message(p).is_some() {
                entries.push(json!({"item":p,"turn_id":current,"timestamp":row["timestamp"]}));
            }
        }
        let allowed = if truth(anchor) {
            vec![anchor.clone()]
        } else {
            turns
                .iter()
                .skip(turns.len().saturating_sub(recent))
                .cloned()
                .collect()
        };
        entries.retain(|r| allowed.is_empty() || allowed.contains(&r["turn_id"]));
        if !truth(anchor) && turns.len() > recent {
            next_offset = turn_offsets[text(&turns[turns.len() - recent])];
        }
        Ok(context_result(
            tid,
            &entries,
            "CODEX_LOCAL_SESSION",
            budget,
            json!(allowed),
            Pagination {
                next: if next_offset > start {
                    cursor_encode(tid, host, "legacy", &json!(next_offset))
                } else {
                    Value::Null
                },
                incomplete: offset > start || turns.len() > recent,
            },
            json!({"indexed_source":"CODEX_STATE_THREADS_PRIMARY_KEY","source_bytes":end-offset,"scan_byte_budget":SCAN_BYTES,"body_byte_budget":budget}),
        ))
    }
    pub fn inventory(&self, a: &Value) -> Result<Value> {
        let p = self.root.join("state_5.sqlite");
        if !p.exists() {
            return Ok(json!([[], null, "NATIVE_INDEX_UNAVAILABLE"]));
        }
        let result = (|| -> Result<Value> {
            let scope = crate::node_scope::Scope::parse(&a["scope"])?;
            let c = readonly(&p, "NATIVE_INDEX_UNAVAILABLE", true)?;
            let cols = columns(&c, "threads")?;
            if !["id", "cwd", "updated_at", "rollout_path"]
                .iter()
                .all(|s| cols.iter().any(|c| c == s))
            {
                return Ok(json!([[], null, "NATIVE_SCHEMA_UNSUPPORTED"]));
            }
            let mut where_ = Vec::new();
            let mut args = Vec::new();
            if !scope.projects.iter().any(|s| s == "*") {
                if scope.projects.is_empty() {
                    return Ok(json!([[], null, "SCOPE_EMPTY"]));
                }
                where_.push(format!(
                    "cwd IN ({})",
                    vec!["?"; scope.projects.len()].join(",")
                ));
                args.extend(scope.projects.into_iter().map(Into::into));
            }
            let audit = a["audit"] == true;
            if truth(&a["after"]) {
                where_.push(
                    if audit {
                        "id>?"
                    } else {
                        "(updated_at,id)>(?,?)"
                    }
                    .into(),
                );
                if audit {
                    args.push(sql(&a["after"]));
                } else {
                    let after = a["after"]
                        .as_array()
                        .filter(|a| a.len() == 2)
                        .ok_or("INVALID_QUERY")?;
                    args.extend(after.iter().map(sql));
                }
            }
            let mut fields = vec![
                "id".into(),
                "cwd".into(),
                "rollout_path".into(),
                "updated_at".into(),
            ];
            for k in [
                "history_mode",
                "title",
                "name",
                "source",
                "created_at",
                "has_user_event",
                "archived",
            ] {
                if cols.iter().any(|s| s == k) {
                    fields.push(if matches!(k, "title" | "name") {
                        format!("substr({k},1,256) AS {k}")
                    } else {
                        k.into()
                    });
                }
            }
            args.push(16.into());
            let records = rows(
                &c,
                &format!(
                    "SELECT {} FROM threads{} ORDER BY {} LIMIT ?",
                    fields.join(","),
                    if where_.is_empty() {
                        String::new()
                    } else {
                        format!(" WHERE {}", where_.join(" AND "))
                    },
                    if audit { "id" } else { "updated_at,id" }
                ),
                &args,
                "NATIVE_INDEX_UNAVAILABLE",
            )?;
            let last = records
                .last()
                .map(|r| {
                    if audit {
                        r["id"].clone()
                    } else {
                        json!([r["updated_at"], r["id"]])
                    }
                })
                .unwrap_or(Value::Null);
            Ok(json!([records, last, "INDEXED"]))
        })();
        Ok(result.unwrap_or(json!([[], null, "NATIVE_INDEX_UNAVAILABLE"])))
    }
    pub fn native_turns(&self, a: &Value) -> Result<Value> {
        let m = &a["metadata"];
        let tid = text(&m["id"]);
        let mut state = a["state"].clone();
        let mut turns = Vec::new();
        let mut coverage = "INDEXED";
        if m["history_mode"] == "paginated" {
            let result = (|| -> Result<()> {
                let c = readonly(
                    &self.root.join("thread_history_1.sqlite"),
                    "NATIVE_HISTORY_UNAVAILABLE",
                    true,
                )?;
                let history_tid = self.projection(tid, m, &c)?;
                if state["history_thread_id"] != history_tid {
                    for k in ["history_before", "history_complete", "active_turns"] {
                        state.as_object_mut().ok_or("INVALID_QUERY")?.remove(k);
                    }
                    state["history_thread_id"] = json!(history_tid);
                }
                let cols = columns(&c, "thread_turns")?;
                let fields = format!(
                    "turn_id,rollout_ordinal,status,started_at,completed_at,{}",
                    if cols.iter().any(|s| s == "rollout_byte_offset") {
                        "rollout_byte_offset"
                    } else {
                        "NULL AS rollout_byte_offset"
                    }
                );
                let recent = rows(
                    &c,
                    &format!(
                        "SELECT {fields} FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 4"
                    ),
                    &[history_tid.clone().into()],
                    "NATIVE_HISTORY_UNAVAILABLE",
                )?;
                if state["history_complete"] == true {
                    state["history_before"] = json!(i64::MAX);
                }
                let older = rows(
                    &c,
                    &format!(
                        "SELECT {fields} FROM thread_turns WHERE thread_id=? AND rollout_ordinal<? ORDER BY rollout_ordinal DESC LIMIT 4"
                    ),
                    &[
                        history_tid.clone().into(),
                        sql(state.get("history_before").unwrap_or(&json!(i64::MAX))),
                    ],
                    "NATIVE_HISTORY_UNAVAILABLE",
                )?;
                let pending = state["active_turns"]
                    .as_array()
                    .cloned()
                    .unwrap_or_default()
                    .into_iter()
                    .take(64)
                    .collect::<Vec<_>>();
                let mut active = Vec::new();
                if !pending.is_empty() {
                    let mut args = vec![history_tid.into()];
                    args.extend(pending.iter().map(sql));
                    active = rows(
                        &c,
                        &format!(
                            "SELECT {fields} FROM thread_turns WHERE thread_id=? AND turn_id IN ({})",
                            vec!["?"; pending.len()].join(",")
                        ),
                        &args,
                        "NATIVE_HISTORY_UNAVAILABLE",
                    )?;
                }
                if let Some(last) = older.last() {
                    state["history_before"] = last["rollout_ordinal"].clone();
                }
                state["history_complete"] = json!(older.len() < 4);
                let mut selected: Vec<Value> = Vec::new();
                for row in recent.into_iter().chain(older).chain(active) {
                    if let Some(i) = selected.iter().position(|r| r["turn_id"] == row["turn_id"]) {
                        selected[i] = row;
                    } else {
                        selected.push(row);
                    }
                }
                state["active_turns"] = json!(
                    selected
                        .iter()
                        .filter(|r| normalize(&r["status"]) == "RUNNING")
                        .take(64)
                        .map(|r| &r["turn_id"])
                        .collect::<Vec<_>>()
                );
                for row in selected {
                    let context = self.context(
                        tid,
                        text(&a["node_id"]),
                        m,
                        ContextLimits {
                            recent: 1,
                            budget: 4500,
                        },
                        &row["turn_id"],
                        &Value::Null,
                    )?;
                    if truth(&context["last_user_intent"]) {
                        state["user_message_observed"] = json!(true);
                    }
                    let native = normalize(&row["status"]);
                    turns.push(json!({"turn_id":row["turn_id"],"ordinal":order(&row["started_at"],row["rollout_ordinal"].as_i64().unwrap_or(0)),"native_state":native,"started_at":row["started_at"],"completed_at":row["completed_at"],"summary":context["last_codex_result"],"progress":if native=="RUNNING"{context["last_codex_result"].clone()}else{Value::Null},"result_processing":"UNMANAGED","model":self.turn_model(tid,m,text(&row["turn_id"]),&row["rollout_byte_offset"])}));
                }
                if state["history_complete"] != true {
                    coverage = "HISTORY_BACKFILL";
                }
                Ok(())
            })();
            if result.is_err() {
                coverage = "NATIVE_HISTORY_UNAVAILABLE";
            }
        } else if m["history_mode"] != "canonical" {
            return self.incremental(a);
        }
        Ok(json!([turns, coverage, state]))
    }
    fn incremental(&self, a: &Value) -> Result<Value> {
        use std::os::unix::fs::MetadataExt;
        let m = &a["metadata"];
        let tid = text(&m["id"]);
        let mut state = a["state"].clone();
        let path = self.path(tid, m)?;
        if !path.exists() {
            return Ok(json!([[], "NATIVE_HISTORY_UNAVAILABLE", state]));
        }
        let stat = path.metadata().map_err(|_| "NATIVE_HISTORY_UNAVAILABLE")?;
        let fileid = json!([stat.dev(), stat.ino()]);
        let mut offset = state["offset"].as_u64().unwrap_or(0);
        let mut gap = None;
        if (!state["file_id"].is_null() && state["file_id"] != fileid) || offset > stat.len() {
            offset = 0;
            state
                .as_object_mut()
                .ok_or("INVALID_QUERY")?
                .remove("current");
            gap = Some("SOURCE_REBUILT");
        }
        let mut f = File::open(path).map_err(|_| "NATIVE_HISTORY_UNAVAILABLE")?;
        f.seek(SeekFrom::Start(offset))
            .map_err(|_| "NATIVE_HISTORY_UNAVAILABLE")?;
        let mut data = Vec::new();
        f.take(65536)
            .read_to_end(&mut data)
            .map_err(|_| "NATIVE_HISTORY_UNAVAILABLE")?;
        if data.is_empty() {
            return Ok(json!([
                if truth(&state["current"]) {
                    vec![state["current"].clone()]
                } else {
                    vec![]
                },
                "INDEXED",
                state
            ]));
        }
        let Some(end) = data.iter().rposition(|b| *b == b'\n') else {
            if data.len() == 65536 {
                state["offset"] = json!(offset + data.len() as u64);
                state["file_id"] = fileid;
                return Ok(json!([[], "OVERSIZED_RECORD_GAP", state]));
            }
            return Ok(json!([
                if truth(&state["current"]) {
                    vec![state["current"].clone()]
                } else {
                    vec![]
                },
                "PARTIAL_RECORD",
                state
            ]));
        };
        data.truncate(end + 1);
        let mut turns = Vec::new();
        let mut current = state["current"].clone();
        for line in data.split(|b| *b == b'\n').filter(|l| !l.is_empty()) {
            let Ok(row) = serde_json::from_slice::<Value>(line) else {
                gap = Some("MALFORMED_RECORD_GAP");
                continue;
            };
            let p = &row["payload"];
            if !p.is_object() {
                continue;
            }
            if row["type"] == "session_meta" && (p["id"] != tid || p["cwd"] != m["cwd"]) {
                return Err("NATIVE_IDENTITY_CONFLICT".into());
            }
            if row["type"] == "turn_context"
                || (row["type"] == "event_msg" && p["type"] == "task_started")
            {
                if truth(&current) {
                    upsert_turn(&mut turns, current.clone());
                }
                current = json!({"turn_id":p["turn_id"],"ordinal":order(&row["timestamp"],offset as i64),"native_state":"RUNNING","started_at":row["timestamp"],"result_processing":"UNMANAGED"});
                if row["type"] == "turn_context" {
                    current["model"] = model(&p["model"]);
                }
            }
            if !truth(&current) || !truth(&current["turn_id"]) {
                continue;
            }
            if row["type"] == "event_msg"
                && matches!(p["type"].as_str(), Some("task_complete" | "turn_aborted"))
            {
                if truth(&p["turn_id"]) && p["turn_id"] != current["turn_id"] {
                    continue;
                }
                current["native_state"] = json!(if p["type"] == "task_complete" {
                    "COMPLETED"
                } else {
                    "INTERRUPTED"
                });
                current["completed_at"] = row["timestamp"].clone();
                if p["type"] == "task_complete" && p["last_agent_message"].is_string() {
                    current["summary"] = crate::observation::clean(&p["last_agent_message"], 3000)?;
                }
            }
            if row["type"] == "response_item"
                && let Some((role, s)) = message(p)
            {
                if role == "user" {
                    state["user_message_observed"] = json!(true);
                } else {
                    current["summary"] = crate::observation::clean(&json!(s), 3000)?;
                    current["progress"] = crate::observation::clean(&json!(s), 1500)?;
                }
            }
            upsert_turn(&mut turns, current.clone());
        }
        state["offset"] = json!(offset + data.len() as u64);
        state["file_id"] = fileid;
        state["current"] = current;
        Ok(json!([
            turns,
            gap.unwrap_or(if offset + (data.len() as u64) < stat.len() {
                "HISTORY_BACKFILL"
            } else {
                "INDEXED"
            }),
            state
        ]))
    }
}
fn upsert_turn(rows: &mut Vec<Value>, value: Value) {
    if let Some(i) = rows.iter().position(|v| v["turn_id"] == value["turn_id"]) {
        rows[i] = value;
    } else {
        rows.push(value);
    }
}
pub fn order(v: &Value, fallback: i64) -> i64 {
    if let Some(n) = v.as_f64() {
        return (n * if n < 100000000000.0 { 1000.0 } else { 1.0 }) as i64;
    }
    chrono::DateTime::parse_from_rfc3339(text(v))
        .map(|t| t.timestamp_millis())
        .unwrap_or(fallback)
}
pub fn invoke(op: &str, a: &Value) -> Result<Value> {
    match op {
        "history.message" => {
            return Ok(message(&a["value"])
                .map(|(r, s)| json!([r, s]))
                .unwrap_or(Value::Null));
        }
        "history.model" => return Ok(model(&a["value"])),
        "history.normalize" => return Ok(json!(normalize(&a["value"]))),
        "history.cursor_encode" => {
            return Ok(cursor_encode(
                text(&a["tid"]),
                text(&a["host"]),
                text(&a["mode"]),
                &a["before"],
            ));
        }
        "history.cursor_decode" => {
            return cursor_decode(
                &a["cursor"],
                text(&a["tid"]),
                text(&a["host"]),
                text(&a["mode"]),
            )
            .map(|n| json!(n));
        }
        _ => {}
    }
    let h = History::new(a["root"].as_str().ok_or("NATIVE_INDEX_UNAVAILABLE")?);
    let tid = text(&a["tid"]);
    let m = &a["metadata"];
    match op {
        "history.metadata" => h.metadata(tid),
        "history.path" => h.path(tid, m).map(|p| json!(p)),
        "history.identity" => h.identity(tid, m),
        "history.pending" => h.pending(tid, m),
        "history.status" => Ok(h.status(tid, m)),
        "history.turn_model" => Ok(h.turn_model(tid, m, text(&a["turn_id"]), &a["offset"])),
        "history.projection" => {
            let c = readonly(
                &h.root.join("thread_history_1.sqlite"),
                "NATIVE_HISTORY_UNAVAILABLE",
                true,
            )?;
            h.projection(tid, m, &c).map(Value::from)
        }
        "history.context" => h.context(
            tid,
            text(&a["host"]),
            m,
            ContextLimits {
                recent: a["recent_turns"].as_u64().unwrap_or(0) as usize,
                budget: a["max_bytes"].as_u64().unwrap_or(0) as usize,
            },
            &a["anchor"],
            &a["cursor"],
        ),
        "history.inventory" => h.inventory(a),
        "history.native_turns" => h.native_turns(a),
        _ => Err("NATIVE_READ_OPERATION_UNKNOWN".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn source_bound_native_cursor_and_privacy_do_not_claim_execution() {
        let old = URL_SAFE_NO_PAD.encode(br#"[1, "exact-thread", "air", "paginated", 123]"#);
        assert_eq!(
            cursor_decode(&json!(old), "exact-thread", "air", "paginated").unwrap(),
            Some(123)
        );
        for (tid, host, mode) in [
            ("other", "air", "paginated"),
            ("exact-thread", "p620", "paginated"),
            ("exact-thread", "air", "legacy"),
        ] {
            assert_eq!(
                cursor_decode(&json!(old), tid, host, mode).unwrap_err(),
                "INVALID_CONTEXT_CURSOR"
            );
        }
        assert!(
            message(&json!({"type":"agentMessage","channel":"analysis","text":"private"}))
                .is_none()
        );
        let display = "\n    code\n\n| a | b |\npassword=fixture_secret\n";
        assert_eq!(
            message(&json!({"type":"agentMessage","text":display}))
                .unwrap()
                .1,
            display.replace("fixture_secret", "[REDACTED]")
        );
    }
}
