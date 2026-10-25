//! Typed business commands in the existing authority transaction. The API peer
//! cannot submit events, timestamps, outcome booleans, or Provider evidence.
use crate::{
    ProtocolRequest,
    authority_store::{PROTOCOL, Store, clock, commit_core, db, parse, random_id},
    execution_core as core,
};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{collections::BTreeMap, os::unix::fs::MetadataExt, path::Path, process::Command};

#[path = "business_handoff.rs"]
mod handoff;
#[path = "business_node.rs"]
mod node;
#[path = "business_targets.rs"]
mod targets;
#[path = "business_workspace.rs"]
pub mod workspace_observer;

type Result<T> = std::result::Result<T, String>;
// SQLite receipts contain several full row snapshots, including both terminal
// evidence and its continuation checkpoint. They are not IPC frames. Keep a
// separate bounded storage budget; public reads expose results, not this body.
const MAX_BUSINESS_RECEIPT_BYTES: usize = 8 * crate::MAX_FRAME_BYTES;
pub const SCHEMA: &str = "
CREATE TABLE business_config(id INTEGER PRIMARY KEY CHECK(id=1), policy TEXT NOT NULL);
CREATE TABLE business_rows(kind TEXT NOT NULL CHECK(kind IN ('prepared','task','execution')), id TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(kind,id));
CREATE TABLE business_commands(position INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, role TEXT NOT NULL, request_hash TEXT NOT NULL, previous TEXT NOT NULL, hash TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE business_acks(position INTEGER PRIMARY KEY REFERENCES business_commands(position), hash TEXT NOT NULL);
CREATE TRIGGER business_command_no_update BEFORE UPDATE ON business_commands BEGIN SELECT RAISE(ABORT,'immutable command'); END;
CREATE TRIGGER business_command_no_delete BEFORE DELETE ON business_commands BEGIN SELECT RAISE(ABORT,'immutable command'); END;
";

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Project {
    pub alias: String,
    pub host: String,
    pub cwd: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Workspace {
    pub host: String,
    pub root: String,
    pub allow_existing_projects: bool,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Policy {
    pub projects: Vec<Project>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub workspaces: Vec<Workspace>,
    pub endpoints: Vec<String>,
    /// When present, every host must have an explicit Provider route. No
    /// fallback from an unknown remote host to the writer's Desktop socket.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub provider_routes: BTreeMap<String, Vec<String>>,
    pub lease_ms: i64,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub host_operations: Vec<Value>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub derived_git_roots: Vec<String>,
}
impl Policy {
    fn endpoints_for(&self, host: &str) -> Result<&[String]> {
        if self.provider_routes.is_empty() {
            Ok(&self.endpoints)
        } else {
            self.provider_routes
                .get(host)
                .map(Vec::as_slice)
                .ok_or_else(|| "PROVIDER_ROUTE_REQUIRED".into())
        }
    }
    fn project(&self, alias: &str, host: &str) -> Result<Project> {
        if let Some(p) = self
            .projects
            .iter()
            .find(|p| p.host == host && p.alias.eq_ignore_ascii_case(alias))
        {
            return Ok(Project {
                alias: alias.into(),
                ..p.clone()
            });
        }
        // Match the existing DynamicProjectResolver: one existing project name
        // inside the configured workspace. This grants no filesystem rights.
        let valid = alias
            .as_bytes()
            .first()
            .is_some_and(u8::is_ascii_alphanumeric)
            && alias
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c));
        if !valid {
            return Err("PROJECT_SCOPE_DENIED".into());
        }
        let w = self
            .workspaces
            .iter()
            .find(|w| w.host == host && w.allow_existing_projects)
            .ok_or("PROJECT_SCOPE_DENIED")?;
        let root = Path::new(&w.root)
            .canonicalize()
            .map_err(|_| "PROJECT_SCOPE_DENIED")?;
        let cwd = root
            .join(alias)
            .canonicalize()
            .map_err(|_| "PROJECT_SCOPE_DENIED")?;
        if !cwd.starts_with(&root) || cwd == root || !cwd.is_dir() {
            return Err("PROJECT_SCOPE_DENIED".into());
        }
        Ok(Project {
            alias: alias.into(),
            host: host.into(),
            cwd: cwd.to_string_lossy().into_owned(),
        })
    }
}
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Role {
    Api,
    Transport,
}
impl Role {
    fn name(self) -> &'static str {
        if self == Self::Api {
            "API"
        } else {
            "TRANSPORT"
        }
    }
}

fn decode<T: for<'a> Deserialize<'a>>(v: &Value) -> Result<T> {
    serde_json::from_value(v.clone()).map_err(|_| "INVALID_BUSINESS_COMMAND".into())
}
fn string<'a>(v: &'a Value, k: &str) -> Result<&'a str> {
    v[k].as_str()
        .filter(|s| !s.is_empty() && s.len() <= 4096)
        .ok_or_else(|| format!("INVALID_{k}"))
}
fn row(c: &Connection, kind: &str, id: &str) -> Result<Value> {
    parse(
        c.query_row(
            "SELECT value FROM business_rows WHERE kind=? AND id=?",
            params![kind, id],
            |r| r.get(0),
        )
        .optional()
        .map_err(db)?
        .ok_or("NOT_FOUND")?,
    )
}
fn put(
    c: &Connection,
    kind: &str,
    id: &str,
    value: &Value,
    changes: &mut Vec<Value>,
) -> Result<()> {
    c.execute("INSERT INTO business_rows VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET value=excluded.value",params![kind,id,core::canonical(value)]).map_err(db)?;
    changes.push(json!({"kind":kind,"id":id,"value":value}));
    Ok(())
}
fn rows(c: &Connection, kind: &str) -> Result<Vec<Value>> {
    c.prepare("SELECT value FROM business_rows WHERE kind=? ORDER BY id")
        .map_err(db)?
        .query_map([kind], |r| r.get::<_, String>(0))
        .map_err(db)?
        .map(|r| parse(r.map_err(db)?))
        .collect()
}
fn git(host: &str, cwd: &str, args: &[&str]) -> Result<String> {
    if let Some(observed) = workspace_observer::read(host, "git", cwd, args)? {
        return Ok(observed["value"].as_str().unwrap().into());
    }
    // Read-only, bounded local identity query. Never interpret repository text as shell.
    let out = Command::new("/usr/bin/timeout")
        .args([
            "10",
            "/usr/bin/git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            &format!("safe.directory={cwd}"),
            "-C",
            cwd,
        ])
        .args(args)
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .env_remove("GIT_INDEX_FILE")
        .output()
        .map_err(|_| "WORKSPACE_IDENTITY_UNAVAILABLE")?;
    if !out.status.success() || out.stdout.len() > 65536 {
        return Err("WORKSPACE_IDENTITY_UNAVAILABLE".into());
    }
    String::from_utf8(out.stdout)
        .map(|s| s.trim().to_owned())
        .map_err(|_| "WORKSPACE_IDENTITY_UNAVAILABLE".into())
}
fn native_workspace(project: &Project, requested: &Value) -> Result<Value> {
    // Historical branch/HEAD are audit inputs, never native admission facts.
    let cwd = if requested.is_null() {
        project.cwd.as_str()
    } else {
        let fields = requested.as_object().ok_or("WORKSPACE_CONTRACT_INVALID")?;
        if fields
            .keys()
            .any(|k| !matches!(k.as_str(), "cwd" | "branch" | "head"))
            || fields.values().any(|v| !v.is_string())
        {
            return Err("WORKSPACE_CONTRACT_INVALID".into());
        }
        requested["cwd"]
            .as_str()
            .ok_or("WORKSPACE_CONTRACT_INVALID")?
    };
    if !Path::new(cwd).is_absolute() || cwd.contains('\0') || cwd.len() > 4096 {
        return Err("WORKSPACE_CONTRACT_INVALID".into());
    }
    Ok(json!({"cwd":cwd}))
}

fn repository(host: &str, cwd: &str) -> Result<String> {
    if let Some(observed) = workspace_observer::read(host, "repository", cwd, &[])? {
        return Ok(string(&observed, "repository_identity")?.into());
    }
    let common = git(
        host,
        cwd,
        &["rev-parse", "--path-format=absolute", "--git-common-dir"],
    )?;
    let metadata = std::fs::metadata(&common).map_err(|_| "WORKSPACE_IDENTITY_UNAVAILABLE")?;
    if !metadata.is_dir() {
        return Err("WORKSPACE_IDENTITY_UNAVAILABLE".into());
    }
    // Shared by linked worktrees, stable across a directory rename, and
    // different when an unrelated repository replaces a former checkout path.
    Ok(format!("git-common:{}:{}", metadata.dev(), metadata.ino()))
}

fn known_repository(c: &Connection, project: &Project, saved: &Value) -> Result<Value> {
    if saved.is_string() {
        return Ok(saved.clone());
    }
    let identities: std::collections::BTreeSet<String> = rows(c, "task")?
        .iter()
        .filter(|t| {
            t["project"]
                .as_str()
                .is_some_and(|alias| alias.eq_ignore_ascii_case(&project.alias))
                && t["host"] == project.host
        })
        .filter_map(|t| t["repository_identity"].as_str().map(str::to_owned))
        .collect();
    match identities.len() {
        1 => Ok(json!(identities.into_iter().next().unwrap())),
        0 => repository(&project.host, &project.cwd)
            .map(Value::from)
            .map_err(|_| "PROJECT_REPOSITORY_UNAVAILABLE".into()),
        _ => Err("PROJECT_REPOSITORY_AMBIGUOUS".into()),
    }
}

fn native_resolution(project: &Project, requested: &Value, identity: &Value) -> Result<Value> {
    let route = native_workspace(project, requested)?;
    if let Some(observed) =
        workspace_observer::read(&project.host, "workspace", string(&route, "cwd")?, &[])?
    {
        let expected = match identity.as_str() {
            Some(saved) => saved.to_owned(),
            None => repository(&project.host, &project.cwd)
                .map_err(|_| "PROJECT_REPOSITORY_UNAVAILABLE")?,
        };
        if observed["repository_identity"] != expected {
            return Err("WORKSPACE_REPOSITORY_MISMATCH".into());
        }
        return Ok(observed);
    }
    let cwd = Path::new(string(&route, "cwd")?)
        .canonicalize()
        .map_err(|_| "WORKSPACE_CWD_UNREADABLE")?;
    if !cwd.is_dir() || std::fs::read_dir(&cwd).is_err() {
        return Err("WORKSPACE_CWD_UNREADABLE".into());
    }
    let cwd = cwd.to_string_lossy().into_owned();
    let common = repository(&project.host, &cwd)?;
    // Once a task has a proven repository identity, a retired registration path
    // need not remain readable. Unknown identities still fail closed.
    let expected = match identity.as_str() {
        Some(s) => s.to_owned(),
        None => {
            repository(&project.host, &project.cwd).map_err(|_| "PROJECT_REPOSITORY_UNAVAILABLE")?
        }
    };
    if common != expected {
        return Err("WORKSPACE_REPOSITORY_MISMATCH".into());
    }
    Ok(json!({"cwd":cwd,"repository_identity":common,
        "repository_common_dir":git(&project.host, &cwd, &["rev-parse", "--path-format=absolute", "--git-common-dir"])?,
        "repository_root":git(&project.host, &cwd, &["rev-parse", "--show-toplevel"])?,
        "worktree":git(&project.host, &cwd, &["rev-parse", "--path-format=absolute", "--git-dir"])?,
        "branch":git(&project.host, &cwd, &["branch", "--show-current"])?,
        "head":git(&project.host, &cwd, &["rev-parse", "HEAD"])?}))
}

fn resolved_contract(resolution: &Value) -> Value {
    json!({"cwd":resolution["cwd"],"branch":resolution["branch"],"head":resolution["head"]})
}

fn thread_bound(task: &Value, thread: &Value) -> bool {
    task["thread_id"] == *thread
        || task["lineage"]
            .as_array()
            .into_iter()
            .flatten()
            .any(|h| h["source"]["thread_id"] == *thread || h["successor_thread_id"] == *thread)
}

