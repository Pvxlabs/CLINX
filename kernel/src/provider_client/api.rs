//! Public Codex request contract. Python supplies only the caller's transport
//! and DTO representation; validation, payloads, ownership and fallback policy
//! are decided here. Callback errors retain their exact original identity.
use serde_json::{Value, json};
use sha1::{Digest, Sha1};
use std::collections::{BTreeMap, BTreeSet};
use unicode_casefold::UnicodeCaseFold;

type Result<T> = std::result::Result<T, Value>;
fn error(kind: &str, message: impl Into<String>) -> Value {
    json!({"kind":kind,"message":message.into()})
}
fn protocol(message: impl Into<String>) -> Value {
    error("protocol", message)
}
fn optional(v: &Value) -> Value {
    if v.is_string() {
        v.clone()
    } else {
        Value::Null
    }
}
fn truth(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Array(v) => !v.is_empty(),
        Value::Object(v) => !v.is_empty(),
        Value::Number(n) => n.as_f64() != Some(0.0),
    }
}
fn text(v: &Value) -> &str {
    v.as_str().unwrap_or("")
}
fn put(params: &mut Value, a: &Value, fields: &[(&str, &str)]) {
    for (src, dst) in fields {
        if !a[src].is_null() {
            params[*dst] = a[*src].clone();
        }
    }
}
fn thread(result: Value, method: &str) -> Result<Value> {
    if !result["thread"].is_object() {
        Err(protocol(format!("{method} result is missing thread")))
    } else {
        Ok(result["thread"].clone())
    }
}
fn paged(result: Value, method: &str) -> Result<Value> {
    if !result["data"].is_array() {
        Err(protocol(format!("{method} result is missing data")))
    } else {
        Ok(result)
    }
}
fn limit(a: &Value) -> Result<Value> {
    if a.as_u64().is_some_and(|v| (1..=100).contains(&v)) {
        Ok(a.clone())
    } else {
        Err(protocol(
            "bounded history limit must be an integer from 1 to 100",
        ))
    }
}
fn tools(a: &Value) -> Result<()> {
    if !a.is_null() && !a.as_array().is_some_and(|v| !v.is_empty()) {
        Err(protocol("dynamic_tools must be a non-empty array"))
    } else {
        Ok(())
    }
}
fn py_repr(v: &Value) -> String {
    if let Some(s) = v.as_str() {
        let quote = if s.contains('\'') && !s.contains('"') {
            '"'
        } else {
            '\''
        };
        let mut out = String::from(quote);
        for c in s.chars() {
            match c {
                '\\' => out.push_str("\\\\"),
                '\n' => out.push_str("\\n"),
                '\r' => out.push_str("\\r"),
                '\t' => out.push_str("\\t"),
                c if c == quote => {
                    out.push('\\');
                    out.push(c)
                }
                c if c.is_control() => {
                    let n = c as u32;
                    out.push_str(&if n < 256 {
                        format!("\\x{n:02x}")
                    } else {
                        format!("\\u{n:04x}")
                    });
                }
                c => out.push(c),
            }
        }
        out.push(quote);
        out
    } else {
        match v {
            Value::Null => "None".into(),
            Value::Bool(true) => "True".into(),
            Value::Bool(false) => "False".into(),
            _ => v.to_string(),
        }
    }
}
fn efforts(a: &Value, id: &str) -> Result<Vec<String>> {
    let bad = || {
        error(
            "model",
            format!("model/list has malformed capability for {id}"),
        )
    };
    a.as_array()
        .ok_or_else(bad)?
        .iter()
        .map(|v| {
            let s = if v.is_object() {
                v["reasoningEffort"].as_str()
            } else {
                v.as_str()
            };
            s.map(str::trim)
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
                .ok_or_else(bad)
        })
        .collect()
}
fn resolve(payload: &Value, requested: &Value, effort: &Value) -> Result<Value> {
    let data = payload["data"]
        .as_array()
        .ok_or_else(|| error("model", "model/list capability data is unavailable"))?;
    let mut entries = Vec::new();
    let mut malformed = BTreeSet::new();
    let mut aliases = BTreeMap::new();
    for item in data {
        if !item.is_object() || item["hidden"] == true {
            continue;
        }
        let id = item["id"]
            .as_str()
            .filter(|v| !v.trim().is_empty())
            .ok_or_else(|| error("model", "model/list contains malformed model id"))?;
        let mut names = BTreeSet::from([id.case_fold().collect::<String>()]);
        for name in ["model", "displayName"] {
            if let Some(v) = item[name].as_str() {
                names.insert(v.case_fold().collect());
            }
        }
        aliases.insert(id, names);
        let Ok(supported) = efforts(&item["supportedReasoningEfforts"], id) else {
            malformed.insert(id);
            continue;
        };
        let default = &item["defaultReasoningEffort"];
        if !default.is_null()
            && !default
                .as_str()
                .is_some_and(|s| !s.trim().is_empty() && supported.iter().any(|v| v == s.trim()))
        {
            malformed.insert(id);
            continue;
        }
        entries.push((id, supported, default.as_str().map(str::trim)));
    }
    if entries.is_empty() {
        return Err(error("model", "model/list returned no usable models"));
    }
    let wanted = text(requested).trim();
    if wanted.is_empty() {
        return Err(error("model", "executable model is required"));
    }
    let malformed_error = || {
        error(
            "model",
            format!("model/list has malformed capability for {wanted}"),
        )
    };
    if malformed.contains(wanted) {
        return Err(malformed_error());
    }
    let selected = if let Some(entry) = entries.iter().find(|e| e.0 == wanted) {
        entry
    } else {
        let needle = wanted.case_fold().collect::<String>();
        if malformed
            .iter()
            .any(|id| aliases.get(id).is_some_and(|v| v.contains(&needle)))
        {
            return Err(malformed_error());
        }
        let matches = entries
            .iter()
            .filter(|e| {
                aliases[e.0].contains(&needle)
                    || e.0
                        .rsplit('-')
                        .next()
                        .unwrap_or(e.0)
                        .case_fold()
                        .collect::<String>()
                        == needle
            })
            .collect::<Vec<_>>();
        if matches.len() != 1 {
            return Err(error(
                "model",
                format!(
                    "model {} is unsupported or ambiguous",
                    py_repr(&json!(wanted))
                ),
            ));
        }
        matches[0]
    };
    let effort = effort
        .as_str()
        .map(|s| json!(s.trim()))
        .unwrap_or_else(|| effort.clone());
    if !effort.is_null()
        && !effort
            .as_str()
            .is_some_and(|s| selected.1.iter().any(|v| v == s))
    {
        return Err(error(
            "model",
            format!(
                "reasoning effort {} is unsupported for {}",
                py_repr(&effort),
                selected.0
            ),
        ));
    }
    Ok(json!([
        selected.0,
        if truth(&effort) {
            effort
        } else {
            json!(selected.2)
        }
    ]))
}
fn client_id(thread: &str, key: &str) -> String {
    let namespace = [
        0x6b, 0xa7, 0xb8, 0x11, 0x9d, 0xad, 0x11, 0xd1, 0x80, 0xb4, 0x00, 0xc0, 0x4f, 0xd4, 0x30,
        0xc8,
    ];
    let mut hash = Sha1::new();
    hash.update(namespace);
    hash.update(format!("clinx-native-input:{thread}:{key}"));
    let mut bytes: [u8; 20] = hash.finalize().into();
    bytes[6] = (bytes[6] & 15) | 0x50;
    bytes[8] = (bytes[8] & 63) | 0x80;
    let hex = bytes[..16]
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>();
    format!(
        "{}-{}-{}-{}-{}",
        &hex[..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..]
    )
}
fn version(v: &Value) -> Value {
    // Return decimal strings: the public Python tuple supports arbitrarily
    // large version components, so don't truncate them into machine integers.
    let Some(s) = v.as_str() else {
        return Value::Null;
    };
    let regex = regex::Regex::new(r"\p{Nd}+\.\p{Nd}+\.\p{Nd}+").expect("constant version regex");
    regex
        .find(s)
        .map(|m| json!(m.as_str().split('.').collect::<Vec<_>>()))
        .unwrap_or(Value::Null)
}

