//! Independent private HTTP oracles for passive diagnostics. No real service
//! inspection, pinned embedded Bridge, Provider request or lifecycle control.
#[path = "../src/diagnostics.rs"]
#[allow(dead_code)]
mod diagnostics;
use diagnostics::{BINARY_SHA, PASSIVE_PATHS, VERSION, assess, evaluate, history, read_json};
use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, BufReader, Write},
    net::TcpListener,
    path::Path,
    process::Command,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
fn samples() -> Vec<Value> {
    let value = json!({"status":{"version":VERSION,"client_instance_id":"instance"},"events":[{"seq":1,"source":"notification","method":"remoteControl/status/changed","payload":{"params":{"status":"disabled"}}},{"seq":2,"source":"notification","method":"account/updated"},{"seq":3,"source":"lifecycle","method":"process/ready"}]});
    vec![value.clone(), value]
}
fn check(samples: &[Value]) -> Value {
    assess(samples, true, BINARY_SHA)
}
#[test]
fn quiet_complete_history_is_observation_not_recycle_authority() {
    let r = check(&samples());
    let expected: Value = serde_json::from_str(include_str!(
        "../../fixtures/diagnostics_quiet_expected.json"
    ))
    .unwrap();
    assert_eq!(r, expected);
    assert_eq!(r["activity_observation"], "NO_WORK_OBSERVED");
    assert_eq!(r["safe_idle_gate"], "BLOCKED");
    assert_eq!(r["safe_to_recycle"], false);
    assert!(
        r["blockers"]
            .as_array()
            .unwrap()
            .contains(&json!("UNCERTAIN_ACK_NOT_EXPOSED"))
    );
}
fn work(event: Value) {
    let mut s = samples();
    let mut event = event;
    event["seq"] = json!(4);
    s[1]["events"].as_array_mut().unwrap().push(event);
    let r = check(&s);
    assert_eq!(r["activity_observation"], "UNKNOWN_OR_ACTIVITY_OBSERVED");
    assert_eq!(r["safe_to_recycle"], false);
}
macro_rules! work {($name:ident,$method:literal,$source:literal)=>{#[test]fn $name(){work(json!({"method":$method,"source":$source}));}};}
work!(thread_started, "thread/started", "notification");
work!(turn_started, "turn/started", "notification");
work!(turn_completed, "turn/completed", "notification");
work!(
    approval_requested,
    "item/commandExecution/requestApproval",
    "notification"
);
work!(decode_error, "decode/error", "stdout");
work!(stderr_unknown, "stderr", "stderr");
work!(process_exited, "process/exited", "lifecycle");
#[test]
fn remote_control_enabled() {
    work(
        json!({"method":"remoteControl/status/changed","source":"notification","payload":{"params":{"status":"enabled"}}}),
    );
}
fn drift(change: &str) {
    let mut s = samples();
    match change {
        "ring" => {
            s[1]["events"].as_array_mut().unwrap().remove(0);
        }
        "empty" => s[1]["events"] = json!([]),
        "restart" => s[1]["status"]["client_instance_id"] = json!("new-instance"),
        "version" => s[1]["status"]["version"] = json!("unknown"),
        "sequence" => s[1]["events"][2]["seq"] = json!(9),
        _ => panic!(),
    };
    assert_eq!(check(&s)["complete_quiet_event_history"], false);
}
macro_rules! drift {
    ($name:ident,$change:literal) => {
        #[test]
        fn $name() {
            drift($change);
        }
    };
}
drift!(partial_ring, "ring");
drift!(empty_history, "empty");
drift!(instance_restart, "restart");
drift!(unknown_version, "version");
drift!(broken_sequence, "sequence");
#[test]
fn process_or_binary_change_is_not_idle() {
    assert_eq!(
        assess(&samples(), false, BINARY_SHA)["complete_quiet_event_history"],
        false
    );
    assert_eq!(
        assess(&samples(), true, "unqualified")["complete_quiet_event_history"],
        false
    );
}
macro_rules! malformed {
    ($name:ident,$v:expr) => {
        #[test]
        fn $name() {
            assert!(!history(&$v));
        }
    };
}
malformed!(history_null, Value::Null);
malformed!(history_object, json!({}));
malformed!(history_empty, json!([]));
malformed!(history_null_event, json!([null]));
malformed!(history_missing_fields, json!([{"seq":1}]));
malformed!(
    history_null_params,
    json!([{"seq":1,"source":"notification","method":"remoteControl/status/changed","payload":{"params":null}}])
);
fn refused(path: &str) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    assert_eq!(
        read_json(&base, path).unwrap_err(),
        "PASSIVE_LOOPBACK_ENDPOINT_REQUIRED"
    );
    listener.set_nonblocking(true).unwrap();
    assert_eq!(
        listener.accept().unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
}
#[test]
fn warmup_status_refused() {
    refused("/api/codex/status")
}
#[test]
fn thread_start_refused() {
    refused("/api/codex/thread/start")
}
#[test]
fn turn_start_refused() {
    refused("/api/codex/turn/start")
}
#[test]
fn login_refused() {
    refused("/api/codex/login/device")
}
struct Server {
    base: String,
    calls: Arc<Mutex<Vec<(String, String)>>>,
    stop: Arc<AtomicBool>,
    worker: Option<thread::JoinHandle<()>>,
}
impl Server {
    fn new(reply: impl Fn(&str) -> Vec<u8> + Send + 'static) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let calls = Arc::new(Mutex::new(vec![]));
        let stop = Arc::new(AtomicBool::new(false));
        let (c, s) = (calls.clone(), stop.clone());
        let worker = thread::spawn(move || {
            while !s.load(Ordering::Acquire) {
                let (mut peer, _) = match listener.accept() {
                    Ok(p) => p,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2));
                        continue;
                    }
                    Err(e) => panic!("{e}"),
                };
                peer.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
                let mut r = BufReader::new(peer.try_clone().unwrap());
                let mut line = String::new();
                r.read_line(&mut line).unwrap();
                let words = line.split_whitespace().collect::<Vec<_>>();
                assert_eq!(words.len(), 3);
                let method = words[0].to_owned();
                let path = words[1].to_owned();
                loop {
                    line.clear();
                    r.read_line(&mut line).unwrap();
                    if line == "\r\n" {
                        break;
                    }
                }
                c.lock().unwrap().push((method, path.clone()));
                let bytes = reply(&path);
                let _ = peer.write_all(&bytes);
            }
        });
        Self {
            base,
            calls,
            stop,
            worker: Some(worker),
        }
    }
    fn calls(&self) -> Vec<(String, String)> {
        self.calls.lock().unwrap().clone()
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        let result = self.worker.take().unwrap().join();
        if !thread::panicking() {
            result.unwrap();
        }
    }
}
fn response(body: &[u8]) -> Vec<u8> {
    let mut bytes =
        format!("HTTP/1.0 200 OK\r\nContent-Length: {}\r\n\r\n", body.len()).into_bytes();
    bytes.extend_from_slice(body);
    bytes
}
#[test]
fn real_http_quiet_events_with_hidden_pending_cannot_pass() {
    let pending = 1;
    let warmups = Arc::new(Mutex::new(0));
    let w = warmups.clone();
    let samples = samples();
    let server = Server::new(move |path| {
        let data = match path {
            "/api/status" => samples[0]["status"].clone(),
            "/api/codex/events?limit=1000" => json!({"events":samples[0]["events"]}),
            _ => {
                *w.lock().unwrap() += 1;
                json!({"state":"ready","thread":null,"turn":null})
            }
        };
        response(data.to_string().as_bytes())
    });
    let observed=(0..2).map(|_|json!({"status":read_json(&server.base,PASSIVE_PATHS[0]).unwrap(),"events":read_json(&server.base,PASSIVE_PATHS[1]).unwrap()["events"]})).collect::<Vec<_>>();
    let r = check(&observed);
    assert_eq!(pending, 1);
    assert_eq!(r["complete_quiet_event_history"], true);
    assert_eq!(r["safe_to_recycle"], false);
    assert_eq!(*warmups.lock().unwrap(), 0);
    assert_eq!(
        server.calls(),
        (0..2)
            .flat_map(|_| PASSIVE_PATHS.map(|p| ("GET".into(), p.into())))
            .collect::<Vec<_>>()
    );
}
#[test]
fn redirect_never_reaches_impure_status() {
    let destination = Server::new(|_| panic!("redirect reached impure endpoint"));
    let target = destination.base.clone();
    let server = Server::new(move |_| {
        format!(
            "HTTP/1.0 302 Found\r\nLocation: {target}/api/codex/status\r\nContent-Length: 0\r\n\r\n"
        )
        .into_bytes()
    });
    assert_eq!(
        read_json(&server.base, "/api/status").unwrap_err(),
        "REDIRECT_REFUSED"
    );
    assert_eq!(server.calls(), [("GET".into(), "/api/status".into())]);
    assert!(destination.calls().is_empty());
}
#[test]
fn passive_response_bound_and_chunked_shape_fail_closed() {
    for (body, expected) in [
        (b"[]".to_vec(), "RESPONSE_SHAPE"),
        (b"null".to_vec(), "RESPONSE_SHAPE"),
        (b"invalid".to_vec(), "RESPONSE_SHAPE"),
        (vec![b'x'; 1048577], "RESPONSE_LIMIT"),
    ] {
        let server = Server::new(move |_| response(&body));
        assert_eq!(
            read_json(&server.base, "/api/status").unwrap_err(),
            expected
        );
    }
    let server = Server::new(|_| {
        b"HTTP/1.0 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n".to_vec()
    });
    assert_eq!(read_json(&server.base, "/api/status").unwrap(), json!({}));
}
#[test]
fn passive_invalid_base_and_collect_bounds_precede_io() {
    for base in [
        "http://localhost:1",
        "https://127.0.0.1:1",
        "http://127.0.0.1:01",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:1/",
    ] {
        assert_eq!(
            read_json(base, "/api/status").unwrap_err(),
            "PASSIVE_LOOPBACK_ENDPOINT_REQUIRED"
        );
    }
    for seconds in [0, 31] {
        assert_eq!(
            diagnostics::collect("http://127.0.0.1:1", 0, 0, seconds).unwrap_err(),
            "OBSERVATION_MUST_BE_BOUNDED_1_TO_30_SECONDS"
        );
    }
    assert_eq!(
        diagnostics::collect("http://127.0.0.1:1", 0, 0, 1).unwrap_err(),
        "EXACT_PID_REQUIRED"
    );
}
#[test]
fn health_original_five_metric_assertions() {
    assert!(evaluate(
        "commands_poll_last_successful_timestamp_seconds 999",
        1000.,
        120.
    ));
    assert!(evaluate(
        "commands_poll_last_successful_timestamp_seconds{scope=\"control\"} 9.99e2",
        1000.,
        120.
    ));
    for text in [
        "",
        "commands_poll_last_successful_timestamp_seconds 1",
        "commands_poll_last_successful_timestamp_seconds 1001",
    ] {
        assert!(!evaluate(text, 1000., 120.));
    }
}
#[test]
fn health_duplicate_nonfinite_zero_and_broken_line_samples_remain_unknown() {
    for text in [
        "commands_poll_last_successful_timestamp_seconds 999\ncommands_poll_last_successful_timestamp_seconds 999",
        "commands_poll_last_successful_timestamp_seconds NaN",
        "commands_poll_last_successful_timestamp_seconds Inf",
        "commands_poll_last_successful_timestamp_seconds 0",
        "commands_poll_last_successful_timestamp_seconds\n999",
    ] {
        assert!(!evaluate(text, 1000., 120.));
    }
}
fn commands(file: &Path, base: &str) -> Vec<Vec<String>> {
    vec![
        vec![
            "systemctl".into(),
            "--user".into(),
            "show".into(),
            "clinx-tunnel.service".into(),
            "--property=ActiveState".into(),
            "--property=MainPID".into(),
        ],
        vec![
            "/home/pvxlabs/.local/bin/tunnel-client".into(),
            "health".into(),
            "--url-file".into(),
            file.display().to_string(),
            "--pid".into(),
            "4242".into(),
            "--require-control-plane-poll".into(),
            "--json".into(),
        ],
    ]
    .into_iter()
    .inspect(|_| assert!(base.starts_with("http://127.0.0.1:")))
    .collect()
}
fn healthy(file: &Path, base: &str) -> diagnostics::Result<Value> {
    let wanted = commands(file, base);
    let mut calls = vec![];
    let result = diagnostics::health(file, 120., |args| {
        let index = calls.len();
        calls.push(args.to_vec());
        assert_eq!(args, wanted[index]);
        Ok(if index == 0 {
            "MainPID=4242\nActiveState=active\n".into()
        } else {
            json!({"base_url":base}).to_string()
        })
    });
    assert_eq!(calls, wanted);
    result
}
#[test]
fn health_exact_pid_and_same_listener_are_readonly() {
    let root = tempfile::tempdir().unwrap();
    let url_file = root.path().join("url");
    let file = url_file.clone();
    let server = Server::new(move |path| {
        assert_eq!(path, "/metrics");
        fs::write(&file, "http://127.0.0.1:1").unwrap();
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs_f64();
        response(format!("commands_poll_last_successful_timestamp_seconds {now}\n").as_bytes())
    });
    fs::write(&url_file, &server.base).unwrap();
    assert_eq!(
        healthy(&url_file, &server.base).unwrap(),
        json!({"status":"PASS","pid":4242,"poll_fresh":true,"action":"READ_ONLY"})
    );
    assert_eq!(server.calls(), [("GET".into(), "/metrics".into())]);
}
#[test]
fn health_stale_redirect_and_wrong_endpoint_never_pass() {
    let root = tempfile::tempdir().unwrap();
    let file = root.path().join("url");
    let server = Server::new(|_| response(b"commands_poll_last_successful_timestamp_seconds 1\n"));
    fs::write(&file, &server.base).unwrap();
    assert_eq!(
        healthy(&file, &server.base).unwrap_err(),
        "TUNNEL_CONTROL_POLL_STALE"
    );
    fs::write(&file, "http://127.0.0.1:1").unwrap();
    assert_eq!(
        healthy(&file, &server.base).unwrap_err(),
        "HEALTH_ENDPOINT_IDENTITY_CONFLICT"
    );
    assert_eq!(server.calls().len(), 1);
    let redirect = Server::new(|_| {
        b"HTTP/1.0 302 Found\r\nLocation: http://127.0.0.1:1/metrics\r\nContent-Length: 0\r\n\r\n"
            .to_vec()
    });
    fs::write(&file, &redirect.base).unwrap();
    assert_eq!(
        healthy(&file, &redirect.base).unwrap_err(),
        "REDIRECT_REFUSED"
    );
}
#[test]
fn health_invalid_state_or_max_age_never_runs_probe() {
    let file = Path::new("/unused-private-url-receipt");
    for age in [0., -1., f64::NAN, f64::INFINITY] {
        assert_eq!(
            diagnostics::health(file, age, |_| panic!("invalid age reached subprocess"))
                .unwrap_err(),
            "INVALID_MAX_AGE"
        );
    }
    for state in [
        "ActiveState=inactive\nMainPID=4242\n",
        "ActiveState=active\nMainPID=0\n",
        "ActiveState=active\nMainPID=invalid\n",
    ] {
        let mut calls = 0;
        assert_eq!(
            diagnostics::health(file, 120., |args| {
                calls += 1;
                assert_eq!(args[0], "systemctl");
                Ok(state.into())
            })
            .unwrap_err(),
            "TUNNEL_NOT_ACTIVE"
        );
        assert_eq!(calls, 1);
    }
}
#[test]
fn cli_refuses_invalid_modes_before_reading_live_state() {
    let bin = env!("CARGO_BIN_EXE_clinx-diagnostics");
    for args in [
        vec!["unknown"],
        vec!["embedded-bridge", "--unexpected", "x"],
        vec![
            "embedded-bridge",
            "--base-url",
            "http://127.0.0.1:1",
            "--tunnel-pid",
            "0",
            "--bridge-pid",
            "0",
        ],
        vec!["tunnel-health", "--url-file", "/unused", "--max-age", "NaN"],
    ] {
        let output = Command::new(bin).args(args).output().unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
    }
    assert!(
        Command::new(bin)
            .arg("--help")
            .output()
            .unwrap()
            .status
            .success()
    );
}