fn utc() -> String {
    // Gregorian civil date from Unix days. Event timestamps are metadata; leases
    // and evidence freshness exclusively use BOOTTIME and the kernel boot ID.
    let t = rustix::time::clock_gettime(rustix::time::ClockId::Realtime);
    let z = t.tv_sec.div_euclid(86400) + 719468;
    let era = z.div_euclid(146097);
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let mut y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = mp + if mp < 10 { 3 } else { -9 };
    y += i64::from(m <= 2);
    let s = t.tv_sec.rem_euclid(86400);
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}.{:06}Z",
        s / 3600,
        s % 3600 / 60,
        s % 60,
        t.tv_nsec / 1000
    )
}
fn binding(e: &Value) -> Value {
    json!({"execution_id":e["execution_ref"],"attempt_id":e["attempt_id"],"provider_session_id":e["provider_session_id"],"operation_id":e["execution_ref"],"thread_id":e["thread_id"],"turn_id":e["turn_id"],"generation":1})
}
fn base_facts(e: &Value, now: i64) -> Value {
    json!({"binding":binding(e),"stored_owner":e["owner"],"presented_owner":e["owner"],"stored_versions":{"assignment":1,"allocation":1,"resource":e["epoch"]},"expected_versions":{"assignment":1,"allocation":1,"resource":e["epoch"]},"safety_pending":false,"allocation_state":"HELD","assignment_state":"ACTIVE","now":now,"expires_at":e["expires_ms"]})
}
fn event(
    e: &mut Value,
    object: &str,
    id: &str,
    action: &str,
    target: &str,
    actor: &str,
    facts: Value,
) -> Result<Value> {
    let version = e["versions"][object].as_i64().unwrap_or(0);
    let initial = core::contract()["objects"][object]["initial"].clone();
    let from = e["states"].get(object).cloned().unwrap_or(initial);
    let r = json!({"object":object,"id":id,"from":from,"to":target,"action":action,"actor":actor,"version":version,"next_version":version+1,"facts":facts});
    let verdict = core::transition(&r);
    if verdict != "APPLY" {
        return Err(format!("CORE_{verdict}:{object}:{action}"));
    }
    e["versions"][object] = json!(version + 1);
    e["states"][object] = json!(target);
    let eid = format!("{object}:{id}:{}", version + 1);
    let at = utc();
    Ok(
        json!({"event_id":eid,"family":"CLINX_EXECUTION_CORE_V1","contract_version":"1.0.0","schema_version":1,"aggregate_type":object,"aggregate_id":id,"sequence":version+1,"aggregate_version":version+1,"actor":actor,"causation_id":e["execution_ref"],"correlation_id":e["execution_ref"],"transaction_id":e["execution_ref"],"idempotency_scope":e["execution_ref"],"idempotency_key":eid,"payload_hash":core::digest(&r),"payload":r,"occurred_at":at,"recorded_at":at}),
    )
}
fn append(
    c: &Connection,
    authority: &str,
    e: &Value,
    op: &str,
    events: Vec<Value>,
    fault: &mut impl FnMut(&str) -> Result<()>,
) -> Result<Value> {
    let cmd = json!({"protocol_version":PROTOCOL,"request_id":op,"operation":"append","payload":{"events":events}});
    let req: ProtocolRequest = decode(&cmd)?;
    commit_core(
        c,
        authority,
        string(e, "resource")?,
        e["epoch"].as_i64().ok_or("INVALID_EPOCH")?,
        &req,
        &cmd,
        fault,
    )
}
fn terminal(e: &Value) -> bool {
    matches!(
        e["states"]["Execution"].as_str(),
        Some("SUCCEEDED" | "FAILED" | "BLOCKED" | "CANCELLED")
    )
}
fn public(e: &Value) -> Value {
    let mut result = json!({"execution_ref":e["execution_ref"],"task_ref":e["task_ref"],"prepared_execution_ref":e["prepared_execution_ref"],"thread_id":e["thread_id"],"native_turn_id":e["turn_id"],"status":e["states"]["Execution"],"dispatch_state":e["dispatch_state"],"resource_state":e["states"]["ResourceAllocation"],"execution_started":e["dispatch_state"]=="ACKED","delivery_certainty":if e["dispatch_state"]=="SENDING" {"UNKNOWN"} else if e["dispatch_state"]=="ACKED" {"CONFIRMED"} else {"NOT_DISPATCHED"},"cancel_requested":e["cancel_requested"],"execution_result":e["execution_result"],"business_outcome":"NOT_ASSERTED","authority":"RUST","read_only":true});
    for key in ["workspace_contract", "workspace_resolution"] {
        if let Some(value) = e.get(key) {
            result[key] = value.clone();
        }
    }
    result
}
// Inventory consumers need task identity and policy, not full native history.
// Keep the authoritative checkpoint/lineage in SQLite for exact continuation.
fn task_summary(task: &Value) -> Value {
    let mut summary = serde_json::Map::new();
    for key in [
        "id",
        "task_id",
        "project",
        "host",
        "title",
        "workspace_contract",
        "thread_id",
        "lifecycle",
        "version",
        "execution_policy",
        "policy_hash",
        "active_execution",
        "repository_identity",
        "workspace_resolution",
        "repository_origin",
        "provider_policy_hash",
    ] {
        if let Some(value) = task.get(key) {
            summary.insert(key.into(), value.clone());
        }
    }
    Value::Object(summary)
}

fn task_summary_page(values: Vec<Value>, cursor_key: &str) -> Result<Value> {
    let count = values.len();
    let mut tasks = Vec::new();
    // Leave room for the response envelope, cursor and the maximum request ID.
    let mut remaining = crate::MAX_FRAME_BYTES - 2048;
    for task in values {
        let summary = task_summary(&task);
        let size = serde_json::to_vec(&summary)
            .map_err(|e| e.to_string())?
            .len()
            + 1;
        if tasks.len() == 64 || size > remaining {
            if tasks.is_empty() {
                return Err("TASK_SUMMARY_CAPACITY_EXCEEDED".into());
            }
            break;
        }
        remaining -= size;
        tasks.push(summary);
    }
    let next = if tasks.len() < count {
        tasks.last().ok_or("INVALID_TASK_PAGE")?[cursor_key].clone()
    } else {
        Value::Null
    };
    Ok(json!({"tasks":tasks,"next_after":next,"read_only":true}))
}

#[cfg(test)]
mod task_read_tests {
    use super::*;

    #[test]
    fn byte_budget_counts_json_escaping_and_utf8_and_retains_cursor() {
        let title = "\"\\\n大".repeat(70000);
        let tasks = vec![
            json!({"task_id":"a","title":title}),
            json!({"task_id":"b","title":title}),
        ];
        let page = task_summary_page(tasks.clone(), "task_id").unwrap();
        assert_eq!(page["tasks"].as_array().unwrap().len(), 1);
        assert_eq!(page["next_after"], "a");
        assert_eq!(page["tasks"][0]["title"], tasks[0]["title"]);
        assert!(serde_json::to_vec(&page).unwrap().len() < crate::MAX_FRAME_BYTES - 1024);
        let last = task_summary_page(tasks[1..].to_vec(), "task_id").unwrap();
        assert_eq!(last["tasks"][0]["task_id"], "b");
        assert!(last["next_after"].is_null());
    }

    #[test]
    fn single_oversized_summary_is_an_explicit_error() {
        assert_eq!(
            task_summary_page(
                vec![json!({"task_id":"a","title":"x".repeat(crate::MAX_FRAME_BYTES)})],
                "task_id"
            )
            .unwrap_err(),
            "TASK_SUMMARY_CAPACITY_EXCEEDED"
        );
    }
}

