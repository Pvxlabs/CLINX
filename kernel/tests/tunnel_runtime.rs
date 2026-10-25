//! Isolated standalone supervisor acceptance. No production profile or service.
use serde_json::{Value, json};
use std::{
    fs,
    io::{Read, Write},
    net::TcpListener,
    os::unix::fs::PermissionsExt,
    path::Path,
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};
const BIN: &str = env!("CARGO_BIN_EXE_clinx-tunnel");
struct Owned(Child);
impl Drop for Owned {
    fn drop(&mut self) {
        if self.0.try_wait().ok().flatten().is_none() {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
}
fn args(dir: &Path) -> Vec<String> {
    vec![
        "run".into(),
        "--state-dir".into(),
        dir.display().to_string(),
        "--interval".into(),
        "0.03".into(),
        "--cooldown".into(),
        "0.03".into(),
        "--probe-timeout".into(),
        "0.15".into(),
        "--stop-timeout".into(),
        "0.15".into(),
        "--startup-grace".into(),
        "2".into(),
        "--failure-grace".into(),
        "0.1".into(),
        "--max-restarts".into(),
        "2".into(),
        "--".into(),
    ]
}
fn receipt(dir: &Path) -> Value {
    fs::read(dir.join("state.json"))
        .ok()
        .and_then(|v| serde_json::from_slice(&v).ok())
        .unwrap_or(Value::Null)
}
fn until(mut check: impl FnMut() -> bool) {
    let until = Instant::now() + Duration::from_secs(5);
    while !check() {
        assert!(Instant::now() < until, "fixture deadline");
        thread::sleep(Duration::from_millis(10));
    }
}
fn stop(child: &mut Child) {
    rustix::process::kill_process(
        rustix::process::Pid::from_raw(child.id() as i32).unwrap(),
        rustix::process::Signal::TERM,
    )
    .unwrap();
    until(|| child.try_wait().unwrap().is_some());
}
#[test]
fn native_exit_budget_survives_supervisor_restart() {
    let temp = tempfile::tempdir().unwrap();
    let dir = temp.path().join("state");
    let mut command = args(&dir);
    command.push("/bin/true".into());
    for _ in 0..2 {
        let mut child = Owned(
            Command::new(BIN)
                .args(&command)
                .stdout(Stdio::null())
                .spawn()
                .unwrap(),
        );
        until(|| child.0.try_wait().unwrap().is_some());
        assert_eq!(child.0.wait().unwrap().code(), Some(78));
        assert_eq!(receipt(&dir)["launch_sequence"], 3);
        assert_eq!(receipt(&dir)["launches"], 3);
        assert_eq!(receipt(&dir)["reason"], "RECOVERY_BUDGET_EXHAUSTED");
    }
}
#[test]
fn native_exact_owner_lock_signal_and_term_resistant_descendant() {
    let temp = tempfile::tempdir().unwrap();
    let dir = temp.path().join("state");
    let pid_file = temp.path().join("descendant.pid");
    let program = temp.path().join("fixture.py");
    fs::write(&program,format!(r#"import http.server,sys,time,pathlib,os,signal,subprocess
child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(20)'])
pathlib.Path({pid:?}).write_text(str(child.pid))
class H(http.server.BaseHTTPRequestHandler):
 def log_message(self,*a): pass
 def do_GET(self):
  body=('commands_poll_last_successful_timestamp_seconds '+str(time.time())+'\n').encode()
  self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
server=http.server.HTTPServer(('127.0.0.1',0),H)
pathlib.Path(sys.argv[-1]).write_text('http://127.0.0.1:'+str(server.server_port))
server.serve_forever()
"#,pid=pid_file.display().to_string())).unwrap();
    let mut command = args(&dir);
    command.extend(["/usr/bin/python3".into(), program.display().to_string()]);
    let mut bystander = Owned(Command::new("/bin/sleep").arg("20").spawn().unwrap());
    let mut owner = Owned(
        Command::new(BIN)
            .args(&command)
            .stdout(Stdio::null())
            .spawn()
            .unwrap(),
    );
    until(|| receipt(&dir)["status"] == "HEALTHY");
    let generation = receipt(&dir)["child_pid"].clone();
    let follower = Command::new(BIN).args(&command).output().unwrap();
    assert_eq!(follower.status.code(), Some(78));
    assert_eq!(receipt(&dir)["child_pid"], generation);
    assert!(
        Command::new(BIN)
            .args(["status", "--state-dir", dir.to_str().unwrap()])
            .stdout(Stdio::null())
            .status()
            .unwrap()
            .success()
    );
    let start = Instant::now();
    stop(&mut owner.0);
    assert!(start.elapsed() < Duration::from_secs(2));
    assert_eq!(owner.0.wait().unwrap().code(), Some(0));
    assert_eq!(receipt(&dir)["status"], "STOPPED");
    assert!(receipt(&dir)["child_pid"].is_null());
    assert_eq!(
        clinx_decision_kernel::tunnel_supervisor::invoke(
            "tunnel.group_live",
            &json!({"pgid":generation})
        )
        .unwrap(),
        false
    );
    assert!(bystander.0.try_wait().unwrap().is_none());
    let pid: Value = json!(
        fs::read_to_string(pid_file)
            .unwrap()
            .parse::<u32>()
            .unwrap()
    );
    assert!(clinx_decision_kernel::tunnel_supervisor::process_start(&pid).is_none());
}
#[test]
fn unknown_spawn_receipt_and_live_prior_child_never_replay() {
    let temp = tempfile::tempdir().unwrap();
    let dir = temp.path().join("state");
    let mut command = args(&dir);
    command.push("/bin/true".into());
    assert_eq!(
        Command::new(BIN)
            .args(&command)
            .stdout(Stdio::null())
            .status()
            .unwrap()
            .code(),
        Some(78)
    );
    let original = receipt(&dir);
    for state in [
        json!({"reason":"LAUNCH_INTENT","child_pid":null}),
        json!({"reason":"AWAITING_NEW_POLL","child_pid":std::process::id()}),
    ] {
        let mut saved = original.clone();
        for (k, v) in state.as_object().unwrap() {
            saved[k] = v.clone();
        }
        fs::write(dir.join("state.json"), saved.to_string()).unwrap();
        let before = fs::read(dir.join("state.json")).unwrap();
        assert_eq!(
            Command::new(BIN)
                .args(&command)
                .stdout(Stdio::null())
                .status()
                .unwrap()
                .code(),
            Some(78)
        );
        assert_eq!(fs::read(dir.join("state.json")).unwrap(), before);
    }
}
#[test]
fn probe_total_deadline_bounds_trickling_body_and_refuses_redirect() {
    for redirect in [false, true] {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut b = [0; 2048];
            let _ = stream.read(&mut b);
            if redirect {
                let _=stream.write_all(b"HTTP/1.0 302 Found\r\nLocation: http://example.com/\r\nContent-Length: 0\r\n\r\n");
            } else {
                let _ = stream.write_all(b"HTTP/1.0 200 OK\r\nContent-Length: 200\r\n\r\n");
                for _ in 0..20 {
                    if stream.write_all(b"x").is_err() {
                        break;
                    }
                    thread::sleep(Duration::from_millis(20));
                }
            }
        });
        let temp = tempfile::tempdir().unwrap();
        let file = temp.path().join("url");
        fs::write(&file, format!("http://{address}")).unwrap();
        let start = Instant::now();
        let result = clinx_decision_kernel::tunnel_runtime::probe(&file, 120., 0.1);
        assert_eq!(
            result.unwrap_err(),
            if redirect {
                "HEALTH_REDIRECT_REFUSED"
            } else {
                "PROBE_DEADLINE"
            }
        );
        assert!(start.elapsed() < Duration::from_millis(500));
        server.join().unwrap();
    }
}
#[test]
fn native_private_directory_and_identity_are_fail_closed() {
    let temp = tempfile::tempdir().unwrap();
    let dir = temp.path().join("state");
    fs::create_dir(&dir).unwrap();
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o755)).unwrap();
    let mut command = args(&dir);
    command.push("/bin/true".into());
    assert_eq!(
        Command::new(BIN)
            .args(&command)
            .stdout(Stdio::null())
            .status()
            .unwrap()
            .code(),
        Some(78)
    );
    assert!(!dir.join("state.json").exists());
}
