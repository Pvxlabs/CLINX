//! Refresh an active Observer's stale read-only history mounts. Only stat the six
//! established files; never open history, start an inactive service, or retry.
use crate::node_tls::Result;
use std::{
    fs,
    io::{Read, Seek, SeekFrom},
    os::unix::fs::MetadataExt,
    path::Path,
    process::{Command, Stdio},
    time::{Duration, Instant},
};

const UNIT: &str = "clinx-observer.service";
const FILES: [&str; 6] = [
    "state_5.sqlite",
    "state_5.sqlite-wal",
    "state_5.sqlite-shm",
    "thread_history_1.sqlite",
    "thread_history_1.sqlite-wal",
    "thread_history_1.sqlite-shm",
];
type Identities = [Option<(u64, u64)>; 6];

fn identities(root: &Path) -> Result<Identities> {
    let mut rows = [None; 6];
    for (index, name) in FILES.iter().enumerate() {
        rows[index] = match fs::metadata(root.join(name)) {
            Ok(m) => Some((m.dev(), m.ino())),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
            Err(e) => return Err(format!("HISTORY_GUARD_STAT_FAILED: {e}")),
        };
    }
    Ok(rows)
}

fn active_pid(output: &str) -> Result<Option<u32>> {
    let fields: std::collections::BTreeMap<_, _> = output
        .lines()
        .filter_map(|line| line.split_once('='))
        .collect();
    let pid = fields
        .get("MainPID")
        .unwrap_or(&"0")
        .parse::<u32>()
        .map_err(|_| "HISTORY_GUARD_INVALID_PID")?;
    Ok((fields.get("ActiveState") == Some(&"active") && pid > 0).then_some(pid))
}

fn refresh(
    native: &Path,
    proc_root: &Path,
    mut stat: impl FnMut(&Path) -> Result<Identities>,
    mut pid: impl FnMut() -> Result<Option<u32>>,
    mut restart: impl FnMut() -> Result<()>,
) -> Result<bool> {
    let Some(owner) = pid()? else {
        return Ok(false);
    };
    let mounted = proc_root.join(owner.to_string()).join("root").join(
        native
            .strip_prefix("/")
            .map_err(|_| "HISTORY_GUARD_ABSOLUTE_PATH_REQUIRED")?,
    );
    let before = stat(native)?;
    if before[0].is_none() || before[3].is_none() {
        return Ok(false);
    }
    let mounted = stat(&mounted)?;
    if before != stat(native)? || before == mounted {
        return Ok(false);
    }
    if pid()? != Some(owner) {
        return Ok(false);
    }
    // try-restart respects an operator stop racing the final PID check.
    restart()?;
    Ok(true)
}

fn systemctl(args: &[&str], timeout: Duration) -> Result<String> {
    systemctl_command(Path::new("systemctl"), args, timeout)
}

fn systemctl_command(program: &Path, args: &[&str], timeout: Duration) -> Result<String> {
    // A private file avoids a pipe filling while waiting for a bounded process.
    let mut output = tempfile::tempfile().map_err(|e| e.to_string())?;
    let mut child = Command::new(program)
        .arg("--user")
        .args(args)
        .stdin(Stdio::null())
        .stdout(output.try_clone().map_err(|e| e.to_string())?)
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("HISTORY_GUARD_SYSTEMCTL: {e}"))?;
    let start = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if start.elapsed() < timeout => std::thread::sleep(Duration::from_millis(10)),
            result => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!(
                    "HISTORY_GUARD_SYSTEMCTL_TIMEOUT_OR_WAIT: {result:?}"
                ));
            }
        }
    };
    if !status.success() {
        return Err("HISTORY_GUARD_SYSTEMCTL_FAILED".into());
    }
    output.seek(SeekFrom::Start(0)).map_err(|e| e.to_string())?;
    let mut text = String::new();
    output
        .take(65537)
        .read_to_string(&mut text)
        .map_err(|e| e.to_string())?;
    if text.len() > 65536 {
        return Err("HISTORY_GUARD_OUTPUT_TOO_LARGE".into());
    }
    Ok(text)
}

