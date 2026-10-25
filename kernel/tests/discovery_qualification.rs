//! Original Python discovery case/parameter migration, using private stores only.
use clinx_decision_kernel::{
    discovery_bootstrap as bootstrap, discovery_identity as identity, discovery_index as index,
    discovery_state::Runtime,
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    os::unix::fs::PermissionsExt,
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
};
fn cmd(op: &str, a: Value) -> Result<Value, String> {
    index::invoke(&format!("discovery_index.{op}"), &a)
}
fn hex(s: &[u8]) -> String {
    s.iter().map(|b| format!("{b:02x}")).collect()
}
fn props(node: &str, fingerprint: &str) -> Value {
    json!([
        [hex(b"node_id"), hex(node.as_bytes())],
        [
            hex(b"display_name"),
            hex((node[..1].to_uppercase() + &node[1..]).as_bytes())
        ],
        [hex(b"protocol_version"), "31"],
        [hex(b"fingerprint"), hex(fingerprint.as_bytes())]
    ])
}
#[derive(Default)]
struct Index {
    records: Value,
    now: f64,
}
impl Index {
    fn new() -> Self {
        Self {
            records: json!([]),
            now: 100.,
        }
    }
    fn update(
        &mut self,
        service: &str,
        properties: Value,
        addresses: Value,
        ttl: f64,
    ) -> Result<(), String> {
        self.records = cmd(
            "update",
            json!({"records":self.records,"now":self.now,"service":service,"properties":properties,"addresses":addresses,"port":9000,"ttl":ttl}),
        )?;
        Ok(())
    }
    fn candidates(&mut self) -> Value {
        let reply = cmd("candidates", json!({"records":self.records,"now":self.now})).unwrap();
        self.records = reply["records"].clone();
        reply["candidates"].clone()
    }
    fn remove(&mut self, service: &str) {
        self.records = cmd("remove", json!({"records":self.records,"service":service})).unwrap();
    }
}
macro_rules! address {($name:ident,$input:expr,$expected:expr)=>{#[test]fn $name(){assert_eq!(cmd("lan_address",json!({"value":$input})).unwrap(),$expected);}};}
macro_rules! bad_address {($name:ident,$input:expr,$error:expr)=>{#[test]fn $name(){assert_eq!(cmd("lan_address",json!({"value":$input})).unwrap_err(),$error);}};}
address!(lan_rfc10, "10.0.0.1", "10.0.0.1");
address!(lan_rfc172_first, "172.16.0.1", "172.16.0.1");
address!(lan_rfc172_last, "172.31.255.255", "172.31.255.255");
address!(lan_rfc192, "192.168.1.2", "192.168.1.2");
address!(lan_linklocal, "169.254.1.2", "169.254.1.2");
address!(lan_loopback, "127.0.0.1", "127.0.0.1");
bad_address!(lan_public, "8.8.8.8", "NON_LAN_ADDRESS");
bad_address!(lan_after_rfc172, "172.32.0.1", "NON_LAN_ADDRESS");
bad_address!(lan_documentation, "192.0.2.1", "NON_LAN_ADDRESS");
bad_address!(lan_ipv6_loopback, "::1", "NON_LAN_ADDRESS");
bad_address!(lan_scoped_ipv6, "fe80::1%eth0", "NON_LAN_ADDRESS");
bad_address!(lan_unspecified, "0.0.0.0", "NON_LAN_ADDRESS");
bad_address!(lan_leading_zero, "010.0.0.1", "INVALID_LAN_ADDRESS");
bad_address!(lan_invalid, "bogus", "INVALID_LAN_ADDRESS");
#[test]
fn candidate_ttl_conflict_goodbye_ip_change_and_capacity() {
    let mut i = Index::new();
    let a = "a".repeat(64);
    i.update("a1", props("alpha", &a), json!(["10.0.0.1"]), 30.)
        .unwrap();
    i.update("a2", props("alpha", &a), json!(["192.168.1.2"]), 20.)
        .unwrap();
    assert_eq!(
        i.candidates()["alpha"]["endpoints"],
        json!([["10.0.0.1", 9000], ["192.168.1.2", 9000]])
    );
    i.update("a1", props("alpha", &a), json!(["10.0.0.9"]), 30.)
        .unwrap();
    i.now += 21.;
    assert_eq!(
        i.candidates()["alpha"]["endpoints"],
        json!([["10.0.0.9", 9000]])
    );
    i.update(
        "imposter",
        props("alpha", &"b".repeat(64)),
        json!(["10.0.0.2"]),
        30.,
    )
    .unwrap();
    assert_eq!(i.candidates()["alpha"]["mismatch"], true);
    i.remove("imposter");
    assert_eq!(i.candidates()["alpha"]["mismatch"], false);
    i.update("a1", props("alpha", &a), json!(["10.0.0.9"]), 0.)
        .unwrap();
    assert_eq!(i.candidates(), json!({}));
    for n in 0..256 {
        i.update(
            &n.to_string(),
            props(&format!("node{n}"), &a),
            json!(["10.0.0.1"]),
            1000.,
        )
        .unwrap();
    }
    assert_eq!(
        i.update("overflow", props("overflow", &a), json!(["10.0.0.1"]), 30.)
            .unwrap_err(),
        "DISCOVERY_CAPACITY"
    );
    i.now += 121.;
    assert_eq!(i.candidates(), json!({}));
}
fn bad_metadata(key: &str, value: &[u8], expected: &str) {
    let mut p = props("alpha", &"a".repeat(64));
    let rows = p.as_array_mut().unwrap();
    let key = hex(key.as_bytes());
    if let Some(row) = rows.iter_mut().find(|r| r[0] == key) {
        row[1] = json!(hex(value));
    } else {
        rows.push(json!([key, hex(value)]));
    }
    assert_eq!(
        Index::new()
            .update("fixture", p, json!(["10.0.0.1"]), 30.)
            .unwrap_err(),
        expected
    );
}
#[test]
fn metadata_unknown_secret() {
    bad_metadata("password", b"never", "INVALID_DISCOVERY_METADATA");
}
#[test]
fn metadata_oversize_name() {
    bad_metadata("display_name", &[b'x'; 201], "INVALID_DISCOVERY_METADATA");
}
#[test]
fn metadata_newline_name() {
    bad_metadata("display_name", b"bad\nname", "INVALID_DISPLAY_NAME");
}
#[test]
fn metadata_protocol() {
    bad_metadata("protocol_version", b"2", "UNSUPPORTED_DISCOVERY_PROTOCOL");
}
#[test]
fn metadata_fingerprint() {
    bad_metadata("fingerprint", &[b'G'; 64], "INVALID_DISCOVERY_FINGERPRINT");
}
#[test]
fn metadata_address_identity() {
    bad_metadata("node_id", b"10.0.0.1", "ADDRESS_IS_NOT_IDENTITY");
}
#[test]
fn metadata_unknown_identity() {
    bad_metadata("node_id", b"unknown", "NON_CANONICAL_NODE_ID");
}
#[test]
fn concurrent_discovery_updates_and_untrusted_device_projection() {
    let shared = Arc::new(Mutex::new(Index::new()));
    std::thread::scope(|scope| {
        for worker in 0..8 {
            let shared = shared.clone();
            scope.spawn(move || {
                for n in (worker..40).step_by(8) {
                    shared
                        .lock()
                        .unwrap()
                        .update(
                            &format!("s{n}"),
                            props(&format!("node{n}"), &"a".repeat(64)),
                            json!(["10.0.0.1"]),
                            30.,
                        )
                        .unwrap();
                }
            });
        }
    });
    assert_eq!(
        shared
            .lock()
            .unwrap()
            .candidates()
            .as_object()
            .unwrap()
            .len(),
        40
    );
    let identity = json!({"node_id":"alpha","display_name":"Alpha","fingerprint":"a".repeat(64)});
    let trusted = json!({"beta":{"friendly_name":"Beta","fingerprint":"b".repeat(64),"trust_state":"TRUSTED","security_status":"OPAQUE_V1"}});
    let mut i = Index::new();
    let rows = cmd(
        "device_rows",
        json!({"identity":identity,"trusted":trusted,"online":i.candidates()}),
    )
    .unwrap();
    assert_eq!(
        rows.as_array()
            .unwrap()
            .iter()
            .map(|r| r["state"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["This Device", "Offline Trusted"]
    );
    i.update(
        "beta",
        props("beta", &"b".repeat(64)),
        json!(["10.0.0.2"]),
        30.,
    )
    .unwrap();
    i.update(
        "gamma",
        props("gamma", &"c".repeat(64)),
        json!(["10.0.0.3"]),
        30.,
    )
    .unwrap();
    let rows = cmd(
        "device_rows",
        json!({"identity":identity,"trusted":trusted,"online":i.candidates()}),
    )
    .unwrap();
    assert_eq!(
        rows.as_array()
            .unwrap()
            .iter()
            .map(|r| r["state"].as_str().unwrap())
            .collect::<BTreeSet<_>>(),
        BTreeSet::from(["This Device", "Online Trusted", "Online Unpaired"])
    );
    i.update(
        "beta",
        props("beta", &"a".repeat(64)),
        json!(["10.0.0.2"]),
        30.,
    )
    .unwrap();
    let rows = cmd(
        "device_rows",
        json!({"identity":identity,"trusted":trusted,"online":i.candidates()}),
    )
    .unwrap();
    assert_eq!(
        rows.as_array()
            .unwrap()
            .iter()
            .find(|r| r["node_id"] == "beta")
            .unwrap()["state"],
        "Identity Mismatch"
    );
    assert!(
        rows.as_array()
            .unwrap()
            .iter()
            .all(|r| r["authenticated"] == false)
    );
}
#[test]
fn actual_dns_remaining_ttl_and_interface_policy() {
    let adapters = json!([{"name":"enp1s0","ips":["10.1.1.2"]},{"name":"docker0","ips":["172.17.0.1"]},{"name":"br-test","ips":["10.2.0.1"]},{"name":"lo","ips":["127.0.0.1"]},{"name":"enp2","ips":["8.8.8.8"]}]);
    assert_eq!(
        cmd("local_addresses", json!({"adapters":adapters})).unwrap(),
        json!(["10.1.1.2"])
    );
    let mut raw = json!({"service":"alpha._clinx._tcp.local.","pointers":[{"type":12,"alias":"alpha._clinx._tcp.local.","remaining":30,"expired":false}],"records":[{"type":33,"remaining":30,"expired":false},{"type":16,"remaining":30,"expired":false}],"addresses":[{"type":1,"address":"0a000001","remaining":30,"expired":false}]});
    let mut index = Index::new();
    for now in [0., 31.] {
        index.now = now;
        let refresh = cmd("refresh", raw.clone()).unwrap();
        assert_eq!(
            refresh,
            json!({"active":true,"ttl":30.,"addresses":["10.0.0.1"]})
        );
        index
            .update(
                "alpha._clinx._tcp.local.",
                props("alpha", &"a".repeat(64)),
                refresh["addresses"].clone(),
                refresh["ttl"].as_f64().unwrap(),
            )
            .unwrap();
        assert!(index.candidates().get("alpha").is_some());
    }
    raw["pointers"][0]["expired"] = json!(true);
    raw["pointers"][0]["remaining"] = json!(0);
    assert_eq!(
        cmd("refresh", raw.clone()).unwrap(),
        json!({"active":false})
    );
    index.remove("alpha._clinx._tcp.local.");
    assert!(index.candidates().get("alpha").is_none());
    raw["pointers"][0]["expired"] = json!(false);
    raw["pointers"][0]["remaining"] = json!(30);
    raw["records"].as_array_mut().unwrap().pop();
    assert_eq!(cmd("refresh", raw).unwrap(), json!({"active":false}));
    assert!(index.candidates().get("alpha").is_none());
}
macro_rules! observer {($name:ident,$endpoint:expr,$allowed:expr)=>{#[test]fn $name(){let raw=json!({"endpoint":$endpoint,"credential":"x".repeat(48)});let result=bootstrap::validate(&raw);if $allowed{assert_eq!(result.unwrap(),json!({"endpoint":$endpoint,"credential":"x".repeat(48),"read_only":true}));}else{assert_eq!(result.unwrap_err(),"INVALID_OBSERVER_CONNECTION");}}};}
observer!(observer_tailscale, "https://p620.example.ts.net", true);
observer!(observer_local, "https://host.local:8766/", true);
observer!(observer_private, "https://10.0.0.1", true);
observer!(observer_loopback, "https://127.0.0.1", true);
observer!(observer_ipv6_loopback, "https://[::1]", true);
observer!(observer_ipv6_private, "https://[fc00::1]", true);
observer!(observer_documentation, "https://192.0.2.1", true);
observer!(observer_cgnat, "https://100.64.0.1", false);
observer!(observer_ipv4_zone, "https://10.0.0.1%eth0", false);
observer!(observer_ipv6_zone, "https://[fe80::1%eth0]", true);
observer!(observer_http, "http://192.168.1.5", false);
observer!(observer_public, "https://example.com", false);
observer!(observer_userinfo, "https://user@p620.ts.net", false);
observer!(observer_path, "https://p620.ts.net/path", false);
observer!(observer_query, "https://p620.ts.net?x=y", false);
observer!(observer_unspecified, "https://0.0.0.0", false);
observer!(observer_zero_port, "https://p620.ts.net:0", false);
observer!(observer_bad_port, "https://p620.ts.net:bad", false);
#[test]
fn bootstrap_opt_in_private_files_bounded_tokens() {
    let temp = tempfile::tempdir().unwrap();
    let credential = temp.path().join("observer.env");
    let config = temp.path().join("monitor.json");
    let token = format!("CLINX_OBSERVER_TOKEN={}\n", "x".repeat(48));
    std::fs::write(&credential, &token).unwrap();
    std::fs::set_permissions(&credential, std::fs::Permissions::from_mode(0o600)).unwrap();
    let mut raw = json!({"share_read_only_observer":true,"endpoint":"https://p620.example.ts.net","credential_file":credential});
    std::fs::write(&config, raw.to_string()).unwrap();
    std::fs::set_permissions(&config, std::fs::Permissions::from_mode(0o600)).unwrap();
    assert_eq!(
        bootstrap::connection(&config).unwrap(),
        json!({"endpoint":raw["endpoint"],"credential":"x".repeat(48),"read_only":true})
    );
    std::fs::write(&credential, token.repeat(2)).unwrap();
    assert_eq!(
        bootstrap::connection(&config).unwrap_err(),
        "INVALID_OBSERVER_CONFIGURATION"
    );
    raw["share_read_only_observer"] = json!(false);
    std::fs::write(&config, raw.to_string()).unwrap();
    assert_eq!(
        bootstrap::connection(&config).unwrap_err(),
        "OBSERVER_SHARING_DISABLED"
    );
    std::fs::set_permissions(&config, std::fs::Permissions::from_mode(0o644)).unwrap();
    assert_eq!(
        bootstrap::connection(&config).unwrap_err(),
        "UNSAFE_STATE_PERMISSIONS"
    );
}
fn state(rt: &Runtime, op: &str, a: Value) -> Result<Value, String> {
    rt.invoke(&format!("discovery.{op}"), &a, |_, _| {
        panic!("unexpected callback")
    })
}
fn trust(rt: &Runtime, root: &Path, b: &Value) -> Result<Value, String> {
    state(
        rt,
        "peers.trust",
        json!({"root":root,"public":identity::public_record(b).unwrap(),"certificate":b["certificate"],"security_status":"OPAQUE_V1","pairing_protocol":"OPAQUE-3DH-RISTRETTO255-SHA512-ARGON2I-v1","fresh_pairing":true}),
    )
}
fn digest(root: &Path) -> BTreeMap<String, Vec<u8>> {
    std::fs::read_dir(root)
        .unwrap()
        .map(|p| p.unwrap().path())
        .filter(|p| p.is_file())
        .map(|p| {
            (
                p.file_name().unwrap().to_str().unwrap().into(),
                std::fs::read(&p).unwrap(),
            )
        })
        .collect()
}
#[test]
fn read_only_peer_snapshot_creates_no_write_lock_and_never_mutates() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("alpha");
    identity::open(&root, "alpha", None).unwrap();
    let b = identity::open(&tmp.path().join("beta"), "beta", None).unwrap();
    let rt = Runtime::default();
    trust(&rt, &root, &b).unwrap();
    std::fs::remove_file(root.join("store.lock")).unwrap();
    let before = digest(&root);
    assert_eq!(
        state(&rt, "peers.all", json!({"root":root})).unwrap()["beta"]["trust_state"],
        "TRUSTED"
    );
    assert_eq!(digest(&root), before);
    assert!(!root.join("store.lock").exists());
}
#[test]
fn rust_trust_persists_conflicts_revoke_and_authentication_without_key_material() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("alpha");
    let a = identity::open(&root, "alpha", None).unwrap();
    let b = identity::open(&tmp.path().join("beta"), "beta", None).unwrap();
    let rt = Runtime::default();
    trust(&rt, &root, &b).unwrap();
    let value = state(
        &rt,
        "peers.authenticate",
        json!({"root":root,"node_id":"beta","public_key":b["public_key"]}),
    )
    .unwrap();
    assert!(!value["last_authenticated_at"].is_null());
    assert_eq!(
        state(
            &rt,
            "peers.authenticate",
            json!({"root":root,"node_id":"beta","public_key":a["public_key"]})
        )
        .unwrap_err(),
        "IDENTITY_MISMATCH"
    );
    assert_eq!(state(&rt,"peers.trust",json!({"root":root,"public":identity::public_record(&b).unwrap(),"certificate":b["certificate"],"security_status":"DEV_ONLY","fresh_pairing":false})).unwrap_err(),"TRUST_DOWNGRADE_REJECTED");
    state(&rt, "peers.revoke", json!({"root":root,"node_id":"beta"})).unwrap();
    assert_eq!(
        state(
            &rt,
            "peers.authenticate",
            json!({"root":root,"node_id":"beta","public_key":b["public_key"]})
        )
        .unwrap_err(),
        "UNTRUSTED_DEVICE"
    );
    assert_eq!(trust(&rt, &root, &b).unwrap_err(), "IDENTITY_MISMATCH");
    let path = root.join("peers.json");
    assert!(
        !std::fs::read_to_string(&path)
            .unwrap()
            .contains("private_key")
    );
    assert_eq!(path.metadata().unwrap().permissions().mode() & 0o777, 0o600);
}
fn window(rt: &Runtime) -> (Value, Value, Value) {
    let id = state(rt, "window.create", json!({})).unwrap();
    let code = state(rt, "window.open", json!({"window":id,"lifetime":60})).unwrap();
    let reservation = state(rt, "window.reserve", json!({"window":id})).unwrap();
    (id, code, reservation)
}
#[test]
fn pairing_commit_failure_is_exact_and_consumed_before_io() {
    let rt = Runtime::default();
    let (id, code, reservation) = window(&rt);
    assert_eq!(code, reservation[2]);
    let error = rt
        .invoke(
            "discovery.window.complete",
            &json!({"window":id,"reservation":reservation[0],"success":true,"commit":true}),
            |op, _| {
                assert_eq!(op, "discovery.commit");
                Err("isolated fixture disk failure".into())
            },
        )
        .unwrap_err();
    assert_eq!(error, "isolated fixture disk failure");
    assert_eq!(
        state(&rt, "window.reserve", json!({"window":id})).unwrap_err(),
        "PAIRING_CLOSED"
    );
    assert_eq!(
        state(
            &rt,
            "window.complete",
            json!({"window":id,"reservation":reservation[0],"success":true})
        )
        .unwrap_err(),
        "PAIRING_REPLAY"
    );
    state(&rt, "window.close", json!({"window":id})).unwrap();
}
#[test]
fn pairing_window_concurrency_has_one_commit_and_no_secret_restoration() {
    let rt = Arc::new(Runtime::default());
    let (id, _, reservation) = window(&rt);
    let commits = AtomicUsize::new(0);
    let results = std::thread::scope(|scope| {
        let threads=(0..8).map(|_|scope.spawn(||rt.invoke("discovery.window.complete",&json!({"window":id,"reservation":reservation[0],"success":true,"commit":true}),|_,_|{commits.fetch_add(1,Ordering::SeqCst);Ok(Value::Null)}))).collect::<Vec<_>>();
        threads
            .into_iter()
            .map(|t| t.join().unwrap())
            .collect::<Vec<_>>()
    });
    assert_eq!(results.iter().filter(|r| r.is_ok()).count(), 1);
    assert_eq!(commits.load(Ordering::SeqCst), 1);
    assert_eq!(
        results
            .iter()
            .filter(|r| r.as_ref().is_err_and(|e| e == "PAIRING_REPLAY"))
            .count(),
        7
    );
    let snapshot = state(&rt, "window.snapshot", json!({"window":id})).unwrap();
    assert!(snapshot["code"].is_null() && snapshot["id"].is_null());
    let fresh = state(&rt, "window.create", json!({})).unwrap();
    assert_eq!(
        state(&rt, "window.reserve", json!({"window":fresh})).unwrap_err(),
        "PAIRING_CLOSED"
    );
    for id in [id, fresh] {
        state(&rt, "window.close", json!({"window":id})).unwrap();
    }
}
