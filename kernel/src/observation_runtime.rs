//! Native collector source IO and live-evidence cache. All reads use the existing
//! Provider, Authority and native history contracts; no execution or lease writes.
use crate::{
    native_history, native_observer_bridge,
    node_tls::Result,
    observation_source,
    provider::{Client, Provider},
    provider_client,
    read_projection::{now, text, truth},
};
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    fs::{self, File},
    io::{Read, Write},
    net::{SocketAddr, TcpStream},
    os::unix::{fs::MetadataExt, process::CommandExt},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{Mutex, mpsc},
    time::{Duration, Instant},
};

pub struct Source {
    config: Value,
    bridge: native_observer_bridge::Runtime,
    provider: provider_client::Runtime,
    cache: Mutex<VecDeque<(String, String, Value)>>,
    managed_unavailable: Mutex<bool>,
}
impl Source {
    pub fn new(mut config: Value) -> Result<Self> {
        config["rust_only"] = json!(config["native_home"].is_null());
        let root = config["native_home"].as_str().unwrap_or("/nonexistent");
        if !Path::new(root).is_absolute() {
            return Err("NATIVE_INDEX_UNAVAILABLE".into());
        }
        config["native_home"] = json!(crate::read_projection::resolve(Path::new(root)));
        Ok(Self {
            config,
            bridge: Default::default(),
            provider: Default::default(),
            cache: Default::default(),
            managed_unavailable: Mutex::new(false),
        })
    }
    fn history(&self, op: &str, a: &Value) -> Result<Value> {
        let mut a = a.clone();
        a["root"] = self.config["native_home"].clone();
        a["node_id"] = self.config["node_id"].clone();
        native_history::invoke(&format!("history.{op}"), &a)
    }
    fn legacy(&self, op: &str, a: &Value) -> Result<Value> {
        let mut a = a.clone();
        a["owner_host"] = self.config["owner_host"].clone();
        a["canonical_path"] = self.config["canonical_path"].clone();
        a["native_index"] =
            json!(Path::new(text(&self.config["native_home"])).join("state_5.sqlite"));
        observation_source::invoke(&format!("source.{op}"), &a, |_, _| {
            Err("OBSERVATION_CALLBACK_INVALID".into())
        })
    }
    fn authority(&self, op: &str, args: Value) -> Result<Value> {
        let c = &self.config["authority"];
        let mut nonce = [0u8; 16];
        File::open("/dev/urandom")
            .and_then(|mut f| f.read_exact(&mut nonce))
            .map_err(|_| "OBSERVATION_SOURCE_UNAVAILABLE")?;
        let request_id = nonce.iter().map(|b| format!("{b:02x}")).collect::<String>();
        crate::authority_client::request(&json!({"endpoint":c["endpoint"],"authority_uid":c["uid"],"timeout":c.get("timeout").cloned().unwrap_or(json!(5)),"wire_operation":if op=="health" {op.to_owned()}else{format!("business.{op}")},"payload":args,"request_id":if op=="health"{"observation-generation"}else{&request_id}}))
            .map_err(|e|format!("SOURCE_AUTHORITY_ERROR:{e}"))
    }
    fn managed(&self, op: &str, a: &Value) -> Result<Value> {
        let mut a = a.clone();
        a["owner_host"] = self.config["owner_host"].clone();
        a["rust_only"] = json!(self.config["rust_only"] == true);
        a["remote"] = json!(self.config["remote_native"].is_object());
        a["managed_inventory_unavailable"] = json!(
            *self
                .managed_unavailable
                .lock()
                .map_err(|_| "OBSERVATION_SOURCE_UNAVAILABLE")?
        );
        self.bridge.invoke(
            &format!("native_observer.managed_source_{op}"),
            &a,
            |op, args| match op {
                "managed.health" => self.authority("health", json!({})),
                "managed.command" => {
                    let mut v =
                        self.authority(text(&args["operation"]), args["arguments"].clone())?;
                    if args["operation"] == "observation_turns" {
                        for row in v["executions"]
                            .as_array_mut()
                            .ok_or("INVALID_OBSERVATION")?
                        {
                            row["created_at_ordinal"] =
                                json!(native_history::order(&row["created_at"], 0));
                        }
                    }
                    Ok(v)
                }
                "managed.base_generation" => self.legacy("generation", &args),
                "managed.base_inventory" => self.history("inventory", &args),
                "managed.base_native_turns" => self.history("native_turns", &args),
                "managed.base_sample" => self.base_sample(&args),
                "managed.native_inventory" => self.remote("inventory", &args),
                "managed.native_turns" => self.remote("turns", &args),
                _ => Err("NATIVE_OBSERVER_CALLBACK_INVALID".into()),
            },
        )
    }
    fn remote(&self, op: &str, a: &Value) -> Result<Value> {
        self.bridge.invoke(
            &format!("native_observer.remote_{op}"),
            a,
            |op, args| match op {
                "native_observer.read" => self.remote("read", &args),
                "native_observer.http" => Ok(remote_http(&self.config["remote_native"], &args)
                    .unwrap_or_else(|_| json!({"transport_error":true}))),
                _ => Err("NATIVE_OBSERVER_CALLBACK_INVALID".into()),
            },
        )
    }
    fn base_sample(&self, a: &Value) -> Result<Value> {
        let mut a = a.clone();
        a["canonical_db"] = json!(truth(&self.config["canonical_path"]));
        a["live_enabled"] = json!(
            truth(&self.config["provider_config"])
                || self.config["native_writer_observation"] == true
        );
        observation_source::invoke("source.sample", &a, |op, args| match op {
            "source.native_turns" => self.invoke("native_turns", &args),
            "source.canonical_turns" => self.invoke("canonical_turns", &args),
            "source.generation" => self.invoke("generation", &args),
            "source.observe_live" => self.observe_live(&args),
            _ => Err("OBSERVATION_CALLBACK_INVALID".into()),
        })
    }
    pub fn invoke(&self, op: &str, a: &Value) -> Result<Value> {
        if op == "observe_live" {
            return self.observe_live(a);
        }
        if self.config["managed"] == true {
            let value = self.managed(op, a)?;
            if op == "canonical_inventory" {
                *self
                    .managed_unavailable
                    .lock()
                    .map_err(|_| "OBSERVATION_SOURCE_UNAVAILABLE")? = value[1] == true;
                return Ok(value[0].clone());
            }
            return Ok(value);
        }
        match op {
            "generation" | "canonical_inventory" | "canonical_turns" => self.legacy(op, a),
            "inventory" | "native_turns" => self.history(op, a),
            "sample" => self.base_sample(a),
            _ => Err("OBSERVATION_SOURCE_OPERATION_UNKNOWN".into()),
        }
    }
    fn observe_live(&self, a: &Value) -> Result<Value> {
        self.observe_live_with(a, |_, _| Err("OBSERVATION_CALLBACK_INVALID".into()))
    }
    fn observe_live_with(
        &self,
        a: &Value,
        mut callback: impl FnMut(&str, Value) -> Result<Value>,
    ) -> Result<Value> {
        let tid = text(&a["tid"]);
        let project = text(&a["project"]);
        let turn = &a["turn_id"];
        let stamp = now();
        let mut cache = self
            .cache
            .lock()
            .map_err(|_| "OBSERVATION_SOURCE_UNAVAILABLE")?;
        if let Some((_, _, v)) = cache.iter().find(|(t, p, v)| {
            t == tid
                && p == project
                && v["turn_id"] == *turn
                && v["observed_at"]
                    .as_f64()
                    .is_some_and(|at| stamp >= at && stamp - at < 10.)
        }) {
            return Ok(v.clone());
        }
        let evidence = if self.config["native_writer_observation"] == true {
            let mut evidence = native_writer(
                &json!({"root":self.config["native_home"],"thread_id":tid,"turn_id":turn,"project":project}),
            )?;
            if evidence["liveness"] != "LIVE" && truth(&self.config["native_observer_socket"]) {
                evidence = self.probe_writer(a).unwrap_or_else(
                    |_| json!({"liveness":"UNKNOWN","reason":"NATIVE_WRITER_PROBE_UNAVAILABLE"}),
                );
            }
            evidence
        } else {
            let config = &self.config["provider_config"]["app_server"];
            let endpoints =
                crate::runtime_config::provider_endpoints(&self.config["provider_config"])?;
            let observed = if self.config["injected_provider"] == true {
                callback("source_runtime.observe_thread", json!({"tid":tid}))?
            } else {
                self.provider.invoke("provider.client.observation.threads",&json!({"thread_id":tid,"endpoints":endpoints,"native_transport":true,"client_info":{"name":config["client_name"],"title":config["client_title"],"version":config["client_version"]}}),|_,_|Err("OBSERVATION_CALLBACK_INVALID".into()))?
            };
            let rows = &observed["observations"];
            let evidence = self.provider.invoke(
                "provider.client.observation.classify",
                &json!({"observations":rows,"thread_id":tid,"turn_id":turn}),
                |_, _| Err("OBSERVATION_CALLBACK_INVALID".into()),
            )?;
            let owner = rows
                .as_array()
                .ok_or("INVALID_OBSERVATION")?
                .iter()
                .find(|r| r["endpoint"] == evidence["owner_endpoint"]);
            let same = owner.is_some_and(|r| r["cwd"] == project);
            json!({"liveness":if evidence["provider_liveness"]=="LIVE"&&same{"LIVE"}else{"UNKNOWN"},"reason":if same||owner.is_none(){evidence["liveness_reason"].clone()}else{json!("WORKSPACE_MISMATCH")}})
        };
        let proof = json!({"thread_id":tid,"turn_id":turn,"observed_at":stamp,"liveness":evidence["liveness"],"reason":evidence["reason"]});
        if let Some(i) = cache.iter().position(|(t, p, _)| t == tid && p == project) {
            cache.remove(i);
        }
        if cache.len() >= 128 {
            cache.pop_front();
        }
        cache.push_back((tid.into(), project.into(), proof.clone()));
        Ok(proof)
    }
    fn probe_writer(&self, a: &Value) -> Result<Value> {
        self.probe_writer_with(a, |_, _| Err("NATIVE_WRITER_PROBE_UNAVAILABLE".into()))
    }
    fn probe_writer_with(
        &self,
        a: &Value,
        mut call: impl FnMut(&str, Value) -> Result<Value>,
    ) -> Result<Value> {
        let injected = self.config["injected_probe"] == true;
        let root = Path::new(text(&self.config["native_home"]));
        let own = if injected {
            PathBuf::from(text(&self.config["injected_home"])).join(".codex")
        } else {
            expand("~/.codex")
        };
        let uid = rustix::process::getuid().as_raw();
        let expected = if uid != 0
            && fs::canonicalize(root).ok() == fs::canonicalize(&own).ok()
            && fs::metadata(&own).is_ok_and(|m| m.uid() == uid)
        {
            uid
        } else if injected {
            u32::try_from(
                call("writer_probe.worker_uid", json!({}))?
                    .as_u64()
                    .ok_or("NATIVE_WRITER_PROBE_UNAVAILABLE")?,
            )
            .map_err(|_| "NATIVE_WRITER_PROBE_UNAVAILABLE")?
        } else {
            crate::workspace_probe::worker_uid()?
        };
        let mut client = if injected {
            None
        } else {
            Some(Client::connect_uninitialized(
                Path::new(text(&self.config["native_observer_socket"])),
                Duration::from_secs(3),
            )?)
        };
        let peer = if let Some(c) = client.as_ref() {
            c.local_peer_uid()?
        } else {
            u32::try_from(
                call(
                    "writer_probe.open",
                    json!({"endpoint":self.config["native_observer_socket"]}),
                )?
                .as_u64()
                .ok_or("NATIVE_WRITER_PROBE_UNAVAILABLE")?,
            )
            .map_err(|_| "NATIVE_WRITER_PROBE_UNAVAILABLE")?
        };
        if peer != expected {
            return Err("NATIVE_WRITER_PROBE_UNAVAILABLE".into());
        }
        let info = json!({"name":"clinx-native-writer-observer","title":"CLINX read-only writer evidence","version":"1"});
        if let Some(c) = client.as_mut() {
            c.request(
                "initialize",
                json!({"clientInfo":info,"capabilities":{"experimentalApi":true}}),
            )?;
            c.send_message(&json!({"method":"initialized"}))?;
        } else {
            call("writer_probe.initialize", info)?;
        }
        let program = self.config["probe_program"]
            .as_str()
            .map(PathBuf::from)
            .or_else(|| std::env::current_exe().ok())
            .ok_or("NATIVE_WRITER_PROBE_UNAVAILABLE")?;
        if !program.is_absolute() {
            return Err("NATIVE_WRITER_PROBE_UNAVAILABLE".into());
        }
        let request =
            json!({"root":root,"thread_id":a["tid"],"turn_id":a["turn_id"],"project":a["project"]});
        let command = if injected && self.config["injected_legacy_script"].is_string() {
            json!([
                "/usr/bin/python3",
                "-I",
                self.config["injected_legacy_script"],
                request.to_string()
            ])
        } else {
            json!([program, "--native-writer-probe", request.to_string()])
        };
        let params = json!({"command":command,"cwd":"/","timeoutMs":2500,"outputBytesCap":4096,"sandboxPolicy":{"type":"dangerFullAccess"}});
        let result = if let Some(c) = client.as_mut() {
            c.request("command/exec", params)?
        } else {
            call("writer_probe.request", params)?
        };
        let stdout = result["stdout"]
            .as_str()
            .filter(|s| s.len() <= 4096)
            .ok_or("NATIVE_WRITER_PROBE_UNAVAILABLE")?;
        let value: Value =
            serde_json::from_str(stdout).map_err(|_| "NATIVE_WRITER_PROBE_UNAVAILABLE")?;
        if result["exitCode"] != 0
            || value["observer_uid"] != expected
            || !matches!(text(&value["liveness"]), "LIVE" | "UNKNOWN")
            || (value["liveness"] == "LIVE" && !value["writer_pid"].as_u64().is_some_and(|n| n > 0))
        {
            return Err("NATIVE_WRITER_PROBE_UNAVAILABLE".into());
        }
        Ok(Value::Object(
            ["liveness", "reason", "writer_pid"]
                .iter()
                .filter_map(|k| value.get(*k).map(|v| ((*k).into(), v.clone())))
                .collect(),
        ))
    }
}
fn expand(p: &str) -> PathBuf {
    if p == "~" || p.starts_with("~/") {
        PathBuf::from(std::env::var_os("HOME").unwrap_or_default())
            .join(p.strip_prefix("~/").unwrap_or(""))
    } else {
        p.into()
    }
}
fn native_writer(a: &Value) -> Result<Value> {
    let mut a = a.clone();
    a["platform"] = json!(if cfg!(target_os = "macos") {
        "darwin"
    } else {
        std::env::consts::OS
    });
    provider_client::Runtime::default().invoke(
        "provider.client.observation.writer",
        &a,
        |op, args| {
            if op != "provider.client.observation" || args["operation"] != "command" {
                return Err("NATIVE_WRITER_PROBE_INVALID".into());
            }
            Ok(json!({"result":read_command(&args["arguments"]["command"])?}))
        },
    )
}
pub fn inspect_writer(a: &Value) -> Value {
    let result = (|| -> Result<Value> {
        let root = Path::new(text(&a["root"]));
        let expected = std::env::var_os("CODEX_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| expand("~/.codex"));
        if !root.is_absolute()
            || fs::canonicalize(root).map_err(|_| "NATIVE_HOME_IDENTITY_CONFLICT")?
                != fs::canonicalize(expected).map_err(|_| "NATIVE_HOME_IDENTITY_CONFLICT")?
        {
            return Err("NATIVE_HOME_IDENTITY_CONFLICT".into());
        }
        let tid = text(&a["thread_id"]).as_bytes();
        if tid.len() != 36
            || tid.iter().enumerate().any(|(i, b)| {
                if [8, 13, 18, 23].contains(&i) {
                    *b != b'-'
                } else {
                    !b.is_ascii_digit() && !(b'a'..=b'f').contains(b)
                }
            })
        {
            return Err("INVALID_THREAD_ID".into());
        }
        native_writer(a)
    })();
    let mut result =
        result.unwrap_or_else(|_| json!({"liveness":"UNKNOWN","reason":"NATIVE_WRITER_UNPROVEN"}));
    result["observer_uid"] = json!(rustix::process::getuid().as_raw());
    result
}
fn read_command(a: &Value) -> Result<Value> {
    let args = a.as_array().ok_or("NATIVE_WRITER_PROBE_INVALID")?;
    let program = args
        .first()
        .and_then(Value::as_str)
        .ok_or("NATIVE_WRITER_PROBE_INVALID")?;
    if !["/usr/sbin/lsof", "/bin/ps"].contains(&program) {
        return Err("NATIVE_WRITER_PROBE_INVALID".into());
    }
    let mut child = Command::new(program)
        .args(args.iter().skip(1).map(text))
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .process_group(0)
        .spawn()
        .map_err(|_| "NATIVE_WRITER_UNPROVEN")?;
    let group = rustix::process::Pid::from_child(&child);
    let mut pipe = child.stdout.take().ok_or("NATIVE_WRITER_UNPROVEN")?;
    let (tx, rx) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut b = Vec::new();
        let r = pipe.by_ref().take(32769).read_to_end(&mut b);
        let _ = tx.send(r.map(|_| b));
    });
    let deadline = Instant::now() + Duration::from_secs(2);
    let mut body = None;
    let mut status = None;
    let result = loop {
        if body.is_none() {
            match rx.try_recv() {
                Ok(Ok(b)) if b.len() <= 32768 => body = Some(b),
                Ok(_) | Err(mpsc::TryRecvError::Disconnected) => {
                    break Err("NATIVE_WRITER_UNPROVEN".into());
                }
                Err(mpsc::TryRecvError::Empty) => (),
            }
        }
        match child.try_wait() {
            Ok(Some(s)) => status = Some(s),
            Ok(None) => (),
            Err(_) => break Err("NATIVE_WRITER_UNPROVEN".into()),
        }
        if let (Some(body), Some(status)) = (&body, status) {
            break String::from_utf8(body.clone()).map(|stdout|json!({"returncode":status.code().unwrap_or(-1),"argv":shell_words(&stdout),"stdout":stdout})).map_err(|_|"NATIVE_WRITER_UNPROVEN".into());
        }
        if Instant::now() >= deadline {
            break Err("NATIVE_WRITER_UNPROVEN".into());
        }
        std::thread::sleep(Duration::from_millis(5));
    };
    let _ = rustix::process::kill_process_group(group, rustix::process::Signal::KILL);
    let _ = child.wait();
    result
}
fn shell_words(s: &str) -> Value {
    let mut words = Vec::new();
    let mut word = String::new();
    let mut quote = None;
    let mut escape = false;
    let mut started = false;
    for c in s.chars() {
        if escape {
            word.push(c);
            escape = false;
            started = true;
            continue;
        }
        if c == '\\' && quote != Some('\'') {
            escape = true;
            continue;
        }
        if let Some(q) = quote {
            if c == q {
                quote = None;
            } else {
                word.push(c);
            }
            started = true;
        } else if c == '\'' || c == '"' {
            quote = Some(c);
            started = true;
        } else if c.is_whitespace() {
            if started {
                words.push(std::mem::take(&mut word));
                started = false;
            }
        } else {
            word.push(c);
            started = true;
        }
    }
    if quote.is_some() || escape {
        return Value::Null;
    }
    if started {
        words.push(word);
    }
    json!(words)
}
fn encode(s: &str) -> String {
    s.bytes()
        .map(|b| {
            if b.is_ascii_alphanumeric() || b"-._~".contains(&b) {
                (b as char).to_string()
            } else if b == b' ' {
                "+".into()
            } else {
                format!("%{b:02X}")
            }
        })
        .collect()
}
fn remote_http(config: &Value, a: &Value) -> Result<Value> {
    let path = text(&a["path"]);
    if path != "inventory"
        && !path
            .strip_prefix("sample/")
            .is_some_and(|s| s.len() == 36 && s.bytes().all(|b| b.is_ascii_hexdigit() || b == b'-'))
    {
        return Err("NATIVE_OBSERVER_UNAVAILABLE".into());
    }
    loopback_http(
        config,
        &format!("/v2/native-collector/{path}"),
        &a["params"],
        Duration::from_secs(8),
    )
}
/// Fixed loopback native read transport shared by Collector and Desktop reads.
/// Callers provide allowlisted route segments; credentials never leave this IO.
pub(crate) fn loopback_http(
    config: &Value,
    path: &str,
    params: &Value,
    timeout: Duration,
) -> Result<Value> {
    if !path.starts_with("/v2/")
        || !path
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"/_-".contains(&b))
    {
        return Err("NATIVE_OBSERVER_UNAVAILABLE".into());
    }
    let mut token = String::new();
    File::open(text(&config["credential_file"]))
        .map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")?
        .take(257)
        .read_to_string(&mut token)
        .map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")?;
    let token = token.trim();
    if !(32..=256).contains(&token.len())
        || !token
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
    {
        return Err("NATIVE_OBSERVER_UNAVAILABLE".into());
    }
    let query = params
        .as_object()
        .ok_or("NATIVE_OBSERVER_UNAVAILABLE")?
        .iter()
        .map(|(k, v)| {
            format!(
                "{}={}",
                encode(k),
                encode(&if let Some(s) = v.as_str() {
                    s.to_owned()
                } else {
                    v.to_string()
                })
            )
        })
        .collect::<Vec<_>>()
        .join("&");
    let port = config["port"].as_u64().unwrap_or(8766);
    let port = u16::try_from(port).map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")?;
    let deadline = Instant::now() + timeout;
    let mut socket = TcpStream::connect_timeout(&SocketAddr::from(([127, 0, 0, 1], port)), timeout)
        .map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")?;
    socket
        .set_write_timeout(Some(timeout))
        .map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")?;
    write!(socket,"GET {path}?{query} HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {token}\r\nConnection: close\r\n\r\n").map_err(|_|"NATIVE_OBSERVER_UNAVAILABLE")?;
    let mut data = Vec::new();
    let mut header_end = None;
    let mut status = 0;
    let mut length = None;
    let mut limit = 1048577;
    loop {
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .filter(|d| !d.is_zero())
            .ok_or("NATIVE_OBSERVER_UNAVAILABLE")?;
        socket
            .set_read_timeout(Some(remaining))
            .map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")?;
        let mut part = [0u8; 8192];
        let n = socket
            .read(&mut part)
            .map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")?;
        if n == 0 {
            break;
        }
        data.extend_from_slice(&part[..n]);
        if header_end.is_none() {
            if let Some(i) = data.windows(4).position(|w| w == b"\r\n\r\n") {
                if i > 65536 {
                    return Err("NATIVE_OBSERVER_UNAVAILABLE".into());
                }
                let h =
                    std::str::from_utf8(&data[..i]).map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")?;
                let mut lines = h.split("\r\n");
                status = lines
                    .next()
                    .and_then(|s| s.split_whitespace().nth(1))
                    .and_then(|s| s.parse::<u16>().ok())
                    .ok_or("NATIVE_OBSERVER_UNAVAILABLE")?;
                for line in lines {
                    let (k, v) = line.split_once(':').ok_or("NATIVE_OBSERVER_UNAVAILABLE")?;
                    if k.eq_ignore_ascii_case("content-length") {
                        if length.is_some() {
                            return Err("NATIVE_OBSERVER_UNAVAILABLE".into());
                        }
                        length = Some(
                            v.trim()
                                .parse::<usize>()
                                .map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")?,
                        );
                    }
                    if k.eq_ignore_ascii_case("transfer-encoding") {
                        return Err("NATIVE_OBSERVER_UNAVAILABLE".into());
                    }
                }
                limit = if status == 200 { 1048577 } else { 4096 };
                header_end = Some(i + 4);
            } else if data.len() > 65536 {
                return Err("NATIVE_OBSERVER_UNAVAILABLE".into());
            }
        }
        if let Some(start) = header_end
            && (data.len() - start >= limit || length.is_some_and(|len| data.len() - start >= len))
        {
            break;
        }
    }
    let start = header_end.ok_or("NATIVE_OBSERVER_UNAVAILABLE")?;
    if length.is_some_and(|len| len <= limit && data.len() - start < len) {
        return Err("NATIVE_OBSERVER_UNAVAILABLE".into());
    }
    let body = String::from_utf8(data[start..].iter().take(limit).copied().collect())
        .map_err(|_| "NATIVE_OBSERVER_UNAVAILABLE")?;
    Ok(json!({"status":status,"body":body}))
}

