//! Full native Helper/Centre lifecycle and independent OpenSSL wire oracles.
//! Authority and Provider fixtures record IO, never invent task authority state.
#[path = "support/node_qualification.rs"]
mod protocol;
#[path = "support/provider.rs"]
mod provider;
use base64::{Engine, engine::general_purpose::STANDARD};
use clinx_decision_kernel::{discovery_identity as identity, node_registry::Registry, node_tls};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{BufRead, BufReader, Write},
    net::TcpListener,
    os::unix::{fs::PermissionsExt, net::UnixListener},
    path::{Path, PathBuf},
    process::{Child, ChildStdout, Command, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant},
};
struct Process {
    child: Child,
    output: BufReader<ChildStdout>,
    log: PathBuf,
    successful_exit_required: bool,
}
impl Process {
    fn spawn(mut c: Command, log: PathBuf) -> Self {
        let mut child = c
            .stdout(Stdio::piped())
            .stderr(fs::File::create(&log).unwrap())
            .spawn()
            .unwrap();
        let output = BufReader::new(child.stdout.take().unwrap());
        Self {
            child,
            output,
            log,
            successful_exit_required: true,
        }
    }
    fn line(&mut self) -> Value {
        assert!(
            rustix::event::poll(
                &mut [rustix::event::PollFd::new(
                    self.output.get_ref(),
                    rustix::event::PollFlags::IN
                )],
                Some(&rustix::event::Timespec {
                    tv_sec: 15,
                    tv_nsec: 0
                })
            )
            .unwrap()
                > 0,
            "readiness deadline {}",
            fs::read_to_string(&self.log).unwrap()
        );
        let mut line = String::new();
        self.output.read_line(&mut line).unwrap();
        serde_json::from_str(&line)
            .unwrap_or_else(|e| panic!("{e}: {line}; {}", fs::read_to_string(&self.log).unwrap()))
    }
}
impl Drop for Process {
    fn drop(&mut self) {
        if self.child.try_wait().unwrap().is_none() {
            let _ = rustix::process::kill_process(
                rustix::process::Pid::from_raw(self.child.id() as i32).unwrap(),
                rustix::process::Signal::TERM,
            );
        }
        let until = Instant::now() + Duration::from_secs(8);
        while self.child.try_wait().unwrap().is_none() {
            if Instant::now() >= until {
                let _ = self.child.kill();
                break;
            }
            thread::sleep(Duration::from_millis(5));
        }
        let status = self.child.wait().unwrap();
        if !thread::panicking() && self.successful_exit_required {
            assert!(
                status.success(),
                "{}",
                fs::read_to_string(&self.log).unwrap()
            );
        }
    }
}
fn cli(root: &Path, node: &str, args: &[&str], input: Option<&str>) -> Value {
    let mut child = Command::new(env!("CARGO_BIN_EXE_clinx-node-daemon"))
        .arg("--state")
        .arg(root)
        .args(["--node-id", node])
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    if let Some(input) = input {
        child
            .stdin
            .take()
            .unwrap()
            .write_all(input.as_bytes())
            .unwrap();
    } else {
        child.stdin.take();
    }
    let until = Instant::now() + Duration::from_secs(15);
    while child.try_wait().unwrap().is_none() {
        if Instant::now() >= until {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("daemon CLI deadline")
        };
        thread::sleep(Duration::from_millis(5));
    }
    let out = child.wait_with_output().unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice(&out.stdout).unwrap()
}
struct Authority {
    path: PathBuf,
    calls: Arc<Mutex<Vec<Value>>>,
    cancel: Arc<AtomicBool>,
    accept: Arc<AtomicBool>,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}
