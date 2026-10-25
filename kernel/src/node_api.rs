//! Node execution selectors and canonical handoff. No local task writer or worker.
use crate::mcp_api::{Api, ApiResult, Error, Reads};
use crate::native_transport::Authority;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use unicode_casefold::UnicodeCaseFold;

#[derive(Clone, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub node_id: String,
    #[serde(default)]
    pub projects: Vec<Project>,
    pub allowed_threads: Option<Vec<String>>,
    pub allowed_projects: Option<Vec<String>>,
    pub allowed_new_projects: Option<Vec<String>>,
    pub allowed_cancel_projects: Option<Vec<String>>,
}
#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Project {
    pub project_alias: String,
    pub repo: String,
}

/// Also used by injected protocol clients. All selector decisions stay in Rust.
pub trait Backend {
    fn invoke(&mut self, method: &str, arguments: Value) -> ApiResult;
    fn task(&mut self, reference: Value) -> ApiResult;
    fn prepared(&mut self, reference: Value) -> ApiResult;
}
impl<A: Authority, R: Reads> Backend for Api<A, R> {
    fn invoke(&mut self, method: &str, arguments: Value) -> ApiResult {
        Api::invoke(self, &format!("clinx_{method}"), &arguments)
    }
    fn task(&mut self, reference: Value) -> ApiResult {
        Api::task(self, &reference)
    }
    fn prepared(&mut self, reference: Value) -> ApiResult {
        self.call("get_prepared", json!({"prepared_execution_ref":reference}))
    }
}
fn some(v: &Value) -> bool {
    !v.is_null()
}
fn present(v: &Value) -> bool {
    some(v) && v != "" && v != false
}
fn matches_optional(v: &Value, expected: &Value) -> bool {
    v.is_null() || v == expected
}
fn pick(a: &Value, fields: &[&str]) -> Value {
    Value::Object(
        fields
            .iter()
            .filter_map(|k| a.get(*k).map(|v| (k.to_string(), v.clone())))
            .collect(),
    )
}
pub struct Node<B: Backend> {
    pub backend: B,
    pub config: Config,
}
impl<B: Backend> Node<B> {
    fn target(&mut self, a: &Value, new: bool, cancel: bool) -> Result<(), Error> {
        if present(&a["canonical_identity"]) {
            return Err("CANONICAL_HANDOFF_REQUIRES_ADOPTION".into());
        }
        if !matches_optional(&a["host"], &json!(self.config.node_id)) {
            return Err("NODE_TARGET_MISMATCH".into());
        }
        let (mut project, mut thread) = (a["project"].clone(), a["thread_id"].clone());
        if present(&a["task_ref"]) {
            let t = self.backend.task(a["task_ref"].clone())?;
            if t["host"] != self.config.node_id
                || !matches_optional(&project, &t["project"])
                || !matches_optional(&thread, &t["thread_id"])
            {
                return Err("EXECUTION_TARGET_DENIED".into());
            }
            project = t["project"].clone();
            thread = t["thread_id"].clone();
        }
        let p = project
            .as_str()
            .filter(|p| !p.is_empty())
            .ok_or("EXECUTION_TARGET_DENIED")?;
        let permits = |scope: &Option<Vec<String>>| {
            scope
                .as_ref()
                .is_none_or(|s| s.iter().any(|v| v.case_fold().eq(p.case_fold())))
        };
        if !permits(&self.config.allowed_projects)
            || (new && !permits(&self.config.allowed_new_projects))
            || (cancel && !permits(&self.config.allowed_cancel_projects))
        {
            return Err("EXECUTION_TARGET_DENIED".into());
        }
        if present(&thread)
            && self
                .config
                .allowed_threads
                .as_ref()
                .is_some_and(|s| !s.iter().any(|v| thread == *v))
            && (!present(&a["task_ref"])
                || self.config.allowed_new_projects.is_none()
                || !permits(&self.config.allowed_new_projects))
        {
            return Err("EXECUTION_TARGET_DENIED".into());
        }
        Ok(())
    }
    fn canonical(&mut self, a: &Value) -> ApiResult {
        let (e, o) = (&a["canonical_identity"], &a["_canonical_owner"]);
        if !e.is_object() || !o.is_object() {
            return Err("CANONICAL_HANDOFF_SCOPE_DENIED".into());
        }
        let proof: Value = serde_json::from_str(
            e["first_provenance_json"]
                .as_str()
                .ok_or("CANONICAL_HANDOFF_IDENTITY_CONFLICT")?,
        )
        .map_err(|_| Error::from("CANONICAL_HANDOFF_IDENTITY_CONFLICT"))?;
        let node = json!(self.config.node_id);
        if !proof.is_object()
            || o["owner"] != node
            || e["node_id"] != node
            || e["host"] != node
            || e["user_scope"] != o["user"]
            || !present(&o["user"])
            || !present(&o["fingerprint"])
            || proof["node_fingerprint"] != o["fingerprint"]
            || proof["node"] != node
            || !matches_optional(&a["host"], &node)
            || !matches_optional(&a["thread_id"], &e["thread_id"])
            || !matches_optional(&a["task_ref"], &e["task_id"])
        {
            return Err("CANONICAL_HANDOFF_IDENTITY_CONFLICT".into());
        }
        let projects: Vec<_> = self
            .config
            .projects
            .iter()
            .filter(|p| e["cwd"] == p.repo)
            .collect();
        if projects.len() != 1 {
            return Err("EXECUTION_TARGET_DENIED".into());
        }
        let project = projects[0].project_alias.clone();
        if a["project"]
            .as_str()
            .is_some_and(|p| !p.case_fold().eq(project.case_fold()))
            || (some(&a["project"]) && !a["project"].is_string())
        {
            return Err("EXECUTION_TARGET_DENIED".into());
        }
        let mut target = json!({"host":node,"project":project,"thread_id":e["thread_id"]});
        self.target(&target, false, false)?;
        if a["exact_turn_id"].as_str().is_none_or(|t| t.is_empty())
            || !matches_optional(&a["native_turn_id"], &a["exact_turn_id"])
        {
            return Err("EXACT_PREDECESSOR_TURN_REQUIRED".into());
        }
        target["title"] = e["title"].clone();
        target["exact_turn_id"] = a["exact_turn_id"].clone();
        target["canonical_identity"] = e.clone();
        self.backend.invoke("adopt_conversation", target)
    }
    fn status(&mut self, a: &Value) -> ApiResult {
        let r = self.backend.invoke(
            "get_status",
            json!({"execution_ref":a["execution_ref"],"task_ref":a["task_ref"]}),
        )?;
        let mut target = a.clone();
        target
            .as_object_mut()
            .ok_or("CALL_ARGUMENT_INVALID")?
            .remove("thread_id");
        target["task_ref"] = r["task_ref"].clone();
        self.target(&target, false, false)?;
        if !matches_optional(&a["thread_id"], &r["thread_id"])
            || !matches_optional(&a["native_turn_id"], &r["native_turn_id"])
        {
            return Err("EXECUTION_TURN_IDENTITY_CONFLICT".into());
        }
        Ok(r)
    }
    pub fn invoke(&mut self, operation: &str, arguments: &Value) -> ApiResult {
        if !arguments.is_object() {
            return Err("CALL_ARGUMENT_INVALID".into());
        }
        let mut a = arguments.clone();
        let mut r = match operation {
            "prepare" => {
                if some(&a["canonical_identity"]) {
                    let adoption = self.canonical(&a)?;
                    if adoption["adoption_status"] != "ADOPTED" {
                        return Err("CANONICAL_ADOPTION_PENDING".into());
                    }
                    a["task_ref"] = adoption["task_ref"].clone();
                    a.as_object_mut().unwrap().remove("canonical_identity");
                }
                self.target(&a, !present(&a["task_ref"]), false)?;
                let mut p = pick(
                    &a,
                    &[
                        "prompt",
                        "approved",
                        "task_mode",
                        "task_action",
                        "task_ref",
                        "project",
                        "title",
                        "summary",
                        "workspace_contract",
                        "model",
                        "reasoning_effort",
                        "reasoning",
                        "query",
                        "execution_mode",
                        "network_access",
                        "execution_surface",
                        "required_capabilities",
                        "operation_classes",
                        "production_mutation_intent",
                        "requested_operations",
                        "codex_uri",
                        "thread_id",
                    ],
                );
                p["host"] = json!(self.config.node_id);
                self.backend.invoke("prepare_execution", p)?
            }
            "start" => {
                let p = self.backend.prepared(a["prepared_execution_ref"].clone())?;
                if p["host"] != self.config.node_id {
                    return Err("NODE_TARGET_MISMATCH".into());
                }
                let mut target = pick(&p, &["host", "project"]);
                if p["new_task"] != true || present(&p["execution_ref"]) {
                    target["task_ref"] = p["task_ref"].clone();
                }
                self.target(&target, p["new_task"] == true, false)?;
                let reference = a["prepared_execution_ref"]
                    .as_str()
                    .ok_or("CALL_ARGUMENT_INVALID")?;
                let expected = json!(format!(
                    "exec_{}",
                    reference.strip_prefix("prepared_").unwrap_or(reference)
                ));
                if !matches_optional(&a["execution_ref"], &expected) {
                    return Err("EXECUTION_TARGET_DENIED".into());
                }
                let mut r = self.backend.invoke("start_execution", json!({"prepared_execution_ref":reference,"approved":a.get("approved").cloned().unwrap_or(json!(false)),"expected_request_hash":a["expected_request_hash"],"expected_task_ref":a["expected_task_ref"]}))?;
                r["operation_state"] = json!("QUEUED");
                r
            }
            "status" | "reconcile" => {
                let mut r = self.status(&a)?;
                if operation == "reconcile" {
                    r["reconciliation"] = json!("RUST_TRANSPORT_PENDING");
                }
                r
            }
            "cancel" => {
                let status = self.status(&a)?;
                self.target(&json!({"task_ref":status["task_ref"]}), false, true)?;
                let mut r = self.backend.invoke("cancel_execution", json!({"execution_ref":status["execution_ref"],"approved":a.get("approved").cloned().unwrap_or(json!(false))}))?;
                r["operation_state"] = json!(if r["status"] == "CANCELLED"
                    && r["resource_state"] == "RELEASED"
                {
                    "CANCELLED"
                } else {
                    "CANCELLATION_PENDING"
                });
                r
            }
            "context" => {
                self.target(&a, false, false)?;
                self.backend.invoke(
                    "get_context",
                    pick(
                        &a,
                        &[
                            "execution_ref",
                            "task_ref",
                            "thread_id",
                            "codex_uri",
                            "host",
                            "project",
                            "node_id",
                            "query",
                            "recent_turns",
                            "max_bytes",
                            "cursor",
                        ],
                    ),
                )?
            }
            "adopt" => {
                if some(&a["canonical_identity"]) {
                    self.canonical(&a)?
                } else {
                    self.target(&a, true, false)?;
                    self.backend.invoke(
                        "adopt_conversation",
                        pick(
                            &a,
                            &[
                                "host",
                                "project",
                                "thread_id",
                                "title",
                                "summary",
                                "conversation_ref",
                                "query",
                                "codex_uri",
                                "exact_turn_id",
                                "workspace_contract",
                            ],
                        ),
                    )?
                }
            }
            _ => return Err("NODE_OPERATION_UNSUPPORTED".into()),
        };
        if !r.is_object() {
            return Err("INVALID_NODE_RESULT".into());
        }
        r["node_id"] = json!(self.config.node_id);
        Ok(r)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;
    struct Golden(VecDeque<Value>);
    impl Golden {
        fn call(&mut self, method: &str, arguments: Value) -> ApiResult {
            let expected = self.0.pop_front().expect("unexpected additional callback");
            assert_eq!(expected["method"], method);
            assert_eq!(expected["arguments"], arguments);
            Ok(expected["result"].clone())
        }
    }
    impl Backend for Golden {
        fn invoke(&mut self, method: &str, args: Value) -> ApiResult {
            self.call(method, args)
        }
        fn task(&mut self, r: Value) -> ApiResult {
            self.call("task", json!({"reference":r}))
        }
        fn prepared(&mut self, r: Value) -> ApiResult {
            self.call("prepared", json!({"reference":r}))
        }
    }
    #[test]
    fn original_python_node_decisions_and_exact_callback_sequences() {
        let golden: Value =
            serde_json::from_str(include_str!("../../fixtures/node_api_v1.json")).unwrap();
        for row in golden["vectors"].as_array().unwrap() {
            let config = serde_json::from_value(row["config"].clone()).unwrap();
            let mut node = Node {
                config,
                backend: Golden(row["calls"].as_array().unwrap().iter().cloned().collect()),
            };
            let result = node.invoke(row["operation"].as_str().unwrap(), &row["arguments"]);
            if let Some(e) = row.get("error") {
                assert_eq!(result.unwrap_err().code, e.as_str().unwrap(), "{row}");
            } else {
                assert_eq!(result.unwrap(), row["result"], "{row}");
            }
            assert!(node.backend.0.is_empty(), "missed callback: {row}");
        }
    }
}
