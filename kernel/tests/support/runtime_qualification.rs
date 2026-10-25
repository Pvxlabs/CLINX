//! Real isolated Authority/Transport children shared by qualification ports.
#![allow(dead_code)]
use clinx_decision_kernel::native_transport::{Authority, UnixAuthority};
use serde_json::{Value, json};
use std::{
    cell::RefCell,
    fs,
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    process::{Child, Command},
    sync::atomic::{AtomicUsize, Ordering},
    thread,
    time::{Duration, Instant},
};

pub fn eventually<T: std::fmt::Debug>(
    mut read: impl FnMut() -> T,
    predicate: impl Fn(&T) -> bool,
) -> T {
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        let value = read();
        if predicate(&value) {
            return value;
        }
        assert!(
            Instant::now() < deadline,
            "condition did not become true: {value:?}"
        );
        thread::sleep(Duration::from_millis(30));
    }
}
pub fn git_repo(path: &std::path::Path) {
    for args in [
        vec!["init", "-q"],
        vec![
            "-c",
            "user.name=CLINX Qualification",
            "-c",
            "user.email=qualification@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        ],
    ] {
        fs::create_dir_all(path).unwrap();
        assert!(
            Command::new("git")
                .current_dir(path)
                .args(args)
                .status()
                .unwrap()
                .success()
        );
    }
}
pub struct Runtime {
    pub temp: tempfile::TempDir,
    pub repo: PathBuf,
    pub repo2: PathBuf,
    pub endpoint: PathBuf,
    pub policy: PathBuf,
    pub config: PathBuf,
    pub provider_path: PathBuf,
    pub host: String,
    pub processes: RefCell<Vec<Child>>,
    pub authority: RefCell<usize>,
    counter: AtomicUsize,
}
impl Runtime {
    pub fn new(host: &str, host_enabled: bool) -> Self {
        let temp = tempfile::Builder::new().prefix("rt-").tempdir().unwrap();
        let root = temp.path();
        let repo = root.join("repo");
        let repo2 = root.join("repo2");
        git_repo(&repo);
        git_repo(&repo2);
        fs::create_dir(root.join("state")).unwrap();
        fs::set_permissions(root.join("state"), fs::Permissions::from_mode(0o700)).unwrap();
        let endpoint = root.join("a.sock");
        let provider_path = root.join("p.sock");
        let policy = root.join("policy.json");
        let config = root.join("host.json");
        let scope = Self::scope();
        fs::write(
            &policy,
            json!({"projects":[{"alias":"fixture","host":host,"cwd":repo},
            {"alias":"fixture2","host":host,"cwd":repo2}],"endpoints":[provider_path],
            "provider_routes":{host:[provider_path]},"lease_ms":120000,
            "host_operations":if host_enabled { vec![scope] } else { vec![] }})
            .to_string(),
        )
        .unwrap();
        fs::write(
            &config,
            json!({"enabled":true,"host":host,"trusted_workspace_roots":[repo,repo2]}).to_string(),
        )
        .unwrap();
        Self {
            temp,
            repo,
            repo2,
            endpoint,
            policy,
            config,
            provider_path,
            host: host.into(),
            processes: RefCell::new(vec![]),
            authority: RefCell::new(0),
            counter: AtomicUsize::new(0),
        }
    }
    pub fn scope() -> Value {
        json!({"capability":"LOCAL_HOST_PROCESS","operation":"development_command","operation_class":"DEVELOPMENT_MUTATION","target":""})
    }
    pub fn client(&self) -> UnixAuthority {
        UnixAuthority {
            endpoint: self.endpoint.clone(),
            uid: rustix::process::getuid().as_raw(),
        }
    }
    pub fn call_result(
        &self,
        op: &str,
        payload: Value,
        key: Option<&str>,
    ) -> Result<Value, String> {
        let id = format!("fixture-{}", self.counter.fetch_add(1, Ordering::SeqCst));
        self.client().call(op, payload, key.unwrap_or(&id))
    }
    pub fn call(&self, op: &str, payload: Value) -> Value {
        self.call_result(op, payload, None).unwrap()
    }
    fn spawn(&self, command: &mut Command, name: &str) -> usize {
        let n = self.processes.borrow().len();
        let log = fs::File::create(self.temp.path().join(format!("{name}-{n}.log"))).unwrap();
        let child = command
            .stdout(log.try_clone().unwrap())
            .stderr(log)
            .spawn()
            .unwrap();
        self.processes.borrow_mut().push(child);
        n
    }
    pub fn launch_authority(&self, init: bool) {
        let mut command = Command::new(env!("CARGO_BIN_EXE_clinx-runtime"));
        command
            .arg("--state-dir")
            .arg(self.temp.path().join("state"))
            .arg("--socket")
            .arg(&self.endpoint)
            .arg("--policy")
            .arg(&self.policy)
            .arg("--qualification")
            .arg("--caller-uid")
            .arg(rustix::process::getuid().as_raw().to_string())
            .arg("--transport-uid")
            .arg(rustix::process::getuid().as_raw().to_string());
        if init {
            command.arg("--init");
        }
        let index = self.spawn(&mut command, "authority");
        *self.authority.borrow_mut() = index;
        eventually(
            || {
                assert!(
                    self.processes.borrow_mut()[index]
                        .try_wait()
                        .unwrap()
                        .is_none(),
                    "Authority failed: {}",
                    fs::read_to_string(self.temp.path().join(format!("authority-{index}.log")))
                        .unwrap()
                );
                self.client().call("health", json!({}), "health").ok()
            },
            |v| v.as_ref().is_some_and(|v| v["business_commands"] == true),
        );
    }
    pub fn kill(&self, index: usize) {
        let mut children = self.processes.borrow_mut();
        let c = &mut children[index];
        c.kill().unwrap();
        c.wait().unwrap();
    }
    pub fn restart_authority(&self) {
        let index = *self.authority.borrow();
        self.kill(index);
        self.launch_authority(false);
    }
    pub fn worker(&self) -> Command {
        let mut command = Command::new(env!("CARGO_BIN_EXE_clinx-native-transport"));
        command
            .arg("--socket")
            .arg(&self.endpoint)
            .arg("--authority-uid")
            .arg(rustix::process::getuid().as_raw().to_string())
            .args(["--timeout-seconds", "2"]);
        command
    }
    pub fn launch_worker(&self) -> usize {
        self.spawn(
            self.worker().arg("--host-config").arg(&self.config),
            "worker",
        )
    }
    pub fn tick(&self, extra: &[String]) -> Value {
        let output = self.worker().arg("--once").args(extra).output().unwrap();
        assert!(
            output.status.success(),
            "transport: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        serde_json::from_slice(&output.stdout).unwrap()
    }
    pub fn start(&self, task: Option<&Value>, project: &str) -> Value {
        let mut p = json!({"host":self.host,"project":project,"prompt":"isolated Host operation","title":"Host fixture","approved":true,
            "execution_policy":{"execution_surface":"HOST_EXECUTOR","operation_scopes":[Self::scope()]}});
        if let Some(task) = task {
            p["task_ref"] = task.clone();
        }
        let p = self.call("prepare", p);
        self.call(
            "start",
            json!({"prepared_execution_ref":p["prepared_execution_ref"],"approved":true}),
        )
    }
    pub fn status(&self, e: &Value) -> Value {
        self.call("get_status", json!({"execution_ref":e["execution_ref"]}))
    }
    pub fn rows(&self, e: &Value) -> Vec<Value> {
        self.call("host_rows", json!({"execution_ref":e["execution_ref"]}))["rows"]
            .as_array()
            .unwrap()
            .clone()
    }
    pub fn wait(&self, e: &Value, state: &str) -> Value {
        eventually(|| self.status(e), |v| v["status"] == state)
    }
}
impl Drop for Runtime {
    fn drop(&mut self) {
        for child in self.processes.get_mut().iter_mut().rev() {
            if child.try_wait().ok().flatten().is_none() {
                if let Some(pid) = rustix::process::Pid::from_raw(child.id() as i32) {
                    let _ = rustix::process::kill_process(pid, rustix::process::Signal::TERM);
                }
                let _ = child.wait();
            }
        }
        if std::thread::panicking() {
            for entry in fs::read_dir(self.temp.path()).unwrap().flatten() {
                if entry.path().extension().is_some_and(|e| e == "log") {
                    eprintln!(
                        "{}:\n{}",
                        entry.path().display(),
                        fs::read_to_string(entry.path()).unwrap_or_default()
                    );
                }
            }
        }
    }
}
pub fn canonical_task(host: &str, tid: &str) -> String {
    use sha1::{Digest, Sha1};
    let mut digest = Sha1::new();
    digest.update([
        0x6b, 0xa7, 0xb8, 0x11, 0x9d, 0xad, 0x11, 0xd1, 0x80, 0xb4, 0, 0xc0, 0x4f, 0xd4, 0x30, 0xc8,
    ]);
    digest.update(json!([host, "codex_app_server", tid]).to_string());
    let mut b = digest.finalize();
    b[6] = (b[6] & 15) | 0x50;
    b[8] = (b[8] & 63) | 0x80;
    format!(
        "task_{}",
        b[..16]
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>()
    )
}
