//! Independent workspace observer oracle for disposable integration tests.
//! This example is not a runtime/release binary and never accesses Provider IO.
use serde_json::{Value, json};
fn main() {
    let args = std::env::args().collect::<Vec<_>>();
    assert_eq!(args[1], "--observe");
    let request: Value = serde_json::from_str(&args[3]).unwrap();
    let path = std::env::current_exe().unwrap();
    let result = if path.file_name().unwrap() == "routes-probe" {
        let host = request["host"].as_str().unwrap();
        assert!(matches!(host, "air" | "imac"));
        assert_eq!(args[2], format!("/fixture/{host}.sock"));
        let cwd = request["cwd"].as_str().unwrap();
        json!({"observer_source":"CODEX_PROVIDER_COMMAND_EXEC","observer_uid":501,
            "repository_identity":if host=="air" {"git-common:9:42"} else {"git-common:9:99"},
            "cwd":cwd,"repository_common_dir":format!("{cwd}/.git"),"repository_root":cwd,
            "worktree":format!("{cwd}/.git"),"branch":"main","head":"a".repeat(40)})
    } else {
        assert_eq!(path.file_name().unwrap(), "observer-probe");
        assert_eq!(args[2], "/fixture/provider.sock");
        let mut value: Value = serde_json::from_slice(
            &std::fs::read(path.parent().unwrap().join("response.json")).unwrap(),
        )
        .unwrap();
        match request["cwd"].as_str().unwrap() {
            "/provider-only/missing" => value = json!({"error":"WORKSPACE_CWD_UNREADABLE"}),
            "/provider-only/different" => value["repository_identity"] = json!("git-common:9:99"),
            _ => {}
        }
        value
    };
    println!("{result}");
}
