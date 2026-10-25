//! Independent byte/SQL-free package and config-CAS obligations from the Python tools.
#[allow(dead_code)]
#[path = "../src/build_release.rs"]
mod build;
use build::*;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    os::unix::fs::{MetadataExt, PermissionsExt, symlink},
    path::{Path, PathBuf},
    process::Command,
};
fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .into()
}
fn temp() -> tempfile::TempDir {
    let p = Path::new("/data/artifacts/cpf25-build");
    fs::create_dir_all(p).unwrap();
    tempfile::tempdir_in(p).unwrap()
}
fn options(values: Vec<(&str, String)>) -> Options {
    Options(
        values
            .into_iter()
            .map(|(k, v)| (k.into(), vec![v]))
            .collect(),
    )
}
fn shell(path: &Path, body: &str) {
    write(path, format!("#!/bin/sh\n{body}\n").as_bytes(), 0o755).unwrap();
}
struct Stage {
    _temp: tempfile::TempDir,
    root: PathBuf,
    base: PathBuf,
    o: Options,
    previous: BTreeMap<String, String>,
}
impl Stage {
    fn new() -> Self {
        let temp = temp();
        let root = temp.path().join("releases");
        let base = root.join("base");
        fs::create_dir_all(&base).unwrap();
        let mut previous = BTreeMap::new();
        for name in [
            "host-worker",
            "deps/keep.dat",
            "clinx-build",
            "clinx-maintenance",
            "bridge.py",
            "clinx/compat/task_registry.py",
            "control/clinx/compat/m9_integration.py",
            "control/provider_adapters/codex.py",
            "provider/bin/codex",
            "deps/cryptography/x.py",
            "deps/cryptography-50.0.2.dist-info/METADATA",
            "deps/_clinx_opaque/__init__.py",
            "deps/_cffi_backend.cpython-312-x86_64-linux-gnu.so",
            "deps/zeroconf/abc.py",
            "deps/bin/jsonschema",
            "deps/bin/cffi-gen-src",
            "deps/.lock",
            "deps/httpx/client.py",
            "deps/httpx/__init__.py",
            "deps/httpx/_model.py",
            "deps/clinx/host/host_executor.py",
            "deps/site-packages/clinx/transport/rust_transport.py",
            "deps/site-packages/task_registry.py",
            "deps/__pycache__/bridge.cpython-313.pyc",
            "deps/rust_workspace_probe.py",
        ] {
            write(&base.join(name), name.as_bytes(), 0o644).unwrap();
            previous.insert(name.into(), sha(name));
        }
        save(&base.join("manifest.json"), &json!({"files":previous})).unwrap();
        let mut o = options(vec![
            ("--source", repo().display().to_string()),
            ("--desktop-base-release", base.display().to_string()),
            (
                "--output",
                temp.path().join("receipt.json").display().to_string(),
            ),
        ]);
        let markers = [
            ("--network-contract", "RUST_NODE_NETWORK_V1"),
            ("--config-compile-contract", "RUST_CONFIG_COMPILER_V1"),
            ("--observation-contract", "RUST_OBSERVATION_V1"),
            ("--collector-contract", "RUST_COLLECTOR_V1"),
            ("--client-runtime-contract", "RUST_CLIENT_RUNTIME_V1"),
            ("--thin-runtime-contract", "RUST_THIN_RUNTIME_V1"),
            ("--device-runtime-contract", "RUST_DEVICE_RUNTIME_V1"),
            ("--daemon-contract", "RUST_NODE_DAEMON_V1"),
            ("--desktop-socket-contract", "RUST_DESKTOP_SOCKET_V1"),
            (
                "--observation-runtime-contract",
                "RUST_OBSERVATION_DIRECTORY_V1",
            ),
            ("--observer-contract", "RUST_OBSERVER_V1"),
            ("--device-cli-contract", "RUST_DEVICE_CLI_V1"),
        ];
        let mut body = "printf '%s\\n' \"$1\" >> \"$0.probes\"\ncase \"$1\" in\n".to_owned();
        for (flag, marker) in markers {
            body.push_str(&format!("{flag}) echo {marker};;\n"));
        }
        body.push_str("esac");
        for (name, key) in BINARIES {
            let p = temp.path().join(name);
            shell(&p, &body);
            o.0.insert(key.to_string(), vec![p.display().to_string()]);
        }
        Self {
            _temp: temp,
            root,
            base,
            o,
            previous,
        }
    }
    fn stage(&self) -> Result<Value> {
        let m = fs::metadata(&self.root).unwrap();
        build::stage(&self.o, &self.root, m.uid(), m.gid())
    }
}
#[test]
fn stage_exact_native_closure_drops_all_old_python_and_dev_tools() {
    let s = Stage::new();
    let r = s.stage().unwrap();
    let release = Path::new(r["release"].as_str().unwrap());
    assert_eq!(r["status"], "STAGED_NOT_SELECTED");
    assert_eq!(r["python_sources"], json!([]));
    assert_eq!(r["python_dependencies"], json!([]));
    let names = walk(release).unwrap();
    assert!(
        names
            .iter()
            .all(|p| p.extension().is_none_or(|e| e != "py"))
    );
    for old in [
        "deps",
        "clinx",
        "bridge.py",
        "clinx-build",
        "clinx-maintenance",
        "control/clinx/compat",
        "control/clinx/monitor",
        "control/clinx/discovery",
        "control/clinx/provider/desktop_socket.py",
        "control/clinx/provider/rust_workspace_probe.py",
    ] {
        assert!(!release.join(old).exists());
    }
    for (name, key) in BINARIES {
        assert_eq!(
            read(&release.join(name)).unwrap(),
            read(Path::new(s.o.get(key).unwrap())).unwrap()
        );
    }
    let node_probes =
        fs::read_to_string(format!("{}.probes", s.o.get("--node-binary").unwrap())).unwrap();
    assert_eq!(
        node_probes
            .lines()
            .filter(|line| *line == "--config-compile-contract")
            .collect::<Vec<_>>(),
        vec!["--config-compile-contract"]
    );
    assert!(release.join("control/clinx/mcp/registry.json").is_file());
    assert_eq!(
        r["retired_files"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_owned())
            .collect::<BTreeSet<_>>(),
        s.previous
            .keys()
            .filter(|n| n.as_str() != "host-worker")
            .cloned()
            .collect()
    );
    for (name, hash) in r["files"].as_object().unwrap() {
        assert_eq!(digest(&release.join(name)).unwrap(), hash.as_str().unwrap());
    }
    for (key, marker) in [
        ("native_transport", "RUST_NATIVE_V1"),
        ("mcp_runtime", "RUST_MCP_V1"),
        ("provider_exchange", "RUST_PROVIDER_V1"),
        ("workspace_observer", "RUST_PROVIDER_V1"),
        ("desktop_socket_maintenance", "RUST_DESKTOP_SOCKET_V1"),
        ("node_api", "RUST_NODE_API_V1"),
        ("config_compiler", "RUST_CONFIG_COMPILER_V1"),
    ] {
        assert_eq!(r[key], marker);
    }
    let pairs = r["files"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(k, v)| format!("\"{k}\": {v}"))
        .collect::<Vec<_>>()
        .join(", ");
    assert_eq!(
        r["version"],
        format!("rust-{}", &sha(format!("{{{pairs}}}"))[..20])
    );
}
#[test]
fn successful_empty_compiler_marker_cannot_stage() {
    let s = Stage::new();
    shell(
        Path::new(s.o.get("--node-binary").unwrap()),
        "[ \"$1\" = --network-contract ] && echo RUST_NODE_NETWORK_V1\nexit 0",
    );
    assert_eq!(
        s.stage().unwrap_err(),
        "QUALIFIED_RUST_CONFIG_COMPILER_REQUIRED"
    );
    assert!(!s.o.path("--output").unwrap().exists());
    assert_eq!(fs::read_dir(&s.root).unwrap().count(), 1);
}
#[test]
fn stage_rejects_base_hash_mode_path_and_unlisted_replacement() {
    for variant in ["hash", "mode", "escape", "replacement"] {
        let mut s = Stage::new();
        match variant {
            "hash" => fs::write(s.base.join("host-worker"), b"changed").unwrap(),
            "mode" => fs::set_permissions(
                s.base.join("host-worker"),
                fs::Permissions::from_mode(0o666),
            )
            .unwrap(),
            "escape" => {
                let mut m = value(&s.base.join("manifest.json")).unwrap();
                m["files"]["../escape"] = json!("bad");
                save(&s.base.join("manifest.json"), &m).unwrap();
            }
            _ => {
                s.o.0
                    .insert("--replace-source".into(), vec!["bridge.py".into()]);
            }
        }
        assert!(s.stage().is_err());
        assert!(!s.o.path("--output").unwrap().exists());
    }
}
struct Cas {
    temp: tempfile::TempDir,
    root: PathBuf,
    capsule: PathBuf,
    plan: Value,
}
impl Cas {
    fn new() -> Self {
        let temp = temp();
        let root = temp.path().join("root");
        fs::create_dir(&root).unwrap();
        fs::write(root.join(".clinx-p1-fixture"), b"ISOLATED_CONFIG_ONLY\n").unwrap();
        let capsule = temp.path().join("capsule");
        fs::create_dir_all(capsule.join("blobs")).unwrap();
        let unit = "/home/test/.config/systemd/user/";
        let mut changes = serde_json::Map::new();
        for (i, n) in [
            "clinx-tunnel.service",
            "clinx-tunnel-health.service",
            "clinx-tunnel.service.d/99-p1-reliability.conf",
        ]
        .iter()
        .enumerate()
        {
            let name = format!("{unit}{n}");
            let target = cas::rooted(&root, &name).unwrap();
            fs::create_dir_all(target.parent().unwrap()).unwrap();
            if i < 2 {
                write(&target, format!("old {i}").as_bytes(), 0o640).unwrap();
            }
            let before = state(&target).unwrap();
            if !before.is_null() {
                write(
                    &capsule
                        .join("blobs")
                        .join(before["sha256"].as_str().unwrap()),
                    &read(&target).unwrap(),
                    0o600,
                )
                .unwrap();
            }
            let after = format!("new {i}");
            write(
                &capsule.join("blobs").join(sha(&after)),
                after.as_bytes(),
                0o600,
            )
            .unwrap();
            changes.insert(
                name,
                json!({"before":before,"after":{"sha256":sha(after),"mode":0o644}}),
            );
        }
        let guard = format!("{unit}clinx-tunnel.service.d/provider.conf");
        write(
            &cas::rooted(&root, &guard).unwrap(),
            b"provider unchanged",
            0o644,
        )
        .unwrap();
        let plan = json!({"home":"/home/test","managed_paths":changes.keys().collect::<Vec<_>>(),"changes":changes,"guards":{&guard:state(&cas::rooted(&root,&guard).unwrap()).unwrap()},"directory_entries":{format!("{unit}clinx-tunnel.service.d"):["provider.conf"]}});
        Self {
            temp,
            root,
            capsule,
            plan,
        }
    }
    fn apply(&self, d: &str) -> Result<Value> {
        cas::apply(&self.plan, &self.capsule, &self.root, d, &self.root)
    }
}
fn snapshot(root: &Path) -> BTreeMap<String, Value> {
    walk(root)
        .unwrap()
        .into_iter()
        .map(|p| (p.display().to_string(), state(&root.join(p)).unwrap()))
        .collect()
}
#[test]
fn config_cas_roundtrip_exact_contents_modes_and_absence() {
    let f = Cas::new();
    let before = snapshot(&f.root);
    f.apply("forward").unwrap();
    assert_ne!(snapshot(&f.root), before);
    f.apply("rollback").unwrap();
    assert_eq!(snapshot(&f.root), before);
}
#[test]
fn config_cas_all_ten_direction_drift_cases_refuse_before_any_write() {
    for direction in ["forward", "rollback"] {
        for drift in ["content", "mode", "guard", "new_dropin", "missing_file"] {
            let f = Cas::new();
            if direction == "rollback" {
                f.apply("forward").unwrap();
            }
            let target =
                cas::rooted(&f.root, f.plan["managed_paths"][0].as_str().unwrap()).unwrap();
            match drift {
                "content" => fs::write(&target, b"concurrent operator").unwrap(),
                "mode" => fs::set_permissions(&target, fs::Permissions::from_mode(0o600)).unwrap(),
                "guard" => {
                    let n = f.plan["guards"].as_object().unwrap().keys().next().unwrap();
                    fs::write(cas::rooted(&f.root, n).unwrap(), b"new provider").unwrap()
                }
                "new_dropin" => {
                    let n = f.plan["directory_entries"]
                        .as_object()
                        .unwrap()
                        .keys()
                        .next()
                        .unwrap();
                    write(
                        &cas::rooted(&f.root, n).unwrap().join("88-foreign.conf"),
                        b"foreign",
                        0o644,
                    )
                    .unwrap()
                }
                _ => fs::remove_file(target).unwrap(),
            };
            let before = snapshot(&f.root);
            assert!(f.apply(direction).is_err(), "{direction} {drift}");
            assert_eq!(snapshot(&f.root), before);
        }
    }
}
#[test]
fn config_cas_live_root_symlink_corrupt_blob_broad_scope_and_partial_switch() {
    let f = Cas::new();
    assert!(
        cas::apply(
            &f.plan,
            &f.capsule,
            Path::new("/"),
            "rollback",
            Path::new("/")
        )
        .unwrap_err()
        .contains("OFFLINE_FIXTURE")
    );
    let target = cas::rooted(&f.root, f.plan["managed_paths"][0].as_str().unwrap()).unwrap();
    let victim = f.temp.path().join("victim");
    fs::write(&victim, b"preserve").unwrap();
    fs::remove_file(&target).unwrap();
    symlink(&victim, &target).unwrap();
    assert!(f.apply("forward").unwrap_err().contains("SYMLINK"));
    assert_eq!(read(&victim).unwrap(), b"preserve");
    let f = Cas::new();
    f.apply("forward").unwrap();
    let before = snapshot(&f.root);
    let entry = f.plan["changes"]
        .as_object()
        .unwrap()
        .values()
        .find(|v| !v["before"].is_null())
        .unwrap();
    fs::write(
        f.capsule
            .join("blobs")
            .join(entry["before"]["sha256"].as_str().unwrap()),
        b"corrupt",
    )
    .unwrap();
    assert!(f.apply("rollback").unwrap_err().contains("BLOB_HASH"));
    assert_eq!(snapshot(&f.root), before);
    let mut f = Cas::new();
    f.plan["managed_paths"]
        .as_array_mut()
        .unwrap()
        .push(json!("/home/test/.local/state/clinx/tasks.sqlite3"));
    assert!(
        f.apply("forward")
            .unwrap_err()
            .contains("CONFIG_ONLY_SCOPE")
    );
    let f = Cas::new();
    let name = f.plan["managed_paths"][0].as_str().unwrap();
    let target = cas::rooted(&f.root, name).unwrap();
    write(
        &target,
        &read(
            &f.capsule
                .join("blobs")
                .join(f.plan["changes"][name]["after"]["sha256"].as_str().unwrap()),
        )
        .unwrap(),
        0o644,
    )
    .unwrap();
    let before = snapshot(&f.root);
    for d in ["forward", "rollback"] {
        assert!(f.apply(d).is_err());
    }
    assert_eq!(snapshot(&f.root), before);
}
#[test]
fn transport_exact_dependencies_and_all_forbidden_package_variants() {
    let deps = transport::REQUIRED
        .iter()
        .map(|s| format!("{}{s}", transport::PREFIX))
        .collect::<Vec<_>>();
    transport::dependencies(&deps.join("\n")).unwrap();
    for i in 0..deps.len() {
        let mut d = deps.clone();
        d.remove(i);
        assert!(
            transport::dependencies(&d.join("\n"))
                .unwrap_err()
                .contains("MISSING")
        );
    }
    for p in transport::FORBIDDEN {
        for suffix in ["", "/nested"] {
            let d = format!("{}\n{}{p}{suffix}", deps.join("\n"), transport::PREFIX);
            assert!(transport::dependencies(&d).unwrap_err().contains("LINKED"));
        }
    }
}
#[test]
fn transport_archive_hash_and_all_escape_types_are_rejected_before_writes() {
    let t = temp();
    let p = t.path().join("archive");
    fs::write(&p, b"wrong archive").unwrap();
    let out = t.path().join("out");
    assert!(
        archive::extract(&p, &out, &"0".repeat(64))
            .unwrap_err()
            .contains("HASH_MISMATCH")
    );
    assert!(!out.exists());
    for kind in ["traversal", "absolute", "symlink", "hardlink", "device"] {
        let mut h = tar::Header::new_ustar();
        h.set_size(0);
        h.set_mode(0o644);
        let name = match kind {
            "traversal" => "../escape",
            "absolute" => "/escape",
            _ => "entry",
        };
        h.as_mut_bytes()[..name.len()].copy_from_slice(name.as_bytes());
        h.set_entry_type(match kind {
            "symlink" => tar::EntryType::Symlink,
            "hardlink" => tar::EntryType::Link,
            "device" => tar::EntryType::Char,
            _ => tar::EntryType::Regular,
        });
        h.set_cksum();
        let mut bytes = h.as_bytes().to_vec();
        bytes.extend([0; 1024]);
        fs::write(&p, &bytes).unwrap();
        assert!(
            archive::extract(&p, &out, &sha(&bytes))
                .unwrap_err()
                .contains("TRAVERSAL")
        );
        assert!(!out.exists());
        assert!(!t.path().join("escape").exists());
    }
}

