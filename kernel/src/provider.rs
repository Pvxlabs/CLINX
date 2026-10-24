//! Codex JSON-RPC over an existing Unix-domain WebSocket. No daemon creation,
//! fallback endpoint, task state, input retry, or unsolicited tool responses.
use serde_json::{Value, json};
use sha1::{Digest, Sha1};
use std::{
    collections::BTreeSet,
    fs::File,
    io::{Read, Write},
    os::unix::net::UnixStream,
    path::Path,
    time::{Duration, Instant},
};

pub type Result<T> = std::result::Result<T, String>;
pub const MAX_MESSAGE: usize = 16 * 1024 * 1024;
const MAX_HEADER: usize = 65536;

fn random<const N: usize>() -> Result<[u8; N]> {
    let mut bytes = [0; N];
    File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .map_err(|_| "RANDOM_UNAVAILABLE")?;
    Ok(bytes)
}
fn base64(bytes: &[u8]) -> String {
    const TABLE: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for c in bytes.chunks(3) {
        let n = ((c[0] as u32) << 16)
            | ((c.get(1).copied().unwrap_or(0) as u32) << 8)
            | c.get(2).copied().unwrap_or(0) as u32;
        for (i, shift) in [18, 12, 6, 0].iter().enumerate() {
            out.push(if i > c.len() {
                '='
            } else {
                TABLE[((n >> shift) & 63) as usize] as char
            });
        }
    }
    out
}

pub trait Provider {
    fn request(&mut self, method: &str, params: Value) -> Result<Value>;
    fn thread(&mut self, tid: &str) -> Result<Value> {
        let thread =
            self.request("thread/read", json!({"threadId":tid,"includeTurns":false}))?["thread"]
                .clone();
        if thread["id"] != tid {
            return Err("THREAD_IDENTITY_CONFLICT".into());
        }
        Ok(thread)
    }
    fn exact_turn(&mut self, tid: &str, turn: &str) -> Result<Value> {
        let mut params =
            json!({"threadId":tid,"limit":20,"sortDirection":"desc","itemsView":"full"});
        let mut seen = BTreeSet::new();
        for _ in 0..5 {
            let page = self.request("thread/turns/list", params.clone())?;
            let data = page["data"].as_array().ok_or("NATIVE_HISTORY_INVALID")?;
            let matches: Vec<_> = data.iter().filter(|t| t["id"] == turn).collect();
            if matches.len() > 1 {
                return Err("NATIVE_TURN_IDENTITY_CONFLICT".into());
            }
            if let Some(turn) = matches.first() {
                return Ok((*turn).clone());
            }
            match page.get("nextCursor") {
                None | Some(Value::Null) => return Err("EXACT_NATIVE_TURN_UNAVAILABLE".into()),
                Some(Value::String(cursor))
                    if !cursor.is_empty() && seen.insert(cursor.clone()) =>
                {
                    params["cursor"] = json!(cursor)
                }
                _ => return Err("NATIVE_HISTORY_SCAN_INCOMPLETE".into()),
            }
        }
        Err("NATIVE_HISTORY_SCAN_INCOMPLETE".into())
    }
    fn turns(&mut self, tid: &str, limit: u64, view: &str) -> Result<Vec<Value>> {
        self.pages(
            "thread/turns/list",
            json!({"threadId":tid,"limit":limit,"sortDirection":"desc","itemsView":view}),
        )
    }
    fn pages(&mut self, method: &str, mut params: Value) -> Result<Vec<Value>> {
        let mut rows = Vec::new();
        let mut seen = BTreeSet::new();
        // A complete bounded scan, including its final cursor, is required.
        // Missing a requested turn or duplicate input cannot justify replay.
        let mut bytes = 0;
        for _ in 0..5 {
            let page = self.request(method, params.clone())?;
            bytes += serde_json::to_vec(&page)
                .map_err(|_| "PROVIDER_PROTOCOL_INVALID")?
                .len();
            if bytes > MAX_MESSAGE {
                return Err("NATIVE_HISTORY_CAPACITY_EXCEEDED".into());
            }
            rows.extend(
                page["data"]
                    .as_array()
                    .ok_or("NATIVE_HISTORY_INVALID")?
                    .iter()
                    .cloned(),
            );
            match page.get("nextCursor") {
                None | Some(Value::Null) => return Ok(rows),
                Some(Value::String(cursor))
                    if !cursor.is_empty() && seen.insert(cursor.clone()) =>
                {
                    params["cursor"] = json!(cursor)
                }
                _ => return Err("NATIVE_HISTORY_SCAN_INCOMPLETE".into()),
            }
        }
        Err("NATIVE_HISTORY_SCAN_INCOMPLETE".into())
    }
}

