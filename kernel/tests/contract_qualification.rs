//! Case-preserving migration of execution_contracts/runtime_config qualification.
//! Expected identities, errors and JSON wire hashes are independent literals.
use clinx_decision_kernel::{execution_contracts as execution, runtime_config};
use serde_json::{Value, json};
use std::path::Path;

fn contract(op: &str, a: Value) -> Value {
    execution::command(op, &a).unwrap()
}
fn route(changes: Value) -> Value {
    let mut a = json!({"host":"WORKSTATION-P620","surface":"codex_app_server","provider":"codex_app_server","transport":"local_stdio","workspace_alias":"p620","project_alias":"线程","worktree_key":"worktree:fixture","project_identity":"repo","conversation_binding":"UNBOUND","authority":"clinx_task_bounded","authority_scopes":["workspace_write"],"network_access":false});
    a.as_object_mut()
        .unwrap()
        .extend(changes.as_object().unwrap().clone());
    contract("build_route", a)
}
macro_rules! host_case { ($name:ident,$raw:expr,$stable:expr) => { #[test] fn $name() {
    assert_eq!(contract("host",json!({"value":$raw,"display":"Fixture","machine_id":"fingerprint"})),json!({"stable_identifier":$stable,"alias":$stable,"display":"Fixture","machine_id":"fingerprint","status":"KNOWN"}));
}}; }
host_case!(host_workstation, "WORKSTATION-P620", "p620");
host_case!(host_casefold, "Straße", "strasse");
host_case!(host_ipv6, "2001:0db8::1", "ip:2001:db8::1");
host_case!(host_ipv4, "192.0.2.1", "ip:192.0.2.1");
host_case!(host_scoped_ipv6, "fe80::1%eth0", "ip:fe80::1-eth0");
#[test]
fn canonical_route_roundtrip_and_one_way_verified_binding() {
    let original = route(json!({}));
    assert_eq!(
        contract("parse_route", json!({"value":original.to_string()})),
        original
    );
    assert_eq!(contract("executable", json!({"route":original})), true);
    assert_eq!(original["network_policy"]["remote_execution"], false);
    assert_eq!(
        original["conversation"],
        json!({"binding":"UNBOUND","status":"UNBOUND"})
    );
    let bound = contract("bind", json!({"route":original,"binding":"native-thread"}));
    assert_eq!(
        contract("bind", json!({"route":bound,"binding":"native-thread"})),
        bound
    );
    assert_eq!(
        contract("parse_route", json!({"value":bound.to_string()})),
        bound
    );
    assert!(
        execution::command("bind", &json!({"route":bound,"binding":"another-thread"}))
            .unwrap_err()
            .contains("conversation changed")
    );
    let mut invalid = route_args();
    invalid["surface"] = json!("host_executor");
    invalid["transport"] = json!("ssh");
    assert!(
        execution::command("build_route", &invalid)
            .unwrap_err()
            .contains("local provider transport")
    );
}
fn route_args() -> Value {
    json!({"host":"WORKSTATION-P620","surface":"codex_app_server","provider":"codex_app_server","transport":"local_stdio","workspace_alias":"p620","project_alias":"线程","worktree_key":"worktree:fixture","project_identity":"repo","conversation_binding":"UNBOUND","authority":"clinx_task_bounded","authority_scopes":["workspace_write"],"network_access":false})
}
macro_rules! mutated_route { ($name:ident,$section:expr,$key:expr,$value:expr) => { #[test] fn $name() {
    let mut raw=route(json!({}));raw[$section][$key]=json!($value);
    assert!(execution::command("parse_route",&json!({"value":raw.to_string()})).unwrap_err().contains("malformed routing identity"));
}}; }
mutated_route!(route_alias_spoof, "host", "alias", "spoofed");
mutated_route!(
    route_host_authority,
    "separation",
    "host_is_authority",
    true
);
mutated_route!(
    route_identity_authority,
    "authority",
    "identity_is_authority",
    true
);
mutated_route!(
    route_remote_execution,
    "network_policy",
    "remote_execution",
    true
);
mutated_route!(
    route_network_string,
    "network_policy",
    "network_access",
    "true"
);
mutated_route!(route_unproven_binding, "conversation", "status", "BOUND");
mutated_route!(
    route_unknown_provider,
    "provider",
    "stable_identifier",
    "unknown-provider"
);
#[test]
fn legacy_unknown_route_does_not_infer_a_host_or_binding() {
    let old = contract(
        "legacy_route",
        json!({"host":"unknown","workspace_alias":"p620","project_alias":"repo","cwd":"/tmp/fixture","project_identity":"repo","worktree_key":null,"conversation_bound":false}),
    );
    use sha2::{Digest, Sha256};
    let expected = format!(
        "legacy:{:.32}",
        format!(
            "{:x}",
            Sha256::digest(b"unknown\x1fp620\x1frepo\x1f/tmp/fixture")
        )
    );
    assert_eq!(old["workspace"]["worktree_key"], expected);
    assert_eq!(contract("executable", json!({"route":old})), false);
    assert_eq!(
        contract("parse_route", json!({"value":old.to_string()})),
        old
    );
    assert!(
        execution::command("bind", &json!({"route":old,"binding":"native-thread"}))
            .unwrap_err()
            .contains("not bindable")
    );
    assert_eq!(
        contract("legacy_policy", json!({"route":old}))["execution_surface"],
        "SANDBOX_WORKSPACE"
    );
}
#[test]
fn exact_policy_scopes_remain_sealed_and_default_native_is_full_access() {
    assert_eq!(
        contract(
            "build_policy",
            json!({"execution_surface":"NATIVE_FULL_ACCESS","network_access":true})
        )["execution_surface"],
        "NATIVE_FULL_ACCESS"
    );
    assert_eq!(
        contract("build_policy", json!({}))["execution_surface"],
        "SANDBOX_WORKSPACE"
    );
    let exact = json!({"capability":"ssh","operation":"remote_status","operation_class":"production_read_only","target":"fixture-host"});
    let policy = contract(
        "build_policy",
        json!({"required_capabilities":["SSH"],"operation_classes":["PRODUCTION_READ_ONLY"],"operation_scopes":[exact,exact]}),
    );
    assert_eq!(
        contract("validate_new", json!({"policy":policy})),
        Value::Null
    );
    for (target, expected) in [("fixture-host", true), ("another-host", false)] {
        assert_eq!(
            contract(
                "permits_operation",
                json!({"policy":policy,"capability":"SSH","requested_operation":"remote_status","operation_class":"PRODUCTION_READ_ONLY","target":target})
            ),
            expected
        );
    }
    assert_eq!(
        contract(
            "permits",
            json!({"policy":policy,"operation_class":"PRODUCTION_MUTATION","capability":"SSH"})
        ),
        false
    );
    assert_eq!(
        contract("parse_policy", json!({"value":policy.to_string()})),
        policy
    );
    let scopes = contract("authority_scopes", json!({"policy":policy}));
    assert_eq!(scopes, json!(["production_read_only", "workspace_write"]));
    let identity = route(json!({"surface":"host_executor","authority_scopes":scopes}));
    assert_eq!(
        contract("validate_route", json!({"policy":policy,"route":identity})),
        Value::Null
    );
    assert_eq!(
        contract("operation_scopes", json!({"value":[exact,exact]}))
            .as_array()
            .unwrap()
            .len(),
        1
    );
    let unscoped = contract(
        "build_policy",
        json!({"required_capabilities":["SSH"],"operation_classes":["PRODUCTION_READ_ONLY"]}),
    );
    assert!(
        execution::command("validate_new", &json!({"policy":unscoped}))
            .unwrap_err()
            .contains("PRODUCTION_SCOPE_REQUIRED")
    );
    let mut invalid = exact;
    invalid["target"] = json!("*");
    assert!(
        execution::command("operation_scopes", &json!({"value":[invalid]}))
            .unwrap_err()
            .contains("exact identities")
    );
}
#[test]
fn policy_cas_hash_preserves_deployed_json_unicode_and_generation_bytes() {
    let policy = contract("build_policy", json!({}));
    let identity = route(json!({"conversation_binding":"线程\u{7f}"}));
    // Independently frozen Python json.dumps(ensure_ascii=True,sort_keys=True,separators) oracle.
    let at7 = contract(
        "policy_identity",
        json!({"policy_json":policy.to_string(),"route_json":identity.to_string(),"version":7}),
    );
    assert_eq!(at7, include_str!("support/policy_hash.txt").trim());
    assert_ne!(
        at7,
        contract(
            "policy_identity",
            json!({"policy_json":policy.to_string(),"route_json":identity.to_string(),"version":8})
        )
    );
}
macro_rules! bad_policy {
    ($name:ident,$args:expr,$error:expr) => {
        #[test]
        fn $name() {
            assert!(
                execution::command("build_policy", &$args)
                    .unwrap_err()
                    .contains($error)
            );
        }
    };
}
bad_policy!(
    policy_host_requires_capabilities,
    json!({"execution_surface":"HOST_EXECUTOR"}),
    "explicit capabilities"
);
bad_policy!(
    policy_capabilities_require_classes,
    json!({"required_capabilities":["SSH"]}),
    "operation classes"
);
bad_policy!(
    policy_business_action_forbidden,
    json!({"required_capabilities":["SSH"],"operation_classes":["BUSINESS_ACTION"]}),
    "BUSINESS_ACTION"
);
bad_policy!(
    policy_mutation_intent_required,
    json!({"required_capabilities":["SSH"],"operation_classes":["PRODUCTION_MUTATION"]}),
    "production_mutation_intent"
);
bad_policy!(
    policy_network_optin_required,
    json!({"execution_surface":"NETWORKED_SANDBOX"}),
    "network_access=true"
);
bad_policy!(
    policy_network_boolean_required,
    json!({"network_access":"true"}),
    "must be a boolean"
);

