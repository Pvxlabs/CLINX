//! Explicit installation tooling. No service start, task import or database IO.
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    io::Write,
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
};
pub type Result<T> = std::result::Result<T, String>;
#[derive(Clone)]
pub struct Account {
    pub name: String,
    pub uid: u32,
    pub gid: u32,
}
pub trait System {
    fn account(&mut self, name: &str) -> Result<Account>;
    fn account_uid(&mut self, uid: u32) -> Result<Account>;
    fn group(&mut self, name: &str) -> Result<u32>;
    fn run(&mut self, args: &[String]) -> Result<String>;
    fn groups(&mut self, name: &str, gid: u32) -> Result<Vec<u32>>;
    fn non_login(&mut self, name: &str) -> Result<bool>;
}
#[derive(Clone)]
pub struct Layout {
    pub root: PathBuf,
    pub systemd: PathBuf,
    pub tmpfiles: PathBuf,
    pub run: PathBuf,
}
impl Default for Layout {
    fn default() -> Self {
        Self {
            root: "/var/lib/clinx-rust".into(),
            systemd: "/etc/systemd/system".into(),
            tmpfiles: "/etc/tmpfiles.d".into(),
            run: "/run/clinx-rust".into(),
        }
    }
}
fn io<T>(v: std::io::Result<T>) -> Result<T> {
    v.map_err(|e| format!("INSTALL_IO:{e}"))
}
fn text(path: &Path) -> Result<String> {
    io(fs::read_to_string(path))
}
fn value(path: &Path) -> Result<Value> {
    serde_json::from_str(&text(path)?).map_err(|_| "INVALID_JSON".into())
}
pub fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
pub fn write(path: &Path, bytes: &[u8], exclusive: bool, mode: u32) -> Result<()> {
    let mut opts = fs::OpenOptions::new();
    opts.write(true)
        .mode(mode)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32);
    if exclusive {
        opts.create_new(true);
    } else {
        opts.create(true).truncate(true);
    }
    let mut f = io(opts.open(path))?;
    io(f.write_all(bytes))?;
    io(f.sync_all())?;
    io(fs::set_permissions(path, fs::Permissions::from_mode(mode)))?;
    io(fs::File::open(path.parent().ok_or("PARENT_REQUIRED")?).and_then(|d| d.sync_all()))
}
fn write_json(path: &Path, v: &Value, exclusive: bool) -> Result<()> {
    let mut b = serde_json::to_vec_pretty(v).map_err(|_| "INVALID_JSON")?;
    b.push(b'\n');
    write(path, &b, exclusive, 0o644)
}
fn owner(path: &Path, a: &Account, group: u32) -> Result<()> {
    let m = io(fs::symlink_metadata(path))?;
    if m.file_type().is_symlink() {
        return Err("SYMLINK_DIRECTORY_DENIED".into());
    }
    if m.uid() != a.uid || m.gid() != group {
        rustix::fs::chown(
            path,
            Some(rustix::fs::Uid::from_raw(a.uid)),
            Some(rustix::fs::Gid::from_raw(group)),
        )
        .map_err(|e| format!("CHOWN_FAILED:{e}"))?;
    }
    Ok(())
}
fn owned_dir(path: &Path, a: &Account, mode: u32, group: u32) -> Result<()> {
    io(fs::DirBuilder::new()
        .recursive(true)
        .mode(mode)
        .create(path))?;
    owner(path, a, group)?;
    io(fs::set_permissions(path, fs::Permissions::from_mode(mode)))
}
pub fn validate_release(release: &Path, expected_uid: u32, callers: bool) -> Result<Value> {
    let m = value(&release.join("manifest.json"))?;
    for (key, expected, error) in [
        (
            "config_compiler",
            "RUST_CONFIG_COMPILER_V1",
            "QUALIFIED_RUST_CONFIG_COMPILER_REQUIRED",
        ),
        (
            "source_layout",
            "qualified-clinx-v1",
            "QUALIFIED_PACKAGE_LAYOUT_REQUIRED",
        ),
        (
            "provider_exchange",
            "RUST_PROVIDER_V1",
            "QUALIFIED_RUST_PROVIDER_REQUIRED",
        ),
        (
            "observation_runtime",
            "RUST_OBSERVATION_V1",
            "QUALIFIED_RUST_OBSERVATION_REQUIRED",
        ),
        (
            "client_runtime",
            "RUST_CLIENT_RUNTIME_V1",
            "QUALIFIED_RUST_CLIENT_RUNTIME_REQUIRED",
        ),
        (
            "device_runtime",
            "RUST_DEVICE_RUNTIME_V1",
            "QUALIFIED_RUST_DEVICE_RUNTIME_REQUIRED",
        ),
        (
            "node_daemon",
            "RUST_NODE_DAEMON_V1",
            "QUALIFIED_RUST_DEVICE_RUNTIME_REQUIRED",
        ),
        (
            "thin_runtime",
            "RUST_THIN_RUNTIME_V1",
            "QUALIFIED_RUST_THIN_RUNTIME_REQUIRED",
        ),
        (
            "collector_runtime",
            "RUST_COLLECTOR_V1",
            "QUALIFIED_RUST_COLLECTOR_REQUIRED",
        ),
        (
            "node_network",
            "RUST_NODE_NETWORK_V1",
            "QUALIFIED_RUST_NODE_NETWORK_REQUIRED",
        ),
        (
            "node_api",
            "RUST_NODE_API_V1",
            "QUALIFIED_RUST_NODE_API_REQUIRED",
        ),
        ("mcp_runtime", "RUST_MCP_V1", "QUALIFIED_RUST_MCP_REQUIRED"),
    ] {
        if m[key] != expected {
            return Err(error.into());
        }
    }
    let files = m["files"].as_object().ok_or("RELEASE_FILES_REQUIRED")?;
    for (name, error) in [
        ("clinx-provider", "QUALIFIED_RUST_PROVIDER_REQUIRED"),
        (
            "clinx-node-daemon",
            "QUALIFIED_RUST_DEVICE_RUNTIME_REQUIRED",
        ),
        ("clinx-node", "QUALIFIED_RUST_NODE_API_REQUIRED"),
        ("clinx-mcp", "QUALIFIED_RUST_MCP_REQUIRED"),
    ] {
        if !files.contains_key(name) {
            return Err(error.into());
        }
    }
    if !callers {
        for (key, expected, error) in [
            (
                "host_transport",
                "RUST_HOST_V1",
                "QUALIFIED_RUST_HOST_WORKER_REQUIRED",
            ),
            (
                "workspace_observer",
                "RUST_PROVIDER_V1",
                "QUALIFIED_RUST_PROVIDER_REQUIRED",
            ),
            (
                "desktop_socket_maintenance",
                "RUST_DESKTOP_SOCKET_V1",
                "QUALIFIED_RUST_DESKTOP_SOCKET_REQUIRED",
            ),
            (
                "observation_directory",
                "RUST_OBSERVATION_DIRECTORY_V1",
                "QUALIFIED_NATIVE_ENTRYPOINT_REQUIRED",
            ),
            (
                "observer_runtime",
                "RUST_OBSERVER_V1",
                "QUALIFIED_NATIVE_ENTRYPOINT_REQUIRED",
            ),
            (
                "device_cli",
                "RUST_DEVICE_CLI_V1",
                "QUALIFIED_NATIVE_ENTRYPOINT_REQUIRED",
            ),
        ] {
            if m[key] != expected {
                return Err(error.into());
            }
        }
        if m["python_sources"] != json!([]) || m["python_dependencies"] != json!([]) {
            return Err("QUALIFIED_RUST_DESKTOP_SOCKET_REQUIRED".into());
        }
        if !files.contains_key("clinx-observer") || !files.contains_key("clinx-device") {
            return Err("QUALIFIED_NATIVE_ENTRYPOINT_REQUIRED".into());
        }
        if release.file_name().and_then(|s| s.to_str()) != m["version"].as_str() {
            return Err("RELEASE_IDENTITY_MISMATCH".into());
        }
    }
    for (name, digest) in files {
        let p = Path::new(name);
        if p.is_absolute()
            || p.components()
                .any(|c| !matches!(c, std::path::Component::Normal(_)))
        {
            return Err("RELEASE_PATH_INVALID".into());
        }
        let p = release.join(p);
        let meta = io(fs::symlink_metadata(&p))?;
        if !meta.is_file()
            || meta.file_type().is_symlink()
            || meta.uid() != expected_uid
            || meta.mode() & 0o022 != 0
            || json!(hash(&io(fs::read(&p))?)) != *digest
        {
            return Err(if callers {
                "IMMUTABLE_RELEASE_REQUIRED".into()
            } else {
                format!("RELEASE_CONTENT_MISMATCH:{name}")
            });
        }
    }
    Ok(m)
}
pub fn render_units(
    values: &BTreeMap<&str, String>,
    callers: bool,
) -> Result<BTreeMap<String, String>> {
    for v in values.values() {
        if v.chars()
            .any(|c| c.is_whitespace() || c == '%' || c == '\0')
        {
            return Err("UNIT_VALUE_INVALID".into());
        }
    }
    let templates: Vec<(&str, &str)> = if callers {
        vec![
            (
                "clinx-rust-mcp.socket",
                include_str!("install_templates/clinx-rust-mcp.socket"),
            ),
            (
                "clinx-rust-mcp@.service",
                include_str!("install_templates/clinx-rust-mcp@.service"),
            ),
            (
                "clinx-rust-centre.service",
                include_str!("install_templates/clinx-rust-centre.service"),
            ),
        ]
    } else {
        vec![
            (
                "clinx-rust-authority.service",
                // Preserve the frozen original unit's final separator line.
                concat!(
                    include_str!("install_templates/clinx-rust-authority.service"),
                    "\n"
                ),
            ),
            (
                "clinx-rust-dispatcher.service",
                concat!(
                    include_str!("install_templates/clinx-rust-dispatcher.service"),
                    "\n"
                ),
            ),
        ]
    };
    let mut out = BTreeMap::new();
    for (name, raw) in templates {
        let mut s = raw.to_string();
        for (k, v) in values {
            s = s.replace(&format!("@{k}@"), v);
        }
        out.insert(name.into(), s);
    }
    Ok(out)
}
pub fn configuration(original: &str, layout: &Layout) -> Result<(String, Value)> {
    let mut config: toml::Value = toml::from_str(original).map_err(|_| "INVALID_BRIDGE_CONFIG")?;
    let app = config
        .get_mut("app_server")
        .and_then(toml::Value::as_table_mut)
        .ok_or("APP_SERVER_REQUIRED")?;
    app.insert(
        "local_socket".into(),
        toml::Value::String(format!("{}-provider/provider.sock", layout.run.display())),
    );
    app.remove("native_socket");
    let rt = config
        .get_mut("runtime")
        .and_then(toml::Value::as_table_mut)
        .ok_or("RUNTIME_REQUIRED")?;
    rt.insert(
        "log_dir".into(),
        toml::Value::String(layout.root.join("transport/logs").display().to_string()),
    );
    rt.remove("task_db_path");
    let json = serde_json::to_value(&config).map_err(|_| "INVALID_BRIDGE_CONFIG")?;
    let projects = json["projects"]
        .as_object()
        .ok_or("PROJECTS_REQUIRED")?
        .iter()
        .map(|(alias, v)| json!({"alias":alias,"host":"p620","cwd":v["cwd"]}))
        .collect::<Vec<_>>();
    let mut workspaces = vec![];
    if let Some(ws) = json["workspaces"].as_object() {
        for w in ws.values() {
            let mut row = json!({});
            for k in ["host", "root", "allow_existing_projects"] {
                row[k] = w.get(k).ok_or("WORKSPACE_CONFIG_INVALID")?.clone();
            }
            workspaces.push(row);
        }
    }
    let policy = json!({"projects":projects,"workspaces":workspaces,"endpoints":[format!("{}-provider/provider.sock",layout.run.display())],"lease_ms":120000,"host_operations":[{"capability":"LOCAL_HOST_PROCESS","operation":"user_identity","operation_class":"READ_ONLY_HOST","target":""}]});
    Ok((
        toml::to_string(&config).map_err(|_| "INVALID_BRIDGE_CONFIG")?,
        policy,
    ))
}
fn no_units(layout: &Layout, callers: bool) -> Result<()> {
    let names = if callers {
        vec![
            "clinx-rust-mcp.socket",
            "clinx-rust-mcp@.service",
            "clinx-rust-centre.service",
        ]
    } else {
        vec![
            "clinx-rust-authority.service",
            "clinx-rust-dispatcher.service",
        ]
    };
    for name in names {
        if fs::symlink_metadata(layout.systemd.join(name)).is_ok() {
            return Err(format!(
                "{}:{name}",
                if callers {
                    "CALLER_UNIT_EXISTS"
                } else {
                    "SERVICE_ALREADY_EXISTS"
                }
            ));
        }
    }
    Ok(())
}
fn variables(
    l: &Layout,
    r: &Path,
    worker: &Account,
    authority: &Account,
) -> BTreeMap<&'static str, String> {
    BTreeMap::from([
        ("ROOT", l.root.display().to_string()),
        ("RUN", l.run.display().to_string()),
        ("RELEASE", r.display().to_string()),
        ("WORKER_UID", worker.uid.to_string()),
        ("AUTHORITY_UID", authority.uid.to_string()),
        ("WORKER_NAME", worker.name.clone()),
    ])
}
fn compiled(sys: &mut impl System, release: &Path, kind: &str, config: &Path) -> Result<Value> {
    let out = sys.run(&[
        release.join("clinx-node").display().to_string(),
        "--compile-config".into(),
        kind.into(),
        config.display().to_string(),
    ])?;
    serde_json::from_str(&out).map_err(|_| "COMPILED_CONFIG_INVALID".into())
}
pub struct Services {
    pub release: PathBuf,
    pub operator_home: PathBuf,
    pub bridge_config: PathBuf,
    pub desktop_socket: PathBuf,
    pub desktop_thread_id: String,
}
pub fn install_services(
    l: &Layout,
    args: &Services,
    uid: u32,
    sys: &mut impl System,
) -> Result<Value> {
    no_units(l, false)?;
    let release = io(args.release.canonicalize())?;
    validate_release(&release, uid, false)?;
    let worker = sys.account_uid(io(fs::metadata(&args.operator_home))?.uid())?;
    if worker.uid == 0
        || ["clinx-authority", "clinx-api", "clinx-transport"].contains(&worker.name.as_str())
    {
        return Err("WORKER_AUTHORITY_IDENTITY_CONFLICT".into());
    }
    let authority = sys.account("clinx-authority")?;
    let api = sys.account("clinx-api")?;
    let transport = sys.account("clinx-transport")?;
    let socket = io(args.desktop_socket.canonicalize())?;
    let (config, policy) = configuration(&text(&args.bridge_config)?, l)?;
    let mut vars = variables(l, &release, &worker, &authority);
    vars.insert("API_UID", api.uid.to_string());
    vars.insert("TRANSPORT_UID", transport.uid.to_string());
    vars.insert(
        "DESKTOP_PARENT",
        socket
            .parent()
            .ok_or("SOCKET_PARENT_REQUIRED")?
            .display()
            .to_string(),
    );
    let units = render_units(&vars, false)?;
    let cfg = l.root.join("config");
    io(fs::DirBuilder::new().mode(0o755).create(&cfg).or_else(|e| {
        if e.kind() == std::io::ErrorKind::AlreadyExists {
            Ok(())
        } else {
            Err(e)
        }
    }))?;
    write(
        &cfg.join("worker-user"),
        format!("{}\n", worker.name).as_bytes(),
        false,
        0o644,
    )?;
    for (a, name) in [
        (&worker, "worker"),
        (&api, "api"),
        (&transport, "transport"),
    ] {
        owned_dir(&l.root.join(name), a, 0o700, a.gid)?;
    }
    let ipc = sys.group("clinx-ipc")?;
    owned_dir(
        &PathBuf::from(format!("{}-provider", l.run.display())),
        &worker,
        0o2750,
        ipc,
    )?;
    sys.run(&[
        "runuser".into(),
        "-u".into(),
        worker.name.clone(),
        "--".into(),
        release.join("clinx-provider").display().to_string(),
        "--desktop-socket".into(),
        "--socket".into(),
        socket.display().to_string(),
        "--witness-thread-id".into(),
        args.desktop_thread_id.clone(),
        "--apply".into(),
    ])?;
    write(&cfg.join("bridge.toml"), config.as_bytes(), false, 0o644)?;
    for kind in ["host", "mcp"] {
        write_json(
            &cfg.join(format!("{kind}-runtime.json")),
            &compiled(sys, &release, kind, &cfg.join("bridge.toml"))?,
            false,
        )?;
    }
    write_json(&cfg.join("policy.json"), &policy, false)?;
    for (name, content) in &units {
        write(&l.systemd.join(name), content.as_bytes(), true, 0o644)?;
    }
    for parent in [&args.operator_home, &args.operator_home.join("dev")] {
        sys.run(&[
            "setfacl".into(),
            "-m".into(),
            [
                "clinx-authority",
                "clinx-api",
                "clinx-transport",
                &worker.name,
            ]
            .iter()
            .map(|u| format!("u:{u}:--x"))
            .collect::<Vec<_>>()
            .join(","),
            parent.display().to_string(),
        ])?;
    }
    sys.run(&["systemctl".into(), "daemon-reload".into()])?;
    let command = units["clinx-rust-authority.service"]
        .lines()
        .find_map(|l| l.strip_prefix("ExecStart="))
        .ok_or("UNIT_COMMAND_MISSING")?;
    Ok(
        json!({"status":"INSTALLED_NOT_STARTED","release":release,"units":units.keys().collect::<Vec<_>>(),"init_command":format!("{command} --init-only")}),
    )
}
pub struct Callers {
    pub release: PathBuf,
    pub legacy_node_state: PathBuf,
    pub operator: String,
    pub output: PathBuf,
}
pub fn stage_callers(l: &Layout, args: &Callers, uid: u32, sys: &mut impl System) -> Result<Value> {
    no_units(l, true)?;
    let release = io(args.release.canonicalize())?;
    validate_release(&release, uid, true)?;
    let api = sys.account("clinx-api")?;
    let worker = sys.account(text(&l.root.join("config/worker-user"))?.trim())?;
    let authority = sys.account("clinx-authority")?;
    let operator = sys.account(&args.operator)?;
    let mut vars = variables(l, &release, &worker, &authority);
    vars.insert("OPERATOR", args.operator.clone());
    vars.insert("OPERATOR_GID", operator.gid.to_string());
    let units = render_units(&vars, true)?;
    let mcp = l.root.join("config/mcp-runtime.json");
    let config = compiled(sys, &release, "mcp", &l.root.join("config/bridge.toml"))?;
    if mcp.exists() {
        if value(&mcp)? != config {
            return Err("MCP_CONFIG_IDENTITY_MISMATCH".into());
        }
    } else {
        write_json(&mcp, &config, true)?;
    }
    let state = l.root.join("api/node-centre");
    io(fs::DirBuilder::new().mode(0o700).create(&state))?;
    owner(&state, &api, api.gid)?;
    let mut copied = BTreeMap::new();
    for name in ["identity.json", "peers.json", "sharing.json"] {
        let bytes = io(fs::read(args.legacy_node_state.join(name)))?;
        let target = state.join(name);
        write(&target, &bytes, true, 0o600)?;
        owner(&target, &api, api.gid)?;
        copied.insert(name, hash(&bytes));
    }
    for (name, content) in &units {
        write(&l.systemd.join(name), content.as_bytes(), true, 0o644)?;
    }
    let mut tmp = include_str!("install_templates/clinx-rust.conf").to_string();
    for (k, v) in &vars {
        tmp = tmp.replace(&format!("@{k}@"), v);
    }
    write(
        &l.tmpfiles.join("clinx-rust.conf"),
        tmp.as_bytes(),
        false,
        0o644,
    )?;
    let run = PathBuf::from(format!("{}-api", l.run.display()));
    if !run.exists() {
        io(fs::DirBuilder::new().mode(0o711).create(&run))?;
    }
    sys.run(&["systemctl".into(), "daemon-reload".into()])?;
    let result = json!({"status":"STAGED_NOT_SELECTED","release":release,"units":units.keys().collect::<Vec<_>>(),"preserved_config_sha256":copied,"legacy_databases_imported":false,"centre_port":18774,"existing_tunnel_launcher":"UNCHANGED","ipc_group":sys.group("clinx-ipc")?});
    write_json(&args.output, &result, false)?;
    Ok(result)
}