pub struct Client {
    stream: UnixStream,
    timeout: Duration,
    sequence: u64,
    pub last_error: Option<Value>,
}
impl Client {
    /// Host sessions own their exact dynamic-tool calls. Observer/native
    /// request() retains its rule of never answering unsolicited requests.
    pub fn send_message(&mut self, message: &Value) -> Result<()> {
        self.send(message, Instant::now() + self.timeout)
    }
    pub fn receive_message(&mut self, timeout: Duration) -> Result<Value> {
        self.receive(Instant::now() + timeout)
    }
    pub fn poll_message(&mut self, timeout: Duration) -> Result<Option<Value>> {
        let mut fds = [rustix::event::PollFd::new(
            &self.stream,
            rustix::event::PollFlags::IN,
        )];
        let at = rustix::event::Timespec {
            tv_sec: timeout.as_secs() as i64,
            tv_nsec: timeout.subsec_nanos() as i64,
        };
        if rustix::event::poll(&mut fds, Some(&at)).map_err(|_| "PROVIDER_DELIVERY_UNKNOWN")? == 0 {
            return Ok(None);
        }
        // Once any frame begins, a timeout is fatal. Never continue parsing a
        // partially consumed WebSocket frame as a new frame.
        self.receive_message(self.timeout).map(Some)
    }
    pub fn request_collect(
        &mut self,
        method: &str,
        params: Value,
        events: &mut Vec<Value>,
    ) -> Result<Value> {
        self.rpc(method, params, Some(events))
    }
    pub fn connect(endpoint: &Path, timeout: Duration) -> Result<Self> {
        if !endpoint.is_absolute() || timeout.is_zero() {
            return Err("PROVIDER_ENDPOINT_INVALID".into());
        }
        let stream = UnixStream::connect(endpoint).map_err(|_| "PROVIDER_UNAVAILABLE")?;
        let mut client = Self {
            stream,
            timeout,
            sequence: 0,
            last_error: None,
        };
        let key = base64(&random::<16>()?);
        let request = format!(
            "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        );
        let deadline = Instant::now() + timeout;
        client.write(request.as_bytes(), deadline)?;
        let mut header = Vec::new();
        while !header.ends_with(b"\r\n\r\n") {
            if header.len() >= MAX_HEADER {
                return Err("PROVIDER_HANDSHAKE_INVALID".into());
            }
            let mut byte = [0];
            client.read(&mut byte, deadline)?;
            header.push(byte[0]);
        }
        let header = std::str::from_utf8(&header).map_err(|_| "PROVIDER_HANDSHAKE_INVALID")?;
        let mut lines = header.split("\r\n");
        if !matches!(
            lines.next(),
            Some("HTTP/1.1 101 Switching Protocols" | "HTTP/1.1 101")
        ) {
            return Err("PROVIDER_HANDSHAKE_INVALID".into());
        }
        let expected = base64(&Sha1::digest(
            format!("{key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11").as_bytes(),
        ));
        let mut accept = None;
        let mut upgrade = false;
        let mut connection = false;
        for line in lines.filter(|l| !l.is_empty()) {
            let (name, value) = line.split_once(':').ok_or("PROVIDER_HANDSHAKE_INVALID")?;
            match name.to_ascii_lowercase().as_str() {
                "sec-websocket-accept" => {
                    if accept.replace(value.trim()).is_some() {
                        return Err("PROVIDER_HANDSHAKE_INVALID".into());
                    }
                }
                "upgrade" => upgrade = value.trim().eq_ignore_ascii_case("websocket"),
                "connection" => {
                    connection = value
                        .split(',')
                        .any(|v| v.trim().eq_ignore_ascii_case("upgrade"))
                }
                _ => (),
            }
        }
        if accept != Some(expected.as_str()) || !upgrade || !connection {
            return Err("PROVIDER_HANDSHAKE_INVALID".into());
        }
        client.request("initialize", json!({"clientInfo":{"name":"clinx-rust-transport","title":"CLINX Rust Transport","version":"1"},"capabilities":{"experimentalApi":true}}))?;
        client.send(&json!({"method":"initialized"}), Instant::now() + timeout)?;
        Ok(client)
    }
    fn remaining(deadline: Instant) -> Result<Duration> {
        deadline
            .checked_duration_since(Instant::now())
            .filter(|v| !v.is_zero())
            .ok_or_else(|| "PROVIDER_DELIVERY_UNKNOWN".into())
    }
    fn read(&mut self, bytes: &mut [u8], deadline: Instant) -> Result<()> {
        self.stream
            .set_read_timeout(Some(Self::remaining(deadline)?))
            .map_err(|_| "PROVIDER_UNAVAILABLE")?;
        self.stream
            .read_exact(bytes)
            .map_err(|_| "PROVIDER_DELIVERY_UNKNOWN".into())
    }
    fn write(&mut self, bytes: &[u8], deadline: Instant) -> Result<()> {
        self.stream
            .set_write_timeout(Some(Self::remaining(deadline)?))
            .map_err(|_| "PROVIDER_UNAVAILABLE")?;
        self.stream
            .write_all(bytes)
            .map_err(|_| "PROVIDER_DELIVERY_UNKNOWN".into())
    }
    fn frame(&mut self, opcode: u8, payload: &[u8], deadline: Instant) -> Result<()> {
        if payload.len() > MAX_MESSAGE {
            return Err("PROVIDER_MESSAGE_TOO_LARGE".into());
        }
        let mask = random::<4>()?;
        let mut frame = vec![0x80 | opcode];
        match payload.len() {
            n @ 0..126 => frame.push(0x80 | n as u8),
            n @ 126..=65535 => {
                frame.push(0x80 | 126);
                frame.extend((n as u16).to_be_bytes());
            }
            n => {
                frame.push(0x80 | 127);
                frame.extend((n as u64).to_be_bytes());
            }
        }
        frame.extend(mask);
        frame.extend(payload.iter().enumerate().map(|(i, b)| b ^ mask[i % 4]));
        self.write(&frame, deadline)
    }
    fn send(&mut self, value: &Value, deadline: Instant) -> Result<()> {
        self.frame(
            1,
            &serde_json::to_vec(value).map_err(|_| "PROVIDER_PROTOCOL_INVALID")?,
            deadline,
        )
    }
    fn receive(&mut self, deadline: Instant) -> Result<Value> {
        let mut fragments = Vec::new();
        let mut started = false;
        loop {
            let mut header = [0; 2];
            self.read(&mut header, deadline)?;
            let fin = header[0] & 0x80 != 0;
            let opcode = header[0] & 15;
            if header[0] & 0x70 != 0 || header[1] & 0x80 != 0 {
                return Err("PROVIDER_FRAME_INVALID".into());
            }
            let size = match header[1] & 127 {
                126 => {
                    let mut n = [0; 2];
                    self.read(&mut n, deadline)?;
                    let n = u16::from_be_bytes(n) as u64;
                    if n < 126 {
                        return Err("PROVIDER_FRAME_INVALID".into());
                    }
                    n
                }
                127 => {
                    let mut n = [0; 8];
                    self.read(&mut n, deadline)?;
                    let n = u64::from_be_bytes(n);
                    if n < 65536 {
                        return Err("PROVIDER_FRAME_INVALID".into());
                    }
                    n
                }
                n => n as u64,
            };
            if size > MAX_MESSAGE as u64
                || (opcode < 8 && fragments.len() + size as usize > MAX_MESSAGE)
            {
                return Err("PROVIDER_MESSAGE_TOO_LARGE".into());
            }
            if opcode >= 8 && (!fin || size > 125) {
                return Err("PROVIDER_FRAME_INVALID".into());
            }
            let mut payload = vec![0; size as usize];
            self.read(&mut payload, deadline)?;
            match opcode {
                9 => {
                    self.frame(10, &payload, deadline)?;
                    continue;
                }
                10 => continue,
                8 => return Err("PROVIDER_DELIVERY_UNKNOWN".into()),
                1 if !started => started = true,
                0 if started => (),
                _ => return Err("PROVIDER_FRAME_INVALID".into()),
            }
            fragments.extend(payload);
            if fin {
                let value: Value =
                    serde_json::from_slice(&fragments).map_err(|_| "PROVIDER_PROTOCOL_INVALID")?;
                if !value.is_object() {
                    return Err("PROVIDER_PROTOCOL_INVALID".into());
                }
                return Ok(value);
            }
        }
    }
}
impl Provider for Client {
    fn request(&mut self, method: &str, params: Value) -> Result<Value> {
        self.rpc(method, params, None)
    }
}
impl Client {
    fn rpc(
        &mut self,
        method: &str,
        params: Value,
        mut events: Option<&mut Vec<Value>>,
    ) -> Result<Value> {
        self.last_error = None;
        self.sequence += 1;
        let id = json!(format!("clinx-rust-{}", self.sequence));
        let deadline = Instant::now() + self.timeout;
        self.send(&json!({"id":id,"method":method,"params":params}), deadline)?;
        // Unsolicited native approvals/tool requests belong to Desktop. Never
        // answer them, execute a command, or infer that they are our own turn.
        for _ in 0..4096 {
            let message = self.receive(deadline)?;
            if message.get("method").is_some() {
                if let Some(events) = events.as_deref_mut() {
                    if events.len() >= 256 {
                        return Err("PROVIDER_NOTIFICATION_CAPACITY_EXCEEDED".into());
                    }
                    events.push(message);
                }
                continue;
            }
            if message["id"] != id {
                return Err("PROVIDER_RESPONSE_IDENTITY_CONFLICT".into());
            }
            if let Some(error) = message.get("error") {
                self.last_error = Some(error.clone());
                if method == "thread/read"
                    && error["code"] == -32600
                    && params["threadId"]
                        .as_str()
                        .is_some_and(|tid| error["message"] == format!("thread not loaded: {tid}"))
                {
                    return Err("NATIVE_THREAD_NOT_LOADED".into());
                }
                return Err("PROVIDER_REMOTE_ERROR".into());
            }
            return message
                .get("result")
                .filter(|v| v.is_object())
                .cloned()
                .ok_or_else(|| "PROVIDER_PROTOCOL_INVALID".into());
        }
        Err("PROVIDER_NOTIFICATION_CAPACITY_EXCEEDED".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::net::UnixListener;
    fn receive(stream: &mut UnixStream) -> Value {
        let mut h = [0; 2];
        stream.read_exact(&mut h).unwrap();
        assert_eq!(h[0], 0x81);
        assert_ne!(h[1] & 128, 0);
        let len = match h[1] & 127 {
            126 => {
                let mut n = [0; 2];
                stream.read_exact(&mut n).unwrap();
                u16::from_be_bytes(n) as usize
            }
            127 => {
                let mut n = [0; 8];
                stream.read_exact(&mut n).unwrap();
                u64::from_be_bytes(n) as usize
            }
            n => n as usize,
        };
        let mut mask = [0; 4];
        stream.read_exact(&mut mask).unwrap();
        let mut data = vec![0; len];
        stream.read_exact(&mut data).unwrap();
        for (i, b) in data.iter_mut().enumerate() {
            *b ^= mask[i % 4];
        }
        serde_json::from_slice(&data).unwrap()
    }
    fn send(stream: &mut UnixStream, opcode: u8, fin: bool, bytes: &[u8]) {
        assert!(bytes.len() < 126);
        let mut frame = vec![(if fin { 128 } else { 0 }) | opcode, bytes.len() as u8];
        frame.extend(bytes);
        stream.write_all(&frame).unwrap();
    }
    fn handshake(stream: &mut UnixStream) {
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        let mut bytes = Vec::new();
        while !bytes.ends_with(b"\r\n\r\n") {
            let mut b = [0];
            stream.read_exact(&mut b).unwrap();
            bytes.push(b[0]);
        }
        let h = String::from_utf8(bytes).unwrap();
        let key = h
            .lines()
            .find_map(|l| l.strip_prefix("Sec-WebSocket-Key: "))
            .unwrap();
        let accept = base64(&Sha1::digest(
            format!("{key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11").as_bytes(),
        ));
        stream.write_all(format!("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: keep-alive, Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").as_bytes()).unwrap();
        let init = receive(stream);
        send(
            stream,
            1,
            true,
            serde_json::to_string(&json!({"id":init["id"],"result":{}}))
                .unwrap()
                .as_bytes(),
        );
        assert_eq!(receive(stream)["method"], "initialized");
    }
    #[test]
    fn websocket_fragment_ping_identity_and_unsolicited_requests() {
        let path = std::env::temp_dir().join(format!("clinx-ws-{}.sock", std::process::id()));
        let listener = UnixListener::bind(&path).unwrap();
        let handle = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            handshake(&mut stream);
            let req = receive(&mut stream);
            assert_eq!(req["method"], "thread/read");
            send(
                &mut stream,
                1,
                true,
                b"{\"id\":\"desktop-tool\",\"method\":\"item/tool/call\",\"params\":{}}",
            );
            send(&mut stream, 9, true, b"ping");
            let reply =
                serde_json::to_vec(&json!({"id":req["id"],"result":{"thread":{"id":"exact"}}}))
                    .unwrap();
            let split = reply.len() / 2;
            send(&mut stream, 1, false, &reply[..split]);
            send(&mut stream, 0, true, &reply[split..]);
            // The only client response to unsolicited data is WebSocket pong.
            let mut h = [0; 2];
            stream.read_exact(&mut h).unwrap();
            assert_eq!(h, [0x8a, 0x84]);
            let mut mask = [0; 4];
            stream.read_exact(&mut mask).unwrap();
            let mut data = [0; 4];
            stream.read_exact(&mut data).unwrap();
            for (i, b) in data.iter_mut().enumerate() {
                *b ^= mask[i % 4];
            }
            assert_eq!(&data, b"ping");
            let _req = receive(&mut stream);
            send(&mut stream, 1, true, b"{\"id\":\"wrong\",\"result\":{}}");
        });
        let mut client = Client::connect(&path, Duration::from_secs(2)).unwrap();
        assert_eq!(client.thread("exact").unwrap()["id"], "exact");
        assert_eq!(
            client.request("thread/read", json!({})).unwrap_err(),
            "PROVIDER_RESPONSE_IDENTITY_CONFLICT"
        );
        handle.join().unwrap();
        std::fs::remove_file(path).unwrap();
    }
    #[test]
    fn base64_handshake_reference() {
        assert_eq!(base64(b"the sample nonce"), "dGhlIHNhbXBsZSBub25jZQ==");
        assert_eq!(
            base64(&Sha1::digest(
                b"dGhlIHNhbXBsZSBub25jZQ==258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
            )),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
        );
    }
}
