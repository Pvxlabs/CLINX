//! Frozen original urllib/BridgeConfig oracles and actual native CLI processes.
//! All state is disposable; no LAN discovery, deployed Helper or task is touched.
use clinx_decision_kernel::discovery_bootstrap;
use serde_json::{Value, json};
use std::{
    fs,
    io::Write,
    os::unix::fs::{PermissionsExt, symlink},
    path::{Path, PathBuf},
    process::{Command, Output, Stdio},
    thread,
    time::{Duration, Instant},
};

fn run(mut command: Command, input: &[u8]) -> Output {
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child.stdin.take().unwrap().write_all(input).unwrap();
    let until = Instant::now() + Duration::from_secs(10);
    while child.try_wait().unwrap().is_none() {
        if Instant::now() >= until {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("native CLI deadline");
        }
        thread::sleep(Duration::from_millis(5));
    }
    child.wait_with_output().unwrap()
}
fn node(root: &Path, arguments: &[&str], input: &Value) -> Output {
    let mut c = Command::new(env!("CARGO_BIN_EXE_clinx-node"));
    c.args(arguments)
        .current_dir(root)
        .env_clear()
        .env("HOME", root.join("home"))
        .env("PATH", "/nonexistent")
        .env("XDG_RUNTIME_DIR", root.join("run"));
    run(c, format!("{input}\n").as_bytes())
}
fn config(root: &Path, op: &str, arguments: Value) -> Value {
    let output = node(
        root,
        &[],
        &json!({"protocol":"CLINX_REQUEST_PIPE_V1","mode":"config","operation":op,"arguments":arguments}),
    );
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(output.stderr.is_empty());
    let response: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(response["kind"], "result");
    assert!(response.get("error").is_none(), "{response}");
    response["result"].clone()
}
#[test]
fn every_original_urllib_field_combination_retains_frozen_independent_result() {
    let oracle: Value = serde_json::from_str(include_str!(
        "../../fixtures/device_connection_oracle_v1.json"
    ))
    .unwrap();
    assert_eq!(oracle["cases"].as_array().unwrap().len(), 812);
    for case in oracle["cases"].as_array().unwrap() {
        let result = match discovery_bootstrap::validate(&case["input"]) {
            Ok(value) => json!({"result":value}),
            Err(error) => json!({"error":error}),
        };
        assert_eq!(result, case["expected"], "input={}", case["input"]);
    }
}
#[test]
fn native_toml_preserves_original_bridge_config_symlinks_order_and_aliases() {
    let t = tempfile::tempdir().unwrap();
    let root = t.path();
    fs::create_dir_all(root.join("deep/repository")).unwrap();
    symlink("deep/repository", root.join("link")).unwrap();
    let raw = include_str!("../../fixtures/device_config_oracle_v1.json")
        .replace("@ROOT@", root.to_str().unwrap())
        .replace("@HOME@", root.join("home").to_str().unwrap());
    let expected: Value = serde_json::from_str(&raw).unwrap();
    let path = root.join("config.toml");
    fs::write(&path, expected["toml"].as_str().unwrap()).unwrap();
    let native = config(root, "native_load", json!({"path":path}));
    assert_eq!(native, expected["native_load"]);
    assert_eq!(
        native["projects"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| p["alias"].clone())
            .collect::<Vec<_>>(),
        vec![json!("z"), json!("a")]
    );
    assert_eq!(
        native["projects"][0]["repo"],
        json!(root.join("deep/absent"))
    );
    let api = config(root, "api_config", json!({"config":native}));
    assert_eq!(api, expected["api_config"]);
    assert_eq!(api["host_aliases"]["ss"], json!(["air", "imac"]));
}
#[test]
fn native_gui_private_pipe_is_bounded_python_free_and_forget_idempotent() {
    let t = tempfile::tempdir().unwrap();
    for wire in ["[]\n".to_owned(), format!("{}\n", "x".repeat(4097))] {
        let mut c = Command::new(env!("CARGO_BIN_EXE_clinx-node"));
        c.arg("--discovery-gui")
            .env_clear()
            .env("HOME", t.path())
            .env("PATH", "/nonexistent");
        let output = run(c, wire.as_bytes());
        assert!(output.status.success());
        assert!(output.stderr.is_empty());
        assert_eq!(
            serde_json::from_slice::<Value>(&output.stdout).unwrap(),
            json!({"error":"DISCOVERY_UNAVAILABLE"})
        );
    }
    for _ in 0..2 {
        let mut c = Command::new(env!("CARGO_BIN_EXE_clinx-node"));
        c.arg("--discovery-gui")
            .env_clear()
            .env("HOME", t.path())
            .env("PATH", "/nonexistent");
        let output = run(c, b"{\"operation\":\"forget\",\"node_id\":\"missing\"}\n");
        assert!(output.status.success());
        assert!(output.stderr.is_empty());
        assert_eq!(
            serde_json::from_slice::<Value>(&output.stdout).unwrap(),
            json!({"forgotten":true})
        );
    }
    let peers: Value = serde_json::from_slice(
        &fs::read(
            t.path()
                .join("Library/Application Support/CLINX Monitor/Devices/peers.json"),
        )
        .unwrap(),
    )
    .unwrap();
    assert_eq!(peers["peers"], json!({}));
}
fn launcher() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .join("bin/clinx")
}
#[test]
fn native_device_cli_and_shell_launcher_require_no_python_and_keep_exact_arguments() {
    let t = tempfile::tempdir().unwrap();
    let binary = env!("CARGO_BIN_EXE_clinx-device");
    let mut c = Command::new(binary);
    c.arg("--device-cli-contract");
    let o = run(c, b"");
    assert!(o.status.success());
    assert_eq!(o.stdout, b"RUST_DEVICE_CLI_V1\n");
    let mut c = Command::new(launcher());
    c.arg("--help")
        .current_dir(t.path())
        .env("CLINX_DEVICE_BINARY", binary)
        .env("CLINX_PYTHON", "/absent-python");
    let o = run(c, b"");
    assert!(o.status.success());
    assert!(
        String::from_utf8(o.stdout)
            .unwrap()
            .contains("devices|pair")
    );
    let recorder = t.path().join("record-cli");
    fs::write(&recorder, "#!/bin/sh\nprintf '%s\\0' \"$@\"\n").unwrap();
    fs::set_permissions(&recorder, fs::Permissions::from_mode(0o700)).unwrap();
    let cfg = t.path().join("a config.toml");
    let mut c = Command::new(launcher());
    c.args([
        "--config",
        cfg.to_str().unwrap(),
        "devices",
        "--display-name",
        "A device",
        "--json",
    ])
    .current_dir(t.path())
    .env("CLINX_DEVICE_BINARY", &recorder)
    .env("CLINX_PYTHON", "/absent-python");
    let o = run(c, b"");
    assert!(o.status.success());
    let args = o
        .stdout
        .split(|b| *b == 0)
        .filter(|v| !v.is_empty())
        .map(|v| String::from_utf8(v.to_vec()).unwrap())
        .collect::<Vec<_>>();
    assert_eq!(
        args,
        vec![
            "--config".to_owned(),
            launcher()
                .parent()
                .unwrap()
                .parent()
                .unwrap()
                .join("bridge.toml")
                .display()
                .to_string(),
            "--config".into(),
            cfg.display().to_string(),
            "devices".into(),
            "--display-name".into(),
            "A device".into(),
            "--json".into()
        ]
    );
}
#[test]
fn native_cli_rejects_every_original_invalid_input_before_state_or_network() {
    let t = tempfile::tempdir().unwrap();
    let state = t.path().join("not-created");
    for (args, code) in [
        (
            vec!["pair", "accept", "--json"],
            "PAIRING_REQUIRES_LOCAL_TTY",
        ),
        (vec!["pair", "accept"], "PAIRING_REQUIRES_LOCAL_TTY"),
        (
            vec!["devices", "--wait", "nan"],
            "INVALID_DISCOVERY_INTERVAL",
        ),
        (
            vec!["devices", "--wait", "31"],
            "INVALID_DISCOVERY_INTERVAL",
        ),
        (
            vec!["devices", "--allow-dev-pairing"],
            "SECURITY_BLOCKER_DEV_ONLY",
        ),
    ] {
        let mut c = Command::new(env!("CARGO_BIN_EXE_clinx-device"));
        c.arg("--config")
            .arg(t.path().join("missing.toml"))
            .args(args)
            .arg("--state-dir")
            .arg(&state);
        let o = run(c, b"");
        assert_eq!(o.status.code(), Some(2));
        assert_eq!(
            String::from_utf8(o.stderr).unwrap(),
            format!("DEVICE_ERROR: {code}\n")
        );
        assert!(o.stdout.is_empty());
        assert!(!state.exists());
    }
    let mut c = Command::new(launcher());
    c.args(["devices", "--allow-dev-pairing", "--state-dir"])
        .arg(&state)
        .env("CLINX_DEVICE_BINARY", env!("CARGO_BIN_EXE_clinx-device"))
        .env("CLINX_PYTHON", "/absent-python");
    let o = run(c, b"");
    assert_eq!(o.status.code(), Some(2));
    assert_eq!(o.stderr, b"DEVICE_ERROR: SECURITY_BLOCKER_DEV_ONLY\n");
    assert!(o.stdout.is_empty());
    assert!(!state.exists());
    let mut missing = Command::new(launcher());
    missing
        .args(["devices", "--state-dir"])
        .arg(&state)
        .env("CLINX_DEVICE_BINARY", t.path().join("absent-native-device"));
    let output = run(missing, b"");
    assert_eq!(output.status.code(), Some(2));
    assert_eq!(
        output.stderr,
        b"DEVICE_ERROR: RUST_DEVICE_BINARY_REQUIRED\n"
    );
    assert!(output.stdout.is_empty());
    assert!(!state.exists());
}
#[cfg(unix)]
#[test]
fn pairing_terminal_cancellation_restores_every_original_termios_field() {
    use std::os::fd::FromRawFd;
    unsafe extern "C" {
        fn posix_openpt(flags: i32) -> i32;
        fn grantpt(fd: i32) -> i32;
        fn unlockpt(fd: i32) -> i32;
        fn ptsname(fd: i32) -> *const std::ffi::c_char;
    }
    // POSIX PTY handles only; no host terminal is read or modified.
    let master = unsafe {
        posix_openpt((rustix::fs::OFlags::RDWR | rustix::fs::OFlags::NOCTTY).bits() as i32)
    };
    assert!(master >= 0);
    let master_file = unsafe { fs::File::from_raw_fd(master) };
    assert_eq!(unsafe { grantpt(master) }, 0);
    assert_eq!(unsafe { unlockpt(master) }, 0);
    let name = unsafe { std::ffi::CStr::from_ptr(ptsname(master)) }
        .to_str()
        .unwrap();
    let slave = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(name)
        .unwrap();
    let before = rustix::termios::tcgetattr(&slave).unwrap();
    let t = tempfile::tempdir().unwrap();
    let mut bootstrap = Command::new(env!("CARGO_BIN_EXE_clinx-node-daemon"));
    bootstrap
        .arg("--state")
        .arg(t.path().join("device"))
        .args(["--node-id", "air", "bootstrap"]);
    assert!(run(bootstrap, b"").status.success());
    let mut child = Command::new(env!("CARGO_BIN_EXE_clinx-node-daemon"))
        .arg("--state")
        .arg(t.path().join("device"))
        .args([
            "--node-id",
            "air",
            "pair-connect",
            "--peer",
            "centre",
            "--fingerprint",
            "fixture",
            "--endpoint",
            "tls://127.0.0.1:1",
        ])
        .stdin(slave.try_clone().unwrap())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let until = Instant::now() + Duration::from_secs(3);
    while rustix::termios::tcgetattr(&slave)
        .unwrap()
        .local_modes
        .contains(rustix::termios::LocalModes::ECHO)
    {
        if Instant::now() >= until {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("pairing did not hide PTY echo")
        };
        thread::sleep(Duration::from_millis(10));
    }
    rustix::process::kill_process(
        rustix::process::Pid::from_raw(child.id() as i32).unwrap(),
        rustix::process::Signal::INT,
    )
    .unwrap();
    let until = Instant::now() + Duration::from_secs(3);
    while child.try_wait().unwrap().is_none() {
        if Instant::now() >= until {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("pair cancellation deadline")
        };
        thread::sleep(Duration::from_millis(5));
    }
    let o = child.wait_with_output().unwrap();
    assert_eq!(o.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&o.stderr).contains("PAIRING_CANCELLED"));
    assert!(o.stdout.is_empty());
    let after = rustix::termios::tcgetattr(&slave).unwrap();
    assert_eq!(after.input_modes, before.input_modes);
    assert_eq!(after.output_modes, before.output_modes);
    assert_eq!(after.control_modes, before.control_modes);
    assert_eq!(after.local_modes, before.local_modes);
    assert_eq!(
        format!("{:?}", after.special_codes),
        format!("{:?}", before.special_codes)
    );
    assert_eq!(after.input_speed(), before.input_speed());
    assert_eq!(after.output_speed(), before.output_speed());
    drop(master_file);
}

