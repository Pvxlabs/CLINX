//! Explicit maintenance only; never staged with normal runtime/App binaries.
#[path = "../maintenance_transfer.rs"]
mod transfer;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    io::{Read, Write},
    os::unix::process::CommandExt,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};
use transfer::{Config, Io, Result};
fn account(name: &str) -> Result<nix::unistd::User> {
    nix::unistd::User::from_name(name)
        .map_err(|_| "ACCOUNT_UNAVAILABLE")?
        .ok_or("ACCOUNT_UNAVAILABLE".into())
}
/// Same runuser/nsenter boundaries as the retired maintenance script. No service
/// command other than read-only MainPID inspection is available here.
struct NativeIo {
    executable: PathBuf,
    source_user: String,
    source_endpoint: PathBuf,
    source_uid: u32,
    destination_endpoint: PathBuf,
    worker_uid: u32,
    authority_uid: u32,
}
struct OwnedChild {
    child: std::process::Child,
    completed: bool,
}
impl Drop for OwnedChild {
    fn drop(&mut self) {
        if !self.completed {
            if let Some(pid) = rustix::process::Pid::from_raw(self.child.id() as i32) {
                let _ = rustix::process::kill_process_group(pid, rustix::process::Signal::KILL);
            }
            let _ = self.child.wait();
        }
    }
}
fn capture(
    mut command: Command,
    input: Vec<u8>,
    limit: usize,
    timeout: Duration,
) -> Result<(std::process::ExitStatus, Vec<u8>)> {
    enum Event {
        Written(std::io::Result<()>),
        Output(std::io::Result<Vec<u8>>),
    }
    let deadline = Instant::now() + timeout;
    let mut owned = OwnedChild {
        child: command
            .process_group(0)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| "MAINTENANCE_CHILD_UNAVAILABLE")?,
        completed: false,
    };
    let mut stdin = owned.child.stdin.take().unwrap();
    let stdout = owned.child.stdout.take().unwrap();
    let (tx, rx) = std::sync::mpsc::channel();
    let writer = tx.clone();
    // Both directions share the deadline. No blocked write or descendant-held
    // stdout can escape it; every early return kills/reaps our private group.
    thread::spawn(move || {
        let result = stdin.write_all(&input).and_then(|_| stdin.flush());
        drop(stdin);
        let _ = writer.send(Event::Written(result));
    });
    thread::spawn(move || {
        let mut bytes = vec![];
        let result = stdout
            .take((limit + 1) as u64)
            .read_to_end(&mut bytes)
            .map(|_| bytes);
        let _ = tx.send(Event::Output(result));
    });
    let (mut written, mut output, mut status) = (false, None, None);
    loop {
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .ok_or("MAINTENANCE_CHILD_DELIVERY_UNKNOWN")?;
        match rx.recv_timeout(remaining.min(Duration::from_millis(10))) {
            Ok(Event::Written(Ok(()))) => written = true,
            Ok(Event::Output(Ok(bytes))) if bytes.len() <= limit => output = Some(bytes),
            Ok(_) => return Err("MAINTENANCE_CHILD_DELIVERY_UNKNOWN".into()),
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) if written && output.is_some() => {
                thread::sleep(remaining.min(Duration::from_millis(1)));
            }
            Err(_) => return Err("MAINTENANCE_CHILD_DELIVERY_UNKNOWN".into()),
        }
        if status.is_none() {
            status = owned
                .child
                .try_wait()
                .map_err(|_| "MAINTENANCE_CHILD_DELIVERY_UNKNOWN")?;
        }
        if written
            && output.is_some()
            && let Some(status) = status
        {
            owned.completed = true;
            return Ok((status, output.unwrap()));
        }
    }
}
fn child_json(command: Command, input: &Value) -> Result<Value> {
    let bytes = serde_json::to_vec(input).map_err(|_| "MAINTENANCE_INPUT_INVALID")?;
    if bytes.len() > clinx_decision_kernel::MAX_FRAME_BYTES {
        return Err("MAINTENANCE_INPUT_INVALID".into());
    }
    let (status, bytes) = capture(
        command,
        bytes,
        clinx_decision_kernel::provider::MAX_MESSAGE * 2,
        Duration::from_secs(45),
    )?;
    let value: Value =
        serde_json::from_slice(&bytes).map_err(|_| "MAINTENANCE_CHILD_DELIVERY_UNKNOWN")?;
    if let Some(e) = value["error"].as_str() {
        return Err(e.into());
    }
    if !status.success() || value.get("result").is_none() {
        return Err("MAINTENANCE_CHILD_DELIVERY_UNKNOWN".into());
    }
    Ok(value["result"].clone())
}
impl NativeIo {
    fn command(&self, user: &str, mode: &str, namespace: bool) -> Result<Command> {
        let mut command = if namespace {
            let mut query = Command::new("/usr/bin/systemctl");
            query.args([
                "show",
                "clinx-rust-dispatcher.service",
                "-p",
                "MainPID",
                "--value",
            ]);
            let (status, output) = capture(query, vec![], 1024, Duration::from_secs(10))
                .map_err(|_| "DISPATCHER_NAMESPACE_UNAVAILABLE")?;
            let pid = std::str::from_utf8(&output)
                .ok()
                .and_then(|s| s.trim().parse::<u32>().ok())
                .filter(|pid| *pid > 1)
                .ok_or("DISPATCHER_NAMESPACE_UNAVAILABLE")?;
            if !status.success() {
                return Err("DISPATCHER_NAMESPACE_UNAVAILABLE".into());
            }
            let mut c = Command::new("/usr/bin/nsenter");
            c.args(["-t", &pid.to_string(), "-m", "--", "/usr/sbin/runuser"]);
            c
        } else {
            Command::new("/usr/sbin/runuser")
        };
        command
            .args(["-u", user, "--"])
            .arg(&self.executable)
            .arg(mode);
        Ok(command)
    }
}
impl Io for NativeIo {
    fn source(&mut self, archive: bool, thread: &str, turn: &str) -> Result<Value> {
        child_json(
            self.command(&self.source_user, "io-provider", false)?,
            &json!({"endpoint":self.source_endpoint,"peer_uid":self.source_uid,"action":if archive{"archive"}else{"read"},"thread_id":thread,"turn_id":turn}),
        )
    }
    fn destination(&mut self, thread: &str, turn: &str) -> Result<Value> {
        child_json(
            self.command("clinx-transport", "io-provider", true)?,
            &json!({"endpoint":self.destination_endpoint,"peer_uid":self.worker_uid,"action":"resume","thread_id":thread,"turn_id":turn}),
        )
    }
    fn adopt(&mut self, arguments: Value, operation_id: &str) -> Result<Value> {
        child_json(
            self.command("clinx-api", "io-authority", false)?,
            &json!({"endpoint":"/run/clinx-rust/authority.sock","peer_uid":self.authority_uid,"arguments":arguments,"operation_id":operation_id}),
        )
    }
    fn writable_holders(&mut self, path: &Path) -> Result<Value> {
        transfer::writable_holders(Path::new("/proc"), path)
    }
}
fn run(args: Vec<String>) -> Result<Value> {
    if args
        .first()
        .is_some_and(|a| a == "io-provider" || a == "io-authority")
    {
        if args.len() != 1 {
            return Err("MAINTENANCE_ARGUMENT_INVALID".into());
        }
        let mut bytes = vec![];
        std::io::stdin()
            .take((clinx_decision_kernel::MAX_FRAME_BYTES + 1) as u64)
            .read_to_end(&mut bytes)
            .map_err(|_| "MAINTENANCE_INPUT_INVALID")?;
        if bytes.len() > clinx_decision_kernel::MAX_FRAME_BYTES {
            return Err("MAINTENANCE_INPUT_INVALID".into());
        }
        let a: Value = serde_json::from_slice(&bytes).map_err(|_| "MAINTENANCE_INPUT_INVALID")?;
        return if args[0] == "io-provider" {
            transfer::provider_io(&a)
        } else {
            transfer::authority_io(&a)
        };
    }
    let mut values = BTreeMap::new();
    let mut iter = args.into_iter();
    if iter.next().as_deref() != Some("transfer-native-session") {
        return Err("MAINTENANCE_COMMAND_REQUIRED".into());
    }
    while let Some(key) = iter.next() {
        if ![
            "--source-receipt",
            "--source-endpoint",
            "--release",
            "--output",
            "--source-user",
            "--source-home",
            "--destination-home",
            "--runtime-config",
            "--worker-user-file",
            "--project",
        ]
        .contains(&key.as_str())
            || values.contains_key(&key)
        {
            return Err("MAINTENANCE_ARGUMENT_INVALID".into());
        }
        values.insert(key, iter.next().ok_or("MAINTENANCE_ARGUMENT_INVALID")?);
    }
    for key in [
        "--source-receipt",
        "--source-endpoint",
        "--release",
        "--output",
        "--destination-home",
    ] {
        if !values.contains_key(key) {
            return Err(format!("MISSING_ARGUMENT:{key}"));
        }
    }
    if !cfg!(target_os = "linux") || rustix::process::geteuid().as_raw() != 0 {
        return Err("ROOT_LINUX_MAINTENANCE_REQUIRED".into());
    }
    let required = |key: &str| {
        values
            .get(key)
            .cloned()
            .ok_or_else(|| format!("MISSING_ARGUMENT:{key}"))
    };
    let source_user = values
        .get("--source-user")
        .map(String::as_str)
        .unwrap_or("pvxlabs");
    let source = account(source_user)?;
    let source_home = values
        .get("--source-home")
        .map(PathBuf::from)
        .unwrap_or_else(|| source.dir.join(".codex"));
    // Never guess the retired codex160 directory. Operator supplies the current
    // destination Provider home explicitly; exact resumed evidence is still required.
    let destination_home = PathBuf::from(required("--destination-home")?);
    let worker_file = values
        .get("--worker-user-file")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/var/lib/clinx-rust/config/worker-user"));
    let worker = account(
        fs::read_to_string(worker_file)
            .map_err(|_| "WORKER_IDENTITY_UNAVAILABLE")?
            .trim(),
    )?;
    let config = clinx_decision_kernel::mcp::load_config(
        &values
            .get("--runtime-config")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("/var/lib/clinx-rust/config/mcp-runtime.json")),
    )?;
    let c = Config {
        source_receipt: PathBuf::from(required("--source-receipt")?),
        source_endpoint: PathBuf::from(required("--source-endpoint")?),
        release: PathBuf::from(required("--release")?),
        output: PathBuf::from(required("--output")?),
        source_sessions: source_home.join("sessions"),
        source_archive: source_home.join("archived_sessions"),
        destination_sessions: destination_home.join("sessions"),
        worker_uid: worker.uid.as_raw(),
        worker_gid: worker.gid.as_raw(),
        host: config.runtime_host,
        project: values.get("--project").cloned().unwrap_or("clinx".into()),
        fd_wait: Duration::from_secs(10),
    };
    account("clinx-api")?;
    account("clinx-transport")?;
    let mut io = NativeIo {
        executable: std::env::current_exe().map_err(|_| "MAINTENANCE_BINARY_UNAVAILABLE")?,
        source_user: source_user.into(),
        source_endpoint: c.source_endpoint.clone(),
        source_uid: source.uid.as_raw(),
        destination_endpoint: config.provider_endpoint,
        worker_uid: worker.uid.as_raw(),
        authority_uid: account("clinx-authority")?.uid.as_raw(),
    };
    transfer::transfer(&c, &mut io)
}
fn main() {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    if args.first().is_some_and(|a| a == "extend-business-policy") {
        match extend_policy(&args[1..]) {
            Ok(value) => println!("{value}"),
            Err(error) => {
                eprintln!("MAINTENANCE=BLOCKED: {error}");
                std::process::exit(1);
            }
        }
        return;
    }
    if args == ["--help"] {
        println!(
            "clinx-maintenance extend-business-policy --state CANONICAL_DIR --policy NEW_JSON --authority-id ID --expected-policy-hash HASH --operation-id ID --approved\nOffline additive device routes only. Run as the existing Authority owner after all writers stop; no active executions or leases. Persists an idempotent receipt, never activates services or replaces policy files."
        );
        println!(
            "clinx-maintenance transfer-native-session --source-receipt FILE --source-endpoint SOCKET --release DIR --output UNIQUE_RECEIPT --destination-home CURRENT_PROVIDER_HOME [--source-user USER] [--source-home CODEX_HOME] [--runtime-config MCP_RUNTIME_JSON] [--worker-user-file FILE] [--project ALIAS]\nExplicit terminal-session maintenance only. No automatic retry, deployment, service lifecycle or task/lease migration.\nDefaults: source user pvxlabs; source home is that account's .codex; project clinx.\nRuntime config: /var/lib/clinx-rust/config/mcp-runtime.json; worker identity: /var/lib/clinx-rust/config/worker-user.\nDestination home is required, never inferred from retired codex160 paths.\nLinux root orchestration retains runuser identities clinx-transport/clinx-api and the existing clinx-rust-dispatcher.service mount namespace. Authority socket: /run/clinx-rust/authority.sock; peer UID is resolved from clinx-authority, never hard-coded."
        );
        return;
    }
    let internal = args.first().is_some_and(|a| a.starts_with("io-"));
    match run(args) {
        Ok(value) => {
            if internal {
                println!("{}", json!({"result":value}))
            } else {
                println!(
                    "{}",
                    json!({"status":value["status"],"thread_id":value["thread_id"],"exact_turn_id":value["exact_turn_id"],"adoption":value["adoption"]})
                )
            }
        }
        Err(error) => {
            if internal {
                println!("{}", json!({"error":error}))
            } else {
                eprintln!("MAINTENANCE=BLOCKED: {error}")
            }
            std::process::exit(1)
        }
    }
}