/// Provision current dedicated identities; the execution worker is selected by
/// services from the operator home. The obsolete clinx-worker account is unused.
pub fn provision(l: &Layout, sys: &mut impl System, parent_uid: u32) -> Result<Value> {
    for group in ["clinx-ipc", "clinx-provider-ipc"] {
        if sys.group(group).is_err() {
            sys.run(&["/usr/sbin/groupadd".into(), "--system".into(), group.into()])?;
        }
    }
    for name in ["clinx-authority", "clinx-api", "clinx-transport"] {
        if sys.account(name).is_err() {
            sys.run(&[
                "/usr/sbin/useradd".into(),
                "--system".into(),
                "--no-create-home".into(),
                "--user-group".into(),
                "--shell".into(),
                "/usr/sbin/nologin".into(),
                name.into(),
            ])?;
        }
        let a = sys.account(name)?;
        if a.uid == 0 || !sys.non_login(name)? {
            return Err(format!("DEDICATED_PRINCIPAL_CONFLICT:{name}"));
        }
        let mut allowed = vec![a.gid];
        for group in ["clinx-ipc", "clinx-provider-ipc"] {
            if name == "clinx-authority" && group == "clinx-provider-ipc" {
                continue;
            }
            sys.run(&[
                "/usr/sbin/usermod".into(),
                "-a".into(),
                "-G".into(),
                group.into(),
                name.into(),
            ])?;
            allowed.push(sys.group(group)?);
        }
        if sys
            .groups(name, a.gid)?
            .iter()
            .any(|g| !allowed.contains(g))
        {
            return Err(format!("PRINCIPAL_HAS_UNEXPECTED_GROUPS:{name}"));
        }
    }
    if fs::symlink_metadata(&l.root).is_ok_and(|m| m.file_type().is_symlink()) {
        return Err("STATE_ROOT_IDENTITY_CONFLICT".into());
    }
    if !l.root.exists() {
        io(fs::DirBuilder::new().mode(0o755).create(&l.root))?;
    }
    let m = io(fs::metadata(&l.root))?;
    if m.uid() != parent_uid || m.mode() & 0o022 != 0 {
        return Err("ROOT_CONTROLLED_STATE_PARENT_REQUIRED".into());
    }
    let writer = sys.account("clinx-authority")?;
    let state = l.root.join("authority");
    if !state.exists() {
        io(fs::DirBuilder::new().mode(0o700).create(&state))?;
        owner(&state, &writer, writer.gid)?;
    }
    let m = io(fs::symlink_metadata(&state))?;
    if m.file_type().is_symlink() || m.uid() != writer.uid || m.mode() & 0o077 != 0 {
        return Err("PRIVATE_AUTHORITY_DIRECTORY_REQUIRED".into());
    }
    let gid = sys.group("clinx-ipc")?;
    if !l.run.exists() {
        io(fs::DirBuilder::new().mode(0o750).create(&l.run))?;
        owner(&l.run, &writer, gid)?;
        io(fs::set_permissions(
            &l.run,
            fs::Permissions::from_mode(0o2750),
        ))?;
    }
    let m = io(fs::symlink_metadata(&l.run))?;
    if m.file_type().is_symlink()
        || m.uid() != writer.uid
        || m.gid() != gid
        || m.mode() & 0o022 != 0
    {
        return Err("PROTECTED_IPC_PARENT_REQUIRED".into());
    }
    Ok(
        json!({"status":"PASS","scope":"PRINCIPAL_PROVISIONING_ONLY","production_authority":"NOT_VERIFIED","state":state}),
    )
}