#[test]
fn github_global_pax_commit_comment_is_inert_and_overrides_fail_before_writes() {
    fn record(key: &str, value: &str) -> Vec<u8> {
        let suffix = format!(" {key}={value}\n");
        let mut length = suffix.len() + 1;
        loop {
            let text = format!("{length}{suffix}");
            if text.len() == length {
                return text.into_bytes();
            }
            length = text.len();
        }
    }
    let comment = record("comment", transport::COMMIT);
    assert_eq!(
        comment,
        b"52 comment=0f870e50a973fa820d4c409000059e181e8d242b\n"
    );
    let mut invalid_utf8 = comment.clone();
    invalid_utf8[15] = 255;
    let mut invalid_length = comment.clone();
    invalid_length[0..2].copy_from_slice(b"99");
    let mut invalid_hex = comment.clone();
    invalid_hex[15] = b'G';
    let variants = [
        ("comment", comment.clone(), true),
        ("path", record("path", "../escape"), false),
        ("linkpath", record("linkpath", "/escape"), false),
        ("mtime", record("mtime", "0"), false),
        ("broken", b"99 comment=truncated\n".to_vec(), false),
        ("invalid_utf8", invalid_utf8, false),
        ("invalid_length", invalid_length, false),
        ("invalid_hex", invalid_hex, false),
        (
            "hidden_record",
            [comment.clone(), b"\n".to_vec(), record("path", "../escape")].concat(),
            false,
        ),
        (
            "mixed",
            [comment.clone(), record("path", "entry")].concat(),
            false,
        ),
        (
            "duplicate",
            [comment.clone(), comment.clone()].concat(),
            false,
        ),
        ("duplicate_header", comment, false),
    ];
    for (name, metadata, accepted) in variants {
        let t = temp();
        let archive = t.path().join(format!("{name}.tar"));
        let mut tar = tar::Builder::new(fs::File::create(&archive).unwrap());
        for (entry, kind, bytes) in [
            (
                "safe",
                tar::EntryType::Regular,
                b"unchanged source".as_slice(),
            ),
            (
                "pax_global_header",
                tar::EntryType::new(b'g'),
                metadata.as_slice(),
            ),
        ] {
            let mut h = tar::Header::new_ustar();
            h.set_path(entry).unwrap();
            h.set_entry_type(kind);
            h.set_mode(0o644);
            h.set_size(bytes.len() as u64);
            h.set_cksum();
            tar.append(&h, bytes).unwrap();
            if name == "duplicate_header" && kind.is_pax_global_extensions() {
                tar.append(&h, bytes).unwrap();
            }
        }
        tar.finish().unwrap();
        let destination = t.path().join("out");
        let result = archive::extract(&archive, &destination, &digest(&archive).unwrap());
        if accepted {
            result.unwrap();
            assert_eq!(walk(&destination).unwrap(), vec![PathBuf::from("safe")]);
            assert_eq!(
                read(&destination.join("safe")).unwrap(),
                b"unchanged source"
            );
            assert!(!destination.join("pax_global_header").exists());
        } else {
            assert_eq!(result.unwrap_err(), "ARCHIVE_GLOBAL_PAX_REFUSED", "{name}");
            assert!(
                !destination.exists(),
                "all metadata must validate before any extraction: {name}"
            );
        }
        assert!(!t.path().join("escape").exists());
    }
}
#[test]
fn transport_refuses_live_existing_output_and_removes_readonly_cache() {
    let t = temp();
    for destination in [
        t.path(),
        Path::new("/home/pvxlabs/.local/bin"),
        Path::new("/"),
    ] {
        assert!(
            transport::build(Path::new("unused"), Path::new("unused"), destination)
                .unwrap_err()
                .contains("NEW_TASK_ARTIFACT")
        );
    }
    let transient = t.path().join("build");
    write(
        &transient.join("cache/module/source.go"),
        b"package fixture",
        0o444,
    )
    .unwrap();
    fs::set_permissions(
        transient.join("cache/module"),
        fs::Permissions::from_mode(0o555),
    )
    .unwrap();
    fs::write(t.path().join("preserve"), b"existing dirty data").unwrap();
    archive::remove_transient(&transient).unwrap();
    assert!(!transient.exists());
    assert_eq!(
        read(&t.path().join("preserve")).unwrap(),
        b"existing dirty data"
    );
}
fn candidate(root: &Path) -> PathBuf {
    fs::create_dir_all(root).unwrap();
    write(
        &root.join("tunnel-client-runtime"),
        b"transport fixture",
        0o755,
    )
    .unwrap();
    let deps = transport::REQUIRED
        .iter()
        .map(|s| format!("{}{s}", transport::PREFIX))
        .collect::<Vec<_>>()
        .join("\n");
    write(&root.join("dependencies.txt"), deps.as_bytes(), 0o644).unwrap();
    write(
        &root.join("builder-source.py"),
        b"# historical source evidence, never executed\n",
        0o644,
    )
    .unwrap();
    write(&root.join("upstream-source-files.json"), b"{}\n", 0o644).unwrap();
    let m = json!({"schema":"CLINX_TRANSPORT_RUNTIME_CANDIDATE_V1","status":"BUILT_OFFLINE_NOT_DEPLOYED","upstream_commit":transport::COMMIT,"upstream_archive_sha256":transport::SOURCE_SHA,"go_archive_sha256":transport::GO_SHA,"entrypoint":transport::ENTRYPOINT,"upstream_patch":null,"embedded_codex":"NOT_LINKED","reproducible_across_source_paths":true,"transient_removed":true,"binary":{"sha256":digest(&root.join("tunnel-client-runtime")).unwrap()},"dependencies_sha256":digest(&root.join("dependencies.txt")).unwrap(),"builder_sha256":digest(&root.join("builder-source.py")).unwrap(),"source_files_manifest_sha256":digest(&root.join("upstream-source-files.json")).unwrap()});
    let p = root.join("manifest.json");
    save(&p, &m).unwrap();
    p
}
#[test]
fn transport_candidate_all_five_original_drift_cases_fail_closed() {
    for drift in [
        "binary",
        "deps",
        "entrypoint",
        "full_dependency",
        "unqualified",
    ] {
        let t = temp();
        let p = candidate(&t.path().join("candidate"));
        transport::qualified(&p).unwrap();
        let mut m = value(&p).unwrap();
        let root = p.parent().unwrap();
        match drift {
            "binary" => fs::write(root.join("tunnel-client-runtime"), b"different").unwrap(),
            "deps" | "full_dependency" => {
                let dep = root.join("dependencies.txt");
                let mut bytes = read(&dep).unwrap();
                bytes.extend(format!("\n{}pkg/codexappserver", transport::PREFIX).as_bytes());
                fs::write(&dep, bytes).unwrap();
                if drift == "full_dependency" {
                    m["dependencies_sha256"] = json!(digest(&dep).unwrap());
                }
            }
            "entrypoint" => m["entrypoint"] = json!("./cmd/client"),
            _ => m["reproducible_across_source_paths"] = json!(false),
        }
        save(&p, &m).unwrap();
        assert!(transport::qualified(&p).is_err(), "{drift}");
    }
}
#[test]
fn native_tunnel_package_reproducible_runtime_free_of_python_and_exact_three_file_cas() {
    let t = temp();
    // Strip only fixture copies, exactly as shipping native artifacts; this
    // keeps deterministic gzip qualification independent of debug-info volume.
    let supervisor = t.path().join("clinx-tunnel");
    let builder = t.path().join("clinx-build");
    for (original, copy) in [
        (env!("CARGO_BIN_EXE_clinx-tunnel"), &supervisor),
        (env!("CARGO_BIN_EXE_clinx-build"), &builder),
    ] {
        fs::copy(original, copy).unwrap();
        assert!(
            Command::new("strip")
                .arg("--strip-debug")
                .arg(copy)
                .status()
                .unwrap()
                .success()
        );
    }
    let old = t.path().join("old-tunnel");
    write(&old, b"preserve installed full client", 0o755).unwrap();
    let home = t.path().join("home");
    fs::create_dir(&home).unwrap();
    let candidate = candidate(&t.path().join("candidate"));
    let common = vec![
        ("--source", repo().display().to_string()),
        ("--home", home.display().to_string()),
        ("--tunnel", old.display().to_string()),
        ("--supervisor-binary", supervisor.display().to_string()),
        ("--cas-binary", builder.display().to_string()),
        (
            "--transport-runtime-manifest",
            candidate.display().to_string(),
        ),
    ];
    let mut receipts = vec![];
    for name in ["a", "b"] {
        let mut fields = common.clone();
        fields.push(("--output", t.path().join(name).display().to_string()));
        receipts.push(tunnel::build(&options(fields)).unwrap());
    }
    assert_eq!(receipts[0], receipts[1]);
    for n in ["release.tar.gz", "rollback-capsule.tar.gz"] {
        assert_eq!(
            read(&t.path().join("a").join(n)).unwrap(),
            read(&t.path().join("b").join(n)).unwrap()
        );
    }
    let release = t.path().join("a/release");
    let m = value(&release.join("manifest.json")).unwrap();
    assert_eq!(m["status"], "NOT_DEPLOYED");
    assert!(
        walk(&release)
            .unwrap()
            .iter()
            .all(|p| p.extension().is_none_or(|e| e != "py"))
    );
    assert!(m["files"].get("clinx-node").is_none());
    assert_eq!(
        m["files"]["clinx-tunnel"]["sha256"],
        digest(&supervisor).unwrap()
    );
    assert_eq!(
        m["inputs"]["supervisor_runtime"]["contract"],
        "RUST_TUNNEL_SUPERVISOR_V1"
    );
    for (n, mode) in [
        ("clinx-tunnel.service", "run"),
        ("clinx-tunnel-health.service", "status"),
    ] {
        let unit = String::from_utf8(read(&release.join("units").join(n)).unwrap()).unwrap();
        let start = unit.lines().find(|l| l.starts_with("ExecStart=")).unwrap();
        assert!(start.contains(&format!("/clinx-tunnel {mode} --state-dir ")));
        assert!(!start.contains("python") && !start.contains('@'));
    }
    let unit =
        String::from_utf8(read(&release.join("units/clinx-tunnel.service")).unwrap()).unwrap();
    assert!(unit.contains(&format!(
        "{}/tunnel-client-runtime run",
        m["install_path"].as_str().unwrap()
    )));
    assert!(!unit.contains(candidate.parent().unwrap().to_str().unwrap()));
    assert_eq!(m["files"]["tunnel-client-runtime"]["mode"], 0o755);
    assert_eq!(
        regular(&release.join("tunnel-client-runtime"))
            .unwrap()
            .mode()
            & 0o777,
        0o755
    );
    assert_eq!(receipts[0]["managed_file_count"], 3);
    let plan = value(&t.path().join("a/rollback-capsule/plan.json")).unwrap();
    assert_eq!(
        plan["guards"][old.to_str().unwrap()]["sha256"],
        digest(&old).unwrap()
    );
    assert_eq!(plan["production_authorization"], "NOT_GRANTED");
    assert_eq!(read(&old).unwrap(), b"preserve installed full client");
    let output = Command::new(release.join("clinx-tunnel"))
        .args(["status", "--state-dir"])
        .arg(t.path().join("unused"))
        .env_clear()
        .env("PATH", "/nonexistent")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(
        serde_json::from_slice::<Value>(&output.stdout).unwrap()["currently_healthy"],
        false
    );
}

