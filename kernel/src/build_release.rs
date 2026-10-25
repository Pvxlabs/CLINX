//! Development-only immutable release construction and offline config CAS.
//! Never linked into a runtime service or copied into a runtime payload.
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::{Read, Seek, SeekFrom, Write},
    os::unix::{
        fs::{MetadataExt, PermissionsExt},
        process::CommandExt,
    },
    path::{Component, Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};
pub type Result<T> = std::result::Result<T, String>;
#[path = "build_archive.rs"]
pub mod archive;
#[path = "build_cas.rs"]
pub mod cas;
#[path = "build_transport.rs"]
pub mod transport;
#[path = "build_tunnel.rs"]
pub mod tunnel;
pub fn read(path: &Path) -> Result<Vec<u8>> {
    fs::read(path).map_err(|e| format!("READ:{}:{e}", path.display()))
}
pub fn value(path: &Path) -> Result<Value> {
    serde_json::from_slice(&read(path)?).map_err(|e| format!("INVALID_JSON:{}:{e}", path.display()))
}
pub fn sha(bytes: impl AsRef<[u8]>) -> String {
    format!("{:x}", Sha256::digest(bytes.as_ref()))
}
pub fn digest(path: &Path) -> Result<String> {
    Ok(sha(read(path)?))
}
pub fn ascii(s: &str) -> String {
    s.chars()
        .flat_map(|c| {
            if c as u32 >= 127 {
                let mut buf = [0; 2];
                c.encode_utf16(&mut buf)
                    .iter()
                    .map(|u| format!("\\u{u:04x}"))
                    .collect::<Vec<_>>()
            } else {
                vec![c.to_string()]
            }
        })
        .collect()
}
pub fn encoded(v: &Value) -> Vec<u8> {
    format!("{}\n", ascii(&serde_json::to_string_pretty(v).unwrap())).into_bytes()
}
fn default_json(v: &Value) -> String {
    let raw = ascii(&v.to_string());
    let (mut quoted, mut escaped) = (false, false);
    let mut out = String::new();
    for c in raw.chars() {
        out.push(c);
        if quoted {
            if escaped {
                escaped = false
            } else if c == '\\' {
                escaped = true
            } else if c == '"' {
                quoted = false
            }
        } else if c == '"' {
            quoted = true
        } else if c == ',' || c == ':' {
            out.push(' ')
        }
    }
    out
}
pub fn write(path: &Path, bytes: &[u8], mode: u32) -> Result<()> {
    if let Some(p) = path.parent() {
        fs::create_dir_all(p).map_err(|e| e.to_string())?
    }
    fs::write(path, bytes).map_err(|e| e.to_string())?;
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).map_err(|e| e.to_string())
}
pub fn save(path: &Path, v: &Value) -> Result<()> {
    write(path, &encoded(v), 0o644)
}
pub fn regular(path: &Path) -> Result<fs::Metadata> {
    let m = fs::symlink_metadata(path).map_err(|e| e.to_string())?;
    if !m.is_file() {
        return Err("NOT_REGULAR_FILE".into());
    }
    Ok(m)
}
pub fn state(path: &Path) -> Result<Value> {
    match fs::symlink_metadata(path) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Value::Null),
        Err(e) => Err(e.to_string()),
        Ok(_) => {
            let m = regular(path)?;
            Ok(json!({"sha256":digest(path)?,"mode":m.mode()&0o7777}))
        }
    }
}
pub fn relative(name: &str) -> Result<PathBuf> {
    let p = Path::new(name);
    if p.as_os_str().is_empty()
        || p.is_absolute()
        || p.components().any(|c| !matches!(c, Component::Normal(_)))
    {
        return Err("REGULAR_RELATIVE_PATH_REQUIRED".into());
    }
    Ok(p.into())
}
pub fn walk(root: &Path) -> Result<Vec<PathBuf>> {
    fn inner(root: &Path, p: &Path, out: &mut Vec<PathBuf>) -> Result<()> {
        for e in fs::read_dir(p).map_err(|e| e.to_string())? {
            let p = e.map_err(|e| e.to_string())?.path();
            let m = fs::symlink_metadata(&p).map_err(|e| e.to_string())?;
            if m.file_type().is_symlink() {
                return Err("SOURCE_SYMLINK_REFUSED".into());
            }
            if m.is_dir() {
                inner(root, &p, out)?
            } else if m.is_file() {
                out.push(p.strip_prefix(root).unwrap().into())
            } else {
                return Err("SOURCE_NON_REGULAR_REFUSED".into());
            }
        }
        Ok(())
    }
    let mut out = vec![];
    inner(root, root, &mut out)?;
    out.sort();
    Ok(out)
}
pub fn task_output(path: &Path, error: &str) -> Result<PathBuf> {
    let path = std::path::absolute(path).map_err(|e| e.to_string())?;
    if !path.starts_with("/data/artifacts")
        || path.exists()
        || path.components().any(|c| matches!(c, Component::ParentDir))
    {
        return Err(error.into());
    }
    for p in path.ancestors() {
        if fs::symlink_metadata(p).is_ok_and(|m| m.file_type().is_symlink()) {
            return Err("OUTPUT_SYMLINK_REFUSED".into());
        }
    }
    Ok(path)
}
pub fn run(cmd: &mut Command, timeout: u64) -> Result<(Vec<u8>, Vec<u8>)> {
    capture(cmd, timeout, None)
}
pub fn run_logged(
    cmd: &mut Command,
    timeout: u64,
    stdout: &Path,
    stderr: &Path,
) -> Result<(Vec<u8>, Vec<u8>)> {
    capture(cmd, timeout, Some((stdout, stderr)))
}
fn capture(
    cmd: &mut Command,
    timeout: u64,
    logs: Option<(&Path, &Path)>,
) -> Result<(Vec<u8>, Vec<u8>)> {
    let open = |path: Option<&Path>| -> Result<fs::File> {
        match path {
            Some(p) => fs::OpenOptions::new()
                .create_new(true)
                .read(true)
                .write(true)
                .open(p)
                .map_err(|e| e.to_string()),
            None => tempfile::tempfile().map_err(|e| e.to_string()),
        }
    };
    let mut stdout = open(logs.map(|v| v.0))?;
    let mut stderr = open(logs.map(|v| v.1))?;
    let mut child = cmd
        .stdin(Stdio::null())
        .stdout(stdout.try_clone().map_err(|e| e.to_string())?)
        .stderr(stderr.try_clone().map_err(|e| e.to_string())?)
        .process_group(0)
        .spawn()
        .map_err(|e| e.to_string())?;
    let until = Instant::now() + Duration::from_secs(timeout);
    let status = loop {
        if let Some(s) = child.try_wait().map_err(|e| e.to_string())? {
            break s;
        }
        if Instant::now() >= until {
            if let Some(pid) = rustix::process::Pid::from_raw(child.id() as i32) {
                let _ = rustix::process::kill_process_group(pid, rustix::process::Signal::KILL);
            }
            let _ = child.wait();
            return Err("BUILD_COMMAND_TIMEOUT".into());
        }
        std::thread::sleep(Duration::from_millis(10));
    };
    let mut out = vec![];
    let mut err = vec![];
    stdout.seek(SeekFrom::Start(0)).map_err(|e| e.to_string())?;
    stderr.seek(SeekFrom::Start(0)).map_err(|e| e.to_string())?;
    stdout
        .take(64 * 1024 * 1024)
        .read_to_end(&mut out)
        .map_err(|e| e.to_string())?;
    stderr
        .take(64 * 1024 * 1024)
        .read_to_end(&mut err)
        .map_err(|e| e.to_string())?;
    if !status.success() {
        return Err(format!(
            "BUILD_COMMAND_FAILED:{}",
            String::from_utf8_lossy(&err)
        ));
    }
    Ok((out, err))
}
pub fn marker(binary: &Path, option: &str, expected: &str, error: &str) -> Result<()> {
    let before = digest(binary)?;
    // Candidate executables are never privileged installation code. Root
    // staging drops groups/identity before asking them for contract markers.
    let mut command = marker_command(binary, option, rustix::process::geteuid().is_root());
    let (out, _) = run(&mut command, 5).map_err(|_| error)?;
    if String::from_utf8_lossy(&out).trim() != expected || digest(binary)? != before {
        return Err(error.into());
    }
    Ok(())
}
pub fn marker_command(binary: &Path, option: &str, privileged: bool) -> Command {
    if privileged {
        let mut command = Command::new("/usr/sbin/runuser");
        command
            .env_clear()
            .args([
                "-u",
                "nobody",
                "--",
                "/usr/bin/setpriv",
                "--no-new-privs",
                "/usr/bin/env",
                "-i",
                "PATH=/usr/bin:/bin",
            ])
            .arg(binary)
            .arg(option);
        command
    } else {
        let mut command = Command::new(binary);
        command.arg(option);
        command
    }
}
#[derive(Default)]
pub struct Options(pub BTreeMap<String, Vec<String>>);
impl Options {
    pub fn parse(args: &[String], allowed: &[&str]) -> Result<Self> {
        let mut map = BTreeMap::<String, Vec<String>>::new();
        for pair in args.chunks(2) {
            if pair.len() != 2 || !pair[0].starts_with("--") || !allowed.contains(&pair[0].as_str())
            {
                return Err("INVALID_BUILD_ARGUMENT".into());
            }
            map.entry(pair[0].clone())
                .or_default()
                .push(pair[1].clone());
        }
        if map
            .iter()
            .any(|(k, v)| v.len() > 1 && k != "--replace-source")
        {
            return Err("DUPLICATE_BUILD_ARGUMENT".into());
        }
        Ok(Self(map))
    }
    pub fn get(&self, key: &str) -> Option<&str> {
        self.0.get(key).and_then(|v| v.first()).map(String::as_str)
    }
    pub fn required(&self, key: &str) -> Result<&str> {
        self.get(key)
            .ok_or_else(|| format!("REQUIRED_ARGUMENT:{key}"))
    }
    pub fn path(&self, key: &str) -> Result<PathBuf> {
        Ok(self.required(key)?.into())
    }
}
pub const BINARIES: &[(&str, &str)] = &[
    ("clinx-runtime", "--runtime-binary"),
    ("clinx-mcp-relay", "--mcp-relay-binary"),
    ("clinx-native-transport", "--native-transport-binary"),
    ("clinx-provider", "--provider-binary"),
    ("clinx-mcp", "--mcp-binary"),
    ("clinx-node", "--node-binary"),
    ("clinx-node-daemon", "--node-daemon-binary"),
    ("clinx-observer", "--observer-binary"),
    ("clinx-device", "--device-binary"),
];
pub fn native_data(source: &Path) -> Result<BTreeMap<String, PathBuf>> {
    let m = value(&source.join("architecture/python-sources.json"))?;
    if m["schema"] != 1 {
        return Err("UNKNOWN_SOURCE_MANIFEST_SCHEMA".into());
    }
    let mut declared = BTreeSet::new();
    for r in m["modules"].as_array().ok_or("INVALID_SOURCE_MANIFEST")? {
        let name = r["path"].as_str().ok_or("INVALID_SOURCE_PATH")?;
        let path = relative(name)?;
        if path.extension().is_none_or(|p| p != "py") {
            return Err("INVALID_SOURCE_PATH".into());
        }
        if !declared.insert(name) {
            return Err("DUPLICATE_SOURCE_MODULE".into());
        }
        let role = r["role"].as_str().ok_or("UNKNOWN_SOURCE_ROLE")?;
        if ![
            "ACTIVE_RUNTIME",
            "COMPAT",
            "DEVOPS",
            "QUALIFICATION",
            "DEAD",
        ]
        .contains(&role)
        {
            return Err("UNKNOWN_SOURCE_ROLE".into());
        }
        let profiles = r["profiles"].as_array().ok_or("UNKNOWN_SOURCE_PROFILE")?;
        if profiles.iter().any(|p| p != "rust" && p != "monitor") {
            return Err("UNKNOWN_SOURCE_PROFILE".into());
        }
        if !profiles.is_empty()
            && (!name.starts_with("clinx/") || !["ACTIVE_RUNTIME", "COMPAT"].contains(&role))
        {
            return Err(format!("NON_RUNTIME_SOURCE_IN_PACKAGE:{name}"));
        }
        if !profiles.is_empty()
            && (name.starts_with("clinx/compat/")
                || (profiles.contains(&json!("rust")) && role == "COMPAT"))
        {
            return Err(format!("LEGACY_CORE_IN_RUNTIME_PACKAGE:{name}"));
        }
        if profiles.contains(&json!("rust")) {
            return Err("PYTHON_FREE_RUST_RELEASE_REQUIRED".into());
        }
    }
    let rows = value(&source.join("architecture/runtime-data.json"))?;
    let mut result = BTreeMap::new();
    let mut names = BTreeSet::new();
    for r in rows.as_array().ok_or("INVALID_DATA_MANIFEST")? {
        let name = r["path"].as_str().ok_or("INVALID_SOURCE_PATH")?;
        let p = relative(name)?;
        if p.extension().is_none_or(|v| v != "json") || !names.insert(name) {
            return Err("INVALID_RUNTIME_DATA".into());
        }
        let profiles = r["profiles"].as_array().ok_or("UNKNOWN_SOURCE_PROFILE")?;
        if profiles.iter().any(|p| p != "rust" && p != "monitor") {
            return Err("UNKNOWN_SOURCE_PROFILE".into());
        }
        if profiles.contains(&json!("rust")) {
            regular(&source.join(&p))?;
            result.insert(format!("control/{name}"), source.join(p));
        }
    }
    Ok(result)
}
pub fn stage(o: &Options, release_root: &Path, owner: u32, group: u32) -> Result<Value> {
    if rustix::process::geteuid().is_root() {
        return Err("ROOT_STAGING_REQUIRES_CLINX_ADMIN".into());
    }
    let (head, _) = run(
        Command::new("git")
            .arg("-C")
            .arg(o.required("--source")?)
            .args(["rev-parse", "HEAD"]),
        5,
    )?;
    stage_at_revision(o, release_root, owner, group, &head)
}
/// Root callers supply a revision read as the operator and an immutable
/// metadata snapshot. Never read an operator's Git configuration as root.
pub fn stage_at_revision(
    o: &Options,
    release_root: &Path,
    owner: u32,
    group: u32,
    head: &[u8],
) -> Result<Value> {
    let revision = String::from_utf8_lossy(head);
    if revision.trim().len() != 40 || !revision.trim().bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err("INVALID_SOURCE_REVISION".into());
    }
    let source = fs::canonicalize(o.required("--source")?).map_err(|e| e.to_string())?;
    let base =
        fs::canonicalize(o.required("--desktop-base-release")?).map_err(|e| e.to_string())?;
    if base.parent() != Some(release_root) {
        return Err("INSTALLED_BASE_RELEASE_REQUIRED".into());
    }
    let m = fs::metadata(release_root).map_err(|e| e.to_string())?;
    if m.uid() != owner || m.gid() != group || m.mode() & 0o002 != 0 {
        return Err("ROOT_CONTROLLED_RELEASE_DIRECTORY_REQUIRED".into());
    }
    let manifest = value(&base.join("manifest.json"))?;
    let previous = manifest["files"]
        .as_object()
        .ok_or("INVALID_RELEASE_MANIFEST")?;
    for (name, hash) in previous {
        let path = base.join(relative(name)?);
        let m = regular(&path)?;
        if m.uid() != owner
            || m.mode() & 0o022 != 0
            || digest(&path)? != hash.as_str().ok_or("INVALID_DIGEST")?
        {
            return Err(format!("BASE_RELEASE_CONTENT_MISMATCH:{name}"));
        }
    }
    let data = native_data(&source)?;
    for name in o.0.get("--replace-source").into_iter().flatten() {
        if !data.contains_key(&format!("control/{name}")) {
            return Err("DECLARED_RUNTIME_SOURCE_REQUIRED".into());
        }
    }
    let mut paths = data.clone();
    if !previous.contains_key("host-worker") {
        return Err("HOST_WORKER_REQUIRED".into());
    }
    paths.insert("host-worker".into(), base.join("host-worker"));
    for (name, key) in BINARIES {
        let p = fs::canonicalize(o.required(key)?).map_err(|e| e.to_string())?;
        if regular(&p)?.mode() & 0o111 == 0 {
            return Err("RUNTIME_EXECUTABLE_REQUIRED".into());
        }
        let checks: Vec<(&str, &str, &str)> = match *name {
            "clinx-node" => vec![
                (
                    "--network-contract",
                    "RUST_NODE_NETWORK_V1",
                    "QUALIFIED_RUST_NODE_NETWORK_REQUIRED",
                ),
                (
                    "--config-compile-contract",
                    "RUST_CONFIG_COMPILER_V1",
                    "QUALIFIED_RUST_CONFIG_COMPILER_REQUIRED",
                ),
                (
                    "--observation-contract",
                    "RUST_OBSERVATION_V1",
                    "QUALIFIED_RUST_OBSERVATION_REQUIRED",
                ),
                (
                    "--collector-contract",
                    "RUST_COLLECTOR_V1",
                    "QUALIFIED_RUST_COLLECTOR_REQUIRED",
                ),
                (
                    "--client-runtime-contract",
                    "RUST_CLIENT_RUNTIME_V1",
                    "QUALIFIED_RUST_CLIENT_RUNTIME_REQUIRED",
                ),
                (
                    "--thin-runtime-contract",
                    "RUST_THIN_RUNTIME_V1",
                    "QUALIFIED_RUST_THIN_RUNTIME_REQUIRED",
                ),
                (
                    "--device-runtime-contract",
                    "RUST_DEVICE_RUNTIME_V1",
                    "QUALIFIED_RUST_DEVICE_RUNTIME_REQUIRED",
                ),
            ],
            "clinx-node-daemon" => vec![(
                "--daemon-contract",
                "RUST_NODE_DAEMON_V1",
                "QUALIFIED_RUST_DEVICE_RUNTIME_REQUIRED",
            )],
            "clinx-provider" => vec![(
                "--desktop-socket-contract",
                "RUST_DESKTOP_SOCKET_V1",
                "QUALIFIED_NATIVE_ENTRYPOINT_REQUIRED:clinx-provider",
            )],
            "clinx-mcp" => vec![(
                "--observation-runtime-contract",
                "RUST_OBSERVATION_DIRECTORY_V1",
                "QUALIFIED_NATIVE_ENTRYPOINT_REQUIRED:clinx-mcp",
            )],
            "clinx-observer" => vec![(
                "--observer-contract",
                "RUST_OBSERVER_V1",
                "QUALIFIED_NATIVE_ENTRYPOINT_REQUIRED:clinx-observer",
            )],
            "clinx-device" => vec![(
                "--device-cli-contract",
                "RUST_DEVICE_CLI_V1",
                "QUALIFIED_NATIVE_ENTRYPOINT_REQUIRED:clinx-device",
            )],
            _ => vec![],
        };
        for (option, expected, error) in checks {
            marker(&p, option, expected, error)?
        }
        paths.insert((*name).into(), p);
    }
    let files: BTreeMap<_, _> = paths
        .iter()
        .map(|(n, p)| Ok((n.clone(), digest(p)?)))
        .collect::<Result<_>>()?;
    let version = format!("rust-{}", &sha(default_json(&json!(files)))[..20]);
    let release = release_root.join(&version);
    fs::set_permissions(release_root, fs::Permissions::from_mode(0o755))
        .map_err(|e| e.to_string())?;
    if release.exists() {
        return Err("RELEASE_ALREADY_EXISTS".into());
    }
    let pending = tempfile::Builder::new()
        .prefix(".stage-")
        .permissions(fs::Permissions::from_mode(0o755))
        .tempdir_in(release_root)
        .map_err(|e| e.to_string())?;
    for (name, path) in &paths {
        let bytes = read(path)?;
        let target = pending.path().join(name);
        write(
            &target,
            &bytes,
            if regular(path)?.mode() & 0o111 != 0 {
                0o755
            } else {
                0o644
            },
        )?;
        if digest(&target)? != files[name] {
            return Err(format!("STAGED_CONTENT_MISMATCH:{name}"));
        }
    }
    if owner == 0 {
        let g = nix::unistd::Group::from_name("clinx-transport")
            .map_err(|e| e.to_string())?
            .ok_or("TRANSPORT_GROUP_REQUIRED")?;
        std::os::unix::fs::chown(
            pending.path().join("host-worker"),
            Some(0),
            Some(g.gid.as_raw()),
        )
        .map_err(|e| e.to_string())?;
        fs::set_permissions(
            pending.path().join("host-worker"),
            fs::Permissions::from_mode(0o4750),
        )
        .map_err(|e| e.to_string())?;
    }
    let replacements: BTreeMap<_, _> = data.keys().map(|n| (n.clone(), files[n].clone())).collect();
    let retired: Vec<_> = previous
        .keys()
        .filter(|n| !files.contains_key(*n))
        .cloned()
        .collect();
    let result = json!({"version":version,"source_cwd":source,"head":String::from_utf8_lossy(head).trim(),"base_release":base,"execution_daemon":"EXISTING_DESKTOP_ONLY","native_transport":"RUST_NATIVE_V1","host_transport":"RUST_HOST_V1","mcp_runtime":"RUST_MCP_V1","node_api":"RUST_NODE_API_V1","node_network":"RUST_NODE_NETWORK_V1","observation_runtime":"RUST_OBSERVATION_V1","collector_runtime":"RUST_COLLECTOR_V1","client_runtime":"RUST_CLIENT_RUNTIME_V1","thin_runtime":"RUST_THIN_RUNTIME_V1","device_runtime":"RUST_DEVICE_RUNTIME_V1","node_daemon":"RUST_NODE_DAEMON_V1","provider_exchange":"RUST_PROVIDER_V1","workspace_observer":"RUST_PROVIDER_V1","observation_directory":"RUST_OBSERVATION_DIRECTORY_V1","observer_runtime":"RUST_OBSERVER_V1","device_cli":"RUST_DEVICE_CLI_V1","desktop_socket_maintenance":"RUST_DESKTOP_SOCKET_V1","config_compiler":"RUST_CONFIG_COMPILER_V1","python_sources":[],"python_dependencies":[],"source_replacements":replacements,"runtime_source_profile":"rust","source_layout":"qualified-clinx-v1","retired_files":retired,"files":files,"status":"STAGED_NOT_SELECTED"});
    save(&pending.path().join("manifest.json"), &result)?;
    fs::rename(pending.path(), &release).map_err(|e| e.to_string())?;
    let mut receipt = result;
    receipt["release"] = json!(release);
    save(&o.path("--output")?, &receipt)?;
    Ok(receipt)
}
pub fn cli(command: &str, args: &[String]) -> Result<Value> {
    if args == ["--help"] {
        return Ok(json!({"command":command,"usage":match command {
            "stage-rust-release"=>"--desktop-base-release PATH --source CHECKOUT --output RECEIPT --runtime-binary PATH --mcp-relay-binary PATH --native-transport-binary PATH --provider-binary PATH --mcp-binary PATH --node-binary PATH --node-daemon-binary PATH --observer-binary PATH --device-binary PATH [--replace-source DECLARED_DATA]",
            "build-transport-runtime"=>"--source-archive PATH --go-archive PATH --output NEW_ARTIFACT_DIRECTORY",
            "build-tunnel-release"=>"--output NEW_ARTIFACT_DIRECTORY --guard-manifest PATH --evidence-manifest PATH [--source CHECKOUT] [--home HOME] [--tunnel PATH] [--supervisor-binary PATH] [--cas-binary PATH] [--transport-runtime-manifest PATH] [--python LEGACY_GUARD_IDENTITY]",
            "tunnel-cas"=>"check|fixture-apply --capsule PATH --root PATH --direction forward|rollback [--guard-root PATH]",
            _=>return Err("UNKNOWN_BUILD_COMMAND".into())
        },"deployment":"NOT_PERFORMED_BY_BUILDERS","cas_write_scope":"MARKED_ARTIFACT_FIXTURES_ONLY"}));
    }
    match command {
        "stage-rust-release" => {
            let mut allowed = vec![
                "--source",
                "--desktop-base-release",
                "--output",
                "--replace-source",
            ];
            allowed.extend(BINARIES.iter().map(|(_, k)| *k));
            let mut o = Options::parse(args, &allowed)?;
            o.0.entry("--source".into()).or_insert_with(|| {
                vec![
                    Path::new(env!("CARGO_MANIFEST_DIR"))
                        .parent()
                        .unwrap()
                        .display()
                        .to_string(),
                ]
            });
            if !rustix::process::geteuid().is_root() {
                return Err("ADMINISTRATOR_REQUIRED".into());
            }
            stage(&o, Path::new("/var/lib/clinx-rust/releases"), 0, 0)
        }
        "tunnel-cas" => cas::cli(args),
        "build-tunnel-release" => tunnel::cli(args),
        "build-transport-runtime" => transport::cli(args),
        _ => Err("UNKNOWN_BUILD_COMMAND".into()),
    }
}
