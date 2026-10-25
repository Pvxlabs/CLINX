//! Independent native Tunnel lifecycle and bounded recovery.
use crate::tunnel_supervisor::{invoke, process_start};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    net::{Ipv4Addr, SocketAddr, TcpStream},
    os::unix::{
        fs::{DirBuilderExt, MetadataExt, OpenOptionsExt},
        process::CommandExt,
    },
    path::{Path, PathBuf},
    process::{Child, Command},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
type Result<T> = std::result::Result<T, String>;
fn wall() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
fn seconds(v: &Value) -> Result<f64> {
    v.as_f64()
        .filter(|v| v.is_finite() && *v >= 0.)
        .ok_or("INVALID_TIME".into())
}
fn duration(v: f64) -> Result<Duration> {
    Duration::try_from_secs_f64(v).map_err(|_| "INVALID_TIME".into())
}
fn remaining(deadline: Instant) -> Result<Duration> {
    deadline
        .checked_duration_since(Instant::now())
        .filter(|d| !d.is_zero())
        .ok_or("PROBE_DEADLINE".into())
}
fn io_probe(e: std::io::Error) -> String {
    if matches!(
        e.kind(),
        std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
    ) {
        "PROBE_DEADLINE"
    } else {
        "PROBE_UNAVAILABLE"
    }
    .into()
}
/// Exact loopback only: no DNS, proxies, credentials or redirect followup.
pub fn probe(file: &Path, max_age: f64, timeout: f64) -> Result<Value> {
    let deadline = Instant::now()
        .checked_add(duration(timeout)?)
        .ok_or("INVALID_TIME")?;
    let metadata = fs::symlink_metadata(file).map_err(|_| "PROBE_UNAVAILABLE")?;
    if !metadata.file_type().is_file() || metadata.len() > 4096 {
        return Err("HEALTH_URL_NOT_LOOPBACK".into());
    }
    let url = fs::read_to_string(file).map_err(|_| "PROBE_UNAVAILABLE")?;
    let address = url
        .trim()
        .strip_prefix("http://127.0.0.1:")
        .ok_or("HEALTH_URL_NOT_LOOPBACK")?;
    let port = address.strip_suffix('/').unwrap_or(address);
    if port.is_empty() || !port.bytes().all(|b| b.is_ascii_digit()) {
        return Err("HEALTH_URL_NOT_LOOPBACK".into());
    }
    let port = port
        .parse::<u16>()
        .ok()
        .filter(|p| *p != 0)
        .ok_or("HEALTH_URL_NOT_LOOPBACK")?;
    let mut stream = TcpStream::connect_timeout(
        &SocketAddr::from((Ipv4Addr::LOCALHOST, port)),
        remaining(deadline)?,
    )
    .map_err(io_probe)?;
    stream
        .set_write_timeout(Some(remaining(deadline)?))
        .map_err(io_probe)?;
    stream
        .write_all(
            format!("GET /metrics HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n")
                .as_bytes(),
        )
        .map_err(io_probe)?;
    let mut bytes = Vec::new();
    let mut header_end = None;
    let mut length = None;
    let mut chunked = false;
    loop {
        stream
            .set_read_timeout(Some(remaining(deadline)?))
            .map_err(io_probe)?;
        let mut buf = [0; 8192];
        let n = stream.read(&mut buf).map_err(io_probe)?;
        if n == 0 {
            break;
        }
        bytes.extend_from_slice(&buf[..n]);
        if header_end.is_none() {
            if let Some(i) = bytes.windows(4).position(|v| v == b"\r\n\r\n") {
                if i > 16384 {
                    return Err("PROBE_UNAVAILABLE".into());
                }
                let headers = std::str::from_utf8(&bytes[..i]).map_err(|_| "PROBE_UNAVAILABLE")?;
                let mut lines = headers.split("\r\n");
                let status = lines
                    .next()
                    .and_then(|v| v.split_whitespace().nth(1))
                    .ok_or("PROBE_UNAVAILABLE")?;
                if status.starts_with('3') {
                    return Err("HEALTH_REDIRECT_REFUSED".into());
                }
                if status != "200" {
                    return Err("PROBE_UNAVAILABLE".into());
                }
                for line in lines {
                    let (k, v) = line.split_once(':').ok_or("PROBE_UNAVAILABLE")?;
                    if k.eq_ignore_ascii_case("content-length") {
                        if length.is_some() {
                            return Err("PROBE_UNAVAILABLE".into());
                        }
                        length = Some(v.trim().parse::<usize>().map_err(|_| "PROBE_UNAVAILABLE")?);
                    }
                    if k.eq_ignore_ascii_case("transfer-encoding") {
                        if !v.trim().eq_ignore_ascii_case("chunked") || chunked {
                            return Err("PROBE_UNAVAILABLE".into());
                        }
                        chunked = true;
                    }
                }
                if chunked && length.is_some() {
                    return Err("PROBE_UNAVAILABLE".into());
                }
                if length.is_some_and(|n| n > 1048576) {
                    return Err("METRICS_TOO_LARGE".into());
                }
                header_end = Some(i + 4);
            } else if bytes.len() > 16384 {
                return Err("PROBE_UNAVAILABLE".into());
            }
        }
        if let Some(start) = header_end {
            if bytes.len() - start > 1048576 + if chunked { 65536 } else { 0 } {
                return Err("METRICS_TOO_LARGE".into());
            }
            if length.is_some_and(|n| bytes.len() - start >= n) {
                break;
            }
            if chunked && decode_chunks(&bytes[start..])?.is_some() {
                break;
            }
        }
    }
    let start = header_end.ok_or("PROBE_UNAVAILABLE")?;
    let body = if chunked {
        decode_chunks(&bytes[start..])?.ok_or("PROBE_UNAVAILABLE")?
    } else {
        if length.is_some_and(|n| bytes.len() - start != n) {
            return Err("PROBE_UNAVAILABLE".into());
        }
        bytes[start..].to_vec()
    };
    if body.len() > 1048576 {
        return Err("METRICS_TOO_LARGE".into());
    }
    remaining(deadline)?;
    invoke(
        "tunnel.poll",
        &json!({"metrics":std::str::from_utf8(&body).map_err(|_|"PROBE_UNAVAILABLE")?,"now":wall(),"max_age":max_age}),
    )
}
fn decode_chunks(body: &[u8]) -> Result<Option<Vec<u8>>> {
    let mut output = Vec::new();
    let mut cursor = 0;
    loop {
        let Some(end) = body[cursor..].windows(2).position(|v| v == b"\r\n") else {
            return Ok(None);
        };
        let size = std::str::from_utf8(&body[cursor..cursor + end])
            .map_err(|_| "PROBE_UNAVAILABLE")?
            .split(';')
            .next()
            .unwrap();
        let n = usize::from_str_radix(size.trim(), 16).map_err(|_| "PROBE_UNAVAILABLE")?;
        cursor += end + 2;
        if n == 0 {
            if body.get(cursor..cursor + 2) == Some(b"\r\n")
                || body[cursor..].windows(4).any(|v| v == b"\r\n\r\n")
            {
                return Ok(Some(output));
            }
            return Ok(None);
        }
        if n > 1048576 - output.len() {
            return Err("METRICS_TOO_LARGE".into());
        }
        let end = cursor.checked_add(n).ok_or("METRICS_TOO_LARGE")?;
        if body.len() < end + 2 {
            return Ok(None);
        }
        if &body[end..end + 2] != b"\r\n" {
            return Err("PROBE_UNAVAILABLE".into());
        }
        output.extend_from_slice(&body[cursor..end]);
        cursor = end + 2;
    }
}
fn failed_sample(reason: &str) -> Value {
    json!({"fresh":false,"last_poll":null,"poll_age":null,"probe_error":if reason == "PROBE_DEADLINE" { "PROBE_DEADLINE" } else { "PROBE_UNAVAILABLE" }})
}
fn default_policy() -> Value {
    json!({"stale_after":120,"startup_grace":45,"interval":5,"probe_timeout":3,"stop_timeout":5,"cooldown":10,"failure_grace":15,"max_restarts":3,"stable_for":300})
}
// Durable identity is byte-compatible with Python json.dumps(sort_keys=True).
fn python_json(v: &Value) -> String {
    match v {
        Value::Array(a) => format!(
            "[{}]",
            a.iter().map(python_json).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(o) => format!(
            "{{{}}}",
            o.iter()
                .map(|(k, v)| format!("{}: {}", python_json(&json!(k)), python_json(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        Value::String(s) => {
            let encoded = serde_json::to_string(s).unwrap();
            let mut result = String::new();
            for ch in encoded.chars() {
                if (ch as u32) < 127 {
                    result.push(ch);
                } else {
                    for unit in ch.encode_utf16(&mut [0; 2]) {
                        result.push_str(&format!("\\u{unit:04x}"));
                    }
                }
            }
            result
        }
        Value::Number(n) if n.is_f64() => {
            let f = n.as_f64().unwrap();
            if f != 0. && (f.abs() < 0.0001 || f.abs() >= 1e16) {
                let s = format!("{f:e}");
                let (base, exponent) = s.split_once('e').unwrap();
                format!("{base}e{:+03}", exponent.parse::<i32>().unwrap())
            } else {
                n.to_string()
            }
        }
        _ => v.to_string(),
    }
}
fn identity(command: &Value, policy: &Value) -> String {
    format!(
        "{:x}",
        Sha256::digest(python_json(&json!([command, policy])).as_bytes())
    )
}

trait Io {
    fn call(&mut self, op: &str, a: Value, snapshot: &Value) -> Result<Value>;
}
struct Engine<I: Io> {
    io: I,
    config: Value,
    state: Value,
    timing: Value,
    launched_mono: f64,
    launched_wall: f64,
    child: Option<u32>,
    stop_unconfirmed: bool,
}
impl<I: Io> Engine<I> {
    fn new(io: I, config: Value) -> Result<Self> {
        invoke("tunnel.policy", &json!({"policy":config["policy"]}))?;
        if config["command"].as_array().is_none_or(|c| {
            c.is_empty()
                || c.iter().any(|v| v.as_str().is_none())
                || !Path::new(c[0].as_str().unwrap()).is_absolute()
        }) {
            return Err("ABSOLUTE_TUNNEL_COMMAND_REQUIRED".into());
        }
        Ok(Self {
            state: config.get("state").cloned().unwrap_or(json!({})),
            timing: json!({"healthy_since":config["healthy_since"],"healthy_start_poll":config["healthy_start_poll"],"failure_started":null}),
            launched_mono: config["launched_mono"].as_f64().unwrap_or(0.),
            launched_wall: config["launched_wall"].as_f64().unwrap_or(0.),
            child: config["child_pid"].as_u64().map(|v| v as u32),
            stop_unconfirmed: config["stop_unconfirmed"] == true,
            io,
            config,
        })
    }
    fn snapshot(&self) -> Value {
        json!({"state":self.state,"healthy_since":self.timing["healthy_since"],"healthy_start_poll":self.timing["healthy_start_poll"],"launched_mono":self.launched_mono,"launched_wall":self.launched_wall,"child_pid":self.child,"stop_unconfirmed":self.stop_unconfirmed})
    }
    fn call(&mut self, op: &str, a: Value) -> Result<Value> {
        self.io.call(op, a, &self.snapshot())
    }
    fn mono(&mut self) -> Result<f64> {
        seconds(&self.call("monotonic", Value::Null)?)
    }
    fn record(&mut self, status: &str, reason: &str, fields: Value) -> Result<()> {
        let now = self.call("time", Value::Null)?;
        let r = invoke(
            "tunnel.record",
            &json!({"directory":self.config["directory"],"state":self.state,"status":status,"reason":reason,"updated_at":now,"supervisor_pid":self.config["supervisor_pid"],"supervisor_start":self.config["supervisor_start"],"policy":self.config["policy"],"fields":fields}),
        )?;
        self.state = r["state"].clone();
        if r["changed"] == true {
            self.call("event", Value::Null)?;
        }
        Ok(())
    }
    fn acquire(&mut self) -> Result<()> {
        self.call("lock", json!({"directory":self.config["directory"]}))?;
        self.state = invoke(
            "tunnel.acquire",
            &json!({"directory":self.config["directory"],"identity":self.config["identity"]}),
        )?;
        Ok(())
    }
    fn launch(&mut self) -> Result<bool> {
        let r = invoke(
            "tunnel.launch",
            &json!({"state":self.state,"policy":self.config["policy"]}),
        )?;
        self.state = r["state"].clone();
        if r["allowed"] != true {
            self.record(
                "HOLD",
                "RECOVERY_BUDGET_EXHAUSTED",
                json!({"child_pid":null}),
            )?;
            return Ok(false);
        }
        self.record("STARTING","LAUNCH_INTENT",json!({"child_pid":null,"child_start":null,"last_poll":null,"poll_age":null,"poll_errors":{},"probe_error":null,"probe_seconds":null,"fresh":false,"recovery_seconds":null}))?;
        let file =
            Path::new(self.config["directory"].as_str().ok_or("INVALID_STATE")?).join("tunnel.url");
        match fs::remove_file(&file) {
            Ok(()) => (),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
            Err(_) => return Err("HEALTH_URL_UNAVAILABLE".into()),
        }
        self.launched_mono = self.mono()?;
        self.launched_wall = seconds(&self.call("time", Value::Null)?)?;
        self.timing["healthy_since"] = Value::Null;
        self.timing["healthy_start_poll"] = Value::Null;
        let spawned = self.call(
            "spawn",
            json!({"command":self.config["command"],"url_file":file}),
        )?;
        self.child = spawned.as_u64().map(|n| n as u32);
        if self.child.is_none() {
            self.record("DEGRADED", "SPAWN_FAILED", json!({"child_pid":null}))?;
            return Ok(false);
        }
        self.record(
            "STARTING",
            "AWAITING_NEW_POLL",
            json!({"child_pid":self.child,"child_start":process_start(&json!(self.child))}),
        )?;
        Ok(true)
    }
    fn terminate(&mut self, allow_override: bool) -> Result<()> {
        if allow_override
            && let Some(pid) = self
                .call("terminate_override", Value::Null)?
                .get("child_pid")
        {
            self.child = pid.as_u64().map(|n| n as u32);
            return Ok(());
        }
        let Some(pid) = self.child else {
            return Ok(());
        };
        for sig in [15, 9] {
            let deadline = self.mono()? + seconds(&self.config["policy"]["stop_timeout"])?;
            self.call("signal", json!({"pid":pid,"signal":sig}))?;
            let timeout = (deadline - self.mono()?).max(0.);
            if self.call("child_wait", json!({"timeout":timeout}))? != true {
                continue;
            }
            loop {
                match self.call("group_live", json!({"pid":pid})) {
                    Ok(v) if v == false => {
                        self.child = None;
                        self.call("child_clear", Value::Null)?;
                        return Ok(());
                    }
                    Ok(_) => (),
                    Err(_) => {
                        return self.unconfirmed(pid);
                    }
                }
                let remaining = deadline - self.mono()?;
                if remaining <= 0. {
                    break;
                }
                self.call("sleep", json!(remaining.min(0.02)))?;
            }
        }
        self.unconfirmed(pid)
    }
    fn unconfirmed(&mut self, pid: u32) -> Result<()> {
        self.stop_unconfirmed = true;
        self.record(
            "HOLD",
            "CHILD_TERMINATION_UNCONFIRMED",
            json!({"child_pid":pid}),
        )?;
        Err("CHILD_TERMINATION_UNCONFIRMED".into())
    }
    fn wait(&mut self, key: &str) -> Result<bool> {
        Ok(self.call("wait", self.config["policy"][key].clone())? == true)
    }
    fn cycle(&mut self) -> Result<i32> {
        while self.call("stopped", Value::Null)? != true {
            if self.child.is_none() && !self.launch()? {
                if self.state["status"] == "HOLD" {
                    return Ok(78);
                }
                if self.wait("cooldown")? {
                    break;
                }
                continue;
            }
            let started = self.mono()?;
            let exited = self.call("poll", Value::Null)? == true;
            let mut sample = if exited {
                json!({"fresh":false,"last_poll":null,"poll_age":null})
            } else {
                self.call(
                    "sample",
                    json!({"directory":self.config["directory"],"policy":self.config["policy"]}),
                )?
            };
            let now = self.mono()?;
            sample["probe_seconds"] = json!(now - started);
            let r = invoke(
                "tunnel.step",
                &json!({"policy":self.config["policy"],"state":self.state,"timing":self.timing,"sample":sample,"now":now,"launched_wall":self.launched_wall,"launched_mono":self.launched_mono,"exited":exited}),
            )?;
            self.state = r["state"].clone();
            self.timing = r["timing"].clone();
            self.record(
                r["status"].as_str().ok_or("INVALID_DECISION")?,
                r["reason"].as_str().ok_or("INVALID_DECISION")?,
                sample,
            )?;
            if r["action"] == "recover" {
                self.terminate(true)?;
                self.record(
                    "BACKOFF",
                    r["reason"].as_str().unwrap(),
                    json!({"child_pid":null}),
                )?;
                if self.wait("cooldown")? {
                    break;
                }
            } else {
                self.wait("interval")?;
            }
        }
        self.record("STOPPED", "SUPERVISOR_STOP_REQUESTED", json!({}))?;
        Ok(0)
    }
    fn run(&mut self) -> Result<i32> {
        if let Err(e) = self.acquire() {
            let _ = self.call("unlock", Value::Null);
            return Err(e);
        }
        let result = self.cycle();
        let cleanup = if !self.stop_unconfirmed {
            self.terminate(true).and_then(|_| {
                // Freeze the values before record replaces the state object.
                let status = self.state["status"]
                    .as_str()
                    .unwrap_or("STOPPED")
                    .to_owned();
                let reason = self.state["reason"]
                    .as_str()
                    .unwrap_or("SUPERVISOR_STOPPED")
                    .to_owned();
                self.record(&status, &reason, json!({"child_pid":null}))
            })
        } else {
            Ok(())
        };
        let unlocked = self.call("unlock", Value::Null);
        cleanup?;
        unlocked?;
        result
    }
}

struct Native {
    lock: Option<File>,
    child: Option<Child>,
    started: Instant,
    stop: Arc<AtomicBool>,
}
impl Native {
    fn new(stop: Arc<AtomicBool>) -> Self {
        Self {
            lock: None,
            child: None,
            started: Instant::now(),
            stop,
        }
    }
}
impl Io for Native {
    fn call(&mut self, op: &str, a: Value, s: &Value) -> Result<Value> {
        match op {
            "monotonic" => Ok(json!(self.started.elapsed().as_secs_f64())),
            "time" => Ok(json!(wall())),
            "lock" => {
                let dir = Path::new(
                    a["directory"]
                        .as_str()
                        .ok_or("PRIVATE_STATE_DIRECTORY_REQUIRED")?,
                );
                fs::DirBuilder::new()
                    .recursive(true)
                    .mode(0o700)
                    .create(dir)
                    .map_err(|_| "PRIVATE_STATE_DIRECTORY_REQUIRED")?;
                let info =
                    fs::symlink_metadata(dir).map_err(|_| "PRIVATE_STATE_DIRECTORY_REQUIRED")?;
                if !info.is_dir()
                    || info.uid() != rustix::process::getuid().as_raw()
                    || info.mode() & 0o077 != 0
                {
                    return Err("PRIVATE_STATE_DIRECTORY_REQUIRED".into());
                }
                let fd = OpenOptions::new()
                    .create(true)
                    .truncate(false)
                    .read(true)
                    .write(true)
                    .mode(0o600)
                    .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
                    .open(dir.join("supervisor.lock"))
                    .map_err(|_| "STATE_LOCK_UNAVAILABLE")?;
                rustix::fs::flock(&fd, rustix::fs::FlockOperation::NonBlockingLockExclusive)
                    .map_err(|_| "STATE_LOCK_BUSY")?;
                self.lock = Some(fd);
                Ok(Value::Null)
            }
            "unlock" => {
                self.lock = None;
                Ok(Value::Null)
            }
            "spawn" => {
                let c = a["command"]
                    .as_array()
                    .ok_or("ABSOLUTE_TUNNEL_COMMAND_REQUIRED")?;
                let mut cmd = Command::new(
                    std::env::current_exe().map_err(|_| "SUPERVISOR_PATH_UNAVAILABLE")?,
                );
                cmd.arg("--owned-child")
                    .args(c.iter().map(|v| v.as_str().unwrap()))
                    .args(["--health.url-file", a["url_file"].as_str().unwrap()]);
                match cmd.spawn() {
                    Ok(child) => {
                        let pid = child.id();
                        self.child = Some(child);
                        Ok(json!(pid))
                    }
                    Err(_) => Ok(Value::Null),
                }
            }
            "poll" => Ok(json!(
                self.child
                    .as_mut()
                    .ok_or("CHILD_UNAVAILABLE")?
                    .try_wait()
                    .map_err(|_| "CHILD_UNAVAILABLE")?
                    .is_some()
            )),
            "sample" => Ok(probe(
                &Path::new(a["directory"].as_str().unwrap()).join("tunnel.url"),
                seconds(&a["policy"]["stale_after"])?,
                seconds(&a["policy"]["probe_timeout"])?,
            )
            .unwrap_or_else(|e| failed_sample(&e))),
            "terminate_override" => Ok(Value::Null),
            "signal" => {
                let pid = rustix::process::Pid::from_raw(
                    a["pid"].as_i64().ok_or("CHILD_UNAVAILABLE")? as i32,
                )
                .ok_or("CHILD_UNAVAILABLE")?;
                let sig = if a["signal"] == 15 {
                    rustix::process::Signal::TERM
                } else {
                    rustix::process::Signal::KILL
                };
                match rustix::process::kill_process_group(pid, sig) {
                    Ok(()) | Err(rustix::io::Errno::SRCH) => Ok(Value::Null),
                    Err(_) => Err("CHILD_SIGNAL_UNCONFIRMED".into()),
                }
            }
            "child_wait" => {
                let deadline = Instant::now()
                    .checked_add(duration(seconds(&a["timeout"])?)?)
                    .ok_or("INVALID_TIME")?;
                loop {
                    if self
                        .child
                        .as_mut()
                        .ok_or("CHILD_UNAVAILABLE")?
                        .try_wait()
                        .map_err(|_| "CHILD_UNAVAILABLE")?
                        .is_some()
                    {
                        return Ok(json!(true));
                    }
                    let Some(left) = deadline.checked_duration_since(Instant::now()) else {
                        return Ok(json!(false));
                    };
                    std::thread::sleep(left.min(Duration::from_millis(10)));
                }
            }
            "child_clear" => {
                self.child = None;
                Ok(Value::Null)
            }
            "group_live" => invoke("tunnel.group_live", &json!({"pgid":a["pid"]})),
            "sleep" => {
                std::thread::sleep(duration(seconds(&a)?)?);
                Ok(Value::Null)
            }
            "stopped" => Ok(json!(self.stop.load(Ordering::Relaxed))),
            "wait" => {
                let deadline = Instant::now()
                    .checked_add(duration(seconds(&a)?)?)
                    .ok_or("INVALID_TIME")?;
                while !self.stop.load(Ordering::Relaxed) {
                    let Some(left) = deadline.checked_duration_since(Instant::now()) else {
                        break;
                    };
                    std::thread::sleep(left.min(Duration::from_millis(20)));
                }
                Ok(json!(self.stop.load(Ordering::Relaxed)))
            }
            "event" => {
                let mut event = s["state"].clone();
                for (k,v) in json!({"event":"TUNNEL_RELIABILITY","task_replay":false,"execution_authority_changed":false,"dispatch_verified":false}).as_object().unwrap() { event[k]=v.clone(); }
                println!("{event}");
                Ok(Value::Null)
            }
            _ => Err("TUNNEL_IO_UNKNOWN".into()),
        }
    }
}
pub fn main_cli(args: Vec<String>) -> i32 {
    if args.first().is_some_and(|a| a == "--owned-child") {
        if args.len() < 2
            || !Path::new(&args[1]).is_absolute()
            || rustix::process::setsid().is_err()
        {
            return 78;
        }
        let _ = Command::new(&args[1]).args(&args[2..]).exec();
        return 78;
    }
    if args.first().is_some_and(|a| a == "--supervisor-contract") {
        println!("RUST_TUNNEL_SUPERVISOR_V1");
        return 0;
    }
    if args.first().is_some_and(|a| a == "--rpc") {
        let mut input = String::new();
        let result = std::io::stdin()
            .read_line(&mut input)
            .map_err(|_| "INVALID_REQUEST".to_string())
            .and_then(|_| {
                serde_json::from_str::<Value>(&input).map_err(|_| "INVALID_REQUEST".to_string())
            })
            .and_then(|r| invoke(r["operation"].as_str().unwrap_or(""), &r["arguments"]));
        println!(
            "{}",
            match result {
                Ok(v) => json!({"result":v}),
                Err(e) => json!({"error":e}),
            }
        );
        return 0;
    }
    match cli(args) {
        Ok(n) => n,
        Err(e) => {
            println!(
                "{}",
                json!({"event":"TUNNEL_RELIABILITY","status":"HOLD","reason":if e=="CHILD_TERMINATION_UNCONFIRMED" {e.as_str()}else{"STATE_OR_PROCESS_OWNERSHIP_UNPROVEN"},"task_replay":false})
            );
            78
        }
    }
}
fn cli(args: Vec<String>) -> Result<i32> {
    let mode = args.first().ok_or("MODE_REQUIRED")?;
    let mut policy = default_policy();
    let mut directory = None;
    let mut url_file = None;
    let mut max_age = 120.;
    let mut timeout = 3.;
    let mut command = Vec::new();
    let mut i = 1;
    while i < args.len() {
        if args[i] == "--" {
            command = args[i + 1..].to_vec();
            break;
        }
        let key = args[i].strip_prefix("--").ok_or("INVALID_OPTION")?;
        let value = args.get(i + 1).ok_or("INVALID_OPTION")?;
        match key {
            "state-dir" => directory = Some(PathBuf::from(value)),
            "url-file" => url_file = Some(PathBuf::from(value)),
            "max-age" => max_age = value.parse::<f64>().map_err(|_| "INVALID_POLICY")?,
            "timeout" => timeout = value.parse::<f64>().map_err(|_| "INVALID_POLICY")?,
            _ => {
                let key = key.replace('-', "_");
                if policy.get(&key).is_none() {
                    return Err("INVALID_OPTION".into());
                }
                policy[&key] = if key == "max_restarts" {
                    json!(value.parse::<u64>().map_err(|_| "INVALID_RESTART_LIMIT")?)
                } else {
                    json!(value.parse::<f64>().map_err(|_| "INVALID_POLICY")?)
                };
            }
        }
        i += 2;
    }
    if mode == "probe" {
        let r = probe(&url_file.ok_or("HEALTH_URL_REQUIRED")?, max_age, timeout);
        match r {
            Ok(v) => {
                println!("{v}");
                return Ok(0);
            }
            Err(e) => {
                println!("{}", json!({"error":e}));
                return Ok(1);
            }
        }
    }
    let directory = std::path::absolute(directory.ok_or("PRIVATE_STATE_DIRECTORY_REQUIRED")?)
        .map_err(|_| "PRIVATE_STATE_DIRECTORY_REQUIRED")?;
    if mode == "status" {
        let report = invoke(
            "tunnel.status",
            &json!({"directory":directory,"now":wall()}),
        )
        .unwrap_or_else(
            |_| json!({"status":"UNKNOWN","currently_healthy":false,"dispatch_verified":false}),
        );
        println!("{report}");
        return Ok(if report["currently_healthy"] == true {
            0
        } else {
            1
        });
    }
    if mode != "run" {
        return Err("MODE_REQUIRED".into());
    }
    let command = json!(command);
    let config = json!({"directory":directory,"command":command,"identity":identity(&command,&policy),"policy":policy,"supervisor_pid":std::process::id(),"supervisor_start":process_start(&json!(std::process::id()))});
    let stop = Arc::new(AtomicBool::new(false));
    for sig in [signal_hook::consts::SIGTERM, signal_hook::consts::SIGINT] {
        signal_hook::flag::register(sig, Arc::clone(&stop))
            .map_err(|_| "SIGNAL_REGISTRATION_FAILED")?;
    }
    Engine::new(Native::new(stop), config)?.run()
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn python_identity_bytes_preserve_existing_budget() {
        assert_eq!(
            identity(&json!(["/bin/true", "\u{7f}"]), &default_policy()),
            "be999da4a9c8a8cb0d0a4374c3c1389707276960853609f5930d433fb22a940d"
        );
        assert_eq!(
            python_json(&json!(["abc",{"a":0.00001,"b":1.0,"c":"中😀"}])),
            "[\"abc\", {\"a\": 1e-05, \"b\": 1.0, \"c\": \"\\u4e2d\\ud83d\\ude00\"}]"
        );
    }
    #[test]
    fn chunked_probe_requires_complete_bounded_frame() {
        assert_eq!(
            decode_chunks(b"3\r\nabc\r\n0\r\n\r\n").unwrap(),
            Some(b"abc".to_vec())
        );
        assert!(decode_chunks(b"3\r\nabc\r\n0\r\n").unwrap().is_none());
        assert!(decode_chunks(b"100001\r\n").is_err());
    }
}

#[cfg(test)]
#[path = "tunnel_retirement_tests.rs"]
mod retirement_tests;
