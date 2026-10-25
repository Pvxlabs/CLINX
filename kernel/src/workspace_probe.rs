//! Read-only metadata, executed by the configured Provider under its user.
//! No Authority filesystem fallback, native thread creation or tool approval.
use crate::{
    native_transport::{Providers, UnixProviders},
    provider::{Client, Provider, Result},
    remote_provider::RoutedProviders,
};
use serde_json::{Value, json};
use std::{
    fs::{self, File},
    io::Read,
    os::unix::{fs::MetadataExt, process::CommandExt},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::mpsc,
    time::{Duration, Instant},
};

pub const LIMIT: usize = 65536;
pub const SOURCE: &str = "CODEX_PROVIDER_COMMAND_EXEC";

/// Kill and reap only our own read-only child group. Output and wall time are
/// bounded even if a configured Git executable leaves a pipe open in a child.
fn output(command: &mut Command) -> Result<String> {
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .process_group(0)
        .spawn()
        .map_err(|_| "WORKSPACE_IDENTITY_UNAVAILABLE")?;
    let pgid = rustix::process::Pid::from_child(&child);
    let mut pipe = child
        .stdout
        .take()
        .ok_or("WORKSPACE_IDENTITY_UNAVAILABLE")?;
    let (tx, rx) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let result = pipe
            .by_ref()
            .take((LIMIT + 1) as u64)
            .read_to_end(&mut bytes);
        let _ = tx.send(result.map(|_| bytes));
    });
    let deadline = Instant::now() + Duration::from_secs(2);
    let mut bytes = None;
    let mut status = None;
    let result = loop {
        if bytes.is_none() {
            match rx.try_recv() {
                Ok(Ok(b)) if b.len() <= LIMIT => bytes = Some(b),
                Ok(_) | Err(mpsc::TryRecvError::Disconnected) => {
                    break Err("WORKSPACE_IDENTITY_UNAVAILABLE".into());
                }
                Err(mpsc::TryRecvError::Empty) => (),
            }
        }
        match child.try_wait() {
            Ok(Some(s)) => status = Some(s),
            Ok(None) => (),
            Err(_) => break Err("WORKSPACE_IDENTITY_UNAVAILABLE".into()),
        }
        if let (Some(s), Some(b)) = (status, bytes.as_ref()) {
            break if s.success() {
                String::from_utf8(b.clone())
                    .map(|s| s.trim().to_owned())
                    .map_err(|_| "WORKSPACE_IDENTITY_UNAVAILABLE".into())
            } else {
                Err("WORKSPACE_IDENTITY_UNAVAILABLE".into())
            };
        }
        if Instant::now() >= deadline {
            break Err("WORKSPACE_IDENTITY_UNAVAILABLE".into());
        }
        std::thread::sleep(Duration::from_millis(5));
    };
    let _ = rustix::process::kill_process_group(pgid, rustix::process::Signal::KILL);
    let _ = child.wait();
    result
}

pub fn git(cwd: &Path, args: &[&str]) -> Result<String> {
    let allowed = args.starts_with(&["rev-parse"])
        || args == ["branch", "--show-current"]
        || [
            ["remote", "get-url"],
            ["worktree", "list"],
            ["symbolic-ref", "--quiet"],
            ["check-ref-format", "--branch"],
            ["for-each-ref", "--format=%(symref)"],
        ]
        .iter()
        .any(|p| args.starts_with(p))
        || args == ["config", "--list"];
    if !allowed || args.iter().any(|v| v.contains('\0')) {
        return Err("WORKSPACE_PROBE_INVALID".into());
    }
    let mut command = Command::new("/usr/bin/git");
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("GIT_") {
            command.env_remove(key);
        }
    }
    command
        .args(["-c", "core.fsmonitor=false", "-c"])
        .arg(format!("safe.directory={}", cwd.display()))
        .arg("-C")
        .arg(cwd)
        .args(args);
    output(&mut command)
}

