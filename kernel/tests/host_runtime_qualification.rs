//! Ports of the real-process Python Host qualification, with the same faults.
//! Provider frames are a protocol oracle, not physical Mac execution evidence.
#[path = "support/provider.rs"]
mod provider;
#[path = "support/runtime_qualification.rs"]
mod runtime;
use provider::Provider;
use runtime::{Runtime, canonical_task, eventually};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    sync::{Arc, Mutex},
    thread,
    time::Duration,
};

struct State {
    threads: BTreeMap<String, Value>,
    turns: BTreeMap<String, Vec<Value>>,
    requests: BTreeMap<String, Value>,
    responses: Vec<Value>,
    starts: Vec<Value>,
    interrupts: Vec<Value>,
    creates: Vec<Value>,
    lose_input: bool,
    lose_ack: bool,
    lose_create: bool,
    duplicates: bool,
    calls: usize,
    operation: Value,
}
impl State {
    fn new() -> Self {
        Self {
            threads: BTreeMap::new(),
            turns: BTreeMap::new(),
            requests: BTreeMap::new(),
            responses: vec![],
            starts: vec![],
            interrupts: vec![],
            creates: vec![],
            lose_input: false,
            lose_ack: false,
            lose_create: false,
            duplicates: false,
            calls: 1,
            operation: json!({"operation_class":"DEVELOPMENT_MUTATION",
            "capability":"LOCAL_HOST_PROCESS","operation":"development_command","arguments":{"argv":["/usr/bin/true"]}}),
        }
    }
    fn tools(&self, tid: &str, turn: &Value) -> Vec<Value> {
        (0..self.calls).map(|i|json!({"id":format!("request-{}-{i}",turn["id"].as_str().unwrap()),"method":"item/tool/call",
            "params":{"threadId":tid,"turnId":turn["id"],"callId":format!("call-{}-{i}",turn["id"].as_str().unwrap()),
            "namespace":"clinx","tool":"clinx_host_operation","arguments":self.operation}})).collect()
    }
    fn reply(&mut self, m: Value) -> Option<Vec<Value>> {
        if m.get("method").is_none() {
            self.responses.push(m.clone());
            if m.get("error").is_some() {
                return Some(vec![]);
            }
            let p = &self.requests[m["id"].as_str().unwrap()]["params"];
            let turn = self
                .turns
                .get_mut(p["threadId"].as_str().unwrap())
                .unwrap()
                .iter_mut()
                .find(|t| t["id"] == p["turnId"])
                .unwrap();
            // Python's dict(..., **result) rejects a non-mapping and duplicate
            // reserved keys; do not silently overwrite a malformed response.
            let result = m["result"].as_object().expect("Host tool result object");
            assert!(
                ["id", "type", "namespace", "tool"]
                    .iter()
                    .all(|key| !result.contains_key(*key))
            );
            let mut item = m["result"].clone();
            item["id"] = p["callId"].clone();
            item["type"] = json!("dynamicToolCall");
            item["namespace"] = json!("clinx");
            item["tool"] = json!("clinx_host_operation");
            let items = turn["items"].as_array_mut().unwrap();
            if !items.iter().any(|i| i["id"] == item["id"]) {
                items.push(item.clone());
            }
            if items
                .iter()
                .filter(|i| i["type"] == "dynamicToolCall")
                .count()
                == self.calls
            {
                items.push(json!({"id":"answer","type":"agentMessage","phase":"final_answer","text":"Host result"}));
                if turn["status"] == "inProgress" {
                    turn["status"] = json!("completed");
                }
                self.threads
                    .get_mut(p["threadId"].as_str().unwrap())
                    .unwrap()["status"] = json!({"type":"idle"});
            }
            if self.lose_ack {
                return None;
            }
            return Some(vec![
                json!({"method":"item/completed","params":{"threadId":p["threadId"],"turnId":p["turnId"],"item":item}}),
            ]);
        }
        let method = m["method"].as_str().unwrap();
        let p = &m["params"];
        let mut staged = vec![];
        let result = match method {
            "initialized" => return Some(vec![]),
            "initialize" => json!({}),
            "thread/start" => {
                self.creates.push(p.clone());
                let tid = format!("thread-{}", self.threads.len() + 1);
                let value = json!({"id":tid,"cwd":p["cwd"],"name":null,"status":{"type":"idle"}});
                self.threads.insert(tid, value.clone());
                assert_eq!(
                    p["dynamicTools"][0]["tools"][0]["name"],
                    "clinx_host_operation"
                );
                if self.lose_create {
                    return None;
                }
                json!({"thread":value})
            }
            "thread/read" | "thread/resume" => {
                let tid = p["threadId"].as_str().unwrap();
                if method == "thread/resume" {
                    for turn in self.turns.get(tid).into_iter().flatten() {
                        if turn["status"] == "inProgress" {
                            staged.extend(self.tools(tid, turn));
                        }
                    }
                }
                json!({"thread":self.threads[tid]})
            }
            "thread/name/set" => {
                self.threads
                    .get_mut(p["threadId"].as_str().unwrap())
                    .unwrap()["name"] = p["name"].clone();
                json!({})
            }
            "thread/turns/list" => {
                json!({"data":self.turns.get(p["threadId"].as_str().unwrap()).cloned().unwrap_or_default(),"nextCursor":null})
            }
            "thread/items/list" => {
                let turn = self.turns[p["threadId"].as_str().unwrap()]
                    .iter()
                    .find(|t| t["id"] == p["turnId"])
                    .unwrap();
                json!({"data":turn["items"].as_array().unwrap().iter().map(|i|json!({"turnId":turn["id"],"item":i})).collect::<Vec<_>>(),"nextCursor":null})
            }
            "turn/start" => {
                self.starts.push(p.clone());
                let n = self.starts.len();
                let tid = p["threadId"].as_str().unwrap();
                let turn = json!({"id":format!("turn-{n}"),"status":"inProgress","items":[{"id":format!("input-{n}"),"type":"userMessage","clientId":p["clientUserMessageId"],"content":p["input"]}]});
                self.turns
                    .entry(tid.into())
                    .or_default()
                    .insert(0, turn.clone());
                self.threads.get_mut(tid).unwrap()["status"] = json!({"type":"active"});
                staged = self.tools(tid, &turn);
                if self.lose_input {
                    return None;
                }
                json!({"turn":turn})
            }
            "turn/interrupt" => {
                self.interrupts.push(p.clone());
                self.turns
                    .get_mut(p["threadId"].as_str().unwrap())
                    .unwrap()
                    .iter_mut()
                    .find(|t| t["id"] == p["turnId"])
                    .unwrap()["status"] = json!("interrupted");
                json!({})
            }
            other => panic!("unexpected Provider method: {other}"),
        };
        let mut frames = vec![];
        if !staged.is_empty() {
            frames.push(json!({"id":"approval","method":"item/commandExecution/requestApproval","params":staged[0]["params"]}));
            let mut foreign = staged[0].clone();
            foreign["id"] = json!("foreign");
            foreign["params"]["turnId"] = json!("foreign");
            frames.push(foreign);
            for request in staged {
                self.requests
                    .insert(request["id"].as_str().unwrap().into(), request.clone());
                let p = &request["params"];
                frames.push(json!({"method":"item/started","params":{"threadId":p["threadId"],"turnId":p["turnId"],"item":{"id":p["callId"],"type":"dynamicToolCall","namespace":p["namespace"],"tool":p["tool"],"arguments":p["arguments"]}}}));
                frames.push(request.clone());
                if self.duplicates {
                    frames.push(request.clone());
                    let mut conflict = request;
                    conflict["params"]["operationId"] = json!("conflict");
                    frames.push(conflict);
                }
            }
        }
        frames.push(json!({"id":m["id"],"result":result}));
        Some(frames)
    }
}
struct Harness {
    r: Runtime,
    state: Arc<Mutex<State>>,
    _provider: Provider,
}
impl Harness {
    fn new() -> Self {
        let r = Runtime::new("p620", true);
        let state = Arc::new(Mutex::new(State::new()));
        let s = state.clone();
        let provider = Provider::new(&r.provider_path, move |m| s.lock().unwrap().reply(m));
        r.launch_authority(true);
        Self {
            r,
            state,
            _provider: provider,
        }
    }
    fn script(&self, name: &str, text: &str) {
        let path = self.r.repo.join(name);
        fs::write(&path, text).unwrap();
        self.state.lock().unwrap().operation["arguments"] = json!({"argv":["/usr/bin/sh",path]});
    }
    fn state<T>(&self, f: impl FnOnce(&mut State) -> T) -> T {
        f(&mut self.state.lock().unwrap())
    }
    fn start(&self) -> Value {
        self.r.start(None, "fixture")
    }
}
fn sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

