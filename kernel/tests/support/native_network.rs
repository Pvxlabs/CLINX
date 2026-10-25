//! Real OPAQUE pairing, Rust Centre/Helper children and authenticated reverse IPC.
#![allow(dead_code)]
use super::runtime::{Runtime, eventually};
use clinx_decision_kernel::{
    native_transport::{Authority, UnixAuthority},
    node_tls,
};
use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, BufReader, Write},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
};
struct Daemon {
    child: Child,
    _output: BufReader<std::process::ChildStdout>,
    log: PathBuf,
}
impl Daemon {
    fn launch(mut command: Command, log: PathBuf) -> (Self, Value) {
        let mut child = command
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(fs::File::create(&log).unwrap())
            .spawn()
            .unwrap();
        let output = BufReader::new(child.stdout.take().unwrap());
        // Own the child before readiness assertions so startup failures reap it.
        let mut daemon = Self {
            child,
            _output: output,
            log,
        };
        let ready = rustix::event::poll(
            &mut [rustix::event::PollFd::new(
                daemon._output.get_ref(),
                rustix::event::PollFlags::IN,
            )],
            Some(&rustix::event::Timespec {
                tv_sec: 15,
                tv_nsec: 0,
            }),
        )
        .unwrap();
        assert!(
            ready > 0,
            "daemon not ready: {}",
            fs::read_to_string(&daemon.log).unwrap()
        );
        let mut line = String::new();
        daemon._output.read_line(&mut line).unwrap();
        let value = serde_json::from_str(&line).unwrap_or_else(|e| {
            panic!(
                "daemon output {line}: {e}; {}",
                fs::read_to_string(&daemon.log).unwrap()
            )
        });
        (daemon, value)
    }
}
impl Drop for Daemon {
    fn drop(&mut self) {
        if self.child.try_wait().ok().flatten().is_none() {
            let pid = rustix::process::Pid::from_raw(self.child.id() as i32).unwrap();
            let _ = rustix::process::kill_process(pid, rustix::process::Signal::TERM);
        }
        let _ = self.child.wait();
        if std::thread::panicking() {
            eprintln!("{}", fs::read_to_string(&self.log).unwrap_or_default());
        }
    }
}
fn cli(root: &Path, node: &str, args: &[&str], input: Option<&str>) -> Value {
    let mut child = Command::new(env!("CARGO_BIN_EXE_clinx-node-daemon"))
        .args(["--state", root.to_str().unwrap(), "--node-id", node])
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
        drop(child.stdin.take());
    }
    let result = child.wait_with_output().unwrap();
    assert!(
        result.status.success(),
        "daemon cli: {}",
        String::from_utf8_lossy(&result.stderr)
    );
    serde_json::from_slice(&result.stdout).unwrap()
}
#[derive(Clone)]
pub struct Reverse {
    pub root: PathBuf,
    pub endpoint: String,
    pub scope: Value,
}
impl Authority for Reverse {
    fn call(&mut self, op: &str, payload: Value, id: &str) -> Result<Value, String> {
        let r = node_tls::call(
            &self.root,
            "centre",
            &self.endpoint,
            10.0,
            &json!({"protocol_version":"clinx-node-v1","operation":"authority.request","node_id":"air",
        "sharing_scope":self.scope,"request_id":id,"command":op,"payload":payload}),
        )?;
        if let Some(e) = r["error_code"].as_str() {
            return Err(e.into());
        }
        if r["request_id"] != id {
            return Err("NODE_RESPONSE_IDENTITY_CONFLICT".into());
        }
        Ok(r["result"].clone())
    }
}
#[derive(Clone)]
pub enum Route {
    Local(UnixAuthority),
    Paired(Reverse),
}
impl Authority for Route {
    fn call(&mut self, op: &str, p: Value, id: &str) -> Result<Value, String> {
        match self {
            Self::Local(a) => a.call(op, p, id),
            Self::Paired(a) => a.call(op, p, id),
        }
    }
}
pub struct Paired {
    pub reverse: Reverse,
    pub fingerprint: Value,
    _helper: Daemon,
    _centre: Daemon,
}
impl Paired {
    pub fn new(r: &Runtime) -> Self {
        let root = r.temp.path();
        let centre = root.join("centre");
        let air = root.join("air");
        let public = cli(&centre, "centre", &["bootstrap"], None);
        let node = cli(&air, "air", &["bootstrap"], None);
        let mut pair = Command::new(env!("CARGO_BIN_EXE_clinx-node-daemon"));
        pair.arg("--state").arg(&centre).args([
            "--node-id",
            "centre",
            "pair-listen",
            "--port",
            "0",
            "--bind",
            "127.0.0.1",
        ]);
        let (pair, offer) = Daemon::launch(pair, root.join("pair.log"));
        let address = format!("tls://127.0.0.1:{}", offer["port"]);
        let result = cli(
            &air,
            "air",
            &[
                "pair-connect",
                "--peer",
                "centre",
                "--fingerprint",
                public["fingerprint"].as_str().unwrap(),
                "--endpoint",
                &address,
            ],
            Some(&format!("{}\n", offer["pairing_code"].as_str().unwrap())),
        );
        assert_eq!(
            result,
            json!({"paired":true,"node_id":"centre","authority_granted":false})
        );
        drop(pair);
        for (at, id, peer) in [(&centre, "centre", "air"), (&air, "air", "centre")] {
            cli(
                at,
                id,
                &[
                    "share",
                    "--peer",
                    peer,
                    "--user-scope",
                    "fixture",
                    "--read-sessions",
                    "--execute-tasks",
                    "--project",
                    r.repo.to_str().unwrap(),
                    "--approved",
                ],
                None,
            );
        }
        let registry = root.join("registry.sqlite3");
        let gateway = root.join("g.sock");
        let mut command = Command::new(env!("CARGO_BIN_EXE_clinx-node-daemon"));
        command
            .arg("--state")
            .arg(&centre)
            .args(["--node-id", "centre", "serve", "--registry"])
            .arg(&registry)
            .args(["--port", "0", "--bind", "127.0.0.1", "--provider-gateway"])
            .arg(&gateway)
            .arg("--provider-transport-uid")
            .arg(rustix::process::getuid().as_raw().to_string())
            .env("CLINX_AUTHORITY", "rust")
            .env("CLINX_RUST_SOCKET", &r.endpoint)
            .env(
                "CLINX_RUST_UID",
                rustix::process::getuid().as_raw().to_string(),
            );
        let (centre_child, ready) = Daemon::launch(command, root.join("centre.log"));
        let endpoint = format!("tls://127.0.0.1:{}", ready["address"][1]);
        let config = root.join("config.toml");
        fs::write(&config,format!("[linear]\nteam_id=\"team\"\ntrigger_label=\"label\"\ntodo_state=\"Todo\"\nrunning_state=\"Running\"\nreview_state=\"Review\"\n[projects.fixture]\ncwd={}\nbranch=\"main\"\n[runtime]\nruntime_host=\"air\"\n[app_server]\nlocal_socket={}\nrequest_timeout_seconds=2\n",json!(r.repo),json!(r.provider_path))).unwrap();
        cli(
            &air,
            "air",
            &[
                "configure-node",
                "--centre",
                "centre",
                "--endpoint",
                &endpoint,
                "--bind",
                "127.0.0.1",
                "--port",
                "0",
                "--execution-config",
                config.to_str().unwrap(),
            ],
            None,
        );
        let native = root.join("native");
        fs::create_dir(&native).unwrap();
        rusqlite::Connection::open(native.join("state_5.sqlite"))
            .unwrap()
            .execute_batch("CREATE TABLE threads(id TEXT,cwd TEXT,rollout_path TEXT);")
            .unwrap();
        let mut helper = Command::new(env!("CARGO_BIN_EXE_clinx-node-daemon"));
        helper
            .arg("helper")
            .env("CLINX_NODE_STATE", &air)
            .env("CLINX_NODE_HEALTH_STATE", root.join("health"))
            .env("CLINX_NATIVE_HOME", native)
            .env("CLINX_NODE_PORT", "0")
            .env("CLINX_NODE_BINARY", env!("CARGO_BIN_EXE_clinx-node"))
            .env(
                "CLINX_PROVIDER_BINARY",
                env!("CARGO_BIN_EXE_clinx-provider"),
            );
        let (helper_child, ready) = Daemon::launch(helper, root.join("helper.log"));
        assert_eq!(ready["runtime"], "RUST_NODE_DAEMON_V1");
        let db = clinx_decision_kernel::node_registry::Registry::default();
        db.invoke("registry.init", &json!({"path":registry}))
            .unwrap();
        let record = eventually(
            || {
                db.invoke("registry.get", &json!({"node_id":"air"}))
                    .unwrap()
            },
            |v| v["authorized"] == true,
        );
        assert_eq!(record["public_key_fingerprint"], node["fingerprint"]);
        let scope = db
            .invoke(
                "registry.scope",
                &json!({"node_id":"air","user_scope":"fixture"}),
            )
            .unwrap();
        assert_eq!(scope["execute_tasks"], true);
        assert_eq!(scope["projects"], json!([r.repo]));
        // Registration can commit before the Helper has consumed its effective scope.
        eventually(
            || {
                node_tls::call(
                    &centre,
                    "air",
                    record["endpoint"].as_str().unwrap(),
                    2.0,
                    &json!({"operation":"node.status","user_scope":"fixture"}),
                )
                .ok()
            },
            |v| {
                v.as_ref()
                    .is_some_and(|v| v["scope"]["execute_tasks"] == true)
            },
        );
        Self {
            reverse: Reverse {
                root: air,
                endpoint,
                scope,
            },
            fingerprint: node["fingerprint"].clone(),
            _helper: helper_child,
            _centre: centre_child,
        }
    }
}
