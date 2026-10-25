//! One bounded Swift request over private pipes. Pairing PIN is a separate line,
//! never argv, environment, a JSON field, or a log message.
use crate::{discovery_identity as identity, discovery_state, node_tls::Result};
use serde_json::{Value, json};
use std::{
    io::{BufRead, Read, Write},
    path::Path,
    sync::Arc,
    time::Duration,
};

fn peers(state: &discovery_state::Runtime, root: &Path) -> Result<Value> {
    state.invoke("discovery.peers.all", &json!({"root":root}), |_, _| {
        Err("DISCOVERY_CALLBACK_INVALID".into())
    })
}
fn open(root: &Path) -> Result<Value> {
    identity::private_store(root)?;
    let existing = identity::read(root, "identity.json")?;
    let hostname = crate::runtime_config::hostname();
    let generated = format!(
        "mac-{}",
        hostname
            .split('.')
            .next()
            .unwrap_or("")
            .to_lowercase()
            .chars()
            .take(48)
            .map(
                |c| if c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-' {
                    c
                } else {
                    '-'
                }
            )
            .collect::<String>()
    );
    let node = existing
        .as_ref()
        .and_then(|v| v["node_id"].as_str())
        .unwrap_or(&generated);
    identity::open(
        root,
        node,
        Some(&hostname.chars().take(100).collect::<String>()),
    )
}
fn forget(root: &Path, request: &Value, state: &discovery_state::Runtime) -> Result<Value> {
    let _lock = identity::lock(root, "store.lock", false)?;
    let mut records = peers(state, root)?;
    records
        .as_object_mut()
        .ok_or("INVALID_PEER_STORE")?
        .remove(request["node_id"].as_str().ok_or("INVALID_OPERATION")?);
    identity::write(
        root,
        "peers.json",
        &json!({"schema_version":1,"peers":records}),
    )?;
    Ok(json!({"forgotten":true}))
}
fn operation(
    root: &Path,
    request: &Value,
    pin: &str,
    local: &Value,
    candidates: Value,
    state: Arc<discovery_state::Runtime>,
) -> Result<Value> {
    let op = request["operation"].as_str().ok_or("INVALID_OPERATION")?;
    if op == "discover" {
        let devices = crate::discovery_index::invoke(
            "discovery_index.device_rows",
            &json!({"identity":identity::public_record(local)?,"trusted":peers(&state,root)?,"online":candidates}),
        )?;
        return Ok(json!({"devices":devices,"backend":"OPAQUE_V1"}));
    }
    let node = request["node_id"].as_str().ok_or("DEVICE_OFFLINE")?;
    let candidate = candidates.get(node).ok_or("DEVICE_OFFLINE")?;
    let enrollment = crate::discovery_enrollment::Runtime::default();
    let args = json!({"root":root,"candidate":candidate,"code":pin});
    let call = |op| {
        enrollment.invoke(op, &args, state.clone(), |_, _| {
            Err("DISCOVERY_CALLBACK_INVALID".into())
        })
    };
    if op == "pair" {
        if pin.len() != 4 || !pin.bytes().all(|b| b.is_ascii_digit()) {
            return Err("INVALID_PAIRING_CODE".into());
        }
        call("discovery.enrollment.client.pair")?;
    } else {
        call("discovery.enrollment.client.reconnect")?;
    }
    let connection = call("discovery.enrollment.client.observer").map_err(|e| {
        if e == "REMOTE_REQUEST_REJECTED" {
            "OBSERVER_SHARING_UNAVAILABLE".into()
        } else {
            e
        }
    })?;
    let mut connection = crate::discovery_bootstrap::validate(&connection)?;
    connection["node_id"] = candidate["node_id"].clone();
    connection["display_name"] = candidate["display_name"].clone();
    Ok(connection)
}

