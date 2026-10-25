//! Independent old Python compiler outputs consumed by the actual offline CLI.
use serde_json::Value;
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    process::{Command, Output},
};

#[test]
fn compiler_capability_is_explicit_without_reading_config_or_state() {
    let result = Command::new(env!("CARGO_BIN_EXE_clinx-node"))
        .arg("--config-compile-contract")
        .env_clear()
        .output()
        .unwrap();
    assert!(result.status.success());
    assert_eq!(result.stdout, b"RUST_CONFIG_COMPILER_V1\n");
    assert!(result.stderr.is_empty());
}

fn snapshot(root: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
    let mut out = BTreeMap::new();
    for entry in fs::read_dir(root).unwrap() {
        let p = entry.unwrap().path();
        if p.is_dir() {
            out.extend(snapshot(&p));
        } else {
            out.insert(p.clone(), fs::read(p).unwrap());
        }
    }
    out
}
fn command(root: &Path, kind: &str, path: &Path) -> Output {
    Command::new(env!("CARGO_BIN_EXE_clinx-node"))
        .args(["--compile-config", kind])
        .arg(path)
        .current_dir(root)
        .env_clear()
        .env("PATH", "/nonexistent")
        .env("HOME", root.join("home"))
        .env("XDG_RUNTIME_DIR", root.join("run"))
        .output()
        .unwrap()
}
fn golden(index: usize, kind: &str) {
    let root = tempfile::tempdir().unwrap();
    let raw = include_str!("../../fixtures/runtime_compile_v1.json")
        .replace("@ROOT@", root.path().to_str().unwrap());
    let values: Value = serde_json::from_str(&raw).unwrap();
    let case = &values["cases"][index];
    let path = root.path().join("config.toml");
    fs::write(&path, case["toml"].as_str().unwrap()).unwrap();
    let before = snapshot(root.path());
    let result = command(root.path(), kind, &path);
    assert!(
        result.status.success(),
        "{} {kind}: {}",
        case["name"],
        String::from_utf8_lossy(&result.stderr)
    );
    assert!(result.stderr.is_empty());
    assert_eq!(
        serde_json::from_slice::<Value>(&result.stdout).unwrap(),
        case["expected"][kind],
        "{} {kind}",
        case["name"]
    );
    assert_eq!(
        snapshot(root.path()),
        before,
        "compiler must only emit stdout"
    );
}
macro_rules! case {
    ($test:ident,$index:expr,$kind:literal) => {
        #[test]
        fn $test() {
            golden($index, $kind);
        }
    };
}
case!(default_host, 0, "host");
case!(mcp_has_no_implicit_runtime_self_route, 0, "mcp");
case!(unicode_workspace_host, 1, "host");
case!(unicode_aliases_and_desktop_routes, 1, "mcp");
case!(host_commands_timeouts_targets_and_workflow, 2, "host");
case!(configured_host_mcp, 2, "mcp");
case!(canonical_host_keeps_lowercase_not_casefold, 3, "host");
case!(disabled_host_mcp, 3, "mcp");
case!(relative_provider_host, 4, "host");
case!(
    mcp_endpoint_local_precedence_and_fractional_timeout,
    4,
    "mcp"
);
case!(native_fallback_host, 5, "host");
case!(mcp_tilde_native_endpoint, 5, "mcp");

#[test]
fn host_compiles_without_provider_and_mcp_missing_endpoint_fails_without_output() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("config.toml");
    fs::write(&path,"[linear]\nteam_id=\"t\"\ntrigger_label=\"go\"\ntodo_state=\"Todo\"\nrunning_state=\"Run\"\nreview_state=\"Review\"\n[runtime]\nruntime_host=\"air\"\n[projects.fixture]\ncwd=\".\"\nbranch=\"main\"\n").unwrap();
    let before = snapshot(root.path());
    assert!(command(root.path(), "host", &path).status.success());
    let result = command(root.path(), "mcp", &path);
    assert!(!result.status.success());
    assert!(result.stdout.is_empty());
    assert_eq!(
        String::from_utf8(result.stderr).unwrap(),
        "PROVIDER_ENDPOINT_REQUIRED\n"
    );
    assert_eq!(snapshot(root.path()), before);
}