pub fn run() -> Result<()> {
    if !cfg!(target_os = "linux") {
        return Err("HISTORY_GUARD_REQUIRES_LINUX".into());
    }
    let native = crate::runtime_config::expanduser(Path::new("~/.codex"))?;
    if refresh(
        &native,
        Path::new("/proc"),
        identities,
        || {
            active_pid(&systemctl(
                &["show", UNIT, "-p", "MainPID", "-p", "ActiveState"],
                Duration::from_secs(5),
            )?)
        },
        || systemctl(&["try-restart", UNIT], Duration::from_secs(15)).map(|_| ()),
    )? {
        println!("Observer native history mounts refreshed");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{cell::Cell, path::PathBuf};
    struct Fixture {
        _temp: tempfile::TempDir,
        native: PathBuf,
        proc_root: PathBuf,
        mounted: PathBuf,
    }
    impl Fixture {
        fn new() -> Self {
            let temp = tempfile::tempdir().unwrap();
            let native = temp.path().join("native");
            let proc_root = temp.path().join("proc");
            let mounted = proc_root
                .join("42/root")
                .join(native.strip_prefix("/").unwrap());
            fs::create_dir_all(&native).unwrap();
            fs::create_dir_all(&mounted).unwrap();
            for name in FILES {
                fs::write(native.join(name), b"fixture").unwrap();
                fs::hard_link(native.join(name), mounted.join(name)).unwrap();
            }
            Self {
                _temp: temp,
                native,
                proc_root,
                mounted,
            }
        }
        fn rotate(&self, name: &str) {
            fs::remove_file(self.native.join(name)).unwrap();
            fs::write(self.native.join(name), b"new fixture").unwrap();
        }
        fn check(&self) -> bool {
            let calls = Cell::new(0);
            let changed = refresh(
                &self.native,
                &self.proc_root,
                identities,
                || Ok(Some(42)),
                || {
                    calls.set(calls.get() + 1);
                    Ok(())
                },
            )
            .unwrap();
            assert_eq!(calls.get(), u32::from(changed));
            changed
        }
    }
    #[test]
    fn content_writes_do_not_restart_and_rotation_refreshes_only_observer() {
        let f = Fixture::new();
        fs::write(f.native.join(FILES[4]), b"same inode").unwrap();
        assert!(!f.check());
        f.rotate(FILES[4]);
        assert!(f.check());
    }
    #[test]
    fn optional_companion_disappears_or_appears() {
        let f = Fixture::new();
        fs::remove_file(f.native.join(FILES[2])).unwrap();
        assert!(f.check());
        fs::remove_file(f.mounted.join(FILES[2])).unwrap();
        assert!(!f.check());
        fs::write(f.native.join(FILES[2]), b"recreated").unwrap();
        assert!(f.check());
    }
    #[test]
    fn inactive_or_replaced_process_is_left_alone() {
        let f = Fixture::new();
        assert!(
            !refresh(
                &f.native,
                &f.proc_root,
                |_| panic!("inactive stat"),
                || Ok(None),
                || panic!("inactive restart")
            )
            .unwrap()
        );
        f.rotate(FILES[1]);
        let mut pids = [Some(42), Some(43)].into_iter();
        assert!(
            !refresh(
                &f.native,
                &f.proc_root,
                identities,
                || Ok(pids.next().unwrap()),
                || panic!("replacement restart")
            )
            .unwrap()
        );
    }
    #[test]
    fn missing_database_or_changing_source_waits() {
        let f = Fixture::new();
        let before = identities(&f.native).unwrap();
        f.rotate(FILES[1]);
        let mut rows = [before, [None; 6], identities(&f.native).unwrap()].into_iter();
        assert!(
            !refresh(
                &f.native,
                &f.proc_root,
                |_| Ok(rows.next().unwrap()),
                || Ok(Some(42)),
                || panic!("changing source restart")
            )
            .unwrap()
        );
        fs::remove_file(f.native.join(FILES[0])).unwrap();
        assert!(!f.check());
    }
    #[test]
    fn unknown_stat_error_never_restarts() {
        let f = Fixture::new();
        assert_eq!(
            refresh(
                &f.native,
                &f.proc_root,
                |_| Err("Permission denied".into()),
                || Ok(Some(42)),
                || panic!("unknown stat restart")
            )
            .unwrap_err(),
            "Permission denied"
        );
        assert!(
            refresh(
                &f.native,
                &f.proc_root,
                |_| Ok([Some((1, 1)); 6]),
                || Err("systemctl failed".into()),
                || panic!("unknown pid restart")
            )
            .is_err()
        );
    }
    #[test]
    fn service_state_and_pid_must_be_known_and_active() {
        assert_eq!(
            active_pid("MainPID=42\nActiveState=active\n").unwrap(),
            Some(42)
        );
        for text in [
            "MainPID=42\nActiveState=inactive",
            "MainPID=0\nActiveState=active",
            "",
        ] {
            assert_eq!(active_pid(text).unwrap(), None);
        }
        assert!(active_pid("MainPID=invalid\nActiveState=active").is_err());
    }
    #[test]
    fn command_arguments_failure_and_timeout_use_only_private_executable() {
        use std::os::unix::fs::PermissionsExt;
        let temp = tempfile::tempdir().unwrap();
        let program = temp.path().join("systemctl-fixture");
        fs::write(&program, "#!/bin/sh\nprintf '%s\\n' \"$@\"\n").unwrap();
        fs::set_permissions(&program, fs::Permissions::from_mode(0o700)).unwrap();
        let output =
            systemctl_command(&program, &["try-restart", UNIT], Duration::from_secs(1)).unwrap();
        assert_eq!(output, "--user\ntry-restart\nclinx-observer.service\n");
        fs::write(&program, "#!/bin/sh\nexit 3\n").unwrap();
        assert_eq!(
            systemctl_command(&program, &[], Duration::from_secs(1)).unwrap_err(),
            "HISTORY_GUARD_SYSTEMCTL_FAILED"
        );
        fs::write(&program, "#!/bin/sh\nexec /bin/sleep 10\n").unwrap();
        assert!(
            systemctl_command(&program, &[], Duration::from_millis(40))
                .unwrap_err()
                .contains("TIMEOUT_OR_WAIT")
        );
    }
}
