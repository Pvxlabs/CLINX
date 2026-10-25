//! Provider request/evidence qualification migrated without weakening IO cases.
#[path = "support/provider.rs"]
mod support;
use clinx_decision_kernel::provider_client::Runtime;
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    os::unix::net::{UnixListener, UnixStream},
    time::{Duration, Instant},
};
use support::Provider;
type Reply = Box<dyn Fn(&Value) -> Vec<Value>>;
struct Scripted {
    reply: Reply,
    sent: Vec<Value>,
    responses: VecDeque<Value>,
    marks: Vec<Value>,
    consumed: Vec<Value>,
}
impl Scripted {
    fn new(reply: impl Fn(&Value) -> Vec<Value> + 'static) -> Self {
        Self {
            reply: Box::new(reply),
            sent: vec![],
            responses: VecDeque::new(),
            marks: vec![],
            consumed: vec![],
        }
    }
    fn call(&mut self, op: &str, args: Value) -> Result<Value, String> {
        assert_eq!(op, "provider.client.io");
        let a = &args["arguments"];
        let v = match args["operation"].as_str().unwrap() {
            "send" => {
                let m = &a["message"];
                self.sent.push(m.clone());
                self.responses.extend((self.reply)(m));
                Value::Null
            }
            "receive" => match self.responses.pop_front() {
                Some(v) => {
                    if v.get("fixture_error").is_some() {
                        return Ok(json!({"error":v["fixture_error"]}));
                    }
                    v
                }
                None => {
                    return Ok(
                        json!({"error":{"kind":"transport","message":"fixture receive timed out"}}),
                    );
                }
            },
            "mark" => {
                self.marks.push(json!([a["id"], a["outcome"]]));
                Value::Null
            }
            "consume_creation" => {
                self.consumed.push(a["cwd"].clone());
                Value::Null
            }
            _ => panic!("unexpected callback {args}"),
        };
        Ok(json!({"result":v}))
    }
}
struct Client {
    runtime: Runtime,
    session: Option<Value>,
    state: Value,
    policy: Value,
    io: Scripted,
    seq: u64,
    timeout: f64,
}
impl Client {
    fn scripted(reply: impl Fn(&Value) -> Vec<Value> + 'static) -> Self {
        Self {
            runtime: Runtime::default(),
            session: None,
            state: json!({"max_received_events":256,"max_method_history":256,"events":[],"received_events":[],"overflow":false}),
            policy: json!({}),
            io: Scripted::new(reply),
            seq: 0,
            timeout: 30.0,
        }
    }
    fn native(path: &std::path::Path, timeout: f64) -> Self {
        let mut c = Self::scripted(|_| panic!("native send must not use callback"));
        c.timeout = timeout;
        let v = c
            .runtime
            .invoke(
                "provider.client.connect",
                &json!({"endpoint":path,"timeout":timeout}),
                |_, _| panic!(),
            )
            .unwrap();
        c.session = Some(v["session"].clone());
        c
    }
    fn call(&mut self, op: &str, mut a: Value) -> Value {
        a["state"] = self.state.clone();
        a["policy"] = self.policy.clone();
        a["timeout"] = a.get("timeout").cloned().unwrap_or(json!(self.timeout));
        if let Some(s) = &self.session {
            a["session"] = s.clone();
        }
        let v = self
            .runtime
            .invoke(&format!("provider.client.{op}"), &a, |op, a| {
                self.io.call(op, a)
            })
            .unwrap();
        if let Some(s) = v.get("state") {
            self.state
                .as_object_mut()
                .unwrap()
                .extend(s.as_object().unwrap().clone());
        }
        if v["creation_consumed"] == true {
            self.policy["native_creation_cwd"] = Value::Null;
        }
        v
    }
    fn request(&mut self, method: &str, params: Value) -> Value {
        self.seq += 1;
        self.call("request",json!({"request":{"id":format!("request-{}",self.seq),"method":method,"params":params}}))
    }
    fn native_value(&self, op: &str) -> Value {
        self.runtime
            .invoke(
                &format!("provider.client.{op}"),
                &json!({"session":self.session}),
                |_, _| panic!(),
            )
            .unwrap()
    }
    fn initialize(&mut self) -> Value {
        Runtime::default().invoke("provider.client.api.initialize",&json!({"arguments":{"client_name":"exact-name","client_title":"Exact","client_version":"1"},"state":{}}),|op,a|{assert_eq!(op,"provider.client.io");let a2=&a["arguments"];match a["operation"].as_str().unwrap(){"request"=>{let v=self.request(a2["method"].as_str().unwrap(),a2["params"].clone());assert!(v.get("error").is_none(),"{v}");Ok(json!({"result":v["value"]}))},"send"=>{let v=self.call("send",json!({"message":a2["message"]}));assert!(v.get("error").is_none(),"{v}");Ok(json!({"result":null}))},_=>panic!("{a}")}}).unwrap()
    }
}
impl Drop for Client {
    fn drop(&mut self) {
        if let Some(s) = &self.session {
            self.runtime
                .invoke(
                    "provider.client.close",
                    &json!({"session":s}),
                    |_, _| panic!(),
                )
                .unwrap();
        }
    }
}
#[test]
fn native_rust_transport_initializes_once_preserves_identity_and_peer_uid() {
    let tmp = tempfile::tempdir().unwrap();
    let p = Provider::new(&tmp.path().join("native.sock"), |r| {
        Some(match r["method"].as_str().unwrap() {
            "initialize" => vec![json!({"id":r["id"],"result":{"userAgent":"fixture"}})],
            "initialized" => vec![],
            _ => vec![
                json!({"id":r["id"],"method":"item/tool/requestUserInput","params":{}}),
                json!({"id":"another-request","result":{"wrong":true}}),
                json!({"id":r["id"],"result":{"data":vec!["row";12000]}}),
            ],
        })
    });
    let mut c = Client::native(&p.path, 1.0);
    assert_eq!(p.calls(), Vec::<Value>::new());
    assert_eq!(
        c.native_value("peer_uid"),
        rustix::process::getuid().as_raw()
    );
    assert_eq!(c.initialize()["value"]["user_agent"], "fixture");
    let result = c.request("model/list", json!({}));
    assert_eq!(result["value"]["data"].as_array().unwrap().len(), 12000);
    assert_eq!(
        c.state["received_events"][0]["method"],
        "item/tool/requestUserInput"
    );
    let calls = p.calls();
    assert_eq!(calls.len(), 3);
    assert_eq!(calls[0]["params"]["clientInfo"]["name"], "exact-name");
    assert_eq!(c.native_value("lifecycle"), "available");
}
#[test]
fn native_unknown_delivery_consumes_creation_and_never_replays() {
    let tmp = tempfile::tempdir().unwrap();
    let p = Provider::new(&tmp.path().join("unknown.sock"), |_| None);
    let mut c = Client::native(&p.path, 0.1);
    c.policy = json!({"shared_native_writer":true,"native_creation_cwd":"/approved"});
    let request = json!({"cwd":"/approved","ephemeral":false,"threadSource":"user"});
    assert_eq!(
        c.request("thread/start", request.clone())["error"]["kind"],
        "transport"
    );
    assert_eq!(c.policy["native_creation_cwd"], Value::Null);
    assert_eq!(
        c.io.consumed,
        json!(["/approved"]).as_array().unwrap().clone()
    );
    assert_eq!(c.native_value("lifecycle"), "unavailable");
    assert_eq!(
        c.request("thread/start", request)["error"]["kind"],
        "shared_writer"
    );
    assert_eq!(
        p.calls()
            .iter()
            .map(|r| r["method"].clone())
            .collect::<Vec<_>>(),
        vec![json!("thread/start")]
    );
}
#[test]
fn admission_rejects_before_transport() {
    for (policy, method, params, kind) in [
        (
            json!({"read_only_observer":true}),
            "thread/resume",
            json!({"threadId":"t"}),
            "protocol",
        ),
        (
            json!({"read_only_observer":true}),
            "thread/list",
            json!({}),
            "protocol",
        ),
        (
            json!({"read_only_observer":true}),
            "command/exec",
            json!({"command":["unsafe"]}),
            "protocol",
        ),
        (
            json!({"shared_native_writer":true}),
            "turn/start",
            json!({"threadId":"t"}),
            "shared_writer",
        ),
        (
            json!({"shared_native_writer":true,"native_forwarding_thread_id":"other"}),
            "thread/resume",
            json!({"threadId":"t"}),
            "shared_writer",
        ),
        (
            json!({"shared_native_writer":true,"native_creation_cwd":"/approved"}),
            "thread/start",
            json!({"cwd":"/elsewhere","ephemeral":false,"threadSource":"user"}),
            "shared_writer",
        ),
    ] {
        let mut c = Client::scripted(|_| vec![]);
        c.policy = policy;
        assert_eq!(c.request(method, params)["error"]["kind"], kind);
        assert!(c.io.sent.is_empty());
    }
}
#[test]
fn exact_metadata_exception_and_index_read_preserve_read_only_scope() {
    let mut c = Client::scripted(|r| vec![json!({"id":r["id"],"result":null})]);
    c.policy = json!({"read_only_observer":true,"workspace_metadata_request":{"command":["fixed","probe"]}});
    for (method, p) in [
        ("command/exec", json!({"command":["fixed","probe"]})),
        ("thread/list", json!({"useStateDbOnly":true})),
    ] {
        let v = c.request(method, p);
        assert!(v.get("error").is_none());
        assert_eq!(v["value"], Value::Null);
    }
    assert_eq!(
        c.request("command/exec", json!({"command":["fixed","other"]}))["error"]["kind"],
        "protocol"
    );
    assert_eq!(c.io.sent.len(), 2);
}
#[test]
fn rpc_preserves_remote_error_and_explicit_result_mark_without_retry() {
    let error =
        json!({"code":-32601,"message":"exact unsupported method","data":{"scope":"fixture"}});
    let e = error.clone();
    let mut c = Client::scripted(move |r| vec![json!({"id":r["id"],"error":e})]);
    let v = c.request("thread/turns/list", json!({"threadId":"exact"}));
    assert_eq!(v["error"]["kind"], "remote");
    assert_eq!(v["error"]["remote"], error);
    assert_eq!(
        c.io.marks,
        vec![json!([c.io.sent[0]["id"], "explicit_rejection"])]
    );
    assert_eq!(c.io.sent.len(), 1);
}
#[test]
fn rpc_missing_result_is_unknown_and_never_retried() {
    let mut c = Client::scripted(|r| vec![json!({"id":r["id"]})]);
    let v = c.request("turn/start", json!({}));
    assert_eq!(v["error"]["kind"], "protocol");
    assert!(
        v["error"]["message"]
            .as_str()
            .unwrap()
            .contains("no result or error")
    );
    assert_eq!(c.io.marks, vec![json!([c.io.sent[0]["id"], "unknown"])]);
    assert_eq!(c.io.sent.len(), 1);
}
#[test]
fn request_events_are_bounded_overflow_explicit_and_tools_unanswered() {
    let mut c = Client::scripted(|r| {
        let mut v = (0..8)
            .map(|i| json!({"method":format!("event/{i}"),"params":{}}))
            .collect::<Vec<_>>();
        v.push(json!({"id":r["id"],"result":{}}));
        v
    });
    c.state["max_received_events"] = json!(3);
    c.state["max_method_history"] = json!(2);
    assert!(c.request("model/list", Value::Null).get("error").is_none());
    assert_eq!(c.state["received_events"].as_array().unwrap().len(), 3);
    assert_eq!(c.state["events"], json!(["event/6", "event/7"]));
    assert_eq!(
        c.call("drain", json!({"max_events":100,"timeout":0}))["value"],
        json!([{"method":"clinx/observation_overflow","params":{"dropped":"bounded_event_buffer_exhausted"}}])
    );
    assert_eq!(c.io.sent.len(), 1);
}
#[test]
fn drain_partial_events_preserves_transport_error() {
    let mut c = Client::scripted(|_| vec![]);
    let event = json!({"id":"approval","method":"approval/request","params":{}});
    c.io.responses = VecDeque::from([
        event.clone(),
        json!({"fixture_error":{"kind":"transport","message":"connection closed"}}),
    ]);
    let v = c.call("drain", json!({"max_events":100,"timeout":0.1}));
    assert_eq!(v["error"]["kind"], "transport");
    assert_eq!(v["error"]["message"], "connection closed");
    assert_eq!(v["drained_events"], json!([event]));
    assert_eq!(c.state["received_events"], json!([]));
    assert!(c.io.sent.is_empty());
}
#[test]
fn nonblocking_empty_drain_is_not_connection_loss() {
    let mut c = Client::scripted(|_| vec![]);
    assert_eq!(
        c.call("drain", json!({"timeout":0,"max_events":100}))["value"],
        json!([])
    );
    for bad in [json!(false), json!(0), json!(101)] {
        assert_eq!(
            c.call("drain", json!({"timeout":0,"max_events":bad}))["error"]["kind"],
            "protocol"
        );
    }
}
fn input(mismatch: &str) {
    let mut c = Client::scripted(|_| vec![]);
    c.timeout = 0.02;
    let mut params = json!({"threadId":"thread","turnId":"turn","item":{"id":"item","type":"userMessage","clientId":"client","content":[{"type":"text","text":"prompt"}]}});
    match mismatch {
        "threadId" | "turnId" => params[mismatch] = json!("wrong"),
        "clientId" => params["item"]["clientId"] = json!("wrong"),
        "text" => params["item"]["content"][0]["text"] = json!("wrong"),
        _ => panic!(),
    };
    c.io.responses
        .push_back(json!({"method":"item/completed","params":params}));
    let v = c.call(
        "confirm",
        json!({"thread_id":"thread","turn":{"id":"turn"},"client_id":"client","prompt":"prompt"}),
    );
    assert_eq!(v["error"]["kind"], "transport");
    assert!(v.get("value").is_none());
    assert!(c.io.sent.is_empty());
}
#[test]
fn native_input_thread_evidence() {
    input("threadId");
}
#[test]
fn native_input_turn_evidence() {
    input("turnId");
}
#[test]
fn native_input_client_evidence() {
    input("clientId");
}
#[test]
fn native_input_text_evidence() {
    input("text");
}
#[test]
fn native_input_confirmation_records_exact_item_without_sending() {
    let mut c = Client::scripted(|_| vec![]);
    let item = json!({"id":"native-item","type":"userMessage","clientId":"client","content":[{"type":"text","text":"prompt"}]});
    let v=c.call("confirm",json!({"thread_id":"thread","turn":{"id":"turn","items":[item]},"client_id":"client","prompt":"prompt"}));
    assert_eq!(
        v["value"],
        json!({"mode":"NATIVE_INPUT_FORWARDING","exclusive_writer":false,"thread_id":"thread","turn_id":"turn","client_message_id":"client","native_item_id":"native-item","input_confirmed":true})
    );
    assert!(c.io.sent.is_empty());
}
#[test]
fn concurrent_clients_cannot_consume_each_others_response() {
    let tmp = tempfile::tempdir().unwrap();
    let providers = (0..4)
        .map(|i| {
            Provider::new(&tmp.path().join(format!("{i}.sock")), |r| {
                Some(vec![json!({"id":r["id"],"result":r["params"]})])
            })
        })
        .collect::<Vec<_>>();
    let workers = providers
        .iter()
        .enumerate()
        .map(|(i, p)| {
            let path = p.path.clone();
            std::thread::spawn(move || {
                Client::native(&path, 1.0).request("thread/read", json!({"threadId":i.to_string()}))
                    ["value"]
                    .clone()
            })
        })
        .collect::<Vec<_>>();
    for (i, w) in workers.into_iter().enumerate() {
        assert_eq!(w.join().unwrap(), json!({"threadId":i.to_string()}));
    }
}
fn observer(
    path: &std::path::Path,
    state: &str,
    status: &str,
    thread: &str,
    turn: &str,
) -> Provider {
    let (state, status, thread, turn) = (
        state.to_owned(),
        status.to_owned(),
        thread.to_owned(),
        turn.to_owned(),
    );
    Provider::new(path, move |r| {
        let result = match r["method"].as_str().unwrap() {
            "initialized" => return Some(vec![]),
            "initialize" => json!({}),
            "thread/read" => {
                json!({"thread":{"id":thread,"status":{"type":state},"sessionId":"session","cwd":"/approved"}})
            }
            "thread/turns/list" => json!({"data":[{"id":turn,"status":status}],"nextCursor":null}),
            _ => panic!("observer attempted mutation: {r}"),
        };
        Some(vec![json!({"id":r["id"],"result":result})])
    })
}
fn observe(mode: &str, endpoints: Value) -> Value {
    Runtime::default().invoke(&format!("provider.client.observation.{mode}"),&json!({"endpoints":endpoints,"native_transport":true,"thread_id":"thread","turn_id":"turn","client_info":{"name":"observer-fixture","title":"Observer","version":"1"}}),|_,_|panic!("unexpected callback")).unwrap()
}
#[test]
fn observer_prefers_exact_live_owner_over_unloaded_interrupted() {
    let t = tempfile::tempdir().unwrap();
    let a = observer(
        &t.path().join("managed.sock"),
        "notLoaded",
        "interrupted",
        "thread",
        "turn",
    );
    let b = observer(
        &t.path().join("native.sock"),
        "active",
        "inProgress",
        "thread",
        "turn",
    );
    let v = observe("execution", json!([a.path, b.path]));
    assert_eq!(v["provider_liveness"], "LIVE");
    assert_eq!(v["owner_endpoint"], json!(b.path));
    assert_eq!(v["transport_health"], "HEALTHY");
    assert_eq!(v["release_safe"], false);
    for p in [&a, &b] {
        assert_eq!(
            p.calls()
                .iter()
                .map(|r| r["method"].clone())
                .collect::<Vec<_>>(),
            vec![
                json!("initialize"),
                json!("initialized"),
                json!("thread/read"),
                json!("thread/turns/list")
            ]
        );
    }
}
#[test]
fn observer_wrong_turn_active_claim_never_proves_completion() {
    let t = tempfile::tempdir().unwrap();
    let a = observer(
        &t.path().join("other.sock"),
        "active",
        "inProgress",
        "thread",
        "other",
    );
    let b = observer(
        &t.path().join("terminal.sock"),
        "idle",
        "completed",
        "thread",
        "turn",
    );
    let v = observe("execution", json!([a.path, b.path]));
    assert_eq!(v["provider_liveness"], "UNKNOWN");
    assert_eq!(v["liveness_reason"], "ACTIVE_OWNER_EXACT_TURN_UNCONFIRMED");
    assert_eq!(v["owner_endpoint"], Value::Null);
    assert_eq!(v["release_safe"], false);
}
#[test]
fn thread_observer_missing_socket_and_wrong_identity_remain_unknown() {
    let t = tempfile::tempdir().unwrap();
    let p = observer(
        &t.path().join("wrong.sock"),
        "idle",
        "completed",
        "another",
        "turn",
    );
    let v = observe("threads", json!([p.path, t.path().join("absent.sock")]));
    assert_eq!(v["state"], "UNKNOWN");
    assert_eq!(v["owner_endpoint"], Value::Null);
    assert_eq!(v["observations"][0]["reason"], "PROVIDER_READ_UNAVAILABLE");
    assert_eq!(v["observations"][1]["state"], "OFFLINE");
    assert_eq!(v["observations"][1]["reason"], "SOCKET_NOT_PRESENT");
    assert_eq!(
        p.calls()
            .iter()
            .map(|r| r["method"].clone())
            .collect::<Vec<_>>(),
        vec![
            json!("initialize"),
            json!("initialized"),
            json!("thread/read")
        ]
    );
}
#[test]
fn liveness_conflict_and_release_rules() {
    for (states, expected, safe) in [
        (
            ["notLoaded", "unloaded"],
            "EXACT_COMPLETED_TURN_UNLOADED",
            true,
        ),
        (["active", "active"], "OWNERSHIP_CONFLICT", false),
        (["idle", "UNKNOWN"], "EXACT_OWNER_TERMINAL", false),
    ] {
        let rows=states.iter().enumerate().map(|(i,s)|json!({"endpoint":i.to_string(),"state":s,"thread_id":"t","turn_id":"turn","turn_status":if *s=="active"{"inProgress"}else{"completed"}})).collect::<Vec<_>>();
        let v = Runtime::default()
            .invoke(
                "provider.client.observation.classify",
                &json!({"observations":rows,"thread_id":"t","turn_id":"turn"}),
                |_, _| panic!(),
            )
            .unwrap();
        assert_eq!(v["liveness_reason"], expected);
        assert_eq!(v["release_safe"], safe);
    }
}
#[test]
fn full_unix_backlog_has_bounded_connect_and_no_provider_request() {
    use rustix::net::{AddressFamily, SocketAddrUnix, SocketFlags, SocketType, bind, listen};
    let t = tempfile::tempdir().unwrap();
    let path = t.path().join("full.sock");
    let socket = rustix::net::socket_with(
        AddressFamily::UNIX,
        SocketType::STREAM,
        SocketFlags::CLOEXEC,
        None,
    )
    .unwrap();
    bind(&socket, &SocketAddrUnix::new(&path).unwrap()).unwrap();
    listen(&socket, 0).unwrap();
    let listener = UnixListener::from(socket);
    let _held = UnixStream::connect(&path).unwrap();
    let start = Instant::now();
    assert!(
        Runtime::default()
            .invoke(
                "provider.client.connect",
                &json!({"endpoint":path,"timeout":0.05}),
                |_, _| panic!()
            )
            .is_err()
    );
    assert!(start.elapsed() < Duration::from_secs(1));
    let (mut peer, _) = listener.accept().unwrap();
    peer.set_nonblocking(true).unwrap();
    use std::io::Read;
    assert_eq!(
        peer.read(&mut [0]).unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
}
#[test]
fn existing_long_timeout_and_truthy_read_only_guard_are_preserved() {
    let mut c = Client::scripted(|r| vec![json!({"id":r["id"],"result":{"accepted":true}})]);
    c.timeout = 300.0;
    assert_eq!(
        c.request("thread/read", json!({"threadId":"thread"}))["value"],
        json!({"accepted":true})
    );
    c.policy["read_only_observer"] = json!(1);
    assert_eq!(
        c.request("turn/start", json!({"threadId":"thread"}))["error"]["kind"],
        "protocol"
    );
    assert_eq!(c.io.sent.len(), 1);
}
