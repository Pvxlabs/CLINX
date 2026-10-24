//! Node ownership and request receipts survive the same atomic transaction as
//! task admission; no network claim can submit Transport evidence.
use clinx_decision_kernel::{
    authority_store::Store,
    business::{Policy, Project, Role},
};
use serde_json::{Value, json};
use std::{fs, os::unix::fs::PermissionsExt, path::PathBuf};

fn policy() -> Policy {
    Policy {
        projects: vec![Project {
            alias: "fixture".into(),
            host: "air".into(),
            cwd: PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .to_string_lossy()
                .into(),
        }],
        workspaces: vec![],
        provider_routes: Default::default(),
        endpoints: vec!["/fixture/provider.sock".into()],
        lease_ms: 120000,
        host_operations: vec![],
        derived_git_roots: vec![],
    }
}
fn request(user: &str, id: &str, op: &str, p: Value) -> Value {
    json!({"scope":{"principal":{"node_id":"air","user_scope":user,"fingerprint":"paired-key"},
        "projects":[policy().projects[0].cwd]},"request_id":id,"operation":op,"payload":p})
}
fn call(s: &mut Store, r: &Value) -> Result<Value, String> {
    s.business(
        Role::Api,
        &policy(),
        "node_request",
        "wire-id-not-business-identity",
        r,
    )
}

#[test]
fn scoped_admission_cancel_receipts_restart_and_conflicts() {
    let root = std::env::temp_dir().join(format!("clinx-node-requests-{}", std::process::id()));
    fs::create_dir(&root).unwrap();
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    let mut s = Store::open(&root, true).unwrap();
    s.configure_business(&policy(), true).unwrap();
    let prepare = json!({"project":"fixture","host":"air","title":"isolated","prompt":"input","approved":true});
    let r = request("alice", "prepare", "prepare", prepare.clone());
    let p = call(&mut s, &r).unwrap();
    assert_eq!(call(&mut s, &r).unwrap(), p);
    let mut changed = r.clone();
    changed["payload"]["prompt"] = json!("other");
    assert_eq!(call(&mut s, &changed).unwrap_err(), "OPERATION_ID_CONFLICT");
    changed = r.clone();
    changed["scope"]["principal"]["fingerprint"] = json!("rotated");
    assert_eq!(call(&mut s, &changed).unwrap_err(), "OPERATION_ID_CONFLICT");
    changed = r.clone();
    changed["payload"]["host"] = json!("p620");
    assert_eq!(call(&mut s, &changed).unwrap_err(), "NODE_OWNER_CONFLICT");
    changed = r.clone();
    changed["scope"]["projects"] = json!(["/elsewhere"]);
    assert_eq!(
        call(&mut s, &changed).unwrap_err(),
        "NODE_PROJECT_SCOPE_DENIED"
    );
    let start = request(
        "alice",
        "start",
        "start",
        json!({"prepared_execution_ref":p["id"],"approved":true}),
    );
    let e = call(&mut s, &start).unwrap();
    let mut other = start.clone();
    other["scope"]["principal"]["user_scope"] = json!("bob");
    assert_eq!(call(&mut s, &other).unwrap_err(), "NODE_OWNER_CONFLICT");
    assert_eq!(
        call(
            &mut s,
            &request(
                "bob",
                "read",
                "read_receipt",
                json!({"operation_id":"start"})
            )
        )
        .unwrap()["verdict"],
        "NOT_FOUND"
    );
    assert_eq!(
        call(
            &mut s,
            &request(
                "bob",
                "status",
                "get_status",
                json!({"execution_ref":e["execution_ref"]})
            )
        )
        .unwrap_err(),
        "NODE_OWNER_CONFLICT"
    );
    assert!(
        call(&mut s, &request("bob", "tasks", "list_tasks", json!({}))).unwrap()["tasks"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    let list = request(
        "alice",
        "tasks",
        "list_tasks",
        json!({"projection":"summary","after":""}),
    );
    assert_eq!(
        call(&mut s, &list).unwrap()["tasks"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    // Independent tasks can be admitted concurrently in the same workspace.
    let second = call(&mut s, &request("alice", "prepare-2", "prepare", prepare)).unwrap();
    let second = call(
        &mut s,
        &request(
            "alice",
            "start-2",
            "start",
            json!({"prepared_execution_ref":second["id"],"approved":true}),
        ),
    )
    .unwrap();
    assert_ne!(e["execution_ref"], second["execution_ref"]);
    for op in [
        "observe_commit",
        "input_commit",
        "ack_outbox",
        "take_dispatch",
        "node_request",
    ] {
        assert_eq!(
            call(&mut s, &request("alice", "forged", op, json!({}))).unwrap_err(),
            "NODE_OPERATION_DENIED"
        );
    }
    // Lose the start reply, close/reopen the store, read back without another send.
    drop(s);
    let mut s = Store::open(&root, false).unwrap();
    s.configure_business(&policy(), false).unwrap();
    let receipt = call(
        &mut s,
        &request(
            "alice",
            "read",
            "read_receipt",
            json!({"operation_id":"start"}),
        ),
    )
    .unwrap();
    assert_eq!(receipt["verdict"], "COMMITTED");
    assert_eq!(receipt["result"], e);
    assert_eq!(call(&mut s, &start).unwrap(), e);
    let cancel = request(
        "alice",
        "cancel",
        "cancel",
        json!({"execution_ref":e["execution_ref"],"approved":true}),
    );
    let cancelled = call(&mut s, &cancel).unwrap();
    assert_eq!(cancelled["status"], "CANCELLED");
    assert_eq!(call(&mut s, &cancel).unwrap(), cancelled);
    s.verify_business().unwrap();
    drop(s);
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn node_owner_and_receipt_commit_together_at_fault_boundaries() {
    for point in ["BEFORE_BUSINESS_COMMIT", "AFTER_BUSINESS_COMMIT"] {
        let root =
            std::env::temp_dir().join(format!("clinx-node-fault-{}-{point}", std::process::id()));
        fs::create_dir(&root).unwrap();
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
        let mut s = Store::open(&root, true).unwrap();
        s.configure_business(&policy(), true).unwrap();
        let r = request(
            "alice",
            "prepare",
            "prepare",
            json!({"project":"fixture","host":"air","title":"fault","prompt":"input","approved":true}),
        );
        assert_eq!(
            s.business_at(Role::Api, &policy(), "node_request", "wire", &r, |at| {
                if at == point {
                    Err("LOST_REPLY".into())
                } else {
                    Ok(())
                }
            })
            .unwrap_err(),
            "LOST_REPLY"
        );
        drop(s);
        let mut s = Store::open(&root, false).unwrap();
        s.configure_business(&policy(), false).unwrap();
        let receipt = call(
            &mut s,
            &request(
                "alice",
                "read",
                "read_receipt",
                json!({"operation_id":"prepare"}),
            ),
        )
        .unwrap();
        assert_eq!(
            receipt["verdict"],
            if point.starts_with("BEFORE") {
                "NOT_FOUND"
            } else {
                "COMMITTED"
            }
        );
        let p = call(&mut s, &r).unwrap();
        assert_eq!(p["node_principal"]["user_scope"], "alice");
        s.verify_business().unwrap();
        drop(s);
        fs::remove_dir_all(root).unwrap();
    }
}
