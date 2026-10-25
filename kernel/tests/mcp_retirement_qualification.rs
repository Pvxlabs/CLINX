//! Coverage for ignored legacy qualification files affected by facade retirement.
//! Public Rust requests are the contract. Prompt-only Python Git routing was
//! already absent in main 29f72a6 and is explicitly NOT claimed as equivalent.
use clinx_decision_kernel::{
    mcp::{self, NoObservations, Server},
    mcp_api::{self, Api, Config, Reads},
    native_transport::Authority,
    provider::Result,
};
use serde_json::{Value, json};
use std::{cell::RefCell, process::Command, rc::Rc};
const THREAD: &str = "11111111-1111-7111-8111-111111111111";
#[derive(Default)]
struct State {
    calls: Vec<(String, Value)>,
    reads: Vec<String>,
    tasks: Vec<Value>,
    prepared: Value,
    cwd: String,
    unreadable: bool,
    pages: Option<Vec<Value>>,
}
#[derive(Clone)]
struct IO(Rc<RefCell<State>>);
impl Authority for IO {
    fn call(&mut self, op: &str, p: Value, _: &str) -> Result<Value> {
        let mut s = self.0.borrow_mut();
        s.calls.push((op.into(), p.clone()));
        Ok(match op {
            "list_tasks" => {
                if let Some(pages) = &mut s.pages {
                    pages.remove(0)
                } else {
                    json!({"tasks":s.tasks})
                }
            }
            "prepare" => {
                json!({"prepared_execution_ref":"prepared_existing","task_ref":p["task_ref"],"workspace_contract":{"cwd":p["workspace_contract"]["cwd"]},"status":"PREPARED"})
            }
            "get_prepared" => s.prepared.clone(),
            "start" => {
                s.prepared["execution_ref"] = json!("exec_existing");
                json!({"execution_ref":"exec_existing"})
            }
            "adopt" => {
                json!({"adoption_status":"PENDING","thread_id":p["thread_id"],"task_ref":"task_adopted"})
            }
            "get_status" => {
                json!({"execution_ref":"exec_old","task_ref":"task_existing","thread_id":THREAD,"native_turn_id":"historical-turn","status":"SUCCEEDED","execution_result":{"response_text":"Original historical reply"}})
            }
            _ => panic!("unexpected command: {op}"),
        })
    }
}
impl Reads for IO {
    fn thread(&mut self, tid: &str) -> Result<Value> {
        let mut s = self.0.borrow_mut();
        s.reads.push(tid.into());
        if s.unreadable {
            Err("WORKSPACE_CWD_UNREADABLE".into())
        } else {
            Ok(json!({"id":tid,"cwd":s.cwd,"status":{"type":"idle"}}))
        }
    }
    fn page(&mut self, tid: &str, _: u64, _: Option<&str>) -> Result<Value> {
        assert_eq!(tid, THREAD);
        Ok(json!({"data":[{"id":"historical-turn","items":[]}],"nextCursor":null}))
    }
    fn desktop(&mut self, _: &str, _: bool, _: u64, _: usize, _: Option<&str>) -> Result<Value> {
        panic!("unexpected desktop read")
    }
}
fn fixture() -> (Server<IO, IO, NoObservations>, Rc<RefCell<State>>) {
    let task = json!({"task_id":"task_existing","project":"orion","host":"p620","title":"Position audit","thread_id":THREAD,"workspace_contract":{"cwd":"/retired/checkout","branch":"old","head":"0".repeat(40)},"execution_policy":{"execution_surface":"CODEX_NATIVE"},"lifecycle":"ACTIVE"});
    let s = Rc::new(RefCell::new(State {
        tasks: vec![task.clone()],
        prepared: json!({"prepared_execution_ref":"prepared_existing","task_ref":"task_existing","new_task":false,"thread_id":THREAD,"workspace_contract":task["workspace_contract"],"execution_policy":task["execution_policy"]}),
        cwd: "/actual/native/worktree".into(),
        ..Default::default()
    }));
    let config:Config=serde_json::from_value(json!({"runtime_host":"p620","provider_endpoint":"/unused","provider_timeout_seconds":1,"host_aliases":{"p620":["p620"]},"codex_host_ids":{"remote-ssh-discovered:p620":["p620"]}})).unwrap();
    (
        Server {
            api: Api {
                authority: IO(s.clone()),
                reads: IO(s.clone()),
                config,
            },
            observations: NoObservations,
        },
        s,
    )
}
fn call(s: &mut Server<IO, IO, NoObservations>, name: &str, a: Value) -> Value {
    let r = s
        .handle(json!({"id":1,"method":"tools/call","params":{"name":name,"arguments":a}}))
        .unwrap();
    assert!(r.get("error").is_none(), "{r}");
    r["result"].clone()
}
fn success(v: Value) -> Value {
    assert_eq!(v["isError"], false, "{v}");
    v["structuredContent"].clone()
}
fn create() -> Value {
    json!({"approved":true,"task_action":"create","project":"orion","prompt":"Unchanged user input","workspace_contract":{"cwd":"/new/native/worktree"}})
}
fn host() -> Value {
    json!({"required_capabilities":["HOST_FILESYSTEM"],"operation_classes":["DEVELOPMENT_MUTATION"],"requested_operations":[{"capability":"HOST_FILESYSTEM","operation":"path_write","operation_class":"DEVELOPMENT_MUTATION","target":"fixture"}]})
}
#[test]
fn cwd_only_catalog_and_public_receipts_preserve_explicit_contract() {
    let registry = mcp::registry();
    for name in ["clinx_prepare_execution", "clinx_adopt_conversation"] {
        let t = registry["tools"]
            .as_array()
            .unwrap()
            .iter()
            .find(|t| t["name"] == name)
            .unwrap();
        let route = &t["inputSchema"]["properties"]["workspace_contract"];
        assert_eq!(route["required"], json!(["cwd"]));
        let keys = route["properties"].as_object().unwrap();
        assert_eq!(keys.len(), 3);
        for k in ["cwd", "branch", "head"] {
            assert!(keys.contains_key(k));
        }
        if name == "clinx_prepare_execution" {
            assert_eq!(
                t["outputSchema"]["properties"]["workspace_contract"]["required"],
                json!(["cwd"])
            );
            assert!(
                !t["description"]
                    .as_str()
                    .unwrap()
                    .contains("exact cwd, branch and HEAD")
            );
        }
    }
    for route in [
        json!({"cwd":"/home/user/worktrees/dev-next"}),
        json!({"cwd":"/home/user/worktrees/dev-next","branch":"old-branch","head":"0".repeat(40)}),
    ] {
        let (mut server, s) = fixture();
        let mut a = create();
        a["workspace_contract"] = route.clone();
        let r = success(call(&mut server, "clinx_prepare_execution", a));
        assert_eq!(s.borrow().calls[0].0, "prepare");
        assert_eq!(s.borrow().calls[0].1["workspace_contract"], route);
        assert_eq!(s.borrow().calls[0].1["prompt"], "Unchanged user input");
        assert_eq!(r["workspace_contract"], json!({"cwd":route["cwd"]}));
    }
}
#[test]
fn continuation_start_selector_and_adoption_use_exact_live_cwd() {
    for action in ["continue", "reopen"] {
        let (mut server, s) = fixture();
        let r = success(call(
            &mut server,
            "clinx_prepare_execution",
            json!({"approved":true,"task_ref":"task_existing","task_action":action,"prompt":"Continue exactly","workspace_contract":{"cwd":"/wrong/request"}}),
        ));
        let state = s.borrow();
        let p = &state.calls.last().unwrap().1;
        assert_eq!(
            p["workspace_contract"],
            json!({"cwd":"/actual/native/worktree"})
        );
        assert_eq!(p["thread_id"], THREAD);
        assert_eq!(r["task_ref"], "task_existing");
    }
    let (mut server, s) = fixture();
    s.borrow_mut().cwd = "/moved/within-repository".into();
    let a = json!({"prepared_execution_ref":"prepared_existing","approved":true});
    let first = success(call(&mut server, "clinx_start_execution", a.clone()));
    assert_eq!(
        s.borrow().calls.last().unwrap().1["native_cwd"],
        "/moved/within-repository"
    );
    s.borrow_mut().unreadable = true;
    let reads = s.borrow().reads.len();
    assert_eq!(
        success(call(&mut server, "clinx_start_execution", a)),
        first
    );
    assert_eq!(s.borrow().reads.len(), reads);
    let (mut server, s) = fixture();
    success(call(
        &mut server,
        "clinx_prepare_execution",
        json!({"approved":true,"codex_uri":format!("codex://threads/{THREAD}"),"prompt":"Exact input"}),
    ));
    assert_eq!(
        s.borrow().calls.last().unwrap().1["task_ref"],
        "task_existing"
    );
    assert_eq!(
        s.borrow().calls.last().unwrap().1["workspace_contract"]["cwd"],
        "/actual/native/worktree"
    );
    let (mut server, s) = fixture();
    s.borrow_mut().cwd = "/actual/adopted/worktree".into();
    success(call(
        &mut server,
        "clinx_adopt_conversation",
        json!({"codex_uri":format!("codex://threads/{THREAD}"),"project":"orion","workspace_contract":{"cwd":"/old/registration"}}),
    ));
    assert_eq!(s.borrow().calls.last().unwrap().0, "adopt");
    assert_eq!(
        s.borrow().calls.last().unwrap().1["workspace_contract"]["cwd"],
        "/actual/adopted/worktree"
    );
}
#[test]
fn unreadable_cwd_never_prepares_or_starts() {
    let (mut server, s) = fixture();
    s.borrow_mut().unreadable = true;
    for (name, a) in [
        (
            "clinx_prepare_execution",
            json!({"approved":true,"task_ref":"task_existing","prompt":"Exact input"}),
        ),
        (
            "clinx_start_execution",
            json!({"approved":true,"prepared_execution_ref":"prepared_existing"}),
        ),
    ] {
        let r = call(&mut server, name, a);
        assert_eq!(r["isError"], true);
        assert!(r.to_string().contains("WORKSPACE_CWD_UNREADABLE"));
    }
    assert!(
        !s.borrow()
            .calls
            .iter()
            .any(|(op, _)| op == "prepare" || op == "start")
    );
}
#[test]
fn summary_pagination_advances_and_rejects_all_invalid_cursors() {
    let (mut server, s) = fixture();
    s.borrow_mut().pages = Some(vec![
        json!({"tasks":[{"task_id":"task-a"}],"next_after":"task-a"}),
        json!({"tasks":[{"task_id":"task-b"}],"next_after":null}),
    ]);
    assert_eq!(
        server.api.tasks().unwrap(),
        vec![json!({"task_id":"task-a"}), json!({"task_id":"task-b"})]
    );
    assert_eq!(
        s.borrow().calls,
        vec![
            (
                "list_tasks".into(),
                json!({"projection":"summary","after":""})
            ),
            (
                "list_tasks".into(),
                json!({"projection":"summary","after":"task-a"})
            )
        ]
    );
    for cursor in [json!(""), json!("task-a"), json!(1)] {
        let (mut server, s) = fixture();
        s.borrow_mut().pages = Some(vec![
            json!({"tasks":[],"next_after":"task-a"}),
            json!({"tasks":[],"next_after":cursor}),
        ]);
        assert_eq!(server.api.tasks().unwrap_err().code, "INVALID_TASK_CURSOR");
    }
}
#[test]
fn explicit_native_context_never_grants_host_policy_all_fourteen_cases() {
    let h = host();
    let contexts = [
        json!({}),
        json!({"required_capabilities":h["required_capabilities"]}),
        json!({"operation_classes":h["operation_classes"]}),
        json!({"production_mutation_intent":true}),
        json!({"requested_operations":h["requested_operations"]}),
        h.clone(),
        {
            let mut x = h;
            x["production_mutation_intent"] = json!(true);
            x
        },
    ];
    for existing in [false, true] {
        for context in &contexts {
            let (mut server, s) = fixture();
            let mut a = if existing {
                json!({"approved":true,"task_ref":"task_existing","prompt":"Preserve this exact input"})
            } else {
                create()
            };
            a["execution_surface"] = json!("CODEX_NATIVE");
            a.as_object_mut()
                .unwrap()
                .extend(context.as_object().unwrap().clone());
            let prompt = a["prompt"].clone();
            let r = success(call(&mut server, "clinx_prepare_execution", a));
            let state = s.borrow();
            let p = &state.calls.last().unwrap().1;
            assert!(p["execution_policy"].is_null());
            assert_eq!(p["prompt"], prompt);
            assert_eq!(
                p["workspace_contract"]["cwd"],
                if existing {
                    "/actual/native/worktree"
                } else {
                    "/new/native/worktree"
                }
            );
            assert_eq!(r["execution_started"], false);
            assert!(
                state
                    .calls
                    .iter()
                    .all(|(op, _)| op == "list_tasks" || op == "prepare")
            );
        }
    }
}
#[test]
fn unbound_uri_keeps_exact_host_thread_without_start() {
    let (mut server, s) = fixture();
    s.borrow_mut().tasks.clear();
    let mut a = json!({"approved":true,"codex_uri":format!("codex://threads/{THREAD}?hostId=remote-ssh-discovered%3Ap620"),"project":"fixture","prompt":"Preserve original input","execution_surface":"CODEX_NATIVE"});
    a.as_object_mut()
        .unwrap()
        .extend(host().as_object().unwrap().clone());
    let r = success(call(&mut server, "clinx_prepare_execution", a));
    let state = s.borrow();
    assert_eq!(state.reads, vec![THREAD]);
    assert_eq!(
        state
            .calls
            .iter()
            .map(|(op, _)| op.as_str())
            .collect::<Vec<_>>(),
        ["list_tasks", "prepare"]
    );
    let p = &state.calls[1].1;
    assert_eq!(p["thread_id"], THREAD);
    assert_eq!(p["host"], "p620");
    assert!(p["task_ref"].is_null());
    assert!(p["execution_policy"].is_null());
    assert_eq!(
        p["workspace_contract"],
        json!({"cwd":"/actual/native/worktree"})
    );
    assert_eq!(r["execution_started"], false);
}
#[test]
fn host_scope_validation_deprecated_network_and_lifecycle_filters() {
    for surface in [Value::Null, json!("HOST_EXECUTOR")] {
        let (mut server, s) = fixture();
        let mut a = create();
        a["execution_surface"] = surface;
        a.as_object_mut()
            .unwrap()
            .extend(host().as_object().unwrap().clone());
        success(call(&mut server, "clinx_prepare_execution", a.clone()));
        assert_eq!(
            s.borrow().calls.last().unwrap().1["execution_policy"],
            json!({"execution_surface":"HOST_EXECUTOR","operation_scopes":host()["requested_operations"]})
        );
        s.borrow_mut().calls.clear();
        a["operation_classes"] = json!(["READ_ONLY_HOST"]);
        assert_eq!(
            call(&mut server, "clinx_prepare_execution", a)["structuredContent"]["error_code"],
            "INVALID_HOST_POLICY"
        );
        assert!(s.borrow().calls.is_empty());
    }
    for network in [false, true] {
        let (mut server, s) = fixture();
        let mut a = create();
        a["network_access"] = json!(network);
        success(call(&mut server, "clinx_prepare_execution", a));
        assert!(s.borrow().calls.last().unwrap().1["execution_policy"].is_null());
        assert_eq!(
            s.borrow().calls.last().unwrap().1["workspace_contract"]["cwd"],
            "/new/native/worktree"
        );
    }
    let (mut server, s) = fixture();
    s.borrow_mut().tasks.push(json!({"task_id":"task_two","title":"Position audit","project":"orion","host":"other","lifecycle":"OPEN"}));
    let r = success(call(
        &mut server,
        "clinx_find_task",
        json!({"query":"Position","project":"orion","host":"p620","status":"ACTIVE"}),
    ));
    assert_eq!(r["tasks"].as_array().unwrap().len(), 1);
    assert_eq!(r["tasks"][0]["task_ref"], "task_existing");
    for a in [
        json!({"query":"Position","host":"p620","status":"OPEN"}),
        json!({"query":"Position","host":"missing"}),
    ] {
        assert_eq!(
            success(call(&mut server, "clinx_find_task", a))["tasks"],
            json!([])
        );
    }
}
#[test]
fn host_failure_projection_preserves_authority_state_and_scrubs_private_fields() {
    let turn = json!({"items":[{"type":"dynamicToolCall","namespace":"clinx","tool":"clinx_host_operation","id":"call-one","success":false,"contentItems":[{"text":json!({"reconciliation_required":true,"error":"ROOT_CONTROLLED_WORKER_LAUNCHER_REQUIRED","private":"hidden"}).to_string()}]}]});
    let r = mcp_api::host_readback(
        json!({"execution_ref":"exec_one","status":"RUNNING"}),
        &turn,
    );
    assert_eq!(r["status"], "RUNNING");
    assert_eq!(r["current_stage"], "HOST_RECONCILIATION_REQUIRED");
    assert_eq!(
        r["failure_code"],
        "ROOT_CONTROLLED_WORKER_LAUNCHER_REQUIRED"
    );
    assert!(r["host_executions"][0].get("private").is_none());
    assert_eq!(
        r["host_executions"][0]["source"],
        "NATIVE_DYNAMIC_TOOL_RECEIPT"
    );
    let mut r = r;
    r["resource_state"] = json!("RELEASED");
    let r = mcp_api::host_readback(r, &turn);
    assert_eq!(r["reconciliation_required"], false);
    assert_eq!(r["current_stage"], "FINALIZED");
}
#[test]
fn exact_historical_receipt_keeps_native_cwd_and_original_task_projection() {
    let (mut server, _) = fixture();
    let r = success(call(
        &mut server,
        "clinx_get_context",
        json!({"execution_ref":"exec_old"}),
    ));
    assert_eq!(r["execution_turn_ref"], "historical-turn");
    assert_eq!(
        r["execution_result"],
        json!({"response_text":"Original historical reply"})
    );
    assert_eq!(r["native_thread"]["cwd"], "/actual/native/worktree");
    assert_eq!(
        r["task_current_projection"]["workspace_contract"]["cwd"],
        "/retired/checkout"
    );
}
#[test]
fn prompt_prose_cannot_override_explicit_workspace_current_rust_contract() {
    // The first nine cases preserve the old no-routing invariant. The remaining
    // nine deliberately document CURRENT Rust semantics, not legacy regex/Git seal.
    let prompts = [
        "保留工作树/Owner/Provider/业务任务",
        "工作目录/配置/数据库",
        "执行目录/资源分类",
        "worktree/owner/provider",
        "cwd/category",
        "working-directory/category",
        "workingXdirectory=/does-not-exist",
        "somecwd=/does-not-exist",
        "my_worktree /does-not-exist",
        "cwd=/other",
        "工作目录：/other",
        "工作树 /other",
        "worktree `/other`",
        "working.directory: \"/other\"",
        "working directory /other",
        "执行目录`/other`",
        "cwd=/missing",
        "cwd=/first 工作目录：/second",
    ];
    for prompt in prompts {
        let (mut server, s) = fixture();
        let mut a = create();
        a["prompt"] = json!(prompt);
        success(call(&mut server, "clinx_prepare_execution", a));
        let state = s.borrow();
        assert_eq!(state.calls.len(), 1);
        assert_eq!(state.calls[0].0, "prepare");
        assert_eq!(state.calls[0].1["prompt"], prompt);
        assert_eq!(
            state.calls[0].1["workspace_contract"],
            json!({"cwd":"/new/native/worktree"})
        );
        assert!(state.reads.is_empty());
    }
}
#[test]
fn missing_config_is_stderr_only_failclosed_for_native_entrypoint() {
    let t = tempfile::tempdir().unwrap();
    let r = Command::new(env!("CARGO_BIN_EXE_clinx-mcp"))
        .args(["--stdio", "--config"])
        .arg(t.path().join("missing.json"))
        .output()
        .unwrap();
    assert!(!r.status.success());
    assert!(r.stdout.is_empty());
    let error = String::from_utf8(r.stderr).unwrap();
    assert!(error.contains("MCP_SERVER=BLOCKED"));
    assert!(error.contains("MCP_CONFIG_REQUIRED"));
}

