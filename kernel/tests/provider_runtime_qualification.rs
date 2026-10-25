//! Real Git, Provider Unix WebSocket, Authority and paired TLS qualifications.
#[path = "support/provider.rs"]
mod support;
use clinx_decision_kernel::{
    authority_client, discovery_enrollment, discovery_identity as identity, discovery_state,
    node_centre, node_gateway, node_owner, node_paired, node_provider, node_registry::Registry,
    node_relay::Relay, node_scope, node_tls,
};
use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, BufReader, Write},
    net::TcpListener,
    os::unix::fs::{MetadataExt, PermissionsExt},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::Duration,
};
use support::Provider;
fn binary() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_clinx-provider"))
}
fn command(program: &Path) -> Command {
    let mut c = Command::new(program);
    c.env_clear().env("PATH", std::env::var_os("PATH").unwrap());
    c
}
fn output(c: &mut Command) -> Value {
    let r = c.output().unwrap();
    assert!(r.status.success(), "{}", String::from_utf8_lossy(&r.stderr));
    serde_json::from_slice(&r.stdout).unwrap()
}
fn git(repo: &Path, args: &[&str]) {
    let r = command(Path::new("git"))
        .arg("-C")
        .arg(repo)
        .args(args)
        .output()
        .unwrap();
    assert!(r.status.success(), "{}", String::from_utf8_lossy(&r.stderr));
}
fn repo(root: &Path) -> PathBuf {
    let p = root.join("repo");
    fs::create_dir(&p).unwrap();
    git(&p, &["init", "-q", "--initial-branch=main"]);
    git(
        &p,
        &[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        ],
    );
    p
}
fn inspect(request: Value) -> Value {
    output(
        command(&binary())
            .args(["--inspect", &request.to_string()])
            .env("GIT_DIR", "/missing/caller-git-dir")
            .env("GIT_WORK_TREE", "/missing/caller-worktree"),
    )
}
#[test]
fn metadata_matches_git_and_shares_identity_across_linked_worktrees() {
    let tmp = tempfile::tempdir().unwrap();
    let p = repo(tmp.path());
    let linked = tmp.path().join("linked");
    git(
        &p,
        &[
            "worktree",
            "add",
            "-q",
            "-b",
            "linked",
            linked.to_str().unwrap(),
        ],
    );
    let a = inspect(json!({"mode":"workspace","cwd":p}));
    let b = inspect(json!({"mode":"workspace","cwd":linked}));
    assert_eq!(a["repository_identity"], b["repository_identity"]);
    let m = fs::metadata(p.join(".git")).unwrap();
    assert_eq!(
        a["repository_identity"],
        format!("git-common:{}:{}", m.dev(), m.ino())
    );
    assert_eq!(a["branch"], "main");
    assert_eq!(b["branch"], "linked");
    assert_eq!(a["observer_uid"], rustix::process::getuid().as_raw());
    assert_eq!(b["repository_common_dir"], json!(p.join(".git")));
    assert_ne!(a["worktree"], b["worktree"]);
    assert_eq!(a["head"], b["head"]);
    assert_eq!(
        inspect(json!({"mode":"repository","cwd":p}))["repository_identity"],
        a["repository_identity"]
    );
    assert_eq!(
        inspect(json!({"mode":"git","cwd":linked,"args":["branch","--show-current"]}))["value"],
        "linked"
    );
}
#[test]
fn metadata_rejects_mutation_and_invalid_cwd_before_git() {
    let t = tempfile::tempdir().unwrap();
    let p = repo(t.path());
    for (case, error) in [
        (
            json!({"mode":"workspace","cwd":"relative"}),
            "WORKSPACE_CONTRACT_INVALID",
        ),
        (
            json!({"mode":"workspace","cwd":"/missing-fixture-cwd"}),
            "WORKSPACE_CWD_UNREADABLE",
        ),
        (
            json!({"mode":"workspace","cwd":"/bad\0cwd"}),
            "WORKSPACE_CONTRACT_INVALID",
        ),
        (json!({"mode":"unexpected"}), "WORKSPACE_PROBE_INVALID"),
        (
            json!({"mode":"git","args":["reset","--hard"]}),
            "WORKSPACE_PROBE_INVALID",
        ),
        (
            json!({"mode":"git","args":["config","user.email","mutation"]}),
            "WORKSPACE_PROBE_INVALID",
        ),
        (
            json!({"mode":"git","args":["rev-parse",null]}),
            "WORKSPACE_PROBE_INVALID",
        ),
    ] {
        let mut r = json!({"cwd":p});
        r.as_object_mut()
            .unwrap()
            .extend(case.as_object().unwrap().clone());
        assert_eq!(inspect(r), json!({"error":error}));
    }
}
#[derive(Default)]
struct Metadata {
    uid: u32,
    over: Option<String>,
    calls: Vec<Value>,
}
fn metadata(path: &Path) -> (Provider, Arc<Mutex<Metadata>>) {
    let state = Arc::new(Mutex::new(Metadata {
        uid: rustix::process::getuid().as_raw(),
        ..Default::default()
    }));
    let s = state.clone();
    let provider = Provider::new(path, move |r| {
        match r["method"].as_str().unwrap() {
            "initialized" => return Some(vec![]),
            "initialize" => return Some(vec![json!({"id":r["id"],"result":{}})]),
            "command/exec" => (),
            _ => panic!("unexpected Provider mutation: {r}"),
        };
        let p = &r["params"];
        assert_eq!(p["command"][0], json!(binary()));
        assert_eq!(p["command"][1], "--inspect");
        assert_eq!(p["cwd"], "/");
        assert_eq!(p["timeoutMs"], 2500);
        assert_eq!(p["outputBytesCap"], 65536);
        assert_eq!(p["sandboxPolicy"], json!({"type":"dangerFullAccess"}));
        let mut c = command(&binary());
        for a in p["command"].as_array().unwrap().iter().skip(1) {
            c.arg(a.as_str().unwrap());
        }
        let mut data = output(&mut c);
        let mut s = s.lock().unwrap();
        s.calls.push(p.clone());
        data["observer_uid"] = json!(s.uid);
        let stdout = s.over.clone().unwrap_or_else(|| data.to_string());
        Some(vec![
            json!({"id":"unsolicited","method":"item/tool/requestUserInput","params":{}}),
            json!({"id":r["id"],"result":{"exitCode":0,"stdout":stdout}}),
        ])
    });
    (provider, state)
}
fn observe(endpoint: &Path, r: &Value, uid: u32) -> Value {
    output(command(&binary()).args([
        "--observe",
        endpoint.to_str().unwrap(),
        &r.to_string(),
        "--qualification-provider-uid",
        &uid.to_string(),
    ]))
}
#[test]
fn local_observer_and_node_exchange_use_rust_probe_with_exact_provider_uid() {
    let tmp = tempfile::tempdir().unwrap();
    let p = repo(tmp.path());
    let (provider, state) = metadata(&tmp.path().join("p.sock"));
    let uid = rustix::process::getuid().as_raw();
    let request = json!({"host":"air","mode":"workspace","cwd":p});
    let result = observe(&provider.path, &request, uid);
    assert_eq!(result["observer_source"], "CODEX_PROVIDER_COMMAND_EXEC");
    assert_eq!(result["cwd"], json!(p));
    assert_eq!(result["observer_uid"], uid);
    let actual = node_provider::exchange(
        &provider.path,
        &binary(),
        &request,
        &json!({"method":"workspace/inspect","params":request}),
        Duration::from_secs(2),
    )
    .unwrap();
    assert_eq!(actual, result);
    state.lock().unwrap().uid = uid + 1;
    assert_eq!(
        observe(&provider.path, &request, uid),
        json!({"error":"WORKSPACE_OBSERVER_IDENTITY_CONFLICT"})
    );
    let count = state.lock().unwrap().calls.len();
    let wire_count = provider.calls().len();
    assert_eq!(
        observe(&provider.path, &request, uid + 1),
        json!({"error":"WORKSPACE_OBSERVER_IDENTITY_CONFLICT"})
    );
    assert_eq!(state.lock().unwrap().calls.len(), count);
    assert_eq!(provider.calls().len(), wire_count);
    state.lock().unwrap().uid = uid;
    for (stdout, error) in [
        ("{}".to_owned(), "WORKSPACE_OBSERVER_IDENTITY_CONFLICT"),
        (
            "{\"error\":\"WORKSPACE_CWD_UNREADABLE\"}".into(),
            "WORKSPACE_CWD_UNREADABLE",
        ),
        (
            "{\"error\":\"UNTRUSTED_ERROR\"}".into(),
            "WORKSPACE_OBSERVER_INVALID",
        ),
        ("x".repeat(65537), "WORKSPACE_OBSERVER_UNAVAILABLE"),
    ] {
        state.lock().unwrap().over = Some(stdout);
        assert_eq!(
            observe(&provider.path, &request, uid),
            json!({"error":error})
        );
    }
}
struct Process(Child);
impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
#[test]
fn authority_workspace_contract_uses_rust_provider_without_python_probe() {
    let tmp = tempfile::tempdir().unwrap();
    let p = repo(tmp.path());
    let (provider, state) = metadata(&tmp.path().join("p.sock"));
    let root = tmp.path().join("state");
    fs::create_dir(&root).unwrap();
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    let endpoint = tmp.path().join("a.sock");
    let policy = tmp.path().join("policy.json");
    fs::write(&policy,json!({"projects":[{"alias":"fixture","host":"air","cwd":p}],"endpoints":[provider.path],"lease_ms":120000}).to_string()).unwrap();
    let uid = rustix::process::getuid().as_raw();
    let mut process = Process(
        command(Path::new(env!("CARGO_BIN_EXE_clinx-runtime")))
            .args([
                "--qualification",
                "--init",
                "--qualification-workspace-observer",
            ])
            .arg(binary())
            .arg("--state-dir")
            .arg(&root)
            .arg("--socket")
            .arg(&endpoint)
            .arg("--policy")
            .arg(policy)
            .args([
                "--caller-uid",
                &uid.to_string(),
                "--transport-uid",
                &uid.to_string(),
            ])
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap(),
    );
    for _ in 0..200 {
        if endpoint.exists() {
            break;
        }
        assert!(process.0.try_wait().unwrap().is_none());
        thread::sleep(Duration::from_millis(10));
    }
    assert!(endpoint.exists());
    let call = |op: &str, payload: Value, id: &str| {
        authority_client::request(&json!({"endpoint":endpoint,"authority_uid":uid,"timeout":10,"wire_operation":op,"payload":payload,"request_id":id})).unwrap()
    };
    let prepared = call(
        "business.prepare",
        json!({"host":"air","project":"fixture","title":"Fixture","prompt":"fixture","approved":true}),
        "prepare",
    );
    assert_eq!(
        prepared["workspace_resolution"]["observer_source"],
        "CODEX_PROVIDER_COMMAND_EXEC"
    );
    assert_eq!(prepared["workspace_resolution"]["observer_uid"], uid);
    let request =
        json!({"prepared_execution_ref":prepared["prepared_execution_ref"],"approved":true});
    let started = call("business.start", request.clone(), "start");
    assert_eq!(
        started["workspace_resolution"]["repository_identity"],
        prepared["workspace_resolution"]["repository_identity"]
    );
    assert_eq!(
        call("business.start", request, "duplicate")["execution_ref"],
        started["execution_ref"]
    );
    assert!(!state.lock().unwrap().calls.is_empty());
}
struct TlsServer {
    address: std::net::SocketAddr,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}
