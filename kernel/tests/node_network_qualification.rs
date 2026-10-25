//! Real process/network/durable receipt equivalence from test_rust_node_network.py
//! at main 5c0107d. TLS/OpenSSL and Python certificate oracles remain in Python.
#[path = "support/node_qualification.rs"]
mod support;
use base64::{Engine, engine::general_purpose::STANDARD};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    io::{BufRead, BufReader, Write},
    net::TcpStream,
    process::Command,
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    thread,
    time::Duration,
};
use support::*;
fn claim(r: &Worker, id: &str, op: &str, payload: Value) -> Result<Value> {
    registry_call(
        r,
        "registry.claim_request",
        json!({"node_id":"air","request_id":id,"operation":op,"payload":payload}),
    )
}
fn finish(r: &Worker, id: &str, response: Value) {
    registry_call(
        r,
        "registry.finish_request",
        json!({"node_id":"air","request_id":id,"state":"COMPLETED","response":response}),
    )
    .unwrap();
}
fn identity() -> Value {
    json!({"node_id":"air","user_scope":"alice","provider":"codex_app_server","native_thread_id":"thread_exact","source_version":"1"})
}
#[test]
fn release_node_identifies_its_network_contract() {
    let result = Command::new(env!("CARGO_BIN_EXE_clinx-node"))
        .arg("--network-contract")
        .output()
        .unwrap();
    assert!(result.status.success());
    assert_eq!(result.stdout, b"RUST_NODE_NETWORK_V1\n");
    assert!(result.stderr.is_empty());
}
#[test]
fn rust_listener_can_rebind_after_process_restart_without_replaying_requests() {
    let calls = Arc::new(Mutex::new(vec![]));
    let make = || {
        let c = calls.clone();
        Worker::new(move |e| {
            assert_eq!(e["kind"], "incoming");
            c.lock().unwrap().push(e["request"].clone());
            Ok(json!({"read_only":true}))
        })
    };
    let first = make();
    let address = first
        .call(
            "listen",
            json!({"address":"127.0.0.1","port":0,"allow_insecure_loopback":true}),
        )
        .unwrap()["address"]
        .clone();
    let call = || {
        let mut stream = TcpStream::connect((
            address[0].as_str().unwrap(),
            address[1].as_u64().unwrap() as u16,
        ))
        .unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        stream
            .write_all(b"{\"operation\":\"node.status\"}\n")
            .unwrap();
        let mut result = String::new();
        BufReader::new(stream).read_line(&mut result).unwrap();
        serde_json::from_str::<Value>(&result).unwrap()
    };
    assert_eq!(call(), json!({"read_only":true}));
    drop(first);
    let replacement = make();
    assert_eq!(
        replacement
            .call(
                "listen",
                json!({"address":address[0],"port":address[1],"allow_insecure_loopback":true})
            )
            .unwrap()["address"],
        address
    );
    assert_eq!(calls.lock().unwrap().len(), 1);
    assert_eq!(call(), json!({"read_only":true}));
    assert_eq!(calls.lock().unwrap().len(), 2);
}
#[test]
fn historical_non_ascii_float_request_digest_survives_rust_reopen() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("existing.sqlite3");
    let payload = json!({"prompt":"准确🙂","weight":1e-7,"zero":-0.0});
    // Literal ORIGINAL Python json.dumps(sort_keys=True,separators=(',',':'))
    // bytes retain ensure_ascii, exponent zero padding and negative zero.
    let wire = r#"{"prompt":"\u51c6\u786e\ud83d\ude42","weight":1e-07,"zero":-0.0}"#;
    let old = rusqlite::Connection::open(&path).unwrap();
    old.execute_batch("CREATE TABLE request_ledger (node_id TEXT NOT NULL, request_id TEXT NOT NULL, operation TEXT NOT NULL, payload_hash TEXT NOT NULL, state TEXT NOT NULL, response_json TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(node_id,request_id));").unwrap();
    old.execute(
        "INSERT INTO request_ledger VALUES (?,?,?,?,?,?,?)",
        rusqlite::params![
            "air",
            "old-exact",
            "execution.start",
            format!("{:x}", Sha256::digest(wire)),
            "IN_FLIGHT",
            Option::<String>::None,
            "2026-10-25T00:00:00+00:00"
        ],
    )
    .unwrap();
    drop(old);
    let r = registry(&path);
    let a = json!({"node_id":"air","request_id":"old-exact","operation":"execution.start","payload":payload,"payload_wire":wire});
    assert_eq!(
        registry_call(&r, "registry.claim_request", a.clone()).unwrap(),
        json!(["IN_FLIGHT", null])
    );
    let mut changed = payload.clone();
    changed["prompt"] = json!("changed");
    assert_eq!(
        claim(&r, "old-exact", "execution.start", changed).unwrap_err(),
        "IDEMPOTENCY_KEY_REUSE"
    );
    finish(&r, "old-exact", json!({"execution_ref":"exact"}));
    drop(r);
    let reopened = registry(&path);
    assert_eq!(
        registry_call(&reopened, "registry.claim_request", a).unwrap(),
        json!(["COMPLETED",{"execution_ref":"exact"}])
    );
}
#[test]
fn distinct_rust_workers_claim_once_and_keep_unknown_after_restart() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("index.sqlite3");
    let first = registry(&path);
    let second = registry(&path);
    let index = AtomicUsize::new(0);
    let ready = std::sync::Barrier::new(8);
    let states = Mutex::new(vec![]);
    thread::scope(|s| {
        for _ in 0..8 {
            s.spawn(|| {
                ready.wait();
                loop {
                    let i = index.fetch_add(1, Ordering::SeqCst);
                    if i >= 20 {
                        break;
                    }
                    let r = if i.is_multiple_of(2) { &second } else { &first };
                    let result = claim(
                        r,
                        "one",
                        "execution.start",
                        json!({"execution_ref":"exec_exact"}),
                    )
                    .unwrap()[0]
                        .clone();
                    states.lock().unwrap().push(result);
                }
            });
        }
    });
    let states = states.into_inner().unwrap();
    assert_eq!(states.iter().filter(|s| **s == "NEW").count(), 1);
    assert_eq!(states.iter().filter(|s| **s == "IN_FLIGHT").count(), 19);
    drop(first);
    drop(second);
    let reopened = registry(&path);
    assert_eq!(
        claim(
            &reopened,
            "one",
            "execution.start",
            json!({"execution_ref":"exec_exact"})
        )
        .unwrap(),
        json!(["IN_FLIGHT", null])
    );
}
#[test]
fn start_lost_reply_is_reconciled_exactly_without_reexecuting() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("node.sqlite3");
    let r = registry(&path);
    let request = json!({"operation":"execution.start","execution_ref":"exec_exact","request_id":"lost","user_scope":"alice"});
    claim(&r, "lost", "execution.start", request.clone()).unwrap();
    drop(r);
    let r = registry(&path);
    let calls = Arc::new(AtomicUsize::new(0));
    let exact = Arc::new(Mutex::new(false));
    let (c, e) = (calls.clone(), exact.clone());
    let node = service(r, move |op, _| {
        assert_eq!(op, "execution.status", "unknown start replayed");
        if *e.lock().unwrap() {
            Ok(json!({"execution_ref":"exec_exact","status":"RUNNING"}))
        } else {
            c.fetch_add(1, Ordering::SeqCst);
            Ok(json!({"execution_ref":"other"}))
        }
    });
    let available = ["execution.start", "execution.status"];
    assert_eq!(
        service_call(&node, "execution.start", request.clone(), &available).unwrap()["operation_state"],
        "UNKNOWN"
    );
    *exact.lock().unwrap() = true;
    let result = service_call(&node, "execution.start", request, &available).unwrap();
    assert_eq!(result["operation_state"], "RECONCILED");
    assert_eq!(result["execution_started"], false);
    assert_eq!(result["execution_ref"], "exec_exact");
    assert_eq!(calls.load(Ordering::SeqCst), 1);
}
#[test]
fn concurrent_start_and_cancel_receipts_survive_node_restart() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("node.sqlite3");
    let starts = Arc::new(AtomicUsize::new(0));
    let cancels = Arc::new(AtomicUsize::new(0));
    let make = |r| {
        let (s, c) = (starts.clone(), cancels.clone());
        service(r, move |op, _| {
            Ok(match op {
                "execution.start" => {
                    s.fetch_add(1, Ordering::SeqCst);
                    json!({"execution_ref":"exec_exact","operation_state":"STARTED"})
                }
                "execution.cancel" => {
                    c.fetch_add(1, Ordering::SeqCst);
                    json!({"execution_ref":"exec_exact","operation_state":"CANCELLED"})
                }
                _ => panic!("unexpected execution {op}"),
            })
        })
    };
    let r = registry(&path);
    let node = make(r.clone());
    let request =
        json!({"execution_ref":"exec_exact","request_id":"start-once","user_scope":"alice"});
    let cancel =
        json!({"execution_ref":"exec_exact","request_id":"cancel-once","user_scope":"alice"});
    let available = ["execution.start", "execution.cancel"];
    let receipts = Mutex::new(vec![]);
    thread::scope(|s| {
        for _ in 0..8 {
            s.spawn(|| {
                let receipt =
                    service_call(&node, "execution.start", request.clone(), &available).unwrap();
                receipts.lock().unwrap().push(receipt);
            });
        }
    });
    assert_eq!(starts.load(Ordering::SeqCst), 1);
    assert!(
        receipts
            .into_inner()
            .unwrap()
            .iter()
            .all(|r| matches!(r["operation_state"].as_str(), Some("UNKNOWN" | "STARTED")))
    );
    assert_eq!(
        service_call(&node, "execution.cancel", cancel.clone(), &available).unwrap()["operation_state"],
        "CANCELLED"
    );
    let mut conflict = request.clone();
    conflict["execution_ref"] = json!("exec_conflict");
    assert_eq!(
        service_call(&node, "execution.start", conflict, &available).unwrap_err(),
        "IDEMPOTENCY_KEY_REUSE"
    );
    drop(node);
    drop(r);
    let reopened = make(registry(&path));
    assert_eq!(
        service_call(&reopened, "execution.cancel", cancel, &available).unwrap()["idempotent"],
        true
    );
    assert_eq!(
        service_call(&reopened, "execution.start", request, &available).unwrap()["idempotent"],
        true
    );
    assert_eq!(starts.load(Ordering::SeqCst), 1);
    assert_eq!(cancels.load(Ordering::SeqCst), 1);
}
#[test]
fn full_wire_budget_unicode_keeps_exact_legacy_ledger_digest() {
    let temp = tempfile::tempdir().unwrap();
    let r = registry(&temp.path().join("node.sqlite3"));
    let calls = Arc::new(Mutex::new(vec![]));
    let c = calls.clone();
    let node = service(r.clone(), move |op, a| {
        assert_eq!(op, "execution.prepare");
        c.lock().unwrap().push(a);
        Ok(json!({"prepared_execution_ref":"prepared_exact"}))
    });
    let request = json!({"operation":"execution.prepare","user_scope":"alice","request_id":"unicode-budget","prompt":"🙂".repeat(60000)});
    assert!(serde_json::to_vec(&request).unwrap().len() < 256 * 1024);
    assert_eq!(
        service_call(
            &node,
            "execution.prepare",
            request.clone(),
            &["execution.prepare"]
        )
        .unwrap()["prepared_execution_ref"],
        "prepared_exact"
    );
    assert_eq!(
        service_call(
            &node,
            "execution.prepare",
            request.clone(),
            &["execution.prepare"]
        )
        .unwrap()["idempotent"],
        true
    );
    assert_eq!(calls.lock().unwrap().len(), 1);
    assert_eq!(calls.lock().unwrap()[0]["prompt"], request["prompt"]);
    assert_eq!(
        claim(&r, "unicode-budget", "execution.prepare", request).unwrap(),
        json!(["COMPLETED",{"prepared_execution_ref":"prepared_exact"}])
    );
}
#[test]
fn independent_callback_fence_conflict_is_durably_blocked_before_execution() {
    let temp = tempfile::tempdir().unwrap();
    let r = registry(&temp.path().join("node.sqlite3"));
    registry_call(
        &r,
        "registry.acquire_fence",
        json!({"identity":identity(),"execution_ref":"holder"}),
    )
    .unwrap();
    let node = service(r.clone(), |_, _| {
        panic!("occupied independent fence executed")
    });
    let request = json!({"thread_id":"thread_exact","execution_ref":"exec_other","request_id":"fence-conflict","user_scope":"alice"});
    let result = service_call(
        &node,
        "execution.start",
        request.clone(),
        &["execution.start"],
    )
    .unwrap();
    assert_eq!(result["operation_state"], "BLOCKED");
    assert_eq!(result["side_effect"], "NONE");
    assert_eq!(result["error_code"], "SINGLE_WRITER_BUSY");
    assert_eq!(result["owner_execution"], "holder");
    assert_eq!(
        claim(&r, "fence-conflict", "execution.start", request).unwrap(),
        json!(["FAILED", result])
    );
}
#[test]
fn cancel_receipt_recovery_finishes_interrupted_independent_fence_release() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("node.sqlite3");
    let r = registry(&path);
    let request = json!({"thread_id":"thread_exact","execution_ref":"exec_exact","request_id":"cancel-crash","user_scope":"alice"});
    assert_eq!(
        registry_call(
            &r,
            "registry.acquire_fence",
            json!({"identity":identity(),"execution_ref":"exec_exact"})
        )
        .unwrap(),
        1
    );
    claim(&r, "cancel-crash", "execution.cancel", request.clone()).unwrap();
    finish(&r, "cancel-crash", json!({"operation_state":"CANCELLED"}));
    drop(r);
    let reopened = registry(&path);
    let node = service(reopened.clone(), |_, _| {
        panic!("cancel replayed after receipt")
    });
    assert_eq!(
        service_call(&node, "execution.cancel", request, &["execution.cancel"]).unwrap(),
        json!({"operation_state":"CANCELLED","idempotent":true})
    );
    assert_eq!(
        registry_call(
            &reopened,
            "registry.acquire_fence",
            json!({"identity":identity(),"execution_ref":"next"})
        )
        .unwrap(),
        2
    );
}
#[test]
fn receipt_write_failure_after_start_stays_unknown_and_never_replays() {
    let temp = tempfile::tempdir().unwrap();
    let r = registry(&temp.path().join("node.sqlite3"));
    let calls = Arc::new(AtomicUsize::new(0));
    let fail = Arc::new(Mutex::new(true));
    let (c, f) = (calls.clone(), fail.clone());
    let node = Worker::new(move |e| {
        let op = e["operation"].as_str().unwrap();
        let a = e["arguments"].clone();
        if op == "registry.finish_request" && *f.lock().unwrap() {
            return Err("NODE_REGISTRY_UNAVAILABLE".into());
        }
        if op.starts_with("registry.") {
            return registry_call(&r, op, a);
        }
        assert_eq!(op, "execute");
        Ok(match a["operation"].as_str().unwrap() {
            "execution.start" => {
                c.fetch_add(1, Ordering::SeqCst);
                json!({"execution_ref":"exec_exact"})
            }
            "execution.status" => json!({"execution_ref":"exec_exact","status":"RUNNING"}),
            _ => panic!("unexpected execution"),
        })
    });
    let request = json!({"operation":"execution.start","execution_ref":"exec_exact","request_id":"receipt-lost","user_scope":"alice"});
    let available = ["execution.start", "execution.status"];
    let reply = service_call(&node, "execution.start", request.clone(), &available).unwrap();
    assert_eq!(reply["operation_state"], "UNKNOWN");
    assert_eq!(reply["side_effect"], "UNKNOWN");
    assert_eq!(reply["retry"], "RECONCILIATION_REQUIRED");
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    *fail.lock().unwrap() = false;
    assert_eq!(
        service_call(&node, "execution.start", request, &available).unwrap()["operation_state"],
        "RECONCILED"
    );
    assert_eq!(calls.load(Ordering::SeqCst), 1);
}
#[test]
fn provider_unknown_and_consumed_reply_cannot_be_replayed_after_worker_loss() {
    let calls = Arc::new(AtomicUsize::new(0));
    let c = calls.clone();
    let relay = Worker::new(move |e| {
        assert_eq!(e["operation"], "relay.exchange");
        c.fetch_add(1, Ordering::SeqCst);
        Err("PROVIDER_DELIVERY_UNKNOWN".into())
    });
    let context = json!({"host":"air","user_scope":"alice","cwd":"/fixture","thread_id":"exact"});
    let body = json!({"method":"thread/read","params":{"threadId":"exact"}}).to_string();
    let request = json!({"operation":"provider.upload","request_id":"a".repeat(32),"context":context,"index":0,"count":1,"digest":format!("{:x}",Sha256::digest(body.as_bytes())),"data":STANDARD.encode(body)});
    let mut scope = scope(true);
    scope["projects"] = json!(["/fixture"]);
    let args = |r: Value| json!({"node_id":"air","request":r,"scope":scope,"context_wire":context.to_string()});
    let read = json!({"operation":"provider.read","request_id":request["request_id"],"context":context,"index":0});
    assert_eq!(
        relay.call("relay.handle", args(request.clone())).unwrap()["accepted"],
        true
    );
    let reply = relay.call("relay.handle", args(read.clone())).unwrap();
    assert_eq!(
        serde_json::from_slice::<Value>(&STANDARD.decode(reply["data"].as_str().unwrap()).unwrap())
            .unwrap(),
        json!({"error":"PROVIDER_DELIVERY_UNKNOWN"})
    );
    assert_eq!(
        relay.call("relay.handle", args(read.clone())).unwrap_err(),
        "PROVIDER_DELIVERY_UNKNOWN"
    );
    assert!(relay.call("relay.handle", args(request)).is_err());
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    drop(relay);
    let replacement = Worker::plain();
    assert_eq!(
        replacement.call("relay.handle", args(read)).unwrap_err(),
        "PROVIDER_REPLY_UNAVAILABLE"
    );
}