pub fn inspect(request: &Value) -> Result<Value> {
    let cwd = request["cwd"]
        .as_str()
        .filter(|s| s.starts_with('/') && !s.contains('\0') && s.chars().count() <= 4096)
        .ok_or("WORKSPACE_CONTRACT_INVALID")?;
    let cwd = fs::canonicalize(cwd).map_err(|_| "WORKSPACE_CWD_UNREADABLE")?;
    if !cwd.is_dir() || rustix::fs::access(&cwd, rustix::fs::Access::EXEC_OK).is_err() {
        return Err("WORKSPACE_CWD_UNREADABLE".into());
    }
    let mode = request["mode"].as_str().ok_or("WORKSPACE_PROBE_INVALID")?;
    let uid = rustix::process::getuid().as_raw();
    if mode == "git" {
        let args = request["args"]
            .as_array()
            .ok_or("WORKSPACE_PROBE_INVALID")?
            .iter()
            .map(|v| v.as_str().ok_or("WORKSPACE_PROBE_INVALID"))
            .collect::<std::result::Result<Vec<_>, _>>()?;
        return Ok(json!({"value":git(&cwd, &args)?, "observer_uid":uid}));
    }
    if !["repository", "workspace"].contains(&mode) {
        return Err("WORKSPACE_PROBE_INVALID".into());
    }
    let common = git(
        &cwd,
        &["rev-parse", "--path-format=absolute", "--git-common-dir"],
    )?;
    let info = fs::metadata(&common).map_err(|_| "WORKSPACE_IDENTITY_UNAVAILABLE")?;
    if !info.is_dir() {
        return Err("WORKSPACE_IDENTITY_UNAVAILABLE".into());
    }
    let mut result = json!({"repository_identity":format!("git-common:{}:{}", info.dev(), info.ino()), "observer_uid":uid});
    if mode == "workspace" {
        result["cwd"] = json!(cwd.to_str().ok_or("WORKSPACE_CONTRACT_INVALID")?);
        result["repository_common_dir"] = json!(common);
        result["repository_root"] = json!(git(&cwd, &["rev-parse", "--show-toplevel"])?);
        result["worktree"] = json!(git(
            &cwd,
            &["rev-parse", "--path-format=absolute", "--git-dir"]
        )?);
        result["branch"] = json!(git(&cwd, &["branch", "--show-current"])?);
        result["head"] = json!(git(&cwd, &["rev-parse", "HEAD"])?);
    }
    Ok(result)
}

/// The production identity comes from the existing root-controlled contract,
/// never from a Node payload or an environment-supplied UID.
pub fn worker_uid() -> Result<u32> {
    let path = Path::new("/var/lib/clinx-rust/config/worker-user");
    for p in path.ancestors() {
        let m = p
            .symlink_metadata()
            .map_err(|_| "ROOT_CONTROLLED_WORKER_IDENTITY_REQUIRED")?;
        if m.file_type().is_symlink() || m.uid() != 0 || m.mode() & 0o022 != 0 {
            return Err("ROOT_CONTROLLED_WORKER_IDENTITY_REQUIRED".into());
        }
    }
    let file = File::open(path).map_err(|_| "ROOT_CONTROLLED_WORKER_IDENTITY_REQUIRED")?;
    if !file
        .metadata()
        .map_err(|_| "ROOT_CONTROLLED_WORKER_IDENTITY_REQUIRED")?
        .is_file()
    {
        return Err("ROOT_CONTROLLED_WORKER_IDENTITY_REQUIRED".into());
    }
    let mut name = String::new();
    file.take(257)
        .read_to_string(&mut name)
        .map_err(|_| "INVALID_WORKER_IDENTITY")?;
    let name = name.trim();
    if name.is_empty()
        || name.len() > 256
        || !name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
    {
        return Err("INVALID_WORKER_IDENTITY".into());
    }
    if ["clinx-authority", "clinx-api", "clinx-transport"].contains(&name) {
        return Err("WORKER_AUTHORITY_IDENTITY_CONFLICT".into());
    }
    let entry = output(Command::new("/usr/bin/getent").args(["passwd", name]))?;
    let fields: Vec<_> = entry.split(':').collect();
    let uid = fields
        .get(2)
        .and_then(|s| s.parse::<u32>().ok())
        .filter(|u| *u != 0)
        .ok_or("WORKER_AUTHORITY_IDENTITY_CONFLICT")?;
    if fields.first() != Some(&name) || entry.contains('\n') {
        return Err("INVALID_WORKER_IDENTITY".into());
    }
    Ok(uid)
}

