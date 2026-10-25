//! Existing-daemon compatibility sessions. Rust owns transport, request identity,
//! single-send delivery and bounded observation. This is not task authority.
mod observation;
use crate::provider;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

type Result<T> = std::result::Result<T, String>;
type Connection = Arc<Mutex<Option<provider::Client>>>;
#[derive(Default)]
pub struct Runtime {
    next: AtomicU64,
    connections: Mutex<BTreeMap<u64, Connection>>,
}
#[derive(Debug)]
struct Fault {
    kind: &'static str,
    message: String,
    remote: Option<Value>,
}
impl Fault {
    fn new(kind: &'static str, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
            remote: None,
        }
    }
    fn transport(message: impl Into<String>) -> Self {
        Self::new("transport", message)
    }
    fn wire(self) -> Value {
        json!({"kind":self.kind,"message":self.message,"remote":self.remote})
    }
}
fn fault(code: String) -> Fault {
    let kind = if matches!(
        code.as_str(),
        "PROVIDER_FRAME_INVALID"
            | "PROVIDER_PROTOCOL_INVALID"
            | "PROVIDER_HANDSHAKE_INVALID"
            | "PROVIDER_MESSAGE_TOO_LARGE"
    ) {
        "protocol"
    } else {
        "transport"
    };
    Fault::new(kind, code)
}
fn duration(value: &Value, zero: bool) -> Result<Duration> {
    let n = value.as_f64().ok_or("PROVIDER_TIMEOUT_INVALID")?;
    if !n.is_finite() || n < 0.0 || (!zero && n == 0.0) {
        return Err("PROVIDER_TIMEOUT_INVALID".into());
    }
    let duration = Duration::try_from_secs_f64(n).map_err(|_| "PROVIDER_TIMEOUT_INVALID")?;
    Instant::now()
        .checked_add(duration)
        .ok_or("PROVIDER_TIMEOUT_INVALID")?;
    Ok(duration)
}
trait Transport {
    fn send(&mut self, message: &Value) -> std::result::Result<(), Fault>;
    fn receive(&mut self, timeout: Duration) -> std::result::Result<Value, Fault>;
    fn mark(&mut self, id: &Value, outcome: &str) -> std::result::Result<(), Fault>;
    fn consume_creation(&mut self, cwd: &Value) -> std::result::Result<(), Fault>;
}
struct Native<'a, F> {
    client: &'a mut Option<provider::Client>,
    token: Value,
    callback: &'a mut F,
}
impl<F: FnMut(&str, Value) -> Result<Value>> Transport for Native<'_, F> {
    fn send(&mut self, message: &Value) -> std::result::Result<(), Fault> {
        let result = self
            .client
            .as_mut()
            .ok_or_else(|| Fault::transport("app-server transport is not connected"))?
            .send_message(message);
        if result.is_err() {
            self.client.take();
        }
        result.map_err(fault)
    }
    fn receive(&mut self, timeout: Duration) -> std::result::Result<Value, Fault> {
        let result = self
            .client
            .as_mut()
            .ok_or_else(|| Fault::transport("app-server transport is not connected"))?
            .poll_message(timeout);
        match result {
            Ok(Some(v)) => Ok(v),
            Ok(None) => Err(Fault::transport(
                "timed out waiting for app-server response",
            )),
            Err(e) => {
                self.client.take();
                Err(fault(e))
            }
        }
    }
    fn mark(&mut self, _: &Value, _: &str) -> std::result::Result<(), Fault> {
        Ok(())
    }
    fn consume_creation(&mut self, cwd: &Value) -> std::result::Result<(), Fault> {
        Callback {
            token: self.token.clone(),
            callback: self.callback,
        }
        .consume_creation(cwd)
    }
}
struct Callback<'a, F> {
    token: Value,
    callback: &'a mut F,
}
impl<F: FnMut(&str, Value) -> Result<Value>> Callback<'_, F> {
    fn call(&mut self, operation: &str, args: Value) -> std::result::Result<Value, Fault> {
        let value = (self.callback)(
            "provider.client.io",
            json!({"token":self.token,"operation":operation,"arguments":args}),
        )
        .map_err(Fault::transport)?;
        if let Some(e) = value.get("error") {
            let kind = match e["kind"].as_str() {
                Some("protocol") => "protocol",
                Some("shared_writer") => "shared_writer",
                _ => "transport",
            };
            return Err(Fault::new(
                kind,
                e["message"].as_str().unwrap_or("PROVIDER_DELIVERY_UNKNOWN"),
            ));
        }
        value
            .get("result")
            .cloned()
            .ok_or_else(|| Fault::transport("PROVIDER_DELIVERY_UNKNOWN"))
    }
}
impl<F: FnMut(&str, Value) -> Result<Value>> Transport for Callback<'_, F> {
    fn send(&mut self, v: &Value) -> std::result::Result<(), Fault> {
        self.call("send", json!({"message":v})).map(|_| ())
    }
    fn receive(&mut self, timeout: Duration) -> std::result::Result<Value, Fault> {
        self.call("receive", json!({"timeout":timeout.as_secs_f64()}))
    }
    fn mark(&mut self, id: &Value, outcome: &str) -> std::result::Result<(), Fault> {
        self.call("mark", json!({"id":id,"outcome":outcome}))
            .map(|_| ())
    }
    fn consume_creation(&mut self, cwd: &Value) -> std::result::Result<(), Fault> {
        self.call("consume_creation", json!({"cwd":cwd}))
            .map(|_| ())
    }
}
struct Events {
    methods: Vec<Value>,
    received: Vec<Value>,
    overflow: bool,
    max_methods: usize,
    max_received: usize,
}
impl Events {
    fn read(v: &Value) -> Result<Self> {
        let max_methods = v["max_method_history"]
            .as_u64()
            .filter(|n| *n > 0)
            .ok_or("PROVIDER_EVENT_CAPACITY_INVALID")? as usize;
        let max_received = v["max_received_events"]
            .as_u64()
            .filter(|n| *n > 0)
            .ok_or("PROVIDER_EVENT_CAPACITY_INVALID")? as usize;
        Ok(Self {
            methods: v["events"].as_array().cloned().unwrap_or_default(),
            received: v["received_events"].as_array().cloned().unwrap_or_default(),
            overflow: v["overflow"].as_bool().unwrap_or(false),
            max_methods,
            max_received,
        })
    }
    fn record(&mut self, message: &Value) {
        if message["method"].is_string() {
            self.methods.push(message["method"].clone());
            if self.methods.len() > self.max_methods {
                self.methods.drain(..self.methods.len() - self.max_methods);
            }
        }
        if self.received.len() < self.max_received - 1 && !self.overflow {
            self.received.push(message.clone());
        } else if !self.overflow {
            self.overflow = true;
            self.received.push(overflow());
        }
    }
    fn wire(self) -> Value {
        json!({"events":self.methods,"received_events":self.received,"overflow":self.overflow})
    }
}
fn overflow() -> Value {
    json!({"method":"clinx/observation_overflow","params":{"dropped":"bounded_event_buffer_exhausted"}})
}
fn guard(a: &Value, creation_consumed: &mut bool) -> std::result::Result<(), Fault> {
    let method = a["request"]["method"]
        .as_str()
        .ok_or_else(|| Fault::new("protocol", "app-server method must be a string"))?;
    let params = &a["request"]["params"];
    let policy = &a["policy"];
    let forwarding = !policy["native_forwarding_thread_id"].is_null()
        && policy["native_forwarding_thread_id"] == params["threadId"];
    let creating = method == "thread/start"
        && !policy["native_creation_cwd"].is_null()
        && policy["native_creation_cwd"] == params["cwd"]
        && params["ephemeral"] == false
        && params["threadSource"] == "user"
        && !truthy(&params["dynamicTools"])
        && !truthy(&params["developerInstructions"]);
    if truthy(&policy["shared_native_writer"])
        && matches!(method, "thread/start" | "thread/resume" | "turn/start")
        && !(creating || (forwarding && matches!(method, "thread/resume" | "turn/start")))
    {
        return Err(Fault::new(
            "shared_writer",
            format!("Managed {method} on the shared Desktop daemon lacks atomic writer admission"),
        ));
    }
    let metadata = method == "command/exec"
        && !params.is_null()
        && params == &policy["workspace_metadata_request"];
    if truthy(&policy["read_only_observer"])
        && !metadata
        && !matches!(
            method,
            "initialize" | "thread/read" | "thread/turns/list" | "thread/items/list"
        )
        && !(method == "thread/list" && params["useStateDbOnly"] == true)
    {
        return Err(Fault::new(
            "protocol",
            "Read-only native observer cannot claim or mutate a thread",
        ));
    }
    *creation_consumed = creating;
    Ok(())
}
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
        Value::Number(n) => n.as_f64() != Some(0.0),
    }
}
fn request(
    io: &mut dyn Transport,
    a: &Value,
    events: &mut Events,
    consumed: &mut bool,
) -> std::result::Result<Value, Fault> {
    guard(a, consumed)?;
    let request = &a["request"];
    let id = request
        .get("id")
        .filter(|v| !v.is_null())
        .ok_or_else(|| Fault::new("protocol", "app-server request id required"))?;
    let timeout = duration(&a["timeout"], false).map_err(Fault::transport)?;
    if *consumed {
        io.consume_creation(&request["params"]["cwd"])?;
    }
    io.send(request)?;
    let deadline = Instant::now() + timeout;
    loop {
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .filter(|v| !v.is_zero())
            .ok_or_else(|| {
                Fault::transport(format!(
                    "timed out waiting for {} response",
                    request["method"].as_str().unwrap()
                ))
            })?;
        let message = io.receive(remaining)?;
        if !message.is_object() {
            return Err(Fault::new(
                "protocol",
                "app-server message must be a JSON object",
            ));
        }
        if message.get("method").is_some() && message.get("id").is_some() {
            events.record(&message);
            continue;
        }
        if message.get("id") != Some(id) {
            if message["method"].is_string() {
                events.record(&message);
            }
            continue;
        }
        if let Some(error) = message.get("error") {
            let remote = if error.is_object() {
                error.clone()
            } else {
                json!({"message":error.as_str().map(str::to_owned).unwrap_or_else(||error.to_string())})
            };
            io.mark(id, "explicit_rejection")?;
            return Err(Fault {
                kind: "remote",
                message: "app-server rejected request".into(),
                remote: Some(remote),
            });
        }
        let Some(result) = message.get("result") else {
            io.mark(id, "unknown")?;
            return Err(Fault::new(
                "protocol",
                format!(
                    "{} response has no result or error",
                    request["method"].as_str().unwrap()
                ),
            ));
        };
        io.mark(id, "accepted")?;
        return Ok(result.clone());
    }
}
fn confirm(
    io: &mut dyn Transport,
    a: &Value,
    events: &mut Events,
) -> std::result::Result<Value, Fault> {
    let deadline = Instant::now() + duration(&a["timeout"], false).map_err(Fault::transport)?;
    let thread = &a["thread_id"];
    let turn = &a["turn"];
    loop {
        let mut candidates: Vec<&Value> = turn["items"]
            .as_array()
            .map(|items| items.iter().collect())
            .unwrap_or_default();
        for event in &events.received {
            let p = &event["params"];
            if &p["threadId"] == thread && p["turnId"] == turn["id"] {
                candidates.push(&p["item"]);
            }
        }
        for item in candidates {
            if item["type"] == "userMessage"
                && item["clientId"] == a["client_id"]
                && item["id"].is_string()
                && item["content"].as_array().is_some_and(|v| {
                    v.len() == 1 && v[0]["type"] == "text" && v[0]["text"] == a["prompt"]
                })
            {
                return Ok(
                    json!({"mode":"NATIVE_INPUT_FORWARDING","exclusive_writer":false,
                    "thread_id":thread,"turn_id":turn["id"],"client_message_id":a["client_id"],
                    "native_item_id":item["id"],"input_confirmed":true}),
                );
            }
        }
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .filter(|n| !n.is_zero())
            .ok_or_else(|| {
                Fault::new(
                    "protocol",
                    "NATIVE_INPUT_UNCONFIRMED: request may have been accepted; do not replay",
                )
            })?;
        let message = io.receive(remaining)?;
        if (message.get("method").is_some() && message.get("id").is_some())
            || message["method"].is_string()
        {
            events.record(&message);
        }
    }
}

