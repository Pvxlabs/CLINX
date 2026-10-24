//! Host routing cannot reuse a different device's cwd or Provider evidence.
use clinx_decision_kernel::{
    authority_store::Store,
    business::{Policy, Project, Role, workspace_observer},
};
use serde_json::{Value, json};
use std::{collections::BTreeMap, fs, os::unix::fs::PermissionsExt};
#[test]
fn workspace_and_provider_evidence_follow_explicit_host_routes() {
    let root = std::env::temp_dir().join(format!("clinx-workspace-routes-{}", std::process::id()));
    fs::create_dir(&root).unwrap();
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    let program = root.join("probe.py");
    fs::write(
        &program,
        r#"import json,sys
r=json.loads(sys.argv[2]);endpoint=sys.argv[1]
assert endpoint=='/fixture/'+r['host']+'.sock'
print(json.dumps(dict(observer_source='CODEX_PROVIDER_COMMAND_EXEC',observer_uid=501,
 repository_identity='git-common:9:'+('42' if r['host']=='air' else '99'),cwd=r['cwd'],
 repository_common_dir=r['cwd']+'/.git',repository_root=r['cwd'],worktree=r['cwd']+'/.git',
 branch='main',head='a'*40)))
"#,
    )
    .unwrap();
    workspace_observer::configure_routes(
        program,
        BTreeMap::from([
            ("air".into(), "/fixture/air.sock".into()),
            ("imac".into(), "/fixture/imac.sock".into()),
        ]),
    )
    .unwrap();
    let mut policy = Policy {
        projects: vec!["air", "imac"]
            .into_iter()
            .map(|h| Project {
                alias: "fixture".into(),
                host: h.into(),
                cwd: "/only-on-mac/repository".into(),
            })
            .collect(),
        workspaces: vec![],
        endpoints: vec!["/fixture/air.sock".into(), "/fixture/imac.sock".into()],
        provider_routes: BTreeMap::from([
            ("air".into(), vec!["/fixture/air.sock".into()]),
            ("imac".into(), vec!["/fixture/imac.sock".into()]),
        ]),
        lease_ms: 120000,
        host_operations: vec![],
        derived_git_roots: vec![],
    };
    let mut s = Store::open(&root, true).unwrap();
    s.configure_business(&policy, true).unwrap();
    let mut n = 0;
    let mut call = |role, op: &str, p: Value| {
        n += 1;
        s.business(role, &policy, op, &format!("route-{n}"), &p)
    };
    let mut prepared = vec![];
    for host in ["air", "imac"] {
        let p=call(Role::Api,"prepare",json!({"host":host,"project":"fixture","title":"route","prompt":"intent","approved":true})).unwrap();
        assert_eq!(
            p["workspace_resolution"]["repository_identity"],
            if host == "air" {
                "git-common:9:42"
            } else {
                "git-common:9:99"
            }
        );
        prepared.push(p.clone());
        let e = call(
            Role::Api,
            "start",
            json!({"prepared_execution_ref":p["id"],"approved":true}),
        )
        .unwrap();
        let take = call(
            Role::Transport,
            "take_dispatch",
            json!({"execution_ref":e["execution_ref"]}),
        )
        .unwrap();
        assert_eq!(take["endpoints"], json!([format!("/fixture/{host}.sock")]));
        let mut bind = json!({"execution_ref":e["execution_ref"],"epoch":take["epoch"],"ticket":take["ticket"],"thread_id":"same-id-on-two-devices","cwd":"/only-on-mac/repository","endpoint":"/fixture/wrong.sock"});
        assert_eq!(
            call(Role::Transport, "bind_thread", bind.clone()).unwrap_err(),
            "BINDING_CONFLICT"
        );
        bind["endpoint"] = json!(format!("/fixture/{host}.sock"));
        call(Role::Transport, "bind_thread", bind).unwrap();
        let scope = json!({"execution_ref":e["execution_ref"],"epoch":take["epoch"],"ticket":take["ticket"]});
        call(Role::Transport, "authorize_send", scope).unwrap();
        let recovery = call(
            Role::Transport,
            "input_begin",
            json!({"execution_ref":e["execution_ref"]}),
        )
        .unwrap();
        call(Role::Transport,"input_commit",json!({"execution_ref":e["execution_ref"],"nonce":recovery["nonce"],"endpoint":recovery["endpoint"],"thread_id":"same-id-on-two-devices","turn_id":"same-turn-id","item":{"id":"input-item","type":"userMessage","clientId":recovery["client_message_id"],"content":[{"type":"text","text":"intent"}]}})).unwrap();
        let observation = call(
            Role::Transport,
            "observe_begin",
            json!({"execution_ref":e["execution_ref"]}),
        )
        .unwrap();
        assert_eq!(
            observation["endpoints"],
            json!([format!("/fixture/{host}.sock")])
        );
        let mut evidence = json!({"execution_ref":e["execution_ref"],"nonce":observation["nonce"],"observations":[{"endpoint":format!("/fixture/{}.sock",if host=="air" {"imac"} else {"air"}),"state":"idle","thread_id":"same-id-on-two-devices","turn_id":"same-turn-id","turn_status":"completed"}],"turn":{"id":"same-turn-id","status":"completed","items":[{"type":"agentMessage","phase":"final_answer","text":"exact response"}]}});
        assert_eq!(
            call(Role::Transport, "observe_commit", evidence.clone()).unwrap_err(),
            "OBSERVATION_ENDPOINT_SET_MISMATCH"
        );
        evidence["observations"][0]["endpoint"] = json!(format!("/fixture/{host}.sock"));
        let terminal = call(Role::Transport, "observe_commit", evidence).unwrap();
        assert_eq!(terminal["status"], "SUCCEEDED");
        assert_eq!(terminal["resource_state"], "RELEASED");
    }
    assert_ne!(
        prepared[0]["workspace_resolution"]["repository_identity"],
        prepared[1]["workspace_resolution"]["repository_identity"]
    );
    for p in &prepared {
        call(Role::Api,"prepare",json!({"host":p["host"],"project":"fixture","task_ref":p["task_ref"],"thread_id":"same-id-on-two-devices","title":"continue","prompt":"continue","approved":true})).unwrap();
    }
    // Policy coverage rejects missing hosts; an explicit wrong route cannot be silently used.
    drop(call);
    policy.provider_routes.remove("imac");
    assert_eq!(
        s.configure_business(&policy, false).unwrap_err(),
        "PROVIDER_ROUTE_REQUIRED"
    );
    s.verify_business().unwrap();
    drop(s);
    fs::remove_dir_all(root).unwrap();
}
