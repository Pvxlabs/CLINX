//! Process/source obligations from retired Python observation fixtures.
#[path = "support/node_qualification.rs"]
mod node;
#[path = "support/observation.rs"]
mod observation;
#[path = "support/provider.rs"]
mod provider;
use clinx_decision_kernel::{discovery_identity as identity, node_registry::Registry};
use observation::{Native, TID, snapshot};
use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, BufReader, Write},
    net::TcpListener,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{Arc, Mutex},
    thread,
    time::{Duration, Instant},
};
fn provider_config(a: &Path, b: Option<&Path>) -> Value {
    json!({"app_server":{"local_socket":a,"native_socket":b,"client_name":"collector-fixture","client_title":"Collector fixture","client_version":"1"}})
}
fn replies(
    turn: &str,
    cwd: &str,
    tid: &str,
) -> impl Fn(Value) -> Option<Vec<Value>> + Send + Sync + 'static {
    let (turn, cwd, tid) = (turn.to_owned(), cwd.to_owned(), tid.to_owned());
    move |r| {
        Some(if r["method"] == "initialized" {
            vec![]
        } else {
            let v = match r["method"].as_str().unwrap() {
                "initialize" => json!({"userAgent":"private fixture"}),
                "thread/read" => json!({"thread":{"id":tid,"status":"active","cwd":cwd}}),
                "thread/turns/list" => {
                    json!({"data":[{"id":turn,"status":"inProgress"}],"nextCursor":null})
                }
                _ => panic!("mutation {r}"),
            };
            vec![json!({"id":r["id"],"result":v})]
        })
    }
}
fn init(root: &Path, extra: Value) -> node::Worker {
    let w = node::Worker::plain();
    let mut cfg = json!({"native_home":root,"node_id":"fixture","owner_host":"fixture"});
    cfg.as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    w.call("source_runtime.init", json!({"config":cfg}))
        .unwrap();
    w
}
fn live(w: &node::Worker, project: &str, turn: &str) -> Value {
    w.call(
        "source_runtime.observe_live",
        json!({"tid":TID,"project":project,"turn_id":turn}),
    )
    .unwrap()
}
#[test]
fn actual_provider_cache_exact_turn_workspace_and_all_conflicts() {
    let t = tempfile::tempdir().unwrap();
    let path = t.path().join("private.sock");
    let p = provider::Provider::new(&path, replies("exact-turn", "/approved", TID));
    let w = init(
        t.path(),
        json!({"provider_config":provider_config(&path,None)}),
    );
    let first = live(&w, "/approved", "exact-turn");
    assert_eq!(first["liveness"], "LIVE");
    assert_eq!(first["reason"], "EXACT_OWNER_ACTIVE");
    assert_eq!(live(&w, "/approved", "exact-turn"), first);
    assert_eq!(
        p.calls()
            .iter()
            .map(|c| c["method"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec![
            "initialize",
            "initialized",
            "thread/read",
            "thread/turns/list"
        ]
    );
    drop(p);
    fs::remove_file(&path).unwrap();
    let p = provider::Provider::new(&path, replies("exact-turn", "/approved", TID));
    let changed = live(&w, "/different", "exact-turn");
    assert_eq!(changed["liveness"], "UNKNOWN");
    assert_eq!(changed["reason"], "WORKSPACE_MISMATCH");
    assert_eq!(p.calls().len(), 4);
    drop(w);
    drop(p);
    for (variant, reason) in [
        ("turn", "ACTIVE_OWNER_EXACT_TURN_UNCONFIRMED"),
        ("thread", "OWNER_UNCONFIRMED"),
        ("conflict", "OWNERSHIP_CONFLICT"),
    ] {
        let t = tempfile::tempdir().unwrap();
        let a = provider::Provider::new(
            &t.path().join("a.sock"),
            replies(
                if variant == "turn" {
                    "other"
                } else {
                    "exact-turn"
                },
                "/approved",
                if variant == "thread" { "other" } else { TID },
            ),
        );
        let b = (variant == "conflict").then(|| {
            provider::Provider::new(
                &t.path().join("b.sock"),
                replies("exact-turn", "/approved", TID),
            )
        });
        let w = init(
            t.path(),
            json!({"provider_config":provider_config(&a.path,b.as_ref().map(|p|p.path.as_path()))}),
        );
        let proof = live(&w, "/approved", "exact-turn");
        assert_eq!(proof["liveness"], "UNKNOWN");
        assert_eq!(proof["reason"], reason);
    }
}
#[test]
fn normal_sample_reads_native_history_without_source_callbacks_or_writes() {
    let native = Native::new();
    let before = snapshot(&native.root);
    let w = init(&native.root, json!({"native_writer_observation":true}));
    let result = w
        .call(
            "source_runtime.sample",
            json!({"metadata":native.metadata,"state":{},"canonical_db":false,"live_enabled":true}),
        )
        .unwrap();
    let events = result[0].as_array().unwrap();
    assert!(!events.is_empty());
    assert!(
        events
            .iter()
            .all(|e| e["native_thread_id"] == TID && e["binding_evidence"] == "THREAD_UNBOUND")
    );
    assert_eq!(snapshot(&native.root), before);
}
#[test]
fn injected_provider_io_retains_exact_callback_arguments_and_identity_cache() {
    let t = tempfile::tempdir().unwrap();
    let calls = Arc::new(Mutex::new(vec![]));
    let c = calls.clone();
    let w = node::Worker::new(move |e| {
        assert_eq!(e["operation"], "source_runtime.observe_thread");
        assert_eq!(e["arguments"], json!({"tid":TID}));
        c.lock().unwrap().push(e);
        Ok(
            json!({"observations":[{"endpoint":"fixture","state":"active","thread_id":TID,"turn_id":"exact-turn","turn_status":"inProgress","cwd":"/approved"}]}),
        )
    });
    w.call("source_runtime.init",json!({"config":{"native_home":t.path(),"node_id":"fixture","owner_host":"fixture","injected_provider":true}})).unwrap();
    let first = live(&w, "/approved", "exact-turn");
    assert_eq!(first["liveness"], "LIVE");
    assert_eq!(live(&w, "/approved", "exact-turn"), first);
    assert_eq!(calls.lock().unwrap().len(), 1);
    assert_eq!(live(&w, "/approved", "different")["liveness"], "UNKNOWN");
    assert_eq!(calls.lock().unwrap().len(), 2);
}
#[test]
fn managed_inventory_uses_native_http_private_credential_and_scope() {
    let t = tempfile::tempdir().unwrap();
    let token = format!("private_collector_{}", "x".repeat(32));
    let credential = t.path().join("token");
    fs::write(&credential, &token).unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let job = thread::spawn(move || {
        let (mut peer, _) = listener.accept().unwrap();
        peer.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
        let mut reader = BufReader::new(peer.try_clone().unwrap());
        let mut lines = vec![];
        loop {
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            if line == "\r\n" {
                break;
            }
            lines.push(line)
        }
        let body=json!({"read_only":true,"rows":[{"id":TID,"cwd":"/approved","history_mode":"paginated"}],"after":null,"coverage":"INDEXED"}).to_string();
        write!(
            peer,
            "HTTP/1.0 200 OK\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        )
        .unwrap();
        lines
    });
    let w = node::Worker::plain();
    w.call("source_runtime.init",json!({"config":{"native_home":null,"node_id":"fixture","owner_host":"fixture","managed":true,"remote_native":{"credential_file":credential,"port":port}}})).unwrap();
    let r=w.call("source_runtime.inventory",json!({"scope":{"user_scope":"fixture","read_sessions":true,"projects":["/approved"]},"after":null,"audit":false})).unwrap();
    assert_eq!(r[0][0]["id"], TID);
    assert_eq!(r[2], "INDEXED");
    let lines = job.join().unwrap();
    assert!(lines[0].starts_with("GET /v2/native-collector/inventory?"));
    assert!(
        lines
            .iter()
            .any(|s| s.trim() == format!("Authorization: Bearer {token}"))
    );
}
#[test]
fn native_writer_rejects_foreign_home_and_injected_io_always_uses_native_program() {
    let t = tempfile::tempdir().unwrap();
    let output=Command::new(env!("CARGO_BIN_EXE_clinx-node")).arg("--native-writer-probe").arg(json!({"root":t.path().join("foreign"),"thread_id":TID,"turn_id":"exact","project":"/approved"}).to_string()).output().unwrap();
    assert!(output.status.success());
    let proof: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(proof["liveness"], "UNKNOWN");
    assert!(proof.get("writer_pid").is_none());
    assert_eq!(proof["observer_uid"], rustix::process::getuid().as_raw());
    let calls = Arc::new(Mutex::new(vec![]));
    let c = calls.clone();
    let w = node::Worker::new(move |e| {
        c.lock().unwrap().push(e.clone());
        Ok(match e["operation"].as_str().unwrap() {
            "writer_probe.worker_uid" | "writer_probe.open" => json!(12345),
            "writer_probe.initialize" => Value::Null,
            "writer_probe.request" => {
                let a = &e["arguments"];
                let argv = a["command"].as_array().unwrap();
                assert_eq!(argv.len(), 3);
                assert_eq!(argv[0], env!("CARGO_BIN_EXE_clinx-node"));
                assert_eq!(argv[1], "--native-writer-probe");
                let request: Value = serde_json::from_str(argv[2].as_str().unwrap()).unwrap();
                assert_eq!(request["thread_id"], TID);
                assert_eq!(request["turn_id"], "exact");
                assert_eq!(request["project"], "/approved");
                json!({"exitCode":0,"stdout":json!({"observer_uid":12345,"liveness":"UNKNOWN","reason":"fixture"}).to_string()})
            }
            _ => panic!("{e}"),
        })
    });
    w.call("source_runtime.init",json!({"config":{"native_home":t.path().join("foreign"),"injected_probe":true,"injected_home":t.path(),"native_observer_socket":"/fixture","probe_program":env!("CARGO_BIN_EXE_clinx-node"),"injected_legacy_script":"/must-not-execute.py"}})).unwrap();
    let r = w
        .call(
            "source_runtime.writer_probe",
            json!({"tid":TID,"turn_id":"exact","project":"/approved"}),
        )
        .unwrap();
    assert_eq!(r, json!({"liveness":"UNKNOWN","reason":"fixture"}));
    assert_eq!(calls.lock().unwrap().len(), 4);
}

struct Local {
    root: tempfile::TempDir,
    native: Native,
    state: PathBuf,
    registry: PathBuf,
    db: PathBuf,
}
fn grant(state: &Path, node: &str, revoked: bool) {
    identity::write(state,"sharing.json",&json!({"schema_version":1,"scopes":{node:{"user_scope":"alice","read_sessions":!revoked,"execute_tasks":false,"providers":["codex_app_server"],"projects":["/approved"],"revoked":revoked,"updated_at":"2026-01-01T00:00:00+00:00"}}})).unwrap();
}
impl Local {
    fn new() -> Self {
        let root = tempfile::tempdir().unwrap();
        let native = Native::new();
        let state = root.path().join("device");
        let public = identity::open(&state, "air", None).unwrap();
        grant(&state, "air", false);
        let registry = root.path().join("registry.db");
        let r = Registry::default();
        r.invoke("registry.init", &json!({"path":registry}))
            .unwrap();
        let mut record = observation::record("air");
        record["public_key_fingerprint"] = public["fingerprint"].clone();
        r.invoke(
            "registry.register",
            &json!({"record":record,"scope":observation::scope()}),
        )
        .unwrap();
        let db = root.path().join("observations.db");
        clinx_decision_kernel::observation::init(&db).unwrap();
        let f = Self {
            root,
            native,
            state,
            registry,
            db,
        };
        f.accept(json!([
            observation::event(1, TID, "turn-1", 1),
            observation::event(2, TID, "turn-2", 2)
        ]));
        fs::remove_file(f.state.join("store.lock")).unwrap();
        f
    }
    fn accept(&self, events: Value) {
        clinx_decision_kernel::observation::accept(&json!({"path":self.db,"node":"air","scope":observation::scope(),"request":{"stream_id":"fixture","events":events}})).unwrap();
    }
    fn command(&self) -> Command {
        let mut c = Command::new(env!("CARGO_BIN_EXE_clinx-mcp"));
        c.arg("--stdio")
            .env_clear()
            .env("HOME", self.root.path())
            .env("LANG", "C.UTF-8")
            .env("PATH", "/no-python-may-be-started")
            .env("CLINX_OBSERVATION_STATE", &self.state)
            .env("CLINX_OBSERVATION_REGISTRY", &self.registry)
            .env("CLINX_OBSERVATION_DB", &self.db)
            .env("CLINX_OBSERVATION_NATIVE_HOME", &self.native.root)
            .env("CLINX_OBSERVATION_NODE_ID", "air")
            .env("CLINX_OBSERVATION_USER_SCOPE", "alice");
        c
    }
    fn mcp(&self) -> Mcp {
        let mut c = self.command();
        c.arg("--observations-only");
        Mcp::new(c, self.root.path().join("mcp.log"))
    }
}
struct Mcp {
    child: std::process::Child,
    input: Option<std::process::ChildStdin>,
    output: BufReader<std::process::ChildStdout>,
    seq: u64,
    log: PathBuf,
}
impl Mcp {
    fn new(mut c: Command, log: PathBuf) -> Self {
        let mut child = c
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(fs::File::create(&log).unwrap())
            .spawn()
            .unwrap();
        let input = child.stdin.take();
        let output = BufReader::new(child.stdout.take().unwrap());
        Self {
            child,
            input,
            output,
            seq: 0,
            log,
        }
    }
    fn request(&mut self, method: &str, params: Value) -> Value {
        self.seq += 1;
        let i = self.input.as_mut().unwrap();
        writeln!(
            i,
            "{}",
            json!({"jsonrpc":"2.0","id":self.seq,"method":method,"params":params})
        )
        .unwrap();
        i.flush().unwrap();
        assert!(
            rustix::event::poll(
                &mut [rustix::event::PollFd::new(
                    self.output.get_ref(),
                    rustix::event::PollFlags::IN
                )],
                Some(&rustix::event::Timespec {
                    tv_sec: 10,
                    tv_nsec: 0
                })
            )
            .unwrap()
                > 0,
            "MCP deadline"
        );
        let mut line = String::new();
        self.output.read_line(&mut line).unwrap();
        let r: Value = serde_json::from_str(&line)
            .unwrap_or_else(|e| panic!("{e}: {line}; {}", fs::read_to_string(&self.log).unwrap()));
        assert_eq!(
            r["id"],
            if r.get("error").is_some() {
                Value::Null
            } else {
                json!(self.seq)
            }
        );
        r
    }
    fn call(&mut self, name: &str, args: Value) -> Value {
        let r = self.request("tools/call", json!({"name":name,"arguments":args}));
        assert!(r.get("error").is_none(), "{r}");
        r["result"].clone()
    }
    fn no_children(&self) {
        #[cfg(target_os = "linux")]
        assert!(
            fs::read_to_string(format!(
                "/proc/{}/task/{}/children",
                self.child.id(),
                self.child.id()
            ))
            .unwrap()
            .trim()
            .is_empty()
        );
    }
}
impl Drop for Mcp {
    fn drop(&mut self) {
        self.input.take();
        let until = Instant::now() + Duration::from_secs(10);
        while self.child.try_wait().unwrap().is_none() {
            if Instant::now() >= until {
                let _ = self.child.kill();
                break;
            }
            thread::sleep(Duration::from_millis(5));
        }
        let status = self.child.wait().unwrap();
        if !thread::panicking() {
            assert!(
                status.success(),
                "{}",
                fs::read_to_string(&self.log).unwrap()
            );
        }
    }
}
#[test]
fn actual_mcp_four_tools_read_exact_private_history_without_python_or_state_writes() {
    let f = Local::new();
    let before = snapshot(&f.state);
    let mut api = f.mcp();
    assert!(api.request("initialize", json!({}))["result"]["protocolVersion"].is_string());
    let tools = api.request("tools/list", json!({}))["result"]["tools"].clone();
    let registry: Value =
        serde_json::from_str(include_str!("../../clinx/mcp/registry.json")).unwrap();
    assert_eq!(tools, registry["observations"]);
    let names = tools
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v["name"].as_str().unwrap())
        .collect::<std::collections::BTreeSet<_>>();
    assert_eq!(
        names,
        std::collections::BTreeSet::from([
            "clinx_list_observations",
            "clinx_get_observation",
            "clinx_get_observation_context",
            "clinx_get_observation_activity"
        ])
    );
    let listed = api.call("clinx_list_observations", json!({}))["structuredContent"].clone();
    assert_eq!(listed["read_only"], true);
    assert_eq!(listed["items"].as_array().unwrap().len(), 1);
    let id = &listed["items"][0]["observation_id"];
    let detail =
        api.call("clinx_get_observation", json!({"observation_id":id}))["structuredContent"]
            .clone();
    assert_eq!(detail["item"]["native_thread_id"], TID);
    assert_eq!(detail["read_only"], true);
    let context = api.call(
        "clinx_get_observation_context",
        json!({"observation_id":id}),
    )["structuredContent"]
        .clone();
    assert_eq!(context["last_codex_result"], "Answer 2");
    assert_eq!(context["read_only"], true);
    assert!(!context.to_string().contains("NEVER_"));
    let activity = api.call(
        "clinx_get_observation_activity",
        json!({"observation_id":id}),
    )["structuredContent"]
        .clone();
    assert_eq!(activity["read_only"], true);
    assert_eq!(activity["items"].as_array().unwrap().len(), 2);
    assert_eq!(activity["coverage"], "RECEIVED_EVENTS_ONLY");
    assert_eq!(
        api.request(
            "tools/call",
            json!({"name":"clinx_start_execution","arguments":{}})
        )["error"]["code"],
        -32602
    );
    api.no_children();
    drop(api);
    assert_eq!(snapshot(&f.state), before);
}
#[test]
fn actual_mcp_rechecks_revocation_and_cursor_binding_without_restart() {
    let f = Local::new();
    let mut api = f.mcp();
    let listed = api.call("clinx_list_observations", json!({}))["structuredContent"].clone();
    let id = &listed["items"][0]["observation_id"];
    let detail = api.call(
        "clinx_get_observation",
        json!({"observation_id":id,"limit":1}),
    )["structuredContent"]
        .clone();
    assert!(detail["next_cursor"].is_string());
    grant(&f.state, "air", true);
    let result = api.call(
        "clinx_get_observation",
        json!({"observation_id":id,"cursor":detail["next_cursor"]}),
    );
    assert_eq!(result["isError"], true);
    assert_eq!(result["structuredContent"]["read_only"], true);
    assert_eq!(
        api.call("clinx_list_observations", json!({}))["structuredContent"]["items"],
        json!([])
    );
}
#[test]
fn actual_mcp_restart_preserves_exact_cursor_and_missing_state_never_bootstraps() {
    let f = Local::new();
    let (id, page) = {
        let mut api = f.mcp();
        let id=api.call("clinx_list_observations",json!({}))["structuredContent"]["items"][0]["observation_id"].clone();
        let page = api.call(
            "clinx_get_observation",
            json!({"observation_id":id,"limit":1}),
        )["structuredContent"]
            .clone();
        (id, page)
    };
    {
        let mut api = f.mcp();
        let older = api.call(
            "clinx_get_observation",
            json!({"observation_id":id,"limit":1,"cursor":page["next_cursor"]}),
        )["structuredContent"]
            .clone();
        assert_ne!(older["turns"][0]["turn_id"], page["turns"][0]["turn_id"]);
    }
    let missing = f.root.path().join("not-created");
    let out = f
        .command()
        .arg("--observations-only")
        .env("CLINX_OBSERVATION_STATE", &missing)
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert!(!out.status.success());
    assert!(!missing.exists());
}

fn accept_at(path: &Path, events: Value) {
    clinx_decision_kernel::observation::accept(&json!({"path":path,"node":"air","scope":observation::scope(),"request":{"stream_id":"fixture","events":events}})).unwrap();
}
fn managed_event(seq: u64, turn: &str, ordinal: u64, execution: &str) -> Value {
    let mut e = observation::event(seq, TID, turn, ordinal);
    e["task_ref"] = json!("task-exact");
    e["binding_evidence"] = json!("CANONICAL_ROUTE");
    e["turn"]["execution_ref"] = json!(execution);
    e
}
struct ReadAuthority {
    path: PathBuf,
    calls: Arc<Mutex<Vec<Value>>>,
    stop: Arc<std::sync::atomic::AtomicBool>,
    job: Option<thread::JoinHandle<()>>,
}
impl ReadAuthority {
    fn new(path: PathBuf, hook: impl Fn() + Send + 'static) -> Self {
        use std::os::unix::{fs::PermissionsExt, net::UnixListener};
        let listener = UnixListener::bind(&path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        listener.set_nonblocking(true).unwrap();
        let calls = Arc::new(Mutex::new(vec![]));
        let stop = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let (c, s) = (calls.clone(), stop.clone());
        let job = thread::spawn(move || {
            while !s.load(std::sync::atomic::Ordering::Relaxed) {
                let (mut peer, _) = match listener.accept() {
                    Ok(p) => p,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2));
                        continue;
                    }
                    Err(e) => panic!("{e}"),
                };
                peer.set_read_timeout(Some(Duration::from_secs(4))).unwrap();
                let mut line = String::new();
                BufReader::new(peer.try_clone().unwrap())
                    .read_line(&mut line)
                    .unwrap();
                let request: Value = serde_json::from_str(&line).unwrap();
                c.lock().unwrap().push(request.clone());
                let result = match request["operation"].as_str().unwrap() {
                    "health" => json!({"business_commands":true}),
                    "business.get_status" => {
                        hook();
                        json!({"task_ref":"task-exact","execution_ref":"exec-exact","thread_id":TID,"native_turn_id":"turn-2","status":"SUCCEEDED"})
                    }
                    "business.list_tasks" => {
                        json!({"tasks":[{"task_id":"task-exact","thread_id":TID,"host":"air","project":"/approved"}]})
                    }
                    _ => panic!("Unexpected authority mutation {request}"),
                };
                writeln!(peer,"{}",json!({"protocol_version":request["protocol_version"],"request_id":request["request_id"],"result":result})).unwrap();
            }
        });
        Self {
            path,
            calls,
            stop,
            job: Some(job),
        }
    }
}
impl Drop for ReadAuthority {
    fn drop(&mut self) {
        self.stop.store(true, std::sync::atomic::Ordering::Relaxed);
        self.job.take().unwrap().join().unwrap();
    }
}
#[test]
fn actual_normal_mcp_managed_context_rechecks_all_four_original_identity_cases() {
    for change in ["none", "scope", "execution", "identity"] {
        let f = Local::new();
        f.accept(json!([managed_event(3, "turn-2", 2, "exec-exact")]));
        let replacement = identity::open(&f.root.path().join("replacement"), "air", None).unwrap();
        let state = f.state.clone();
        let db = f.db.clone();
        let authority = ReadAuthority::new(f.root.path().join("a.sock"), move || match change {
            "scope" => grant(&state, "air", true),
            "execution" => accept_at(&db, json!([managed_event(4, "turn-new", 3, "exec-new")])),
            "identity" => identity::write(&state, "identity.json", &replacement).unwrap(),
            _ => (),
        });
        let p = provider::Provider::new(&f.root.path().join("p.sock"), |r| {
            Some(if r["method"] == "initialized" {
                vec![]
            } else {
                let v = match r["method"].as_str().unwrap() {
                    "initialize" => json!({"userAgent":"fixture"}),
                    "thread/read" => {
                        json!({"thread":{"id":TID,"cwd":"/approved","status":{"type":"idle"}}})
                    }
                    "thread/turns/list" => {
                        json!({"data":[{"id":"turn-2","status":"completed","items":[{"id":"input","type":"userMessage","content":[{"type":"text","text":"Exact input"}]},{"id":"answer","type":"agentMessage","phase":"final_answer","text":"Exact provider result"}]}],"nextCursor":null})
                    }
                    _ => panic!("Provider mutation {r}"),
                };
                vec![json!({"id":r["id"],"result":v})]
            })
        });
        let config = f.root.path().join("mcp.json");
        fs::write(&config,json!({"runtime_host":"air","provider_endpoint":p.path,"provider_timeout_seconds":2,"host_aliases":{"air":["air"]},"codex_host_ids":{}}).to_string()).unwrap();
        let before = snapshot(&f.state);
        let mut command = f.command();
        command
            .arg("--config")
            .arg(&config)
            .env("CLINX_RUST_SOCKET", &authority.path)
            .env(
                "CLINX_RUST_UID",
                rustix::process::getuid().as_raw().to_string(),
            )
            .env("CLINX_RUST_OBSERVATION_READS", "1")
            .env("CLINX_MCP_BINARY", "/must-not-recurse")
            .env("CLINX_OBSERVER_PORT", "0");
        {
            let mut api = Mcp::new(command, f.root.path().join("mcp.log"));
            let tools = api.request("tools/list", json!({}))["result"]["tools"].clone();
            for name in ["clinx_get_context", "clinx_get_observation_context"] {
                assert!(tools.as_array().unwrap().iter().any(|v| v["name"] == name));
            }
            let listed =
                api.call("clinx_list_observations", json!({}))["structuredContent"].clone();
            let id = &listed["items"][0]["observation_id"];
            assert_eq!(
                api.call("clinx_get_observation", json!({"observation_id":id}))["structuredContent"]
                    ["item"]["task_ref"],
                "task-exact"
            );
            assert_eq!(
                api.call(
                    "clinx_get_observation_activity",
                    json!({"observation_id":id})
                )["structuredContent"]["read_only"],
                true
            );
            let result = api.call(
                "clinx_get_observation_context",
                json!({"observation_id":id}),
            );
            if change == "none" {
                let c = &result["structuredContent"];
                assert_eq!(result["isError"], false);
                assert_eq!(c["last_codex_result"], "Exact provider result");
                assert_eq!(
                    json!([
                        c["task_ref"],
                        c["execution_ref"],
                        c["thread_id"],
                        c["execution_turn_ref"],
                        c["context_scope"]
                    ]),
                    json!([
                        "task-exact",
                        "exec-exact",
                        TID,
                        "turn-2",
                        "EXACT_EXECUTION_TURN"
                    ])
                );
                assert_eq!(c["read_only"], true);
                assert_eq!(snapshot(&f.state), before);
            } else {
                assert_eq!(result["isError"], true);
                assert!(
                    [
                        "OBSERVATION_NOT_FOUND",
                        "EXECUTION_TURN_IDENTITY_CONFLICT",
                        "NODE_IDENTITY_MISMATCH"
                    ]
                    .contains(&result["structuredContent"]["error_code"].as_str().unwrap())
                );
                if change == "identity" {
                    assert_eq!(
                        result["structuredContent"]["error_code"],
                        "NODE_IDENTITY_MISMATCH"
                    );
                }
            }
            api.no_children();
        }
        assert_eq!(
            authority
                .calls
                .lock()
                .unwrap()
                .iter()
                .map(|r| r["operation"].as_str().unwrap())
                .collect::<Vec<_>>(),
            vec![
                "health",
                "health",
                "business.get_status",
                "business.list_tasks"
            ]
        );
        assert!(p.calls().iter().all(|r| {
            [
                "initialize",
                "initialized",
                "thread/read",
                "thread/turns/list",
            ]
            .contains(&r["method"].as_str().unwrap())
        }));
    }
}
#[test]
fn actual_mcp_paired_context_rechecks_all_five_original_scope_identity_cases() {
    for change in ["none", "scope", "projection", "peer_revoked", "identity"] {
        let mut f = Local::new();
        let centre = f.root.path().join("centre");
        let centre_identity = identity::open(&centre, "centre", None).unwrap();
        let air_identity = identity::read(&f.state, "identity.json").unwrap().unwrap();
        let runtime = clinx_decision_kernel::discovery_state::Runtime::default();
        for (root, peer) in [(&centre, &air_identity), (&f.state, &centre_identity)] {
            runtime.invoke("discovery.peers.trust",&json!({"root":root,"public":identity::public_record(peer).unwrap(),"certificate":peer["certificate"],"security_status":"OPAQUE_V1","pairing_protocol":"OPAQUE-3DH-RISTRETTO255-SHA512-ARGON2I-v1","fresh_pairing":true}),|_,_|panic!()).unwrap();
        }
        grant(&centre, "air", false);
        let replacement = identity::open(&f.root.path().join("replacement"), "air", None).unwrap();
        let requests = Arc::new(Mutex::new(vec![]));
        let (seen, state, db) = (requests.clone(), centre.clone(), f.db.clone());
        let server = node::Worker::new(move |e| {
            assert_eq!(e["kind"], "incoming");
            seen.lock().unwrap().push(e["request"].clone());
            match change {
                "scope" => grant(&state, "air", true),
                "projection" => accept_at(&db, json!([observation::event(3, TID, "new-turn", 3)])),
                "peer_revoked" | "identity" => {
                    let mut peers = identity::read(&state, "peers.json").unwrap().unwrap();
                    if change == "peer_revoked" {
                        peers["peers"]["air"]["trust_state"] = json!("REVOKED");
                    } else {
                        let mut public = identity::public_record(&replacement).unwrap();
                        public["certificate"] = replacement["certificate"].clone();
                        peers["peers"]["air"]
                            .as_object_mut()
                            .unwrap()
                            .extend(public.as_object().unwrap().clone());
                    }
                    identity::write(&state, "peers.json", &peers).unwrap();
                }
                _ => (),
            }
            Ok(
                json!({"queried_thread_id":TID,"lookup_status":"THREAD_UNBOUND","provider_existence":"CONFIRMED","last_codex_result":"remote exact result","read_only":true}),
            )
        });
        let address = server
            .call(
                "listen",
                json!({"state":f.state,"address":"127.0.0.1","port":0}),
            )
            .unwrap()["address"]
            .clone();
        let r = Registry::default();
        r.invoke("registry.init", &json!({"path":f.registry}))
            .unwrap();
        let mut row = observation::record("air");
        row["public_key_fingerprint"] = air_identity["fingerprint"].clone();
        row["endpoint"] = json!(format!("tls://127.0.0.1:{}", address[1]));
        r.invoke(
            "registry.register",
            &json!({"record":row,"scope":observation::scope()}),
        )
        .unwrap();
        f.state = centre;
        fs::remove_file(f.state.join("store.lock")).unwrap();
        let before = snapshot(&f.state);
        let mut cmd = f.command();
        cmd.arg("--observations-only")
            .env("CLINX_OBSERVATION_NODE_ID", "centre")
            .env(
                "CLINX_OBSERVATION_NATIVE_HOME",
                f.root.path().join("no-native-history"),
            );
        {
            let mut api = Mcp::new(cmd, f.root.path().join("mcp.log"));
            let id = api.call("clinx_list_observations", json!({}))["structuredContent"]["items"]
                [0]["observation_id"]
                .clone();
            let result = api.call(
                "clinx_get_observation_context",
                json!({"observation_id":id}),
            );
            if change == "none" {
                assert_eq!(result["isError"], false);
                assert_eq!(
                    result["structuredContent"]["last_codex_result"],
                    "remote exact result"
                );
            } else {
                assert_eq!(result["isError"], true);
                assert!(
                    [
                        "OBSERVATION_NOT_FOUND",
                        "SHARING_SCOPE_DENIED",
                        "EXECUTION_TURN_IDENTITY_CONFLICT",
                        "NODE_IDENTITY_MISMATCH"
                    ]
                    .contains(&result["structuredContent"]["error_code"].as_str().unwrap())
                );
                if change == "identity" {
                    assert_eq!(
                        result["structuredContent"]["error_code"],
                        "NODE_IDENTITY_MISMATCH"
                    );
                }
            }
        }
        let requests = requests.lock().unwrap();
        assert_eq!(requests.len(), 1);
        assert_eq!(requests[0]["node_id"], "air");
        assert_eq!(requests[0]["user_scope"], "alice");
        if change == "none" {
            assert_eq!(snapshot(&f.state), before);
        }
    }
}
