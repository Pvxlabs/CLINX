//! Original 3 entry paths x 3 Provider routes, with real isolated Rust processes.
//! All completion/recovery evidence comes from the independent WebSocket oracle.
#[path = "support/native_network.rs"]
mod network;
#[path = "support/provider.rs"]
mod provider;
#[path = "support/runtime_qualification.rs"]
mod runtime;
use clinx_decision_kernel::{
    discovery_identity,
    mcp_api::{Api, Config, NativeReads},
    node_api::{Config as NodeConfig, Node},
    node_registry::Registry,
    observation,
};
use network::{Paired, Route};
use provider::Provider;
use runtime::{Runtime, canonical_task};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    io::Write,
    process::{Command, Stdio},
    sync::{Arc, Mutex},
    thread,
};

#[derive(Default)]
struct State {
    threads: BTreeMap<String, Value>,
    turns: BTreeMap<String, Vec<Value>>,
    inputs: Vec<Value>,
    interrupts: Vec<Value>,
    lose_input: bool,
    lose_cancel: bool,
}
impl State {
    fn reply(&mut self, m: Value) -> Option<Vec<Value>> {
        let p = &m["params"];
        let result = match m["method"].as_str().unwrap() {
            "initialized" => return Some(vec![]),
            "initialize" => json!({}),
            "thread/start" => {
                let tid = format!("thread-{}", self.threads.len() + 1);
                let value = json!({"id":tid,"cwd":p["cwd"],"name":null,"status":{"type":"idle"}});
                self.threads.insert(tid, value.clone());
                json!({"thread":value})
            }
            "thread/read" | "thread/resume" => {
                json!({"thread":self.threads[p["threadId"].as_str().unwrap()]})
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
                self.inputs.push(p.clone());
                let n = self.inputs.len();
                let turn = json!({"id":format!("turn-{n}"),"status":if p["input"][0]["text"]=="running" {"inProgress"}else{"completed"},"items":[
                    {"id":format!("input-{n}"),"type":"userMessage","clientId":p["clientUserMessageId"],"content":p["input"]},
                    {"id":"answer","type":"agentMessage","phase":"final_answer","text":"Exact Rust result"}]});
                self.turns
                    .entry(p["threadId"].as_str().unwrap().into())
                    .or_default()
                    .insert(0, turn.clone());
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
                if self.lose_cancel {
                    return None;
                }
                json!({})
            }
            other => panic!("unexpected Provider method: {other}"),
        };
        Some(vec![json!({"id":m["id"],"result":result})])
    }
}
#[derive(Clone, Copy, PartialEq)]
enum Entry {
    Direct,
    Mcp,
    Node,
}
#[derive(Clone, Copy, PartialEq)]
enum Routing {
    Local,
    Paired,
    Routed,
}
#[derive(Clone)]
struct NodeEntry {
    route: Route,
    api: Config,
    config: NodeConfig,
}
impl NodeEntry {
    fn call_result(&self, op: &str, args: Value) -> clinx_decision_kernel::mcp_api::ApiResult {
        Node {
            backend: Api {
                authority: self.route.clone(),
                reads: NativeReads(self.api.clone()),
                config: self.api.clone(),
            },
            config: self.config.clone(),
        }
        .invoke(op, &args)
    }
    fn call(&self, op: &str, args: Value) -> Value {
        self.call_result(op, args).unwrap()
    }
    fn start(&self, prompt: &str, task: Option<&Value>) -> Value {
        let mut p = json!({"host":"air","project":"fixture","title":"Native title","prompt":prompt,"approved":true,"task_action":if task.is_some(){"continue"}else{"create"}});
        if let Some(task) = task {
            p["task_ref"] = task.clone();
        }
        let p = self.call("prepare", p);
        self.call(
            "start",
            json!({"prepared_execution_ref":p["prepared_execution_ref"],"approved":true}),
        )
    }
}
struct Harness {
    // Stop Helper/Centre before the runtime removes their owned state directory.
    _paired: Option<Paired>,
    r: Runtime,
    state: Arc<Mutex<State>>,
    _provider: Provider,
    entry: Entry,
    node: NodeEntry,
    extra: Vec<String>,
    endpoint: std::path::PathBuf,
}
impl Harness {
    fn new(entry: Entry, routing: Routing) -> Self {
        let r = Runtime::new("air", false);
        let state = Arc::new(Mutex::new(State::default()));
        let s = state.clone();
        let provider = Provider::new(&r.provider_path, move |m| s.lock().unwrap().reply(m));
        let endpoint = if routing == Routing::Local {
            r.provider_path.clone()
        } else {
            r.temp.path().join("g.sock")
        };
        let mut policy: Value = serde_json::from_slice(&fs::read(&r.policy).unwrap()).unwrap();
        policy["endpoints"] = json!([endpoint]);
        policy["provider_routes"] = json!({"air":[endpoint]});
        fs::write(&r.policy, policy.to_string()).unwrap();
        r.launch_authority(true);
        let paired = if routing == Routing::Local {
            None
        } else {
            Some(Paired::new(&r))
        };
        let route = paired
            .as_ref()
            .map(|p| Route::Paired(p.reverse.clone()))
            .unwrap_or_else(|| Route::Local(r.client()));
        let api:Config=serde_json::from_value(json!({"runtime_host":"air","provider_endpoint":r.provider_path,"provider_timeout_seconds":2,"host_aliases":{"air":["air"]},"codex_host_ids":{}})).unwrap();
        let node=NodeEntry{route,api:api.clone(),config:serde_json::from_value(json!({"node_id":"air","projects":[{"project_alias":"fixture","repo":r.repo}],"allowed_projects":["fixture"]})).unwrap()};
        fs::write(
            r.temp.path().join("mcp.json"),
            serde_json::to_vec(&api).unwrap(),
        )
        .unwrap();
        if entry == Entry::Mcp {
            discovery_identity::private_store(&r.temp.path().join("observation-state")).unwrap();
            Registry::default()
                .invoke(
                    "registry.init",
                    &json!({"path":r.temp.path().join("observation-registry.sqlite3")}),
                )
                .unwrap();
            observation::invoke(
                "observation.init",
                &json!({"path":r.temp.path().join("observations.sqlite3")}),
                |_, _| panic!("unexpected observation init callback"),
            )
            .unwrap();
        }
        let uid = rustix::process::getuid().as_raw().to_string();
        let extra = match routing {
            Routing::Local => vec![],
            Routing::Paired => vec![
                "--provider-gateway".into(),
                endpoint.display().to_string(),
                "--provider-gateway-uid".into(),
                uid,
                "--remote-host".into(),
                "air".into(),
            ],
            Routing::Routed => vec![
                "--provider-route".into(),
                "another-node".into(),
                r.temp.path().join("wrong.sock").display().to_string(),
                "--provider-route".into(),
                "air".into(),
                endpoint.display().to_string(),
                "--provider-gateway-uid".into(),
                uid,
            ],
        };
        Self {
            r,
            _paired: paired,
            state,
            _provider: provider,
            entry,
            node,
            extra,
            endpoint,
        }
    }
    fn mcp(&self, name: &str, args: Value) -> Value {
        let mut child = Command::new(env!("CARGO_BIN_EXE_clinx-mcp"))
            .arg("--config")
            .arg(self.r.temp.path().join("mcp.json"))
            .arg("--stdio")
            .env("CLINX_RUST_SOCKET", &self.r.endpoint)
            .env(
                "CLINX_RUST_UID",
                rustix::process::getuid().as_raw().to_string(),
            )
            .env("CLINX_RUST_OBSERVATION_READS", "1")
            .env(
                "CLINX_OBSERVATION_STATE",
                self.r.temp.path().join("observation-state"),
            )
            .env(
                "CLINX_OBSERVATION_DB",
                self.r.temp.path().join("observations.sqlite3"),
            )
            .env(
                "CLINX_OBSERVATION_REGISTRY",
                self.r.temp.path().join("observation-registry.sqlite3"),
            )
            .env("CLINX_OBSERVATION_USER_SCOPE", "fixture")
            .env("CLINX_OBSERVATION_NODE_ID", "air")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        writeln!(child.stdin.take().unwrap(),"{}",json!({"jsonrpc":"2.0","id":"fixture","method":"tools/call","params":{"name":name,"arguments":args}})).unwrap();
        let result = child.wait_with_output().unwrap();
        assert!(
            result.status.success(),
            "MCP: {}",
            String::from_utf8_lossy(&result.stderr)
        );
        let response: Value = serde_json::from_slice(&result.stdout).unwrap();
        assert_eq!(response["id"], "fixture");
        assert!(response.get("error").is_none(), "{response}");
        assert_eq!(response["result"]["isError"], false, "{response}");
        response["result"]["structuredContent"].clone()
    }
    fn start(&self, prompt: &str, task: Option<&Value>, key: &str) -> Value {
        if self.entry == Entry::Node {
            return self.node.start(prompt, task);
        }
        let mut p = json!({"host":"air","project":"fixture","title":"Native title","prompt":prompt,"approved":true});
        if let Some(task) = task {
            p["task_ref"] = task.clone();
        }
        if self.entry == Entry::Mcp {
            p["task_action"] = json!(if task.is_some() { "continue" } else { "create" });
            let p = self.mcp("clinx_prepare_execution", p);
            self.mcp(
                "clinx_start_execution",
                json!({"prepared_execution_ref":p["prepared_execution_ref"],"approved":true}),
            )
        } else {
            let p = self.r.call("prepare", p);
            self.r
                .call_result(
                    "start",
                    json!({"prepared_execution_ref":p["prepared_execution_ref"],"approved":true}),
                    Some(key),
                )
                .unwrap()
        }
    }
    fn tick(&self) {
        self.r.tick(&self.extra);
    }
    fn state<T>(&self, f: impl FnOnce(&mut State) -> T) -> T {
        f(&mut self.state.lock().unwrap())
    }
}
fn sequence(entry: Entry, routing: Routing) {
    let h = Harness::new(entry, routing);
    let first = h.start("exact input", None, "start");
    h.tick();
    h.tick();
    assert_eq!(h.r.status(&first)["status"], "SUCCEEDED");
    if entry == Entry::Mcp {
        let observations = h.mcp("clinx_list_observations", json!({"limit":1}));
        assert_eq!(observations["read_only"], true);
        assert_eq!(observations["items"], json!([]));
        let exact = h.mcp(
            "clinx_get_context",
            json!({"execution_ref":first["execution_ref"]}),
        );
        assert_eq!(exact["execution_ref"], first["execution_ref"]);
        assert_eq!(exact["context_scope"], "EXACT_EXECUTION_TURN");
        assert_eq!(
            exact["execution_turn_ref"],
            h.r.status(&first)["native_turn_id"]
        );
        assert_eq!(exact["last_user_intent"], "exact input");
    }
    if entry == Entry::Node {
        let exact = h.node.call(
            "context",
            json!({"execution_ref":first["execution_ref"],"task_ref":first["task_ref"]}),
        );
        assert_eq!(exact["last_user_intent"], "exact input");
        assert_eq!(exact["context_scope"], "EXACT_EXECUTION_TURN");
        assert_eq!(exact["execution_ref"], first["execution_ref"]);
        assert!(h.node.call_result("cancel",json!({"execution_ref":first["execution_ref"],"project":"another-project","approved":true})).is_err());
        assert_eq!(h.state(|s| s.interrupts.len()), 0);
    }
    h.state(|s| {
        assert_eq!(s.threads["thread-1"]["name"], "Native title");
        assert_eq!(s.inputs.len(), 1);
        assert_eq!(
            s.inputs[0]["input"],
            json!([{"type":"text","text":"exact input"}])
        );
        assert_eq!(
            s.inputs[0]["sandboxPolicy"],
            json!({"type":"dangerFullAccess"})
        );
        s.lose_input = true;
    });
    let lost = h.start("lost ACK", Some(&first["task_ref"]), "lost-start");
    h.tick();
    assert_eq!(h.r.status(&lost)["dispatch_state"], "SENDING");
    h.r.restart_authority();
    h.tick();
    h.tick();
    assert_eq!(h.r.status(&lost)["status"], "SUCCEEDED");
    assert_eq!(h.state(|s| s.inputs.len()), 2);
    let duplicate_payload =
        json!({"prepared_execution_ref":lost["prepared_execution_ref"],"approved":true});
    match entry {
        Entry::Mcp => {
            let duplicate = h.mcp("clinx_start_execution", duplicate_payload);
            assert_eq!(duplicate, h.r.status(&lost));
            assert_eq!(duplicate["execution_ref"], lost["execution_ref"]);
            assert_eq!(h.state(|s| s.inputs.len()), 2);
        }
        Entry::Node => {
            let duplicate = h.node.call("start", duplicate_payload);
            assert_eq!(duplicate["execution_ref"], lost["execution_ref"]);
            assert_eq!(duplicate["status"], "SUCCEEDED");
            assert_eq!(h.state(|s| s.inputs.len()), 2);
        }
        Entry::Direct => assert_eq!(
            h.r.call_result("start", duplicate_payload, Some("lost-start"))
                .unwrap(),
            lost
        ),
    }
    h.state(|s| s.lose_input = false);
    let running = h.start("running", Some(&first["task_ref"]), "running-start");
    h.tick();
    let cancel = json!({"execution_ref":running["execution_ref"],"approved":true});
    match entry {
        Entry::Mcp => {
            h.mcp("clinx_cancel_execution", cancel);
        }
        Entry::Node => {
            assert_eq!(
                h.node.call("cancel", cancel)["operation_state"],
                "CANCELLATION_PENDING"
            );
        }
        Entry::Direct => {
            h.r.call("cancel", cancel);
        }
    }
    h.state(|s| s.lose_cancel = true);
    h.tick();
    h.r.restart_authority();
    h.tick();
    assert_eq!(h.r.status(&running)["status"], "CANCELLED");
    if entry == Entry::Node {
        let receipt = h.node.call(
            "reconcile",
            json!({"execution_ref":running["execution_ref"]}),
        );
        assert_eq!(receipt["status"], "CANCELLED");
        assert_eq!(receipt["resource_state"], "RELEASED");
        assert_eq!(receipt["reconciliation"], "RUST_TRANSPORT_PENDING");
    }
    h.state(|s| {
        assert_eq!(s.interrupts.len(), 1);
        assert_eq!(s.inputs.len(), 3);
    });
    assert_eq!(
        h.r.call("adoption_endpoints", json!({"host":"air"}))["endpoints"],
        json!([h.endpoint])
    );
    if entry == Entry::Node {
        let tid = "11111111-2222-3333-4444-555555555555";
        let fingerprint = h
            ._paired
            .as_ref()
            .map(|p| p.fingerprint.clone())
            .unwrap_or(json!("isolated-key"));
        let canonical = json!({"node_id":"air","host":"air","provider":"codex_app_server","thread_id":tid,"user_scope":"fixture","origin":"CODEX_NATIVE","title":"Canonical fixture","cwd":h.r.repo,"task_id":canonical_task("air",tid),
            "first_provenance_json":format!(r#"{{"node": "air", "node_fingerprint": {}, "observation_id": "isolated-observation", "digest": "isolated-digest"}}"#,fingerprint)});
        let handoff = json!({"canonical_identity":canonical,"exact_turn_id":"predecessor","_canonical_owner":{"owner":"air","user":"fixture","fingerprint":fingerprint}});
        h.state(|s| {
            s.threads.insert(
                tid.into(),
                json!({"id":tid,"cwd":h.r.repo,"name":null,"status":{"type":"idle"}}),
            );
            s.turns.insert(
                tid.into(),
                vec![json!({"id":"predecessor","status":"completed","items":[]})],
            );
        });
        let adopted = h.node.call("adopt", handoff.clone());
        assert_eq!(adopted["adoption_status"], "PENDING");
        let mut p = handoff.clone();
        p["prompt"] = json!("Exact canonical input");
        p["approved"] = json!(true);
        let error = h.node.call_result("prepare", p.clone()).unwrap_err();
        // Python maps this exact Rust code to NodeProtocolError with the
        // Transport explanation. The retained facade smoke checks that text.
        assert_eq!(error.code, "CANONICAL_ADOPTION_PENDING");
        assert_eq!(h.state(|s| s.inputs.len()), 3);
        h.tick();
        let committed = h.node.call("adopt", handoff.clone());
        assert_eq!(committed["adoption_status"], "ADOPTED");
        assert_eq!(committed["task_ref"], canonical["task_id"]);
        assert_eq!(committed["task_ref"], adopted["task_ref"]);
        let prepared = h.node.call("prepare", p);
        assert_eq!(prepared["task_ref"], canonical["task_id"]);
        let e = h.node.call(
            "start",
            json!({"prepared_execution_ref":prepared["prepared_execution_ref"],"approved":true}),
        );
        h.tick();
        h.tick();
        assert_eq!(
            h.node
                .call("status", json!({"execution_ref":e["execution_ref"]}))["status"],
            "SUCCEEDED"
        );
        h.state(|s| {
            assert_eq!(s.inputs.len(), 4);
            assert_eq!(s.inputs.last().unwrap()["threadId"], tid);
        });
        assert_eq!(
            h.node.call("adopt", handoff)["task_ref"],
            committed["task_ref"]
        );
        assert_eq!(h.state(|s| s.inputs.len()), 4);
        let children: Vec<_> = ["parallel one", "parallel two"]
            .into_iter()
            .map(|prompt| {
                let node = h.node.clone();
                thread::spawn(move || node.start(prompt, None))
            })
            .collect();
        let concurrent: Vec<_> = children.into_iter().map(|c| c.join().unwrap()).collect();
        assert_ne!(concurrent[0]["task_ref"], concurrent[1]["task_ref"]);
        assert_ne!(
            concurrent[0]["execution_ref"],
            concurrent[1]["execution_ref"]
        );
        h.tick();
        h.tick();
        for e in concurrent {
            assert_eq!(
                h.node
                    .call("status", json!({"execution_ref":e["execution_ref"]}))["status"],
                "SUCCEEDED"
            );
        }
        assert_eq!(h.state(|s| s.inputs.len()), 6);
    }
}
#[test]
fn direct_local() {
    sequence(Entry::Direct, Routing::Local);
}
#[test]
fn mcp_local() {
    sequence(Entry::Mcp, Routing::Local);
}
#[test]
fn node_local() {
    sequence(Entry::Node, Routing::Local);
}
#[test]
fn direct_paired() {
    sequence(Entry::Direct, Routing::Paired);
}
#[test]
fn mcp_paired() {
    sequence(Entry::Mcp, Routing::Paired);
}
#[test]
fn node_paired() {
    sequence(Entry::Node, Routing::Paired);
}
#[test]
fn direct_routed() {
    sequence(Entry::Direct, Routing::Routed);
}
#[test]
fn mcp_routed() {
    sequence(Entry::Mcp, Routing::Routed);
}
#[test]
fn node_routed() {
    sequence(Entry::Node, Routing::Routed);
}