fn extend_policy(args: &[String]) -> Result<Value> {
    let mut values = BTreeMap::new();
    let mut approved = false;
    let mut i = 0;
    while i < args.len() {
        if args[i] == "--approved" && !approved {
            approved = true;
            i += 1;
            continue;
        }
        if ![
            "--state",
            "--policy",
            "--authority-id",
            "--expected-policy-hash",
            "--operation-id",
        ]
        .contains(&args[i].as_str())
        {
            return Err("POLICY_ARGUMENT_INVALID".into());
        }
        let value = args
            .get(i + 1)
            .filter(|v| !v.starts_with("--"))
            .ok_or("POLICY_ARGUMENT_REQUIRED")?;
        if values.insert(args[i].as_str(), value.as_str()).is_some() {
            return Err("POLICY_ARGUMENT_DUPLICATE".into());
        }
        i += 2;
    }
    if !approved || values.len() != 5 {
        return Err("APPROVED_EXACT_POLICY_ARGUMENTS_REQUIRED".into());
    }
    let state = Path::new(values["--state"]);
    let policy = Path::new(values["--policy"]);
    if !state.is_absolute()
        || !policy.is_absolute()
        || !policy
            .symlink_metadata()
            .is_ok_and(|m| m.is_file() && m.len() <= 1024 * 1024)
    {
        return Err("POLICY_PATH_INVALID".into());
    }
    let next: clinx_decision_kernel::business::Policy =
        serde_json::from_slice(&fs::read(policy).map_err(|_| "POLICY_READ_FAILED")?)
            .map_err(|_| "INVALID_POLICY")?;
    let mut store = clinx_decision_kernel::authority_store::Store::open(state, false)?;
    store.extend_business_policy(
        &next,
        values["--authority-id"],
        values["--expected-policy-hash"],
        values["--operation-id"],
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn own_process_group_timeout_includes_descendant_held_stdout_and_blocked_stdin() {
        for (script, input) in [
            ("sleep 60 & exit 0", vec![]),
            ("sleep 60", vec![b'x'; 1_000_000]),
        ] {
            let mut command = Command::new("/bin/sh");
            command.args(["-c", script]);
            let start = Instant::now();
            assert_eq!(
                capture(command, input, 1024, Duration::from_millis(100)).unwrap_err(),
                "MAINTENANCE_CHILD_DELIVERY_UNKNOWN"
            );
            assert!(start.elapsed() < Duration::from_secs(3));
        }
    }
    #[test]
    fn capture_reaps_success_and_bounds_output() {
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "cat >/dev/null; printf '{\"result\":1}'"]);
        assert_eq!(child_json(command, &json!({})).unwrap(), 1);
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "printf 12345"]);
        assert_eq!(
            capture(command, vec![], 4, Duration::from_secs(1)).unwrap_err(),
            "MAINTENANCE_CHILD_DELIVERY_UNKNOWN"
        );
    }
    #[test]
    fn cli_requires_explicit_destination_home_before_any_identity_or_io_lookup() {
        let args = [
            "transfer-native-session",
            "--source-receipt",
            "/missing-receipt",
            "--source-endpoint",
            "/missing-socket",
            "--release",
            "/missing-release",
            "--output",
            "/missing-output",
        ]
        .into_iter()
        .map(str::to_string)
        .collect();
        assert_eq!(
            run(args).unwrap_err(),
            "MISSING_ARGUMENT:--destination-home"
        );
    }
}
