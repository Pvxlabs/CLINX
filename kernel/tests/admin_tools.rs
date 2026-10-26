#[path = "../src/admin_tools.rs"]
#[allow(dead_code)]
mod admin;
#[path = "../src/build_release.rs"]
#[allow(dead_code)]
mod build;
#[path = "../src/install_tools.rs"]
#[allow(dead_code)]
mod install;
use std::{
    fs,
    os::unix::fs::{PermissionsExt, symlink},
    path::Path,
    process::Command,
};

#[test]
fn privileged_surface_rejects_other_services_shell_paths_and_unapproved_switches() {
    for args in [
        vec!["service", "restart", "orion-ops-surface.service"],
        vec!["service", "restart", "clinx-rust-mcp@../../evil.service"],
        vec!["service", "enable", "clinx-rust-authority.service"],
        vec!["journal", "clinx-rust-centre.service", "2001"],
        vec!["exec", "/bin/bash"],
        vec!["sql", "SELECT 1"],
        vec!["stage", "/etc", "1234567890123456789012345678901234567890"],
        vec![
            "stage",
            "/data/artifacts/../../etc",
            "1234567890123456789012345678901234567890",
        ],
        vec!["select", "rust-01234567890123456789"],
        vec!["select", "../../etc", "--approved"],
        vec![
            "recover",
            "rust-01234567890123456789",
            "exec_01234567890123456789012345678901",
        ],
        vec![
            "recover",
            "../../etc",
            "exec_01234567890123456789012345678901",
            "--approved",
        ],
        vec![
            "recover",
            "rust-01234567890123456789",
            "--once",
            "--approved",
        ],
        vec!["share", "--state", "user", "execute"],
        vec!["pair-listen", "--state", "/elsewhere"],
        vec!["probe", "p620", "clinx", "--command", "/bin/sh"],
    ] {
        assert_eq!(
            admin::validate(&args.iter().map(|s| s.to_string()).collect::<Vec<_>>()).unwrap_err(),
            "ADMIN_ARGUMENT_DENIED",
            "{args:?}"
        );
    }
    for args in [
        vec!["status"],
        vec!["nodes"],
        vec!["pair-listen"],
        vec!["probe", "air.local", "clinx"],
        vec!["share", "air.local", "tinzleung", "execute"],
        vec!["service", "restart", "clinx-rust-mcp@0-127.0.0.1.service"],
        vec!["select", "rust-01234567890123456789", "--approved"],
        vec![
            "recover",
            "rust-01234567890123456789",
            "exec_01234567890123456789012345678901",
            "--approved",
        ],
    ] {
        admin::validate(&args.iter().map(|s| s.to_string()).collect::<Vec<_>>()).unwrap();
    }
}
#[test]
fn recovery_reuses_only_trusted_provider_routes_never_host_execution() {
    let args = admin::recovery_routes("ExecStart=/release/clinx-native-transport --socket /fixed --authority-uid 993 --host-config /host.json --provider-route air.local /run/air.sock --provider-gateway-uid 990").unwrap();
    assert_eq!(
        args,
        [
            "--provider-route",
            "air.local",
            "/run/air.sock",
            "--provider-gateway-uid",
            "990"
        ]
    );
    for text in [
        "ExecStart=/bin/sh -c anything",
        "ExecStart=/release/bin --provider-route air.local",
        "ExecStart=/release/bin --once",
    ] {
        assert!(admin::recovery_routes(text).is_err());
    }
}
#[test]
fn root_control_rejects_operator_files_writable_paths_and_symlinks() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("entry");
    fs::write(&p, b"not privileged code").unwrap();
    fs::set_permissions(&p, fs::Permissions::from_mode(0o777)).unwrap();
    assert!(
        admin::trusted(&p, 0)
            .unwrap_err()
            .contains("ADMIN_ROOT_CONTROL_REQUIRED")
    );
    let link = d.path().join("link");
    symlink("/usr/bin/true", &link).unwrap();
    assert!(
        admin::trusted(&link, 0)
            .unwrap_err()
            .contains("ADMIN_ROOT_CONTROL_REQUIRED")
    );
    // Some distributions use a symlink to a multicall coreutils executable.
    // The positive case must be its actual root-owned file. The symlink
    // rejection above remains unconditional.
    admin::trusted(&fs::canonicalize("/usr/bin/true").unwrap(), 0).unwrap();
}
#[test]
fn candidate_markers_never_execute_with_root_identity() {
    let c = build::marker_command(
        Path::new("/candidate/clinx-node"),
        "--network-contract",
        true,
    );
    assert_eq!(c.get_program(), "/usr/sbin/runuser");
    assert_eq!(
        c.get_args().collect::<Vec<_>>(),
        [
            "-u",
            "nobody",
            "--",
            "/usr/bin/setpriv",
            "--no-new-privs",
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "/candidate/clinx-node",
            "--network-contract"
        ]
    );
    let c = build::marker_command(
        Path::new("/candidate/clinx-node"),
        "--network-contract",
        false,
    );
    assert_eq!(c.get_program(), "/candidate/clinx-node");
}
#[test]
fn cli_rejects_unsafe_requests_before_any_privileged_operation() {
    let out = Command::new(env!("CARGO_BIN_EXE_clinx-admin"))
        .args(["service", "restart", "ssh.service"])
        .output()
        .unwrap();
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("ADMIN_ARGUMENT_DENIED"));
    let help = Command::new(env!("CARGO_BIN_EXE_clinx-admin"))
        .arg("--help")
        .output()
        .unwrap();
    assert!(help.status.success());
    assert!(
        String::from_utf8_lossy(&help.stdout)
            .contains("Sudo permission never replaces task-specific user authorization")
    );
}

#[test]
fn bootstrap_shell_and_exact_sudo_policy_parse_with_system_validators() {
    let script = Path::new(env!("CARGO_MANIFEST_DIR")).join("../scripts/install-clinx-admin.sh");
    assert!(
        Command::new("/bin/bash")
            .arg("-n")
            .arg(&script)
            .status()
            .unwrap()
            .success()
    );
    let body = fs::read_to_string(script).unwrap();
    let rule = body
        .split("<<EOF\n")
        .nth(1)
        .unwrap()
        .split("\nEOF")
        .next()
        .unwrap();
    let rule = rule.replace("$admin_hash", &"a".repeat(64));
    let d = tempfile::tempdir().unwrap();
    let path = d.path().join("sudoers");
    fs::write(&path, rule).unwrap();
    // Ubuntu's sudo-rs visudo does not implement command digests. Validate this
    // P620 policy with the original sudo parser, shipped as visudo.ws there.
    let validator = if Path::new("/usr/sbin/visudo.ws").exists() {
        "/usr/sbin/visudo.ws"
    } else {
        "/usr/sbin/visudo"
    };
    let out = Command::new(validator)
        .args(["-c", "-f"])
        .arg(path)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
}
