//! Private loopback HTTP fixture; no production configuration or service access.
use clinx_decision_kernel::{node_tls::Result, observer_http::Runtime};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    io::{Read, Write},
    net::{SocketAddr, TcpStream},
    sync::Arc,
    time::Duration,
};

pub type Callback = Arc<dyn Fn(&str, Value) -> Result<Value> + Send + Sync>;
pub struct Server {
    runtime: Runtime,
    bound: Value,
    call: Callback,
    closed: bool,
}
impl Server {
    pub fn new(call: Callback, audit: bool) -> Self {
        let runtime = Runtime::default();
        let c = call.clone();
        let bound = runtime
            .invoke(
                "observer_http.bind",
                &json!({"port":0,"audit_methods":audit}),
                move |op, a| c(op, a),
            )
            .unwrap();
        assert_eq!(bound["address"][0], "127.0.0.1");
        let server = Self {
            runtime,
            bound,
            call,
            closed: false,
        };
        server.start();
        server
    }
    pub fn address(&self) -> SocketAddr {
        (
            [127, 0, 0, 1],
            self.bound["address"][1].as_u64().unwrap() as u16,
        )
            .into()
    }
    pub fn start(&self) {
        let c = self.call.clone();
        self.runtime
            .invoke("observer_http.start", &self.bound, move |op, a| c(op, a))
            .unwrap();
    }
    pub fn stop(&self) {
        let c = self.call.clone();
        self.runtime
            .invoke("observer_http.shutdown", &self.bound, move |op, a| c(op, a))
            .unwrap();
    }
    pub fn close(&mut self) {
        if !self.closed {
            let c = self.call.clone();
            self.runtime
                .invoke("observer_http.close", &self.bound, move |op, a| c(op, a))
                .unwrap();
            self.closed = true;
        }
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        self.close();
    }
}
pub fn raw(address: SocketAddr, wire: &[u8]) -> Vec<u8> {
    let mut socket = TcpStream::connect_timeout(&address, Duration::from_secs(3)).unwrap();
    socket
        .set_read_timeout(Some(Duration::from_secs(6)))
        .unwrap();
    socket
        .set_write_timeout(Some(Duration::from_secs(3)))
        .unwrap();
    socket.write_all(wire).unwrap();
    let mut out = Vec::new();
    let mut buffer = [0; 65536];
    loop {
        match socket.read(&mut buffer) {
            Ok(0) => break,
            Ok(n) => out.extend_from_slice(&buffer[..n]),
            Err(e) if e.kind() == std::io::ErrorKind::ConnectionReset => break,
            Err(e) => panic!("private fixture socket: {e}"),
        }
    }
    out
}
pub struct Response {
    pub status: u16,
    pub headers: BTreeMap<String, String>,
    pub body: Vec<u8>,
}
impl Response {
    pub fn value(&self) -> Value {
        serde_json::from_slice(&self.body).unwrap()
    }
}
pub fn http(
    address: SocketAddr,
    method: &str,
    path: &str,
    headers: &[(&str, &str)],
    body: &[u8],
) -> Response {
    let mut wire = format!("{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n");
    for (k, v) in headers {
        wire.push_str(&format!("{k}: {v}\r\n"));
    }
    if !body.is_empty() {
        wire.push_str(&format!("Content-Length: {}\r\n", body.len()));
    }
    wire.push_str("\r\n");
    let mut wire = wire.into_bytes();
    wire.extend(body);
    let response = raw(address, &wire);
    let end = response.windows(4).position(|w| w == b"\r\n\r\n").unwrap();
    let head = std::str::from_utf8(&response[..end]).unwrap();
    let mut lines = head.split("\r\n");
    let status = lines
        .next()
        .unwrap()
        .split_whitespace()
        .nth(1)
        .unwrap()
        .parse()
        .unwrap();
    let headers = lines
        .map(|line| {
            let (k, v) = line.split_once(':').unwrap();
            (k.into(), v.trim().into())
        })
        .collect();
    Response {
        status,
        headers,
        body: response[end + 4..].to_vec(),
    }
}
