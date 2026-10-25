//! Provider process, API and wire cases migrated from test_rust_provider_api.py.
use clinx_decision_kernel::provider_client::Runtime;
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    io::{BufRead, BufReader, Write},
    process::{Child, ChildStdin, ChildStdout, Command, Stdio},
    time::{Duration, Instant},
};
fn invoke<F: FnMut(&str, Value) -> Result<Value, String>>(
    op: &str,
    a: Value,
    callback: F,
) -> Value {
    Runtime::default()
        .invoke(&format!("provider.client.{op}"), &a, callback)
        .unwrap()
}
fn api<F: FnMut(&str, Value) -> Result<Value, String>>(
    op: &str,
    args: Value,
    state: Value,
    callback: F,
) -> Value {
    invoke(
        &format!("api.{op}"),
        json!({"arguments":args,"state":state}),
        callback,
    )
}
fn no_io(_: &str, _: Value) -> Result<Value, String> {
    panic!("unexpected provider IO")
}
fn bytes(s: &str) -> Vec<u8> {
    assert!(s.len().is_multiple_of(2));
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}
fn hex(s: &[u8]) -> String {
    s.iter().map(|b| format!("{b:02x}")).collect()
}
struct Node {
    child: Child,
    input: Option<ChildStdin>,
    output: BufReader<ChildStdout>,
    id: u64,
    _home: tempfile::TempDir,
}
impl Node {
    fn new() -> Self {
        let home = tempfile::tempdir().unwrap();
        let mut child = Command::new(env!("CARGO_BIN_EXE_clinx-node"))
            .arg("--network-worker")
            .env_clear()
            .env("HOME", home.path())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        Self {
            input: child.stdin.take(),
            output: BufReader::new(child.stdout.take().unwrap()),
            child,
            id: 0,
            _home: home,
        }
    }
    fn call(&mut self, op: &str, args: Value) -> Value {
        self.id += 1;
        writeln!(
            self.input.as_mut().unwrap(),
            "{}",
            json!({"id":self.id,"operation":format!("provider.client.{op}"),"arguments":args})
        )
        .unwrap();
        self.input.as_mut().unwrap().flush().unwrap();
        let mut poll = [rustix::event::PollFd::new(
            self.output.get_ref(),
            rustix::event::PollFlags::IN,
        )];
        assert!(
            rustix::event::poll(
                &mut poll,
                Some(&rustix::event::Timespec {
                    tv_sec: 5,
                    tv_nsec: 0
                })
            )
            .unwrap()
                > 0,
            "provider worker timed out"
        );
        let mut line = String::new();
        self.output.read_line(&mut line).unwrap();
        let reply: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(reply["id"], self.id);
        assert!(reply.get("error").is_none(), "{reply}");
        reply["result"].clone()
    }
    fn value(&mut self, op: &str, args: Value) -> Value {
        let result = self.call(op, args);
        assert!(result.get("error").is_none(), "{result}");
        result["value"].clone()
    }
}
impl Drop for Node {
    fn drop(&mut self) {
        self.input.take();
        let end = Instant::now() + Duration::from_secs(3);
        while self.child.try_wait().unwrap().is_none() {
            if Instant::now() > end {
                self.child.kill().unwrap();
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        self.child.wait().unwrap();
    }
}
fn helper() -> std::path::PathBuf {
    let path = std::env::current_exe()
        .unwrap()
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .join("examples/qualification_provider");
    assert!(
        path.is_file(),
        "build qualification example first: cargo build --examples (expected {})",
        path.display()
    );
    path
}
#[test]
fn rust_owned_process_exec_sets_session_and_jsonl_unicode() {
    let mut node = Node::new();
    let session = node.value(
        "wire.process_connect",
        json!({"command":[helper(),"jsonl"]}),
    );
    node.value(
        "wire.send_line",
        json!({"process_session":session,"message":{"id":"exact","content":"中文"}}),
    );
    let result = node.value(
        "wire.receive_line",
        json!({"process_session":session,"timeout":2}),
    );
    assert_eq!(result["pid"], result["sid"]);
    assert_eq!(result["pid"], result["pgid"]);
    assert_eq!(result["value"], json!({"id":"exact","content":"中文"}));
    node.value("wire.process_close", json!({"process_session":session}));
    // The native session is physically removed. The Python facade's post-close
    // `unavailable` projection is retained in the thin binding smoke.
    assert_eq!(
        node.call("wire.process_lifecycle", json!({"process_session":session}))["error"]["message"],
        "app-server transport is not connected"
    );
}
#[test]
fn rust_process_exec_failure_is_synchronous() {
    let mut node = Node::new();
    let reply = node.call(
        "wire.process_connect",
        json!({"command":["/missing-clinx-provider-contract-fixture"]}),
    );
    assert_eq!(reply["error"]["kind"], "transport");
    assert!(
        reply["error"]["message"]
            .as_str()
            .unwrap()
            .contains("failed to start")
    );
    assert!(reply.get("value").is_none());
}
#[test]
fn unsubscribe_failure_is_recorded_without_replay_or_active_turn_release() {
    let mut requests = vec![];
    let mut closed = 0;
    let result = api(
        "close",
        json!({}),
        json!({"owned_thread_ids":["idle","live"],"owned_turns":{"live":"running-turn"}}),
        |op, a| {
            assert_eq!(op, "provider.client.io");
            match a["operation"].as_str().unwrap() {
                "request" => {
                    requests.push(a["arguments"].clone());
                    Ok(
                        json!({"error":{"kind":"transport","message":"ACK lost","class":"AppServerTransportError"}}),
                    )
                }
                "close" => {
                    closed += 1;
                    Ok(json!({"result":null}))
                }
                _ => panic!("unexpected callback {a}"),
            }
        },
    );
    assert_eq!(
        requests,
        [json!({"method":"thread/unsubscribe","params":{"threadId":"idle"}})]
    );
    assert_eq!(
        result["state"]["writer_release"],
        json!({"idle":"UNCONFIRMED:AppServerTransportError"})
    );
    assert_eq!(result["state"]["owned_thread_ids"], json!(["live"]));
    assert_eq!(
        result["state"]["owned_turns"],
        json!({"live":"running-turn"})
    );
    assert_eq!(closed, 1);
}
fn conflict(other: bool) {
    let message = if other {
        "thread other already has an active writer"
    } else {
        "thread exact already has an active writer"
    };
    let remote = json!({"kind":"remote","message":"original remote error","class":"AppServerRemoteError","injected_error":42,"remote":{"code":-32600,"message":message}});
    let mut requests = vec![];
    let result = api(
        "thread_resume",
        json!({"thread_id":"exact"}),
        json!({}),
        |_, a| {
            requests.push(a["arguments"].clone());
            Ok(json!({"error":remote}))
        },
    );
    let mut expected = remote;
    if !other {
        expected["kind"] = json!("writer_conflict");
    }
    assert_eq!(result["error"], expected);
    assert_eq!(
        requests,
        [json!({"method":"thread/resume","params":{"threadId":"exact","excludeTurns":true}})]
    );
}
#[test]
fn writer_conflict_exact() {
    conflict(false);
}
#[test]
fn writer_conflict_other_preserves_error_identity() {
    conflict(true);
}
#[test]
fn model_alias_ambiguity_and_malformed_alias_are_not_resolved_by_guessing() {
    let rows = json!({"data":[{"id":"gpt-bad","model":"Terra","supportedReasoningEfforts":null},{"id":"gpt-good","model":"Terra","supportedReasoningEfforts":["high"]},{"id":"gpt-straße","model":"Straße","supportedReasoningEfforts":["medium"]}]});
    let call = |name, effort| {
        api(
            "resolve_model",
            json!({"requested":name,"reasoning_effort":effort}),
            json!({}),
            |_, a| {
                assert_eq!(a["operation"], "model_list");
                Ok(json!({"result":rows}))
            },
        )
    };
    let bad = call("Terra", "high");
    assert_eq!(bad["error"]["kind"], "model");
    assert!(
        bad["error"]["message"]
            .as_str()
            .unwrap()
            .contains("malformed capability")
    );
    assert_eq!(
        call("STRASSE", "medium")["value"],
        json!(["gpt-straße", "medium"])
    );
    assert_eq!(
        call("gpt-good", "high")["value"],
        json!(["gpt-good", "high"])
    );
}
#[test]
fn version_soft_guard_keeps_unbounded_decimal_components() {
    let huge = "99999999999999999999999999999999999999999999";
    assert_eq!(
        api("version", json!({"value":"codex/٣.٢.١"}), json!({}), no_io)["value"],
        json!(["٣", "٢", "١"])
    );
    assert_eq!(
        api(
            "version",
            json!({"value":format!("{huge}.1.0")}),
            json!({}),
            no_io
        )["value"],
        json!([huge, "1", "0"])
    );
    assert_eq!(
        api(
            "versions_compatible",
            json!({"expected":[huge,"1","0"],"actual":["100000000000000000000000000000000000000000000","1","0"]}),
            json!({}),
            no_io
        )["value"],
        false
    );
    assert_eq!(
        api(
            "versions_compatible",
            json!({"expected":null,"actual":["3","0","0"]}),
            json!({}),
            no_io
        )["value"],
        true
    );
    assert_eq!(
        api(
            "versions_compatible",
            json!({"expected":["3","0","0"],"actual":["3","999","1"]}),
            json!({}),
            no_io
        )["value"],
        true
    );
}
fn wire(payload: &[u8]) -> (Value, Vec<Vec<u8>>) {
    let mut pending = VecDeque::from(payload.to_vec());
    let mut sent = vec![];
    let reply = invoke("wire.receive", json!({"timeout":1}), |op, a| {
        assert_eq!(op, "provider.client.io");
        match a["operation"].as_str().unwrap() {
            "send_bytes" => {
                sent.push(bytes(a["arguments"]["payload"].as_str().unwrap()));
                Ok(json!({"result":null}))
            }
            "receive_bytes" => {
                let n = a["arguments"]["length"].as_u64().unwrap() as usize;
                if n > pending.len() {
                    return Ok(json!({"error":{"kind":"transport","message":"fixture EOF"}}));
                }
                let part = pending.drain(..n).collect::<Vec<_>>();
                Ok(json!({"result":hex(&part)}))
            }
            _ => panic!(),
        }
    });
    (reply, sent)
}
#[test]
fn rust_websocket_handles_control_frames_and_exact_fragment_assembly() {
    let (reply, sent) = wire(b"\x89\x01?\x01\x05{\"x\":\x80\x021}");
    assert_eq!(reply["value"], json!({"x":1}));
    let pong = &sent[0];
    assert_eq!(&pong[..2], b"\x8a\x81");
    assert_eq!(pong[pong.len() - 1] ^ pong[2], b'?');
}
fn bad_wire(payload: &[u8], reason: &str) {
    let (reply, _) = wire(payload);
    assert_eq!(reply["error"]["kind"], "protocol");
    assert!(
        regex::Regex::new(reason)
            .unwrap()
            .is_match(reply["error"]["message"].as_str().unwrap()),
        "{reply}"
    );
}
#[test]
fn websocket_unexpected_continuation() {
    bad_wire(b"\x80\x00", "unexpected.*continuation");
}
#[test]
fn websocket_not_text() {
    bad_wire(b"\x82\x02{}", "not text");
}
#[test]
fn websocket_not_json_object() {
    bad_wire(b"\x81\x02[]", "JSON object");
}
#[test]
fn websocket_malformed_utf8() {
    bad_wire(b"\x81\x02\xff\xff", "malformed websocket JSON");
}
#[test]
fn websocket_oversize() {
    let mut payload = b"\x81\x7f".to_vec();
    payload.extend((16 * 1024 * 1024 + 1u64).to_be_bytes());
    bad_wire(&payload, "too large");
}
#[test]
fn rust_process_websocket_uses_same_codec_without_python_byte_io() {
    let mut node = Node::new();
    let session = node.value(
        "wire.process_connect",
        json!({"command":[helper(),"websocket"]}),
    );
    node.value(
        "wire.handshake",
        json!({"process_session":session,"path":"/","timeout":2}),
    );
    let reply = api(
        "thread_read",
        json!({"thread_id":"exact-thread"}),
        json!({}),
        |_, a| {
            assert_eq!(a["operation"], "request");
            let request = &a["arguments"];
            node.value("wire.send",json!({"process_session":session,"message":{"id":1,"method":request["method"],"params":request["params"]}}));
            let reply = node.value(
                "wire.receive",
                json!({"process_session":session,"timeout":2}),
            );
            assert_eq!(reply["id"], 1);
            Ok(json!({"result":reply["result"]}))
        },
    );
    assert_eq!(reply["value"], json!({"id":"exact-thread"}));
    node.value("wire.process_close", json!({"process_session":session}));
}
#[test]
fn concurrent_api_calls_produce_distinct_owned_turn_patches() {
    let barrier = std::sync::Barrier::new(2);
    let replies = std::thread::scope(|scope| {
        let barrier = &barrier;
        ["a", "b"]
            .map(|thread| {
                scope.spawn(move || {
                    api(
                        "turn_start",
                        json!({"thread_id":thread,"prompt":"run","cwd":"/project"}),
                        json!({}),
                        |_, a| {
                            assert_eq!(a["operation"], "request");
                            assert_eq!(a["arguments"]["method"], "turn/start");
                            barrier.wait();
                            Ok(json!({"result":{"turn":{"id":format!("{thread}-turn")}}}))
                        },
                    )
                })
            })
            .into_iter()
            .map(|t| t.join().unwrap())
            .collect::<Vec<_>>()
    });
    assert_eq!(replies[0]["value"]["turn_id"], "a-turn");
    assert_eq!(replies[1]["value"]["turn_id"], "b-turn");
    assert_eq!(replies[0]["patch"]["owned_turns"], json!({"a":"a-turn"}));
    assert_eq!(replies[1]["patch"]["owned_turns"], json!({"b":"b-turn"}));
}

#[test]
fn api_explicit_selection_uses_catalog_maps_effort_and_preserves_upstream_diagnosis() {
    let mut requests = vec![];
    let reply = api(
        "turn_start",
        json!({"thread_id":"exact","prompt":"OK","cwd":"/fixture",
        "model":"gpt-6-astra","reasoning_effort":"extra_high","full_access":true}),
        json!({}),
        |_, a| {
            assert_eq!(a["operation"], "request");
            let r = &a["arguments"];
            requests.push(r.clone());
            match r["method"].as_str().unwrap() {
                "model/list" => Ok(
                    json!({"result":{"data":[{"id":"gpt-6-astra","model":"gpt-6-astra",
                "supportedReasoningEfforts":[{"reasoningEffort":"xhigh"}]}],"nextCursor":null}}),
                ),
                "turn/start" => {
                    assert_eq!(r["params"]["model"], "gpt-6-astra");
                    assert_eq!(r["params"]["effort"], "xhigh");
                    Ok(
                        json!({"error":{"kind":"remote","message":"rejected","remote":{"code":-32000,
                    "message":"{\"error\":{\"type\":\"upstream_error\",\"message\":\"SECRET_SENTINEL\"}}"}}}),
                    )
                }
                other => panic!("unexpected {other}"),
            }
        },
    );
    assert_eq!(requests.len(), 2);
    assert_eq!(reply["error"]["kind"], "remote");
    assert_eq!(reply["error"]["diagnostic"]["category"], "UPSTREAM_SERVICE");
    assert_eq!(reply["error"]["diagnostic"]["rpc_code"], -32000);
    assert!(!reply.to_string().contains("SECRET_SENTINEL"));
}
#[test]
fn api_invalid_effort_and_missing_or_incomplete_catalog_never_send_input() {
    for catalog in [
        json!({"data":[{"id":"native","supportedReasoningEfforts":["low"]}]}),
        json!({}),
        json!({"data":[],"nextCursor":"repeated"}),
    ] {
        let mut calls = 0;
        let result = api(
            "turn_start",
            json!({"thread_id":"exact","prompt":"OK","cwd":"/fixture",
            "model":"native","reasoning_effort":"xhigh"}),
            json!({}),
            |_, a| {
                calls += 1;
                assert_eq!(a["arguments"]["method"], "model/list");
                Ok(json!({"result":catalog}))
            },
        );
        assert!(result.get("error").is_some());
        assert!(calls <= 2);
        assert_eq!(result["patch"]["owned_turns"], json!({}));
    }
}