struct Api<'a, F> {
    callback: &'a mut F,
    token: &'a Value,
    state: Value,
    policy: &'a Value,
}
impl<F: FnMut(&str, Value) -> super::Result<Value>> Api<'_, F> {
    fn call(&mut self, operation: &str, arguments: Value) -> Result<Value> {
        let reply = (self.callback)(
            "provider.client.io",
            json!({"token":self.token,"operation":operation,"arguments":arguments}),
        )
        .map_err(|e| error("transport", e))?;
        if let Some(e) = reply.get("error") {
            return Err(e.clone());
        }
        reply
            .get("result")
            .cloned()
            .ok_or_else(|| error("transport", "PROVIDER_DELIVERY_UNKNOWN"))
    }
    fn request(&mut self, method: &str, params: Value) -> Result<Value> {
        self.call("request", json!({"method":method,"params":params}))
    }
    fn own_thread(&mut self, id: Value) -> Result<()> {
        if !id.is_string() {
            return Err(protocol("thread response is missing thread.id"));
        }
        let ids = self.state["owned_thread_ids"]
            .as_array_mut()
            .expect("normalized ids");
        if !ids.contains(&id) {
            ids.push(id);
        }
        Ok(())
    }
    fn release(&mut self, id: &Value) {
        if !self.state["owned_thread_ids"]
            .as_array()
            .is_some_and(|v| v.contains(id))
        {
            return;
        }
        let result = self
            .request("thread/unsubscribe", json!({"threadId":id}))
            .and_then(|v| {
                if matches!(
                    v["status"].as_str(),
                    Some("unsubscribed" | "notSubscribed" | "notLoaded")
                ) {
                    Ok(v["status"].clone())
                } else {
                    Err(protocol("Invalid thread/unsubscribe response"))
                }
            });
        let status = match result {
            Ok(v) => v,
            Err(e) => json!(format!(
                "UNCONFIRMED:{}",
                e["class"].as_str().unwrap_or(if e["kind"] == "protocol" {
                    "AppServerProtocolError"
                } else {
                    "AppServerTransportError"
                })
            )),
        };
        self.state["writer_release"][text(id)] = status;
        self.state["owned_thread_ids"]
            .as_array_mut()
            .unwrap()
            .retain(|v| v != id);
    }
    fn lifecycle(&mut self) -> Result<Value> {
        let explicit = self.call(
            "attribute",
            json!({"name":"transport_lifecycle_state","call":true}),
        )?;
        if explicit["failed"] == true {
            return Ok(json!("unknown"));
        }
        if explicit["callable"] == true
            && matches!(
                explicit["value"].as_str(),
                Some("available" | "unavailable" | "unknown")
            )
        {
            return Ok(explicit["value"].clone());
        }
        let closed = self.call("attribute", json!({"name":"closed"}))?;
        let connected = self.call("attribute", json!({"name":"connected"}))?;
        if closed["value"] == true || connected["value"] == false {
            return Ok(json!("unavailable"));
        }
        if (closed["present"] == true && closed["value"] != false)
            || (connected["present"] == true && connected["value"] != true)
        {
            return Ok(json!("unknown"));
        }
        let process = self.call("attribute", json!({"name":"_process"}))?;
        if process["present"] == true {
            if process["none"] == true {
                return Ok(json!("unavailable"));
            }
            let poll = self.call(
                "attribute",
                json!({"name":"poll","call":true,"process":true}),
            )?;
            return Ok(json!(
                if poll["failed"] == true || poll["callable"] != true {
                    "unknown"
                } else if poll["none"] == true {
                    "available"
                } else {
                    "unavailable"
                }
            ));
        }
        if connected["value"] == true || closed["value"] == false {
            return Ok(json!("available"));
        }
        let flag = self.call("attribute", json!({"name":"_connected"}))?;
        Ok(json!(if flag["value"] == true {
            "available"
        } else if flag["value"] == false {
            "unavailable"
        } else {
            "unknown"
        }))
    }
    fn run(&mut self, op: &str, a: &Value) -> Result<Value> {
        match op {
            "lifecycle" => self.lifecycle(),
            "versions_compatible" => {
                let expected = &a["expected"];
                let actual = &a["actual"];
                Ok(json!(
                    expected.is_null() || actual.is_null() || expected[0] == actual[0]
                ))
            }
            "validate_capacity" => {
                for name in ["max_received_events", "max_method_history"] {
                    if !a[name].as_u64().is_some_and(|n| n > 0) {
                        return Err(protocol(format!("{name} must be a positive integer")));
                    }
                }
                Ok(Value::Null)
            }
            "version" => Ok(version(&a["value"])),
            "efforts" => efforts(&a["value"], text(&a["model_id"])).map(|v| json!(v)),
            "thread_result" => thread(a["result"].clone(), text(&a["method"])),
            "paged_result" => paged(a["result"].clone(), text(&a["method"])),
            "bounded_limit" => limit(&a["limit"]),
            "release" => {
                self.release(&a["thread_id"]);
                Ok(Value::Null)
            }
            "close" => {
                let ids = self.state["owned_thread_ids"].as_array().unwrap().clone();
                for id in ids {
                    if self.state["owned_turns"].get(text(&id)).is_none() {
                        self.release(&id);
                    }
                }
                self.call("close", json!({}))
            }
            "initialize" => {
                let r=self.request("initialize",json!({"clientInfo":{"name":a["client_name"],"title":a["client_title"],"version":a["client_version"]},"capabilities":{"experimentalApi":true}}))?;
                if !r.is_object() {
                    return Err(protocol("initialize result must be an object"));
                }
                self.call("send", json!({"message":{"method":"initialized"}}))?;
                Ok(
                    json!({"server_name":optional(&r["serverInfo"]["name"]),"server_version":optional(&r["serverInfo"]["version"]),"user_agent":optional(&r["userAgent"])}),
                )
            }
            "thread_read" => thread(
                self.request(
                    "thread/read",
                    json!({"threadId":a["thread_id"],"includeTurns":false}),
                )?,
                "thread/read",
            ),
            "thread_turns_list" | "thread_items_list" => {
                let method = if op == "thread_turns_list" {
                    "thread/turns/list"
                } else {
                    "thread/items/list"
                };
                if !matches!(a["sort_direction"].as_str(), Some("asc" | "desc")) {
                    return Err(protocol(format!(
                        "{method} sort_direction must be asc or desc"
                    )));
                }
                if op == "thread_turns_list"
                    && !matches!(
                        a["items_view"].as_str(),
                        Some("summary" | "full" | "notLoaded")
                    )
                {
                    return Err(protocol("thread/turns/list items_view is invalid"));
                }
                let mut p = json!({"threadId":a["thread_id"],"limit":limit(&a["limit"])?,"sortDirection":a["sort_direction"]});
                put(
                    &mut p,
                    a,
                    &[
                        ("cursor", "cursor"),
                        ("turn_id", "turnId"),
                        ("items_view", "itemsView"),
                    ],
                );
                paged(self.request(method, p)?, method)
            }
            "thread_list" => {
                let mut p = json!({"limit":a["limit"]});
                put(&mut p, a, &[("cursor", "cursor")]);
                if truth(&self.policy["read_only_observer"]) {
                    p["useStateDbOnly"] = json!(true);
                }
                paged(self.request("thread/list", p)?, "thread/list")
            }
            "thread_loaded_list" => paged(
                self.request("thread/loaded/list", json!({}))?,
                "thread/loaded/list",
            ),
            "thread_start" => {
                tools(&a["dynamic_tools"])?;
                let creation = truth(&self.policy["shared_native_writer"])
                    && self.policy["native_creation_cwd"] == a["cwd"];
                let mut p = json!({"cwd":a["cwd"],"ephemeral":a["ephemeral"]});
                put(
                    &mut p,
                    a,
                    &[
                        ("model", "model"),
                        ("project_id", "projectId"),
                        ("sandbox", "sandbox"),
                        ("thread_source", "threadSource"),
                        ("dynamic_tools", "dynamicTools"),
                        ("developer_instructions", "developerInstructions"),
                    ],
                );
                let t = thread(self.request("thread/start", p)?, "thread/start")?;
                self.own_thread(t["id"].clone())?;
                if creation {
                    self.state["native_forwarding_thread_id"] = t["id"].clone();
                }
                Ok(t)
            }
            "thread_resume" => {
                tools(&a["dynamic_tools"])?;
                let mut p = json!({"threadId":a["thread_id"],"excludeTurns":true});
                put(
                    &mut p,
                    a,
                    &[("developer_instructions", "developerInstructions")],
                );
                let r = self.request("thread/resume", p).map_err(|mut e| {
                    if e["remote"]["code"] == -32600
                        && e["remote"]["message"]
                            == format!(
                                "thread {} already has an active writer",
                                text(&a["thread_id"])
                            )
                    {
                        e["kind"] = json!("writer_conflict");
                    }
                    e
                })?;
                let t = thread(r, "thread/resume")?;
                if t["id"] != a["thread_id"] {
                    return Err(protocol("thread/resume returned a different thread"));
                }
                self.own_thread(a["thread_id"].clone())?;
                Ok(t)
            }
            "thread_name_set" => {
                self.request(
                    "thread/name/set",
                    json!({"threadId":a["thread_id"],"name":a["name"]}),
                )?;
                Ok(Value::Null)
            }
            "thread_inject_items" => {
                let p = json!({"threadId":a["thread_id"],"items":a["items"]});
                if let Err(e) = self.request("thread/inject_items", p.clone()) {
                    if e["remote"]["code"] != -32600
                        || !text(&e["remote"]["message"])
                            .starts_with("Invalid request: unknown variant `thread/inject_items`")
                    {
                        return Err(e);
                    }
                    self.request("thread/injectItems", p)?;
                }
                Ok(Value::Null)
            }
            "model_list" => {
                let r = self.request("model/list", json!({}))?;
                if !r.is_object() {
                    return Err(protocol("model/list result must be an object"));
                }
                Ok(r)
            }
            "resolve_model" => {
                let p = self.call("model_list", json!({}))?;
                resolve(&p, &a["requested"], &a["reasoning_effort"])
            }
            "turn_start" => {
                let forwarding = self.policy["native_forwarding_thread_id"] == a["thread_id"];
                if truth(&self.policy["shared_native_writer"]) && !forwarding {
                    return Err(error(
                        "shared_writer",
                        "Managed start on the shared Desktop daemon lacks atomic writer admission",
                    ));
                }
                if !a["network_access"].is_null() && !a["network_access"].is_boolean() {
                    return Err(error("sandbox", "network_access must be a boolean"));
                }
                let mut p = json!({"threadId":a["thread_id"],"input":[{"type":"text","text":a["prompt"]}],"cwd":a["cwd"]});
                if forwarding || truth(&self.policy["rust_authority_request_id"]) {
                    let key = [
                        &self.policy["rust_authority_request_id"],
                        &self.policy["native_forwarding_request_id"],
                        &a["request_key"],
                    ]
                    .into_iter()
                    .find(|v| truth(v))
                    .unwrap();
                    p["clientUserMessageId"] = json!(client_id(text(&a["thread_id"]), text(key)));
                }
                put(
                    &mut p,
                    a,
                    &[
                        ("model", "model"),
                        ("reasoning_effort", "effort"),
                        ("approval_policy", "approvalPolicy"),
                    ],
                );
                if truth(&a["full_access"]) {
                    p["sandboxPolicy"] = json!({"type":"dangerFullAccess"});
                    p["approvalPolicy"] = json!("never");
                } else if !a["network_access"].is_null() {
                    let roots = if a["writable_roots"].is_null() {
                        json!([a["cwd"]])
                    } else {
                        a["writable_roots"].clone()
                    };
                    if !text(&a["cwd"]).starts_with('/') {
                        return Err(error(
                            "sandbox",
                            "NETWORK_ACCESS=ENABLED requires an absolute turn cwd",
                        ));
                    }
                    if !roots.as_array().is_some_and(|v| {
                        !v.is_empty() && v.iter().all(|v| text(v).starts_with('/'))
                    }) {
                        return Err(error(
                            "sandbox",
                            "NETWORK_ACCESS=ENABLED requires non-empty absolute writableRoots",
                        ));
                    }
                    p["sandboxPolicy"] = json!({"type":"workspaceWrite","writableRoots":roots,"networkAccess":a["network_access"]});
                }
                let r=self.request("turn/start",p.clone()).map_err(|e|{
                    if e.get("remote").is_some_and(|v|!v.is_null())&&(truth(&a["network_access"])||truth(&a["full_access"])){let prefix=if truth(&a["full_access"]){"TURN_START=BLOCKED: app-server rejected the requested native permissions: "}else{"NETWORK_ACCESS=ENABLED TURN_START=BLOCKED: app-server rejected or could not apply workspaceWrite network policy: "};error("sandbox",format!("{prefix}{}",text(&e["message"])))}else{e}
                })?;
                if !r.is_object() {
                    return Err(protocol("turn/start result must be an object"));
                }
                if !r["turn"]["id"].is_string() {
                    return Err(protocol("turn/start result is missing turn.id"));
                }
                if forwarding {
                    self.call("confirm",json!({"thread_id":a["thread_id"],"turn":r["turn"],"client_id":p["clientUserMessageId"],"prompt":a["prompt"]}))?;
                }
                self.state["owned_turns"][text(&a["thread_id"])] = r["turn"]["id"].clone();
                let model = optional(&r["model"]);
                let effort = optional(&r["reasoningEffort"]);
                Ok(
                    json!({"turn_id":r["turn"]["id"],"model":if truth(&model){model}else{a["model"].clone()},"reasoning_effort":if truth(&effort){effort}else{a["reasoning_effort"].clone()}}),
                )
            }
            "turn_interrupt" => {
                let r = self.request(
                    "turn/interrupt",
                    json!({"threadId":a["thread_id"],"turnId":a["turn_id"]}),
                )?;
                Ok(json!(r.is_null() || r.is_object()))
            }
            _ => Err(protocol("PROVIDER_API_OPERATION_UNKNOWN")),
        }
    }
}
pub(super) fn invoke<F: FnMut(&str, Value) -> super::Result<Value>>(
    operation: &str,
    a: &Value,
    mut callback: F,
) -> super::Result<Value> {
    let mut state = a["state"].clone();
    if !state.is_object() {
        state = json!({});
    }
    if !state["owned_thread_ids"].is_array() {
        state["owned_thread_ids"] = json!([]);
    }
    for k in ["owned_turns", "writer_release"] {
        if !state[k].is_object() {
            state[k] = json!({});
        }
    }
    let before = state.clone();
    let mut api = Api {
        callback: &mut callback,
        token: &a["callback_token"],
        state,
        policy: &a["policy"],
    };
    let result = api.run(
        operation.trim_start_matches("provider.client.api."),
        &a["arguments"],
    );
    let ids = api.state["owned_thread_ids"].as_array().unwrap();
    let old_ids = before["owned_thread_ids"].as_array().unwrap();
    let added = ids
        .iter()
        .filter(|id| !old_ids.contains(id))
        .collect::<Vec<_>>();
    let removed = old_ids
        .iter()
        .filter(|id| !ids.contains(id))
        .collect::<Vec<_>>();
    let mut patch =
        json!({"add_threads":added,"remove_threads":removed,"owned_turns":{},"writer_release":{}});
    for name in ["owned_turns", "writer_release"] {
        for (key, value) in api.state[name].as_object().unwrap() {
            if before[name].get(key) != Some(value) {
                patch[name][key] = value.clone();
            }
        }
    }
    let mut out = json!({"state":api.state,"patch":patch});
    match result {
        Ok(v) => out["value"] = v,
        Err(e) => out["error"] = e,
    };
    Ok(out)
}
