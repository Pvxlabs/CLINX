//! Public native request mapping. Rust Authority remains the sole state writer.
//! Every call has a unique receipt identity; an unknown delivery is never retried.
use crate::{
    native_transport::Authority,
    provider::{Client, Provider, Result},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::{Read, Write},
    path::PathBuf,
    time::Duration,
};
use unicode_casefold::UnicodeCaseFold;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub runtime_host: String,
    pub provider_endpoint: PathBuf,
    pub provider_timeout_seconds: f64,
    pub host_aliases: BTreeMap<String, Vec<String>>,
    pub codex_host_ids: BTreeMap<String, Vec<String>>,
}
#[derive(Debug)]
pub struct Error {
    pub code: String,
    pub operation_id: Option<String>,
    pub provider: bool,
}
impl From<String> for Error {
    fn from(code: String) -> Self {
        Self {
            code,
            operation_id: None,
            provider: false,
        }
    }
}
impl From<&str> for Error {
    fn from(code: &str) -> Self {
        code.to_owned().into()
    }
}
pub type ApiResult = std::result::Result<Value, Error>;
fn text<'a>(v: &'a Value, k: &str) -> Option<&'a str> {
    v[k].as_str()
}
fn nonnull(v: &Value, k: &str) -> bool {
    !v[k].is_null()
}
fn truth(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
        _ => v != 0,
    }
}
fn pick(v: &Value, keys: &[&str]) -> Value {
    Value::Object(
        keys.iter()
            .filter_map(|k| v.get(*k).map(|v| (k.to_string(), v.clone())))
            .collect(),
    )
}
fn default(v: &Value, k: &str, d: Value) -> Value {
    v.get(k).cloned().unwrap_or(d)
}
fn required(v: &Value, keys: &[&str]) -> std::result::Result<(), Error> {
    if keys.iter().any(|k| v.get(*k).is_none()) {
        Err("CALL_ARGUMENT_INVALID".into())
    } else {
        Ok(())
    }
}
fn now() -> String {
    chrono::DateTime::<chrono::Utc>::from(std::time::SystemTime::now())
        .format("%Y-%m-%dT%H:%M:%S%.6f+00:00")
        .to_string()
}
pub fn operation_id() -> Result<String> {
    let mut b = [0; 16];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut b))
        .map_err(|_| "RANDOM_UNAVAILABLE")?;
    Ok(b.iter().map(|b| format!("{b:02x}")).collect())
}

/// Only observations. Implementations never resume/start a Provider or claim a writer.
pub trait Reads {
    fn thread(&mut self, tid: &str) -> Result<Value>;
    fn page(&mut self, tid: &str, limit: u64, cursor: Option<&str>) -> Result<Value>;
    fn desktop(
        &mut self,
        tid: &str,
        context: bool,
        limit: u64,
        budget: usize,
        cursor: Option<&str>,
    ) -> Result<Value>;
    fn exact(&mut self, tid: &str, turn: &str) -> Result<Value> {
        let mut cursor = None;
        let mut seen = BTreeSet::new();
        for _ in 0..100 {
            let page = self.page(tid, 20, cursor.as_deref())?;
            let rows = page["data"].as_array().ok_or("NATIVE_HISTORY_INVALID")?;
            let found: Vec<_> = rows.iter().filter(|t| t["id"] == turn).collect();
            if found.len() == 1 {
                return Ok(found[0].clone());
            }
            if !found.is_empty() {
                break;
            }
            match page["nextCursor"].as_str() {
                Some(c) if !c.is_empty() && seen.insert(c.to_owned()) => {
                    cursor = Some(c.to_owned())
                }
                _ => break,
            }
        }
        Err("EXACT_NATIVE_TURN_UNAVAILABLE".into())
    }
}
pub struct NativeReads(pub Config);
impl NativeReads {
    fn client(&self) -> Result<Client> {
        Client::connect(
            &self.0.provider_endpoint,
            Duration::try_from_secs_f64(self.0.provider_timeout_seconds)
                .map_err(|_| "TIMEOUT_INVALID")?,
        )
    }
}
impl Reads for NativeReads {
    fn thread(&mut self, tid: &str) -> Result<Value> {
        self.client()?.thread(tid).map_err(|e| {
            if e == "THREAD_IDENTITY_CONFLICT" {
                "NATIVE_OBSERVATION_IDENTITY_CONFLICT".into()
            } else {
                e
            }
        })
    }
    fn page(&mut self, tid: &str, limit: u64, cursor: Option<&str>) -> Result<Value> {
        let mut p = json!({"threadId":tid,"limit":limit,"itemsView":"full","sortDirection":"desc"});
        if let Some(c) = cursor {
            p["cursor"] = json!(c)
        }
        let page = self.client()?.request("thread/turns/list", p)?;
        if !page["data"].is_array() {
            return Err("NATIVE_HISTORY_INVALID".into());
        }
        Ok(page)
    }
    fn desktop(
        &mut self,
        tid: &str,
        context: bool,
        limit: u64,
        budget: usize,
        cursor: Option<&str>,
    ) -> Result<Value> {
        desktop(tid, context, limit, budget, cursor)
    }
}

