//! Exact provider and local writer evidence. Read-only sources are rechecked;
//! uncertainty never creates authority, completion or cancellation permission.
use super::*;
use crate::{native_history::History, read_projection::resolve};
use std::{
    collections::BTreeSet,
    fs::{self, File},
    io::Read,
    os::unix::fs::MetadataExt,
};

fn normalized(v: &Value) -> String {
    let value = if v.is_object() {
        ["type", "status", "state"]
            .iter()
            .map(|k| &v[k])
            .find(|v| truthy(v))
            .unwrap_or(&Value::Null)
    } else {
        v
    };
    if !truthy(value) {
        return "unknown".into();
    }
    value
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| value.to_string())
        .replace(['_', '-'], "")
        .to_lowercase()
}
fn active(v: &Value) -> bool {
    matches!(normalized(v).as_str(), "inprogress" | "running" | "active")
}
fn terminal(v: &Value) -> bool {
    matches!(
        normalized(v).as_str(),
        "completed"
            | "succeeded"
            | "success"
            | "failed"
            | "error"
            | "systemerror"
            | "errored"
            | "timeout"
            | "timedout"
            | "disconnected"
            | "connectionlost"
            | "connectionclosed"
            | "cancelled"
            | "canceled"
            | "interrupted"
            | "aborted"
    )
}
pub fn classify(rows: &[Value], tid: &Value, turn: &Value) -> Value {
    let unavailable: Vec<_> = rows
        .iter()
        .filter(|r| {
            matches!(r["state"].as_str(), Some("UNKNOWN" | "OFFLINE"))
                || truthy(&r["transport_error"])
        })
        .collect();
    let health = if rows.is_empty() || unavailable.len() == rows.len() {
        "UNAVAILABLE"
    } else if !unavailable.is_empty() {
        "DEGRADED"
    } else {
        "HEALTHY"
    };
    let claims: Vec<_> = rows.iter().filter(|r| active(&r["state"])).collect();
    let exact: Vec<_> = rows
        .iter()
        .filter(|r| &r["thread_id"] == tid && &r["turn_id"] == turn && truthy(turn))
        .collect();
    let live: Vec<_> = exact
        .iter()
        .copied()
        .filter(|r| active(&r["state"]) && active(&r["turn_status"]))
        .collect();
    let conflict = claims.len() > 1;
    let (mut owner, mut liveness, mut reason) = (None, "UNKNOWN", "OWNER_UNCONFIRMED");
    if conflict {
        reason = "OWNERSHIP_CONFLICT";
    } else if live.len() == 1 {
        owner = Some(live[0]);
        liveness = "LIVE";
        reason = "EXACT_OWNER_ACTIVE";
    } else if !claims.is_empty() {
        reason = "ACTIVE_OWNER_EXACT_TURN_UNCONFIRMED";
    } else {
        let loaded: Vec<_> = rows
            .iter()
            .filter(|r| {
                !matches!(
                    r["state"].as_str(),
                    Some("UNKNOWN" | "OFFLINE" | "notLoaded" | "unloaded")
                )
            })
            .collect();
        let terminals: Vec<_> = exact
            .iter()
            .copied()
            .filter(|r| r["state"] == "idle" && terminal(&r["turn_status"]))
            .collect();
        if loaded.len() == 1 && terminals.len() == 1 {
            owner = Some(terminals[0]);
            liveness = "TERMINAL";
            reason = "EXACT_OWNER_TERMINAL";
        } else if !rows.is_empty()
            && unavailable.is_empty()
            && exact.len() == rows.len()
            && exact.iter().all(|r| {
                matches!(r["state"].as_str(), Some("notLoaded" | "unloaded"))
                    && normalized(&r["turn_status"]) == "completed"
            })
        {
            owner = Some(exact[0]);
            liveness = "TERMINAL";
            reason = "EXACT_COMPLETED_TURN_UNLOADED";
        }
    }
    json!({"provider_liveness":liveness,"transport_health":health,"liveness_reason":reason,
        "owner_endpoint":owner.map(|r|r["endpoint"].clone()),"ownership_conflict":conflict,
        "release_safe":liveness=="TERMINAL" && unavailable.is_empty() && claims.is_empty() && !conflict,"observations":rows})
}
fn call<F: FnMut(&str, Value) -> Result<Value>>(
    callback: &mut F,
    a: &Value,
    op: &str,
    args: Value,
) -> Result<Value> {
    let value = callback(
        "provider.client.observation",
        json!({"token":a["callback_token"],"operation":op,"arguments":args}),
    )?;
    if let Some(error) = value["error"].as_str() {
        return Err(error.to_owned());
    }
    Ok(value["result"].clone())
}
fn bounded(path: &Path, limit: usize) -> Result<Vec<u8>> {
    let mut data = Vec::new();
    File::open(path)
        .map_err(|_| "NATIVE_WRITER_UNPROVEN")?
        .take((limit + 1) as u64)
        .read_to_end(&mut data)
        .map_err(|_| "NATIVE_WRITER_UNPROVEN")?;
    if data.len() > limit {
        return Err("NATIVE_WRITER_UNPROVEN".into());
    }
    Ok(data)
}
fn identity(path: &Path) -> Result<(u64, u64)> {
    let m = fs::metadata(path).map_err(|_| "NATIVE_WRITER_UNPROVEN")?;
    Ok((m.dev(), m.ino()))
}
fn owners(root: &Path, lock: &Path) -> Result<BTreeSet<u32>> {
    let data = bounded(&root.join("locks"), 1024 * 1024)?;
    let (dev, inode) = identity(lock)?;
    // MetadataExt widens dev_t to u64; Darwin rustix expects signed dev_t.
    // Reject an unrepresentable identity rather than truncate or alias it.
    #[cfg(target_vendor = "apple")]
    let dev = i32::try_from(dev).map_err(|_| "NATIVE_WRITER_UNPROVEN")?;
    let mut owners = BTreeSet::new();
    for line in std::str::from_utf8(&data)
        .map_err(|_| "NATIVE_WRITER_UNPROVEN")?
        .lines()
    {
        let p: Vec<_> = line.split_whitespace().collect();
        if p.len() != 8 || p[1..4] != ["FLOCK", "ADVISORY", "WRITE"] || p[6..] != ["0", "EOF"] {
            continue;
        }
        let v: Vec<_> = p[5].split(':').collect();
        if v.len() != 3 {
            return Err("NATIVE_WRITER_UNPROVEN".into());
        }
        if u32::from_str_radix(v[0], 16).ok() == Some(rustix::fs::major(dev))
            && u32::from_str_radix(v[1], 16).ok() == Some(rustix::fs::minor(dev))
            && v[2].parse::<u64>().ok() == Some(inode)
        {
            owners.insert(p[4].parse().map_err(|_| "NATIVE_WRITER_UNPROVEN")?);
        }
    }
    Ok(owners)
}
fn start_time(stat: &[u8]) -> Option<&[u8]> {
    stat.splitn(2, |b| *b == b')')
        .nth(1)?
        .split(|b| b.is_ascii_whitespace())
        .filter(|v| !v.is_empty())
        .nth(19)
}
fn linux_writer<F: FnMut(&str, Value) -> Result<Value>>(
    a: &Value,
    callback: &mut F,
) -> Result<Option<u32>> {
    let root = Path::new(a["proc_root"].as_str().unwrap_or("/proc"));
    let lock = Path::new(a["lock"].as_str().ok_or("NATIVE_WRITER_UNPROVEN")?);
    let rollout = Path::new(a["rollout"].as_str().ok_or("NATIVE_WRITER_UNPROVEN")?);
    let pids = owners(root, lock)?;
    if pids.len() != 1 || pids.contains(&0) {
        return Ok(None);
    }
    let pid = *pids.first().unwrap();
    let proc = root.join(pid.to_string());
    let mut read_stat = |p: &Path| -> Result<Vec<u8>> {
        if a["stat_callback"] == true {
            let encoded = call(callback, a, "read_bytes", json!({"path":p}))?;
            use base64::Engine;
            base64::engine::general_purpose::STANDARD
                .decode(encoded.as_str().ok_or("NATIVE_WRITER_UNPROVEN")?)
                .map_err(|_| "NATIVE_WRITER_UNPROVEN".into())
        } else {
            bounded(p, 65536)
        }
    };
    let start = read_stat(&proc.join("stat"))?;
    let exe = fs::read_link(proc.join("exe")).map_err(|_| "NATIVE_WRITER_UNPROVEN")?;
    if fs::metadata(&proc)
        .map_err(|_| "NATIVE_WRITER_UNPROVEN")?
        .uid()
        != rustix::process::getuid().as_raw()
        || exe.file_name().and_then(|v| v.to_str()) != Some("codex")
    {
        return Ok(None);
    }
    let cmd = bounded(&proc.join("cmdline"), 65536)?;
    let args: Vec<_> = cmd.split(|b| *b == 0).collect();
    if args.is_empty()
        || Path::new(std::str::from_utf8(args[0]).map_err(|_| "NATIVE_WRITER_UNPROVEN")?)
            .file_name()
            .and_then(|n| n.to_str())
            != Some("codex")
        || !args.iter().any(|a| matches!(*a, b"exec" | b"app-server"))
    {
        return Ok(None);
    }
    let expected = BTreeSet::from([identity(lock)?, identity(rollout)?]);
    let mut writable = BTreeSet::new();
    for (i, entry) in fs::read_dir(proc.join("fd"))
        .map_err(|_| "NATIVE_WRITER_UNPROVEN")?
        .enumerate()
    {
        if i >= 1024 {
            return Ok(None);
        }
        let fd = entry.map_err(|_| "NATIVE_WRITER_UNPROVEN")?;
        let info = match fs::read_to_string(proc.join("fdinfo").join(fd.file_name())) {
            Ok(v) => v,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(_) => return Ok(None),
        };
        let Some(flags) = info
            .lines()
            .find_map(|l| l.strip_prefix("flags:"))
            .and_then(|s| u64::from_str_radix(s.trim(), 8).ok())
        else {
            return Ok(None);
        };
        if matches!(flags & 3, 1 | 2) {
            match identity(&fd.path()) {
                Ok(id) => {
                    writable.insert(id);
                }
                Err(_) => continue,
            }
        }
    }
    let after = read_stat(&proc.join("stat"))?;
    if !expected.is_subset(&writable)
        || owners(root, lock)? != pids
        || start_time(&start).is_none()
        || start_time(&start) != start_time(&after)
    {
        return Ok(None);
    }
    Ok(Some(pid))
}
fn writer<F: FnMut(&str, Value) -> Result<Value>>(a: &Value, callback: &mut F) -> Result<Value> {
    let unknown = json!({"liveness":"UNKNOWN","reason":"NATIVE_WRITER_UNPROVEN"});
    let platform = a["platform"].as_str().unwrap_or("");
    if !matches!(platform, "linux" | "darwin") {
        return Ok(unknown);
    }
    let h = History::new(a["root"].as_str().ok_or("NATIVE_WRITER_UNPROVEN")?);
    let tid = a["thread_id"].as_str().ok_or("NATIVE_WRITER_UNPROVEN")?;
    let meta = h.metadata(tid)?;
    if meta.is_null() || meta["cwd"] != a["project"] {
        return Ok(unknown);
    }
    let before = h.status(tid, &meta);
    if before["turn_id"] != a["turn_id"]
        || before["state"] != "RUNNING"
        || !before["completed_at"].is_null()
    {
        return Ok(unknown);
    }
    let rollout = h.path(tid, &meta)?;
    let lock = h
        .root
        .join("thread-writer-locks")
        .join(format!("{tid}.lock"));
    if !fs::symlink_metadata(&lock).is_ok_and(|m| m.is_file() && !m.file_type().is_symlink()) {
        return Ok(unknown);
    }
    let pid = if platform == "linux" {
        linux_writer(&json!({"lock":lock,"rollout":rollout}), callback)?
    } else {
        let result = call(
            callback,
            a,
            "command",
            json!({"command":["/usr/sbin/lsof","-n","-P","-Fpcuftan","--",lock,rollout]}),
        )?;
        let out = result["stdout"].as_str().ok_or("NATIVE_WRITER_UNPROVEN")?;
        if result["returncode"] != 0 || out.len() > 32768 {
            return Ok(unknown);
        }
        let expected = BTreeSet::from([
            resolve(&lock).to_string_lossy().into_owned(),
            rollout.to_string_lossy().into_owned(),
        ]);
        let mut rows = Vec::<(u32, String, String, BTreeSet<String>)>::new();
        let mut access = "";
        for line in out.lines() {
            let (k, v) = line.split_at_checked(1).ok_or("NATIVE_WRITER_UNPROVEN")?;
            if k == "p" {
                rows.push((
                    v.parse().map_err(|_| "NATIVE_WRITER_UNPROVEN")?,
                    String::new(),
                    String::new(),
                    BTreeSet::new(),
                ));
            } else if let Some(r) = rows.last_mut() {
                match k {
                    "c" => r.1 = v.into(),
                    "u" => r.2 = v.into(),
                    "f" => access = "",
                    "a" => access = v,
                    "n" if matches!(access, "u" | "w") => {
                        r.3.insert(v.into());
                    }
                    _ => (),
                }
            }
        }
        let owned: Vec<_> = rows
            .iter()
            .filter(|r| {
                r.1 == "codex"
                    && r.2 == rustix::process::getuid().as_raw().to_string()
                    && expected.is_subset(&r.3)
            })
            .collect();
        if owned.len() != 1 {
            return Ok(unknown);
        }
        let pid = owned[0].0;
        let command = call(
            callback,
            a,
            "command",
            json!({"command":["/bin/ps","-p",pid.to_string(),"-o","command="]}),
        )?;
        let args = command["argv"].as_array().ok_or("NATIVE_WRITER_UNPROVEN")?;
        if command["returncode"] != 0
            || args
                .first()
                .and_then(Value::as_str)
                .and_then(|s| Path::new(s).file_name())
                .and_then(|s| s.to_str())
                != Some("codex")
            || !args.iter().any(|v| v == "app-server")
        {
            return Ok(unknown);
        }
        Some(pid)
    };
    let Some(pid) = pid else {
        return Ok(unknown);
    };
    let current = h.metadata(tid)?;
    if current.is_null()
        || ["id", "cwd", "rollout_path", "history_mode"]
            .iter()
            .any(|k| current[k] != meta[k])
        || h.status(tid, &meta) != before
    {
        return Ok(unknown);
    }
    Ok(json!({"liveness":"LIVE","reason":"NATIVE_WRITER_PROCESS","writer_pid":pid}))
}

