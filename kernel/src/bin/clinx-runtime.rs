//! Rust-owned SQLite authority endpoint. No Python Owner or subprocess fallback.
#![forbid(unsafe_code)]
use clinx_decision_kernel::{
    MAX_FRAME_BYTES,
    authority_store::{PROTOCOL, Store},
    business::{Policy, Role},
    parse_protocol_request,
};
use serde_json::json;
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    os::unix::{
        fs::{MetadataExt, PermissionsExt},
        net::UnixListener,
    },
    path::PathBuf,
    time::Duration,
};

fn main() -> Result<(), String> {
    let mut args = std::env::args().skip(1);
    let mut state = None;
    let mut endpoint = None;
    let mut caller_uid = None;
    let mut transport_uid = None;
    let mut policy_path = None;
    let mut init = false;
    let mut init_only = false;
    let mut qualification = false;
    let mut qualification_observer = None;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--state-dir" => state = args.next().map(PathBuf::from),
            "--socket" => endpoint = args.next().map(PathBuf::from),
            "--policy" => policy_path = args.next().map(PathBuf::from),
            "--transport-uid" => transport_uid = Some(args.next().ok_or("MISSING_TRANSPORT_UID")?.parse::<u32>().map_err(|_|"INVALID_TRANSPORT_UID")?),
            "--caller-uid" => caller_uid = Some(args.next().ok_or("MISSING_CALLER_UID")?.parse::<u32>().map_err(|_|"INVALID_CALLER_UID")?),
            "--init" => init = true,
            "--init-only" => {init = true; init_only = true;},
            "--qualification" => qualification = true,
            "--qualification-workspace-observer" => qualification_observer = args.next().map(PathBuf::from),
            _ => return Err("usage: clinx-runtime --state-dir PRIVATE_DIR --socket ABSOLUTE_SOCKET --caller-uid UID [--init] [--qualification]".into()),
        }
    }
    let state = state.ok_or("MISSING_STATE_DIRECTORY")?;
    let endpoint = endpoint.ok_or("MISSING_SOCKET")?;
    let caller_uid = caller_uid.ok_or("MISSING_CALLER_UID")?;
    let uid = rustix::process::getuid().as_raw();
    if !qualification && (uid == 0 || caller_uid == uid) {
        return Err("DISTINCT_UNPRIVILEGED_AUTHORITY_PRINCIPAL_REQUIRED".into());
    }
    if !qualification
        && (policy_path.is_none()
            || transport_uid.is_none_or(|t| t == uid || t == caller_uid || t == 0))
    {
        return Err("DISTINCT_TRANSPORT_PRINCIPAL_AND_POLICY_REQUIRED".into());
    }
    let policy: Option<Policy> = if let Some(path) = policy_path {
        if !qualification {
            for ancestor in path.ancestors() {
                let m = ancestor.symlink_metadata().map_err(|e| e.to_string())?;
                if m.file_type().is_symlink() || m.uid() != 0 || m.mode() & 0o022 != 0 {
                    return Err("ROOT_CONTROLLED_POLICY_REQUIRED".into());
                }
            }
        }
        Some(
            serde_json::from_slice(&fs::read(path).map_err(|e| e.to_string())?)
                .map_err(|_| "INVALID_POLICY")?,
        )
    } else {
        None
    };
    if qualification_observer.is_some() && !qualification {
        return Err("QUALIFICATION_OBSERVER_REQUIRES_QUALIFICATION".into());
    }
    if !qualification || qualification_observer.is_some() {
        let program = if let Some(program) = qualification_observer {
            program
        } else {
            std::env::current_exe()
                .map_err(|_| "WORKSPACE_OBSERVER_UNAVAILABLE")?
                .parent()
                .ok_or("WORKSPACE_OBSERVER_UNAVAILABLE")?
                .join("clinx-provider")
        };
        let info = program
            .symlink_metadata()
            .map_err(|_| "WORKSPACE_OBSERVER_UNAVAILABLE")?;
        if !info.is_file()
            || info.mode() & 0o111 == 0
            || (!qualification && (info.uid() != 0 || info.mode() & 0o022 != 0))
        {
            return Err("ROOT_CONTROLLED_WORKSPACE_OBSERVER_REQUIRED".into());
        }
        let policy = policy.as_ref().ok_or("BUSINESS_POLICY_REQUIRED")?;
        let routes = if policy.provider_routes.is_empty() {
            std::collections::BTreeMap::from([(
                "*".into(),
                policy
                    .endpoints
                    .first()
                    .ok_or("BUSINESS_POLICY_REQUIRED")?
                    .clone(),
            )])
        } else {
            policy
                .provider_routes
                .iter()
                .map(|(host, endpoints)| {
                    if endpoints.len() != 1 {
                        return Err("EXACT_WORKSPACE_PROVIDER_REQUIRED");
                    }
                    Ok((host.clone(), endpoints[0].clone()))
                })
                .collect::<Result<_, &str>>()?
        };
        if qualification {
            clinx_decision_kernel::business::workspace_observer::configure_qualification(
                program, routes, uid,
            )?;
        } else {
            clinx_decision_kernel::business::workspace_observer::configure_routes(program, routes)?;
        }
    }
    if !qualification {
        // Protect the path's parent identities, not merely the current inode.
        for ancestor in state.ancestors() {
            let m = ancestor.symlink_metadata().map_err(|e| e.to_string())?;
            if !m.is_dir() || ![0, uid].contains(&m.uid()) || m.mode() & 0o022 != 0 {
                return Err("PROTECTED_STATE_ANCESTRY_REQUIRED".into());
            }
        }
    }
    let parent = endpoint.parent().ok_or("INVALID_SOCKET")?;
    let metadata = parent.symlink_metadata().map_err(|e| e.to_string())?;
    if !endpoint.is_absolute()
        || parent.canonicalize().map_err(|e| e.to_string())? != parent
        || !metadata.is_dir()
        || metadata.uid() != uid
        || metadata.mode() & 0o022 != 0
    {
        return Err("PROTECTED_SOCKET_DIRECTORY_REQUIRED".into());
    }
    // Serialize startup/recovery before removing only this store's stale socket.
    let mut store = Store::open(&state, init)?;
    if let Some(policy) = &policy {
        store.configure_business(policy, init)?;
    }
    if init_only {
        return Ok(());
    }
    if endpoint.exists() {
        use std::os::unix::fs::FileTypeExt;
        let m = endpoint.symlink_metadata().map_err(|e| e.to_string())?;
        if !m.file_type().is_socket() || m.uid() != uid {
            return Err("SOCKET_IDENTITY_CONFLICT".into());
        }
        if std::os::unix::net::UnixStream::connect(&endpoint).is_ok() {
            return Err("ENDPOINT_ALREADY_ACTIVE".into());
        }
        fs::remove_file(&endpoint).map_err(|e| e.to_string())?;
    }
    let listener = UnixListener::bind(&endpoint).map_err(|e| e.to_string())?;
    fs::set_permissions(&endpoint, fs::Permissions::from_mode(0o660)).map_err(|e| e.to_string())?;
    for stream in listener.incoming() {
        let Ok(mut stream) = stream else { continue };
        let peer = rustix::net::sockopt::socket_peercred(&stream).map_err(|e| e.to_string())?;
        if peer.uid.as_raw() != caller_uid && Some(peer.uid.as_raw()) != transport_uid {
            continue;
        }
        let timeout = Some(Duration::from_secs(5));
        stream
            .set_read_timeout(timeout)
            .and_then(|_| stream.set_write_timeout(timeout))
            .map_err(|e| e.to_string())?;
        let mut frame = Vec::new();
        if BufReader::new(&stream)
            .take((MAX_FRAME_BYTES + 1) as u64)
            .read_until(b'\n', &mut frame)
            .is_err()
        {
            continue;
        }
        let mut request_id = String::new();
        let result = if frame.len() > MAX_FRAME_BYTES || frame.last() != Some(&b'\n') {
            Err("INVALID_FRAME".into())
        } else {
            parse_protocol_request(&frame).map_err(|_|"INVALID_REQUEST".to_owned()).and_then(|req| {
                request_id = req.request_id;
                if req.protocol_version != PROTOCOL || request_id.is_empty() || request_id.len() > 128 { return Err("INVALID_PROTOCOL".into()); }
                if let Some(op) = req.operation.strip_prefix("business.") {
                    let policy = policy.as_ref().ok_or("BUSINESS_POLICY_REQUIRED")?;
                    let transport = clinx_decision_kernel::business::transport_operation(op);
                    if (transport && Some(peer.uid.as_raw()) != transport_uid) || (!transport && peer.uid.as_raw() != caller_uid && Some(peer.uid.as_raw()) != transport_uid) { return Err("PEER_ROLE_DENIED".into()); }
                    // Transport receipt reads remain scoped to its authenticated UID.
                    let role = if transport || (op == "read_receipt" && Some(peer.uid.as_raw()) == transport_uid && transport_uid != Some(caller_uid)) { Role::Transport } else { Role::Api };
                    return store.business(role, policy, op, &request_id, &req.payload);
                }
                match req.operation.as_str() {
                    "claim" if qualification && policy.is_none() => store.claim(&req.payload),
                    "commit" if qualification && policy.is_none() => store.execute(&req.payload),
                    "renew" if qualification && policy.is_none() => store.renew(&req.payload),
                    "read_commit" if qualification && policy.is_none() => store.read_commit(&req.payload),
                    "health" if req.payload == json!({}) => Ok(json!({"authority_id":store.identity,"storage":"RUST_SQLITE","contract":"CLINX_EXECUTION_CORE_V1","qualification":qualification,"business_commands":policy.is_some()})),
                    _ => Err("UNSUPPORTED_OPERATION".into()),
                }
            })
        };
        let response = match result {
            Ok(value) => {
                json!({"protocol_version":PROTOCOL,"request_id":request_id,"result":value})
            }
            Err(error) => {
                json!({"protocol_version":PROTOCOL,"request_id":request_id,"error":error})
            }
        };
        let mut encoded = serde_json::to_vec(&response).map_err(|e| e.to_string())?;
        if encoded.len() >= MAX_FRAME_BYTES {
            encoded = serde_json::to_vec(&json!({"protocol_version":PROTOCOL,"request_id":request_id,"error":"RESPONSE_CAPACITY_EXCEEDED"})).map_err(|e|e.to_string())?;
        }
        encoded.push(b'\n');
        // Commit is already durable; send failure is not grounds for replay.
        let _ = stream.write_all(&encoded);
    }
    Ok(())
}