impl TlsServer {
    fn new(
        root: PathBuf,
        handle: impl Fn(Value, Vec<u8>, std::net::SocketAddr) -> Result<Value, String>
        + Send
        + Sync
        + 'static,
    ) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        listener.set_nonblocking(true).unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let s = stop.clone();
        let handle = Arc::new(handle);
        let thread = thread::spawn(move || {
            let mut workers = vec![];
            while !s.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((stream, remote)) => {
                        stream
                            .set_read_timeout(Some(Duration::from_secs(5)))
                            .unwrap();
                        stream
                            .set_write_timeout(Some(Duration::from_secs(5)))
                            .unwrap();
                        let (root, h) = (root.clone(), handle.clone());
                        workers.push(thread::spawn(move || {
                            let (mut stream, _, der) = node_tls::connection(stream, &root).unwrap();
                            let mut line = String::new();
                            BufReader::new(&mut stream).read_line(&mut line).unwrap();
                            let request: Value = serde_json::from_str(&line).unwrap();
                            let response =
                                h(request, der, remote).unwrap_or_else(|e| json!({"error_code":e}));
                            writeln!(stream, "{response}").unwrap();
                            stream.flush().unwrap();
                        }));
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(1))
                    }
                    Err(e) => panic!("{e}"),
                }
            }
            for worker in workers {
                worker.join().unwrap();
            }
        });
        Self {
            address,
            stop,
            thread: Some(thread),
        }
    }
}
impl Drop for TlsServer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(t) = self.thread.take() {
            t.join().unwrap();
        }
    }
}
fn hex(b: &[u8]) -> String {
    b.iter().map(|v| format!("{v:02x}")).collect()
}
#[test]
fn remote_workspace_observer_reuses_paired_mtls_and_exact_host_route() {
    let tmp = tempfile::tempdir().unwrap();
    let p = repo(tmp.path());
    let (provider, state) = metadata(&tmp.path().join("p.sock"));
    let nr = tmp.path().join("node");
    let cr = tmp.path().join("centre");
    identity::open(&nr, "air", Some("Air")).unwrap();
    let centre = identity::open(&cr, "centre", Some("centre")).unwrap();
    let discovery = Arc::new(discovery_state::Runtime::default());
    let enrollment = discovery_enrollment::Runtime::default();
    let server = enrollment
        .invoke(
            "discovery.enrollment.server.start",
            &json!({"root":cr,"address":"127.0.0.1","port":0}),
            discovery.clone(),
            |_, _| Err("UNEXPECTED_CALLBACK".into()),
        )
        .unwrap();
    let code = discovery
        .invoke(
            "discovery.window.open",
            &json!({"window":server["window"],"lifetime":60}),
            |_, _| panic!(),
        )
        .unwrap();
    let paired=enrollment.invoke("discovery.enrollment.client.pair",&json!({"root":nr,"candidate":{"node_id":"centre","display_name":"centre","fingerprint":centre["fingerprint"],"endpoints":[["127.0.0.1",server["port"]]],"mismatch":false},"code":code}),discovery.clone(),|_,_|Err("UNEXPECTED_CALLBACK".into())).unwrap();
    assert_eq!(paired["node_id"], "centre");
    enrollment
        .invoke(
            "discovery.enrollment.server.close",
            &json!({"server":server["server"]}),
            discovery,
            |_, _| panic!(),
        )
        .unwrap();
    let scope = node_scope::Scope::parse(
        &json!({"user_scope":"fixture","execute_tasks":true,"projects":[p]}),
    )
    .unwrap()
    .value();
    identity::write(
        &cr,
        "sharing.json",
        &json!({"schema_version":1,"scopes":{"air":scope}}),
    )
    .unwrap();
    identity::write(
        &nr,
        "sharing.json",
        &json!({"schema_version":1,"scopes":{"centre":scope}}),
    )
    .unwrap();
    let registry = Arc::new(Registry::default());
    registry
        .invoke(
            "registry.init",
            &json!({"path":tmp.path().join("centre.sqlite3")}),
        )
        .unwrap();
    let (node_root, node_scope, provider_path) = (nr.clone(), scope.clone(), provider.path.clone());
    let relay = Relay::default();
    let node = TlsServer::new(nr.clone(), move |request, der, _| {
        node_owner::handle(
            &json!({"state":node_root,"certificate_hex":hex(&der),"centre_id":"centre","node_id":"air","active_scope":node_scope,"provider_relay":true,"request":request}),
            |op, a| {
                match op{"scope.apply"=>Ok(Value::Null),"provider.handle"=>relay.handle(&json!({"node_id":"air","request":a["request"],"scope":a["scope"],"context_wire":a["request"]["context"].to_string()}),|context,body|node_provider::exchange(&provider_path,&binary(),context,body,Duration::from_secs(2))),_=>panic!("unexpected owner {op}")}
            },
        )
    });
    let (croot, reg) = (cr.clone(), registry.clone());
    let centre_server = TlsServer::new(cr.clone(), move |request, der, address| {
        node_centre::handle(
            &json!({"state":croot,"certificate_hex":hex(&der),"request":request,"address":[address.ip().to_string(),address.port()],"stale_after":45}),
            |op, mut a| {
                if op == "registry.register" {
                    let mut defaults = json!({"protocol_version":"clinx-node-v1","providers":["codex_app_server"],"route_ids":[],"trusted":true,"authorized":true,"version_compatible":true,"identity_generation":1});
                    defaults
                        .as_object_mut()
                        .unwrap()
                        .extend(a["record"].as_object().unwrap().clone());
                    a["record"] = defaults;
                }
                reg.invoke(op, &a)
            },
        )
    });
    let registered=node_tls::call(&nr,"centre",&format!("tls://{}",centre_server.address),5.0,&json!({"operation":"node.register","protocol_version":"clinx-node-v1","node_id":"air","sharing_scope":scope,"port":node.address.port(),"capabilities":[]})).unwrap();
    assert_eq!(registered["registered"], true, "{registered}");
    let endpoint = tmp.path().join("g.sock");
    let (cr, reg) = (cr.clone(), registry.clone());
    let _gateway=node_gateway::listen(&json!({"endpoint":endpoint,"transport_uid":rustix::process::getuid().as_raw()}),move|r|node_paired::exchange(&json!({"state":cr,"node_id":r["context"]["host"],"context":r["context"],"body_wire":json!({"method":r["method"],"params":r["params"]}).to_string()}),|op,a|reg.invoke(op,&a))).unwrap();
    let observe = |r: &Value| {
        output(
            command(&binary())
                .args(["--observe", endpoint.to_str().unwrap(), &r.to_string()])
                .env(
                    "CLINX_RUST_PROVIDER_GATEWAYS",
                    json!({"air":endpoint,"imac":tmp.path().join("wrong.sock")}).to_string(),
                )
                .env(
                    "CLINX_RUST_PROVIDER_GATEWAY_UID",
                    rustix::process::getuid().as_raw().to_string(),
                ),
        )
    };
    let request = json!({"host":"air","cwd":p,"mode":"workspace"});
    let result = observe(&request);
    assert_eq!(result["cwd"], json!(p));
    assert_eq!(result["observer_source"], "CODEX_PROVIDER_COMMAND_EXEC");
    assert_eq!(result["observer_uid"], rustix::process::getuid().as_raw());
    assert_eq!(state.lock().unwrap().calls.len(), 1);
    let mut wrong = request.clone();
    wrong["host"] = json!("imac");
    assert_eq!(
        observe(&wrong),
        json!({"error":"REMOTE_PROVIDER_ENDPOINT_CONFLICT"})
    );
    wrong = request.clone();
    wrong["cwd"] = json!(tmp.path().join("outside-grant"));
    assert_eq!(observe(&wrong), json!({"error":"PROVIDER_SCOPE_DENIED"}));
    assert_eq!(state.lock().unwrap().calls.len(), 1);
    state.lock().unwrap().uid += 1;
    assert_eq!(
        observe(&request),
        json!({"error":"WORKSPACE_OBSERVER_IDENTITY_CONFLICT"})
    );
}
