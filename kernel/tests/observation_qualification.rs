//! Private SQLite/native files and real Rust worker regressions. No live source.
#[path = "support/observation.rs"]
mod support;
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use clinx_decision_kernel::{native_history, node_registry::Registry};
use hmac::{Hmac, Mac};
use rusqlite::Connection;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Write,
    os::unix::fs::{PermissionsExt, symlink},
    sync::{Arc, Mutex},
    time::Instant,
};
use support::*;

#[test]
fn exact_history_pages_models_privacy_and_no_native_mutation() {
    let n = Native::new();
    let before = snapshot(&n.root);
    let p = n.context(&n.metadata, 1, 16000, &Value::Null).unwrap();
    assert_eq!(p["last_user_intent"], "User 2");
    assert_eq!(p["last_codex_result"], "Answer 2");
    assert_eq!(p["provenance"]["context_turn_refs"], json!(["turn-2"]));
    assert!(!p.to_string().contains("NEVER_"));
    assert_eq!(
        n.context(&n.metadata, 1, 16000, &p["next_cursor"]).unwrap()["last_codex_result"],
        "Answer 1"
    );
    let rows = n
        .history
        .native_turns(&json!({"metadata":n.metadata,"state":{},"node_id":"air"}))
        .unwrap();
    let models = rows[0]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| {
            (
                r["turn_id"].as_str().unwrap().to_owned(),
                r["model"].as_str().unwrap().to_owned(),
            )
        })
        .collect::<std::collections::BTreeMap<_, _>>();
    assert_eq!(
        models,
        (0..3)
            .map(|i| (format!("turn-{i}"), format!("model-{i}")))
            .collect()
    );
    assert_eq!(snapshot(&n.root), before);
}
fn cursor_case(index: usize, value: Value, legacy_bad: &str) {
    let n = Native::new();
    let mut prefix = json!([1, TID, "air", "paginated", 2]);
    // Literal CPython json.dumps bytes preserve deployed cursors, including spaces.
    let legacy_valid = r#"[1, "01a0fa4e-11e7-7540-91c8-1eee45d1a4d0", "air", "paginated", 2]"#;
    for raw in [legacy_valid.to_owned(), prefix.to_string()] {
        let valid = URL_SAFE_NO_PAD.encode(raw);
        assert_eq!(
            n.context(&n.metadata, 1, 16000, &json!(valid)).unwrap()["last_codex_result"],
            "Answer 1"
        );
    }
    prefix[index] = value;
    assert_eq!(serde_json::from_str::<Value>(legacy_bad).unwrap(), prefix);
    for raw in [legacy_bad.to_owned(), prefix.to_string()] {
        let bad = URL_SAFE_NO_PAD.encode(raw);
        assert_eq!(
            n.context(&n.metadata, 1, 16000, &json!(bad)).unwrap_err(),
            "INVALID_CONTEXT_CURSOR"
        );
    }
}
macro_rules! cursor_case {
    ($name:ident,$index:expr,$value:expr,$legacy:expr) => {
        #[test]
        fn $name() {
            cursor_case($index, json!($value), $legacy);
        }
    };
}
cursor_case!(
    native_cursor_host,
    2,
    "another-node",
    r#"[1, "01a0fa4e-11e7-7540-91c8-1eee45d1a4d0", "another-node", "paginated", 2]"#
);
cursor_case!(
    native_cursor_thread,
    1,
    "another-thread",
    r#"[1, "another-thread", "air", "paginated", 2]"#
);
cursor_case!(
    native_cursor_mode,
    3,
    "legacy",
    r#"[1, "01a0fa4e-11e7-7540-91c8-1eee45d1a4d0", "air", "legacy", 2]"#
);
cursor_case!(
    native_cursor_negative,
    4,
    -1,
    r#"[1, "01a0fa4e-11e7-7540-91c8-1eee45d1a4d0", "air", "paginated", -1]"#
);
cursor_case!(
    native_cursor_bool,
    4,
    true,
    r#"[1, "01a0fa4e-11e7-7540-91c8-1eee45d1a4d0", "air", "paginated", true]"#
);
fn fork_case(conflict: &str) {
    let n = Native::new();
    let fork = "11111111-2222-3333-4444-555555555555";
    let target = n
        .rollout
        .with_file_name(format!("rollout-fixture-{TID}_{fork}.jsonl"));
    fs::rename(&n.rollout, &target).unwrap();
    let mut metadata = n.metadata.clone();
    metadata["rollout_path"] = json!(target);
    let c = Connection::open(n.root.join("thread_history_1.sqlite")).unwrap();
    for table in [
        "thread_turns",
        "thread_items",
        "thread_history_projection_state",
    ] {
        c.execute(&format!("UPDATE {table} SET thread_id=?"), [fork])
            .unwrap();
    }
    assert_eq!(
        n.context(&metadata, 1, 16000, &Value::Null).unwrap()["provenance"]["history_thread_id"],
        fork
    );
    match conflict {
        "symlink" => {
            let outside = n.temp.path().join("outside.jsonl");
            fs::rename(&target, &outside).unwrap();
            symlink(outside, &target).unwrap();
        }
        "header" => fs::write(&target, "[]\n").unwrap(),
        "turn" => {
            c.execute(
                "UPDATE thread_turns SET turn_id='foreign' WHERE turn_id='turn-2'",
                [],
            )
            .unwrap();
        }
        "offset" => {
            c.execute(
                "UPDATE thread_turns SET rollout_byte_offset=0 WHERE turn_id='turn-2'",
                [],
            )
            .unwrap();
        }
        _ => unreachable!(),
    };
    assert_eq!(
        n.context(&metadata, 1, 16000, &Value::Null).unwrap_err(),
        "NATIVE_IDENTITY_CONFLICT"
    );
}
macro_rules! fork_case {
    ($name:ident,$value:expr) => {
        #[test]
        fn $name() {
            fork_case($value);
        }
    };
}
fork_case!(fork_symlink, "symlink");
fork_case!(fork_header, "header");
fork_case!(fork_turn, "turn");
fork_case!(fork_offset, "offset");
#[test]
fn utf8_markdown_redaction_and_role_budget() {
    let n = Native::new();
    let text = "\n    code\n\n| 列 | 值 |\n|---|---|\npassword=fixture_secret\n";
    assert_eq!(
        native_history::message(&json!({"type":"agentMessage","text":text}))
            .unwrap()
            .1,
        text.replace("fixture_secret", "[REDACTED]")
    );
    let c = Connection::open(n.root.join("thread_history_1.sqlite")).unwrap();
    c.execute(
        "UPDATE thread_items SET item_json=? WHERE turn_id='turn-2' AND item_type='agentMessage'",
        [json!({"type":"agentMessage","text":"字".repeat(4000)}).to_string()],
    )
    .unwrap();
    let v = n.context(&n.metadata, 1, 1024, &Value::Null).unwrap();
    assert!(v["last_codex_result"].as_str().unwrap().len() <= 512);
    assert_eq!(v["last_user_intent"], "User 2");
    assert_eq!(v["context_truncated"], true);
}
#[test]
fn native_legacy_incremental_checkpoint_and_partial_record() {
    let n = Native::new();
    Connection::open(n.root.join("state_5.sqlite"))
        .unwrap()
        .execute("UPDATE threads SET history_mode='legacy'", [])
        .unwrap();
    let mut f = fs::OpenOptions::new()
        .append(true)
        .open(&n.rollout)
        .unwrap();
    writeln!(f,"{}",json!({"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Legacy result"}]}})).unwrap();
    writeln!(
        f,
        "{}",
        json!({"type":"event_msg","payload":{"type":"task_complete","turn_id":"turn-2"}})
    )
    .unwrap();
    let metadata = n.history.metadata(TID).unwrap();
    let s = n.source();
    let sampled = s
        .invoke("sample", &json!({"metadata":metadata,"state":{}}))
        .unwrap();
    let event = sampled[0].as_array().unwrap().last().unwrap();
    assert_eq!(event["turn"]["native_state"], "COMPLETED");
    assert_eq!(event["turn"]["summary"], "Legacy result");
    write!(f, "{{\"type\":").unwrap();
    let next = s
        .invoke("sample", &json!({"metadata":metadata,"state":sampled[1]}))
        .unwrap();
    assert_eq!(next[1]["offset"], sampled[1]["offset"]);
    assert_eq!(
        n.context(&metadata, 4, 16000, &Value::Null).unwrap()["last_codex_result"],
        "Legacy result"
    );
}
#[test]
fn observation_atomic_gap_conflict_duplicate_terminal_and_restart() {
    let d = Directory::new();
    let e = event(1, TID, "turn-1", 1);
    for _ in 0..2 {
        assert_eq!(d.accept("air", "stream", json!([e])).unwrap()["ack_seq"], 1);
    }
    let gap = d
        .accept(
            "air",
            "stream",
            json!([event(2, TID, "turn-1", 1), event(4, TID, "turn-1", 1)]),
        )
        .unwrap();
    assert_eq!(gap["accepted"], false);
    assert_eq!(gap["expected_seq"], 3);
    assert_eq!(gap["ack_seq"], 1);
    assert_eq!(d.count("events"), 1);
    let mut conflict = e;
    conflict["title"] = json!("different");
    assert_eq!(
        d.accept("air", "stream", json!([conflict])).unwrap_err(),
        "OBSERVATION_SEQUENCE_CONFLICT"
    );
    let mut stale = event(2, TID, "turn-1", 1);
    stale["turn"]["native_state"] = json!("RUNNING");
    stale["turn"]["summary"] = Value::Null;
    d.accept("air", "stream", json!([stale])).unwrap();
    let item = d.read("list", json!({})).unwrap()["items"][0].clone();
    assert_eq!(item["turn"]["native_state"], "COMPLETED");
    assert_eq!(item["turn"]["summary"], "公开结果");
    let mut reopened = Worker::new();
    let detail=reopened.call("observation.detail",json!({"path":d.path,"user_scope":"alice","request":{"observation_id":item["observation_id"]}}),|op,a|{assert_eq!(op,"directory.node");Ok(Some(json!({"record":record(a["node_id"].as_str().unwrap()),"scope":scope(),"approval":scope(),"peer":{"trust_state":"TRUSTED","security_status":"OPAQUE_V1"}})))}).unwrap();
    assert_eq!(detail["turns"][0]["native_state"], "COMPLETED");
    assert_eq!(
        d.accept("air", "stream", json!([stale])).unwrap()["ack_seq"],
        2
    );
}
#[test]
fn two_rust_workers_concurrently_claim_same_observation_batch() {
    let d = Directory::new();
    let workers = [
        Arc::new(Mutex::new(Worker::new())),
        Arc::new(Mutex::new(Worker::new())),
    ];
    let handles=(0..8).map(|i|{let w=workers[i%2].clone();let path=d.path.clone();std::thread::spawn(move||w.lock().unwrap().call("observation.accept",json!({"path":path,"node":"air","scope":scope(),"request":{"stream_id":"stream","events":[event(1,TID,"turn-1",1)]}}),|_,_|panic!("unexpected callback")).unwrap())}).collect::<Vec<_>>();
    for h in handles {
        assert_eq!(h.join().unwrap()["ack_seq"], 1);
    }
    assert_eq!(d.count("events"), 1);
    assert_eq!(
        d.read("list", json!({})).unwrap()["items"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
}
#[test]
fn old_python_observation_digest_and_cursor_survive_migration() {
    let d = Directory::new();
    for n in 1..4 {
        d.accept(
            "air",
            "stream",
            json!([event(n, &format!("thread-{n}"), "turn-1", n)]),
        )
        .unwrap();
    }
    let first = d.read("list", json!({"limit":1})).unwrap();
    let token = first["next_cursor"].as_str().unwrap();
    let (raw, signature) = token.split_once('.').unwrap();
    let c = Connection::open(&d.path).unwrap();
    let secret: String = c
        .query_row(
            "SELECT value FROM settings WHERE key='cursor_secret'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let mut stmt = c.prepare("SELECT payload,digest FROM events").unwrap();
    for row in stmt
        .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))
        .unwrap()
    {
        let (payload, digest) = row.unwrap();
        let literal: Value = serde_json::from_str(&payload).unwrap();
        assert_eq!(
            format!("{:x}", Sha256::digest(literal.to_string().as_bytes())),
            digest
        );
    }
    let data = URL_SAFE_NO_PAD.decode(raw).unwrap();
    let mut h = Hmac::<Sha256>::new_from_slice(secret.as_bytes()).unwrap();
    h.update(&data);
    assert_eq!(hex(&h.finalize().into_bytes()), signature);
    let decoded: Value = serde_json::from_slice(&data).unwrap();
    let old = serde_json::to_vec(&decoded).unwrap();
    let mut h = Hmac::<Sha256>::new_from_slice(secret.as_bytes()).unwrap();
    h.update(&old);
    let old_token = format!(
        "{}.{}",
        URL_SAFE_NO_PAD.encode(&old),
        hex(&h.finalize().into_bytes())
    );
    assert_ne!(
        d.read("list", json!({"limit":1,"cursor":old_token}))
            .unwrap()["items"][0]["native_thread_id"],
        first["items"][0]["native_thread_id"]
    );
    let before = fs::read(&d.path).unwrap();
    let read = |method: &str| {
        Registry::read(&json!({"path":d.registry_path,"method":format!("registry.{method}"),"arguments":{"node_id":"air","user_scope":"alice"}})).unwrap()
    };
    assert_eq!(read("get")["node_id"], "air");
    assert_eq!(read("scope")["read_sessions"], true);
    assert_eq!(fs::read(&d.path).unwrap(), before);
}
fn hex(b: &[u8]) -> String {
    b.iter().map(|v| format!("{v:02x}")).collect()
}
// Independent CPython json.dumps canonical golden digests over literal payloads;
// never derive the expected hash with observation::validate/compact.
fn float_digest(stamp: f64, expected: &str) {
    let d = Directory::new();
    let mut e = event(1, TID, "turn-1", 1);
    e["turn"]["native_state"] = json!("RUNNING");
    e["turn"]["live_observation"] =
        json!({"thread_id":TID,"turn_id":"turn-1","liveness":"UNKNOWN","observed_at":stamp});
    d.accept("air", "stream", json!([e])).unwrap();
    let actual: String = Connection::open(&d.path)
        .unwrap()
        .query_row("SELECT digest FROM events", [], |r| r.get(0))
        .unwrap();
    assert_eq!(actual, expected);
}
macro_rules! float_digest {
    ($name:ident,$stamp:expr,$digest:expr) => {
        #[test]
        fn $name() {
            float_digest($stamp, $digest);
        }
    };
}
float_digest!(
    historical_float_small,
    0.00001,
    "b142acc4b4f0b8edd75ff73cb2959016bea9515835c3ac2f5678e0083c8d1c6e"
);
float_digest!(
    historical_float_exp,
    1e-6,
    "ba3afd4083457946bf155d92da07d8a47492feeb4faddcc239dce5715cd3caf3"
);
float_digest!(
    historical_float_large,
    1e16,
    "37bb263dc158a613fbd37d3de6692c051bb1cca6ec45e64fbe5e69282e479c09"
);
float_digest!(
    historical_float_one,
    1.0,
    "97af0bd373281c9f0b7170c1962d31f866a14efc65f575308b8b0161db2481ca"
);
float_digest!(
    historical_float_negative_zero,
    -0.0,
    "c0c2bc1e01acdd2ac6b8d904fff7d20f21990ca70b75a85f5676d94209338cd7"
);
float_digest!(
    historical_float_precision,
    1.2345678901234567,
    "1b492d100a273c01662eff1e7b9e1ce2b34cb10b08e3d2cee38310690a7b5acc"
);
#[test]
fn revoke_filters_cursor_and_remote_context_after_read() {
    let d = Directory::new();
    d.accept(
        "air",
        "stream",
        json!([event(1, TID, "turn-1", 1), event(2, "another", "turn-1", 1)]),
    )
    .unwrap();
    let first = d.read("list", json!({"limit":1})).unwrap();
    let result = d.read_with(
        "context",
        json!({"observation_id":first["items"][0]["observation_id"]}),
        json!({}),
        |_| {
            d.scopes.borrow_mut().get_mut("air").unwrap()["revoked"] = json!(true);
            Ok(json!({"read_only":true,"last_codex_result":"WITHHELD"}))
        },
    );
    assert!(result.is_err());
    assert_eq!(d.read("list", json!({})).unwrap()["items"], json!([]));
    assert!(
        d.read("list", json!({"cursor":first["next_cursor"]}))
            .is_err()
    );
}
#[test]
fn copied_node_identity_never_proves_running_or_enables_control() {
    let d = Directory::new();
    let mut e = event(1, TID, "turn-1", 1);
    e["turn"]["native_state"] = json!("RUNNING");
    e["turn"]["live_observation"] = json!({"thread_id":TID,"turn_id":"turn-1","observed_at":clinx_decision_kernel::read_projection::now(),"liveness":"LIVE"});
    d.accept("air", "one", json!([e])).unwrap();
    d.scopes.borrow_mut().insert("imac".into(), scope());
    d.registry
        .invoke(
            "registry.register",
            &json!({"record":record("imac"),"scope":scope()}),
        )
        .unwrap();
    d.accept("imac", "two", json!([e])).unwrap();
    for p in d.read("list", json!({})).unwrap()["items"]
        .as_array()
        .unwrap()
    {
        assert_eq!(p["coverage"], "MULTI_SOURCE_THREAD_IDENTITY");
        assert_eq!(p["liveness"], "NOT_PROVEN");
        assert_eq!(p["control"]["enabled"], false);
    }
}
#[test]
fn release_observation_marker_and_invalid_unicode_cursor_fail_promptly() {
    let mut child = std::process::Command::new(env!("CARGO_BIN_EXE_clinx-node"))
        .arg("--observation-contract")
        .env_clear()
        .stdout(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = Instant::now();
    while child.try_wait().unwrap().is_none() {
        if deadline.elapsed().as_secs() >= 5 {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("observation contract probe exceeded its five-second deadline");
        }
        std::thread::sleep(std::time::Duration::from_millis(5));
    }
    let result = child.wait_with_output().unwrap();
    assert!(result.status.success());
    assert_eq!(
        String::from_utf8(result.stdout).unwrap().trim(),
        "RUST_OBSERVATION_V1"
    );
    let d = Directory::new();
    d.accept(
        "air",
        "s",
        json!([event(1, TID, "turn-1", 1), event(2, "second", "turn-1", 1)]),
    )
    .unwrap();
    let first = d.read("list", json!({"limit":1})).unwrap();
    let raw = first["next_cursor"]
        .as_str()
        .unwrap()
        .split('.')
        .next()
        .unwrap();
    let start = Instant::now();
    assert!(
        d.read(
            "list",
            json!({"cursor":format!("{raw}.{}x","字".repeat(21))})
        )
        .is_err()
    );
    assert!(start.elapsed().as_secs() < 5);
}
#[test]
fn missing_native_index_never_creates_database() {
    let d = tempfile::tempdir().unwrap();
    assert_eq!(
        native_history::History::new(d.path().to_str().unwrap())
            .metadata(TID)
            .unwrap_err(),
        "NATIVE_INDEX_UNAVAILABLE"
    );
    assert_eq!(fs::read_dir(d.path()).unwrap().count(), 0);
}
#[test]
fn local_directory_context_preserves_public_read_only_flag() {
    let n = Native::new();
    let d = Directory::new();
    d.accept("air", "s", json!([event(1, TID, "turn-1", 1)]))
        .unwrap();
    let before = snapshot(&n.root);
    let id = d.read("list", json!({})).unwrap()["items"][0]["observation_id"].clone();
    let result = d
        .read_with(
            "context",
            json!({"observation_id":id}),
            json!({"local_identity":{"node_id":"air"},"native_home":n.root}),
            |_| panic!("unexpected remote"),
        )
        .unwrap();
    assert_eq!(result["last_codex_result"], "Answer 2");
    assert_eq!(result["read_only"], true);
    assert_eq!(snapshot(&n.root), before);
}
#[test]
fn remote_context_rechecks_a_new_projection_snapshot() {
    let d = Directory::new();
    d.accept("air", "s", json!([event(1, TID, "turn-1", 1)]))
        .unwrap();
    let id = d.read("list", json!({})).unwrap()["items"][0]["observation_id"].clone();
    let mut count = 0;
    let result = d.read_with("context", json!({"observation_id":id}), json!({}), |_| {
        count += 1;
        d.accept("air", "s", json!([event(2, TID, "next", 2)]))?;
        Ok(json!({"context_status":"AVAILABLE","last_codex_result":"stale turn"}))
    });
    assert_eq!(result.unwrap_err(), "EXECUTION_TURN_IDENTITY_CONFLICT");
    assert_eq!(count, 1);
    assert_eq!(
        d.read("detail", json!({"observation_id":id})).unwrap()["item"]["turn"]["turn_id"],
        "next"
    );
}
fn permission(index: &str) {
    assert_ne!(
        rustix::process::getuid().as_raw(),
        0,
        "qualification requires an unprivileged user"
    );
    let n = Native::new();
    let before = snapshot(&n.root);
    let path = n.root.join(index);
    let mode = fs::metadata(&path).unwrap().permissions();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o0)).unwrap();
    assert_eq!(
        fs::File::open(&path).unwrap_err().kind(),
        std::io::ErrorKind::PermissionDenied
    );
    let result = if index == "state_5.sqlite" {
        n.history.metadata(TID)
    } else {
        n.context(&n.metadata, 1, 16000, &Value::Null)
    };
    fs::set_permissions(&path, mode).unwrap();
    assert_eq!(result.unwrap_err(), "NATIVE_ACCESS_DENIED");
    assert_eq!(snapshot(&n.root), before);
}
#[test]
fn native_index_permission_denied() {
    permission("state_5.sqlite");
}
#[test]
fn native_history_permission_denied() {
    permission("thread_history_1.sqlite");
}
fn malformed(field: &str) {
    let d = Directory::new();
    let mut e = event(1, TID, "turn-1", 1);
    let target = if ["result_processing", "progress", "summary"].contains(&field) {
        &mut e["turn"]
    } else {
        &mut e
    };
    target[field] = json!({"text":"not display text"});
    assert_eq!(
        d.accept("air", "s", json!([e])).unwrap_err(),
        "INVALID_OBSERVATION"
    );
    assert_eq!(d.read("list", json!({})).unwrap()["items"], json!([]));
    assert_eq!(
        d.accept("air", "s", json!([event(1, TID, "turn-1", 1)]))
            .unwrap()["ack_seq"],
        1
    );
}
macro_rules! malformed {
    ($name:ident,$field:expr) => {
        #[test]
        fn $name() {
            malformed($field);
        }
    };
}
malformed!(malformed_title, "title");
malformed!(malformed_source_client, "source_client");
malformed!(malformed_coverage, "coverage");
malformed!(malformed_result_processing, "result_processing");
malformed!(malformed_progress, "progress");
malformed!(malformed_summary, "summary");
