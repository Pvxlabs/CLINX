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
    let program = Path::new(BIN)
        .parent()
        .unwrap()
        .join("examples/qualification_tunnel");
    assert!(
        program.is_file(),
        "build qualification_tunnel example before the suite"
    );
    let mut command = args(&dir);
    command.extend([
        program.display().to_string(),
        "--pid-file".into(),
        pid_file.display().to_string(),
    ]);
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

#[test]
fn native_launch_preserves_original_unicode_identity_and_budget() {
    let expected: Value =
        serde_json::from_str(include_str!("../../fixtures/tunnel_identity_expected.json")).unwrap();
    let temp = tempfile::tempdir().unwrap();
    let dir = temp.path().join("state");
    fs::create_dir(&dir).unwrap();
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(dir.join("state.json"),json!({"identity":expected["identity"],"launches":2,"launch_sequence":7,"reason":"CHILD_EXITED","child_pid":null}).to_string()).unwrap();
    let result = Command::new(BIN)
        .args([
            "run",
            "--state-dir",
            dir.to_str().unwrap(),
            "--interval",
            "0.01",
            "--cooldown",
            "0.01",
            "--max-restarts",
            "2",
            "--",
            "/bin/true",
            "Unicode-中😀",
        ])
        .output()
        .unwrap();
    assert_eq!(result.status.code(), Some(78));
    let r = receipt(&dir);
    assert_eq!(r["identity"], expected["identity"]);
    assert_eq!(r["launches"], 3);
    assert_eq!(r["launch_sequence"], 8);
    assert_eq!(r["reason"], "RECOVERY_BUDGET_EXHAUSTED");
}
#[test]
fn native_cli_rejects_nonfinite_policy_before_any_launch() {
    // NaN/inf cannot be represented in JSON; exercise the original spellings
    // through the real CLI, in addition to the null wire-value checks below.
    for (key, value) in [("--probe-timeout", "NaN"), ("--stable-for", "inf")] {
        let temp = tempfile::tempdir().unwrap();
        let dir = temp.path().join("state");
        let marker = temp.path().join("child-started");
        let result = Command::new(BIN)
            .args([
                "run",
                "--state-dir",
                dir.to_str().unwrap(),
                key,
                value,
                "--",
                "/bin/sh",
                "-c",
                "printf launched > \"$1\"",
                "private-tunnel-qualification",
                marker.to_str().unwrap(),
            ])
            .output()
            .unwrap();
        assert_eq!(result.status.code(), Some(78), "{key}={value}");
        let report: Value = serde_json::from_slice(&result.stdout).unwrap();
        assert_eq!(report["status"], "HOLD");
        assert_eq!(report["task_replay"], false);
        assert!(
            !marker.exists(),
            "invalid policy launched the private child"
        );
        assert!(!dir.exists(), "invalid policy wrote supervisor state");
    }
}
#[test]
fn all_original_poll_budget_and_saved_health_parameters_fail_closed() {
    use clinx_decision_kernel::tunnel_supervisor::{invoke, process_start};
    for value in ["", "NaN", "Inf", "-Inf", "nonsense", "-1", "0", "1001"] {
        assert_eq!(invoke("tunnel.poll",&json!({"metrics":format!("commands_poll_last_successful_timestamp_seconds {value}"),"now":1000,"max_age":120})).unwrap()["fresh"],false,"{value}");
    }
    let line = "commands_poll_last_successful_timestamp_seconds{scope=\"control\"} 999\n";
    assert_eq!(
        invoke(
            "tunnel.poll",
            &json!({"metrics":line,"now":1000,"max_age":120})
        )
        .unwrap()["fresh"],
        true
    );
    assert_eq!(
        invoke(
            "tunnel.poll",
            &json!({"metrics":line.repeat(2),"now":1000,"max_age":120})
        )
        .unwrap()["fresh"],
        false
    );
    let sample=invoke("tunnel.poll",&json!({"metrics":format!("{line}commands_poll_errors_total{{error_kind=\"timeout\"}} 7"),"now":1000,"max_age":120})).unwrap();
    assert_eq!(sample["poll_age"], 1.0);
    assert_eq!(sample["poll_errors"], json!({"timeout":7.0}));
    let temp = tempfile::tempdir().unwrap();
    let policy = json!({"stale_after":0.3,"startup_grace":0.5,"interval":0.05,"probe_timeout":0.3,"stop_timeout":0.2,"cooldown":0.1,"failure_grace":0.1,"max_restarts":2,"stable_for":10});
    for (key, value) in [
        ("interval", json!(0)),
        ("probe_timeout", Value::Null),
        ("cooldown", json!(-1)),
        ("max_restarts", json!(1.5)),
        ("stable_for", Value::Null),
    ] {
        let mut p = policy.clone();
        p[key] = value;
        assert!(invoke("tunnel.policy", &json!({"policy":p})).is_err());
    }
    for fault in ["none", "stale", "dead", "pid_reuse", "old_snapshot"] {
        let mut state = json!({"policy":policy,"status":"HEALTHY","updated_at":1000,"last_poll":1000,"supervisor_pid":std::process::id(),"supervisor_start":process_start(&json!(std::process::id())),"child_pid":std::process::id(),"child_start":process_start(&json!(std::process::id()))});
        match fault {
            "stale" => state["last_poll"] = json!(990),
            "old_snapshot" => state["updated_at"] = json!(990),
            "dead" => state["child_pid"] = json!(999999999),
            "pid_reuse" => state["child_start"] = json!("foreign"),
            _ => {}
        }
        fs::write(temp.path().join("state.json"), state.to_string()).unwrap();
        assert_eq!(
            invoke(
                "tunnel.status",
                &json!({"directory":temp.path(),"now":1000})
            )
            .unwrap()["currently_healthy"],
            fault == "none",
            "{fault}"
        );
    }
}
#[test]
fn corrupt_foreign_and_retired_callback_entries_never_launch() {
    for raw in [
        "{".into(),
        json!({"identity":"foreign","launches":1}).to_string(),
    ] {
        let t = tempfile::tempdir().unwrap();
        let dir = t.path().join("state");
        fs::create_dir(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
        fs::write(dir.join("state.json"), &raw).unwrap();
        let mut command = args(&dir);
        command.push("/bin/true".into());
        let r = Command::new(BIN).args(command).output().unwrap();
        assert_eq!(r.status.code(), Some(78));
        assert_eq!(fs::read_to_string(dir.join("state.json")).unwrap(), raw);
    }
    let r = Command::new(BIN).arg("--compat").output().unwrap();
    assert_eq!(r.status.code(), Some(78));
    assert!(!String::from_utf8(r.stdout).unwrap().contains("callback"));
}
#[test]
fn invalid_urls_reject_without_network() {
    let t = tempfile::tempdir().unwrap();
    let file = t.path().join("url");
    for url in [
        "https://127.0.0.1:80",
        "http://example.com",
        "http://127.0.0.1:80/?key=secret",
    ] {
        fs::write(&file, url).unwrap();
        assert_eq!(
            clinx_decision_kernel::tunnel_runtime::probe(&file, 120., 0.1).unwrap_err(),
            "HEALTH_URL_NOT_LOOPBACK"
        );
    }
}
