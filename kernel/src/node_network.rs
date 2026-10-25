//! Persistent bounded Node wire transport. Python observation callbacks have no socket.
//! TLS peer metadata is emitted separately from the untrusted request body.
use crate::node_tls::{self, FRAME, Result};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    io::{BufRead, BufReader, Read, Write},
    net::{TcpListener, ToSocketAddrs},
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc,
    },
    thread,
    time::Duration,
};
const PRIVATE_FRAME: usize = 18 * 1024 * 1024;
#[derive(Default)]
struct Worker {
    next: AtomicU64,
    stopped: AtomicBool,
    relay: crate::node_relay::Relay,
    registry: crate::node_registry::Registry,
    gateways: Mutex<Vec<crate::node_gateway::Gateway>>,
    replies: Mutex<BTreeMap<u64, mpsc::SyncSender<Value>>>,
}
fn emit(v: &Value) -> Result<()> {
    let mut bytes = serde_json::to_vec(v).map_err(|_| "NODE_RESPONSE_INVALID")?;
    bytes.push(b'\n');
    if bytes.len() > PRIVATE_FRAME {
        return Err("NODE_RESPONSE_TOO_LARGE".into());
    }
    std::io::stdout()
        .lock()
        .write_all(&bytes)
        .map_err(|_| "NODE_WORKER_UNAVAILABLE".into())
}
fn packet<S: Read + Write>(stream: &mut S) -> Result<Value> {
    let mut data = Vec::new();
    let read = BufReader::new(stream)
        .take((FRAME + 1) as u64)
        .read_until(b'\n', &mut data);
    if read.is_err_and(|e| e.kind() != std::io::ErrorKind::UnexpectedEof) {
        return Err("INVALID_NODE_REQUEST".into());
    }
    // Deployed v1 accepts a complete JSON object at EOF as well as newline.
    // Truncation is still rejected by JSON parsing; a send is never retried.
    if data.len() > FRAME || data.is_empty() {
        return Err("INVALID_NODE_REQUEST".into());
    }
    let v: Value = serde_json::from_slice(&data).map_err(|_| "INVALID_NODE_REQUEST")?;
    if !v.is_object() || !v["operation"].is_string() {
        return Err("INVALID_NODE_REQUEST".into());
    }
    Ok(v)
}
fn respond<S: Write>(stream: &mut S, result: &Value) -> Result<()> {
    let mut bytes = serde_json::to_vec(result).map_err(|_| "NODE_RESPONSE_INVALID")?;
    bytes.push(b'\n');
    if bytes.len() > FRAME {
        return Err("NODE_RESPONSE_TOO_LARGE".into());
    }
    stream
        .write_all(&bytes)
        .and_then(|_| stream.flush())
        .map_err(|_| "NODE_RESPONSE_UNAVAILABLE".into())
}
impl Worker {
    fn callback(&self, operation: &str, arguments: Value) -> Result<Value> {
        let id = self.next.fetch_add(1, Ordering::Relaxed) + 1;
        let (tx, rx) = mpsc::sync_channel(1);
        self.replies
            .lock()
            .map_err(|_| "NODE_WORKER_UNAVAILABLE")?
            .insert(id, tx);
        let sent = emit(
            &json!({"kind":"callback","connection_id":id,"operation":operation,"arguments":arguments}),
        );
        let response = if sent.is_ok() {
            rx.recv_timeout(Duration::from_secs(120)).ok()
        } else {
            None
        };
        self.replies
            .lock()
            .map_err(|_| "NODE_WORKER_UNAVAILABLE")?
            .remove(&id);
        let response = response.ok_or("PROVIDER_DELIVERY_UNKNOWN")?;
        if let Some(code) = response["error"].as_str() {
            return Err(code.to_owned());
        }
        if response.get("result").is_none() {
            return Err("PROVIDER_DELIVERY_UNKNOWN".into());
        }
        Ok(response["result"].clone())
    }
    fn incoming<S: Read + Write>(
        &self,
        stream: &mut S,
        peer: Value,
        der: &[u8],
        address: &str,
        port: u16,
    ) -> Result<()> {
        let request = match packet(stream) {
            Ok(v) => v,
            Err(_) => {
                return respond(
                    stream,
                    &json!({"error_code":"INVALID_NODE_REQUEST","read_only":true}),
                );
            }
        };
        let id = self.next.fetch_add(1, Ordering::Relaxed) + 1;
        let (tx, rx) = mpsc::sync_channel(1);
        self.replies
            .lock()
            .map_err(|_| "NODE_WORKER_UNAVAILABLE")?
            .insert(id, tx);
        let cert = der.iter().map(|v| format!("{v:02x}")).collect::<String>();
        let sent = emit(
            &json!({"kind":"incoming","connection_id":id,"request":request,"peer":peer,"certificate_hex":cert,"address":[address,port]}),
        );
        let reply = if sent.is_ok() {
            rx.recv_timeout(Duration::from_secs(120)).ok()
        } else {
            None
        };
        self.replies
            .lock()
            .map_err(|_| "NODE_WORKER_UNAVAILABLE")?
            .remove(&id);
        respond(stream,&reply.unwrap_or(json!({"error_code":"NODE_REQUEST_FAILED","operation_state":"UNKNOWN","side_effect":"UNKNOWN","retry":"RECONCILIATION_REQUIRED"})))
    }
    fn listen(self: &Arc<Self>, a: &Value) -> Result<Value> {
        let host = a["address"].as_str().ok_or("INVALID_NODE_ENDPOINT")?;
        let port = a["port"]
            .as_u64()
            .filter(|p| *p <= 65535)
            .ok_or("INVALID_NODE_ENDPOINT")? as u16;
        let state = a["state"].as_str().map(PathBuf::from);
        if state.is_none()
            && (a["allow_insecure_loopback"] != true || !matches!(host, "127.0.0.1" | "::1"))
        {
            return Err("PAIRED_TLS_REQUIRED".into());
        }
        if let Some(root) = state.as_ref() {
            node_tls::server(root)?;
        }
        // Preserve ThreadingTCPServer.allow_reuse_address across process restart.
        // SO_REUSEPORT is deliberately not enabled: there is one bound listener.
        let mut listener = None;
        for address in (host, port)
            .to_socket_addrs()
            .map_err(|_| "NODE_LISTEN_UNAVAILABLE")?
        {
            use rustix::net::{AddressFamily, SocketFlags, SocketType};
            let attempt = (|| -> Result<TcpListener> {
                let fd = rustix::net::socket_with(
                    if address.is_ipv4() {
                        AddressFamily::INET
                    } else {
                        AddressFamily::INET6
                    },
                    SocketType::STREAM,
                    SocketFlags::CLOEXEC,
                    Some(rustix::net::ipproto::TCP),
                )
                .map_err(|_| "NODE_LISTEN_UNAVAILABLE")?;
                rustix::net::sockopt::set_socket_reuseaddr(&fd, true)
                    .map_err(|_| "NODE_LISTEN_UNAVAILABLE")?;
                rustix::net::bind(&fd, &address).map_err(|_| "NODE_LISTEN_UNAVAILABLE")?;
                rustix::net::listen(&fd, 128).map_err(|_| "NODE_LISTEN_UNAVAILABLE")?;
                Ok(TcpListener::from(fd))
            })();
            if let Ok(socket) = attempt {
                listener = Some(socket);
                break;
            }
        }
        let listener = listener.ok_or("NODE_LISTEN_UNAVAILABLE")?;
        listener
            .set_nonblocking(true)
            .map_err(|_| "NODE_LISTEN_UNAVAILABLE")?;
        let bound = listener
            .local_addr()
            .map_err(|_| "NODE_LISTEN_UNAVAILABLE")?;
        let worker = self.clone();
        thread::spawn(move || {
            while !worker.stopped.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((mut socket, address)) => {
                        let worker = worker.clone();
                        let state = state.clone();
                        thread::spawn(move || {
                            if socket
                                .set_read_timeout(Some(Duration::from_secs(5)))
                                .and_then(|_| {
                                    socket.set_write_timeout(Some(Duration::from_secs(5)))
                                })
                                .is_err()
                            {
                                return;
                            }
                            if let Some(root) = state {
                                if let Ok((mut stream, peer, der)) =
                                    node_tls::connection(socket, &root)
                                {
                                    let _ = worker.incoming(
                                        &mut stream,
                                        peer,
                                        &der,
                                        &address.ip().to_string(),
                                        address.port(),
                                    );
                                }
                            } else {
                                let _ = worker.incoming(
                                    &mut socket,
                                    Value::Null,
                                    &[],
                                    &address.ip().to_string(),
                                    address.port(),
                                );
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
        Ok(json!({"address":[bound.ip().to_string(),bound.port()]}))
    }
}
pub fn command(operation: &str, a: &Value) -> Result<Value> {
    if operation == "registry.read" {
        return crate::node_registry::Registry::read(a);
    }
    if operation.starts_with("history.") {
        return crate::native_history::invoke(operation, a);
    }
    if operation.starts_with("observation.") {
        return crate::observation::invoke(operation, a, |_, _| {
            Err("OBSERVATION_CALLBACK_REQUIRED".into())
        });
    }
    if operation == "scope.intersect" {
        return crate::node_scope::intersect(
            crate::node_scope::optional(&a["local"])?,
            crate::node_scope::optional(&a["centre"])?,
        )
        .map(|s| s.value());
    }
    if operation == "relay.exchange" {
        return crate::node_relay::exchange(a);
    }
    let root = PathBuf::from(a["state"].as_str().ok_or("IDENTITY_BOOTSTRAP_REQUIRED")?);
    match operation {
        "network.peer" => node_tls::peer(&root, a["peer_id"].as_str().ok_or("PAIRING_REQUIRED")?),
        "network.authenticate" => {
            let h = a["certificate_hex"].as_str().ok_or("INVALID_CERTIFICATE")?;
            let der = node_tls::decode_certificate(h)?;
            node_tls::authenticate(&root, &der, None, true)
        }
        "network.call" => node_tls::call(
            &root,
            a["peer_id"].as_str().ok_or("PAIRING_REQUIRED")?,
            a["endpoint"].as_str().ok_or("INVALID_NODE_ENDPOINT")?,
            a["timeout"].as_f64().ok_or("TIMEOUT_INVALID")?,
            &a["request"],
        ),
        _ => Err("NODE_NETWORK_OPERATION_UNKNOWN".into()),
    }
}
pub fn serve() -> Result<()> {
    let worker = Arc::new(Worker::default());
    let input = std::io::stdin();
    let mut input = input.lock();
    loop {
        let mut bytes = Vec::new();
        input
            .by_ref()
            .take((PRIVATE_FRAME + 1) as u64)
            .read_until(b'\n', &mut bytes)
            .map_err(|_| "NODE_WORKER_UNAVAILABLE")?;
        if bytes.is_empty() {
            break;
        }
        if bytes.len() > PRIVATE_FRAME || bytes.last() != Some(&b'\n') {
            return Err("NODE_WORKER_FRAME_INVALID".into());
        }
        let v: Value = serde_json::from_slice(&bytes).map_err(|_| "NODE_WORKER_FRAME_INVALID")?;
        let operation = v["operation"].as_str().ok_or("NODE_WORKER_FRAME_INVALID")?;
        if operation == "reply" {
            let id = v["connection_id"]
                .as_u64()
                .ok_or("NODE_WORKER_FRAME_INVALID")?;
            if let Some(tx) = worker
                .replies
                .lock()
                .map_err(|_| "NODE_WORKER_UNAVAILABLE")?
                .remove(&id)
            {
                let _ = tx.send(v["result"].clone());
            }
            continue;
        }
        let worker = worker.clone();
        thread::spawn(move || {
            let operation = v["operation"].as_str().unwrap();
            let result = match operation {
                "listen" => worker.listen(&v["arguments"]),
                "service.handle" => crate::node_service::handle(&v["arguments"], |op, args| {
                    worker.callback(op, args)
                }),
                "gateway.request" => crate::node_gateway::request(&v["arguments"]),
                "gateway.listen" => {
                    let w = worker.clone();
                    let root = v["arguments"]["state"].clone();
                    crate::node_gateway::listen(&v["arguments"],move|r|{
                        let body=serde_json::to_string(&json!({"method":r["method"],"params":r["params"]})).map_err(|_|"PROVIDER_DELIVERY_UNKNOWN")?;
                        crate::node_paired::exchange(&json!({"state":root,"node_id":r["context"]["host"],"context":r["context"],"body_wire":body}),|op,args|w.callback(op,args))
                    }).and_then(|g|{worker.gateways.lock().map_err(|_|"PROVIDER_GATEWAY_UNAVAILABLE")?.push(g);Ok(json!({"ready":true}))})
                }
                "paired.exchange" => crate::node_paired::exchange(&v["arguments"], |op, args| {
                    worker.callback(op, args)
                }),
                "relay.count" => worker.relay.count().map(|n| json!(n)),
                "relay.handle" => worker.relay.handle(&v["arguments"], |context, body| {
                    worker.callback("relay.exchange", json!({"context":context,"body":body}))
                }),
                "registry.read" => crate::node_registry::Registry::read(&v["arguments"]),
                op if op.starts_with("registry.") => worker.registry.invoke(op, &v["arguments"]),
                op if op.starts_with("observation.") => {
                    crate::observation::invoke(op, &v["arguments"], |op, args| {
                        worker.callback(op, args)
                    })
                }
                _ => command(operation, &v["arguments"]),
            };
            let response = match result {
                Ok(r) => json!({"kind":"result","id":v["id"],"result":r}),
                Err(e) => json!({"kind":"result","id":v["id"],"error":e}),
            };
            let _ = emit(&response);
        });
    }
    worker.stopped.store(true, Ordering::Relaxed);
    worker
        .gateways
        .lock()
        .map_err(|_| "NODE_WORKER_UNAVAILABLE")?
        .clear();
    worker
        .replies
        .lock()
        .map_err(|_| "NODE_WORKER_UNAVAILABLE")?
        .clear();
    Ok(())
}
