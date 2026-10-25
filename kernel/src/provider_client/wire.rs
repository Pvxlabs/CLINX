//! Process transport and WebSocket/JSONL framing for legacy public client APIs.
//! Normal child processes live in Rust. Explicitly injected test streams use
//! the same parser through a byte callback, never a second protocol engine.
use super::{Fault, duration};
use base64::{Engine, engine::general_purpose::STANDARD};
use serde_json::{Value, json};
use sha1::{Digest, Sha1};
use std::{
    collections::BTreeMap,
    fs::File,
    io::{Read, Write},
    process::{Child, Command, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
type Result<T> = std::result::Result<T, Fault>;
fn protocol(s: impl Into<String>) -> Fault {
    Fault::new("protocol", s)
}
fn random<const N: usize>() -> Result<[u8; N]> {
    let mut b = [0; N];
    File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut b))
        .map_err(|e| Fault::transport(e.to_string()))?;
    Ok(b)
}
fn hex(b: &[u8]) -> String {
    b.iter().map(|b| format!("{b:02x}")).collect()
}
fn bytes(v: &Value) -> Result<Vec<u8>> {
    let s = v
        .as_str()
        .ok_or_else(|| protocol("PROVIDER_BYTES_INVALID"))?;
    if !s.len().is_multiple_of(2) || !s.is_ascii() {
        return Err(protocol("PROVIDER_BYTES_INVALID"));
    }
    (0..s.len())
        .step_by(2)
        .map(|n| {
            u8::from_str_radix(&s[n..n + 2], 16).map_err(|_| protocol("PROVIDER_BYTES_INVALID"))
        })
        .collect()
}
fn timeout(v: &Value) -> Result<Duration> {
    duration(v, true).map_err(Fault::transport)
}
fn remaining(deadline: Instant, message: &str) -> Result<Duration> {
    deadline
        .checked_duration_since(Instant::now())
        .filter(|v| !v.is_zero())
        .ok_or_else(|| Fault::transport(message))
}
trait Bytes {
    fn send(&mut self, b: &[u8]) -> Result<()>;
    fn read(&mut self, n: usize, t: Duration) -> Result<Vec<u8>>;
}
struct Process {
    child: Child,
}
impl Drop for Process {
    fn drop(&mut self) {
        self.child.stdin.take();
        let pgid = rustix::process::Pid::from_raw(self.child.id() as i32);
        if let Some(id) = pgid {
            let _ = rustix::process::kill_process_group(id, rustix::process::Signal::TERM);
        }
        let end = Instant::now() + Duration::from_secs(2);
        while Instant::now() < end {
            if self.child.try_wait().ok().flatten().is_some() {
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        if let Some(id) = pgid {
            let _ = rustix::process::kill_process_group(id, rustix::process::Signal::KILL);
        }
        let _ = self.child.wait();
    }
}
impl Bytes for Process {
    fn send(&mut self, b: &[u8]) -> Result<()> {
        self.child
            .stdin
            .as_mut()
            .ok_or_else(|| Fault::transport("app-server transport is not connected"))?
            .write_all(b)
            .map_err(|e| Fault::transport(format!("failed to write app-server bytes: {e}")))
    }
    fn read(&mut self, n: usize, t: Duration) -> Result<Vec<u8>> {
        let end = Instant::now() + t;
        let mut out = vec![0; n];
        let mut got = 0;
        while got < n {
            let rest = remaining(end, &format!("timed out waiting for {n} app-server bytes"))?;
            let stdout = self
                .child
                .stdout
                .as_mut()
                .ok_or_else(|| Fault::transport("app-server transport is not connected"))?;
            let ts = rustix::event::Timespec {
                tv_sec: rest.as_secs() as i64,
                tv_nsec: rest.subsec_nanos() as i64,
            };
            let ready = rustix::event::poll(
                &mut [rustix::event::PollFd::new(
                    &*stdout,
                    rustix::event::PollFlags::IN,
                )],
                Some(&ts),
            )
            .map_err(|e| Fault::transport(e.to_string()))?;
            if ready == 0 {
                return Err(Fault::transport(format!(
                    "timed out waiting for {n} app-server bytes"
                )));
            }
            let count = stdout
                .read(&mut out[got..])
                .map_err(|e| Fault::transport(e.to_string()))?;
            if count == 0 {
                let code = self
                    .child
                    .try_wait()
                    .ok()
                    .flatten()
                    .and_then(|s| s.code())
                    .map(|s| format!(" (exit {s})"))
                    .unwrap_or_default();
                return Err(Fault::transport(format!(
                    "app-server byte transport closed{code}"
                )));
            }
            got += count;
        }
        Ok(out)
    }
}
struct Callback<'a, F> {
    f: &'a mut F,
    token: &'a Value,
}
impl<F: FnMut(&str, Value) -> super::Result<Value>> Callback<'_, F> {
    fn call(&mut self, operation: &str, arguments: Value) -> Result<Value> {
        let r = (self.f)(
            "provider.client.io",
            json!({"token":self.token,"operation":operation,"arguments":arguments}),
        )
        .map_err(Fault::transport)?;
        if let Some(e) = r.get("error") {
            return Err(Fault::new(
                if e["kind"] == "protocol" {
                    "protocol"
                } else {
                    "transport"
                },
                e["message"].as_str().unwrap_or("PROVIDER_DELIVERY_UNKNOWN"),
            ));
        }
        r.get("result")
            .cloned()
            .ok_or_else(|| Fault::transport("PROVIDER_DELIVERY_UNKNOWN"))
    }
}
impl<F: FnMut(&str, Value) -> super::Result<Value>> Bytes for Callback<'_, F> {
    fn send(&mut self, b: &[u8]) -> Result<()> {
        self.call("send_bytes", json!({"payload":hex(b)}))
            .map(|_| ())
    }
    fn read(&mut self, n: usize, t: Duration) -> Result<Vec<u8>> {
        let r = self.call(
            "receive_bytes",
            json!({"length":n,"timeout":t.as_secs_f64()}),
        )?;
        let b = bytes(&r)?;
        if b.len() != n {
            return Err(protocol("PROVIDER_SHORT_BYTE_READ"));
        }
        Ok(b)
    }
}
fn header(io: &mut dyn Bytes, t: Duration) -> Result<Vec<u8>> {
    let end = Instant::now() + t;
    let mut b = Vec::new();
    while !b.ends_with(b"\r\n\r\n") {
        b.extend(io.read(
            1,
            remaining(end, "timed out during app-server websocket handshake")?,
        )?);
        if b.len() > 64 * 1024 {
            return Err(protocol("app-server websocket handshake is too large"));
        }
    }
    b.truncate(b.len() - 4);
    Ok(b)
}
fn handshake(io: &mut dyn Bytes, path: &str, t: Duration) -> Result<()> {
    if !path.starts_with('/') {
        return Err(Fault::transport("websocket path must start with '/'"));
    }
    let key = STANDARD.encode(random::<16>()?);
    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    );
    if !request.is_ascii() {
        return Err(protocol("websocket path must be ASCII"));
    }
    io.send(request.as_bytes())?;
    let raw = header(io, t)?;
    let response = raw.iter().map(|b| *b as char).collect::<String>();
    let mut lines = response.split("\r\n");
    let status = lines.next().unwrap_or("empty response");
    if !status.starts_with("HTTP/1.1 101") {
        return Err(protocol(format!(
            "app-server websocket handshake failed: {status}"
        )));
    }
    let mut headers = BTreeMap::new();
    for line in lines {
        if let Some((k, v)) = line.split_once(':') {
            headers.insert(k.to_lowercase(), v.trim().to_owned());
        }
    }
    let accept = STANDARD.encode(Sha1::digest(
        format!("{key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11").as_bytes(),
    ));
    if headers.get("sec-websocket-accept") != Some(&accept) {
        return Err(protocol("app-server websocket accept mismatch"));
    }
    Ok(())
}
fn send_frame(io: &mut dyn Bytes, payload: &[u8], opcode: u8) -> Result<()> {
    let mask = random::<4>()?;
    let mut wire = vec![0x80 | opcode];
    match payload.len() {
        n if n < 126 => wire.push(0x80 | n as u8),
        n if n <= 65535 => {
            wire.push(0x80 | 126);
            wire.extend((n as u16).to_be_bytes());
        }
        n => {
            wire.push(0x80 | 127);
            wire.extend((n as u64).to_be_bytes());
        }
    }
    wire.extend(mask);
    wire.extend(payload.iter().enumerate().map(|(i, b)| *b ^ mask[i % 4]));
    io.send(&wire)
}
fn frame(io: &mut dyn Bytes, t: Duration) -> Result<(u8, Vec<u8>, bool)> {
    let h = io.read(2, t)?;
    let mut len = (h[1] & 127) as u64;
    if len == 126 {
        len = u16::from_be_bytes(io.read(2, t)?.try_into().unwrap()) as u64;
    } else if len == 127 {
        len = u64::from_be_bytes(io.read(8, t)?.try_into().unwrap());
    }
    if len > 16 * 1024 * 1024 {
        return Err(protocol("app-server websocket frame is too large"));
    }
    let mask = if h[1] & 128 != 0 {
        io.read(4, t)?
    } else {
        Vec::new()
    };
    let mut payload = io.read(len as usize, t)?;
    if !mask.is_empty() {
        for (i, b) in payload.iter_mut().enumerate() {
            *b ^= mask[i % 4];
        }
    }
    Ok((h[0] & 15, payload, h[0] & 128 != 0))
}
fn message(io: &mut dyn Bytes, t: Duration) -> Result<Value> {
    let end = Instant::now() + t;
    let mut fragments = Vec::new();
    let mut kind = None;
    loop {
        let (op, payload, fin) = frame(
            io,
            remaining(end, "timed out waiting for app-server websocket message")?,
        )?;
        match op {
            9 => {
                send_frame(io, &payload, 10)?;
                continue;
            }
            10 => continue,
            8 => return Err(Fault::transport("app-server websocket closed")),
            1 | 2 => {
                if kind.is_some() {
                    return Err(protocol("nested app-server websocket fragment"));
                }
                kind = Some(op);
                fragments.extend(payload);
            }
            0 => {
                if kind.is_none() {
                    return Err(protocol("unexpected app-server websocket continuation"));
                }
                fragments.extend(payload);
            }
            _ => {
                return Err(protocol(format!(
                    "unsupported app-server websocket opcode: {op}"
                )));
            }
        }
        if !fin {
            continue;
        }
        if kind != Some(1) {
            return Err(protocol("app-server websocket message is not text"));
        }
        let r: Value = serde_json::from_slice(&fragments)
            .map_err(|_| protocol("app-server returned malformed websocket JSON"))?;
        if !r.is_object() {
            return Err(protocol("app-server message must be a JSON object"));
        }
        return Ok(r);
    }
}
fn operate(op: &str, a: &Value, io: &mut dyn Bytes) -> Result<Value> {
    match op {
        "handshake" => {
            handshake(
                io,
                a["path"].as_str().unwrap_or(""),
                timeout(&a["timeout"])?,
            )?;
            Ok(Value::Null)
        }
        "header" => Ok(json!(hex(&header(io, timeout(&a["timeout"])?)?))),
        "send" => {
            send_frame(
                io,
                &serde_json::to_vec(&a["message"]).map_err(|e| protocol(e.to_string()))?,
                1,
            )?;
            Ok(Value::Null)
        }
        "send_frame" => {
            send_frame(
                io,
                &bytes(&a["payload"])?,
                a["opcode"]
                    .as_u64()
                    .ok_or_else(|| protocol("PROVIDER_OPCODE_INVALID"))? as u8,
            )?;
            Ok(Value::Null)
        }
        "frame" => {
            let (op, b, fin) = frame(io, timeout(&a["timeout"])?)?;
            Ok(json!([op, hex(&b), fin]))
        }
        "receive" => message(io, timeout(&a["timeout"])?),
        "send_bytes" => {
            io.send(&bytes(&a["payload"])?)?;
            Ok(Value::Null)
        }
        "receive_bytes" => {
            let n = a["length"]
                .as_u64()
                .ok_or_else(|| Fault::transport("byte read length must not be negative"))?;
            if n > 64 * 1024 * 1024 {
                return Err(protocol("PROVIDER_BYTE_READ_TOO_LARGE"));
            }
            Ok(json!(hex(&io.read(n as usize, timeout(&a["timeout"])?)?)))
        }
        "send_line" => {
            let mut b = serde_json::to_vec(&a["message"]).map_err(|e| protocol(e.to_string()))?;
            b.push(b'\n');
            io.send(&b)?;
            Ok(Value::Null)
        }
        "receive_line" => {
            let end = Instant::now() + timeout(&a["timeout"])?;
            let mut b = Vec::new();
            loop {
                let part = io.read(
                    1,
                    remaining(end, "timed out waiting for app-server response")?,
                )?;
                if part == b"\n" {
                    break;
                }
                b.extend(part);
                if b.len() > 16 * 1024 * 1024 {
                    return Err(protocol("app-server message is too large"));
                }
            }
            let v: Value = serde_json::from_slice(&b)
                .map_err(|_| protocol("app-server returned malformed JSON"))?;
            if !v.is_object() {
                return Err(protocol("app-server message must be a JSON object"));
            }
            Ok(v)
        }
        _ => Err(protocol("PROVIDER_WIRE_OPERATION_UNKNOWN")),
    }
}
#[derive(Default)]
pub(super) struct Runtime {
    next: AtomicU64,
    processes: Mutex<BTreeMap<u64, Arc<Mutex<Process>>>>,
}
impl Runtime {
    fn invoke_inner<F: FnMut(&str, Value) -> super::Result<Value>>(
        &self,
        op: &str,
        a: &Value,
        mut callback: F,
    ) -> Result<Value> {
        if op == "process_connect" {
            let cmd = a["command"]
                .as_array()
                .filter(|v| !v.is_empty())
                .ok_or_else(|| Fault::transport("app-server command is required"))?;
            let cmd = cmd
                .iter()
                .map(|v| {
                    v.as_str()
                        .ok_or_else(|| Fault::transport("app-server command must contain strings"))
                })
                .collect::<Result<Vec<_>>>()?;
            let executable =
                std::env::current_exe().map_err(|e| Fault::transport(e.to_string()))?;
            let mut command = Command::new(executable);
            command
                .arg("--provider-process-child")
                .args(&cmd)
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());
            let mut child = command.spawn().map_err(|e| {
                Fault::transport(format!("failed to start app-server transport: {e}"))
            })?;
            // The helper duplicates this private stderr pipe with CLOEXEC.
            // EOF therefore proves exec succeeded; errors are reported before
            // exit. The actual Provider still receives /dev/null as stderr.
            let mut report = child.stderr.take().expect("piped bootstrap stderr");
            let mut failure = Vec::new();
            report
                .read_to_end(&mut failure)
                .map_err(|e| Fault::transport(e.to_string()))?;
            if !failure.is_empty() {
                let _ = child.wait();
                return Err(Fault::transport(format!(
                    "failed to start app-server transport: {}",
                    String::from_utf8_lossy(&failure)
                )));
            }
            let id = self.next.fetch_add(1, Ordering::Relaxed) + 1;
            let process = Process { child };
            let mut processes = self
                .processes
                .lock()
                .map_err(|_| Fault::transport("PROVIDER_PROCESS_UNAVAILABLE"))?;
            if processes.len() >= 128 {
                return Err(Fault::transport("PROVIDER_PROCESS_CAPACITY_EXCEEDED"));
            }
            processes.insert(id, Arc::new(Mutex::new(process)));
            return Ok(json!(id));
        }
        let session = a["process_session"].as_u64();
        if op == "process_close" {
            let process = self
                .processes
                .lock()
                .map_err(|_| Fault::transport("PROVIDER_PROCESS_UNAVAILABLE"))?
                .remove(&session.ok_or_else(|| Fault::transport("PROVIDER_PROCESS_INVALID"))?);
            drop(process);
            return Ok(Value::Null);
        }
        if let Some(id) = session {
            let process = self
                .processes
                .lock()
                .map_err(|_| Fault::transport("PROVIDER_PROCESS_UNAVAILABLE"))?
                .get(&id)
                .cloned()
                .ok_or_else(|| Fault::transport("app-server transport is not connected"))?;
            let mut process = process
                .lock()
                .map_err(|_| Fault::transport("PROVIDER_PROCESS_UNAVAILABLE"))?;
            if op == "process_lifecycle" {
                return Ok(json!(match process.child.try_wait() {
                    Ok(None) => "available",
                    Ok(Some(_)) => "unavailable",
                    Err(_) => "unknown",
                }));
            }
            return operate(op, a, &mut *process);
        }
        operate(
            op,
            a,
            &mut Callback {
                f: &mut callback,
                token: &a["callback_token"],
            },
        )
    }
    pub(super) fn invoke<F: FnMut(&str, Value) -> super::Result<Value>>(
        &self,
        operation: &str,
        args: &Value,
        callback: F,
    ) -> super::Result<Value> {
        Ok(
            match self.invoke_inner(
                operation.trim_start_matches("provider.client.wire."),
                args,
                callback,
            ) {
                Ok(v) => json!({"value":v}),
                Err(e) => json!({"error":e.wire()}),
            },
        )
    }
}