#[test]
fn invalid_kind_unreadable_and_invalid_toml_never_write_configuration() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("config.toml");
    let invalid = command(root.path(), "other", &path);
    assert!(!invalid.status.success());
    assert!(invalid.stdout.is_empty());
    assert_eq!(
        String::from_utf8(invalid.stderr).unwrap(),
        "CONFIG_COMPILE_KIND_INVALID\n"
    );
    let missing = command(root.path(), "host", &path);
    assert!(!missing.status.success());
    assert!(missing.stdout.is_empty());
    assert_eq!(
        String::from_utf8(missing.stderr).unwrap(),
        "CONFIG_READ_FAILED\n"
    );
    fs::write(&path, "[invalid").unwrap();
    let before = snapshot(root.path());
    let broken = command(root.path(), "mcp", &path);
    assert!(!broken.status.success());
    assert!(broken.stdout.is_empty());
    assert_eq!(
        String::from_utf8(broken.stderr).unwrap(),
        "CONFIG_TOML_INVALID\n"
    );
    assert_eq!(snapshot(root.path()), before);
}

fn reject_nonfinite(kind: &str, table: &str, key: &str) {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("config.toml");
    let cases: Value =
        serde_json::from_str(include_str!("../../fixtures/runtime_compile_v1.json")).unwrap();
    let base = cases["cases"][0]["toml"]
        .as_str()
        .unwrap()
        .replace("@ROOT@", root.path().to_str().unwrap());
    for value in ["nan", "inf"] {
        let input = if table == "app_server" {
            format!("{base}{key}={value}\n")
        } else {
            format!("{base}[{table}]\n{key}={value}\n")
        };
        fs::write(&path, input).unwrap();
        let before = snapshot(root.path());
        let result = command(root.path(), kind, &path);
        assert!(!result.status.success());
        assert!(result.stdout.is_empty());
        assert_eq!(
            String::from_utf8(result.stderr).unwrap(),
            "NONFINITE_RUNTIME_CONFIG\n"
        );
        assert_eq!(snapshot(root.path()), before);
    }
}

#[test]
fn host_nonfinite_timeout_cannot_emit_an_unloadable_worker_config() {
    reject_nonfinite("host", "host_executor", "max_timeout_seconds");
    // Independent exact legacy Python JSON encodings: serde_json is also the
    // runtime Host configuration decoder and rejects both before loading.
    let cases: Value =
        serde_json::from_str(include_str!("../../fixtures/runtime_compile_v1.json")).unwrap();
    let mut config = cases["cases"][0]["expected"]["host"].clone();
    assert!(
        serde_json::from_value::<clinx_decision_kernel::host_executor::Config>(config.clone())
            .is_ok()
    );
    config["max_timeout_seconds"] = Value::String("NONFINITE".into());
    for value in ["NaN", "Infinity"] {
        let wire = config.to_string().replace("\"NONFINITE\"", value);
        match serde_json::from_str::<clinx_decision_kernel::host_executor::Config>(&wire) {
            Err(error) => assert!(error.is_syntax()),
            Ok(_) => panic!("runtime unexpectedly accepted nonfinite JSON"),
        }
    }
}

#[test]
fn mcp_nonfinite_timeout_cannot_emit_an_unloadable_provider_config() {
    reject_nonfinite("mcp", "app_server", "request_timeout_seconds");
    let cases: Value =
        serde_json::from_str(include_str!("../../fixtures/runtime_compile_v1.json")).unwrap();
    let mut config = cases["cases"][0]["expected"]["mcp"].clone();
    assert!(
        serde_json::from_value::<clinx_decision_kernel::mcp_api::Config>(config.clone()).is_ok()
    );
    config["provider_timeout_seconds"] = Value::String("NONFINITE".into());
    for value in ["NaN", "Infinity"] {
        let wire = config.to_string().replace("\"NONFINITE\"", value);
        match serde_json::from_str::<clinx_decision_kernel::mcp_api::Config>(&wire) {
            Err(error) => assert!(error.is_syntax()),
            Ok(_) => panic!("runtime unexpectedly accepted nonfinite JSON"),
        }
    }
}
