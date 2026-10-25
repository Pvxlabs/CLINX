//! Existing device-state storage and Ed25519 identity bootstrap. No key rotation,
//! enrollment permission or task authority is created by this module.
use crate::node_tls::{self, Result};
use base64::{Engine, engine::general_purpose::STANDARD};
use rcgen::{
    BasicConstraints, CertificateParams, DistinguishedName, DnType, IsCa, KeyPair, PKCS_ED25519,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs::{DirBuilder, File},
    io::{Read, Write},
    os::unix::fs::{DirBuilderExt, MetadataExt},
    path::{Component, Path},
};

pub struct OwnedLock(File);
impl Drop for OwnedLock {
    fn drop(&mut self) {
        let _ = self.0.unlock();
    }
}
pub fn private_store(root: &Path) -> Result<()> {
    if !root.is_absolute()
        || root
            .symlink_metadata()
            .is_ok_and(|m| m.file_type().is_symlink())
    {
        return Err("UNSAFE_STATE_DIRECTORY".into());
    }
    DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(root)
        .map_err(|_| "STATE_UNAVAILABLE")?;
    node_tls::check(root, true)
}
fn filename(name: &str) -> Result<()> {
    if name.is_empty()
        || Path::new(name)
            .components()
            .any(|c| !matches!(c, Component::Normal(_)))
    {
        return Err("INVALID_STATE_NAME".into());
    }
    Ok(())
}
pub fn lock(root: &Path, name: &str, nonblocking: bool) -> Result<OwnedLock> {
    filename(name)?;
    node_tls::check(root, true)?;
    let fd = rustix::fs::open(
        root.join(name),
        rustix::fs::OFlags::CREATE
            | rustix::fs::OFlags::RDWR
            | rustix::fs::OFlags::NOFOLLOW
            | rustix::fs::OFlags::CLOEXEC,
        rustix::fs::Mode::from_raw_mode(0o600),
    )
    .map_err(|_| "STATE_LOCK_UNAVAILABLE")?;
    let f = File::from(fd);
    let m = f.metadata().map_err(|_| "STATE_UNAVAILABLE")?;
    if !m.is_file() || m.uid() != rustix::process::getuid().as_raw() || m.mode() & 0o077 != 0 {
        return Err("UNSAFE_STATE_PERMISSIONS".into());
    }
    let op = if nonblocking {
        rustix::fs::FlockOperation::NonBlockingLockExclusive
    } else {
        rustix::fs::FlockOperation::LockExclusive
    };
    rustix::fs::flock(&f, op).map_err(|e| {
        if e == rustix::io::Errno::WOULDBLOCK {
            "DEVICE_ALREADY_SERVING"
        } else {
            "STATE_LOCK_UNAVAILABLE"
        }
    })?;
    Ok(OwnedLock(f))
}
pub fn read(root: &Path, name: &str) -> Result<Option<Value>> {
    filename(name)?;
    node_tls::check(root, true)?;
    node_tls::read_unlocked(root, name).map_err(|e| {
        if e == "UNSUPPORTED_STATE_SCHEMA" {
            "INVALID_STATE".into()
        } else {
            e
        }
    })
}
pub fn write(root: &Path, name: &str, value: &Value) -> Result<()> {
    filename(name)?;
    node_tls::check(root, true)?;
    let target = root.join(name);
    if target.symlink_metadata().is_ok() {
        node_tls::check(&target, false)?;
    }
    let mut tmp = tempfile::NamedTempFile::new_in(root).map_err(|_| "STATE_WRITE_UNAVAILABLE")?;
    tmp.write_all(&serde_json::to_vec(value).map_err(|_| "INVALID_STATE")?)
        .and_then(|_| tmp.as_file().sync_all())
        .map_err(|_| "STATE_WRITE_UNAVAILABLE")?;
    tmp.persist(&target)
        .map_err(|_| "STATE_WRITE_UNAVAILABLE")?;
    File::open(root)
        .and_then(|f| f.sync_all())
        .map_err(|_| "STATE_WRITE_UNAVAILABLE".into())
}
pub fn public_record(raw: &Value) -> Result<Value> {
    node_tls::validate_identity(raw)?;
    let key = STANDARD
        .decode(raw["public_key"].as_str().ok_or("INVALID_PUBLIC_KEY")?)
        .map_err(|_| "INVALID_ENCODING")?;
    if key.len() != 32 {
        return Err("INVALID_PUBLIC_KEY".into());
    }
    let fingerprint = format!("{:x}", Sha256::digest(key));
    if raw["fingerprint"] != fingerprint {
        return Err("IDENTITY_OR_PROTOCOL_MISMATCH".into());
    }
    Ok(
        json!({"node_id":raw["node_id"],"display_name":raw["display_name"],"protocol_version":1,"public_key":raw["public_key"],"fingerprint":fingerprint}),
    )
}
pub fn generate(host: &str, display: Option<&str>) -> Result<Value> {
    let display = display.filter(|s| !s.is_empty()).unwrap_or(host);
    node_tls::validate_identity(
        &json!({"node_id":host,"display_name":display,"protocol_version":1}),
    )?;
    let key = KeyPair::generate_for(&PKCS_ED25519).map_err(|_| "IDENTITY_GENERATION_FAILED")?;
    let now = time::OffsetDateTime::now_utc();
    let mut params = CertificateParams::default();
    params.not_before = now - time::Duration::minutes(5);
    params.not_after = now + time::Duration::days(3650);
    params.distinguished_name = DistinguishedName::new();
    params.distinguished_name.push(DnType::CommonName, host);
    params.is_ca = IsCa::Ca(BasicConstraints::Constrained(0));
    let mut serial = [0u8; 20];
    File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut serial))
        .map_err(|_| "IDENTITY_GENERATION_FAILED")?;
    serial[0] &= 0x7f;
    if serial.iter().all(|b| *b == 0) {
        serial[19] = 1;
    }
    params.serial_number = Some(serial.to_vec().into());
    let cert = params
        .self_signed(&key)
        .map_err(|_| "IDENTITY_GENERATION_FAILED")?;
    // Keep the persisted format accepted by existing Python/OpenSSL clients.
    // ring emits PKCS#8 v2; standard PKCS#8 encoding without its optional
    // public-key field is the original v1 document, with the same secret key.
    let der = key.serialize_der();
    let mut info = pkcs8::PrivateKeyInfo::try_from(der.as_slice())
        .map_err(|_| "IDENTITY_GENERATION_FAILED")?;
    info.public_key = None;
    let private = pkcs8::SecretDocument::try_from(info)
        .map_err(|_| "IDENTITY_GENERATION_FAILED")?
        .to_pem("PRIVATE KEY", pkcs8::LineEnding::LF)
        .map_err(|_| "IDENTITY_GENERATION_FAILED")?;
    let public = key.public_key_raw();
    Ok(
        json!({"schema_version":1,"node_id":host,"display_name":display,"protocol_version":1,"public_key":STANDARD.encode(public),"fingerprint":format!("{:x}",Sha256::digest(public)),"created_at":node_tls::now(),"private_key":private.as_str(),"certificate":cert.pem()}),
    )
}
pub fn validate(raw: &Value, host: &str) -> Result<Value> {
    let public = public_record(raw)?;
    if public["node_id"] != host {
        return Err("CONFIGURED_HOST_IDENTITY_CHANGED".into());
    }
    let key = KeyPair::from_pkcs8_pem_and_sign_algo(
        raw["private_key"].as_str().ok_or("INVALID_PRIVATE_KEY")?,
        &PKCS_ED25519,
    )
    .map_err(|_| "INVALID_PRIVATE_KEY")?;
    let actual = STANDARD.encode(key.public_key_raw());
    let cert = node_tls::cert(raw["certificate"].as_str().ok_or("INVALID_CERTIFICATE")?)?;
    if actual != public["public_key"] || node_tls::public(&cert)?.0 != actual {
        return Err("LOCAL_KEY_MISMATCH".into());
    }
    Ok(public)
}
pub fn open(root: &Path, host: &str, display: Option<&str>) -> Result<Value> {
    private_store(root)?;
    // Callers supply the canonical routing host. No address-based identity.
    node_tls::validate_identity(&json!({"node_id":host,"display_name":host,"protocol_version":1}))?;
    let _lock = lock(root, "store.lock", false)?;
    let raw = match read(root, "identity.json")? {
        Some(raw) => raw,
        None => {
            let raw = generate(host, display)?;
            write(root, "identity.json", &raw)?;
            raw
        }
    };
    validate(&raw, host)?;
    Ok(raw)
}
pub fn credentials(
    root: &Path,
) -> Result<(
    Vec<rustls::pki_types::CertificateDer<'static>>,
    rustls::pki_types::PrivateKeyDer<'static>,
)> {
    let raw = node_tls::read_state(root, "identity.json")?.ok_or("IDENTITY_BOOTSTRAP_REQUIRED")?;
    validate(&raw, raw["node_id"].as_str().ok_or("INVALID_NODE_ID")?)?;
    let cert = node_tls::cert(raw["certificate"].as_str().ok_or("INVALID_CERTIFICATE")?)?;
    let key = rustls_pemfile::private_key(
        &mut raw["private_key"]
            .as_str()
            .ok_or("INVALID_PRIVATE_KEY")?
            .as_bytes(),
    )
    .map_err(|_| "INVALID_PRIVATE_KEY")?
    .ok_or("INVALID_PRIVATE_KEY")?;
    Ok((vec![cert], key))
}
