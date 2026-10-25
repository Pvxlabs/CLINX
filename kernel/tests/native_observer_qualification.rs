//! Isolated native Observer qualification, migrated from the original Python cases.
#[path = "support/observer_fixture.rs"]
mod fixture;
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use clinx_decision_kernel::{
    native_history, native_observer_bridge::Runtime, node_tls::Result, observation_runtime::Source,
    observation_source,
};
use fixture::{Callback, Server, http};
use hmac::{Hmac, Mac};
use rusqlite::{Connection, params};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::{BufRead, BufReader, Write},
    os::unix::net::UnixListener,
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{SystemTime, UNIX_EPOCH},
};
use tempfile::TempDir;
const TID: &str = "01a0fa4e-11e7-7540-91c8-1eee45d1a4d0";
const TURN: &str = "00000000-0000-0000-0000-000000000002";
fn scope(project: &str) -> Value {
    json!({"user_scope":"alice","read_sessions":true,"execute_tasks":false,"providers":["codex_app_server"],"projects":[project],"revoked":false,"updated_at":""})
}
fn route() -> Value {
    json!({"host":{"stable_identifier":"p620"},"provider":{"stable_identifier":"codex_app_server"},"conversation":{"binding":TID}})
}
fn execution() -> Value {
    json!({"execution_ref":"exec-fixture","turn_id":TURN,"routing_identity_json":route().to_string()})
}
fn invoke(runtime: &Runtime, op: &str, a: &Value) -> Result<Value> {
    runtime.invoke(&format!("native_observer.{op}"), a, |op, _| {
        panic!("unexpected callback {op}")
    })
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
fn unhex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}
fn snapshot(root: &Path) -> BTreeMap<PathBuf, String> {
    fn scan(root: &Path, p: &Path, out: &mut BTreeMap<PathBuf, String>) {
        for e in fs::read_dir(p).unwrap() {
            let p = e.unwrap().path();
            if p.is_dir() {
                scan(root, &p, out)
            } else if !["-wal", "-shm"]
                .iter()
                .any(|suffix| p.to_string_lossy().ends_with(suffix))
            {
                out.insert(
                    p.strip_prefix(root).unwrap().into(),
                    hex(&Sha256::digest(fs::read(p).unwrap())),
                );
            }
        }
    }
    let mut out = BTreeMap::new();
    scan(root, root, &mut out);
    out
}
struct Native {
    root: TempDir,
    source: Source,
}
impl Native {
    fn new() -> Self {
        let root = tempfile::tempdir().unwrap();
        let rollout = root.path().join(format!("rollout-fixture-{TID}.jsonl"));
        let mut raw = Vec::new();
        writeln!(
            raw,
            "{}",
            json!({"type":"session_meta","payload":{"id":TID,"cwd":"/approved"}})
        )
        .unwrap();
        let mut offsets = vec![];
        for n in 0..3 {
            offsets.push(raw.len());
            for row in [
                json!({"type":"event_msg","payload":{"type":"task_started","turn_id":format!("turn-{n}")}}),
                json!({"type":"turn_context","payload":{"turn_id":format!("turn-{n}"),"cwd":"/approved","model":format!("model-{n}")}}),
            ] {
                writeln!(raw, "{row}").unwrap();
            }
        }
        fs::write(&rollout, &raw).unwrap();
        let c = Connection::open(root.path().join("state_5.sqlite")).unwrap();
        c.execute_batch("CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT,rollout_path TEXT,history_mode TEXT,name TEXT,updated_at INT)").unwrap();
        c.execute(
            "INSERT INTO threads VALUES(?,?,?,?,?,?)",
            params![
                TID,
                "/approved",
                rollout.to_str().unwrap(),
                "paginated",
                "Native",
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_secs()
            ],
        )
        .unwrap();
        drop(c);
        let c = Connection::open(root.path().join("thread_history_1.sqlite")).unwrap();
        c.execute_batch("CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,rollout_ordinal INT,status TEXT,started_at INT,completed_at INT,rollout_byte_offset INT,PRIMARY KEY(thread_id,turn_id));CREATE INDEX turns_page ON thread_turns(thread_id,rollout_ordinal);CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,rollout_ordinal INT,created_at_ms INT,item_type TEXT,item_json TEXT,PRIMARY KEY(thread_id,turn_id,item_id));CREATE INDEX items_page ON thread_items(thread_id,turn_id,rollout_ordinal);CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY,next_rollout_byte_offset INT);").unwrap();
        for (n, offset) in offsets.iter().enumerate() {
            c.execute(
                "INSERT INTO thread_turns VALUES(?,?,?,?,?,?,?)",
                params![
                    TID,
                    format!("turn-{n}"),
                    n,
                    if n == 2 { "inProgress" } else { "completed" },
                    n,
                    if n == 2 { None } else { Some(n + 1) },
                    offset
                ],
            )
            .unwrap();
            for(j,item)in [json!({"type":"userMessage","content":[{"type":"text","text":format!("User {n}")}]}),json!({"type":"agentMessage","text":format!("Answer {n}"),"phase":"final_answer"}),json!({"type":"agentMessage","text":"NEVER_ANALYSIS","channel":"analysis"}),json!({"type":"reasoning","text":"NEVER_REASONING"}),json!({"type":"commandExecution","text":"NEVER_TOOL"})].iter().enumerate(){c.execute("INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)",params![TID,format!("turn-{n}"),j.to_string(),n*10+j,n,item["type"].as_str().unwrap(),item.to_string()]).unwrap();}
        }
        c.execute(
            "INSERT INTO thread_history_projection_state VALUES(?,?)",
            params![TID, raw.len()],
        )
        .unwrap();
        drop(c);
        let source=Source::new(json!({"native_home":root.path(),"node_id":"p620","owner_host":"p620","native_writer_observation":true})).unwrap();
        Self { root, source }
    }
    fn history(&self, op: &str, mut a: Value) -> Result<Value> {
        a["root"] = json!(self.root.path());
        native_history::invoke(&format!("history.{op}"), &a)
    }
    fn metadata(&self) -> Value {
        self.history("metadata", json!({"tid":TID})).unwrap()
    }
}
struct Bridge {
    native: Native,
    runtime: Runtime,
    scope: Mutex<Value>,
    revoke_sample: AtomicBool,
}
impl Bridge {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            native: Native::new(),
            runtime: Runtime::default(),
            scope: Mutex::new(scope("/approved")),
            revoke_sample: AtomicBool::new(false),
        })
    }
    fn callback(&self, op: &str, a: Value) -> Result<Value> {
        match op {
            "native_observer.facts" => Ok(
                json!({"local_identity":{"node_id":"p620"},"native_home":self.native.root.path(),"scope":*self.scope.lock().unwrap()}),
            ),
            "native_observer.metadata" => self.native.history("metadata", a),
            "native_observer.generation" => self.native.source.invoke("generation", &a),
            "native_observer.inventory" => self.native.source.invoke("inventory", &a),
            "native_observer.sample" => {
                let r = self.native.source.invoke("sample", &a)?;
                if self.revoke_sample.load(Ordering::Acquire) {
                    *self.scope.lock().unwrap() = Value::Null;
                }
                Ok(r)
            }
            _ => panic!("unexpected {op}"),
        }
    }
    fn call(&self, op: &str, a: &Value) -> Result<Value> {
        self.runtime
            .invoke(&format!("native_observer.{op}"), a, |op, a| {
                self.callback(op, a)
            })
    }
    fn sample(&self, checkpoint: Value) -> Result<Value> {
        self.call("sample", &json!({"tid":TID,"checkpoint":checkpoint}))
    }
    fn key(&self, bytes: &[u8]) {
        invoke(&self.runtime, "key", &json!({"value":hex(bytes)})).unwrap();
    }
}
#[test]
fn checkpoint_contract_tampering_scope_generation_and_readonly() {
    let b = Bridge::new();
    let before = snapshot(b.native.root.path());
    let first = b.sample(Value::Null).unwrap();
    let key = unhex(
        invoke(&b.runtime, "key", &json!({}))
            .unwrap()
            .as_str()
            .unwrap(),
    );
    let (wire, signature) = first["checkpoint"]
        .as_str()
        .unwrap()
        .split_once('.')
        .unwrap();
    let raw = URL_SAFE_NO_PAD.decode(wire).unwrap();
    let mut h = Hmac::<Sha256>::new_from_slice(&key).unwrap();
    h.update(&raw);
    assert_eq!(hex(&h.finalize().into_bytes()), signature);
    let mut saved: Value = serde_json::from_slice(&raw).unwrap();
    assert_eq!(saved["binding"][0], TID);
    assert!(!saved["state"].as_object().unwrap().is_empty());
    assert_eq!(
        b.sample(first["checkpoint"].clone()).unwrap()["thread_id"],
        TID
    );
    for token in [
        format!("{}x", first["checkpoint"].as_str().unwrap()),
        format!("{wire}.fake"),
        format!("{wire}.{}", "中".repeat(64)),
    ] {
        assert_eq!(
            b.sample(json!(token)).unwrap_err(),
            "NATIVE_COLLECTOR_CURSOR_RESET"
        );
    }
    saved["expires"] = json!(0);
    let raw = serde_json::to_vec(&saved).unwrap();
    let mut h = Hmac::<Sha256>::new_from_slice(&key).unwrap();
    h.update(&raw);
    let expired = format!(
        "{}.{}",
        URL_SAFE_NO_PAD.encode(raw),
        hex(&h.finalize().into_bytes())
    );
    assert_eq!(
        b.sample(json!(expired)).unwrap_err(),
        "NATIVE_COLLECTOR_CURSOR_RESET"
    );
    b.key(b"new-lifetime");
    assert_eq!(
        b.sample(first["checkpoint"].clone()).unwrap_err(),
        "NATIVE_COLLECTOR_CURSOR_RESET"
    );
    *b.scope.lock().unwrap() = scope("/foreign");
    assert_eq!(b.sample(Value::Null).unwrap_err(), "PROJECT_SCOPE_DENIED");
    *b.scope.lock().unwrap() = Value::Null;
    assert_eq!(
        b.call("inventory", &json!({"audit":false})).unwrap_err(),
        "SHARING_SCOPE_DENIED"
    );
    assert_eq!(snapshot(b.native.root.path()), before);
}
#[test]
fn scope_revocation_during_read_is_rechecked() {
    let b = Bridge::new();
    b.call("inventory", &json!({"audit":false})).unwrap();
    b.revoke_sample.store(true, Ordering::Release);
    assert_eq!(b.sample(Value::Null).unwrap_err(), "SHARING_SCOPE_DENIED");
}
fn quote(s: &str) -> String {
    s.bytes()
        .map(|b| {
            if b.is_ascii_alphanumeric() || b"-._~".contains(&b) {
                (b as char).to_string()
            } else {
                format!("%{b:02X}")
            }
        })
        .collect()
}
struct Remote {
    runtime: Runtime,
    address: std::net::SocketAddr,
    credential: PathBuf,
}
impl Remote {
    fn call(&self, op: &str, a: &Value) -> Result<Value> {
        self.runtime
            .invoke(&format!("native_observer.{op}"), a, |op, a| match op {
                "native_observer.read" => self.call("remote_read", &a),
                "native_observer.http" => {
                    let params = a["params"]
                        .as_object()
                        .unwrap()
                        .iter()
                        .map(|(k, v)| {
                            format!(
                                "{}={}",
                                quote(k),
                                quote(
                                    &v.as_str()
                                        .map(str::to_owned)
                                        .unwrap_or_else(|| v.to_string())
                                )
                            )
                        })
                        .collect::<Vec<_>>()
                        .join("&");
                    let token = fs::read_to_string(&self.credential).unwrap();
                    let r = http(
                        self.address,
                        "GET",
                        &format!(
                            "/v2/native-collector/{}?{params}",
                            a["path"].as_str().unwrap()
                        ),
                        &[("Authorization", &format!("Bearer {}", token.trim()))],
                        &[],
                    );
                    assert_eq!(r.headers["Cache-Control"], "no-store");
                    Ok(json!({"status":r.status,"body":String::from_utf8(r.body).unwrap()}))
                }
                _ => panic!("unexpected {op}"),
            })
    }
}
#[test]
fn authenticated_http_remote_checkpoint_reset_and_no_writes() {
    let b = Bridge::new();
    let before = snapshot(b.native.root.path());
    let token = "a".repeat(40);
    let home = tempfile::tempdir().unwrap();
    let credential = home.path().join("credential");
    fs::write(&credential, &token).unwrap();
    let owned = b.clone();
    let secret = token.clone();
    let cb: Callback = Arc::new(move |op, a| {
        Ok(match op {
            "observer_http.context" => {
                json!({"value":{"credential":secret,"network_configured":false}})
            }
            "observer_http.credential" => json!({"value":secret}),
            "observer_http.read" => match owned.call("route", &a["arguments"]) {
                Ok(value) => json!({"value":value}),
                Err(code) => json!({"fault":{"kind":"coded","code":code}}),
            },
            _ => panic!("unexpected {op}"),
        })
    });
    let server = Server::new(cb, false);
    let remote = Remote {
        runtime: Runtime::default(),
        address: server.address(),
        credential,
    };
    let rows = remote
        .call(
            "remote_inventory",
            &json!({"scope":scope("/approved"),"audit":false}),
        )
        .unwrap();
    assert_eq!(rows[2], "INDEXED");
    assert_eq!(rows[0][0]["id"], TID);
    let turns = remote
        .call("remote_turns", &json!({"metadata":rows[0][0],"state":{}}))
        .unwrap();
    assert_eq!(
        turns[0]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v["turn_id"].as_str().unwrap())
            .collect::<BTreeSet<_>>(),
        BTreeSet::from(["turn-0", "turn-1", "turn-2"])
    );
    b.key(b"restart-session");
    assert!(
        !remote
            .call(
                "remote_turns",
                &json!({"metadata":rows[0][0],"state":turns[2]})
            )
            .unwrap()[0]
            .as_array()
            .unwrap()
            .is_empty()
    );
    for (method, expected, credential) in [("GET", 401, "wrong"), ("POST", 405, token.as_str())] {
        assert_eq!(
            http(
                server.address(),
                method,
                "/v2/native-collector/inventory",
                &[("Authorization", &format!("Bearer {credential}"))],
                &[]
            )
            .status,
            expected
        );
    }
    for query in ["audit=2", "unexpected=1", "after=not-json"] {
        let r = http(
            server.address(),
            "GET",
            &format!("/v2/native-collector/inventory?{query}"),
            &[("Authorization", &format!("Bearer {token}"))],
            &[],
        );
        assert_eq!(r.value()["error"], "INVALID_QUERY");
    }
    *b.scope.lock().unwrap() = Value::Null;
    assert_eq!(
        remote
            .call(
                "remote_turns",
                &json!({"metadata":rows[0][0],"state":turns[2]})
            )
            .unwrap_err(),
        "SHARING_SCOPE_DENIED"
    );
    server.stop();
    drop(server);
    assert_eq!(snapshot(b.native.root.path()), before);
}
#[test]
fn remote_unavailable_cooldown_and_authority_claims() {
    let runtime = Runtime::default();
    let mut calls = 0;
    for _ in 0..56 {
        assert_eq!(
            runtime
                .invoke(
                    "native_observer.remote_read",
                    &json!({"path":"inventory","params":{}}),
                    |op, _| {
                        assert_eq!(op, "native_observer.http");
                        calls += 1;
                        Ok(json!({"transport_error":true}))
                    }
                )
                .unwrap_err(),
            "NATIVE_OBSERVER_UNAVAILABLE"
        );
    }
    assert_eq!(calls, 1);
    assert_eq!(runtime.invoke("native_observer.remote_turns",&json!({"metadata":{"id":TID,"cwd":"/approved"},"state":{}}),|op,_|{assert_eq!(op,"native_observer.read");Ok(json!({"thread_id":TID,"project":"/approved","checkpoint":"cursor","events":[{"native_thread_id":TID,"project":"/approved","task_ref":"forged","binding_evidence":"CANONICAL_ROUTE","turn":{"execution_ref":"fake"}}]}))}).unwrap_err(),"NATIVE_OBSERVATION_IDENTITY_CONFLICT");
}
struct Activity {
    root: TempDir,
    history: PathBuf,
    runtime: Runtime,
}
impl Activity {
    fn new() -> Self {
        let root = tempfile::tempdir().unwrap();
        let c = Connection::open(root.path().join("state_5.sqlite")).unwrap();
        c.execute_batch("CREATE TABLE threads(id TEXT PRIMARY KEY,history_mode TEXT)")
            .unwrap();
        c.execute(
            "INSERT INTO threads VALUES (?,?)",
            params![TID, "paginated"],
        )
        .unwrap();
        drop(c);
        let history = root.path().join("thread_history_1.sqlite");
        let c = Connection::open(&history).unwrap();
        c.execute_batch("CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT);CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,rollout_ordinal INT,updated_at_ordinal INT,created_at_ms INT,item_type TEXT,item_json TEXT);CREATE INDEX items_page ON thread_items(thread_id,turn_id,rollout_ordinal);CREATE INDEX items_update ON thread_items(thread_id,turn_id,updated_at_ordinal);").unwrap();
        c.execute("INSERT INTO thread_turns VALUES (?,?)", params![TID, TURN])
            .unwrap();
        Self {
            root,
            history,
            runtime: Runtime::default(),
        }
    }
    fn add(&self, n: i64, item: Value) {
        let mut value = json!({"text":format!("Feedback {n}"),"phase":"commentary"});
        value
            .as_object_mut()
            .unwrap()
            .extend(item.as_object().unwrap().clone());
        Connection::open(&self.history)
            .unwrap()
            .execute(
                "INSERT INTO thread_items VALUES(?,?,?,?,?,?,?,?)",
                params![
                    TID,
                    TURN,
                    format!("item{n}"),
                    n,
                    n,
                    n * 1000,
                    item["type"].as_str().unwrap_or("agentMessage"),
                    value.to_string()
                ],
            )
            .unwrap();
    }
    fn read(&self, mut a: Value) -> Result<Value> {
        a["root"] = json!(self.root.path());
        if a["task_ref"].is_null() {
            a["task_ref"] = json!("task-fixture");
        }
        a["execution"] = execution();
        invoke(&self.runtime, "activity_read", &a)
    }
}
#[test]
fn activity_pagination_revision_and_restart() {
    let a = Activity::new();
    for n in 1..92 {
        a.add(n, json!({}));
    }
    let first = a.read(json!({})).unwrap();
    assert_eq!(
        first["items"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| r["ordinal"].as_i64().unwrap())
            .collect::<Vec<_>>(),
        (52..92).collect::<Vec<_>>()
    );
    let older = a.read(json!({"before":first["older_cursor"]})).unwrap();
    assert_eq!(
        older["items"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| r["ordinal"].as_i64().unwrap())
            .collect::<Vec<_>>(),
        (12..52).collect::<Vec<_>>()
    );
    assert_eq!(
        a.read(json!({"before":older["older_cursor"]})).unwrap()["items"]
            .as_array()
            .unwrap()
            .len(),
        11
    );
    Connection::open(&a.history)
        .unwrap()
        .execute(
            "UPDATE thread_items SET item_json=?,updated_at_ordinal=92 WHERE item_id='item91'",
            params![json!({"text":"Revised","phase":"commentary"}).to_string()],
        )
        .unwrap();
    a.add(93, json!({}));
    let delta = a.read(json!({"after":first["next_cursor"]})).unwrap();
    assert_eq!(
        delta["items"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| r["id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["item91", "item93"]
    );
    assert_eq!(delta["items"][0]["text"], "Revised");
    for n in 94..144 {
        a.add(n, json!({}));
    }
    let delta2 = a.read(json!({"after":delta["next_cursor"]})).unwrap();
    assert_eq!(delta2["has_more"], true);
    assert_eq!(delta2["items"].as_array().unwrap().len(), 40);
    assert_eq!(
        a.read(json!({"after":delta2["next_cursor"]})).unwrap()["items"]
            .as_array()
            .unwrap()
            .len(),
        10
    );
    assert_eq!(invoke(&Runtime::default(),"activity_read",&json!({"root":a.root.path(),"task_ref":"task-fixture","execution":execution(),"after":first["next_cursor"]})).unwrap_err(),"ACTIVITY_CURSOR_RESET_REQUIRED");
}
#[test]
fn activity_scope_allowlist_redaction_and_readonly() {
    let a = Activity::new();
    a.add(1, json!({}));
    a.add(2, json!({"phase":"analysis","text":"NEVER_ANALYSIS"}));
    a.add(3, json!({"channel":"analysis","text":"NEVER_ANALYSIS"}));
    a.add(4, json!({"type":"commandExecution","text":"NEVER_TOOL"}));
    a.add(5,json!({"text":format!("secret=NEVER_SECRET Bearer NEVER_TOKEN sk-abcdefghijklmnopqrstuvwxyz {}","中".repeat(20000))}));
    a.add(
        6,
        json!({"text":format!("-----BEGIN PRIVATE KEY-----\nNEVER_PRIVATE{}","x".repeat(20000))}),
    );
    a.add(7, json!({"text":"Done","phase":"final_answer"}));
    let before = snapshot(a.root.path());
    let first = a.read(json!({})).unwrap();
    for secret in [
        "NEVER_ANALYSIS",
        "NEVER_TOOL",
        "NEVER_SECRET",
        "NEVER_TOKEN",
        "NEVER_PRIVATE",
        "sk-abcdefghijklmnopqrstuvwxyz",
    ] {
        assert!(!first.to_string().contains(secret));
    }
    let items = first["items"].as_array().unwrap();
    assert!(
        items
            .iter()
            .all(|r| r["text"].as_str().unwrap().len() <= 16384)
    );
    assert_eq!(items.last().unwrap()["kind"], "result");
    assert!(items.iter().any(|r| r["truncated"] == true));
    assert_eq!(snapshot(a.root.path()), before);
    for args in [
        json!({"after":"garbage"}),
        json!({"before":first["next_cursor"]}),
        json!({"task_ref":"task-other","after":first["next_cursor"]}),
    ] {
        assert_eq!(a.read(args).unwrap_err(), "ACTIVITY_CURSOR_RESET_REQUIRED");
    }
    Connection::open(&a.history)
        .unwrap()
        .execute("DELETE FROM thread_items", [])
        .unwrap();
    assert_eq!(
        a.read(json!({"after":first["next_cursor"]})).unwrap_err(),
        "ACTIVITY_CURSOR_RESET_REQUIRED"
    );
    fs::remove_file(&a.history).unwrap();
    assert_eq!(
        a.read(json!({})).unwrap()["reason"],
        "NATIVE_HISTORY_UNAVAILABLE"
    );
    assert!(!a.history.exists());
}
#[test]
fn activity_cursor_codec_and_text_match_public_contract() {
    let runtime = Runtime::default();
    let expected = URL_SAFE_NO_PAD.encode(br#"["\u5b57\ud83d\ude00", "after", 7, "item"]"#);
    assert_eq!(
        invoke(
            &runtime,
            "activity_encode",
            &json!({"scope":"字😀","mode":"after","position":[7,"item"]})
        )
        .unwrap(),
        expected
    );
    assert_eq!(
        invoke(
            &runtime,
            "activity_decode",
            &json!({"value":expected,"scope":"字😀","mode":"after"})
        )
        .unwrap(),
        json!([7, "item"])
    );
    for position in [
        json!([true, "item"]),
        json!([-1, "item"]),
        json!([9223372036854775808u64, "item"]),
        json!([1, "x".repeat(257)]),
    ] {
        let token = URL_SAFE_NO_PAD.encode(
            serde_json::to_vec(&json!(["scope", "after", position[0], position[1]])).unwrap(),
        );
        assert!(
            invoke(
                &runtime,
                "activity_decode",
                &json!({"value":token,"scope":"scope","mode":"after"})
            )
            .is_err()
        );
    }
    assert_eq!(
        invoke(
            &runtime,
            "activity_text",
            &json!({"value":"prefix\0 secret=bad Bearer token"})
        )
        .unwrap(),
        "prefix [REDACTED] Bearer [REDACTED]"
    );
}
#[test]
fn managed_context_requires_exact_provider_identity_and_projection_recheck() {
    let runtime = Runtime::default();
    let mut item = json!({"node_id":"p620","project":"/approved","native_thread_id":TID,"task_ref":"task-exact","turn":{"execution_ref":"exec-exact","turn_id":"turn-exact"},"source":"CLINX_MANAGED","binding_evidence":"CANONICAL_ROUTE"});
    let result = json!({"read_only":true,"thread_id":TID,"task_ref":"task-exact","execution_ref":"exec-exact","execution_turn_ref":"turn-exact","context_scope":"EXACT_EXECUTION_TURN"});
    let args = json!({"observation_id":"observation","runtime_host":"p620"});
    let mut calls = vec![];
    let out = runtime
        .invoke("native_observer.managed_context", &args, |op, a| match op {
            "managed.detail" => Ok(json!({"item":item})),
            "managed.exact_context" => {
                calls.push(a);
                Ok(result.clone())
            }
            _ => panic!("unexpected {op}"),
        })
        .unwrap();
    assert_eq!(out["schema_version"], "clinx-observation-v1");
    assert_eq!(calls.last().unwrap()["execution_ref"], "exec-exact");
    assert_eq!(calls.last().unwrap()["thread_id"], TID);
    for (key, value) in [
        ("read_only", json!(false)),
        ("thread_id", json!("wrong")),
        ("task_ref", json!("other")),
        ("execution_ref", json!("other")),
        ("execution_turn_ref", json!("other")),
        ("context_scope", json!("LATEST_THREAD")),
    ] {
        let mut wrong = result.clone();
        wrong[key] = value;
        assert_eq!(
            runtime
                .invoke("native_observer.managed_context", &args, |op, _| Ok(
                    if op == "managed.detail" {
                        json!({"item":item})
                    } else {
                        wrong.clone()
                    }
                ))
                .unwrap_err(),
            "EXECUTION_TURN_IDENTITY_CONFLICT"
        );
    }
    let mut reads = 0;
    assert_eq!(
        runtime
            .invoke("native_observer.managed_context", &args, |op, _| {
                if op == "managed.detail" {
                    reads += 1;
                    let mut current = item.clone();
                    if reads > 1 {
                        current["project"] = json!("/changed");
                    }
                    Ok(json!({"item":current}))
                } else {
                    Ok(result.clone())
                }
            })
            .unwrap_err(),
        "EXECUTION_TURN_IDENTITY_CONFLICT"
    );
    item["binding_evidence"] = json!("THREAD_UNBOUND");
    assert_eq!(
        runtime
            .invoke("native_observer.managed_context", &args, |_, _| Ok(
                json!({"item":item})
            ))
            .unwrap_err(),
        "EXACT_RUST_EXECUTION_REQUIRED"
    );
    item["node_id"] = json!("air");
    assert_eq!(
        runtime
            .invoke("native_observer.managed_context", &args, |op, _| Ok(
                if op == "managed.detail" {
                    json!({"item":item})
                } else {
                    assert_eq!(op, "managed.remote_context");
                    json!({"remote":true})
                }
            ))
            .unwrap(),
        json!({"remote":true})
    );
    item["node_id"] = json!("p620");
    item["source"] = json!("CODEX_NATIVE");
    assert_eq!(
        runtime
            .invoke("native_observer.managed_context", &args, |op, _| Ok(
                if op == "managed.detail" {
                    json!({"item":item})
                } else {
                    assert_eq!(op, "managed.desktop_context");
                    json!({"project":"/other"})
                }
            ))
            .unwrap_err(),
        "THREAD_SCOPE_MISMATCH"
    );
}
#[test]
fn existing_unix_mcp_read_transport_checks_identity_and_public_errors() {
    for fault in [
        None,
        Some("id"),
        Some("writable"),
        Some("public-error"),
        Some("truncated"),
    ] {
        let root = tempfile::tempdir_in("/tmp").unwrap();
        let socket = root.path().join("m.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        let handle = thread::spawn(move || {
            let (mut peer, _) = listener.accept().unwrap();
            let mut wire = String::new();
            BufReader::new(peer.try_clone().unwrap())
                .read_line(&mut wire)
                .unwrap();
            let request: Value = serde_json::from_str(&wire).unwrap();
            let result = if fault == Some("public-error") {
                json!({"isError":true,"structuredContent":{"error_code":"SHARING_SCOPE_DENIED"}})
            } else {
                json!({"structuredContent":{"read_only":fault!=Some("writable"),"items":[]}})
            };
            let reply = json!({"jsonrpc":"2.0","id":if fault==Some("id"){json!("wrong")}else{request["id"].clone()},"result":result});
            write!(
                peer,
                "{reply}{}",
                if fault == Some("truncated") { "" } else { "\n" }
            )
            .unwrap();
            request
        });
        let runtime = Runtime::default();
        assert_eq!(
            invoke(
                &runtime,
                "monitor_mcp",
                &json!({"endpoint":socket,"name":"clinx_create_task","arguments":{}})
            )
            .unwrap_err(),
            "OBSERVER_READ_ONLY"
        );
        let r = invoke(
            &runtime,
            "monitor_mcp",
            &json!({"endpoint":socket,"name":"clinx_list_observations","arguments":{"limit":1}}),
        );
        if let Some(f) = fault {
            assert_eq!(
                r.unwrap_err(),
                if f == "public-error" {
                    "SHARING_SCOPE_DENIED"
                } else {
                    "RUST_OBSERVATION_UNAVAILABLE"
                }
            );
        } else {
            assert_eq!(r.unwrap(), json!({"read_only":true,"items":[]}));
        }
        assert_eq!(
            handle.join().unwrap()["params"],
            json!({"name":"clinx_list_observations","arguments":{"limit":1}})
        );
    }
}
#[test]
fn monitor_v1_projection_requires_live_rust_reads() {
    let runtime = Runtime::default();
    let mut calls = vec![];
    let mut callback = |op: &str, a: Value| {
        assert_eq!(op, "monitor.read");
        calls.push(a["name"].clone());
        Ok(json!({"authority":"RUST","read_only":true}))
    };
    assert_eq!(
        runtime
            .invoke("native_observer.monitor_health", &json!({}), &mut callback)
            .unwrap()["observation_source"],
        "RUST_CENTRE_V2"
    );
    assert_eq!(
        runtime
            .invoke("native_observer.monitor_list", &json!({}), &mut callback)
            .unwrap()["items"],
        json!([])
    );
    assert_eq!(
        calls,
        vec![
            json!("clinx_get_capabilities"),
            json!("clinx_list_observations"),
            json!("clinx_list_observations")
        ]
    );
    assert_eq!(
        runtime
            .invoke("native_observer.monitor_health", &json!({}), |_, _| Ok(
                json!({"authority":"PYTHON"})
            ))
            .unwrap_err(),
        "RUST_AUTHORITY_REQUIRED"
    );
}
#[test]
fn native_desktop_display_does_not_promote_history_and_rechecks_scope() {
    let b = Bridge::new();
    let before = snapshot(b.native.root.path());
    let mut live = json!({"id":TID,"cwd":"/approved","status":{"type":"idle"}});
    let invoke_desktop = |endpoint: bool, context: bool, revoke: bool, live: &Value| {
        b.runtime.invoke("native_observer.desktop_observe",&json!({"thread_id":TID,"context":context,"recent_turns":8,"max_bytes":32000,"endpoint":if endpoint{json!("/fixture-read-only")}else{Value::Null}}),|op,mut a|match op{"desktop.facts"=>b.callback("native_observer.facts",a),"desktop.scope"=>Ok(if revoke{Value::Null}else{scope("/approved")}),"desktop.metadata"|"desktop.status"|"desktop.context"=>{a["tid"]=a["thread_id"].clone();a["host"]=a["node_id"].clone();b.native.history(op.strip_prefix("desktop.").unwrap(),a)},"desktop.writer"=>Ok(json!({"liveness":"UNKNOWN","reason":"NOT_OBSERVED"})),"desktop.live"=>Ok(json!({"thread":live})),_=>panic!("unexpected {op}")})
    };
    let result = invoke_desktop(false, true, false, &live).unwrap();
    assert_eq!(result["read_only"], true);
    assert!(result["task_ref"].is_null());
    assert_eq!(result["native_display_state"], "UNKNOWN");
    assert_eq!(result["codex_running"], false);
    assert!(result.to_string().contains("Answer 2"));
    for (recent, bytes) in [
        (json!(true), json!(32000)),
        (json!(0), json!(32000)),
        (json!(21), json!(32000)),
        (json!(8), json!(true)),
        (json!(8), json!(128001)),
    ] {
        assert_eq!(
            invoke(
                &b.runtime,
                "desktop_limits",
                &json!({"recent_turns":recent,"max_bytes":bytes})
            )
            .unwrap_err(),
            "INVALID_CONTEXT_LIMIT"
        );
    }
    assert_eq!(
        invoke(
            &b.runtime,
            "desktop_limits",
            &json!({"recent_turns":null,"max_bytes":null})
        )
        .unwrap(),
        json!([8, 32000])
    );
    assert_eq!(
        invoke_desktop(true, false, false, &live).unwrap()["native_display_state"],
        "IDLE"
    );
    live["cwd"] = json!("/different");
    assert_eq!(
        invoke_desktop(true, false, false, &live).unwrap_err(),
        "NATIVE_OBSERVATION_IDENTITY_CONFLICT"
    );
    live["cwd"] = json!("/approved");
    assert_eq!(
        invoke_desktop(true, true, true, &live).unwrap_err(),
        "SHARING_SCOPE_DENIED"
    );
    assert_eq!(snapshot(b.native.root.path()), before);
}

struct Managed {
    native: Native,
    runtime: Runtime,
    failure: Mutex<String>,
    code: Mutex<String>,
    unavailable: AtomicBool,
}
impl Managed {
    fn callback(&self, op: &str, a: Value) -> Result<Value> {
        match op {
            "managed.command" => {
                if a["operation"] == *self.failure.lock().unwrap() {
                    return Err(format!(
                        "SOURCE_AUTHORITY_ERROR:{}",
                        self.code.lock().unwrap()
                    ));
                }
                Ok(if a["operation"] == "observation_inventory" {
                    json!({"tasks":[]})
                } else {
                    json!({"executions":[{"id":"id-1","task_ref":"task-owned","execution_ref":"exec-owned","turn_id":"turn-2","created_at":"2026-10-25T00:00:00Z","created_at_ordinal":1792886400000i64,"status":"RUNNING","result":null}]})
                })
            }
            "managed.base_sample" => {
                let mut a = a;
                a["live_enabled"] = json!(false);
                a["canonical_db"] = json!(false);
                observation_source::invoke("source.sample", &a, |op, a| match op {
                    "source.native_turns" => self.native.source.invoke("native_turns", &a),
                    "source.canonical_turns" => self.call("canonical_turns", a),
                    "source.generation" => self.native.source.invoke("generation", &a),
                    _ => panic!("unexpected {op}"),
                })
            }
            _ => panic!("unexpected {op}"),
        }
    }
    fn call(&self, op: &str, mut a: Value) -> Result<Value> {
        a["owner_host"] = json!("p620");
        a["managed_inventory_unavailable"] = json!(self.unavailable.load(Ordering::Acquire));
        let value = self.runtime.invoke(
            &format!("native_observer.managed_source_{op}"),
            &a,
            |op, a| self.callback(op, a),
        )?;
        if op == "canonical_inventory" {
            self.unavailable.store(value[1] == true, Ordering::Release);
            Ok(value[0].clone())
        } else {
            Ok(value)
        }
    }
}
#[test]
fn managed_source_capacity_projection_recovery_and_exact_error() {
    let m = Managed {
        native: Native::new(),
        runtime: Runtime::default(),
        failure: Mutex::new("observation_inventory".into()),
        code: Mutex::new("RESPONSE_CAPACITY_EXCEEDED".into()),
        unavailable: AtomicBool::new(false),
    };
    assert_eq!(
        m.call(
            "canonical_inventory",
            json!({"scope":scope("/approved"),"after":""})
        )
        .unwrap(),
        json!([])
    );
    assert!(m.unavailable.load(Ordering::Acquire));
    let sampled = m
        .call("sample", json!({"metadata":m.native.metadata(),"state":{}}))
        .unwrap();
    assert!(
        sampled[0]
            .as_array()
            .unwrap()
            .iter()
            .all(|e| e["coverage"] == "RUST_MANAGED_PROJECTION_UNAVAILABLE")
    );
    assert_eq!(sampled[1]["active"], true);
    *m.failure.lock().unwrap() = "observation_turns".into();
    m.call(
        "canonical_inventory",
        json!({"scope":scope("/approved"),"after":""}),
    )
    .unwrap();
    let sampled = m
        .call("sample", json!({"metadata":m.native.metadata(),"state":{}}))
        .unwrap();
    assert_eq!(sampled[1]["rust_projection_unavailable"], true);
    assert!(
        sampled[0]
            .as_array()
            .unwrap()
            .iter()
            .all(|e| e["coverage"] == "RUST_MANAGED_PROJECTION_UNAVAILABLE")
    );
    *m.failure.lock().unwrap() = String::new();
    let recovered = m
        .call(
            "sample",
            json!({"metadata":m.native.metadata(),"state":sampled[1]}),
        )
        .unwrap();
    assert!(recovered[1].get("rust_projection_unavailable").is_none());
    let rows = recovered[0]
        .as_array()
        .unwrap()
        .iter()
        .filter(|e| e["turn"]["turn_id"] == "turn-2")
        .collect::<Vec<_>>();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["turn"]["execution_ref"], "exec-owned");
    assert_eq!(rows[0]["turn"]["summary"], "Answer 2");
    *m.failure.lock().unwrap() = "observation_inventory".into();
    *m.code.lock().unwrap() = "IDENTITY_CONFLICT".into();
    assert_eq!(
        m.call("canonical_inventory", json!({"scope":null}))
            .unwrap_err(),
        "SOURCE_AUTHORITY_ERROR:IDENTITY_CONFLICT"
    );
    *m.failure.lock().unwrap() = "observation_turns".into();
    assert_eq!(
        m.call("sample", json!({"metadata":m.native.metadata(),"state":{}}))
            .unwrap_err(),
        "SOURCE_AUTHORITY_ERROR:IDENTITY_CONFLICT"
    );
}

struct Historical {
    root: TempDir,
    path: PathBuf,
    runtime: Runtime,
    grant: Mutex<Value>,
}
impl Historical {
    fn new() -> Self {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("historical.sqlite3");
        let c = Connection::open(&path).unwrap();
        c.execute_batch("CREATE TABLE tasks(task_id TEXT PRIMARY KEY,project_alias TEXT,title TEXT,host TEXT,status TEXT,execution_state TEXT,current_stage TEXT,current_blocker TEXT,created_at TEXT,updated_at TEXT,last_progress_at TEXT,codex_running INT,retry_required INT,turn_id TEXT);
CREATE TABLE executions(execution_ref TEXT,task_id TEXT,logical_model TEXT,resolved_model TEXT,stage TEXT,execution_state TEXT,turn_id TEXT,acquired_at TEXT,routing_identity_json TEXT,execution_policy_json TEXT);
CREATE TABLE execution_history(execution_ref TEXT,task_id TEXT,logical_model TEXT,resolved_model TEXT,stage TEXT,execution_state TEXT,turn_id TEXT,acquired_at TEXT,routing_identity_json TEXT,execution_policy_json TEXT,released_at TEXT);
CREATE TABLE execution_results(task_id TEXT,execution_ref TEXT,turn_id TEXT,status TEXT,summary TEXT,validation TEXT,blockers TEXT,next_state TEXT,received_at TEXT);
CREATE TABLE conversation_bindings(task_id TEXT,thread_id TEXT);
CREATE TABLE prepared_executions(resulting_execution_ref TEXT,resulting_task_id TEXT,reasoning_effort TEXT,created_at TEXT);
CREATE TABLE host_executions(task_id TEXT,host_execution_ref TEXT,execution_ref TEXT,host TEXT,surface TEXT,operation_class TEXT,capability TEXT,operation TEXT,started_at TEXT,completed_at TEXT,duration_ms INT,exit_code INT,result_state TEXT,timed_out INT);
CREATE TABLE v2_events(cursor INTEGER PRIMARY KEY,event_id TEXT,event_type TEXT,occurred_at TEXT,recorded_at TEXT,payload_json TEXT);").unwrap();
        c.execute(
            "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            params![
                "task-fixture",
                "fixture",
                "Title",
                "p620",
                "ACTIVE",
                "COMPLETED",
                "COMPLETE",
                "",
                "2026-10-25T01:00:00Z",
                "2026-10-25T02:00:00Z",
                "2026-10-25T02:00:00Z",
                0,
                0,
                TURN
            ],
        )
        .unwrap();
        c.execute("INSERT INTO execution_history VALUES(?,?,?,?,?,?,?,?,?,?,?)",params!["exec-fixture","task-fixture","logical","resolved","COMPLETE","COMPLETED",TURN,"2026-10-25T01:00:00Z",route().to_string(),json!({"operation_classes":["DEVELOPMENT_MUTATION"],"execution_surface":"HOST_EXECUTOR"}).to_string(),"2026-10-25T02:00:00Z"]).unwrap();
        c.execute(
            "INSERT INTO execution_results VALUES(?,?,?,?,?,?,?,?,?)",
            params![
                "task-fixture",
                "exec-fixture",
                TURN,
                "PASS",
                "Done",
                "checked",
                "NONE",
                "IN_REVIEW",
                "2026-10-25T02:00:00Z"
            ],
        )
        .unwrap();
        c.execute(
            "INSERT INTO conversation_bindings VALUES(?,?)",
            params!["task-fixture", TID],
        )
        .unwrap();
        Self {
            root,
            path,
            runtime: Runtime::default(),
            grant: Mutex::new(json!({"scope":null,"record":null,"user_scope":null})),
        }
    }
    fn call(&self, op: &str, mut a: Value, snapshot: Option<&Connection>) -> Result<Value> {
        a["path"] = json!(self.path);
        a["codex_host_ids"] = json!({"p620":"remote-ssh-discovered:p620"});
        a["external_snapshot"] = json!(snapshot.is_some());
        self.runtime.invoke(
            &format!("native_observer.legacy_{op}"),
            &a,
            |op, a| match op {
                "legacy.codex_uri" => Ok(if a["thread_id"] == TID {
                    json!(format!(
                        "codex://threads/{TID}?hostId=remote-ssh-discovered%3Ap620"
                    ))
                } else {
                    Value::Null
                }),
                "legacy.grant" => Ok(self.grant.lock().unwrap().clone()),
                "legacy.sql" => {
                    let c = snapshot.unwrap();
                    let mut statement = c
                        .prepare(a["sql"].as_str().unwrap())
                        .map_err(|_| "LEGACY_SOURCE_UNAVAILABLE")?;
                    let columns = statement
                        .column_names()
                        .iter()
                        .map(|s| s.to_string())
                        .collect::<Vec<_>>();
                    let params = a["arguments"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|v| match v {
                            Value::Null => rusqlite::types::Value::Null,
                            Value::String(s) => rusqlite::types::Value::Text(s.clone()),
                            Value::Number(n) => {
                                rusqlite::types::Value::Integer(n.as_i64().unwrap())
                            }
                            _ => panic!("unexpected SQL argument"),
                        })
                        .collect::<Vec<_>>();
                    let rows = statement
                        .query_map(rusqlite::params_from_iter(params), |row| {
                            let mut object = serde_json::Map::new();
                            for (i, name) in columns.iter().enumerate() {
                                let value = match row.get_ref(i)? {
                                    rusqlite::types::ValueRef::Null => Value::Null,
                                    rusqlite::types::ValueRef::Integer(n) => json!(n),
                                    rusqlite::types::ValueRef::Real(n) => json!(n),
                                    rusqlite::types::ValueRef::Text(v) => {
                                        json!(std::str::from_utf8(v).unwrap())
                                    }
                                    _ => panic!("unexpected blob"),
                                };
                                object.insert(name.clone(), value);
                            }
                            Ok(Value::Object(object))
                        })
                        .map_err(|_| "LEGACY_SOURCE_UNAVAILABLE")?
                        .collect::<rusqlite::Result<Vec<_>>>()
                        .map_err(|_| "LEGACY_SOURCE_UNAVAILABLE")?;
                    Ok(json!(rows))
                }
                _ => panic!("unexpected {op}"),
            },
        )
    }
    fn task(&self) -> Result<Value> {
        self.call("task", json!({"task_ref":"task-fixture"}), None)
    }
}
#[test]
fn historical_observer_rust_projection_exact_result_and_snapshot() {
    let h = Historical::new();
    let before = fs::read(&h.path).unwrap();
    let item = h.task().unwrap();
    assert_eq!(item["menu_state"], "PASS");
    assert_eq!(item["elapsed_seconds"], 3600);
    assert_eq!(item["final_result"]["execution_ref"], "exec-fixture");
    assert_eq!(
        item["codex_uri"],
        format!("codex://threads/{TID}?hostId=remote-ssh-discovered%3Ap620")
    );
    assert_eq!(item["native_conversation"]["thread_id"], TID);
    assert_eq!(
        item["routing"]["provider"]["identifier"],
        "codex_app_server"
    );
    assert_eq!(item["mutation_boundary"]["allowed_actions"], json!([]));
    assert_eq!(
        h.call("list", json!({"state":"active","offset":0}), None)
            .unwrap()["items"],
        json!([])
    );
    assert_eq!(
        h.call("list", json!({"state":"recent","offset":0}), None)
            .unwrap()["items"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    let c =
        Connection::open_with_flags(&h.path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    c.execute_batch("PRAGMA query_only=ON;BEGIN").unwrap();
    assert!(c.execute("DELETE FROM tasks", []).is_err());
    let task = h
        .call("task_row", json!({"task_ref":"task-fixture"}), Some(&c))
        .unwrap();
    assert_eq!(task["turn_id"], TURN);
    let projected = h
        .call(
            "project",
            json!({"task":task,"observed":"fixed-observation"}),
            Some(&c),
        )
        .unwrap();
    assert_eq!(projected["timestamps"]["observed_at"], "fixed-observation");
    drop(c);
    assert_eq!(fs::read(&h.path).unwrap(), before);
    Connection::open(&h.path)
        .unwrap()
        .execute(
            "UPDATE tasks SET turn_id='new-turn',execution_state='QUEUED'",
            [],
        )
        .unwrap();
    let item = h.task().unwrap();
    assert!(item["execution_ref"].is_null() && item["final_result"].is_null());
    assert_eq!(item["menu_state"], "IDLE");
    let missing = h.root.path().join("missing.sqlite");
    assert_eq!(
        invoke(&h.runtime, "legacy_health", &json!({"path":missing})).unwrap_err(),
        "LEGACY_SOURCE_UNAVAILABLE"
    );
    assert!(!missing.exists());
}
#[test]
fn historical_events_cursor_scope_capacity_and_redaction() {
    let h = Historical::new();
    let c = Connection::open(&h.path).unwrap();
    for n in 0..105 {
        c.execute(
            "INSERT INTO v2_events VALUES(?,?,?,?,?,?)",
            params![
                n + 1,
                format!("event-{n}"),
                "V1ExecutionProgressObserved",
                "2026-10-25T01:00:00Z",
                "2026-10-25T01:00:00Z",
                json!({"source_task_id":"task-fixture","source_execution_ref":"exec-fixture"})
                    .to_string()
            ],
        )
        .unwrap();
    }
    c.execute(
        "UPDATE tasks SET title=?,current_blocker=?",
        params![
            "A".repeat(2000),
            format!("token=NEVER_SHOW {}", "B".repeat(9000))
        ],
    )
    .unwrap();
    c.execute(
        "UPDATE execution_results SET summary=?",
        params!["X".repeat(9000)],
    )
    .unwrap();
    drop(c);
    let first = h
        .call(
            "task_events",
            json!({"task_ref":"task-fixture","after":null}),
            None,
        )
        .unwrap();
    assert_eq!(first["items"].as_array().unwrap().len(), 100);
    assert_eq!(first["has_more"], true);
    let second = h
        .call(
            "task_events",
            json!({"task_ref":"task-fixture","after":first["next_cursor"]}),
            None,
        )
        .unwrap();
    assert_eq!(second["items"].as_array().unwrap().len(), 5);
    assert_eq!(second["has_more"], false);
    assert_eq!(
        h.call(
            "task_events",
            json!({"task_ref":"task-fixture","after":second["next_cursor"]}),
            None
        )
        .unwrap()["items"],
        json!([])
    );
    let wrong = invoke(
        &h.runtime,
        "legacy_event_cursor",
        &json!({"task_ref":"other","cursor":1}),
    )
    .unwrap();
    assert_eq!(
        h.call(
            "task_events",
            json!({"task_ref":"task-fixture","after":wrong}),
            None
        )
        .unwrap_err(),
        "INVALID_CURSOR"
    );
    let future = invoke(
        &h.runtime,
        "legacy_event_cursor",
        &json!({"task_ref":"task-fixture","cursor":9999}),
    )
    .unwrap();
    assert_eq!(
        h.call(
            "task_events",
            json!({"task_ref":"task-fixture","after":future}),
            None
        )
        .unwrap_err(),
        "CURSOR_RESET_REQUIRED"
    );
    let recent = h
        .call(
            "events",
            json!({"task_ref":"task-fixture","recent":true}),
            None,
        )
        .unwrap();
    assert_eq!(recent["items"].as_array().unwrap().len(), 20);
    assert_eq!(recent["has_more"], true);
    let task = h.task().unwrap();
    assert_eq!(task["title"].as_str().unwrap().chars().count(), 512);
    assert_eq!(task["final_result"]["text_truncated"], true);
    assert!(!task.to_string().contains("NEVER_SHOW"));
}
#[test]
fn historical_native_binding_requires_current_grants() {
    let h = Historical::new();
    let c = Connection::open(&h.path).unwrap();
    c.execute_batch("DELETE FROM conversation_bindings;CREATE TABLE native_task_bindings(task_id TEXT,node_id TEXT,thread_id TEXT,origin TEXT,first_provenance_json TEXT,user_scope TEXT,provider TEXT,project TEXT);").unwrap();
    c.execute(
        "INSERT INTO native_task_bindings VALUES(?,?,?,?,?,?,?,?)",
        params![
            "task-fixture",
            "air",
            TID,
            "CODEX_NATIVE",
            json!({"node_fingerprint":"fingerprint"}).to_string(),
            "alice",
            "codex_app_server",
            "/approved"
        ],
    )
    .unwrap();
    drop(c);
    assert_eq!(h.task().unwrap_err(), "TASK_NOT_FOUND");
    *h.grant.lock().unwrap() = json!({"scope":scope("/approved"),"record":{"public_key_fingerprint":"fingerprint"},"user_scope":"alice"});
    assert_eq!(h.task().unwrap()["native_conversation"]["node_id"], "air");
    h.grant.lock().unwrap()["scope"] = Value::Null;
    assert_eq!(
        h.call("list", json!({"state":"recent","offset":0}), None)
            .unwrap()["items"],
        json!([])
    );
}