impl Authority {
    fn new(path: PathBuf) -> Self {
        let listener = UnixListener::bind(&path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        listener.set_nonblocking(true).unwrap();
        let accept = Arc::new(AtomicBool::new(false));
        let calls = Arc::new(Mutex::new(vec![]));
        let cancel = Arc::new(AtomicBool::new(false));
        let stop = Arc::new(AtomicBool::new(false));
        let (c, k, s, a) = (calls.clone(), cancel.clone(), stop.clone(), accept.clone());
        let thread = thread::spawn(move || {
            while !s.load(Ordering::Relaxed) {
                let (mut peer, _) = match listener.accept() {
                    Ok(p) => p,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2));
                        continue;
                    }
                    Err(e) => panic!("{e}"),
                };
                peer.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
                let mut line = String::new();
                BufReader::new(peer.try_clone().unwrap())
                    .read_line(&mut line)
                    .unwrap();
                let request: Value = serde_json::from_str(&line).unwrap();
                let mut response = json!({"protocol_version":request["protocol_version"],"request_id":request["request_id"]});
                if request["operation"] == "health" {
                    response["result"] = json!({"business_commands":true});
                } else {
                    c.lock().unwrap().push(request.clone());
                    if request["payload"]["payload"]["prepared_execution_ref"] == "prepared_lost" {
                        continue;
                    }
                    let op = request["payload"]["operation"].as_str().unwrap();
                    if a.load(Ordering::Relaxed) {
                        response["result"] = json!({"accepted_by_fixture":true});
                    } else if k.load(Ordering::Relaxed) {
                        response["result"] = match op {
                            "get_status" => {
                                json!({"task_ref":"task-exact","execution_ref":"exec-exact","thread_id":"thread-exact","native_turn_id":"turn-exact"})
                            }
                            "list_tasks" => {
                                json!({"tasks":[{"task_id":"task-exact","host":"air","project":"fixture","thread_id":"thread-exact"}]})
                            }
                            "cancel" => {
                                json!({"status":"CANCELLATION_PENDING","resource_state":"HELD","execution_ref":"exec-exact"})
                            }
                            _ => Value::Null,
                        };
                    } else {
                        response["error"] = json!("FIXTURE_AUTHORITY_BOUNDARY");
                    }
                }
                writeln!(peer, "{response}").unwrap();
            }
        });
        Self {
            path,
            calls,
            cancel,
            accept,
            stop,
            thread: Some(thread),
        }
    }
    fn calls(&self) -> Vec<Value> {
        self.calls.lock().unwrap().clone()
    }
}
impl Drop for Authority {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        self.thread.take().unwrap().join().unwrap();
    }
}
struct Fixture {
    _centre_child: Process,
    authority: Authority,
    registry: Registry,
    root: tempfile::TempDir,
    air: PathBuf,
    centre: PathBuf,
    endpoint: String,
    centre_endpoint: String,
    public: Value,
    cfg: PathBuf,
    helper_binary: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let root = tempfile::tempdir().unwrap();
        let air = root.path().join("air");
        let centre = root.path().join("centre");
        let public = cli(&centre, "centre", &["bootstrap"], None);
        let node = cli(&air, "air", &["bootstrap"], None);
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_clinx-node-daemon"));
        cmd.arg("--state")
            .arg(&centre)
            .args([
                "--node-id",
                "centre",
                "pair-listen",
                "--bind",
                "127.0.0.1",
                "--port",
                "0",
            ])
            .stdin(Stdio::null());
        let mut pairing = Process::spawn(cmd, root.path().join("pair.log"));
        let offer = pairing.line();
        assert_eq!(
            cli(
                &air,
                "air",
                &[
                    "pair-connect",
                    "--peer",
                    "centre",
                    "--fingerprint",
                    public["fingerprint"].as_str().unwrap(),
                    "--endpoint",
                    &format!("tls://127.0.0.1:{}", offer["port"])
                ],
                Some(&format!("{}\n", offer["pairing_code"].as_str().unwrap()))
            ),
            json!({"paired":true,"node_id":"centre","authority_granted":false})
        );
        drop(pairing);
        for (path, node, peer) in [(&centre, "centre", "air"), (&air, "air", "centre")] {
            cli(
                path,
                node,
                &[
                    "share",
                    "--peer",
                    peer,
                    "--user-scope",
                    "alice",
                    "--read-sessions",
                    "--execute-tasks",
                    "--approved",
                ],
                None,
            );
        }
        let authority = Authority::new(root.path().join("authority.sock"));
        let native = root.path().join("native");
        fs::create_dir(&native).unwrap();
        rusqlite::Connection::open(native.join("state_5.sqlite"))
            .unwrap()
            .execute_batch("CREATE TABLE threads(id TEXT,cwd TEXT,rollout_path TEXT);")
            .unwrap();
        let cfg = root.path().join("config.toml");
        fs::write(&cfg,format!("[linear]\nteam_id='team'\ntrigger_label='label'\ntodo_state='Todo'\nrunning_state='Running'\nreview_state='Review'\n[projects.fixture]\ncwd='/fixture'\nbranch='main'\n[runtime]\nruntime_host='air'\n[app_server]\nlocal_socket={}\n",json!(root.path().join("provider.sock")))).unwrap();
        let registry_path = root.path().join("nodes.sqlite3");
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_clinx-node-daemon"));
        cmd.arg("--state")
            .arg(&centre)
            .args(["--node-id", "centre", "serve", "--registry"])
            .arg(&registry_path)
            .args(["--bind", "127.0.0.1", "--port", "0"])
            .env("CLINX_AUTHORITY", "rust")
            .env("CLINX_RUST_SOCKET", &authority.path)
            .env(
                "CLINX_RUST_UID",
                rustix::process::getuid().as_raw().to_string(),
            )
            .stdin(Stdio::null());
        let mut centre_child = Process::spawn(cmd, root.path().join("centre.log"));
        let ready = centre_child.line();
        let centre_endpoint = format!("tls://127.0.0.1:{}", ready["address"][1]);
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        cli(
            &air,
            "air",
            &[
                "configure-node",
                "--centre",
                "centre",
                "--endpoint",
                &centre_endpoint,
                "--bind",
                "127.0.0.1",
                "--port",
                &port.to_string(),
                "--execution-config",
                cfg.to_str().unwrap(),
            ],
            None,
        );
        let registry = Registry::default();
        registry
            .invoke("registry.init", &json!({"path":registry_path}))
            .unwrap();
        Self {
            _centre_child: centre_child,
            authority,
            registry,
            root,
            air,
            centre,
            endpoint: format!("tls://127.0.0.1:{port}"),
            centre_endpoint,
            public: node,
            cfg,
            helper_binary: env!("CARGO_BIN_EXE_clinx-node-daemon").into(),
        }
    }
    fn helper(&self) -> Process {
        let mut c = Command::new(&self.helper_binary);
        if self.helper_binary.file_name().unwrap() != "CLINXNodeService" {
            c.arg("helper");
        }
        c.env_clear()
            .env("HOME", self.root.path())
            .env("PATH", "/usr/bin:/bin")
            .env("CLINX_NODE_STATE", &self.air)
            .env("CLINX_NODE_HEALTH_STATE", self.root.path().join("health"))
            .env("CLINX_NATIVE_HOME", self.root.path().join("native"))
            .env("CLINX_NODE_BINARY", env!("CARGO_BIN_EXE_clinx-node"))
            .env(
                "CLINX_PROVIDER_BINARY",
                env!("CARGO_BIN_EXE_clinx-provider"),
            )
            .stdin(Stdio::null());
        let mut child = Process::spawn(c, self.root.path().join("helper.log"));
        assert_eq!(child.line()["runtime"], "RUST_NODE_DAEMON_V1");
        let until = Instant::now() + Duration::from_secs(8);
        loop {
            let row = self.row();
            if row["endpoint"] == self.endpoint
                && self.call(
                    json!({"operation":"session.read","user_scope":"alice","thread_id":"absent"}),
                )["lookup_status"]
                    == "THREAD_NOT_FOUND"
            {
                break;
            }
            assert!(Instant::now() < until, "{row}");
            thread::sleep(Duration::from_millis(20));
        }
        child
    }
    fn row(&self) -> Value {
        self.registry
            .invoke("registry.get", &json!({"node_id":"air"}))
            .unwrap()
    }
    fn call(&self, r: Value) -> Value {
        node_tls::call(&self.centre, "air", &self.endpoint, 5.0, &r).unwrap()
    }
    fn ops(&self) -> Vec<String> {
        self.authority
            .calls()
            .iter()
            .map(|c| c["payload"]["operation"].as_str().unwrap().into())
            .collect()
    }
}
fn start(id: &str) -> Value {
    json!({"operation":"execution.start","request_id":id,"execution_ref":format!("exec_{id}"),"prepared_execution_ref":format!("prepared_{id}"),"user_scope":"alice","approved":true})
}
#[test]
fn standalone_pair_registration_reverse_identity_and_scope_match_original_contract() {
    let f = Fixture::new();
    let _h = f.helper();
    let r = f.call(start("one"));
    assert_eq!(r["error_code"], "FIXTURE_AUTHORITY_BOUNDARY");
    let calls = f.authority.calls();
    assert_eq!(calls.len(), 1);
    let wire = &calls[0];
    assert_eq!(wire["operation"], "business.node_request");
    assert_eq!(wire["payload"]["operation"], "get_prepared");
    assert_eq!(
        wire["payload"]["payload"],
        json!({"prepared_execution_ref":"prepared_one"})
    );
    assert_eq!(
        wire["payload"]["scope"],
        json!({"principal":{"node_id":"air","user_scope":"alice","fingerprint":f.public["fingerprint"]},"projects":["*"]})
    );
    assert!(!wire["payload"]["request_id"].as_str().unwrap().is_empty());
    assert_eq!(f.call(start("one"))["idempotent"], true);
    assert_eq!(f.ops(), vec!["get_prepared", "get_status"]);
    let mut bad = start("other");
    bad["user_scope"] = json!("bob");
    assert_eq!(f.call(bad)["error_code"], "USER_SCOPE_DENIED");
    cli(
        &f.air,
        "air",
        &[
            "share",
            "--peer",
            "centre",
            "--user-scope",
            "alice",
            "--revoke",
            "--approved",
        ],
        None,
    );
    assert_eq!(
        f.call(start("revoked"))["error_code"],
        "SHARING_SCOPE_DENIED"
    );
    assert_eq!(f.authority.calls().len(), 2);
}
#[test]
fn standalone_lost_ack_restart_reads_exact_receipt_without_replaying_start() {
    let f = Fixture::new();
    {
        let _h = f.helper();
        let r = f.call(start("lost"));
        assert_eq!(r["operation_state"], "FAILED");
        assert_eq!(r["error_code"], "DELIVERY_UNKNOWN");
        assert_eq!(r["side_effect"], "UNKNOWN");
    }
    assert_eq!(f.ops(), vec!["get_prepared"]);
    {
        let _h = f.helper();
        let r = f.call(start("lost"));
        assert_eq!(r["operation_state"], "UNKNOWN");
        assert_eq!(r["idempotent"], true);
    }
    let ops = f.ops();
    assert_eq!(ops.iter().filter(|s| *s == "get_prepared").count(), 1);
    assert!(!ops.iter().any(|s| s == "start"));
    assert!(ops.len() > 1);
}
#[test]
fn standalone_concurrent_requests_claim_once_and_conflicting_payload_fails() {
    let f = Fixture::new();
    let _h = f.helper();
    thread::scope(|s| {
        let jobs = (0..6)
            .map(|_| s.spawn(|| f.call(start("one"))))
            .collect::<Vec<_>>();
        for job in jobs {
            let r = job.join().unwrap();
            assert!(
                r["error_code"] == "FIXTURE_AUTHORITY_BOUNDARY"
                    || r["operation_state"] == "UNKNOWN",
                "{r}"
            );
        }
    });
    assert_eq!(f.ops().iter().filter(|s| *s == "get_prepared").count(), 1);
    let mut conflict = start("one");
    conflict["prepared_execution_ref"] = json!("prepared_changed");
    assert_eq!(f.call(conflict)["error_code"], "IDEMPOTENCY_KEY_REUSE");
}
#[test]
fn standalone_provider_evidence_has_exact_identity_and_single_submission() {
    let f = Fixture::new();
    let evidence =
        json!({"id":"exact","cwd":"/fixture","turns":[{"id":"turn-exact","status":"completed"}]});
    let expected = evidence.clone();
    let p = provider::Provider::new(&f.root.path().join("provider.sock"), move |r| {
        Some(if r["method"] == "initialized" {
            vec![]
        } else {
            vec![
                json!({"id":r["id"],"result":if r["method"]=="initialize"{json!({"userAgent":"fixture"})}else{json!({"thread":expected})}}),
            ]
        })
    });
    let _h = f.helper();
    let body = serde_json::to_vec(
        &json!({"method":"thread/read","params":{"threadId":"exact","includeTurns":true}}),
    )
    .unwrap();
    let context = json!({"host":"air","user_scope":"alice","cwd":"/fixture","thread_id":"exact","mode":"read"});
    let mut req = json!({"operation":"provider.upload","protocol_version":"clinx-node-v1","node_id":"air","request_id":"a".repeat(32),"context":context,"index":0,"count":1,"digest":format!("{:x}",Sha256::digest(&body)),"data":STANDARD.encode(&body)});
    assert_eq!(f.call(req.clone())["accepted"], true);
    assert_eq!(
        f.call(req.clone())["error_code"],
        "PROVIDER_REQUEST_IDENTITY_CONFLICT"
    );
    let r=f.call(json!({"operation":"provider.read","protocol_version":"clinx-node-v1","node_id":"air","request_id":"a".repeat(32),"context":context,"index":0}));
    let decoded = STANDARD.decode(r["data"].as_str().unwrap()).unwrap();
    assert_eq!(format!("{:x}", Sha256::digest(&decoded)), r["digest"]);
    assert_eq!(
        serde_json::from_slice::<Value>(&decoded).unwrap(),
        json!({"result":{"thread":evidence}})
    );
    req["request_id"] = json!("b".repeat(32));
    req["context"]["user_scope"] = json!("bob");
    assert_eq!(f.call(req)["error_code"], "PROVIDER_SCOPE_DENIED");
    assert_eq!(
        p.calls()
            .iter()
            .map(|r| r["method"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["initialize", "initialized", "thread/read"]
    );
}
#[test]
fn standalone_cancel_preserves_exact_turn_pending_state_and_restart_idempotency() {
    let f = Fixture::new();
    f.authority.cancel.store(true, Ordering::Relaxed);
    let request = json!({"operation":"execution.cancel","request_id":"cancel-exact","execution_ref":"exec-exact","user_scope":"alice","thread_id":"thread-exact","native_turn_id":"turn-exact","approved":true});
    {
        let _h = f.helper();
        let r = f.call(request.clone());
        assert_eq!(r["operation_state"], "CANCELLATION_PENDING");
        assert_eq!(r["resource_state"], "HELD");
        let calls = f.authority.calls();
        let cancel = calls
            .iter()
            .filter(|v| v["payload"]["operation"] == "cancel")
            .collect::<Vec<_>>();
        assert_eq!(cancel.len(), 1);
        assert_eq!(
            cancel[0]["payload"]["payload"],
            json!({"execution_ref":"exec-exact","approved":true})
        );
        assert_eq!(f.call(request.clone())["idempotent"], true);
        let mut wrong = request.clone();
        wrong["request_id"] = json!("cancel-wrong");
        wrong["native_turn_id"] = json!("other-turn");
        assert_eq!(
            f.call(wrong)["error_code"],
            "EXECUTION_TURN_IDENTITY_CONFLICT"
        );
    }
    {
        let _h = f.helper();
        assert_eq!(f.call(request)["operation_state"], "CANCELLATION_PENDING");
    }
    assert_eq!(f.ops().iter().filter(|s| *s == "cancel").count(), 1);
}
#[test]
fn standalone_wrong_host_and_missing_socket_preserve_independent_read_permission() {
    for reason in ["wrong_host", "no_socket"] {
        let f = Fixture::new();
        let original = fs::read_to_string(&f.cfg).unwrap();
        fs::write(
            &f.cfg,
            if reason == "wrong_host" {
                original.replace("runtime_host='air'", "runtime_host='wrong'")
            } else {
                original.split("[app_server]").next().unwrap().into()
            },
        )
        .unwrap();
        let _h = f.helper();
        let row = f.row();
        assert!(
            row["capabilities"]
                .as_array()
                .unwrap()
                .contains(&json!("session.read"))
        );
        assert!(
            !row["capabilities"]
                .as_array()
                .unwrap()
                .contains(&json!("execution.start"))
        );
        assert_eq!(
            f.call(start("one"))["error_code"],
            "OPERATION_NOT_IMPLEMENTED"
        );
        assert!(f.authority.calls().is_empty());
    }
}
#[test]
fn actual_native_monitor_package_registers_reads_and_restarts_without_business_calls() {
    let mut f = Fixture::new();
    let resources = f.root.path().join("App/Contents/Resources");
    let source = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_clinx-build"))
        .arg("native-monitor-resources")
        .arg(source)
        .arg(&resources)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let manifest: Value =
        serde_json::from_slice(&fs::read(resources.join("python-sources.json")).unwrap()).unwrap();
    assert_eq!(manifest["files"], json!({}));
    assert!(!resources.join("DiscoveryRuntime").exists());
    let helpers = resources.parent().unwrap().join("Helpers");
    fs::create_dir(&helpers).unwrap();
    for (name, source) in [
        ("clinx-node", env!("CARGO_BIN_EXE_clinx-node")),
        ("clinx-node-daemon", env!("CARGO_BIN_EXE_clinx-node-daemon")),
        ("clinx-provider", env!("CARGO_BIN_EXE_clinx-provider")),
    ] {
        fs::copy(source, helpers.join(name)).unwrap();
    }
    let installed = helpers.join("clinx-node");
    assert_eq!(
        Sha256::digest(fs::read(&installed).unwrap()),
        Sha256::digest(fs::read(env!("CARGO_BIN_EXE_clinx-node")).unwrap())
    );
    for (args, request, expected) in [
        (
            vec![],
            json!({"protocol":"CLINX_REQUEST_PIPE_V1","mode":"api","operation":"clinx_get_capabilities","arguments":{},"config":{"runtime_host":"air","provider_endpoint":"/unconfigured-provider","provider_timeout_seconds":3,"host_aliases":{"air":["air"]},"codex_host_ids":{}}}),
            None,
        ),
        (
            vec!["--network-worker"],
            json!({"id":1,"operation":"history.message","arguments":{"value":{"type":"agentMessage","text":"password=hidden"}}}),
            Some(json!(["assistant", "password=[REDACTED]"])),
        ),
    ] {
        let mut child = Command::new(&installed)
            .args(args)
            .current_dir(f.root.path())
            .env_clear()
            .env("HOME", f.root.path())
            .env("PATH", "/nonexistent")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let mut input = child.stdin.take().unwrap();
        writeln!(input, "{request}").unwrap();
        input.flush().unwrap();
        let mut stdout = BufReader::new(child.stdout.take().unwrap());
        assert!(
            rustix::event::poll(
                &mut [rustix::event::PollFd::new(
                    stdout.get_ref(),
                    rustix::event::PollFlags::IN
                )],
                Some(&rustix::event::Timespec {
                    tv_sec: 10,
                    tv_nsec: 0
                })
            )
            .unwrap()
                > 0
        );
        let mut line = String::new();
        stdout.read_line(&mut line).unwrap();
        drop(input);
        let output = child.wait_with_output().unwrap();
        assert!(output.status.success());
        assert!(output.stderr.is_empty());
        let response: Value = serde_json::from_str(&line).unwrap();
        if let Some(expected) = expected {
            assert_eq!(response["result"], expected);
        } else {
            assert_eq!(response["result"]["authority"], "RUST");
            assert_eq!(response["result"]["host_tools"], true);
        }
    }
    let launcher = resources.join("CLINXNodeService");
    fs::copy(
        source.join("MonitorApp/Scripts/CLINXNodeService"),
        &launcher,
    )
    .unwrap();
    fs::set_permissions(&launcher, fs::Permissions::from_mode(0o755)).unwrap();
    f.helper_binary = launcher;
    for (path, node, peer) in [(&f.air, "air", "centre"), (&f.centre, "centre", "air")] {
        cli(
            path,
            node,
            &[
                "share",
                "--peer",
                peer,
                "--user-scope",
                "alice",
                "--read-sessions",
                "--approved",
            ],
            None,
        );
    }
    let mut config = identity::read(&f.air, "node-config.json").unwrap().unwrap();
    config.as_object_mut().unwrap().remove("execution_config");
    identity::write(&f.air, "node-config.json", &config).unwrap();
    for _ in 0..2 {
        let _h = f.helper();
        let r =
            f.call(json!({"operation":"session.read","user_scope":"alice","thread_id":"absent"}));
        assert_eq!(r["lookup_status"], "THREAD_NOT_FOUND");
        assert_eq!(r["provider_existence"], "NOT_FOUND");
        assert_eq!(r["read_only"], true);
        assert!(f.authority.calls().is_empty());
    }
}

fn reverse(f: &Fixture, id: &str) -> Value {
    json!({"operation":"authority.request","protocol_version":"clinx-node-v1","node_id":"air","sharing_scope":f.registry.invoke("registry.scope",&json!({"node_id":"air","user_scope":"alice"})).unwrap(),"request_id":id,"command":"prepare","payload":{"prompt":"intent"}})
}
#[test]
fn reverse_gateway_uses_authenticated_identity_and_rechecks_user_and_revocation() {
    let f = Fixture::new();
    f.authority.accept.store(true, Ordering::Relaxed);
    for (root, node, peer) in [(&f.air, "air", "centre"), (&f.centre, "centre", "air")] {
        cli(
            root,
            node,
            &[
                "share",
                "--peer",
                peer,
                "--user-scope",
                "alice",
                "--read-sessions",
                "--execute-tasks",
                "--project",
                "/fixture/repo",
                "--approved",
            ],
            None,
        );
    }
    let _h = f.helper();
    let mut wire = reverse(&f, "exact-id");
    let call = |v: &Value| node_tls::call(&f.air, "centre", &f.centre_endpoint, 5.0, v).unwrap();
    assert_eq!(call(&wire)["result"], json!({"accepted_by_fixture":true}));
    let original = f.authority.calls()[0].clone();
    assert_eq!(original["operation"], "business.node_request");
    assert_eq!(
        original["payload"],
        json!({"scope":{"principal":{"node_id":"air","user_scope":"alice","fingerprint":f.public["fingerprint"]},"projects":["/fixture/repo"]},"operation":"prepare","request_id":"exact-id","payload":{"prompt":"intent"}})
    );
    wire["request_id"] = json!("another-id");
    wire["command"] = json!("read_receipt");
    wire["payload"] = json!({});
    wire["scope"] = json!({"principal":{"node_id":"victim","user_scope":"bob"}});
    wire["principal"] = json!({"node_id":"victim"});
    assert!(call(&wire)["result"].is_object());
    assert_eq!(
        f.authority.calls()[1]["payload"]["scope"],
        original["payload"]["scope"]
    );
    wire["node_id"] = json!("victim");
    assert_eq!(call(&wire)["error_code"], "NODE_IDENTITY_MISMATCH");
    assert_eq!(f.authority.calls().len(), 2);
    wire["node_id"] = json!("air");
    cli(
        &f.centre,
        "centre",
        &[
            "share",
            "--peer",
            "air",
            "--user-scope",
            "bob",
            "--execute-tasks",
            "--approved",
        ],
        None,
    );
    assert_eq!(call(&wire)["error_code"], "USER_SCOPE_DENIED");
    cli(
        &f.centre,
        "centre",
        &[
            "share",
            "--peer",
            "air",
            "--user-scope",
            "alice",
            "--revoke",
            "--approved",
        ],
        None,
    );
    assert_eq!(call(&wire)["error_code"], "SHARING_SCOPE_DENIED");
    assert_eq!(f.authority.calls().len(), 2);
}
fn certificate_files(root: &Path, identity_root: &Path, label: &str) -> (PathBuf, PathBuf) {
    let raw = identity::read(identity_root, "identity.json")
        .unwrap()
        .unwrap();
    let cert = root.join(format!("{label}.crt"));
    let key = root.join(format!("{label}.key"));
    fs::write(&cert, raw["certificate"].as_str().unwrap()).unwrap();
    fs::write(&key, raw["private_key"].as_str().unwrap()).unwrap();
    fs::set_permissions(&key, fs::Permissions::from_mode(0o600)).unwrap();
    (cert, key)
}
#[test]
fn independent_openssl_and_rust_nodes_preserve_bidirectional_json_lines_and_eof() {
    for newline in [true, false] {
        let f = Fixture::new();
        f.authority.accept.store(true, Ordering::Relaxed);
        let _h = f.helper();
        let (ac, ak) = certificate_files(f.root.path(), &f.air, "air");
        let (cc, ck) = certificate_files(f.root.path(), &f.centre, "centre");
        let mut cmd = Command::new("openssl");
        cmd.args([
            "s_client",
            "-quiet",
            "-tls1_3",
            "-verify_return_error",
            "-connect",
            f.centre_endpoint.trim_start_matches("tls://"),
        ])
        .arg("-cert")
        .arg(&ac)
        .arg("-key")
        .arg(&ak)
        .arg("-CAfile")
        .arg(&cc)
        .stdin(Stdio::piped());
        let mut client = Process::spawn(cmd, f.root.path().join("openssl-client.log"));
        let mut request = reverse(&f, "openssl-to-rust");
        request["command"] = json!("read_receipt");
        request["payload"] = json!({});
        writeln!(client.child.stdin.as_mut().unwrap(), "{request}").unwrap();
        client.child.stdin.as_mut().unwrap().flush().unwrap();
        assert_eq!(
            client.line(),
            json!({"request_id":"openssl-to-rust","result":{"accepted_by_fixture":true}})
        );
        // OpenSSL has no signal-driven graceful exit contract; own/reap this oracle explicitly.
        client.successful_exit_required = false;
        client.child.kill().unwrap();
        client.child.wait().unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        drop(listener);
        let mut cmd = Command::new("openssl");
        cmd.args([
            "s_server",
            "-quiet",
            "-tls1_3",
            "-verify_return_error",
            "-Verify",
            "1",
            "-accept",
            &address.to_string(),
        ])
        .arg("-cert")
        .arg(&cc)
        .arg("-key")
        .arg(&ck)
        .arg("-CAfile")
        .arg(&ac)
        .stdin(Stdio::piped());
        let mut server = Process::spawn(cmd, f.root.path().join("openssl-server.log"));
        server.successful_exit_required = false;
        // s_server readiness is observable from the owned process TCP listener;
        // no authenticated request or task is emitted by this socket probe.
        let until = Instant::now() + Duration::from_secs(3);
        loop {
            if std::net::TcpStream::connect(address).is_ok() {
                break;
            }
            assert!(Instant::now() < until);
            thread::sleep(Duration::from_millis(10));
        }
        let endpoint = format!("tls://{address}");
        let air = f.air.clone();
        let job = thread::spawn(move || {
            node_tls::call(
                &air,
                "centre",
                &endpoint,
                5.0,
                &json!({"operation":"node.status"}),
            )
        });
        assert_eq!(server.line(), json!({"operation":"node.status"}));
        let reply = if newline {
            b"{\"old_openssl_server\":true}\n".as_slice()
        } else {
            b"{\"old_openssl_server\":true}".as_slice()
        };
        server
            .child
            .stdin
            .as_mut()
            .unwrap()
            .write_all(reply)
            .unwrap();
        server.child.stdin.as_mut().unwrap().flush().unwrap();
        if !newline {
            server.child.stdin.take();
        }
        assert_eq!(
            job.join().unwrap().unwrap(),
            json!({"old_openssl_server":true})
        );
        server.child.kill().unwrap();
        server.child.wait().unwrap();
        // Prevent Drop from treating deliberately killed test-oracle tools as daemons.
        drop(client);
        drop(server);
    }
}
#[test]
fn live_tls_listener_reloads_revocation_and_expired_certificate_before_dispatch() {
    for expired in [false, true] {
        let f = Fixture::new();
        let _h = f.helper();
        let mut peers = identity::read(&f.centre, "peers.json").unwrap().unwrap();
        if expired {
            let raw = identity::read(&f.air, "identity.json").unwrap().unwrap();
            let key = rcgen::KeyPair::from_pem(raw["private_key"].as_str().unwrap()).unwrap();
            let mut params = rcgen::CertificateParams::default();
            params
                .distinguished_name
                .push(rcgen::DnType::CommonName, "air");
            let now = time::OffsetDateTime::now_utc();
            params.not_before = now - time::Duration::days(2);
            params.not_after = now - time::Duration::days(1);
            peers["peers"]["air"]["certificate"] = json!(params.self_signed(&key).unwrap().pem());
        } else {
            peers["peers"]["air"]["trust_state"] = json!("REVOKED");
        }
        identity::write(&f.centre, "peers.json", &peers).unwrap();
        assert!(
            node_tls::call(
                &f.air,
                "centre",
                &f.centre_endpoint,
                3.0,
                &reverse(&f, "rejected")
            )
            .is_err()
        );
        assert!(f.authority.calls().is_empty());
    }
}

#[test]
fn service_read_validates_every_original_source_version_before_provider_io() {
    let reads = Arc::new(Mutex::new(vec![]));
    let seen = reads.clone();
    let w = protocol::Worker::new(move |e| match e["operation"].as_str().unwrap() {
        "service.read" => {
            seen.lock().unwrap().push(e["arguments"].clone());
            Ok(json!({"next_cursor":"page2","lookup_status":"THREAD_UNBOUND"}))
        }
        "service.snapshot" => {
            Ok(json!({"record":protocol::record("air"),"scope":protocol::scope(false)}))
        }
        "registry.update_thread" => Ok(Value::Null),
        _ => panic!("{e}"),
    });
    let mut a = json!({"record":protocol::record("air"),"scope":protocol::scope(false),"secret_hex":"666978747572652d6b6579","request":{"operation":"session.read","user_scope":"alice","thread_id":" t "}});
    for version in [
        Value::Null,
        json!(""),
        json!("v\n1"),
        json!(4),
        json!("x".repeat(513)),
    ] {
        a["request"]["source_version"] = version;
        assert_eq!(
            w.call("router.service_read", a.clone()).unwrap_err(),
            "ValueError"
        );
    }
    assert!(reads.lock().unwrap().is_empty());
    a["request"]["source_version"] = json!(" v1 ");
    let result = w.call("router.service_read", a).unwrap();
    assert_eq!(
        *reads.lock().unwrap(),
        vec![json!({"thread_id":"t","kwargs":{}})]
    );
    assert_eq!(w.call("cursor.provider_decode",json!({"cursor":result["next_cursor"],"node_id":"air","user_scope":"alice","provider":"codex_app_server","native_thread_id":"t","source":"native","source_version":"v1","secret_hex":"666978747572652d6b6579"})).unwrap(),"page2");
}
#[test]
fn cached_remote_request_rechecks_bilateral_scope_user_and_lost_ack_without_replay() {
    let sent = Arc::new(Mutex::new(vec![]));
    let response = Arc::new(Mutex::new(Ok(json!({"read_only":true}))));
    let (s, r) = (sent.clone(), response.clone());
    let w = protocol::Worker::new(move |e| match e["operation"].as_str().unwrap() {
        "remote.peer" => Ok(json!({})),
        "remote.scope" => Ok(protocol::scope(true)),
        "remote.request" => {
            s.lock().unwrap().push(e["arguments"].clone());
            r.lock().unwrap().clone()
        }
        _ => panic!("{e}"),
    });
    let mut record = protocol::record("air");
    record["endpoint"] = json!("tls://fixture:1");
    let mut a = json!({"record":record,"scope":protocol::scope(true),"node_id":"air","request":{"operation":"execution.context","user_scope":"alice","thread_id":"t"}});
    assert_eq!(
        w.call("router.remote_request", a.clone()).unwrap()["read_only"],
        true
    );
    assert_eq!(sent.lock().unwrap()[0]["request"]["node_id"], "air");
    a["scope"]["revoked"] = json!(true);
    assert_eq!(
        w.call("router.remote_request", a.clone()).unwrap_err(),
        "SHARING_SCOPE_DENIED"
    );
    assert_eq!(sent.lock().unwrap().len(), 1);
    a["scope"] = protocol::scope(true);
    a["request"]["user_scope"] = json!("other");
    assert_eq!(
        w.call("router.remote_request", a.clone()).unwrap_err(),
        "USER_SCOPE_DENIED"
    );
    assert_eq!(sent.lock().unwrap().len(), 1);
    a["request"]["user_scope"] = json!("alice");
    a["request"]["operation"] = json!("execution.start");
    *response.lock().unwrap() = Err("ACK lost".into());
    assert_eq!(w.call("router.remote_request", a).unwrap_err(), "ACK lost");
    assert_eq!(sent.lock().unwrap().len(), 2);
}

#[test]
fn native_helper_prepare_preserves_exact_prompt_and_generated_identity_after_lost_ack() {
    let t = tempfile::tempdir().unwrap();
    let mut command = Command::new(env!("CARGO_BIN_EXE_clinx-node"));
    command
        .stdin(Stdio::piped())
        .env_clear()
        .env("PATH", "/no-python");
    let mut p = Process::spawn(command, t.path().join("node.log"));
    let mut input = p.child.stdin.take().unwrap();
    writeln!(input,"{}",json!({"protocol":"CLINX_REQUEST_PIPE_V1","mode":"node","node":{"node_id":"air","projects":[]},"operation":"prepare","arguments":{"project":"project","task_action":"create","prompt":"Exact input"},"config":{"runtime_host":"air","provider_endpoint":"/unconfigured-provider","provider_timeout_seconds":3,"host_aliases":{"air":["air"]},"codex_host_ids":{}}})).unwrap();
    input.flush().unwrap();
    let sent = p.line();
    assert_eq!(sent["kind"], "authority");
    assert_eq!(sent["operation"], "prepare");
    assert_eq!(sent["arguments"]["prompt"], "Exact input");
    assert_eq!(sent["operation_id"].as_str().unwrap().len(), 32);
    writeln!(
        input,
        "{}",
        json!({"id":sent["id"],"error":{"code":"DELIVERY_UNKNOWN"}})
    )
    .unwrap();
    input.flush().unwrap();
    let result = p.line();
    assert_eq!(result["error"]["code"], "DELIVERY_UNKNOWN");
    assert_eq!(result["error"]["operation_id"], sent["operation_id"]);
    drop(input);
    assert!(p.child.wait().unwrap().success());
    let mut extra = String::new();
    assert_eq!(p.output.read_line(&mut extra).unwrap(), 0);
}