#[path = "support/provider.rs"]
mod provider;
#[path = "support/runtime_qualification.rs"]
mod runtime;
#[test]
fn native_transport_sends_observed_linked_cwd_and_unchanged_prompt() {
    use std::sync::{Arc, Mutex};
    let r = runtime::Runtime::new("air", false);
    let linked = r.temp.path().join("linked");
    assert!(
        Command::new("git")
            .current_dir(&r.repo)
            .args(["worktree", "add", "-q", "-b", "native-linked"])
            .arg(&linked)
            .status()
            .unwrap()
            .success()
    );
    let turn = Arc::new(Mutex::new(Value::Null));
    let saved = turn.clone();
    let cwd = linked.clone();
    let provider = provider::Provider::new(&r.provider_path, move |m| {
        if m.get("id").is_none() {
            return Some(vec![]);
        }
        let p = &m["params"];
        let result = match m["method"].as_str().unwrap() {
            "initialize" => json!({"userAgent":"qualification"}),
            "thread/read" | "thread/resume" => {
                json!({"thread":{"id":"native-thread","cwd":cwd,"status":{"type":"idle"}}})
            }
            "thread/name/set" => json!({}),
            "thread/turns/list" => {
                let t = saved.lock().unwrap();
                json!({"data":if t.is_null(){vec![]}else{vec![t.clone()]},"nextCursor":null})
            }
            "thread/items/list" => {
                let t = saved.lock().unwrap();
                json!({"data":t["items"].as_array().unwrap().iter().map(|i|json!({"turnId":"turn-exact","item":i})).collect::<Vec<_>>(),"nextCursor":null})
            }
            "turn/start" => {
                let t = json!({"id":"turn-exact","status":"completed","items":[{"id":"input-exact","type":"userMessage","clientId":p["clientUserMessageId"],"content":p["input"]},{"id":"result-exact","type":"agentMessage","phase":"final_answer","text":"completed"}]});
                *saved.lock().unwrap() = t.clone();
                json!({"turn":t})
            }
            op => panic!("unexpected Provider operation {op}"),
        };
        Some(vec![json!({"id":m["id"],"result":result})])
    });
    r.launch_authority(true);
    let p=r.call("prepare",json!({"host":"air","project":"fixture","title":"Exact input","prompt":"Unchanged concurrent input","approved":true,"thread_id":"native-thread","workspace_contract":{"cwd":r.repo}}));
    let e = r.call(
        "start",
        json!({"prepared_execution_ref":p["prepared_execution_ref"],"approved":true}),
    );
    r.tick(&[]);
    let calls = provider.calls.lock().unwrap();
    let starts = calls
        .iter()
        .filter(|m| m["method"] == "turn/start")
        .collect::<Vec<_>>();
    assert_eq!(starts.len(), 1, "{calls:?}");
    let p = &starts[0]["params"];
    assert_eq!(p["threadId"], "native-thread");
    assert_eq!(p["cwd"], json!(linked));
    assert_eq!(p["input"][0]["text"], "Unchanged concurrent input");
    assert_eq!(r.status(&e)["workspace_contract"]["cwd"], json!(linked));
}
