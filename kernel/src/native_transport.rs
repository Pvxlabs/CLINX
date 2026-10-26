//! Native execution orchestration. Authority commands are the only durable
//! decisions; Provider reads, never a turn/start ACK, prove exact input/outcome.
use crate::{
    MAX_FRAME_BYTES,
    authority_store::PROTOCOL,
    provider::{Client, Provider, Result},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    io::{BufRead, BufReader, Read, Write},
    os::unix::{
        fs::{FileTypeExt, MetadataExt},
        net::UnixStream,
    },
    path::{Path, PathBuf},
    time::Duration,
};

/// Connect only before transmission, bounded even when an AF_UNIX backlog is full.
/// No request has been sent here, and no caller may retry after write begins.
pub fn connect_local(endpoint: &Path, timeout: Duration) -> std::io::Result<UnixStream> {
    use rustix::{
        event::{PollFd, PollFlags, Timespec, poll},
        io::Errno,
        net::{self, AddressFamily, SocketAddrUnix, SocketType},
    };
    use std::{io, thread, time::Instant};
    let fd = crate::platform::socket(AddressFamily::UNIX, SocketType::STREAM, None, true)?;
    let address = SocketAddrUnix::new(endpoint)?;
    let deadline = Instant::now()
        .checked_add(timeout)
        .ok_or(io::ErrorKind::InvalidInput)?;
    loop {
        if Instant::now() >= deadline {
            return Err(io::ErrorKind::TimedOut.into());
        }
        match net::connect(&fd, &address) {
            Ok(()) => break,
            Err(Errno::INTR) => continue,
            Err(Errno::AGAIN) => thread::sleep(
                Duration::from_millis(10).min(deadline.saturating_duration_since(Instant::now())),
            ),
            Err(Errno::INPROGRESS) => {
                loop {
                    let remaining = deadline.saturating_duration_since(Instant::now());
                    if remaining.is_zero() {
                        return Err(io::ErrorKind::TimedOut.into());
                    }
                    let timeout = Timespec::try_from(remaining).map_err(io::Error::other)?;
                    match poll(&mut [PollFd::new(&fd, PollFlags::OUT)], Some(&timeout)) {
                        Ok(0) => return Err(io::ErrorKind::TimedOut.into()),
                        Err(Errno::INTR) => continue,
                        result => {
                            result?;
                            net::sockopt::socket_error(&fd)??;
                            break;
                        }
                    }
                }
                break;
            }
            Err(error) => return Err(error.into()),
        }
    }
    let stream = UnixStream::from(fd);
    stream.set_nonblocking(false)?;
    Ok(stream)
}

