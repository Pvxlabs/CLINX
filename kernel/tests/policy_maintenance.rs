//! Operator profile qualification: native stdio and real disposable Authority IPC.
//! Preserves policy_maintenance.py's exact approval/review boundary. No Provider
//! or execution worker is launched by this profile, including during restart.
use clinx_decision_kernel::{
    mcp::{self, NoObservations, Profile, Server},
    mcp_api::{Api, Config, Reads},
    native_transport::Authority,
    provider::Result,
};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    io::Write,
    process::{Command, Stdio},
};
#[path = "support/runtime_qualification.rs"]
mod runtime;
use runtime::Runtime;

struct NoReads;
impl Reads for NoReads {
    fn thread(&mut self, _: &str) -> Result<Value> {
        panic!("policy profile read Provider")
    }
    fn page(&mut self, _: &str, _: u64, _: Option<&str>) -> Result<Value> {
        panic!("policy profile read Provider")
    }
    fn desktop(&mut self, _: &str, _: bool, _: u64, _: usize, _: Option<&str>) -> Result<Value> {
        panic!("policy profile read Provider")
    }
}
#[derive(Default)]
struct Recording(Vec<(String, Value)>);
impl Authority for Recording {
    fn call(&mut self, op: &str, args: Value, _: &str) -> Result<Value> {
        self.0.push((op.into(), args));
        Ok(match op {
            "effective_policy" => json!({"task":{"policy_hash":"baseline"},"operation_scopes":[]}),
            "get_prepared" => json!({"kind":"POLICY","task_ref":"task_exact"}),
            "list_tasks" => json!({"tasks":[{"task_id":"task_exact"}]}),
            _ => json!({"status":"PREPARED","task_ref":"task_exact"}),
        })
    }
}
fn config() -> Config {
    serde_json::from_value(json!({"runtime_host":"air","provider_endpoint":"/unconfigured-provider","provider_timeout_seconds":1,"host_aliases":{},"codex_host_ids":{}})).unwrap()
}
fn server<A: Authority>(authority: A) -> Server<A, NoReads, NoObservations> {
    Server {
        api: Api {
            authority,
            reads: NoReads,
            config: config(),
        },
        observations: NoObservations,
    }
}
fn call<A: Authority>(
    s: &mut Server<A, NoReads, NoObservations>,
    name: &str,
    args: Value,
) -> Value {
    s.handle_profile(
        json!({"id":1,"method":"tools/call","params":{"name":name,"arguments":args}}),
        Profile::PolicyMaintenance,
    )
    .unwrap()["result"]
        .clone()
}
fn success(v: Value) -> Value {
    assert_eq!(v["isError"], false, "{v}");
    v["structuredContent"].clone()
}
#[test]
fn exactly_four_tools_private_metadata_and_required_review_without_registry_mutation() {
    let baseline = mcp::registry();
    let mut s = server(Recording::default());
    for method in ["initialize", "server/discover", "tools/list"] {
        let response = s
            .handle_profile(json!({"id":1,"method":method}), Profile::PolicyMaintenance)
            .unwrap();
        let r = &response["result"];
        if method == "initialize" {
            assert_eq!(
                r["serverInfo"],
                json!({"name":"clinx-policy-maintenance","version":"1"})
            );
            assert_eq!(r["protocolVersion"], "2025-06-18");
        } else {
            assert_eq!(r["ttlMs"], 60000);
            assert_eq!(r["cacheScope"], "private");
        }
        if method == "server/discover" {
            assert_eq!(
                *r,
                json!({"resultType":"complete","supportedVersions":["2026-07-28"],"capabilities":{"tools":{}},"instructions":mcp::POLICY_INSTRUCTIONS,"ttlMs":60000,"cacheScope":"private"})
            );
        }
        if method != "tools/list" {
            assert_eq!(r["instructions"], mcp::POLICY_INSTRUCTIONS);
        } else {
            let tools = r["tools"].as_array().unwrap();
            assert_eq!(tools.len(), 4);
            assert_eq!(
                tools
                    .iter()
                    .map(|t| t["name"].as_str().unwrap())
                    .collect::<BTreeSet<_>>(),
                mcp::POLICY_TOOLS.into_iter().collect()
            );
            for t in tools {
                assert_eq!(t["annotations"]["openWorldHint"], false);
                let mut expected = baseline["tools"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .find(|old| old["name"] == t["name"])
                    .unwrap()
                    .clone();
                expected["annotations"]["openWorldHint"] = json!(false);
                if t["name"] == mcp::POLICY_TOOLS[3] {
                    expected["inputSchema"]["required"]
                        .as_array_mut()
                        .unwrap()
                        .extend([json!("expected_request_hash"), json!("expected_task_ref")]);
                }
                assert_eq!(*t, expected);
            }
        }
        assert!(
            s.handle_profile(json!({"method":method}), Profile::PolicyMaintenance)
                .is_none()
        );
    }
    for name in [
        "clinx_start_execution",
        "clinx_cancel_execution",
        "clinx_register_derived_git_target",
        "clinx_list_observations",
    ] {
        let r = s
            .handle_profile(
                json!({"id":2,"method":"tools/call","params":{"name":name,"arguments":{}}}),
                Profile::PolicyMaintenance,
            )
            .unwrap();
        assert_eq!(r["error"]["code"], -32602);
    }
    assert_eq!(baseline, mcp::registry());
    assert!(s.api.authority.0.is_empty());
}
#[test]
fn exact_arguments_boolean_approval_review_binding_and_four_dispatches() {
    let mut s = server(Recording::default());
    let valid = json!({"approved":true,"prepared_reauthorization_ref":"reauth_exact","expected_request_hash":"a".repeat(64),"expected_task_ref":"task_exact"});
    for (key, replacement, code) in [
        ("approved", json!(false), "EXPLICIT_OPERATOR_APPROVAL"),
        ("approved", json!(1), "EXPLICIT_OPERATOR_APPROVAL"),
        ("approved", json!("true"), "EXPLICIT_OPERATOR_APPROVAL"),
        ("expected_task_ref", json!(""), "REVIEW_BINDING"),
        ("expected_task_ref", json!(5), "REVIEW_BINDING"),
        (
            "expected_request_hash",
            json!("A".repeat(64)),
            "REVIEW_BINDING",
        ),
        (
            "expected_request_hash",
            json!("a".repeat(63)),
            "REVIEW_BINDING",
        ),
        ("shell", json!("forbidden"), "EXACT_ARGUMENTS"),
    ] {
        let mut a = valid.clone();
        a[key] = replacement;
        let r = call(&mut s, mcp::POLICY_TOOLS[3], a);
        assert_eq!(r["isError"], true);
        assert!(
            r["structuredContent"]["error_code"]
                .as_str()
                .unwrap()
                .contains(code)
        );
    }
    for key in valid.as_object().unwrap().keys() {
        let mut a = valid.clone();
        a.as_object_mut().unwrap().remove(key);
        let r = call(&mut s, mcp::POLICY_TOOLS[3], a);
        assert_eq!(
            r["structuredContent"]["error_code"],
            "POLICY_MAINTENANCE_EXACT_ARGUMENTS_REQUIRED"
        );
    }
    for a in [json!([]), json!(null), json!("invalid")] {
        assert_eq!(
            call(&mut s, mcp::POLICY_TOOLS[3], a)["structuredContent"]["error_code"],
            "POLICY_MAINTENANCE_EXACT_ARGUMENTS_REQUIRED"
        );
    }
    let prepare = json!({"approved":true,"task_ref":"task_exact","expected_policy_hash":"b".repeat(64),"target_policy":{"execution_surface":"HOST_EXECUTOR","operation_scopes":[Runtime::scope()]},"reason":"qualification"});
    for approved in [json!(false), json!(1), json!(null)] {
        let mut a = prepare.clone();
        a["approved"] = approved;
        assert_eq!(
            call(&mut s, mcp::POLICY_TOOLS[2], a)["structuredContent"]["error_code"],
            "POLICY_MAINTENANCE_EXPLICIT_OPERATOR_APPROVAL_REQUIRED"
        );
    }
    assert!(s.api.authority.0.is_empty());
    success(call(
        &mut s,
        mcp::POLICY_TOOLS[0],
        json!({"task_ref":"task_exact"}),
    ));
    success(call(
        &mut s,
        mcp::POLICY_TOOLS[1],
        json!({"request_ref":"reauth_exact"}),
    ));
    success(call(&mut s, mcp::POLICY_TOOLS[2], prepare.clone()));
    success(call(&mut s, mcp::POLICY_TOOLS[3], valid.clone()));
    assert_eq!(
        s.api
            .authority
            .0
            .iter()
            .map(|(op, _)| op.as_str())
            .collect::<Vec<_>>(),
        [
            "effective_policy",
            "get_prepared",
            "list_tasks",
            "prepare_policy",
            "apply_policy"
        ]
    );
    assert_eq!(s.api.authority.0[3].1, prepare);
    assert_eq!(s.api.authority.0[4].1, valid);
}
fn cli(r: &Runtime, requests: &[Value]) -> Vec<Value> {
    let path = r.temp.path().join("mcp.json");
    std::fs::write(&path, serde_json::to_vec(&config()).unwrap()).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_clinx-mcp"))
        .args(["--policy-maintenance", "--stdio", "--config"])
        .arg(path)
        .env("CLINX_RUST_SOCKET", &r.endpoint)
        .env(
            "CLINX_RUST_UID",
            rustix::process::getuid().as_raw().to_string(),
        )
        // This profile must not expand or initialize observations.
        .env("CLINX_RUST_OBSERVATION_READS", "1")
        .env_remove("CLINX_OBSERVATION_CONFIG")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut input = child.stdin.take().unwrap();
    for request in requests {
        writeln!(input, "{request}").unwrap();
    }
    drop(input);
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}
#[test]
fn native_stdio_real_ipc_cas_review_active_history_restart_and_duplicate() {
    let r = Runtime::new("air", true);
    r.launch_authority(true);
    let execution = r.start(None, "fixture");
    let task = execution["task_ref"].clone();
    let mut s = server(r.client());
    let before = success(call(&mut s, mcp::POLICY_TOOLS[0], json!({"task_ref":task})));
    let prepare = json!({"approved":true,"task_ref":task,"expected_policy_hash":before["policy_hash"],"target_policy":{"execution_surface":"NATIVE_FULL_ACCESS"},"reason":"isolated policy qualification"});
    let first = success(call(&mut s, mcp::POLICY_TOOLS[2], prepare.clone()));
    let stale = success(call(&mut s, mcp::POLICY_TOOLS[2], prepare));
    let review = success(call(
        &mut s,
        mcp::POLICY_TOOLS[1],
        json!({"request_ref":first["prepared_reauthorization_ref"]}),
    ));
    assert_eq!(review["task_ref"], task);
    assert_eq!(review["request_type"], "POLICY_REAUTHORIZATION");
    assert_eq!(review["request_hash"], first["request_hash"]);
    let apply = json!({"approved":true,"prepared_reauthorization_ref":first["prepared_reauthorization_ref"],"expected_request_hash":review["request_hash"],"expected_task_ref":task});
    assert_eq!(
        call(&mut s, mcp::POLICY_TOOLS[3], apply.clone())["structuredContent"]["error_code"],
        "TASK_STILL_HELD"
    );
    assert_eq!(
        success(call(&mut s, mcp::POLICY_TOOLS[0], json!({"task_ref":task}))),
        before
    );
    assert_eq!(r.status(&execution)["status"], "ADMITTED");
    r.call(
        "cancel",
        json!({"execution_ref":execution["execution_ref"],"approved":true}),
    );
    let sealed = r.status(&execution);
    assert_eq!(sealed["status"], "CANCELLED");
    for (k, v) in [
        ("expected_request_hash", json!("0".repeat(64))),
        ("expected_task_ref", json!("task_other")),
    ] {
        let mut mismatch = apply.clone();
        mismatch[k] = v;
        assert_eq!(
            call(&mut s, mcp::POLICY_TOOLS[3], mismatch)["structuredContent"]["error_code"],
            "PREPARATION_REVIEW_MISMATCH"
        );
    }
    let request = json!({"id":1,"method":"tools/call","params":{"name":mcp::POLICY_TOOLS[3],"arguments":apply}});
    let result = cli(&r, std::slice::from_ref(&request));
    let done = success(result[0]["result"].clone());
    assert_eq!(done["status"], "APPLIED");
    assert_eq!(done["execution_started"], false);
    r.restart_authority();
    // A fresh MCP process and Authority restart return the exact prepared result,
    // for the same reviewed reference; no execution or Provider command is issued.
    assert_eq!(success(cli(&r, &[request])[0]["result"].clone()), done);
    let mut stale_apply = apply;
    stale_apply["prepared_reauthorization_ref"] = stale["prepared_reauthorization_ref"].clone();
    stale_apply["expected_request_hash"] = stale["request_hash"].clone();
    assert_eq!(
        call(&mut s, mcp::POLICY_TOOLS[3], stale_apply)["structuredContent"]["error_code"],
        "POLICY_VERSION_CONFLICT"
    );
    assert_eq!(r.status(&execution), sealed);
    assert!(!r.provider_path.exists());
    let invalid = json!({"approved":true,"task_ref":task,"expected_policy_hash":success(call(&mut s,mcp::POLICY_TOOLS[0],json!({"task_ref":task})))["policy_hash"],"target_policy":{"execution_surface":"NATIVE_FULL_ACCESS","production_mutation_intent":true},"network_access":true,"reason":"must not grant native production authority"});
    assert_eq!(call(&mut s, mcp::POLICY_TOOLS[2], invalid)["isError"], true);
}
#[test]
fn maintenance_cli_cannot_be_combined_with_observation_or_call_mode() {
    for args in [
        vec!["--policy-maintenance", "--stdio", "--observations-only"],
        vec!["--policy-maintenance", "--call", "clinx_get_context"],
    ] {
        let output = Command::new(env!("CARGO_BIN_EXE_clinx-mcp"))
            .args(args)
            .output()
            .unwrap();
        assert!(!output.status.success());
        assert!(String::from_utf8_lossy(&output.stderr).contains("MCP_ARGUMENT_INVALID"));
    }
}
