//! Fixed read-only metadata and scope-bound Provider protocol entrypoints.
#![forbid(unsafe_code)]
use clinx_decision_kernel::{MAX_FRAME_BYTES, node_provider, provider::Result, workspace_probe};
use serde_json::{Value, json};
use std::{
    io::{Read, Write},
    path::PathBuf,
    time::Duration,
};

fn run() -> Result<Value> {
    let mut args = std::env::args().skip(1);
    let mode = args.next().ok_or("PROVIDER_REQUEST_INVALID")?;
    let program = std::env::current_exe().map_err(|_| "WORKSPACE_OBSERVER_UNAVAILABLE")?;
    if mode == "--inspect" {
        let value: Value = serde_json::from_str(&args.next().ok_or("WORKSPACE_PROBE_INVALID")?)
            .map_err(|_| "WORKSPACE_PROBE_INVALID")?;
        if args.next().is_some() {
            return Err("WORKSPACE_PROBE_INVALID".into());
        }
        return workspace_probe::inspect(&value);
    }
    if mode == "--observe" {
        let endpoint = args.next().ok_or("WORKSPACE_PROBE_INVALID")?;
        let request: Value = serde_json::from_str(&args.next().ok_or("WORKSPACE_PROBE_INVALID")?)
            .map_err(|_| "WORKSPACE_PROBE_INVALID")?;
        let qualification_uid = match args.next().as_deref() {
            None => None,
            Some("--qualification-provider-uid") => Some(
                args.next()
                    .ok_or("PROVIDER_UID_REQUIRED")?
                    .parse::<u32>()
                    .map_err(|_| "PROVIDER_UID_INVALID")?,
            ),
            _ => return Err("WORKSPACE_PROBE_INVALID".into()),
        };
        if args.next().is_some() {
            return Err("WORKSPACE_PROBE_INVALID".into());
        }
        return workspace_probe::observe(&endpoint, &program, &request, qualification_uid);
    }
    if mode != "--exchange" {
        return Err("PROVIDER_REQUEST_INVALID".into());
    }
    let endpoint = PathBuf::from(args.next().ok_or("PROVIDER_ENDPOINT_INVALID")?);
    let timeout = args
        .next()
        .ok_or("TIMEOUT_REQUIRED")?
        .parse::<f64>()
        .map_err(|_| "TIMEOUT_INVALID")?;
    if !timeout.is_finite() || timeout <= 0.0 || args.next().is_some() {
        return Err("TIMEOUT_INVALID".into());
    }
    // The paired protocol bounds body and authenticated context separately.
    // Preserve that envelope when the prompt appears in both exact identities.
    let max_exchange = 2 * MAX_FRAME_BYTES + 4096;
    let mut wire = Vec::new();
    std::io::stdin()
        .take((max_exchange + 1) as u64)
        .read_to_end(&mut wire)
        .map_err(|_| "PROVIDER_REQUEST_INVALID")?;
    if wire.len() > max_exchange {
        return Err("PROVIDER_REQUEST_TOO_LARGE".into());
    }
    let value: Value = serde_json::from_slice(&wire).map_err(|_| "PROVIDER_REQUEST_INVALID")?;
    node_provider::exchange(
        &endpoint,
        &program,
        &value["context"],
        &value["body"],
        Duration::try_from_secs_f64(timeout).map_err(|_| "TIMEOUT_INVALID")?,
    )
}
fn main() {
    let value = match run() {
        Ok(v) => json!({"result":v}),
        Err(e) => json!({"error":e}),
    };
    // Metadata command/exec historically returns the metadata object itself.
    let metadata = std::env::args()
        .nth(1)
        .is_some_and(|s| s == "--inspect" || s == "--observe");
    let value = if metadata && value.get("result").is_some() {
        &value["result"]
    } else {
        &value
    };
    if let Ok(mut bytes) = serde_json::to_vec(value) {
        bytes.push(b'\n');
        let _ = std::io::stdout().lock().write_all(&bytes);
    }
}
