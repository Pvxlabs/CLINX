//! Standalone paired Helper/Centre assembly. All callbacks below are Rust calls;
//! no Python process participates in admission, authority routing or tick loops.
use crate::{
    native_transport::{Authority, UnixAuthority},
    node_scope::{self, PROTOCOL, Scope},
    node_tls::{self, Result},
};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    io::{BufRead, BufReader, Read, Write},
    net::{SocketAddr, TcpListener, ToSocketAddrs},
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant},
};
fn text<'a>(v: &'a Value, k: &str) -> Result<&'a str> {
    v[k].as_str()
        .ok_or_else(|| format!("NODE_CONFIG_INVALID_{k}"))
}
fn expand(value: &str) -> PathBuf {
    if value == "~" {
        home()
    } else if let Some(s) = value.strip_prefix("~/") {
        home().join(s)
    } else {
        PathBuf::from(value)
    }
}
fn home() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/"))
}
fn absolute(value: &str) -> Result<PathBuf> {
    std::path::absolute(crate::runtime_config::expanduser(Path::new(value))?)
        .map_err(|_| "NODE_PATH_UNAVAILABLE".into())
}
fn env(name: &str, default: String) -> String {
    std::env::var(name).unwrap_or(default)
}
fn hostname() -> String {
    crate::runtime_config::hostname()
}
fn canonical_node(value: &str) -> Result<String> {
    let host =
        crate::execution_contracts::command("execution_contract.host", &json!({"value":value}))?;
    Ok(text(&host, "stable_identifier")?.to_owned())
}