const BASE: &str = "[linear]\nteam_id='team'\ntrigger_label='label'\ntodo_state='Todo'\nrunning_state='Running'\nreview_state='Review'\n[projects.z]\ncwd='./z'\nbranch='main'\n[projects.a]\ncwd='./a'\nbranch='main'\n";
fn load(root: &Path, body: &str) -> Result<Value, String> {
    let p = root.join("config.toml");
    std::fs::write(&p, body).unwrap();
    runtime_config::native_load(&p)
}
#[test]
fn order_and_path_io_preserve_existing_config() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path();
    std::fs::create_dir(root.join("destination")).unwrap();
    std::os::unix::fs::symlink("destination", root.join("z")).unwrap();
    let body = format!(
        "{BASE}\n[workspaces.z]\nroot='./z'\ncodex_host_ids=['desktop-z']\n[workspaces.a]\nroot='./a'\n[host_executor]\ntrusted_workspace_roots=['{}','{}']\n",
        root.display(),
        root.display()
    );
    let path = root.join("config.toml");
    std::fs::write(&path, body).unwrap();
    use std::io::Write;
    use std::process::{Command, Stdio};
    let mut child = Command::new(env!("CARGO_BIN_EXE_clinx-node"))
        .current_dir(root)
        .env_clear()
        .env("HOME", root)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    writeln!(child.stdin.take().unwrap(),"{}",json!({"protocol":"CLINX_REQUEST_PIPE_V1","mode":"config","operation":"native_load","arguments":{"path":path}})).unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(output.status.success());
    let reply: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert!(reply.get("error").is_none(), "{reply}");
    let cfg = &reply["result"];
    assert_eq!(
        cfg["projects"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| p["alias"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["z", "a"]
    );
    assert_eq!(
        cfg["workspaces"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| p["alias"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["z", "a"]
    );
    assert_eq!(
        cfg["projects"][0]["repo"],
        root.join("destination").to_str().unwrap()
    );
    assert_eq!(
        cfg["host_executor"]["trusted_workspace_roots"],
        json!([root])
    );
    assert_eq!(cfg["workspaces"][0]["codex_host_ids"], json!(["desktop-z"]));
    assert!(cfg["node_router"].is_null());
}
macro_rules! integer_config {
    ($name:ident,$value:expr,$expected:expr) => {
        #[test]
        fn $name() {
            let tmp = tempfile::tempdir().unwrap();
            let text = BASE.replace(
                "[projects.z]",
                &format!("poll_interval_seconds={}\n[projects.z]", $value),
            );
            assert_eq!(
                load(tmp.path(), &text).unwrap()["poll_interval_seconds"],
                $expected
            );
        }
    };
}
integer_config!(integer_underscore, "'1_024'", 1024);
integer_config!(integer_unicode, "'５'", 5);
integer_config!(integer_float_truncate, "5.9", 5);
integer_config!(integer_sign, "'+15'", 15);
macro_rules! invalid_integer {
    ($name:ident,$value:expr) => {
        #[test]
        fn $name() {
            let tmp = tempfile::tempdir().unwrap();
            let text = BASE.replace(
                "[projects.z]",
                &format!("poll_interval_seconds={}\n[projects.z]", $value),
            );
            assert!(
                load(tmp.path(), &text)
                    .unwrap_err()
                    .contains("invalid literal")
            );
        }
    };
}
invalid_integer!(integer_fraction_string, "'5.5'");
invalid_integer!(integer_exponent_string, "'5e0'");
invalid_integer!(integer_bad_underscore, "'1__0'");
fn config(op: &str, args: Value) -> Result<Value, String> {
    runtime_config::invoke(op, &args, |_, _| panic!("unexpected IO"))
}
#[test]
fn exact_host_selection_and_executor_binding() {
    assert_eq!(
        config("canonical_host", json!({"value":" Workstation-P620 "})).unwrap(),
        "p620"
    );
    assert_eq!(config("resolve_transport",json!({"runtime_host":"workstation-p620","target_host":"P620","remote_transport":"local"})).unwrap(),"local");
    assert!(
        config(
            "resolve_transport",
            json!({"runtime_host":"mac","target_host":"p620","remote_transport":"local"})
        )
        .unwrap_err()
        .contains("cross-host")
    );
    let tmp = tempfile::tempdir().unwrap();
    assert!(load(tmp.path(),&format!("{BASE}\n[runtime]\nruntime_host='mac'\n[host_executor]\nenabled=true\nhost='p620'\n")).unwrap_err().contains("configured runtime host"));
}
fn workflow() -> Value {
    json!({"identity":"DEPLOY","target":"app","action":"apply","operation_class":"PRODUCTION_MUTATION","argv":["/bin/echo","/fixed/{release}","{sha}"],"resource":"app","parameters":{"sha":{"format":"source_sha"},"release":{"format":"identity"}}})
}
#[test]
fn workflow_schema_and_render_share_rust_host_contract() {
    let rows = config("workflows", json!({"rows":{"entry":workflow()}})).unwrap();
    let item = &rows[0];
    assert_eq!(
        config("workflow_schema", json!({"workflow":item})).unwrap()["required"],
        json!(["release", "sha"])
    );
    assert_eq!(
        config(
            "workflow_render",
            json!({"workflow":item,"parameters":{"sha":"a".repeat(40),"release":"r1"}})
        )
        .unwrap(),
        json!(["/bin/echo", "/fixed/r1", "a".repeat(40)])
    );
    assert!(
        config(
            "workflow_render",
            json!({"workflow":item,"parameters":{"sha":"a".repeat(40),"release":"../escape"}})
        )
        .unwrap_err()
        .contains("invalid workflow parameter release")
    );
    assert!(
        config(
            "workflow_render",
            json!({"workflow":item,"parameters":{"sha":"a".repeat(40),"release":"r1","extra":"x"}})
        )
        .unwrap_err()
        .contains("registered schema")
    );
    let mut other = workflow();
    other["action"] = json!("check");
    other["resource"] = json!("other");
    assert!(
        config(
            "workflows",
            json!({"rows":{"first":workflow(),"second":other}})
        )
        .unwrap_err()
        .contains("resource fence")
    );
}
#[test]
fn workflow_registration_rejects_path_and_placeholder_changes() {
    for argv in [
        json!(["relative", "{sha}"]),
        json!(["/bin/echo", "/fixed/../{release}", "{sha}"]),
        json!(["/bin/echo", "--arg={release}", "{sha}"]),
    ] {
        let mut item = workflow();
        item["argv"] = argv;
        assert!(config("workflows", json!({"rows":{"entry":item}})).is_err());
    }
}
