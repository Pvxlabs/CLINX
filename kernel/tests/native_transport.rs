//! Real Store transactions with deterministic Provider protocol faults. No
//! production services, tasks, databases or installed native history are used.
use clinx_decision_kernel::{
    authority_store::Store,
    business::{Policy, Project, Role},
    native_transport::{Authority, NativeTransport, Providers},
    provider::{Provider, Result},
};
use serde_json::{Value, json};
use std::{cell::RefCell, fs, os::unix::fs::PermissionsExt, path::PathBuf, rc::Rc};
struct Api {
    store: Store,
    policy: Policy,
}
impl Authority for Api {
    fn call(&mut self, op: &str, p: Value, id: &str) -> Result<Value> {
        self.store
            .business(Role::Transport, &self.policy, op, id, &p)
    }
}
#[derive(Default)]
struct State {
    thread: Value,
    turns: Vec<Value>,
    sent: u64,
    created: u64,
    interrupted: u64,
    lose_input: bool,
    lose_create: bool,
    lose_cancel: bool,
    lose_title: bool,
    live: bool,
    wrong_input: bool,
}
#[derive(Clone)]
struct Factory(Rc<RefCell<State>>);
struct Connection(Rc<RefCell<State>>);
impl Providers for Factory {
    type Connection = Connection;
    fn connect(&mut self, endpoint: &str) -> Result<Connection> {
        assert_eq!(endpoint, "/fixture/air.sock");
        Ok(Connection(self.0.clone()))
    }
}
impl Provider for Connection {
    fn request(&mut self, method: &str, p: Value) -> Result<Value> {
        let mut s = self.0.borrow_mut();
        match method {
            "thread/start" => {
                s.created += 1;
                s.thread = json!({"id":"native-thread","cwd":p["cwd"],"status":{"type":"idle"},"name":null});
                if s.lose_create {
                    Err("PROVIDER_DELIVERY_UNKNOWN".into())
                } else {
                    Ok(json!({"thread":s.thread}))
                }
            }
            "thread/read" | "thread/resume" => {
                assert_eq!(p["threadId"], s.thread["id"]);
                Ok(json!({"thread":s.thread}))
            }
            "thread/name/set" => {
                s.thread["name"] = p["name"].clone();
                if s.lose_title {
                    Err("PROVIDER_DELIVERY_UNKNOWN".into())
                } else {
                    Ok(json!({}))
                }
            }
            "turn/start" => {
                s.sent += 1;
                let id = format!("native-turn-{}", s.sent);
                let client = if s.wrong_input {
                    json!("somebody-else")
                } else {
                    p["clientUserMessageId"].clone()
                };
                let status = if s.live { "inProgress" } else { "completed" };
                let turn = json!({"id":id,"status":status,"items":[{"id":format!("input-{}",s.sent),"type":"userMessage","clientId":client,"content":p["input"]},{"id":"result","type":"agentMessage","phase":"final_answer","text":"Exact native result"}]});
                s.turns.insert(0, turn.clone());
                if s.lose_input {
                    Err("PROVIDER_DELIVERY_UNKNOWN".into())
                } else {
                    Ok(json!({"turn":turn}))
                }
            }
            "thread/turns/list" => Ok(json!({"data":s.turns,"nextCursor":null})),
            "thread/items/list" => Ok(
                json!({"data":s.turns.iter().find(|t|t["id"]==p["turnId"]).unwrap()["items"].as_array().unwrap().iter().map(|i|json!({"turnId":p["turnId"],"item":i})).collect::<Vec<_>>(),"nextCursor":null}),
            ),
            "turn/interrupt" => {
                s.interrupted += 1;
                let turn = s.turns.iter_mut().find(|t| t["id"] == p["turnId"]).unwrap();
                turn["status"] = json!("interrupted");
                if s.lose_cancel {
                    Err("PROVIDER_DELIVERY_UNKNOWN".into())
                } else {
                    Ok(json!({}))
                }
            }
            _ => panic!("unexpected method {method}"),
        }
    }
}
fn environment(name: &str) -> (PathBuf, Api, Factory) {
    let path = std::env::temp_dir().join(format!("clinx-native-{name}-{}", std::process::id()));
    fs::create_dir(&path).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
    let policy = Policy {
        projects: vec![Project {
            alias: "fixture".into(),
            host: "air".into(),
            cwd: PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .to_string_lossy()
                .into(),
        }],
        endpoints: vec!["/fixture/air.sock".into()],
        provider_routes: Default::default(),
        lease_ms: 120000,
        workspaces: vec![],
        derived_git_roots: vec![],
        host_operations: vec![],
    };
    let store = Store::open(&path, true).unwrap();
    store.configure_business(&policy, true).unwrap();
    (
        path,
        Api { store, policy },
        Factory(Rc::new(RefCell::new(State::default()))),
    )
}
fn start(api: &mut Api, key: &str, task: Option<&str>) -> Value {
    let mut p = json!({"host":"air","project":"fixture","title":"Native title","prompt":format!("Exact input {key}"),"approved":true});
    if let Some(task) = task {
        p["task_ref"] = json!(task);
        p["thread_id"] = json!("native-thread");
    }
    let p = api.call("prepare", p, &format!("prepare-{key}")).unwrap();
    api.call(
        "start",
        json!({"prepared_execution_ref":p["prepared_execution_ref"],"approved":true}),
        &format!("start-{key}"),
    )
    .unwrap()
}
#[test]
fn native_completion_metadata_lost_input_restart_and_conflicts() {
    let (dir, mut api, factory) = environment("complete");
    let e = start(&mut api, "first", None);
    let eid = e["execution_ref"].as_str().unwrap();
    let mut worker = NativeTransport::new(api, factory.clone());
    assert_eq!(
        worker.dispatch(eid, false).unwrap()["dispatch_state"],
        "ACKED"
    );
    assert_eq!(worker.observe(eid).unwrap()["status"], "SUCCEEDED");
    // Metadata outbox survives terminal completion; lost title ACK is read back
    // from Provider and never changes execution outcome.
    factory.0.borrow_mut().lose_title = true;
    assert_eq!(worker.metadata(eid).unwrap()["state"], "DELIVERED");
    let task = e["task_ref"].as_str().unwrap();
    let e2 = start(&mut worker.authority, "lost", Some(task));
    let eid2 = e2["execution_ref"].as_str().unwrap();
    factory.0.borrow_mut().lose_input = true;
    assert_eq!(
        worker.dispatch(eid2, false).unwrap_err(),
        "PROVIDER_DELIVERY_UNKNOWN"
    );
    let sends = factory.0.borrow().sent;
    let policy = worker.authority.policy.clone();
    drop(worker);
    let store = Store::open(&dir, false).unwrap();
    store.configure_business(&policy, false).unwrap();
    let mut worker = NativeTransport::new(Api { store, policy }, factory.clone());
    assert_eq!(
        worker.recover_input(eid2).unwrap()["dispatch_state"],
        "ACKED"
    );
    assert_eq!(worker.observe(eid2).unwrap()["status"], "SUCCEEDED");
    worker.tick().unwrap();
    assert_eq!(factory.0.borrow().sent, sends);
    let duplicate = worker
        .authority
        .call(
            "start",
            json!({"prepared_execution_ref":e2["prepared_execution_ref"],"approved":true}),
            "start-lost",
        )
        .unwrap();
    assert_eq!(duplicate, e2);
    assert_eq!(
        worker
            .authority
            .call(
                "start",
                json!({"prepared_execution_ref":"different","approved":true}),
                "start-lost"
            )
            .unwrap_err(),
        "OPERATION_ID_CONFLICT"
    );
    worker.authority.store.verify_business().unwrap();
    drop(worker);
    fs::remove_dir_all(dir).unwrap();
}
#[test]
fn unknown_create_is_not_repeated_and_api_cannot_grant() {
    let (dir, mut api, factory) = environment("create");
    let e = start(&mut api, "unknown", None);
    let eid = e["execution_ref"].as_str().unwrap();
    factory.0.borrow_mut().lose_create = true;
    let mut worker = NativeTransport::new(api, factory.clone());
    assert_eq!(
        worker.dispatch(eid, false).unwrap_err(),
        "PROVIDER_DELIVERY_UNKNOWN"
    );
    assert_eq!(
        worker.dispatch(eid, true).unwrap_err(),
        "NATIVE_CREATE_ACK_UNKNOWN"
    );
    assert_eq!(factory.0.borrow().created, 1);
    assert_eq!(factory.0.borrow().sent, 0);
    assert_eq!(
        worker
            .authority
            .store
            .business(
                Role::Api,
                &worker.authority.policy,
                "native_create_take",
                "forged",
                &json!({"execution_ref":eid})
            )
            .unwrap_err(),
        "TRANSPORT_PEER_REQUIRED"
    );
    worker.authority.store.verify_business().unwrap();
    drop(worker);
    fs::remove_dir_all(dir).unwrap();
}
#[test]
fn cancel_lost_ack_is_observed_without_second_interrupt() {
    let (dir, mut api, factory) = environment("cancel");
    let e = start(&mut api, "running", None);
    let eid = e["execution_ref"].as_str().unwrap();
    factory.0.borrow_mut().live = true;
    let mut worker = NativeTransport::new(api, factory.clone());
    worker.dispatch(eid, false).unwrap();
    worker
        .authority
        .call(
            "cancel",
            json!({"execution_ref":eid,"approved":true}),
            "cancel",
        )
        .unwrap();
    factory.0.borrow_mut().lose_cancel = true;
    let tick = worker.tick().unwrap();
    assert_eq!(
        tick["results"]
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["execution_ref"] == eid)
            .unwrap()["error"],
        "PROVIDER_DELIVERY_UNKNOWN"
    );
    let tick = worker.tick().unwrap();
    assert_eq!(
        tick["results"]
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["execution_ref"] == eid)
            .unwrap()["result"]["status"],
        "CANCELLED"
    );
    assert_eq!(factory.0.borrow().interrupted, 1);
    worker.authority.store.verify_business().unwrap();
    drop(worker);
    fs::remove_dir_all(dir).unwrap();
}
#[test]
fn foreign_input_cannot_ack_or_replay() {
    let (dir, mut api, factory) = environment("foreign");
    let e = start(&mut api, "wrong", None);
    let eid = e["execution_ref"].as_str().unwrap();
    factory.0.borrow_mut().wrong_input = true;
    let mut worker = NativeTransport::new(api, factory.clone());
    assert_eq!(
        worker.dispatch(eid, false).unwrap_err(),
        "NATIVE_INPUT_IDENTITY_UNPROVEN"
    );
    worker.tick().unwrap();
    assert_eq!(factory.0.borrow().sent, 1);
    let status = worker
        .authority
        .call("get_status", json!({"execution_ref":eid}), "status")
        .unwrap();
    assert_eq!(status["dispatch_state"], "SENDING");
    assert_ne!(status["resource_state"], "RELEASED");
    drop(worker);
    fs::remove_dir_all(dir).unwrap();
}

