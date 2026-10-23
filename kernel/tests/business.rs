//! Real SIGKILL across admission's lease/core/preparation transaction boundary.
use clinx_decision_kernel::{
    authority_store::Store,
    business::{Policy, Project, Role},
};
use serde_json::{Value, json};
use std::{
    fs,
    os::unix::{fs::PermissionsExt, process::ExitStatusExt},
    path::{Path, PathBuf},
    process::Command,
};
fn policy() -> Policy {
    Policy {
        derived_git_roots: vec![],
        workspaces: vec![],
        projects: vec![Project {
            alias: "clinx".into(),
            host: "p620".into(),
            cwd: PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .to_string_lossy()
                .into(),
        }],
        endpoints: vec!["/fixture/provider.sock".into()],
        lease_ms: 120000,
        host_operations: vec![],
    }
}
#[test]
fn business_crash_child() {
    let Ok(dir) = std::env::var("CLINX_BUSINESS_CRASH_DIR") else {
        return;
    };
    let point = std::env::var("CLINX_BUSINESS_CRASH_POINT").unwrap();
    let mut store = Store::open(Path::new(&dir), false).unwrap();
    store.configure_business(&policy(), false).unwrap();
    let p: Value =
        serde_json::from_slice(&fs::read(Path::new(&dir).join("input.json")).unwrap()).unwrap();
    let operation = std::env::var("CLINX_BUSINESS_CRASH_ACTION").unwrap_or_else(|_| "start".into());
    store
        .business_at(Role::Api, &policy(), &operation, &operation, &p, |at| {
            if at == point {
                rustix::process::kill_process(
                    rustix::process::getpid(),
                    rustix::process::Signal::KILL,
                )
                .unwrap();
            }
            Ok(())
        })
        .unwrap();
    panic!("crash boundary not reached");
}
#[test]
fn business_sigkill_and_role_boundary() {
    for point in [
        "BEFORE_COMMIT_ROWS",
        "AFTER_COMMIT_ROWS",
        "BEFORE_BUSINESS_COMMIT",
        "AFTER_BUSINESS_COMMIT",
    ] {
        let dir = std::env::temp_dir().join(format!(
            "clinx-business-crash-{}-{point}",
            std::process::id()
        ));
        fs::create_dir(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
        let mut store = Store::open(&dir, true).unwrap();
        store.configure_business(&policy(), true).unwrap();
        assert_eq!(
            store
                .business(
                    Role::Api,
                    &policy(),
                    "take_dispatch",
                    "forged",
                    &json!({"execution_ref":"x"})
                )
                .unwrap_err(),
            "TRANSPORT_PEER_REQUIRED"
        );
        let p=store.business(Role::Api,&policy(),"prepare","prepare",&json!({"prompt":"fixture","title":"fixture","host":"p620","project":"clinx","approved":true})).unwrap();
        let start = json!({"prepared_execution_ref":p["prepared_execution_ref"],"approved":true});
        fs::write(dir.join("input.json"), serde_json::to_vec(&start).unwrap()).unwrap();
        drop(store);
        let status = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "business_crash_child", "--nocapture"])
            .env("CLINX_BUSINESS_CRASH_DIR", &dir)
            .env("CLINX_BUSINESS_CRASH_POINT", point)
            .status()
            .unwrap();
        assert_eq!(status.signal(), Some(9));
        let mut store = Store::open(&dir, false).unwrap();
        store.configure_business(&policy(), false).unwrap();
        let receipt = store
            .business(
                Role::Api,
                &policy(),
                "read_receipt",
                "read",
                &json!({"operation_id":"start"}),
            )
            .unwrap();
        assert_eq!(
            receipt["verdict"],
            if point == "AFTER_BUSINESS_COMMIT" {
                "COMMITTED"
            } else {
                "NOT_FOUND"
            }
        );
        let applied = store
            .business(Role::Api, &policy(), "start", "start", &start)
            .unwrap();
        assert_eq!(applied["status"], "ADMITTED");
        store.verify_business().unwrap();
        drop(store);
        fs::remove_dir_all(dir).unwrap();
    }
}

#[test]
fn presend_closure_sigkill_atomicity() {
    for point in [
        "BEFORE_COMMIT_ROWS",
        "AFTER_COMMIT_ROWS",
        "BEFORE_BUSINESS_COMMIT",
        "AFTER_BUSINESS_COMMIT",
    ] {
        let dir = std::env::temp_dir().join(format!(
            "clinx-presend-crash-{}-{point}",
            std::process::id()
        ));
        fs::create_dir(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
        let mut store = Store::open(&dir, true).unwrap();
        store.configure_business(&policy(), true).unwrap();
        let prepared=store.business(Role::Api,&policy(),"prepare","prepare",&json!({"prompt":"no send","title":"no send","host":"p620","project":"clinx","approved":true})).unwrap();
        let e=store.business(Role::Api,&policy(),"start","start",&json!({"prepared_execution_ref":prepared["prepared_execution_ref"],"approved":true})).unwrap();
        let p = json!({"execution_ref":e["execution_ref"],"approved":true});
        fs::write(dir.join("input.json"), serde_json::to_vec(&p).unwrap()).unwrap();
        drop(store);
        let status = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "business_crash_child", "--nocapture"])
            .env("CLINX_BUSINESS_CRASH_DIR", &dir)
            .env("CLINX_BUSINESS_CRASH_POINT", point)
            .env("CLINX_BUSINESS_CRASH_ACTION", "cancel")
            .status()
            .unwrap();
        assert_eq!(status.signal(), Some(9));
        let mut store = Store::open(&dir, false).unwrap();
        store.configure_business(&policy(), false).unwrap();
        let receipt = store
            .business(
                Role::Api,
                &policy(),
                "read_receipt",
                "read",
                &json!({"operation_id":"cancel"}),
            )
            .unwrap();
        assert_eq!(
            receipt["verdict"],
            if point == "AFTER_BUSINESS_COMMIT" {
                "COMMITTED"
            } else {
                "NOT_FOUND"
            }
        );
        let current = store
            .business(
                Role::Api,
                &policy(),
                "get_status",
                "status",
                &json!({"execution_ref":e["execution_ref"]}),
            )
            .unwrap();
        assert_eq!(
            current["resource_state"],
            if point == "AFTER_BUSINESS_COMMIT" {
                "RELEASED"
            } else {
                "HELD"
            }
        );
        let closed = store
            .business(Role::Api, &policy(), "cancel", "cancel", &p)
            .unwrap();
        assert_eq!(closed["resource_state"], "RELEASED");
        store.verify_business().unwrap();
        drop(store);
        fs::remove_dir_all(dir).unwrap();
    }
}
