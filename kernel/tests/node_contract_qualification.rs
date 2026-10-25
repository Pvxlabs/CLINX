//! Assertion-preserving port of fixtures/test_rust_node_contract.py at 29f72a6.
//! Real clinx-node child frames replace the retired in-process Python MCP factory.
#[path = "support/node_qualification.rs"]
mod support;
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    io::{BufRead, BufReader, Write},
    process::{Child, Command, Stdio},
    sync::{Arc, Mutex},
};
struct ChildGuard(Child);
impl Drop for ChildGuard {
    fn drop(&mut self) {
        if self.0.try_wait().unwrap().is_none() {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
}
fn pipe(mut request: Value, mut reply: impl FnMut(&Value) -> Value) -> (Value, Vec<Value>) {
    request["protocol"] = json!("CLINX_REQUEST_PIPE_V1");
    let mut child = ChildGuard(
        Command::new(env!("CARGO_BIN_EXE_clinx-node"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap(),
    );
    let mut input = child.0.stdin.take().unwrap();
    let mut output = BufReader::new(child.0.stdout.take().unwrap());
    writeln!(input, "{request}").unwrap();
    input.flush().unwrap();
    let mut frames = vec![];
    loop {
        let mut line = String::new();
        assert_ne!(output.read_line(&mut line).unwrap(), 0, "premature EOF");
        let frame: Value = serde_json::from_str(&line).unwrap();
        if frame["kind"] == "result" {
            drop(input);
            assert!(child.0.wait().unwrap().success());
            let mut remainder = String::new();
            assert_eq!(
                output.read_line(&mut remainder).unwrap(),
                0,
                "extra callback"
            );
            return (frame, frames);
        }
        let result = reply(&frame);
        writeln!(input, "{}", json!({"id":frame["id"],"result":result})).unwrap();
        input.flush().unwrap();
        frames.push(frame);
    }
}
fn config() -> Value {
    json!({"runtime_host":"air","provider_endpoint":"/provider-must-not-be-read","provider_timeout_seconds":1,"host_aliases":{},"codex_host_ids":{}})
}
fn node() -> Value {
    json!({"node_id":"air","projects":[{"project_alias":"project","repo":"/fixture/project"}],"allowed_projects":["project"]})
}
fn task() -> Value {
    json!({"host":"air","project":"project","thread_id":"current-thread"})
}
fn receipt() -> Value {
    json!({"execution_ref":"exec_exact","task_ref":"task_exact","thread_id":"historic-thread","native_turn_id":"exact-turn","status":"RUNNING","resource_state":"HELD"})
}
fn delegated(operation: &str, a: Value, reply: impl FnMut(&Value) -> Value) -> (Value, Vec<Value>) {
    let mut config = node();
    if a["canonical_identity"].is_object() {
        config["allowed_threads"] = json!(["11111111-2222-3333-4444-555555555555"]);
    }
    pipe(
        json!({"mode":"delegated_node","node":config,"operation":operation,"arguments":a}),
        reply,
    )
}
fn current(f: &Value) -> Value {
    assert_eq!(f["kind"], "integration");
    match f["operation"].as_str().unwrap() {
        "get_status" => {
            assert_eq!(
                f["arguments"],
                json!({"execution_ref":"exec_exact","task_ref":null})
            );
            receipt()
        }
        "task" => {
            assert_eq!(f["arguments"], json!({"reference":"task_exact"}));
            task()
        }
        _ => panic!("unexpected mutation: {f}"),
    }
}
#[test]
fn conflicting_cancel_selectors_never_mutate_and_historical_status_keeps_exact_thread() {
    for (key, value) in [
        ("host", "another-node"),
        ("project", "another-project"),
        ("thread_id", "another-thread"),
        ("native_turn_id", "another-turn"),
    ] {
        let mut a = json!({"execution_ref":"exec_exact","approved":true});
        a[key] = json!(value);
        let (r, calls) = delegated("cancel", a, current);
        assert!(r.get("error").is_some());
        assert!(!calls.iter().any(|f| f["operation"] == "cancel_execution"));
    }
    let (r, _) = delegated(
        "status",
        json!({"execution_ref":"exec_exact","thread_id":"historic-thread","native_turn_id":"exact-turn","host":"air"}),
        current,
    );
    for k in ["thread_id", "native_turn_id", "task_ref"] {
        assert_eq!(r["result"][k], receipt()[k]);
    }
}
#[test]
fn cancel_reports_only_authoritative_terminal_receipt() {
    for (status, resource, expected) in [
        ("CANCELLED", "RELEASED", "CANCELLED"),
        ("CANCELLED", "HELD", "CANCELLATION_PENDING"),
        ("RUNNING", "HELD", "CANCELLATION_PENDING"),
    ] {
        let (r, calls) = delegated(
            "cancel",
            json!({"execution_ref":"exec_exact","approved":true}),
            |f| {
                if f["operation"] == "cancel_execution" {
                    assert_eq!(
                        f["arguments"],
                        json!({"execution_ref":"exec_exact","approved":true})
                    );
                    let mut r = receipt();
                    r["status"] = json!(status);
                    r["resource_state"] = json!(resource);
                    r
                } else {
                    current(f)
                }
            },
        );
        assert_eq!(r["result"]["operation_state"], expected);
        assert_eq!(r["result"]["status"], status);
        assert_eq!(r["result"]["resource_state"], resource);
        assert_eq!(
            calls
                .iter()
                .filter(|f| f["operation"] == "cancel_execution")
                .count(),
            1
        );
    }
}
fn canonical() -> Value {
    let thread = "11111111-2222-3333-4444-555555555555";
    // The literal UUIDv5 was independently frozen by Python's UUID oracle.
    let id = json!({"node_id":"air","host":"air","provider":"codex_app_server","thread_id":thread,"user_scope":"fixture-user","origin":"CODEX_NATIVE","title":"Native fixture","cwd":"/fixture/project","task_id":"task_180a1c8b002759ce872201685ea09a19","first_provenance_json":json!({"node":"air","node_fingerprint":"paired-fingerprint","observation_id":"fixture-observation","digest":"fixture-digest"}).to_string()});
    json!({"canonical_identity":id,"exact_turn_id":"exact-predecessor","_canonical_owner":{"owner":"air","user":"fixture-user","fingerprint":"paired-fingerprint"}})
}
fn adopt(f: &Value, a: &Value, state: &str) -> Value {
    assert_eq!(f["kind"], "integration");
    assert_eq!(f["operation"], "adopt_conversation");
    assert_eq!(
        f["arguments"],
        json!({"host":"air","project":"project","thread_id":a["canonical_identity"]["thread_id"],"title":"Native fixture","exact_turn_id":"exact-predecessor","canonical_identity":a["canonical_identity"]})
    );
    json!({"adoption_status":state,"task_ref":a["canonical_identity"]["task_id"]})
}
#[test]
fn canonical_adoption_forwards_identity_without_committing_provider_evidence() {
    let a = canonical();
    let (r, calls) = delegated("adopt", a.clone(), |f| adopt(f, &a, "PENDING"));
    assert_eq!(r["result"]["adoption_status"], "PENDING");
    assert_eq!(calls.len(), 1);
}
#[test]
fn canonical_scope_identity_and_predecessor_conflicts_fail_before_request() {
    for (k, v) in [
        ("_canonical_owner", json!(null)),
        (
            "_canonical_owner",
            json!({"owner":"air","user":"other","fingerprint":"paired-fingerprint"}),
        ),
        (
            "_canonical_owner",
            json!({"owner":"air","user":"fixture-user","fingerprint":"forged"}),
        ),
        ("host", json!("imac")),
        ("thread_id", json!("other-thread")),
        ("task_ref", json!("task_other")),
        ("project", json!("other")),
        ("exact_turn_id", json!(null)),
        ("native_turn_id", json!("conflicting-turn")),
    ] {
        let mut a = canonical();
        a[k] = v;
        let (r, calls) = delegated("adopt", a, |f| {
            panic!("must fail before authority request: {f}")
        });
        assert!(r.get("error").is_some(), "{k}: {r}");
        assert!(calls.is_empty());
    }
}
#[test]
fn pending_adoption_cannot_prepare_but_committed_adoption_prepares_same_task() {
    let mut a = canonical();
    a["prompt"] = json!("Exact input");
    let (r, calls) = delegated("prepare", a.clone(), |f| adopt(f, &a, "PENDING"));
    assert_eq!(r["error"]["code"], "CANONICAL_ADOPTION_PENDING");
    assert_eq!(calls.len(), 1);
    let (r, calls) = delegated("prepare", a.clone(), |f| {
        match f["operation"].as_str().unwrap() {
            "adopt_conversation" => adopt(f, &a, "ADOPTED"),
            "task" => {
                assert_eq!(
                    f["arguments"]["reference"],
                    a["canonical_identity"]["task_id"]
                );
                json!({"host":"air","project":"project","thread_id":a["canonical_identity"]["thread_id"]})
            }
            "prepare_execution" => {
                assert_eq!(
                    f["arguments"],
                    json!({"task_ref":a["canonical_identity"]["task_id"],"host":"air","prompt":"Exact input"})
                );
                json!({"prepared_execution_ref":"prepared_fixture"})
            }
            _ => panic!("unexpected callback: {f}"),
        }
    });
    assert_eq!(r["result"]["prepared_execution_ref"], "prepared_fixture");
    assert_eq!(calls.len(), 3);
}
#[test]
fn canonical_adoption_readback_needs_no_api_side_provider_access() {
    let a = canonical();
    let id = &a["canonical_identity"];
    let (r, calls) = pipe(
        json!({"mode":"api","config":config(),"operation":"clinx_adopt_conversation","arguments":{"host":"air","project":"project","thread_id":id["thread_id"],"exact_turn_id":"exact-predecessor","canonical_identity":id}}),
        |f| {
            assert_eq!(f["kind"], "authority");
            assert_eq!(f["operation"], "adopt");
            assert_eq!(
                f["arguments"]["workspace_contract"],
                json!({"cwd":id["cwd"]})
            );
            assert_eq!(f["arguments"]["canonical_identity"], *id);
            json!({"adoption_status":"ADOPTED","task_ref":id["task_id"]})
        },
    );
    assert_eq!(r["result"]["task_ref"], id["task_id"]);
    assert_eq!(calls.len(), 1);
}
#[test]
fn rust_start_uses_authority_without_second_node_writer_fence() {
    let calls = Arc::new(Mutex::new(vec![]));
    let captured = calls.clone();
    let worker = support::Worker::new(move |f| {
        let op = f["operation"].as_str().unwrap();
        captured.lock().unwrap().push(op.to_string());
        Ok(match op {
            "registry.claim_request" => json!(["NEW", null]),
            "registry.finish_request" => json!(null),
            "execute" => {
                assert_eq!(f["arguments"]["operation"], "execution.start");
                let request = &f["arguments"]["request"];
                assert!(request["fence"].is_null());
                let (r, calls) = delegated("start", request.clone(), |event| {
                    match event["operation"].as_str().unwrap() {
                        "prepared" => {
                            json!({"host":"air","project":"project","new_task":true,"execution_ref":null,"task_ref":"task_exact"})
                        }
                        "start_execution" => {
                            assert_eq!(
                                event["arguments"]["prepared_execution_ref"],
                                "prepared_exact"
                            );
                            assert_eq!(event["arguments"]["approved"], true);
                            json!({"execution_ref":"exec_exact","status":"ADMITTED"})
                        }
                        _ => panic!("unexpected start callback: {event}"),
                    }
                });
                assert_eq!(calls.len(), 2);
                r["result"].clone()
            }
            _ => panic!("unexpected second writer/fence: {f}"),
        })
    });
    let r=worker.call("service.handle",json!({"record":support::record("air"),"scope":support::scope(true),"available":{"execution.start":true},"authority_owned":true,"operation":"execution.start","request":{"request_id":"start-exact","execution_ref":"exec_exact","prepared_execution_ref":"prepared_exact","thread_id":"exact-thread","approved":true}})).unwrap();
    assert_eq!(r["status"], "ADMITTED");
    drop(worker);
    assert_eq!(
        *calls.lock().unwrap(),
        [
            "registry.claim_request",
            "execute",
            "registry.finish_request"
        ]
    );
}
#[test]
fn helper_close_has_no_provider_or_authority_lifecycle_effect() {
    let calls = Arc::new(Mutex::new(VecDeque::new()));
    let captured = calls.clone();
    let worker = support::Worker::new(move |f| {
        captured.lock().unwrap().push_back(f);
        panic!("closing Helper must not call Authority or Provider")
    });
    drop(worker);
    assert!(calls.lock().unwrap().is_empty());
}
#[test]
fn native_mcp_never_falls_back_to_python_owner() {
    let t = tempfile::tempdir().unwrap();
    let path = t.path().join("mcp.json");
    std::fs::write(&path, config().to_string()).unwrap();
    for extra in [vec![], vec!["--own-executions"]] {
        let output = Command::new(env!("CARGO_BIN_EXE_clinx-mcp"))
            .args(["--stdio", "--config"])
            .arg(&path)
            .args(&extra)
            .env("PATH", "/no-python-or-other-runtime")
            .env("HOME", t.path())
            .env("CLINX_AUTHORITY", "python")
            .env_remove("CLINX_RUST_SOCKET")
            .env_remove("CLINX_RUST_UID")
            .output()
            .unwrap();
        assert!(!output.status.success());
        assert!(output.stdout.is_empty());
        assert!(
            String::from_utf8_lossy(&output.stderr).contains(if extra.is_empty() {
                "RUST_AUTHORITY_CONFIG_REQUIRED"
            } else {
                "MCP_ARGUMENT_INVALID"
            })
        );
    }
}