#[path = "support/node_qualification.rs"]
mod network;
fn enrollment_server(w: &network::Worker, root: &Path, node: &str) -> (Value, Value, Value) {
    let identity = w
        .call("discovery.identity.open", json!({"root":root,"host":node}))
        .unwrap();
    let server = w
        .call(
            "discovery.enrollment.server.start",
            json!({"root":root,"address":"127.0.0.1","port":0}),
        )
        .unwrap();
    let p = &identity["public"];
    let candidate = json!({"node_id":p["node_id"],"display_name":p["display_name"],"fingerprint":p["fingerprint"],"endpoints":[["127.0.0.1",server["port"]]],"mismatch":false});
    (identity, server, candidate)
}
fn enroll(
    a: &network::Worker,
    b: &network::Worker,
    root: &Path,
    server: &Value,
    target: &Value,
) -> Value {
    let pin = b
        .call(
            "discovery.window.open",
            json!({"window":server["window"],"lifetime":60}),
        )
        .unwrap();
    a.call(
        "discovery.enrollment.client.pair",
        json!({"root":root,"candidate":target,"code":pin}),
    )
    .unwrap()
}
#[test]
fn native_pairing_keeps_private_files_zero_authority_reconnect_and_rejects_business_commands() {
    let t = tempfile::tempdir().unwrap();
    let a = network::Worker::plain();
    let b = network::Worker::plain();
    let ar = t.path().join("alpha");
    let br = t.path().join("beta");
    let ai = a
        .call("discovery.identity.open", json!({"root":ar,"host":"alpha"}))
        .unwrap();
    let (bi, server, target) = enrollment_server(&b, &br, "beta");
    let session = enroll(&a, &b, &ar, &server, &target);
    assert_eq!(session["authority_granted"], false);
    assert_eq!(session["authorization_required"], true);
    assert_eq!(session["capabilities"], json!([]));
    let ap = a.call("discovery.peers.all", json!({"root":ar})).unwrap();
    let bp = b.call("discovery.peers.all", json!({"root":br})).unwrap();
    assert_eq!(ap["beta"]["public_key"], bi["public"]["public_key"]);
    assert_eq!(bp["alpha"]["public_key"], ai["public"]["public_key"]);
    for key in ["private_key", "code", "pin", "session_key"] {
        assert!(ap["beta"].get(key).is_none());
    }
    for root in [&ar, &br] {
        assert_eq!(
            fs::metadata(root).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(
            fs::metadata(root.join("identity.json"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
    }
    assert!(
        a.call(
            "discovery.enrollment.client.pair",
            json!({"root":ar,"candidate":target,"code":"0000"})
        )
        .is_err()
    );
    drop(a);
    let restarted = network::Worker::plain();
    restarted
        .call("discovery.identity.open", json!({"root":ar,"host":"alpha"}))
        .unwrap();
    assert_eq!(
        restarted
            .call(
                "discovery.enrollment.client.reconnect",
                json!({"root":ar,"candidate":target})
            )
            .unwrap(),
        session
    );
    let connection = restarted
        .call(
            "discovery.enrollment.client.connect",
            json!({"root":ar,"candidate":target,"pairing":false}),
        )
        .unwrap();
    restarted
        .call(
            "discovery.enrollment.client.send",
            json!({"connection":connection,"message":{"op":"host_execute"}}),
        )
        .unwrap();
    assert!(
        restarted
            .call(
                "discovery.enrollment.client.receive",
                json!({"connection":connection})
            )
            .is_err()
    );
    restarted
        .call(
            "discovery.enrollment.client.close",
            json!({"connection":connection}),
        )
        .unwrap();
}
#[test]
fn concurrent_network_confirmation_has_exactly_one_durable_pair() {
    let t = tempfile::tempdir().unwrap();
    let a = network::Worker::plain();
    let b = network::Worker::plain();
    let ar = t.path().join("alpha");
    let br = t.path().join("beta");
    a.call("discovery.identity.open", json!({"root":ar,"host":"alpha"}))
        .unwrap();
    let (_, server, target) = enrollment_server(&b, &br, "beta");
    let code = b
        .call(
            "discovery.window.open",
            json!({"window":server["window"],"lifetime":60}),
        )
        .unwrap();
    let accepted = thread::scope(|s| {
        let jobs = (0..4)
            .map(|_| {
                s.spawn(|| {
                    a.call(
                        "discovery.enrollment.client.pair",
                        json!({"root":ar,"candidate":target,"code":code}),
                    )
                    .is_ok()
                })
            })
            .collect::<Vec<_>>();
        jobs.into_iter()
            .map(|j| usize::from(j.join().unwrap()))
            .sum::<usize>()
    });
    assert_eq!(accepted, 1);
    assert_eq!(
        a.call("discovery.peers.all", json!({"root":ar}))
            .unwrap()
            .as_object()
            .unwrap()
            .len(),
        1
    );
    assert_eq!(
        b.call("discovery.peers.all", json!({"root":br}))
            .unwrap()
            .as_object()
            .unwrap()
            .len(),
        1
    );
}
#[test]
fn tls_substitution_and_revocation_cannot_repair_or_repair_trust_implicitly() {
    let t = tempfile::tempdir().unwrap();
    let a = network::Worker::plain();
    let b = network::Worker::plain();
    let ar = t.path().join("alpha");
    let br = t.path().join("beta");
    a.call("discovery.identity.open", json!({"root":ar,"host":"alpha"}))
        .unwrap();
    let (_, server, target) = enrollment_server(&b, &br, "beta");
    enroll(&a, &b, &ar, &server, &target);
    let other = network::Worker::plain();
    let (_, wrong, _) = enrollment_server(&other, &t.path().join("other"), "other");
    let mut changed = target.clone();
    changed["endpoints"] = json!([["127.0.0.1", wrong["port"]]]);
    assert_eq!(
        a.call(
            "discovery.enrollment.client.reconnect",
            json!({"root":ar,"candidate":changed})
        )
        .unwrap_err(),
        "TLS_HANDSHAKE_FAILED"
    );
    b.call(
        "discovery.peers.revoke",
        json!({"root":br,"node_id":"alpha"}),
    )
    .unwrap();
    assert!(
        a.call(
            "discovery.enrollment.client.reconnect",
            json!({"root":ar,"candidate":target})
        )
        .is_err()
    );
    assert_eq!(
        b.call("discovery.peers.all", json!({"root":br})).unwrap()["alpha"]["trust_state"],
        "REVOKED"
    );
}

#[test]
fn empty_interface_mdns_lifecycle_needs_no_python_resolver_or_network() {
    let w = network::Worker::plain();
    let b = w.call("mdns.start", json!({"interfaces":[]})).unwrap();
    assert_eq!(b["interfaces"], json!([]));
    assert_eq!(
        w.call("mdns.candidates", json!({"id":b["id"]})).unwrap(),
        json!({"candidates":{},"records":[]})
    );
    w.call("mdns.close", json!({"id":b["id"]})).unwrap();
}