fn drain(
    io: &mut dyn Transport,
    a: &Value,
    events: &mut Events,
    partial: &mut Vec<Value>,
) -> std::result::Result<Value, Fault> {
    let max = a["max_events"]
        .as_u64()
        .filter(|n| (1..=100).contains(n))
        .ok_or_else(|| Fault::new("protocol", "max_events must be an integer from 1 to 100"))?
        as usize;
    let timeout = duration(&a["timeout"], true)
        .map_err(|_| Fault::new("protocol", "timeout_seconds must not be negative"))?;
    if events.overflow {
        events.received.clear();
        return Ok(json!([overflow()]));
    }
    if !events.received.is_empty() {
        partial.extend(events.received.drain(..max.min(events.received.len())));
        if partial.len() >= max || timeout.is_zero() {
            return Ok(json!(partial));
        }
    }
    let deadline = Instant::now() + timeout;
    while partial.len() < max {
        if timeout.is_zero() && !partial.is_empty() {
            break;
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        let message = match io.receive(remaining) {
            Ok(v) => v,
            Err(e)
                if e.kind == "transport"
                    && (e.message.to_lowercase().contains("timeout")
                        || e.message.to_lowercase().contains("timed out"))
                    && (!partial.is_empty() || timeout.is_zero()) =>
            {
                break;
            }
            Err(e) => return Err(e),
        };
        let server_request = message.get("method").is_some() && message.get("id").is_some();
        if !server_request && !message["method"].is_string() {
            return Err(Fault::new(
                "protocol",
                "unexpected non-event message while observing",
            ));
        }
        let before = events.received.len();
        events.record(&message);
        partial.push(message.clone());
        events.received.truncate(before);
        if events.overflow {
            partial.push(overflow());
            events.received.clear();
            break;
        }
        if !server_request && message["method"] == "clinx/observation_overflow" {
            break;
        }
    }
    Ok(json!(partial))
}

impl Runtime {
    fn connection(&self, args: &Value) -> Result<Connection> {
        self.connections
            .lock()
            .map_err(|_| "PROVIDER_SESSION_UNAVAILABLE")?
            .get(&args["session"].as_u64().ok_or("PROVIDER_SESSION_INVALID")?)
            .cloned()
            .ok_or_else(|| "PROVIDER_SESSION_UNAVAILABLE".into())
    }
    pub fn invoke<F: FnMut(&str, Value) -> Result<Value>>(
        &self,
        operation: &str,
        args: &Value,
        mut callback: F,
    ) -> Result<Value> {
        if operation.starts_with("provider.client.observation.") {
            return observation::invoke(operation, args, callback);
        }
        if operation == "provider.client.connect" {
            let endpoint = args["endpoint"]
                .as_str()
                .ok_or("PROVIDER_ENDPOINT_INVALID")?;
            let timeout = duration(&args["timeout"], false)?;
            let client = provider::Client::connect_uninitialized(Path::new(endpoint), timeout)?;
            let id = self.next.fetch_add(1, Ordering::Relaxed) + 1;
            let mut connections = self
                .connections
                .lock()
                .map_err(|_| "PROVIDER_SESSION_UNAVAILABLE")?;
            if connections.len() >= 256 {
                return Err("PROVIDER_SESSION_CAPACITY_EXCEEDED".into());
            }
            connections.insert(id, Arc::new(Mutex::new(Some(client))));
            return Ok(json!({"session":id}));
        }
        if operation == "provider.client.close" {
            let Some(connection) = self
                .connections
                .lock()
                .map_err(|_| "PROVIDER_SESSION_UNAVAILABLE")?
                .remove(&args["session"].as_u64().ok_or("PROVIDER_SESSION_INVALID")?)
            else {
                return Ok(Value::Null);
            };
            connection
                .lock()
                .map_err(|_| "PROVIDER_SESSION_UNAVAILABLE")?
                .take();
            return Ok(Value::Null);
        }
        let connection = if args.get("session").is_some() {
            Some(self.connection(args)?)
        } else {
            None
        };
        let mut locked = if let Some(connection) = &connection {
            Some(
                connection
                    .lock()
                    .map_err(|_| "PROVIDER_SESSION_UNAVAILABLE")?,
            )
        } else {
            None
        };
        if operation == "provider.client.peer_uid" {
            let guard = locked.as_ref().ok_or("PROVIDER_SESSION_INVALID")?;
            return guard
                .as_ref()
                .ok_or("PROVIDER_SESSION_UNAVAILABLE")?
                .local_peer_uid()
                .map(|uid| json!(uid));
        }
        if operation == "provider.client.lifecycle" {
            return Ok(json!(if locked.as_ref().is_some_and(|v| v.is_some()) {
                "available"
            } else {
                "unavailable"
            }));
        }
        let mut native;
        let mut cb;
        let io: &mut dyn Transport = if let Some(guard) = locked.as_mut() {
            native = Native {
                client: guard,
                token: args["callback_token"].clone(),
                callback: &mut callback,
            };
            &mut native
        } else {
            cb = Callback {
                token: args["callback_token"].clone(),
                callback: &mut callback,
            };
            &mut cb
        };
        match operation {
            "provider.client.send" => Ok(match io.send(&args["message"]) {
                Ok(()) => json!({"value":null}),
                Err(e) => json!({"error":e.wire()}),
            }),
            "provider.client.receive" => Ok(match io.receive(duration(&args["timeout"], true)?) {
                Ok(v) => json!({"value":v}),
                Err(e) => json!({"error":e.wire()}),
            }),
            "provider.client.record" => {
                let mut events = Events::read(&args["state"])?;
                events.record(&args["message"]);
                Ok(json!({"state":events.wire(),"value":null}))
            }
            "provider.client.confirm" => {
                let mut events = Events::read(&args["state"])?;
                let result = confirm(io, args, &mut events);
                let mut output = json!({"state":events.wire()});
                match result {
                    Ok(v) => output["value"] = v,
                    Err(e) => output["error"] = e.wire(),
                }
                Ok(output)
            }
            "provider.client.drain" => {
                let mut events = Events::read(&args["state"])?;
                let mut partial = Vec::new();
                let result = drain(io, args, &mut events, &mut partial);
                let mut output = json!({"state":events.wire()});
                match result {
                    Ok(v) => output["value"] = v,
                    Err(e) => {
                        output["error"] = e.wire();
                        output["drained_events"] = json!(partial);
                    }
                }
                Ok(output)
            }
            "provider.client.request" => {
                let mut events = Events::read(&args["state"])?;
                let mut consumed = false;
                let result = request(io, args, &mut events, &mut consumed);
                let mut output = json!({"state":events.wire(),"creation_consumed":consumed});
                match result {
                    Ok(v) => output["value"] = v,
                    Err(e) => output["error"] = e.wire(),
                }
                Ok(output)
            }
            _ => Err("PROVIDER_CLIENT_OPERATION_UNKNOWN".into()),
        }
    }
}
