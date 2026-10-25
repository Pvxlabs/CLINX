#[path = "../src/install_tools.rs"]
mod install;
use install::*;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    os::unix::fs::{PermissionsExt, symlink},
    path::PathBuf,
};
struct Fixture {
    temp: tempfile::TempDir,
    layout: Layout,
    release: PathBuf,
    manifest: Value,
}
impl Fixture {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let p = temp.path();
        let layout = Layout {
            root: p.join("root"),
            systemd: p.join("systemd"),
            tmpfiles: p.join("tmpfiles"),
            run: p.join("run/clinx-rust"),
        };
        for d in [
            &layout.root,
            &layout.systemd,
            &layout.tmpfiles,
            &p.join("run"),
        ] {
            fs::create_dir_all(d).unwrap();
        }
        let release = p.join("candidate");
        fs::create_dir(&release).unwrap();
        let mut manifest =
            json!({"version":"candidate","files":{},"python_sources":[],"python_dependencies":[]});
        for (k, v) in [
            ("config_compiler", "RUST_CONFIG_COMPILER_V1"),
            ("source_layout", "qualified-clinx-v1"),
            ("provider_exchange", "RUST_PROVIDER_V1"),
            ("observation_runtime", "RUST_OBSERVATION_V1"),
            ("client_runtime", "RUST_CLIENT_RUNTIME_V1"),
            ("device_runtime", "RUST_DEVICE_RUNTIME_V1"),
            ("node_daemon", "RUST_NODE_DAEMON_V1"),
            ("thin_runtime", "RUST_THIN_RUNTIME_V1"),
            ("collector_runtime", "RUST_COLLECTOR_V1"),
            ("node_network", "RUST_NODE_NETWORK_V1"),
            ("node_api", "RUST_NODE_API_V1"),
            ("mcp_runtime", "RUST_MCP_V1"),
            ("host_transport", "RUST_HOST_V1"),
            ("workspace_observer", "RUST_PROVIDER_V1"),
            ("desktop_socket_maintenance", "RUST_DESKTOP_SOCKET_V1"),
            ("observation_directory", "RUST_OBSERVATION_DIRECTORY_V1"),
            ("observer_runtime", "RUST_OBSERVER_V1"),
            ("device_cli", "RUST_DEVICE_CLI_V1"),
        ] {
            manifest[k] = json!(v);
        }
        for name in [
            "clinx-node",
            "clinx-provider",
            "clinx-mcp",
            "clinx-node-daemon",
            "clinx-observer",
            "clinx-device",
            "clinx-runtime",
            "clinx-native-transport",
            "host-worker",
        ] {
            fs::write(release.join(name), name).unwrap();
            fs::set_permissions(release.join(name), fs::Permissions::from_mode(0o644)).unwrap();
            manifest["files"][name] = json!(hash(name.as_bytes()));
        }
        fs::write(release.join("manifest.json"), manifest.to_string()).unwrap();
        Self {
            temp,
            layout,
            release,
            manifest,
        }
    }
    fn save(&self) {
        fs::write(
            self.release.join("manifest.json"),
            self.manifest.to_string(),
        )
        .unwrap();
    }
    fn services(&self) -> Services {
        let home = self.temp.path().join("home");
        fs::create_dir_all(home.join("dev")).unwrap();
        let socket = self.temp.path().join("desktop.sock");
        fs::write(
            &socket,
            b"private socket witness; never connected by fixture",
        )
        .unwrap();
        let cfg = self.temp.path().join("input.toml");
        fs::write(&cfg, CONFIG).unwrap();
        Services {
            release: self.release.clone(),
            operator_home: home,
            bridge_config: cfg,
            desktop_socket: socket,
            desktop_thread_id: "thread-exact".into(),
        }
    }
    fn callers(&self) -> Callers {
        fs::create_dir_all(self.layout.root.join("config")).unwrap();
        fs::create_dir_all(self.layout.root.join("api")).unwrap();
        fs::write(self.layout.root.join("config/worker-user"), "worker\n").unwrap();
        fs::write(self.layout.root.join("config/bridge.toml"), CONFIG).unwrap();
        let legacy = self.temp.path().join("legacy");
        fs::create_dir(&legacy).unwrap();
        for name in [
            "identity.json",
            "peers.json",
            "sharing.json",
            "registry.sqlite3",
        ] {
            fs::write(legacy.join(name), format!("original-{name}")).unwrap();
        }
        Callers {
            release: self.release.clone(),
            legacy_node_state: legacy,
            operator: "operator".into(),
            output: self.temp.path().join("receipt.json"),
        }
    }
}
const CONFIG: &str = r#"[app_server]
local_socket="/old/provider.sock"
native_socket="/old/native.sock"
[runtime]
log_dir="/old/log"
task_db_path="/DO_NOT_OPEN/old.sqlite3"
[projects.example]
cwd="/repo/Unicode-中"
[workspaces.home]
host="p620"
root="/projects"
allow_existing_projects=true
[host.operations.echo]
command=["/bin/echo","unchanged"]
"#;
struct Fake {
    commands: Vec<Vec<String>>,
    failure: Option<String>,
}
impl Fake {
    fn new() -> Self {
        Self {
            commands: vec![],
            failure: None,
        }
    }
}
fn uid() -> u32 {
    nix::unistd::geteuid().as_raw()
}
impl System for Fake {
    fn groups(&mut self, _: &str, gid: u32) -> Result<Vec<u32>> {
        Ok(vec![gid])
    }
    fn non_login(&mut self, _: &str) -> Result<bool> {
        Ok(true)
    }
    fn account(&mut self, name: &str) -> Result<Account> {
        Ok(Account {
            name: name.into(),
            uid: uid(),
            gid: nix::unistd::getegid().as_raw(),
        })
    }
    fn account_uid(&mut self, _: u32) -> Result<Account> {
        Ok(Account {
            name: "worker".into(),
            uid: uid().max(1),
            gid: nix::unistd::getegid().as_raw(),
        })
    }
    fn group(&mut self, _: &str) -> Result<u32> {
        Ok(nix::unistd::getegid().as_raw())
    }
    fn run(&mut self, args: &[String]) -> Result<String> {
        self.commands.push(args.to_vec());
        if self
            .failure
            .as_ref()
            .is_some_and(|f| args.iter().any(|a| a == f))
        {
            return Err("FIXTURE_COMMAND_FAILURE".into());
        }
        Ok("{\"compiled\":true}".into())
    }
}
#[test]
fn historical_units_preserve_identity_paths_and_privileges_without_python_environment() {
    let expected: Value = serde_json::from_str(include_str!(
        "../../fixtures/install_historical_expected.json"
    ))
    .unwrap();
    let vars = BTreeMap::from([
        ("ROOT", "/var/lib/clinx-rust".into()),
        ("RUN", "/run/clinx-rust".into()),
        ("RELEASE", "/release".into()),
        ("DESKTOP_PARENT", "/desktop".into()),
        ("WORKER_UID", "1200".into()),
        ("WORKER_NAME", "worker".into()),
        ("AUTHORITY_UID", "1201".into()),
        ("API_UID", "1202".into()),
        ("TRANSPORT_UID", "1203".into()),
        ("OPERATOR_GID", "1204".into()),
        ("OPERATOR", "operator".into()),
    ]);
    for (callers, file) in [
        (false, "install_rust_services.py"),
        (true, "stage_rust_callers.py"),
    ] {
        let actual = render_units(&vars, callers).unwrap();
        assert_eq!(
            actual.len(),
            expected[file]["units"].as_object().unwrap().len()
        );
        for (name, v) in expected[file]["units"].as_object().unwrap() {
            let original = v
                .as_str()
                .unwrap()
                .split('\n')
                .filter(|l| {
                    !l.starts_with("Environment=PYTHONPATH=")
                        && !l.starts_with("Environment=PYTHONDONTWRITEBYTECODE=")
                })
                .collect::<Vec<_>>()
                .join("\n");
            assert_eq!(actual[name], original, "{name}");
            assert!(!actual[name].contains("python"));
        }
    }
    let mut invalid = vars;
    invalid.insert("RELEASE", "/release\nExecStart=/evil".into());
    assert_eq!(
        render_units(&invalid, false).unwrap_err(),
        "UNIT_VALUE_INVALID"
    );
}
#[test]
fn release_requires_every_contract_and_immutable_file_before_install() {
    let mut f = Fixture::new();
    assert!(validate_release(&f.release, uid(), false).is_ok());
    let original = f.manifest.clone();
    for k in original
        .as_object()
        .unwrap()
        .keys()
        .filter(|k| k.as_str() != "files" && k.as_str() != "version")
    {
        f.manifest = original.clone();
        f.manifest[k] = json!("wrong");
        f.save();
        assert!(validate_release(&f.release, uid(), false).is_err(), "{k}");
    }
    f.manifest = original;
    f.save();
    fs::write(f.release.join("clinx-node"), "drift").unwrap();
    assert_eq!(
        validate_release(&f.release, uid(), false).unwrap_err(),
        "RELEASE_CONTENT_MISMATCH:clinx-node"
    );
    fs::write(f.release.join("clinx-node"), "clinx-node").unwrap();
    fs::set_permissions(
        f.release.join("clinx-node"),
        fs::Permissions::from_mode(0o666),
    )
    .unwrap();
    assert!(validate_release(&f.release, uid(), true).is_err());
    fs::remove_file(f.release.join("clinx-node")).unwrap();
    symlink("clinx-provider", f.release.join("clinx-node")).unwrap();
    assert!(validate_release(&f.release, uid(), true).is_err());
    f.manifest["files"]["../outside"] = json!("bad");
    f.save();
    assert_eq!(
        validate_release(&f.release, uid(), false).unwrap_err(),
        "RELEASE_PATH_INVALID"
    );
}
#[test]
fn configuration_retains_routes_and_host_commands_but_removes_legacy_state() {
    let l = Layout::default();
    let (cfg, p) = configuration(CONFIG, &l).unwrap();
    let c: toml::Value = toml::from_str(&cfg).unwrap();
    assert_eq!(
        c["app_server"]["local_socket"].as_str(),
        Some("/run/clinx-rust-provider/provider.sock")
    );
    assert!(c["app_server"].get("native_socket").is_none());
    assert!(c["runtime"].get("task_db_path").is_none());
    assert_eq!(
        c["host"]["operations"]["echo"]["command"]
            .as_array()
            .unwrap()[1]
            .as_str(),
        Some("unchanged")
    );
    assert_eq!(
        p,
        json!({"projects":[{"alias":"example","host":"p620","cwd":"/repo/Unicode-中"}],"workspaces":[{"host":"p620","root":"/projects","allow_existing_projects":true}],"endpoints":["/run/clinx-rust-provider/provider.sock"],"lease_ms":120000,"host_operations":[{"capability":"LOCAL_HOST_PROCESS","operation":"user_identity","operation_class":"READ_ONLY_HOST","target":""}]})
    );
}
#[test]
fn native_installer_runs_exact_compilers_and_only_reloads_not_starts() {
    let f = Fixture::new();
    let args = f.services();
    let mut sys = Fake::new();
    let r = install_services(&f.layout, &args, uid(), &mut sys).unwrap();
    assert_eq!(r["status"], "INSTALLED_NOT_STARTED");
    assert!(
        r["init_command"]
            .as_str()
            .unwrap()
            .ends_with(" --init-only")
    );
    assert_eq!(sys.commands.len(), 6);
    assert_eq!(&sys.commands[0][..4], &["runuser", "-u", "worker", "--"]);
    assert_eq!(
        &sys.commands[0][7..],
        &[
            args.desktop_socket.display().to_string(),
            "--witness-thread-id".into(),
            "thread-exact".into(),
            "--apply".into()
        ]
    );
    assert_eq!(&sys.commands[1][1..3], &["--compile-config", "host"]);
    assert_eq!(&sys.commands[2][1..3], &["--compile-config", "mcp"]);
    assert_eq!(sys.commands[5], vec!["systemctl", "daemon-reload"]);
    assert!(!f.layout.root.join("authority").exists());
    assert!(
        !fs::read_to_string(f.layout.root.join("config/bridge.toml"))
            .unwrap()
            .contains("task_db_path")
    );
    let n = sys.commands.len();
    assert!(
        install_services(&f.layout, &args, uid(), &mut sys)
            .unwrap_err()
            .starts_with("SERVICE_ALREADY_EXISTS")
    );
    assert_eq!(sys.commands.len(), n);
}
#[test]
fn callers_copy_exact_identity_without_database_or_service_activation() {
    let f = Fixture::new();
    let args = f.callers();
    let mut sys = Fake::new();
    let r = stage_callers(&f.layout, &args, uid(), &mut sys).unwrap();
    assert_eq!(r["status"], "STAGED_NOT_SELECTED");
    assert_eq!(r["centre_port"], 18774);
    assert_eq!(r["legacy_databases_imported"], false);
    for name in ["identity.json", "peers.json", "sharing.json"] {
        assert_eq!(
            fs::read(args.legacy_node_state.join(name)).unwrap(),
            fs::read(f.layout.root.join("api/node-centre").join(name)).unwrap()
        );
    }
    assert!(
        !f.layout
            .root
            .join("api/node-centre/registry.sqlite3")
            .exists()
    );
    assert_eq!(sys.commands.len(), 2);
    assert_eq!(sys.commands[1], vec!["systemctl", "daemon-reload"]);
    assert!(
        !fs::read_to_string(f.layout.systemd.join("clinx-rust-mcp@.service"))
            .unwrap()
            .contains("PYTHON")
    );
    assert!(
        stage_callers(&f.layout, &args, uid(), &mut sys)
            .unwrap_err()
            .starts_with("CALLER_UNIT_EXISTS")
    );
}
#[test]
fn compiled_config_conflict_and_compiler_failure_never_write_units() {
    let f = Fixture::new();
    let args = f.callers();
    fs::write(
        f.layout.root.join("config/mcp-runtime.json"),
        "{\"different\":true}",
    )
    .unwrap();
    let mut sys = Fake::new();
    assert_eq!(
        stage_callers(&f.layout, &args, uid(), &mut sys).unwrap_err(),
        "MCP_CONFIG_IDENTITY_MISMATCH"
    );
    assert_eq!(fs::read_dir(&f.layout.systemd).unwrap().count(), 0);
    assert!(!f.layout.root.join("api/node-centre").exists());
    let f = Fixture::new();
    let args = f.services();
    let mut sys = Fake::new();
    sys.failure = Some("--compile-config".into());
    assert_eq!(
        install_services(&f.layout, &args, uid(), &mut sys).unwrap_err(),
        "FIXTURE_COMMAND_FAILURE"
    );
    assert_eq!(fs::read_dir(&f.layout.systemd).unwrap().count(), 0);
    assert!(
        !sys.commands
            .iter()
            .any(|c| c.first().is_some_and(|s| s == "systemctl"))
    );
}
#[test]
fn cli_help_is_root_and_config_free() {
    let r = std::process::Command::new(env!("CARGO_BIN_EXE_clinx-install"))
        .arg("--help")
        .env_clear()
        .output()
        .unwrap();
    assert!(r.status.success());
    assert!(
        String::from_utf8(r.stdout)
            .unwrap()
            .contains("never starts services")
    );
}

