//! Passive observations; a quiet embedded Bridge never grants recycle authority.
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{Read, Write},
    net::{Ipv4Addr, SocketAddr, TcpStream},
    path::Path,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
pub type Result<T> = std::result::Result<T, String>;
pub const VERSION: &str = "0.0.14+0f870e50a973fa820d4c409000059e181e8d242b";
pub const BINARY_SHA: &str = "472eb9dd9dd625b4e6023c3b4a5736b3a2e5a1b6dbe9338e001887a64ec992a6";
pub const PASSIVE_PATHS: [&str; 2] = ["/api/status", "/api/codex/events?limit=1000"];
fn io<T>(v: std::io::Result<T>) -> Result<T> {
    v.map_err(|e| format!("PASSIVE_IO:{e}"))
}
pub fn port(base: &str) -> Result<u16> {
    let p = base
        .strip_prefix("http://127.0.0.1:")
        .ok_or("PASSIVE_LOOPBACK_ENDPOINT_REQUIRED")?;
    if p.is_empty() || p.starts_with('0') || p.len() > 5 || !p.bytes().all(|b| b.is_ascii_digit()) {
        return Err("PASSIVE_LOOPBACK_ENDPOINT_REQUIRED".into());
    }
    p.parse::<u16>()
        .ok()
        .filter(|n| *n > 0)
        .ok_or("PASSIVE_LOOPBACK_ENDPOINT_REQUIRED".into())
}
pub fn read_json(base: &str, path: &str) -> Result<Value> {
    if !PASSIVE_PATHS.contains(&path) {
        return Err("PASSIVE_LOOPBACK_ENDPOINT_REQUIRED".into());
    }
    let body = read_body(base, path, Duration::from_secs(3))?;
    let result: Value = serde_json::from_slice(&body).map_err(|_| "RESPONSE_SHAPE")?;
    if !result.is_object() {
        return Err("RESPONSE_SHAPE".into());
    }
    Ok(result)
}
// Only callers above/below can choose a path. There is no DNS, ambient proxy,
// redirect followup or second read of a mutable endpoint receipt.
fn read_body(base: &str, path: &str, timeout: Duration) -> Result<Vec<u8>> {
    let port = port(base)?;
    let deadline = Instant::now() + timeout;
    let mut s = io(TcpStream::connect_timeout(
        &SocketAddr::from((Ipv4Addr::LOCALHOST, port)),
        timeout,
    ))?;
    io(s.set_write_timeout(Some(timeout)))?;
    io(write!(
        s,
        "GET {path} HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
    ))?;
    let mut b = vec![];
    loop {
        let left = deadline
            .checked_duration_since(Instant::now())
            .ok_or("PASSIVE_DEADLINE")?;
        io(s.set_read_timeout(Some(left)))?;
        let mut part = [0; 8192];
        let n = io(s.read(&mut part))?;
        if n == 0 {
            break;
        }
        b.extend_from_slice(&part[..n]);
        if b.len() > 1048576 + 65536 {
            return Err("RESPONSE_LIMIT".into());
        }
    }
    let end = b
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .filter(|n| *n <= 16384)
        .ok_or("RESPONSE_SHAPE")?;
    let header = std::str::from_utf8(&b[..end]).map_err(|_| "RESPONSE_SHAPE")?;
    let status = header
        .lines()
        .next()
        .and_then(|l| l.split_whitespace().nth(1))
        .ok_or("RESPONSE_SHAPE")?;
    if status.starts_with('3') {
        return Err("REDIRECT_REFUSED".into());
    }
    if status != "200" {
        return Err("HTTP_STATUS".into());
    }
    let mut body = b[end + 4..].to_vec();
    let mut length = None;
    let mut chunked = false;
    for line in header.lines().skip(1) {
        let (k, v) = line.split_once(':').ok_or("RESPONSE_SHAPE")?;
        if k.eq_ignore_ascii_case("Content-Length") {
            if length.is_some() {
                return Err("RESPONSE_SHAPE".into());
            }
            length = Some(v.trim().parse::<usize>().map_err(|_| "RESPONSE_SHAPE")?);
        }
        if k.eq_ignore_ascii_case("Transfer-Encoding") {
            if chunked || !v.trim().eq_ignore_ascii_case("chunked") {
                return Err("RESPONSE_SHAPE".into());
            }
            chunked = true;
        }
    }
    if chunked {
        if length.is_some() {
            return Err("RESPONSE_SHAPE".into());
        }
        let mut decoded = vec![];
        let mut pos = 0;
        loop {
            let line = body[pos..]
                .windows(2)
                .position(|w| w == b"\r\n")
                .ok_or("RESPONSE_SHAPE")?
                + pos;
            let n = usize::from_str_radix(
                std::str::from_utf8(&body[pos..line])
                    .map_err(|_| "RESPONSE_SHAPE")?
                    .split(';')
                    .next()
                    .unwrap(),
                16,
            )
            .map_err(|_| "RESPONSE_SHAPE")?;
            pos = line + 2;
            if n == 0 {
                if body.get(pos..pos + 2) != Some(b"\r\n") {
                    return Err("RESPONSE_SHAPE".into());
                }
                break;
            }
            if n > 1048576 - decoded.len() {
                return Err("RESPONSE_LIMIT".into());
            }
            let data = body.get(pos..pos + n).ok_or("RESPONSE_SHAPE")?;
            decoded.extend_from_slice(data);
            pos += n;
            if body.get(pos..pos + 2) != Some(b"\r\n") {
                return Err("RESPONSE_SHAPE".into());
            }
            pos += 2;
        }
        body = decoded;
    }
    if body.len() > 1048576 {
        return Err("RESPONSE_LIMIT".into());
    }
    if length.is_some_and(|n| n != body.len()) {
        return Err("RESPONSE_SHAPE".into());
    }
    Ok(body)
}
pub fn history(events: &Value) -> bool {
    let Some(events) = events
        .as_array()
        .filter(|a| !a.is_empty() && a.len() <= 512)
    else {
        return false;
    };
    let (mut ready, mut status) = (0, false);
    for (i, e) in events.iter().enumerate() {
        if e["seq"] != json!(i + 1)
            || ![json!("notification"), json!("lifecycle")].contains(&e["source"])
        {
            return false;
        }
        match e["method"].as_str() {
            Some("remoteControl/status/changed") => {
                if e["payload"]["params"]["status"] != "disabled" {
                    return false;
                }
                status = true;
            }
            Some("process/ready") => ready += 1,
            Some("account/updated") => {}
            _ => return false,
        }
    }
    ready == 1 && status
}
pub fn assess(samples: &[Value], stable: bool, binary: &str) -> Value {
    let instance = samples
        .first()
        .and_then(|s| s["status"]["client_instance_id"].as_str())
        .filter(|s| !s.is_empty());
    let known = binary == BINARY_SHA
        && samples.len() >= 2
        && instance.is_some()
        && samples.iter().all(|s| {
            s["status"]["version"] == VERSION
                && s["status"]["client_instance_id"].as_str() == instance
        });
    let quiet = known
        && stable
        && samples
            .iter()
            .all(|s| history(&s["events"]) && s["events"] == samples[0]["events"]);
    json!({"activity_observation":if quiet{"NO_WORK_OBSERVED"}else{"UNKNOWN_OR_ACTIVITY_OBSERVED"},"pinned_contract_verified":known,"process_identity_stable":stable,"complete_quiet_event_history":quiet,"safe_idle_gate":"BLOCKED","safe_to_recycle":false,"blockers":["PENDING_RPC_NOT_EXPOSED","UNCERTAIN_ACK_NOT_EXPOSED","MUTATION_ADMISSION_NOT_FENCED"],"status_get_excluded":"GET /api/codex/status invokes Warmup / EnsureStarted","safe_idle_requires":"Atomic passive snapshot of pending operations, unresolved ACKs, all loaded/active turns and a generation-bound mutation admission fence","production_mutations":0,"provider_rpc_calls":0,"second_consumers_started":0})
}
pub fn process(pid: u32) -> Result<Value> {
    if pid <= 1 {
        return Err("EXACT_PID_REQUIRED".into());
    }
    let root = std::path::PathBuf::from(format!("/proc/{pid}"));
    let stat = io(fs::read_to_string(root.join("stat")))?;
    let tail = stat.rsplit_once(')').ok_or("PROCESS_STATE_INVALID")?.1;
    let parts: Vec<_> = tail.split_whitespace().collect();
    let number = |i: usize| {
        parts
            .get(i)
            .and_then(|s| s.parse::<u32>().ok())
            .ok_or("PROCESS_STATE_INVALID")
    };
    let mut descriptors = serde_json::Map::new();
    for fd in io(fs::read_dir(root.join("fd")))? {
        let fd = io(fd)?;
        match fs::read_link(fd.path()) {
            Ok(p) => {
                let p = p.to_string_lossy();
                if p.starts_with("pipe:") || p.starts_with("socket:") {
                    descriptors.insert(fd.file_name().to_string_lossy().to_string(), json!(p));
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(format!("PROCESS_STATE_UNAVAILABLE:{e}")),
        }
    }
    Ok(
        json!({"pid":pid,"ppid":number(1)?,"pgrp":number(2)?,"session":number(3)?,"start_ticks":parts.get(19).ok_or("PROCESS_STATE_INVALID")?,"cgroup":io(fs::read_to_string(root.join("cgroup")))?,"descriptors":descriptors}),
    )
}
pub fn collect(base: &str, tunnel: u32, bridge: u32, seconds: u64) -> Result<Value> {
    if !(1..=30).contains(&seconds) {
        return Err("OBSERVATION_MUST_BE_BOUNDED_1_TO_30_SECONDS".into());
    }
    let port = port(base)?;
    let before = [process(tunnel)?, process(bridge)?];
    let (t, b) = (&before[0], &before[1]);
    let fds = t["descriptors"]
        .as_object()
        .ok_or("PROCESS_STATE_INVALID")?;
    let owns = io(fs::read_to_string("/proc/net/tcp"))?
        .lines()
        .skip(1)
        .any(|l| {
            let f: Vec<_> = l.split_whitespace().collect();
            f.len() > 9
                && f[1] == format!("0100007F:{port:04X}")
                && f[3] == "0A"
                && fds
                    .values()
                    .any(|v| v == &json!(format!("socket:[{}]", f[9])))
        });
    if b["ppid"] != tunnel
        || b["cgroup"] != t["cgroup"]
        || b["pgrp"] != t["pgrp"]
        || !owns
        || ["0", "1", "2"]
            .iter()
            .any(|fd| !fds.values().any(|v| v == &b["descriptors"][fd]))
    {
        return Err("TUNNEL_BRIDGE_ENDPOINT_IDENTITY_CONFLICT".into());
    }
    let binary = format!(
        "{:x}",
        Sha256::digest(io(fs::read(format!("/proc/{tunnel}/exe")))?)
    );
    if binary != BINARY_SHA {
        return Err("UNQUALIFIED_TUNNEL_BINARY".into());
    }
    let mut samples = vec![];
    for i in 0..2 {
        samples.push(json!({"observed_at":SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs_f64(),"status":read_json(base,PASSIVE_PATHS[0])?,"events":read_json(base,PASSIVE_PATHS[1])?["events"]}));
        if i == 0 {
            std::thread::sleep(Duration::from_secs(seconds));
        }
    }
    let after = [process(tunnel)?, process(bridge)?];
    let stable = before.iter().zip(&after).all(|(a, b)| {
        ["pid", "ppid", "pgrp", "session", "start_ticks", "cgroup"]
            .iter()
            .all(|k| a[k] == b[k])
    });
    let mut report = assess(&samples, stable, &binary);
    report["samples"] = json!(samples);
    report["processes_before"] = json!(before);
    report["processes_after"] = json!(after);
    report["binary_sha256"] = json!(binary);
    report["observation_seconds"] = json!(seconds);
    report["collected_paths"] = json!(PASSIVE_PATHS);
    Ok(report)
}
pub fn health(
    url_file: &Path,
    max_age: f64,
    mut run: impl FnMut(&[String]) -> Result<String>,
) -> Result<Value> {
    if !max_age.is_finite() || max_age <= 0. {
        return Err("INVALID_MAX_AGE".into());
    }
    let state = run(&[
        "systemctl".into(),
        "--user".into(),
        "show".into(),
        "clinx-tunnel.service".into(),
        "--property=ActiveState".into(),
        "--property=MainPID".into(),
    ])?;
    let fields: std::collections::BTreeMap<_, _> =
        state.lines().filter_map(|l| l.split_once('=')).collect();
    let pid = fields
        .get("MainPID")
        .and_then(|p| p.parse::<u32>().ok())
        .filter(|n| *n > 0)
        .ok_or("TUNNEL_NOT_ACTIVE")?;
    if fields.get("ActiveState") != Some(&"active") {
        return Err("TUNNEL_NOT_ACTIVE".into());
    }
    let report: Value = serde_json::from_str(&run(&[
        "/home/pvxlabs/.local/bin/tunnel-client".into(),
        "health".into(),
        "--url-file".into(),
        url_file.display().to_string(),
        "--pid".into(),
        pid.to_string(),
        "--require-control-plane-poll".into(),
        "--json".into(),
    ])?)
    .map_err(|_| "HEALTH_REPORT_INVALID")?;
    let base = report["base_url"].as_str().ok_or("HEALTH_REPORT_INVALID")?;
    port(base)?;
    // The endpoint receipt and health result must name the same exact listener.
    let metadata = io(fs::symlink_metadata(url_file))?;
    if !metadata.is_file() || metadata.len() > 4096 {
        return Err("HEALTH_ENDPOINT_IDENTITY_CONFLICT".into());
    }
    if io(fs::read_to_string(url_file))?.trim() != base {
        return Err("HEALTH_ENDPOINT_IDENTITY_CONFLICT".into());
    }
    let metrics = String::from_utf8(read_body(base, "/metrics", Duration::from_secs(5))?)
        .map_err(|_| "HEALTH_REPORT_INVALID")?;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    if !evaluate(&metrics, now, max_age) {
        return Err("TUNNEL_CONTROL_POLL_STALE".into());
    }
    Ok(json!({"status":"PASS","pid":pid,"poll_fresh":true,"action":"READ_ONLY"}))
}
/// Preserve the original line-by-line health gate while delegating freshness
/// arithmetic to the existing native Tunnel contract. A broken metric cannot
/// join onto a later line and become a successful sample.
pub fn evaluate(metrics: &str, now: f64, max_age: f64) -> bool {
    let re = regex::Regex::new(
        r"^commands_poll_last_successful_timestamp_seconds(?:\{[^}]*\})?\s+(\S+)$",
    )
    .unwrap();
    let rows = metrics
        .lines()
        .filter(|line| re.is_match(line))
        .collect::<Vec<_>>()
        .join("\n");
    clinx_decision_kernel::tunnel_supervisor::invoke(
        "tunnel.poll",
        &json!({"metrics":rows,"now":now,"max_age":max_age}),
    )
    .is_ok_and(|v| v["fresh"] == true)
}
