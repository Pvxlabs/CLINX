//! Real private-process checks for bounded operator-tool command capture.
#[path = "../src/devops_command.rs"]
mod command;
use std::{
    fs,
    process::{Child, Command},
    time::{Duration, Instant},
};

struct Owned(Child);
impl Drop for Owned {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[test]
fn capture_preserves_success_and_refuses_failed_or_excessive_output() {
    let timeout = Duration::from_secs(2);
    assert_eq!(
        command::run(
            &["/usr/bin/printf".into(), "private output".into()],
            timeout
        )
        .unwrap(),
        "private output"
    );
    assert_eq!(
        command::run(&["/bin/false".into()], timeout).unwrap_err(),
        "COMMAND_FAILED"
    );
    assert_eq!(
        command::run(
            &[
                "/usr/bin/head".into(),
                "-c".into(),
                "2097153".into(),
                "/dev/zero".into(),
            ],
            timeout,
        )
        .unwrap_err(),
        "COMMAND_OUTPUT_LIMIT"
    );
}

#[test]
fn exited_leader_with_descendant_holding_stdout_obeys_deadline_and_private_cleanup() {
    let temp = tempfile::tempdir().unwrap();
    let receipt = temp.path().join("owned-pids");
    let mut bystander = Owned(Command::new("/bin/sleep").arg("20").spawn().unwrap());
    let started = Instant::now();
    let result = command::run(
        &[
            "/bin/sh".into(),
            "-c".into(),
            // The leader exits successfully; the descendant inherits stdout
            // and keeps the capture pipe open beyond the total command budget.
            "sleep 20 & printf '%s %s' \"$$\" \"$!\" > \"$1\"; exit 0".into(),
            "private-command-qualification".into(),
            receipt.display().to_string(),
        ],
        Duration::from_millis(300),
    );
    assert_eq!(result.unwrap_err(), "COMMAND_OUTCOME_UNKNOWN");
    assert!(started.elapsed() < Duration::from_secs(2));
    let pids: Vec<u64> = fs::read_to_string(receipt)
        .unwrap()
        .split_whitespace()
        .map(|v| v.parse().unwrap())
        .collect();
    assert_eq!(pids.len(), 2);
    let deadline = Instant::now() + Duration::from_secs(2);
    while clinx_decision_kernel::tunnel_supervisor::invoke(
        "tunnel.group_live",
        &serde_json::json!({"pgid":pids[0]}),
    )
    .unwrap()
        == true
    {
        assert!(
            Instant::now() < deadline,
            "owned descendant survived timeout"
        );
        std::thread::sleep(Duration::from_millis(5));
    }
    assert!(
        clinx_decision_kernel::tunnel_supervisor::process_start(&serde_json::json!(pids[1]))
            .is_none()
    );
    assert!(bystander.0.try_wait().unwrap().is_none());
}
