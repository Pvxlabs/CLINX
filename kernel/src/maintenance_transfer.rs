//! Explicit terminal-session maintenance. Not linked into a runtime entrypoint.
//! Provider acknowledgements never constitute Authority adoption evidence.
use clinx_decision_kernel::{
    mcp_api::{self, Reads},
    native_transport::{Authority, UnixAuthority},
    provider::{Client, Provider},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::{BufRead, BufReader, Read, Seek, SeekFrom, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    time::{Duration, Instant},
};
pub type Result<T> = std::result::Result<T, String>;
#[derive(Clone)]
pub struct Config {
    pub source_receipt: PathBuf,
    pub source_endpoint: PathBuf,
    pub release: PathBuf,
    pub output: PathBuf,
    pub source_sessions: PathBuf,
    pub source_archive: PathBuf,
    pub destination_sessions: PathBuf,
    pub worker_uid: u32,
    pub worker_gid: u32,
    pub host: String,
    pub project: String,
    pub fd_wait: Duration,
}
/// Existing OS identity transport is supplied by the operator process. Tests use
/// their own current UID, temporary sockets, fake procfs and a disposable store.
pub trait Io {
    fn source(&mut self, archive: bool, thread: &str, turn: &str) -> Result<Value>;
    fn destination(&mut self, thread: &str, turn: &str) -> Result<Value>;
    fn adopt(&mut self, arguments: Value, operation_id: &str) -> Result<Value>;
    fn writable_holders(&mut self, path: &Path) -> Result<Value>;
}
fn err<T>(v: std::io::Result<T>, code: &str) -> Result<T> {
    v.map_err(|_| code.into())
}
fn digest(data: &[u8]) -> String {
    format!("{:x}", Sha256::digest(data))
}
fn canonical(path: &Path) -> Result<()> {
    if !path.is_absolute() || err(fs::canonicalize(path), "PATH_UNAVAILABLE")? != path {
        return Err("PATH_IDENTITY_CONFLICT".into());
    }
    Ok(())
}
fn identity(m: &fs::Metadata) -> Value {
    json!({"device":m.dev(),"inode":m.ino()})
}
fn plain(path: &Path) -> Result<fs::Metadata> {
    let m = err(fs::symlink_metadata(path), "PATH_UNAVAILABLE")?;
    if !m.is_file() || m.file_type().is_symlink() {
        return Err("SOURCE_ROLLOUT_PATH_CONFLICT".into());
    }
    Ok(m)
}
fn read_json(path: &Path) -> Result<Value> {
    serde_json::from_slice(&err(fs::read(path), "JSON_FILE_UNAVAILABLE")?)
        .map_err(|_| "JSON_FILE_INVALID".into())
}
struct Receipt {
    path: PathBuf,
    identity: Value,
    value: Value,
}
impl Receipt {
    fn create(path: &Path, value: Value) -> Result<Self> {
        canonical(path.parent().ok_or("RECEIPT_PARENT_REQUIRED")?)?;
        let mut f = err(
            OpenOptions::new()
                .create_new(true)
                .write(true)
                .mode(0o600)
                .open(path),
            "UNIQUE_RECEIPT_REQUIRED",
        )?;
        let bytes = serde_json::to_vec_pretty(&value).map_err(|_| "RECEIPT_INVALID")?;
        err(f.write_all(&bytes), "RECEIPT_WRITE_FAILED")?;
        err(f.sync_all(), "RECEIPT_SYNC_FAILED")?;
        sync_dir(path.parent().unwrap())?;
        Ok(Self {
            path: path.into(),
            identity: identity(&err(f.metadata(), "RECEIPT_IDENTITY_UNKNOWN")?),
            value,
        })
    }
    fn save(&mut self, stage: &str) -> Result<()> {
        if identity(&plain(&self.path)?) != self.identity {
            return Err("RECEIPT_IDENTITY_CONFLICT".into());
        }
        self.value["stage"] = json!(stage);
        let mut temp = err(
            tempfile::Builder::new()
                .prefix(".clinx-transfer-")
                .tempfile_in(self.path.parent().unwrap()),
            "RECEIPT_WRITE_FAILED",
        )?;
        err(
            temp.write_all(&serde_json::to_vec_pretty(&self.value).map_err(|_| "RECEIPT_INVALID")?),
            "RECEIPT_WRITE_FAILED",
        )?;
        err(temp.as_file().sync_all(), "RECEIPT_SYNC_FAILED")?;
        let f = temp
            .persist(&self.path)
            .map_err(|_| "RECEIPT_REPLACE_FAILED")?;
        self.identity = identity(&err(f.metadata(), "RECEIPT_IDENTITY_UNKNOWN")?);
        sync_dir(self.path.parent().unwrap())
    }
}
fn sync_dir(path: &Path) -> Result<()> {
    err(
        err(File::open(path), "DIRECTORY_UNAVAILABLE")?.sync_all(),
        "DIRECTORY_SYNC_FAILED",
    )
}
fn owner(file: &File, uid: u32, gid: u32) -> Result<()> {
    let m = err(file.metadata(), "COPY_METADATA_UNAVAILABLE")?;
    if m.uid() != uid || m.gid() != gid {
        rustix::fs::fchown(
            file,
            Some(rustix::fs::Uid::from_raw(uid)),
            Some(rustix::fs::Gid::from_raw(gid)),
        )
        .map_err(|_| "COPY_OWNER_FAILED")?;
    }
    Ok(())
}
fn directory(path: &Path, uid: u32, gid: u32) -> Result<()> {
    if !path.exists() {
        let parent = path.parent().ok_or("TARGET_PARENT_REQUIRED")?;
        directory(parent, uid, gid)?;
        err(fs::create_dir(path), "TARGET_DIRECTORY_CREATE_FAILED")?;
        err(
            fs::set_permissions(path, fs::Permissions::from_mode(0o700)),
            "TARGET_DIRECTORY_MODE_FAILED",
        )?;
        let f = err(File::open(path), "TARGET_DIRECTORY_UNAVAILABLE")?;
        owner(&f, uid, gid)?;
        err(f.sync_all(), "TARGET_DIRECTORY_SYNC_FAILED")?;
        sync_dir(parent)?;
    }
    canonical(path)?;
    if !err(fs::metadata(path), "TARGET_DIRECTORY_UNAVAILABLE")?.is_dir() {
        return Err("TARGET_DIRECTORY_CONFLICT".into());
    }
    Ok(())
}
fn read_source(path: &Path, expected: &Value) -> Result<File> {
    canonical(path)?;
    if identity(&plain(path)?) != *expected {
        return Err("ARCHIVED_INODE_CONFLICT".into());
    }
    let f = err(
        OpenOptions::new()
            .read(true)
            .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
            .open(path),
        "SOURCE_ROLLOUT_UNAVAILABLE",
    )?;
    if identity(&err(f.metadata(), "SOURCE_ROLLOUT_UNAVAILABLE")?) != *expected {
        return Err("ARCHIVED_INODE_CONFLICT".into());
    }
    Ok(f)
}
fn checksum(f: &mut File) -> Result<String> {
    err(f.seek(SeekFrom::Start(0)), "SOURCE_READ_FAILED")?;
    let mut hash = Sha256::new();
    let mut b = [0; 65536];
    loop {
        let n = err(f.read(&mut b), "SOURCE_READ_FAILED")?;
        if n == 0 {
            break;
        }
        hash.update(&b[..n]);
    }
    Ok(format!("{:x}", hash.finalize()))
}
fn perform(c: &Config, io: &mut impl Io, r: &mut Receipt, thread: &str, turn: &str) -> Result<()> {
    let source = io.source(false, thread, turn)?;
    if source["thread"]["id"] != thread
        || source["thread"]["status"]["type"] != "idle"
        || source["turn"]["id"] != turn
        || source["turn"]["status"] != "completed"
    {
        return Err("SOURCE_EXACT_TERMINAL_EVIDENCE_REQUIRED".into());
    }
    let path = PathBuf::from(
        source["thread"]["path"]
            .as_str()
            .ok_or("SOURCE_ROLLOUT_PATH_CONFLICT")?,
    );
    canonical(&c.source_sessions)?;
    canonical(&path)?;
    let relative = path
        .strip_prefix(&c.source_sessions)
        .map_err(|_| "SOURCE_ROLLOUT_PATH_CONFLICT")?;
    if relative.as_os_str().is_empty() {
        return Err("SOURCE_ROLLOUT_PATH_CONFLICT".into());
    }
    let before = identity(&plain(&path)?);
    r.value["source"] = source.clone();
    r.value["source_inode"] = before.clone();
    r.value["source_endpoint"] = json!(c.source_endpoint);
    r.save("SOURCE_VERIFIED")?;
    r.save("ARCHIVE_ATTEMPT")?;
    let archived = io.source(true, thread, turn)?;
    if archived["thread"]["path"] != json!(path)
        || archived["turn"] != source["turn"]
        || archived["thread"]["cwd"] != source["thread"]["cwd"]
    {
        return Err("SOURCE_CHANGED_DURING_ARCHIVE".into());
    }
    r.value["archive_ack"] = archived["archive_ack"].clone();
    r.save("ARCHIVE_ACKNOWLEDGED")?;
    let archive = c
        .source_archive
        .join(path.file_name().ok_or("SOURCE_ROLLOUT_PATH_CONFLICT")?);
    let mut file = read_source(&archive, &before)?;
    let deadline = Instant::now() + c.fd_wait;
    loop {
        let holders = io.writable_holders(&archive)?;
        if holders == json!([]) {
            break;
        }
        if Instant::now() >= deadline {
            return Err("SOURCE_WRITABLE_FD_STILL_OPEN".into());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    let mut first = vec![];
    err(
        BufReader::new((&mut file).take((clinx_decision_kernel::provider::MAX_MESSAGE + 1) as u64))
            .read_until(b'\n', &mut first),
        "ROLLOUT_METADATA_UNAVAILABLE",
    )?;
    if first.len() > clinx_decision_kernel::provider::MAX_MESSAGE {
        return Err("ROLLOUT_METADATA_INVALID".into());
    }
    let metadata: Value = serde_json::from_slice(&first).map_err(|_| "ROLLOUT_METADATA_INVALID")?;
    if metadata["type"] != "session_meta" || metadata["payload"]["id"] != thread {
        return Err("ROLLOUT_THREAD_ID_CONFLICT".into());
    }
    if metadata["payload"]["cwd"] != source["thread"]["cwd"] {
        return Err("ROLLOUT_WORKSPACE_CONFLICT".into());
    }
    let source_hash = checksum(&mut file)?;
    let target = c.destination_sessions.join(relative);
    r.value["target_path"] = json!(target);
    r.value["archived_path"] = json!(archive);
    r.value["source_sha256"] = json!(source_hash);
    r.save("COPY_ATTEMPT")?;
    directory(
        target.parent().ok_or("TARGET_PARENT_REQUIRED")?,
        c.worker_uid,
        c.worker_gid,
    )?;
    let mut out = err(
        OpenOptions::new()
            .create_new(true)
            .read(true)
            .write(true)
            .mode(0o600)
            .open(&target),
        "TARGET_ALREADY_EXISTS_OR_UNAVAILABLE",
    )?;
    owner(&out, c.worker_uid, c.worker_gid)?;
    err(file.seek(SeekFrom::Start(0)), "SOURCE_READ_FAILED")?;
    err(std::io::copy(&mut file, &mut out), "COPY_FAILED")?;
    err(out.sync_all(), "COPY_SYNC_FAILED")?;
    sync_dir(target.parent().unwrap())?;
    let target_inode = identity(&err(out.metadata(), "TARGET_READ_FAILED")?);
    if identity(&plain(&target)?) != target_inode {
        return Err("TARGET_IDENTITY_CONFLICT".into());
    }
    let mut current = read_source(&archive, &before)?;
    if checksum(&mut current)? != source_hash
        || checksum(&mut out)? != source_hash
        || io.writable_holders(&archive)? != json!([])
    {
        return Err("SOURCE_CHANGED_DURING_COPY".into());
    }
    drop(out);
    r.value["target_inode"] = target_inode;
    r.value["source_writable_holders"] = json!([]);
    r.save("COPY_VERIFIED")?;
    r.save("DESTINATION_RESUME_ATTEMPT")?;
    let destination = io.destination(thread, turn)?;
    if destination["thread"]["id"] != thread
        || destination["thread"]["cwd"] != source["thread"]["cwd"]
        || destination["thread"]["status"]["type"] != "idle"
        || destination["turn"] != source["turn"]
    {
        return Err("DESTINATION_EXACT_TURN_CONFLICT".into());
    }
    r.value["destination"] = destination;
    r.save("DESTINATION_VERIFIED")?;
    let operation_id = mcp_api::operation_id()?;
    let arguments = json!({"host":c.host,"project":c.project,"thread_id":thread,"exact_turn_id":turn,"workspace_contract":{"cwd":source["thread"]["cwd"]},"title":"Transferred exact native acceptance session"});
    r.value["adoption_operation_id"] = json!(operation_id);
    r.value["adoption_arguments"] = arguments.clone();
    r.save("ADOPTION_ATTEMPT")?;
    r.value["adoption"] = io.adopt(arguments, &operation_id)?;
    r.value["status"] = json!("TRANSFERRED_PENDING_RUST_ADOPTION");
    r.save("COMPLETE")
}
pub fn transfer(c: &Config, io: &mut impl Io) -> Result<Value> {
    if read_json(&c.release.join("manifest.json"))?["source_layout"] != "qualified-clinx-v1" {
        return Err("QUALIFIED_PACKAGE_LAYOUT_REQUIRED".into());
    }
    let bytes = err(fs::read(&c.source_receipt), "SOURCE_RECEIPT_UNAVAILABLE")?;
    let receipt: Value = serde_json::from_slice(&bytes).map_err(|_| "SOURCE_RECEIPT_INVALID")?;
    let original = &receipt["status"];
    if original["status"] != "SUCCEEDED" || original["resource_state"] != "RELEASED" {
        return Err("EXACT_TERMINAL_SOURCE_RECEIPT_REQUIRED".into());
    }
    let thread = original["thread_id"]
        .as_str()
        .filter(|s| {
            s.len() == 36
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b) || b == b'-')
        })
        .ok_or("INVALID_THREAD_ID")?;
    let turn = original["native_turn_id"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or("EXACT_NATIVE_TURN_REQUIRED")?;
    let mut r = Receipt::create(
        &c.output,
        json!({"status":"STARTED","stage":"STARTED","scope":"EXACT_NATIVE_SESSION_TRANSFER","thread_id":thread,"exact_turn_id":turn,"old_worker_revocation":"NOT_VERIFIED","source_receipt_sha256":digest(&bytes)}),
    )?;
    if let Err(e) = perform(c, io, &mut r, thread, turn) {
        let stage = r.value["stage"].clone();
        r.value["status"] = json!("BLOCKED");
        r.value["error"] = json!(e);
        r.value["failed_stage"] = stage;
        r.save("BLOCKED")?;
        return Err(e);
    }
    Ok(r.value)
}
/// Match by (device,inode), including renamed and unlinked files. Permission or
/// malformed fdinfo is UNKNOWN, never an empty holder set.
pub fn writable_holders(root: &Path, path: &Path) -> Result<Value> {
    let expected = identity(&plain(path)?);
    let mut holders = vec![];
    for proc in err(fs::read_dir(root), "PROC_SCAN_UNAVAILABLE")? {
        let proc = err(proc, "PROC_SCAN_UNAVAILABLE")?;
        if !proc
            .file_name()
            .to_string_lossy()
            .bytes()
            .all(|b| b.is_ascii_digit())
        {
            continue;
        }
        let entries = match fs::read_dir(proc.path().join("fd")) {
            Ok(v) => v,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(_) => return Err("PROC_SCAN_UNAVAILABLE".into()),
        };
        for fd in entries {
            let fd = err(fd, "PROC_SCAN_UNAVAILABLE")?;
            let metadata = match fs::metadata(fd.path()) {
                Ok(v) => v,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
                Err(_) => return Err("PROC_SCAN_UNAVAILABLE".into()),
            };
            if identity(&metadata) != expected {
                continue;
            }
            let info = match fs::read_to_string(proc.path().join("fdinfo").join(fd.file_name())) {
                Ok(v) => v,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
                Err(_) => return Err("PROC_SCAN_UNAVAILABLE".into()),
            };
            let flags = info
                .lines()
                .find_map(|l| l.strip_prefix("flags:"))
                .and_then(|s| u64::from_str_radix(s.trim(), 8).ok())
                .ok_or("PROC_SCAN_UNAVAILABLE")?;
            if flags & 3 != 0 {
                holders.push(json!({"pid":proc.file_name().to_string_lossy().parse::<u64>().map_err(|_|"PROC_SCAN_UNAVAILABLE")?,"fd":fd.file_name().to_string_lossy().parse::<u64>().map_err(|_|"PROC_SCAN_UNAVAILABLE")?}));
            }
        }
    }
    Ok(json!(holders))
}
struct Evidence<'a>(&'a mut Client);
impl Reads for Evidence<'_> {
    fn thread(&mut self, tid: &str) -> Result<Value> {
        self.0.thread(tid)
    }
    fn page(&mut self, tid: &str, limit: u64, cursor: Option<&str>) -> Result<Value> {
        let mut p = json!({"threadId":tid,"limit":limit,"itemsView":"full","sortDirection":"desc"});
        if let Some(c) = cursor {
            p["cursor"] = json!(c)
        }
        self.0.request("thread/turns/list", p)
    }
    fn desktop(&mut self, _: &str, _: bool, _: u64, _: usize, _: Option<&str>) -> Result<Value> {
        Err("UNSUPPORTED_MAINTENANCE_OPERATION".into())
    }
}
pub fn provider_io(a: &Value) -> Result<Value> {
    let endpoint = Path::new(a["endpoint"].as_str().ok_or("PROVIDER_ENDPOINT_REQUIRED")?);
    let uid = u32::try_from(a["peer_uid"].as_u64().ok_or("PROVIDER_IDENTITY_REQUIRED")?)
        .map_err(|_| "PROVIDER_IDENTITY_REQUIRED")?;
    let mut client = Client::connect_uninitialized(endpoint, Duration::from_secs(20))?;
    if client.local_peer_uid()? != uid {
        return Err("PROVIDER_PEER_IDENTITY_CONFLICT".into());
    }
    let action = a["action"]
        .as_str()
        .ok_or("UNSUPPORTED_MAINTENANCE_OPERATION")?;
    if !["read", "archive", "resume"].contains(&action) {
        return Err("UNSUPPORTED_MAINTENANCE_OPERATION".into());
    }
    let initialized = client.request("initialize",json!({"clientInfo":{"name":if action=="resume"{"clinx-exact-native-import"}else{"clinx-exact-native-transfer"},"title":"Clinx","version":"1"},"capabilities":{"experimentalApi":true}}))?;
    if !initialized.is_object() {
        return Err("PROVIDER_INITIALIZE_INVALID".into());
    }
    client.send_message(&json!({"method":"initialized"}))?;
    let tid = a["thread_id"].as_str().ok_or("INVALID_THREAD_ID")?;
    let turn = a["turn_id"].as_str().ok_or("EXACT_NATIVE_TURN_REQUIRED")?;
    let thread = if action == "resume" {
        client.request("thread/resume", json!({"threadId":tid,"excludeTurns":true}))?["thread"]
            .clone()
    } else {
        client.thread(tid)?
    };
    let exact = Evidence(&mut client).exact(tid, turn)?;
    if thread["id"] != tid || thread["status"]["type"] != "idle" || exact["status"] != "completed" {
        return Err(if action == "resume" {
            "DESTINATION_EXACT_TURN_CONFLICT"
        } else {
            "SOURCE_EXACT_TERMINAL_EVIDENCE_REQUIRED"
        }
        .into());
    }
    let mut result = json!({"thread":thread,"turn":exact});
    if action == "archive" {
        result["archive_ack"] = client.request("thread/archive", json!({"threadId":tid}))?;
    }
    Ok(result)
}
pub fn authority_io(a: &Value) -> Result<Value> {
    let mut client = UnixAuthority {
        endpoint: PathBuf::from(
            a["endpoint"]
                .as_str()
                .ok_or("AUTHORITY_ENDPOINT_REQUIRED")?,
        ),
        uid: u32::try_from(
            a["peer_uid"]
                .as_u64()
                .ok_or("AUTHORITY_IDENTITY_REQUIRED")?,
        )
        .map_err(|_| "AUTHORITY_IDENTITY_REQUIRED")?,
    };
    client.call(
        "adopt",
        a["arguments"].clone(),
        a["operation_id"].as_str().ok_or("OPERATION_ID_REQUIRED")?,
    )
}