fn worker_fence(c: &Connection, e: &Value, p: &Value, boot: &str) -> Result<()> {
    if p["ticket"] != e["ticket"] || p["epoch"] != e["epoch"] || e["ticket"].is_null() {
        return Err("FENCED".into());
    }
    let (b,x,s,current):(String,i64,String,i64)=c.query_row("SELECT l.boot,l.expires_ms,l.state,r.epoch FROM leases l JOIN resources r USING(resource) WHERE l.resource=? AND l.epoch=?",params![string(e,"resource")?,e["epoch"].as_i64()],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).map_err(db)?;
    if b != boot || clock() >= x || s != "ACTIVE" || Some(current) != e["epoch"].as_i64() {
        return Err("FENCED".into());
    }
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Prepare {
    prompt: String,
    approved: bool,
    project: String,
    host: String,
    task_ref: Option<String>,
    thread_id: Option<String>,
    title: String,
    workspace_contract: Option<Value>,
    model: Option<String>,
    reasoning_effort: Option<String>,
    execution_policy: Option<Value>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Start {
    prepared_execution_ref: String,
    approved: bool,
    expected_request_hash: Option<String>,
    expected_task_ref: Option<String>,
    native_cwd: Option<String>,
}

impl Store {
    pub fn configure_business(&self, policy: &Policy, initialize: bool) -> Result<()> {
        if policy.projects.is_empty()
            || policy.endpoints.is_empty()
            || policy.endpoints.iter().any(|p| !Path::new(p).is_absolute())
            || !(1..=120_000).contains(&policy.lease_ms)
        {
            return Err("INVALID_AUTHORITY_POLICY".into());
        }
        let mut aliases = std::collections::BTreeSet::new();
        let mut routed = std::collections::BTreeSet::new();
        for (host, endpoints) in &policy.provider_routes {
            if host.is_empty()
                || host == "*"
                || endpoints.is_empty()
                || endpoints
                    .iter()
                    .any(|e| !policy.endpoints.contains(e) || !routed.insert(e))
            {
                return Err("INVALID_PROVIDER_ROUTE".into());
            }
        }
        for p in &policy.projects {
            policy.endpoints_for(&p.host)?;
            if p.alias.is_empty()
                || p.host.is_empty()
                || !Path::new(&p.cwd).is_absolute()
                || !aliases.insert((&p.host, &p.alias))
            {
                return Err("INVALID_PROJECT_POLICY".into());
            }
        }
        let mut hosts = std::collections::BTreeSet::new();
        for w in &policy.workspaces {
            policy.endpoints_for(&w.host)?;
            if w.host.is_empty()
                || !hosts.insert(&w.host)
                || !Path::new(&w.root).is_absolute()
                || Path::new(&w.root).canonicalize().ok().as_deref() != Some(Path::new(&w.root))
                || !Path::new(&w.root).is_dir()
            {
                return Err("INVALID_WORKSPACE_POLICY".into());
            }
        }
        let data = core::canonical(&serde_json::to_value(policy).map_err(|_| "INVALID_POLICY")?);
        if initialize {
            self.conn
                .execute("INSERT INTO business_config VALUES(1,?)", [&data])
                .map_err(db)?;
        }
        let stored: String = self
            .conn
            .query_row("SELECT policy FROM business_config WHERE id=1", [], |r| {
                r.get(0)
            })
            .map_err(db)?;
        if stored != data {
            return Err("AUTHORITY_POLICY_MISMATCH".into());
        }
        self.verify_business()
    }
    pub fn verify_business(&self) -> Result<()> {
        let mut projection: BTreeMap<(String, String), Value> = BTreeMap::new();
        let mut previous = String::new();
        let mut pos = 0;
        let mut stmt=self.conn.prepare("SELECT position,id,role,request_hash,previous,hash,body FROM business_commands ORDER BY position").map_err(db)?;
        let mut rs = stmt.query([]).map_err(db)?;
        while let Some(r) = rs.next().map_err(db)? {
            pos += 1;
            let body = parse(r.get(6).map_err(db)?)?;
            let hash = core::digest(&body);
            if r.get::<_, i64>(0).map_err(db)? != pos
                || r.get::<_, String>(1).map_err(db)? != body["operation_id"]
                || r.get::<_, String>(2).map_err(db)? != body["role"]
                || r.get::<_, String>(3).map_err(db)? != body["request_hash"]
                || r.get::<_, String>(4).map_err(db)? != previous
                || body["previous"] != previous
                || body["position"] != pos
                || body["authority_id"] != self.identity
                || r.get::<_, String>(5).map_err(db)? != hash
            {
                return Err("CORRUPT_BUSINESS_CHAIN".into());
            }
            for change in body["changes"]
                .as_array()
                .ok_or("CORRUPT_BUSINESS_CHANGE")?
            {
                projection.insert(
                    (string(change, "kind")?.into(), string(change, "id")?.into()),
                    change["value"].clone(),
                );
            }
            previous = hash;
        }
        let mut actual = BTreeMap::new();
        for kind in ["task", "prepared", "execution"] {
            for value in rows(&self.conn, kind)? {
                actual.insert((kind.into(), string(&value, "id")?.into()), value);
            }
        }
        if projection != actual {
            return Err("CORRUPT_BUSINESS_PROJECTION".into());
        }
        for e in rows(&self.conn, "execution")? {
            // Store::open already replayed and verified every immutable core
            // stream against this snapshot before configuring the business API.
            // Compare domains to that verified projection; do not replay all
            // historical core commands a second time during startup.
            let snapshot = parse(
                self.conn
                    .query_row(
                        "SELECT snapshot FROM projections WHERE resource=? AND epoch=?",
                        params![
                            string(&e, "resource")?,
                            e["epoch"].as_i64().ok_or("INVALID_EPOCH")?
                        ],
                        |r| r.get(0),
                    )
                    .map_err(db)?,
            )?;
            for item in snapshot["projection"]
                .as_array()
                .ok_or("CORRUPT_CORE_PROJECTION")?
            {
                if item["lifecycle"]
                    != e["states"][item["object"].as_str().ok_or("CORRUPT_CORE_PROJECTION")?]
                {
                    return Err("CORE_BUSINESS_PROJECTION_MISMATCH".into());
                }
            }
        }
        Ok(())
    }
    pub fn business(
        &mut self,
        role: Role,
        policy: &Policy,
        operation: &str,
        id: &str,
        p: &Value,
    ) -> Result<Value> {
        self.business_at(role, policy, operation, id, p, |_| Ok(()))
    }
    pub fn business_at(
        &mut self,
        role: Role,
        policy: &Policy,
        operation: &str,
        id: &str,
        p: &Value,
        fault: impl FnMut(&str) -> Result<()>,
    ) -> Result<Value> {
        if operation == "node_request" {
            if role != Role::Api {
                return Err("NODE_GATEWAY_API_PEER_REQUIRED".into());
            }
            let request: node::Request = decode(p)?;
            request.validate()?;
            return self.business_scoped_at(
                role,
                policy,
                &request.operation,
                &request.scope.operation_id(&request.request_id)?,
                &request.payload,
                Some(&request.scope),
                fault,
            );
        }
        self.business_scoped_at(role, policy, operation, id, p, None, fault)
    }
    fn business_scoped_at(
        &mut self,
        role: Role,
        policy: &Policy,
        operation: &str,
        id: &str,
        p: &Value,
        scope: Option<&node::Scope>,
        mut fault: impl FnMut(&str) -> Result<()>,
    ) -> Result<Value> {
        self.check_identity()?;
        if id.is_empty() || id.len() > 128 {
            return Err("INVALID_OPERATION_ID".into());
        }
        let transport = transport_operation(operation);
        if transport && role != Role::Transport {
            return Err("TRANSPORT_PEER_REQUIRED".into());
        }
        if let Some(scope) = scope {
            scope.authorize(&self.conn, policy, operation, p)?;
            if let Some(result) = scope.read(&self.conn, policy, operation, p)? {
                return Ok(result);
            }
        }
        if matches!(
            operation,
            "get_status"
                | "get_prepared"
                | "list_tasks"
                | "read_receipt"
                | "work"
                | "outbox"
                | "host_rows"
                | "resume_dispatch"
                | "observation_inventory"
                | "observation_turns"
        ) {
            return self.business_read(role, policy, operation, p);
        }
        if operation == "effective_policy" && core::keys(p, "task_ref") {
            let task = row(&self.conn, "task", string(p, "task_ref")?)?;
            let mut allowed = vec![];
            let mut denied = vec![];
            for scope in task["execution_policy"]["operation_scopes"]
                .as_array()
                .into_iter()
                .flatten()
            {
                let single =
                    json!({"execution_surface":"HOST_EXECUTOR","operation_scopes":[scope]});
                match targets::validate(policy, Some(&task), &single) {
                    Ok(()) => allowed.push(scope.clone()),
                    Err(reason) => denied.push(json!({"scope":scope,"reason":reason})),
                }
            }
            return Ok(json!({"operation_scopes":allowed,"denied_scopes":denied,"task":task}));
        }
        if operation == "adoption_endpoints" && core::keys(p, "host") {
            return Ok(json!({"endpoints":policy.endpoints_for(string(p,"host")?)?}));
        }
        if operation == "adoption_endpoints" && core::keys(p, "") {
            return Ok(json!({"endpoints":policy.endpoints}));
        }
        if operation == "list_projects" && core::keys(p, "") {
            let mut projects = policy.projects.clone();
            // The writer has traversal access, not workspace directory-listing
            // privileges. Include exact routes already resolved by preparation;
            // do not expand filesystem access merely for project discovery.
            for prepared in rows(&self.conn, "prepared")? {
                let alias = string(&prepared, "project")?;
                let host = string(&prepared, "host")?;
                if !projects.iter().any(|p| p.host == host && p.alias == alias)
                    && let Ok(project) = policy.project(alias, host)
                {
                    projects.push(project);
                }
            }
            return Ok(json!({"projects":projects}));
        }
        let mut request = json!({"operation":operation,"payload":p});
        if let Some(scope) = scope {
            request["node_principal"] = scope.principal.clone();
        }
        let hash = core::digest(&request);
        let tx = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(db)?;
        if let Some(scope) = scope {
            scope.authorize(&tx, policy, operation, p)?;
        }
        if matches!(
            operation,
            "bind_thread"
                | "authorize_send"
                | "successor_begin"
                | "successor_take"
                | "successor_commit"
                | "successor_recover_empty"
        ) {
            worker_fence(
                &tx,
                &row(&tx, "execution", string(p, "execution_ref")?)?,
                p,
                &self.boot,
            )?;
        }
        if let Some((old, prior_role, body)) = tx
            .query_row(
                "SELECT request_hash,role,body FROM business_commands WHERE id=?",
                [id],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, String>(2)?,
                    ))
                },
            )
            .optional()
            .map_err(db)?
        {
            if old != hash || prior_role != role.name() {
                return Err("OPERATION_ID_CONFLICT".into());
            }
            // A receipt is never another send permit, including when the first ACK was lost.
            if matches!(
                operation,
                "take_dispatch"
                    | "authorize_send"
                    | "take_cancel"
                    | "host_take"
                    | "host_begin"
                    | "successor_take"
            ) {
                return Ok(json!({"verdict":"DUPLICATE","send_permitted":false,"operation_id":id}));
            }
            return Ok(parse(body)?["result"].clone());
        }
        let mut changes = vec![];
        let mut result = match operation {
            "host_receive"
            | "host_take"
            | "host_begin"
            | "host_complete"
            | "host_sent"
            | "host_failed"
            | "host_ack"
            | "host_reconcile_rejected" => {
                let eid = string(p, "execution_ref")?;
                let mut e = row(&tx, "execution", eid)?;
                if terminal(&e) {
                    return Err("ALREADY_TERMINAL".into());
                }
                let prepared = row(&tx, "prepared", string(&e, "prepared_execution_ref")?)?;
                if matches!(operation, "host_take" | "host_begin") {
                    let task = row(&tx, "task", string(&e, "task_ref")?)?;
                    targets::validate(policy, Some(&task), &prepared["execution_policy"])?;
                    if task["execution_policy"] != prepared["execution_policy"] {
                        return Err("PREPARED_POLICY_STALE".into());
                    }
                }
                if operation == "host_begin" {
                    for other in rows(&tx, "execution")? {
                        for delivery in other["host_deliveries"]
                            .as_object()
                            .into_iter()
                            .flat_map(|m| m.values())
                        {
                            if delivery["host_ref"] == p["host_ref"] && !p["host_ref"].is_null() {
                                return Err("HOST_EXECUTION_IDENTITY_CONFLICT".into());
                            }
                        }
                    }
                }
                let result = host_command(policy, &mut e, &prepared, operation, p)?;
                put(&tx, "execution", eid, &e, &mut changes)?;
                result
            }
            "successor_begin"
            | "successor_take"
            | "successor_commit"
            | "successor_recover_empty" => {
                handoff::command(&tx, policy, &self.boot, operation, p, &mut changes)?
            }
            "register_git_target" | "revoke_git_target" | "resolve_git_target" => {
                targets::command(&tx, policy, operation, p, &mut changes)?
            }
            "adopt" | "adoption_begin" | "adoption_commit" | "prepare_policy" | "apply_policy" => {
                control(
                    &tx,
                    policy,
                    operation,
                    p,
                    id,
                    &self.boot,
                    &hash,
                    &mut changes,
                )?
            }
            "prepare" => {
                let mut r: Prepare = decode(p)?;
                r.reasoning_effort = crate::provider::normalize_effort(&json!(r.reasoning_effort))?
                    .as_str()
                    .map(str::to_owned);
                if !r.approved
                    || r.prompt.trim().is_empty()
                    || r.prompt.len() > 262144
                    || r.title.is_empty()
                    || r.title.len() > 256
                {
                    return Err("INVALID_PREPARATION".into());
                }
                let project = policy.project(&r.project, &r.host)?;
                let task = r
                    .task_ref
                    .as_ref()
                    .map(|id| row(&tx, "task", id))
                    .transpose()?;
                if task
                    .as_ref()
                    .is_some_and(|t| t["project"] != r.project || t["host"] != r.host)
                {
                    return Err("TASK_ROUTE_MISMATCH".into());
                }
                let requested = r
                    .workspace_contract
                    .or_else(|| task.as_ref().map(|t| t["workspace_contract"].clone()))
                    .unwrap_or(Value::Null);
                let task_policy = task
                    .as_ref()
                    .and_then(|t| t.get("execution_policy"))
                    .cloned()
                    .unwrap_or_else(native_policy);
                let execution_policy = r.execution_policy.unwrap_or(task_policy.clone());
                targets::validate(policy, task.as_ref(), &execution_policy)?;
                if task.is_some() && execution_policy != task_policy {
                    return Err("POLICY_REAUTHORIZATION_REQUIRED".into());
                }
                if let Some(thread) = &r.thread_id {
                    if thread.is_empty()
                        || thread.len() > 4096
                        || task.as_ref().is_some_and(|t| t["thread_id"] != *thread)
                    {
                        return Err("TASK_ROUTE_MISMATCH".into());
                    }
                    if rows(&tx, "task")?.iter().any(|t| {
                        t["host"] == r.host
                            && thread_bound(t, &json!(thread))
                            && task
                                .as_ref()
                                .is_none_or(|selected| selected["id"] != t["id"])
                    }) {
                        return Err("THREAD_ALREADY_BOUND".into());
                    }
                }
                let saved_identity = task
                    .as_ref()
                    .map(|t| t["repository_identity"].clone())
                    .unwrap_or(Value::Null);
                let resolution = native_resolution(
                    &project,
                    &requested,
                    &known_repository(&tx, &project, &saved_identity)?,
                )?;
                let seal = resolved_contract(&resolution);
                let prepared = format!("prepared_{}", &random_id()?[..32]);
                let task_ref = r
                    .task_ref
                    .unwrap_or(format!("task_{}", &random_id()?[..32]));
                let v = json!({"id":prepared,"prepared_execution_ref":prepared,"task_ref":task_ref,"new_task":task.is_none(),"project":r.project,"host":r.host,"prompt":r.prompt,"title":r.title,"model":r.model,"reasoning_effort":r.reasoning_effort,"workspace_contract":seal,"workspace_resolution":resolution,"requested_workspace_contract":requested,"thread_id":r.thread_id,"status":"PREPARED","request_hash":hash,"execution_policy":execution_policy,"policy_hash":core::digest(&execution_policy)});
                put(&tx, "prepared", &prepared, &v, &mut changes)?;
                v
            }
            "start" => {
                let r: Start = decode(p)?;
                if !r.approved {
                    return Err("APPROVAL_REQUIRED".into());
                }
                let mut prepared = row(&tx, "prepared", &r.prepared_execution_ref)?;
                if r.expected_request_hash
                    .as_ref()
                    .is_some_and(|h| prepared["request_hash"] != *h)
                    || r.expected_task_ref
                        .as_ref()
                        .is_some_and(|t| prepared["task_ref"] != *t)
                {
                    return Err("PREPARATION_REVIEW_MISMATCH".into());
                }
                if let Some(existing) = prepared["execution_ref"].as_str() {
                    public(&row(&tx, "execution", existing)?)
                } else {
                    let project = policy
                        .project(string(&prepared, "project")?, string(&prepared, "host")?)?;
                    if let Some(cwd) = r.native_cwd {
                        prepared["workspace_contract"] = json!({"cwd":cwd});
                    }
                    let resolution = native_resolution(
                        &project,
                        &prepared["workspace_contract"],
                        &known_repository(
                            &tx,
                            &project,
                            &prepared["workspace_resolution"]["repository_identity"],
                        )?,
                    )?;
                    prepared["workspace_contract"] = resolved_contract(&resolution);
                    prepared["workspace_resolution"] = resolution;
                    let resource = core::digest(&json!([prepared["host"], prepared["task_ref"]]));
                    // Leases fence attempts of this task. A shared cwd is not
                    // an execution lock; independent tasks keep independent
                    // resources, including when another delivery is unknown.
                    let prior = tx
                        .query_row(
                            "SELECT epoch FROM resources WHERE resource=?",
                            [&resource],
                            |r| r.get::<_, i64>(0),
                        )
                        .optional()
                        .map_err(db)?
                        .unwrap_or(0);
                    if prior > 0
                        && tx
                            .query_row(
                                "SELECT state FROM leases WHERE resource=? AND epoch=?",
                                params![resource, prior],
                                |r| r.get::<_, String>(0),
                            )
                            .map_err(db)?
                            != "RELEASED"
                    {
                        return Err("RESOURCE_STILL_HELD".into());
                    }
                    let epoch = prior.checked_add(1).ok_or("EPOCH_EXHAUSTED")?;
                    let now = clock();
                    let expires = now + policy.lease_ms;
                    let eid = format!(
                        "exec_{}",
                        r.prepared_execution_ref
                            .strip_prefix("prepared_")
                            .ok_or("INVALID_PREPARED_ID")?
                    );
                    let aid = format!("attempt_{}", &random_id()?[..32]);
                    let owner = json!({"execution_id":eid,"attempt_id":aid,"assignment_id":format!("assignment_{eid}"),"worker_id":"native-transport","incarnation_id":self.identity,"resource_key":resource,"resource_epoch":epoch});
                    let task_id = string(&prepared, "task_ref")?.to_owned();
                    let mut task = if prepared["new_task"] == true {
                        json!({"id":task_id,"task_id":task_id,"project":prepared["project"],"host":prepared["host"],"title":prepared["title"],"workspace_contract":prepared["workspace_contract"],"thread_id":prepared["thread_id"],"lifecycle":"OPEN","version":0})
                    } else {
                        row(&tx, "task", &task_id)?
                    };
                    node::inherit(&prepared, &mut task);
                    if prepared["new_task"] != true
                        && prepared["thread_id"].is_string()
                        && prepared["thread_id"] != task["thread_id"]
                    {
                        return Err("TASK_ROUTE_MISMATCH".into());
                    }
                    if task["thread_id"].is_string()
                        && rows(&tx, "task")?.iter().any(|other| {
                            other["id"] != task["id"]
                                && other["host"] == task["host"]
                                && thread_bound(other, &task["thread_id"])
                        })
                    {
                        return Err("THREAD_ALREADY_BOUND".into());
                    }
                    if prepared["new_task"] == true
                        && prepared["execution_policy"]["execution_surface"] == "HOST_EXECUTOR"
                    {
                        task["repository_origin"] = git(
                            &project.host,
                            string(&prepared["workspace_contract"], "cwd")?,
                            &["remote", "get-url", "--all", "origin"],
                        )
                        .ok()
                        .filter(|s| !s.is_empty() && !s.contains('\n'))
                        .map(Value::from)
                        .unwrap_or(Value::Null);
                    }
                    if !prepared["execution_policy"].is_null() {
                        targets::validate(policy, Some(&task), &prepared["execution_policy"])?;
                        if prepared["new_task"] != true
                            && task["execution_policy"] != prepared["execution_policy"]
                        {
                            return Err("PREPARED_POLICY_STALE".into());
                        }
                        task["execution_policy"] = prepared["execution_policy"].clone();
                        task["policy_hash"] = prepared["policy_hash"].clone();
                    }
                    if task["active_execution"].is_string() {
                        return Err("TASK_STILL_HELD".into());
                    }
                    if task["lifecycle"] != "ACTIVE" {
                        let action = if task["lifecycle"] == "OPEN" {
                            "activate"
                        } else if task["lifecycle"] == "BLOCKED" {
                            "resume"
                        } else {
                            return Err("TASK_NOT_OPEN".into());
                        };
                        let tr = json!({"object":"Task","id":task_id,"from":task["lifecycle"],"to":"ACTIVE","action":action,"actor":"task_command","version":task["version"],"next_version":task["version"].as_i64().ok_or("CORRUPT_TASK")?+1,"facts":{}});
                        if core::transition(&tr) != "APPLY" {
                            return Err("TASK_TRANSITION_REJECTED".into());
                        }
                        task["lifecycle"] = json!("ACTIVE");
                        task["version"] = tr["next_version"].clone();
                        task["last_event"] = tr;
                    }
                    let mut e = json!({"id":eid,"execution_ref":eid,"task_ref":task_id,"prepared_execution_ref":r.prepared_execution_ref,"resource":resource,"epoch":epoch,"attempt_id":aid,"provider_session_id":format!("session_{eid}"),"owner_ref":self.identity,"owner":owner,"expires_ms":expires,"thread_id":task["thread_id"],"turn_id":null,"endpoint":null,"ticket":null,"dispatch_state":"READY","cancel_requested":false,"versions":{},"states":{},"host_deliveries":{},"created_at":utc()});
                    e["workspace_contract"] = prepared["workspace_contract"].clone();
                    e["workspace_resolution"] = prepared["workspace_resolution"].clone();
                    if handoff::needed(&task, &prepared) {
                        e["predecessor_thread_id"] = e["thread_id"].clone();
                        e["thread_id"] = Value::Null;
                    }
                    tx.execute("INSERT INTO resources VALUES(?,?) ON CONFLICT(resource) DO UPDATE SET epoch=excluded.epoch",params![resource,epoch]).map_err(db)?;
                    tx.execute(
                        "INSERT INTO leases VALUES(?,?,?,?,?,?,?,?,?,'ACTIVE')",
                        params![
                            resource,
                            epoch,
                            id,
                            hash,
                            core::canonical(&owner),
                            random_id()?,
                            self.boot,
                            expires,
                            expires
                        ],
                    )
                    .map_err(db)?;
                    let f = base_facts(&e, now);
                    let mut grant = f.clone();
                    grant["previous_epoch"] = json!(prior);
                    grant["capacity_units"] = json!(1);
                    grant["used_units"] = json!(0);
                    grant["capacity"] = json!(1);
                    grant["unresolved_allocation_ids"] = json!([]);
                    let allocation = format!("allocation_{eid}");
                    e["allocation_id"] = json!(allocation);
                    let events = vec![
                        event(
                            &mut e,
                            "Execution",
                            &eid,
                            "admit",
                            "ADMITTED",
                            "command",
                            json!({"commit_bundle":["execution","request","prepared","owner","event","receipt","outbox"]}),
                        )?,
                        event(
                            &mut e,
                            "ResourceAllocation",
                            &allocation,
                            "grant",
                            "HELD",
                            "coordinator",
                            grant,
                        )?,
                        event(
                            &mut e,
                            "Attempt",
                            &aid,
                            "assign",
                            "ASSIGNED",
                            "coordinator",
                            f,
                        )?,
                    ];
                    append(&tx, &self.identity, &e, id, events, &mut fault)?;
                    prepared["status"] = json!("DISPATCHED");
                    prepared["execution_ref"] = json!(eid);
                    task["active_execution"] = json!(eid);
                    task["workspace_contract"] = prepared["workspace_contract"].clone();
                    if !prepared["workspace_resolution"].is_null() {
                        task["repository_identity"] =
                            prepared["workspace_resolution"]["repository_identity"].clone();
                        task["workspace_resolution"] = prepared["workspace_resolution"].clone();
                    }
                    put(
                        &tx,
                        "prepared",
                        &r.prepared_execution_ref,
                        &prepared,
                        &mut changes,
                    )?;
                    put(&tx, "task", &task_id, &task, &mut changes)?;
                    put(&tx, "execution", &eid, &e, &mut changes)?;
                    public(&e)
                }
            }
            "metadata_begin" | "metadata_commit" | "native_create_take" | "take_dispatch"
            | "bind_thread" | "authorize_send" | "observe_begin" | "observe_commit"
            | "input_begin" | "input_commit" | "cancel" | "take_cancel" | "fail_before_send" => {
                let eid = string(p, "execution_ref")?.to_owned();
                let mut e = row(&tx, "execution", &eid)?;
                let prepared_route = row(&tx, "prepared", string(&e, "prepared_execution_ref")?)?;
                let route_endpoints = policy.endpoints_for(string(&prepared_route, "host")?)?;
                let mut result;
                let mut events = vec![];
                match operation {
                    "take_dispatch" => {
                        if !core::keys(p, "execution_ref") {
                            return Err("INVALID_TAKE".into());
                        }
                        if e["dispatch_state"] != "READY" || e["cancel_requested"] == true {
                            return Err("DISPATCH_NOT_AVAILABLE".into());
                        }
                        e["ticket"] = json!(random_id()?);
                        let fence = json!({"ticket":e["ticket"],"epoch":e["epoch"]});
                        worker_fence(&tx, &e, &fence, &self.boot)?;
                        e["dispatch_state"] = json!("TAKEN");
                        let prepared = row(&tx, "prepared", string(&e, "prepared_execution_ref")?)?;
                        let task = row(&tx, "task", string(&e, "task_ref")?)?;
                        result = json!({"execution_ref":eid,"epoch":e["epoch"],"ticket":e["ticket"],"thread_id":e["thread_id"].as_str().map(Value::from).unwrap_or(e["predecessor_thread_id"].clone()),"prepared":prepared,"handoff":e["handoff"],"endpoints":route_endpoints,"successor_required":handoff::needed(&task, &prepared),"send_permitted":false});
                    }
                    "metadata_begin" => {
                        if !core::keys(p, "execution_ref") {
                            return Err("INVALID_METADATA_REQUEST".into());
                        }
                        let m = &e["metadata_publication"];
                        if m.is_null()
                            || matches!(
                                m["state"].as_str(),
                                Some("DELIVERED" | "SUPERSEDED" | "EXHAUSTED")
                            )
                            || m["next_ms"].as_i64().is_some_and(|at| at > clock())
                            || (m["boot"] == self.boot
                                && m["deadline"].as_i64().is_some_and(|at| at > clock()))
                        {
                            return Err("METADATA_NOT_AVAILABLE".into());
                        }
                        let attempts = m["attempts"].as_u64().ok_or("INVALID_METADATA_STATE")?;
                        if attempts >= 8 {
                            e["metadata_publication"]["state"] = json!("EXHAUSTED");
                            result = json!({"state":"EXHAUSTED"});
                        } else {
                            let nonce = random_id()?;
                            e["metadata_publication"]["attempts"] = json!(attempts + 1);
                            e["metadata_publication"]["nonce"] = json!(nonce);
                            e["metadata_publication"]["boot"] = json!(self.boot);
                            e["metadata_publication"]["deadline"] = json!(clock() + 30000);
                            result = json!({"execution_ref":eid,"nonce":nonce,"publication":e["metadata_publication"],"route":{"host":prepared_route["host"],"project":prepared_route["project"],"cwd":e["metadata_publication"]["cwd"],"node_principal":prepared_route["node_principal"]}});
                        }
                    }
                    "metadata_commit" => {
                        if !core::keys(p, "execution_ref nonce endpoint thread") {
                            return Err("INVALID_METADATA_EVIDENCE".into());
                        }
                        let m = &e["metadata_publication"];
                        if m.is_null()
                            || p["nonce"].is_null()
                            || m["nonce"] != p["nonce"]
                            || m["boot"] != self.boot
                            || m["deadline"].as_i64().is_none_or(|at| at <= clock())
                        {
                            return Err("METADATA_OBSERVATION_FENCED".into());
                        }
                        if p["endpoint"] != m["endpoint"]
                            || p["thread"]["id"] != m["thread_id"]
                            || p["thread"]["cwd"] != m["cwd"]
                            || !route_endpoints.iter().any(|v| p["endpoint"] == *v)
                        {
                            return Err("METADATA_IDENTITY_CONFLICT".into());
                        }
                        let state = if p["thread"]["name"] == m["title"] {
                            "DELIVERED"
                        } else if p["thread"]["name"].as_str().is_some() {
                            "SUPERSEDED"
                        } else {
                            "RETRY"
                        };
                        let attempts = m["attempts"].as_u64().ok_or("INVALID_METADATA_STATE")?;
                        e["metadata_publication"]["state"] = json!(state);
                        e["metadata_publication"]["nonce"] = Value::Null;
                        e["metadata_publication"]["deadline"] = Value::Null;
                        e["metadata_publication"]["next_ms"] =
                            json!(clock() + (1i64 << attempts.min(8)) * 1000);
                        result = json!({"state":state});
                    }
                    "native_create_take" => {
                        if !core::keys(p, "execution_ref epoch ticket") {
                            return Err("INVALID_NATIVE_CREATE".into());
                        }
                        worker_fence(&tx, &e, p, &self.boot)?;
                        if e["dispatch_state"] != "TAKEN"
                            || e["cancel_requested"] == true
                            || !e["thread_id"].is_null()
                            || !e["predecessor_thread_id"].is_null()
                            || !e["native_create_state"].is_null()
                            || !matches!(
                                prepared_route["execution_policy"]["execution_surface"].as_str(),
                                Some("CODEX_NATIVE" | "HOST_EXECUTOR")
                            )
                        {
                            return Err("NATIVE_CREATE_ACK_UNKNOWN".into());
                        }
                        e["native_create_state"] = json!("TAKEN");
                        result = json!({"create_permitted":true,"execution_ref":eid});
                    }
                    "bind_thread" => {
                        if !core::keys(p, "execution_ref epoch ticket endpoint thread_id cwd") {
                            return Err("INVALID_BINDING".into());
                        }
                        worker_fence(&tx, &e, p, &self.boot)?;
                        if e["dispatch_state"] != "TAKEN"
                            || (!e["thread_id"].is_null() && e["thread_id"] != p["thread_id"])
                            || !route_endpoints.iter().any(|x| p["endpoint"] == *x)
                        {
                            return Err("BINDING_CONFLICT".into());
                        }
                        string(p, "thread_id")?;
                        let prepared = row(&tx, "prepared", string(&e, "prepared_execution_ref")?)?;
                        let project = policy
                            .project(string(&prepared, "project")?, string(&prepared, "host")?)?;
                        let resolution = native_resolution(
                            &project,
                            &json!({"cwd":p["cwd"]}),
                            &known_repository(
                                &tx,
                                &project,
                                &prepared["workspace_resolution"]["repository_identity"],
                            )?,
                        )?;
                        e["workspace_contract"] = resolved_contract(&resolution);
                        e["workspace_resolution"] = resolution;
                        e["thread_id"] = p["thread_id"].clone();
                        e["endpoint"] = p["endpoint"].clone();
                        // Only a new, Authority-permitted thread gets a metadata
                        // outbox. Adoption/continuation must preserve user titles.
                        if e["native_create_state"] == "TAKEN"
                            && e["metadata_publication"].is_null()
                            && prepared["title"]
                                .as_str()
                                .is_some_and(|t| !t.is_empty() && t.chars().count() <= 200)
                        {
                            e["metadata_publication"] = json!({"state":"PENDING","attempts":0,
                                "thread_id":p["thread_id"],"endpoint":p["endpoint"],"cwd":p["cwd"],
                                "title":prepared["title"],"task_ref":e["task_ref"]});
                        }

                        let tid = string(&e, "task_ref")?;
                        let mut t = row(&tx, "task", tid)?;
                        if rows(&tx, "task")?.iter().any(|other| {
                            other["id"] != t["id"]
                                && other["host"] == t["host"]
                                && thread_bound(other, &e["thread_id"])
                        }) {
                            return Err("THREAD_ALREADY_BOUND".into());
                        }
                        if handoff::needed(&t, &prepared) {
                            return Err("EXACT_HOST_THREAD_HANDOFF_REQUIRED".into());
                        }
                        t["provider_policy_hash"] = prepared["policy_hash"].clone();
                        t["thread_id"] = e["thread_id"].clone();
                        if !e["workspace_resolution"].is_null() {
                            t["repository_identity"] =
                                e["workspace_resolution"]["repository_identity"].clone();
                            t["workspace_contract"] = e["workspace_contract"].clone();
                            t["workspace_resolution"] = e["workspace_resolution"].clone();
                        }
                        put(&tx, "task", tid, &t, &mut changes)?;
                        result = public(&e);
                    }
                    "authorize_send" => {
                        if !core::keys(p, "execution_ref epoch ticket") {
                            return Err("INVALID_SEND".into());
                        }
                        worker_fence(&tx, &e, p, &self.boot)?;
                        let prepared = row(&tx, "prepared", string(&e, "prepared_execution_ref")?)?;
                        let task = row(&tx, "task", string(&e, "task_ref")?)?;
                        targets::validate(policy, Some(&task), &prepared["execution_policy"])?;
                        if handoff::needed(&task, &prepared) {
                            return Err("EXACT_HOST_THREAD_HANDOFF_REQUIRED".into());
                        }
                        if e["dispatch_state"] != "TAKEN" || e["cancel_requested"] == true {
                            return Err("SEND_NOT_AVAILABLE".into());
                        }
                        string(&e, "thread_id")?;
                        string(&e, "endpoint")?;
                        let prepared = row(&tx, "prepared", string(&e, "prepared_execution_ref")?)?;
                        let project = policy
                            .project(string(&prepared, "project")?, string(&prepared, "host")?)?;
                        let resolution = native_resolution(
                            &project,
                            &e["workspace_contract"],
                            &known_repository(
                                &tx,
                                &project,
                                &e["workspace_resolution"]["repository_identity"],
                            )?,
                        )?;
                        e["workspace_contract"] = resolved_contract(&resolution);
                        e["workspace_resolution"] = resolution;
                        let mut f = base_facts(&e, clock());
                        f["dispatch_certainty"] = json!("NOT_DISPATCHED");
                        f["non_dispatch_proof"] = json!({"source":"dispatch_boundary","boundary":"PRE_SEND","evidence_ref":id,"execution_id":eid,"attempt_id":e["attempt_id"]});
                        events.push(event(
                            &mut e,
                            "Execution",
                            &eid,
                            "dispatch",
                            "RUNNING",
                            "worker",
                            f.clone(),
                        )?);
                        let aid = string(&e, "attempt_id")?.to_owned();
                        events.push(event(
                            &mut e,
                            "Attempt",
                            &aid,
                            "start",
                            "PROVIDER_STARTED",
                            "worker",
                            f,
                        )?);
                        e["dispatch_state"] = json!("SENDING");
                        result = json!({"send_permitted":true,"execution_ref":eid,"request_identity":eid});
                    }
                    "input_begin" => {
                        if !core::keys(p, "execution_ref") || e["dispatch_state"] != "SENDING" {
                            return Err("INPUT_RECOVERY_NOT_AVAILABLE".into());
                        }
                        let prepared = row(&tx, "prepared", string(&e, "prepared_execution_ref")?)?;
                        let nonce = random_id()?;
                        e["input_observation"] =
                            json!({"nonce":nonce,"boot":self.boot,"deadline":clock()+30000});
                        let actual = e
                            .get("workspace_contract")
                            .unwrap_or(&prepared["workspace_contract"]);
                        result = json!({"execution_ref":eid,"thread_id":e["thread_id"],"endpoint":e["endpoint"],"workspace_contract":actual,"prompt":prepared["prompt"],"client_message_id":native_client_id(string(&e,"thread_id")?,&eid),"nonce":nonce,"route":{"host":prepared["host"],"project":prepared["project"],"cwd":actual["cwd"],"node_principal":prepared["node_principal"]}});
                    }
                    "input_commit" => {
                        if !core::keys(p, "execution_ref nonce endpoint thread_id turn_id item") {
                            return Err("INVALID_NATIVE_INPUT_EVIDENCE".into());
                        }
                        if e["dispatch_state"] != "SENDING"
                            || e["input_observation"]["nonce"] != p["nonce"]
                            || p["nonce"].is_null()
                            || e["input_observation"]["boot"] != self.boot
                            || e["input_observation"]["deadline"]
                                .as_i64()
                                .is_none_or(|x| clock() >= x)
                        {
                            return Err("INPUT_OBSERVATION_FENCED".into());
                        }
                        let prepared = row(&tx, "prepared", string(&e, "prepared_execution_ref")?)?;
                        if e["thread_id"] != p["thread_id"]
                            || e["endpoint"] != p["endpoint"]
                            || p["item"]["type"] != "userMessage"
                            || p["item"]["clientId"]
                                != native_client_id(string(&e, "thread_id")?, &eid)
                            || p["item"]["content"].as_array().is_none_or(|items| {
                                items.len() != 1
                                    || items[0]["type"] != "text"
                                    || items[0]["text"] != prepared["prompt"]
                            })
                        {
                            return Err("NATIVE_INPUT_IDENTITY_CONFLICT".into());
                        }
                        string(&p["item"], "id")?;
                        string(p, "turn_id")?;
                        e["turn_id"] = p["turn_id"].clone();
                        e["dispatch_state"] = json!("ACKED");
                        e["ticket"] = Value::Null;
                        e["input_observation"] = Value::Null;
                        e["input_item_id"] = p["item"]["id"].clone();
                        e["native_input_evidence"] = p["item"].clone();
                        result = public(&e);
                    }
                    "observe_begin" => {
                        if !core::keys(p, "execution_ref") {
                            return Err("INVALID_OBSERVATION_REQUEST".into());
                        }
                        string(&e, "thread_id")?;
                        string(&e, "turn_id")?;
                        if terminal(&e) {
                            return Ok(public(&e));
                        }
                        let nonce = random_id()?;
                        e["observation"] =
                            json!({"nonce":nonce,"boot":self.boot,"deadline":clock()+30000});
                        result = json!({"nonce":nonce,"execution_ref":eid,"thread_id":e["thread_id"],"turn_id":e["turn_id"],"endpoints":route_endpoints,"route":{"host":prepared_route["host"],"project":prepared_route["project"],"cwd":e["workspace_contract"]["cwd"],"node_principal":prepared_route["node_principal"]}});
                    }
                    "observe_commit" => {
                        if !core::keys(p, "execution_ref nonce observations turn") {
                            return Err("INVALID_OBSERVATION".into());
                        }
                        if e["observation"]["nonce"] != p["nonce"]
                            || p["nonce"].is_null()
                            || e["observation"]["boot"] != self.boot
                            || e["observation"]["deadline"]
                                .as_i64()
                                .is_none_or(|x| clock() >= x)
                        {
                            return Err("OBSERVATION_FENCED".into());
                        }
                        if terminal(&e) {
                            return Err("ALREADY_TERMINAL".into());
                        }
                        let evidence = classify(route_endpoints, &e, p)?;
                        e["last_evidence"] = json!({"received_ms":clock(),"boot":self.boot,"request_hash":hash,"observations":p["observations"]});
                        e["observation"] = Value::Null;
                        if evidence == "TERMINAL" {
                            finalize(&tx, &mut e, p, id, &mut events, &mut changes)?;
                        } else if evidence == "UNKNOWN"
                            && e["states"]["Execution"] != "RECOVERY_REQUIRED"
                        {
                            events.push(event(
                                &mut e,
                                "Execution",
                                &eid,
                                "uncertain",
                                "RECOVERY_REQUIRED",
                                "owner",
                                json!({}),
                            )?);
                        } else if evidence == "LIVE"
                            && e["states"]["Execution"] == "RECOVERY_REQUIRED"
                        {
                            let f = observation_facts(&e, "LIVE");
                            events.push(event(
                                &mut e,
                                "Execution",
                                &eid,
                                "observe_live",
                                "RUNNING",
                                "reconciler",
                                f,
                            )?);
                        }
                        if evidence == "LIVE"
                            && clock() < e["expires_ms"].as_i64().ok_or("INVALID_LEASE")?
                        {
                            renew_observed(
                                &tx,
                                &self.identity,
                                &self.boot,
                                &mut e,
                                id,
                                policy.lease_ms,
                            )?;
                        }
                        result = public(&e);
                        result["provider_liveness"] = json!(evidence);
                    }
                    "cancel" => {
                        if !core::keys(p, "execution_ref approved") || p["approved"] != true {
                            return Err("APPROVAL_REQUIRED".into());
                        }
                        if !terminal(&e) {
                            e["cancel_requested"] = json!(true);
                        }
                        if matches!(e["dispatch_state"].as_str(), Some("READY" | "TAKEN")) {
                            close_before_send(
                                &tx,
                                &self.identity,
                                &mut e,
                                id,
                                "CANCELLED",
                                "USER_CANCELLED",
                                &mut changes,
                                &mut fault,
                            )?;
                        }
                        result = public(&e);
                    }
                    "fail_before_send" => {
                        if !core::keys(p, "execution_ref reason")
                            && !core::keys(p, "execution_ref reason provider_error")
                        {
                            return Err("INVALID_PRE_SEND_FAILURE".into());
                        }
                        let reason = string(p, "reason")?;
                        if !terminal(&e) {
                            close_before_send(
                                &tx,
                                &self.identity,
                                &mut e,
                                id,
                                "FAILED",
                                reason,
                                &mut changes,
                                &mut fault,
                            )?;
                            if let Some(detail) = p.get("provider_error") {
                                e["execution_result"]["provider_error"] =
                                    crate::provider::error_diagnostic(
                                        &detail.to_string(),
                                        "model/list",
                                        &Value::Null,
                                    );
                            }
                        }
                        result = public(&e);
                    }
                    "take_cancel" => {
                        if !core::keys(p, "execution_ref")
                            || e["cancel_requested"] != true
                            || e["cancel_taken"] == true
                            || terminal(&e)
                        {
                            return Err("CANCEL_NOT_AVAILABLE".into());
                        }
                        string(&e, "turn_id")?;
                        e["cancel_taken"] = json!(true);
                        result = json!({"send_permitted":true,"execution_ref":eid,"thread_id":e["thread_id"],"turn_id":e["turn_id"],"endpoint":e["endpoint"],"route":{"host":prepared_route["host"],"project":prepared_route["project"],"cwd":e["workspace_contract"]["cwd"],"node_principal":prepared_route["node_principal"]}});
                    }
                    _ => unreachable!(),
                }
                if !events.is_empty() {
                    append(&tx, &self.identity, &e, id, events, &mut fault)?;
                }
                put(&tx, "execution", &eid, &e, &mut changes)?;
                result
            }
            "ack_outbox" => {
                if !core::keys(p, "position hash") {
                    return Err("INVALID_OUTBOX_ACK".into());
                }
                let position = p["position"].as_i64().ok_or("INVALID_POSITION")?;
                let hash = string(p, "hash")?;
                let expected: String = tx
                    .query_row(
                        "SELECT hash FROM business_commands WHERE position=?",
                        [position],
                        |r| r.get(0),
                    )
                    .map_err(db)?;
                if expected != hash {
                    return Err("OUTBOX_ACK_CONFLICT".into());
                }
                tx.execute(
                    "INSERT OR IGNORE INTO business_acks VALUES(?,?)",
                    params![position, hash],
                )
                .map_err(db)?;
                json!({"acknowledged":true})
            }
            _ => return Err("UNSUPPORTED_BUSINESS_OPERATION".into()),
        };
        if let Some(scope) = scope {
            if matches!(operation, "prepare" | "adopt") && result["id"].is_string() {
                result["node_principal"] = scope.principal.clone();
                put(
                    &tx,
                    "prepared",
                    string(&result, "id")?,
                    &result,
                    &mut changes,
                )?;
            }
            for change in &changes {
                if !change["value"]["node_principal"].is_null() {
                    scope.owned(&change["value"])?;
                }
            }
        }
        let (position, previous) = tx
            .query_row(
                "SELECT position,hash FROM business_commands ORDER BY position DESC LIMIT 1",
                [],
                |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)),
            )
            .optional()
            .map_err(db)?
            .unwrap_or((0, String::new()));
        let mut body = json!({"authority_id":self.identity,"position":position+1,"previous":previous,"operation_id":id,"operation":operation,"role":role.name(),"request_hash":hash,"changes":changes,"result":result});
        if let Some(scope) = scope {
            body["node_principal"] = scope.principal.clone();
            body["node_request"] = request;
        }
        let digest = core::digest(&body);
        if core::canonical(&body).len() > MAX_BUSINESS_RECEIPT_BYTES {
            return Err("BUSINESS_RECEIPT_CAPACITY_EXCEEDED".into());
        }
        tx.execute(
            "INSERT INTO business_commands VALUES(?,?,?,?,?,?,?)",
            params![
                position + 1,
                id,
                role.name(),
                hash,
                previous,
                digest,
                core::canonical(&body)
            ],
        )
        .map_err(db)?;
        fault("BEFORE_BUSINESS_COMMIT")?;
        tx.commit().map_err(db)?;
        fault("AFTER_BUSINESS_COMMIT")?;
        Ok(result)
    }
    fn business_read(&self, role: Role, policy: &Policy, op: &str, p: &Value) -> Result<Value> {
        match op {
            "get_prepared" if core::keys(p, "prepared_execution_ref") => {
                row(&self.conn, "prepared", string(p, "prepared_execution_ref")?)
            }
            "get_status" if core::keys(p, "execution_ref") => Ok(public(&row(
                &self.conn,
                "execution",
                string(p, "execution_ref")?,
            )?)),
            "observation_inventory" if core::keys(p, "host after") => {
                let host = string(p, "host")?;
                let after = p["after"].as_str().ok_or("INVALID_CURSOR")?;
                let mut stmt=self.conn.prepare("SELECT value FROM business_rows WHERE kind='task' AND json_extract(value,'$.host')=? AND json_extract(value,'$.thread_id')>? ORDER BY json_extract(value,'$.thread_id') LIMIT 64").map_err(db)?;
                let values = stmt
                    .query_map(params![host, after], |r| r.get::<_, String>(0))
                    .map_err(db)?
                    .map(|r| parse(r.map_err(db)?))
                    .collect::<Result<Vec<Value>>>()?;
                task_summary_page(values, "thread_id")
            }
            "observation_turns" if core::keys(p, "host thread_id before") => {
                let host = string(p, "host")?;
                if p.get("exact_turn_id").is_some_and(|v| !v.is_null()) {
                    string(p, "exact_turn_id")?;
                }
                let thread = string(p, "thread_id")?;
                let before = p["before"].as_str().ok_or("INVALID_CURSOR")?;
                let mut stmt=self.conn.prepare("SELECT e.value FROM business_rows e JOIN business_rows t ON t.kind='task' AND t.id=json_extract(e.value,'$.task_ref') WHERE e.kind='execution' AND json_extract(t.value,'$.host')=? AND json_extract(e.value,'$.thread_id')=? AND e.id<? AND json_extract(e.value,'$.turn_id') IS NOT NULL ORDER BY e.id DESC LIMIT 16").map_err(db)?;
                let values=stmt.query_map(params![host,thread,before],|r|r.get::<_,String>(0)).map_err(db)?
                    .map(|r|{let e=parse(r.map_err(db)?)?; Ok(json!({"id":e["id"],"execution_ref":e["execution_ref"],"task_ref":e["task_ref"],"turn_id":e["turn_id"],"status":e["states"]["Execution"],"created_at":e["created_at"],"result":e["execution_result"]}))}).collect::<Result<Vec<Value>>>()?;
                Ok(json!({"executions":values,"read_only":true}))
            }
            "resume_dispatch" if core::keys(p, "execution_ref") => {
                let e = row(&self.conn, "execution", string(p, "execution_ref")?)?;
                let scope = json!({"epoch":e["epoch"],"ticket":e["ticket"]});
                worker_fence(&self.conn, &e, &scope, &self.boot)?;
                if e["dispatch_state"] != "TAKEN" || e["cancel_requested"] == true {
                    return Err("DISPATCH_NOT_AVAILABLE".into());
                }
                let prepared = row(
                    &self.conn,
                    "prepared",
                    string(&e, "prepared_execution_ref")?,
                )?;
                let task = row(&self.conn, "task", string(&e, "task_ref")?)?;
                Ok(
                    json!({"execution_ref":e["execution_ref"],"epoch":e["epoch"],"ticket":e["ticket"],
                    "thread_id":e["thread_id"].as_str().map(Value::from).unwrap_or(e["predecessor_thread_id"].clone()),"prepared":prepared,"handoff":e["handoff"],
                    "endpoints":policy.endpoints_for(string(&prepared,"host")?)?,"successor_required":handoff::needed(&task,&prepared),"send_permitted":false}),
                )
            }
            "host_rows" if core::keys(p, "execution_ref") => {
                let e = row(&self.conn, "execution", string(p, "execution_ref")?)?;
                Ok(
                    json!({"rows":e["host_deliveries"].as_object().map(|m|m.values().cloned().collect::<Vec<_>>()).unwrap_or_default()}),
                )
            }
            "list_tasks" if core::keys(p, "projection after") && p["projection"] == "summary" => {
                let after = p["after"].as_str().ok_or("INVALID_CURSOR")?;
                let mut stmt = self.conn.prepare("SELECT value FROM business_rows WHERE kind='task' AND id>? ORDER BY id LIMIT 65").map_err(db)?;
                let values = stmt
                    .query_map([after], |r| r.get::<_, String>(0))
                    .map_err(db)?
                    .map(|r| parse(r.map_err(db)?))
                    .collect::<Result<Vec<Value>>>()?;
                task_summary_page(values, "task_id")
            }
            "list_tasks" if core::keys(p, "") => {
                Ok(json!({"tasks":rows(&self.conn,"task")?,"read_only":true}))
            }
            "work" if core::keys(p, "") => Ok(
                json!({"executions":rows(&self.conn,"execution")?.iter().filter(|e|!terminal(e)).take(100).map(public).collect::<Vec<_>>(),
                    "metadata":rows(&self.conn,"execution")?.iter().filter(|e| {
                        let m = &e["metadata_publication"];
                        !m.is_null() && !matches!(m["state"].as_str(),Some("DELIVERED"|"SUPERSEDED"|"EXHAUSTED"))
                            && m["next_ms"].as_i64().is_none_or(|at|at<=clock())
                            && (m["boot"] != self.boot || m["deadline"].as_i64().is_none_or(|at|at<=clock()))
                    }).take(100).map(|e| json!({"execution_ref":e["execution_ref"]})).collect::<Vec<_>>(),
                    "adoptions":rows(&self.conn,"prepared")?.iter().filter(|p|p["kind"]=="ADOPTION" && p["status"]=="PENDING").take(100).cloned().collect::<Vec<_>>()}),
            ),
            "read_receipt" if core::keys(p, "operation_id") => {
                let r = self
                    .conn
                    .query_row(
                        "SELECT body FROM business_commands WHERE id=? AND role=?",
                        params![string(p, "operation_id")?, role.name()],
                        |r| r.get::<_, String>(0),
                    )
                    .optional()
                    .map_err(db)?;
                Ok(match r {
                    Some(r) => {
                        let body = parse(r)?;
                        json!({"verdict":"COMMITTED","operation_id":body["operation_id"],"hash":core::digest(&body),"result":body["result"]})
                    }
                    None => json!({"verdict":"NOT_FOUND"}),
                })
            }
            "outbox" if core::keys(p, "after_position") => {
                let after = p["after_position"]
                    .as_i64()
                    .filter(|v| *v >= 0)
                    .ok_or("INVALID_CURSOR")?;
                let mut stmt=self.conn.prepare("SELECT c.position,c.hash,c.body FROM business_commands c LEFT JOIN business_acks a USING(position) WHERE c.position>? AND a.position IS NULL AND json_extract(c.body,'$.operation')!='ack_outbox' ORDER BY c.position LIMIT 1").map_err(db)?;
                let rs = stmt
                    .query_map([after], |r| {
                        Ok((
                            r.get::<_, i64>(0)?,
                            r.get::<_, String>(1)?,
                            r.get::<_, String>(2)?,
                        ))
                    })
                    .map_err(db)?;
                let mut out = vec![];
                for r in rs {
                    let (pos, hash, body) = r.map_err(db)?;
                    let b = parse(body)?;
                    out.push(json!({"position":pos,"hash":hash,"operation_id":b["operation_id"],"operation":b["operation"],"result":b["result"]}));
                }
                Ok(json!({"receipts":out}))
            }
            _ => Err("INVALID_BUSINESS_READ".into()),
        }
    }
}
fn normalized(v: &Value) -> String {
    v.as_str()
        .unwrap_or("")
        .replace(['_', '-'], "")
        .to_ascii_lowercase()
}
fn classify(endpoints: &[String], e: &Value, p: &Value) -> Result<&'static str> {
    let rs = p["observations"].as_array().ok_or("INVALID_OBSERVATIONS")?;
    if rs.len() != endpoints.len() {
        return Err("OBSERVATION_ENDPOINT_SET_MISMATCH".into());
    }
    let mut seen = std::collections::BTreeSet::new();
    let mut unknown = false;
    let mut active = 0;
    let mut exact_live = 0;
    let mut terminals = 0;
    for r in rs {
        if !core::keys(r, "endpoint state thread_id turn_id turn_status")
            || !endpoints.iter().any(|x| r["endpoint"] == *x)
            || !seen.insert(string(r, "endpoint")?)
        {
            return Err("OBSERVATION_ENDPOINT_SET_MISMATCH".into());
        }
        let state = normalized(&r["state"]);
        let status = normalized(&r["turn_status"]);
        let exact = r["thread_id"] == e["thread_id"] && r["turn_id"] == e["turn_id"];
        let live = matches!(state.as_str(), "active" | "running" | "inprogress");
        active += usize::from(live);
        exact_live += usize::from(
            live && exact && matches!(status.as_str(), "active" | "running" | "inprogress"),
        );
        terminals += usize::from(
            exact
                && ((state == "idle"
                    && matches!(
                        status.as_str(),
                        "completed" | "failed" | "error" | "cancelled" | "interrupted"
                    ))
                    || (matches!(state.as_str(), "notloaded" | "unloaded")
                        && matches!(
                            status.as_str(),
                            "completed" | "failed" | "error" | "cancelled" | "interrupted"
                        ))),
        );
        unknown |= matches!(state.as_str(), "unknown" | "offline" | "");
    }
    if active == 1 && exact_live == 1 {
        return Ok("LIVE");
    }
    if !unknown
        && active == 0
        && terminals == rs.len()
        && p["turn"]["id"] == e["turn_id"]
        && rs
            .iter()
            .all(|r| normalized(&r["turn_status"]) == normalized(&p["turn"]["status"]))
    {
        return Ok("TERMINAL");
    }
    Ok("UNKNOWN")
}
fn observation_facts(e: &Value, liveness: &str) -> Value {
    let now = clock();
    json!({"binding":binding(e),"now":now,"observation":{"binding":binding(e),"observed_at":now,"liveness":liveness,"complete":true,"active_claims":if liveness=="LIVE"{1}else{0}}})
}
fn finalize(
    c: &Connection,
    e: &mut Value,
    p: &Value,
    id: &str,
    events: &mut Vec<Value>,
    changes: &mut Vec<Value>,
) -> Result<()> {
    let eid = string(e, "execution_ref")?.to_owned();
    let aid = string(e, "attempt_id")?.to_owned();
    let allocation = string(e, "allocation_id")?.to_owned();
    let status = normalized(&p["turn"]["status"]);
    let host_rejected = e["host_deliveries"].as_object().is_some_and(|rows| {
        rows.values()
            .any(|row| row["terminal_rejection_reconciled"] == true)
    });
    let outcome = match status.as_str() {
        "completed" if host_rejected => "FAILED",
        "completed" => "SUCCEEDED",
        "failed" | "error" => "FAILED",
        "cancelled" | "interrupted" if e["cancel_requested"] == true => "CANCELLED",
        "cancelled" | "interrupted" => "FAILED",
        _ => return Err("TERMINAL_STATUS_UNPROVEN".into()),
    };
    let items = p["turn"]["items"].as_array().ok_or("RESULT_NOT_LOADED")?;
    let texts: Vec<_> = items
        .iter()
        .filter(|i| i["type"] == "agentMessage" && i["phase"] != "commentary")
        .map(|i| {
            i["text"]
                .as_str()
                .filter(|s| s.len() <= 524288)
                .ok_or_else(|| "INVALID_RESULT_TEXT".to_owned())
        })
        .collect::<Result<_>>()?;
    let response = texts.join("\n\n");
    if outcome == "SUCCEEDED" && response.is_empty() {
        return Err("RESULT_NOT_LOADED".into());
    }
    let deliveries = host_finalization(e, items)?;
    let mut f = observation_facts(e, "TERMINAL");
    f["deliveries"] = json!(deliveries);
    f["delivery_ledger_complete"] = json!(true);
    let action = if e["states"]["Execution"] == "RECOVERY_REQUIRED" {
        "resume_finalizer"
    } else {
        "begin_finalizer"
    };
    events.push(event(
        e,
        "Execution",
        &eid,
        action,
        "FINALIZING",
        "finalizer",
        f.clone(),
    )?);
    // A turn may complete before the first live observation. Preserve the same
    // Attempt through LOST -> OBSERVING; never manufacture a live observation.
    events.push(event(
        e,
        "Attempt",
        &aid,
        "lost",
        "LOST",
        "coordinator",
        json!({}),
    )?);
    events.push(event(
        e,
        "Attempt",
        &aid,
        "observe_same",
        "OBSERVING",
        "reconciler",
        json!({"binding":binding(e)}),
    )?);
    events.push(event(
        e,
        "Attempt",
        &aid,
        "finish",
        "TERMINAL",
        "finalizer",
        f.clone(),
    )?);
    f["result"] = json!({"execution_id":eid,"attempt_id":aid,"outcome":outcome,"result_ref":core::digest(&p["turn"])});
    f["committed_outcome"] = json!(outcome);
    f["cancel_requested"] = e["cancel_requested"].clone();
    f["finalization_phase"] = json!("TERMINAL_COMMITTED");
    f["commit_bundle"] = json!(["result", "terminal", "event", "receipt", "outbox"]);
    events.push(event(
        e,
        "Execution",
        &eid,
        &format!("commit_{}", outcome.to_ascii_lowercase()),
        outcome,
        "finalizer",
        f.clone(),
    )?);
    f["lease_execution_id"] = json!(eid);
    f["lease_attempt_id"] = json!(aid);
    f["old_writer_stopped_ref"] = json!(format!("exact-provider-terminal:{id}"));
    f["side_effect_fenced_ref"] = json!(format!("consumed-dispatch:{eid}"));
    events.push(event(
        e,
        "ResourceAllocation",
        &allocation,
        "release",
        "RELEASE_PENDING",
        "finalizer",
        f.clone(),
    )?);
    events.push(event(
        e,
        "ResourceAllocation",
        &allocation,
        "commit_release",
        "RELEASED",
        "coordinator",
        f,
    )?);
    e["terminal_turn"] = p["turn"].clone();
    e["execution_result"] =
        json!({"response_text":response,"outcome":outcome,"result_ref":core::digest(&p["turn"])});
    if outcome == "FAILED" && p["turn"]["error"].is_object() {
        e["execution_result"]["provider_error"] = crate::provider::error_diagnostic(
            "PROVIDER_TURN_FAILED",
            "turn/terminal",
            &p["turn"]["error"],
        );
    }
    e["ticket"] = Value::Null;
    let tid = string(e, "task_ref")?;
    let mut t = row(c, "task", tid)?;
    t["native_checkpoint"] = json!({"endpoint":e["endpoint"],"thread_id":e["thread_id"],"turn":p["turn"],"execution_ref":e["execution_ref"]});
    t["active_execution"] = Value::Null;
    // The user's task can continue after a completed native turn. Business task
    // completion is not inferred from Provider completion.
    put(c, "task", tid, &t, changes)?;
    Ok(())
}

