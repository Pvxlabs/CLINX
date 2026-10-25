//! Newline JSON-RPC MCP boundary. Business dispatch and reads stay in Rust.
use crate::{
    mcp_api::{Api, Config, Error, Reads},
    native_transport::Authority,
    provider::Result,
};
use serde_json::{Value, json};
use std::path::Path;

pub fn registry() -> Value {
    serde_json::from_str(include_str!("../../clinx/mcp/registry.json"))
        .expect("compiled MCP registry")
}
pub fn public_json(v: Value) -> Value {
    match v {
        Value::Object(o) => Value::Object(
            o.into_iter()
                .filter(|(k, _)| {
                    ![
                        "turn_ids",
                        "item_ids",
                        "message_ids",
                        "execution_ids",
                        "task_id",
                        "taskId",
                        "origin",
                        "repository_origin",
                        "branch",
                        "raw_result",
                        "credentials",
                        "token",
                        "api_key",
                        "authorization",
                    ]
                    .contains(&k.as_str())
                })
                .map(|(k, v)| {
                    let preserved = v.as_object().is_some_and(|o| {
                        if k == "workspace_contract" {
                            o.contains_key("cwd")
                                && o.keys()
                                    .all(|k| ["cwd", "branch", "head"].contains(&k.as_str()))
                        } else if k == "workspace_resolution" {
                            o.keys().all(|k| {
                                [
                                    "cwd",
                                    "branch",
                                    "head",
                                    "repository_identity",
                                    "repository_root",
                                    "repository_common_dir",
                                    "worktree",
                                    "observer_uid",
                                    "observer_source",
                                ]
                                .contains(&k.as_str())
                            })
                        } else {
                            false
                        }
                    });
                    (k, if preserved { v } else { public_json(v) })
                })
                .collect(),
        ),
        Value::Array(a) => Value::Array(a.into_iter().map(public_json).collect()),
        _ => v,
    }
}
pub fn tool_result(v: Value, error: bool) -> Value {
    json!({"resultType":"complete","content":[{"type":"text","text":serde_json::to_string(&v).unwrap()}],"structuredContent":v,"isError":error})
}
fn public_code(code: &str) -> String {
    let code = code.split(':').next().unwrap_or("UNKNOWN");
    if code.len() <= 160
        && !code.is_empty()
        && code
            .bytes()
            .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'_')
    {
        code.to_owned()
    } else {
        "UNKNOWN".into()
    }
}
pub fn tool_error(e: Error, name: &str, a: &Value) -> Value {
    let code = public_code(&e.code);
    if e.provider {
        // Read errors cannot justify another execution. Do not reflect upstream
        // bodies, credentials, prompts or arbitrary exception text.
        let mut correlation = json!({"tool":name,"request_ref":a.get("prepared_reauthorization_ref").or_else(||a.get("prepared_execution_ref")).or_else(||a.get("request_ref"))});
        for k in ["task_ref", "execution_ref"] {
            if let Some(v) = a.get(k) {
                correlation[k] = v.clone()
            }
        }
        return json!({"error":code,"error_type":"ProviderReadError","failure_code":code,"failure_stage":"APP_SERVER_REQUEST","failure_source":"PROVIDER","failure_reason":code,"server_received":true,"caller_approval":"NOT_OBSERVED","correlation":correlation,"side_effect_certainty":"UNKNOWN","outcome_certainty":"UNKNOWN","retry_allowed":false,"retry_required":false,"reconciliation_required":true,"codex_running":null,"recovery_action":"READ_EXACT_REQUEST_AND_EXECUTION; DO_NOT_REPLAY"});
    }
    json!({"error_code":code,"operation_id":e.operation_id,"authority":"RUST","retry_required":false,"reconciliation_required":e.code=="DELIVERY_UNKNOWN"})
}
/// Only the existing four read-only directory operations cross this boundary.
/// The native directory runs in the same API identity, without another listener.
pub trait Observations {
    fn only(&self) -> bool {
        false
    }
    fn enabled(&self) -> bool;
    fn call(&mut self, name: &str, args: &Value) -> Result<Value>;
}
pub struct NoObservations;
impl Observations for NoObservations {
    fn enabled(&self) -> bool {
        false
    }
    fn call(&mut self, _: &str, _: &Value) -> Result<Value> {
        Err("OBSERVATION_NOT_CONFIGURED".into())
    }
}
pub struct Directory {
    inner: crate::observation_directory::Directory,
    only: bool,
}
impl Directory {
    pub fn new(config: &Path) -> Result<Self> {
        let inner = crate::observation_directory::Directory::from_env()?
            .ok_or("RUST_OBSERVATION_CONFIG_REQUIRED")?
            .with_managed(load_config(config)?);
        Ok(Self { inner, only: false })
    }
    pub fn standalone() -> Result<Self> {
        Ok(Self {
            inner: crate::observation_directory::Directory::from_env()?
                .ok_or("OBSERVATION_NOT_CONFIGURED")?,
            only: true,
        })
    }
}
impl Observations for Directory {
    fn enabled(&self) -> bool {
        true
    }
    fn only(&self) -> bool {
        self.only
    }
    fn call(&mut self, name: &str, args: &Value) -> Result<Value> {
        self.inner.invoke(name, args).and_then(|v| {
            if v["read_only"] == true {
                Ok(v)
            } else {
                Err("OBSERVATION_UNAVAILABLE".into())
            }
        })
    }
}
impl<T: Observations + ?Sized> Observations for Box<T> {
    fn only(&self) -> bool {
        (**self).only()
    }
    fn enabled(&self) -> bool {
        (**self).enabled()
    }
    fn call(&mut self, n: &str, a: &Value) -> Result<Value> {
        (**self).call(n, a)
    }
}

