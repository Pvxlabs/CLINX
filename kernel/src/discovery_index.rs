//! Untrusted LAN discovery and explicit Observer bootstrap validation.
//! Presence never grants trust, execution authority, or automatic enrollment.
use crate::{
    node_tls,
    read_projection::{text, truth},
};
use node_tls::Result;
use regex::Regex;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    net::{IpAddr, Ipv4Addr, Ipv6Addr},
};
fn lan(v: &Value) -> Result<String> {
    let ip = parse_ip(text(v)).ok_or("INVALID_LAN_ADDRESS")?;
    let IpAddr::V4(ip) = ip else {
        return Err("NON_LAN_ADDRESS".into());
    };
    let [a, b, _, _] = ip.octets();
    if !(a == 10
        || (a == 172 && (16..=31).contains(&b))
        || (a == 192 && b == 168)
        || (a == 169 && b == 254)
        || a == 127)
    {
        return Err("NON_LAN_ADDRESS".into());
    }
    Ok(ip.to_string())
}
fn unhex(value: &Value) -> Result<Vec<u8>> {
    let s = value
        .as_str()
        .filter(|s| s.is_ascii() && s.len() % 2 == 0)
        .ok_or("INVALID_DISCOVERY_METADATA")?;
    (0..s.len())
        .step_by(2)
        .map(|i| {
            u8::from_str_radix(&s[i..i + 2], 16).map_err(|_| "INVALID_DISCOVERY_METADATA".into())
        })
        .collect()
}
fn props(v: &Value) -> Result<Value> {
    let raw = v
        .as_array()
        .filter(|v| v.len() == 4)
        .ok_or("INVALID_DISCOVERY_METADATA")?;
    let mut result = serde_json::Map::new();
    for pair in raw {
        let key = String::from_utf8(unhex(&pair[0])?).map_err(|_| "INVALID_DISCOVERY_METADATA")?;
        if !matches!(
            key.as_str(),
            "node_id" | "display_name" | "protocol_version" | "fingerprint"
        ) || result.contains_key(&key)
        {
            return Err("INVALID_DISCOVERY_METADATA".into());
        }
        let value = unhex(&pair[1])?;
        if value.len() > 200 {
            return Err("INVALID_DISCOVERY_METADATA".into());
        }
        result.insert(
            key,
            json!(String::from_utf8(value).map_err(|_| "INVALID_DISCOVERY_METADATA")?),
        );
    }
    let v = Value::Object(result);
    node_tls::validate_identity(
        &json!({"node_id":v["node_id"],"display_name":v["display_name"],"protocol_version":1}),
    )?;
    if v["protocol_version"] != "1" {
        return Err("UNSUPPORTED_DISCOVERY_PROTOCOL".into());
    }
    if !text(&v["fingerprint"])
        .bytes()
        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        || text(&v["fingerprint"]).len() != 64
    {
        return Err("INVALID_DISCOVERY_FINGERPRINT".into());
    }
    Ok(v)
}
fn records(a: &Value) -> Result<Vec<Value>> {
    let now = a["now"]
        .as_f64()
        .filter(|n| n.is_finite())
        .ok_or("INVALID_DISCOVERY_CLOCK")?;
    Ok(a["records"]
        .as_array()
        .ok_or("INVALID_DISCOVERY_METADATA")?
        .iter()
        .filter(|r| r[3].as_f64().is_some_and(|t| t > now))
        .cloned()
        .collect())
}
fn candidates(rows: &[Value]) -> Result<Value> {
    let mut grouped: BTreeMap<String, Vec<&Value>> = BTreeMap::new();
    for row in rows {
        grouped
            .entry(text(&row[1]["node_id"]).into())
            .or_default()
            .push(row);
    }
    let mut out = serde_json::Map::new();
    for (id, rows) in grouped {
        let p = &rows[0][1];
        let mut endpoints = BTreeSet::new();
        let mut fingerprints = BTreeSet::new();
        for row in rows {
            fingerprints.insert(text(&row[1]["fingerprint"]));
            for e in row[2].as_array().ok_or("INVALID_DISCOVERY_METADATA")? {
                endpoints.insert((
                    text(&e[0]).to_owned(),
                    e[1].as_u64().ok_or("INVALID_DISCOVERY_ENDPOINT")?,
                ));
            }
        }
        out.insert(id.clone(),json!({"node_id":id,"display_name":p["display_name"],"fingerprint":p["fingerprint"],"endpoints":endpoints,"mismatch":fingerprints.len()!=1}));
    }
    Ok(Value::Object(out))
}
fn devices(a: &Value) -> Result<Value> {
    let identity = &a["identity"];
    let trusted = a["trusted"].as_object().ok_or("INVALID_PEER_STORE")?;
    let online = a["online"]
        .as_object()
        .ok_or("INVALID_DISCOVERY_METADATA")?;
    let mut local = identity.clone();
    let map = local.as_object_mut().ok_or("INVALID_IDENTITY")?;
    map.insert("state".into(), json!("This Device"));
    map.insert("authenticated".into(), json!(false));
    let mut rows = vec![local];
    let ids = trusted.keys().chain(online.keys()).collect::<BTreeSet<_>>();
    for id in ids {
        let candidate = online.get(id).unwrap_or(&Value::Null);
        let peer = trusted.get(id).unwrap_or(&Value::Null);
        if identity["node_id"] == *id {
            if !candidate.is_null()
                && (truth(&candidate["mismatch"])
                    || candidate["fingerprint"] != identity["fingerprint"])
            {
                rows.push(json!({"node_id":id,"display_name":candidate["display_name"],"state":"Identity Mismatch","authenticated":false}));
            }
            continue;
        }
        let state = if !candidate.is_null()
            && (truth(&candidate["mismatch"])
                || (!peer.is_null() && peer["fingerprint"] != candidate["fingerprint"]))
        {
            "Identity Mismatch"
        } else if peer["trust_state"] == "TRUSTED" {
            if candidate.is_null() {
                "Offline Trusted"
            } else {
                "Online Trusted"
            }
        } else if !candidate.is_null() {
            "Online Unpaired"
        } else {
            continue;
        };
        rows.push(json!({"node_id":id,"display_name":if !peer.is_null(){peer["friendly_name"].clone()}else{candidate["display_name"].clone()},"state":state,"authenticated":false,"security_status":if peer.is_null(){json!("UNPAIRED")}else{peer["security_status"].clone()}}));
    }
    Ok(json!(rows))
}
fn in4(ip: Ipv4Addr, network: &str, bits: u32) -> bool {
    let n: u32 = network.parse::<Ipv4Addr>().unwrap().into();
    let mask = u32::MAX.checked_shl(32 - bits).unwrap_or(0);
    u32::from(ip) & mask == n & mask
}
fn private4(ip: Ipv4Addr) -> bool {
    !["192.0.0.9", "192.0.0.10"]
        .iter()
        .any(|n| ip.to_string() == *n)
        && [
            ("0.0.0.0", 8),
            ("10.0.0.0", 8),
            ("127.0.0.0", 8),
            ("169.254.0.0", 16),
            ("172.16.0.0", 12),
            ("192.0.0.0", 24),
            ("192.0.0.170", 31),
            ("192.0.2.0", 24),
            ("192.168.0.0", 16),
            ("198.18.0.0", 15),
            ("198.51.100.0", 24),
            ("203.0.113.0", 24),
            ("240.0.0.0", 4),
            ("255.255.255.255", 32),
        ]
        .iter()
        .any(|(n, b)| in4(ip, n, *b))
}
fn in6(ip: Ipv6Addr, network: &str, bits: u32) -> bool {
    let n: u128 = network.parse::<Ipv6Addr>().unwrap().into();
    let mask = u128::MAX.checked_shl(128 - bits).unwrap_or(0);
    u128::from(ip) & mask == n & mask
}
fn parse_ip(host: &str) -> Option<IpAddr> {
    let address = if let Some((address, zone)) = host.split_once('%') {
        if zone.is_empty() || zone.contains('%') || !address.contains(':') {
            return None;
        }
        address
    } else {
        host
    };
    address.parse::<IpAddr>().ok()
}
fn private_ip(host: &str) -> Option<bool> {
    let ip = parse_ip(host)?;
    Some(match ip {
        IpAddr::V4(ip) => private4(ip) && !ip.is_unspecified() && !ip.is_multicast(),
        IpAddr::V6(ip) => {
            let private = if let Some(v4) = ip.to_ipv4_mapped() {
                private4(v4)
            } else {
                ![
                    ("2001:1::1", 128),
                    ("2001:1::2", 128),
                    ("2001:3::", 32),
                    ("2001:4:112::", 48),
                    ("2001:20::", 28),
                    ("2001:30::", 28),
                ]
                .iter()
                .any(|(n, b)| in6(ip, n, *b))
                    && [
                        ("::1", 128),
                        ("::", 128),
                        ("::ffff:0:0", 96),
                        ("64:ff9b:1::", 48),
                        ("100::", 64),
                        ("2001::", 23),
                        ("2001:db8::", 32),
                        ("2002::", 16),
                        ("fc00::", 7),
                        ("fe80::", 10),
                    ]
                    .iter()
                    .any(|(n, b)| in6(ip, n, *b))
            };
            private && !ip.is_unspecified() && !ip.is_multicast()
        }
    })
}
fn connection(a: &Value) -> Result<Value> {
    let endpoint = a["endpoint"]
        .as_str()
        .ok_or("INVALID_OBSERVER_CONNECTION")?;
    let credential = a["credential"]
        .as_str()
        .ok_or("INVALID_OBSERVER_CONNECTION")?;
    let url = &a["url"];
    let host = text(&url["hostname"]);
    let private =
        private_ip(host).unwrap_or_else(|| host.ends_with(".ts.net") || host.ends_with(".local"));
    if url["scheme"] != "https"
        || !private
        || !url["username"].is_null()
        || !url["password"].is_null()
        || !matches!(text(&url["path"]), "" | "/")
        || truth(&url["query"])
        || truth(&url["fragment"])
        || endpoint.chars().count() > 2048
        || !(32..=256).contains(&credential.len())
        || !credential
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
        || (!url["port"].is_null()
            && !url["port"]
                .as_u64()
                .is_some_and(|p| (1..=65535).contains(&p)))
    {
        return Err("INVALID_OBSERVER_CONNECTION".into());
    }
    Ok(json!({"endpoint":endpoint,"credential":credential,"read_only":true}))
}
pub fn invoke(op: &str, a: &Value) -> Result<Value> {
    match op {
        "discovery_index.lan_address" => Ok(json!(lan(&a["value"])?)),
        "discovery_index.update" => {
            let p = props(&a["properties"])?;
            let port = a["port"]
                .as_u64()
                .filter(|p| (1..=65535).contains(p))
                .ok_or("INVALID_DISCOVERY_ENDPOINT")?;
            let addresses = a["addresses"]
                .as_array()
                .filter(|v| v.len() <= 16)
                .ok_or("INVALID_DISCOVERY_ENDPOINT")?;
            let endpoints = addresses
                .iter()
                .map(|v| lan(v).map(|s| (s, port)))
                .collect::<Result<BTreeSet<_>>>()?;
            if endpoints.is_empty() {
                return Err("NO_LAN_ADDRESSES".into());
            }
            let mut rows = records(a)?;
            let existing = rows.iter().position(|r| r[0] == a["service"]);
            if rows.len() >= 256 && existing.is_none() {
                return Err("DISCOVERY_CAPACITY".into());
            }
            let ttl = a["ttl"]
                .as_f64()
                .filter(|n| n.is_finite())
                .ok_or("INVALID_DISCOVERY_METADATA")?;
            if ttl <= 0.0 {
                if let Some(i) = existing {
                    rows.remove(i);
                }
            } else {
                let record = json!([
                    a["service"],
                    p,
                    endpoints,
                    a["now"].as_f64().unwrap() + ttl.min(120.0)
                ]);
                if let Some(i) = existing {
                    rows[i] = record
                } else {
                    rows.push(record)
                }
            }
            Ok(json!(rows))
        }
        "discovery_index.remove" => Ok(json!(
            a["records"]
                .as_array()
                .ok_or("INVALID_DISCOVERY_METADATA")?
                .iter()
                .filter(|r| r[0] != a["service"])
                .collect::<Vec<_>>()
        )),
        "discovery_index.expire" => Ok(json!(records(a)?)),
        "discovery_index.candidates" => {
            let rows = records(a)?;
            Ok(json!({"candidates":candidates(&rows)?,"records":rows}))
        }
        "discovery_index.device_rows" => devices(a),
        "discovery_index.local_addresses" => {
            let mut found = BTreeSet::new();
            for adapter in a["adapters"]
                .as_array()
                .ok_or("INVALID_DISCOVERY_METADATA")?
            {
                if [
                    "lo",
                    "docker",
                    "veth",
                    "br-",
                    "bridge",
                    "virbr",
                    "tailscale",
                    "tun",
                    "tap",
                    "vnet",
                ]
                .iter()
                .any(|p| text(&adapter["name"]).starts_with(p))
                {
                    continue;
                }
                for ip in adapter["ips"]
                    .as_array()
                    .ok_or("INVALID_DISCOVERY_METADATA")?
                {
                    if let Ok(ip) = lan(ip)
                        && !ip.starts_with("127.")
                    {
                        found.insert(ip);
                    }
                }
            }
            Ok(json!(found.into_iter().take(16).collect::<Vec<_>>()))
        }
        "discovery_index.validate_connection" => connection(a),
        "discovery_index.refresh" => {
            let pointer = a["pointers"]
                .as_array()
                .ok_or("INVALID_DISCOVERY_METADATA")?
                .iter()
                .rfind(|r| r["type"] == 12 && r["expired"] == false && r["alias"] == a["service"]);
            let records = a["records"]
                .as_array()
                .ok_or("INVALID_DISCOVERY_METADATA")?
                .iter()
                .filter(|r| matches!(r["type"].as_i64(), Some(16 | 33)) && r["expired"] == false)
                .collect::<Vec<_>>();
            let addresses = a["addresses"]
                .as_array()
                .ok_or("INVALID_DISCOVERY_METADATA")?
                .iter()
                .filter(|r| r["type"] == 1 && r["expired"] == false)
                .collect::<Vec<_>>();
            if pointer.is_none()
                || ![16, 33]
                    .iter()
                    .all(|t| records.iter().any(|r| r["type"] == *t))
                || addresses.is_empty()
            {
                return Ok(json!({"active":false}));
            }
            let ttl = std::iter::once(pointer.unwrap())
                .chain(records)
                .chain(addresses.iter().copied())
                .map(|r| {
                    r["remaining"]
                        .as_f64()
                        .filter(|n| n.is_finite())
                        .ok_or_else(|| "INVALID_DISCOVERY_METADATA".to_owned())
                })
                .collect::<Result<Vec<_>>>()?
                .into_iter()
                .reduce(f64::min)
                .unwrap();
            let addresses = addresses
                .into_iter()
                .map(|r| {
                    let bytes: [u8; 4] = unhex(&r["address"])?
                        .try_into()
                        .map_err(|_| "INVALID_LAN_ADDRESS")?;
                    Ok(Ipv4Addr::from(bytes).to_string())
                })
                .collect::<Result<Vec<_>>>()?;
            Ok(json!({"active":true,"ttl":ttl,"addresses":addresses}))
        }
        "discovery_index.bootstrap_config" => {
            if a["size"].as_u64().is_none_or(|n| n > 4096) {
                return Err("INVALID_OBSERVER_CONFIGURATION".into());
            }
            let raw: Value = serde_json::from_str(text(&a["text"]))
                .map_err(|_| "INVALID_OBSERVER_CONFIGURATION")?;
            if raw["share_read_only_observer"] != true {
                return Err("OBSERVER_SHARING_DISABLED".into());
            }
            Ok(json!({"endpoint":raw["endpoint"],"credential_file":raw["credential_file"]}))
        }
        "discovery_index.bootstrap_token" => {
            if a["size"].as_u64().is_none_or(|n| n > 16384) {
                return Err("INVALID_OBSERVER_CONFIGURATION".into());
            }
            let input = a["text"].as_str().ok_or("INVALID_OBSERVER_CONFIGURATION")?;
            let re = Regex::new(r"(?m)^CLINX_OBSERVER_TOKEN=([A-Za-z0-9_-]{32,256})$").unwrap();
            let tokens = re.captures_iter(input).collect::<Vec<_>>();
            if tokens.len() != 1 {
                return Err("INVALID_OBSERVER_CONFIGURATION".into());
            }
            Ok(json!(&tokens[0][1]))
        }
        _ => Err("DISCOVERY_INDEX_OPERATION_UNKNOWN".into()),
    }
}
