//! Full opt-in smoke orchestration on private loopback, without LAN multicast.
#[path = "../examples/support/local_discovery_smoke.rs"]
mod smoke;
use serde_json::json;
use std::{ffi::OsString, path::Path};

#[test]
fn real_nodes_pair_restart_both_directions_and_reject_wrong_tls_identity() {
    let receipt = smoke::run(
        Path::new(env!("CARGO_BIN_EXE_clinx-node")),
        smoke::Mode::Loopback,
    )
    .unwrap();
    assert_eq!(
        receipt,
        json!({"contract":"RUST_DISCOVERY_SMOKE_V1","mode":"isolated_loopback","mdns":"NOT_RUN_SEPARATE_PRIVATE_UDP_QUALIFICATION","opaque_pairing":"PASS","two_process_restart":"PASS","bidirectional_mtls_without_pin":"PASS","wrong_tls_identity_rejected":"PASS","authorization_isolation":"PASS","physical_cross_device":"NOT_VERIFIED"})
    );
}

#[test]
fn lan_is_explicit_opt_in_and_arguments_cannot_supply_pairing_secrets() {
    for args in [
        vec![],
        vec!["--code", "1234"],
        vec!["--lan", "--code", "1234"],
        vec!["--lan", "--node-binary"],
    ] {
        assert!(smoke::options(args.into_iter().map(OsString::from)).is_err());
    }
    let (mode, path) = smoke::options(
        ["--lan", "--node-binary", "/private/test/clinx-node"]
            .into_iter()
            .map(OsString::from),
    )
    .unwrap();
    assert!(mode == smoke::Mode::Lan);
    assert_eq!(path, Path::new("/private/test/clinx-node"));
}