fn scope(root: &Path, peer: &str) -> Result<Value> {
    let raw = node_tls::read_state(root, "sharing.json")?.unwrap_or(json!({"scopes":{}}));
    if !raw["scopes"].is_object() {
        return Err("INVALID_SHARING_STORE".into());
    }
    node_scope::optional(&raw["scopes"][peer]).map(|s| s.map(|s| s.value()).unwrap_or(Value::Null))
}
fn public(identity: &Value) -> Result<Value> {
    crate::discovery_state::Runtime::default().invoke(
        "discovery.public",
        &json!({"value":identity}),
        |_, _| Err("DISCOVERY_CALLBACK_INVALID".into()),
    )
}
fn complete_record(mut record: Value) -> Value {
    for (k,v) in json!({"user_scope":"default","public_key_fingerprint":"","protocol_version":PROTOCOL,"providers":["codex_app_server"],"capabilities":["session.read","session.status"],"route_ids":[],"display_name":"","endpoint":null,"state":"UNKNOWN","last_seen":null,"stale_after_seconds":120,"trusted":true,"authorized":true,"version_compatible":true,"identity_generation":1}).as_object().unwrap(){ if record.get(k).is_none(){record[k]=v.clone();} }
    record
}
fn payload_wire(value: &Value) -> String {
    let mut result = String::new();
    for c in crate::execution_core::canonical(value).chars() {
        if (c as u32) < 127 {
            result.push(c);
        } else {
            for u in c.encode_utf16(&mut [0; 2]) {
                result.push_str(&format!("\\u{u:04x}"));
            }
        }
    }
    result
}
fn registry(registry: &crate::node_registry::Registry, op: &str, mut a: Value) -> Result<Value> {
    if op == "registry.register" {
        a["record"] = complete_record(a["record"].clone());
    }
    if op == "registry.claim_request" {
        a["payload_wire"] = json!(payload_wire(&a["payload"]));
    }
    let r = registry.invoke(op, &a)?;
    if let Some(code) = r["registry_error"]["error_code"].as_str() {
        return Err(code.into());
    }
    Ok(r)
}
#[derive(Clone)]
struct PairedAuthority {
    state: Arc<Daemon>,
}
impl Authority for PairedAuthority {
    fn call(&mut self, op: &str, payload: Value, id: &str) -> Result<Value> {
        let scope = self
            .state
            .active
            .lock()
            .map_err(|_| "SHARING_SCOPE_DENIED")?
            .clone();
        crate::node_observation_router::handle(
            "router.authority_request",
            &json!({"operation":format!("business.{op}"),"payload":payload,"request_id":id,"node_id":self.state.identity["node_id"],"scope":scope}),
            |method, request| {
                if method != "authority.request" {
                    return Err("NODE_CALLBACK_INVALID".into());
                }
                self.state.remote(&request)
            },
        )
    }
}
struct Daemon {
    root: PathBuf,
    identity: Value,
    registry: crate::node_registry::Registry,
    observations: Option<PathBuf>,
    authority: Option<UnixAuthority>,
    config: Value,
    api: Option<crate::mcp_api::Config>,
    node: crate::node_api::Config,
    relay: crate::node_relay::Relay,
    active: Mutex<Value>,
    record: Mutex<Value>,
    collector: crate::observation_collector::Runtime,
    source: Option<crate::observation_runtime::Source>,
    port: Mutex<u16>,
    capabilities: Value,
    health: Option<PathBuf>,
    stale_after: i64,
    local_codex_host_id: Option<String>,
    provider: Option<PathBuf>,
    relay_enabled: bool,
    provider_timeout: f64,
    helper: bool,
}
impl Daemon {
    fn registry(&self, op: &str, args: Value) -> Result<Value> {
        registry(&self.registry, op, args)
    }
    fn remote(&self, request: &Value) -> Result<Value> {
        node_tls::call(
            &self.root,
            text(&self.config, "centre_id")?,
            text(&self.config, "centre_endpoint")?,
            3.,
            request,
        )
    }
    fn current_scope(&self) -> Result<Value> {
        scope(
            &self.root,
            if self.helper {
                text(&self.config, "centre_id")?
            } else {
                text(&self.identity, "node_id")?
            },
        )
    }
    fn register(&self, approval: Value, heartbeat: bool) -> Result<Value> {
        let id = &self.identity["node_id"];
        if self.helper {
            return self.remote(&json!({"operation":if heartbeat{"node.heartbeat"}else{"node.register"},"protocol_version":PROTOCOL,"node_id":id,"port":*self.port.lock().map_err(|_|"NODE_LISTEN_UNAVAILABLE")?,"capabilities":self.capabilities,"sharing_scope":approval}));
        }
        let approved = self.current_scope()?;
        if !node_scope::optional(&approved)?.is_some_and(|s| s.allows("observation.upload")) {
            let old = self.registry("registry.get", json!({"node_id":id}))?;
            if !old.is_null() {
                self.registry(
                    "registry.revoke",
                    json!({"node_id":id,"user_scope":old["user_scope"]}),
                )?;
            }
            return Ok(json!({"registered":false,"error_code":"LOCAL_OBSERVATION_SCOPE_DENIED"}));
        }
        let old = self.registry("registry.get", json!({"node_id":id}))?;
        let routes = self
            .local_codex_host_id
            .as_ref()
            .map(|s| json!([s]))
            .unwrap_or_else(|| old.get("route_ids").cloned().unwrap_or(json!([])));
        self.registry("registry.register",json!({"record":{"node_id":id,"user_scope":approved["user_scope"],"public_key_fingerprint":self.identity["fingerprint"],"display_name":self.identity["display_name"],"route_ids":routes,"capabilities":["session.read","session.status"],"state":"ONLINE","last_seen":node_tls::now(),"stale_after_seconds":self.stale_after},"scope":approved}))?;
        Ok(json!({"registered":true,"scope":approved}))
    }
    fn accept(&self, node: &Value, scope: &Value, request: &Value) -> Result<Value> {
        crate::observation::invoke(
            "observation.accept",
            &json!({"path":self.observations.as_ref().ok_or("OBSERVATION_NOT_CONFIGURED")?,"node":node,"scope":scope,"request":request}),
            |_, _| Err("OBSERVATION_CALLBACK_REQUIRED".into()),
        )
    }
    fn collect(&self) -> Result<Value> {
        self.collector.invoke("collector.tick",&json!({"previous_stream_id":self.config["observation_previous_stream"],"outbox_limit":2048}),|op,a|{
        match op {
            "collector.scope"=>self.current_scope(),
            "collector.heartbeat"=>self.register(a["scope"].clone(),true),
            "collector.upload"=>{
                if self.helper {let mut r=a["batch"].clone();for(k,v)in json!({"operation":"observation.upload","protocol_version":PROTOCOL,"node_id":self.identity["node_id"],"sharing_scope":a["scope"]}).as_object().unwrap(){r[k]=v.clone();}self.remote(&r)}
                else{let approval=self.current_scope()?;if !node_scope::optional(&approval)?.is_some_and(|s|s.allows("observation.upload")){return Err("LOCAL_OBSERVATION_SCOPE_DENIED".into());}self.accept(&self.identity["node_id"],&approval,&a["batch"])}
            }
            "collector.generation"|"collector.inventory"|"collector.canonical_inventory"|"collector.sample"=>self.source.as_ref().ok_or("OBSERVATION_SOURCE_UNAVAILABLE")?.invoke(op.trim_start_matches("collector."),&a),
            _=>Err("COLLECTOR_CALLBACK_INVALID".into()),
        }
    })
    }
    fn health(&self, mut value: Value) {
        if let Some(path) = &self.health {
            value["observed_at"] = json!(node_tls::now());
            let _ = (|| -> std::io::Result<()> {
                fs::create_dir_all(path.parent().unwrap())?;
                let tmp = path.with_extension("tmp");
                fs::write(&tmp, value.to_string())?;
                fs::rename(tmp, path)
            })();
        }
    }
    fn centre(&self, request: Value, der: &[u8], address: SocketAddr) -> Result<Value> {
        crate::node_centre::handle(
            &json!({"state":self.root,"request":request,"certificate_hex":hex(der),"address":[address.ip().to_string(),address.port()],"stale_after":self.stale_after,"authority":self.authority.is_some(),"observations":self.observations.is_some()}),
            |op, a| {
                if op.starts_with("registry.") {
                    return self.registry(op, a);
                }
                match op {
                    "authority.node_request" => self
                        .authority
                        .clone()
                        .ok_or("RUST_AUTHORITY_NOT_CONFIGURED")?
                        .call("node_request", a, &crate::mcp_api::operation_id()?),
                    "observation.accept" => self.accept(&a["node_id"], &a["scope"], &a["request"]),
                    _ => Err("NETWORK_CALLBACK_INVALID".into()),
                }
            },
        )
    }
    fn execute(self: &Arc<Self>, operation: &str, args: Value) -> Result<Value> {
        if operation.starts_with("session.") {
            return self.project(operation == "session.status", args);
        }
        let api = self.api.clone().ok_or("OPERATION_NOT_IMPLEMENTED")?;
        let mut node = crate::node_api::Node {
            backend: crate::mcp_api::Api {
                authority: PairedAuthority {
                    state: self.clone(),
                },
                reads: crate::mcp_api::NativeReads(api.clone()),
                config: api,
            },
            config: self.node.clone(),
        };
        node.invoke(operation.trim_start_matches("execution."), &args)
            .map_err(|e| e.code)
    }
    fn snapshot(&self, effective: Option<&Value>) -> Result<Value> {
        let approval = effective.cloned().unwrap_or(
            self.active
                .lock()
                .map_err(|_| "SHARING_SCOPE_DENIED")?
                .clone(),
        );
        let mut record = self
            .record
            .lock()
            .map_err(|_| "NODE_STATE_UNAVAILABLE")?
            .clone();
        if !approval.is_null() {
            record["user_scope"] = approval["user_scope"].clone();
            record["authorized"] = json!(approval["revoked"] != true);
            if approval["revoked"] == true {
                record["state"] = json!("REVOKED");
            }
        }
        Ok(json!({"record":record,"scope":approval}))
    }
    fn read(self: &Arc<Self>, thread: &Value, kwargs: &Value) -> Result<Value> {
        crate::node_observation_router::handle(
            "router.helper_read",
            &json!({"native_home":self.config["native_home"],"node_id":self.identity["node_id"],"thread_id":thread,"kwargs":kwargs,"execution":self.api.is_some()}),
            |op, a| match op {
                "helper.scopes" => Ok(
                    json!({"local":self.current_scope()?,"active":self.active.lock().map_err(|_|"SHARING_SCOPE_DENIED")?.clone()}),
                ),
                "helper.context" => self.execute("execution.context", a),
                _ => Err("NODE_CALLBACK_INVALID".into()),
            },
        )
    }
    fn project(self: &Arc<Self>, status: bool, request: Value) -> Result<Value> {
        let snapshot = self.snapshot(None)?;
        crate::node_observation_router::handle(
            if status {
                "router.service_status"
            } else {
                "router.service_read"
            },
            &json!({"request":request,"record":snapshot["record"],"scope":snapshot["scope"],"secret_hex":null}),
            |op, a| match op {
                "service.snapshot" => self.snapshot(None),
                "service.read" | "service.status" => self.read(&a["thread_id"], &a["kwargs"]),
                "registry.update_thread" => self.registry(op, a),
                _ => Err("NODE_CALLBACK_INVALID".into()),
            },
        )
    }
    fn service(self: &Arc<Self>, request: Value, effective: &Value) -> Result<Value> {
        let snapshot = self.snapshot(Some(effective))?;
        let mut available = json!({"session.read":true,"session.status":true});
        for op in [
            "adopt",
            "context",
            "prepare",
            "start",
            "status",
            "cancel",
            "reconcile",
        ] {
            available[format!("execution.{op}")] = json!(self.api.is_some());
        }
        crate::node_service::handle(
            &json!({"operation":request["operation"],"request":request,"record":snapshot["record"],"scope":effective,"available":available,"authority_owned":true}),
            |op, a| {
                if op.starts_with("registry.") {
                    return self.registry(op, a);
                }
                if op != "execute" {
                    return Err("NODE_CALLBACK_INVALID".into());
                }
                Ok(
                    match self.execute(text(&a, "operation")?, a["request"].clone()) {
                        Ok(v) => v,
                        Err(e) => json!({"callback_error":{"code":e,"reason":e,"details":{}}}),
                    },
                )
            },
        )
    }
    fn helper(self: &Arc<Self>, request: Value, der: &[u8]) -> Result<Value> {
        let active = self
            .active
            .lock()
            .map_err(|_| "SHARING_SCOPE_DENIED")?
            .clone();
        let mut effective = Value::Null;
        crate::node_owner::handle(
            &json!({"state":self.root,"centre_id":self.config["centre_id"],"node_id":self.identity["node_id"],"certificate_hex":hex(der),"request":request,"active_scope":active,"provider_relay":self.relay_enabled}),
            |op, a| {
                match op{
            "scope.apply"=>{effective=a;Ok(Value::Null)},
            "node.handle"=>self.service(a,&effective),
            "provider.handle"=>self.relay.handle(&json!({"node_id":self.identity["node_id"],"request":a["request"],"scope":a["scope"],"context_wire":crate::execution_core::canonical(&a["request"]["context"])}),|context,body|{
                crate::node_relay::exchange(&json!({"endpoint":self.provider,"program":provider_program()?,"timeout":self.provider_timeout,"context":context,"body":body}))
            }),
            _=>Err("NODE_CALLBACK_INVALID".into()),
        }
            },
        )
    }
    fn helper_tick(
        self: &Arc<Self>,
        supervisor: &mut crate::node_observation_router::HelperSupervisor,
        endpoint: &str,
    ) -> Result<Value> {
        supervisor.tick(&json!({"centre_id":self.config["centre_id"],"node_id":self.identity["node_id"],"endpoint":endpoint,"capabilities":self.capabilities}),|op,a|{
            let result=(||->Result<Value>{match op{
                "helper.approval"=>self.current_scope(),
                "helper.active"=>{*self.active.lock().map_err(|_|"SHARING_SCOPE_DENIED")?=a;Ok(Value::Null)},
                "helper.register"=>self.register(a["scope"].clone(),a["heartbeat"]==true),
                "helper.state"=>{let mut r=self.record.lock().map_err(|_|"NODE_STATE_UNAVAILABLE")?;r["state"]=a["state"].clone();if !a["last_seen"].is_null(){r["last_seen"]=a["last_seen"].clone();}Ok(Value::Null)},
                "helper.health"=>{self.health(a);Ok(Value::Null)},
                "helper.collect"=>self.collect(),
                _=>Err("NODE_CALLBACK_INVALID".into()),
            }})();
            Ok(match result {Ok(v)=>json!({"result":v}),Err(e)=>json!({"error_type":e})})
        })
    }
}
fn provider_program() -> Result<PathBuf> {
    let explicit = std::env::var_os("CLINX_PROVIDER_BINARY").map(PathBuf::from);
    let path = explicit.unwrap_or(
        std::env::current_exe()
            .map_err(|_| "RUST_PROVIDER_BINARY_REQUIRED")?
            .with_file_name("clinx-provider"),
    );
    if path.is_absolute() && path.is_file() {
        Ok(path)
    } else {
        Err("RUST_PROVIDER_BINARY_REQUIRED".into())
    }
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
fn frame<S: Read>(stream: &mut S) -> Result<Value> {
    let mut bytes = Vec::new();
    let read = BufReader::new(stream)
        .take((node_tls::FRAME + 1) as u64)
        .read_until(b'\n', &mut bytes);
    if read.is_err_and(|e| e.kind() != std::io::ErrorKind::UnexpectedEof)
        || bytes.len() > node_tls::FRAME
        || bytes.is_empty()
    {
        return Err("INVALID_NODE_REQUEST".into());
    }
    let v: Value = serde_json::from_slice(&bytes).map_err(|_| "INVALID_NODE_REQUEST")?;
    if !v.is_object() || !v["operation"].is_string() {
        return Err("INVALID_NODE_REQUEST".into());
    }
    Ok(v)
}
fn respond<S: Write>(stream: &mut S, reply: &Value) -> Result<()> {
    let mut bytes = serde_json::to_vec(reply).map_err(|_| "NODE_RESPONSE_INVALID")?;
    bytes.push(b'\n');
    if bytes.len() > node_tls::FRAME {
        return Err("NODE_RESPONSE_TOO_LARGE".into());
    }
    stream
        .write_all(&bytes)
        .and_then(|_| stream.flush())
        .map_err(|_| "NODE_RESPONSE_UNAVAILABLE".into())
}
fn listener(host: &str, port: u16) -> Result<TcpListener> {
    for address in (host, port)
        .to_socket_addrs()
        .map_err(|_| "NODE_LISTEN_UNAVAILABLE")?
    {
        let attempt = (|| -> Result<TcpListener> {
            use rustix::net::{AddressFamily, SocketFlags, SocketType};
            let fd = rustix::net::socket_with(
                if address.is_ipv4() {
                    AddressFamily::INET
                } else {
                    AddressFamily::INET6
                },
                SocketType::STREAM,
                SocketFlags::CLOEXEC,
                Some(rustix::net::ipproto::TCP),
            )
            .map_err(|_| "NODE_LISTEN_UNAVAILABLE")?;
            rustix::net::sockopt::set_socket_reuseaddr(&fd, true)
                .map_err(|_| "NODE_LISTEN_UNAVAILABLE")?;
            rustix::net::bind(&fd, &address).map_err(|_| "NODE_LISTEN_UNAVAILABLE")?;
            rustix::net::listen(&fd, 128).map_err(|_| "NODE_LISTEN_UNAVAILABLE")?;
            Ok(TcpListener::from(fd))
        })();
        if let Ok(v) = attempt {
            return Ok(v);
        }
    }
    Err("NODE_LISTEN_UNAVAILABLE".into())
}
fn serve(state: Arc<Daemon>, host: &str, port: u16, gateways: &[Value]) -> Result<()> {
    node_tls::server(&state.root)?;
    let listener = listener(host, port)?;
    listener
        .set_nonblocking(true)
        .map_err(|_| "NODE_LISTEN_UNAVAILABLE")?;
    let bound = listener
        .local_addr()
        .map_err(|_| "NODE_LISTEN_UNAVAILABLE")?;
    *state.port.lock().map_err(|_| "NODE_LISTEN_UNAVAILABLE")? = bound.port();
    let stopped = Arc::new(AtomicBool::new(false));
    for sig in [signal_hook::consts::SIGINT, signal_hook::consts::SIGTERM] {
        signal_hook::flag::register(sig, stopped.clone())
            .map_err(|_| "SIGNAL_REGISTRATION_FAILED")?;
    }
    let mut gateway_handles = vec![];
    for args in gateways {
        let state = state.clone();
        let root = state.root.clone();
        gateway_handles.push(crate::node_gateway::listen(args,move|r|crate::node_paired::exchange(&json!({"state":root,"node_id":r["context"]["host"],"context":r["context"],"body_wire":serde_json::to_string(&json!({"method":r["method"],"params":r["params"]})).map_err(|_|"PROVIDER_DELIVERY_UNKNOWN")?}),|op,a|state.registry(op,a)))?);
    }
    let s = state.clone();
    let stop = stopped.clone();
    let thread = thread::spawn(move || {
        let mut requests = vec![];
        while !stop.load(Ordering::Relaxed) {
            match listener.accept() {
                Ok((socket, address)) => {
                    let s = s.clone();
                    requests.push(thread::spawn(move||{
        if socket.set_read_timeout(Some(Duration::from_secs(5))).and_then(|_|socket.set_write_timeout(Some(Duration::from_secs(5)))).is_err(){return;}
        let Ok((mut stream,_peer,der))=node_tls::connection(socket,&s.root)else{return;};
        let request=match frame(&mut stream){Ok(v)=>v,Err(e)=>{let _=respond(&mut stream,&json!({"error_code":e,"read_only":true}));return;}};
        let r=if s.helper{s.helper(request,&der)}else{s.centre(request,&der,address)};
        let reply=r.unwrap_or_else(|e|json!({"error_code":e,"unavailable_reason":if s.helper{"Node owner route rejected"}else{"Centre route rejected"},"registered":false,"read_only":true}));let _=respond(&mut stream,&reply);
    }));
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(10))
                }
                Err(_) => break,
            }
            requests.retain(|r| !r.is_finished());
        }
        for request in requests {
            let _ = request.join();
        }
    });
    println!(
        "{}",
        json!({"ready":true,"address":[bound.ip().to_string(),bound.port()],"runtime":"RUST_NODE_DAEMON_V1","node_id":state.identity["node_id"],"registry":state.config["registry"]})
    );
    let mut helper = crate::node_observation_router::HelperSupervisor::default();
    let mut next = Instant::now();
    let outcome = (|| -> Result<()> {
        while !stopped.load(Ordering::Relaxed) {
            if state.helper {
                state.helper_tick(
                    &mut helper,
                    &format!("tls://{}:{}", bound.ip(), bound.port()),
                )?;
            } else {
                state.registry("registry.refresh_states", json!({}))?;
                if state.source.is_some() && Instant::now() >= next {
                    let delay = match state.collect() {
                        Ok(v) => v["delay"].as_u64().unwrap_or(2),
                        Err(e) => {
                            println!("{}", json!({"observation_error":e}));
                            2
                        }
                    };
                    next = Instant::now() + Duration::from_secs(delay);
                }
            }
            thread::sleep(Duration::from_millis(250));
        }
        Ok(())
    })();
    // Always revoke the in-memory route and stop listeners, including tick errors.
    stopped.store(true, Ordering::Relaxed);
    if let Ok(mut active) = state.active.lock() {
        *active = Value::Null;
    }
    state.health(json!({"status":"stopped"}));
    drop(gateway_handles);
    let _ = thread.join();
    let _ = state
        .collector
        .invoke("collector.close", &json!({}), |_, _| {
            Err("COLLECTOR_CLOSED".into())
        });
    outcome
}
#[derive(Default)]
struct Options {
    command: String,
    values: BTreeMap<String, Vec<String>>,
    flags: Vec<String>,
}
impl Options {
    fn parse(args: Vec<String>) -> Result<Self> {
        let mut result = Self::default();
        let mut i = 0;
        while i < args.len() {
            let arg = &args[i];
            if arg.starts_with("--") {
                let key = arg.trim_start_matches("--");
                if [
                    "local-source",
                    "read-sessions",
                    "execute-tasks",
                    "revoke",
                    "approved",
                    "rust-projection",
                ]
                .contains(&key)
                {
                    result.flags.push(key.into());
                    i += 1;
                } else {
                    if ![
                        "state",
                        "node-id",
                        "peer",
                        "user-scope",
                        "project",
                        "centre",
                        "endpoint",
                        "bind",
                        "port",
                        "execution-config",
                        "observation-canonical-db",
                        "observation-owner-host",
                        "observation-previous-stream",
                        "execution-thread",
                        "execution-project",
                        "execution-new-project",
                        "execution-cancel-project",
                        "local-canonical-db",
                        "registry",
                        "observations",
                        "local-native-home",
                        "local-provider-config",
                        "local-owner-host",
                        "provider-gateway",
                        "provider-transport-uid",
                        "provider-observer-uid",
                        "provider-gateway-gid",
                        "stale-after",
                        "local-codex-host-id",
                        "seconds",
                        "fingerprint",
                    ]
                    .contains(&key)
                    {
                        return Err("NODE_ARGUMENT_INVALID".into());
                    }
                    let value = args
                        .get(i + 1)
                        .filter(|v| !v.starts_with("--"))
                        .ok_or("NODE_ARGUMENT_REQUIRED")?;
                    result
                        .values
                        .entry(key.into())
                        .or_default()
                        .push(value.clone());
                    i += 2;
                }
            } else if result.command.is_empty() {
                result.command = arg.clone();
                i += 1;
            } else {
                return Err("NODE_ARGUMENT_INVALID".into());
            }
        }
        Ok(result)
    }
    fn get(&self, key: &str) -> Option<&str> {
        self.values
            .get(key)
            .and_then(|v| v.last())
            .map(String::as_str)
    }
    fn required(&self, key: &str) -> Result<&str> {
        self.get(key)
            .ok_or_else(|| format!("NODE_ARGUMENT_REQUIRED_{key}"))
    }
    fn flag(&self, key: &str) -> bool {
        self.flags.iter().any(|v| v == key)
    }
    fn value(&self, key: &str) -> Value {
        self.get(key).map(|v| json!(v)).unwrap_or(Value::Null)
    }
    fn array(&self, key: &str) -> Value {
        self.values
            .get(key)
            .map(|v| json!(v))
            .unwrap_or(Value::Null)
    }
    fn port(&self, default: u16) -> Result<u16> {
        self.get("port")
            .map(|v| v.parse().map_err(|_| "INVALID_NODE_ENDPOINT".into()))
            .unwrap_or(Ok(default))
    }
}
fn authority() -> Result<Option<UnixAuthority>> {
    match std::env::var("CLINX_AUTHORITY")
        .unwrap_or_else(|_| "python".into())
        .as_str()
    {
        "python" => Ok(None),
        "rust" => {
            let mut a = UnixAuthority {
                endpoint: PathBuf::from(
                    std::env::var("CLINX_RUST_SOCKET")
                        .map_err(|_| "RUST_AUTHORITY_CONFIG_REQUIRED")?,
                ),
                uid: std::env::var("CLINX_RUST_UID")
                    .map_err(|_| "RUST_AUTHORITY_CONFIG_REQUIRED")?
                    .parse()
                    .map_err(|_| "RUST_AUTHORITY_CONFIG_REQUIRED")?,
            };
            if a.call("health", json!({}), "health")?["business_commands"] != true {
                return Err("RUST_BUSINESS_AUTHORITY_REQUIRED".into());
            }
            Ok(Some(a))
        }
        _ => Err("INVALID_AUTHORITY_SELECTION".into()),
    }
}
fn source_config(
    native: Value,
    node: &Value,
    owner: Value,
    canonical: Value,
    provider: Value,
    managed: bool,
    a: &Option<UnixAuthority>,
) -> Result<Value> {
    let executable = std::env::var_os("CLINX_NODE_BINARY")
        .map(PathBuf::from)
        .unwrap_or(
            std::env::current_exe()
                .map_err(|_| "RUST_NODE_BINARY_REQUIRED")?
                .with_file_name("clinx-node"),
        );
    let auth = a
        .as_ref()
        .map(|a| json!({"endpoint":a.endpoint,"uid":a.uid,"timeout":10}))
        .unwrap_or(Value::Null);
    let remote=std::env::var("CLINX_NATIVE_OBSERVER_TOKEN_FILE").ok().map(|p|json!({"credential_file":p,"port":std::env::var("CLINX_NATIVE_OBSERVER_PORT").ok().and_then(|s|s.parse::<u16>().ok()).unwrap_or(8766)})).unwrap_or(Value::Null);
    Ok(
        json!({"native_home":native,"node_id":node,"owner_host":if crate::read_projection::truth(&owner){owner}else{node.clone()},"canonical_path":canonical,"provider_config":provider,"managed":managed,"authority":auth,"remote_native":remote,"native_observer_socket":std::env::var("CLINX_NATIVE_OBSERVER_SOCKET").ok(),"native_writer_observation":false,"probe_program":executable}),
    )
}
fn helper_main() -> Result<()> {
    let root = absolute(&env(
        "CLINX_NODE_STATE",
        home()
            .join("Library/Application Support/CLINX Monitor/Devices")
            .display()
            .to_string(),
    ))?;
    let health = absolute(&env(
        "CLINX_NODE_HEALTH_STATE",
        home()
            .join("Library/Application Support/CLINX Monitor/Node")
            .display()
            .to_string(),
    ))?
    .join("health.json");
    let outcome = (|| -> Result<()> {
        crate::discovery_identity::private_store(&root)?;
        let persisted = crate::discovery_identity::read(&root, "identity.json")?;
        let host = hostname();
        let fallback = host.to_lowercase().chars().take(63).collect::<String>();
        let node = env(
            "CLINX_NODE_ID",
            persisted
                .as_ref()
                .and_then(|v| v["node_id"].as_str())
                .unwrap_or(&fallback)
                .into(),
        );
        let node = canonical_node(&node)?;
        let identity = crate::discovery_identity::open(
            &root,
            &node,
            Some(&host.chars().take(100).collect::<String>()),
        )?;
        let mut config = crate::discovery_identity::read(&root, "node-config.json")?
            .ok_or("CENTRE_NOT_CONFIGURED")?;
        let centre = text(&config, "centre_id")?;
        node_tls::endpoint(text(&config, "centre_endpoint")?)?;
        node_tls::peer(&root, centre)?;
        let approval = scope(&root, centre)?;
        let local = node_scope::optional(&approval)?
            .filter(|s| !s.revoked && (s.read_sessions || s.execute_tasks))
            .ok_or("SHARING_AUTHORIZATION_REQUIRED")?;
        let native = absolute(&env(
            "CLINX_NATIVE_HOME",
            home().join(".codex").display().to_string(),
        ))?;
        config["native_home"] = json!(native);
        let execution = config["execution_config"]
            .as_str()
            .filter(|p| !p.is_empty())
            .map(|p| crate::runtime_config::native_load(&expand(p)))
            .transpose()?;
        // An unavailable execution route must not disable an independently
        // granted read-only observation route. Relay config remains validated.
        let api = if local.execute_tasks {
            execution.as_ref().and_then(|cfg| {
                crate::runtime_config::api_config(cfg)
                    .ok()
                    .filter(|a| a.runtime_host == node)
            })
        } else {
            None
        };
        let projects=execution.as_ref().map(|cfg|cfg["projects"].as_array().cloned().unwrap_or_default().into_iter().map(|p|json!({"project_alias":p.get("alias").filter(|v|v.as_str().is_some_and(|s|!s.is_empty())).or_else(||p.get("target_alias").filter(|v|v.as_str().is_some_and(|s|!s.is_empty()))).cloned().unwrap_or_else(||json!(p["linear_name"].as_str().unwrap_or("").to_lowercase().replace(' ',"-"))),"repo":p["repo"]})).collect::<Vec<_>>()).unwrap_or_default();
        let node_config:crate::node_api::Config=serde_json::from_value(json!({"node_id":node,"projects":projects,"allowed_threads":config["execution_threads"],"allowed_projects":config["execution_projects"],"allowed_new_projects":config["execution_new_projects"],"allowed_cancel_projects":config["execution_cancel_projects"]})).map_err(|_|"NODE_CONFIG_INVALID")?;
        let mut capabilities = Vec::new();
        if local.read_sessions {
            capabilities.extend(["session.read", "session.status"]);
        }
        if api.is_some() {
            capabilities.extend([
                "execution.adopt",
                "execution.prepare",
                "execution.start",
                "execution.status",
                "execution.context",
                "execution.cancel",
                "execution.reconcile",
                "execution.native_canonical_v1",
            ]);
        }
        let native_writer = config
            .get("observation_native_writer")
            .cloned()
            .unwrap_or(json!(false));
        if !native_writer.is_boolean()
            || (native_writer == true && !config["observation_provider_config"].is_null())
        {
            return Err("Invalid native writer observation route".into());
        }
        let observation_provider = if config["observation_provider_config"].is_null() {
            Value::Null
        } else {
            let p = config["observation_provider_config"]
                .as_str()
                .filter(|p| !p.trim().is_empty())
                .ok_or("Invalid observation_provider_config")?;
            crate::runtime_config::native_load(&expand(p))?
        };
        let mut source = source_config(
            json!(native),
            &json!(node),
            config["observation_owner_host"].clone(),
            config["observation_canonical_db"].clone(),
            observation_provider,
            false,
            &None,
        )?;
        source["native_writer_observation"] = native_writer;
        let source = crate::observation_runtime::Source::new(source)?;
        let registry = crate::node_registry::Registry::default();
        registry.invoke("registry.init", &json!({"path":root.join("node.sqlite3")}))?;
        let collector = crate::observation_collector::Runtime::default();
        collector.invoke(
            "collector.init",
            &json!({"path":root.join("observation-outbox.sqlite3")}),
            |_, _| Err("COLLECTOR_CALLBACK_INVALID".into()),
        )?;
        let relay_enabled = execution.is_some();
        let provider_timeout = execution
            .as_ref()
            .and_then(|cfg| cfg["app_server"]["request_timeout_seconds"].as_f64())
            .unwrap_or(3.);
        let endpoints = execution
            .as_ref()
            .map(crate::runtime_config::provider_endpoints)
            .transpose()?
            .unwrap_or_default();
        let provider = if endpoints.len() == 1 {
            endpoints.into_iter().next()
        } else {
            None
        };
        let bind = config["bind_address"]
            .as_str()
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .unwrap_or_else(|| env("CLINX_NODE_BIND_ADDRESS", "0.0.0.0".into()));
        let supplied_port = if crate::read_projection::truth(&config["port"]) {
            config["port"].clone()
        } else {
            json!(env("CLINX_NODE_PORT", "8772".into()))
        };
        let port = if let Some(s) = supplied_port.as_str() {
            crate::python_numeric::integer_text(s)
                .ok()
                .and_then(|s| s.parse::<u16>().ok())
        } else if let Some(b) = supplied_port.as_bool() {
            Some(u16::from(b))
        } else {
            supplied_port
                .as_u64()
                .and_then(|v| u16::try_from(v).ok())
                .or_else(|| {
                    supplied_port
                        .as_f64()
                        .filter(|v| v.is_finite() && *v >= 0. && *v <= 65535.)
                        .map(|v| v.trunc() as u16)
                })
        }
        .ok_or("INVALID_NODE_ENDPOINT")?;
        let record = complete_record(
            json!({"node_id":node,"user_scope":local.user_scope,"public_key_fingerprint":identity["fingerprint"],"display_name":identity["display_name"],"capabilities":[]}),
        );
        serve(
            Arc::new(Daemon {
                root: root.clone(),
                identity,
                registry,
                observations: None,
                authority: None,
                config,
                api,
                node: node_config,
                relay: Default::default(),
                active: Mutex::new(Value::Null),
                record: Mutex::new(record),
                collector,
                source: Some(source),
                port: Mutex::new(0),
                capabilities: json!(capabilities),
                health: Some(health.clone()),
                stale_after: 45,
                local_codex_host_id: None,
                provider,
                relay_enabled,
                provider_timeout,
                helper: true,
            }),
            &bind,
            port,
            &[],
        )
    })();
    if let Err(reason) = outcome {
        if let Some(parent) = health.parent() {
            let _ = fs::create_dir_all(parent);
        }
        let _ = fs::write(
            &health,
            json!({"status":"blocked","reason":reason,"observed_at":node_tls::now()}).to_string(),
        );
    }
    Ok(())
}
fn centre_main(options: Options) -> Result<()> {
    let root = absolute(options.required("state")?)?;
    let node_value = canonical_node(options.required("node-id")?)?;
    let node = node_value.as_str();
    crate::discovery_identity::private_store(&root)?;
    if options.command != "bootstrap"
        && crate::discovery_identity::read(&root, "identity.json")?.is_none()
    {
        return Err("IDENTITY_BOOTSTRAP_REQUIRED".into());
    }
    let identity = crate::discovery_identity::open(&root, node, None)?;
    if options.command == "bootstrap" {
        println!("{}", public(&identity)?);
        return Ok(());
    }
    if options.command == "share" {
        if !options.flag("approved") {
            return Err("APPROVED_ARGUMENT_REQUIRED".into());
        }
        let peer = options.required("peer")?;
        if options.flag("local-source") {
            if peer != node || options.flag("execute-tasks") {
                return Err("LOCAL_OBSERVATION_GRANT_INVALID".into());
            }
        } else {
            node_tls::peer(&root, peer)?;
        }
        let previous = scope(&root, peer)?;
        let sharing = Scope::parse(
            &json!({"user_scope":options.required("user-scope")?,"read_sessions":options.flag("read-sessions")&&!options.flag("revoke"),"execute_tasks":options.flag("execute-tasks")&&!options.flag("revoke"),"providers":if options.flag("revoke"){previous.get("providers").cloned().unwrap_or(json!(["codex_app_server"]))}else{json!(["codex_app_server"])},"projects":if options.flag("revoke")||options.array("project").is_null(){json!(["*"])}else{options.array("project")},"revoked":options.flag("revoke"),"updated_at":node_tls::now()}),
        )?;
        let _lock = crate::discovery_identity::lock(&root, "store.lock", false)?;
        let mut value = crate::discovery_identity::read(&root, "sharing.json")?
            .unwrap_or(json!({"schema_version":1,"scopes":{}}));
        if !value["scopes"].is_object() {
            return Err("INVALID_SHARING_STORE".into());
        }
        value["scopes"][peer] = sharing.value();
        crate::discovery_identity::write(&root, "sharing.json", &value)?;
        println!("{}", json!({"peer_id":peer,"scope":sharing.value()}));
        return Ok(());
    }
    if options.command == "configure-node" {
        let centre = options.required("centre")?;
        node_tls::peer(&root, centre)?;
        node_tls::endpoint(options.required("endpoint")?)?;
        if std::env::var("CLINX_AUTHORITY").as_deref() == Ok("rust")
            && options.get("observation-canonical-db").is_some()
        {
            return Err("RUST_AUTHORITY_REQUIRES_REMOVAL_OF_LEGACY_CANONICAL_DB_ARGUMENTS".into());
        }
        let config = json!({"schema_version":1,"centre_id":centre,"centre_endpoint":options.required("endpoint")?,"bind_address":options.get("bind").unwrap_or("0.0.0.0"),"port":options.port(8772)?,"execution_config":options.get("execution-config").map(absolute).transpose()?,"observation_canonical_db":options.get("observation-canonical-db").map(absolute).transpose()?,"observation_owner_host":options.value("observation-owner-host"),"observation_previous_stream":options.value("observation-previous-stream"),"execution_threads":options.array("execution-thread"),"execution_projects":options.array("execution-project"),"execution_new_projects":options.array("execution-new-project"),"execution_cancel_projects":options.array("execution-cancel-project")});
        let _lock = crate::discovery_identity::lock(&root, "store.lock", false)?;
        crate::discovery_identity::write(&root, "node-config.json", &config)?;
        println!("{}", json!({"configured":true,"centre_id":centre}));
        return Ok(());
    }
    if matches!(options.command.as_str(), "pair-listen" | "pair-connect") {
        return pairing(&options, &root, &identity);
    }
    if options.command != "serve" {
        return Err("NODE_COMMAND_INVALID".into());
    }
    if std::env::var("CLINX_AUTHORITY").as_deref() == Ok("rust")
        && options.get("local-canonical-db").is_some()
    {
        return Err("RUST_AUTHORITY_REQUIRES_REMOVAL_OF_LEGACY_CANONICAL_DB_ARGUMENTS".into());
    }
    let authority = authority()?;
    let registry = crate::node_registry::Registry::default();
    registry.invoke(
        "registry.init",
        &json!({"path":absolute(options.required("registry")?)?}),
    )?;
    let observations = options.get("observations").map(absolute).transpose()?;
    if let Some(path) = &observations {
        crate::observation::invoke("observation.init", &json!({"path":path}), |_, _| {
            Err("OBSERVATION_CALLBACK_INVALID".into())
        })?;
    }
    let collector = crate::observation_collector::Runtime::default();
    let source = if options.get("local-native-home").is_some() || options.flag("rust-projection") {
        if options.flag("rust-projection") && authority.is_none() {
            return Err("RUST_PROJECTION_REQUIRES_RUST_AUTHORITY".into());
        }
        if observations.is_none() {
            return Err("LOCAL_OBSERVATION_REQUIRES_PROJECTION".into());
        }
        let cfg = options
            .get("local-provider-config")
            .map(|p| crate::runtime_config::native_load(&expand(p)))
            .transpose()?
            .unwrap_or(Value::Null);
        let source = crate::observation_runtime::Source::new(source_config(
            options
                .get("local-native-home")
                .map(absolute)
                .transpose()?
                .map(|p| json!(p))
                .unwrap_or(Value::Null),
            &identity["node_id"],
            options.value("local-owner-host"),
            options
                .get("local-canonical-db")
                .map(absolute)
                .transpose()?
                .map(|p| json!(p))
                .unwrap_or(Value::Null),
            cfg,
            authority.is_some(),
            &authority,
        )?)?;
        collector.invoke(
            "collector.init",
            &json!({"path":root.join("local-observation-outbox.sqlite3")}),
            |_, _| Err("COLLECTOR_CALLBACK_INVALID".into()),
        )?;
        Some(source)
    } else {
        None
    };
    let mut gateways = vec![];
    if let Some(paths) = options.values.get("provider-gateway") {
        if authority.is_none() || options.get("provider-transport-uid").is_none() {
            return Err("RUST_TRANSPORT_GATEWAY_CONFIG_REQUIRED".into());
        }
        let uid = options
            .required("provider-transport-uid")?
            .parse::<u32>()
            .map_err(|_| "PROVIDER_TRANSPORT_UID_REQUIRED")?;
        for path in paths {
            gateways.push(json!({"endpoint":absolute(path)?,"transport_uid":uid,"observer_uid":options.get("provider-observer-uid").map(str::parse::<u32>).transpose().map_err(|_|"PROVIDER_OBSERVER_UID_INVALID")?,"socket_gid":options.get("provider-gateway-gid").map(str::parse::<u32>).transpose().map_err(|_|"PROVIDER_GATEWAY_GROUP_INVALID")?,"state":root}));
        }
    }
    let stale = options
        .get("stale-after")
        .unwrap_or("45")
        .parse::<i64>()
        .map_err(|_| "INVALID_STALE_AFTER")?;
    serve(
        Arc::new(Daemon {
            root,
            identity,
            registry,
            observations,
            authority,
            config: json!({"registry":absolute(options.required("registry")?)?}),
            api: None,
            node: Default::default(),
            relay: Default::default(),
            active: Mutex::new(Value::Null),
            record: Mutex::new(json!({})),
            collector,
            source,
            port: Mutex::new(0),
            capabilities: json!(["session.read"]),
            health: None,
            stale_after: stale,
            local_codex_host_id: options.get("local-codex-host-id").map(str::to_owned),
            provider: None,
            relay_enabled: false,
            provider_timeout: 3.,
            helper: false,
        }),
        options.get("bind").unwrap_or("127.0.0.1"),
        options.port(8771)?,
        &gateways,
    )
}
fn pairing_code() -> Result<String> {
    use std::io::IsTerminal;
    let input = std::io::stdin();
    let saved = if input.is_terminal() {
        Some(rustix::termios::tcgetattr(&input).map_err(|_| "PAIRING_CODE_REQUIRED")?)
    } else {
        None
    };
    let stopped = Arc::new(AtomicBool::new(false));
    let mut signals = vec![];
    // Install handlers before hiding terminal echo; cancellation must restore it.
    if saved.is_some() {
        for sig in [signal_hook::consts::SIGINT, signal_hook::consts::SIGTERM] {
            match signal_hook::flag::register(sig, stopped.clone()) {
                Ok(id) => signals.push(id),
                Err(_) => {
                    for id in signals {
                        signal_hook::low_level::unregister(id);
                    }
                    return Err("SIGNAL_REGISTRATION_FAILED".into());
                }
            }
        }
    }
    let outcome = (|| -> Result<String> {
        if let Some(saved) = &saved {
            let mut hidden = saved.clone();
            hidden.local_modes.remove(rustix::termios::LocalModes::ECHO);
            rustix::termios::tcsetattr(&input, rustix::termios::OptionalActions::Flush, &hidden)
                .map_err(|_| "PAIRING_CODE_REQUIRED")?;
        }
        eprint!("Pairing code: ");
        if saved.is_some() {
            loop {
                if stopped.load(Ordering::Relaxed) {
                    return Err("PAIRING_CANCELLED".into());
                }
                let mut fds = [rustix::event::PollFd::new(
                    &input,
                    rustix::event::PollFlags::IN,
                )];
                match rustix::event::poll(
                    &mut fds,
                    Some(&rustix::event::Timespec {
                        tv_sec: 0,
                        tv_nsec: 100_000_000,
                    }),
                ) {
                    Ok(n) if n > 0 => break,
                    Ok(_) | Err(rustix::io::Errno::INTR) => (),
                    Err(_) => return Err("PAIRING_CODE_REQUIRED".into()),
                }
            }
        }
        let mut code = String::new();
        input
            .read_line(&mut code)
            .map_err(|_| "PAIRING_CODE_REQUIRED")?;
        Ok(code)
    })();
    if let Some(saved) = &saved {
        let _ = rustix::termios::tcsetattr(&input, rustix::termios::OptionalActions::Flush, saved);
        eprintln!();
    }
    for id in signals {
        signal_hook::low_level::unregister(id);
    }
    outcome
}

