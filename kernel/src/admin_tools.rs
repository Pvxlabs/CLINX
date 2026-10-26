//! Narrow operator capabilities. Reuses release construction and Rust authority
//! contracts; does not interpret commands supplied by the operator.
use crate::{build, install};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    io::Read,
    os::unix::fs::{MetadataExt, PermissionsExt},
    path::{Component, Path, PathBuf},
    process::Command,
    time::{Duration, Instant},
};
pub type Result<T> = std::result::Result<T, String>;
const ROOT: &str = "/var/lib/clinx-rust";
const SOURCE: &str = "/home/pvxlabs/dev/clinx";
const CENTRE: &str = "/var/lib/clinx-rust/api/node-centre";
const STATE: &str = "/var/lib/clinx-rust/authority";
const ADMIN: &str = "/var/lib/clinx-rust/admin";
const MAINTENANCE: &str = "/usr/local/libexec/clinx-admin-tools/clinx-maintenance";
const UNITS: [&str; 4] = [
    "clinx-rust-authority.service",
    "clinx-rust-centre.service",
    "clinx-rust-dispatcher.service",
    "clinx-rust-mcp@.service",
];

pub fn token(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && !s.starts_with('-')
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_.:@".contains(&b))
}
pub fn release_id(s: &str) -> bool {
    s.strip_prefix("rust-")
        .is_some_and(|v| v.len() == 20 && v.bytes().all(|b| b.is_ascii_hexdigit()))
}
pub fn service(s: &str) -> bool {
    UNITS[..3].contains(&s)
        || s == "clinx-rust-mcp.socket"
        || (token(s)
            && s.starts_with("clinx-rust-mcp@")
            && s.ends_with(".service")
            && s != "clinx-rust-mcp@.service")
}
fn digest(s: &str, n: usize) -> bool {
    s.len() == n && s.bytes().all(|b| b.is_ascii_hexdigit())
}
pub fn validate(a: &[String]) -> Result<()> {
    let ok = match a.iter().map(String::as_str).collect::<Vec<_>>().as_slice() {
        ["status" | "nodes" | "pair-listen"] => true,
        ["journal", s] => service(s),
        ["journal", s, n] => service(s) && n.parse::<u32>().is_ok_and(|n| (1..=2000).contains(&n)),
        ["service", action, s] => ["start", "stop", "restart"].contains(action) && service(s),
        ["share", peer, user, mode] => {
            token(peer) && token(user) && ["read", "execute", "revoke"].contains(mode)
        }
        ["probe", host, project] => token(host) && token(project),
        ["stage", path, head] => {
            let p = Path::new(path);
            (p.starts_with("/data/artifacts") || p.starts_with("/lab/runtime-worktrees/p620"))
                && p.components()
                    .all(|c| matches!(c, Component::RootDir | Component::Normal(_)))
                && digest(head, 40)
        }
        ["select", id, "--approved"] => release_id(id),
        ["recover", id, execution, "--approved"] => {
            release_id(id)
                && execution
                    .strip_prefix("exec_")
                    .is_some_and(|s| digest(s, 32))
        }
        ["policy", hash, op, "--approved"] => digest(hash, 64) && token(op),
        _ => false,
    };
    if ok {
        Ok(())
    } else {
        Err("ADMIN_ARGUMENT_DENIED".into())
    }
}
pub fn trusted(path: &Path, owner: u32) -> Result<()> {
    for p in path.ancestors() {
        let m = fs::symlink_metadata(p).map_err(|_| "ADMIN_PATH_UNAVAILABLE")?;
        if m.file_type().is_symlink() || m.uid() != owner || m.mode() & 0o022 != 0 {
            return Err(format!("ADMIN_ROOT_CONTROL_REQUIRED:{}", p.display()));
        }
    }
    Ok(())
}
fn io<T>(r: std::io::Result<T>) -> Result<T> {
    r.map_err(|e| e.to_string())
}
fn command(program: &str) -> Command {
    let mut c = Command::new(program);
    c.env_clear()
        .env("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
        .env("LANG", "C.UTF-8");
    c
}
fn output(mut c: Command, seconds: u64) -> Result<String> {
    let (out, _) = build::run(&mut c, seconds)?;
    String::from_utf8(out).map_err(|_| "ADMIN_OUTPUT_INVALID".into())
}
fn system(args: &[&str]) -> Result<String> {
    let mut c = command("/usr/bin/systemctl");
    c.args(args);
    output(c, 60)
}
fn as_user(user: &str, program: &Path, args: &[String]) -> Command {
    let mut c = command("/usr/sbin/runuser");
    c.args(["-u", user, "--", "/usr/bin/env", "-i", "PATH=/usr/bin:/bin"])
        .arg(program)
        .args(args);
    c
}
fn unit_path(unit: &str) -> PathBuf {
    Path::new("/etc/systemd/system").join(unit)
}
fn current() -> Result<PathBuf> {
    let p = unit_path(UNITS[0]);
    trusted(&p, 0)?;
    let s = io(fs::read_to_string(p))?;
    let exe = s
        .lines()
        .find_map(|l| l.strip_prefix("ExecStart="))
        .and_then(|l| l.split_whitespace().next())
        .ok_or("ADMIN_RELEASE_UNAVAILABLE")?;
    let release = Path::new(exe).parent().ok_or("ADMIN_RELEASE_UNAVAILABLE")?;
    checked_release(release)?;
    Ok(release.into())
}
fn checked_release(release: &Path) -> Result<Value> {
    if release.parent() != Some(Path::new(ROOT).join("releases").as_path())
        || !release
            .file_name()
            .and_then(|s| s.to_str())
            .is_some_and(release_id)
    {
        return Err("ADMIN_RELEASE_PATH_DENIED".into());
    }
    trusted(release, 0)?;
    let manifest = install::validate_release(release, 0, false)?;
    trusted(&release.join("manifest.json"), 0)?;
    for name in manifest["files"]
        .as_object()
        .ok_or("INVALID_RELEASE_MANIFEST")?
        .keys()
    {
        trusted(&release.join(name), 0)?;
    }
    Ok(manifest)
}
fn policy_path() -> Result<PathBuf> {
    let s = io(fs::read_to_string(unit_path(UNITS[0])))?;
    let args = s
        .lines()
        .find_map(|l| l.strip_prefix("ExecStart="))
        .ok_or("ADMIN_POLICY_UNAVAILABLE")?
        .split_whitespace()
        .collect::<Vec<_>>();
    let p = PathBuf::from(
        args.windows(2)
            .find(|p| p[0] == "--policy")
            .ok_or("ADMIN_POLICY_UNAVAILABLE")?[1],
    );
    if p.parent() != Some(Path::new(ROOT).join("config").as_path()) {
        return Err("ADMIN_POLICY_PATH_DENIED".into());
    }
    trusted(&p, 0)?;
    Ok(p)
}
fn read_json(p: &Path) -> Result<Value> {
    build::value(p)
}
fn admin_dirs() -> Result<()> {
    for p in [
        ADMIN,
        &format!("{ADMIN}/staging"),
        &format!("{ADMIN}/receipts"),
    ] {
        if !Path::new(p).exists() {
            io(fs::create_dir(p))?;
            io(fs::set_permissions(p, fs::Permissions::from_mode(0o755)))?;
        }
        trusted(Path::new(p), 0)?;
    }
    Ok(())
}
fn idle() -> Result<()> {
    let c = clinx_decision_kernel::read_projection::readonly(
        &Path::new(STATE).join("authority.sqlite3"),
        "ADMIN_STATE_UNAVAILABLE",
        false,
    )?;
    let active:i64=c.query_row("SELECT count(*) FROM business_rows WHERE kind='execution' AND (json_extract(value,'$.states.ResourceAllocation') IS NOT 'RELEASED' OR coalesce(json_extract(value,'$.states.Execution'),'UNKNOWN') NOT IN ('SUCCEEDED','FAILED','CANCELLED'))",[],|r|r.get(0)).map_err(|_|"ADMIN_STATE_UNAVAILABLE")?;
    let leases: i64 = c
        .query_row(
            "SELECT count(*) FROM leases WHERE state='ACTIVE'",
            [],
            |r| r.get(0),
        )
        .map_err(|_| "ADMIN_STATE_UNAVAILABLE")?;
    if active != 0 || leases != 0 {
        return Err("ADMIN_ACTIVE_EXECUTIONS_OR_LEASES".into());
    }
    Ok(())
}
fn stop_all() -> Result<()> {
    idle()?;
    system(&["stop", "clinx-rust-mcp.socket"])?;
    let instances = system(&[
        "list-units",
        "--type=service",
        "--no-legend",
        "--plain",
        "clinx-rust-mcp@*.service",
    ])?;
    for unit in instances
        .lines()
        .filter_map(|l| l.split_whitespace().next())
    {
        if service(unit) {
            system(&["stop", unit])?;
        }
    }
    system(&["stop", UNITS[2], UNITS[1], UNITS[0]])?;
    for unit in &UNITS[..3] {
        if system(&["show", unit, "-p", "MainPID", "--value"])?.trim() != "0" {
            return Err("ADMIN_WRITER_STILL_RUNNING".into());
        }
        let group = system(&["show", unit, "-p", "ControlGroup", "--value"])?;
        let group = group.trim();
        if !group.is_empty() {
            let group = Path::new(group);
            if !group.is_absolute()
                || group
                    .components()
                    .any(|c| matches!(c, Component::ParentDir))
            {
                return Err("ADMIN_CGROUP_IDENTITY_INVALID".into());
            }
            let events = Path::new("/sys/fs/cgroup")
                .join(group.strip_prefix("/").unwrap())
                .join("cgroup.events");
            match fs::read_to_string(events) {
                Ok(text) if text.lines().any(|line| line == "populated 0") => {}
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                _ => return Err("ADMIN_WRITER_CGROUP_NOT_EMPTY".into()),
            }
        }
    }
    // Close the race with admission between the initial read and stopping MCP.
    // Leave services stopped if a newly admitted task needs reconciliation.
    idle()?;
    Ok(())
}
fn start_all() -> Result<()> {
    system(&["daemon-reload"])?;
    system(&["start", UNITS[0]])?;
    // Type=simple is not readiness. Wait for the actual Authority listener
    // before starting its callers; never use a fixed three-second assumption.
    let deadline = Instant::now() + Duration::from_secs(60);
    while std::os::unix::net::UnixStream::connect("/run/clinx-rust/authority.sock").is_err() {
        if Instant::now() >= deadline {
            return Err("ADMIN_AUTHORITY_NOT_READY".into());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    system(&["start", UNITS[1], UNITS[2], "clinx-rust-mcp.socket"])?;
    Ok(())
}
fn backup() -> Result<PathBuf> {
    let dir = tempfile::Builder::new()
        .prefix("backup-")
        .tempdir_in(ADMIN)
        .map_err(|e| e.to_string())?
        .keep();
    io(fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)))?;
    let mut c = command("/usr/bin/cp");
    c.args(["-a", STATE]).arg(dir.join("authority"));
    output(c, 60)?;
    for unit in UNITS {
        io(fs::copy(unit_path(unit), dir.join(unit)))?;
    }
    io(fs::copy(policy_path()?, dir.join("policy.before.json")))?;
    io(io(fs::File::open(&dir))?.sync_all())?;
    Ok(dir)
}
fn select(id: &str) -> Result<Value> {
    let next = Path::new(ROOT).join("releases").join(id);
    checked_release(&next)?;
    let old = current()?;
    if next == old {
        return Ok(json!({"status":"ALREADY_SELECTED","release":next}));
    }
    let mut units = BTreeMap::new();
    for (unit, user) in UNITS.into_iter().zip([
        "clinx-authority",
        "clinx-api",
        "clinx-transport",
        "clinx-api",
    ]) {
        let p = unit_path(unit);
        trusted(&p, 0)?;
        let text = io(fs::read_to_string(p))?;
        if !text.lines().any(|l| l == format!("User={user}"))
            || !text.contains(old.to_str().unwrap())
        {
            return Err("ADMIN_UNIT_IDENTITY_CONFLICT".into());
        }
        units.insert(
            unit,
            text.replace(old.to_str().unwrap(), next.to_str().unwrap()),
        );
    }
    stop_all()?;
    let saved = backup()?;
    for (unit, text) in units {
        install::write(&unit_path(unit), text.as_bytes(), false, 0o644)?;
    }
    start_all()?;
    Ok(json!({"status":"SELECTED_ACCEPTANCE_PENDING","before":old,"release":next,"backup":saved}))
}
fn stage(candidate: &str, head: &str) -> Result<Value> {
    // Read every operator-owned input as the operator, never as root. The
    // resulting snapshot is immutable before any candidate program executes.
    let git = |args: &[&str]| -> Result<Vec<u8>> {
        let mut c = as_user(
            "pvxlabs",
            Path::new("/usr/bin/git"),
            &[
                vec!["-C".into(), SOURCE.into()],
                args.iter().map(|s| s.to_string()).collect(),
            ]
            .concat(),
        );
        build::run(&mut c, 15).map(|(o, _)| o)
    };
    if String::from_utf8_lossy(&git(&["rev-parse", "HEAD"])?).trim() != head {
        return Err("ADMIN_SOURCE_HEAD_CONFLICT".into());
    }
    if !git(&["status", "--porcelain=v1"])?.is_empty() {
        return Err("ADMIN_SOURCE_DIRTY".into());
    }
    let base = current()?;
    let temp = tempfile::Builder::new()
        .prefix("stage-")
        .permissions(fs::Permissions::from_mode(0o755))
        .tempdir_in(format!("{ADMIN}/staging"))
        .map_err(|e| e.to_string())?;
    let source = temp.path().join("source");
    io(fs::create_dir(&source))?;
    io(fs::set_permissions(
        &source,
        fs::Permissions::from_mode(0o755),
    ))?;
    for name in [
        "architecture/python-sources.json",
        "architecture/runtime-data.json",
        "clinx/mcp/registry.json",
    ] {
        let bytes = git(&["show", &format!("{head}:{name}")])?;
        build::write(&source.join(name), &bytes, 0o644)?;
    }
    let mut options = build::Options::default();
    options
        .0
        .insert("--source".into(), vec![source.display().to_string()]);
    options.0.insert(
        "--desktop-base-release".into(),
        vec![base.display().to_string()],
    );
    options.0.insert(
        "--output".into(),
        vec![temp.path().join("receipt.json").display().to_string()],
    );
    for (name, key) in build::BINARIES {
        let input = Path::new(candidate).join(name);
        let mut c = as_user(
            "pvxlabs",
            Path::new("/usr/bin/cat"),
            &[input.display().to_string()],
        );
        let (bytes, _) = build::run(&mut c, 15)?;
        if bytes.len() > 32 * 1024 * 1024 || !bytes.starts_with(b"\x7fELF") {
            return Err("ADMIN_NATIVE_CANDIDATE_REQUIRED".into());
        }
        let p = temp.path().join(name);
        build::write(&p, &bytes, 0o755)?;
        options
            .0
            .insert((*key).into(), vec![p.display().to_string()]);
    }
    let mut result = build::stage_at_revision(
        &options,
        &Path::new(ROOT).join("releases"),
        0,
        0,
        head.as_bytes(),
    )?;
    if result["head"] != head {
        return Err("ADMIN_SOURCE_HEAD_CONFLICT".into());
    }
    result["source_cwd"] = json!(SOURCE);
    let release = PathBuf::from(
        result["release"]
            .as_str()
            .ok_or("ADMIN_RELEASE_UNAVAILABLE")?,
    );
    let mut manifest = result.clone();
    manifest.as_object_mut().unwrap().remove("release");
    build::save(&release.join("manifest.json"), &manifest)?;
    checked_release(&release)?;
    build::save(
        &Path::new(ADMIN).join("receipts").join(format!(
            "{}-stage.json",
            result["version"].as_str().unwrap()
        )),
        &result,
    )?;
    Ok(result)
}
pub fn recovery_routes(unit: &str) -> Result<Vec<String>> {
    let line = unit
        .lines()
        .find_map(|l| l.strip_prefix("ExecStart="))
        .ok_or("ADMIN_DISPATCHER_CONFIG_INVALID")?;
    let parts = line.split_whitespace().skip(1).collect::<Vec<_>>();
    let mut out = vec![];
    let mut i = 0;
    while i < parts.len() {
        let (count, keep) = match parts[i] {
            "--provider-route" => (3, true),
            "--provider-gateway" | "--provider-gateway-uid" | "--remote-host" => (2, true),
            "--socket" | "--authority-uid" | "--host-config" => (2, false),
            _ => return Err("ADMIN_DISPATCHER_CONFIG_INVALID".into()),
        };
        if i + count > parts.len() || parts[i + 1..i + count].iter().any(|v| v.starts_with('-')) {
            return Err("ADMIN_DISPATCHER_CONFIG_INVALID".into());
        }
        if keep {
            out.extend(parts[i..i + count].iter().map(|s| s.to_string()));
        }
        i += count;
    }
    Ok(out)
}
fn recover(id: &str, execution: &str) -> Result<Value> {
    let release = Path::new(ROOT).join("releases").join(id);
    checked_release(&release)?;
    let unit = unit_path(UNITS[2]);
    trusted(&unit, 0)?;
    let mut args = vec![
        "--socket".into(),
        "/run/clinx-rust/authority.sock".into(),
        "--authority-uid".into(),
        "993".into(),
        "--recover-execution".into(),
        execution.into(),
    ];
    args.extend(recovery_routes(&io(fs::read_to_string(unit))?)?);
    // No idle precondition: this operation resolves a held execution using
    // original Provider evidence. It cannot stop services or send new work.
    let result = output(
        as_user(
            "clinx-transport",
            &release.join("clinx-native-transport"),
            &args,
        ),
        90,
    )?;
    let value: Value =
        serde_json::from_str(&result).map_err(|_| "ADMIN_RECOVERY_OUTPUT_INVALID")?;
    if value["execution_ref"] != execution {
        return Err("ADMIN_RECOVERY_IDENTITY_CONFLICT".into());
    }
    Ok(value)
}
fn policy(hash: &str, op: &str) -> Result<Value> {
    let mut bytes = Vec::new();
    io(std::io::stdin()
        .take(1024 * 1024 + 1)
        .read_to_end(&mut bytes))?;
    if bytes.len() > 1024 * 1024 {
        return Err("ADMIN_POLICY_TOO_LARGE".into());
    }
    let next: clinx_decision_kernel::business::Policy =
        serde_json::from_slice(&bytes).map_err(|_| "INVALID_POLICY")?;
    let next = serde_json::to_value(next).map_err(|_| "INVALID_POLICY")?;
    trusted(Path::new(MAINTENANCE), 0)?;
    let db = clinx_decision_kernel::read_projection::readonly(
        &Path::new(STATE).join("authority.sqlite3"),
        "ADMIN_STATE_UNAVAILABLE",
        false,
    )?;
    let identity: String = db
        .query_row("SELECT id FROM identity", [], |r| r.get(0))
        .map_err(|_| "ADMIN_STATE_UNAVAILABLE")?;
    drop(db);
    let path = Path::new(ROOT).join("config").join(format!(
        "policy-{}.json",
        clinx_decision_kernel::execution_core::digest(&next)
    ));
    if !path.exists() {
        install::write(
            &path,
            serde_json::to_string(&next).unwrap().as_bytes(),
            true,
            0o644,
        )?;
    }
    trusted(&path, 0)?;
    if read_json(&path)? != next {
        return Err("ADMIN_POLICY_FILE_CONFLICT".into());
    }
    let old = policy_path()?;
    stop_all()?;
    let saved = backup()?;
    let args = vec![
        "extend-business-policy".into(),
        "--state".into(),
        STATE.into(),
        "--policy".into(),
        path.display().to_string(),
        "--authority-id".into(),
        identity,
        "--expected-policy-hash".into(),
        hash.into(),
        "--operation-id".into(),
        op.into(),
        "--approved".into(),
    ];
    let receipt: Value = serde_json::from_str(&output(
        as_user("clinx-authority", Path::new(MAINTENANCE), &args),
        60,
    )?)
    .map_err(|_| "ADMIN_RECEIPT_UNKNOWN")?;
    // The Rust transaction owns unknown-ACK/idempotency semantics. Do not
    // restore an old DB if subsequent service/configuration selection fails.
    build::save(
        &Path::new(ADMIN)
            .join("receipts")
            .join(format!("policy-{op}.json")),
        &receipt,
    )?;
    let unit = unit_path(UNITS[0]);
    let text = io(fs::read_to_string(&unit))?;
    install::write(
        &unit,
        text.replace(old.to_str().unwrap(), path.to_str().unwrap())
            .as_bytes(),
        false,
        0o644,
    )?;
    start_all()?;
    Ok(json!({"receipt":receipt,"backup":saved,"status":"POLICY_SELECTED_ACCEPTANCE_PENDING"}))
}
pub fn run(args: &[String]) -> Result<Value> {
    validate(args)?;
    if !rustix::process::geteuid().is_root() {
        return Err("ADMINISTRATOR_REQUIRED".into());
    }
    trusted(&io(std::env::current_exe())?, 0)?;
    admin_dirs()?;
    let lock_path = Path::new(ADMIN).join("operator.lock");
    let lock = io(fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(&lock_path))?;
    trusted(&lock_path, 0)?;
    lock.try_lock().map_err(|_| "ADMIN_OPERATION_IN_PROGRESS")?;
    let release = current()?;
    match args[0].as_str() {
        "status" => Ok(
            json!({"release":release,"policy":policy_path()?,"services":system(&["show",UNITS[0],UNITS[1],UNITS[2],"clinx-rust-mcp.socket","-p","Id,MainPID,ActiveState,SubState,NRestarts"])?}),
        ),
        "recover" => recover(&args[1], &args[2]),
        "nodes" => {
            let registry = clinx_decision_kernel::node_registry::Registry::read(
                &json!({"path":Path::new(CENTRE).join("registry.sqlite3"),"method":"registry.list_nodes","arguments":{"authorized_only":false}}),
            )?;
            Ok(
                json!({"nodes":registry,"sharing":read_json(&Path::new(CENTRE).join("sharing.json"))?}),
            )
        }
        "journal" => {
            let mut c = command("/usr/bin/journalctl");
            c.args([
                "--no-pager",
                "-u",
                &args[1],
                "-n",
                args.get(2).map(String::as_str).unwrap_or("100"),
            ]);
            Ok(json!({"journal":output(c,15)?}))
        }
        "service" => {
            if args[1] != "start" {
                idle()?;
            }
            Ok(json!({"result":system(&[&args[1],&args[2]])?}))
        }
        "pair-listen" => {
            let a = [
                "--state",
                CENTRE,
                "--node-id",
                "p620",
                "pair-listen",
                "--bind",
                "0.0.0.0",
                "--port",
                "18774",
                "--seconds",
                "120",
            ]
            .map(String::from);
            let status = io(as_user("clinx-api", &release.join("clinx-node-daemon"), &a).status())?;
            if !status.success() {
                return Err("ADMIN_PAIR_LISTENER_FAILED".into());
            }
            Ok(json!({"status":"PAIRING_WINDOW_CLOSED"}))
        }
        "share" => {
            let previous = read_json(&Path::new(CENTRE).join("sharing.json"))?;
            let old = &previous["scopes"][&args[1]];
            if old.is_null() || old["user_scope"] != args[2] {
                return Err("ADMIN_EXISTING_SCOPE_REQUIRED".into());
            }
            let mut a = vec![
                "--state".into(),
                CENTRE.into(),
                "--node-id".into(),
                "p620".into(),
                "share".into(),
                "--peer".into(),
                args[1].clone(),
                "--user-scope".into(),
                args[2].clone(),
                "--approved".into(),
            ];
            for project in old["projects"]
                .as_array()
                .cloned()
                .unwrap_or_else(|| vec![json!("*")])
            {
                a.extend([
                    "--project".into(),
                    project.as_str().ok_or("ADMIN_SCOPE_INVALID")?.into(),
                ]);
            }
            if args[3] == "revoke" {
                a.push("--revoke".into())
            } else {
                a.push("--read-sessions".into());
                if args[3] == "execute" {
                    a.push("--execute-tasks".into())
                }
            }
            serde_json::from_str(&output(
                as_user("clinx-api", &release.join("clinx-node-daemon"), &a),
                15,
            )?)
            .map_err(|_| "ADMIN_SCOPE_RECEIPT_INVALID".into())
        }
        "probe" => {
            let policy = read_json(&policy_path()?)?;
            let project = policy["projects"]
                .as_array()
                .ok_or("INVALID_POLICY")?
                .iter()
                .find(|p| p["host"] == args[1] && p["alias"] == args[2])
                .ok_or("ADMIN_PROJECT_NOT_CONFIGURED")?;
            let routes = policy["provider_routes"]
                .as_object()
                .ok_or("ADMIN_ROUTES_REQUIRED")?;
            let route = routes
                .get(&args[1])
                .and_then(|r| r[0].as_str())
                .ok_or("ADMIN_ROUTE_REQUIRED")?;
            let gateways: serde_json::Map<String, Value> = routes
                .iter()
                .filter(|(host, _)| *host != "p620")
                .map(|(h, v)| (h.clone(), v[0].clone()))
                .collect();
            let request =
                json!({"host":args[1],"cwd":project["cwd"],"mode":"repository","args":[]});
            let mut a = vec![
                "-i".into(),
                "PATH=/usr/bin:/bin".into(),
                format!("CLINX_RUST_PROVIDER_GATEWAYS={}", Value::Object(gateways)),
                "CLINX_RUST_PROVIDER_GATEWAY_UID=990".into(),
                release.join("clinx-provider").display().to_string(),
                "--observe".into(),
                route.into(),
                request.to_string(),
            ];
            let c = as_user("clinx-authority", Path::new("/usr/bin/env"), &a);
            a.clear();
            serde_json::from_str(&output(c, 45)?).map_err(|_| "ADMIN_PROBE_INVALID".into())
        }
        "stage" => stage(&args[1], &args[2]),
        "select" => select(&args[1]),
        "policy" => policy(&args[1], &args[2]),
        _ => Err("ADMIN_ARGUMENT_DENIED".into()),
    }
}