// No Provider evidence is needed before the durable send boundary. This is a
// separate, versioned production command: frozen V1 events are never fabricated.
#[allow(clippy::too_many_arguments)]
fn close_before_send(
    c: &Connection,
    authority: &str,
    e: &mut Value,
    id: &str,
    outcome: &str,
    reason: &str,
    changes: &mut Vec<Value>,
    fault: &mut impl FnMut(&str) -> Result<()>,
) -> Result<()> {
    if !matches!(e["dispatch_state"].as_str(), Some("READY" | "TAKEN"))
        || e["states"]["Execution"] != "ADMITTED"
        || !e["turn_id"].is_null()
    {
        return Err("SEND_MAY_HAVE_OCCURRED".into());
    }
    let payload = json!({"contract":"CLINX_PRE_SEND_CLOSURE_V1","execution_id":e["execution_ref"],
        "attempt_id":e["attempt_id"],"allocation_id":e["allocation_id"],"outcome":outcome,
        "reason":reason,"authority_id":authority,"command_id":id,"cancel_requested":e["cancel_requested"]});
    let cmd = json!({"protocol_version":PROTOCOL,"request_id":id,"operation":"finalize_before_send","payload":payload});
    let req: ProtocolRequest = decode(&cmd)?;
    commit_core(
        c,
        authority,
        string(e, "resource")?,
        e["epoch"].as_i64().ok_or("INVALID_EPOCH")?,
        &req,
        &cmd,
        fault,
    )?;
    e["states"]["Execution"] = json!(outcome);
    e["states"]["Attempt"] = json!("TERMINAL");
    e["states"]["ResourceAllocation"] = json!("RELEASED");
    for object in ["Execution", "Attempt", "ResourceAllocation"] {
        e["versions"][object] = json!(2);
    }
    e["ticket"] = Value::Null;
    e["dispatch_state"] = json!("NOT_DISPATCHED");
    e["execution_result"] = json!({"outcome":outcome,"reason":reason,"response_text":"","result_ref":core::digest(&payload),"contract":"CLINX_PRE_SEND_CLOSURE_V1"});
    let task_id = string(e, "task_ref")?;
    let mut task = row(c, "task", task_id)?;
    if task["active_execution"] != e["execution_ref"] {
        return Err("TASK_OWNER_CONFLICT".into());
    }
    task["active_execution"] = Value::Null;
    put(c, "task", task_id, &task, changes)
}

