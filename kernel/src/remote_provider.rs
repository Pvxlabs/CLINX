//! Provider RPC through the Centre's existing paired channel. The Unix peer
//! authenticates the Centre locally; no Linux credential crosses to macOS.
use crate::{
    MAX_FRAME_BYTES,
    native_transport::{Providers, UnixProviders},
    provider::{MAX_MESSAGE, Provider, Result},
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::{BufRead, BufReader, Read, Write},
    os::unix::{
        fs::{FileTypeExt, MetadataExt},
        net::UnixStream,
    },
    path::PathBuf,
    time::Duration,
};
pub struct RoutedProviders {
    pub local: UnixProviders,
    pub gateway: Option<PathBuf>,
    pub gateway_uid: u32,
    pub remote_hosts: BTreeSet<String>,
    pub routes: BTreeMap<String, PathBuf>,
}
pub enum Connection {
    Local(crate::provider::Client),
    Remote(Relay),
}
impl Provider for Connection {
    fn request(&mut self, method: &str, params: Value) -> Result<Value> {
        match self {
            Self::Local(c) => c.request(method, params),
            Self::Remote(c) => c.request(method, params),
        }
    }
    fn error_details(&self, code: &str, stage: &str) -> Value {
        match self {
            Self::Local(c) => c.error_details(code, stage),
            Self::Remote(c) => c.error_details(code, stage),
        }
    }
}
impl Providers for RoutedProviders {
    type Connection = Connection;
    fn connect(&mut self, endpoint: &str) -> Result<Connection> {
        Ok(Connection::Local(self.local.connect(endpoint)?))
    }
    fn connect_scoped(&mut self, endpoint: &str, context: Value) -> Result<Connection> {
        let host = context["host"].as_str().unwrap_or("");
        if self.remote_hosts.contains(host) || self.routes.contains_key(host) {
            let gateway = self
                .routes
                .get(host)
                .cloned()
                .or_else(|| self.gateway.clone())
                .ok_or("REMOTE_PROVIDER_GATEWAY_REQUIRED")?;
            if gateway.to_str() != Some(endpoint) {
                return Err("REMOTE_PROVIDER_ENDPOINT_CONFLICT".into());
            }
            Ok(Connection::Remote(Relay {
                gateway,
                uid: self.gateway_uid,
                context,
                last_error: None,
            }))
        } else {
            self.connect(endpoint)
        }
    }
}
pub struct Relay {
    gateway: PathBuf,
    uid: u32,
    context: Value,
    last_error: Option<String>,
}
impl Relay {
    pub fn new(gateway: PathBuf, uid: u32, context: Value) -> Self {
        Self {
            gateway,
            uid,
            context,
            last_error: None,
        }
    }
}
impl Provider for Relay {
    fn error_details(&self, code: &str, stage: &str) -> Value {
        crate::provider::error_diagnostic(
            self.last_error.as_deref().unwrap_or(code),
            stage,
            &Value::Null,
        )
    }
    fn request(&mut self, method: &str, params: Value) -> Result<Value> {
        self.last_error = None;
        let info = self
            .gateway
            .symlink_metadata()
            .map_err(|_| "PROVIDER_GATEWAY_UNAVAILABLE")?;
        let parent = self
            .gateway
            .parent()
            .ok_or("PROVIDER_GATEWAY_UNTRUSTED")?
            .symlink_metadata()
            .map_err(|_| "PROVIDER_GATEWAY_UNAVAILABLE")?;
        if !self.gateway.is_absolute()
            || !info.file_type().is_socket()
            || info.uid() != self.uid
            || info.mode() & 7 != 0
            || !parent.is_dir()
            || parent.uid() != self.uid
            || parent.mode() & 0o022 != 0
        {
            return Err("PROVIDER_GATEWAY_UNTRUSTED".into());
        }
        let mut stream =
            UnixStream::connect(&self.gateway).map_err(|_| "PROVIDER_GATEWAY_UNAVAILABLE")?;
        #[cfg(target_os = "linux")]
        if rustix::net::sockopt::socket_peercred(&stream)
            .map_err(|_| "PROVIDER_GATEWAY_UNTRUSTED")?
            .uid
            .as_raw()
            != self.uid
        {
            return Err("PROVIDER_GATEWAY_UNTRUSTED".into());
        }
        #[cfg(not(target_os = "linux"))]
        return Err("CENTRE_LOCAL_LINUX_IPC_REQUIRED".into());
        stream
            .set_read_timeout(Some(Duration::from_secs(40)))
            .and_then(|_| stream.set_write_timeout(Some(Duration::from_secs(40))))
            .map_err(|_| "PROVIDER_GATEWAY_UNAVAILABLE")?;
        let mut wire =
            serde_json::to_vec(&json!({"context":self.context,"method":method,"params":params}))
                .map_err(|_| "PROVIDER_REQUEST_INVALID")?;
        wire.push(b'\n');
        if wire.len() > MAX_FRAME_BYTES {
            return Err("PROVIDER_REQUEST_TOO_LARGE".into());
        }
        stream
            .write_all(&wire)
            .map_err(|_| "PROVIDER_DELIVERY_UNKNOWN")?;
        let mut frame = Vec::new();
        BufReader::new(stream)
            .take((MAX_MESSAGE + 1) as u64)
            .read_until(b'\n', &mut frame)
            .map_err(|_| "PROVIDER_DELIVERY_UNKNOWN")?;
        if frame.len() > MAX_MESSAGE || frame.last() != Some(&b'\n') {
            return Err("PROVIDER_DELIVERY_UNKNOWN".into());
        }
        let reply: Value =
            serde_json::from_slice(&frame).map_err(|_| "PROVIDER_DELIVERY_UNKNOWN")?;
        if reply.get("error").is_some() == reply.get("result").is_some() {
            return Err("PROVIDER_DELIVERY_UNKNOWN".into());
        }
        if let Some(error) = reply["error"].as_str() {
            self.last_error = Some(error.to_owned());
            return Err(error.to_owned());
        }
        let result = reply["result"]
            .as_object()
            .ok_or("PROVIDER_DELIVERY_UNKNOWN")?;
        if matches!(method, "thread/read" | "thread/start" | "thread/resume") {
            let thread = &reply["result"]["thread"];
            let tid = thread["id"].as_str().ok_or("THREAD_IDENTITY_CONFLICT")?;
            if self.context["thread_id"]
                .as_str()
                .is_some_and(|expected| expected != tid)
            {
                return Err("THREAD_IDENTITY_CONFLICT".into());
            }
            self.context["thread_id"] = json!(tid);
            self.context["cwd"] = thread["cwd"].clone();
        }
        Ok(Value::Object(result.clone()))
    }
}
