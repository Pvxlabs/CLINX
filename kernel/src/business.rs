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
use std::{collections::BTreeMap, path::Path, process::Command};

#[path = "business_handoff.rs"]
mod handoff;
#[path = "business_targets.rs"]
mod targets;

type Result<T> = std::result::Result<T, String>;
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
pub struct Policy {
    pub projects: Vec<Project>,
    pub endpoints: Vec<String>,
    pub lease_ms: i64,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub host_operations: Vec<Value>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub derived_git_roots: Vec<String>,
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
fn git(cwd: &str, args: &[&str]) -> Result<String> {
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
fn workspace(project: &Project, requested: &Value) -> Result<Value> {
    let cwd = if requested.is_null() {
        project.cwd.as_str()
    } else {
        string(requested, "cwd")?
    };
    if !Path::new(cwd).is_absolute()
        || Path::new(cwd)
            .canonicalize()
            .map_err(|_| "WORKSPACE_IDENTITY_UNAVAILABLE")?
            != Path::new(cwd)
    {
        return Err("WORKSPACE_CONTRACT_MISMATCH".into());
    }
    if git(
        &project.cwd,
        &["rev-parse", "--path-format=absolute", "--git-common-dir"],
    )? != git(
        cwd,
        &["rev-parse", "--path-format=absolute", "--git-common-dir"],
    )? {
        return Err("WORKSPACE_REPOSITORY_MISMATCH".into());
    }
    let registered = git(&project.cwd, &["worktree", "list", "--porcelain"])?;
    if !registered
        .lines()
        .any(|s| s.strip_prefix("worktree ") == Some(cwd))
        || git(cwd, &["rev-parse", "--show-toplevel"])? != cwd
    {
        return Err("WORKSPACE_NOT_REGISTERED".into());
    }
    let actual = json!({"cwd":cwd,"branch":git(cwd,&["branch","--show-current"])? ,"head":git(cwd,&["rev-parse","HEAD"])?});
    if actual["branch"] == "" || (!requested.is_null() && *requested != actual) {
        return Err("WORKSPACE_CONTRACT_MISMATCH".into());
    }
    Ok(actual)
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
    json!({"execution_ref":e["execution_ref"],"task_ref":e["task_ref"],"prepared_execution_ref":e["prepared_execution_ref"],"thread_id":e["thread_id"],"native_turn_id":e["turn_id"],"status":e["states"]["Execution"],"dispatch_state":e["dispatch_state"],"resource_state":e["states"]["ResourceAllocation"],"execution_started":e["dispatch_state"]=="ACKED","delivery_certainty":if e["dispatch_state"]=="SENDING" {"UNKNOWN"} else if e["dispatch_state"]=="ACKED" {"CONFIRMED"} else {"NOT_DISPATCHED"},"cancel_requested":e["cancel_requested"],"execution_result":e["execution_result"],"business_outcome":"NOT_ASSERTED","authority":"RUST","read_only":true})
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
        for p in &policy.projects {
            if p.alias.is_empty()
                || p.host.is_empty()
                || !Path::new(&p.cwd).is_absolute()
                || !aliases.insert((&p.host, &p.alias))
            {
                return Err("INVALID_PROJECT_POLICY".into());
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
            return self.business_read(role, operation, p);
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
        if operation == "adoption_endpoints" && core::keys(p, "") {
            return Ok(json!({"endpoints":policy.endpoints}));
        }
        let hash = core::digest(&json!({"operation":operation,"payload":p}));
        let tx = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(db)?;
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
        let result = match operation {
            "host_receive" | "host_take" | "host_begin" | "host_complete" | "host_sent"
            | "host_failed" | "host_ack" => {
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
                    let resource = &p["evidence"]["resource_key"];
                    for other in rows(&tx, "execution")? {
                        for delivery in other["host_deliveries"]
                            .as_object()
                            .into_iter()
                            .flat_map(|m| m.values())
                        {
                            if delivery["host_ref"] == p["host_ref"] && !p["host_ref"].is_null()
                                || !resource.is_null()
                                    && delivery["start_evidence"]["resource_key"] == *resource
                                    && delivery["delivery_state"] != "DELIVERED"
                                    && delivery["host_ref"].is_string()
                            {
                                return Err("HOST_RESOURCE_STILL_HELD".into());
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
                let r: Prepare = decode(p)?;
                if !r.approved
                    || r.prompt.trim().is_empty()
                    || r.prompt.len() > 262144
                    || r.title.is_empty()
                    || r.title.len() > 256
                {
                    return Err("INVALID_PREPARATION".into());
                }
                let project = policy
                    .projects
                    .iter()
                    .find(|x| x.alias == r.project && x.host == r.host)
                    .ok_or("PROJECT_SCOPE_DENIED")?;
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
                let seal = workspace(project, &requested)?;
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
                let prepared = format!("prepared_{}", &random_id()?[..32]);
                let task_ref = r
                    .task_ref
                    .unwrap_or(format!("task_{}", &random_id()?[..32]));
                let v = json!({"id":prepared,"prepared_execution_ref":prepared,"task_ref":task_ref,"new_task":task.is_none(),"project":r.project,"host":r.host,"prompt":r.prompt,"title":r.title,"model":r.model,"reasoning_effort":r.reasoning_effort,"workspace_contract":seal,"status":"PREPARED","request_hash":hash,"execution_policy":execution_policy,"policy_hash":core::digest(&execution_policy)});
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
                        .projects
                        .iter()
                        .find(|x| prepared["project"] == x.alias && prepared["host"] == x.host)
                        .ok_or("PROJECT_SCOPE_DENIED")?;
                    workspace(project, &prepared["workspace_contract"])?;
                    let resource = core::digest(&json!([
                        prepared["host"],
                        prepared["workspace_contract"]["cwd"]
                    ]));
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
                        json!({"id":task_id,"task_id":task_id,"project":prepared["project"],"host":prepared["host"],"title":prepared["title"],"workspace_contract":prepared["workspace_contract"],"thread_id":null,"lifecycle":"OPEN","version":0})
                    } else {
                        row(&tx, "task", &task_id)?
                    };
                    if prepared["new_task"] == true {
                        task["repository_origin"] = git(
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
            "take_dispatch" | "bind_thread" | "authorize_send" | "observe_begin"
            | "observe_commit" | "input_begin" | "input_commit" | "cancel" | "take_cancel"
            | "fail_before_send" => {
                let eid = string(p, "execution_ref")?.to_owned();
                let mut e = row(&tx, "execution", &eid)?;
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
                        result = json!({"execution_ref":eid,"epoch":e["epoch"],"ticket":e["ticket"],"thread_id":e["thread_id"].as_str().map(Value::from).unwrap_or(e["predecessor_thread_id"].clone()),"prepared":prepared,"handoff":e["handoff"],"successor_required":handoff::needed(&task, &prepared),"send_permitted":false});
                    }
                    "bind_thread" => {
                        if !core::keys(p, "execution_ref epoch ticket endpoint thread_id cwd") {
                            return Err("INVALID_BINDING".into());
                        }
                        worker_fence(&tx, &e, p, &self.boot)?;
                        if e["dispatch_state"] != "TAKEN"
                            || (!e["thread_id"].is_null() && e["thread_id"] != p["thread_id"])
                            || !policy.endpoints.iter().any(|x| p["endpoint"] == *x)
                        {
                            return Err("BINDING_CONFLICT".into());
                        }
                        string(p, "thread_id")?;
                        let prepared = row(&tx, "prepared", string(&e, "prepared_execution_ref")?)?;
                        if p["cwd"] != prepared["workspace_contract"]["cwd"] {
                            return Err("WORKSPACE_CONTRACT_MISMATCH".into());
                        }
                        e["thread_id"] = p["thread_id"].clone();
                        e["endpoint"] = p["endpoint"].clone();
                        let tid = string(&e, "task_ref")?;
                        let mut t = row(&tx, "task", tid)?;
                        if handoff::needed(&t, &prepared) {
                            return Err("EXACT_HOST_THREAD_HANDOFF_REQUIRED".into());
                        }
                        t["provider_policy_hash"] = prepared["policy_hash"].clone();
                        t["thread_id"] = e["thread_id"].clone();
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
                            .projects
                            .iter()
                            .find(|x| prepared["project"] == x.alias && prepared["host"] == x.host)
                            .ok_or("PROJECT_SCOPE_DENIED")?;
                        workspace(project, &prepared["workspace_contract"])?;
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
                        result = json!({"execution_ref":eid,"thread_id":e["thread_id"],"endpoint":e["endpoint"],"workspace_contract":prepared["workspace_contract"],"prompt":prepared["prompt"],"client_message_id":native_client_id(string(&e,"thread_id")?,&eid),"nonce":nonce});
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
                        result = json!({"nonce":nonce,"execution_ref":eid,"thread_id":e["thread_id"],"turn_id":e["turn_id"],"endpoints":policy.endpoints});
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
                        let evidence = classify(policy, &e, p)?;
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
                        if !core::keys(p, "execution_ref reason") {
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
                        result = json!({"send_permitted":true,"execution_ref":eid,"thread_id":e["thread_id"],"turn_id":e["turn_id"]});
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
        let (position, previous) = tx
            .query_row(
                "SELECT position,hash FROM business_commands ORDER BY position DESC LIMIT 1",
                [],
                |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)),
            )
            .optional()
            .map_err(db)?
            .unwrap_or((0, String::new()));
        let body = json!({"authority_id":self.identity,"position":position+1,"previous":previous,"operation_id":id,"operation":operation,"role":role.name(),"request_hash":hash,"changes":changes,"result":result});
        let digest = core::digest(&body);
        if core::canonical(&body).len() > crate::MAX_FRAME_BYTES - 2048 {
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
    fn business_read(&self, role: Role, op: &str, p: &Value) -> Result<Value> {
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
                Ok(json!({"tasks":values,"read_only":true}))
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
                    "successor_required":handoff::needed(&task,&prepared),"send_permitted":false}),
                )
            }
            "host_rows" if core::keys(p, "execution_ref") => {
                let e = row(&self.conn, "execution", string(p, "execution_ref")?)?;
                Ok(
                    json!({"rows":e["host_deliveries"].as_object().map(|m|m.values().cloned().collect::<Vec<_>>()).unwrap_or_default()}),
                )
            }
            "list_tasks" if core::keys(p, "") => {
                Ok(json!({"tasks":rows(&self.conn,"task")?,"read_only":true}))
            }
            "work" if core::keys(p, "") => Ok(
                json!({"executions":rows(&self.conn,"execution")?.iter().filter(|e|!terminal(e)).take(100).map(public).collect::<Vec<_>>(),
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
fn classify(policy: &Policy, e: &Value, p: &Value) -> Result<&'static str> {
    let rs = p["observations"].as_array().ok_or("INVALID_OBSERVATIONS")?;
    if rs.len() != policy.endpoints.len() {
        return Err("OBSERVATION_ENDPOINT_SET_MISMATCH".into());
    }
    let mut seen = std::collections::BTreeSet::new();
    let mut unknown = false;
    let mut active = 0;
    let mut exact_live = 0;
    let mut terminals = 0;
    for r in rs {
        if !core::keys(r, "endpoint state thread_id turn_id turn_status")
            || !policy.endpoints.iter().any(|x| r["endpoint"] == *x)
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
    let outcome = match status.as_str() {
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

fn native_client_id(thread: &str, execution: &str) -> String {
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
            {
                return Err("INVALID_ADOPTION".into());
            }
            let thread = string(p, "thread_id")?;
            let project = policy
                .projects
                .iter()
                .find(|r| p["host"] == r.host && p["project"] == r.alias)
                .ok_or("PROJECT_SCOPE_DENIED")?;
            string(p, "title")?;
            let seal = workspace(project, &p["workspace_contract"])?;
            for t in rows(c, "task")? {
                if t["thread_id"] == thread && t["host"] == p["host"] {
                    if t["project"] != p["project"]
                        || t["workspace_contract"] != seal
                        || (!p["exact_turn_id"].is_null()
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
                    || old["workspace_contract"] != seal
                    || old["exact_turn_id"] != p["exact_turn_id"]
                {
                    return Err("ADOPTION_IDENTITY_CONFLICT".into());
                }
                return Ok(old);
            }
            let result = json!({"id":ref_id,"adoption_ref":ref_id,"kind":"ADOPTION","exact_turn_id":p["exact_turn_id"],"status":"PENDING","adoption_status":"PENDING","host":p["host"],"project":p["project"],"thread_id":thread,"title":p["title"],"workspace_contract":seal,"task_ref":format!("task_{}",&random_id()?[..32]),"request_hash":hash});
            put(c, "prepared", &ref_id, &result, changes)?;
            Ok(result)
        }
        "adoption_begin" | "adoption_commit" => {
            let ref_id = string(p, "adoption_ref")?;
            let mut a = row(c, "prepared", ref_id)?;
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
                    json!({"adoption_ref":ref_id,"nonce":a["observation"]["nonce"],"thread_id":a["thread_id"],"exact_turn_id":a["exact_turn_id"],"endpoints":policy.endpoints}),
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
            if observations.len() != policy.endpoints.len() {
                return Err("OBSERVATION_ENDPOINT_SET_MISMATCH".into());
            }
            for observation in observations {
                let endpoint = string(observation, "endpoint")?;
                if !(if a["exact_turn_id"].is_null() {
                    core::keys(observation, "endpoint thread")
                } else {
                    core::keys(observation, "endpoint thread turn")
                }) || !policy.endpoints.iter().any(|e| e == endpoint)
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
                    || t["cwd"] != a["workspace_contract"]["cwd"]
                    || t["status"]["type"] != "idle"
                {
                    return Err("NATIVE_ADOPTION_NOT_IDLE_OR_IDENTITY_CONFLICT".into());
                }
            }
            let project = policy
                .projects
                .iter()
                .find(|r| a["host"] == r.host && a["project"] == r.alias)
                .ok_or("PROJECT_SCOPE_DENIED")?;
            workspace(project, &a["workspace_contract"])?;
            if rows(c, "task")?
                .iter()
                .any(|t| t["host"] == a["host"] && t["thread_id"] == a["thread_id"])
            {
                return Err("THREAD_ALREADY_BOUND".into());
            }
            let task_id = string(&a, "task_ref")?.to_owned();
            let execution_policy = native_policy();
            let mut task = json!({"id":task_id,"task_id":task_id,"project":a["project"],"host":a["host"],"title":a["title"],"workspace_contract":a["workspace_contract"],"thread_id":a["thread_id"],"lifecycle":"OPEN","version":0,"execution_policy":execution_policy,"policy_hash":core::digest(&execution_policy),"adoption_evidence":observations});
            task["repository_origin"] = git(
                string(&a["workspace_contract"], "cwd")?,
                &["remote", "get-url", "--all", "origin"],
            )
            .ok()
            .filter(|s| !s.is_empty() && !s.contains('\n'))
            .map(Value::from)
            .unwrap_or(Value::Null);
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
