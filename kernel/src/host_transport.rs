//! Exact Host Provider sessions. Process ownership is ephemeral; Authority's
//! input, dispatch and delivery receipts fence all effects across restarts.
use crate::{
    host_executor::{Config, call},
    native_transport::{Authority, NativeTransport, UnixAuthority, UnixProviders},
    provider::{Client, Provider, Result},
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, VecDeque},
    path::Path,
    thread::JoinHandle,
    time::{Duration, Instant},
};

pub struct Session<A: Authority> {
    pub authority: A,
    pub config: Config,
    pub prepared: Value,
    pub execution: String,
    pub endpoint: String,
    pub thread: String,
    pub turn: String,
    connection: String,
    pending: BTreeMap<String, Instant>,
    queue: VecDeque<Value>,
    records: BTreeMap<String, String>,
    timeout: Duration,
}
fn text<'a>(v: &'a Value, k: &str) -> Result<&'a str> {
    v[k].as_str()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "HOST_IDENTITY_REQUIRED".into())
}
fn response(body: Value, success: bool) -> Value {
    json!({"success":success,"contentItems":[{"type":"inputText","text":serde_json::to_string(&body).unwrap()}]})
}
fn failure(code: &str, row: Option<&Value>) -> Value {
    let state = row
        .map(|r| r["execution_state"].clone())
        .unwrap_or(json!("COMMAND_NOT_DISPATCHED"));
    let safe = state == "COMMAND_NOT_DISPATCHED";
    response(
        json!({"result_state":code,"error":code,"execution_state":state,"failure_stage":if safe{"PRE_DISPATCH_VALIDATION"}else{"RESULT_DELIVERY"},"host_dispatched":if safe{json!(false)}else{Value::Null},"side_effect_certainty":if safe{"NOT_EXECUTED"}else{"UNKNOWN"},"delivery_state":"PENDING","continuation_state":if safe{"SAFE_TO_CONTINUE"}else{"RECONCILIATION_REQUIRED"},"retry_allowed":false,"retry_required":false,"reconciliation_required":!safe,"execution_can_continue":safe}),
        false,
    )
}
impl<A: Authority> Session<A> {
    pub fn new(
        authority: A,
        config: Config,
        prepared: Value,
        execution: String,
        endpoint: String,
        thread: String,
        turn: String,
        timeout: Duration,
    ) -> Result<Self> {
        Ok(Self {
            authority,
            config,
            prepared,
            execution,
            endpoint,
            thread,
            turn,
            connection: crate::authority_store::random_id()?,
            pending: BTreeMap::new(),
            queue: VecDeque::new(),
            records: BTreeMap::new(),
            timeout,
        })
    }
    fn rows(&mut self) -> Result<Vec<Value>> {
        Ok(call(
            &mut self.authority,
            "host_rows",
            json!({"execution_ref":self.execution}),
        )?["rows"]
            .as_array()
            .ok_or("INVALID_HOST_ROWS")?
            .clone())
    }
    fn owned(&self, p: &Value) -> bool {
        p["threadId"] == self.thread && p["turnId"] == self.turn
    }
    fn identity(&self) -> Value {
        let mut endpoint = json!({"path":self.endpoint});
        if let Ok(m) = std::fs::metadata(&self.endpoint) {
            use std::os::unix::fs::MetadataExt;
            endpoint["device"] = json!(m.dev());
            endpoint["inode"] = json!(m.ino());
            endpoint["created_ns"] =
                json!(m.ctime() as i128 * 1_000_000_000 + m.ctime_nsec() as i128);
        }
        json!({"thread_id":self.thread,"turn_id":self.turn,"namespace":"clinx","tool":"clinx_host_operation","provider_endpoint":endpoint,"connection_id":self.connection,"listener_id":self.connection,"registry_generation":1,"client_pid":std::process::id()})
    }
    fn ingress(&mut self, request: &Value) -> Result<Value> {
        let identity = self.identity();
        call(
            &mut self.authority,
            "host_receive",
            json!({"execution_ref":self.execution,"call_id":request["params"]["callId"],"request":request,"identity":identity}),
        )
    }
    fn send(&mut self, client: &mut Client, request: &Value, body: Value) -> Result<()> {
        let call_id = text(&request["params"], "callId")?;
        // A successful write is not a Provider ACK. Record it only after send;
        // disconnects never cause response or Host-process replay.
        client.send_message(&json!({"id":request["id"],"result":body}))?;
        call(
            &mut self.authority,
            "host_sent",
            json!({"execution_ref":self.execution,"call_id":call_id,"response_id":request["id"]}),
        )?;
        self.pending
            .insert(call_id.into(), Instant::now() + self.timeout);
        Ok(())
    }
    fn execute(&mut self, client: &mut Client, request: &Value) -> Result<()> {
        let call_id = text(&request["params"], "callId")?.to_owned();
        let result = (|| -> Result<Value> {
            if call(
                &mut self.authority,
                "host_take",
                json!({"execution_ref":self.execution,"call_id":call_id}),
            )?["send_permitted"]
                != true
            {
                return Err("HOST_PERMIT_ALREADY_CONSUMED".into());
            }
            let task = call(
                &mut self.authority,
                "effective_policy",
                json!({"task_ref":self.prepared["task_ref"]}),
            )?["task"]
                .clone();
            self.config.execute(
                &mut self.authority,
                &self.execution,
                &call_id,
                &self.prepared,
                &task,
                &request["params"]["arguments"],
            )
        })();
        let body = match result {
            Ok(mut result) => {
                let completed = result["exit_code"].is_i64();
                let failed = result["exit_code"] != 0;
                let fields = json!({"execution_state":if !completed{"COMMAND_DISPATCHED"}else if failed{"COMMAND_EXECUTION_FAILED"}else{"COMMAND_EXECUTED_RESULT_DELIVERY_PENDING"},"failure_code":if failed{json!("COMMAND_FAILED")}else{Value::Null},"failure_stage":if failed{json!("HOST_EXECUTION")}else{Value::Null},"host_dispatched":true,"result_certainty":"KNOWN","side_effect_certainty":"EXECUTED","delivery_state":"PENDING","delivery_state_scope":"RESPONSE_SNAPSHOT","ack_tracking":"CLINX_TRANSPORT_LEDGER","continuation_state":"SAFE_TO_CONTINUE","execution_can_continue":true,"reconciliation_required":false,"next_call_admission":"PERSISTED_LEDGER","retry_scope":"ORIGINAL_OPERATION","retry_allowed":false,"retry_required":false});
                result
                    .as_object_mut()
                    .unwrap()
                    .extend(fields.as_object().unwrap().clone());
                response(result, true)
            }
            Err(code) => {
                // Authority determines non-dispatch from the durable boundary.
                // No exception type can erase a consumed host_begin permit.
                call(
                    &mut self.authority,
                    "host_failed",
                    json!({"execution_ref":self.execution,"call_id":call_id,"failure_code":code}),
                )?;
                let row = self.rows()?.into_iter().find(|r| r["call_id"] == call_id);
                failure(&code, row.as_ref())
            }
        };
        self.send(client, request, body)
    }
    pub fn event(&mut self, client: &mut Client, message: Value) -> Result<()> {
        let p = &message["params"];
        if !self.owned(p) {
            return Ok(());
        }
        match message["method"].as_str() {
            Some("item/started")
                if matches!(
                    p["item"]["type"].as_str(),
                    Some("dynamicToolCall" | "DynamicToolCall")
                ) =>
            {
                let item = &p["item"];
                let request = json!({"id":null,"params":{"threadId":self.thread,"turnId":self.turn,"callId":item["id"],"namespace":item["namespace"],"tool":item["tool"],"arguments":item["arguments"]}});
                let _ = self.ingress(&request); // No side effect without later exact admission.
            }
            Some("item/completed")
                if matches!(
                    p["item"]["type"].as_str(),
                    Some("dynamicToolCall" | "DynamicToolCall")
                ) =>
            {
                let id = text(&p["item"], "id")?.to_owned();
                if self.rows()?.iter().any(|r| r["call_id"] == id) {
                    call(
                        &mut self.authority,
                        "host_ack",
                        json!({"execution_ref":self.execution,"call_id":id,"message":message}),
                    )?;
                    self.pending.remove(&id);
                }
            }
            Some("item/tool/call") => {
                if !message["id"].is_string() && !message["id"].is_i64() {
                    return Err("INVALID_PROVIDER_REQUEST_ID".into());
                }
                let id = serde_json::to_string(&message["id"]).unwrap();
                let semantic = serde_json::to_string(&message["params"]).unwrap();
                if let Some(previous) = self.records.get(&id) {
                    if previous != &semantic {
                        // Do not replace an original result with a different tool
                        // result under the same RPC id. A distinct fresh call is
                        // durably rejected, without claiming Provider delivery.
                        if let Ok(row) = self.ingress(&message) {
                            if row["call_state"] == "RECEIVED" {
                                call(
                                    &mut self.authority,
                                    "host_failed",
                                    json!({"execution_ref":self.execution,"call_id":p["callId"],"failure_code":"TOOL_REQUEST_ID_CONFLICT"}),
                                )?;
                            }
                        }
                        client.send_message(&json!({"id":message["id"],"error":{"code":-32600,"message":"server request id was reused with different semantics"}}))?;
                    }
                    return Ok(());
                }
                let row = match self.ingress(&message) {
                    Ok(row) => row,
                    Err(code) => {
                        let previous = self
                            .rows()?
                            .into_iter()
                            .find(|r| r["call_id"] == p["callId"]);
                        client.send_message(
                            &json!({"id":message["id"],"result":failure(&code,previous.as_ref())}),
                        )?;
                        return Ok(());
                    }
                };
                if self.records.len() >= 256 {
                    return Err("TOOL_ADMISSION_CAPACITY_EXHAUSTED".into());
                }
                self.records.insert(id, semantic);
                // A replayed request cannot acquire another physical dispatch,
                // nor overwrite the original owner's successful/unknown reply.
                if row["call_state"] != "RECEIVED" {
                    return Ok(());
                }
                if let Some(op) = p.get("operationId").or_else(|| p.get("operation_id")) {
                    if op != &self.execution {
                        call(
                            &mut self.authority,
                            "host_failed",
                            json!({"execution_ref":self.execution,"call_id":p["callId"],"failure_code":"INVALID_TOOL_BINDING"}),
                        )?;
                        return self.send(client, &message, failure("INVALID_TOOL_BINDING", None));
                    }
                }
                if !self.pending.is_empty() {
                    if self.queue.len() >= 256 {
                        return Err("TOOL_ACK_QUEUE_CAPACITY_EXCEEDED".into());
                    }
                    self.queue.push_back(message);
                } else {
                    self.execute(client, &message)?;
                }
            }
            _ => (), // Desktop retains all unrelated approvals and user input.
        }
        while self.pending.is_empty() {
            if let Some(request) = self.queue.pop_front() {
                self.execute(client, &request)?;
            } else {
                break;
            }
        }
        Ok(())
    }
    pub fn supervise(&mut self, client: &mut Client, events: Vec<Value>) -> Result<()> {
        let result = (|| -> Result<()> {
            for message in events {
                self.event(client, message)?;
            }
            let mut observed = Instant::now();
            loop {
                if self.pending.values().any(|at| *at <= Instant::now()) {
                    return Err("PROVIDER_ACK_DEADLINE_EXPIRED".into());
                }
                if let Some(message) = client.poll_message(Duration::from_millis(100))? {
                    self.event(client, message)?;
                }
                if observed.elapsed() >= Duration::from_millis(500) {
                    let status = call(
                        &mut self.authority,
                        "get_status",
                        json!({"execution_ref":self.execution}),
                    )?;
                    if status["resource_state"] != "HELD" {
                        return Ok(());
                    }
                    observed = Instant::now();
                }
            }
        })();
        if result.is_err() {
            for row in self.rows().unwrap_or_default() {
                if row["identity"]["connection_id"] == self.connection
                    && row["delivery_state"] == "PENDING"
                {
                    let _ = call(
                        &mut self.authority,
                        "host_failed",
                        json!({"execution_ref":self.execution,"call_id":row["call_id"],"failure_code":"TRANSPORT_LOST"}),
                    );
                }
            }
        }
        result
    }
}

