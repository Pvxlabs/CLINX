//! Offline routing changes retain the existing writer, command chain and tasks.
use clinx_decision_kernel::{
    authority_store::Store,
    business::{Policy, Role},
    execution_core,
};
use serde_json::{Value, json};
use std::{fs, os::unix::fs::PermissionsExt, path::Path, process::Command};

fn fixture() -> (tempfile::TempDir, Store, Policy, Policy) {
    let d = tempfile::Builder::new()
        .permissions(fs::Permissions::from_mode(0o700))
        .tempdir()
        .unwrap();
    let root = d.path().canonicalize().unwrap();
    let old: Policy = serde_json::from_value(json!({"projects":[{"alias":"clinx","host":"p620","cwd":Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap()}],"endpoints":["/old.sock"],"lease_ms":120000,"host_operations":[],"derived_git_roots":[]})).unwrap();
    let mut v = serde_json::to_value(&old).unwrap();
    v["projects"]
        .as_array_mut()
        .unwrap()
        .push(json!({"alias":"clinx","host":"air","cwd":"/only-on-air/clinx"}));
    v["endpoints"] = json!(["/old.sock", "/air.sock"]);
    v["provider_routes"] = json!({"p620":["/old.sock"],"air":["/air.sock"]});
    let next = serde_json::from_value(v).unwrap();
    let s = Store::open(&root, true).unwrap();
    s.configure_business(&old, true).unwrap();
    (d, s, old, next)
}
fn hash(p: &Policy) -> String {
    execution_core::digest(&serde_json::to_value(p).unwrap())
}
fn count(d: &Path) -> i64 {
    rusqlite::Connection::open_with_flags(
        d.join("authority.sqlite3"),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .unwrap()
    .query_row("SELECT count(*) FROM business_commands", [], |r| r.get(0))
    .unwrap()
}
#[test]
fn policy_and_receipt_survive_reopen_and_duplicate_never_appends_or_rolls_back() {
    let (d, mut s, old, next) = fixture();
    let id = s.identity.clone();
    assert_eq!(
        Store::open(&d.path().canonicalize().unwrap(), false)
            .err()
            .unwrap(),
        "WRITER_ALREADY_ACTIVE"
    );
    let receipt = s
        .extend_business_policy(&next, &id, &hash(&old), "add-air")
        .unwrap();
    assert_eq!(receipt["before_hash"], hash(&old));
    assert_eq!(receipt["after_hash"], hash(&next));
    assert_eq!(count(d.path()), 1);
    s.configure_business(&next, false).unwrap();
    assert_eq!(
        s.configure_business(&old, false).unwrap_err(),
        "AUTHORITY_POLICY_MISMATCH"
    );
    drop(s);
    let mut s = Store::open(&d.path().canonicalize().unwrap(), false).unwrap();
    assert_eq!(
        s.extend_business_policy(&next, &id, &hash(&old), "add-air")
            .unwrap(),
        receipt
    );
    assert_eq!(count(d.path()), 1);
    assert_eq!(
        s.extend_business_policy(&next, &id, "wrong", "add-air")
            .unwrap_err(),
        "POLICY_OPERATION_CONFLICT"
    );
    s.verify_business().unwrap();
}
#[test]
fn wrong_authority_stale_cas_and_nonadditive_changes_leave_policy_unchanged() {
    let (d, mut s, old, next) = fixture();
    let id = s.identity.clone();
    assert_eq!(
        s.extend_business_policy(&next, "other-authority", &hash(&old), "bad")
            .unwrap_err(),
        "POLICY_AUTHORITY_CONFLICT"
    );
    assert_eq!(
        s.extend_business_policy(&next, &id, "stale", "bad")
            .unwrap_err(),
        "POLICY_CAS_CONFLICT"
    );
    for mutate in 0..5 {
        let mut p = serde_json::to_value(&next).unwrap();
        match mutate {
            0 => p["projects"][0]["cwd"] = json!("/moved"),
            1 => p["lease_ms"] = json!(1000),
            2 => p["derived_git_roots"] = json!(["/"]),
            3 => p["host_operations"] = json!([{"capability":"NEW"}]),
            _ => p["provider_routes"] = json!({"p620":["/air.sock"],"air":["/old.sock"]}),
        }
        let p = serde_json::from_value(p).unwrap();
        assert!(
            s.extend_business_policy(&p, &id, &hash(&old), "bad")
                .is_err()
        );
        s.configure_business(&old, false).unwrap();
        assert_eq!(count(d.path()), 0);
    }
}
#[test]
fn active_execution_blocks_extension_then_terminal_cancel_preserves_rows() {
    let (d, mut s, old, next) = fixture();
    let id = s.identity.clone();
    let p = s.business(Role::Api,&old,"prepare","prepare",&json!({"prompt":"fixture","title":"fixture","host":"p620","project":"clinx","approved":true})).unwrap();
    let e = s
        .business(
            Role::Api,
            &old,
            "start",
            "start",
            &json!({"prepared_execution_ref":p["prepared_execution_ref"],"approved":true}),
        )
        .unwrap();
    assert_eq!(
        s.extend_business_policy(&next, &id, &hash(&old), "add-air")
            .unwrap_err(),
        "POLICY_ACTIVE_EXECUTIONS_OR_LEASES"
    );
    let cancel = s
        .business(
            Role::Api,
            &old,
            "cancel",
            "cancel",
            &json!({"execution_ref":e["execution_ref"],"approved":true}),
        )
        .unwrap();
    assert_eq!(cancel["resource_state"], "RELEASED");
    let before = count(d.path());
    s.extend_business_policy(&next, &id, &hash(&old), "add-air")
        .unwrap();
    assert_eq!(count(d.path()), before + 1);
    let after = s
        .business(
            Role::Api,
            &next,
            "get_status",
            "read",
            &json!({"execution_ref":e["execution_ref"]}),
        )
        .unwrap();
    assert_eq!(after["status"], cancel["status"]);
    assert_eq!(after["thread_id"], Value::Null);
    s.verify_business().unwrap();
}
#[test]
fn transaction_failure_rolls_back_and_unknown_ack_has_exact_receipt() {
    for point in ["BEFORE_POLICY_COMMIT", "AFTER_POLICY_COMMIT"] {
        let (d, mut s, old, next) = fixture();
        let id = s.identity.clone();
        assert_eq!(
            s.extend_business_policy_at(&next, &id, &hash(&old), "change", |at| if at == point {
                Err("INJECTED".into())
            } else {
                Ok(())
            })
            .unwrap_err(),
            "INJECTED"
        );
        drop(s);
        let mut s = Store::open(&d.path().canonicalize().unwrap(), false).unwrap();
        if point == "BEFORE_POLICY_COMMIT" {
            s.configure_business(&old, false).unwrap();
            assert_eq!(count(d.path()), 0);
        } else {
            s.configure_business(&next, false).unwrap();
            assert_eq!(count(d.path()), 1);
            let receipt = s
                .extend_business_policy(&next, &id, &hash(&old), "change")
                .unwrap();
            assert_eq!(receipt["after_hash"], hash(&next));
            assert_eq!(count(d.path()), 1);
        }
    }
}
#[test]
fn cli_requires_approval_and_owner_writer_lock() {
    let (d, s, old, next) = fixture();
    let policy = d.path().join("next.json");
    fs::write(&policy, serde_json::to_vec(&next).unwrap()).unwrap();
    let args = vec![
        "extend-business-policy".to_owned(),
        "--state".into(),
        d.path()
            .canonicalize()
            .unwrap()
            .to_string_lossy()
            .into_owned(),
        "--policy".into(),
        policy.to_string_lossy().into_owned(),
        "--authority-id".into(),
        s.identity.clone(),
        "--expected-policy-hash".into(),
        hash(&old),
        "--operation-id".into(),
        "cli".into(),
    ];
    let output = Command::new(env!("CARGO_BIN_EXE_clinx-maintenance"))
        .args(&args)
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stderr)
            .contains("APPROVED_EXACT_POLICY_ARGUMENTS_REQUIRED")
    );
    let output = Command::new(env!("CARGO_BIN_EXE_clinx-maintenance"))
        .args(&args)
        .arg("--approved")
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("WRITER_ALREADY_ACTIVE"));
    drop(s);
    let output = Command::new(env!("CARGO_BIN_EXE_clinx-maintenance"))
        .args(&args)
        .arg("--approved")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let receipt: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(receipt["status"], "POLICY_EXTENDED_NOT_ACTIVATED");
}
