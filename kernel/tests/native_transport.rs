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
    catalog: Option<Value>,
    catalog_unavailable: bool,
    input_params: Vec<Value>,
    turn_error: Option<Value>,
    item_page_size: usize,
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
            "model/list" => {
                if s.catalog_unavailable {
                    Err("PROVIDER_REMOTE_ERROR".into())
                } else {
                    Ok(s.catalog.clone().unwrap_or_else(|| json!({"data":[
                    {"id":"gpt-6-astra","model":"gpt-6-astra","defaultReasoningEffort":"low",
                     "supportedReasoningEfforts":[{"reasoningEffort":"low"},{"reasoningEffort":"xhigh"}]}],"nextCursor":null})))
                }
            }
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
                s.input_params.push(p.clone());
                let id = format!("native-turn-{}", s.sent);
                let client = if s.wrong_input {
                    json!("somebody-else")
                } else {
                    p["clientUserMessageId"].clone()
                };
                let status = if s.live { "inProgress" } else { "completed" };
                let mut turn = json!({"id":id,"status":status,"items":[{"id":format!("input-{}",s.sent),"type":"userMessage","clientId":client,"content":p["input"]},{"id":"result","type":"agentMessage","phase":"final_answer","text":"Exact native result"}]});
                if let Some(error) = &s.turn_error {
                    turn["status"] = json!("failed");
                    turn["error"] = error.clone();
                }
                s.turns.insert(0, turn.clone());
                if s.lose_input {
                    Err("PROVIDER_DELIVERY_UNKNOWN".into())
                } else {
                    Ok(json!({"turn":turn}))
                }
            }
            "thread/turns/list" => Ok(json!({"data":s.turns,"nextCursor":null})),
            "thread/items/list" => {
                let items = s.turns.iter().find(|t| t["id"] == p["turnId"]).unwrap()["items"]
                    .as_array()
                    .unwrap();
                let start = p["cursor"]
                    .as_str()
                    .map(|c| c.parse::<usize>().unwrap())
                    .unwrap_or(0);
                let size = if s.item_page_size == 0 {
                    items.len()
                } else {
                    s.item_page_size
                };
                let end = (start + size).min(items.len());
                Ok(
                    json!({"data":items[start..end].iter().map(|i|json!({"turnId":p["turnId"],"item":i})).collect::<Vec<_>>(),
                    "nextCursor":if end < items.len() {json!(end.to_string())}else{Value::Null}}),
                )
            }
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
fn long_native_turn_recovery_scans_six_pages_and_rejects_late_duplicate_without_resend() {
    for duplicate in [false, true] {
        let (_dir, mut api, factory) = environment(if duplicate {
            "long-duplicate"
        } else {
            "long-recovery"
        });
        let e = start(&mut api, "long", None);
        let eid = e["execution_ref"].as_str().unwrap();
        factory.0.borrow_mut().lose_input = true;
        let mut worker = NativeTransport::new(api, factory.clone());
        assert_eq!(
            worker.dispatch(eid, false).unwrap_err(),
            "PROVIDER_DELIVERY_UNKNOWN"
        );
        {
            let mut s = factory.0.borrow_mut();
            s.item_page_size = 100;
            let items = s.turns[0]["items"].as_array_mut().unwrap();
            let input = items[0].clone();
            for i in 2..552 {
                items.push(json!({"type":"commandExecution","id":format!("tool-{i}")}));
            }
            if duplicate {
                items[551] = input;
            }
        }
        let result = worker.recover_execution(eid);
        if duplicate {
            assert_eq!(result.unwrap_err(), "NATIVE_INPUT_IDENTITY_UNPROVEN");
        } else {
            let value = result.unwrap();
            assert_eq!(value["status"], "SUCCEEDED");
            assert_eq!(value["resource_state"], "RELEASED");
            assert_eq!(value["dispatch_state"], "ACKED");
        }
        let s = factory.0.borrow();
        assert_eq!(s.sent, 1);
        assert_eq!(s.created, 1);
        assert_eq!(s.interrupted, 0);
    }
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

fn selected(api: &mut Api, key: &str, model: Value, effort: Value) -> Value {
    let p = api.call("prepare", json!({"host":"air","project":"fixture","title":"Model qualification",
        "prompt":"Reply only OK. Use no tools.","approved":true,"model":model,"reasoning_effort":effort}),
        &format!("prepare-{key}")).unwrap();
    api.call(
        "start",
        json!({"prepared_execution_ref":p["prepared_execution_ref"],"approved":true}),
        &format!("start-{key}"),
    )
    .unwrap()
}
#[test]
fn selected_model_default_mapping_lost_ack_and_exact_read_never_resend() {
    let (dir, mut api, factory) = environment("model-lost-ack");
    let e = selected(&mut api, "astra", json!("gpt-6-astra"), json!("extra_high"));
    let eid = e["execution_ref"].as_str().unwrap();
    factory.0.borrow_mut().lose_input = true;
    let mut worker = NativeTransport::new(api, factory.clone());
    assert_eq!(
        worker.dispatch(eid, false).unwrap_err(),
        "PROVIDER_DELIVERY_UNKNOWN"
    );
    assert_eq!(
        worker.last_provider_error.as_ref().unwrap()["request_stage"],
        "turn/start"
    );
    assert_eq!(factory.0.borrow().input_params[0]["model"], "gpt-6-astra");
    assert_eq!(factory.0.borrow().input_params[0]["effort"], "xhigh");
    assert_eq!(
        worker.recover_input(eid).unwrap()["dispatch_state"],
        "ACKED"
    );
    assert_eq!(worker.observe(eid).unwrap()["status"], "SUCCEEDED");
    worker.tick().unwrap();
    assert_eq!(factory.0.borrow().sent, 1);
    fs::remove_dir_all(dir).unwrap();
}
#[test]
fn invalid_selection_and_unavailable_catalog_close_before_any_native_mutation() {
    for (name, model, effort, missing, expected) in [
        (
            "effort",
            "gpt-6-astra",
            json!("nonsense"),
            false,
            "PROVIDER_REASONING_EFFORT_UNSUPPORTED",
        ),
        (
            "display",
            "GPT-6-Astra",
            json!("low"),
            false,
            "PROVIDER_MODEL_UNAVAILABLE",
        ),
        (
            "missing",
            "unavailable-model",
            Value::Null,
            false,
            "PROVIDER_MODEL_UNAVAILABLE",
        ),
        (
            "catalog",
            "gpt-6-astra",
            Value::Null,
            true,
            "PROVIDER_MODEL_LIST_UNAVAILABLE",
        ),
    ] {
        let (dir, mut api, factory) = environment(&format!("reject-{name}"));
        factory.0.borrow_mut().catalog_unavailable = missing;
        let e = selected(&mut api, name, json!(model), effort);
        let eid = e["execution_ref"].as_str().unwrap();
        let mut worker = NativeTransport::new(api, factory.clone());
        let result = worker.dispatch(eid, false).unwrap();
        assert_eq!(result["status"], "FAILED");
        assert_eq!(result["delivery_certainty"], "NOT_DISPATCHED");
        assert_eq!(result["resource_state"], "RELEASED");
        assert_eq!(result["execution_result"]["reason"], expected);
        assert_eq!(
            result["execution_result"]["provider_error"]["code"],
            expected
        );
        worker.tick().unwrap();
        assert_eq!(
            (factory.0.borrow().created, factory.0.borrow().sent),
            (0, 0)
        );
        fs::remove_dir_all(dir).unwrap();
    }
}
#[test]
fn provider_terminal_error_is_durable_structural_and_redacted() {
    let (dir, mut api, factory) = environment("upstream-terminal");
    factory.0.borrow_mut().turn_error = Some(json!({"codexErrorInfo":"other",
        "message":"{\"error\":{\"type\":\"upstream_error\",\"message\":\"Upstream request failed SECRET_SENTINEL\"}}",
        "additionalDetails":"Authorization: Bearer SECRET_SENTINEL"}));
    let e = selected(&mut api, "terminal", json!("gpt-6-astra"), Value::Null);
    let eid = e["execution_ref"].as_str().unwrap();
    let mut worker = NativeTransport::new(api, factory.clone());
    assert_eq!(
        worker.dispatch(eid, false).unwrap()["dispatch_state"],
        "ACKED"
    );
    assert!(factory.0.borrow().input_params[0].get("effort").is_none());
    let result = worker.observe(eid).unwrap();
    assert_eq!(result["status"], "FAILED");
    let detail = &result["execution_result"]["provider_error"];
    assert_eq!(detail["category"], "UPSTREAM_SERVICE");
    assert_eq!(detail["provider_type"], "upstream_error");
    assert_eq!(detail["request_stage"], "turn/terminal");
    assert!(
        !result["execution_result"]
            .to_string()
            .contains("SECRET_SENTINEL")
    );
    assert_eq!(
        worker
            .authority
            .call(
                "get_status",
                json!({"execution_ref":eid}),
                "exact-error-status"
            )
            .unwrap()["execution_result"],
        result["execution_result"]
    );
    fs::remove_dir_all(dir).unwrap();
}
#[test]
fn capability_values_are_model_specific_native_ids_and_diagnostics_do_not_guess_from_400() {
    use clinx_decision_kernel::provider::{error_diagnostic, validate_model_selection};
    let catalog = json!({"data":[{"id":"picker-entry","model":"native-model","displayName":"Friendly Name",
        "supportedReasoningEfforts":[{"reasoningEffort":"ultra"}],"defaultReasoningEffort":"ultra"}]});
    assert_eq!(
        validate_model_selection(&catalog, &json!("native-model"), &json!("ultra")).unwrap(),
        json!({"model":"native-model","effort":"ultra"})
    );
    for wrong in ["picker-entry", "Friendly Name", "MODEL"] {
        assert_eq!(
            validate_model_selection(&catalog, &json!(wrong), &Value::Null).unwrap_err(),
            "PROVIDER_MODEL_UNAVAILABLE"
        );
    }
    for (remote, expected) in [
        (
            json!({"code":-32602,"message":"secret"}),
            "INVALID_PARAMETERS",
        ),
        (
            json!({"code":-32000,"type":"authentication_error"}),
            "AUTHENTICATION",
        ),
        (
            json!({"code":-32000,"type":"error","message":"{\"error\":{\"code\":\"model_not_found\"}}"}),
            "MODEL_UNAVAILABLE",
        ),
        (
            json!({"codexErrorInfo":{"httpConnectionFailed":{"httpStatusCode":null}}}),
            "NETWORK",
        ),
        (
            json!({"codexErrorInfo":{"responseStreamConnectionFailed":{"httpStatusCode":503}}}),
            "UPSTREAM_SERVICE",
        ),
        (
            json!({"data":{"httpStatusCode":400},"message":"Upstream request failed"}),
            "UNKNOWN",
        ),
        (
            json!({"data":{"httpStatusCode":400},"message":"{\"error\":{\"type\":\"upstream_error\"}}"}),
            "UPSTREAM_SERVICE",
        ),
    ] {
        let detail = error_diagnostic("PROVIDER_REMOTE_ERROR", "turn/start", &remote);
        assert_eq!(detail["category"], expected, "{remote}");
        assert!(!detail.to_string().contains("secret"));
        assert_eq!(
            error_diagnostic(&detail.to_string(), "turn/start", &Value::Null)["category"],
            expected
        );
    }
}

#[test]
fn model_catalog_pagination_malformed_and_hidden_entries_fail_closed() {
    struct Catalog {
        pages: std::collections::VecDeque<Value>,
        cursors: Vec<Value>,
    }
    impl Provider for Catalog {
        fn request(&mut self, method: &str, params: Value) -> Result<Value> {
            assert_eq!(method, "model/list");
            self.cursors.push(params["cursor"].clone());
            Ok(self.pages.pop_front().unwrap())
        }
    }
    let row = json!({"id":"native","supportedReasoningEfforts":["low"]});
    let mut provider = Catalog {
        pages: [
            json!({"data":[],"nextCursor":"next"}),
            json!({"data":[row],"nextCursor":null}),
        ]
        .into(),
        cursors: vec![],
    };
    assert_eq!(
        provider
            .model_selection(&json!("native"), &json!("low"))
            .unwrap(),
        json!({"model":"native","effort":"low"})
    );
    assert_eq!(provider.cursors, vec![Value::Null, json!("next")]);
    for (catalog, expected) in [
        (
            json!({"data":[{"id":"native","supportedReasoningEfforts":null}]}),
            "PROVIDER_MODEL_CAPABILITY_INVALID",
        ),
        (
            json!({"data":[{"id":"native","supportedReasoningEfforts":["low"],"hidden":true}]}),
            "PROVIDER_MODEL_UNAVAILABLE",
        ),
        (json!({"data":[row,row]}), "PROVIDER_MODEL_UNAVAILABLE"),
        (
            json!({"data":[{"id":"native","supportedReasoningEfforts":["low"],"defaultReasoningEffort":"high"}]}),
            "PROVIDER_MODEL_CAPABILITY_INVALID",
        ),
    ] {
        let mut provider = Catalog {
            pages: [catalog].into(),
            cursors: vec![],
        };
        assert_eq!(
            provider
                .model_selection(&json!("native"), &json!("low"))
                .unwrap_err(),
            expected
        );
    }
}
