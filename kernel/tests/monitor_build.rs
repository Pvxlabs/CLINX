//! Actual build CLI against disposable App resources; no SDK/signing implied.
use serde_json::{Value, json};
use std::{
    fs,
    os::unix::fs::{PermissionsExt, symlink},
    path::{Path, PathBuf},
    process::{Command, Output},
};

struct Fixture {
    temp: tempfile::TempDir,
    source: PathBuf,
    resources: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let source = temp.path().join("source");
        let resources = temp.path().join("App/Contents/Resources");
        fs::create_dir_all(source.join("architecture")).unwrap();
        fs::write(
            source.join("architecture/native-runtime.json"),
            include_str!("../../architecture/native-runtime.json"),
        )
        .unwrap();
        Self {
            temp,
            source,
            resources,
        }
    }
    fn file(&self, name: &str, content: &[u8]) -> PathBuf {
        let p = self.resources.join(name);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(&p, content).unwrap();
        p
    }
    fn run(&self) -> Output {
        Command::new(env!("CARGO_BIN_EXE_clinx-build"))
            .arg("native-monitor-resources")
            .arg(&self.source)
            .arg(&self.resources)
            .env("PATH", "/nonexistent")
            .output()
            .unwrap()
    }
    fn clean(&self) -> Value {
        let result = self.run();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        serde_json::from_slice(&result.stdout).unwrap()
    }
    fn manifest(&self) -> Value {
        serde_json::from_slice(&fs::read(self.resources.join("python-sources.json")).unwrap())
            .unwrap()
    }
}

