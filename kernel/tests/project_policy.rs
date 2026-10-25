//! Mutation regressions run only in a disposable source snapshot, never the checkout.
#[path = "../src/source_policy.rs"]
mod policy;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
};
fn snapshot() -> tempfile::TempDir {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let temp = tempfile::Builder::new()
        .prefix("policy-")
        .tempdir_in("/data/artifacts")
        .unwrap();
    let o = Command::new("git")
        .args([
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ])
        .current_dir(repo)
        .output()
        .unwrap();
    assert!(o.status.success());
    for p in o.stdout.split(|b| *b == 0).filter(|b| !b.is_empty()) {
        let p = std::str::from_utf8(p).unwrap();
        let to = temp.path().join(p);
        fs::create_dir_all(to.parent().unwrap()).unwrap();
        fs::copy(repo.join(p), to).unwrap();
    }
    assert!(
        Command::new("git")
            .args(["init", "--quiet"])
            .current_dir(temp.path())
            .status()
            .unwrap()
            .success()
    );
    temp
}
fn rejected(root: &Path, p: &str, bytes: &[u8], expected: &str) {
    let path = root.join(p);
    let old = fs::read(&path).ok();
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(&path, bytes).unwrap();
    let result = policy::project(root);
    if let Some(old) = old {
        fs::write(path, old).unwrap();
    } else {
        fs::remove_file(path).unwrap();
    }
    let error = result.unwrap_err();
    assert!(error.contains(expected), "expected {expected}, got {error}");
}
#[test]
fn project_rejects_document_language_dependency_and_target_drift() {
    let temp = snapshot();
    let root = temp.path();
    assert_eq!(policy::project(root).unwrap()["status"], "PASS");
    let readme = fs::read_to_string(root.join("README.md")).unwrap();
    rejected(
        root,
        "README.md",
        format!("{readme}\n[bad](does-not-exist.md)\n").as_bytes(),
        "BROKEN_DOC_LINK",
    );
    rejected(
        root,
        "README.md",
        format!("{readme}\n[bad](docs/TESTING.md#does-not-exist)\n").as_bytes(),
        "BROKEN_DOC_ANCHOR",
    );
    rejected(
        root,
        "README.md",
        format!("{readme}\n[bad][missing-reference]\n").as_bytes(),
        "UNDEFINED_DOC_REFERENCE",
    );
    rejected(
        root,
        "docs/new.md",
        b"# Missing disposition\n",
        "UNINDEXED_DOCUMENT",
    );
    rejected(
        root,
        "README.md",
        readme.replace("用途：", "purpose:").as_bytes(),
        "DOCUMENT_METADATA_MISSING",
    );
    rejected(
        root,
        "kernel/src/new.rs",
        b"pub fn new() {}\n",
        "UNCLASSIFIED_SOURCE",
    );
    rejected(
        root,
        "kernel/src/new.py",
        b"print('bad')\n",
        "UNCLASSIFIED_SOURCE",
    );
    rejected(
        root,
        "kernel/src/new.swift",
        b"import Foundation\n",
        "UNCLASSIFIED_SOURCE",
    );
    rejected(
        root,
        "pyproject.toml",
        b"[project]\nname='bad'\n",
        "PYTHON_BUILD_DEPENDENCY_FORBIDDEN",
    );
    rejected(
        root,
        ".github/workflows/invalid.yml",
        b"run: python3 install.py\n",
        "PYTHON_CI_DEPENDENCY_FORBIDDEN",
    );
    rejected(
        root,
        "bin/clinx",
        b"#!/usr/bin/env python3\nprint('bad')\n",
        "SCRIPT_LANGUAGE_FORBIDDEN",
    );
    rejected(
        root,
        "runtime/host_worker.c",
        b"int main() {return 0;}\n",
        "LEGACY_LANGUAGE_CHANGE_REQUIRES_APPROVAL",
    );
    let runtime = fs::read_to_string(root.join("kernel/src/native_transport.rs")).unwrap();
    rejected(
        root,
        "kernel/src/native_transport.rs",
        format!("{runtime}\nuse crate::source_policy;\n").as_bytes(),
        "MODULE_DEPENDENCY_DRIFT",
    );
    // Even updating the declaration cannot legalize runtime -> tool.
    let manifest_path = root.join("architecture/source-policy.json");
    let original = fs::read(&manifest_path).unwrap();
    let mut manifest: Value = serde_json::from_slice(&original).unwrap();
    manifest["sources"]["kernel/src/native_transport.rs"]["dependencies"]
        .as_array_mut()
        .unwrap()
        .push(json!("kernel/src/source_policy.rs"));
    fs::write(&manifest_path, manifest.to_string()).unwrap();
    rejected(
        root,
        "kernel/src/native_transport.rs",
        format!("{runtime}\nuse crate::source_policy;\n").as_bytes(),
        "RUNTIME_DEPENDENCY_DIRECTION",
    );
    fs::write(&manifest_path, &original).unwrap();
    let mut manifest: Value = serde_json::from_slice(&original).unwrap();
    manifest["release_binaries"]
        .as_array_mut()
        .unwrap()
        .push(json!("clinx-build"));
    rejected(
        root,
        "architecture/source-policy.json",
        manifest.to_string().as_bytes(),
        "RELEASE_BINARY_LIST_DRIFT",
    );
    let app = fs::read_to_string(root.join("MonitorApp/Scripts/build-app.sh")).unwrap();
    rejected(
        root,
        "MonitorApp/Scripts/build-app.sh",
        app.replace(
            "for helper in clinx-provider clinx-node clinx-node-daemon;",
            "for helper in clinx-provider clinx-node clinx-node-daemon clinx-build;",
        )
        .as_bytes(),
        "MONITOR_HELPER_BUILD_DRIFT",
    );
    let cargo = fs::read_to_string(root.join("kernel/Cargo.toml")).unwrap();
    rejected(
        root,
        "kernel/Cargo.toml",
        format!("{cargo}\npyo3 = '0.23'\n").as_bytes(),
        "PYTHON_DEPENDENCY_FORBIDDEN",
    );
    // Declaring a forbidden language does not bypass language ownership.
    let mut manifest: Value = serde_json::from_slice(&original).unwrap();
    manifest["sources"]["kernel/src/backend.swift"] = json!({"role":"runtime"});
    fs::write(&manifest_path, manifest.to_string()).unwrap();
    rejected(
        root,
        "kernel/src/backend.swift",
        b"import Foundation\n",
        "SWIFT_BACKEND_FORBIDDEN",
    );
    fs::write(&manifest_path, &original).unwrap();
    // A classified Rust target still needs explicit release/development ownership.
    let mut manifest: Value = serde_json::from_slice(&original).unwrap();
    manifest["sources"]["kernel/src/bin/unclassified-target.rs"] =
        json!({"role":"runtime","dependencies":[]});
    fs::write(&manifest_path, manifest.to_string()).unwrap();
    rejected(
        root,
        "kernel/src/bin/unclassified-target.rs",
        b"fn main() {}\n",
        "CARGO_RELEASE_CLASSIFICATION_DRIFT",
    );
    fs::write(&manifest_path, &original).unwrap();
    // Reference syntax, URL escaping and fenced examples remain usable.
    fs::write(root.join("README.md"), format!("{readme}\n[test][test-ref]\n[test-ref]: docs/TEST%49NG.md\n\n```text\n[example](missing-file.md)\n```\n")).unwrap();
    assert!(policy::project(root).is_ok());
    fs::write(root.join("README.md"), readme).unwrap();
}
fn package(root: &Path, dir: &Path) -> Value {
    let manifest: Value =
        serde_json::from_slice(&fs::read(root.join("architecture/source-policy.json")).unwrap())
            .unwrap();
    let mut files = serde_json::Map::new();
    for name in manifest["release_binaries"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(Value::as_str)
        .chain(std::iter::once("host-worker"))
    {
        let bytes = b"\x7fELFpolicy-fixture-not-an-executable";
        fs::write(dir.join(name), bytes).unwrap();
        files.insert(name.into(), json!(format!("{:x}", Sha256::digest(bytes))));
    }
    let data: Value =
        serde_json::from_slice(&fs::read(root.join("architecture/runtime-data.json")).unwrap())
            .unwrap();
    for row in data.as_array().unwrap() {
        if row["profiles"].as_array().unwrap().contains(&json!("rust")) {
            let p = row["path"].as_str().unwrap();
            let dest = format!("control/{p}");
            let bytes = fs::read(root.join(p)).unwrap();
            fs::create_dir_all(dir.join(&dest).parent().unwrap()).unwrap();
            fs::write(dir.join(&dest), &bytes).unwrap();
            files.insert(dest, json!(format!("{:x}", Sha256::digest(&bytes))));
        }
    }
    let m = json!({"python_sources":[],"python_dependencies":[],"files":files});
    fs::write(dir.join("manifest.json"), m.to_string()).unwrap();
    m
}
#[test]
fn artifact_checks_actual_bytes_members_native_format_and_dependencies() {
    let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .to_owned();
    let temp = tempfile::tempdir_in("/data/artifacts").unwrap();
    let root = temp.path();
    let manifest = package(&repo, root);
    assert!(policy::artifact(&repo, root).is_ok());
    fs::write(root.join("sneaked.py"), b"pass").unwrap();
    assert!(
        policy::artifact(&repo, root)
            .unwrap_err()
            .contains("ARTIFACT_UNDECLARED")
    );
    fs::remove_file(root.join("sneaked.py")).unwrap();
    let mut bad = manifest.clone();
    bad["python_dependencies"] = json!(["python"]);
    fs::write(root.join("manifest.json"), bad.to_string()).unwrap();
    assert!(
        policy::artifact(&repo, root)
            .unwrap_err()
            .contains("PYTHON_IN_ARTIFACT")
    );
    fs::write(root.join("manifest.json"), manifest.to_string()).unwrap();
    fs::write(root.join("clinx-runtime"), b"tampered").unwrap();
    assert!(
        policy::artifact(&repo, root)
            .unwrap_err()
            .contains("ARTIFACT_HASH_MISMATCH")
    );
    for (bytes, error) in [
        (
            b"#!/usr/bin/python3\n".as_slice(),
            "ARTIFACT_NATIVE_BINARY_REQUIRED",
        ),
        (
            b"\x7fELFlibpython3.12.so".as_slice(),
            "PYTHON_LINKAGE_IN_ARTIFACT",
        ),
    ] {
        fs::write(root.join("clinx-runtime"), bytes).unwrap();
        let mut bad = manifest.clone();
        bad["files"]["clinx-runtime"] = json!(format!("{:x}", Sha256::digest(bytes)));
        fs::write(root.join("manifest.json"), bad.to_string()).unwrap();
        assert!(policy::artifact(&repo, root).unwrap_err().contains(error));
    }
    fs::remove_file(root.join("clinx-runtime")).unwrap();
    std::os::unix::fs::symlink("/bin/true", root.join("clinx-runtime")).unwrap();
    assert!(
        policy::artifact(&repo, root)
            .unwrap_err()
            .contains("ARTIFACT_SPECIAL_FILE")
    );
}

// Reuse the existing stage implementation with real freshly built programs.
// The C identity launcher is compiled into a private non-setuid base and never run.
#[allow(dead_code)]
#[path = "../src/build_release.rs"]
mod build;
#[test]
fn real_built_candidate_passes_existing_stage_and_artifact_gate() {
    use std::{collections::BTreeMap, os::unix::fs::PermissionsExt};
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let temp = tempfile::Builder::new()
        .prefix("package-")
        .tempdir_in("/data/artifacts")
        .unwrap();
    let releases = temp.path().join("releases");
    let base = releases.join("base");
    fs::create_dir_all(&base).unwrap();
    assert!(
        Command::new("cc")
            .arg(repo.join("runtime/host_worker.c"))
            .arg("-o")
            .arg(base.join("host-worker"))
            .status()
            .unwrap()
            .success()
    );
    fs::set_permissions(base.join("host-worker"), fs::Permissions::from_mode(0o755)).unwrap();
    build::save(
        &base.join("manifest.json"),
        &json!({"files":{"host-worker":build::digest(&base.join("host-worker")).unwrap()}}),
    )
    .unwrap();
    let mut options = BTreeMap::from([
        ("--source".into(), vec![repo.display().to_string()]),
        (
            "--desktop-base-release".into(),
            vec![base.display().to_string()],
        ),
        (
            "--output".into(),
            vec![temp.path().join("receipt.json").display().to_string()],
        ),
    ]);
    for (flag, binary) in [
        ("--runtime-binary", env!("CARGO_BIN_EXE_clinx-runtime")),
        ("--mcp-relay-binary", env!("CARGO_BIN_EXE_clinx-mcp-relay")),
        (
            "--native-transport-binary",
            env!("CARGO_BIN_EXE_clinx-native-transport"),
        ),
        ("--provider-binary", env!("CARGO_BIN_EXE_clinx-provider")),
        ("--mcp-binary", env!("CARGO_BIN_EXE_clinx-mcp")),
        ("--node-binary", env!("CARGO_BIN_EXE_clinx-node")),
        (
            "--node-daemon-binary",
            env!("CARGO_BIN_EXE_clinx-node-daemon"),
        ),
        ("--observer-binary", env!("CARGO_BIN_EXE_clinx-observer")),
        ("--device-binary", env!("CARGO_BIN_EXE_clinx-device")),
    ] {
        options.insert(flag.into(), vec![binary.into()]);
    }
    let receipt = build::stage(
        &build::Options(options),
        &releases,
        rustix::process::getuid().as_raw(),
        rustix::process::getgid().as_raw(),
    )
    .unwrap();
    assert_eq!(receipt["status"], "STAGED_NOT_SELECTED");
    let dir = Path::new(receipt["release"].as_str().unwrap());
    assert_eq!(policy::artifact(repo, dir).unwrap()["members"], 11);
    // New development tools must never enter the runtime candidate.
    assert!(!dir.join("clinx-source-audit").exists());
    assert!(!dir.join("clinx-build").exists());
}