pub struct HostWorker {
    pub authority: UnixAuthority,
    pub config: Config,
    pub timeout: Duration,
    sessions: BTreeMap<String, JoinHandle<Result<()>>>,
}
impl HostWorker {
    pub fn new(authority: UnixAuthority, config: Config, timeout: Duration) -> Self {
        Self {
            authority,
            config,
            timeout,
            sessions: BTreeMap::new(),
        }
    }
    pub fn tick(&mut self) -> Result<Value> {
        let mut results = vec![];
        let finished: Vec<_> = self
            .sessions
            .iter()
            .filter(|(_, h)| h.is_finished())
            .map(|(id, _)| id.clone())
            .collect();
        for id in finished {
            let result = self
                .sessions
                .remove(&id)
                .unwrap()
                .join()
                .unwrap_or_else(|_| Err("HOST_SESSION_PANICKED".into()));
            results.push(json!({"execution_ref":id,"session":result.err().unwrap_or_else(||"DETACHED".into())}));
        }
        let work = call(&mut self.authority, "work", json!({}))?;
        for e in work["executions"].as_array().ok_or("INVALID_WORK")? {
            let eid = text(e, "execution_ref")?.to_owned();
            let p = call(
                &mut self.authority,
                "get_prepared",
                json!({"prepared_execution_ref":e["prepared_execution_ref"]}),
            )?;
            if p["execution_policy"]["execution_surface"] != "HOST_EXECUTOR" {
                continue;
            }
            if p["host"] != self.config.host {
                results.push(json!({"execution_ref":eid,"status":"BLOCKED","error":"HOST_EXECUTOR_UNAVAILABLE"}));
                continue;
            }
            let result = (|| -> Result<Value> {
                let mut reader = NativeTransport::new(
                    self.authority.clone(),
                    UnixProviders {
                        timeout: self.timeout,
                    },
                );
                if e["cancel_requested"] == true && e["native_turn_id"].is_string() {
                    match call(
                        &mut self.authority,
                        "take_cancel",
                        json!({"execution_ref":eid}),
                    ) {
                        Ok(grant) if grant["send_permitted"] == true => {
                            let mut c = Client::connect(
                                Path::new(text(&grant, "endpoint")?),
                                self.timeout,
                            )?;
                            c.request(
                                "turn/interrupt",
                                json!({"threadId":grant["thread_id"],"turnId":grant["turn_id"]}),
                            )?;
                        }
                        Err(error) if error == "CANCEL_NOT_AVAILABLE" => (),
                        Err(error) => return Err(error),
                        _ => (),
                    }
                }
                if e["dispatch_state"] == "SENDING" {
                    return reader.recover_input(&eid);
                }
                if e["native_turn_id"].is_string() {
                    let observed = reader.observe_host(&eid)?;
                    if observed["resource_state"] != "HELD" || e["cancel_requested"] == true {
                        return Ok(observed);
                    }
                }
                if !self.sessions.contains_key(&eid) {
                    let authority = self.authority.clone();
                    let config = self.config.clone();
                    let timeout = self.timeout;
                    let execution = e.clone();
                    let id = eid.clone();
                    let handle = std::thread::spawn(move || {
                        run_session(authority, config, timeout, &id, execution, p)
                    });
                    self.sessions.insert(eid.clone(), handle);
                }
                Ok(json!({"session":"RUST_HOST"}))
            })();
            results.push(match result {
                Ok(v) => json!({"execution_ref":eid,"result":v}),
                Err(error) => json!({"execution_ref":eid,"status":"BLOCKED","error":error}),
            });
        }
        Ok(json!({"transport":"RUST_HOST","results":results}))
    }
    pub fn has_sessions(&self) -> bool {
        !self.sessions.is_empty()
    }
}