// A competing worker may have consumed the physical send grant while this
// worker was reading/binding the same existing thread. Cached true is a receipt,
// never permission to issue another turn/start.
#[test]
fn concurrent_worker_cannot_reuse_another_workers_cached_send_permit() {
    struct Competitor(Api);
    impl Authority for Competitor {
        fn call(&mut self, op: &str, p: Value, id: &str) -> Result<Value> {
            if op == "authorize_send" {
                let competing_id = clinx_decision_kernel::native_transport::stable_id(
                    "authorize_send",
                    p["execution_ref"].as_str().unwrap(),
                );
                self.0.call(op, p.clone(), &competing_id)?;
            }
            self.0.call(op, p, id)
        }
    }
    let (dir, mut api, factory) = environment("competing-send");
    let e = start(&mut api, "competing", None);
    let eid = e["execution_ref"].as_str().unwrap();
    let mut worker = NativeTransport::new(Competitor(api), factory.clone());
    assert_eq!(
        worker.dispatch(eid, false).unwrap_err(),
        "SEND_NOT_AVAILABLE"
    );
    assert_eq!(factory.0.borrow().sent, 0);
    assert!(worker.recover_input(eid).is_err());
    assert_eq!(factory.0.borrow().sent, 0);
    drop(worker);
    fs::remove_dir_all(dir).unwrap();
}
