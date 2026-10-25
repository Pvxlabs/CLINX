use serde_json::json;
use std::{fs, process::Command};
#[test]
fn native_source_check_rejects_python_reintroduction_and_bad_data() {
    let dir = tempfile::Builder::new()
        .prefix("clinx-source-check-")
        .tempdir_in("/data/artifacts")
        .unwrap();
    let root = dir.path();
    assert!(
        Command::new("git")
            .args(["init", "--quiet"])
            .current_dir(root)
            .status()
            .unwrap()
            .success()
    );
    fs::create_dir(root.join("architecture")).unwrap();
    let inventory =
        json!({"schema":1,"modules":[{"path":"clinx/old.py","role":"DEAD","profiles":[]}]});
    fs::write(
        root.join("architecture/python-sources.json"),
        inventory.to_string(),
    )
    .unwrap();
    let native = json!({"monitor_python_sources":[],"monitor_python_dependencies":[],"release_python_sources":[],"release_python_dependencies":[],"monitor_helpers":["clinx-provider","clinx-node","clinx-node-daemon"]});
    fs::write(
        root.join("architecture/native-runtime.json"),
        native.to_string(),
    )
    .unwrap();
    fs::write(root.join("architecture/runtime-data.json"), "[]").unwrap();
    let run = || {
        Command::new(env!("CARGO_BIN_EXE_clinx-source-audit"))
            .arg("--check-structure")
            .current_dir(root)
            .output()
            .unwrap()
    };
    assert!(run().status.success());
    std::os::unix::fs::symlink("missing-target", root.join("dangling.py")).unwrap();
    assert!(!run().status.success());
    fs::remove_file(root.join("dangling.py")).unwrap();
    fs::create_dir(root.join("clinx")).unwrap();
    std::os::unix::fs::symlink("missing-target", root.join("clinx/old.py")).unwrap();
    assert!(!run().status.success());
    fs::remove_file(root.join("clinx/old.py")).unwrap();

    fs::write(root.join("new.py"), "pass\n").unwrap();
    let bad = run();
    assert!(!bad.status.success());
    assert!(
        String::from_utf8(bad.stderr)
            .unwrap()
            .contains("UNDECLARED_PYTHON_SOURCE:new.py")
    );
    fs::remove_file(root.join("new.py")).unwrap();
    let mut invalid = native;
    invalid["release_python_dependencies"] = json!(["python"]);
    fs::write(
        root.join("architecture/native-runtime.json"),
        invalid.to_string(),
    )
    .unwrap();
    assert!(!run().status.success());
    invalid["release_python_dependencies"] = json!([]);
    fs::write(
        root.join("architecture/native-runtime.json"),
        invalid.to_string(),
    )
    .unwrap();
    for name in ["../escape.json", "/outside.json", "absent.json"] {
        fs::write(
            root.join("architecture/runtime-data.json"),
            json!([{"path":name,"profiles":["rust"]}]).to_string(),
        )
        .unwrap();
        assert!(!run().status.success(), "{name}");
    }
    fs::write(root.join("architecture/runtime-data.json"), "[]").unwrap();
    let mut inventory = inventory;
    inventory["modules"][0]["role"] = json!("COMPAT");
    fs::write(
        root.join("architecture/python-sources.json"),
        inventory.to_string(),
    )
    .unwrap();
    assert!(!run().status.success());
}