fn pairing(options: &Options, root: &Path, identity: &Value) -> Result<()> {
    let state = Arc::new(crate::discovery_state::Runtime::default());
    let transport = crate::discovery_enrollment::Runtime::default();
    if options.command == "pair-listen" {
        let server=transport.invoke("discovery.enrollment.server.start",&json!({"root":root,"address":options.get("bind").unwrap_or("127.0.0.1"),"port":options.port(8770)?,"allow_direct":true}),state.clone(),|_,_|Err("DISCOVERY_CALLBACK_INVALID".into()))?;
        let code = state.invoke(
            "discovery.window.open",
            &json!({"window":server["window"],"lifetime":60}),
            |_, _| Err("DISCOVERY_CALLBACK_INVALID".into()),
        )?;
        println!(
            "{}",
            json!({"identity":public(identity)?,"port":server["port"],"pairing_code":code})
        );
        let stop = Arc::new(AtomicBool::new(false));
        for sig in [signal_hook::consts::SIGINT, signal_hook::consts::SIGTERM] {
            signal_hook::flag::register(sig, stop.clone())
                .map_err(|_| "SIGNAL_REGISTRATION_FAILED")?;
        }
        let until = Instant::now()
            + Duration::from_secs(
                options
                    .get("seconds")
                    .unwrap_or("120")
                    .parse::<u64>()
                    .map_err(|_| "INVALID_SECONDS")?
                    .clamp(1, 120),
            );
        while Instant::now() < until && !stop.load(Ordering::Relaxed) {
            thread::sleep(Duration::from_millis(20));
        }
        state.invoke(
            "discovery.window.close",
            &json!({"window":server["window"]}),
            |_, _| Err("DISCOVERY_CALLBACK_INVALID".into()),
        )?;
        transport.invoke(
            "discovery.enrollment.server.close",
            &json!({"server":server["server"]}),
            state,
            |_, _| Err("DISCOVERY_CALLBACK_INVALID".into()),
        )?;
    } else {
        let endpoint = node_tls::endpoint(options.required("endpoint")?)?;
        // Existing interactive pairing secret, never argv or stdout. Redirected
        // stdin remains available to isolated CLI qualification fixtures.
        let code = pairing_code()?;
        let result=transport.invoke("discovery.enrollment.client.pair",&json!({"root":root,"candidate":{"node_id":options.required("peer")?,"display_name":options.required("peer")?,"fingerprint":options.required("fingerprint")?,"endpoints":[[endpoint.0,endpoint.1]],"mismatch":false},"code":code.trim_end(),"allow_direct":true}),state,|_,_|Err("DISCOVERY_CALLBACK_INVALID".into()))?;
        println!(
            "{}",
            json!({"paired":true,"node_id":result["node_id"],"authority_granted":false})
        );
    }
    Ok(())
}
pub fn main_cli(args: Vec<String>) -> i32 {
    if args.first().is_some_and(|v| v == "--daemon-contract") {
        println!("RUST_NODE_DAEMON_V1");
        return 0;
    }
    if args.first().is_some_and(|v| v == "--help") {
        println!(
            "clinx-node-daemon helper | --state PATH --node-id ID bootstrap|serve|share|configure-node|pair-listen|pair-connect"
        );
        return 0;
    }
    let result = if args.first().is_some_and(|v| v == "helper") {
        helper_main()
    } else {
        Options::parse(args).and_then(centre_main)
    };
    match result {
        Ok(()) => 0,
        Err(code) => {
            eprintln!(
                "{}",
                json!({"error_code":code,"runtime":"RUST_NODE_DAEMON_V1"})
            );
            1
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn node_request_hash_preserves_legacy_compact_python_json() {
        assert_eq!(
            payload_wire(&json!({"unicode":"中😀\u{7f}","n":1e-5})),
            "{\"n\":1e-05,\"unicode\":\"\\u4e2d\\ud83d\\ude00\\u007f\"}"
        );
    }
}