#[derive(Default)]
pub struct Runtime(Mutex<Option<Source>>);
impl Runtime {
    pub fn invoke(
        &self,
        op: &str,
        a: &Value,
        call: impl FnMut(&str, Value) -> Result<Value>,
    ) -> Result<Value> {
        if op == "source_runtime.inspect_writer" {
            return Ok(inspect_writer(a));
        }
        if op == "source_runtime.native_writer" {
            return native_writer(a);
        }
        let mut source = self
            .0
            .lock()
            .map_err(|_| "OBSERVATION_SOURCE_UNAVAILABLE")?;
        if op == "source_runtime.init" {
            *source = Some(Source::new(a["config"].clone())?);
            return Ok(Value::Null);
        }
        if op == "source_runtime.writer_probe" {
            return Ok(source
                .as_ref()
                .ok_or("OBSERVATION_SOURCE_UNAVAILABLE")?
                .probe_writer_with(a, call)
                .unwrap_or_else(
                    |_| json!({"liveness":"UNKNOWN","reason":"NATIVE_WRITER_PROBE_UNAVAILABLE"}),
                ));
        }
        if op == "source_runtime.observe_live" {
            return source
                .as_ref()
                .ok_or("OBSERVATION_SOURCE_UNAVAILABLE")?
                .observe_live_with(a, call);
        }
        source
            .as_ref()
            .ok_or("OBSERVATION_SOURCE_UNAVAILABLE")?
            .invoke(
                op.strip_prefix("source_runtime.")
                    .ok_or("OBSERVATION_SOURCE_OPERATION_UNKNOWN")?,
                a,
            )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn live_cache_bounds_exact_identity_expiry_and_restart() {
        let source =
            Source::new(json!({"native_home":"/nonexistent","injected_provider":true})).unwrap();
        let mut probes = 0;
        let mut read = |_: &str, a: Value| {
            probes += 1;
            Ok(
                json!({"observations":[{"endpoint":"fixture","state":"active","thread_id":a["tid"],"turn_id":"turn","turn_status":"inProgress","cwd":"/approved"}]}),
            )
        };
        let a = json!({"tid":"exact","turn_id":"turn","project":"/approved"});
        let first = source.observe_live_with(&a, &mut read).unwrap();
        assert_eq!(first["liveness"], "LIVE");
        assert_eq!(source.observe_live_with(&a, &mut read).unwrap(), first);
        source.cache.lock().unwrap()[0].2["observed_at"] = json!(now() - 11.);
        source.observe_live_with(&a, &mut read).unwrap();
        assert_eq!(probes, 2);
        let changed=source.observe_live_with(&json!({"tid":"exact","turn_id":"turn","project":"/other"}),|_,args|Ok(json!({"observations":[{"endpoint":"fixture","state":"active","thread_id":args["tid"],"turn_id":"turn","turn_status":"inProgress","cwd":"/approved"}]}))).unwrap();
        assert_eq!(changed["liveness"], "UNKNOWN");
        assert_eq!(changed["reason"], "WORKSPACE_MISMATCH");
        for n in 0..140 {
            source
                .observe_live_with(
                    &json!({"tid":format!("thread-{n}"),"turn_id":"turn","project":"/approved"}),
                    |_, _| Ok(json!({"observations":[]})),
                )
                .unwrap();
        }
        assert_eq!(source.cache.lock().unwrap().len(), 128);
        assert!(
            Source::new(json!({"native_home":"/nonexistent"}))
                .unwrap()
                .cache
                .lock()
                .unwrap()
                .is_empty()
        );
    }
    #[test]
    fn remote_probe_total_deadline_rejects_trickling_http() {
        use std::net::TcpListener;
        let temp = tempfile::tempdir().unwrap();
        let credential = temp.path().join("credential");
        fs::write(&credential, "x".repeat(40)).unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let peer = std::thread::spawn(move || {
            let (mut peer, _) = listener.accept().unwrap();
            let mut request = [0; 1024];
            let _ = peer.read(&mut request);
            let _ = peer.write_all(b"HTTP/1.0 200 OK\r\nContent-Length: 1000\r\n\r\n");
            for _ in 0..400 {
                if peer.write_all(b" ").is_err() {
                    break;
                }
                std::thread::sleep(Duration::from_millis(30));
            }
        });
        let started = Instant::now();
        assert!(
            remote_http(
                &json!({"credential_file":credential,"port":port}),
                &json!({"path":"inventory","params":{}})
            )
            .is_err()
        );
        assert!(started.elapsed() < Duration::from_secs(10));
        peer.join().unwrap();
    }
}