pub fn handle(root: &Path, request: &Value, pin: &str) -> Result<Value> {
    let local = open(root)?;
    let state = Arc::new(discovery_state::Runtime::default());
    let op = request["operation"].as_str().ok_or("INVALID_OPERATION")?;
    if op == "forget" {
        return forget(root, request, &state);
    }
    if !matches!(op, "discover" | "pair" | "connect") {
        return Err("INVALID_OPERATION".into());
    }
    let browser = crate::mdns_runtime::Browser::start(None)?;
    std::thread::sleep(Duration::from_secs(2));
    let result = browser.candidates();
    browser.close()?;
    operation(
        root,
        request,
        pin,
        &local,
        result?["candidates"].clone(),
        state,
    )
}

pub fn serve<R: BufRead, W: Write>(
    mut input: R,
    mut output: W,
    root: &Path,
) -> std::io::Result<()> {
    let run = || -> Result<Value> {
        let mut request = Vec::new();
        input
            .by_ref()
            .take(4097)
            .read_until(b'\n', &mut request)
            .map_err(|_| "DISCOVERY_UNAVAILABLE")?;
        if request.len() > 4096 {
            return Err("DISCOVERY_UNAVAILABLE".into());
        }
        let request: Value =
            serde_json::from_slice(&request).map_err(|_| "DISCOVERY_UNAVAILABLE")?;
        if !request.is_object() {
            return Err("DISCOVERY_UNAVAILABLE".into());
        }
        let mut pin = Vec::new();
        if request["operation"] == "pair" {
            input
                .by_ref()
                .take(6)
                .read_until(b'\n', &mut pin)
                .map_err(|_| "DISCOVERY_UNAVAILABLE")?;
        }
        let result = std::str::from_utf8(&pin)
            .map_err(|_| "DISCOVERY_UNAVAILABLE".to_string())
            .and_then(|pin| handle(root, &request, pin.trim()));
        // Own the one-use buffer only for this request; never serialize it back.
        pin.fill(0);
        result
    };
    let mut run = run;
    let result=run().unwrap_or_else(|e|json!({"error":if !e.is_empty()&&e.len()<=80&&e.bytes().all(|b|b.is_ascii_uppercase()||b==b'_'){e}else{"PAIRING_FAILED".into()}}));
    let mut bytes = serde_json::to_vec(&result)?;
    if bytes.len() > 65536 {
        bytes = br#"{"error":"TOO_MANY_DEVICES"}"#.to_vec();
    }
    output.write_all(&bytes)
}

pub fn main_cli() -> i32 {
    let root = match crate::runtime_config::expanduser(Path::new(
        "~/Library/Application Support/CLINX Monitor/Devices",
    )) {
        Ok(root) => root,
        Err(_) => {
            println!("{{\"error\":\"DISCOVERY_UNAVAILABLE\"}}");
            return 0;
        }
    };
    if serve(std::io::stdin().lock(), std::io::stdout().lock(), &root).is_ok() {
        0
    } else {
        1
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn malformed_and_oversize_gui_input_never_initializes_identity() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("absent");
        for bytes in [
            vec![b'x'; 4097],
            b"[]\n".to_vec(),
            b"{\"operation\":\n".to_vec(),
        ] {
            let mut out = vec![];
            serve(std::io::Cursor::new(bytes), &mut out, &root).unwrap();
            assert_eq!(
                serde_json::from_slice::<Value>(&out).unwrap(),
                json!({"error":"DISCOVERY_UNAVAILABLE"})
            );
            assert!(!root.exists());
        }
    }
    #[test]
    fn forget_is_local_removal_and_identity_is_stable() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("devices");
        let request = json!({"operation":"forget","node_id":"missing"});
        assert_eq!(
            handle(&root, &request, "").unwrap(),
            json!({"forgotten":true})
        );
        let before = identity::read(&root, "identity.json").unwrap();
        assert_eq!(
            handle(&root, &request, "").unwrap(),
            json!({"forgotten":true})
        );
        assert_eq!(identity::read(&root, "identity.json").unwrap(), before);
        assert_eq!(
            identity::read(&root, "peers.json").unwrap().unwrap()["peers"],
            json!({})
        );
    }
}
