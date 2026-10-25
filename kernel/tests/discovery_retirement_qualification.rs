//! Product invariants formerly exercised through the retired DEV SPAKE2 server.
//! Exercise the current Rust OPAQUE/TLS contract via real independent processes.
#[path = "support/node_qualification.rs"]
mod support;
use serde_json::{Value, json};
use std::{
    path::Path,
    thread,
    time::{Duration, Instant},
};
use support::Worker;

fn open(w: &Worker, root: &Path, host: &str) -> Value {
    w.call("discovery.identity.open", json!({"root":root,"host":host}))
        .unwrap()
}
fn server(w: &Worker, root: &Path, address: &str) -> Value {
    w.call(
        "discovery.enrollment.server.start",
        json!({"root":root,"address":address,"port":0}),
    )
    .unwrap()
}
fn candidate(identity: &Value, server: &Value, address: &str) -> Value {
    let p = &identity["public"];
    json!({"node_id":p["node_id"],"display_name":p["display_name"],"fingerprint":p["fingerprint"],"endpoints":[[address,server["port"]]],"mismatch":false})
}
fn pair(client: &Worker, owner: &Worker, root: &Path, server: &Value, target: &Value) -> Value {
    let code = owner
        .call(
            "discovery.window.open",
            json!({"window":server["window"],"lifetime":60}),
        )
        .unwrap();
    client
        .call(
            "discovery.enrollment.client.pair",
            json!({"root":root,"candidate":target,"code":code}),
        )
        .unwrap()
}
#[test]
fn anonymous_tls_cannot_reconnect_or_dispatch_and_does_not_revoke_trust() {
    let t = tempfile::tempdir().unwrap();
    let a = t.path().join("alpha");
    let b = t.path().join("beta");
    let client = Worker::plain();
    let owner = Worker::plain();
    open(&client, &a, "alpha");
    let identity = open(&owner, &b, "beta");
    let s = server(&owner, &b, "127.0.0.1");
    let target = candidate(&identity, &s, "127.0.0.1");
    pair(&client, &owner, &a, &s, &target);
    for request in [
        json!({"op":"session","node_id":"alpha"}),
        json!({"op":"host_execute","capability":"GIT"}),
        json!({"op":"terminal"}),
        json!({"op":"production_mutation"}),
    ] {
        let connection = client
            .call(
                "discovery.enrollment.client.connect",
                json!({"root":a,"candidate":target,"pairing":true}),
            )
            .unwrap();
        client
            .call(
                "discovery.enrollment.client.send",
                json!({"connection":connection,"message":request}),
            )
            .unwrap();
        assert_eq!(
            client
                .call(
                    "discovery.enrollment.client.receive",
                    json!({"connection":connection})
                )
                .unwrap_err(),
            "REMOTE_REQUEST_REJECTED"
        );
        client
            .call(
                "discovery.enrollment.client.close",
                json!({"connection":connection}),
            )
            .unwrap();
    }
    assert_eq!(
        owner
            .call(
                "discovery.store.read",
                json!({"root":b,"name":"peers.json"})
            )
            .unwrap()["peers"]["alpha"]["trust_state"],
        "TRUSTED"
    );
}
#[test]
fn duplicate_server_process_cannot_acquire_the_same_identity_lease() {
    let t = tempfile::tempdir().unwrap();
    let root = t.path().join("beta");
    let first = Worker::plain();
    open(&first, &root, "beta");
    server(&first, &root, "127.0.0.1");
    let second = Worker::plain();
    assert_eq!(
        second
            .call(
                "discovery.enrollment.server.start",
                json!({"root":root,"address":"127.0.0.1","port":0})
            )
            .unwrap_err(),
        "DEVICE_ALREADY_SERVING"
    );
}
#[test]
fn owner_process_restart_and_address_change_preserve_trust_without_new_pin() {
    let t = tempfile::tempdir().unwrap();
    let a = t.path().join("alpha");
    let b = t.path().join("beta");
    let client = Worker::plain();
    let owner = Worker::plain();
    open(&client, &a, "alpha");
    let original = open(&owner, &b, "beta");
    let s = server(&owner, &b, "127.0.0.1");
    let session = pair(
        &client,
        &owner,
        &a,
        &s,
        &candidate(&original, &s, "127.0.0.1"),
    );
    assert_eq!(session["node_id"], "beta");
    assert_eq!(session["capabilities"], json!([]));
    assert_eq!(session["authority_granted"], false);
    assert_eq!(session["authorization_required"], true);
    drop(owner); // Wait for actual worker exit and OS revocation of its listener/lock FDs.
    let replacement = Worker::plain();
    let restored = open(&replacement, &b, "beta");
    assert_eq!(restored, original);
    let s = server(&replacement, &b, "127.0.0.2");
    let target = candidate(&restored, &s, "127.0.0.2");
    assert_eq!(
        client
            .call(
                "discovery.enrollment.client.reconnect",
                json!({"root":a,"candidate":target})
            )
            .unwrap(),
        session
    );
    assert_eq!(
        replacement
            .call("discovery.window.reserve", json!({"window":s["window"]}))
            .unwrap_err(),
        "PAIRING_CLOSED"
    );
}
#[test]
fn disconnected_opaque_attempt_consumes_failure_without_persisting_trust() {
    let t = tempfile::tempdir().unwrap();
    let a = t.path().join("alpha");
    let b = t.path().join("beta");
    let client = Worker::plain();
    let owner = Worker::plain();
    let identity = open(&client, &a, "alpha");
    let remote = open(&owner, &b, "beta");
    let s = server(&owner, &b, "127.0.0.1");
    let target = candidate(&remote, &s, "127.0.0.1");
    owner
        .call(
            "discovery.window.open",
            json!({"window":s["window"],"lifetime":60}),
        )
        .unwrap();
    let connection = client
        .call(
            "discovery.enrollment.client.connect",
            json!({"root":a,"candidate":target,"pairing":true}),
        )
        .unwrap();
    client.call("discovery.enrollment.client.send",json!({"connection":connection,"message":{"op":"pair","protocol":"OPAQUE-3DH-RISTRETTO255-SHA512-ARGON2I-v1","identity":identity["public"],"certificate":identity["certificate"],"client_nonce":"0123456789abcdef0123456789abcdef"}})).unwrap();
    let hello = client
        .call(
            "discovery.enrollment.client.receive",
            json!({"connection":connection}),
        )
        .unwrap();
    assert!(hello["session_id"].is_string());
    client
        .call(
            "discovery.enrollment.client.close",
            json!({"connection":connection}),
        )
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    let snapshot = loop {
        let snapshot = owner
            .call("discovery.window.snapshot", json!({"window":s["window"]}))
            .unwrap();
        if snapshot["failures"] != 0 || Instant::now() >= deadline {
            break snapshot;
        }
        thread::sleep(Duration::from_millis(10));
    };
    assert_eq!(snapshot["failures"], 1);
    let peers = owner
        .call(
            "discovery.store.read",
            json!({"root":b,"name":"peers.json"}),
        )
        .unwrap();
    assert!(peers.is_null() || peers["peers"] == json!({}));
}
