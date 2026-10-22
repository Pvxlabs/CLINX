//! Bounded stdio-to-existing-Owner transport. No DB or journal handles.
#![forbid(unsafe_code)]

use serde_json::{json, Value};
use std::{
    io::{self, BufRead, BufReader, Read, Write},
    os::unix::{fs::MetadataExt, net::UnixStream},
    path::Path,
    time::Duration,
};

const MAX: usize = 256 * 1024;

fn request(endpoint: &Path, database: &str, frame: &[u8]) -> Result<Value, &'static str> {
    let mut value: Value = serde_json::from_slice(frame).map_err(|_| "INVALID_REQUEST")?;
    let object = value.as_object_mut().ok_or("INVALID_REQUEST")?;
    if object.len() != 2
        || !object.get("payload").is_some_and(Value::is_object)
        || !matches!(
            object.get("action").and_then(Value::as_str),
            Some("deliver" | "commit" | "read_commit")
        )
    {
        return Err("INVALID_REQUEST");
    }
    object.insert("database".into(), json!(database));
    let mut bytes = serde_json::to_vec(&value).map_err(|_| "INVALID_REQUEST")?;
    bytes.push(b'\n');
    if bytes.len() > MAX {
        return Err("INVALID_REQUEST");
    }
    let uid = rustix::process::getuid();
    // Pinned by the existing service launcher, never by a request frame.
    let owner_uid = match std::env::var("CLINX_OWNER_UID") {
        Ok(value) => value.parse::<u32>().map_err(|_| "OWNER_UID_INVALID")?,
        Err(std::env::VarError::NotPresent) => uid.as_raw(),
        Err(_) => return Err("OWNER_UID_INVALID"),
    };
    let forbidden = if owner_uid == uid.as_raw() {
        0o077
    } else {
        0o007
    };
    let metadata = endpoint
        .symlink_metadata()
        .map_err(|_| "OWNER_UNAVAILABLE")?;
    use std::os::unix::fs::FileTypeExt;
    let parent = endpoint
        .parent()
        .ok_or("OWNER_ENDPOINT_UNTRUSTED")?
        .symlink_metadata()
        .map_err(|_| "OWNER_UNAVAILABLE")?;
    if !metadata.file_type().is_socket()
        || metadata.uid() != owner_uid
        || metadata.mode() & forbidden != 0
        || !parent.is_dir()
        || parent.uid() != owner_uid
        || parent.mode() & 0o022 != 0
    {
        return Err("OWNER_ENDPOINT_UNTRUSTED");
    }
    let mut socket = UnixStream::connect(endpoint).map_err(|_| "OWNER_UNAVAILABLE")?;
    let peer =
        rustix::net::sockopt::socket_peercred(&socket).map_err(|_| "OWNER_PEER_UNTRUSTED")?;
    if peer.uid.as_raw() != owner_uid {
        return Err("OWNER_PEER_UNTRUSTED");
    }
    let timeout = Some(Duration::from_secs(90));
    socket
        .set_read_timeout(timeout)
        .and_then(|_| socket.set_write_timeout(timeout))
        .map_err(|_| "OWNER_UNAVAILABLE")?;
    // Any partial send/EOF/invalid reply is UNKNOWN. Never retry or dispatch
    // locally. The caller can read_commit using the original operation tuple.
    socket
        .write_all(&bytes)
        .map_err(|_| "EXECUTION_OWNER_DELIVERY_UNKNOWN")?;
    let mut response = Vec::new();
    BufReader::new(socket)
        .take((MAX + 1) as u64)
        .read_until(b'\n', &mut response)
        .map_err(|_| "EXECUTION_OWNER_DELIVERY_UNKNOWN")?;
    if response.len() > MAX || response.last() != Some(&b'\n') {
        return Err("EXECUTION_OWNER_DELIVERY_UNKNOWN");
    }
    let result: Value =
        serde_json::from_slice(&response).map_err(|_| "EXECUTION_OWNER_DELIVERY_UNKNOWN")?;
    if !result.is_object() || (result.get("result").is_none() && result.get("error").is_none()) {
        return Err("EXECUTION_OWNER_DELIVERY_UNKNOWN");
    }
    Ok(result)
}

fn main() -> Result<(), String> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 5
        || args[1] != "--socket"
        || args[3] != "--database"
        || !Path::new(&args[2]).is_absolute()
        || !Path::new(&args[4]).is_absolute()
    {
        return Err(
            "usage: clinx-owner-client --socket EXISTING_SOCKET --database CANONICAL_DB_IDENTITY"
                .into(),
        );
    }
    let stdin = io::stdin();
    let mut input = stdin.lock();
    let stdout = io::stdout();
    let mut output = stdout.lock();
    loop {
        let mut frame = Vec::new();
        let n = (&mut input)
            .take((MAX + 1) as u64)
            .read_until(b'\n', &mut frame)
            .map_err(|e| e.to_string())?;
        if n == 0 {
            return Ok(());
        }
        let response = if n > MAX || frame.last() != Some(&b'\n') {
            json!({"error":"INVALID_REQUEST"})
        } else {
            match request(Path::new(&args[2]), &args[4], &frame) {
                Ok(response) => response,
                Err(error) => json!({"error":error}),
            }
        };
        serde_json::to_writer(&mut output, &response).map_err(|e| e.to_string())?;
        output
            .write_all(b"\n")
            .and_then(|_| output.flush())
            .map_err(|e| e.to_string())?;
        if n > MAX {
            return Ok(());
        }
    }
}