#[test]
fn host_process_exact_result_duplicate_requests_ack_queue_and_broadcasts() {
    let h = Harness::new();
    h.state(|s| {
        s.duplicates = true;
        s.calls = 2;
    });
    h.script(
        "call.sh",
        "printf x >> calls\nprintf \"token=fixture-secret\\n\"\n",
    );
    let e = h.start();
    h.r.launch_worker();
    h.r.wait(&e, "SUCCEEDED");
    assert_eq!(fs::read_to_string(h.r.repo.join("calls")).unwrap(), "xx");
    h.state(|s| {
        assert_eq!(s.starts.len(), 1);
        assert!(
            s.responses
                .iter()
                .all(|r| r["id"] != "approval" && r["id"] != "foreign")
        );
        assert_eq!(
            s.starts[0]["sandboxPolicy"],
            json!({"type":"workspaceWrite","writableRoots":[h.r.repo],"networkAccess":false})
        );
    });
    let rows = h.r.rows(&e);
    assert_eq!(rows.len(), 2);
    for row in rows {
        assert_eq!(row["delivery_state"], "DELIVERED");
        assert_eq!(row["host_completion"], "KNOWN");
        assert!(
            !row["result"]["stdout"]
                .as_str()
                .unwrap()
                .contains("fixture-secret")
        );
        assert_eq!(
            row["result"]["stdout_sha256"],
            sha256(b"token=fixture-secret\n")
        );
    }
}
fn lost_ack(input: bool) {
    let h = Harness::new();
    h.script("call.sh", "printf x >> calls\n");
    h.state(|s| {
        s.lose_input = input;
        s.lose_ack = !input;
    });
    let e = h.start();
    let worker = h.r.launch_worker();
    if input {
        eventually(|| h.r.status(&e), |s| s["dispatch_state"] == "SENDING");
        eventually(|| h.state(|s| s.turns.len()), |n| *n > 0);
    } else {
        eventually(
            || h.r.rows(&e),
            |rows| !rows.is_empty() && rows[0]["response_sent"] == true,
        );
    }
    h.r.kill(worker);
    h.r.restart_authority();
    h.state(|s| {
        s.lose_input = false;
        s.lose_ack = false;
    });
    h.r.launch_worker();
    h.r.wait(&e, "SUCCEEDED");
    assert_eq!(fs::read_to_string(h.r.repo.join("calls")).unwrap(), "x");
    assert_eq!(h.state(|s| s.starts.len()), 1);
    assert_eq!(h.r.rows(&e)[0]["delivery_state"], "DELIVERED");
}
#[test]
fn host_lost_input_ack_exact_history_restart_and_no_process_replay() {
    lost_ack(true);
}
#[test]
fn host_lost_result_ack_exact_history_restart_and_no_process_replay() {
    lost_ack(false);
}
#[test]
fn host_cancel_kills_running_process_and_preserves_exact_receipts() {
    let h = Harness::new();
    h.script("cancel.sh", "printf ready > ready\nexec sleep 20\n");
    let e = h.start();
    h.r.launch_worker();
    eventually(|| h.r.repo.join("ready").exists(), |b| *b);
    h.r.call(
        "cancel",
        json!({"execution_ref":e["execution_ref"],"approved":true}),
    );
    h.r.wait(&e, "CANCELLED");
    let row = &h.r.rows(&e)[0];
    assert_eq!(row["host_completion"], "KNOWN");
    assert_eq!(row["delivery_state"], "DELIVERED");
    assert_eq!(row["result"]["cancel_requested"], true);
    assert!(row["result"]["exit_code"].as_i64().unwrap() < 0);
    h.state(|s| {
        assert_eq!(s.starts.len(), 1);
        assert_eq!(s.interrupts.len(), 1);
    });
}
#[test]
fn host_pre_dispatch_rejection_is_durable_and_never_executes() {
    let h = Harness::new();
    h.state(|s| {
        s.operation["arguments"] =
            json!({"argv":["/usr/bin/env","touch",h.r.repo.join("forbidden")]})
    });
    let e = h.start();
    h.r.launch_worker();
    h.r.wait(&e, "SUCCEEDED");
    let row = &h.r.rows(&e)[0];
    assert_eq!(row["execution_state"], "COMMAND_NOT_DISPATCHED");
    assert!(row["host_ref"].is_null());
    assert_eq!(row["delivery_state"], "DELIVERED");
    assert!(!h.r.repo.join("forbidden").exists());
}
#[test]
fn host_competing_workers_dispatch_one_process() {
    let h = Harness::new();
    h.script("concurrent.sh", "sleep 1\nprintf x >> calls\n");
    let e = h.start();
    h.r.launch_worker();
    h.r.launch_worker();
    h.r.wait(&e, "SUCCEEDED");
    assert_eq!(fs::read_to_string(h.r.repo.join("calls")).unwrap(), "x");
    assert_eq!(h.state(|s| s.starts.len()), 1);
    assert_eq!(h.r.rows(&e)[0]["delivery_state"], "DELIVERED");
}
#[test]
fn host_parallel_projects_keep_distinct_execution_and_turn_evidence() {
    let h = Harness::new();
    h.script(
        "parallel.sh",
        "printf ready > ready\nsleep 1\nprintf x >> calls\n",
    );
    let a = h.start();
    let b = h.r.start(None, "fixture2");
    h.r.launch_worker();
    eventually(
        || h.r.repo.join("ready").exists() && h.r.repo2.join("ready").exists(),
        |b| *b,
    );
    h.r.wait(&a, "SUCCEEDED");
    h.r.wait(&b, "SUCCEEDED");
    assert_eq!(fs::read_to_string(h.r.repo.join("calls")).unwrap(), "x");
    assert_eq!(fs::read_to_string(h.r.repo2.join("calls")).unwrap(), "x");
    let ar = &h.r.rows(&a)[0];
    let br = &h.r.rows(&b)[0];
    assert_ne!(ar["host_ref"], br["host_ref"]);
    assert_ne!(ar["identity"]["thread_id"], br["identity"]["thread_id"]);
    assert_ne!(ar["identity"]["turn_id"], br["identity"]["turn_id"]);
}
struct ProcessGroup(rustix::process::Pid);
impl Drop for ProcessGroup {
    fn drop(&mut self) {
        let _ = rustix::process::kill_process_group(self.0, rustix::process::Signal::KILL);
    }
}
#[test]
fn host_unknown_process_result_after_restart_is_never_replayed() {
    let h = Harness::new();
    h.script(
        "unknown.sh",
        "printf x >> calls\nprintf \"%s\" \"$$\" > pid\nexec sleep 30\n",
    );
    let e = h.start();
    let worker = h.r.launch_worker();
    let pid = eventually(
        || {
            fs::read_to_string(h.r.repo.join("pid"))
                .ok()
                .and_then(|s| s.parse::<i32>().ok())
        },
        |p| p.is_some(),
    )
    .unwrap();
    let group = ProcessGroup(rustix::process::Pid::from_raw(pid).unwrap());
    h.r.kill(worker);
    rustix::process::kill_process_group(group.0, rustix::process::Signal::TERM).unwrap();
    h.r.restart_authority();
    h.r.launch_worker();
    thread::sleep(Duration::from_secs(3));
    assert_eq!(fs::read_to_string(h.r.repo.join("calls")).unwrap(), "x");
    let row = &h.r.rows(&e)[0];
    assert_eq!(row["host_completion"], "UNKNOWN");
    assert_eq!(row["call_state"], "RUNNING");
    assert_eq!(row["reconciliation_required"], true);
    assert_eq!(h.r.status(&e)["resource_state"], "HELD");
    assert_eq!(h.state(|s| s.starts.len()), 1);
}
#[test]
fn host_timeout_and_bounded_output_preserve_raw_evidence() {
    let h = Harness::new();
    let mut config: Value = serde_json::from_slice(&fs::read(&h.r.config).unwrap()).unwrap();
    config["max_output_bytes"] = json!(1024);
    fs::write(&h.r.config, config.to_string()).unwrap();
    h.script(
        "timeout.sh",
        "printf \"%02000d\" 0\nprintf ready > ready\nexec sleep 20\n",
    );
    h.state(|s| s.operation["timeout_seconds"] = json!(0.3));
    let e = h.start();
    h.r.launch_worker();
    h.r.wait(&e, "SUCCEEDED");
    let row = &h.r.rows(&e)[0];
    assert_eq!(row["host_completion"], "KNOWN");
    assert_eq!(row["delivery_state"], "DELIVERED");
    let result = &row["result"];
    assert_eq!(result["timed_out"], true);
    assert!(result["exit_code"].as_i64().unwrap() < 0);
    assert_eq!(result["stdout"], "0".repeat(1024));
    assert_eq!(result["stdout_bytes"], 2000);
    assert_eq!(result["stdout_truncated"], true);
    assert_eq!(result["stdout_sha256"], sha256(&vec![b'0'; 2000]));
}
#[test]
fn host_thread_creation_ack_unknown_survives_restart_without_recreate() {
    let h = Harness::new();
    h.state(|s| s.lose_create = true);
    let e = h.start();
    let worker = h.r.launch_worker();
    eventually(|| h.state(|s| s.creates.len()), |n| *n > 0);
    h.r.kill(worker);
    h.r.restart_authority();
    h.state(|s| s.lose_create = false);
    h.r.launch_worker();
    thread::sleep(Duration::from_secs(3));
    h.state(|s| {
        assert_eq!(s.creates.len(), 1);
        assert!(s.starts.is_empty());
    });
    assert_eq!(h.r.status(&e)["resource_state"], "HELD");
    assert!(h.r.rows(&e).is_empty());
}
#[test]
fn host_canonical_checkpoint_successor_runs_once_and_keeps_task_identity() {
    let h = Harness::new();
    let tid = "11111111-2222-3333-4444-555555555555";
    let prior = json!({"id":"predecessor-exact","status":"completed","items":[{"id":"original-result","type":"agentMessage","text":"Exact original predecessor"}]});
    h.state(|s| {
        s.threads.insert(
            tid.into(),
            json!({"id":tid,"cwd":h.r.repo,"name":"Native","status":{"type":"idle"}}),
        );
        s.turns.insert(tid.into(), vec![prior.clone()]);
    });
    let task = canonical_task("p620", tid);
    let identity = json!({"node_id":"p620","host":"p620","provider":"codex_app_server","thread_id":tid,"user_scope":"fixture-user","task_id":task,
        "origin":"CODEX_NATIVE","title":"Native","cwd":h.r.repo,"first_provenance_json":r#"{"node": "p620", "node_fingerprint": "fixture", "observation_id": "fixture-observation", "digest": "fixture-digest"}"#});
    let adoption=h.r.call("adopt",json!({"host":"p620","project":"fixture","thread_id":tid,"title":"Native","workspace_contract":{"cwd":h.r.repo},"exact_turn_id":prior["id"],"canonical_identity":identity}));
    h.r.launch_worker();
    let tasks = eventually(
        || h.r.call("list_tasks", json!({}))["tasks"].clone(),
        |rows| rows.as_array().is_some_and(|r| !r.is_empty()),
    );
    assert_eq!(tasks.as_array().unwrap().len(), 1);
    assert_eq!(tasks[0]["task_id"], task);
    assert_eq!(adoption["task_ref"], task);
    let change=h.r.call("prepare_policy",json!({"task_ref":task,"expected_policy_hash":tasks[0]["policy_hash"],"target_policy":{"execution_surface":"HOST_EXECUTOR","operation_scopes":[Runtime::scope()]},"reason":"Host continuation qualification","approved":true}));
    h.r.call("apply_policy",json!({"prepared_reauthorization_ref":change["prepared_reauthorization_ref"],"expected_request_hash":change["request_hash"],"expected_task_ref":task,"approved":true}));
    let e = h.r.start(Some(&json!(task)), "fixture");
    h.r.wait(&e, "SUCCEEDED");
    let final_task = h.r.call("list_tasks", json!({}))["tasks"][0].clone();
    assert_eq!(final_task["task_id"], task);
    assert_ne!(final_task["thread_id"], tid);
    assert_eq!(final_task["canonical_handoff"]["identity"], identity);
    assert_eq!(final_task["lineage"][0]["source"]["turn"], prior);
    assert_eq!(final_task["lineage"][0]["state"], "COMMITTED");
    eventually(
        || h.state(|s| s.threads[final_task["thread_id"].as_str().unwrap()]["name"].clone()),
        |t| t == "Host fixture",
    );
    h.state(|s| {
        assert_eq!(s.threads[tid]["name"], "Native");
        assert_eq!(s.creates.len(), 1);
        assert_eq!(s.starts.len(), 1);
        let context = s.creates[0]["developerInstructions"].as_str().unwrap();
        assert!(context.contains(tid));
        assert!(context.contains(prior["id"].as_str().unwrap()));
        assert!(context.contains("Exact original predecessor"));
    });
    assert_eq!(h.r.rows(&e)[0]["delivery_state"], "DELIVERED");
}