pub trait Authority {
    fn call(&mut self, operation: &str, payload: Value, id: &str) -> Result<Value>;
}
// Full Provider turns are durable evidence, not public API requests. Only the
// authenticated Transport peer's two evidence commits have this larger bound.
// Responses and all other requests retain the public 1 MiB frame limit.
pub const MAX_PROVIDER_EVIDENCE_FRAME_BYTES: usize = 4 * MAX_FRAME_BYTES;
pub fn request_frame_limit(operation: &str, transport_peer: bool) -> usize {
    if transport_peer
        && matches!(
            operation,
            "business.observe_commit" | "business.adoption_commit"
        )
    {
        MAX_PROVIDER_EVIDENCE_FRAME_BYTES
    } else {
        MAX_FRAME_BYTES
    }
}
#[derive(Clone)]
pub struct UnixAuthority {
    pub endpoint: PathBuf,
    pub uid: u32,
}
impl Authority for UnixAuthority {
    fn call(&mut self, operation: &str, payload: Value, id: &str) -> Result<Value> {
        let operation = if operation == "health" {
            "health".to_owned()
        } else {
            format!("business.{operation}")
        };
        self.request(&operation, payload, id, Duration::from_secs(10))
    }
}
impl UnixAuthority {
    /// Exact wire request for the existing Python compatibility client. No retry.
    pub fn request(
        &self,
        operation: &str,
        payload: Value,
        id: &str,
        timeout: Duration,
    ) -> Result<Value> {
        let info = self
            .endpoint
            .symlink_metadata()
            .map_err(|_| "AUTHORITY_UNAVAILABLE")?;
        let parent = self
            .endpoint
            .parent()
            .ok_or("AUTHORITY_ENDPOINT_UNTRUSTED")?
            .symlink_metadata()
            .map_err(|_| "AUTHORITY_UNAVAILABLE")?;
        if !self.endpoint.is_absolute()
            || !info.file_type().is_socket()
            || info.uid() != self.uid
            || info.mode() & 7 != 0
            || !parent.is_dir()
            || parent.uid() != self.uid
            || parent.mode() & 0o022 != 0
        {
            return Err("AUTHORITY_ENDPOINT_UNTRUSTED".into());
        }
        let mut wire = serde_json::to_vec(&json!({"protocol_version":PROTOCOL,"operation":operation,"request_id":id,"payload":payload})).map_err(|_| "INVALID_REQUEST")?;
        wire.push(b'\n');
        if wire.len() > request_frame_limit(operation, true) {
            return Err("REQUEST_TOO_LARGE".into());
        }
        let mut socket =
            connect_local(&self.endpoint, timeout).map_err(|_| "AUTHORITY_UNAVAILABLE")?;
        #[cfg(target_os = "linux")]
        if rustix::net::sockopt::socket_peercred(&socket)
            .map_err(|_| "AUTHORITY_PEER_UNTRUSTED")?
            .uid
            .as_raw()
            != self.uid
        {
            return Err("AUTHORITY_PEER_UNTRUSTED".into());
        }
        // Remote Nodes use the paired network route, not UnixAuthority or a
        // fabricated Linux peer identity. This client is for the Centre worker.
        #[cfg(not(target_os = "linux"))]
        {
            return Err("LINUX_AUTHORITY_IPC_REQUIRED".into());
        }
        socket
            .set_read_timeout(Some(timeout))
            .and_then(|_| socket.set_write_timeout(Some(timeout)))
            .map_err(|_| "AUTHORITY_UNAVAILABLE")?;
        socket
            .write_all(&wire)
            .map_err(|_| "AUTHORITY_DELIVERY_UNKNOWN")?;
        let mut frame = Vec::new();
        BufReader::new(socket)
            .take((MAX_FRAME_BYTES + 1) as u64)
            .read_until(b'\n', &mut frame)
            .map_err(|_| "AUTHORITY_DELIVERY_UNKNOWN")?;
        if frame.len() > MAX_FRAME_BYTES || frame.last() != Some(&b'\n') {
            return Err("AUTHORITY_DELIVERY_UNKNOWN".into());
        }
        let response: Value =
            serde_json::from_slice(&frame).map_err(|_| "AUTHORITY_DELIVERY_UNKNOWN")?;
        if response["protocol_version"] != PROTOCOL
            || response["request_id"] != id
            || response.get("error").is_some() == response.get("result").is_some()
        {
            return Err("AUTHORITY_DELIVERY_UNKNOWN".into());
        }
        if let Some(error) = response["error"].as_str() {
            return Err(error.to_owned());
        }
        response
            .get("result")
            .filter(|v| v.is_object())
            .cloned()
            .ok_or_else(|| "AUTHORITY_DELIVERY_UNKNOWN".into())
    }
}

pub trait Providers {
    type Connection: Provider;
    fn connect(&mut self, endpoint: &str) -> Result<Self::Connection>;
    fn connect_scoped(&mut self, endpoint: &str, _context: Value) -> Result<Self::Connection> {
        self.connect(endpoint)
    }
}
pub struct UnixProviders {
    pub timeout: Duration,
}
impl Providers for UnixProviders {
    type Connection = Client;
    fn connect(&mut self, endpoint: &str) -> Result<Client> {
        Client::connect(Path::new(endpoint), self.timeout)
    }
}