pub fn command(program: &Path, request: &Value) -> Result<Value> {
    if !program.is_absolute() || !request.is_object() {
        return Err("WORKSPACE_PROBE_INVALID".into());
    }
    Ok(
        json!({"command":[program, "--inspect", serde_json::to_string(request).map_err(|_|"WORKSPACE_PROBE_INVALID")?],
        "cwd":"/", "timeoutMs":2500, "outputBytesCap":LIMIT, "sandboxPolicy":{"type":"dangerFullAccess"}}),
    )
}

pub fn decode(result: Value, uid: u32) -> Result<Value> {
    let stdout = result["stdout"]
        .as_str()
        .filter(|s| s.len() <= LIMIT)
        .ok_or("WORKSPACE_OBSERVER_UNAVAILABLE")?;
    if result["exitCode"].as_i64() != Some(0) {
        return Err("WORKSPACE_OBSERVER_UNAVAILABLE".into());
    }
    let mut data: Value = serde_json::from_str(stdout).map_err(|_| "WORKSPACE_OBSERVER_INVALID")?;
    if let Some(error) = data["error"].as_str() {
        return Err(match error {
            "WORKSPACE_CWD_UNREADABLE"
            | "WORKSPACE_IDENTITY_UNAVAILABLE"
            | "WORKSPACE_CONTRACT_INVALID"
            | "WORKSPACE_PROBE_INVALID" => error.into(),
            _ => "WORKSPACE_OBSERVER_INVALID".into(),
        });
    }
    if uid == 0 || data["observer_uid"].as_u64() != Some(uid as u64) {
        return Err("WORKSPACE_OBSERVER_IDENTITY_CONFLICT".into());
    }
    data.as_object_mut()
        .ok_or("WORKSPACE_OBSERVER_INVALID")?
        .insert("observer_source".into(), json!(SOURCE));
    Ok(data)
}

pub fn local(
    endpoint: &Path,
    program: &Path,
    request: &Value,
    uid: u32,
    timeout: Duration,
) -> Result<Value> {
    let params = command(program, request)?;
    let mut client = Client::connect_checked(endpoint, timeout, uid).map_err(|e| {
        if e == "PROVIDER_PEER_IDENTITY_CONFLICT" {
            "WORKSPACE_OBSERVER_IDENTITY_CONFLICT".to_owned()
        } else {
            e
        }
    })?;
    decode(client.request("command/exec", params)?, uid)
}

pub fn observe(
    endpoint: &str,
    program: &Path,
    request: &Value,
    qualification_uid: Option<u32>,
) -> Result<Value> {
    let mut providers = RoutedProviders {
        local: UnixProviders {
            timeout: Duration::from_secs(3),
        },
        gateway: std::env::var_os("CLINX_RUST_PROVIDER_GATEWAY").map(PathBuf::from),
        gateway_uid: std::env::var("CLINX_RUST_PROVIDER_GATEWAY_UID")
            .ok()
            .map(|v| v.parse::<u32>().map_err(|_| "INVALID_PROVIDER_ROUTE"))
            .transpose()?
            .unwrap_or(u32::MAX),
        remote_hosts: serde_json::from_str(
            &std::env::var("CLINX_RUST_REMOTE_HOSTS").unwrap_or_else(|_| "[]".into()),
        )
        .map_err(|_| "INVALID_PROVIDER_ROUTE")?,
        routes: serde_json::from_str(
            &std::env::var("CLINX_RUST_PROVIDER_GATEWAYS").unwrap_or_else(|_| "{}".into()),
        )
        .map_err(|_| "INVALID_PROVIDER_ROUTE")?,
    };
    let host = request["host"].as_str().unwrap_or("");
    if providers.remote_hosts.contains(host) || providers.routes.contains_key(host) {
        let mut client = providers.connect_scoped(
            endpoint,
            json!({"host":host,"cwd":request["cwd"],"mode":"workspace"}),
        )?;
        let data = client.request("workspace/inspect", request.clone())?;
        if data["observer_source"] != SOURCE || data["observer_uid"].as_u64().is_none_or(|u| u == 0)
        {
            return Err("WORKSPACE_OBSERVER_INVALID".into());
        }
        Ok(data)
    } else {
        local(
            Path::new(endpoint),
            program,
            request,
            qualification_uid.map(Ok).unwrap_or_else(worker_uid)?,
            Duration::from_secs(3),
        )
    }
}