pub struct Api<A: Authority, R: Reads> {
    pub authority: A,
    pub reads: R,
    pub config: Config,
}
impl<A: Authority, R: Reads> Api<A, R> {
    pub fn call(&mut self, op: &str, p: Value) -> ApiResult {
        let id = operation_id().map_err(Error::from)?;
        self.authority.call(op, p, &id).map_err(|code| Error {
            code: if code == "AUTHORITY_DELIVERY_UNKNOWN" {
                "DELIVERY_UNKNOWN".into()
            } else {
                code
            },
            operation_id: Some(id),
            provider: false,
        })
    }
    fn native(&mut self, tid: &str) -> ApiResult {
        self.reads.thread(tid).map_err(|code| Error {
            code,
            operation_id: None,
            provider: true,
        })
    }
    fn cwd(&mut self, tid: &str) -> ApiResult {
        let native = self.native(tid)?;
        if native["id"] != tid {
            return Err("NATIVE_OBSERVATION_IDENTITY_CONFLICT".into());
        }
        match text(&native, "cwd") {
            Some(c) if c.starts_with('/') && !c.contains('\0') => Ok(json!(c)),
            _ => Err("WORKSPACE_CWD_UNREADABLE".into()),
        }
    }
    fn tasks(&mut self) -> std::result::Result<Vec<Value>, Error> {
        let mut after = String::new();
        let mut tasks = vec![];
        loop {
            let page = self.call("list_tasks", json!({"projection":"summary","after":after}))?;
            tasks.extend(
                page["tasks"]
                    .as_array()
                    .ok_or("INVALID_TASK_CURSOR")?
                    .iter()
                    .cloned(),
            );
            match page.get("next_after") {
                None | Some(Value::Null) => return Ok(tasks),
                Some(Value::String(c)) if c > &after => after = c.clone(),
                _ => return Err("INVALID_TASK_CURSOR".into()),
            }
        }
    }
    fn task(&mut self, reference: &Value) -> ApiResult {
        self.tasks()?
            .into_iter()
            .find(|t| t["task_id"] == *reference)
            .ok_or_else(|| "UNKNOWN_TASK".into())
    }
    pub fn invoke(&mut self, name: &str, a: &Value) -> ApiResult {
        if !a.is_object() {
            return Err("CALL_ARGUMENT_INVALID".into());
        }
        match name {
            "clinx_get_capabilities" => Ok(
                json!({"authority":"RUST","capabilities":["prepare","start","cancel","receipt_readback","native_result","native_adoption","policy_reauthorization"],"legacy_history":false,"host_tools":true,"read_only":true}),
            ),
            "clinx_prepare_execution" => self.prepare(a),
            "clinx_start_execution" => self.start(a),
            "clinx_cancel_execution" => {
                if !truth(&a["execution_ref"])
                    || nonnull(a, "task_ref")
                    || nonnull(a, "native_turn_id")
                {
                    return Err("EXACT_RUST_EXECUTION_REQUIRED".into());
                }
                self.call("cancel",json!({"execution_ref":a["execution_ref"],"approved":default(a,"approved",json!(false))}))
            }
            "clinx_adopt_conversation" => self.adopt(a),
            "clinx_find_task" => {
                let rows = self
                    .tasks()?
                    .into_iter()
                    .filter(|t| {
                        [
                            ("task_ref", "task_id"),
                            ("project", "project"),
                            ("host", "host"),
                            ("status", "lifecycle"),
                        ]
                        .iter()
                        .all(|(k, p)| !truth(&a[*k]) || a[*k] == t[*p])
                            && text(a, "query").is_none_or(|q| {
                                text(t, "title")
                                    .unwrap_or("")
                                    .case_fold()
                                    .collect::<String>()
                                    .contains(&q.case_fold().collect::<String>())
                            })
                    })
                    .map(|t| task_public(&t))
                    .collect::<Vec<_>>();
                Ok(json!({"tasks":rows,"read_only":true}))
            }
            "clinx_list_projects" => {
                let p = self.call("list_projects", json!({}))?;
                let rows = p["projects"]
                    .as_array()
                    .ok_or("INVALID_PROJECTS")?
                    .iter()
                    .filter(|p| !truth(&a["host"]) || a["host"] == p["host"])
                    .filter(|p| {
                        text(a, "query").is_none_or(|q| {
                            text(p, "alias")
                                .unwrap_or("")
                                .case_fold()
                                .collect::<String>()
                                .contains(&q.case_fold().collect::<String>())
                        })
                    })
                    .map(|p| json!({"project":p["alias"],"cwd":p["cwd"],"host":p["host"]}))
                    .collect::<Vec<_>>();
                Ok(json!({"projects":rows,"read_only":true}))
            }
            "clinx_get_prepared_request" => self.prepared(a),
            "clinx_get_effective_authority" => {
                required(a, &["task_ref"])?;
                let e = self.call("effective_policy", json!({"task_ref":a["task_ref"]}))?;
                if nonnull(a, "requested_operations") && !a["requested_operations"].is_array() {
                    return Err("CALL_ARGUMENT_INVALID".into());
                }
                let requested = a["requested_operations"].as_array();
                let scopes = e["operation_scopes"].as_array().ok_or("INVALID_POLICY")?;
                let missing = requested
                    .map(|rows| {
                        rows.iter()
                            .filter(|s| !scopes.contains(s))
                            .cloned()
                            .collect::<Vec<_>>()
                    })
                    .unwrap_or_default();
                Ok(
                    json!({"task_ref":a["task_ref"],"future_execution_policy":e["task"]["execution_policy"],"policy_hash":e["task"]["policy_hash"],"effective_authority":{"authority":"RUST","operation_scopes":scopes,"denied_scopes":e["denied_scopes"]},"requirements_evaluated":nonnull(a,"requested_operations"),"requirements_covered":if nonnull(a,"requested_operations"){json!(missing.is_empty())}else{Value::Null},"missing_operations":missing,"read_only":true}),
                )
            }
            "clinx_prepare_policy_reauthorization" => {
                required(
                    a,
                    &[
                        "task_ref",
                        "expected_policy_hash",
                        "target_policy",
                        "reason",
                    ],
                )?;
                if nonnull(a, "network_access") {
                    return Err("NETWORK_POLICY_CHANGE_NOT_SUPPORTED".into());
                }
                let policy = policy(&a["target_policy"])?;
                let r=self.call("prepare_policy",json!({"approved":default(a,"approved",json!(false)),"task_ref":a["task_ref"],"expected_policy_hash":a["expected_policy_hash"],"target_policy":policy,"reason":a["reason"]}))?;
                Ok(policy_result(&r))
            }
            "clinx_apply_policy_reauthorization" => {
                required(a, &["prepared_reauthorization_ref"])?;
                let r=self.call("apply_policy",json!({"prepared_reauthorization_ref":a["prepared_reauthorization_ref"],"approved":default(a,"approved",json!(false)),"expected_request_hash":a["expected_request_hash"],"expected_task_ref":a["expected_task_ref"]}))?;
                Ok(policy_result(&r))
            }
            "clinx_register_derived_git_target" => {
                required(a, &["task_ref", "path", "ownership_evidence"])?;
                self.call("register_git_target",json!({"approved":default(a,"approved",json!(false)),"task_ref":a["task_ref"],"path":a["path"],"ownership_evidence":a["ownership_evidence"],"creation_host_execution_ref":a["creation_host_execution_ref"]}))
            }
            "clinx_revoke_derived_git_target" => {
                required(a, &["task_ref", "target"])?;
                self.call("revoke_git_target",json!({"approved":default(a,"approved",json!(false)),"task_ref":a["task_ref"],"target":a["target"]}))
            }
            "clinx_get_status" => self.read(a, false),
            "clinx_get_context" => self.read(a, true),
            _ => Err("UNKNOWN_TOOL".into()),
        }
    }
    fn prepare(&mut self, a: &Value) -> ApiResult {
        required(a, &["prompt"])?;
        let prompt = text(a, "prompt").ok_or("CALL_ARGUMENT_INVALID")?;
        if truth(&a["query"])
            || default(a, "execution_mode", json!("normal")) != "normal"
            || !matches!(
                text(a, "execution_surface"),
                None | Some("CODEX_NATIVE" | "HOST_EXECUTOR")
            )
        {
            return Err("RUST_REQUEST_CAPABILITY_NOT_IMPLEMENTED".into());
        }
        if !matches!(
            text(a, "task_action"),
            None | Some("create" | "continue" | "reopen")
        ) || !matches!(text(a, "task_mode"), None | Some("new" | "continue"))
        {
            return Err("RUST_TASK_ACTION_NOT_IMPLEMENTED".into());
        }
        if (a.get("task_mode").is_some()
            && !matches!(text(a, "task_mode"), Some("new" | "continue")))
            || (nonnull(a, "task_action") && text(a, "task_action").is_none())
        {
            return Err("RUST_TASK_ACTION_NOT_IMPLEMENTED".into());
        }
        if nonnull(a, "execution_surface") && text(a, "execution_surface").is_none() {
            return Err("RUST_REQUEST_CAPABILITY_NOT_IMPLEMENTED".into());
        }
        let mut create = a["task_action"] == "create" || a["task_mode"] == "new";
        let mut reference = a["task_ref"].clone();
        let mut host = a["host"].clone();
        let mut project = a["project"].clone();
        let mut tid = a["thread_id"].clone();
        if nonnull(a, "thread_id") || nonnull(a, "codex_uri") {
            let (thread, resolved) = selector(&self.config, a)?;
            if resolved != self.config.runtime_host {
                return Err("NATIVE_HOST_UNAVAILABLE".into());
            }
            tid = json!(thread);
            host = json!(resolved);
            let rows: Vec<_> = self
                .tasks()?
                .into_iter()
                .filter(|t| t["thread_id"] == tid && t["host"] == host)
                .collect();
            if rows.len() > 1 {
                return Err("THREAD_IDENTITY_CONFLICT".into());
            }
            if let Some(t) = rows.first() {
                if !reference.is_null() && reference != t["task_id"] {
                    return Err("TASK_ROUTE_MISMATCH".into());
                }
                reference = t["task_id"].clone();
                create = false;
            } else if reference.is_null() {
                create = true
            }
        }
        if create == truth(&reference) {
            return Err("EXACT_TASK_OR_CREATE_REQUIRED".into());
        }
        if truth(&reference) {
            let t = self.task(&reference)?;
            if (!host.is_null() && host != t["host"])
                || (!project.is_null() && project != t["project"])
                || (!tid.is_null() && tid != t["thread_id"])
            {
                return Err("TASK_ROUTE_MISMATCH".into());
            }
            host = t["host"].clone();
            project = t["project"].clone();
            tid = t["thread_id"].clone();
        }
        if !truth(&project) {
            return Err("EXACT_PROJECT_REQUIRED".into());
        }
        if truth(&a["reasoning"])
            && truth(&a["reasoning_effort"])
            && a["reasoning"] != a["reasoning_effort"]
        {
            return Err("REASONING_CONFLICT".into());
        }
        let ep = if a["execution_surface"] != "CODEX_NATIVE"
            && (a["execution_surface"] == "HOST_EXECUTOR" || truth(&a["requested_operations"]))
        {
            host_policy(a)?
        } else if a["execution_surface"] != "CODEX_NATIVE"
            && [
                "required_capabilities",
                "operation_classes",
                "production_mutation_intent",
            ]
            .iter()
            .any(|k| truth(&a[*k]))
        {
            return Err("EXACT_HOST_POLICY_REQUIRED".into());
        } else {
            Value::Null
        };
        let workspace = if let Some(tid) = tid.as_str() {
            json!({"cwd":self.cwd(tid)?})
        } else {
            a["workspace_contract"].clone()
        };
        let title = if truth(&a["title"]) {
            a["title"].clone()
        } else if truth(&a["summary"]) {
            a["summary"].clone()
        } else {
            json!(prompt.chars().take(120).collect::<String>())
        };
        let mut r=self.call("prepare",json!({"prompt":prompt,"approved":default(a,"approved",json!(false)),"project":project,"host":if truth(&host){host}else{json!(self.config.runtime_host)},"task_ref":reference,"title":title,"workspace_contract":workspace,"model":a["model"],"reasoning_effort":if truth(&a["reasoning_effort"]){a["reasoning_effort"].clone()}else{a["reasoning"].clone()},"execution_policy":ep,"thread_id":tid}))?;
        if let Some(o) = r.as_object_mut() {
            for k in ["prompt", "id", "new_task"] {
                o.remove(k);
            }
        }
        r["execution_started"] = json!(false);
        r["read_only"] = json!(false);
        Ok(r)
    }
    fn start(&mut self, a: &Value) -> ApiResult {
        required(a, &["prepared_execution_ref"])?;
        let p = self.call(
            "get_prepared",
            json!({"prepared_execution_ref":a["prepared_execution_ref"]}),
        )?;
        let mut cwd = Value::Null;
        if !truth(&p["execution_ref"]) {
            let mut tid = p["thread_id"].clone();
            if !truth(&tid) && p["new_task"] == false {
                tid = self.task(&p["task_ref"])?["thread_id"].clone()
            }
            if let Some(tid) = tid.as_str() {
                cwd = self.cwd(tid)?
            }
        }
        self.call("start",json!({"prepared_execution_ref":a["prepared_execution_ref"],"approved":default(a,"approved",json!(false)),"expected_request_hash":a["expected_request_hash"],"expected_task_ref":a["expected_task_ref"],"native_cwd":cwd}))
    }
    fn adopt(&mut self, a: &Value) -> ApiResult {
        let mut host = a["host"].clone();
        let mut tid = a["thread_id"].clone();
        if nonnull(a, "codex_uri") {
            let (t, h) = selector(&self.config, a)?;
            tid = json!(t);
            host = json!(h)
        }
        if truth(&a["conversation_ref"])
            || truth(&a["query"])
            || !truth(&tid)
            || !truth(&a["project"])
        {
            return Err("EXACT_NATIVE_THREAD_AND_PROJECT_REQUIRED".into());
        }
        if !host.is_null() && host != self.config.runtime_host {
            return Err("NATIVE_HOST_UNAVAILABLE".into());
        }
        let cwd = if nonnull(a, "canonical_identity") {
            if !a["canonical_identity"].is_object() {
                return Err("CANONICAL_HANDOFF_IDENTITY_CONFLICT".into());
            }
            a["canonical_identity"]["cwd"].clone()
        } else {
            self.cwd(tid.as_str().ok_or("INVALID_THREAD_ID")?)?
        };
        let mut p = json!({"host":if truth(&host){host}else{json!(self.config.runtime_host)},"project":a["project"],"thread_id":tid,"title":if truth(&a["title"]){a["title"].clone()}else if truth(&a["summary"]){a["summary"].clone()}else{json!("Native Codex conversation")},"workspace_contract":{"cwd":cwd},"exact_turn_id":a["exact_turn_id"]});
        if nonnull(a, "canonical_identity") {
            p["canonical_identity"] = a["canonical_identity"].clone()
        }
        let mut r = pick(
            &self.call("adopt", p)?,
            &[
                "adoption_ref",
                "adoption_status",
                "task_ref",
                "thread_id",
                "workspace_contract",
            ],
        );
        r["read_only"] = json!(false);
        Ok(r)
    }
    fn prepared(&mut self, a: &Value) -> ApiResult {
        required(a, &["request_ref"])?;
        let p = self.call(
            "get_prepared",
            json!({"prepared_execution_ref":a["request_ref"]}),
        )?;
        if p["kind"] == "POLICY" {
            let t = self.task(&p["task_ref"])?;
            Ok(
                json!({"request_ref":a["request_ref"],"request_type":"POLICY_REAUTHORIZATION","request_hash":p["request_hash"],"request_state":p["status"],"task_ref":p["task_ref"],"target":{"project":t["project"],"host":t["host"],"workspace_contract":t["workspace_contract"]},"baseline_policy_hash":p["expected_policy_hash"],"current_policy_hash":t["policy_hash"],"proposed_policy":p["target_policy"],"current_policy":t["execution_policy"],"execution_started":false,"read_only":true}),
            )
        } else {
            let digest = format!(
                "{:x}",
                Sha256::digest(
                    text(&p, "prompt")
                        .ok_or("PREPARED_REQUEST_INVALID")?
                        .as_bytes()
                )
            );
            Ok(
                json!({"request_ref":a["request_ref"],"request_type":"EXECUTION","request_hash":p["request_hash"],"request_state":p["status"],"task_ref":p["task_ref"],"execution_ref":p["execution_ref"],"target":{"project":p["project"],"host":p["host"],"workspace_contract":p["workspace_contract"],"workspace_resolution":p["workspace_resolution"],"requested_workspace_contract":p["requested_workspace_contract"]},"prompt_sha256":digest,"read_only":true}),
            )
        }
    }
    fn read(&mut self, a: &Value, context: bool) -> ApiResult {
        let (limit, budget) = if context {
            let l = a["recent_turns"].as_u64().unwrap_or(8);
            let b = a["max_bytes"].as_u64().unwrap_or(32000);
            if (nonnull(a, "recent_turns") && a["recent_turns"].as_u64().is_none())
                || (nonnull(a, "max_bytes") && a["max_bytes"].as_u64().is_none())
                || !(1..=20).contains(&l)
                || !(1024..=128000).contains(&b)
            {
                return Err("INVALID_CONTEXT_LIMIT".into());
            }
            (l, b as usize)
        } else {
            (8, 32000)
        };
        if !context
            && [
                "thread_id",
                "codex_uri",
                "host",
                "project",
                "node_id",
                "query",
            ]
            .iter()
            .all(|k| !nonnull(a, k))
        {
            if truth(&a["execution_ref"]) {
                let r = self.call("get_status", json!({"execution_ref":a["execution_ref"]}))?;
                if truth(&a["task_ref"]) && a["task_ref"] != r["task_ref"] {
                    return Err("TASK_ROUTE_MISMATCH".into());
                }
                return Ok(self.host_readback(r));
            }
            if truth(&a["task_ref"]) {
                return Ok(task_public(&self.task(&a["task_ref"])?));
            }
        }
        if nonnull(a, "query") {
            return Err("EXACT_THREAD_SELECTOR_REQUIRED".into());
        }
        let mut selected = Value::Null;
        let mut task = Value::Null;
        let mut reference = a["task_ref"].clone();
        let mut tid = a["thread_id"].clone();
        let mut host = a["host"].clone();
        if truth(&a["execution_ref"]) {
            selected = self.call("get_status", json!({"execution_ref":a["execution_ref"]}))?;
            if !reference.is_null() && reference != selected["task_ref"] {
                return Err("TASK_ROUTE_MISMATCH".into());
            }
            reference = selected["task_ref"].clone();
        }
        if truth(&reference) {
            task = self.task(&reference)?
        }
        if nonnull(a, "thread_id") || nonnull(a, "codex_uri") {
            let (t, h) = selector(&self.config, a)?;
            tid = json!(t);
            host = json!(h);
            if !selected.is_null() && tid != selected["thread_id"] {
                return Err("EXECUTION_TURN_IDENTITY_CONFLICT".into());
            }
            if task.is_null() {
                let rows: Vec<_> = self
                    .tasks()?
                    .into_iter()
                    .filter(|t| t["thread_id"] == tid)
                    .collect();
                if rows.len() > 1 {
                    return Err("THREAD_IDENTITY_CONFLICT".into());
                }
                if let Some(t) = rows.first() {
                    task = self.task(&t["task_id"])?
                }
            }
        } else if !selected.is_null() {
            tid = selected["thread_id"].clone()
        } else if !task.is_null() {
            tid = task["thread_id"].clone()
        }
        if (!host.is_null() && host != self.config.runtime_host)
            || (nonnull(a, "node_id") && a["node_id"] != self.config.runtime_host)
        {
            return Err("NATIVE_HOST_UNAVAILABLE".into());
        }
        if !task.is_null()
            && ((!host.is_null() && host != task["host"])
                || (nonnull(a, "project") && a["project"] != task["project"])
                || (selected.is_null() && tid != task["thread_id"]))
        {
            return Err("TASK_ROUTE_MISMATCH".into());
        }
        let tid = tid
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or("EXACT_THREAD_SELECTOR_REQUIRED")?;
        let cursor = if nonnull(a, "cursor") {
            Some(text(a, "cursor").ok_or("INVALID_CONTEXT_CURSOR")?)
        } else {
            None
        };
        if task.is_null() {
            // Exact UUIDs only; never allow URI/path injection into loopback HTTP.
            validate_thread(tid)?;
            let r = self
                .reads
                .desktop(tid, context, limit, budget, cursor)
                .map_err(Error::from)?;
            if nonnull(a, "project") && a["project"] != r["project"] {
                return Err("THREAD_SCOPE_MISMATCH".into());
            }
            return Ok(r);
        }
        let exact = text(&selected, "native_turn_id");
        if !selected.is_null() && context && exact.is_none() {
            return Err("EXACT_NATIVE_TURN_UNAVAILABLE".into());
        }
        if exact.is_some() && cursor.is_some() {
            return Err("EXECUTION_CONTEXT_CURSOR_NOT_SUPPORTED".into());
        }
        let native = self.native(tid)?;
        if native["id"] != tid {
            return Err("NATIVE_OBSERVATION_IDENTITY_CONFLICT".into());
        }
        let state = text(&native["status"], "type").unwrap_or("UNKNOWN");
        let mut r = json!({"queried_thread_id":tid,"thread_id":tid,"host":task["host"],"project":task["project"],"task_ref":task["task_id"],"execution_ref":selected["execution_ref"],"execution_turn_ref":exact,"execution_state":selected["status"],"execution_result":selected["execution_result"],"lookup_status":"RESOLVED","read_only":true,"observed_at":now(),"status_source":"RUST_AUTHORITY","selection_scope":if selected.is_null(){"QUERIED_THREAD"}else{"EXACT_EXECUTION"},"task_current_projection":task_public(&task),"current_thread_id":task["thread_id"],"is_current_thread":tid==task["thread_id"],"native_thread":{"id":tid,"cwd":native["cwd"],"status":native["status"]},"provider_observation":{"state":state,"source":"LIVE_THREAD_READ","writer_claimed":false},"native_status_source":"LIVE_THREAD_READ","native_display_state":match state{"active"=>"RUNNING","idle"=>"IDLE",_=>"UNKNOWN"}});
        if context {
            let page = match exact {
                Some(turn) => self
                    .reads
                    .exact(tid, turn)
                    .map(|t| json!({"data":[t],"nextCursor":null})),
                None => self.reads.page(tid, limit, cursor),
            }
            .map_err(|code| Error {
                code,
                provider: true,
                operation_id: None,
            })?;
            let projected = context_projection(tid, &page, budget)?;
            r.as_object_mut()
                .unwrap()
                .extend(projected.as_object().unwrap().clone());
            r["context_scope"] = json!(if exact.is_some() {
                "EXACT_EXECUTION_TURN"
            } else {
                "THREAD_RECENT"
            });
            r["provenance"]["execution_attribution"] = if selected.is_null() {
                json!("NOT_CLAIMED")
            } else {
                selected["execution_ref"].clone()
            };
            // Python's previous json.dumps page size (ASCII escaped + spaces)
            // is reproduced, so raw page inclusion never grows past its budget.
            if python_json_size(&page) <= budget {
                r["context"] = page
            } else {
                r["context_truncated"] = json!(true)
            }
        }
        if !selected.is_null() {
            r = self.host_readback(r)
        }
        Ok(r)
    }
    fn host_readback(&mut self, r: Value) -> Value {
        let tid = text(&r, "thread_id");
        let turn = text(&r, "native_turn_id").or_else(|| text(&r, "execution_turn_ref"));
        if let (Some(tid), Some(turn)) = (tid, turn) {
            if let Ok(t) = self.reads.exact(tid, turn) {
                return host_readback(r, &t);
            }
        }
        r
    }
}
fn task_public(t: &Value) -> Value {
    let mut p = pick(
        t,
        &[
            "title",
            "project",
            "host",
            "workspace_contract",
            "thread_id",
            "lifecycle",
            "policy_hash",
            "execution_policy",
            "active_execution",
            "repository_identity",
            "workspace_resolution",
        ],
    );
    p["task_ref"] = t["task_id"].clone();
    p["read_only"] = json!(true);
    p["authority"] = json!("RUST");
    p
}
fn policy_result(r: &Value) -> Value {
    let mut p = pick(
        r,
        &[
            "prepared_reauthorization_ref",
            "task_ref",
            "expected_policy_hash",
            "request_hash",
            "operation_id",
            "status",
        ],
    );
    p["future_execution_policy"] = r["target_policy"].clone();
    p["execution_started"] = json!(false);
    p
}
fn policy(p: &Value) -> std::result::Result<Value, Error> {
    if !p.is_object() {
        return Err("INVALID_POLICY".into());
    }
    if matches!(
        text(p, "execution_surface"),
        None | Some("CODEX_NATIVE" | "NATIVE_FULL_ACCESS")
    ) {
        if truth(&p["operation_scopes"]) || truth(&p["required_capabilities"]) {
            return Err("INVALID_NATIVE_POLICY".into());
        }
        Ok(json!({"execution_surface":"CODEX_NATIVE","operation_scopes":[]}))
    } else {
        Ok(
            json!({"execution_surface":p["execution_surface"],"operation_scopes":if truth(&p["operation_scopes"]){p["operation_scopes"].clone()}else{json!([])}}),
        )
    }
}
const CAPS: &[&str] = &[
    "LOCAL_HOST_PROCESS",
    "SYSTEMD_USER",
    "OUTBOUND_NETWORK",
    "SSH",
    "HOST_FILESYSTEM",
    "AWS_CLI",
    "CLOUDFLARE_CLI",
    "CLOUD_API",
    "GIT",
    "DOCKER",
    "POSTGRES",
    "TAILSCALE",
];
const CLASSES: &[&str] = &[
    "READ_ONLY_HOST",
    "DEVELOPMENT_MUTATION",
    "PRODUCTION_READ_ONLY",
    "PRODUCTION_MUTATION",
    "BUSINESS_ACTION",
];
fn normalized(v: &Value, allowed: &[&str]) -> std::result::Result<BTreeSet<String>, Error> {
    if v.is_null() {
        return Ok(BTreeSet::new());
    }
    v.as_array()
        .ok_or("INVALID_HOST_POLICY")?
        .iter()
        .map(|v| {
            let s = v
                .as_str()
                .ok_or("INVALID_HOST_POLICY")?
                .trim()
                .to_uppercase();
            if !allowed.contains(&s.as_str()) {
                return Err("INVALID_HOST_POLICY".into());
            }
            Ok(s)
        })
        .collect()
}
fn host_policy(a: &Value) -> std::result::Result<Value, Error> {
    let caps = normalized(&a["required_capabilities"], CAPS)?;
    let classes = normalized(&a["operation_classes"], CLASSES)?;
    if caps.is_empty()
        || classes.is_empty()
        || classes.contains("BUSINESS_ACTION")
        || (classes.contains("PRODUCTION_MUTATION") && !truth(&a["production_mutation_intent"]))
    {
        return Err("INVALID_HOST_POLICY".into());
    }
    let mut scopes = BTreeSet::new();
    if !a["requested_operations"].is_null() {
        let rows = a["requested_operations"]
            .as_array()
            .filter(|r| r.len() <= 128)
            .ok_or("INVALID_HOST_POLICY")?;
        for r in rows {
            let keys = ["capability", "operation", "operation_class", "target"];
            if r.as_object()
                .is_none_or(|o| o.len() != 4 || !keys.iter().all(|k| o.contains_key(*k)))
                || keys.iter().any(|k| {
                    r[*k]
                        .as_str()
                        .is_none_or(|v| v.chars().count() > 160 || v.chars().any(|c| c < ' '))
                })
            {
                return Err("INVALID_HOST_POLICY".into());
            }
            let cap = text(r, "capability").unwrap().trim().to_uppercase();
            let class = text(r, "operation_class").unwrap().trim().to_uppercase();
            let op = text(r, "operation").unwrap();
            let target = text(r, "target").unwrap();
            if !caps.contains(&cap)
                || !classes.contains(&class)
                || op.trim().is_empty()
                || op.contains('*')
                || target.contains('*')
            {
                return Err("INVALID_HOST_POLICY".into());
            }
            scopes.insert((cap, op.to_owned(), class, target.to_owned()));
        }
    }
    Ok(
        json!({"execution_surface":"HOST_EXECUTOR","operation_scopes":scopes.into_iter().map(|(cap,op,class,target)|json!({"capability":cap,"operation":op,"operation_class":class,"target":target})).collect::<Vec<_>>()}),
    )
}
fn validate_thread(t: &str) -> std::result::Result<(), Error> {
    if regex::Regex::new(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        .unwrap()
        .is_match(t)
    {
        Ok(())
    } else {
        Err("INVALID_THREAD_ID".into())
    }
}
fn unique(values: Option<&Vec<String>>, missing: &str) -> std::result::Result<String, Error> {
    let values = values.ok_or(missing)?;
    if values.is_empty() {
        return Err(missing.into());
    }
    if values.len() != 1 {
        return Err("THREAD_HOST_CONFLICT".into());
    }
    Ok(values[0].clone())
}
fn percent_decode(s: &str) -> std::result::Result<String, Error> {
    let mut out = vec![];
    let mut b = s.as_bytes().iter();
    while let Some(c) = b.next() {
        if *c == b'%' {
            let a = b
                .next()
                .and_then(|c| (*c as char).to_digit(16))
                .ok_or("INVALID_CODEX_THREAD_URI")?;
            let d = b
                .next()
                .and_then(|c| (*c as char).to_digit(16))
                .ok_or("INVALID_CODEX_THREAD_URI")?;
            out.push((a * 16 + d) as u8)
        } else {
            out.push(*c)
        }
    }
    String::from_utf8(out).map_err(|_| "INVALID_CODEX_THREAD_URI".into())
}
fn selector(cfg: &Config, a: &Value) -> std::result::Result<(String, String), Error> {
    if nonnull(a, "thread_id") && nonnull(a, "codex_uri") {
        return Err("IDENTITY_SELECTOR_CONFLICT".into());
    }
    let mut host_id = None;
    let tid = if nonnull(a, "codex_uri") {
        let uri = text(a, "codex_uri").ok_or("INVALID_CODEX_THREAD_URI")?;
        if uri.chars().any(|c| c <= ' ' || c == '\x7f') || uri.contains('#') {
            return Err("INVALID_CODEX_THREAD_URI".into());
        }
        let rest = uri
            .strip_prefix("codex://threads/")
            .ok_or("INVALID_CODEX_THREAD_URI")?;
        let mut parts = rest.splitn(2, '?');
        let t = parts.next().unwrap();
        validate_thread(t).map_err(|_| Error::from("INVALID_CODEX_THREAD_URI"))?;
        if let Some(q) = parts.next() {
            let h = percent_decode(
                q.strip_prefix("hostId=")
                    .ok_or("INVALID_CODEX_THREAD_URI")?,
            )?;
            if h.is_empty()
                || !h
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || "_.:-".contains(c))
            {
                return Err("INVALID_CODEX_THREAD_URI".into());
            }
            host_id = Some(h)
        }
        t.to_owned()
    } else {
        let t = text(a, "thread_id").ok_or("INVALID_THREAD_ID")?;
        validate_thread(t)?;
        t.to_owned()
    };
    let explicit = if nonnull(a, "host") {
        Some(unique(
            cfg.host_aliases.get(
                &text(a, "host")
                    .ok_or("UNKNOWN_THREAD_HOST")?
                    .case_fold()
                    .collect::<String>(),
            ),
            "UNKNOWN_THREAD_HOST",
        )?)
    } else {
        None
    };
    let routed = host_id
        .map(|h| unique(cfg.codex_host_ids.get(&h), "UNKNOWN_CODEX_HOST_ID"))
        .transpose()?;
    if routed.is_some() && explicit.is_some() && routed != explicit {
        return Err("THREAD_HOST_CONFLICT".into());
    }
    Ok((
        tid,
        routed.or(explicit).map(Ok).unwrap_or_else(|| {
            unique(
                cfg.host_aliases
                    .get(&cfg.runtime_host.case_fold().collect::<String>()),
                "UNKNOWN_THREAD_HOST",
            )
        })?,
    ))
}