#[test]
fn source_profiles_preserve_original_role_path_and_duplicate_guards() {
    let t = temp();
    let source = t.path();
    save(&source.join("architecture/runtime-data.json"), &json!([])).unwrap();
    let good = json!({"path":"clinx/a.py","role":"COMPAT","profiles":["monitor"]});
    let cases = [
        (
            json!({"schema":2,"modules":[]}),
            "UNKNOWN_SOURCE_MANIFEST_SCHEMA",
        ),
        (
            json!({"schema":1,"modules":[good,good]}),
            "DUPLICATE_SOURCE_MODULE",
        ),
        (
            json!({"schema":1,"modules":[{"path":"/outside.py","role":"DEAD","profiles":[]}]}),
            "REGULAR_RELATIVE_PATH_REQUIRED",
        ),
        (
            json!({"schema":1,"modules":[{"path":"clinx/a.rs","role":"DEAD","profiles":[]}]}),
            "INVALID_SOURCE_PATH",
        ),
        (
            json!({"schema":1,"modules":[{"path":"clinx/a.py","role":"unknown","profiles":[]}]}),
            "UNKNOWN_SOURCE_ROLE",
        ),
        (
            json!({"schema":1,"modules":[{"path":"clinx/a.py","role":"COMPAT","profiles":["other"]}]}),
            "UNKNOWN_SOURCE_PROFILE",
        ),
        (
            json!({"schema":1,"modules":[{"path":"scripts/a.py","role":"DEVOPS","profiles":["monitor"]}]}),
            "NON_RUNTIME_SOURCE_IN_PACKAGE",
        ),
        (
            json!({"schema":1,"modules":[{"path":"clinx/compat/a.py","role":"COMPAT","profiles":["monitor"]}]}),
            "LEGACY_CORE_IN_RUNTIME_PACKAGE",
        ),
        (
            json!({"schema":1,"modules":[{"path":"clinx/a.py","role":"ACTIVE_RUNTIME","profiles":["rust"]}]}),
            "PYTHON_FREE_RUST_RELEASE_REQUIRED",
        ),
    ];
    for (manifest, error) in cases {
        save(&source.join("architecture/python-sources.json"), &manifest).unwrap();
        assert!(native_data(source).unwrap_err().contains(error));
    }
    save(
        &source.join("architecture/python-sources.json"),
        &json!({"schema":1,"modules":[]}),
    )
    .unwrap();
    for rows in [
        json!([{"path":"a.json","profiles":["other"]}]),
        json!([{"path":"../a.json","profiles":["rust"]}]),
        json!([{"path":"a.json","profiles":[]},{"path":"a.json","profiles":[]}]),
    ] {
        save(&source.join("architecture/runtime-data.json"), &rows).unwrap();
        assert!(native_data(source).is_err());
    }
}