struct Reader<'a, F> {
    a: &'a Value,
    endpoint: Value,
    native: Option<provider::Client>,
    callback: &'a mut F,
    sequence: u64,
}
impl<F: FnMut(&str, Value) -> Result<Value>> Reader<'_, F> {
    fn callback(&mut self, operation: &str, args: Value) -> std::result::Result<Value, Fault> {
        let reply = (self.callback)(
            "provider.client.observation",
            json!({"token":self.a["callback_token"],"operation":operation,"arguments":args}),
        )
        .map_err(Fault::transport)?;
        if let Some(error) = reply.get("error") {
            let kind = match error["kind"].as_str() {
                Some("protocol") => "protocol",
                Some("remote") => "remote",
                Some("not_found") => "not_found",
                Some("permission") => "permission",
                _ => "transport",
            };
            return Err(Fault {
                kind,
                message: error["message"]
                    .as_str()
                    .unwrap_or("PROVIDER_READ_UNAVAILABLE")
                    .into(),
                remote: error.get("remote").cloned(),
            });
        }
        Ok(reply["result"].clone())
    }
    fn open(&mut self) -> std::result::Result<(), Fault> {
        if self.a["native_transport"] == true {
            self.native = Some(
                provider::Client::connect_uninitialized(
                    Path::new(
                        self.endpoint
                            .as_str()
                            .ok_or_else(|| Fault::transport("PROVIDER_ENDPOINT_INVALID"))?,
                    ),
                    Duration::from_secs(3),
                )
                .map_err(|e| match e.as_str() {
                    "PROVIDER_SOCKET_NOT_PRESENT" => Fault::new("not_found", e),
                    "PROVIDER_SOCKET_PERMISSION_DENIED" => Fault::new("permission", e),
                    _ => fault(e),
                })?,
            );
            self.rpc(
                "initialize",
                json!({"clientInfo":self.a["client_info"],"capabilities":{"experimentalApi":true}}),
            )?;
            self.native
                .as_mut()
                .unwrap()
                .send_message(&json!({"method":"initialized"}))
                .map_err(fault)?;
            Ok(())
        } else {
            self.callback("open", json!({"endpoint":self.endpoint}))
                .map(|_| ())
        }
    }
    fn rpc(&mut self, method: &str, params: Value) -> std::result::Result<Value, Fault> {
        if self.a["native_transport"] == true {
            self.sequence += 1;
            let a = json!({"request":{"id":format!("clinx-observation-{}",self.sequence),"method":method,"params":params},"timeout":3,"policy":{"read_only_observer":true}});
            let mut events = Events {
                methods: Vec::new(),
                received: Vec::new(),
                overflow: false,
                max_methods: 256,
                max_received: 256,
            };
            let mut io = Native {
                client: &mut self.native,
                token: Value::Null,
                callback: self.callback,
            };
            request(&mut io, &a, &mut events, &mut false)
        } else {
            self.callback(
                "rpc",
                json!({"endpoint":self.endpoint,"method":method,"params":params}),
            )
        }
    }
    fn thread(&mut self, tid: &Value) -> std::result::Result<Value, Fault> {
        let value = self.rpc("thread/read", json!({"threadId":tid,"includeTurns":false}))?;
        value
            .get("thread")
            .filter(|v| v.is_object())
            .cloned()
            .ok_or_else(|| Fault::new("protocol", "thread/read result is missing thread"))
    }
    fn turns(&mut self, params: Value) -> std::result::Result<Value, Fault> {
        let page = self.rpc("thread/turns/list", params)?;
        if !page["data"].is_array() {
            return Err(Fault::new(
                "protocol",
                "thread/turns/list result is missing data",
            ));
        }
        Ok(page)
    }
    fn close(&mut self) {
        self.native.take();
        if self.a["native_transport"] != true {
            let _ = self.callback("close", json!({"endpoint":self.endpoint}));
        }
    }
}
fn observe<F: FnMut(&str, Value) -> Result<Value>>(
    a: &Value,
    execution: bool,
    callback: &mut F,
) -> Result<Value> {
    let endpoints = a["endpoints"]
        .as_array()
        .ok_or("PROVIDER_ENDPOINT_INVALID")?;
    let tid = &a["thread_id"];
    let mut rows = Vec::new();
    for endpoint in endpoints {
        let mut reader = Reader {
            a,
            endpoint: endpoint.clone(),
            native: None,
            callback,
            sequence: 0,
        };
        let mut state = json!("UNKNOWN");
        let result = (|| -> std::result::Result<Value, Fault> {
            reader.open()?;
            let thread = reader.thread(tid)?;
            if &thread["id"] != tid {
                return Err(Fault::new("protocol", "exact thread identity mismatch"));
            }
            state = if thread["status"].is_object() {
                thread["status"]["type"].clone()
            } else {
                thread["status"].clone()
            };
            let mut turn = json!({});
            if execution {
                let mut cursor = Value::Null;
                let mut seen = BTreeSet::new();
                for _ in 0..5 {
                    let mut params = json!({"threadId":tid,"limit":20,"sortDirection":"desc","itemsView":"notLoaded"});
                    if truthy(&cursor) {
                        params["cursor"] = cursor.clone();
                    }
                    let page = reader.turns(params)?;
                    turn = page["data"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .find(|r| r["id"] == a["turn_id"])
                        .cloned()
                        .unwrap_or_else(|| json!({}));
                    cursor = page["nextCursor"].clone();
                    if truthy(&turn) || !truthy(&cursor) || !seen.insert(cursor.to_string()) {
                        break;
                    }
                }
            } else {
                match reader.turns(json!({"threadId":tid,"limit":1,"sortDirection":"desc","itemsView":"notLoaded"})){
                    Ok(page)=>turn=page["data"].as_array().unwrap().first().cloned().unwrap_or_else(||json!({})),
                    Err(e) if e.kind=="remote" && e.remote.as_ref().is_some_and(|v|v["code"] == -32601)=>(),
                    Err(e)=>return Err(e),
                }
            }
            let mut row = json!({"endpoint":endpoint,"state":if truthy(&state){state.clone()}else{json!("UNKNOWN")},"thread_id":tid,"turn_id":turn["id"],"turn_status":turn["status"]});
            if !execution {
                row["session_id"] = thread["sessionId"].clone();
                row["cwd"] = thread["cwd"].clone();
                row["source"] = json!("LIVE_THREAD_READ");
            }
            Ok(row)
        })();
        reader.close();
        rows.push(match result{Ok(v)=>v,Err(e)=>{
            if execution{json!({"endpoint":endpoint,"state":state,"transport_error":true,"reason":"PROVIDER_READ_UNAVAILABLE","error_type":match e.kind{"not_found"=>"FileNotFoundError","permission"=>"PermissionError","protocol"=>"AppServerProtocolError","remote"=>"AppServerRemoteError",_=>"AppServerTransportError"}})}
            else{json!({"endpoint":endpoint,"state":if e.kind=="not_found"{"OFFLINE"}else{"UNKNOWN"},"reason":match e.kind{"not_found"=>"SOCKET_NOT_PRESENT","permission"=>"PROVIDER_ACCESS_DENIED",_=>"PROVIDER_READ_UNAVAILABLE"}})}
        }});
    }
    if execution {
        return Ok(classify(&rows, tid, &a["turn_id"]));
    }
    let loaded: Vec<_> = rows
        .iter()
        .filter(|r| {
            !matches!(
                r["state"].as_str(),
                Some("UNKNOWN" | "OFFLINE" | "notLoaded" | "unloaded")
            )
        })
        .collect();
    Ok(
        json!({"state":if loaded.len()==1{loaded[0]["state"].clone()}else{json!("UNKNOWN")},"owner_endpoint":if loaded.len()==1{loaded[0]["endpoint"].clone()}else{Value::Null},"ownership_conflict":loaded.len()>1,"observations":rows,"source":"LIVE_PROVIDER_OBSERVATION","writer_claimed":false}),
    )
}

pub(super) fn invoke<F: FnMut(&str, Value) -> Result<Value>>(
    op: &str,
    a: &Value,
    mut callback: F,
) -> Result<Value> {
    match op {
        "provider.client.observation.normalize" => Ok(json!(normalized(&a["value"]))),
        "provider.client.observation.classify" => Ok(classify(
            a["observations"]
                .as_array()
                .ok_or("PROVIDER_OBSERVATION_INVALID")?,
            &a["thread_id"],
            &a["turn_id"],
        )),
        "provider.client.observation.threads" => observe(a, false, &mut callback),
        "provider.client.observation.execution" => observe(a, true, &mut callback),
        "provider.client.observation.writer" => Ok(writer(a, &mut callback)
            .unwrap_or_else(|_| json!({"liveness":"UNKNOWN","reason":"NATIVE_WRITER_UNPROVEN"}))),
        "provider.client.observation.linux_writer" => {
            Ok(json!(linux_writer(a, &mut callback).unwrap_or(None)))
        }
        _ => Err("PROVIDER_OBSERVATION_OPERATION_UNKNOWN".into()),
    }
}