pub fn host_readback(mut r: Value, turn: &Value) -> Value {
    let mut rows = vec![];
    for item in turn["items"].as_array().into_iter().flatten() {
        if item["type"] != "dynamicToolCall"
            || item["namespace"] != "clinx"
            || item["tool"] != "clinx_host_operation"
        {
            continue;
        }
        for part in item["contentItems"].as_array().into_iter().flatten() {
            if let Some(body) = text(part, "text")
                .and_then(|s| serde_json::from_str::<Value>(s).ok())
                .filter(Value::is_object)
            {
                let mut row = pick(
                    &body,
                    &[
                        "execution_state",
                        "delivery_state",
                        "result_state",
                        "reconciliation_required",
                        "host_execution_ref",
                        "exit_code",
                        "stdout",
                        "stderr",
                        "error",
                        "delivery_state_scope",
                    ],
                );
                row["call_id"] = item["id"].clone();
                row["source"] = json!("NATIVE_DYNAMIC_TOOL_RECEIPT");
                row["success"] = item["success"].clone();
                rows.push(row)
            }
        }
    }
    if rows.is_empty() {
        return r;
    }
    if let Some(p) = rows.iter().find(|v| truth(&v["reconciliation_required"])) {
        let finalized = r["resource_state"] == "RELEASED"
            || matches!(
                text(&r, "execution_state"),
                Some("SUCCEEDED" | "FAILED" | "CANCELLED")
            );
        r["reconciliation_required"] = json!(!finalized);
        r["current_stage"] = json!(if finalized {
            "FINALIZED"
        } else {
            "HOST_RECONCILIATION_REQUIRED"
        });
        r["failure_code"] = p["error"].clone()
    }
    r["host_executions"] = json!(rows);
    r
}

