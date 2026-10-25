//! Standalone Observer process qualifications ported from the original Python
//! fixture, with its frozen historical oracle and independent native databases.
#[path = "support/observer_fixture.rs"]
#[allow(dead_code)]
mod http_fixture;
#[path = "support/observation.rs"]
mod observation_fixture;
#[path = "support/provider.rs"]
mod provider_fixture;
use clinx_decision_kernel::{discovery_identity as identity, node_registry::Registry, observation};
use observation_fixture::{Native, TID, record, scope, snapshot};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    io::{BufRead, BufReader, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    os::unix::net::UnixListener,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant},
};
const TOKEN: &str = "private_observer_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx";
fn historical(root: &Path) -> PathBuf {
    let path = root.join("historical.sqlite3");
    rusqlite::Connection::open(&path)
        .unwrap()
        .execute_batch(include_str!("../../fixtures/observer_historical_v1.sql"))
        .unwrap();
    path
}
fn environment(root: &Path) -> BTreeMap<String, String> {
    let home = root.join("home");
    fs::create_dir_all(&home).unwrap();
    [
        ("HOME", home.to_str().unwrap()),
        ("PATH", "/usr/bin:/bin"),
        ("LANG", "C.UTF-8"),
        ("CLINX_OBSERVER_TOKEN", TOKEN),
        ("CLINX_OBSERVER_AUDIT_METHODS", "1"),
    ]
    .into_iter()
    .map(|(k, v)| (k.into(), v.into()))
    .collect()
}
struct Process {
    child: Child,
    address: SocketAddr,
    stdout: PathBuf,
    stderr: PathBuf,
}
impl Process {
    fn new(root: &Path, environment: &BTreeMap<String, String>) -> Self {
        let reserve = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = reserve.local_addr().unwrap();
        drop(reserve);
        let stdout = root.join(format!("observer-{}.out", address.port()));
        let stderr = root.join(format!("observer-{}.err", address.port()));
        let child = Command::new(env!("CARGO_BIN_EXE_clinx-observer"))
            .env_clear()
            .envs(environment)
            .env("CLINX_OBSERVER_PORT", address.port().to_string())
            .current_dir(root)
            .stdin(Stdio::null())
            .stdout(fs::File::create(&stdout).unwrap())
            .stderr(fs::File::create(&stderr).unwrap())
            .spawn()
            .unwrap();
        let mut process = Self {
            child,
            address,
            stdout,
            stderr,
        };
        let deadline = Instant::now() + Duration::from_secs(8);
        loop {
            assert!(
                process.child.try_wait().unwrap().is_none(),
                "{}",
                fs::read_to_string(&process.stderr).unwrap()
            );
            if TcpStream::connect_timeout(&address, Duration::from_millis(50)).is_ok() {
                assert_eq!(process.request("/v1/health").0, 200);
                break;
            }
            assert!(
                Instant::now() < deadline,
                "{}",
                fs::read_to_string(&process.stderr).unwrap()
            );
            thread::sleep(Duration::from_millis(20));
        }
        process
    }
    fn request(&self, path: &str) -> (u16, Value) {
        request(self.address, "GET", path, TOKEN)
    }
    fn close(&mut self) {
        if self.child.try_wait().unwrap().is_none() {
            rustix::process::kill_process(
                rustix::process::Pid::from_raw(self.child.id() as i32).unwrap(),
                rustix::process::Signal::TERM,
            )
            .unwrap();
        }
        let deadline = Instant::now() + Duration::from_secs(8);
        while self.child.try_wait().unwrap().is_none() {
            if Instant::now() >= deadline {
                self.child.kill().unwrap();
                self.child.wait().unwrap();
                panic!("Rust Observer did not stop on SIGTERM")
            }
            thread::sleep(Duration::from_millis(10));
        }
        assert!(
            self.child.wait().unwrap().success(),
            "{}",
            fs::read_to_string(&self.stderr).unwrap()
        );
    }
}
impl Drop for Process {
    fn drop(&mut self) {
        if thread::panicking() {
            let _ = self.child.kill();
            let _ = self.child.wait();
        } else {
            self.close()
        }
    }
}
fn request(address: SocketAddr, method: &str, path: &str, token: &str) -> (u16, Value) {
    let r = http_fixture::http(
        address,
        method,
        path,
        &[("Authorization", &format!("Bearer {token}"))],
        &[],
    );
    (r.status, r.value())
}
fn without_time(value: Value) -> Value {
    match value {
        Value::Object(mut m) => {
            m.remove("observed_at");
            Value::Object(m.into_iter().map(|(k, v)| (k, without_time(v))).collect())
        }
        Value::Array(a) => Value::Array(a.into_iter().map(without_time).collect()),
        v => v,
    }
}
#[test]
fn standalone_historical_projection_auth_audit_native_process_and_readonly_source() {
    let root = tempfile::tempdir().unwrap();
    let path = historical(root.path());
    let before = fs::read(&path).unwrap();
    let mut env = environment(root.path());
    env.insert("CLINX_OBSERVER_DB".into(), path.display().to_string());
    env.insert(
        "CLINX_OBSERVER_CODEX_HOST_IDS".into(),
        json!({"p620":"remote-ssh-discovered:p620"}).to_string(),
    );
    let mut observer = Process::new(root.path(), &env);
    assert_eq!(request(observer.address, "GET", "/v1/health", "bad").0, 401);
    assert_eq!(
        request(observer.address, "POST", "/v1/health", TOKEN).0,
        405
    );
    assert_eq!(observer.request("/v1/tasks?state=active&offset=-1").0, 400);
    assert_eq!(observer.request("/v1/tasks/absent").0, 404);
    let (status, item) = observer.request("/v1/tasks/task-fixture");
    assert_eq!(status, 200);
    let expected: Value = serde_json::from_str(include_str!(
        "../../fixtures/observer_historical_expected.json"
    ))
    .unwrap();
    assert_eq!(without_time(item.clone()), expected);
    assert_eq!(item["final_result"]["execution_ref"], "exec-fixture");
    assert!(
        item["codex_uri"]
            .as_str()
            .unwrap()
            .ends_with("?hostId=remote-ssh-discovered%3Ap620")
    );
    assert_eq!(observer.request("/v1/tasks/task-fixture/events").0, 200);
    assert_eq!(
        observer.request("/v2/observations").1["error"],
        "OBSERVATION_NOT_CONFIGURED"
    );
    let pid = observer.child.id();
    assert_eq!(
        fs::read_link(format!("/proc/{pid}/exe")).unwrap(),
        fs::canonicalize(env!("CARGO_BIN_EXE_clinx-observer")).unwrap()
    );
    assert!(
        fs::read_to_string(format!("/proc/{pid}/task/{pid}/children"))
            .unwrap()
            .trim()
            .is_empty()
    );
    observer.close();
    let stdout = fs::read_to_string(&observer.stdout).unwrap();
    let stderr = fs::read_to_string(&observer.stderr).unwrap();
    let rows = stdout
        .lines()
        .map(|l| serde_json::from_str::<Value>(l).unwrap())
        .collect::<Vec<_>>();
    assert!(!rows.is_empty());
    for row in rows {
        assert_eq!(
            row.as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect::<Vec<_>>(),
            ["observer_http_method", "status"]
        );
    }
    assert!(!format!("{stdout}{stderr}").contains(TOKEN));
    assert_eq!(fs::read(path).unwrap(), before);
}
fn bad_config(values: &[(&str, &str)], error: &str) {
    let root = tempfile::tempdir().unwrap();
    let absent = root.path().join("absent.db");
    let mut env = environment(root.path());
    env.insert("CLINX_OBSERVER_DB".into(), absent.display().to_string());
    for (k, v) in values {
        env.insert((*k).into(), (*v).into());
    }
    let result = Command::new(env!("CARGO_BIN_EXE_clinx-observer"))
        .env_clear()
        .envs(env)
        .output()
        .unwrap();
    assert_eq!(result.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&result.stderr).contains(error));
    assert!(!absent.exists());
}
#[test]
fn invalid_token_never_initializes_source() {
    bad_config(&[("CLINX_OBSERVER_TOKEN", "bad")], "CLINX_OBSERVER_TOKEN")
}
#[test]
fn invalid_host_map_never_initializes_source() {
    bad_config(
        &[("CLINX_OBSERVER_CODEX_HOST_IDS", "[]")],
        "CLINX_OBSERVER_CODEX_HOST_IDS",
    )
}
#[test]
fn invalid_host_id_never_initializes_source() {
    bad_config(
        &[("CLINX_OBSERVER_CODEX_HOST_IDS", "{\"p620\":\"host/name\"}")],
        "CLINX_OBSERVER_CODEX_HOST_IDS",
    )
}
#[test]
fn invalid_port_never_initializes_source() {
    bad_config(
        &[("CLINX_OBSERVER_PORT", "65536")],
        "OBSERVER_HTTP_BIND_FAILED",
    )
}
#[test]
fn missing_historical_database_is_never_created() {
    bad_config(&[], "LEGACY_SOURCE_UNAVAILABLE")
}
fn grant(state: &Path, revoked: bool) {
    identity::write(state,"sharing.json",&json!({"schema_version":1,"scopes":{"observer-fixture":{"user_scope":"alice","read_sessions":true,"execute_tasks":false,"providers":["codex_app_server"],"projects":["/approved"],"revoked":revoked,"updated_at":"2026-01-01T00:00:00+00:00"}}})).unwrap();
}
struct Config {
    native: Native,
    state: PathBuf,
    registry: PathBuf,
    env: BTreeMap<String, String>,
    _connection: Registry,
}
impl Config {
    fn new() -> Self {
        let native = Native::new();
        let root = native.temp.path();
        let state = root.join("state");
        let identity = identity::open(&state, "observer-fixture", None).unwrap();
        grant(&state, false);
        let registry = root.join("nodes.db");
        let connection = Registry::default();
        connection
            .invoke("registry.init", &json!({"path":registry}))
            .unwrap();
        let mut node = record("observer-fixture");
        node["public_key_fingerprint"] = identity["fingerprint"].clone();
        connection
            .invoke("registry.register", &json!({"record":node,"scope":scope()}))
            .unwrap();
        let observations = root.join("observations.db");
        observation::init(&observations).unwrap();
        let mut env = environment(root);
        for (k, v) in [
            ("CLINX_OBSERVER_DB", historical(root)),
            ("CLINX_OBSERVATION_DB", observations),
            ("CLINX_OBSERVATION_STATE", state.clone()),
            ("CLINX_OBSERVATION_REGISTRY", registry.clone()),
            ("CLINX_OBSERVATION_NATIVE_HOME", native.root.clone()),
        ] {
            env.insert(k.into(), v.display().to_string());
        }
        env.insert("CLINX_OBSERVATION_USER_SCOPE".into(), "alice".into());
        env.insert(
            "CLINX_OBSERVATION_NODE_ID".into(),
            "observer-fixture".into(),
        );
        Self {
            native,
            state,
            registry,
            env,
            _connection: connection,
        }
    }
}
fn quote(raw: &str) -> String {
    raw.bytes()
        .map(|b| {
            if b.is_ascii_alphanumeric() || b"-._~".contains(&b) {
                (b as char).to_string()
            } else {
                format!("%{b:02X}")
            }
        })
        .collect()
}
#[test]
fn collector_checkpoints_concurrency_restart_revocation_and_readonly_sources() {
    let c = Config::new();
    let before = snapshot(&c.native.root);
    let registry_before = fs::read(&c.registry).unwrap();
    let root = c.native.temp.path();
    let mut observer = Process::new(root, &c.env);
    let (status, inventory) = observer.request("/v2/native-collector/inventory");
    assert_eq!(status, 200);
    assert_eq!(inventory["read_only"], true);
    let (status, sampled) = observer.request(&format!("/v2/native-collector/sample/{TID}"));
    assert_eq!(status, 200);
    assert_eq!(sampled["thread_id"], TID);
    assert!(!sampled["events"].as_array().unwrap().is_empty());
    assert!(!sampled.to_string().contains("NEVER_"));
    let route = format!(
        "/v2/native-collector/sample/{TID}?checkpoint={}",
        quote(sampled["checkpoint"].as_str().unwrap())
    );
    thread::scope(|s| {
        let workers = (0..8)
            .map(|_| s.spawn(|| request(observer.address, "GET", &route, TOKEN)))
            .collect::<Vec<_>>();
        for w in workers {
            let (status, body) = w.join().unwrap();
            assert_eq!(status, 200);
            assert_eq!(body["thread_id"], TID);
        }
    });
    let (status, denied) = observer.request(&(route.clone() + "corrupted"));
    assert_eq!(status, 409);
    assert_eq!(denied["error"], "NATIVE_COLLECTOR_CURSOR_RESET");
    let (status, context) = observer.request(&format!("/v2/native/{TID}?context=1"));
    assert_eq!(status, 200);
    assert_eq!(context["last_user_intent"], "User 2");
    assert_eq!(context["codex_running"], false);
    assert_eq!(context["native_display_state"], "UNKNOWN");
    assert!(!context.to_string().contains("NEVER_"));
    assert_eq!(
        observer
            .request(&format!(
                "/v2/native/{TID}?recent_turns=999999999999999999999"
            ))
            .1["error"],
        "INVALID_CONTEXT_LIMIT"
    );
    observer.close();
    let reopened = Process::new(root, &c.env);
    assert_eq!(reopened.request(&route).0, 409);
    assert_eq!(
        reopened
            .request(&format!("/v2/native-collector/sample/{TID}"))
            .0,
        200
    );
    grant(&c.state, true);
    assert_eq!(
        reopened.request("/v2/native-collector/inventory").1["error"],
        "SHARING_SCOPE_DENIED"
    );
    assert_eq!(
        reopened.request(&format!("/v2/native/{TID}")).1["error"],
        "SHARING_SCOPE_DENIED"
    );
    drop(reopened);
    assert_eq!(snapshot(&c.native.root), before);
    assert_eq!(fs::read(&c.registry).unwrap(), registry_before);
}
fn provider_read(mode: &'static str) {
    let c = Config::new();
    let state = c.state.clone();
    let provider = provider_fixture::Provider::new(
        &c.native.temp.path().join("p.sock"),
        move |r| {
            let value = match r["method"].as_str().unwrap() {
                "initialized" => return Some(vec![]),
                "initialize" => json!({"userAgent":"private fixture"}),
                "thread/read" => {
                    assert_eq!(r["params"], json!({"threadId":TID,"includeTurns":false}));
                    if mode == "revoked" {
                        grant(&state, true)
                    }
                    json!({"thread":{"id":TID,"cwd":if mode=="wrong_workspace"{"/foreign"}else{"/approved"},"status":{"type":"active"}}})
                }
                _ => panic!("unexpected Provider call: {r}"),
            };
            Some(vec![json!({"id":r["id"],"result":value})])
        },
    );
    let mut env = c.env.clone();
    env.insert(
        "CLINX_NATIVE_OBSERVER_SOCKET".into(),
        provider.path.display().to_string(),
    );
    let observer = Process::new(c.native.temp.path(), &env);
    let (status, result) = observer.request(&format!("/v2/native/{TID}"));
    if mode == "exact" {
        assert_eq!(status, 200);
        assert_eq!(result["codex_running"], true);
        assert_eq!(result["native_display_state"], "RUNNING");
        assert_eq!(result["queried_thread_id"], TID);
    } else {
        assert_eq!(status, 400);
        assert_eq!(
            result["error"],
            if mode == "wrong_workspace" {
                "NATIVE_OBSERVATION_IDENTITY_CONFLICT"
            } else {
                "SHARING_SCOPE_DENIED"
            }
        );
    }
    drop(observer);
    assert_eq!(
        provider
            .calls()
            .iter()
            .map(|r| r["method"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["initialize", "initialized", "thread/read"]
    );
}
#[test]
fn provider_read_exact_identity() {
    provider_read("exact")
}
#[test]
fn provider_read_wrong_workspace() {
    provider_read("wrong_workspace")
}
#[test]
fn provider_read_revoked_scope() {
    provider_read("revoked")
}
#[test]
fn monitor_reuses_mcp_read_transport_without_historical_database() {
    let root = tempfile::tempdir().unwrap();
    let endpoint = root.path().join("m.sock");
    let listener = UnixListener::bind(&endpoint).unwrap();
    listener.set_nonblocking(true).unwrap();
    let calls = Arc::new(Mutex::new(vec![]));
    let stopped = Arc::new(AtomicBool::new(false));
    let recorded = calls.clone();
    let stop = stopped.clone();
    let worker = thread::spawn(move || {
        while !stop.load(Ordering::Acquire) {
            let (mut peer, _) = match listener.accept() {
                Ok(v) => v,
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(10));
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
            let name = request["params"]["name"].as_str().unwrap();
            recorded.lock().unwrap().push(name.to_owned());
            let result = match name {
                "clinx_get_capabilities" => json!({"authority":"RUST","read_only":true}),
                "clinx_list_observations" => json!({"items":[],"read_only":true}),
                _ => panic!("unexpected MCP method {name}"),
            };
            writeln!(
                peer,
                "{}",
                json!({"jsonrpc":"2.0","id":request["id"],"result":{"structuredContent":result}})
            )
            .unwrap();
        }
    });
    let absent = root.path().join("not-created.db");
    let mut env = environment(root.path());
    env.insert(
        "CLINX_OBSERVER_RUST_MCP".into(),
        endpoint.display().to_string(),
    );
    env.insert("CLINX_OBSERVER_DB".into(), absent.display().to_string());
    let observer = Process::new(root.path(), &env);
    assert_eq!(observer.request("/v1/health").1["authority"], "RUST");
    assert_eq!(
        observer.request("/v1/tasks?state=active").1["legacy_history"],
        false
    );
    assert_eq!(
        observer.request("/v1/tasks/old").1["error"],
        "RUST_TASKS_USE_OBSERVATIONS_V2"
    );
    assert_eq!(observer.request("/v2/observations").0, 200);
    drop(observer);
    stopped.store(true, Ordering::Release);
    worker.join().unwrap();
    assert!(!calls.lock().unwrap().is_empty());
    assert!(!absent.exists());
}
