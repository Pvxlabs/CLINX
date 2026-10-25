//! Centre-local IPC. Linux peer credentials never stand in for the Mac mTLS hop.
use crate::{
    node_relay::{MAX_REQUEST, MAX_RESPONSE},
    node_tls::Result,
};
use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    os::unix::{
        fs::{MetadataExt, PermissionsExt},
        net::{UnixListener, UnixStream},
    },
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::Duration,
};
pub struct Gateway {
    path: PathBuf,
    dev: u64,
    ino: u64,
    stopped: Arc<AtomicBool>,
}
impl Drop for Gateway {
    fn drop(&mut self) {
        self.stopped.store(true, Ordering::Relaxed);
        if self
            .path
            .symlink_metadata()
            .is_ok_and(|m| m.dev() == self.dev && m.ino() == self.ino)
        {
            let _ = fs::remove_file(&self.path);
        }
    }
}
#[cfg(target_os = "linux")]
fn uid(stream: &UnixStream) -> Result<u32> {
    rustix::net::sockopt::socket_peercred(stream)
        .map(|v| v.uid.as_raw())
        .map_err(|_| "PROVIDER_GATEWAY_UNTRUSTED".into())
}
#[cfg(not(target_os = "linux"))]
fn uid(_: &UnixStream) -> Result<u32> {
    Err("CENTRE_LOCAL_LINUX_IPC_REQUIRED".into())
}
pub fn listen(
    a: &Value,
    route: impl Fn(Value) -> Result<Value> + Send + Sync + 'static,
) -> Result<Gateway> {
    #[cfg(not(target_os = "linux"))]
    return Err("CENTRE_LOCAL_LINUX_IPC_REQUIRED".into());
    let path = PathBuf::from(
        a["endpoint"]
            .as_str()
            .ok_or("PROVIDER_GATEWAY_PATH_INVALID")?,
    );
    let parent = path
        .parent()
        .and_then(|p| p.symlink_metadata().ok())
        .ok_or("PROVIDER_GATEWAY_PATH_INVALID")?;
    if !path.is_absolute()
        || path.symlink_metadata().is_ok()
        || !parent.is_dir()
        || parent.uid() != rustix::process::getuid().as_raw()
        || parent.mode() & 0o022 != 0
    {
        return Err("PROVIDER_GATEWAY_PATH_INVALID".into());
    }
    let transport = a["transport_uid"]
        .as_u64()
        .and_then(|n| u32::try_from(n).ok())
        .ok_or("PROVIDER_TRANSPORT_UID_REQUIRED")?;
    let observer = a["observer_uid"].as_u64();
    let listener = UnixListener::bind(&path).map_err(|_| "PROVIDER_GATEWAY_PATH_INVALID")?;
    let meta = path
        .symlink_metadata()
        .map_err(|_| "PROVIDER_GATEWAY_PATH_INVALID")?;
    let gateway = Gateway {
        path: path.clone(),
        dev: meta.dev(),
        ino: meta.ino(),
        stopped: Arc::new(AtomicBool::new(false)),
    };
    if let Some(group) = a["socket_gid"].as_u64() {
        let group = u32::try_from(group).map_err(|_| "PROVIDER_GATEWAY_GROUP_UNAVAILABLE")?;
        // chown can only select an existing group allowed by the OS; no account
        // or group memberships are changed by this listener.
        rustix::fs::chown(&path, None, Some(rustix::fs::Gid::from_raw(group)))
            .map_err(|_| "PROVIDER_GATEWAY_GROUP_UNAVAILABLE")?;
    }
    fs::set_permissions(&path, fs::Permissions::from_mode(0o660))
        .map_err(|_| "PROVIDER_GATEWAY_PATH_INVALID")?;
    listener
        .set_nonblocking(true)
        .map_err(|_| "PROVIDER_GATEWAY_UNAVAILABLE")?;
    let stopped = gateway.stopped.clone();
    let route = Arc::new(route);
    thread::spawn(move || {
        while !stopped.load(Ordering::Relaxed) {
            match listener.accept() {
                Ok((mut stream, _)) => {
                    let route = route.clone();
                    thread::spawn(move || {
                        let Ok(uid) = uid(&stream) else {
                            return;
                        };
                        if uid != transport && Some(uid as u64) != observer {
                            return;
                        }
                        if stream
                            .set_read_timeout(Some(Duration::from_secs(40)))
                            .and_then(|_| stream.set_write_timeout(Some(Duration::from_secs(40))))
                            .is_err()
                        {
                            return;
                        }
                        let result = (|| -> Result<Value> {
                            let mut bytes = Vec::new();
                            BufReader::new(&mut stream)
                                .take((MAX_REQUEST + 1) as u64)
                                .read_until(b'\n', &mut bytes)
                                .map_err(|_| "PROVIDER_DELIVERY_UNKNOWN")?;
                            if bytes.len() > MAX_REQUEST || bytes.last() != Some(&b'\n') {
                                return Err("PROVIDER_REQUEST_TOO_LARGE".into());
                            }
                            let request: Value = serde_json::from_slice(&bytes)
                                .map_err(|_| "PROVIDER_DELIVERY_UNKNOWN")?;
                            if uid != transport && request["method"] != "workspace/inspect" {
                                return Err("PROVIDER_TRANSPORT_UID_REQUIRED".into());
                            }
                            route(request)
                        })()
                        .unwrap_or_else(|e| json!({"error":e}));
                        if let Ok(mut bytes) = serde_json::to_vec(&result) {
                            bytes.push(b'\n');
                            if bytes.len() <= MAX_RESPONSE {
                                let _ = stream.write_all(&bytes);
                            }
                        }
                    });
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(10))
                }
                Err(_) => break,
            }
        }
    });
    Ok(gateway)
}
pub fn request(a: &Value) -> Result<Value> {
    use crate::provider::Provider;
    let uid = a["expected_uid"]
        .as_u64()
        .and_then(|n| u32::try_from(n).ok())
        .ok_or("PROVIDER_GATEWAY_UNTRUSTED")?;
    let mut relay = crate::remote_provider::Relay::new(
        PathBuf::from(a["endpoint"].as_str().ok_or("PROVIDER_GATEWAY_UNTRUSTED")?),
        uid,
        a["context"].clone(),
    );
    relay.request(
        a["method"].as_str().ok_or("PROVIDER_METHOD_DENIED")?,
        a["params"].clone(),
    )
}
