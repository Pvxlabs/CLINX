//! Bounded child IO for operator tools. Kills only the private child group.
use std::{
    io::Read,
    os::unix::process::CommandExt,
    process::{Command, Stdio},
    time::{Duration, Instant},
};
struct Owned(std::process::Child, bool);
impl Drop for Owned {
    fn drop(&mut self) {
        if !self.1 {
            if let Some(pid) = rustix::process::Pid::from_raw(self.0.id() as i32) {
                let _ = rustix::process::kill_process_group(pid, rustix::process::Signal::KILL);
            }
            let _ = self.0.wait();
        }
    }
}
pub fn run(args: &[String], timeout: Duration) -> Result<String, String> {
    let mut child = Owned(
        Command::new(args.first().ok_or("COMMAND_REQUIRED")?)
            .args(&args[1..])
            .process_group(0)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| "COMMAND_UNAVAILABLE")?,
        false,
    );
    let output = child.0.stdout.take().unwrap();
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut b = vec![];
        let r = output
            .take(2 * 1024 * 1024 + 1)
            .read_to_end(&mut b)
            .map(|_| b);
        let _ = tx.send(r);
    });
    let deadline = Instant::now() + timeout;
    let (mut bytes, mut status) = (None, None);
    while Instant::now() < deadline {
        if bytes.is_none()
            && let Ok(r) = rx.try_recv()
        {
            let b = r.map_err(|_| "COMMAND_OUTPUT_UNAVAILABLE")?;
            if b.len() > 2 * 1024 * 1024 {
                return Err("COMMAND_OUTPUT_LIMIT".into());
            }
            bytes = Some(b);
        }
        if status.is_none() {
            status = child.0.try_wait().map_err(|_| "COMMAND_OUTCOME_UNKNOWN")?;
        }
        if let (Some(b), Some(s)) = (&bytes, status) {
            if !s.success() {
                return Err("COMMAND_FAILED".into());
            }
            child.1 = true;
            return String::from_utf8(b.clone()).map_err(|_| "COMMAND_OUTPUT_INVALID".into());
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    Err("COMMAND_OUTCOME_UNKNOWN".into())
}