#[test]
fn cas_parent_escape_and_invalid_late_mode_fail_before_mutation() {
    let outside = tempfile::tempdir().unwrap();
    fs::write(
        outside.path().join(".clinx-p1-fixture"),
        b"ISOLATED_CONFIG_ONLY\n",
    )
    .unwrap();
    let escaped = PathBuf::from(format!("/data/artifacts/../..{}", outside.path().display()));
    assert_eq!(
        cas::fixture_root(&escaped).unwrap_err(),
        "OFFLINE_FIXTURE_ROOT_REQUIRED"
    );
    for mode in [json!("invalid"), json!(true), json!(65536)] {
        let mut f = Cas::new();
        let last = f.plan["managed_paths"]
            .as_array()
            .unwrap()
            .last()
            .unwrap()
            .as_str()
            .unwrap()
            .to_owned();
        f.plan["changes"][&last]["after"]["mode"] = mode;
        let before = snapshot(&f.root);
        assert_eq!(f.apply("forward").unwrap_err(), "INVALID_MODE");
        assert_eq!(snapshot(&f.root), before);
    }
}

#[test]
fn staged_real_node_uses_native_api_without_checkout_or_python_path() {
    use std::io::Write;
    use std::process::Stdio;
    let mut s = Stage::new();
    let native = s._temp.path().join("tested-node");
    fs::copy(env!("CARGO_BIN_EXE_clinx-node"), &native).unwrap();
    assert!(
        Command::new("strip")
            .arg("--strip-debug")
            .arg(&native)
            .status()
            .unwrap()
            .success()
    );
    let binary = native.to_str().unwrap();
    s.o.0.insert("--node-binary".into(), vec![binary.into()]);
    let receipt = s.stage().unwrap();
    let release = Path::new(receipt["release"].as_str().unwrap());
    let copied = release.join("clinx-node");
    assert_eq!(digest(&copied).unwrap(), digest(Path::new(binary)).unwrap());
    let request = json!({"protocol":"CLINX_REQUEST_PIPE_V1","mode":"api","operation":"clinx_get_capabilities","arguments":{},"config":{"runtime_host":"air","provider_endpoint":"/unconfigured-provider","provider_timeout_seconds":3,"host_aliases":{"air":["air"]},"codex_host_ids":{}}});
    let mut child = Command::new(&copied)
        .current_dir(release)
        .env_clear()
        .env("PATH", "/no-python")
        .env("HOME", s._temp.path())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(format!("{request}\n").as_bytes())
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let result: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(result["result"]["authority"], "RUST");
    assert_eq!(result["result"]["host_tools"], true);
}