pub fn native_client_id(thread: &str, execution: &str) -> String {
    use sha1::{Digest, Sha1};
    let namespace: [u8; 16] = [
        0x6b, 0xa7, 0xb8, 0x11, 0x9d, 0xad, 0x11, 0xd1, 0x80, 0xb4, 0x00, 0xc0, 0x4f, 0xd4, 0x30,
        0xc8,
    ];
    let mut hash = Sha1::new();
    hash.update(namespace);
    hash.update(format!("clinx-native-input:{thread}:{execution}"));
    let mut bytes = hash.finalize();
    bytes[6] = (bytes[6] & 0x0f) | 0x50;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let hex: String = bytes[..16].iter().map(|b| format!("{b:02x}")).collect();
    format!(
        "{}-{}-{}-{}-{}",
        &hex[..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..]
    )
}

fn native_policy() -> Value {
    json!({"execution_surface":"CODEX_NATIVE","operation_scopes":[]})
}
fn validate_policy(policy: &Policy, p: &Value) -> Result<()> {
    if *p == native_policy() {
        return Ok(());
    }
    if !core::keys(p, "execution_surface operation_scopes")
        || p["execution_surface"] != "HOST_EXECUTOR"
    {
        return Err("INVALID_EXECUTION_POLICY".into());
    }
    let scopes = p["operation_scopes"]
        .as_array()
        .filter(|s| !s.is_empty() && s.len() <= 64)
        .ok_or("EXACT_OPERATION_SCOPES_REQUIRED")?;
    let mut seen = std::collections::BTreeSet::new();
    for scope in scopes {
        if !core::keys(scope, "capability operation operation_class target")
            || !matches!(
                scope["operation_class"].as_str(),
                Some(
                    "READ_ONLY_HOST"
                        | "DEVELOPMENT_MUTATION"
                        | "PRODUCTION_READ_ONLY"
                        | "PRODUCTION_MUTATION"
                )
            )
            || string(scope, "capability").is_err()
            || string(scope, "operation").is_err()
            || !scope["target"].is_string()
            || !seen.insert(core::canonical(scope))
            || !(policy.host_operations.contains(scope)
                || policy.host_operations.contains(&targets::template(scope)))
        {
            return Err("HOST_OPERATION_SCOPE_DENIED".into());
        }
    }
    Ok(())
}
#[allow(clippy::too_many_arguments)]
fn control(
    c: &Connection,
    policy: &Policy,
    op: &str,
    p: &Value,
    id: &str,
    boot: &str,
    hash: &str,
    changes: &mut Vec<Value>,
) -> Result<Value> {
    match op {
        "adopt" => {
            if !core::keys(p, "host project thread_id title workspace_contract")
                && !core::keys(
                    p,
                    "host project thread_id title workspace_contract exact_turn_id",
                )
                && !core::keys(
                    p,
                    "host project thread_id title workspace_contract exact_turn_id canonical_identity",
                )
            {
                return Err("INVALID_ADOPTION".into());
            }
            let thread = string(p, "thread_id")?;
            let project = policy.project(string(p, "project")?, string(p, "host")?)?;
            string(p, "title")?;
            let seal = native_workspace(&project, &p["workspace_contract"])?;
            let canonical = handoff::adoption_identity(p)?;
            for t in rows(c, "task")? {
                if t["thread_id"] == thread && t["host"] == p["host"] {
                    if t["project"] != p["project"]
                        || (canonical.is_some()
                            && t["canonical_handoff"] != canonical.as_ref().unwrap().clone())
                        || (canonical.is_none()
                            && !p["exact_turn_id"].is_null()
                            && t["native_checkpoint"]["turn"]["id"] != p["exact_turn_id"])
                    {
                        return Err("ADOPTION_IDENTITY_CONFLICT".into());
                    }
                    return Ok(
                        json!({"adoption_status":"ADOPTED","task_ref":t["id"],"thread_id":thread,"read_only":false}),
                    );
                }
            }
            if rows(c, "task")?.iter().any(|t| {
                t["host"] == p["host"]
                    && t["lineage"].as_array().into_iter().flatten().any(|h| {
                        h["source"]["thread_id"] == thread || h["successor_thread_id"] == thread
                    })
            }) {
                return Err("THREAD_HISTORICALLY_BOUND".into());
            }
            let ref_id = format!("adopt_{}", core::digest(&json!([p["host"], thread])));
            if let Ok(old) = row(c, "prepared", &ref_id) {
                if old["project"] != p["project"]
                    || old["exact_turn_id"] != p["exact_turn_id"]
                    || old["canonical_handoff"] != canonical.clone().unwrap_or(Value::Null)
                {
                    return Err("ADOPTION_IDENTITY_CONFLICT".into());
                }
                return Ok(old);
            }
            let task_ref = if let Some(identity) = &canonical {
                string(&identity["identity"], "task_id")?.to_owned()
            } else {
                format!("task_{}", &random_id()?[..32])
            };
            if row(c, "task", &task_ref).is_ok() {
                return Err("ADOPTION_IDENTITY_CONFLICT".into());
            }
            let mut result = json!({"id":ref_id,"adoption_ref":ref_id,"kind":"ADOPTION","exact_turn_id":p["exact_turn_id"],"status":"PENDING","adoption_status":"PENDING","host":p["host"],"project":p["project"],"thread_id":thread,"title":p["title"],"workspace_contract":seal,"task_ref":task_ref,"request_hash":hash});
            if let Some(identity) = canonical {
                result["canonical_handoff"] = identity;
            }
            put(c, "prepared", &ref_id, &result, changes)?;
            Ok(result)
        }
        "adoption_begin" | "adoption_commit" => {
            let ref_id = string(p, "adoption_ref")?;
            let mut a = row(c, "prepared", ref_id)?;
            let route_endpoints = policy.endpoints_for(string(&a, "host")?)?;
            if a["kind"] != "ADOPTION" {
                return Err("INVALID_ADOPTION".into());
            }
            if a["status"] == "ADOPTED" {
                return Ok(a);
            }
            if op == "adoption_begin" {
                if !core::keys(p, "adoption_ref") {
                    return Err("INVALID_ADOPTION".into());
                }
                a["observation"] =
                    json!({"nonce":random_id()?,"boot":boot,"deadline":clock()+30000});
                put(c, "prepared", ref_id, &a, changes)?;
                return Ok(
                    json!({"adoption_ref":ref_id,"nonce":a["observation"]["nonce"],"thread_id":a["thread_id"],"exact_turn_id":a["exact_turn_id"],"endpoints":route_endpoints,"route":{"host":a["host"],"project":a["project"],"cwd":a["workspace_contract"]["cwd"],"node_principal":a["node_principal"]}}),
                );
            }
            if !core::keys(p, "adoption_ref nonce observations")
                || p["nonce"].is_null()
                || p["nonce"] != a["observation"]["nonce"]
                || a["observation"]["boot"] != boot
                || a["observation"]["deadline"]
                    .as_i64()
                    .is_none_or(|d| clock() >= d)
            {
                return Err("ADOPTION_OBSERVATION_FENCED".into());
            }
            let observations = p["observations"]
                .as_array()
                .ok_or("INVALID_ADOPTION_OBSERVATIONS")?;
            let mut endpoints = std::collections::BTreeSet::new();
            if observations.len() != route_endpoints.len() {
                return Err("OBSERVATION_ENDPOINT_SET_MISMATCH".into());
            }
            for observation in observations {
                let endpoint = string(observation, "endpoint")?;
                if !(if a["exact_turn_id"].is_null() {
                    core::keys(observation, "endpoint thread")
                } else {
                    core::keys(observation, "endpoint thread turn")
                }) || !route_endpoints.iter().any(|e| e == endpoint)
                    || !endpoints.insert(endpoint)
                {
                    return Err("OBSERVATION_ENDPOINT_SET_MISMATCH".into());
                }
                if !a["exact_turn_id"].is_null()
                    && (observation["turn"]["id"] != a["exact_turn_id"]
                        || !matches!(
                            normalized(&observation["turn"]["status"]).as_str(),
                            "completed" | "failed" | "interrupted" | "cancelled"
                        )
                        || observations
                            .iter()
                            .any(|other| other["turn"] != observation["turn"]))
                {
                    return Err("EXACT_ADOPTION_TURN_CONFLICT".into());
                }
                let t = &observation["thread"];
                if t["id"] != a["thread_id"]
                    || (!a["canonical_handoff"].is_null()
                        && t["cwd"] != a["canonical_handoff"]["identity"]["cwd"])
                    || observations
                        .iter()
                        .any(|other| other["thread"]["cwd"] != t["cwd"])
                    || t["status"]["type"] != "idle"
                {
                    return Err("NATIVE_ADOPTION_NOT_IDLE_OR_IDENTITY_CONFLICT".into());
                }
            }
            let project = policy.project(string(&a, "project")?, string(&a, "host")?)?;
            let resolution = native_resolution(
                &project,
                &json!({"cwd":observations[0]["thread"]["cwd"]}),
                &known_repository(c, &project, &Value::Null)?,
            )?;
            a["requested_workspace_contract"] = a["workspace_contract"].clone();
            a["workspace_contract"] = resolved_contract(&resolution);
            a["workspace_resolution"] = resolution.clone();
            if rows(c, "task")?
                .iter()
                .any(|t| t["host"] == a["host"] && t["thread_id"] == a["thread_id"])
            {
                return Err("THREAD_ALREADY_BOUND".into());
            }
            let task_id = string(&a, "task_ref")?.to_owned();
            if row(c, "task", &task_id).is_ok() {
                return Err("ADOPTION_IDENTITY_CONFLICT".into());
            }
            let execution_policy = native_policy();
            let mut task = json!({"id":task_id,"task_id":task_id,"project":a["project"],"host":a["host"],"title":a["title"],"workspace_contract":a["workspace_contract"],"thread_id":a["thread_id"],"lifecycle":"OPEN","version":0,"execution_policy":execution_policy,"policy_hash":core::digest(&execution_policy),"adoption_evidence":observations});
            node::inherit(&a, &mut task);
            task["repository_identity"] = resolution["repository_identity"].clone();
            task["workspace_resolution"] = resolution;
            if !a["canonical_handoff"].is_null() {
                task["canonical_handoff"] = a["canonical_handoff"].clone();
            }
            if !a["exact_turn_id"].is_null() {
                task["native_checkpoint"] = json!({"endpoint":observations[0]["endpoint"],"thread_id":a["thread_id"],"turn":observations[0]["turn"],"adoption_ref":ref_id});
            }
            put(c, "task", &task_id, &task, changes)?;
            a["status"] = json!("ADOPTED");
            a["adoption_status"] = json!("ADOPTED");
            a["observation"] = Value::Null;
            put(c, "prepared", ref_id, &a, changes)?;
            Ok(a)
        }
        "prepare_policy" => {
            if !core::keys(
                p,
                "task_ref expected_policy_hash target_policy reason approved",
            ) || p["approved"] != true
            {
                return Err("INVALID_POLICY_REQUEST".into());
            }
            let task = row(c, "task", string(p, "task_ref")?)?;
            string(p, "reason")?;
            if task["policy_hash"] != p["expected_policy_hash"] {
                return Err("POLICY_VERSION_CONFLICT".into());
            }
            targets::validate(policy, Some(&task), &p["target_policy"])?;
            let ref_id = format!("reauth_{}", &random_id()?[..32]);
            let result = json!({"id":ref_id,"prepared_reauthorization_ref":ref_id,"kind":"POLICY","status":"PREPARED","task_ref":p["task_ref"],"expected_policy_hash":p["expected_policy_hash"],"target_policy":p["target_policy"],"reason":p["reason"],"request_hash":hash,"operation_id":id});
            put(c, "prepared", &ref_id, &result, changes)?;
            Ok(result)
        }
        "apply_policy" => {
            if !core::keys(
                p,
                "prepared_reauthorization_ref expected_request_hash expected_task_ref approved",
            ) || p["approved"] != true
            {
                return Err("INVALID_POLICY_REQUEST".into());
            }
            let ref_id = string(p, "prepared_reauthorization_ref")?;
            let mut r = row(c, "prepared", ref_id)?;
            if r["kind"] != "POLICY"
                || (!p["expected_request_hash"].is_null()
                    && p["expected_request_hash"] != r["request_hash"])
                || (!p["expected_task_ref"].is_null() && p["expected_task_ref"] != r["task_ref"])
            {
                return Err("PREPARATION_REVIEW_MISMATCH".into());
            }
            if r["status"] == "APPLIED" {
                return Ok(r);
            }
            let task_id = string(&r, "task_ref")?;
            let mut task = row(c, "task", task_id)?;
            if task["active_execution"].is_string() {
                return Err("TASK_STILL_HELD".into());
            }
            if task["policy_hash"] != r["expected_policy_hash"] {
                return Err("POLICY_VERSION_CONFLICT".into());
            }
            targets::validate(policy, Some(&task), &r["target_policy"])?;
            task["execution_policy"] = r["target_policy"].clone();
            task["policy_hash"] = json!(core::digest(&r["target_policy"]));
            put(c, "task", task_id, &task, changes)?;
            r["status"] = json!("APPLIED");
            put(c, "prepared", ref_id, &r, changes)?;
            Ok(r)
        }
        _ => Err("UNSUPPORTED_CONTROL_OPERATION".into()),
    }
}

