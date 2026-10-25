//! Standalone read-only Observer assembly. HTTP, projections and native reads
//! share this process; no Python callbacks, authority mutation or Provider start.
pub mod history_guard;
use crate::{
    native_history, native_observer_bridge,
    node_tls::Result,
    observation_directory::Directory,
    observation_runtime::{self, Source},
    observer_http,
    provider::{Client, Provider},
    read_projection::{text, truth},
};
use regex::Regex;
use serde_json::{Value, json};
use std::{
    io::Write,
    path::Path,
    sync::{
        Arc, LazyLock, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};
static THREAD: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$").unwrap());
static HOST: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[A-Za-z0-9_.:-]+$").unwrap());

fn env(name: &str) -> Value {
    std::env::var(name)
        .ok()
        .filter(|s| !s.is_empty())
        .map_or(Value::Null, Value::String)
}
fn token_valid(value: &Value) -> bool {
    value.as_str().is_some_and(|s| {
        (32..=256).contains(&s.len())
            && s.bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
    })
}
fn codex_uri(a: &Value) -> Value {
    let tid = text(&a["thread_id"]);
    if !THREAD.is_match(tid) {
        return Value::Null;
    }
    let mut uri = format!("codex://threads/{tid}");
    if let Some(host) = a["host_id"].as_str() {
        uri.push_str("?hostId=");
        for byte in host.bytes() {
            if byte.is_ascii_alphanumeric() || b"-._~".contains(&byte) {
                uri.push(byte as char);
            } else {
                uri.push_str(&format!("%{byte:02X}"));
            }
        }
    }
    json!(uri)
}

pub struct Observer {
    config: Value,
    directory: Option<Directory>,
    bridge: native_observer_bridge::Runtime,
    probe: observation_runtime::Runtime,
    source: Mutex<Option<Arc<Source>>>,
}
impl Observer {
    pub fn from_env() -> Result<Arc<Self>> {
        let token = env("CLINX_OBSERVER_TOKEN");
        if !token_valid(&token) {
            return Err(
                "CLINX_OBSERVER_TOKEN must be a 32..256 character URL-safe credential".into(),
            );
        }
        let hosts: Value = serde_json::from_str(&std::env::var("CLINX_OBSERVER_CODEX_HOST_IDS").unwrap_or_else(|_| "{}".into()))
            .map_err(|_| "CLINX_OBSERVER_CODEX_HOST_IDS must map canonical hosts to configured Desktop host IDs")?;
        if !hosts.as_object().is_some_and(|m| {
            m.values()
                .all(|v| v.as_str().is_some_and(|s| HOST.is_match(s)))
        }) {
            return Err("CLINX_OBSERVER_CODEX_HOST_IDS must map canonical hosts to configured Desktop host IDs".into());
        }
        let port = std::env::var("CLINX_OBSERVER_PORT").unwrap_or_else(|_| "8766".into());
        let port = crate::python_numeric::integer_text(&port)
            .map_err(|_| "OBSERVER_HTTP_BIND_FAILED")?
            .parse::<u16>()
            .map_err(|_| "OBSERVER_HTTP_BIND_FAILED")?;
        let path = env("CLINX_OBSERVER_DB");
        let endpoint = env("CLINX_OBSERVER_RUST_MCP");
        if !truth(&endpoint) && !truth(&path) {
            return Err("CLINX_OBSERVER_DB must identify the existing canonical registry".into());
        }
        let home = std::env::var("HOME").map_err(|_| "NATIVE_INDEX_UNAVAILABLE")?;
        Self::new(
            json!({
                "credential":token,"path":path,"mcp":endpoint,"codex_host_ids":hosts,
                "port":port,"audit_methods":std::env::var("CLINX_OBSERVER_AUDIT_METHODS").ok().as_deref()==Some("1"),
                "native_home":Path::new(&home).join(".codex"),
                "native_observer_socket":env("CLINX_NATIVE_OBSERVER_SOCKET"),
                "probe_program":std::env::current_exe().ok(),
            }),
            Directory::from_env()?,
        )
    }
    pub fn new(mut config: Value, directory: Option<Directory>) -> Result<Arc<Self>> {
        if truth(&config["path"]) {
            config["path"] = json!(crate::read_projection::resolve(
                &crate::runtime_config::expanduser(Path::new(text(&config["path"])))?
            ));
        }
        let probe = observation_runtime::Runtime::default();
        probe.invoke(
            "source_runtime.init",
            &json!({"config":{
                "native_home":directory.as_ref().map(|d| &d.config()["native_home"]),
                "native_observer_socket":config["native_observer_socket"],
                "probe_program":config["probe_program"],
            }}),
            |_, _| Err("NATIVE_OBSERVER_CALLBACK_INVALID".into()),
        )?;
        Ok(Arc::new(Self {
            config,
            directory,
            bridge: Default::default(),
            probe,
            source: Default::default(),
        }))
    }
    fn facts(&self) -> Result<Value> {
        let Some(d) = &self.directory else {
            return Ok(json!({"local_identity":null,"native_home":null,"scope":null}));
        };
        let c = d.config();
        let scope = if truth(&c["local_identity"]) && truth(&c["native_home"]) {
            d.invoke("scope", &json!({"node_id":c["local_identity"]["node_id"]}))?
        } else {
            Value::Null
        };
        Ok(
            json!({"local_identity":c["local_identity"],"native_home":c["native_home"],"scope":scope}),
        )
    }
    fn history(&self, operation: &str, a: &Value) -> Result<Value> {
        let d = self.directory.as_ref().ok_or("SHARING_SCOPE_DENIED")?;
        let mut a = a.clone();
        a["root"] = d.config()["native_home"].clone();
        native_history::invoke(&format!("history.{operation}"), &a)
    }
    fn source(&self) -> Result<Arc<Source>> {
        let mut source = self
            .source
            .lock()
            .map_err(|_| "OBSERVATION_SOURCE_UNAVAILABLE")?;
        if source.is_none() {
            let d = self.directory.as_ref().ok_or("SHARING_SCOPE_DENIED")?;
            *source = Some(Arc::new(Source::new(json!({
                "native_home":d.config()["native_home"],"node_id":d.config()["local_identity"]["node_id"],
                "owner_host":d.config()["local_identity"]["node_id"],"native_writer_observation":true,
                "native_observer_socket":self.config["native_observer_socket"],"probe_program":self.config["probe_program"],
            }))?));
        }
        Ok(source.as_ref().unwrap().clone())
    }
    fn native_callback(&self, op: &str, mut a: Value) -> Result<Value> {
        match op {
            "native_observer.facts" | "desktop.facts" => self.facts(),
            "native_observer.generation" => self.source()?.invoke("generation", &a),
            "native_observer.inventory" => self.source()?.invoke("inventory", &a),
            "native_observer.sample" => self.source()?.invoke("sample", &a),
            "native_observer.metadata" => self.history("metadata", &a),
            "desktop.scope" => self
                .directory
                .as_ref()
                .ok_or("SHARING_SCOPE_DENIED")?
                .invoke("scope", &a),
            "desktop.metadata" | "desktop.status" | "desktop.context" => {
                a["tid"] = a["thread_id"].clone();
                a["host"] = a["node_id"].clone();
                self.history(op.strip_prefix("desktop.").unwrap(), &a)
            }
            "desktop.live" => {
                let result = (|| -> Result<Value> {
                    let mut client = Client::connect_uninitialized(
                        Path::new(text(&a["endpoint"])),
                        Duration::from_secs(3),
                    )?;
                    client.request("initialize",json!({"clientInfo":{"name":"clinx-native-observer","title":"CLINX read-only Observer","version":"1"},"capabilities":{"experimentalApi":true}}))?;
                    client.send_message(&json!({"method":"initialized"}))?;
                    let result = client.request(
                        "thread/read",
                        json!({"threadId":a["thread_id"],"includeTurns":false}),
                    )?;
                    if !result["thread"].is_object() {
                        return Err("PROVIDER_PROTOCOL_ERROR".into());
                    }
                    Ok(json!({"thread":result["thread"]}))
                })();
                Ok(result.unwrap_or_else(|_| json!({"unavailable":true})))
            }
            "desktop.writer" | "desktop.probe" => {
                a["root"] = self
                    .directory
                    .as_ref()
                    .ok_or("SHARING_SCOPE_DENIED")?
                    .config()["native_home"]
                    .clone();
                a["tid"] = a["thread_id"].clone();
                self.probe.invoke(
                    if op == "desktop.writer" {
                        "source_runtime.native_writer"
                    } else {
                        "source_runtime.writer_probe"
                    },
                    &a,
                    |_, _| Err("NATIVE_OBSERVER_CALLBACK_INVALID".into()),
                )
            }
            "legacy.codex_uri" => Ok(codex_uri(&a)),
            "legacy.grant" => {
                let Some(d) = &self.directory else {
                    return Ok(json!({"scope":null,"record":null,"user_scope":null}));
                };
                let facts = d.node_facts(text(&a["node_id"]))?;
                Ok(
                    json!({"scope":d.invoke("scope", &a)?,"record":facts["record"],"user_scope":d.config()["user_scope"]}),
                )
            }
            "monitor.read" => self.mcp(text(&a["name"]), &a["arguments"]),
            _ => Err("NATIVE_OBSERVER_CALLBACK_INVALID".into()),
        }
    }
    fn mcp(&self, name: &str, arguments: &Value) -> Result<Value> {
        self.bridge.invoke(
            "native_observer.monitor_mcp",
            &json!({"endpoint":self.config["mcp"],"name":name,"arguments":arguments}),
            |_, _| Err("OBSERVER_READ_ONLY".into()),
        )
    }
    fn store(&self, method: &str, args: &Value) -> Result<Value> {
        if truth(&self.config["mcp"]) {
            return match method {
                "health" | "list_tasks" => self.bridge.invoke(
                    if method == "health" {
                        "native_observer.monitor_health"
                    } else {
                        "native_observer.monitor_list"
                    },
                    args,
                    |op, a| self.native_callback(op, a),
                ),
                _ => Err("RUST_TASKS_USE_OBSERVATIONS_V2".into()),
            };
        }
        let mut args = args.clone();
        args["path"] = self.config["path"].clone();
        args["codex_host_ids"] = self.config["codex_host_ids"].clone();
        let operation = match method {
            "health" => "health",
            "list_tasks" => "list",
            "task" => "task",
            "task_events" => "task_events",
            "task_activity" => "activity_execution",
            _ => return Err("NATIVE_OBSERVER_OPERATION_UNKNOWN".into()),
        };
        let value = self.bridge.invoke(
            &format!("native_observer.legacy_{operation}"),
            &args,
            |op, a| self.native_callback(op, a),
        )?;
        if method == "task_activity" {
            self.bridge.invoke("native_observer.activity_read", &json!({"root":self.config["native_home"],"task_ref":args["task_ref"],"execution":value,"after":args["after"],"before":args["before"]}), |_, _| Err("NATIVE_OBSERVER_CALLBACK_INVALID".into()))
        } else {
            Ok(value)
        }
    }
    fn read(&self, request: &Value) -> Result<Value> {
        let args = &request["arguments"];
        let method = text(&request["method"]);
        match text(&request["target"]) {
            "store" => self.store(method, args),
            "network" if truth(&self.config["mcp"]) => self.mcp(
                match method {
                    "list" => "clinx_list_observations",
                    "detail" => "clinx_get_observation",
                    "context" => "clinx_get_observation_context",
                    "activity" => "clinx_get_observation_activity",
                    _ => return Err("OBSERVER_READ_ONLY".into()),
                },
                args,
            ),
            "network" => self
                .directory
                .as_ref()
                .ok_or("OBSERVATION_NOT_CONFIGURED")?
                .invoke(method, args),
            "native" => {
                let mut a = args.clone();
                a["endpoint"] = self.config["native_observer_socket"].clone();
                self.bridge
                    .invoke("native_observer.desktop_observe", &a, |op, a| {
                        self.native_callback(op, a)
                    })
            }
            "collector" => self.bridge.invoke("native_observer.route", args, |op, a| {
                self.native_callback(op, a)
            }),
            _ => Err("OBSERVER_HTTP_CALLBACK_INVALID".into()),
        }
    }
    pub fn callback(&self, op: &str, a: Value) -> Result<Value> {
        let result = match op {
            "observer_http.context" => Ok(
                json!({"credential":self.config["credential"],"network_configured":truth(&self.config["mcp"])||self.directory.is_some()}),
            ),
            "observer_http.credential" => Ok(self.config["credential"].clone()),
            "observer_http.audit" => {
                let _ = writeln!(std::io::stdout().lock(), "{a}");
                Ok(Value::Null)
            }
            "observer_http.read" => self.read(&a),
            _ => Err("OBSERVER_HTTP_CALLBACK_INVALID".into()),
        };
        Ok(match result {
            Ok(value) => json!({"value":value}),
            Err(code) => fault(text(&a["target"]), &code),
        })
    }
    pub fn health(&self) -> Result<Value> {
        self.store("health", &json!({}))
    }
    pub fn port(&self) -> &Value {
        &self.config["port"]
    }
    pub fn audit(&self) -> bool {
        self.config["audit_methods"] == true
    }
}
fn fault(target: &str, code: &str) -> Value {
    let (kind, status) = if target == "store" {
        match code {
            "TASK_NOT_FOUND" | "RUST_TASKS_USE_OBSERVATIONS_V2" => ("api", 404),
            "INVALID_CURSOR" => ("api", 400),
            "CURSOR_RESET_REQUIRED"
            | "ACTIVITY_EXECUTION_CHANGED"
            | "ACTIVITY_CURSOR_RESET_REQUIRED" => ("api", 409),
            "LEGACY_SOURCE_UNAVAILABLE" | "NATIVE_HISTORY_BUDGET_EXHAUSTED" => ("source", 503),
            _ => ("coded", 503),
        }
    } else if target == "native" && code == "INVALID_CONTEXT_LIMIT" {
        ("authority", 503)
    } else {
        ("coded", 503)
    };
    json!({"fault":{"kind":kind,"code":code,"status":status}})
}

pub fn desktop_read_remote(args: &Value) -> Result<Value> {
    let mut a = args.clone();
    a["credential"] = env("CLINX_NATIVE_OBSERVER_TOKEN_FILE");
    native_observer_bridge::Runtime::default().invoke("native_observer.desktop_read", &a, |op,a| {
        if op != "desktop.http" { return Err("NATIVE_OBSERVER_CALLBACK_INVALID".into()); }
        let mut query = json!({"context":if truth(&a["context"]){1}else{0},"recent_turns":a.get("recent_turns").cloned().unwrap_or(json!(8)),"max_bytes":a.get("max_bytes").cloned().unwrap_or(json!(32000))});
        if !a["cursor"].is_null() { query["cursor"] = a["cursor"].clone(); }
        Ok(observation_runtime::loopback_http(&json!({"credential_file":a["credential"]}), &format!("/v2/native/{}",text(&a["thread_id"])), &query, Duration::from_secs(12)).unwrap_or_else(|_| json!({"transport_error":true})))
    })
}

pub fn run() -> Result<()> {
    let observer = Observer::from_env()?;
    observer.health()?; // Existing source only: never initialize or migrate it.
    let http = observer_http::Runtime::default();
    let owner = observer.clone();
    let bound = http.invoke(
        "observer_http.bind",
        &json!({"port":observer.port(),"audit_methods":observer.audit()}),
        move |op, a| owner.callback(op, a),
    )?;
    let owner = observer.clone();
    http.invoke("observer_http.start", &bound, move |op, a| {
        owner.callback(op, a)
    })?;
    let stop = Arc::new(AtomicBool::new(false));
    let mut signals = Vec::new();
    for signal in [signal_hook::consts::SIGTERM, signal_hook::consts::SIGINT] {
        signals.push(
            signal_hook::flag::register(signal, stop.clone())
                .map_err(|_| "SIGNAL_REGISTRATION_FAILED")?,
        );
    }
    while !stop.load(Ordering::Acquire) {
        std::thread::sleep(Duration::from_millis(100));
    }
    http.invoke("observer_http.close", &bound, move |op, a| {
        observer.callback(op, a)
    })?;
    for signal in signals {
        signal_hook::low_level::unregister(signal);
    }
    Ok(())
}
