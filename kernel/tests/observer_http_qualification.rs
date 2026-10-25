//! Migrated case-for-case from fixtures/test_rust_observer_http.py.
#[path = "support/observer_fixture.rs"]
mod fixture;
use clinx_decision_kernel::{node_tls::Result, observer_http::Runtime};
use fixture::{Callback, Server, http, raw};
use serde_json::{Value, json};
use std::{
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    sync::{
        Arc, Condvar, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::Duration,
};
const TOKEN: &str = "observer_qualification_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx";
const TID: &str = "12345678-1234-1234-1234-123456789abc";
const OID: &str = "obs_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
struct State {
    credential: Mutex<Value>,
    health: Mutex<Value>,
    reads: Mutex<Vec<Value>>,
    fault: Mutex<Value>,
    audit: Mutex<Vec<Value>>,
    blocked: AtomicBool,
    entered: Mutex<usize>,
    changed: Condvar,
    released: Mutex<bool>,
}
impl State {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            credential: Mutex::new(json!(TOKEN)),
            health: Mutex::new(json!({"value":{"ok":true}})),
            reads: Default::default(),
            fault: Mutex::new(Value::Null),
            audit: Default::default(),
            blocked: AtomicBool::new(false),
            entered: Mutex::new(0),
            changed: Condvar::new(),
            released: Mutex::new(false),
        })
    }
    fn callback(&self, op: &str, a: Value) -> Result<Value> {
        Ok(match op {
            "observer_http.context" => {
                json!({"value":{"credential":*self.credential.lock().unwrap(),"network_configured":true}})
            }
            "observer_http.credential" => json!({"value":*self.credential.lock().unwrap()}),
            "observer_http.audit" => {
                self.audit.lock().unwrap().push(a);
                json!({"value":null})
            }
            "observer_http.read" => {
                self.reads.lock().unwrap().push(a.clone());
                let fault = self.fault.lock().unwrap().clone();
                if !fault.is_null() {
                    return Ok(fault);
                }
                let args = &a["arguments"];
                let v = match a["target"].as_str().unwrap() {
                    "store" if a["method"] == "health" => {
                        if self.blocked.load(Ordering::Acquire) {
                            let mut n = self.entered.lock().unwrap();
                            *n += 1;
                            self.changed.notify_all();
                            drop(n);
                            let done = self.released.lock().unwrap();
                            let (done, timeout) = self
                                .changed
                                .wait_timeout_while(done, Duration::from_secs(5), |done| !*done)
                                .unwrap();
                            assert!(*done && !timeout.timed_out());
                        }
                        return Ok(self.health.lock().unwrap().clone());
                    }
                    "store" => args.clone(),
                    "network" => {
                        let mut args = args.clone();
                        let id = args.as_object_mut().unwrap().remove("observation_id");
                        if let Some(id) = id {
                            json!({"method":a["method"],"id":id,"args":args})
                        } else {
                            json!({"method":a["method"],"args":args})
                        }
                    }
                    "native" => json!({"native":true}),
                    "collector" => args.clone(),
                    _ => panic!("unexpected read {a}"),
                };
                json!({"value":v})
            }
            _ => panic!("unexpected callback {op}"),
        })
    }
    fn call(self: &Arc<Self>) -> Callback {
        let state = self.clone();
        Arc::new(move |op, a| state.callback(op, a))
    }
    fn request(self: &Arc<Self>, path: &str) -> Value {
        let state = self.clone();
        Runtime::default()
            .invoke(
                "observer_http.request",
                &json!({"method":"GET","target":path,"authorization":format!("Bearer {TOKEN}")}),
                move |op, a| state.callback(op, a),
            )
            .unwrap()
    }
}
fn get(server: &Server, path: &str) -> fixture::Response {
    http(
        server.address(),
        "GET",
        path,
        &[("Authorization", &format!("Bearer {TOKEN}"))],
        &[],
    )
}
#[test]
fn routes_and_exact_callback_arguments() {
    let api = State::new();
    let cases = [
        ("/v1/health".into(), json!({"ok":true})),
        (
            "/v1/tasks?state=active".into(),
            json!({"state":"active","offset":0}),
        ),
        (
            "/v1/tasks?state=recent&offset=000123".into(),
            json!({"state":"recent","offset":123}),
        ),
        ("/v1/tasks/task_A".into(), json!({"task_ref":"task_A"})),
        (
            "/v1/tasks/task_A/events?after=a%2Bb".into(),
            json!({"task_ref":"task_A","after":"a+b"}),
        ),
        (
            "/v1/tasks/task_A/activity?execution_ref=exec_A&before=checkpoint".into(),
            json!({"task_ref":"task_A","execution_ref":"exec_A","after":null,"before":"checkpoint"}),
        ),
        (
            "/v2/observations?node=mac&limit=%2B1_0".into(),
            json!({"method":"list","args":{"node":"mac","limit":10}}),
        ),
        (
            "/v2/observations?limit=１２".into(),
            json!({"method":"list","args":{"limit":12}}),
        ),
        (
            format!("/v2/observations/{OID}?limit=4"),
            json!({"method":"detail","id":OID,"args":{"limit":4}}),
        ),
        (
            format!("/v2/observations/{OID}/context?cursor=x"),
            json!({"method":"context","id":OID,"args":{"cursor":"x"}}),
        ),
        (
            format!("/v2/observations/{OID}/activity?limit=8"),
            json!({"method":"activity","id":OID,"args":{"limit":8}}),
        ),
    ];
    for (path, expected) in cases {
        assert_eq!(api.request(&path), json!({"value":expected}), "{path}");
    }
}
#[test]
fn invalid_route_query_bounds() {
    let api = State::new();
    let mut cases = vec![];
    for path in [
        "/v1/tasks",
        "/v1/tasks?state=all",
        "/v1/tasks?state=active&offset=-1",
        "/v1/tasks?state=active&offset=100001",
        "/v1/tasks?state=active&offset=0000001",
        "/v1/tasks?state=active&offset=１２",
        "/v1/health?x",
        "/v1/health?x=1&x=2",
        "/v1/health?x=1&%78=2",
        "/v1/tasks/task_A/activity?execution_ref=e&after=x&before=y",
        "/v1/tasks/task_A/activity?execution_ref=e&after=",
        "/v1/tasks/task_A/activity?execution_ref=",
        "/v2/observations?limit=1__2",
        "/v2/observations?limit=1.5",
        "/v2/observations?limit=²",
    ] {
        cases.push((path.to_owned(), 400, "INVALID_QUERY"));
    }
    cases.extend([
        (
            format!(
                "/v1/health?{}",
                (0..9)
                    .map(|i| format!("x{i}=1"))
                    .collect::<Vec<_>>()
                    .join("&")
            ),
            400,
            "INVALID_QUERY",
        ),
        (
            format!("/v2/observations/{OID}/context?limit=2"),
            400,
            "INVALID_QUERY",
        ),
        (
            format!("/v2/native/{TID}?context=true"),
            400,
            "INVALID_QUERY",
        ),
    ]);
    for path in [
        "/v1/health#fragment",
        "https://foreign/v1/health",
        "//foreign/v1/health",
        "/v1/%68ealth",
    ] {
        cases.push((path.to_owned(), 400, "INVALID_REQUEST"));
    }
    cases.extend([
        ("/v1/health?extra=1".into(), 404, "NOT_FOUND"),
        ("/does-not-exist".into(), 404, "NOT_FOUND"),
        (format!("/{}", "x".repeat(2048)), 400, "INVALID_REQUEST"),
    ]);
    assert_eq!(cases.len(), 25);
    for (path, status, code) in cases {
        let result = api.request(&path);
        assert_eq!(result["fault"]["status"], status, "{path}");
        assert_eq!(result["fault"]["code"], code, "{path}");
    }
}
#[test]
fn native_collector_and_errors_use_exact_rust_route() {
    let api = State::new();
    assert_eq!(
        api.request(&format!(
            "/v2/native/{TID}?context=1&recent_turns=2&max_bytes=300&cursor=exact"
        ))["value"],
        json!({"native":true})
    );
    assert_eq!(
        api.reads.lock().unwrap()[0],
        json!({"target":"native","method":"observe","arguments":{"thread_id":TID,"context":true,"recent_turns":2,"max_bytes":300,"cursor":"exact"}})
    );
    assert_eq!(
        api.request(&format!(
            "/v2/native-collector/sample/{TID}?checkpoint=exact"
        ))["value"],
        json!({"route":format!("sample/{TID}"),"query":{"checkpoint":"exact"}})
    );
    *api.fault.lock().unwrap() =
        json!({"fault":{"kind":"coded","code":"NATIVE_COLLECTOR_CURSOR_RESET"}});
    let r = api.request(&format!("/v2/native-collector/sample/{TID}"));
    assert_eq!(r["fault"]["status"], 409);
    assert_eq!(r["fault"]["code"], "NATIVE_COLLECTOR_CURSOR_RESET");
    *api.fault.lock().unwrap() = json!({"fault":{"kind":"coded","code":"OBSERVATION_NOT_FOUND"}});
    let r = api.request(&format!("/v2/observations/{OID}"));
    assert_eq!(r["fault"]["status"], 404);
    assert_eq!(r["fault"]["code"], "OBSERVATION_NOT_FOUND");
}
#[test]
fn wide_integer_wire_preserves_exact_decimal() {
    let api = State::new();
    let decimal = "9".repeat(100);
    api.request(&format!("/v2/observations?limit={decimal}"));
    assert_eq!(
        api.reads.lock().unwrap()[0]["arguments"]["limit"],
        json!({"__observer_decimal":decimal})
    );
}
#[test]
fn http_auth_rotation_readonly_redaction_response_bound_and_audit() {
    let api = State::new();
    let mut server = Server::new(api.call(), true);
    *api.health.lock().unwrap() = json!({"value":{"secret":TOKEN,"text":"中文\u{7f}🙂"}});
    let r = get(&server, "/v1/health");
    assert_eq!(r.status, 200);
    assert!(!String::from_utf8_lossy(&r.body).contains(TOKEN));
    assert!(r.body.is_ascii() && r.body.windows(6).any(|b| b == b"\\u007f"));
    assert_eq!(
        r.value(),
        json!({"secret":"[REDACTED]","text":"中文\u{7f}🙂"})
    );
    assert_eq!(r.headers["Cache-Control"], "no-store");
    assert_eq!(r.headers["Connection"], "close");
    assert_eq!(r.headers["X-Content-Type-Options"], "nosniff");
    assert_eq!(
        r.headers["Content-Length"].parse::<usize>().unwrap(),
        r.body.len()
    );
    assert_eq!(
        http(server.address(), "GET", "/v1/health", &[], &[]).status,
        401
    );
    assert_eq!(
        http(
            server.address(),
            "GET",
            "/v1/health",
            &[("Authorization", "Bearer wrong")],
            &[]
        )
        .status,
        401
    );
    let auth = format!("Bearer {TOKEN}");
    assert_eq!(
        http(
            server.address(),
            "POST",
            &format!("/private-{TOKEN}"),
            &[("Authorization", &auth)],
            &[]
        )
        .status,
        405
    );
    assert_eq!(
        http(
            server.address(),
            TOKEN,
            "/v1/health",
            &[("Authorization", &auth)],
            &[]
        )
        .status,
        405
    );
    let r = http(
        server.address(),
        "HEAD",
        "/v1/health",
        &[("Authorization", &auth)],
        &[],
    );
    assert_eq!(r.status, 405);
    assert_eq!(r.headers["Allow"], "GET");
    assert!(r.body.is_empty());
    *api.credential.lock().unwrap() = json!(format!("new_{TOKEN}"));
    assert_eq!(get(&server, "/v1/health").status, 401);
    *api.credential.lock().unwrap() = json!(TOKEN);
    *api.health.lock().unwrap() = json!({"value":{"large":"x".repeat(2*1024*1024)}});
    let r = get(&server, "/v1/health");
    assert_eq!(r.status, 503);
    assert_eq!(r.value()["error"], "RESPONSE_BOUND_EXCEEDED");
    server.close();
    let audit = api.audit.lock().unwrap();
    assert_eq!(
        audit
            .iter()
            .map(|v| v["observer_http_method"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["GET", "GET", "GET", "POST", "OTHER", "HEAD", "GET", "GET"]
    );
    assert!(audit.iter().all(|v| {
        v.as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>()
            == ["observer_http_method", "status"]
    }));
    assert!(!json!(*audit).to_string().contains(TOKEN));
}
#[test]
fn duplicate_auth_body_source_faults_and_auth_unavailable() {
    let api = State::new();
    let server = Server::new(api.call(), false);
    let auth = format!("Bearer {TOKEN}");
    assert_eq!(
        http(
            server.address(),
            "GET",
            "/v1/health",
            &[("Authorization", &auth), ("Authorization", &auth)],
            &[]
        )
        .status,
        401
    );
    assert_eq!(
        http(
            server.address(),
            "GET",
            "/v1/health",
            &[("Authorization", &auth)],
            b"x"
        )
        .status,
        400
    );
    assert_eq!(
        http(
            server.address(),
            "GET",
            "/v1/health",
            &[("Authorization", &auth), ("Transfer-Encoding", "chunked")],
            &[]
        )
        .status,
        400
    );
    for (kind, expected) in [
        ("source", "SOURCE_UNAVAILABLE"),
        ("other", "PROJECTION_UNAVAILABLE"),
    ] {
        *api.health.lock().unwrap() = json!({"fault":{"kind":kind,"code":"sensitive source"}});
        let r = get(&server, "/v1/health");
        assert_eq!(r.status, 503);
        assert_eq!(r.value()["error"], expected);
    }
    *api.credential.lock().unwrap() = json!("invalid");
    let r = get(&server, "/v1/health");
    assert_eq!(r.status, 503);
    assert_eq!(r.value()["error"], "AUTH_UNAVAILABLE");
}
#[test]
fn listener_restart_close_and_new_socket_ownership() {
    let api = State::new();
    let mut server = Server::new(api.call(), false);
    for _ in 0..2 {
        assert_eq!(get(&server, "/v1/health").status, 200);
        server.stop();
        server.start();
    }
    let address = server.address();
    server.close();
    server.close();
    let listener = TcpListener::bind(address).unwrap();
    assert_eq!(listener.local_addr().unwrap(), address);
}
#[test]
fn http_parser_bounds_and_version() {
    let api = State::new();
    let cases = [
        (b"GET / HTTP/2.0\r\n\r\n".to_vec(), 505),
        (
            format!("GET / HTTP/1.1\r\n{}\r\n", "X: a\r\n".repeat(100)).into_bytes(),
            431,
        ),
        (
            format!("GET / HTTP/1.1\r\nX: {}\r\n\r\n", "a".repeat(65536)).into_bytes(),
            431,
        ),
        (
            format!("GET /{} HTTP/1.1\r\n\r\n", "x".repeat(65536)).into_bytes(),
            414,
        ),
    ];
    for (wire, status) in cases {
        let server = Server::new(api.call(), false);
        assert!(raw(server.address(), &wire).starts_with(format!("HTTP/1.0 {status} ").as_bytes()));
    }
}
#[test]
fn sixteen_concurrent_requests_bound_without_leaking_slots() {
    let api = State::new();
    api.blocked.store(true, Ordering::Release);
    let server = Server::new(api.call(), false);
    let mut handles = vec![];
    for _ in 0..16 {
        let address = server.address();
        handles.push(thread::spawn(move || {
            http(
                address,
                "GET",
                "/v1/health",
                &[("Authorization", &format!("Bearer {TOKEN}"))],
                &[],
            )
            .status
        }));
    }
    let entered = api.entered.lock().unwrap();
    let (entered, timeout) = api
        .changed
        .wait_timeout_while(entered, Duration::from_secs(3), |n| *n < 16)
        .unwrap();
    let count = *entered;
    drop(entered);
    if timeout.timed_out() || count != 16 {
        *api.released.lock().unwrap() = true;
        api.changed.notify_all();
        panic!("16 requests must be accepted; received {count}");
    }
    let mut overflow = TcpStream::connect(server.address()).unwrap();
    overflow
        .set_read_timeout(Some(Duration::from_secs(3)))
        .unwrap();
    overflow
        .write_all(b"GET /v1/health HTTP/1.0\r\n\r\n")
        .unwrap();
    let mut b = [0];
    let result = overflow.read(&mut b);
    *api.released.lock().unwrap() = true;
    api.changed.notify_all();
    assert!(
        matches!(result, Ok(0))
            || result.is_err_and(|e| e.kind() == std::io::ErrorKind::ConnectionReset)
    );
    assert_eq!(*api.entered.lock().unwrap(), 16);
    assert_eq!(
        handles
            .into_iter()
            .map(|h| h.join().unwrap())
            .collect::<Vec<_>>(),
        vec![200; 16]
    );
    api.blocked.store(false, Ordering::Release);
    assert_eq!(get(&server, "/v1/health").status, 200);
}