pub struct NativeTransport<A: Authority, P: Providers> {
    pub authority: A,
    pub providers: P,
    pub last_provider_error: Option<Value>,
}
fn text<'a>(v: &'a Value, k: &str) -> Result<&'a str> {
    v[k].as_str()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "NATIVE_IDENTITY_MISSING".into())
}
pub fn stable_id(action: &str, identity: &str) -> String {
    format!(
        "rust-native-{:x}",
        Sha256::digest(format!("{action}\0{identity}").as_bytes())
    )
}
impl<A: Authority, P: Providers> NativeTransport<A, P> {
    pub fn new(authority: A, providers: P) -> Self {
        Self {
            authority,
            providers,
            last_provider_error: None,
        }
    }
    fn call(&mut self, op: &str, p: Value) -> Result<Value> {
        // Non-idempotent challenge/read IDs are unique across process restarts.
        let id = crate::authority_store::random_id()?;
        self.authority.call(op, p, &id)
    }
    fn once(&mut self, op: &str, p: Value, identity: &str) -> Result<Value> {
        self.authority.call(op, p, &stable_id(op, identity))
    }
    fn context(mode: &str, route: &Value, extra: Value) -> Value {
        let mut context = route.as_object().cloned().unwrap_or_default();
        context.insert("mode".into(), json!(mode));
        for (key, value) in extra.as_object().into_iter().flatten() {
            context.insert(key.clone(), value.clone());
        }
        Value::Object(context)
    }
    pub fn adoption(&mut self, reference: &str) -> Result<Value> {
        let challenge = self.call("adoption_begin", json!({"adoption_ref":reference}))?;
        if challenge.get("nonce").is_none() {
            return Ok(challenge);
        }
        let tid = text(&challenge, "thread_id")?;
        let mut observations = Vec::new();
        for endpoint in challenge["endpoints"]
            .as_array()
            .ok_or("PROVIDER_ROUTE_REQUIRED")?
        {
            let mut client = self
                .providers
                .connect_scoped(endpoint.as_str().ok_or("PROVIDER_ROUTE_REQUIRED")?,Self::context("read",&challenge["route"],json!({"thread_id":challenge["thread_id"],"turn_id":challenge.get("exact_turn_id").unwrap_or(&challenge["turn_id"])})))?;
            let mut observed = json!({"endpoint":endpoint,"thread":client.thread(tid)?});
            if let Some(turn) = challenge["exact_turn_id"].as_str() {
                observed["turn"] = client.exact_turn(tid, turn)?;
            }
            observations.push(observed);
        }
        self.call("adoption_commit",json!({"adoption_ref":reference,"nonce":challenge["nonce"],"observations":observations}))
    }
    pub fn recover_input(&mut self, eid: &str) -> Result<Value> {
        let challenge = self.call("input_begin", json!({"execution_ref":eid}))?;
        let tid = text(&challenge, "thread_id")?;
        let mut client = self.providers.connect_scoped(
            text(&challenge, "endpoint")?,
            Self::context(
                "read",
                &challenge["route"],
                json!({"thread_id":tid,"execution_ref":eid}),
            ),
        )?;
        client.thread(tid)?;
        let mut matches = Vec::new();
        let mut turns = std::collections::BTreeSet::new();
        for turn in client.turns(tid, 5, "notLoaded")? {
            let turn_id = text(&turn, "id")?;
            if !turns.insert(turn_id.to_owned()) {
                return Err("NATIVE_TURN_IDENTITY_CONFLICT".into());
            }
            for record in client.pages(
                "thread/items/list",
                json!({"threadId":tid,"turnId":turn_id,"limit":100,"sortDirection":"asc"}),
            )? {
                if record["turnId"] != turn_id {
                    return Err("NATIVE_INPUT_TURN_CONFLICT".into());
                }
                let item = &record["item"];
                if item["type"] == "userMessage"
                    && item["clientId"] == challenge["client_message_id"]
                {
                    matches.push((turn_id.to_owned(), item.clone()));
                }
            }
        }
        if matches.len() != 1 {
            return Err("NATIVE_INPUT_IDENTITY_UNPROVEN".into());
        }
        let (turn_id, item) = matches.pop().unwrap();
        self.call("input_commit",json!({"execution_ref":eid,"nonce":challenge["nonce"],"endpoint":challenge["endpoint"],"thread_id":tid,"turn_id":turn_id,"item":item}))
    }
    /// Recover one existing execution through the original evidence/transaction
    /// contracts. Never dispatch, create a turn, cancel, or touch another task.
    pub fn recover_execution(&mut self, eid: &str) -> Result<Value> {
        match self.recover_input(eid) {
            Ok(_) => (),
            Err(e) if e == "INPUT_RECOVERY_NOT_AVAILABLE" => (),
            Err(e) => return Err(e),
        }
        self.observe(eid)
    }
    pub fn observe(&mut self, eid: &str) -> Result<Value> {
        self.observe_inner(eid, false)
    }
    pub fn observe_host(&mut self, eid: &str) -> Result<Value> {
        self.observe_inner(eid, true)
    }
    fn observe_inner(&mut self, eid: &str, host: bool) -> Result<Value> {
        let challenge = self.call("observe_begin", json!({"execution_ref":eid}))?;
        if challenge.get("nonce").is_none() {
            return Ok(challenge);
        }
        let tid = text(&challenge, "thread_id")?;
        let turn_id = text(&challenge, "turn_id")?;
        let mut observations = Vec::new();
        let mut exact = Vec::new();
        for endpoint in challenge["endpoints"]
            .as_array()
            .ok_or("PROVIDER_ROUTE_REQUIRED")?
        {
            let mut row = json!({"endpoint":endpoint,"state":"UNKNOWN","thread_id":tid,"turn_id":null,"turn_status":null});
            let result = (|| -> Result<(Value, Value)> {
                let mut client = self
                    .providers
                    .connect_scoped(endpoint.as_str().ok_or("PROVIDER_ROUTE_REQUIRED")?,Self::context("read",&challenge["route"],json!({"thread_id":challenge["thread_id"],"turn_id":challenge.get("exact_turn_id").unwrap_or(&challenge["turn_id"])})))?;
                let thread = client.thread(tid)?;
                let turn = client.exact_turn(tid, turn_id)?;
                Ok((thread, turn))
            })();
            if let Ok((thread, turn)) = result {
                row["state"] = thread["status"]
                    .get("type")
                    .unwrap_or(&thread["status"])
                    .clone();
                row["turn_id"] = turn["id"].clone();
                row["turn_status"] = turn["status"].clone();
                exact.push(turn);
            }
            observations.push(row);
        }
        let turn = if !exact.is_empty() && exact.iter().all(|t| t == &exact[0]) {
            exact[0].clone()
        } else {
            json!({})
        };
        if host && turn.is_object() {
            let rows = self.call("host_rows", json!({"execution_ref":eid}))?;
            for item in turn["items"].as_array().into_iter().flatten() {
                let Some(row) = rows["rows"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .find(|r| r["call_id"] == item["id"] && r["delivery_state"] != "DELIVERED")
                else {
                    continue;
                };
                if row["call_state"]=="RUNNING"&&row["delivery_state"]=="FAILED"&&item["success"]==false && self.call("host_reconcile_rejected",json!({"execution_ref":eid,"call_id":item["id"],"endpoint":challenge["endpoints"][0],"turn":turn})).is_ok() {continue;}
                self.call("host_ack",json!({"execution_ref":eid,"call_id":item["id"],"message":{"method":"item/completed","params":{"threadId":tid,"turnId":turn_id,"item":item}}}))?;
            }
        }
        self.call("observe_commit",json!({"execution_ref":eid,"nonce":challenge["nonce"],"observations":observations,"turn":turn}))
    }
    pub fn dispatch(&mut self, eid: &str, resume: bool) -> Result<Value> {
        self.last_provider_error = None;
        let take = if resume {
            self.call("resume_dispatch", json!({"execution_ref":eid}))?
        } else {
            self.once("take_dispatch", json!({"execution_ref":eid}), eid)?
        };
        if take.get("ticket").is_none() {
            return Ok(take);
        }
        let prepared = &take["prepared"];
        if prepared["execution_policy"]["execution_surface"] != "CODEX_NATIVE" {
            return Err("NATIVE_EXECUTION_SURFACE_REQUIRED".into());
        }
        let endpoints = take["endpoints"]
            .as_array()
            .ok_or("EXACT_DESKTOP_ENDPOINT_REQUIRED")?;
        if endpoints.len() != 1 {
            return Err("EXACT_DESKTOP_ENDPOINT_REQUIRED".into());
        }
        let endpoint = endpoints[0]
            .as_str()
            .ok_or("EXACT_DESKTOP_ENDPOINT_REQUIRED")?;
        let scope = json!({"execution_ref":eid,"epoch":take["epoch"],"ticket":take["ticket"]});
        let mut client = self.providers.connect_scoped(endpoint,Self::context("dispatch",&json!({"host":prepared["host"],"project":prepared["project"],"cwd":prepared["workspace_contract"]["cwd"],"node_principal":prepared["node_principal"]}),json!({"execution_ref":eid,"thread_id":take["thread_id"],"prompt":prepared["prompt"],"model":prepared["model"],"effort":prepared["reasoning_effort"]})))?;
        let selection =
            match client.model_selection(&prepared["model"], &prepared["reasoning_effort"]) {
                Ok(v) => v,
                Err(code) => {
                    let detail = client.error_details(&code, "model/list");
                    let mut result = self.once(
                        "fail_before_send",
                        json!({"execution_ref":eid,"reason":code,"provider_error":detail}),
                        eid,
                    )?;
                    result["provider_error"] = detail;
                    return Ok(result);
                }
            };
        let mut thread = if let Some(tid) = take["thread_id"].as_str() {
            // Reading/re-attaching an existing exact thread never replays input.
            // A generic remote error is not proof that the thread does not exist.
            let thread = match client.thread(tid) {
                Ok(thread) => thread,
                Err(error) if error == "NATIVE_THREAD_NOT_LOADED" => {
                    json!({"id":tid,"status":{"type":"notLoaded"}})
                }
                Err(error) => return Err(error),
            };
            if matches!(
                thread["status"]["type"].as_str(),
                Some("notLoaded" | "unloaded")
            ) {
                let thread=client.request("thread/resume",json!({"threadId":tid,"excludeTurns":true}))?["thread"].clone();
                if thread["id"] != tid {
                    return Err("THREAD_IDENTITY_CONFLICT".into());
                }
                thread
            } else {
                thread
            }
        } else {
            let permit = self.call("native_create_take", scope.clone())?;
            if permit["create_permitted"] != true {
                return Err("NATIVE_CREATE_ACK_UNKNOWN".into());
            }
            let mut params = json!({"cwd":prepared["workspace_contract"]["cwd"],"ephemeral":false,"threadSource":"user"});
            if !prepared["model"].is_null() {
                params["model"] = prepared["model"].clone();
            }
            client.request("thread/start", params)?["thread"].clone()
        };
        let tid = text(&thread, "id")?.to_owned();
        if !matches!(thread["status"]["type"].as_str(), Some("idle" | "active")) {
            return Err("NATIVE_WRITER_NOT_IDLE_OR_IDENTITY_CONFLICT".into());
        }
        let cwd = text(&thread, "cwd")?.to_owned();
        let mut binding = scope.clone();
        binding["endpoint"] = json!(endpoint);
        binding["thread_id"] = json!(tid);
        binding["cwd"] = json!(cwd);
        self.call("bind_thread", binding)?;
        // A cached true receipt must never grant another physical send to a
        // concurrent/restarted worker. A lost grant reply leaves SENDING and
        // is recovered exclusively from Provider history.
        let grant = self.call("authorize_send", scope)?;
        if grant["send_permitted"] != true {
            return Ok(grant);
        }
        let mut params = json!({"threadId":tid,"input":[{"type":"text","text":prepared["prompt"]}],"cwd":cwd,"clientUserMessageId":crate::business::native_client_id(&tid,eid),"sandboxPolicy":{"type":"dangerFullAccess"},"approvalPolicy":"never"});
        for (key, value) in selection.as_object().ok_or("PROVIDER_MODEL_LIST_INVALID")? {
            params[key] = value.clone();
        }
        // A lost ACK leaves SENDING in Authority. Reopening this process only
        // scans Provider input; it never repeats turn/start.
        thread = match client.request("turn/start", params) {
            Ok(v) => v,
            Err(code) => {
                self.last_provider_error = Some(client.error_details(&code, "turn/start"));
                return Err(code);
            }
        };
        if thread["turn"]["id"].as_str().is_none() {
            return Err("PROVIDER_DELIVERY_UNKNOWN".into());
        }
        drop(client);
        self.recover_input(eid)
    }
    pub fn metadata(&mut self, eid: &str) -> Result<Value> {
        let challenge = self.call("metadata_begin", json!({"execution_ref":eid}))?;
        if challenge.get("nonce").is_none() {
            return Ok(challenge);
        }
        let m = &challenge["publication"];
        let tid = text(m, "thread_id")?;
        let mut client = self.providers.connect_scoped(
            text(m, "endpoint")?,
            Self::context(
                "metadata",
                &challenge["route"],
                json!({"thread_id":tid,"title":m["title"],"execution_ref":eid}),
            ),
        )?;
        let mut thread = client.thread(tid)?;
        if thread["cwd"] != m["cwd"] {
            return Err("METADATA_IDENTITY_CONFLICT".into());
        }
        if thread["name"].is_null() {
            // Existing metadata semantics permit at-least-once name/set. On a
            // lost ACK a later Provider read proves delivery before any retry.
            let _ = client.request("thread/name/set", json!({"threadId":tid,"name":m["title"]}));
            drop(client);
            thread = self
                .providers
                .connect_scoped(
                    text(m, "endpoint")?,
                    Self::context(
                        "metadata",
                        &challenge["route"],
                        json!({"thread_id":tid,"title":m["title"],"execution_ref":eid}),
                    ),
                )?
                .thread(tid)?;
        }
        self.call("metadata_commit",json!({"execution_ref":eid,"nonce":challenge["nonce"],"endpoint":m["endpoint"],"thread":thread}))
    }
    pub fn tick(&mut self) -> Result<Value> {
        let work = self.call("work", json!({}))?;
        let mut results = Vec::new();
        for row in work["metadata"].as_array().ok_or("INVALID_WORK")? {
            let eid = text(row, "execution_ref")?;
            results.push(match self.metadata(eid) {
                Ok(v) => json!({"metadata_execution_ref":eid,"result":v}),
                Err(e) => json!({"metadata_execution_ref":eid,"status":"BLOCKED","error":e}),
            });
        }
        for adoption in work["adoptions"].as_array().ok_or("INVALID_WORK")? {
            let id = text(adoption, "adoption_ref")?;
            results.push(match self.adoption(id) {
                Ok(v) => json!({"adoption_ref":id,"result":v}),
                Err(e) => json!({"adoption_ref":id,"status":"BLOCKED","error":e}),
            });
        }
        for execution in work["executions"].as_array().ok_or("INVALID_WORK")? {
            let eid = text(execution, "execution_ref")?;
            let p = self.call(
                "get_prepared",
                json!({"prepared_execution_ref":execution["prepared_execution_ref"]}),
            )?;
            if p["execution_policy"]["execution_surface"] != "CODEX_NATIVE" {
                continue;
            }
            let result = (|| -> Result<Value> {
                match execution["dispatch_state"].as_str() {
                    Some("READY" | "TAKEN") if execution["cancel_requested"] != true => {
                        self.dispatch(eid, execution["dispatch_state"] == "TAKEN")
                    }
                    Some("SENDING") => self.recover_input(eid),
                    _ if execution["native_turn_id"].is_string() => {
                        if execution["cancel_requested"] == true {
                            match self.call("take_cancel", json!({"execution_ref":eid})) {
                                Ok(grant) if grant["send_permitted"] == true => {
                                    let mut client =
                                        self.providers.connect_scoped(text(&grant, "endpoint")?,Self::context("cancel",&grant["route"],json!({"thread_id":grant["thread_id"],"turn_id":grant["turn_id"],"execution_ref":eid})))?;
                                    client.request("turn/interrupt",json!({"threadId":grant["thread_id"],"turnId":grant["turn_id"]}))?;
                                }
                                Err(e) if e == "CANCEL_NOT_AVAILABLE" => (),
                                Err(e) => return Err(e),
                                _ => (),
                            }
                        }
                        self.observe(eid)
                    }
                    _ => self.once(
                        "fail_before_send",
                        json!({"execution_ref":eid,"reason":"TRANSPORT_RESTART_BEFORE_SEND"}),
                        eid,
                    ),
                }
            })();
            results.push(match result {
                Ok(v) => json!({"execution_ref":eid,"result":v}),
                Err(error) => json!({"execution_ref":eid,"status":"BLOCKED","error":error,
                    "provider_error":self.last_provider_error.take(),"retry_allowed":false,
                    "recovery_action":"READ_EXACT_ORIGINAL_EXECUTION"}),
            });
        }
        Ok(json!({"authority":"RUST","transport":"RUST_NATIVE","results":results}))
    }
}