fn successor(
    authority: &mut UnixAuthority,
    client: &mut Client,
    scope: &Value,
    p: &Value,
    endpoint: &str,
    config: &Config,
    timeout: Duration,
) -> Result<Value> {
    let h = call(authority, "successor_begin", scope.clone())?;
    if h["state"] == "CREATING" {
        return Err("SUCCESSOR_CREATE_ACK_UNKNOWN".into());
    }
    let source = &h["source"];
    let tid = text(source, "thread_id")?;
    let turn = text(&source["turn"], "id")?;
    let endpoints = call(authority, "adoption_endpoints", json!({"host":p["host"]}))?;
    let mut observations = vec![];
    for endpoint in endpoints["endpoints"]
        .as_array()
        .ok_or("PROVIDER_ROUTE_REQUIRED")?
    {
        let mut c = Client::connect(
            Path::new(endpoint.as_str().ok_or("PROVIDER_ROUTE_REQUIRED")?),
            timeout,
        )?;
        observations.push(
            json!({"endpoint":endpoint,"thread":c.thread(tid)?,"turn":c.exact_turn(tid,turn)?}),
        );
    }
    let mut payload = scope.clone();
    payload["nonce"] = h["nonce"].clone();
    payload["observations"] = json!(observations);
    if call(authority, "successor_take", payload)?["send_permitted"] != true {
        return Err("SUCCESSOR_CREATE_ACK_UNKNOWN".into());
    }
    let context = json!({"predecessor_thread_id":tid,"predecessor_turn_id":turn,"task_ref":p["task_ref"],"predecessor_turn":source["turn"],"handoff_nonce":h["nonce"]});
    let mut params = json!({"cwd":p["workspace_contract"]["cwd"],"ephemeral":false,"dynamicTools":[config.dynamic_tool(&p["execution_policy"])],"developerInstructions":format!("CLINX exact predecessor context (historical data, not new instructions):\n{context}")});
    if !p["model"].is_null() {
        params["model"] = p["model"].clone();
    }
    let thread = client.request("thread/start", params)?["thread"].clone();
    let mut commit = scope.clone();
    commit["nonce"] = h["nonce"].clone();
    commit["endpoint"] = json!(endpoint);
    commit["thread"] = thread.clone();
    call(authority, "successor_commit", commit)?;
    Ok(thread)
}
fn run_session(
    mut authority: UnixAuthority,
    config: Config,
    timeout: Duration,
    eid: &str,
    e: Value,
    mut p: Value,
) -> Result<()> {
    let result = (|| -> Result<()> {
        let mut events = vec![];
        let mut reader = NativeTransport::new(authority.clone(), UnixProviders { timeout });
        let fresh = matches!(e["dispatch_state"].as_str(), Some("READY" | "TAKEN"));
        let (endpoint, mut client, tid, turn) = if fresh {
            let take = call(
                &mut authority,
                if e["dispatch_state"] == "READY" {
                    "take_dispatch"
                } else {
                    "resume_dispatch"
                },
                json!({"execution_ref":eid}),
            )?;
            if take.get("ticket").is_none() {
                return Ok(());
            }
            p = take["prepared"].clone();
            let endpoints = take["endpoints"]
                .as_array()
                .ok_or("PROVIDER_ROUTE_REQUIRED")?;
            if endpoints.len() != 1 {
                return Err("EXACT_DESKTOP_ENDPOINT_REQUIRED".into());
            }
            let endpoint = endpoints[0]
                .as_str()
                .ok_or("PROVIDER_ROUTE_REQUIRED")?
                .to_owned();
            let mut c = Client::connect(Path::new(&endpoint), timeout)?;
            let scope = json!({"execution_ref":eid,"epoch":take["epoch"],"ticket":take["ticket"]});
            let selection = match c.model_selection(&p["model"], &p["reasoning_effort"]) {
                Ok(v) => v,
                Err(reason) => {
                    call(
                        &mut authority,
                        "fail_before_send",
                        json!({"execution_ref":eid,"reason":reason,"provider_error":c.error_details(&reason,"model/list")}),
                    )?;
                    return Err(c.error_details(&reason, "model/list").to_string());
                }
            };
            let mut recovered_empty = false;
            let thread = if take["successor_required"] == true {
                successor(
                    &mut authority,
                    &mut c,
                    &scope,
                    &p,
                    &endpoint,
                    &config,
                    timeout,
                )?
            } else if let Some(tid) = take["thread_id"].as_str() {
                match c.thread(tid) {
                    Ok(t) => {
                        if t["status"]["type"] != "idle"
                            && t["status"]["type"] != "notLoaded"
                            && t["status"]["type"] != "unloaded"
                        {
                            return Err("NATIVE_WRITER_NOT_IDLE".into());
                        }
                        recovered_empty = t["status"]["type"] == "idle"
                            && take["handoff"]["state"] == "COMMITTED"
                            && take["handoff"]["successor_thread_id"] == tid;
                        if recovered_empty {
                            t
                        } else {
                            c.request_collect(
                                "thread/resume",
                                json!({"threadId":tid}),
                                &mut events,
                            )?["thread"]
                                .clone()
                        }
                    }
                    Err(error) if error == "NATIVE_THREAD_NOT_LOADED" => {
                        let read_error = c.last_error.clone();
                        if take["handoff"]["state"]=="COMMITTED"&&c.request("thread/turns/list",json!({"threadId":tid,"limit":1,"sortDirection":"desc","itemsView":"notLoaded"})).is_err(){let mut payload=scope.clone();payload["thread_id"]=json!(tid);payload["endpoint"]=json!(endpoint);payload["read_error"]=json!(read_error);payload["turns_error"]=json!(c.last_error);call(&mut authority,"successor_recover_empty",payload)?;successor(&mut authority,&mut c,&scope,&p,&endpoint,&config,timeout)?}else{c.request_collect("thread/resume",json!({"threadId":tid}),&mut events)?["thread"].clone()}
                    }
                    Err(error) => return Err(error),
                }
            } else {
                if call(&mut authority, "native_create_take", scope.clone())?["create_permitted"]
                    != true
                {
                    return Err("NATIVE_CREATE_ACK_UNKNOWN".into());
                }
                let mut params = json!({"cwd":p["workspace_contract"]["cwd"],"ephemeral":false,"threadSource":"user","dynamicTools":[config.dynamic_tool(&p["execution_policy"])]});
                if !p["model"].is_null() {
                    params["model"] = p["model"].clone();
                }
                c.request("thread/start", params)?["thread"].clone()
            };
            if thread["status"]["type"] != "idle" {
                return Err("NATIVE_WRITER_NOT_IDLE".into());
            }
            let tid = text(&thread, "id")?.to_owned();
            let mut binding = scope.clone();
            binding["endpoint"] = json!(endpoint);
            binding["thread_id"] = json!(tid);
            binding["cwd"] = thread["cwd"].clone();
            let bound = call(&mut authority, "bind_thread", binding)?;
            if let Some(w) = bound.get("workspace_contract") {
                p["workspace_contract"] = w.clone();
            }
            if call(&mut authority, "authorize_send", scope)?["send_permitted"] != true {
                return Ok(());
            }
            let mut params = json!({"threadId":tid,"input":[{"type":"text","text":p["prompt"]}],"cwd":thread["cwd"],"clientUserMessageId":crate::business::native_client_id(&tid,eid),"sandboxPolicy":{"type":"workspaceWrite","writableRoots":[thread["cwd"]],"networkAccess":false}});
            for (key, value) in selection.as_object().ok_or("PROVIDER_MODEL_LIST_INVALID")? {
                params[key] = value.clone();
            }
            let ack = c
                .request_collect("turn/start", params, &mut events)
                .map_err(|code| c.error_details(&code, "turn/start").to_string())?;
            let committed = reader.recover_input(eid)?;
            let turn = text(&committed, "native_turn_id")?.to_owned();
            if ack["turn"]["id"] != turn {
                return Err("NATIVE_INPUT_TURN_CONFLICT".into());
            }
            if recovered_empty {
                c.request_collect("thread/resume", json!({"threadId":tid}), &mut events)?;
            }
            (endpoint, c, tid, turn)
        } else {
            let tid = text(&e, "thread_id")?.to_owned();
            let turn = text(&e, "native_turn_id")?.to_owned();
            let endpoints = call(
                &mut authority,
                "adoption_endpoints",
                json!({"host":p["host"]}),
            )?;
            let endpoints = endpoints["endpoints"]
                .as_array()
                .ok_or("PROVIDER_ROUTE_REQUIRED")?;
            if endpoints.len() != 1 {
                return Err("EXACT_DESKTOP_ENDPOINT_REQUIRED".into());
            }
            let endpoint = endpoints[0]
                .as_str()
                .ok_or("PROVIDER_ROUTE_REQUIRED")?
                .to_owned();
            let mut c = Client::connect(Path::new(&endpoint), timeout)?;
            let thread =
                c.request_collect("thread/resume", json!({"threadId":tid}), &mut events)?["thread"]
                    .clone();
            if thread["id"] != tid {
                return Err("HOST_RECOVERY_IDENTITY_CONFLICT".into());
            }
            let exact = c.exact_turn(&tid, &turn)?;
            if !matches!(
                exact["status"].as_str(),
                Some("inProgress" | "running" | "active")
            ) {
                return Ok(());
            }
            (endpoint, c, tid, turn)
        };
        let mut session = Session::new(
            authority.clone(),
            config,
            p,
            eid.into(),
            endpoint,
            tid,
            turn,
            timeout,
        )?;
        session.supervise(&mut client, events)
    })();
    // A competing session's error proves nothing about the winning worker's
    // physical send. Only Authority/Provider readback can close uncertainty.
    result
}