pub fn context_projection(tid: &str, page: &Value, budget: usize) -> ApiResult {
    let turns = page["data"].as_array().ok_or("NATIVE_HISTORY_INVALID")?;
    let mut latest = BTreeMap::new();
    for turn in turns.iter().rev() {
        for item in turn["items"].as_array().into_iter().flatten() {
            if let Some((role, text)) = message(item) {
                latest.insert(role, (text, turn["id"].clone()));
            }
        }
    }
    let max = budget / latest.len().max(1);
    let mut texts = BTreeMap::new();
    let mut refs = serde_json::Map::new();
    let mut truncated = truth(&page["nextCursor"]);
    for (role, (text, turn)) in latest {
        let mut end = text.len().min(max);
        while !text.is_char_boundary(end) {
            end -= 1
        }
        texts.insert(role, text[..end].to_owned());
        truncated |= text.len() > max;
        refs.insert(role.to_owned(), json!({"turn_id":turn,"timestamp":null}));
    }
    Ok(
        json!({"context_status":if texts.is_empty(){"CONTEXT_UNAVAILABLE"}else{"AVAILABLE"},"context_source":"LIVE_PROVIDER_TURNS","context_scope":"THREAD_RECENT","context_range":"cursor-page","context_truncated":truncated,"next_cursor":page["nextCursor"],"last_user_intent":texts.get("user"),"last_codex_result":texts.get("assistant"),"provenance":{"queried_thread_id":tid,"context_turn_refs":turns.iter().map(|t|t["id"].clone()).collect::<Vec<_>>(),"message_refs":refs,"execution_attribution":"NOT_CLAIMED"}}),
    )
}
fn message(item: &Value) -> Option<(&'static str, String)> {
    if matches!(text(item, "channel"), Some("analysis" | "reasoning"))
        || matches!(text(item, "phase"), Some("analysis" | "reasoning"))
    {
        return None;
    }
    let (role, text) = match text(item, "type") {
        Some("agentMessage") => ("assistant", text(item, "text")?.to_owned()),
        Some("userMessage") | Some("message") => {
            let role = if item["type"] == "userMessage" {
                "user"
            } else {
                match text(item, "role") {
                    Some("user") => "user",
                    Some("assistant") => "assistant",
                    _ => return None,
                }
            };
            let text = item["content"]
                .as_array()?
                .iter()
                .filter(|v| matches!(text(v, "type"), Some("text" | "input_text" | "output_text")))
                .filter_map(|v| text(v, "text"))
                .collect::<Vec<_>>()
                .join("\n");
            (role, text)
        }
        _ => return None,
    };
    if text.trim().is_empty() {
        return None;
    }
    let mut text = text;
    for (pattern, replacement) in [
        (
            r"(?i)\b(authorization\s*[:=]\s*bearer\s+|(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*)[^\s,;]+",
            "${1}[REDACTED]",
        ),
        (
            r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,})\b",
            "[REDACTED]",
        ),
        (
            r"(?s)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
            "[REDACTED]",
        ),
        (r"\blin_api_[A-Za-z0-9]+\b", "[REDACTED]"),
        (
            r"(?is)<in-app-browser-context\b[^>]*>.*?</in-app-browser-context>",
            "",
        ),
    ] {
        text = regex::Regex::new(pattern)
            .unwrap()
            .replace_all(&text, replacement)
            .into_owned()
    }
    Some((role, text.replace('\0', "")))
}
fn python_json_size(value: &Value) -> usize {
    match value {
        Value::String(s) => {
            2 + s
                .chars()
                .map(|c| match c {
                    '"' | '\\' | '\n' | '\r' | '\t' | '\x08' | '\x0c' => 2,
                    c if c < ' ' => 6,
                    c if c >= '\x7f' => {
                        if c as u32 > 0xffff {
                            12
                        } else {
                            6
                        }
                    }
                    _ => 1,
                })
                .sum::<usize>()
        }
        Value::Array(a) => {
            2 + a.iter().map(python_json_size).sum::<usize>() + a.len().saturating_sub(1) * 2
        }
        Value::Object(o) => {
            2 + o
                .iter()
                .map(|(k, v)| python_json_size(&json!(k)) + 2 + python_json_size(v))
                .sum::<usize>()
                + o.len().saturating_sub(1) * 2
        }
        _ => value.to_string().len(),
    }
}
fn url_encode(s: &str) -> String {
    s.as_bytes()
        .iter()
        .map(|b| {
            if b.is_ascii_alphanumeric() || b"-_.~".contains(b) {
                (*b as char).to_string()
            } else {
                format!("%{b:02X}")
            }
        })
        .collect()
}
fn desktop(
    tid: &str,
    context: bool,
    limit: u64,
    budget: usize,
    cursor: Option<&str>,
) -> Result<Value> {
    let credential = std::env::var("CLINX_NATIVE_OBSERVER_TOKEN_FILE")
        .map_err(|_| "NATIVE_OBSERVER_NOT_CONFIGURED")?;
    let token = std::fs::read_to_string(credential).map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")?;
    let token = token.trim();
    if token.is_empty() || token.contains(['\r', '\n']) {
        return Err("NATIVE_OBSERVER_UNAVAILABLE".into());
    }
    let mut path = format!(
        "/v2/native/{tid}?context={}&recent_turns={limit}&max_bytes={budget}",
        u8::from(context)
    );
    if let Some(c) = cursor {
        path.push_str(&format!("&cursor={}", url_encode(c)))
    }
    let mut stream = std::net::TcpStream::connect_timeout(
        &"127.0.0.1:8766".parse().unwrap(),
        Duration::from_secs(12),
    )
    .map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")?;
    stream
        .set_read_timeout(Some(Duration::from_secs(12)))
        .and_then(|_| stream.set_write_timeout(Some(Duration::from_secs(12))))
        .map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")?;
    stream.write_all(format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1:8766\r\nAuthorization: Bearer {token}\r\nConnection: close\r\n\r\n").as_bytes()).map_err(|_|"NATIVE_OBSERVER_UNAVAILABLE")?;
    let mut wire = vec![];
    stream
        .take(1048576 + 65536 + 1)
        .read_to_end(&mut wire)
        .map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")?;
    let split = wire
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .filter(|i| *i <= 65536)
        .ok_or("NATIVE_OBSERVER_UNAVAILABLE")?;
    let header = std::str::from_utf8(&wire[..split]).map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")?;
    let body = &wire[split + 4..];
    if body.len() > 1048576 {
        return Err("CONTEXT_CAPACITY_EXCEEDED".into());
    }
    let v: Value = serde_json::from_slice(body).map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")?;
    if !header
        .lines()
        .next()
        .is_some_and(|l| l.starts_with("HTTP/1.1 200 ") || l.starts_with("HTTP/1.0 200 "))
    {
        return Err(match text(&v, "error") {
            Some(
                c @ ("PROJECT_SCOPE_DENIED"
                | "SHARING_SCOPE_DENIED"
                | "NATIVE_THREAD_NOT_FOUND"
                | "INVALID_NATIVE_CONTEXT_CURSOR"
                | "INVALID_QUERY"),
            ) => c,
            _ => "NATIVE_OBSERVER_UNAVAILABLE",
        }
        .into());
    }
    if v["queried_thread_id"] != tid || v["read_only"] != true {
        return Err("NATIVE_OBSERVATION_IDENTITY_CONFLICT".into());
    }
    Ok(v)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{cell::RefCell, collections::VecDeque, rc::Rc};
    struct Commands(Rc<RefCell<VecDeque<Value>>>);
    impl Authority for Commands {
        fn call(&mut self, op: &str, p: Value, id: &str) -> Result<Value> {
            assert_eq!(id.len(), 32);
            let row = self
                .0
                .borrow_mut()
                .pop_front()
                .expect("unexpected authority call");
            assert_eq!(row["operation"], op);
            assert_eq!(row["payload"], p, "{op}");
            if let Some(e) = row["error"].as_str() {
                Err(e.into())
            } else {
                Ok(row["result"].clone())
            }
        }
    }
    struct Reader(Rc<RefCell<VecDeque<Value>>>);
    impl Reads for Reader {
        fn thread(&mut self, tid: &str) -> Result<Value> {
            let r = self
                .0
                .borrow_mut()
                .pop_front()
                .expect("unexpected Provider thread read");
            assert_eq!(r["method"], "thread");
            assert_eq!(r["thread"], tid);
            Ok(r["result"].clone())
        }
        fn page(&mut self, tid: &str, limit: u64, cursor: Option<&str>) -> Result<Value> {
            let r = self
                .0
                .borrow_mut()
                .pop_front()
                .expect("unexpected Provider page read");
            assert_eq!(r["method"], "page");
            assert_eq!(r["thread"], tid);
            assert_eq!(r["limit"], limit);
            assert_eq!(r["cursor"], json!(cursor));
            Ok(r["result"].clone())
        }
        fn desktop(
            &mut self,
            _: &str,
            _: bool,
            _: u64,
            _: usize,
            _: Option<&str>,
        ) -> Result<Value> {
            panic!("unexpected desktop read")
        }
    }
    #[test]
    fn original_python_public_request_and_read_vectors() {
        let golden: Value =
            serde_json::from_str(include_str!("../../fixtures/mcp_api_v1.json")).unwrap();
        for row in golden["cases"].as_array().unwrap() {
            let commands = Rc::new(RefCell::new(
                row["commands"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .cloned()
                    .collect::<VecDeque<_>>(),
            ));
            let reads = Rc::new(RefCell::new(
                row["reads"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .cloned()
                    .collect::<VecDeque<_>>(),
            ));
            let mut api = Api {
                authority: Commands(commands.clone()),
                reads: Reader(reads.clone()),
                config: serde_json::from_value(golden["config"].clone()).unwrap(),
            };
            let result = api.invoke(row["tool"].as_str().unwrap(), &row["arguments"]);
            if let Some(e) = row["error"].as_str() {
                assert_eq!(result.unwrap_err().code, e, "{}", row["name"])
            } else {
                let mut value = result.unwrap_or_else(|e| panic!("{}: {e:?}", row["name"]));
                value.as_object_mut().unwrap().remove("observed_at");
                assert_eq!(value, row["result"], "{}", row["name"])
            }
            assert!(
                commands.borrow().is_empty(),
                "{} missing authority calls",
                row["name"]
            );
            assert!(
                reads.borrow().is_empty(),
                "{} missing Provider reads",
                row["name"]
            );
        }
    }
    #[test]
    fn selector_rejects_identity_and_uri_host_injection() {
        let golden: Value =
            serde_json::from_str(include_str!("../../fixtures/mcp_api_v1.json")).unwrap();
        let cfg: Config = serde_json::from_value(golden["config"].clone()).unwrap();
        let tid = "12345678-1234-1234-1234-123456789abc";
        for tail in [
            "#",
            "?",
            "?hostId=",
            "?hostId=mac-id&hostId=mac-id",
            "?hostId=mac%252did",
            "?hostId=mac+id",
            "?hostId=mac%GGid",
            "?hostId=%0a",
            "/extra",
        ] {
            assert_eq!(
                selector(
                    &cfg,
                    &json!({"codex_uri":format!("codex://threads/{tid}{tail}")})
                )
                .unwrap_err()
                .code,
                "INVALID_CODEX_THREAD_URI",
                "{tail}"
            );
        }
        assert_eq!(
            selector(
                &cfg,
                &json!({"codex_uri":format!("codex://threads/{tid}?hostId=mac%2did")})
            )
            .unwrap()
            .1,
            "p620"
        );
        let mut cfg = cfg.clone();
        cfg.codex_host_ids
            .insert("mac-id".into(), vec!["air".into()]);
        assert_eq!(
            selector(
                &cfg,
                &json!({"codex_uri":format!("codex://threads/{tid}?hostId=mac-id"),"host":"p620"})
            )
            .unwrap_err()
            .code,
            "THREAD_HOST_CONFLICT"
        );
    }
    #[test]
    fn exact_history_scan_keeps_100_page_bound_and_rejects_duplicate_or_loop() {
        struct Pages {
            page: usize,
            mode: u8,
        }
        impl Reads for Pages {
            fn thread(&mut self, _: &str) -> Result<Value> {
                unreachable!()
            }
            fn desktop(
                &mut self,
                _: &str,
                _: bool,
                _: u64,
                _: usize,
                _: Option<&str>,
            ) -> Result<Value> {
                unreachable!()
            }
            fn page(&mut self, _: &str, _: u64, _: Option<&str>) -> Result<Value> {
                self.page += 1;
                Ok(match self.mode {
                    0 if self.page == 99 => json!({"data":[{"id":"wanted"}],"nextCursor":null}),
                    1 => json!({"data":[{"id":"wanted"},{"id":"wanted"}],"nextCursor":null}),
                    2 => json!({"data":[],"nextCursor":"loop"}),
                    _ => json!({"data":[],"nextCursor":format!("p{}",self.page)}),
                })
            }
        }
        let mut p = Pages { page: 0, mode: 0 };
        assert_eq!(p.exact("tid", "wanted").unwrap()["id"], "wanted");
        assert_eq!(p.page, 99);
        for mode in [1, 2, 3] {
            let mut p = Pages { page: 0, mode };
            assert_eq!(
                p.exact("tid", "wanted").unwrap_err(),
                "EXACT_NATIVE_TURN_UNAVAILABLE"
            );
            assert!(p.page <= 100)
        }
    }
    #[test]
    fn public_context_keeps_markdown_redaction_roles_and_utf8_budget() {
        let page = json!({"data":[{"id":"turn","items":[{"type":"userMessage","content":[{"type":"input_text","text":"用户请求"}]},{"type":"agentMessage","text":"# Heading\n\n    code\npassword=secretvalue\nlin_api_xyz\n<in-app-browser-context>hidden</in-app-browser-context>"},{"type":"agentMessage","phase":"analysis","text":"never expose"},{"type":"commandExecution","text":"tool output"}]}],"nextCursor":"more"});
        let r = context_projection("thread", &page, 1024).unwrap();
        assert_eq!(r["last_user_intent"], "用户请求");
        let text = r["last_codex_result"].as_str().unwrap();
        assert!(text.starts_with("# Heading\n\n    code"));
        assert!(
            !text.contains("secretvalue")
                && !text.contains("lin_api_")
                && !text.contains("hidden")
                && !text.contains("never expose")
                && !text.contains("tool output")
        );
        assert_eq!(r["context_truncated"], true);
        let r = context_projection("thread", &page, 5).unwrap();
        assert_eq!(r["last_user_intent"], "");
        assert_eq!(r["provenance"]["execution_attribution"], "NOT_CLAIMED");
    }
    #[test]
    fn host_receipt_reconciliation_does_not_undo_rust_terminal_state() {
        let turn = json!({"items":[{"type":"dynamicToolCall","namespace":"clinx","tool":"clinx_host_operation","id":"call","success":true,"contentItems":[{"text":"{\"reconciliation_required\":true,\"error\":\"ACK_LOST\"}"}]}]});
        let active = host_readback(json!({"resource_state":"HELD"}), &turn);
        assert_eq!(active["reconciliation_required"], true);
        let terminal = host_readback(
            json!({"resource_state":"RELEASED","status":"SUCCEEDED"}),
            &turn,
        );
        assert_eq!(terminal["reconciliation_required"], false);
        assert_eq!(terminal["status"], "SUCCEEDED");
    }
}
