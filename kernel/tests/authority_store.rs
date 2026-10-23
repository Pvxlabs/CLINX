//! Actual process-kill matrix at SQLite bundle boundaries; no production state.
use clinx_decision_kernel::{
    authority_store::{PROTOCOL, Store},
    execution_core::digest,
};
use serde_json::{Value, json};
use std::{
    fs,
    os::unix::{fs::PermissionsExt, process::ExitStatusExt},
    path::Path,
    process::Command,
};

fn admission() -> Value {
    let r = json!({"object":"Execution","id":"e1","from":"REQUESTED","to":"ADMITTED","action":"admit","actor":"command","version":0,"next_version":1,"facts":{"commit_bundle":["execution","request","prepared","owner","event","receipt","outbox"]}});
    json!({"event_id":"admit","family":"CLINX_EXECUTION_CORE_V1","contract_version":"1.0.0","schema_version":1,"aggregate_type":"Execution","aggregate_id":"e1","sequence":1,"aggregate_version":1,"actor":"command","causation_id":"cmd","correlation_id":"e1","transaction_id":"tx1","idempotency_scope":"e1","idempotency_key":"admit","payload_hash":digest(&r),"payload":r,"occurred_at":"2026-10-22T00:00:00.000000Z","recorded_at":"2026-10-22T00:00:01.000000Z"})
}

#[test]
fn crash_child() {
    let Ok(path) = std::env::var("CLINX_STORE_CRASH_TEST_DIRECTORY") else {
        return;
    };
    let point = std::env::var("CLINX_STORE_CRASH_TEST_POINT").unwrap();
    let request: Value =
        serde_json::from_slice(&fs::read(Path::new(&path).join("test-input.json")).unwrap())
            .unwrap();
    let mut store = Store::open(Path::new(&path), false).unwrap();
    let _ = store.execute_at(&request, |at| {
        if at == point {
            rustix::process::kill_process(rustix::process::getpid(), rustix::process::Signal::KILL)
                .unwrap();
            unreachable!();
        }
        Ok(())
    });
    panic!("crash boundary was not reached");
}

#[test]
fn sigkill_rolls_back_or_preserves_entire_native_bundle() {
    for point in [
        "BEFORE_COMMIT_ROWS",
        "AFTER_COMMIT_ROWS",
        "BEFORE_SQL_COMMIT",
        "AFTER_SQL_COMMIT",
    ] {
        let directory =
            std::env::temp_dir().join(format!("clinx-rust-crash-{}-{point}", std::process::id()));
        fs::create_dir(&directory).unwrap();
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
        let mut store = Store::open(&directory, true).unwrap();
        let lease = store.claim(&json!({"resource":"r1","expected_epoch":0,"claim_id":"c1","lease_ms":120000,
            "owner":{"execution_id":"e1","attempt_id":"a1","assignment_id":"as1","worker_id":"w1","incarnation_id":"i1","resource_key":"r1","resource_epoch":1}})).unwrap();
        let request = json!({"resource":"r1","epoch":1,"capability":lease["capability"],"command":{"protocol_version":PROTOCOL,"request_id":"admit","operation":"append","payload":{"events":[admission()]}}});
        fs::write(
            directory.join("test-input.json"),
            serde_json::to_vec(&request).unwrap(),
        )
        .unwrap();
        drop(store);
        let status = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "crash_child", "--nocapture"])
            .env("CLINX_STORE_CRASH_TEST_DIRECTORY", &directory)
            .env("CLINX_STORE_CRASH_TEST_POINT", point)
            .status()
            .unwrap();
        assert_eq!(status.signal(), Some(9));
        let mut store = Store::open(&directory, false).unwrap();
        let read = json!({"resource":"r1","epoch":1,"capability":lease["capability"],"operation_id":"admit"});
        let prior = store.read_commit(&read).unwrap();
        assert_eq!(
            prior["verdict"],
            if point == "AFTER_SQL_COMMIT" {
                "COMMITTED"
            } else {
                "NOT_FOUND"
            }
        );
        let retry = store.execute(&request).unwrap();
        assert_eq!(
            retry["verdict"],
            if point == "AFTER_SQL_COMMIT" {
                "DUPLICATE"
            } else {
                "COMMITTED"
            }
        );
        assert_eq!(
            store.read_commit(&read).unwrap()["receipt"],
            retry["receipt"]
        );
        drop(store);
        // Reopen performs full request/result/receipt/projection/outbox validation.
        drop(Store::open(&directory, false).unwrap());
        fs::remove_dir_all(directory).unwrap();
    }
}
