//! Native workspace identity follows the repository and the actual native cwd.
use clinx_decision_kernel::{
    authority_store::Store,
    business::{Policy, Project, Role},
};
use serde_json::{Value, json};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    process::Command,
    sync::atomic::{AtomicUsize, Ordering},
};

static NEXT: AtomicUsize = AtomicUsize::new(0);

struct Fixture {
    root: PathBuf,
    policy: Policy,
    store: Store,
}
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "clinx-native-routing-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&root).unwrap();
        let cwd = root.join("plain-project");
        fs::create_dir(&cwd).unwrap();
        git(&cwd, &["init", "--initial-branch=master"]);
        git(
            &cwd,
            &[
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "--allow-empty",
                "-m",
                "base",
            ],
        );
        let policy = Policy {
            projects: vec![Project {
                alias: "orion".into(),
                host: "p620".into(),
                cwd: cwd.to_str().unwrap().into(),
            }],
            workspaces: vec![],
            endpoints: vec!["/fixture/provider.sock".into()],
            lease_ms: 120000,
            host_operations: vec![
                json!({"capability":"HOST_FILESYSTEM","operation":"path_read","operation_class":"READ_ONLY_HOST","target":""}),
            ],
            derived_git_roots: vec![],
        };
        fs::create_dir(root.join("state")).unwrap();
        fs::set_permissions(root.join("state"), fs::Permissions::from_mode(0o700)).unwrap();
        let store = Store::open(&root.join("state"), true).unwrap();
        store.configure_business(&policy, true).unwrap();
        Self {
            root,
            policy,
            store,
        }
    }
    fn call(&mut self, role: Role, op: &str, value: Value) -> Result<Value, String> {
        let id = format!("{}-{}", op, NEXT.fetch_add(1, Ordering::Relaxed));
        self.store.business(role, &self.policy, op, &id, &value)
    }
    fn prepare(&mut self, route: Value) -> Result<Value, String> {
        self.call(Role::Api, "prepare", json!({"approved":true,"project":"orion","host":"p620","title":"Native routing regression","prompt":"Keep this exact message unchanged","workspace_contract":route}))
    }
    fn start(&mut self, prepared: &Value) -> Value {
        self.call(
            Role::Api,
            "start",
            json!({"approved":true,"prepared_execution_ref":prepared["prepared_execution_ref"]}),
        )
        .unwrap()
    }
    fn bind(&mut self, execution: &Value, cwd: &Value) -> Value {
        let take = self
            .call(
                Role::Transport,
                "take_dispatch",
                json!({"execution_ref":execution["execution_ref"]}),
            )
            .unwrap();
        let scope = json!({"execution_ref":execution["execution_ref"],"epoch":take["epoch"],"ticket":take["ticket"]});
        let mut binding = scope.clone();
        binding["endpoint"] = json!("/fixture/provider.sock");
        binding["thread_id"] = execution["thread_id"]
            .as_str()
            .map(Value::from)
            .unwrap_or(json!(format!(
                "thread-{}",
                execution["execution_ref"].as_str().unwrap()
            )));
        binding["cwd"] = json!("/wrong/route");
        assert_eq!(
            self.call(Role::Transport, "bind_thread", binding.clone())
                .unwrap_err(),
            "WORKSPACE_CWD_UNREADABLE"
        );
        binding["cwd"] = cwd.clone();
        self.call(Role::Transport, "bind_thread", binding).unwrap();
        scope
    }
    fn send(&mut self, execution: &Value, cwd: &Value, ack: bool) -> Value {
        let scope = self.bind(execution, cwd);
        self.call(Role::Transport, "authorize_send", scope).unwrap();
        if !ack {
            return execution.clone();
        }
        let request = self
            .call(
                Role::Transport,
                "input_begin",
                json!({"execution_ref":execution["execution_ref"]}),
            )
            .unwrap();
        self.call(Role::Transport, "input_commit", json!({"execution_ref":execution["execution_ref"],
            "nonce":request["nonce"],"endpoint":request["endpoint"],"thread_id":request["thread_id"],
            "turn_id":format!("turn-{}", execution["execution_ref"].as_str().unwrap()),
            "item":{"id":format!("input-{}", execution["execution_ref"].as_str().unwrap()),"type":"userMessage",
                "clientId":request["client_message_id"],"content":[{"type":"text","text":request["prompt"]}]}})).unwrap()
    }
    fn observe(&mut self, execution: &Value, state: &str, status: &str, text: &str) -> Value {
        let challenge = self
            .call(
                Role::Transport,
                "observe_begin",
                json!({"execution_ref":execution["execution_ref"]}),
            )
            .unwrap();
        self.call(Role::Transport, "observe_commit", json!({"execution_ref":execution["execution_ref"],
            "nonce":challenge["nonce"],"observations":[{"endpoint":"/fixture/provider.sock","state":state,
                "thread_id":execution["thread_id"],"turn_id":execution["native_turn_id"],"turn_status":status}],
            "turn":{"id":execution["native_turn_id"],"status":status,"items":[{"type":"agentMessage","text":text}]}})).unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.root).unwrap();
    }
}

