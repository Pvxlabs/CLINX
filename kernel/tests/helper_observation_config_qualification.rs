//! Configuration obligations from the retired Python observation factory.
//! Each case starts the real Rust Helper against task-owned trust and state.
#[path = "support/observation.rs"]
mod observation;
#[path = "support/provider.rs"]
mod provider;
use clinx_decision_kernel::discovery_identity as identity;
use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, BufReader},
    net::TcpListener,
    os::unix::net::UnixListener,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};

struct Fixture {
    root: tempfile::TempDir,
    state: PathBuf,
    // A private endpoint prevents any connection to a configured production Centre.
    centre: TcpListener,
}
impl Fixture {
    fn new() -> Self {
        let root = tempfile::tempdir().unwrap();
        let state = root.path().join("state");
        identity::open(&state, "air", None).unwrap();
        let mut peer = identity::open(&root.path().join("centre"), "centre", None).unwrap();
        peer.as_object_mut().unwrap().remove("private_key");
        peer["trust_state"] = json!("TRUSTED");
        peer["security_status"] = json!("OPAQUE_V1");
        peer["pairing_protocol"] = json!("OPAQUE-3DH-RISTRETTO255-SHA512-ARGON2I-v1");
        identity::write(
            &state,
            "peers.json",
            &json!({"schema_version":1,"peers":{"centre":peer}}),
        )
        .unwrap();
        identity::write(&state, "sharing.json", &json!({"schema_version":1,"scopes":{"centre":{"user_scope":"alice","read_sessions":true,"execute_tasks":false}}})).unwrap();
        let mut helper = identity::read(&state, "identity.json").unwrap().unwrap();
        helper.as_object_mut().unwrap().remove("private_key");
        for key in ["trust_state", "security_status", "pairing_protocol"] {
            helper[key] = peer[key].clone();
        }
        identity::write(
            &root.path().join("centre"),
            "peers.json",
            &json!({"schema_version":1,"peers":{"air":helper}}),
        )
        .unwrap();
        identity::write(&root.path().join("centre"), "sharing.json", &json!({"schema_version":1,"scopes":{"air":{"user_scope":"alice","read_sessions":true,"execute_tasks":false}}})).unwrap();
        let centre = TcpListener::bind("127.0.0.1:0").unwrap();
        Self {
            root,
            state,
            centre,
        }
    }
    fn provider(&self) -> (PathBuf, UnixListener) {
        let socket = self.root.path().join("provider.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        listener.set_nonblocking(true).unwrap();
        let path = self.root.path().join("provider.toml");
        fs::write(
            &path,
            format!(
                "[linear]\nteam_id='team'\ntrigger_label='label'\ntodo_state='Todo'\nrunning_state='Running'\nreview_state='Review'\n[projects.fixture]\ncwd='/fixture'\nbranch='main'\n[app_server]\nlocal_socket={:?}\n[runtime]\nruntime_host='air'\n",
                socket
            ),
        )
        .unwrap();
        (path, listener)
    }
    fn start(&self, overrides: Value) -> Process {
        self.start_native(overrides, &self.root.path().join("native"))
    }
    fn start_native(&self, overrides: Value, native: &Path) -> Process {
        let mut config = json!({"schema_version":1,"centre_id":"centre","centre_endpoint":format!("tls://127.0.0.1:{}",self.centre.local_addr().unwrap().port()),"bind_address":"127.0.0.1"});
        config
            .as_object_mut()
            .unwrap()
            .extend(overrides.as_object().unwrap().clone());
        identity::write(&self.state, "node-config.json", &config).unwrap();
        let log = self.root.path().join("stderr");
        let mut child = Command::new(env!("CARGO_BIN_EXE_clinx-node-daemon"))
            .arg("helper")
            .env_clear()
            .env("HOME", self.root.path())
            .env("PATH", "/no-python")
            .env("CLINX_NODE_STATE", &self.state)
            .env("CLINX_NODE_HEALTH_STATE", self.root.path().join("health"))
            .env("CLINX_NATIVE_HOME", native)
            .env("CLINX_NODE_ID", "air")
            .env("CLINX_NODE_PORT", "0")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(fs::File::create(log).unwrap())
            .spawn()
            .unwrap();
        let output = BufReader::new(child.stdout.take().unwrap());
        Process { child, output }
    }
    fn serve_centre(&self) -> (Process, Value) {
        let mut child = Command::new(env!("CARGO_BIN_EXE_clinx-node-daemon"))
            .arg("--state")
            .arg(self.root.path().join("centre"))
            .args([
                "--node-id",
                "centre",
                "serve",
                "--bind",
                "127.0.0.1",
                "--port",
                "0",
                "--registry",
            ])
            .arg(self.root.path().join("nodes.db"))
            .arg("--observations")
            .arg(self.root.path().join("observations.db"))
            .env_clear()
            .env("HOME", self.root.path())
            .env("PATH", "/no-python")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(fs::File::create(self.root.path().join("centre-stderr")).unwrap())
            .spawn()
            .unwrap();
        let output = BufReader::new(child.stdout.take().unwrap());
        let mut process = Process { child, output };
        let ready = process.ready_node("centre");
        (process, ready)
    }
    fn blocked(&self, config: Value, expected: &str) {
        let mut p = self.start(config);
        p.wait();
        let health: Value =
            serde_json::from_slice(&fs::read(self.root.path().join("health/health.json")).unwrap())
                .unwrap();
        assert_eq!(health["status"], "blocked");
        assert_eq!(health["reason"], expected);
        assert!(!self.state.join("observation-outbox.sqlite3").exists());
    }
}
struct Process {
    child: Child,
    output: BufReader<std::process::ChildStdout>,
}
impl Process {
    fn wait(&mut self) {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            if let Some(status) = self.child.try_wait().unwrap() {
                assert!(status.success());
                return;
            }
            assert!(Instant::now() < deadline, "Helper shutdown deadline");
            std::thread::sleep(Duration::from_millis(10));
        }
    }
    fn ready(&mut self) {
        self.ready_node("air");
    }
    fn ready_node(&mut self, node: &str) -> Value {
        let count = rustix::event::poll(
            &mut [rustix::event::PollFd::new(
                self.output.get_ref(),
                rustix::event::PollFlags::IN,
            )],
            Some(&rustix::event::Timespec {
                tv_sec: 5,
                tv_nsec: 0,
            }),
        )
        .unwrap();
        assert!(count > 0, "Helper readiness deadline");
        let mut line = String::new();
        self.output.read_line(&mut line).unwrap();
        let ready: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(ready["runtime"], "RUST_NODE_DAEMON_V1");
        assert_eq!(ready["node_id"], node);
        assert_eq!(ready["ready"], true);
        ready
    }
}
impl Drop for Process {
    fn drop(&mut self) {
        if self.child.try_wait().ok().flatten().is_none() {
            let pid = rustix::process::Pid::from_raw(self.child.id() as i32).unwrap();
            let _ = rustix::process::kill_process(pid, rustix::process::Signal::TERM);
            let deadline = Instant::now() + Duration::from_secs(7);
            while self.child.try_wait().ok().flatten().is_none() && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(10));
            }
            if self.child.try_wait().ok().flatten().is_none() {
                let _ = self.child.kill();
            }
        }
        let _ = self.child.wait();
    }
}
#[test]
fn explicit_provider_loads_readonly_without_execution_or_provider_startup() {
    let f = Fixture::new();
    let (path, listener) = f.provider();
    let mut child = f.start(json!({"observation_provider_config":path,"execute_tasks":false}));
    child.ready();
    assert_eq!(
        listener.accept().unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
}
#[test]
fn native_writer_observation_is_independent_of_execution_config() {
    let f = Fixture::new();
    let mut child = f.start(json!({"observation_native_writer":true,"execution_config":null}));
    child.ready();
}
#[test]
fn ambiguous_native_writer_routes_fail_closed() {
    for config in [
        json!({"observation_native_writer":"true"}),
        json!({"observation_native_writer":true,"observation_provider_config":"/other"}),
    ] {
        Fixture::new().blocked(config, "Invalid native writer observation route");
    }
}
#[test]
fn invalid_explicit_provider_routes_fail_closed() {
    for path in [json!(""), json!(" "), json!(false), json!(3), json!([])] {
        Fixture::new().blocked(
            json!({"observation_provider_config":path}),
            "Invalid observation_provider_config",
        );
    }
}
#[test]
fn missing_provider_config_never_falls_back_to_execution() {
    let f = Fixture::new();
    let (execution, listener) = f.provider();
    f.blocked(json!({"observation_provider_config":f.root.path().join("missing.toml"),"execution_config":execution}), "CONFIG_READ_FAILED");
    assert_eq!(
        listener.accept().unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
}

#[test]
fn execution_config_never_implicitly_selects_observation_provider() {
    // Both variants cross real authenticated Centre/Helper sockets and commit
    // three independent native-history events. The positive control must read
    // the very same Provider socket, so zero calls cannot hide a missed tick.
    for explicit in [false, true] {
        let f = Fixture::new();
        let native = observation::Native::new();
        let before = observation::snapshot(&native.root);
        let (config_path, initial_listener) = f.provider();
        drop(initial_listener);
        let socket = f.root.path().join("provider.sock");
        fs::remove_file(&socket).unwrap();
        let provider = provider::Provider::new(&socket, |request| {
            let result = match request["method"].as_str().unwrap() {
                "initialized" => return Some(vec![]),
                "initialize" => json!({"userAgent":"private qualification"}),
                "thread/read" => {
                    assert_eq!(
                        request["params"],
                        json!({"threadId":observation::TID,"includeTurns":false})
                    );
                    json!({"thread":{"id":observation::TID,"status":"active","cwd":"/approved"}})
                }
                "thread/turns/list" => {
                    json!({"data":[{"id":"turn-2","status":"inProgress"}],"nextCursor":null})
                }
                other => panic!("Observer called mutation: {other}"),
            };
            Some(vec![json!({"id":request["id"],"result":result})])
        });
        let (centre, ready) = f.serve_centre();
        let mut config = json!({"execution_config":config_path,"execute_tasks":false,"centre_endpoint":format!("tls://127.0.0.1:{}",ready["address"][1])});
        if explicit {
            config["observation_provider_config"] = json!(config_path);
        }
        let mut helper = f.start_native(config, &native.root);
        helper.ready();
        let observations = rusqlite::Connection::open_with_flags(
            f.root.path().join("observations.db"),
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
        )
        .unwrap();
        observations
            .busy_timeout(Duration::from_millis(300))
            .unwrap();
        let until = Instant::now() + Duration::from_secs(15);
        let events = loop {
            let rows = observations
                .prepare("SELECT payload FROM events ORDER BY seq")
                .unwrap()
                .query_map([], |r| r.get::<_, String>(0))
                .unwrap()
                .map(|v| serde_json::from_str::<Value>(&v.unwrap()).unwrap())
                .collect::<Vec<_>>();
            if rows.len() == 3 {
                break rows;
            }
            assert!(
                Instant::now() < until,
                "no sampled upload: {:?}",
                fs::read_to_string(f.root.path().join("health/health.json"))
            );
            std::thread::sleep(Duration::from_millis(25));
        };
        assert!(
            events
                .iter()
                .all(|e| e["native_thread_id"] == observation::TID)
        );
        let running = events
            .iter()
            .find(|e| e["turn"]["turn_id"] == "turn-2")
            .unwrap();
        assert_eq!(running["turn"]["native_state"], "RUNNING");
        let calls = provider.calls.lock().unwrap().clone();
        if explicit {
            assert_eq!(
                calls
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
            assert_eq!(running["turn"]["live_observation"]["liveness"], "LIVE");
            assert_eq!(running["turn"]["live_observation"]["turn_id"], "turn-2");
        } else {
            assert!(calls.is_empty());
            assert!(running["turn"].get("live_observation").is_none());
        }
        assert_eq!(observation::snapshot(&native.root), before);
        drop(helper);
        drop(centre);
    }
}
