//! Private real-process Node IPC harness. Callbacks are fixture IO, never outcomes
//! synthesized in place of the Rust service, router, registry or relay under test.
#![allow(dead_code)]
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    io::{BufRead, BufReader, Write},
    path::Path,
    process::{Child, ChildStdin, Command, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
        mpsc,
    },
    thread,
    time::Duration,
};
pub type Result<T> = std::result::Result<T, String>;
type Pending = Arc<Mutex<BTreeMap<u64, mpsc::Sender<Result<Value>>>>>;
type Input = Arc<Mutex<Option<ChildStdin>>>;
fn send(input: &Input, value: Value) -> Result<()> {
    let mut input = input.lock().unwrap();
    let input = input.as_mut().ok_or("WORKER_CLOSED")?;
    writeln!(input, "{value}").map_err(|e| e.to_string())?;
    input.flush().map_err(|e| e.to_string())
}
pub struct Worker {
    child: Mutex<Child>,
    input: Input,
    pending: Pending,
    sequence: AtomicU64,
    reader: Option<thread::JoinHandle<()>>,
}
impl Worker {
    pub fn new(callback: impl Fn(Value) -> Result<Value> + Send + Sync + 'static) -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_clinx-node"))
            .arg("--network-worker")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap();
        let input = Arc::new(Mutex::new(child.stdin.take()));
        let stdout = child.stdout.take().unwrap();
        let pending: Pending = Arc::new(Mutex::new(BTreeMap::new()));
        let (writes, replies, callback) = (input.clone(), pending.clone(), Arc::new(callback));
        let reader = thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                let Ok(line) = line else { break };
                let event: Value = serde_json::from_str(&line).expect("worker JSON");
                if event["kind"] == "callback" || event["kind"] == "incoming" {
                    let (callback, writes) = (callback.clone(), writes.clone());
                    thread::spawn(move || {
                        let result = callback(event.clone());
                        let result = if event["kind"] == "callback" {
                            match result {
                                Ok(v) => json!({"result":v}),
                                Err(e) => json!({"error":e}),
                            }
                        } else {
                            result.expect("incoming fixture dispatch")
                        };
                        let _ = send(
                            &writes,
                            json!({"operation":"reply","connection_id":event["connection_id"],"result":result}),
                        );
                    });
                } else {
                    let id = event["id"].as_u64().unwrap();
                    if let Some(tx) = replies.lock().unwrap().remove(&id) {
                        let _ = tx.send(if let Some(e) = event["error"].as_str() {
                            Err(e.into())
                        } else {
                            Ok(event["result"].clone())
                        });
                    }
                }
            }
            for (_, tx) in std::mem::take(&mut *replies.lock().unwrap()) {
                let _ = tx.send(Err("WORKER_CLOSED".into()));
            }
        });
        Self {
            child: Mutex::new(child),
            input,
            pending,
            sequence: AtomicU64::new(0),
            reader: Some(reader),
        }
    }
    pub fn plain() -> Self {
        Self::new(|e| Err(format!("UNEXPECTED_CALLBACK: {e}")))
    }
    pub fn call(&self, operation: &str, arguments: Value) -> Result<Value> {
        let id = self.sequence.fetch_add(1, Ordering::Relaxed) + 1;
        let (tx, rx) = mpsc::channel();
        self.pending.lock().unwrap().insert(id, tx);
        send(
            &self.input,
            json!({"id":id,"operation":operation,"arguments":arguments}),
        )?;
        rx.recv_timeout(Duration::from_secs(15))
            .expect("real worker IPC deadline")
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        self.input.lock().unwrap().take();
        assert!(self.child.lock().unwrap().wait().unwrap().success());
        self.reader.take().unwrap().join().unwrap();
    }
}
pub fn record(node: &str) -> Value {
    json!({"node_id":node,"user_scope":"alice","public_key_fingerprint":"","protocol_version":"clinx-node-v1","providers":["codex_app_server"],"capabilities":["session.read","session.status"],"route_ids":[],"display_name":"","endpoint":null,"state":"ONLINE","last_seen":clinx_decision_kernel::node_tls::now(),"stale_after_seconds":120,"trusted":true,"authorized":true,"version_compatible":true,"identity_generation":1})
}
pub fn scope(execute: bool) -> Value {
    json!({"user_scope":"alice","read_sessions":true,"execute_tasks":execute,"providers":["codex_app_server"],"revoked":false,"updated_at":clinx_decision_kernel::node_tls::now(),"projects":["*"]})
}
pub fn registry(path: &Path) -> Arc<Worker> {
    let w = Arc::new(Worker::plain());
    w.call("registry.init", json!({"path":path})).unwrap();
    w
}
// Independent deployed JSON wire oracle: sorted JSON object keys, ensure_ascii
// including UTF-16 surrogate pairs. Float legacy bytes use a literal vector.
pub fn ascii_wire(v: &Value) -> String {
    let mut result = String::new();
    for c in v.to_string().chars() {
        if c.is_ascii() && c != '\u{7f}' {
            result.push(c)
        } else {
            for u in c.encode_utf16(&mut [0; 2]) {
                result.push_str(&format!("\\u{u:04x}"));
            }
        }
    }
    result
}
pub fn registry_call(w: &Worker, op: &str, mut args: Value) -> Result<Value> {
    if op == "registry.claim_request" && args["payload_wire"].is_null() {
        args["payload_wire"] = json!(ascii_wire(&args["payload"]));
    }
    if op == "registry.touch" && args["state"].is_null() {
        args["state"] = json!("ONLINE");
    }
    let r = w.call(op, args)?;
    if let Some(e) = r.get("registry_error") {
        return if op == "registry.acquire_fence" {
            Ok(
                json!({"callback_error":{"code":e["error_code"],"reason":"Node index contract rejected","details":e["details"]}}),
            )
        } else {
            Err(e["error_code"].as_str().unwrap().into())
        };
    }
    Ok(r)
}
pub fn service(
    registry: Arc<Worker>,
    execute: impl Fn(&str, Value) -> Result<Value> + Send + Sync + 'static,
) -> Worker {
    Worker::new(move |e| {
        let op = e["operation"].as_str().unwrap();
        let a = e["arguments"].clone();
        if op.starts_with("registry.") {
            registry_call(&registry, op, a)
        } else {
            assert_eq!(op, "execute");
            execute(a["operation"].as_str().unwrap(), a["request"].clone())
        }
    })
}
pub fn service_args(op: &str, request: Value, available: &[&str]) -> Value {
    // Match NodeRecord('air') and SharingScope('alice', execute_tasks=True)
    // used by the source NodeService cases, including their read denial.
    let mut node = record("air");
    node["user_scope"] = json!("default");
    node["state"] = json!("UNKNOWN");
    node["last_seen"] = Value::Null;
    let mut sharing = scope(true);
    sharing["read_sessions"] = json!(false);
    let mut a = json!({"operation":op,"request":request,"record":node,"scope":sharing,"authority_owned":false,"available":{"session.read":true,"session.status":true}});
    for op in available {
        a["available"][*op] = json!(true);
    }
    a
}
pub fn service_call(w: &Worker, op: &str, request: Value, available: &[&str]) -> Result<Value> {
    w.call("service.handle", service_args(op, request, available))
}