#[test]
fn provisioning_preserves_existing_state_and_has_no_lifecycle_operation() {
    let f = Fixture::new();
    fs::create_dir(f.layout.root.join("authority")).unwrap();
    fs::set_permissions(
        f.layout.root.join("authority"),
        fs::Permissions::from_mode(0o700),
    )
    .unwrap();
    let sentinel = f.layout.root.join("authority/do-not-open.sqlite3");
    fs::write(&sentinel, b"unrelated runtime bytes").unwrap();
    let mut sys = Fake::new();
    let r = provision(&f.layout, &mut sys, uid()).unwrap();
    assert_eq!(r["scope"], "PRINCIPAL_PROVISIONING_ONLY");
    assert_eq!(r["production_authority"], "NOT_VERIFIED");
    assert_eq!(fs::read(sentinel).unwrap(), b"unrelated runtime bytes");
    assert_eq!(sys.commands.len(), 5);
    assert!(sys.commands.iter().all(|c| c[0] == "/usr/sbin/usermod"));
    assert!(
        sys.commands
            .iter()
            .all(|c| !c.iter().any(|s| s == "clinx-worker"))
    );
    fs::set_permissions(
        f.layout.root.join("authority"),
        fs::Permissions::from_mode(0o755),
    )
    .unwrap();
    assert_eq!(
        provision(&f.layout, &mut sys, uid()).unwrap_err(),
        "PRIVATE_AUTHORITY_DIRECTORY_REQUIRED"
    );
    assert_eq!(
        fs::metadata(f.layout.root.join("authority"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o755
    );
}
