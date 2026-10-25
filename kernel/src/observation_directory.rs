//! Read-only ObservationDirectory assembly. Queries, cursors, scope intersection
//! and exact-context rechecks reuse the existing contracts. Never initializes a
//! database/identity, writes a trust timestamp, or starts an execution Provider.
use crate::{
    discovery_identity, discovery_state, mcp_api,
    native_transport::{Authority, UnixAuthority},
    node_registry::Registry,
    node_scope,
    node_tls::{self, Result},
    read_projection::{text, truth},
};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

pub struct Directory {
    config: Value,
    managed: Option<mcp_api::Config>,
    bridge: crate::native_observer_bridge::Runtime,
}
fn path(value: &str) -> Result<PathBuf> {
    std::path::absolute(crate::runtime_config::expanduser(Path::new(value))?)
        .map_err(|_| "OBSERVATION_CONFIG_INVALID".into())
}
fn required(name: &str) -> Result<String> {
    std::env::var(name).map_err(|_| "RUST_OBSERVATION_CONFIG_REQUIRED".into())
}
impl Directory {
    pub fn from_env() -> Result<Option<Self>> {
        let Some(db) = std::env::var("CLINX_OBSERVATION_DB")
            .ok()
            .filter(|s| !s.is_empty())
        else {
            return Ok(None);
        };
        let native = std::env::var("CLINX_OBSERVATION_NATIVE_HOME")
            .ok()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "~/.codex".into());
        Self::new(
            json!({"path":db,"state":required("CLINX_OBSERVATION_STATE")?,
            "registry":required("CLINX_OBSERVATION_REGISTRY")?,
            "user_scope":required("CLINX_OBSERVATION_USER_SCOPE")?,
            "node_id":std::env::var("CLINX_OBSERVATION_NODE_ID").ok(),
            "native_home":native,"canonical_path":std::env::var("CLINX_CANONICAL_TASK_DB").ok()}),
        )
        .map(Some)
    }
    /// Explicit config is used by isolated qualifications and the native CLI.
    pub fn new(mut config: Value) -> Result<Self> {
        for field in ["path", "state", "registry"] {
            config[field] = json!(path(
                config[field].as_str().ok_or("OBSERVATION_CONFIG_INVALID")?
            )?);
        }
        for field in ["native_home", "canonical_path"] {
            if truth(&config[field]) {
                config[field] = json!(path(text(&config[field]))?);
            }
        }
        config["user_scope"]
            .as_str()
            .ok_or("OBSERVATION_CONFIG_INVALID")?;
        let root = Path::new(text(&config["state"]));
        // This read validates the directory, even if no identity has been paired.
        let raw = discovery_identity::read(root, "identity.json")?;
        config["local_identity"] = raw
            .as_ref()
            .map(discovery_identity::public_record)
            .transpose()?
            .unwrap_or(Value::Null);
        Ok(Self {
            config,
            managed: None,
            bridge: Default::default(),
        })
    }
    pub fn with_managed(mut self, config: mcp_api::Config) -> Self {
        self.managed = Some(config);
        self
    }
    pub fn config(&self) -> &Value {
        &self.config
    }
    fn root(&self) -> &Path {
        Path::new(text(&self.config["state"]))
    }
    fn registry(&self, method: &str, arguments: Value) -> Result<Value> {
        Registry::read(
            &json!({"path":self.config["registry"],"method":format!("registry.{method}"),"arguments":arguments}),
        )
    }
    fn approval(&self, node: &str) -> Result<Value> {
        let state =
            discovery_identity::read(self.root(), "sharing.json")?.unwrap_or(json!({"scopes":{}}));
        if !state["scopes"].is_object() {
            return Err("INVALID_SHARING_STORE".into());
        }
        node_scope::optional(&state["scopes"][node])
            .map(|s| s.map(|s| s.value()).unwrap_or(Value::Null))
    }
    pub fn node_facts(&self, node: &str) -> Result<Value> {
        let local = &self.config["local_identity"];
        if !local.is_null() {
            let current = discovery_identity::read(self.root(), "identity.json")?
                .map(|v| discovery_identity::public_record(&v))
                .transpose()?
                .ok_or("NODE_IDENTITY_MISMATCH")?;
            if ["node_id", "fingerprint", "public_key"]
                .iter()
                .any(|k| current[*k] != local[*k])
            {
                return Err("NODE_IDENTITY_MISMATCH".into());
            }
        }
        let peers = discovery_state::Runtime::default().invoke(
            "discovery.peers.all",
            &json!({"root":self.root()}),
            |_, _| Err("OBSERVATION_CALLBACK_INVALID".into()),
        )?;
        let record = self.registry("get", json!({"node_id": node}))?;
        if !record.is_null()
            && peers[node]["trust_state"] == "TRUSTED"
            && record["public_key_fingerprint"] != peers[node]["fingerprint"]
        {
            return Err("NODE_IDENTITY_MISMATCH".into());
        }
        Ok(json!({"record":record,
            "scope":self.registry("scope",json!({"node_id":node,"user_scope":self.config["user_scope"]}))?,
            "approval":self.approval(node)?,"peer":peers[node]}))
    }
    fn remote(&self, request: &Value, node: &str) -> Result<Value> {
        let identity =
            discovery_identity::read(self.root(), "identity.json")?.ok_or("PAIRING_REQUIRED")?;
        let configured = self.config["node_id"]
            .as_str()
            .ok_or("RUST_OBSERVATION_CONFIG_REQUIRED")?;
        let normalized = crate::execution_contracts::command(
            "execution_contract.host",
            &json!({"value":configured}),
        )?;
        discovery_identity::validate(&identity, text(&normalized["stable_identifier"]))?;
        let record = self.registry("get", json!({"node_id":node}))?;
        let approval = self.approval(node)?;
        let result = crate::node_observation_router::handle(
            "router.remote_request",
            &json!({"node_id":node,"request":request,"record":record,"scope":approval}),
            |op, a| match op {
                "remote.peer" => node_tls::peer_readonly(self.root(), text(&a["node_id"])),
                "remote.scope" => self.registry("scope", a),
                "remote.request" => node_tls::call_readonly(
                    self.root(),
                    text(&a["node_id"]),
                    text(&a["endpoint"]),
                    3.,
                    &a["request"],
                ),
                _ => Err("NODE_CALLBACK_INVALID".into()),
            },
        );
        // Existing AuthorizedRemoteClient returns route failures as read-only
        // response envelopes. No alternate node or endpoint is tried.
        Ok(result.unwrap_or_else(|code| {
            json!({"error_code":code,"unavailable_reason":match code.as_str(){
            "SHARING_SCOPE_DENIED"=>"Current centre approval denies this request",
            "NODE_OFFLINE"=>"Registered node is offline",
            "USER_SCOPE_DENIED"=>"Request has no approved user mapping",
            _=>"Remote route rejected"},"node_id":node,"read_only":true})
        }))
    }
    fn ordinary(&self, method: &str, arguments: &Value) -> Result<Value> {
        let mut args = self.config.clone();
        args["request"] = arguments.clone();
        args["remote"] = json!(true);
        args["node"] = arguments
            .get("node_id")
            .or_else(|| arguments.get("node"))
            .cloned()
            .unwrap_or(Value::Null);
        crate::observation::invoke(&format!("observation.{method}"), &args, |op, a| match op {
            "directory.nodes" => self.registry(
                "list_nodes",
                json!({"user_scope":self.config["user_scope"]}),
            ),
            "directory.node" => self.node_facts(text(&a["node_id"])),
            "directory.remote" => self.remote(&a["request"], text(&a["record"]["node_id"])),
            _ => Err("OBSERVATION_CALLBACK_INVALID".into()),
        })
    }
    fn exact_context(&self, config: &mcp_api::Config, args: Value) -> Result<Value> {
        let mut authority = UnixAuthority {
            endpoint: PathBuf::from(required("CLINX_RUST_SOCKET")?),
            uid: required("CLINX_RUST_UID")?
                .parse()
                .map_err(|_| "RUST_AUTHORITY_CONFIG_REQUIRED")?,
        };
        if authority.call("health", json!({}), "health")?["business_commands"] != true {
            return Err("RUST_BUSINESS_AUTHORITY_REQUIRED".into());
        }
        let mut api = mcp_api::Api {
            authority,
            reads: mcp_api::NativeReads(config.clone()),
            config: config.clone(),
        };
        api.invoke("clinx_get_context", &args)
            .map(crate::mcp::public_json)
            .map_err(|e| e.code)
    }
    pub fn invoke(&self, method: &str, arguments: &Value) -> Result<Value> {
        if !arguments.is_object() {
            return Err("INVALID_OBSERVATION_QUERY".into());
        }
        let method = match method {
            "clinx_list_observations" => "list",
            "clinx_get_observation" => "detail",
            "clinx_get_observation_context" => "context",
            "clinx_get_observation_activity" => "activity",
            method => method,
        };
        if !["list", "detail", "context", "activity", "scope"].contains(&method) {
            return Err("OBSERVATION_READ_ONLY".into());
        }
        if method == "context"
            && let Some(config) = &self.managed
        {
            let mut args = arguments.clone();
            args["runtime_host"] = json!(config.runtime_host);
            return self.bridge.invoke(
                "native_observer.managed_context",
                &args,
                |op, a| match op {
                    "managed.detail" => self.ordinary("detail", &a),
                    "managed.remote_context" => self.ordinary("context", &a),
                    "managed.desktop_context" => crate::observer_runtime::desktop_read_remote(&a),
                    "managed.exact_context" => self.exact_context(config, a),
                    _ => Err("NATIVE_OBSERVER_CALLBACK_INVALID".into()),
                },
            );
        }
        self.ordinary(method, arguments)
    }
}