fn git(cwd: &std::path::Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .arg("-C")
        .arg(cwd)
        .args(args)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap().trim().into()
}

#[test]
fn native_repository_resolution_retains_lease_and_send_identity() {
    let mut f = Fixture::new();
    let prepared = f.prepare(Value::Null).unwrap();
    assert_eq!(
        prepared["workspace_contract"]["cwd"],
        f.policy.projects[0].cwd
    );
    assert_eq!(
        prepared["workspace_resolution"]["repository_root"],
        f.policy.projects[0].cwd
    );
    assert_eq!(prepared["workspace_resolution"]["branch"], "master");
    let execution = f.start(&prepared);
    assert_eq!(
        f.start(&prepared)["execution_ref"],
        execution["execution_ref"]
    );
    let competitor = f.prepare(Value::Null).unwrap();
    let independent = f.start(&competitor);
    assert_ne!(independent["execution_ref"], execution["execution_ref"]);
    assert_ne!(independent["task_ref"], execution["task_ref"]);
    let scope = f.bind(&execution, &prepared["workspace_contract"]["cwd"]);
    assert_eq!(
        f.call(Role::Transport, "authorize_send", scope.clone())
            .unwrap()["send_permitted"],
        true
    );
    assert_eq!(
        f.call(Role::Transport, "authorize_send", scope)
            .unwrap_err(),
        "SEND_NOT_AVAILABLE"
    );
    f.store.verify_business().unwrap();
}

#[test]
fn native_unreadable_cwd_and_unknown_repository_fail_closed() {
    let mut f = Fixture::new();
    let cwd = f.root.join("missing-directory");
    assert_eq!(
        f.prepare(json!({"cwd":cwd})).unwrap_err(),
        "WORKSPACE_CWD_UNREADABLE"
    );
    fs::create_dir(&cwd).unwrap();
    assert_eq!(
        f.prepare(json!({"cwd":cwd})).unwrap_err(),
        "WORKSPACE_IDENTITY_UNAVAILABLE"
    );
    fs::remove_dir_all(PathBuf::from(&f.policy.projects[0].cwd).join(".git")).unwrap();
    assert_eq!(
        f.prepare(Value::Null).unwrap_err(),
        "PROJECT_REPOSITORY_UNAVAILABLE"
    );
}

