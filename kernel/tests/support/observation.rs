//! Literal qualification fixtures ported from test_rust_observation.py.
#![allow(dead_code)]
use clinx_decision_kernel::{
    native_history::{ContextLimits, History},
    node_registry::Registry,
    observation,
    observation_runtime::Source,
};
use rusqlite::{Connection, params};
use serde_json::{Value, json};
use std::{
    cell::RefCell,
    collections::BTreeMap,
    fs,
    io::{BufRead, BufReader, Write},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, ChildStdout, Command, Stdio},
};
pub const TID: &str = "01a0fa4e-11e7-7540-91c8-1eee45d1a4d0";
pub type Result<T> = std::result::Result<T, String>;
pub fn scope() -> Value {
    json!({"user_scope":"alice","read_sessions":true,"projects":["/approved"],"updated_at":"2026-01-01T00:00:00+00:00"})
}
pub fn snapshot(root: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
    fn visit(root: &Path, at: &Path, out: &mut BTreeMap<PathBuf, Vec<u8>>) {
        for entry in fs::read_dir(at).unwrap() {
            let p = entry.unwrap().path();
            if p.is_dir() {
                visit(root, &p, out)
            } else if !p.to_string_lossy().ends_with("-wal")
                && !p.to_string_lossy().ends_with("-shm")
            {
                out.insert(
                    p.strip_prefix(root).unwrap().to_owned(),
                    fs::read(&p).unwrap(),
                );
            }
        }
    }
    let mut result = BTreeMap::new();
    visit(root, root, &mut result);
    result
}
pub struct Native {
    pub temp: tempfile::TempDir,
    pub root: PathBuf,
    pub rollout: PathBuf,
    pub history: History,
    pub metadata: Value,
}
impl Native {
    pub fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("native");
        fs::create_dir(&root).unwrap();
        let rollout = root.join(format!("rollout-fixture-{TID}.jsonl"));
        let mut bytes = Vec::new();
        writeln!(
            bytes,
            "{}",
            json!({"type":"session_meta","payload":{"id":TID,"cwd":"/approved"}})
        )
        .unwrap();
        let mut offsets = vec![];
        for n in 0..3 {
            offsets.push(bytes.len());
            for row in [
                json!({"type":"event_msg","payload":{"type":"task_started","turn_id":format!("turn-{n}")}}),
                json!({"type":"turn_context","payload":{"turn_id":format!("turn-{n}"),"cwd":"/approved","model":format!("model-{n}")}}),
            ] {
                writeln!(bytes, "{row}").unwrap();
            }
        }
        fs::write(&rollout, &bytes).unwrap();
        let c = Connection::open(root.join("state_5.sqlite")).unwrap();
        c.execute_batch("CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT,rollout_path TEXT,history_mode TEXT,name TEXT,updated_at INT);").unwrap();
        c.execute(
            "INSERT INTO threads VALUES(?,?,?,?,?,?)",
            params![
                TID,
                "/approved",
                rollout.to_str().unwrap(),
                "paginated",
                "Native",
                10
            ],
        )
        .unwrap();
        drop(c);
        let c = Connection::open(root.join("thread_history_1.sqlite")).unwrap();
        c.execute_batch("CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,rollout_ordinal INT,status TEXT,started_at INT,completed_at INT,rollout_byte_offset INT,PRIMARY KEY(thread_id,turn_id)); CREATE INDEX turns_page ON thread_turns(thread_id,rollout_ordinal); CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,rollout_ordinal INT,created_at_ms INT,item_type TEXT,item_json TEXT,PRIMARY KEY(thread_id,turn_id,item_id)); CREATE INDEX items_page ON thread_items(thread_id,turn_id,rollout_ordinal); CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY,next_rollout_byte_offset INT);").unwrap();
        for (n, offset) in offsets.iter().enumerate() {
            let turn = format!("turn-{n}");
            c.execute(
                "INSERT INTO thread_turns VALUES(?,?,?,?,?,?,?)",
                params![
                    TID,
                    turn,
                    n as i64,
                    if n == 2 { "inProgress" } else { "completed" },
                    n as i64,
                    if n == 2 { None } else { Some(n as i64 + 1) },
                    *offset as i64
                ],
            )
            .unwrap();
            let items = [
                json!({"type":"userMessage","content":[{"type":"text","text":format!("User {n}")}]}),
                json!({"type":"agentMessage","text":format!("Answer {n}"),"phase":"final_answer"}),
                json!({"type":"agentMessage","text":"NEVER_ANALYSIS","channel":"analysis"}),
                json!({"type":"reasoning","text":"NEVER_REASONING"}),
                json!({"type":"commandExecution","text":"NEVER_TOOL"}),
            ];
            for (j, item) in items.iter().enumerate() {
                c.execute(
                    "INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)",
                    params![
                        TID,
                        turn,
                        j.to_string(),
                        (n * 10 + j) as i64,
                        n as i64,
                        item["type"].as_str().unwrap(),
                        item.to_string()
                    ],
                )
                .unwrap();
            }
        }
        c.execute(
            "INSERT INTO thread_history_projection_state VALUES(?,?)",
            params![TID, bytes.len() as i64],
        )
        .unwrap();
        drop(c);
        let history = History::new(root.to_str().unwrap());
        let metadata = history.metadata(TID).unwrap();
        Self {
            temp,
            root,
            rollout,
            history,
            metadata,
        }
    }
    pub fn context(
        &self,
        m: &Value,
        recent: usize,
        budget: usize,
        cursor: &Value,
    ) -> Result<Value> {
        self.history.context(
            TID,
            "air",
            m,
            ContextLimits { recent, budget },
            &Value::Null,
            cursor,
        )
    }
    pub fn source(&self) -> Source {
        Source::new(json!({"native_home":self.root,"node_id":"air","owner_host":"air"})).unwrap()
    }
}
pub fn event(seq: u64, tid: &str, turn: &str, ordinal: u64) -> Value {
    json!({"seq":seq,"native_thread_id":tid,"provider":"codex_app_server","project":"/approved","title":"简短标题\nsecond line","source_generation":"generation-1","turn":{"turn_id":turn,"ordinal":ordinal,"native_state":"COMPLETED","summary":"公开结果"}})
}
pub fn record(node: &str) -> Value {
    json!({"node_id":node,"user_scope":"alice","public_key_fingerprint":"","protocol_version":"clinx-node-v1","providers":["codex_app_server"],"capabilities":[],"route_ids":[],"display_name":"Air","endpoint":null,"state":"ONLINE","last_seen":clinx_decision_kernel::node_tls::now(),"stale_after_seconds":45,"trusted":true,"authorized":true,"version_compatible":true,"identity_generation":1})
}
pub struct Directory {
    pub temp: tempfile::TempDir,
    pub path: PathBuf,
    pub registry_path: PathBuf,
    pub registry: Registry,
    pub scopes: RefCell<BTreeMap<String, Value>>,
}
impl Directory {
    pub fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("observations.db");
        let registry_path = temp.path().join("nodes.db");
        observation::init(&path).unwrap();
        let registry = Registry::default();
        registry
            .invoke("registry.init", &json!({"path":registry_path}))
            .unwrap();
        registry
            .invoke(
                "registry.register",
                &json!({"record":record("air"),"scope":scope()}),
            )
            .unwrap();
        Self {
            temp,
            path,
            registry_path,
            registry,
            scopes: RefCell::new(BTreeMap::from([("air".into(), scope())])),
        }
    }
    pub fn accept(&self, node: &str, stream: &str, events: Value) -> Result<Value> {
        observation::accept(
            &json!({"path":self.path,"node":node,"scope":scope(),"request":{"stream_id":stream,"events":events}}),
        )
    }
    pub fn read(&self, method: &str, request: Value) -> Result<Value> {
        self.read_with(method, request, json!({}), |_| {
            Err("UNEXPECTED_REMOTE".into())
        })
    }
    pub fn read_with(
        &self,
        method: &str,
        request: Value,
        extra: Value,
        mut remote: impl FnMut(Value) -> Result<Value>,
    ) -> Result<Value> {
        let mut a = json!({"path":self.path,"user_scope":"alice","request":request,"remote":true});
        a.as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        observation::invoke(&format!("observation.{method}"), &a, |op, args| {
            let registry = |method: &str, arguments: Value| {
                Registry::read(
                    &json!({"path":self.registry_path,"method":format!("registry.{method}"),"arguments":arguments}),
                )
            };
            match op {
                "directory.nodes" => registry("list_nodes", json!({"user_scope":"alice"})),
                "directory.node" => {
                    let node = args["node_id"].as_str().unwrap();
                    Ok(
                        json!({"record":registry("get",args.clone())?,"scope":registry("scope",json!({"node_id":node,"user_scope":"alice"}))?,"approval":self.scopes.borrow().get(node),"peer":{"trust_state":"TRUSTED","security_status":"OPAQUE_V1"}}),
                    )
                }
                "directory.remote" => remote(args),
                _ => Err("UNEXPECTED_CALLBACK".into()),
            }
        })
    }
    pub fn count(&self, table: &str) -> i64 {
        Connection::open(&self.path)
            .unwrap()
            .query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0))
            .unwrap()
    }
}
/// Real child protocol, including abrupt process loss at a callback boundary.
pub struct Worker {
    pub child: Child,
    input: ChildStdin,
    output: BufReader<ChildStdout>,
    seq: u64,
}
impl Worker {
    pub fn new() -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_clinx-node"))
            .arg("--network-worker")
            .env_clear()
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let input = child.stdin.take().unwrap();
        let output = BufReader::new(child.stdout.take().unwrap());
        Self {
            child,
            input,
            output,
            seq: 0,
        }
    }
    pub fn call(
        &mut self,
        op: &str,
        args: Value,
        mut cb: impl FnMut(&str, Value) -> Result<Option<Value>>,
    ) -> Result<Value> {
        self.seq += 1;
        writeln!(
            self.input,
            "{}",
            json!({"id":self.seq,"operation":op,"arguments":args})
        )
        .unwrap();
        self.input.flush().unwrap();
        loop {
            if self.output.buffer().is_empty() {
                let ready = rustix::event::poll(
                    &mut [rustix::event::PollFd::new(
                        self.output.get_ref(),
                        rustix::event::PollFlags::IN,
                    )],
                    Some(&rustix::event::Timespec {
                        tv_sec: 10,
                        tv_nsec: 0,
                    }),
                )
                .map_err(|_| "WORKER_UNAVAILABLE")?;
                if ready == 0 {
                    return Err("WORKER_RESPONSE_TIMEOUT".into());
                }
            }
            let mut line = String::new();
            let count = self
                .output
                .read_line(&mut line)
                .map_err(|_| "WORKER_UNAVAILABLE")?;
            if count == 0 {
                return Err("WORKER_UNAVAILABLE".into());
            }
            let frame: Value = serde_json::from_str(&line).unwrap();
            if frame["kind"] == "callback" {
                let reply = match cb(
                    frame["operation"].as_str().unwrap(),
                    frame["arguments"].clone(),
                ) {
                    Ok(Some(v)) => json!({"result":v}),
                    Err(e) => json!({"error":e}),
                    Ok(None) => {
                        self.child.kill().unwrap();
                        self.child.wait().unwrap();
                        return Err("WORKER_UNAVAILABLE".into());
                    }
                };
                writeln!(self.input,"{}",json!({"operation":"reply","connection_id":frame["connection_id"],"result":reply})).unwrap();
                self.input.flush().unwrap();
            } else {
                assert_eq!(frame["id"], self.seq);
                return if let Some(e) = frame["error"].as_str() {
                    Err(e.into())
                } else {
                    Ok(frame["result"].clone())
                };
            }
        }
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}
