//! First pairing and authenticated discovery sessions over TLS 1.3. The current
//! OPAQUE transcript, window budget and durable trust files are authoritative;
//! discovery authentication grants no task/execution permission.
use crate::{
    discovery_identity as identity, discovery_state,
    node_tls::{self, Result},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use clinx_opaque_core::{Client as OpaqueClient, Server as OpaqueServer};
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::server::danger::{ClientCertVerified, ClientCertVerifier};
use rustls::{ClientConfig, ClientConnection, ServerConfig, ServerConnection, StreamOwned};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs::File,
    io::{Read, Write},
    net::{TcpListener, TcpStream, ToSocketAddrs},
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
    },
    thread,
    time::{Duration, Instant},
};
pub const PROTOCOL: &str = clinx_opaque_core::BACKEND_ID;
const FRAME: usize = 16384;
type Callback = Arc<dyn Fn(&str, Value) -> Result<Value> + Send + Sync>;
type ClientStream = StreamOwned<ClientConnection, TcpStream>;
type ServerStream = StreamOwned<ServerConnection, TcpStream>;
fn text(v: &Value) -> &str {
    v.as_str().unwrap_or("")
}
fn random<const N: usize>() -> Result<[u8; N]> {
    let mut b = [0; N];
    File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut b))
        .map_err(|_| "PAIRING_RANDOM_UNAVAILABLE")?;
    Ok(b)
}
fn nonce() -> Result<String> {
    Ok(random::<16>()?.iter().map(|b| format!("{b:02x}")).collect())
}
fn decoded(v: &Value) -> Result<Vec<u8>> {
    STANDARD
        .decode(v.as_str().ok_or("INVALID_ENCODING")?)
        .map_err(|_| "INVALID_ENCODING".into())
}
fn canonical(v: &Value) -> Vec<u8> {
    serde_json::to_string(v)
        .unwrap()
        .chars()
        .map(|c| {
            if c.is_ascii() {
                c.to_string()
            } else {
                c.encode_utf16(&mut [0; 2])
                    .iter()
                    .map(|v| format!("\\u{v:04x}"))
                    .collect()
            }
        })
        .collect::<String>()
        .into_bytes()
}
pub fn binding(
    client: &Value,
    server: &Value,
    window: &str,
    session: &str,
    client_nonce: &str,
) -> Result<(Vec<u8>, Vec<u8>, Vec<u8>)> {
    let client = identity::public_record(client)?;
    let server = identity::public_record(server)?;
    if client["node_id"] == server["node_id"] || client["public_key"] == server["public_key"] {
        return Err("DUPLICATE_PAIRING_IDENTITY".into());
    }
    if [window, session, client_nonce].iter().any(|s| {
        s.len() != 32
            || !s
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    }) {
        return Err("INVALID_PAIRING_SESSION".into());
    }
    Ok((
        canonical(
            &json!({"service":"CLINX_LOCAL_PAIRING","version":2,"protocol":PROTOCOL,"window_id":window,"session_id":session,"client_nonce":client_nonce,"initiator":client,"acceptor":server}),
        ),
        canonical(&json!({"role":"initiator","identity":client})),
        canonical(&json!({"role":"acceptor","identity":server})),
    ))
}
fn cert_pem(der: &[u8]) -> String {
    let b = STANDARD.encode(der);
    format!(
        "-----BEGIN CERTIFICATE-----\n{}-----END CERTIFICATE-----\n",
        b.as_bytes()
            .chunks(64)
            .map(|s| format!("{}\n", std::str::from_utf8(s).unwrap()))
            .collect::<String>()
    )
}
fn state(runtime: &discovery_state::Runtime, op: &str, a: Value) -> Result<Value> {
    runtime.invoke(&format!("discovery.{op}"), &a, |_, _| {
        Err("PAIRING_REPLAY".into())
    })
}
fn all(runtime: &discovery_state::Runtime, root: &Path) -> Result<Value> {
    state(runtime, "peers.all", json!({"root":root}))
}
fn trust(
    runtime: &discovery_state::Runtime,
    root: &Path,
    public: &Value,
    certificate: &str,
) -> Result<()> {
    state(runtime,"peers.trust",json!({"root":root,"public":public,"certificate":certificate,"security_status":"OPAQUE_V1","pairing_protocol":PROTOCOL,"fresh_pairing":true})).map(|_|())
}
fn authenticate(
    runtime: &discovery_state::Runtime,
    root: &Path,
    id: &str,
    der: &[u8],
) -> Result<Value> {
    let (public, _) = node_tls::public(der)?;
    state(
        runtime,
        "peers.authenticate",
        json!({"root":root,"node_id":id,"public_key":public,"allow_dev":false}),
    )
}
fn lan(ip: &str) -> Result<()> {
    crate::discovery_index::invoke("discovery_index.lan_address", &json!({"value":ip})).map(|_| ())
}
fn check_candidate(
    runtime: &discovery_state::Runtime,
    root: &Path,
    c: &Value,
    pairing: bool,
) -> Result<()> {
    if c["mismatch"] == true {
        return Err("IDENTITY_MISMATCH".into());
    }
    let peers = all(runtime, root)?;
    let old = &peers[text(&c["node_id"])];
    if !old.is_null() && old["fingerprint"] != c["fingerprint"] {
        return Err("IDENTITY_MISMATCH".into());
    }
    if !old.is_null() && old["trust_state"] != "TRUSTED" {
        return Err("TRUST_REVOKED".into());
    }
    if !pairing && old.is_null() {
        return Err("UNTRUSTED_DEVICE".into());
    }
    if !pairing && old["security_status"] == "DEV_ONLY" {
        return Err("SECURITY_BLOCKER_DEV_ONLY".into());
    }
    Ok(())
}
#[derive(Debug)]
struct Verify {
    root: PathBuf,
    pairing: bool,
    expected: Option<String>,
    fingerprint: Option<String>,
}
fn tls_error(_: String) -> rustls::Error {
    rustls::Error::General("IDENTITY_MISMATCH".into())
}
fn verify_signature(
    m: &[u8],
    c: &CertificateDer<'_>,
    d: &rustls::DigitallySignedStruct,
) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
    rustls::crypto::verify_tls13_signature(
        m,
        c,
        d,
        &rustls::crypto::ring::default_provider().signature_verification_algorithms,
    )
}
fn schemes() -> Vec<rustls::SignatureScheme> {
    rustls::crypto::ring::default_provider()
        .signature_verification_algorithms
        .supported_schemes()
}
impl ServerCertVerifier for Verify {
    fn verify_server_cert(
        &self,
        end: &CertificateDer<'_>,
        _: &[CertificateDer<'_>],
        _: &ServerName<'_>,
        _: &[u8],
        _: UnixTime,
    ) -> std::result::Result<ServerCertVerified, rustls::Error> {
        let (_, fp) = node_tls::public(end).map_err(tls_error)?;
        if self
            .fingerprint
            .as_ref()
            .is_some_and(|expected| expected != &fp)
        {
            return Err(tls_error("IDENTITY_MISMATCH".into()));
        }
        if !self.pairing {
            node_tls::authenticate(&self.root, end, self.expected.as_deref(), false)
                .map_err(tls_error)?;
        }
        Ok(ServerCertVerified::assertion())
    }
    fn verify_tls12_signature(
        &self,
        _: &[u8],
        _: &CertificateDer<'_>,
        _: &rustls::DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        Err(tls_error("TLS13_REQUIRED".into()))
    }
    fn verify_tls13_signature(
        &self,
        m: &[u8],
        c: &CertificateDer<'_>,
        d: &rustls::DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        verify_signature(m, c, d)
    }
    fn supported_verify_schemes(&self) -> Vec<rustls::SignatureScheme> {
        schemes()
    }
}
impl ClientCertVerifier for Verify {
    fn client_auth_mandatory(&self) -> bool {
        false
    }
    fn root_hint_subjects(&self) -> &[rustls::DistinguishedName] {
        &[]
    }
    fn verify_client_cert(
        &self,
        end: &CertificateDer<'_>,
        _: &[CertificateDer<'_>],
        _: UnixTime,
    ) -> std::result::Result<ClientCertVerified, rustls::Error> {
        node_tls::authenticate(&self.root, end, None, false).map_err(tls_error)?;
        Ok(ClientCertVerified::assertion())
    }
    fn verify_tls12_signature(
        &self,
        _: &[u8],
        _: &CertificateDer<'_>,
        _: &rustls::DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        Err(tls_error("TLS13_REQUIRED".into()))
    }
    fn verify_tls13_signature(
        &self,
        m: &[u8],
        c: &CertificateDer<'_>,
        d: &rustls::DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        verify_signature(m, c, d)
    }
    fn supported_verify_schemes(&self) -> Vec<rustls::SignatureScheme> {
        schemes()
    }
}
trait Wire: Read + Write {
    fn read_timeout(&mut self, t: Duration) -> Result<()>;
}
impl Wire for ClientStream {
    fn read_timeout(&mut self, t: Duration) -> Result<()> {
        self.sock
            .set_read_timeout(Some(t))
            .map_err(|_| "FRAME_TIMEOUT".into())
    }
}
impl Wire for ServerStream {
    fn read_timeout(&mut self, t: Duration) -> Result<()> {
        self.sock
            .set_read_timeout(Some(t))
            .map_err(|_| "FRAME_TIMEOUT".into())
    }
}
fn send(sock: &mut dyn Wire, value: &Value) -> Result<()> {
    let mut b = canonical(value);
    b.push(b'\n');
    if b.len() > FRAME {
        return Err("FRAME_TOO_LARGE".into());
    }
    sock.write_all(&b)
        .and_then(|_| sock.flush())
        .map_err(|_| "CONNECTION_CLOSED".into())
}
fn receive(sock: &mut dyn Wire) -> Result<Value> {
    let end = Instant::now() + Duration::from_secs(5);
    let mut b = Vec::new();
    while b.len() < FRAME {
        sock.read_timeout(
            end.checked_duration_since(Instant::now())
                .filter(|v| !v.is_zero())
                .ok_or("FRAME_TIMEOUT")?,
        )?;
        let mut byte = [0];
        let n = sock.read(&mut byte).map_err(|e| {
            if matches!(
                e.kind(),
                std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
            ) {
                "FRAME_TIMEOUT"
            } else {
                "CONNECTION_CLOSED"
            }
        })?;
        if n == 0 {
            return Err("CONNECTION_CLOSED".into());
        }
        b.push(byte[0]);
        if byte[0] == b'\n' {
            let v: Value = serde_json::from_slice(&b).map_err(|_| "INVALID_FRAME")?;
            if !v.is_object() {
                return Err("INVALID_FRAME".into());
            }
            if v.get("error").is_some() {
                return Err("REMOTE_REQUEST_REJECTED".into());
            }
            return Ok(v);
        }
    }
    Err("FRAME_TOO_LARGE".into())
}
fn connect(
    runtime: &discovery_state::Runtime,
    root: &Path,
    c: &Value,
    pairing: bool,
    direct: bool,
) -> Result<ClientStream> {
    check_candidate(runtime, root, c, pairing)?;
    let builder = ClientConfig::builder_with_protocol_versions(&[&rustls::version::TLS13])
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(Verify {
            root: root.into(),
            pairing,
            expected: Some(text(&c["node_id"]).into()),
            fingerprint: Some(text(&c["fingerprint"]).into()),
        }));
    let cfg = if pairing {
        builder.with_no_client_auth()
    } else {
        let (cert, key) = identity::credentials(root)?;
        builder
            .with_client_auth_cert(cert, key)
            .map_err(|_| "LOCAL_KEY_MISMATCH")?
    };
    let cfg = Arc::new(cfg);
    for endpoint in c["endpoints"].as_array().ok_or("DEVICE_UNREACHABLE")? {
        let address = endpoint[0].as_str().ok_or("INVALID_LAN_ADDRESS")?;
        let port = endpoint[1]
            .as_u64()
            .filter(|v| *v > 0 && *v <= 65535)
            .ok_or("DEVICE_UNREACHABLE")? as u16;
        if !direct {
            lan(address)?;
        }
        let Ok(addrs) = (address, port).to_socket_addrs() else {
            continue;
        };
        for addr in addrs {
            let Ok(sock) = TcpStream::connect_timeout(&addr, Duration::from_secs(5)) else {
                continue;
            };
            sock.set_read_timeout(Some(Duration::from_secs(5)))
                .and_then(|_| sock.set_write_timeout(Some(Duration::from_secs(5))))
                .map_err(|_| "DEVICE_UNREACHABLE")?;
            let name =
                ServerName::try_from(address.to_owned()).map_err(|_| "INVALID_LAN_ADDRESS")?;
            let conn =
                ClientConnection::new(cfg.clone(), name).map_err(|_| "TLS_HANDSHAKE_FAILED")?;
            let mut stream = StreamOwned::new(conn, sock);
            while stream.conn.is_handshaking() {
                stream
                    .conn
                    .complete_io(&mut stream.sock)
                    .map_err(|_| "TLS_HANDSHAKE_FAILED")?;
            }
            return Ok(stream);
        }
    }
    Err("DEVICE_UNREACHABLE".into())
}
fn peer_client(stream: &ClientStream) -> Result<Vec<u8>> {
    stream
        .conn
        .peer_certificates()
        .and_then(|v| v.first())
        .map(|v| v.to_vec())
        .ok_or("CLIENT_CERTIFICATE_REQUIRED".into())
}
fn peer_server(stream: &ServerStream) -> Result<Vec<u8>> {
    stream
        .conn
        .peer_certificates()
        .and_then(|v| v.first())
        .map(|v| v.to_vec())
        .ok_or("CLIENT_CERTIFICATE_REQUIRED".into())
}
fn local(root: &Path) -> Result<Value> {
    let v = node_tls::read_state(root, "identity.json")?.ok_or("IDENTITY_BOOTSTRAP_REQUIRED")?;
    identity::validate(&v, text(&v["node_id"]))?;
    Ok(v)
}
fn session(
    runtime: &discovery_state::Runtime,
    root: &Path,
    c: &Value,
    direct: bool,
) -> Result<Value> {
    let mut sock = connect(runtime, root, c, false, direct)?;
    let public = local(root)?;
    let der = peer_client(&sock)?;
    send(
        &mut sock,
        &json!({"op":"session","node_id":public["node_id"]}),
    )?;
    if receive(&mut sock)?
        != json!({"node_id":c["node_id"],"capabilities":[],"authority_granted":false,"authorization_required":true})
    {
        return Err("INVALID_CAPABILITY_NEGOTIATION".into());
    }
    let peer = authenticate(runtime, root, text(&c["node_id"]), &der)?;
    Ok(
        json!({"node_id":c["node_id"],"fingerprint":peer["fingerprint"],"transport":"lan_tls","capabilities":[],"authority_granted":false,"authorization_required":true}),
    )
}
fn pair(
    runtime: &discovery_state::Runtime,
    root: &Path,
    c: &Value,
    code: &str,
    direct: bool,
) -> Result<Value> {
    let raw = local(root)?;
    let public = identity::public_record(&raw)?;
    if c["node_id"] == public["node_id"] {
        return Err("DUPLICATE_LOCAL_NODE_ID".into());
    }
    let client_nonce = nonce()?;
    let mut sock = connect(runtime, root, c, true, direct)?;
    let cert = cert_pem(&peer_client(&sock)?);
    send(
        &mut sock,
        &json!({"op":"pair","protocol":PROTOCOL,"client_nonce":client_nonce,"identity":public,"certificate":raw["certificate"]}),
    )?;
    let hello = receive(&mut sock)?;
    if hello["protocol"] != PROTOCOL {
        return Err("PAIRING_PROTOCOL_MISMATCH".into());
    }
    let server = identity::public_record(&hello["identity"])?;
    if server["node_id"] != c["node_id"]
        || server["public_key"] != node_tls::public(&node_tls::cert(&cert)?)?.0
    {
        return Err("IDENTITY_MISMATCH".into());
    }
    let (context, client_id, server_id) = binding(
        &public,
        &server,
        text(&hello["window_id"]),
        text(&hello["session_id"]),
        &client_nonce,
    )?;
    let mut exchange =
        OpaqueClient::new(code.as_bytes(), context, client_id, server_id).map_err(str::to_owned)?;
    send(
        &mut sock,
        &json!({"protocol":PROTOCOL,"message":STANDARD.encode(exchange.start_message().map_err(str::to_owned)?)}),
    )?;
    let reply = receive(&mut sock)?;
    if reply["protocol"] != PROTOCOL {
        return Err("PAIRING_PROTOCOL_MISMATCH".into());
    }
    let finalization = exchange
        .finish_message(&decoded(&reply["message"])?)
        .map_err(str::to_owned)?;
    send(
        &mut sock,
        &json!({"protocol":PROTOCOL,"message":STANDARD.encode(finalization)}),
    )?;
    if receive(&mut sock)? != json!({"protocol":PROTOCOL,"paired":true}) {
        return Err("PAIRING_COMMIT_FAILED".into());
    }
    trust(runtime, root, &server, &cert)?;
    drop(sock);
    session(runtime, root, c, direct)
}
struct Server {
    root: PathBuf,
    identity: Value,
    window: u64,
    state: Arc<discovery_state::Runtime>,
    callback: Callback,
    handler: Value,
    custom_clock: bool,
    observer: bool,
    direct: bool,
    stopped: AtomicBool,
    active: AtomicUsize,
    thread: Mutex<Option<thread::JoinHandle<()>>>,
    clients: Mutex<Vec<thread::JoinHandle<()>>>,
    lease: Mutex<Option<identity::OwnedLock>>,
}
impl Server {
    fn now(&self) -> Result<Value> {
        if self.custom_clock {
            (self.callback)("discovery.enrollment.clock", json!({"token":self.handler}))
        } else {
            Ok(Value::Null)
        }
    }
    fn window(&self, op: &str, mut args: Value) -> Result<Value> {
        args["window"] = json!(self.window);
        args["now"] = self.now()?;
        state(&self.state, &format!("window.{op}"), args)
    }
    fn handle(&self, sock: &mut ServerStream) -> Result<()> {
        let mut reserved = None;
        let result = (|| {
            let req = receive(sock)?;
            match text(&req["op"]) {
                "session" => {
                    authenticate(
                        &self.state,
                        &self.root,
                        text(&req["node_id"]),
                        &peer_server(sock)?,
                    )?;
                    send(
                        sock,
                        &json!({"node_id":self.identity["node_id"],"capabilities":[],"authority_granted":false,"authorization_required":true}),
                    )
                }
                "observer_connection" => {
                    authenticate(
                        &self.state,
                        &self.root,
                        text(&req["node_id"]),
                        &peer_server(sock)?,
                    )?;
                    if !self.observer {
                        return Err("OBSERVER_SHARING_DISABLED".into());
                    }
                    let mut connection = (self.callback)(
                        "discovery.enrollment.observer",
                        json!({"token":self.handler}),
                    )?;
                    connection["node_id"] = self.identity["node_id"].clone();
                    send(sock, &connection)
                }
                "pair" => {
                    let values = self.window("reserve", json!({}))?;
                    let reservation = text(&values[0]).to_owned();
                    let window_id = text(&values[1]);
                    let code = text(&values[2]);
                    reserved = Some(reservation.clone());
                    if req["protocol"] != PROTOCOL {
                        return Err("PAIRING_PROTOCOL_MISMATCH".into());
                    }
                    let client = identity::public_record(&req["identity"])?;
                    if client["node_id"] == self.identity["node_id"] {
                        return Err("DUPLICATE_LOCAL_NODE_ID".into());
                    }
                    let certificate = text(&req["certificate"]);
                    if node_tls::public(&node_tls::cert(certificate)?)?.0 != client["public_key"] {
                        return Err("IDENTITY_MISMATCH".into());
                    }
                    let peers = all(&self.state, &self.root)?;
                    let old = &peers[text(&client["node_id"])];
                    if !old.is_null()
                        && (old["public_key"] != client["public_key"]
                            || old["trust_state"] != "TRUSTED")
                    {
                        return Err("IDENTITY_MISMATCH".into());
                    }
                    let public = identity::public_record(&self.identity)?;
                    let (context, cid, sid) = binding(
                        &client,
                        &public,
                        window_id,
                        &reservation,
                        text(&req["client_nonce"]),
                    )?;
                    send(
                        sock,
                        &json!({"protocol":PROTOCOL,"identity":public,"window_id":window_id,"session_id":reservation}),
                    )?;
                    let first = receive(sock)?;
                    if first["protocol"] != PROTOCOL {
                        return Err("PAIRING_PROTOCOL_MISMATCH".into());
                    }
                    let mut exchange = OpaqueServer::new(
                        code.as_bytes(),
                        context,
                        cid,
                        sid,
                        &decoded(&first["message"])?,
                    )
                    .map_err(str::to_owned)?;
                    send(
                        sock,
                        &json!({"protocol":PROTOCOL,"message":STANDARD.encode(exchange.start_message().map_err(str::to_owned)?)}),
                    )?;
                    let answer = receive(sock)?;
                    if answer["protocol"] != PROTOCOL {
                        return Err("PAIRING_PROTOCOL_MISMATCH".into());
                    }
                    exchange
                        .finish(&decoded(&answer["message"])?)
                        .map_err(str::to_owned)?;
                    self.state.invoke("discovery.window.complete",&json!({"window":self.window,"now":self.now()?,"reservation":reservation,"success":true,"commit":true,"max_failures":3}),|op,_|{if op!="discovery.commit"{return Err("PAIRING_REPLAY".into());}trust(&self.state,&self.root,&client,certificate).map(|_|Value::Null)})?;
                    reserved = None;
                    send(sock, &json!({"protocol":PROTOCOL,"paired":true}))
                }
                _ => Err("EXISTING_AUTHORIZATION_REQUIRED".into()),
            }
        })();
        if result.is_err() {
            if let Some(reservation) = reserved {
                let _=self.window("complete",json!({"reservation":reservation,"success":false,"commit":false,"max_failures":3}));
            }
            let _ = send(sock, &json!({"error":"REQUEST_REJECTED"}));
        }
        result
    }
    fn serve(&self, stream: TcpStream) {
        let Ok(stream) = crate::platform::blocking_tcp(stream) else {
            self.active.fetch_sub(1, Ordering::AcqRel);
            return;
        };
        let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
        let _ = stream.set_write_timeout(Some(Duration::from_secs(5)));
        let result = (|| -> Result<()> {
            if !self.direct {
                lan(&stream
                    .peer_addr()
                    .map_err(|_| "DEVICE_UNREACHABLE")?
                    .ip()
                    .to_string())?;
            }
            let (cert, key) = identity::credentials(&self.root)?;
            let cfg = ServerConfig::builder_with_protocol_versions(&[&rustls::version::TLS13])
                .with_client_cert_verifier(Arc::new(Verify {
                    root: self.root.clone(),
                    pairing: false,
                    expected: None,
                    fingerprint: None,
                }))
                .with_single_cert(cert, key)
                .map_err(|_| "LOCAL_KEY_MISMATCH")?;
            let mut sock = StreamOwned::new(
                ServerConnection::new(Arc::new(cfg)).map_err(|_| "TLS_HANDSHAKE_FAILED")?,
                stream,
            );
            while sock.conn.is_handshaking() {
                sock.conn
                    .complete_io(&mut sock.sock)
                    .map_err(|_| "TLS_HANDSHAKE_FAILED")?;
            }
            self.handle(&mut sock)
        })();
        let _ = result;
        self.active.fetch_sub(1, Ordering::AcqRel);
    }
    fn close(&self) {
        let _ = self.window("close", json!({}));
        self.stopped.store(true, Ordering::Release);
        if let Ok(mut t) = self.thread.lock()
            && let Some(t) = t.take()
        {
            let _ = t.join();
        }
        if let Ok(mut clients) = self.clients.lock() {
            for child in clients.drain(..) {
                let _ = child.join();
            }
        }
        if let Ok(mut lease) = self.lease.lock() {
            lease.take();
        }
    }
}
enum Exchange {
    Client(Box<OpaqueClient>),
    Server(Box<OpaqueServer>),
}
#[derive(Default)]
pub struct Runtime {
    next: AtomicU64,
    locks: Mutex<BTreeMap<u64, identity::OwnedLock>>,
    servers: Mutex<BTreeMap<u64, Arc<Server>>>,
    connections: Mutex<BTreeMap<u64, Arc<Mutex<ClientStream>>>>,
    exchanges: Mutex<BTreeMap<u64, Exchange>>,
}
impl Runtime {
    fn id(&self) -> u64 {
        self.next.fetch_add(1, Ordering::Relaxed) + 1
    }
    pub fn invoke<F: Fn(&str, Value) -> Result<Value> + Send + Sync + 'static>(
        &self,
        op: &str,
        a: &Value,
        discovery: Arc<discovery_state::Runtime>,
        callback: F,
    ) -> Result<Value> {
        let root = Path::new(text(&a["root"]));
        match op {
            "discovery.identity.open" => {
                let raw = identity::open(root, text(&a["host"]), a["display_name"].as_str())?;
                Ok(
                    json!({"public":identity::public_record(&raw)?,"certificate":raw["certificate"]}),
                )
            }
            "discovery.identity.generate" => {
                identity::generate(text(&a["host"]), a["display_name"].as_str())
            }
            "discovery.identity.validate" => identity::validate(&a["value"], text(&a["host"])),
            "discovery.identity.credentials" => local(root),
            "discovery.store.open" => {
                identity::private_store(root)?;
                Ok(Value::Null)
            }
            "discovery.store.check" => {
                node_tls::check(Path::new(text(&a["path"])), a["directory"] == true)?;
                Ok(Value::Null)
            }
            "discovery.store.read" => {
                Ok(identity::read(root, text(&a["name"]))?.unwrap_or(Value::Null))
            }
            "discovery.store.write" => {
                identity::write(root, text(&a["name"]), &a["value"])?;
                Ok(Value::Null)
            }
            "discovery.store.lock" => {
                let lock = identity::lock(root, text(&a["name"]), a["nonblocking"] == true)?;
                let id = self.id();
                self.locks
                    .lock()
                    .map_err(|_| "STATE_LOCK_UNAVAILABLE")?
                    .insert(id, lock);
                Ok(json!(id))
            }
            "discovery.store.unlock" => {
                self.locks
                    .lock()
                    .map_err(|_| "STATE_LOCK_UNAVAILABLE")?
                    .remove(&a["lock"].as_u64().ok_or("STATE_LOCK_UNAVAILABLE")?);
                Ok(Value::Null)
            }
            "discovery.opaque.status" => {
                Ok(json!({"BACKEND_ID":PROTOCOL,"OPAQUE_KE_VERSION":"4.0.1"}))
            }
            "discovery.opaque.binding" => {
                let (c, a, b) = binding(
                    &a["client"],
                    &a["server"],
                    text(&a["window_id"]),
                    text(&a["session_id"]),
                    text(&a["client_nonce"]),
                )?;
                Ok(json!([
                    STANDARD.encode(c),
                    STANDARD.encode(a),
                    STANDARD.encode(b)
                ]))
            }
            "discovery.opaque.client" | "discovery.opaque.server" => {
                let b = &a["binding"];
                let (context, client, server) = (decoded(&b[0])?, decoded(&b[1])?, decoded(&b[2])?);
                let code = decoded(&a["code"])?;
                let e = if op.ends_with("client") {
                    Exchange::Client(Box::new(
                        OpaqueClient::new(&code, context, client, server).map_err(str::to_owned)?,
                    ))
                } else {
                    Exchange::Server(Box::new(
                        OpaqueServer::new(&code, context, client, server, &decoded(&a["message"])?)
                            .map_err(str::to_owned)?,
                    ))
                };
                let id = self.id();
                self.exchanges
                    .lock()
                    .map_err(|_| "PAIRING_FAILED")?
                    .insert(id, e);
                Ok(json!(id))
            }
            "discovery.opaque.delete" => {
                self.exchanges
                    .lock()
                    .map_err(|_| "PAIRING_FAILED")?
                    .remove(&a["exchange"].as_u64().ok_or("PAIRING_FAILED")?);
                Ok(Value::Null)
            }
            "discovery.opaque.start" => {
                let mut states = self.exchanges.lock().map_err(|_| "PAIRING_FAILED")?;
                let e = states
                    .get_mut(&a["exchange"].as_u64().ok_or("PAIRING_FAILED")?)
                    .ok_or("PAIRING_FAILED")?;
                Ok(json!(
                    STANDARD.encode(
                        match e {
                            Exchange::Client(c) => c.start_message(),
                            Exchange::Server(s) => s.start_message(),
                        }
                        .map_err(str::to_owned)?
                    )
                ))
            }
            "discovery.opaque.finish" => {
                let mut e = self
                    .exchanges
                    .lock()
                    .map_err(|_| "PAIRING_FAILED")?
                    .remove(&a["exchange"].as_u64().ok_or("PAIRING_FAILED")?)
                    .ok_or("PAIRING_FAILED")?;
                let b = decoded(&a["message"])?;
                let result = match &mut e {
                    Exchange::Client(c) => c.finish_message(&b),
                    Exchange::Server(s) => s.finish(&b).map(|_| Vec::new()),
                }
                .map_err(str::to_owned)?;
                Ok(json!(STANDARD.encode(result)))
            }
            "discovery.enrollment.client.check" => {
                check_candidate(&discovery, root, &a["candidate"], a["pairing"] == true)?;
                Ok(Value::Null)
            }
            "discovery.enrollment.client.pair" => pair(
                &discovery,
                root,
                &a["candidate"],
                text(&a["code"]),
                a["allow_direct"] == true,
            ),
            "discovery.enrollment.client.reconnect" => {
                session(&discovery, root, &a["candidate"], a["allow_direct"] == true)
            }
            "discovery.enrollment.client.observer" => {
                let c = &a["candidate"];
                let mut sock = connect(&discovery, root, c, false, a["allow_direct"] == true)?;
                authenticate(&discovery, root, text(&c["node_id"]), &peer_client(&sock)?)?;
                send(
                    &mut sock,
                    &json!({"op":"observer_connection","node_id":local(root)?["node_id"]}),
                )?;
                let reply = receive(&mut sock)?;
                if reply["node_id"] != c["node_id"] || reply["read_only"] != true {
                    return Err("INVALID_OBSERVER_CONNECTION".into());
                }
                Ok(reply)
            }
            "discovery.enrollment.client.connect" => {
                let conn = connect(
                    &discovery,
                    root,
                    &a["candidate"],
                    a["pairing"] == true,
                    a["allow_direct"] == true,
                )?;
                let id = self.id();
                self.connections
                    .lock()
                    .map_err(|_| "CONNECTION_CLOSED")?
                    .insert(id, Arc::new(Mutex::new(conn)));
                Ok(json!(id))
            }
            "discovery.enrollment.client.close" => {
                self.connections
                    .lock()
                    .map_err(|_| "CONNECTION_CLOSED")?
                    .remove(&a["connection"].as_u64().ok_or("CONNECTION_CLOSED")?);
                Ok(Value::Null)
            }
            "discovery.enrollment.client.send"
            | "discovery.enrollment.client.receive"
            | "discovery.enrollment.client.certificate" => {
                let conn = self
                    .connections
                    .lock()
                    .map_err(|_| "CONNECTION_CLOSED")?
                    .get(&a["connection"].as_u64().ok_or("CONNECTION_CLOSED")?)
                    .cloned()
                    .ok_or("CONNECTION_CLOSED")?;
                let mut conn = conn.lock().map_err(|_| "CONNECTION_CLOSED")?;
                if op.ends_with("send") {
                    send(&mut *conn, &a["message"]).map(|_| Value::Null)
                } else if op.ends_with("receive") {
                    receive(&mut *conn)
                } else {
                    Ok(json!(cert_pem(&peer_client(&conn)?)))
                }
            }
            "discovery.enrollment.server.close" => {
                let server = self
                    .servers
                    .lock()
                    .map_err(|_| "DEVICE_UNAVAILABLE")?
                    .remove(&a["server"].as_u64().ok_or("DEVICE_UNAVAILABLE")?);
                if let Some(server) = server {
                    server.close();
                }
                Ok(Value::Null)
            }
            "discovery.enrollment.server.start" => {
                let address = a["address"].as_str().unwrap_or("0.0.0.0");
                if address != "0.0.0.0" {
                    lan(address)?;
                }
                let port = a["port"]
                    .as_u64()
                    .filter(|v| *v <= 65535)
                    .ok_or("INVALID_PORT")? as u16;
                let lease = identity::lock(root, "server.lock", true)?;
                let local = local(root)?;
                let listener =
                    TcpListener::bind((address, port)).map_err(|_| "DEVICE_BIND_FAILED")?;
                listener
                    .set_nonblocking(true)
                    .map_err(|_| "DEVICE_BIND_FAILED")?;
                let port = listener
                    .local_addr()
                    .map_err(|_| "DEVICE_BIND_FAILED")?
                    .port();
                let window = if let Some(w) = a["window"].as_u64() {
                    w
                } else {
                    state(&discovery, "window.create", json!({}))?
                        .as_u64()
                        .ok_or("PAIRING_UNAVAILABLE")?
                };
                let server = Arc::new(Server {
                    root: root.into(),
                    identity: local,
                    window,
                    state: discovery,
                    callback: Arc::new(callback),
                    handler: a["handler_token"].clone(),
                    custom_clock: a["custom_clock"] == true,
                    observer: a["observer_enabled"] == true,
                    direct: a["allow_direct"] == true,
                    stopped: AtomicBool::new(false),
                    active: AtomicUsize::new(0),
                    thread: Mutex::new(None),
                    clients: Mutex::new(Vec::new()),
                    lease: Mutex::new(Some(lease)),
                });
                let owned = server.clone();
                let handle = thread::spawn(move || {
                    while !owned.stopped.load(Ordering::Acquire) {
                        match listener.accept() {
                            Ok((stream, _)) => {
                                if owned
                                    .active
                                    .fetch_update(Ordering::AcqRel, Ordering::Acquire, |n| {
                                        (n < 16).then_some(n + 1)
                                    })
                                    .is_err()
                                {
                                    continue;
                                }
                                let server = owned.clone();
                                let child = thread::spawn(move || server.serve(stream));
                                if let Ok(mut clients) = owned.clients.lock() {
                                    clients.retain(|c| !c.is_finished());
                                    clients.push(child);
                                }
                            }
                            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                                let timeout = rustix::event::Timespec {
                                    tv_sec: 0,
                                    tv_nsec: 100_000_000,
                                };
                                let _ = rustix::event::poll(
                                    &mut [rustix::event::PollFd::new(
                                        &listener,
                                        rustix::event::PollFlags::IN,
                                    )],
                                    Some(&timeout),
                                );
                            }
                            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => (),
                            Err(_) => break,
                        }
                    }
                });
                *server.thread.lock().map_err(|_| "DEVICE_UNAVAILABLE")? = Some(handle);
                let id = self.id();
                self.servers
                    .lock()
                    .map_err(|_| "DEVICE_UNAVAILABLE")?
                    .insert(id, server);
                Ok(json!({"server":id,"port":port,"window":window}))
            }
            _ => Err("DISCOVERY_OPERATION_UNKNOWN".into()),
        }
    }
}
