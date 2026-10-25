//! Remaining Python IPC/relay/package assertions expressed against native IO.
//! All sockets, repositories and bundles are disposable qualification resources.
#[path = "support/provider.rs"]
mod support;
use base64::{Engine, engine::general_purpose::STANDARD};
use clinx_decision_kernel::{
    authority_client,
    authority_store::PROTOCOL,
    node_provider,
    node_relay::{CHUNK, Relay},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    os::unix::{
        fs::{PermissionsExt, symlink},
        net::{UnixListener, UnixStream},
    },
    path::Path,
    process::{Command, Stdio},
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

fn authority(endpoint: &Path, operation: &str, request_id: &str) -> Value {
    json!({"endpoint":endpoint,"authority_uid":rustix::process::getuid().as_raw(),
        "timeout":1.0,"wire_operation":operation,"request_id":request_id,"payload":{"value":"资格"}})
}
fn authority_ack(reply: &str) {
    let root = tempfile::tempdir().unwrap();
    let endpoint = root.path().join("a.sock");
    let listener = UnixListener::bind(&endpoint).unwrap();
    fs::set_permissions(&endpoint, fs::Permissions::from_mode(0o600)).unwrap();
    let reply = reply.to_owned();
    let mode = reply.clone();
    let (done, completed) = std::sync::mpsc::channel();
    let worker = thread::spawn(move || {
        let (mut peer, _) = listener.accept().unwrap();
        peer.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        let mut wire = String::new();
        BufReader::new(peer.try_clone().unwrap())
            .read_line(&mut wire)
            .unwrap();
        let request: Value = serde_json::from_str(&wire).unwrap();
        assert_eq!(
            request,
            json!({"protocol_version":PROTOCOL,"request_id":"original", "operation":"business.start","payload":{"value":"资格"}})
        );
        let mut response =
            json!({"protocol_version":PROTOCOL,"request_id":"original","result":{"received":true}});
        match reply.as_str() {
            "lost" => {}
            "foreign" => response["request_id"] = json!("wrong"),
            "both" => response["error"] = json!("CONFLICT"),
            "remote" => {
                response.as_object_mut().unwrap().remove("result");
                response["error"] = json!("EXACT_CONFLICT");
            }
            "valid" => {}
            _ => panic!("unknown fixture reply"),
        }
        if reply != "lost" {
            writeln!(peer, "{response}").unwrap();
        }
        drop(peer);
        completed.recv_timeout(Duration::from_secs(3)).unwrap();
        // The client cannot have silently reconnected/replayed after any reply.
        listener.set_nonblocking(true).unwrap();
        assert_eq!(
            listener.accept().unwrap_err().kind(),
            std::io::ErrorKind::WouldBlock
        );
    });
    let result = authority_client::request(&authority(&endpoint, "business.start", "original"));
    match mode.as_str() {
        "valid" => assert_eq!(result.unwrap(), json!({"received":true})),
        "remote" => assert_eq!(result.unwrap_err(), "EXACT_CONFLICT"),
        _ => assert_eq!(result.unwrap_err(), "DELIVERY_UNKNOWN"),
    }
    done.send(()).unwrap();
    worker.join().unwrap();
}
macro_rules! ack {
    ($name:ident, $reply:literal) => {
        #[test]
        fn $name() {
            authority_ack($reply);
        }
    };
}
ack!(authority_ack_exact, "valid");
ack!(authority_ack_lost_never_replays, "lost");
ack!(authority_ack_foreign_identity, "foreign");
ack!(authority_ack_conflicting_outcome, "both");
ack!(authority_ack_remote_error, "remote");
#[test]
fn authority_validation_precedes_endpoint_io() {
    for (op, id, error) in [
        ("execute_arbitrary", "a".to_owned(), "UNSUPPORTED_OPERATION"),
        ("health", String::new(), "INVALID_REQUEST_ID"),
        ("health", "a".repeat(129), "INVALID_REQUEST_ID"),
    ] {
        assert_eq!(
            authority_client::request(&authority(
                Path::new("/missing-qualification-endpoint"),
                op,
                &id
            ))
            .unwrap_err(),
            error
        );
    }
}
#[test]
fn authority_endpoint_symlink_and_foreign_uid_are_rejected() {
    let root = tempfile::tempdir().unwrap();
    let endpoint = root.path().join("a.sock");
    let listener = UnixListener::bind(&endpoint).unwrap();
    fs::set_permissions(&endpoint, fs::Permissions::from_mode(0o600)).unwrap();
    let alias = root.path().join("alias.sock");
    symlink(&endpoint, &alias).unwrap();
    let a = authority(&alias, "health", "health");
    let mut b = authority(&endpoint, "health", "health");
    b["authority_uid"] = json!(rustix::process::getuid().as_raw() + 1);
    for request in [a, b] {
        assert_eq!(
            authority_client::request(&request).unwrap_err(),
            "AUTHORITY_ENDPOINT_UNTRUSTED"
        );
    }
    listener.set_nonblocking(true).unwrap();
    assert_eq!(
        listener.accept().unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
}
#[test]
fn authority_full_backlog_fails_before_send_with_bounded_wait() {
    use rustix::net::{AddressFamily, SocketAddrUnix, SocketFlags, SocketType, bind, listen};
    let root = tempfile::tempdir().unwrap();
    let endpoint = root.path().join("a.sock");
    let socket = rustix::net::socket_with(
        AddressFamily::UNIX,
        SocketType::STREAM,
        SocketFlags::CLOEXEC,
        None,
    )
    .unwrap();
    bind(&socket, &SocketAddrUnix::new(&endpoint).unwrap()).unwrap();
    fs::set_permissions(&endpoint, fs::Permissions::from_mode(0o600)).unwrap();
    listen(&socket, 0).unwrap();
    let listener = UnixListener::from(socket);
    let _held = UnixStream::connect(&endpoint).unwrap();
    let mut request = authority(&endpoint, "health", "bounded");
    request["timeout"] = json!(0.05);
    let start = Instant::now();
    assert_eq!(
        authority_client::request(&request).unwrap_err(),
        "AUTHORITY_UNAVAILABLE"
    );
    assert!(start.elapsed() < Duration::from_secs(1));
    let (mut peer, _) = listener.accept().unwrap();
    peer.set_nonblocking(true).unwrap();
    assert_eq!(
        peer.read(&mut [0]).unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
}
#[test]
fn writer_peer_credentials_failure_stays_unknown_without_initialization_or_fallback() {
    for peer_failure in [true, false] {
        let runtime = clinx_decision_kernel::observation_runtime::Runtime::default();
        runtime.invoke("source_runtime.init", &json!({"config":{"native_home":"/nonexistent/qualification-native",
            "native_observer_socket":"/unused-qualification-socket","injected_probe":true,"injected_home":"/nonexistent/qualification-home"}}), |_,_|panic!()).unwrap();
        let mut calls = vec![];
        let result = runtime
            .invoke(
                "source_runtime.writer_probe",
                &json!({"tid":"thread","turn_id":"turn","project":"/project"}),
                |op, _| {
                    calls.push(op.to_owned());
                    match op {
                        "writer_probe.worker_uid" => Ok(json!(1001)),
                        "writer_probe.open" if peer_failure => {
                            Err("PROVIDER_PEER_IDENTITY_CONFLICT".into())
                        }
                        "writer_probe.open" => Ok(json!(1002)),
                        _ => panic!("peer failure initialized or requested Provider: {op}"),
                    }
                },
            )
            .unwrap();
        assert_eq!(
            result,
            json!({"liveness":"UNKNOWN","reason":"NATIVE_WRITER_PROBE_UNAVAILABLE"})
        );
        assert_eq!(calls, ["writer_probe.worker_uid", "writer_probe.open"]);
    }
}
fn digest(data: &[u8]) -> String {
    format!("{:x}", Sha256::digest(data))
}
fn relay_request(body: &Value) -> Value {
    let data = serde_json::to_vec(body).unwrap();
    let context = json!({"host":"air","user_scope":"alice","cwd":"/fixture","thread_id":"exact"});
    json!({"node_id":"air","scope":{"user_scope":"alice","execute_tasks":true,"projects":["/fixture"]},
        "context_wire":context.to_string(),"request":{"operation":"provider.upload","request_id":"a".repeat(32),
        "context":context,"index":0,"count":1,"digest":digest(&data),"data":STANDARD.encode(data)}})
}
#[test]
fn relay_seals_before_io_and_consumed_response_never_replays() {
    let relay = Relay::default();
    let request = relay_request(&json!({"method":"thread/read","params":{"threadId":"exact"}}));
    let calls = AtomicUsize::new(0);
    let response = relay
        .handle(&request, |_, _| {
            calls.fetch_add(1, Ordering::SeqCst);
            assert_eq!(
                relay
                    .handle(&request, |_, _| panic!(
                        "reentrant duplicate reached Provider"
                    ))
                    .unwrap_err(),
                "PROVIDER_REQUEST_IDENTITY_CONFLICT"
            );
            Ok(json!({"raw_provider":true}))
        })
        .unwrap();
    assert_eq!(response["accepted"], true);
    assert_eq!(
        relay.handle(&request, |_, _| panic!()).unwrap_err(),
        "PROVIDER_REQUEST_IDENTITY_CONFLICT"
    );
    let mut read = request.clone();
    read["request"]["operation"] = json!("provider.read");
    let result = relay.handle(&read, |_, _| panic!()).unwrap();
    let data = STANDARD.decode(result["data"].as_str().unwrap()).unwrap();
    assert_eq!(digest(&data), result["digest"]);
    assert_eq!(
        serde_json::from_slice::<Value>(&data).unwrap(),
        json!({"result":{"raw_provider":true}})
    );
    assert_eq!(
        relay.handle(&request, |_, _| panic!()).unwrap_err(),
        "PROVIDER_REQUEST_IDENTITY_CONFLICT"
    );
    assert_eq!(calls.load(Ordering::SeqCst), 1);
}
#[test]
fn relay_concurrent_duplicate_requests_run_provider_once() {
    let relay = Arc::new(Relay::default());
    let request = relay_request(&json!({"method":"thread/read","params":{}}));
    let calls = Arc::new(AtomicUsize::new(0));
    let mut workers = vec![];
    for _ in 0..8 {
        let (relay, request, calls) = (relay.clone(), request.clone(), calls.clone());
        workers.push(thread::spawn(move || {
            relay.handle(&request, |_, _| {
                calls.fetch_add(1, Ordering::SeqCst);
                Ok(json!({}))
            })
        }));
    }
    let results = workers
        .into_iter()
        .map(|w| w.join().unwrap())
        .collect::<Vec<_>>();
    assert_eq!(
        results
            .iter()
            .filter(|r| r.as_ref().is_ok_and(|v| v["accepted"] == true))
            .count(),
        1
    );
    for error in results.into_iter().filter_map(Result::err) {
        assert_eq!(error, "PROVIDER_REQUEST_IDENTITY_CONFLICT");
    }
    assert_eq!(calls.load(Ordering::SeqCst), 1);
}
#[test]
fn relay_scope_conflicts_fail_without_buffer_or_provider() {
    for (field, value) in [
        ("host", "other"),
        ("user_scope", "bob"),
        ("cwd", "/another-project"),
    ] {
        let relay = Relay::default();
        let mut request = relay_request(&json!({}));
        request["request"]["context"][field] = json!(value);
        request["context_wire"] = json!(request["request"]["context"].to_string());
        assert_eq!(
            relay
                .handle(&request, |_, _| panic!("scope conflict reached Provider"))
                .unwrap_err(),
            "PROVIDER_SCOPE_DENIED"
        );
        assert_eq!(relay.count().unwrap(), 0);
    }
}
#[test]
fn relay_chunk_digest_conflict_preserves_complete_original_input() {
    let relay = Relay::default();
    let body = json!({"method":"turn/start","params":{"input":"x".repeat(CHUNK+100)}});
    let wire = serde_json::to_vec(&body).unwrap();
    let mut request = relay_request(&body);
    request["request"]["count"] = json!(2);
    request["request"]["data"] = json!(STANDARD.encode(&wire[..CHUNK]));
    assert_eq!(
        relay.handle(&request, |_, _| panic!()).unwrap()["next_index"],
        1
    );
    request["request"]["index"] = json!(1);
    request["request"]["data"] = json!(STANDARD.encode(&wire[CHUNK..]));
    let mut conflict = request.clone();
    conflict["request"]["digest"] = json!("0".repeat(64));
    assert_eq!(
        relay.handle(&conflict, |_, _| panic!()).unwrap_err(),
        "PROVIDER_REQUEST_IDENTITY_CONFLICT"
    );
    let mut calls = vec![];
    assert_eq!(
        relay
            .handle(&request, |_, value| {
                calls.push(value.clone());
                Ok(json!({}))
            })
            .unwrap()["accepted"],
        true
    );
    assert_eq!(calls, vec![body]);
}
#[test]
fn provider_protocol_guards_fail_before_connecting() {
    for (method, params, scope, error) in [
        (
            "turn/start",
            json!({"threadId":"wrong"}),
            json!({"mode":"dispatch","thread_id":"exact"}),
            "PROVIDER_THREAD_IDENTITY_CONFLICT",
        ),
        (
            "turn/start",
            json!({"threadId":"exact"}),
            json!({"mode":"read","thread_id":"exact"}),
            "PROVIDER_INPUT_IDENTITY_CONFLICT",
        ),
        (
            "turn/interrupt",
            json!({"threadId":"exact","turnId":"wrong"}),
            json!({"mode":"cancel","thread_id":"exact","turn_id":"turn"}),
            "PROVIDER_CANCEL_DENIED",
        ),
        (
            "command/exec",
            json!({"command":["rm"]}),
            json!({"mode":"dispatch"}),
            "PROVIDER_METHOD_DENIED",
        ),
        (
            "thread/resume",
            json!({"threadId":"exact","excludeTurns":true}),
            json!({"mode":"read","thread_id":"exact"}),
            "PROVIDER_RESUME_DENIED",
        ),
    ] {
        let root = tempfile::tempdir().unwrap();
        let endpoint = root.path().join("p.sock");
        let listener = UnixListener::bind(&endpoint).unwrap();
        let mut context = json!({"host":"air","cwd":"/fixture"});
        context
            .as_object_mut()
            .unwrap()
            .extend(scope.as_object().unwrap().clone());
        assert_eq!(
            node_provider::exchange(
                &endpoint,
                Path::new("/unused-provider-program"),
                &context,
                &json!({"method":method,"params":params}),
                Duration::from_millis(100)
            )
            .unwrap_err(),
            error
        );
        listener.set_nonblocking(true).unwrap();
        assert_eq!(
            listener.accept().unwrap_err().kind(),
            std::io::ErrorKind::WouldBlock
        );
    }
}
fn packaged_provider(profile: &str) {
    let root = tempfile::tempdir().unwrap();
    let bundle = root.path().join("release");
    let binary = if profile == "rust" {
        bundle.join("clinx-provider")
    } else {
        bundle.join("Contents/Helpers/clinx-provider")
    };
    fs::create_dir_all(binary.parent().unwrap()).unwrap();
    fs::copy(env!("CARGO_BIN_EXE_clinx-provider"), &binary).unwrap();
    let repo = root.path().join("repo");
    fs::create_dir(&repo).unwrap();
    for args in [
        vec!["init", "-q", "--initial-branch=main"],
        vec![
            "-c",
            "user.name=Qualification",
            "-c",
            "user.email=qualification@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        ],
    ] {
        assert!(
            Command::new("git")
                .current_dir(&repo)
                .args(args)
                .status()
                .unwrap()
                .success()
        );
    }
    let program = binary.clone();
    let calls = Arc::new(AtomicUsize::new(0));
    let count = calls.clone();
    let provider = support::Provider::new(&root.path().join("p.sock"), move |request| {
        match request["method"].as_str().unwrap() {
            "initialize" => return Some(vec![json!({"id":request["id"],"result":{}})]),
            "initialized" => return Some(vec![]),
            "command/exec" => {}
            _ => panic!("unexpected Provider mutation/tool reply: {request}"),
        }
        let p = &request["params"];
        assert_eq!(p["command"][0], json!(program));
        assert_eq!(p["command"][1], "--inspect");
        assert_eq!(p["cwd"], "/");
        assert_eq!(p["timeoutMs"], 2500);
        assert_eq!(p["outputBytesCap"], 65536);
        assert_eq!(p["sandboxPolicy"], json!({"type":"dangerFullAccess"}));
        count.fetch_add(1, Ordering::SeqCst);
        let output = Command::new(&program)
            .args(
                p["command"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .skip(1)
                    .map(|a| a.as_str().unwrap()),
            )
            .env_clear()
            .env("PATH", std::env::var_os("PATH").unwrap())
            .output()
            .unwrap();
        assert!(output.status.success());
        Some(vec![
            json!({"id":"unsolicited","method":"item/tool/requestUserInput","params":{}}),
            json!({"id":request["id"],"result":{"exitCode":0,"stdout":String::from_utf8(output.stdout).unwrap()}}),
        ])
    });
    let context = json!({"mode":"workspace","host":"air","cwd":repo});
    let mut child = Command::new(&binary)
        .args(["--exchange", provider.path.to_str().unwrap(), "2"])
        .current_dir(root.path())
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    writeln!(
        child.stdin.take().unwrap(),
        "{}",
        json!({"context":context,"body":{"method":"workspace/inspect","params":context}})
    )
    .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let reply: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(
        reply["result"]["observer_source"],
        "CODEX_PROVIDER_COMMAND_EXEC"
    );
    assert_eq!(reply["result"]["cwd"], json!(repo));
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    fn native_only(root: &Path) {
        for entry in fs::read_dir(root).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                native_only(&path);
            } else {
                assert_eq!(path.file_name().unwrap(), "clinx-provider");
            }
        }
    }
    native_only(&bundle);
}
#[test]
fn rust_bundle_provider_works_without_python_or_checkout() {
    packaged_provider("rust");
}
#[test]
fn monitor_bundle_provider_works_without_python_or_checkout() {
    packaged_provider("monitor");
}