#[test]
fn current_app_closure_removes_retired_python_and_keeps_swift_assets() {
    let f = Fixture::new();
    let names = [
        "bridge.py",
        ".py",
        ".hidden.py",
        "clinx/compat/task_registry.py",
        "Discovery/bridge.py",
        "local_discovery/identity.py",
        "domain/event.py",
        "shadow_ledger/log.py",
        "runtime_control/owner.py",
        "provider_adapters/provider.py",
        "DiscoveryRuntime/bin/python",
    ];
    for name in names {
        f.file(name, b"retired");
    }
    f.file("AppIcon.icns", b"Swift icon");
    f.file("ThirdPartyNotices.txt", b"licenses");
    f.file("Swift.bundle/resource.json", b"asset");
    f.file("UPPER.PY", b"unrelated");
    fs::set_permissions(
        f.resources.join("DiscoveryRuntime/bin"),
        fs::Permissions::from_mode(0o500),
    )
    .unwrap();
    fs::set_permissions(
        f.resources.join("DiscoveryRuntime"),
        fs::Permissions::from_mode(0o500),
    )
    .unwrap();
    let result = f.clean();
    assert_eq!(result["contract"], "RUST_MONITOR_BUILD_V1");
    for name in names {
        assert!(!f.resources.join(name).exists(), "retired: {name}");
    }
    for (name, expected) in [
        ("AppIcon.icns", b"Swift icon".as_slice()),
        ("ThirdPartyNotices.txt", b"licenses"),
        ("Swift.bundle/resource.json", b"asset"),
        ("UPPER.PY", b"unrelated"),
    ] {
        assert_eq!(fs::read(f.resources.join(name)).unwrap(), expected);
    }
    let declared: Value =
        serde_json::from_str(include_str!("../../architecture/native-runtime.json")).unwrap();
    assert_eq!(
        f.manifest(),
        json!({"schema":1,"profile":"monitor-native","files":{},"device_runtime":declared["device_runtime"],"required_helpers":declared["monitor_helpers"]})
    );
    assert_eq!(result["manifest"], f.manifest());
    assert_eq!(result["removed"].as_array().unwrap().len(), 11);
    assert_eq!(
        fs::metadata(f.resources.join("python-sources.json"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o644
    );
    assert_eq!(f.clean()["removed"], json!([]));
}

#[test]
fn links_are_removed_without_mutating_external_targets_or_swift_links() {
    let f = Fixture::new();
    fs::create_dir_all(&f.resources).unwrap();
    let external = f.temp.path().join("external");
    fs::create_dir(&external).unwrap();
    fs::write(external.join("keep.py"), b"outside").unwrap();
    fs::set_permissions(&external, fs::Permissions::from_mode(0o500)).unwrap();
    for name in ["clinx", "DiscoveryRuntime", "bridge.py"] {
        symlink(&external, f.resources.join(name)).unwrap();
    }
    symlink(&external, f.resources.join("SwiftAssets")).unwrap();
    let target = f.temp.path().join("manifest-target");
    fs::write(&target, b"outside manifest").unwrap();
    symlink(&target, f.resources.join("python-sources.json")).unwrap();
    let result = f.clean();
    assert_eq!(
        result["removed"],
        json!(["DiscoveryRuntime", "bridge.py", "clinx"])
    );
    assert_eq!(fs::read(external.join("keep.py")).unwrap(), b"outside");
    assert_eq!(
        fs::metadata(&external).unwrap().permissions().mode() & 0o777,
        0o500
    );
    assert!(f.resources.join("SwiftAssets").is_symlink());
    assert_eq!(fs::read(&target).unwrap(), b"outside manifest");
    assert!(!f.resources.join("python-sources.json").is_symlink());
    fs::set_permissions(&external, fs::Permissions::from_mode(0o700)).unwrap();
}

#[test]
fn readonly_runtime_nested_symlink_never_changes_external_permissions() {
    let f = Fixture::new();
    f.file("DiscoveryRuntime/lib/nested/module.py", b"old");
    let external = f.temp.path().join("external");
    fs::create_dir(&external).unwrap();
    fs::write(external.join("keep"), b"outside").unwrap();
    fs::set_permissions(&external, fs::Permissions::from_mode(0o500)).unwrap();
    symlink(&external, f.resources.join("DiscoveryRuntime/lib/linked")).unwrap();
    for path in [
        "DiscoveryRuntime/lib/nested",
        "DiscoveryRuntime/lib",
        "DiscoveryRuntime",
    ] {
        fs::set_permissions(f.resources.join(path), fs::Permissions::from_mode(0o500)).unwrap();
    }
    f.clean();
    assert!(!f.resources.join("DiscoveryRuntime").exists());
    assert_eq!(fs::read(external.join("keep")).unwrap(), b"outside");
    assert_eq!(
        fs::metadata(&external).unwrap().permissions().mode() & 0o777,
        0o500
    );
    fs::set_permissions(external, fs::Permissions::from_mode(0o700)).unwrap();
}

#[test]
fn resources_root_symlink_is_rejected_without_deleting_its_target() {
    let f = Fixture::new();
    let external = f.temp.path().join("external");
    fs::create_dir(&external).unwrap();
    fs::write(external.join("bridge.py"), b"outside").unwrap();
    fs::create_dir_all(f.resources.parent().unwrap()).unwrap();
    symlink(&external, &f.resources).unwrap();
    let result = f.run();
    assert!(!result.status.success());
    assert!(
        String::from_utf8_lossy(&result.stderr).contains("MONITOR_RESOURCES_MUST_BE_DIRECTORY")
    );
    assert_eq!(fs::read(external.join("bridge.py")).unwrap(), b"outside");
    assert!(!external.join("python-sources.json").exists());
}

#[test]
fn invalid_or_python_bearing_manifest_fails_before_cleanup() {
    let f = Fixture::new();
    f.file("bridge.py", b"unchanged");
    for raw in ["{broken".into(),json!({"schema":1,"device_runtime":"RUST_DEVICE_RUNTIME_V1","monitor_helpers":["clinx-node"],"monitor_python_sources":["bridge.py"],"monitor_python_dependencies":[]}).to_string()] {
        fs::write(f.source.join("architecture/native-runtime.json"),raw).unwrap();let result=f.run();assert!(!result.status.success());
        assert_eq!(fs::read(f.resources.join("bridge.py")).unwrap(),b"unchanged");assert!(!f.resources.join("python-sources.json").exists());
    }
}

#[test]
fn cargo_target_directory_preserves_explicit_environment_and_spaces() {
    let temp = tempfile::tempdir().unwrap();
    let target = temp.path().join("build products");
    let result = Command::new(env!("CARGO_BIN_EXE_clinx-build"))
        .arg("cargo-target-directory")
        .arg(Path::new(env!("CARGO_MANIFEST_DIR")).join("Cargo.toml"))
        .env("CARGO_TARGET_DIR", &target)
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    assert_eq!(
        String::from_utf8(result.stdout).unwrap(),
        format!("{}\n", target.display())
    );
}
