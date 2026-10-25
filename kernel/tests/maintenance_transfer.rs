//! Private native transfer qualification; no installed identities, tasks or data.
#[path = "support/provider.rs"]
mod provider;
#[path = "support/runtime_qualification.rs"]
mod runtime;
#[path = "../src/maintenance_transfer.rs"]
mod transfer;
use serde_json::{Value, json};
use std::{
    fs::{self, OpenOptions},
    io::{BufRead, BufReader, Write},
    os::unix::{
        fs::{MetadataExt, PermissionsExt, symlink},
        net::{UnixListener, UnixStream},
    },
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{Arc, Mutex},
    thread,
    time::Duration,
};
use transfer::{Config, Io, Result};
const THREAD: &str = "11111111-2222-3333-4444-555555555555";
const TURN: &str = "exact-terminal-turn";
#[derive(Clone, Copy, PartialEq)]
enum Fault {
    None,
    ArchiveUnknown,
    ResumeUnknown,
    ArchiveInode,
    DestinationCwd,
    DestinationThread,
    DeepHistory,
    DestinationTurn,
    DestinationActive,
}
struct NativeIo {
    source: PathBuf,
    destination: PathBuf,
    authority: PathBuf,
    proc: PathBuf,
    calls: Vec<String>,
    child: bool,
}
fn child(op: &str, a: &Value) -> Result<Value> {
    let mut p = Command::new(env!("CARGO_BIN_EXE_clinx-maintenance"))
        .arg(op)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    serde_json::to_writer(p.stdin.take().unwrap(), a).unwrap();
    let out = p.wait_with_output().unwrap();
    let v: Value = serde_json::from_slice(&out.stdout).unwrap();
    if out.status.success() {
        Ok(v["result"].clone())
    } else {
        Err(v["error"].as_str().unwrap().into())
    }
}
impl NativeIo {
    fn provider(&self, endpoint: &Path, action: &str, tid: &str, turn: &str) -> Result<Value> {
        let a = json!({"endpoint":endpoint,"peer_uid":rustix::process::getuid().as_raw(),"action":action,"thread_id":tid,"turn_id":turn});
        if self.child {
            child("io-provider", &a)
        } else {
            transfer::provider_io(&a)
        }
    }
}
impl Io for NativeIo {
    fn source(&mut self, archive: bool, t: &str, u: &str) -> Result<Value> {
        self.calls
            .push(if archive { "archive" } else { "source-read" }.into());
        self.provider(&self.source, if archive { "archive" } else { "read" }, t, u)
    }
    fn destination(&mut self, t: &str, u: &str) -> Result<Value> {
        self.calls.push("resume".into());
        self.provider(&self.destination, "resume", t, u)
    }
    fn adopt(&mut self, arguments: Value, operation_id: &str) -> Result<Value> {
        self.calls.push("adopt".into());
        let a = json!({"endpoint":self.authority,"peer_uid":rustix::process::getuid().as_raw(),"arguments":arguments,"operation_id":operation_id});
        if self.child {
            child("io-authority", &a)
        } else {
            transfer::authority_io(&a)
        }
    }
    fn writable_holders(&mut self, path: &Path) -> Result<Value> {
        transfer::writable_holders(&self.proc, path)
    }
}
struct Fixture {
    runtime: runtime::Runtime,
    config: Config,
    io: NativeIo,
    source: provider::Provider,
    destination: provider::Provider,
    path: PathBuf,
    archive: PathBuf,
    target: PathBuf,
    bytes: Vec<u8>,
    turn: Arc<Mutex<Value>>,
}
fn read(path: &Path) -> Value {
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}
impl Fixture {
    fn new(fault: Fault) -> Self {
        let runtime = runtime::Runtime::new("air", false);
        let root = runtime.temp.path();
        let sessions = root.join("source/sessions");
        let archive_root = root.join("source/archived_sessions");
        let destination_root = root.join("destination/sessions");
        fs::create_dir_all(sessions.join("2026/10/25")).unwrap();
        fs::create_dir_all(&archive_root).unwrap();
        fs::create_dir_all(&destination_root).unwrap();
        let path = sessions.join(format!("2026/10/25/rollout-{THREAD}.jsonl"));
        let archive = archive_root.join(path.file_name().unwrap());
        let target = destination_root.join(path.strip_prefix(&sessions).unwrap());
        let bytes = format!(
            "{}\n{}\n",
            json!({"type":"session_meta","payload":{"id":THREAD,"cwd":runtime.repo}}),
            json!({"type":"test_fixture","payload":"preserved exact bytes"})
        )
        .into_bytes();
        fs::write(&path, &bytes).unwrap();
        let source_receipt = root.join("source-receipt.json");
        fs::write(&source_receipt,json!({"status":{"thread_id":THREAD,"native_turn_id":TURN,"status":"SUCCEEDED","resource_state":"RELEASED"}}).to_string()).unwrap();
        let release = root.join("release");
        fs::create_dir(&release).unwrap();
        fs::write(
            release.join("manifest.json"),
            json!({"source_layout":"qualified-clinx-v1"}).to_string(),
        )
        .unwrap();
        let output = root.join("transfer.json");
        let source_endpoint = root.join("s.sock");
        let proc = root.join("proc");
        fs::create_dir(&proc).unwrap();
        let turn = Arc::new(Mutex::new(
            json!({"id":TURN,"status":"completed","items":[{"id":"result","type":"agentMessage","phase":"final_answer","text":"exact native result"}]}),
        ));
        let (cwd, source_path, archived, receipt, exact) = (
            runtime.repo.clone(),
            path.clone(),
            archive.clone(),
            output.clone(),
            turn.clone(),
        );
        let source = provider::Provider::new(&source_endpoint, move |m| {
            if m.get("id").is_none() {
                assert_eq!(m["method"], "initialized");
                return Some(vec![]);
            }
            let result = match m["method"].as_str().unwrap() {
                "initialize" => {
                    assert_eq!(
                        m["params"]["clientInfo"]["name"],
                        "clinx-exact-native-transfer"
                    );
                    json!({"serverInfo":{"name":"fixture"}})
                }
                "thread/read" => {
                    assert_eq!(m["params"]["threadId"], THREAD);
                    json!({"thread":{"id":THREAD,"cwd":cwd,"path":source_path,"status":{"type":"idle"}}})
                }
                "thread/turns/list" => {
                    let page = m["params"]["cursor"]
                        .as_str()
                        .and_then(|s| s.parse::<u64>().ok())
                        .unwrap_or(0);
                    if fault == Fault::DeepHistory && page < 6 {
                        json!({"data":[],"nextCursor":(page+1).to_string()})
                    } else {
                        json!({"data":[exact.lock().unwrap().clone()],"nextCursor":null})
                    }
                }
                "thread/archive" => {
                    assert_eq!(read(&receipt)["stage"], "ARCHIVE_ATTEMPT");
                    if fault == Fault::ArchiveInode {
                        fs::copy(&source_path, &archived).unwrap();
                        fs::remove_file(&source_path).unwrap();
                    } else {
                        fs::rename(&source_path, &archived).unwrap();
                    }
                    if fault == Fault::ArchiveUnknown {
                        return None;
                    }
                    json!({"archived":true})
                }
                op => panic!("unexpected source operation {op}"),
            };
            Some(vec![json!({"id":m["id"],"result":result})])
        });
        let (cwd, receipt, exact) = (runtime.repo.clone(), output.clone(), turn.clone());
        let destination = provider::Provider::new(&runtime.provider_path, move |m| {
            if m.get("id").is_none() {
                assert_eq!(m["method"], "initialized");
                return Some(vec![]);
            }
            let result = match m["method"].as_str().unwrap() {
                "initialize" => {
                    assert_eq!(
                        m["params"]["clientInfo"]["name"],
                        "clinx-exact-native-import"
                    );
                    json!({})
                }
                "thread/resume" => {
                    assert_eq!(read(&receipt)["stage"], "DESTINATION_RESUME_ATTEMPT");
                    assert_eq!(m["params"], json!({"threadId":THREAD,"excludeTurns":true}));
                    if fault == Fault::ResumeUnknown {
                        return None;
                    }
                    json!({"thread":{"id":if fault==Fault::DestinationThread{"other-thread"}else{THREAD},"cwd":if fault==Fault::DestinationCwd{json!("/other")}else{json!(cwd)},"status":{"type":if fault==Fault::DestinationActive{"active"}else{"idle"}}}})
                }
                "thread/turns/list" => {
                    let mut t = exact.lock().unwrap().clone();
                    if fault == Fault::DestinationTurn {
                        t["items"][0]["text"] = json!("changed")
                    }
                    json!({"data":[t],"nextCursor":null})
                }
                op => panic!("unexpected destination operation {op}"),
            };
            Some(vec![json!({"id":m["id"],"result":result})])
        });
        let config = Config {
            source_receipt,
            source_endpoint: source_endpoint.clone(),
            release,
            output,
            source_sessions: sessions,
            source_archive: archive_root,
            destination_sessions: destination_root,
            worker_uid: rustix::process::getuid().as_raw(),
            worker_gid: rustix::process::getgid().as_raw(),
            host: "air".into(),
            project: "fixture".into(),
            fd_wait: Duration::ZERO,
        };
        let io = NativeIo {
            source: source_endpoint,
            destination: runtime.provider_path.clone(),
            authority: runtime.endpoint.clone(),
            proc,
            calls: vec![],
            child: false,
        };
        Self {
            runtime,
            config,
            io,
            source,
            destination,
            path,
            archive,
            target,
            bytes,
            turn,
        }
    }
    fn execute(&mut self) -> Result<Value> {
        transfer::transfer(&self.config, &mut self.io)
    }
    fn blocked(&self, stage: &str, error: &str) {
        let r = read(&self.config.output);
        assert_eq!(r["status"], "BLOCKED");
        assert_eq!(r["failed_stage"], stage);
        assert_eq!(r["error"], error);
        assert_eq!(r["old_worker_revocation"], "NOT_VERIFIED");
    }
    fn source_count(&self, method: &str) -> usize {
        self.source
            .calls
            .lock()
            .unwrap()
            .iter()
            .filter(|m| m["method"] == method)
            .count()
    }
    fn destination_count(&self, method: &str) -> usize {
        self.destination
            .calls
            .lock()
            .unwrap()
            .iter()
            .filter(|m| m["method"] == method)
            .count()
    }
}
#[test]
fn real_provider_file_copy_and_authority_adoption_keep_exact_identity() {
    let mut f = Fixture::new(Fault::None);
    f.io.child = true;
    f.runtime.launch_authority(true);
    let before = fs::metadata(&f.path).unwrap();
    let r = f.execute().unwrap();
    assert_eq!(r["status"], "TRANSFERRED_PENDING_RUST_ADOPTION");
    assert_eq!(r["stage"], "COMPLETE");
    assert_eq!(r["adoption"]["adoption_status"], "PENDING");
    assert_eq!(r["thread_id"], THREAD);
    assert_eq!(r["exact_turn_id"], TURN);
    assert_eq!(
        r["source_inode"],
        json!({"device":before.dev(),"inode":before.ino()})
    );
    assert_eq!(fs::read(&f.target).unwrap(), f.bytes);
    assert_eq!(fs::read(&f.archive).unwrap(), f.bytes);
    assert!(!f.path.exists());
    assert_eq!(
        fs::metadata(&f.target).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(r["source_writable_holders"], json!([]));
    assert_eq!(r["source"]["turn"], r["destination"]["turn"]);
    assert_eq!(f.io.calls, ["source-read", "archive", "resume", "adopt"]);
    assert_eq!(f.source_count("thread/archive"), 1);
    assert_eq!(f.destination_count("thread/resume"), 1);
    let receipt = f.runtime.call(
        "read_receipt",
        json!({"operation_id":r["adoption_operation_id"]}),
    );
    assert_eq!(receipt["verdict"], "COMMITTED");
    assert_eq!(receipt["result"], r["adoption"]);
    let requests = f.io.calls.clone();
    assert_eq!(f.execute().unwrap_err(), "UNIQUE_RECEIPT_REQUIRED");
    assert_eq!(f.io.calls, requests);
}
#[test]
fn source_receipt_and_unique_output_fail_before_provider_io() {
    for field in ["status", "resource_state", "thread_id", "native_turn_id"] {
        let mut f = Fixture::new(Fault::None);
        let mut receipt = read(&f.config.source_receipt);
        receipt["status"][field] = json!(if field == "native_turn_id" {
            ""
        } else {
            "invalid"
        });
        fs::write(&f.config.source_receipt, receipt.to_string()).unwrap();
        assert!(f.execute().is_err());
        assert!(f.io.calls.is_empty());
        assert!(!f.config.output.exists());
    }
    let mut f = Fixture::new(Fault::None);
    fs::write(&f.config.output, "unrelated").unwrap();
    assert_eq!(f.execute().unwrap_err(), "UNIQUE_RECEIPT_REQUIRED");
    assert_eq!(fs::read_to_string(&f.config.output).unwrap(), "unrelated");
    assert!(f.io.calls.is_empty());
}
#[test]
fn archive_and_resume_ack_loss_are_durable_unknown_without_replay() {
    for fault in [Fault::ArchiveUnknown, Fault::ResumeUnknown] {
        let mut f = Fixture::new(fault);
        assert_eq!(f.execute().unwrap_err(), "PROVIDER_DELIVERY_UNKNOWN");
        f.blocked(
            if fault == Fault::ArchiveUnknown {
                "ARCHIVE_ATTEMPT"
            } else {
                "DESTINATION_RESUME_ATTEMPT"
            },
            "PROVIDER_DELIVERY_UNKNOWN",
        );
        assert_eq!(f.source_count("thread/archive"), 1);
        assert!(!f.io.calls.iter().any(|op| op == "adopt"));
        assert_eq!(f.target.exists(), fault == Fault::ResumeUnknown);
        let calls = f.io.calls.clone();
        assert_eq!(f.execute().unwrap_err(), "UNIQUE_RECEIPT_REQUIRED");
        assert_eq!(f.io.calls, calls);
    }
}
#[test]
fn inode_metadata_and_existing_copy_conflicts_never_resume() {
    let mut f = Fixture::new(Fault::ArchiveInode);
    assert_eq!(f.execute().unwrap_err(), "ARCHIVED_INODE_CONFLICT");
    assert!(!f.target.exists());
    assert_eq!(f.io.calls, ["source-read", "archive"]);
    for key in ["id", "cwd"] {
        let mut f = Fixture::new(Fault::None);
        fs::write(&f.path,format!("{}\n",json!({"type":"session_meta","payload":{"id":if key=="id"{"other"}else{THREAD},"cwd":if key=="cwd"{json!("/other")}else{json!(f.runtime.repo)}}}))).unwrap();
        assert_eq!(
            f.execute().unwrap_err(),
            if key == "id" {
                "ROLLOUT_THREAD_ID_CONFLICT"
            } else {
                "ROLLOUT_WORKSPACE_CONFLICT"
            }
        );
        assert!(!f.target.exists());
        assert_eq!(f.io.calls, ["source-read", "archive"]);
    }
    let mut f = Fixture::new(Fault::None);
    fs::create_dir_all(f.target.parent().unwrap()).unwrap();
    fs::write(&f.target, "existing unique import").unwrap();
    assert_eq!(
        f.execute().unwrap_err(),
        "TARGET_ALREADY_EXISTS_OR_UNAVAILABLE"
    );
    assert_eq!(
        fs::read_to_string(&f.target).unwrap(),
        "existing unique import"
    );
    assert_eq!(f.io.calls, ["source-read", "archive"]);
}
#[test]
fn symlink_source_and_foreign_paths_never_archive() {
    let mut f = Fixture::new(Fault::None);
    let other = f.path.with_extension("other");
    fs::rename(&f.path, &other).unwrap();
    symlink(&other, &f.path).unwrap();
    assert_eq!(f.execute().unwrap_err(), "PATH_IDENTITY_CONFLICT");
    assert_eq!(f.source_count("thread/archive"), 0);
    let mut f = Fixture::new(Fault::None);
    f.config.source_sessions = f.runtime.repo.clone();
    assert_eq!(f.execute().unwrap_err(), "SOURCE_ROLLOUT_PATH_CONFLICT");
    assert_eq!(f.source_count("thread/archive"), 0);
}
#[test]
fn writable_fd_tracks_renamed_inode_and_scan_uncertainty_is_not_empty() {
    use std::os::fd::AsRawFd;
    let mut f = Fixture::new(Fault::None);
    let handle = OpenOptions::new().write(true).open(&f.path).unwrap();
    let pid = std::process::id();
    let proc = f.io.proc.join(pid.to_string());
    fs::create_dir_all(proc.join("fd")).unwrap();
    fs::create_dir_all(proc.join("fdinfo")).unwrap();
    let fd = handle.as_raw_fd();
    symlink(
        format!("/proc/{pid}/fd/{fd}"),
        proc.join("fd").join(fd.to_string()),
    )
    .unwrap();
    symlink(
        format!("/proc/{pid}/fdinfo/{fd}"),
        proc.join("fdinfo").join(fd.to_string()),
    )
    .unwrap();
    assert_eq!(f.execute().unwrap_err(), "SOURCE_WRITABLE_FD_STILL_OPEN");
    f.blocked("ARCHIVE_ACKNOWLEDGED", "SOURCE_WRITABLE_FD_STILL_OPEN");
    assert_eq!(
        transfer::writable_holders(&f.io.proc, &f.archive).unwrap(),
        json!([{"pid":pid,"fd":fd}])
    );
    assert!(!f.target.exists());
    drop(handle);
    assert_eq!(
        transfer::writable_holders(&f.io.proc, &f.archive).unwrap(),
        json!([])
    );
    let proc = f.io.proc.join("99999999");
    fs::create_dir_all(proc.join("fd")).unwrap();
    fs::create_dir_all(proc.join("fdinfo")).unwrap();
    symlink(&f.archive, proc.join("fd/1")).unwrap();
    fs::write(proc.join("fdinfo/1"), "flags: invalid").unwrap();
    assert_eq!(
        transfer::writable_holders(&f.io.proc, &f.archive).unwrap_err(),
        "PROC_SCAN_UNAVAILABLE"
    );
}
#[test]
fn destination_must_match_thread_cwd_turn_and_idle_evidence() {
    for fault in [
        Fault::DestinationCwd,
        Fault::DestinationThread,
        Fault::DestinationTurn,
        Fault::DestinationActive,
    ] {
        let mut f = Fixture::new(fault);
        let e = f.execute().unwrap_err();
        assert_eq!(e, "DESTINATION_EXACT_TURN_CONFLICT");
        assert!(!f.io.calls.iter().any(|op| op == "adopt"));
        assert_eq!(fs::read(&f.target).unwrap(), f.bytes);
        assert_eq!(f.destination_count("thread/resume"), 1);
    }
}
#[test]
fn adoption_lost_ack_keeps_exact_operation_receipt_after_authority_restart() {
    let mut f = Fixture::new(Fault::None);
    f.runtime.launch_authority(true);
    let proxy = f.runtime.temp.path().join("proxy.sock");
    let listener = UnixListener::bind(&proxy).unwrap();
    fs::set_permissions(&proxy, fs::Permissions::from_mode(0o660)).unwrap();
    let endpoint = f.runtime.endpoint.clone();
    let proxy_thread = thread::spawn(move || {
        let (mut caller, _) = listener.accept().unwrap();
        let mut request = String::new();
        BufReader::new(caller.try_clone().unwrap())
            .read_line(&mut request)
            .unwrap();
        let mut authority = UnixStream::connect(endpoint).unwrap();
        authority.write_all(request.as_bytes()).unwrap();
        let mut committed = String::new();
        BufReader::new(authority).read_line(&mut committed).unwrap();
        assert!(!committed.is_empty());
        caller.flush().unwrap();
        drop(caller);
    });
    f.io.authority = proxy;
    assert_eq!(f.execute().unwrap_err(), "AUTHORITY_DELIVERY_UNKNOWN");
    proxy_thread.join().unwrap();
    f.blocked("ADOPTION_ATTEMPT", "AUTHORITY_DELIVERY_UNKNOWN");
    let r = read(&f.config.output);
    assert_eq!(r["adoption_operation_id"].as_str().unwrap().len(), 32);
    assert!(r.get("adoption").is_none());
    assert_eq!(r["adoption_arguments"]["thread_id"], THREAD);
    assert_eq!(r["adoption_arguments"]["exact_turn_id"], TURN);
    f.runtime.restart_authority();
    let recovered = f.runtime.call(
        "read_receipt",
        json!({"operation_id":r["adoption_operation_id"]}),
    );
    assert_eq!(recovered["verdict"], "COMMITTED");
    assert_eq!(recovered["result"]["thread_id"], THREAD);
    let calls = f.io.calls.clone();
    assert_eq!(f.execute().unwrap_err(), "UNIQUE_RECEIPT_REQUIRED");
    assert_eq!(f.io.calls, calls);
}
#[test]
fn source_terminal_evidence_and_provider_peer_identity_fail_before_archive() {
    let mut f = Fixture::new(Fault::None);
    f.turn.lock().unwrap()["status"] = json!("inProgress");
    assert_eq!(
        f.execute().unwrap_err(),
        "SOURCE_EXACT_TERMINAL_EVIDENCE_REQUIRED"
    );
    assert_eq!(f.source_count("thread/archive"), 0);
    let f = Fixture::new(Fault::None);
    assert_eq!(transfer::provider_io(&json!({"endpoint":f.io.source,"peer_uid":rustix::process::getuid().as_raw()+1,"action":"archive","thread_id":THREAD,"turn_id":TURN})).unwrap_err(),"PROVIDER_PEER_IDENTITY_CONFLICT");
    assert_eq!(f.source_count("initialize"), 0);
    assert_eq!(f.source_count("thread/archive"), 0);
}
#[test]
fn exact_provider_history_keeps_existing_hundred_page_contract() {
    let f = Fixture::new(Fault::DeepHistory);
    let r = transfer::provider_io(&json!({"endpoint":f.io.source,"peer_uid":rustix::process::getuid().as_raw(),"action":"read","thread_id":THREAD,"turn_id":TURN})).unwrap();
    assert_eq!(r["turn"], *f.turn.lock().unwrap());
    assert_eq!(f.source_count("thread/turns/list"), 7);
    assert_eq!(f.source_count("thread/archive"), 0);
}
#[test]
fn help_needs_no_root_identity_or_config_and_documents_required_home() {
    let output = Command::new(env!("CARGO_BIN_EXE_clinx-maintenance"))
        .arg("--help")
        .env("PATH", "/no-runtime-tools")
        .output()
        .unwrap();
    assert!(output.status.success());
    assert!(output.stderr.is_empty());
    let text = String::from_utf8(output.stdout).unwrap();
    for required in [
        "--source-receipt",
        "--source-endpoint",
        "--release",
        "--output",
        "--destination-home",
        "clinx-authority",
        "mcp-runtime.json",
    ] {
        assert!(text.contains(required));
    }
}