fn host_command(
    policy: &Policy,
    e: &mut Value,
    prepared: &Value,
    op: &str,
    p: &Value,
) -> Result<Value> {
    let call_id = string(p, "call_id")?.to_owned();
    if prepared["execution_policy"]["execution_surface"] != "HOST_EXECUTOR" {
        return Err("HOST_POLICY_REQUIRED".into());
    }
    if e["dispatch_state"] != "ACKED" {
        return Err("NATIVE_INPUT_ACK_REQUIRED".into());
    }
    if !e["host_deliveries"].is_object() {
        e["host_deliveries"] = json!({});
    }
    if op == "host_receive" {
        if !core::keys(p, "execution_ref call_id request identity") {
            return Err("INVALID_HOST_REQUEST".into());
        }
        let params = &p["request"]["params"];
        if params["threadId"] != e["thread_id"]
            || params["turnId"] != e["turn_id"]
            || params["callId"] != call_id
            || params["namespace"] != "clinx"
            || params["tool"] != "clinx_host_operation"
            || p["identity"]["thread_id"] != e["thread_id"]
            || p["identity"]["turn_id"] != e["turn_id"]
            || p["identity"]["provider_endpoint"]["path"] != e["endpoint"]
        {
            return Err("HOST_BINDING_CONFLICT".into());
        }
        let digest = core::digest(params);
        if let Some(old) = e["host_deliveries"].get(&call_id) {
            if old["request_hash"] != digest {
                return Err("HOST_CALL_ID_CONFLICT".into());
            }
            let mut updated = old.clone();
            if updated["request"]["id"].is_null() && updated["call_state"] == "RECEIVED" {
                updated["request"]["id"] = p["request"]["id"].clone();
                e["host_deliveries"][&call_id] = updated.clone();
            }
            return Ok(updated);
        }
        if e["host_deliveries"].as_object().unwrap().len() >= 64 {
            return Err("HOST_LEDGER_CAPACITY_EXCEEDED".into());
        }
        let row = json!({"tool_call_id":call_id,"call_id":call_id,"execution_id":e["execution_ref"],"attempt_id":e["attempt_id"],
            "request":p["request"],"request_hash":digest,"identity":p["identity"],"identity_json":core::canonical(&p["identity"]),
            "call_state":"RECEIVED","execution_state":"REQUEST_RECEIVED","delivery_state":"PENDING","host_exit_code":null,
            "host_execution_ref":null,"host_ref":null,"host_completion":"UNKNOWN","response_sent":false,
            "response_sent_at":null,"failure_code":null,"reconciliation_required":true});
        e["host_deliveries"][&call_id] = row.clone();
        return Ok(row);
    }
    if matches!(op, "host_take" | "host_begin")
        && clock() >= e["expires_ms"].as_i64().ok_or("INVALID_LEASE")?
    {
        return Err("HOST_LEASE_EXPIRED".into());
    }
    let mut row = e["host_deliveries"]
        .get(&call_id)
        .cloned()
        .ok_or("HOST_CALL_NOT_FOUND")?;
    let result = match op {
        "host_take" => {
            if !core::keys(p, "execution_ref call_id") || row["call_state"] != "RECEIVED" {
                return Err("HOST_CALL_ALREADY_RECORDED".into());
            }
            if e["cancel_requested"] == true {
                return Err("HOST_EXECUTION_CANCELLED".into());
            }
            let args = &row["request"]["params"]["arguments"];
            let scope = json!({"capability":args["capability"],"operation":args["operation"],"operation_class":args["operation_class"],
                "target":args["arguments"].get("target").cloned().unwrap_or(json!(""))});
            validate_policy(policy, &prepared["execution_policy"])?;
            if !prepared["execution_policy"]["operation_scopes"]
                .as_array()
                .unwrap()
                .contains(&scope)
                || e["host_deliveries"].as_object().unwrap().values().any(|r| {
                    r["call_id"] != call_id
                        && r["execution_state"] != "COMMAND_NOT_DISPATCHED"
                        && r["delivery_state"] != "DELIVERED"
                })
            {
                return Err("HOST_OPERATION_OR_PRIOR_DELIVERY_DENIED".into());
            }
            row["call_state"] = json!("ADMITTED");
            json!({"send_permitted":true})
        }
        "host_begin" => {
            if !core::keys(p, "execution_ref call_id host_ref evidence")
                || row["call_state"] != "ADMITTED"
                || e["cancel_requested"] == true
            {
                return Err("HOST_START_DENIED".into());
            }
            string(p, "host_ref")?;
            let ev = &p["evidence"];
            let args = &row["request"]["params"]["arguments"];
            if ev["task_id"] != e["task_ref"]
                || ev["execution_ref"] != e["execution_ref"]
                || ev["host"] != prepared["host"]
                || ["capability", "operation", "operation_class"]
                    .iter()
                    .any(|k| ev[k] != args[k])
                || ev["tool_call_id"] != call_id
                || (args["arguments"]["target"]
                    .as_str()
                    .is_some_and(|s| s.starts_with("gitwt_"))
                    && ev["target_identity"] != args["arguments"]["target"])
            {
                return Err("HOST_START_IDENTITY_CONFLICT".into());
            }
            row["host_ref"] = p["host_ref"].clone();
            row["host_execution_ref"] = p["host_ref"].clone();
            row["call_state"] = json!("RUNNING");
            row["execution_state"] = json!("COMMAND_DISPATCHED");
            row["start_evidence"] = ev.clone();
            json!({"send_permitted":true})
        }
        "host_complete" => {
            if !core::keys(p, "execution_ref call_id host_ref result")
                || row["host_ref"] != p["host_ref"]
                || row["call_state"] != "RUNNING"
            {
                return Err("HOST_COMPLETION_CONFLICT".into());
            }
            let result = &p["result"];
            if result["exit_code"].as_i64().is_none() {
                return Err("HOST_COMPLETION_UNKNOWN".into());
            }
            row["result"] = result.clone();
            row["host_exit_code"] = result["exit_code"].clone();
            row["host_completion"] = json!("KNOWN");
            row["call_state"] = json!("COMPLETED");
            row["execution_state"] = json!(if result["exit_code"] == 0 {
                "COMMAND_EXECUTED_RESULT_DELIVERY_PENDING"
            } else {
                "COMMAND_EXECUTION_FAILED"
            });
            row.clone()
        }
        "host_sent" => {
            if !core::keys(p, "execution_ref call_id response_id")
                || p["response_id"] != row["request"]["id"]
            {
                return Err("HOST_RESPONSE_ID_CONFLICT".into());
            }
            row["response_sent"] = json!(true);
            let wall = rustix::time::clock_gettime(rustix::time::ClockId::Realtime);
            row["response_sent_at"] =
                json!(wall.tv_sec as f64 + wall.tv_nsec as f64 / 1_000_000_000.0);
            row.clone()
        }
        "host_failed" => {
            if !core::keys(p, "execution_ref call_id failure_code") {
                return Err("INVALID_HOST_FAILURE".into());
            }
            if row["delivery_state"] != "DELIVERED" {
                row["delivery_state"] = json!("FAILED");
                row["failure_code"] = p["failure_code"].clone();
                if matches!(row["call_state"].as_str(), Some("RECEIVED" | "ADMITTED")) {
                    row["call_state"] = json!("REJECTED");
                    row["execution_state"] = json!("COMMAND_NOT_DISPATCHED");
                    row["reconciliation_required"] = json!(false);
                }
            }
            row.clone()
        }
        "host_reconcile_rejected" => {
            if !core::keys(p, "execution_ref call_id endpoint turn")
                || p["endpoint"] != e["endpoint"]
                || p["turn"]["id"] != e["turn_id"]
                || !matches!(
                    normalized(&p["turn"]["status"]).as_str(),
                    "completed" | "failed"
                )
                || row["call_state"] != "RUNNING"
                || row["delivery_state"] != "FAILED"
                || row["response_sent"] != true
                || row["host_completion"] != "UNKNOWN"
                || row["start_evidence"]["mutating"] != 0
                || row["start_evidence"]["operation_class"] != "READ_ONLY_HOST"
            {
                return Err("HOST_REJECTION_RECONCILIATION_DENIED".into());
            }
            let items = p["turn"]["items"]
                .as_array()
                .ok_or("HOST_REJECTION_ITEM_REQUIRED")?;
            let exact: Vec<_> = items.iter().filter(|item| item["id"] == call_id).collect();
            if exact.len() != 1 {
                return Err("HOST_REJECTION_ITEM_REQUIRED".into());
            }
            let item = exact[0];
            let proved = item["contentItems"].as_array().is_some_and(|parts| {
                parts.iter().any(|part| {
                    part["text"]
                        .as_str()
                        .and_then(|text| serde_json::from_str::<Value>(text).ok())
                        .is_some_and(|body| {
                            body["error"] == "ROOT_CONTROLLED_WORKER_LAUNCHER_REQUIRED"
                                && body["result_state"]
                                    == "RESULT_DELIVERY_UNCONFIRMED_AFTER_DISPATCH"
                                && body["execution_state"] == "COMMAND_DISPATCHED"
                                && body["reconciliation_required"] == true
                        })
                })
            });
            // This named guard throws while evaluating spawn_argv, before
            // Popen. Generic command/transport failures prove no such fact.
            if item["type"] != "dynamicToolCall"
                || item["namespace"] != "clinx"
                || item["tool"] != "clinx_host_operation"
                || item["success"] != false
                || !proved
            {
                return Err("HOST_PRESPAWN_REJECTION_UNPROVEN".into());
            }
            row["reconciliation"] = json!({"contract":"HOST_PRESPAWN_REJECTION_V1",
                "prior_call_state":row["call_state"],"prior_execution_state":row["execution_state"],
                "prior_delivery_state":row["delivery_state"],"prior_failure_code":row["failure_code"],
                "native_item_digest":core::digest(item),"native_turn_id":e["turn_id"],
                "native_turn_status":p["turn"]["status"],"endpoint":p["endpoint"]});
            row["call_state"] = json!("REJECTED");
            row["execution_state"] = json!("COMMAND_NOT_DISPATCHED");
            row["host_completion"] = json!("NOT_EXECUTED");
            row["delivery_state"] = json!("DELIVERED");
            row["failure_code"] = json!("ROOT_CONTROLLED_WORKER_LAUNCHER_REQUIRED");
            row["reconciliation_required"] = json!(false);
            row["terminal_rejection_reconciled"] = json!(true);
            row["provider_item"] = item.clone();
            row.clone()
        }
        "host_ack" => {
            if !core::keys(p, "execution_ref call_id message") {
                return Err("INVALID_HOST_ACK".into());
            }
            let message = &p["message"];
            let params = &message["params"];
            let item = &params["item"];
            if message["method"] != "item/completed"
                || params["threadId"] != e["thread_id"]
                || params["turnId"] != e["turn_id"]
                || item["id"] != call_id
                || item["namespace"] != "clinx"
                || item["tool"] != "clinx_host_operation"
                || !matches!(
                    item["type"].as_str(),
                    Some("dynamicToolCall" | "DynamicToolCall")
                )
                || row["response_sent"] != true
            {
                return Err("HOST_ACK_IDENTITY_CONFLICT".into());
            }
            let bodies: Vec<Value> = item["contentItems"]
                .as_array()
                .ok_or("HOST_ACK_CONTENT_REQUIRED")?
                .iter()
                .filter_map(|part| serde_json::from_str(part["text"].as_str()?).ok())
                .collect();
            if row["execution_state"] == "COMMAND_NOT_DISPATCHED" {
                if item["success"] != false
                    || !bodies.iter().any(|b| {
                        b["execution_state"] == "COMMAND_NOT_DISPATCHED"
                            && b["result_state"] == row["failure_code"]
                    })
                {
                    return Err("HOST_ACK_REJECTION_CONFLICT".into());
                }
            } else {
                if row["host_completion"] != "KNOWN"
                    || item["success"] != true
                    || !bodies.iter().any(|b| {
                        b["host_execution_ref"] == row["host_ref"]
                            && b["execution_ref"] == e["execution_ref"]
                    })
                {
                    return Err("HOST_ACK_RESULT_CONFLICT".into());
                }
                row["ack_host_ref"] = row["host_ref"].clone();
            }
            row["delivery_state"] = json!("DELIVERED");
            if row["host_exit_code"] == 0 {
                row["execution_state"] = json!("COMMAND_EXECUTED_RESULT_DELIVERED");
            }
            row["reconciliation_required"] = json!(false);
            row["provider_item"] = item.clone();
            row.clone()
        }
        _ => return Err("INVALID_HOST_OPERATION".into()),
    };
    e["host_deliveries"][&call_id] = row;
    Ok(result)
}
fn host_finalization(e: &Value, items: &[Value]) -> Result<Vec<Value>> {
    let rows = e["host_deliveries"]
        .as_object()
        .cloned()
        .unwrap_or_default();
    let calls: Vec<_> = items
        .iter()
        .filter(|i| {
            matches!(
                i["type"].as_str(),
                Some("dynamicToolCall" | "DynamicToolCall" | "hostToolCall")
            )
        })
        .collect();
    if calls.len() != rows.len()
        || calls
            .iter()
            .any(|i| i["id"].as_str().is_none_or(|id| !rows.contains_key(id)))
    {
        return Err("HOST_DELIVERY_LEDGER_INCOMPLETE".into());
    }
    let mut deliveries = vec![];
    for (id, row) in rows {
        if row["delivery_state"] != "DELIVERED"
            || calls.iter().filter(|i| i["id"] == id).count() != 1
            || calls
                .iter()
                .any(|i| i["id"] == id && **i != row["provider_item"])
        {
            return Err("HOST_DELIVERY_RECONCILIATION_REQUIRED".into());
        }
        // An authority-rejected call had no Host permit and no side effect.
        if row["execution_state"] != "COMMAND_NOT_DISPATCHED" {
            deliveries.push(row);
        }
    }
    Ok(deliveries)
}

