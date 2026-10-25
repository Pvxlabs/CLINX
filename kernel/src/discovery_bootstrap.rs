//! Existing explicit Observer opt-in: private file IO and urllib-compatible URL
//! fields. Endpoint/token policy remains in discovery_index::validate_connection.
use crate::{discovery_index, node_tls, node_tls::Result, runtime_config};
use serde_json::{Value, json};
use std::{
    io::Read,
    net::Ipv6Addr,
    path::{Path, PathBuf},
};
use unicode_normalization::UnicodeNormalization;

const INVALID: &str = "INVALID_OBSERVER_CONNECTION";

fn url_fields(endpoint: &str) -> Result<Value> {
    let input = endpoint
        .trim_start_matches(|c: char| c <= ' ')
        .replace(['\r', '\n', '\t'], "");
    let (scheme, rest) = input.split_once(':').ok_or(INVALID)?;
    if scheme.is_empty()
        || !scheme.as_bytes()[0].is_ascii_alphabetic()
        || !scheme
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"+-.".contains(&b))
    {
        return Err(INVALID.into());
    }
    let rest = rest.strip_prefix("//").ok_or(INVALID)?;
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let (netloc, rest) = rest.split_at(end);
    if !netloc.is_ascii() {
        let n = netloc.replace(['@', ':', '#', '?'], "");
        let normalized = n.nfkc().collect::<String>();
        if n != normalized && normalized.contains(['/', '?', '#', '@', ':']) {
            return Err(INVALID.into());
        }
    }
    let (username, password, hostport) = if let Some((user, host)) = netloc.rsplit_once('@') {
        let (user, pass) = user
            .split_once(':')
            .map(|(u, p)| (u, Some(p)))
            .unwrap_or((user, None));
        (json!(user), json!(pass), host)
    } else {
        (Value::Null, Value::Null, netloc)
    };
    let (hostname, port) = if let Some(rest) = hostport.strip_prefix('[') {
        let (host, suffix) = rest.split_once(']').ok_or(INVALID)?;
        if !suffix.is_empty() && !suffix.starts_with(':') {
            return Err(INVALID.into());
        }
        if let Some(future) = host.strip_prefix('v') {
            let (version, name) = future.split_once('.').ok_or(INVALID)?;
            if version.is_empty()
                || !version.bytes().all(|b| b.is_ascii_hexdigit())
                || name.is_empty()
            {
                return Err(INVALID.into());
            }
        } else {
            let address = if let Some((address, zone)) = host.split_once('%') {
                if zone.is_empty() || zone.contains('%') {
                    return Err(INVALID.into());
                }
                address
            } else {
                host
            };
            address.parse::<Ipv6Addr>().map_err(|_| INVALID)?;
        }
        (host, suffix.strip_prefix(':').unwrap_or(""))
    } else {
        if hostport.contains(['[', ']']) {
            return Err(INVALID.into());
        }
        hostport.split_once(':').unwrap_or((hostport, ""))
    };
    let port = if port.is_empty() {
        Value::Null
    } else {
        if !port.bytes().all(|b| b.is_ascii_digit()) {
            return Err(INVALID.into());
        }
        json!(port.parse::<u16>().map_err(|_| INVALID)?)
    };
    // urllib lowercases the hostname, retaining the case of an IPv6 zone ID.
    let hostname = if let Some((host, zone)) = hostname.split_once('%') {
        format!("{}%{zone}", host.to_lowercase())
    } else {
        hostname.to_lowercase()
    };
    let (rest, fragment) = rest.split_once('#').unwrap_or((rest, ""));
    let (path, query) = rest.split_once('?').unwrap_or((rest, ""));
    Ok(
        json!({"scheme":scheme.to_ascii_lowercase(),"hostname":hostname,
        "username":username,"password":password,"port":port,
        "path":path,"query":query,"fragment":fragment}),
    )
}

pub fn validate(raw: &Value) -> Result<Value> {
    let endpoint = raw["endpoint"].as_str().ok_or(INVALID)?;
    discovery_index::invoke(
        "discovery_index.validate_connection",
        &json!({
            "endpoint":endpoint,"credential":raw["credential"],"url":url_fields(endpoint)?
        }),
    )
}

fn absolute(path: &Path) -> Result<PathBuf> {
    let path = runtime_config::expanduser(path)?;
    if path.is_absolute() {
        Ok(path)
    } else {
        Ok(std::env::current_dir()
            .map_err(|_| "STATE_UNAVAILABLE")?
            .join(path))
    }
}
fn private_text(path: &Path, limit: u64) -> Result<(String, u64)> {
    node_tls::check(path, false)?;
    let mut text = String::new();
    std::fs::File::open(path)
        .map_err(|_| "STATE_UNAVAILABLE")?
        .take(limit + 1)
        .read_to_string(&mut text)
        .map_err(|_| "INVALID_OBSERVER_CONFIGURATION")?;
    let size = path.metadata().map_err(|_| "STATE_UNAVAILABLE")?.len();
    Ok((text.replace("\r\n", "\n").replace('\r', "\n"), size))
}
pub fn connection(config: &Path) -> Result<Value> {
    let (text, size) = private_text(&absolute(config)?, 4096)?;
    let raw = discovery_index::invoke(
        "discovery_index.bootstrap_config",
        &json!({"text":text,"size":size}),
    )?;
    let path = raw["credential_file"]
        .as_str()
        .ok_or("INVALID_OBSERVER_CONFIGURATION")?;
    let (text, size) = private_text(&absolute(Path::new(path))?, 16384)?;
    let token = discovery_index::invoke(
        "discovery_index.bootstrap_token",
        &json!({"text":text,"size":size}),
    )?;
    validate(&json!({"endpoint":raw["endpoint"],"credential":token}))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn codec_keeps_original_endpoint_and_existing_private_policy() {
        for endpoint in [
            "https://p620.ts.net",
            "HTTPS://P620.TS.NET:443/",
            "\n https://p620.ts.net\t",
            "https://[fe80::1%En0]",
            "https://127.0.0.1:",
        ] {
            assert_eq!(
                validate(&json!({"endpoint":endpoint,"credential":"x".repeat(48)})).unwrap(),
                json!({"endpoint":endpoint,"credential":"x".repeat(48),"read_only":true})
            );
        }
        for endpoint in [
            "https://user@p620.ts.net",
            "https://p620.ts.net:0",
            "https://p620.ts.net:bad",
            "https://p620.ts.net/path",
            "https://p620.ts.net?x=y",
            "https://p620.ts.net\u{ff0f}evil",
            "https://[127.0.0.1]",
            "https://before[::1]",
        ] {
            assert_eq!(
                validate(&json!({"endpoint":endpoint,"credential":"x".repeat(48)})).unwrap_err(),
                INVALID
            );
        }
    }
}