#[test]
fn all_nine_real_binaries_stage_with_exact_hashes_and_no_retired_runtime() {
    let mut s = Stage::new();
    let actual = [
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
    ];
    let before: BTreeMap<_, _> = actual
        .iter()
        .map(|(key, path)| (*key, digest(Path::new(path)).unwrap()))
        .collect();
    for (key, path) in actual {
        s.o.0.insert(key.into(), vec![path.into()]);
    }
    let previous = snapshot(&s.base);
    let receipt = s.stage().unwrap();
    assert_eq!(snapshot(&s.base), previous);
    let release = Path::new(receipt["release"].as_str().unwrap());
    for (name, key) in BINARIES {
        assert_eq!(receipt["files"][name], before[key]);
        assert_eq!(digest(&release.join(name)).unwrap(), before[key]);
        assert_eq!(
            digest(Path::new(s.o.get(key).unwrap())).unwrap(),
            before[key]
        );
        assert_eq!(regular(&release.join(name)).unwrap().mode() & 0o777, 0o755);
    }
    assert_eq!(
        read(&release.join("host-worker")).unwrap(),
        read(&s.base.join("host-worker")).unwrap()
    );
    let mut expected: BTreeSet<String> = BINARIES.iter().map(|(name, _)| (*name).into()).collect();
    expected.insert("host-worker".into());
    expected.extend(native_data(&repo()).unwrap().into_keys());
    assert_eq!(
        receipt["files"]
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect::<BTreeSet<_>>(),
        expected
    );
    expected.insert("manifest.json".into());
    assert_eq!(
        walk(release)
            .unwrap()
            .iter()
            .map(|p| p.display().to_string())
            .collect::<BTreeSet<_>>(),
        expected
    );
    for (field, marker) in [
        ("native_transport", "RUST_NATIVE_V1"),
        ("mcp_runtime", "RUST_MCP_V1"),
        ("node_api", "RUST_NODE_API_V1"),
        ("node_network", "RUST_NODE_NETWORK_V1"),
        ("observation_runtime", "RUST_OBSERVATION_V1"),
        ("collector_runtime", "RUST_COLLECTOR_V1"),
        ("client_runtime", "RUST_CLIENT_RUNTIME_V1"),
        ("thin_runtime", "RUST_THIN_RUNTIME_V1"),
        ("device_runtime", "RUST_DEVICE_RUNTIME_V1"),
        ("node_daemon", "RUST_NODE_DAEMON_V1"),
        ("provider_exchange", "RUST_PROVIDER_V1"),
        ("workspace_observer", "RUST_PROVIDER_V1"),
        ("observation_directory", "RUST_OBSERVATION_DIRECTORY_V1"),
        ("observer_runtime", "RUST_OBSERVER_V1"),
        ("device_cli", "RUST_DEVICE_CLI_V1"),
        ("desktop_socket_maintenance", "RUST_DESKTOP_SOCKET_V1"),
        ("config_compiler", "RUST_CONFIG_COMPILER_V1"),
    ] {
        assert_eq!(receipt[field], marker);
    }
    assert_eq!(receipt["python_sources"], json!([]));
    assert_eq!(receipt["python_dependencies"], json!([]));
    for retired in [
        "deps",
        "clinx",
        "bridge.py",
        "clinx-build",
        "clinx-install",
        "clinx-maintenance",
        "control/clinx/compat",
    ] {
        assert!(!release.join(retired).exists());
    }
}

