//! Loopback Observer HTTP lifecycle, authentication and route policy. Read-only
//! callbacks use existing Rust projections; no task writer or Provider start.
use crate::{
    node_tls::Result,
    read_projection::{text, truth},
};
use regex::Regex;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    io::{BufRead, BufReader, Read, Write},
    net::{TcpListener, TcpStream},
    sync::{
        Arc, Condvar, LazyLock, Mutex,
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
    },
    thread,
    time::Duration,
};
const RESPONSE_LIMIT: usize = 2 * 1024 * 1024;
const LINE_LIMIT: usize = 65536;
const CLIENT_LIMIT: usize = 16;
static SCHEME: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z][A-Za-z0-9+.-]*:").unwrap());
static COLLECTOR: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^/v2/native-collector/(inventory|sample/([0-9a-f-]{36}))$").unwrap()
});
static NATIVE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^/v2/native/([0-9a-f-]{36})$").unwrap());
static OBSERVATION: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^/v2/observations/(obs_[a-f0-9]{40})(/context|/activity)?$").unwrap()
});
static ACTIVITY: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^/v1/tasks/([A-Za-z0-9_-]{1,128})/activity$").unwrap());
static IDENTIFIER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9_-]{1,128}$").unwrap());
static TASK: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^/v1/tasks/([A-Za-z0-9_-]{1,128})(/events)?$").unwrap());
type Callback = Arc<dyn Fn(&str, Value) -> Result<Value> + Send + Sync>;
#[derive(Clone, Debug)]
struct Failure {
    kind: String,
    code: String,
    status: u16,
}
impl Failure {
    fn api(status: u16, code: &str) -> Self {
        Self {
            kind: "api".into(),
            code: code.into(),
            status,
        }
    }
    fn other() -> Self {
        Self {
            kind: "other".into(),
            code: "PROJECTION_UNAVAILABLE".into(),
            status: 503,
        }
    }
    fn wire(&self) -> Value {
        json!({"kind":self.kind,"code":self.code,"status":self.status})
    }
    fn boundary(self, native: bool, collector: bool) -> Self {
        match self.kind.as_str() {
            "coded" => Self::api(
                if collector && self.code == "NATIVE_COLLECTOR_CURSOR_RESET" {
                    409
                } else if !native && self.code == "OBSERVATION_NOT_FOUND" {
                    404
                } else {
                    400
                },
                &self.code,
            ),
            "thread" if native => Self::api(
                if collector && self.code == "NATIVE_COLLECTOR_CURSOR_RESET" {
                    409
                } else {
                    400
                },
                &self.code,
            ),
            "thread" => Self::api(400, "INVALID_QUERY"),
            "authority" if native && !collector => Self::api(400, &self.code),
            "invalid" => Self::api(400, "INVALID_QUERY"),
            _ => self,
        }
    }
}
type Response = std::result::Result<Value, Failure>;
fn callback(call: &Callback, op: &str, a: Value) -> Response {
    let v = call(op, a).map_err(|_| Failure::other())?;
    if let Some(f) = v.get("fault") {
        return Err(Failure {
            kind: text(&f["kind"]).into(),
            code: text(&f["code"]).into(),
            status: f["status"].as_u64().unwrap_or(503) as u16,
        });
    }
    v.get("value").cloned().ok_or_else(Failure::other)
}
fn token_valid(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        (32..=256).contains(&s.len())
            && s.bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c))
    })
}
fn unquote(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::new();
    let mut n = 0;
    while n < b.len() {
        if b[n] == b'+' {
            out.push(b' ');
            n += 1;
        } else if b[n] == b'%'
            && n + 2 < b.len()
            && b[n + 1].is_ascii_hexdigit()
            && b[n + 2].is_ascii_hexdigit()
        {
            out.push(
                u8::from_str_radix(std::str::from_utf8(&b[n + 1..n + 3]).unwrap(), 16).unwrap(),
            );
            n += 3;
        } else {
            out.push(b[n]);
            n += 1;
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}
fn target(s: &str) -> std::result::Result<(String, BTreeMap<String, Value>), Failure> {
    if s.chars().count()
        > if s.starts_with("/v2/native-collector/") {
            16384
        } else {
            2048
        }
    {
        return Err(Failure::api(400, "INVALID_REQUEST"));
    }
    // Match the established urllib URL wire normalization before policy checks.
    let s = s
        .trim_start_matches(|c: char| c <= ' ')
        .replace(['\r', '\n', '\t'], "");
    let (s, fragment) = s.split_once('#').unwrap_or((&s, ""));
    if !fragment.is_empty() {
        return Err(Failure::api(400, "INVALID_REQUEST"));
    }
    let s = if let Some(rest) = s.strip_prefix("//") {
        if !rest.is_empty() && !rest.starts_with(['/', '?']) {
            return Err(Failure::api(400, "INVALID_REQUEST"));
        }
        rest
    } else {
        s
    };
    let (path, query) = s.split_once('?').unwrap_or((s, ""));
    if path.contains('%') || SCHEME.is_match(path) {
        return Err(Failure::api(400, "INVALID_REQUEST"));
    }
    let mut args = BTreeMap::new();
    if !query.is_empty() {
        let parts = query.split('&').collect::<Vec<_>>();
        if parts.len() > 8 {
            return Err(Failure::api(400, "INVALID_QUERY"));
        }
        for part in parts {
            let (key, value) = part
                .split_once('=')
                .ok_or_else(|| Failure::api(400, "INVALID_QUERY"))?;
            if args.insert(unquote(key), json!(unquote(value))).is_some() {
                return Err(Failure::api(400, "INVALID_QUERY"));
            }
        }
    }
    Ok((path.into(), args))
}
fn allowed(args: &BTreeMap<String, Value>, keys: &[&str]) -> bool {
    args.keys().all(|k| keys.contains(&k.as_str()))
}
fn arg(args: &BTreeMap<String, Value>, name: &str, default: Value) -> Value {
    args.get(name).cloned().unwrap_or(default)
}
fn integer(v: &Value) -> std::result::Result<Value, Failure> {
    // Wide integers cross the pipe as decimal text; JSON floats must not round
    // an exact source argument. The Python binding only decodes this wire tag.
    let normalized = crate::python_numeric::integer_text(text(v))
        .map_err(|_| Failure::api(400, "INVALID_QUERY"))?;
    Ok(normalized
        .parse::<i64>()
        .map(|v| json!(v))
        .unwrap_or_else(|_| json!({"__observer_decimal": normalized})))
}
fn read(call: &Callback, target: &str, method: &str, args: Value) -> Response {
    callback(
        call,
        "observer_http.read",
        json!({"target":target,"method":method,"arguments":args}),
    )
}
fn request(a: &Value, call: &Callback) -> Response {
    let context = callback(call, "observer_http.context", json!({}))?;
    let token = &context["credential"];
    if !token_valid(token) {
        return Err(Failure::api(503, "AUTH_UNAVAILABLE"));
    }
    let expected = Sha256::digest(format!("Bearer {}", text(token)).as_bytes());
    let actual = Sha256::digest(text(&a["authorization"]).as_bytes());
    if !a["authorization"].is_string()
        || expected
            .iter()
            .zip(actual)
            .fold(0u8, |n, (a, b)| n | (a ^ b))
            != 0
    {
        return Err(Failure::api(401, "UNAUTHORIZED"));
    }
    if a["method"] != "GET" {
        return Err(Failure::api(405, "READ_ONLY"));
    }
    let (path, mut query) = target(text(&a["target"]))?;
    let collector = &COLLECTOR;
    if let Some(c) = collector.captures(&path) {
        return read(
            call,
            "collector",
            "request",
            json!({"route":&c[1],"query":query}),
        )
        .map_err(|e| e.boundary(true, true));
    }
    let native = &NATIVE;
    if let Some(c) = native.captures(&path) {
        if !allowed(&query, &["context", "recent_turns", "max_bytes", "cursor"]) {
            return Err(Failure::api(400, "INVALID_QUERY"));
        }
        let context = arg(&query, "context", json!("0"));
        if context != "0" && context != "1" {
            return Err(Failure::api(400, "INVALID_QUERY"));
        }
        return read(call,"native","observe",json!({"thread_id":&c[1],"context":context=="1","recent_turns":integer(&arg(&query,"recent_turns",json!("8")))?,"max_bytes":integer(&arg(&query,"max_bytes",json!("32000")))?,"cursor":arg(&query,"cursor",Value::Null)})).map_err(|e|e.boundary(true,false));
    }
    if path.starts_with("/v2/observations") {
        if context["network_configured"] != true {
            return Err(Failure::api(503, "OBSERVATION_NOT_CONFIGURED"));
        }
        if let Some(limit) = query.get("limit") {
            query.insert("limit".into(), integer(limit)?);
        }
        if path == "/v2/observations"
            && allowed(
                &query,
                &[
                    "node",
                    "project",
                    "state",
                    "kind",
                    "native_thread_id",
                    "cursor",
                    "limit",
                ],
            )
        {
            return read(call, "network", "list", json!(query))
                .map_err(|e| e.boundary(false, false));
        }
        let pattern = &OBSERVATION;
        if let Some(c) = pattern.captures(&path) {
            let suffix = c.get(2).map(|m| m.as_str()).unwrap_or("");
            let keys = if suffix == "/context" {
                &["cursor"][..]
            } else {
                &["cursor", "limit"][..]
            };
            if allowed(&query, keys) {
                query.insert("observation_id".into(), json!(&c[1]));
                return read(
                    call,
                    "network",
                    match suffix {
                        "/context" => "context",
                        "/activity" => "activity",
                        _ => "detail",
                    },
                    json!(query),
                )
                .map_err(|e| e.boundary(false, false));
            }
        }
        return Err(Failure::api(400, "INVALID_QUERY"));
    }
    if path == "/v1/health" && query.is_empty() {
        return read(call, "store", "health", json!({}));
    }
    if path == "/v1/tasks" {
        if !allowed(&query, &["state", "offset"])
            || !matches!(query.get("state").map(text), Some("active" | "recent"))
        {
            return Err(Failure::api(400, "INVALID_QUERY"));
        }
        let offset = arg(&query, "offset", json!("0"));
        let raw = text(&offset);
        if raw.is_empty()
            || raw.len() > 6
            || !raw.bytes().all(|b| b.is_ascii_digit())
            || raw.parse::<u64>().unwrap() > 100000
        {
            return Err(Failure::api(400, "INVALID_QUERY"));
        }
        return read(
            call,
            "store",
            "list_tasks",
            json!({"state":query["state"],"offset":raw.parse::<u64>().unwrap()}),
        );
    }
    let activity = &ACTIVITY;
    if let Some(c) = activity.captures(&path) {
        let eref = arg(&query, "execution_ref", json!(""));
        if !allowed(&query, &["execution_ref", "after", "before"])
            || (query.contains_key("after") && query.contains_key("before"))
            || !IDENTIFIER.is_match(text(&eref))
            || ["after", "before"]
                .iter()
                .any(|k| query.get(*k).is_some_and(|v| !truth(v)))
        {
            return Err(Failure::api(400, "INVALID_QUERY"));
        }
        return read(
            call,
            "store",
            "task_activity",
            json!({"task_ref":&c[1],"execution_ref":eref,"after":arg(&query,"after",Value::Null),"before":arg(&query,"before",Value::Null)}),
        );
    }
    let task = &TASK;
    if let Some(c) = task.captures(&path) {
        if c.get(2).is_some() && allowed(&query, &["after"]) {
            return read(
                call,
                "store",
                "task_events",
                json!({"task_ref":&c[1],"after":arg(&query,"after",Value::Null)}),
            );
        }
        if c.get(2).is_none() && query.is_empty() {
            return read(call, "store", "task", json!({"task_ref":&c[1]}));
        }
        return Err(Failure::api(400, "INVALID_QUERY"));
    }
    Err(Failure::api(404, "NOT_FOUND"))
}
fn ascii_json(value: &Value) -> String {
    match value {
        Value::String(s) => serde_json::to_string(s)
            .unwrap()
            .chars()
            .map(|c| {
                if (c as u32) < 127 {
                    c.to_string()
                } else {
                    c.encode_utf16(&mut [0; 2])
                        .iter()
                        .map(|n| format!("\\u{n:04x}"))
                        .collect()
                }
            })
            .collect(),
        Value::Array(rows) => format!(
            "[{}]",
            rows.iter().map(ascii_json).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(rows) => format!(
            "{{{}}}",
            rows.iter()
                .map(|(k, v)| format!("{}: {}", ascii_json(&json!(k)), ascii_json(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        _ => crate::read_projection::compact(value),
    }
}
fn response(result: Response, token: &Value) -> (u16, Vec<u8>) {
    let (mut status, payload) = match result {
        Ok(v) => (200, v),
        Err(e) => {
            let (status, code) = match e.kind.as_str() {
                "api" => (e.status, e.code),
                "source" => (503, "SOURCE_UNAVAILABLE".into()),
                _ => (503, "PROJECTION_UNAVAILABLE".into()),
            };
            (status, json!({"schema_version":"1","error":code}))
        }
    };
    let mut body = ascii_json(&payload);
    if let Some(token) = token.as_str().filter(|s| !s.is_empty()) {
        body = body.replace(token, "[REDACTED]");
    }
    if body.len() > RESPONSE_LIMIT {
        status = 503;
        body = "{\"schema_version\":\"1\",\"error\":\"RESPONSE_BOUND_EXCEEDED\"}".into();
    }
    (status, body.into_bytes())
}
struct HttpRequest {
    method: String,
    target: String,
    headers: Vec<(String, String)>,
}
fn line(reader: &mut BufReader<TcpStream>) -> std::io::Result<Vec<u8>> {
    let mut line = Vec::new();
    reader
        .take((LINE_LIMIT + 1) as u64)
        .read_until(b'\n', &mut line)?;
    Ok(line)
}
fn parse(reader: &mut BufReader<TcpStream>) -> std::result::Result<Option<HttpRequest>, Failure> {
    let first = line(reader).map_err(|_| Failure::other())?;
    if first.is_empty() {
        return Ok(None);
    }
    if first.len() > LINE_LIMIT {
        return Err(Failure::api(414, "INVALID_REQUEST"));
    }
    let first = first.iter().map(|b| char::from(*b)).collect::<String>();
    let fields = first.split_whitespace().collect::<Vec<_>>();
    if !(2..=3).contains(&fields.len()) {
        return Err(Failure::api(400, "INVALID_REQUEST"));
    }
    if fields.len() == 3 {
        let version = fields[2]
            .strip_prefix("HTTP/")
            .and_then(|s| s.split_once('.'))
            .filter(|(a, b)| {
                !a.is_empty()
                    && !b.is_empty()
                    && a.len() <= 10
                    && b.len() <= 10
                    && a.bytes().chain(b.bytes()).all(|c| c.is_ascii_digit())
            })
            .ok_or_else(|| Failure::api(400, "INVALID_REQUEST"))?;
        if version.0.parse::<u64>().unwrap() >= 2 {
            return Err(Failure::api(505, "INVALID_REQUEST"));
        }
    } else if fields[0] != "GET" {
        return Err(Failure::api(400, "INVALID_REQUEST"));
    }
    let mut headers: Vec<(String, String)> = Vec::new();
    for count in 1.. {
        let bytes = line(reader).map_err(|_| Failure::other())?;
        if bytes.len() > LINE_LIMIT || count > 100 {
            return Err(Failure::api(431, "INVALID_REQUEST"));
        }
        if matches!(bytes.as_slice(), b"\r\n" | b"\n" | b"") {
            break;
        }
        let raw = bytes.iter().map(|b| char::from(*b)).collect::<String>();
        if raw.starts_with([' ', '\t']) {
            let Some((_, value)) = headers.last_mut() else {
                return Err(Failure::api(400, "INVALID_REQUEST"));
            };
            value.push_str("\r\n");
            value.push_str(raw.trim_end_matches(['\r', '\n']));
            continue;
        }
        let (key, value) = raw
            .trim_end_matches(['\r', '\n'])
            .split_once(':')
            .ok_or_else(|| Failure::api(400, "INVALID_REQUEST"))?;
        if key.is_empty() || key.chars().any(|c| c.is_whitespace()) {
            return Err(Failure::api(400, "INVALID_REQUEST"));
        }
        headers.push((
            key.to_ascii_lowercase(),
            value.trim_start_matches([' ', '\t']).into(),
        ));
    }
    let target = if fields[1].starts_with("//") {
        format!("/{}", fields[1].trim_start_matches('/'))
    } else {
        fields[1].into()
    };
    Ok(Some(HttpRequest {
        method: fields[0].into(),
        target,
        headers,
    }))
}
fn serve_client(mut socket: TcpStream, audit: bool, call: Callback) {
    let _ = socket.set_read_timeout(Some(Duration::from_secs(5)));
    let _ = socket.set_write_timeout(Some(Duration::from_secs(5)));
    let Ok(copy) = socket.try_clone() else { return };
    let mut reader = BufReader::new(copy);
    let mut method = String::new();
    let result = match parse(&mut reader) {
        Ok(None) => return,
        Ok(Some(r)) => {
            method = r.method.clone();
            let auth = r
                .headers
                .iter()
                .filter(|(k, _)| k == "authorization")
                .map(|(_, v)| v)
                .collect::<Vec<_>>();
            if auth.len() != 1 {
                Err(Failure::api(401, "UNAUTHORIZED"))
            } else if r
                .headers
                .iter()
                .find(|(k, _)| k == "transfer-encoding")
                .is_some_and(|(_, v)| !v.is_empty())
                || r.headers
                    .iter()
                    .find(|(k, _)| k == "content-length")
                    .is_some_and(|(_, v)| v != "0")
            {
                Err(Failure::api(400, "BODY_NOT_ALLOWED"))
            } else {
                request(
                    &json!({"method":r.method,"target":r.target,"authorization":auth[0]}),
                    &call,
                )
            }
        }
        Err(e) if e.kind == "other" => return,
        Err(e) => Err(e),
    };
    let Ok(token) = callback(&call, "observer_http.credential", json!({})) else {
        return;
    };
    let (status, body) = response(result, &token);
    let reason = match status {
        200 => "OK",
        400 => "Bad Request",
        401 => "Unauthorized",
        404 => "Not Found",
        405 => "Method Not Allowed",
        409 => "Conflict",
        414 => "Request-URI Too Long",
        431 => "Request Header Fields Too Large",
        503 => "Service Unavailable",
        505 => "HTTP Version Not Supported",
        _ => "Error",
    };
    let now = chrono::DateTime::from_timestamp(crate::read_projection::now() as i64, 0).unwrap();
    let date = now.format("%a, %d %b %Y %H:%M:%S GMT");
    let headers = format!(
        "HTTP/1.0 {status} {reason}\r\nServer: CLINXObserver/1 \r\nDate: {date}\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: {}\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nConnection: close\r\n{}\r\n",
        body.len(),
        match status {
            401 => "WWW-Authenticate: Bearer\r\n",
            405 => "Allow: GET\r\n",
            _ => "",
        }
    );
    if socket.write_all(headers.as_bytes()).is_err() {
        return;
    }
    if audit {
        let method = if matches!(
            method.as_str(),
            "GET" | "HEAD" | "POST" | "PUT" | "PATCH" | "DELETE" | "OPTIONS"
        ) {
            method.as_str()
        } else {
            "OTHER"
        };
        let _ = callback(
            &call,
            "observer_http.audit",
            json!({"observer_http_method":method,"status":status}),
        );
    }
    if method != "HEAD" {
        let _ = socket.write_all(&body);
    }
}
struct Server {
    listener: Mutex<Option<TcpListener>>,
    stopped: AtomicBool,
    active: AtomicUsize,
    done: Mutex<bool>,
    changed: Condvar,
    thread: Mutex<Option<thread::JoinHandle<()>>>,
    audit: bool,
}
struct ActiveClient(Arc<Server>);
impl Drop for ActiveClient {
    fn drop(&mut self) {
        self.0.active.fetch_sub(1, Ordering::AcqRel);
    }
}
#[derive(Default)]
pub struct Runtime {
    next: AtomicU64,
    servers: Mutex<BTreeMap<u64, Arc<Server>>>,
}
impl Runtime {
    fn server(&self, a: &Value) -> Result<Arc<Server>> {
        self.servers
            .lock()
            .map_err(|_| "OBSERVER_HTTP_UNAVAILABLE")?
            .get(&a["server"].as_u64().ok_or("OBSERVER_HTTP_UNAVAILABLE")?)
            .cloned()
            .ok_or("OBSERVER_HTTP_UNAVAILABLE".into())
    }
    pub fn invoke(
        &self,
        op: &str,
        a: &Value,
        call: impl Fn(&str, Value) -> Result<Value> + Send + Sync + 'static,
    ) -> Result<Value> {
        let call: Callback = Arc::new(call);
        match op {
            "observer_http.request" => {
                return Ok(match request(a, &call) {
                    Ok(v) => json!({"value":v}),
                    Err(e) => json!({"fault":e.wire()}),
                });
            }
            "observer_http.response" => {
                let result = if a["fault"].is_null() {
                    Ok(a["value"].clone())
                } else {
                    Err(Failure {
                        kind: text(&a["fault"]["kind"]).into(),
                        code: text(&a["fault"]["code"]).into(),
                        status: a["fault"]["status"].as_u64().unwrap_or(503) as u16,
                    })
                };
                let (status, body) = response(result, &a["credential"]);
                return Ok(json!({"status":status,"body":String::from_utf8(body).unwrap()}));
            }
            "observer_http.bind" => {
                let port = a["port"]
                    .as_u64()
                    .filter(|n| *n <= 65535)
                    .ok_or("OBSERVER_HTTP_BIND_FAILED")? as u16;
                use rustix::net::{self, AddressFamily, SocketType};
                let fd = crate::platform::socket(
                    AddressFamily::INET,
                    SocketType::STREAM,
                    Some(net::ipproto::TCP),
                    false,
                )
                .map_err(|_| "OBSERVER_HTTP_BIND_FAILED")?;
                net::sockopt::set_socket_reuseaddr(&fd, true)
                    .map_err(|_| "OBSERVER_HTTP_BIND_FAILED")?;
                net::bind(
                    &fd,
                    &std::net::SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, port),
                )
                .and_then(|_| net::listen(&fd, 16))
                .map_err(|_| "OBSERVER_HTTP_BIND_FAILED")?;
                let listener = TcpListener::from(fd);
                listener
                    .set_nonblocking(true)
                    .map_err(|_| "OBSERVER_HTTP_BIND_FAILED")?;
                let address = listener
                    .local_addr()
                    .map_err(|_| "OBSERVER_HTTP_BIND_FAILED")?;
                let id = self.next.fetch_add(1, Ordering::Relaxed) + 1;
                self.servers
                    .lock()
                    .map_err(|_| "OBSERVER_HTTP_UNAVAILABLE")?
                    .insert(
                        id,
                        Arc::new(Server {
                            listener: Mutex::new(Some(listener)),
                            stopped: AtomicBool::new(false),
                            active: AtomicUsize::new(0),
                            done: Mutex::new(false),
                            changed: Condvar::new(),
                            thread: Mutex::new(None),
                            audit: a["audit_methods"] == true,
                        }),
                    );
                return Ok(json!({"server":id,"address":["127.0.0.1",address.port()]}));
            }
            _ => (),
        }
        let server = self.server(a)?;
        match op {
            "observer_http.start" => {
                let mut handle = server
                    .thread
                    .lock()
                    .map_err(|_| "OBSERVER_HTTP_UNAVAILABLE")?;
                if handle.is_some() {
                    return Ok(Value::Null);
                }
                let listener = server
                    .listener
                    .lock()
                    .map_err(|_| "OBSERVER_HTTP_UNAVAILABLE")?
                    .as_ref()
                    .ok_or("OBSERVER_HTTP_UNAVAILABLE")?
                    .try_clone()
                    .map_err(|_| "OBSERVER_HTTP_UNAVAILABLE")?;
                *server
                    .done
                    .lock()
                    .map_err(|_| "OBSERVER_HTTP_UNAVAILABLE")? = false;
                server.stopped.store(false, Ordering::Release);
                let owned = server.clone();
                let thread = thread::spawn(move || {
                    while !owned.stopped.load(Ordering::Acquire) {
                        match listener.accept() {
                            Ok((socket, _)) => {
                                if owned
                                    .active
                                    .fetch_update(Ordering::AcqRel, Ordering::Acquire, |n| {
                                        (n < CLIENT_LIMIT).then_some(n + 1)
                                    })
                                    .is_err()
                                {
                                    continue;
                                }
                                let owner = owned.clone();
                                let callback = call.clone();
                                thread::spawn(move || {
                                    let guard = ActiveClient(owner);
                                    serve_client(socket, guard.0.audit, callback);
                                });
                            }
                            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                                let timeout = rustix::event::Timespec {
                                    tv_sec: 0,
                                    tv_nsec: 100_000_000,
                                };
                                let _ = rustix::event::poll(
                                    &mut [rustix::event::PollFd::new(
                                        &listener,
                                        rustix::event::PollFlags::IN,
                                    )],
                                    Some(&timeout),
                                );
                            }
                            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => (),
                            Err(_) => break,
                        }
                    }
                    if let Ok(mut done) = owned.done.lock() {
                        *done = true;
                        owned.changed.notify_all();
                    }
                });
                *handle = Some(thread);
                Ok(Value::Null)
            }
            "observer_http.wait" => {
                let done = server
                    .done
                    .lock()
                    .map_err(|_| "OBSERVER_HTTP_UNAVAILABLE")?;
                let (done, _) = server
                    .changed
                    .wait_timeout_while(done, Duration::from_secs(30), |done| !(*done))
                    .map_err(|_| "OBSERVER_HTTP_UNAVAILABLE")?;
                Ok(json!(*done))
            }
            "observer_http.shutdown" | "observer_http.close" => {
                let mut handle = server
                    .thread
                    .lock()
                    .map_err(|_| "OBSERVER_HTTP_UNAVAILABLE")?;
                server.stopped.store(true, Ordering::Release);
                if let Some(thread) = handle.take() {
                    let _ = thread.join();
                }
                *server
                    .done
                    .lock()
                    .map_err(|_| "OBSERVER_HTTP_UNAVAILABLE")? = true;
                server.changed.notify_all();
                if op == "observer_http.close" {
                    server
                        .listener
                        .lock()
                        .map_err(|_| "OBSERVER_HTTP_UNAVAILABLE")?
                        .take();
                    self.servers
                        .lock()
                        .map_err(|_| "OBSERVER_HTTP_UNAVAILABLE")?
                        .remove(&a["server"].as_u64().unwrap());
                }
                Ok(Value::Null)
            }
            _ => Err("OBSERVER_HTTP_OPERATION_UNKNOWN".into()),
        }
    }
}