pub fn transport_operation(operation: &str) -> bool {
    matches!(
        operation,
        "work"
            | "resume_dispatch"
            | "metadata_begin"
            | "metadata_commit"
            | "native_create_take"
            | "take_dispatch"
            | "bind_thread"
            | "authorize_send"
            | "input_begin"
            | "input_commit"
            | "observe_begin"
            | "observe_commit"
            | "host_receive"
            | "host_take"
            | "host_begin"
            | "host_complete"
            | "host_sent"
            | "host_failed"
            | "host_ack"
            | "host_reconcile_rejected"
            | "host_rows"
            | "successor_begin"
            | "successor_take"
            | "successor_commit"
            | "successor_recover_empty"
            | "adoption_endpoints"
            | "adoption_begin"
            | "adoption_commit"
            | "fail_before_send"
            | "take_cancel"
            | "outbox"
            | "ack_outbox"
    )
}

fn renew_observed(
    c: &Connection,
    authority: &str,
    boot: &str,
    e: &mut Value,
    id: &str,
    duration: i64,
) -> Result<()> {
    let resource = string(e, "resource")?;
    let epoch = e["epoch"].as_i64().ok_or("INVALID_EPOCH")?;
    let (stored_boot, state, expiry): (String, String, i64) = c
        .query_row(
            "SELECT boot,state,expires_ms FROM leases WHERE resource=? AND epoch=?",
            params![resource, epoch],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .map_err(db)?;
    let now = clock();
    if stored_boot != boot || state != "ACTIVE" || now >= expiry {
        return Ok(());
    }
    let expires = expiry.max(now + duration);
    let hash = core::digest(
        &json!({"resource":resource,"epoch":epoch,"operation_id":id,"lease_ms":duration}),
    );
    let receipt = json!({"authority_id":authority,"resource":resource,"epoch":epoch,"operation_id":id,"operation":"renew","request_hash":hash,"trusted_ms":now,"boot":boot,"expires_ms":expires});
    c.execute(
        "UPDATE leases SET expires_ms=? WHERE resource=? AND epoch=?",
        params![expires, resource, epoch],
    )
    .map_err(db)?;
    c.execute(
        "INSERT INTO renewals VALUES(?,?,?,?,?)",
        params![resource, epoch, id, hash, core::canonical(&receipt)],
    )
    .map_err(db)?;
    c.execute(
        "INSERT INTO outbox VALUES(?,?,?,NULL,?)",
        params![
            core::digest(&receipt),
            resource,
            epoch,
            core::canonical(&receipt)
        ],
    )
    .map_err(db)?;
    e["expires_ms"] = json!(expires);
    Ok(())
}