#[test]
fn current_monitor_script_uses_native_tool_without_python() {
    let script = fs::read_to_string(repo().join("MonitorApp/Scripts/build-app.sh")).unwrap();
    assert!(!script.contains("python3") && !script.contains("runtime_sources.py"));
    assert!(script.contains("clinx-build\" native-monitor-resources"));
    assert!(script.contains("for helper in clinx-provider clinx-node clinx-node-daemon;"));
}

#[test]
fn build_cli_help_and_privilege_gate_do_not_touch_any_installation() {
    for command in [
        "stage-rust-release",
        "build-tunnel-release",
        "build-transport-runtime",
        "tunnel-cas",
    ] {
        let output = Command::new(env!("CARGO_BIN_EXE_clinx-build"))
            .args([command, "--help"])
            .env_clear()
            .env("PATH", "/no-python")
            .output()
            .unwrap();
        assert!(output.status.success());
        let help: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(help["command"], command);
        assert_eq!(help["deployment"], "NOT_PERFORMED_BY_BUILDERS");
    }
    if !rustix::process::geteuid().is_root() {
        let output = Command::new(env!("CARGO_BIN_EXE_clinx-build"))
            .arg("stage-rust-release")
            .output()
            .unwrap();
        assert!(!output.status.success());
        assert_eq!(
            String::from_utf8_lossy(&output.stderr).trim(),
            "ADMINISTRATOR_REQUIRED"
        );
    }
}
