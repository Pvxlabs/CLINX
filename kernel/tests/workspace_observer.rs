//! Provider-only workspaces must not require filesystem access by the writer.
use clinx_decision_kernel::{
    authority_store::Store,
    business::{Policy, Project, Role, workspace_observer},
};
use serde_json::{Value, json};
use std::{fs, os::unix::fs::PermissionsExt};

#[test]
fn native_observation_drives_all_workspace_gates_without_local_filesystem_fallback() {
    let root =
        std::env::temp_dir().join(format!("clinx-workspace-observer-{}", std::process::id()));
    fs::create_dir(&root).unwrap();
    let program = root.join("probe.py");
    let response = root.join("response.json");
    // The simulated native workspace intentionally does not exist on the writer.
    // Only the authenticated adapter supplies its metadata in this fixture.
    let actual = json!({"cwd":"/provider-only/repo","repository_identity":"git-common:9:42",
        "repository_root":"/provider-only/repo","repository_common_dir":"/provider-only/repo/.git",
        "worktree":"/provider-only/repo/.git","branch":"native-branch","head":"a".repeat(40),
        "observer_uid":1001,"observer_source":"CODEX_PROVIDER_COMMAND_EXEC"});
    fs::write(&response, actual.to_string()).unwrap();
    fs::write(
        &program,
        format!(
            r#"#!/usr/bin/python3
import json,sys,pathlib
request=json.loads(sys.argv[3])
value=json.loads(pathlib.Path({:?}).read_text())
if request['cwd']=='/provider-only/missing':value={{'error':'WORKSPACE_CWD_UNREADABLE'}}
if request['cwd']=='/provider-only/different':value['repository_identity']='git-common:9:99'
print(json.dumps(value))
"#,
            response.to_string_lossy()
        ),
    )
    .unwrap();
    fs::set_permissions(&program, fs::Permissions::from_mode(0o700)).unwrap();
    workspace_observer::configure(program.clone(), "/fixture/provider.sock".into()).unwrap();
    let state = root.join("state");
    fs::create_dir(&state).unwrap();
    fs::set_permissions(&state, fs::Permissions::from_mode(0o700)).unwrap();
    let mut store = Store::open(&state, true).unwrap();
    let policy = Policy {
        projects: vec![Project {
            alias: "orion".into(),
            host: "p620".into(),
            cwd: "/provider-only/repo".into(),
        }],
        workspaces: vec![],
        provider_routes: Default::default(),
        endpoints: vec!["/fixture/provider.sock".into()],
        lease_ms: 120000,
        host_operations: vec![],
        derived_git_roots: vec![],
    };
    store.configure_business(&policy, true).unwrap();
    let mut n = 0;
    let mut call = |role, op: &str, payload: Value| {
        n += 1;
        store.business(role, &policy, op, &format!("probe-{n}"), &payload)
    };
    let payload = json!({"approved":true,"project":"orion","host":"p620","title":"native observer","prompt":"message"});
    let p = call(Role::Api, "prepare", payload.clone()).unwrap();
    assert_eq!(p["workspace_resolution"]["observer_uid"], 1001);
    let mut changed = actual.clone();
    changed["head"] = json!("b".repeat(40));
    changed["branch"] = json!("new-branch");
    fs::write(&response, changed.to_string()).unwrap();
    let e = call(
        Role::Api,
        "start",
        json!({"approved":true,"prepared_execution_ref":p["prepared_execution_ref"]}),
    )
    .unwrap();
    assert_eq!(e["workspace_resolution"]["head"], changed["head"]);
    let take = call(
        Role::Transport,
        "take_dispatch",
        json!({"execution_ref":e["execution_ref"]}),
    )
    .unwrap();
    let scope =
        json!({"execution_ref":e["execution_ref"],"epoch":take["epoch"],"ticket":take["ticket"]});
    let mut bind = scope.clone();
    bind["endpoint"] = json!("/fixture/provider.sock");
    bind["thread_id"] = json!("thread-native");
    bind["cwd"] = json!("/provider-only/repo");
    let bound = call(Role::Transport, "bind_thread", bind).unwrap();
    assert_eq!(bound["workspace_resolution"]["branch"], "new-branch");
    assert_eq!(
        call(Role::Transport, "authorize_send", scope.clone()).unwrap()["send_permitted"],
        true
    );
    let mut other = payload.clone();
    other["workspace_contract"] = json!({"cwd":"/provider-only/different"});
    assert_eq!(
        call(Role::Api, "prepare", other).unwrap_err(),
        "WORKSPACE_REPOSITORY_MISMATCH"
    );
    let mut missing = payload.clone();
    missing["workspace_contract"] = json!({"cwd":"/provider-only/missing"});
    assert_eq!(
        call(Role::Api, "prepare", missing).unwrap_err(),
        "WORKSPACE_CWD_UNREADABLE"
    );
    let adoption=call(Role::Api,"adopt",json!({"host":"p620","project":"orion","title":"adoption","thread_id":"adopted-native","workspace_contract":{"cwd":"/provider-only/repo"}})).unwrap();
    let challenge = call(
        Role::Transport,
        "adoption_begin",
        json!({"adoption_ref":adoption["adoption_ref"]}),
    )
    .unwrap();
    let adopted=call(Role::Transport,"adoption_commit",json!({"adoption_ref":adoption["adoption_ref"],"nonce":challenge["nonce"],"observations":[{"endpoint":"/fixture/provider.sock","thread":{"id":"adopted-native","cwd":"/provider-only/repo","status":{"type":"idle"}}}]})).unwrap();
    assert_eq!(adopted["adoption_status"], "ADOPTED");
    let continued=call(Role::Api,"prepare",json!({"approved":true,"task_ref":adopted["task_ref"],"project":"orion","host":"p620","title":"continue","prompt":"continue","workspace_contract":{"cwd":"/provider-only/repo"}})).unwrap();
    assert_eq!(
        continued["workspace_resolution"]["repository_identity"],
        actual["repository_identity"]
    );
    // A forged source, malformed HEAD and an unavailable probe cannot fall back.
    let mut bad = actual.clone();
    bad["observer_source"] = json!("API_CLAIM");
    fs::write(&response, bad.to_string()).unwrap();
    assert_eq!(
        call(Role::Api, "prepare", payload.clone()).unwrap_err(),
        "WORKSPACE_OBSERVER_INVALID"
    );
    let mut bad = actual.clone();
    bad["head"] = json!("not-a-head");
    fs::write(&response, bad.to_string()).unwrap();
    assert_eq!(
        call(Role::Api, "prepare", payload.clone()).unwrap_err(),
        "WORKSPACE_OBSERVER_INVALID"
    );
    fs::remove_file(&program).unwrap();
    assert_eq!(
        call(Role::Api, "prepare", payload).unwrap_err(),
        "WORKSPACE_OBSERVER_UNAVAILABLE"
    );
    // Historical reads and duplicate start keep their existing exact identities.
    assert_eq!(
        call(
            Role::Api,
            "get_status",
            json!({"execution_ref":e["execution_ref"]})
        )
        .unwrap()["execution_ref"],
        e["execution_ref"]
    );
    assert_eq!(
        call(
            Role::Api,
            "start",
            json!({"approved":true,"prepared_execution_ref":p["prepared_execution_ref"]})
        )
        .unwrap()["execution_ref"],
        e["execution_ref"]
    );
    store.verify_business().unwrap();
    fs::remove_dir_all(root).unwrap();
}
