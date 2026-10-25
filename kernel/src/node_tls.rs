//! Existing paired identity and TLS 1.3. No enrollment, key rotation or new trust.
use base64::{Engine, engine::general_purpose::STANDARD};
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::server::danger::{ClientCertVerified, ClientCertVerifier};
use rustls::{ClientConfig, ClientConnection, ServerConfig, ServerConnection, StreamOwned};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs::File,
    io::{BufRead, BufReader, Read, Write},
    net::{TcpStream, ToSocketAddrs},
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use x509_parser::prelude::*;
pub type Result<T> = std::result::Result<T, String>;
pub const FRAME: usize = 256 * 1024;
pub fn now() -> String {
    chrono::DateTime::<chrono::Utc>::from(std::time::SystemTime::now())
        .format("%Y-%m-%dT%H:%M:%S%.6f+00:00")
        .to_string()
}
pub(crate) fn check(path: &Path, directory: bool) -> Result<()> {
    let m = path.symlink_metadata().map_err(|_| "STATE_UNAVAILABLE")?;
    if m.uid() != rustix::process::getuid().as_raw()
        || m.mode() & 0o077 != 0
        || (if directory { !m.is_dir() } else { !m.is_file() })
    {
        return Err("UNSAFE_STATE_PERMISSIONS".into());
    }
    Ok(())
}
pub(crate) fn lock(root: &Path) -> Result<File> {
    if !root.is_absolute() {
        return Err("UNSAFE_STATE_DIRECTORY".into());
    }
    check(root, true)?;
    let fd = rustix::fs::open(
        root.join("store.lock"),
        rustix::fs::OFlags::RDWR
            | rustix::fs::OFlags::CREATE
            | rustix::fs::OFlags::NOFOLLOW
            | rustix::fs::OFlags::CLOEXEC,
        rustix::fs::Mode::from_raw_mode(0o600),
    )
    .map_err(|_| "STATE_LOCK_UNAVAILABLE")?;
    let file = File::from(fd);
    let m = file.metadata().map_err(|_| "STATE_UNAVAILABLE")?;
    if !m.is_file() || m.uid() != rustix::process::getuid().as_raw() || m.mode() & 0o077 != 0 {
        return Err("UNSAFE_STATE_PERMISSIONS".into());
    }
    rustix::fs::flock(&file, rustix::fs::FlockOperation::LockExclusive)
        .map_err(|_| "STATE_LOCK_UNAVAILABLE")?;
    Ok(file)
}
pub(crate) fn read_unlocked(root: &Path, name: &str) -> Result<Option<Value>> {
    let path = root.join(name);
    if !path.try_exists().map_err(|_| "STATE_UNAVAILABLE")? && path.symlink_metadata().is_err() {
        return Ok(None);
    }
    check(&path, false)?;
    let fd = rustix::fs::open(
        &path,
        rustix::fs::OFlags::RDONLY | rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::CLOEXEC,
        rustix::fs::Mode::empty(),
    )
    .map_err(|_| "STATE_UNAVAILABLE")?;
    let file = File::from(fd);
    let m = file.metadata().map_err(|_| "STATE_UNAVAILABLE")?;
    if !m.is_file() || m.uid() != rustix::process::getuid().as_raw() || m.mode() & 0o077 != 0 {
        return Err("UNSAFE_STATE_PERMISSIONS".into());
    }
    let mut bytes = Vec::new();
    file.take(2_000_001)
        .read_to_end(&mut bytes)
        .map_err(|_| "STATE_UNAVAILABLE")?;
    if bytes.len() > 2_000_000 {
        return Err("STATE_TOO_LARGE".into());
    }
    let v: Value = serde_json::from_slice(&bytes).map_err(|_| "INVALID_STATE")?;
    if !v.is_object() || v["schema_version"] != 1 {
        return Err("UNSUPPORTED_STATE_SCHEMA".into());
    }
    Ok(Some(v))
}
pub fn read_state(root: &Path, name: &str) -> Result<Option<Value>> {
    if !matches!(
        name,
        "identity.json" | "peers.json" | "sharing.json" | "node-config.json"
    ) {
        return Err("INVALID_STATE_NAME".into());
    }
    let _lock = lock(root)?;
    read_unlocked(root, name)
}
pub(crate) fn cert(pem: &str) -> Result<CertificateDer<'static>> {
    let all = rustls_pemfile::certs(&mut pem.as_bytes())
        .collect::<std::result::Result<Vec<_>, _>>()
        .map_err(|_| "INVALID_CERTIFICATE")?;
    if all.len() != 1 {
        return Err("INVALID_CERTIFICATE".into());
    }
    Ok(all[0].clone())
}
pub(crate) fn public(der: &[u8]) -> Result<(String, String)> {
    let (rest, c) = X509Certificate::from_der(der).map_err(|_| "INVALID_CERTIFICATE")?;
    if !rest.is_empty()
        || c.public_key().algorithm.algorithm.to_id_string() != "1.3.101.112"
        || c.public_key().subject_public_key.data.len() != 32
    {
        return Err("INVALID_CERTIFICATE_KEY".into());
    }
    if !c.validity().is_valid() {
        return Err("CERTIFICATE_EXPIRED_OR_NOT_YET_VALID".into());
    }
    c.verify_signature(Some(c.public_key()))
        .map_err(|_| "INVALID_CERTIFICATE")?;
    let key = &c.public_key().subject_public_key.data;
    Ok((STANDARD.encode(key), format!("{:x}", Sha256::digest(key))))
}
pub(crate) fn validate_identity(p: &Value) -> Result<()> {
    let id = p["node_id"].as_str().ok_or("INVALID_NODE_ID")?;
    if id.is_empty()
        || id.len() > 63
        || !id.bytes().enumerate().all(|(i, c)| {
            c.is_ascii_lowercase() || c.is_ascii_digit() || (i > 0 && b"_.-".contains(&c))
        })
    {
        return Err("INVALID_NODE_ID".into());
    }
    if id.starts_with("workstation-") || matches!(id, "unknown" | "unset" | "none" | "null") {
        return Err("NON_CANONICAL_NODE_ID".into());
    }
    if id.replace('.', "").bytes().all(|c| c.is_ascii_digit()) {
        return Err("ADDRESS_IS_NOT_IDENTITY".into());
    }
    let name = p["display_name"].as_str().ok_or("INVALID_DISPLAY_NAME")?;
    if name.is_empty() || name.len() > 120 || name.chars().any(|c| c < ' ' || c == '\u{7f}') {
        return Err("INVALID_DISPLAY_NAME".into());
    }
    if p["protocol_version"] != 1 {
        return Err("IDENTITY_OR_PROTOCOL_MISMATCH".into());
    }
    Ok(())
}
fn state_read(root: &Path, name: &str, readonly: bool) -> Result<Option<Value>> {
    if readonly {
        check(root, true)?;
        read_unlocked(root, name)
    } else {
        read_state(root, name)
    }
}
fn peers_mode(root: &Path, readonly: bool) -> Result<Value> {
    let v =
        state_read(root, "peers.json", readonly)?.unwrap_or(json!({"schema_version":1,"peers":{}}));
    let rows = v["peers"].as_object().ok_or("INVALID_PEER_STORE")?;
    for (id, p) in rows {
        validate_identity(p)?;
        let (key, fp) = public(&cert(
            p["certificate"].as_str().ok_or("INVALID_CERTIFICATE")?,
        )?)?;
        if p["node_id"] != *id
            || p["protocol_version"] != 1
            || p["public_key"] != key
            || p["fingerprint"] != fp
            || !matches!(p["trust_state"].as_str(), Some("TRUSTED" | "REVOKED"))
        {
            return Err("INVALID_PEER_STORE".into());
        }
        if !matches!(
            p["security_status"].as_str(),
            Some("OPAQUE_V1" | "DEV_ONLY")
        ) || (p["security_status"] == "OPAQUE_V1"
            && p["pairing_protocol"] != "OPAQUE-3DH-RISTRETTO255-SHA512-ARGON2I-v1")
        {
            return Err("INVALID_PEER_SECURITY_STATUS".into());
        }
    }
    Ok(v)
}
pub fn peer(root: &Path, id: &str) -> Result<Value> {
    peer_mode(root, id, false)
}
pub fn peer_readonly(root: &Path, id: &str) -> Result<Value> {
    peer_mode(root, id, true)
}
fn peer_mode(root: &Path, id: &str, readonly: bool) -> Result<Value> {
    let all = peers_mode(root, readonly)?;
    let p = &all["peers"][id];
    if p["trust_state"] != "TRUSTED" || p["security_status"] != "OPAQUE_V1" {
        return Err("PAIRING_REQUIRED".into());
    }
    Ok(p.clone())
}
pub fn authenticate(root: &Path, der: &[u8], expected: Option<&str>, touch: bool) -> Result<Value> {
    authenticate_mode(root, der, expected, touch, false)
}
fn authenticate_mode(
    root: &Path,
    der: &[u8],
    expected: Option<&str>,
    touch: bool,
    readonly: bool,
) -> Result<Value> {
    let (key, _) = public(der)?;
    let all = peers_mode(root, readonly)?;
    let found: Vec<_> = all["peers"]
        .as_object()
        .unwrap()
        .values()
        .filter(|p| p["public_key"] == key)
        .collect();
    if found.len() != 1 {
        return Err("IDENTITY_MISMATCH".into());
    }
    let p = found[0];
    let id = p["node_id"].as_str().ok_or("IDENTITY_MISMATCH")?;
    if expected.is_some_and(|e| e != id) {
        return Err("IDENTITY_MISMATCH".into());
    }
    if p["trust_state"] != "TRUSTED" || p["security_status"] != "OPAQUE_V1" {
        return Err("PAIRING_REQUIRED".into());
    }
    if !touch {
        return Ok(p.clone());
    }
    let _lock = lock(root)?;
    let mut all = read_unlocked(root, "peers.json")?.ok_or("PAIRING_REQUIRED")?;
    let p = &mut all["peers"][id];
    if p["trust_state"] != "TRUSTED"
        || p["public_key"] != key
        || p["security_status"] != "OPAQUE_V1"
    {
        return Err("PAIRING_REQUIRED".into());
    }
    p["last_authenticated_at"] = json!(now());
    p["updated_at"] = p["last_authenticated_at"].clone();
    let result = p.clone();
    let mut tmp = tempfile::NamedTempFile::new_in(root).map_err(|_| "STATE_WRITE_UNAVAILABLE")?;
    tmp.write_all(&serde_json::to_vec(&all).map_err(|_| "INVALID_STATE")?)
        .and_then(|_| tmp.as_file().sync_all())
        .map_err(|_| "STATE_WRITE_UNAVAILABLE")?;
    tmp.persist(root.join("peers.json"))
        .map_err(|_| "STATE_WRITE_UNAVAILABLE")?;
    File::open(root)
        .and_then(|f| f.sync_all())
        .map_err(|_| "STATE_WRITE_UNAVAILABLE")?;
    Ok(result)
}
fn credentials(
    root: &Path,
    readonly: bool,
) -> Result<(
    Vec<CertificateDer<'static>>,
    rustls::pki_types::PrivateKeyDer<'static>,
)> {
    let v = state_read(root, "identity.json", readonly)?.ok_or("IDENTITY_BOOTSTRAP_REQUIRED")?;
    validate_identity(&v)?;
    let c = cert(v["certificate"].as_str().ok_or("INVALID_CERTIFICATE")?)?;
    let (key, fp) = public(&c)?;
    if v["public_key"] != key || v["fingerprint"] != fp {
        return Err("LOCAL_KEY_MISMATCH".into());
    }
    let pem = v["private_key"].as_str().ok_or("INVALID_PRIVATE_KEY")?;
    let key = rustls_pemfile::private_key(&mut pem.as_bytes())
        .map_err(|_| "INVALID_PRIVATE_KEY")?
        .ok_or("INVALID_PRIVATE_KEY")?;
    Ok((vec![c], key))
}
#[derive(Debug)]
struct Paired {
    root: PathBuf,
    readonly: bool,
    expected: Option<String>,
    subjects: Vec<rustls::DistinguishedName>,
}
fn tls_error(_: String) -> rustls::Error {
    rustls::Error::General("PAIRING_REQUIRED".into())
}
impl ServerCertVerifier for Paired {
    fn verify_server_cert(
        &self,
        end: &CertificateDer<'_>,
        _: &[CertificateDer<'_>],
        _: &ServerName<'_>,
        _: &[u8],
        _: UnixTime,
    ) -> std::result::Result<ServerCertVerified, rustls::Error> {
        authenticate_mode(
            &self.root,
            end,
            self.expected.as_deref(),
            false,
            self.readonly,
        )
        .map_err(tls_error)?;
        Ok(ServerCertVerified::assertion())
    }
    fn verify_tls12_signature(
        &self,
        _: &[u8],
        _: &CertificateDer<'_>,
        _: &rustls::DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        Err(rustls::Error::General("TLS13_REQUIRED".into()))
    }
    fn verify_tls13_signature(
        &self,
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
    fn supported_verify_schemes(&self) -> Vec<rustls::SignatureScheme> {
        rustls::crypto::ring::default_provider()
            .signature_verification_algorithms
            .supported_schemes()
    }
}
impl ClientCertVerifier for Paired {
    fn root_hint_subjects(&self) -> &[rustls::DistinguishedName] {
        &self.subjects
    }
    fn verify_client_cert(
        &self,
        end: &CertificateDer<'_>,
        _: &[CertificateDer<'_>],
        _: UnixTime,
    ) -> std::result::Result<ClientCertVerified, rustls::Error> {
        authenticate_mode(&self.root, end, None, false, self.readonly).map_err(tls_error)?;
        Ok(ClientCertVerified::assertion())
    }
    fn verify_tls12_signature(
        &self,
        m: &[u8],
        c: &CertificateDer<'_>,
        d: &rustls::DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        ServerCertVerifier::verify_tls12_signature(self, m, c, d)
    }
    fn verify_tls13_signature(
        &self,
        m: &[u8],
        c: &CertificateDer<'_>,
        d: &rustls::DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        ServerCertVerifier::verify_tls13_signature(self, m, c, d)
    }
    fn supported_verify_schemes(&self) -> Vec<rustls::SignatureScheme> {
        ServerCertVerifier::supported_verify_schemes(self)
    }
}
pub fn decode_certificate(h: &str) -> Result<Vec<u8>> {
    if h.len() > 65536 || !h.len().is_multiple_of(2) || !h.is_ascii() {
        return Err("INVALID_CERTIFICATE".into());
    }
    (0..h.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&h[i..i + 2], 16).map_err(|_| "INVALID_CERTIFICATE".to_owned()))
        .collect()
}
pub fn server(root: &Path) -> Result<Arc<ServerConfig>> {
    let (cert, key) = credentials(root, false)?;
    let config = ServerConfig::builder_with_protocol_versions(&[&rustls::version::TLS13])
        .with_client_cert_verifier(Arc::new(Paired {
            root: root.to_owned(),
            expected: None,
            readonly: false,
            subjects: vec![],
        }))
        .with_single_cert(cert, key)
        .map_err(|_| "LOCAL_KEY_MISMATCH")?;
    Ok(Arc::new(config))
}
pub fn endpoint(value: &str) -> Result<(String, u16)> {
    let v = value
        .strip_prefix("tls://")
        .ok_or("INVALID_NODE_ENDPOINT")?
        .strip_suffix('/')
        .unwrap_or(value.strip_prefix("tls://").unwrap());
    if v.chars().any(|c| c.is_whitespace() || "@?#/".contains(c)) {
        return Err("INVALID_NODE_ENDPOINT".into());
    }
    let (h, p) = v.rsplit_once(':').ok_or("INVALID_NODE_ENDPOINT")?;
    let h = if h.starts_with('[') && h.ends_with(']') {
        &h[1..h.len() - 1]
    } else {
        if h.contains(':') {
            return Err("INVALID_NODE_ENDPOINT".into());
        }
        h
    };
    let p: u16 = p.parse().map_err(|_| "INVALID_NODE_ENDPOINT")?;
    if h.is_empty() || p == 0 {
        return Err("INVALID_NODE_ENDPOINT".into());
    }
    Ok((h.to_owned(), p))
}
pub fn call(root: &Path, id: &str, address: &str, timeout: f64, request: &Value) -> Result<Value> {
    call_mode(root, id, address, timeout, request, false)
}
/// Existing paired client using only complete read snapshots. No identity,
/// lock file, trust timestamp or protocol receipt is written by an observer.
pub fn call_readonly(
    root: &Path,
    id: &str,
    address: &str,
    timeout: f64,
    request: &Value,
) -> Result<Value> {
    call_mode(root, id, address, timeout, request, true)
}
fn call_mode(
    root: &Path,
    id: &str,
    address: &str,
    timeout: f64,
    request: &Value,
    readonly: bool,
) -> Result<Value> {
    peer_mode(root, id, readonly)?;
    let (cert, key) = credentials(root, readonly)?;
    let config = ClientConfig::builder_with_protocol_versions(&[&rustls::version::TLS13])
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(Paired {
            root: root.to_owned(),
            expected: Some(id.to_owned()),
            readonly,
            subjects: vec![],
        }))
        .with_client_auth_cert(cert, key)
        .map_err(|_| "LOCAL_KEY_MISMATCH")?;
    let (host, port) = endpoint(address)?;
    let timeout = Duration::try_from_secs_f64(timeout)
        .ok()
        .filter(|v| !v.is_zero())
        .ok_or("TIMEOUT_INVALID")?;
    let addrs = (host.as_str(), port)
        .to_socket_addrs()
        .map_err(|_| "NODE_RESPONSE_UNAVAILABLE")?;
    let mut socket = None;
    for address in addrs {
        if let Ok(s) = TcpStream::connect_timeout(&address, timeout) {
            socket = Some(s);
            break;
        }
    }
    let socket = socket.ok_or("NODE_RESPONSE_UNAVAILABLE")?;
    socket
        .set_read_timeout(Some(timeout))
        .and_then(|_| socket.set_write_timeout(Some(timeout)))
        .map_err(|_| "NODE_RESPONSE_UNAVAILABLE")?;
    let name = ServerName::try_from(host).map_err(|_| "INVALID_NODE_ENDPOINT")?;
    let conn = ClientConnection::new(Arc::new(config), name).map_err(|_| "PAIRING_REQUIRED")?;
    let mut stream = StreamOwned::new(conn, socket);
    let mut wire = serde_json::to_vec(request).map_err(|_| "INVALID_NODE_REQUEST")?;
    wire.push(b'\n');
    if wire.len() > FRAME {
        return Err("REQUEST_TOO_LARGE".into());
    }
    stream
        .write_all(&wire)
        .and_then(|_| stream.flush())
        .map_err(|_| "NODE_RESPONSE_UNAVAILABLE")?;
    let mut b = Vec::new();
    let read = BufReader::new(&mut stream)
        .take((FRAME + 1) as u64)
        .read_until(b'\n', &mut b);
    if read.is_err_and(|e| e.kind() != std::io::ErrorKind::UnexpectedEof) {
        return Err("NODE_RESPONSE_UNAVAILABLE".into());
    }
    if b.len() > FRAME || b.is_empty() {
        return Err("NODE_RESPONSE_UNAVAILABLE".into());
    }
    let v: Value = serde_json::from_slice(&b).map_err(|_| "NODE_RESPONSE_INVALID")?;
    if !v.is_object() {
        return Err("NODE_RESPONSE_INVALID".into());
    }
    Ok(v)
}
pub fn connection(
    stream: TcpStream,
    root: &Path,
) -> Result<(StreamOwned<ServerConnection, TcpStream>, Value, Vec<u8>)> {
    let mut stream = StreamOwned::new(
        ServerConnection::new(server(root)?).map_err(|_| "TLS_UNAVAILABLE")?,
        stream,
    );
    while stream.conn.is_handshaking() {
        stream
            .conn
            .complete_io(&mut stream.sock)
            .map_err(|_| "PAIRING_REQUIRED")?;
    }
    let der = stream
        .conn
        .peer_certificates()
        .and_then(|c| c.first())
        .ok_or("PAIRING_REQUIRED")?
        .to_vec();
    let peer = authenticate(root, &der, None, true)?;
    Ok((stream, peer, der))
}