pub struct Server<A: Authority, R: Reads, O: Observations> {
    pub api: Api<A, R>,
    pub observations: O,
}
impl<A: Authority, R: Reads, O: Observations> Server<A, R, O> {
    pub fn handle(&mut self, request: Value) -> Option<Value> {
        let r = self.dispatch(&request);
        match r {
            Ok(r) => {
                if request["id"].is_null() || request["method"] == "notifications/initialized" {
                    None
                } else {
                    Some(json!({"jsonrpc":"2.0","id":request["id"],"result":r}))
                }
            }
            Err((code, message)) => {
                Some(json!({"jsonrpc":"2.0","id":null,"error":{"code":code,"message":message}}))
            }
        }
    }
    fn dispatch(&mut self, r: &Value) -> std::result::Result<Value, (i32, String)> {
        let err = |code, msg: &str| (code, msg.to_owned());
        if !r.is_object() {
            return Err(err(-32600, "request must be an object"));
        }
        let method = r["method"]
            .as_str()
            .ok_or_else(|| err(-32600, "method is required"))?;
        let params = if r["params"].is_null() {
            json!({})
        } else {
            r["params"].clone()
        };
        if !params.is_object() {
            return Err(err(-32602, "params must be an object"));
        }
        let registry = registry();
        match method {
            "notifications/initialized" | "ping" => Ok(json!({})),
            "initialize" => Ok(
                json!({"protocolVersion":registry["protocol"],"capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":registry["server_name"],"version":registry["server_version"]},"instructions":registry["instructions"]}),
            ),
            "server/discover" => Ok(
                json!({"resultType":"complete","supportedVersions":[registry["discover_protocol"]],"capabilities":{"tools":{}},"_meta":{"io.modelcontextprotocol/serverInfo":{"name":registry["server_name"],"version":registry["server_version"]}},"instructions":registry["instructions"],"ttlMs":3600000,"cacheScope":"public"}),
            ),
            "tools/list" => {
                let mut tools = if self.observations.only() {
                    vec![]
                } else {
                    registry["tools"].as_array().unwrap().clone()
                };
                if self.observations.enabled() {
                    tools.extend(registry["observations"].as_array().unwrap().iter().cloned())
                }
                Ok(
                    json!({"resultType":"complete","tools":tools,"ttlMs":3600000,"cacheScope":"public"}),
                )
            }
            "tools/call" => {
                let name = params["name"]
                    .as_str()
                    .ok_or_else(|| err(-32602, "tools/call requires name"))?;
                let obs = registry["observations"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|t| t["name"] == name);
                let tool = registry["tools"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .chain(registry["observations"].as_array().unwrap())
                    .find(|t| t["name"] == name);
                if tool.is_none()
                    || (obs && !self.observations.enabled())
                    || (!obs && self.observations.only())
                {
                    return Err(err(
                        -32602,
                        &format!(
                            "{}: {name}",
                            if name == "clinx_execute" {
                                "tool is not exposed"
                            } else {
                                "unknown tool"
                            }
                        ),
                    ));
                }
                let args = params.get("arguments").cloned().unwrap_or(json!({}));
                if !args.is_object() {
                    return Err(err(-32602, "tool arguments must be an object"));
                }
                let allowed = &tool.unwrap()["inputSchema"]["properties"];
                if name != "clinx_get_capabilities"
                    && args.as_object().unwrap().keys().any(|k| {
                        allowed.get(k).is_none()
                            && !(name == "clinx_find_task" && k == "task_ref")
                            && !(name == "clinx_adopt_conversation"
                                && ["canonical_identity", "conversation_ref", "query"]
                                    .contains(&k.as_str()))
                    })
                {
                    return Ok(tool_result(
                        tool_error("CALL_ARGUMENT_INVALID".into(), name, &args),
                        true,
                    ));
                }
                if obs {
                    return Ok(match self.observations.call(name, &args) {
                        Ok(v) => tool_result(v, false),
                        Err(code) => tool_result(
                            json!({"error_code":public_code(&code),"error":public_code(&code),"read_only":true}),
                            true,
                        ),
                    });
                }
                Ok(match self.api.invoke(name, &args) {
                    Ok(v) => tool_result(public_json(v), false),
                    Err(e) => tool_result(tool_error(e, name, &args), true),
                })
            }
            _ => Err(err(-32601, &format!("method not found: {method}"))),
        }
    }
}
pub fn load_config(path: &Path) -> Result<Config> {
    let data = std::fs::read(path).map_err(|_| "MCP_CONFIG_REQUIRED")?;
    let cfg: Config = serde_json::from_slice(&data).map_err(|_| "MCP_CONFIG_INVALID")?;
    if cfg.runtime_host.is_empty()
        || !cfg.provider_endpoint.is_absolute()
        || !cfg.provider_timeout_seconds.is_finite()
        || cfg.provider_timeout_seconds <= 0.0
    {
        return Err("MCP_CONFIG_INVALID".into());
    }
    Ok(cfg)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::provider::Result;
    use std::collections::VecDeque;
    struct Commands(VecDeque<std::result::Result<Value, String>>);
    impl Authority for Commands {
        fn call(&mut self, _: &str, _: Value, _: &str) -> Result<Value> {
            self.0.pop_front().expect("unexpected command")
        }
    }
    struct Reader;
    impl Reads for Reader {
        fn thread(&mut self, _: &str) -> Result<Value> {
            panic!("unexpected native read")
        }
        fn page(&mut self, _: &str, _: u64, _: Option<&str>) -> Result<Value> {
            panic!("unexpected native page")
        }
        fn desktop(
            &mut self,
            _: &str,
            _: bool,
            _: u64,
            _: usize,
            _: Option<&str>,
        ) -> Result<Value> {
            panic!("unexpected native history")
        }
    }
    struct Obs;
    impl Observations for Obs {
        fn enabled(&self) -> bool {
            true
        }
        fn call(&mut self, n: &str, a: &Value) -> Result<Value> {
            assert_eq!(n, "clinx_list_observations");
            assert_eq!(*a, json!({"limit":1}));
            Ok(json!({"read_only":true,"items":[],"schema_version":"clinx-observation-v1"}))
        }
    }
    fn config() -> Config {
        serde_json::from_value(json!({"runtime_host":"p620","provider_endpoint":"/socket","provider_timeout_seconds":3,"host_aliases":{"p620":["p620"]},"codex_host_ids":{}})).unwrap()
    }
    #[test]
    fn protocol_registry_notifications_and_observation_boundary() {
        let api = Api {
            authority: Commands(VecDeque::new()),
            reads: Reader,
            config: config(),
        };
        let mut server = Server {
            api,
            observations: Obs,
        };
        let init = server
            .handle(json!({"id":1,"method":"initialize"}))
            .unwrap();
        assert_eq!(init["result"]["protocolVersion"], "2025-06-18");
        assert_eq!(init["result"]["instructions"], registry()["instructions"]);
        assert!(
            server
                .handle(json!({"method":"notifications/initialized"}))
                .is_none()
        );
        let tools = server
            .handle(json!({"id":2,"method":"tools/list"}))
            .unwrap();
        assert_eq!(tools["result"]["tools"].as_array().unwrap().len(), 19);
        let mut expected = registry()["tools"].as_array().unwrap().clone();
        expected.extend(
            registry()["observations"]
                .as_array()
                .unwrap()
                .iter()
                .cloned(),
        );
        assert_eq!(tools["result"]["tools"], json!(expected));
        let discover = server
            .handle(json!({"id":3,"method":"server/discover"}))
            .unwrap();
        assert_eq!(
            discover["result"]["supportedVersions"],
            json!(["2026-07-28"])
        );
        let r=server.handle(json!({"id":4,"method":"tools/call","params":{"name":"clinx_list_observations","arguments":{"limit":1}}})).unwrap();
        assert_eq!(r["result"]["structuredContent"]["read_only"], true);
        assert_eq!(r["result"]["isError"], false);
        assert_eq!(
            server
                .handle(json!({"id":5,"method":"tools/call","params":{"name":"clinx_execute"}}))
                .unwrap()["error"]["code"],
            -32602
        );
        assert_eq!(server.handle(json!([])).unwrap()["error"]["code"], -32600);
        assert_eq!(server.handle(json!({"id":6,"method":"tools/call","params":{"name":"clinx_get_context","arguments":[]}})).unwrap()["error"]["code"],-32602);
    }
    #[test]
    fn unknown_delivery_preserves_operation_identity_and_never_retries() {
        let api = Api {
            authority: Commands(VecDeque::from([Err("AUTHORITY_DELIVERY_UNKNOWN".into())])),
            reads: Reader,
            config: config(),
        };
        let mut server = Server {
            api,
            observations: NoObservations,
        };
        let r=server.handle(json!({"id":1,"method":"tools/call","params":{"name":"clinx_cancel_execution","arguments":{"execution_ref":"exec","approved":true}}})).unwrap();
        let p = &r["result"]["structuredContent"];
        assert_eq!(p["error_code"], "DELIVERY_UNKNOWN");
        assert_eq!(p["reconciliation_required"], true);
        assert_eq!(p["retry_required"], false);
        assert_eq!(p["operation_id"].as_str().unwrap().len(), 32);
        assert!(server.api.authority.0.is_empty());
        assert_eq!(r["result"]["isError"], true);
        let tools = server
            .handle(json!({"id":2,"method":"tools/list"}))
            .unwrap();
        assert_eq!(tools["result"]["tools"].as_array().unwrap().len(), 15);
    }
    #[test]
    fn public_scrub_preserves_exact_native_and_workspace_seals() {
        let input = json!({"thread_id":"thread","turn_id":"turn","cwd":"/cwd","task_id":"internal","authorization":"secret","nested":{"token":"secret","branch":"internal","value":1},"workspace_contract":{"cwd":"/cwd","branch":"sealed","head":"abc"},"workspace_resolution":{"cwd":"/cwd","branch":"observed","observer_uid":1000}});
        let v = public_json(input);
        assert_eq!(v["thread_id"], "thread");
        assert_eq!(v["turn_id"], "turn");
        assert_eq!(v["workspace_contract"]["branch"], "sealed");
        assert_eq!(v["workspace_resolution"]["branch"], "observed");
        assert!(v.get("task_id").is_none());
        assert!(v["nested"].get("token").is_none());
        assert!(v["nested"].get("branch").is_none());
        let e = tool_error(
            Error {
                code: "PROVIDER: authorization bearer SECRET".into(),
                operation_id: None,
                provider: true,
            },
            "clinx_get_context",
            &json!({}),
        );
        assert!(!e.to_string().contains("SECRET"));
        assert_eq!(e["retry_required"], false);
    }
}
