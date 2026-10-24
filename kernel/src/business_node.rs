//! Centre-attested node requests. Identity, row ownership and receipts are
//! checked by the authority; the network gateway never writes business state.
use super::*;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Request {
    pub scope: Scope,
    pub request_id: String,
    pub operation: String,
    pub payload: Value,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Scope {
    pub principal: Value,
    pub projects: Vec<String>,
}

impl Request {
    pub fn validate(&self) -> Result<()> {
        if !core::keys(&self.scope.principal, "node_id user_scope fingerprint")
            || !self.payload.is_object()
            || self.scope.projects.len() > 64
        {
            return Err("INVALID_NODE_SCOPE".into());
        }
        for key in ["node_id", "user_scope", "fingerprint"] {
            let s = string(&self.scope.principal, key)?;
            if s.len() > 256 || s.chars().any(char::is_control) {
                return Err("INVALID_NODE_SCOPE".into());
            }
        }
        for cwd in &self.scope.projects {
            if cwd != "*" && (!Path::new(cwd).is_absolute() || cwd.contains('\0')) {
                return Err("INVALID_NODE_SCOPE".into());
            }
        }
        if !matches!(
            self.operation.as_str(),
            "adopt"
                | "prepare"
                | "start"
                | "cancel"
                | "get_status"
                | "get_prepared"
                | "list_tasks"
                | "list_projects"
                | "read_receipt"
        ) {
            return Err("NODE_OPERATION_DENIED".into());
        }
        self.scope.operation_id(&self.request_id)?;
        Ok(())
    }
}

impl Scope {
    pub fn operation_id(&self, request: &str) -> Result<String> {
        if request.is_empty() || request.len() > 128 || request.chars().any(char::is_control) {
            return Err("INVALID_OPERATION_ID".into());
        }
        // Payload is deliberately absent: same identity/id with changed intent
        // must conflict, not generate an independent command. Key rotation also
        // cannot silently create a new execution under the same request id.
        Ok(format!(
            "node_{}",
            core::digest(&json!([
                self.principal["node_id"],
                self.principal["user_scope"],
                request
            ]))
        ))
    }

    fn cwd(&self, value: &Value) -> Result<()> {
        let cwd = string(value, "cwd")?;
        if !self.projects.iter().any(|p| p == "*" || p == cwd) {
            return Err("NODE_PROJECT_SCOPE_DENIED".into());
        }
        Ok(())
    }

    pub fn owned(&self, value: &Value) -> Result<()> {
        if value["node_principal"] != self.principal || value["host"] != self.principal["node_id"] {
            return Err("NODE_OWNER_CONFLICT".into());
        }
        self.cwd(&value["workspace_contract"])
    }

    fn execution(&self, c: &Connection, p: &Value) -> Result<()> {
        let e = row(c, "execution", string(p, "execution_ref")?)?;
        self.owned(&row(c, "prepared", string(&e, "prepared_execution_ref")?)?)
    }

    pub fn authorize(&self, c: &Connection, policy: &Policy, op: &str, p: &Value) -> Result<()> {
        match op {
            "prepare" | "adopt" => {
                if p["host"] != self.principal["node_id"] {
                    return Err("NODE_OWNER_CONFLICT".into());
                }
                let project = policy.project(string(p, "project")?, string(p, "host")?)?;
                self.cwd(&json!({"cwd":project.cwd}))?;
                if !p["workspace_contract"].is_null() {
                    self.cwd(&p["workspace_contract"])?;
                }
                if let Some(id) = p["task_ref"].as_str() {
                    self.owned(&row(c, "task", id)?)?;
                }
                if op == "adopt" {
                    let identity = &p["canonical_identity"];
                    let provenance: Value =
                        serde_json::from_str(string(identity, "first_provenance_json")?)
                            .map_err(|_| "NODE_OWNER_CONFLICT")?;
                    if identity["node_id"] != self.principal["node_id"]
                        || identity["user_scope"] != self.principal["user_scope"]
                        || provenance["node_fingerprint"] != self.principal["fingerprint"]
                    {
                        return Err("NODE_OWNER_CONFLICT".into());
                    }
                    let id = format!(
                        "adopt_{}",
                        core::digest(&json!([p["host"], p["thread_id"]]))
                    );
                    if let Ok(old) = row(c, "prepared", &id) {
                        self.owned(&old)?;
                    }
                    for task in rows(c, "task")? {
                        if task["host"] == p["host"] && thread_bound(&task, &p["thread_id"]) {
                            self.owned(&task)?;
                        }
                    }
                }
            }
            "start" | "get_prepared" => {
                self.owned(&row(c, "prepared", string(p, "prepared_execution_ref")?)?)?;
                if !p["native_cwd"].is_null() {
                    self.cwd(&json!({"cwd":p["native_cwd"]}))?;
                }
            }
            "cancel" | "get_status" => self.execution(c, p)?,
            "list_tasks" | "list_projects" if core::keys(p, "") => (),
            "list_tasks"
                if core::keys(p, "projection after")
                    && p["projection"] == "summary"
                    && p["after"].is_string() =>
            {
                ()
            }
            "read_receipt" if core::keys(p, "operation_id") => (),
            _ => return Err("NODE_OPERATION_DENIED".into()),
        }
        Ok(())
    }

    pub fn read(
        &self,
        c: &Connection,
        policy: &Policy,
        op: &str,
        p: &Value,
    ) -> Result<Option<Value>> {
        Ok(Some(match op {
            "list_tasks" => {
                let values: Vec<_> = rows(c, "task")?
                    .into_iter()
                    .filter(|t| self.owned(t).is_ok())
                    .collect();
                if p["projection"] == "summary" {
                    let after = p["after"].as_str().ok_or("INVALID_CURSOR")?;
                    task_summary_page(
                        values
                            .into_iter()
                            .filter(|v| v["task_id"].as_str().is_some_and(|id| id > after))
                            .take(65)
                            .collect(),
                        "task_id",
                    )?
                } else {
                    json!({"tasks":values,"read_only":true})
                }
            }
            "list_projects" => json!({"projects":policy.projects.iter().filter(|p|
                p.host == self.principal["node_id"] && self.cwd(&json!({"cwd":p.cwd})).is_ok()
            ).collect::<Vec<_>>()}),
            "read_receipt" => {
                let id = self.operation_id(string(p, "operation_id")?)?;
                let body: Option<String> = c
                    .query_row(
                        "SELECT body FROM business_commands WHERE id=? AND role='API'",
                        [&id],
                        |r| r.get(0),
                    )
                    .optional()
                    .map_err(db)?;
                match body {
                    None => json!({"verdict":"NOT_FOUND"}),
                    Some(body) => {
                        let body = parse(body)?;
                        if body["node_principal"] != self.principal {
                            return Err("NODE_OWNER_CONFLICT".into());
                        }
                        let request = &body["node_request"];
                        self.authorize(
                            c,
                            policy,
                            string(request, "operation")?,
                            &request["payload"],
                        )?;
                        for change in body["changes"]
                            .as_array()
                            .ok_or("CORRUPT_BUSINESS_CHANGE")?
                        {
                            if !change["value"]["node_principal"].is_null() {
                                self.owned(&change["value"])?;
                            }
                        }
                        json!({"verdict":"COMMITTED","operation_id":p["operation_id"],
                            "hash":core::digest(&body),"result":body["result"]})
                    }
                }
            }
            _ => return Ok(None),
        }))
    }
}

pub(super) fn inherit(source: &Value, target: &mut Value) {
    if let Some(principal) = source.get("node_principal") {
        target["node_principal"] = principal.clone();
    }
}