#[test]
fn native_linked_worktree_commits_do_not_block_start_send_or_continuation() {
    let mut f = Fixture::new();
    let main = PathBuf::from(&f.policy.projects[0].cwd);
    git(&main, &["init", "--initial-branch=master"]);
    git(
        &main,
        &[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
    );
    let linked = f.root.join("dev-next");
    git(
        &main,
        &[
            "worktree",
            "add",
            "-b",
            "dev/next",
            linked.to_str().unwrap(),
        ],
    );
    let legacy =
        json!({"cwd":linked,"branch":"dev/next","head":git(&linked, &["rev-parse","HEAD"])});
    let prepared = f.prepare(legacy.clone()).unwrap();
    git(
        &linked,
        &[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "after-prepare",
        ],
    );
    let execution = f.start(&prepared);
    let scope = f.bind(&execution, &prepared["workspace_contract"]["cwd"]);
    git(
        &linked,
        &[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "before-send",
        ],
    );
    git(&linked, &["checkout", "-b", "switched-before-send"]);
    assert_eq!(
        f.call(Role::Transport, "authorize_send", scope).unwrap()["send_permitted"],
        true
    );
    let next = f.call(Role::Api, "prepare", json!({"approved":true,"project":"orion","host":"p620","title":"Continue","prompt":"Continue on the same task","task_ref":prepared["task_ref"]})).unwrap();
    assert_eq!(next["task_ref"], prepared["task_ref"]);
    assert_eq!(next["workspace_contract"]["branch"], "switched-before-send");
    assert_eq!(next["workspace_contract"]["cwd"], json!(linked));
    assert_eq!(
        next["workspace_contract"]["head"],
        git(&linked, &["rev-parse", "HEAD"])
    );
    // A legacy caller's stale branch/HEAD is compatibility metadata, not authority.
    assert_eq!(
        f.prepare(legacy).unwrap()["workspace_contract"]["head"],
        git(&linked, &["rev-parse", "HEAD"])
    );
}

#[test]
fn native_route_rejects_malformed_inputs() {
    let mut f = Fixture::new();
    for route in [
        json!({"cwd":"relative"}),
        json!({"cwd":"/bad\u{0}path"}),
        json!({"cwd":42}),
        json!({"cwd":"/tmp","unexpected":true}),
        json!({"cwd":"/tmp","head":42}),
    ] {
        assert_eq!(f.prepare(route).unwrap_err(), "WORKSPACE_CONTRACT_INVALID");
    }
}

#[test]
fn explicit_host_execution_retains_git_identity_checks() {
    let mut f = Fixture::new();
    fs::remove_dir_all(PathBuf::from(&f.policy.projects[0].cwd).join(".git")).unwrap();
    let scope = f.policy.host_operations[0].clone();
    let request = json!({"approved":true,"project":"orion","host":"p620","title":"Explicit Host request","prompt":"Host fixture","execution_policy":{"execution_surface":"HOST_EXECUTOR","operation_scopes":[scope]}});
    assert_eq!(
        f.call(Role::Api, "prepare", request).unwrap_err(),
        "PROJECT_REPOSITORY_UNAVAILABLE"
    );
}

#[test]
fn explicit_host_execution_refreshes_head_and_branch_after_prepare() {
    let mut f = Fixture::new();
    let cwd = PathBuf::from(&f.policy.projects[0].cwd);
    git(&cwd, &["init", "--initial-branch=dev"]);
    git(
        &cwd,
        &[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
    );
    let scope = f.policy.host_operations[0].clone();
    let prepared = f.call(Role::Api, "prepare", json!({"approved":true,"project":"orion","host":"p620","title":"Explicit Host request","prompt":"Host fixture","execution_policy":{"execution_surface":"HOST_EXECUTOR","operation_scopes":[scope]}})).unwrap();
    git(
        &cwd,
        &[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "changed",
        ],
    );
    git(&cwd, &["checkout", "-b", "host-switched"]);
    let execution = f.start(&prepared);
    assert_ne!(
        execution["workspace_contract"]["head"],
        prepared["workspace_contract"]["head"]
    );
    assert_eq!(execution["workspace_contract"]["branch"], "host-switched");
    let scope = f.bind(&execution, &json!(cwd));
    git(&cwd, &["checkout", "-b", "host-before-send"]);
    assert_eq!(
        f.call(Role::Transport, "authorize_send", scope).unwrap()["send_permitted"],
        true
    );
}

#[test]
fn native_adoption_uses_observed_worktree_and_keeps_exact_thread_evidence() {
    let mut f = Fixture::new();
    let cwd = f.root.join("native-linked");
    git(
        &PathBuf::from(&f.policy.projects[0].cwd),
        &["worktree", "add", "-b", "native", cwd.to_str().unwrap()],
    );
    let adoption = f.call(Role::Api, "adopt", json!({"host":"p620","project":"orion","thread_id":"existing-native-thread","title":"Existing native thread","workspace_contract":null})).unwrap();
    let challenge = f
        .call(
            Role::Transport,
            "adoption_begin",
            json!({"adoption_ref":adoption["adoption_ref"]}),
        )
        .unwrap();
    let mut evidence = json!({"adoption_ref":adoption["adoption_ref"],"nonce":challenge["nonce"],"observations":[{"endpoint":"/fixture/provider.sock","thread":{"id":"wrong-thread","cwd":cwd,"status":{"type":"idle"}}}]});
    assert_eq!(
        f.call(Role::Transport, "adoption_commit", evidence.clone())
            .unwrap_err(),
        "NATIVE_ADOPTION_NOT_IDLE_OR_IDENTITY_CONFLICT"
    );
    evidence["observations"][0]["thread"]["id"] = json!("existing-native-thread");
    let adopted = f
        .call(Role::Transport, "adoption_commit", evidence)
        .unwrap();
    assert_eq!(adopted["adoption_status"], "ADOPTED");
    let next = f.call(Role::Api, "prepare", json!({"approved":true,"project":"orion","host":"p620","title":"Continue","prompt":"Continue existing native thread","task_ref":adopted["task_ref"]})).unwrap();
    assert_eq!(next["task_ref"], adopted["task_ref"]);
    assert_eq!(next["workspace_contract"]["cwd"], json!(cwd));
    assert_eq!(next["workspace_contract"]["branch"], "native");
    f.store.verify_business().unwrap();
}

#[test]
fn same_cwd_concurrent_tasks_write_and_complete_with_independent_receipts() {
    let mut f = Fixture::new();
    let cwd = PathBuf::from(&f.policy.projects[0].cwd);
    fs::write(cwd.join("existing-user-change"), "preserve me").unwrap();
    let a = f.prepare(Value::Null).unwrap();
    let a = f.start(&a);
    let a = f.send(&a, &json!(cwd), true);
    assert_eq!(a["status"], "RUNNING");
    let b_prepared = f.prepare(Value::Null).unwrap();
    let b = f.start(&b_prepared);
    let b = f.send(&b, &json!(cwd), true);
    assert_eq!(b["status"], "RUNNING");
    assert_ne!(a["thread_id"], b["thread_id"]);
    assert_ne!(a["native_turn_id"], b["native_turn_id"]);
    // Two real file operations overlap in the same cwd. No task owns or cleans
    // the entire directory. Existing user content is preserved.
    let barrier = std::sync::Barrier::new(2);
    std::thread::scope(|scope| {
        for name in ["task-a-output", "task-b-output"] {
            let cwd = &cwd;
            let barrier = &barrier;
            scope.spawn(move || {
                use std::io::Write;
                barrier.wait();
                let mut out = fs::OpenOptions::new()
                    .create_new(true)
                    .write(true)
                    .open(cwd.join(name))
                    .unwrap();
                out.write_all(name.as_bytes()).unwrap();
            });
        }
    });
    let mut b_done = f.observe(&b, "idle", "completed", "result B");
    b_done.as_object_mut().unwrap().remove("provider_liveness");
    assert_eq!(b_done["status"], "SUCCEEDED");
    let still_a = f
        .call(
            Role::Api,
            "get_status",
            json!({"execution_ref":a["execution_ref"]}),
        )
        .unwrap();
    assert_eq!(still_a["status"], "RUNNING");
    assert_eq!(still_a["resource_state"], "HELD");
    let a_done = f.observe(&a, "idle", "completed", "result A");
    assert_eq!(a_done["execution_result"]["response_text"], "result A");
    assert_eq!(b_done["execution_result"]["response_text"], "result B");
    assert_eq!(f.start(&b_prepared), b_done);
    for name in ["task-a-output", "task-b-output"] {
        assert_eq!(fs::read_to_string(cwd.join(name)).unwrap(), name);
    }
    assert_eq!(
        fs::read_to_string(cwd.join("existing-user-change")).unwrap(),
        "preserve me"
    );
    if let Ok(output) = std::env::var("CLINX_TEST_EVIDENCE_DIR") {
        fs::write(PathBuf::from(output).join("same-cwd-concurrency.json"), serde_json::to_vec_pretty(&json!({
            "scope":"LOCAL_RUST_AUTHORITY_WITH_NATIVE_OBSERVATION_FIXTURES",
            "both_running":{"a":a,"b":b},"a_after_b_completed":still_a,
            "completed":{"a":a_done,"b":b_done},"files":{
                "task-a-output":fs::read_to_string(cwd.join("task-a-output")).unwrap(),
                "task-b-output":fs::read_to_string(cwd.join("task-b-output")).unwrap(),
                "existing-user-change":fs::read_to_string(cwd.join("existing-user-change")).unwrap()}
        })).unwrap()).unwrap();
    }
    f.store.verify_business().unwrap();
}

#[test]
fn unknown_and_unacknowledged_executions_do_not_block_other_tasks_or_cancellation() {
    let mut f = Fixture::new();
    let cwd = json!(f.policy.projects[0].cwd);
    let a_prepared = f.prepare(Value::Null).unwrap();
    let a = f.start(&a_prepared);
    let a = f.send(&a, &cwd, true);
    let unknown = f.observe(&a, "UNKNOWN", "UNKNOWN", "");
    assert_eq!(unknown["status"], "RECOVERY_REQUIRED");
    let b_prepared = f.prepare(Value::Null).unwrap();
    let b = f.start(&b_prepared);
    f.send(&b, &cwd, false); // durable SENDING, ACK remains unknown
    let c_prepared = f.prepare(Value::Null).unwrap();
    let c = f.start(&c_prepared);
    let c_cancelled = f
        .call(
            Role::Api,
            "cancel",
            json!({"execution_ref":c["execution_ref"],"approved":true}),
        )
        .unwrap();
    assert_eq!(c_cancelled["resource_state"], "RELEASED");
    let d_prepared = f.prepare(Value::Null).unwrap();
    let d = f.start(&d_prepared);
    let d_failed = f
        .call(
            Role::Transport,
            "fail_before_send",
            json!({"execution_ref":d["execution_ref"],"reason":"fixture"}),
        )
        .unwrap();
    assert_eq!(d_failed["status"], "FAILED");
    assert_eq!(
        f.call(
            Role::Api,
            "get_status",
            json!({"execution_ref":b["execution_ref"]})
        )
        .unwrap()["dispatch_state"],
        "SENDING"
    );
    assert_eq!(
        f.call(
            Role::Api,
            "get_status",
            json!({"execution_ref":a["execution_ref"]})
        )
        .unwrap()["status"],
        "RECOVERY_REQUIRED"
    );
    f.call(
        Role::Api,
        "cancel",
        json!({"execution_ref":a["execution_ref"],"approved":true}),
    )
    .unwrap();
    assert_eq!(
        f.observe(&a, "idle", "interrupted", "cancelled A")["status"],
        "CANCELLED"
    );
    assert_eq!(
        f.call(
            Role::Api,
            "get_status",
            json!({"execution_ref":b["execution_ref"]})
        )
        .unwrap()["resource_state"],
        "HELD"
    );
    f.store.verify_business().unwrap();
}

#[test]
fn different_repository_rejected_at_prepare_bind_and_adoption() {
    let mut f = Fixture::new();
    let other = f.root.join("other-repository");
    fs::create_dir(&other).unwrap();
    git(&other, &["init"]);
    assert_eq!(
        f.prepare(json!({"cwd":other})).unwrap_err(),
        "WORKSPACE_REPOSITORY_MISMATCH"
    );
    let prepared = f.prepare(Value::Null).unwrap();
    assert_eq!(
        f.call(
            Role::Api,
            "start",
            json!({"approved":true,"prepared_execution_ref":prepared["prepared_execution_ref"],
        "native_cwd":other})
        )
        .unwrap_err(),
        "WORKSPACE_REPOSITORY_MISMATCH"
    );
    let e = f.start(&prepared);
    let take = f
        .call(
            Role::Transport,
            "take_dispatch",
            json!({"execution_ref":e["execution_ref"]}),
        )
        .unwrap();
    assert_eq!(f.call(Role::Transport,"bind_thread",json!({"execution_ref":e["execution_ref"],"epoch":take["epoch"],
        "ticket":take["ticket"],"endpoint":"/fixture/provider.sock","thread_id":"other-thread","cwd":other})).unwrap_err(), "WORKSPACE_REPOSITORY_MISMATCH");
    let adoption = f.call(Role::Api,"adopt",json!({"host":"p620","project":"orion","thread_id":"foreign-thread","title":"foreign","workspace_contract":null})).unwrap();
    let challenge = f
        .call(
            Role::Transport,
            "adoption_begin",
            json!({"adoption_ref":adoption["adoption_ref"]}),
        )
        .unwrap();
    assert_eq!(f.call(Role::Transport,"adoption_commit",json!({"adoption_ref":adoption["adoption_ref"],"nonce":challenge["nonce"],
        "observations":[{"endpoint":"/fixture/provider.sock","thread":{"id":"foreign-thread","cwd":other,"status":{"type":"idle"}}}]})).unwrap_err(), "WORKSPACE_REPOSITORY_MISMATCH");
}

#[test]
fn native_binding_follows_changed_cwd_and_historical_reads_need_no_live_workspace() {
    let mut f = Fixture::new();
    let main = PathBuf::from(&f.policy.projects[0].cwd);
    let linked = f.root.join("actual-native-worktree");
    git(
        &main,
        &["worktree", "add", "-b", "actual", linked.to_str().unwrap()],
    );
    let prepared = f.prepare(Value::Null).unwrap();
    let e = f
        .store
        .business(
            Role::Api,
            &f.policy,
            "start",
            "historical-start",
            &json!({"approved":true,"prepared_execution_ref":prepared["prepared_execution_ref"]}),
        )
        .unwrap();
    let e = f.send(&e, &json!(linked), true);
    assert_eq!(e["workspace_contract"]["cwd"], json!(linked));
    assert_eq!(e["workspace_resolution"]["branch"], "actual");
    let mut done = f.observe(&e, "idle", "completed", "exact historical result");
    done.as_object_mut().unwrap().remove("provider_liveness");
    let receipt = f
        .call(
            Role::Api,
            "read_receipt",
            json!({"operation_id":"historical-start"}),
        )
        .unwrap();
    assert_eq!(receipt["verdict"], "COMMITTED");
    fs::remove_dir_all(&linked).unwrap();
    fs::remove_dir_all(&main).unwrap();
    assert_eq!(
        f.call(
            Role::Api,
            "get_status",
            json!({"execution_ref":e["execution_ref"]})
        )
        .unwrap(),
        done
    );
    assert_eq!(
        f.call(
            Role::Api,
            "get_prepared",
            json!({"prepared_execution_ref":prepared["prepared_execution_ref"]})
        )
        .unwrap()["task_ref"],
        prepared["task_ref"]
    );
    assert_eq!(
        f.call(Role::Api, "list_tasks", json!({})).unwrap()["tasks"][0]["thread_id"],
        e["thread_id"]
    );
    assert_eq!(
        f.call(
            Role::Api,
            "read_receipt",
            json!({"operation_id":"historical-start"})
        )
        .unwrap(),
        receipt
    );
    f.store.verify_business().unwrap();
}

#[test]
fn distinct_worktrees_run_independently_and_retired_registration_is_only_discovery() {
    let mut f = Fixture::new();
    let main = PathBuf::from(&f.policy.projects[0].cwd);
    let registration = main.join("old-registration-cwd");
    fs::create_dir(&registration).unwrap();
    f.policy.projects[0].cwd = registration.to_str().unwrap().into();
    let linked = f.root.join("independent-worktree");
    git(
        &main,
        &[
            "worktree",
            "add",
            "-b",
            "independent",
            linked.to_str().unwrap(),
        ],
    );
    git(
        &linked,
        &[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "different HEAD",
        ],
    );
    let a_prepared = f.prepare(json!({"cwd":main})).unwrap();
    let a = f.start(&a_prepared);
    let a = f.send(&a, &json!(main), true);
    fs::remove_dir(&registration).unwrap();
    // The project has a previously proven common-dir identity. Its old default
    // directory is no longer a mandatory live execution directory.
    let b_prepared = f
        .prepare(json!({"cwd":linked,"branch":"stale","head":"stale"}))
        .unwrap();
    let b = f.start(&b_prepared);
    let b = f.send(&b, &json!(linked), true);
    assert_eq!(a["status"], "RUNNING");
    assert_eq!(b["status"], "RUNNING");
    assert_ne!(
        a["workspace_contract"]["branch"],
        b["workspace_contract"]["branch"]
    );
    assert_ne!(
        a["workspace_contract"]["head"],
        b["workspace_contract"]["head"]
    );
    assert_eq!(
        a["workspace_resolution"]["repository_identity"],
        b["workspace_resolution"]["repository_identity"]
    );
    assert_eq!(
        f.observe(&a, "idle", "completed", "main result")["status"],
        "SUCCEEDED"
    );
    assert_eq!(
        f.observe(&b, "idle", "completed", "linked result")["status"],
        "SUCCEEDED"
    );
    f.store.verify_business().unwrap();
}

#[test]
fn host_commands_share_resources_but_keep_host_execution_identity_unique() {
    let mut f = Fixture::new();
    let cwd = json!(f.policy.projects[0].cwd);
    let scope = f.policy.host_operations[0].clone();
    let mut executions = vec![];
    for index in 0..2 {
        let prepared = f
            .call(
                Role::Api,
                "prepare",
                json!({"approved":true,"project":"orion","host":"p620",
            "title":"Concurrent Host fixture","prompt":"Host fixture", "execution_policy":{
                "execution_surface":"HOST_EXECUTOR","operation_scopes":[scope]}}),
            )
            .unwrap();
        let e = f.start(&prepared);
        let e = f.send(&e, &cwd, true);
        let call = format!("host-call-{index}");
        f.call(Role::Transport,"host_receive",json!({"execution_ref":e["execution_ref"],"call_id":call,
            "request":{"id":index,"params":{"threadId":e["thread_id"],"turnId":e["native_turn_id"],
                "callId":call,"namespace":"clinx","tool":"clinx_host_operation", "arguments":{
                    "capability":"HOST_FILESYSTEM","operation":"path_read","operation_class":"READ_ONLY_HOST","arguments":{}}}},
            "identity":{"thread_id":e["thread_id"],"turn_id":e["native_turn_id"],"provider_endpoint":{"path":"/fixture/provider.sock"}}})).unwrap();
        f.call(
            Role::Transport,
            "host_take",
            json!({"execution_ref":e["execution_ref"],"call_id":call}),
        )
        .unwrap();
        let mut begin = json!({"execution_ref":e["execution_ref"],"call_id":call,"host_ref":"host-a", "evidence":{
            "resource_key":"same-shared-workspace-resource","task_id":e["task_ref"],"execution_ref":e["execution_ref"],
            "host":"p620","capability":"HOST_FILESYSTEM","operation":"path_read","operation_class":"READ_ONLY_HOST","tool_call_id":call}});
        if index == 1 {
            assert_eq!(
                f.call(Role::Transport, "host_begin", begin.clone())
                    .unwrap_err(),
                "HOST_EXECUTION_IDENTITY_CONFLICT"
            );
            begin["host_ref"] = json!("host-b");
        }
        assert_eq!(
            f.call(Role::Transport, "host_begin", begin).unwrap()["send_permitted"],
            true
        );
        executions.push(e);
    }
    f.call(
        Role::Transport,
        "host_complete",
        json!({"execution_ref":executions[0]["execution_ref"],
        "call_id":"host-call-0","host_ref":"host-a","result":{"exit_code":0}}),
    )
    .unwrap();
    assert_eq!(
        f.call(
            Role::Transport,
            "host_rows",
            json!({"execution_ref":executions[1]["execution_ref"]})
        )
        .unwrap()["rows"][0]["call_state"],
        "RUNNING"
    );
    f.store.verify_business().unwrap();
}

#[test]
fn repository_rename_follows_actual_cwd_but_replacement_at_old_path_is_rejected() {
    let mut f = Fixture::new();
    let original = PathBuf::from(&f.policy.projects[0].cwd);
    let prepared = f.prepare(Value::Null).unwrap();
    let moved = f.root.join("renamed-repository");
    fs::rename(&original, &moved).unwrap();
    let e = f
        .call(
            Role::Api,
            "start",
            json!({"approved":true,"prepared_execution_ref":prepared["prepared_execution_ref"],
        "native_cwd":moved}),
        )
        .unwrap();
    let e = f.send(&e, &json!(moved), true);
    assert_eq!(
        e["workspace_resolution"]["repository_identity"],
        prepared["workspace_resolution"]["repository_identity"]
    );
    fs::create_dir(&original).unwrap();
    git(&original, &["init"]);
    assert_eq!(
        f.prepare(json!({"cwd":original})).unwrap_err(),
        "WORKSPACE_REPOSITORY_MISMATCH"
    );
    let next = f.prepare(json!({"cwd":moved})).unwrap();
    assert_ne!(f.start(&next)["task_ref"], e["task_ref"]);
    f.store.verify_business().unwrap();
}
