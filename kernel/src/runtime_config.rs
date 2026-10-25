//! Existing configuration contract. TOML decoding and platform path IO are callbacks;
//! all defaults, binding, validation and workflow rules live here, never task state.
use crate::{node_tls::Result, read_projection::truth};
use regex::Regex;
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
mod native;
pub use native::{
    api_config, compile_config, expanduser, hostname, native_load, provider_endpoints,
};

fn string(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        Value::Object(_) if wire_number(v, "__config_int").is_some() => {
            wire_number(v, "__config_int").unwrap().into()
        }
        Value::Object(_) if wire_number(v, "__config_float").is_some() => {
            wire_number(v, "__config_float").unwrap().into()
        }
        _ => v.to_string(),
    }
}
fn get(v: &Value, key: &str, default: Value) -> Value {
    v.get(key).cloned().unwrap_or(default)
}
fn text(v: &Value, key: &str, default: &str) -> String {
    string(&get(v, key, json!(default)))
}
fn optional(v: &Value) -> Value {
    if truth(v) {
        json!(string(v).trim())
    } else {
        Value::Null
    }
}
fn quoted(s: &str) -> String {
    // Python repr(str), including quote selection and non-printable Unicode.
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let nonprint = Regex::new(r"[\p{C}\p{Z}]").unwrap();
    let mut result = String::from(quote);
    for c in s.chars() {
        match c {
            '\\' => result.push_str("\\\\"),
            '\n' => result.push_str("\\n"),
            '\r' => result.push_str("\\r"),
            '\t' => result.push_str("\\t"),
            c if c == quote => {
                result.push('\\');
                result.push(c);
            }
            c if c != ' ' && nonprint.is_match(&c.to_string()) => {
                let n = c as u32;
                result.push_str(&if n <= 0xff {
                    format!("\\x{n:02x}")
                } else if n <= 0xffff {
                    format!("\\u{n:04x}")
                } else {
                    format!("\\U{n:08x}")
                });
            }
            c => result.push(c),
        }
    }
    result.push(quote);
    result
}
fn canonical(v: &str) -> Result<String> {
    let s = v.trim().to_lowercase();
    let s = s.strip_prefix("workstation-").unwrap_or(&s);
    if s.is_empty() {
        Err("host identity is required".into())
    } else {
        Ok(s.into())
    }
}
fn numeric_type(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
        Value::Bool(_) => "bool",
        Value::String(_) => "str",
        _ => "float",
    }
}
fn float_wire(n: f64) -> Value {
    if n.is_finite() {
        json!(n)
    } else {
        json!({"__config_float":if n.is_nan(){"nan"}else if n.is_sign_negative(){"-inf"}else{"inf"}})
    }
}
fn wire_number<'a>(v: &'a Value, key: &str) -> Option<&'a str> {
    v.as_object().filter(|o| o.len() == 1)?.get(key)?.as_str()
}
fn number(v: &Value) -> Result<f64> {
    if let Some(s) = wire_number(v, "__config_float") {
        return s
            .parse()
            .map_err(|_| "VALUE_ERROR:invalid float wire value".into());
    }
    if let Some(s) = wire_number(v, "__config_int") {
        let n = s
            .parse::<f64>()
            .map_err(|_| "OVERFLOW_ERROR:int too large to convert to float")?;
        return if n.is_finite() {
            Ok(n)
        } else {
            Err("OVERFLOW_ERROR:int too large to convert to float".into())
        };
    }
    if let Some(n) = v.as_f64() {
        return Ok(n);
    }
    if let Some(b) = v.as_bool() {
        return Ok(if b { 1. } else { 0. });
    }
    if let Some(s) = v.as_str() {
        let normalized: String = s
            .trim()
            .chars()
            .map(|c| {
                crate::python_numeric::integer_text(&c.to_string())
                    .ok()
                    .filter(|n| n.len() == 1)
                    .unwrap_or_else(|| c.to_string())
            })
            .collect();
        let decimal = Regex::new(r"^[+-]?(?:[0-9](?:_?[0-9])*(?:\.(?:[0-9](?:_?[0-9])*)?)?|\.[0-9](?:_?[0-9])*)(?:[eE][+-]?[0-9](?:_?[0-9])*)?$").unwrap();
        let special = Regex::new(r"(?i)^[+-]?(?:inf(?:inity)?|nan)$").unwrap();
        if (decimal.is_match(&normalized) || special.is_match(&normalized))
            && let Ok(n) = normalized.replace('_', "").parse::<f64>()
        {
            return Ok(n);
        }
        return Err(format!(
            "VALUE_ERROR:could not convert string to float: {}",
            quoted(s)
        ));
    }
    Err(format!(
        "TYPE_ERROR:float() argument must be a string or a real number, not '{}'",
        numeric_type(v)
    ))
}
fn integer(v: &Value) -> Result<Value> {
    let text = if let Some(s) = wire_number(v, "__config_int") {
        s.to_string()
    } else if let Some(s) = v.as_str() {
        match crate::python_numeric::integer_text(s) {
            Ok(n) => n,
            Err(()) => {
                let trimmed = s.trim();
                let raw = trimmed
                    .strip_prefix('+')
                    .or_else(|| trimmed.strip_prefix('-'))
                    .unwrap_or(trimmed);
                let digits = raw.chars().filter(|c| *c != '_').count();
                let grammar = !raw.is_empty()
                    && !raw.starts_with('_')
                    && !raw.ends_with('_')
                    && !raw.contains("__")
                    && raw
                        .chars()
                        .filter(|c| *c != '_')
                        .all(|c| crate::python_numeric::integer_text(&c.to_string()).is_ok());
                if grammar && digits > 4300 {
                    return Err(format!(
                        "VALUE_ERROR:Exceeds the limit (4300 digits) for integer string conversion: value has {digits} digits; use sys.set_int_max_str_digits() to increase the limit"
                    ));
                }
                return Err(format!(
                    "VALUE_ERROR:invalid literal for int() with base 10: {}",
                    quoted(s).chars().take(200).collect::<String>()
                ));
            }
        }
    } else if let Some(b) = v.as_bool() {
        if b { "1" } else { "0" }.into()
    } else if let Some(n) = v.as_i64() {
        n.to_string()
    } else if let Some(n) = v.as_u64() {
        n.to_string()
    } else if v.is_number() || wire_number(v, "__config_float").is_some() {
        let n = number(v)?;
        if n.is_nan() {
            return Err("VALUE_ERROR:cannot convert float NaN to integer".into());
        }
        if n.is_infinite() {
            return Err("OVERFLOW_ERROR:cannot convert float infinity to integer".into());
        }
        format!("{:.0}", n.trunc())
    } else {
        return Err(format!(
            "TYPE_ERROR:int() argument must be a string, a bytes-like object or a real number, not '{}'",
            numeric_type(v)
        ));
    };
    let negative = text.starts_with('-');
    let digits = text.trim_start_matches('-').trim_start_matches('0');
    let normalized = if digits.is_empty() {
        "0".to_string()
    } else {
        format!("{}{digits}", if negative { "-" } else { "" })
    };
    if let Ok(n) = normalized.parse::<i64>() {
        Ok(json!(n))
    } else if let Ok(n) = normalized.parse::<u64>() {
        Ok(json!(n))
    } else {
        Ok(json!({"__config_int":normalized}))
    }
}
fn integer_below(v: &Value, lower: u64) -> bool {
    if let Some(n) = v.as_u64() {
        n < lower
    } else if let Some(n) = v.as_i64() {
        n < 0 || (n as u64) < lower
    } else {
        wire_number(v, "__config_int").is_some_and(|s| s.starts_with('-'))
    }
}
fn array<'a>(v: &'a Value, error: &str) -> Result<&'a Vec<Value>> {
    v.as_array().ok_or_else(|| error.into())
}
fn strings(v: &Value, error: &str, nonempty: bool, trim: bool) -> Result<Vec<String>> {
    let a = array(v, error)?;
    if nonempty && a.is_empty() {
        return Err(error.into());
    }
    a.iter()
        .map(|x| {
            x.as_str()
                .filter(|s| !if trim { s.trim() } else { s }.is_empty())
                .map(|s| if trim { s.trim().into() } else { s.into() })
                .ok_or_else(|| error.into())
        })
        .collect()
}
fn object(v: &Value, error: &str) -> Result<()> {
    if v.is_object() {
        Ok(())
    } else {
        Err(error.into())
    }
}
fn project_alias(v: &Value) -> String {
    for key in ["alias", "target_alias"] {
        if truth(&v[key]) {
            return string(&v[key]);
        }
    }
    string(&v["linear_name"]).to_lowercase().replace(' ', "-")
}

struct Source<'a, F> {
    a: &'a Value,
    io: F,
}
impl<F: FnMut(&str, Value) -> Result<Value>> Source<'_, F> {
    fn rows<'a>(&self, v: &'a Value, path: &[&str]) -> Vec<(String, &'a Value)> {
        let mut keys: Vec<String> = self.a["orders"]
            .as_array()
            .into_iter()
            .flatten()
            .find(|r| r[0] == json!(path))
            .and_then(|r| r[1].as_array())
            .map(|r| {
                r.iter()
                    .filter_map(|x| x.as_str().map(str::to_owned))
                    .collect()
            })
            .unwrap_or_default();
        if let Some(map) = v.as_object() {
            for k in map.keys() {
                if !keys.contains(k) {
                    keys.push(k.clone());
                }
            }
        }
        keys.into_iter()
            .filter_map(|k| v.get(&k).map(|value| (k, value)))
            .collect()
    }
    fn path(&mut self, value: &Value, expand: bool, resolve: bool) -> Result<Value> {
        (self.io)(
            "path",
            json!({"value":value,"expand":expand,"resolve":resolve}),
        )
    }
    fn load(&mut self) -> Result<Value> {
        let raw = self.a["raw"].clone();
        let linear = get(&raw, "linear", json!({}));
        let codex = get(&raw, "codex", json!({}));
        let app = get(&raw, "app_server", json!({}));
        let runtime = get(&raw, "runtime", json!({}));
        let project_tables = get(&raw, "projects", json!({}));
        let target_rows = get(&raw, "targets", json!({}));
        let thread_tables = get(&raw, "threads", json!({}));
        let workspace_tables = get(&raw, "workspaces", json!({}));
        let host = get(&raw, "host_executor", json!({}));
        let fields = [
            "team_id",
            "trigger_label",
            "todo_state",
            "running_state",
            "review_state",
        ];
        let missing: Vec<_> = fields
            .iter()
            .filter(|k| !truth(&linear[**k]))
            .map(|k| format!("linear.{k}"))
            .collect();
        if !missing.is_empty() {
            return Err(format!("Missing required config: {}", missing.join(", ")));
        }
        let mut projects = vec![];
        if truth(&project_tables) {
            object(&project_tables, "[projects.<alias>] entries must be tables")?;
            for (alias, row) in self.rows(&project_tables, &["projects"]) {
                object(row, &format!("Project {} must be a table", quoted(&alias)))?;
                let cwd = text(row, "cwd", "").trim().to_owned();
                let branch = text(row, "branch", "").trim().to_owned();
                let origin = if row["repository_origin"].is_null() {
                    Value::Null
                } else {
                    optional(&json!(string(&row["repository_origin"]).trim()))
                };
                if cwd.is_empty() || branch.is_empty() {
                    return Err(format!(
                        "Project {} requires cwd and branch; repository_origin is optional",
                        quoted(&alias)
                    ));
                }
                let policy = text(row, "unexpected_origin_policy", "FAIL")
                    .trim()
                    .to_uppercase();
                if policy != "FAIL" {
                    return Err(format!(
                        "Project {} has unsupported unexpected_origin_policy={}",
                        quoted(&alias),
                        quoted(&policy)
                    ));
                }
                let path = self.path(&json!(cwd), true, true)?;
                projects.push(json!({"linear_name":text(row,"linear_name",&alias).trim(),"repo":path["path"],"alias":alias,"target_alias":null,"repository_origin":origin,"unexpected_origin_policy":policy,"branch":branch,"read_only":truth(&row["read_only"]),"workspace_alias":optional(&row["workspace"])}));
            }
        }
        for row in array(
            &get(&raw, "project", json!([])),
            "Every [[project]] requires linear_name and repo",
        )? {
            let name = text(row, "linear_name", "").trim().to_owned();
            let repo = text(row, "repo", "").trim().to_owned();
            if name.is_empty() || repo.is_empty() {
                return Err("Every [[project]] requires linear_name and repo".into());
            }
            let path = self.path(&json!(repo), true, true)?;
            projects.push(json!({"linear_name":name,"repo":path["path"],"target_alias":optional(&json!(text(row,"target_alias","").trim())),"alias":null,"repository_origin":null,"unexpected_origin_policy":"FAIL","branch":null,"read_only":false,"workspace_alias":null}));
        }
        if projects.is_empty() {
            return Err("At least one [[project]] mapping is required".into());
        }
        object(&target_rows, "[targets.<alias>] entries must be tables")?;
        let mut targets = vec![];
        for (alias, row) in self.rows(&target_rows, &["targets"]) {
            object(row, &format!("Target {} must be a table", quoted(&alias)))?;
            let keys = [
                "ssh_alias",
                "thread_id",
                "session_id",
                "cwd",
                "repository_origin",
                "branch",
                "app_server_version",
            ];
            let missing: Vec<_> = keys
                .iter()
                .filter(|k| !truth(&row[**k]) || string(&row[**k]).trim().is_empty())
                .copied()
                .collect();
            if !missing.is_empty() {
                return Err(format!(
                    "Target {} missing required fields: {}",
                    quoted(&alias),
                    missing.join(", ")
                ));
            }
            let mut target = json!({"alias":alias,"project_id":if row["project_id"].is_null(){Value::Null}else{optional(&json!(string(&row["project_id"]).trim()))},"target_host":optional(&row["host"]).as_str().unwrap_or(""),"transport":""});
            for k in keys {
                target[k] = json!(string(&row[k]));
            }
            targets.push(target);
        }
        let aliases: BTreeSet<_> = targets.iter().map(|t| string(&t["alias"])).collect();
        let unmapped: BTreeSet<_> = projects
            .iter()
            .filter(|p| truth(&p["target_alias"]) && !aliases.contains(&string(&p["target_alias"])))
            .map(|p| string(&p["target_alias"]))
            .collect();
        if !unmapped.is_empty() && !truth(&thread_tables) {
            return Err(format!(
                "Project mappings reference unknown target aliases: {}",
                unmapped.into_iter().collect::<Vec<_>>().join(", ")
            ));
        }
        let default_command = json!(["codex", "app-server", "proxy"]);
        let remote = if app["remote_command"].is_array() && truth(&app["remote_command"]) {
            app["remote_command"].clone()
        } else {
            default_command.clone()
        };
        let command = get(&app, "command", default_command);
        if !command.is_array() || !truth(&command) {
            return Err("[app_server].command must be a non-empty TOML string array".into());
        }
        for key in ["local_socket", "native_socket"] {
            if !app[key].is_null() && app[key].as_str().is_none_or(|s| s.trim().is_empty()) {
                return Err(format!(
                    "[app_server].{key} must be a non-empty socket path"
                ));
            }
        }
        let mut native_home = Value::Null;
        if !app["native_home"].is_null() {
            let value = &app["native_home"];
            if value.as_str().is_none_or(|s| s.trim().is_empty()) {
                return Err(
                    "[app_server].native_home must be an absolute native history root".into(),
                );
            }
            if self.path(value, true, false)?["absolute"] != true {
                return Err(
                    "[app_server].native_home must be an absolute native history root".into(),
                );
            }
            native_home = self.path(value, true, true)?["path"].clone();
        }
        let transport = text(&app, "transport", "local").trim().to_lowercase();
        if transport != "local" && transport != "ssh" {
            return Err("[app_server].transport must be 'local' or 'ssh'".into());
        }
        let ssh_args = get(&app, "ssh_args", json!(["-T"]));
        array(
            &ssh_args,
            "[app_server].ssh_args must be a TOML string array",
        )?;
        let timeout = number(&get(&app, "request_timeout_seconds", json!(30)))?;
        if timeout <= 0. {
            return Err("request_timeout_seconds must be positive".into());
        }
        let poll = integer(&get(&linear, "poll_interval_seconds", json!(15)))?;
        if integer_below(&poll, 5) {
            return Err("poll_interval_seconds must be >= 5".into());
        }
        let max_batch = integer(&get(&linear, "max_batch", json!(1)))?;
        if max_batch.as_u64().is_none_or(|n| !(1..=10).contains(&n)) {
            return Err("max_batch must be between 1 and 10".into());
        }
        let mut threads = vec![];
        if truth(&thread_tables) {
            object(
                &thread_tables,
                "[threads.<project>.<alias>] entries must be tables",
            )?;
            for (project, aliases) in self.rows(&thread_tables, &["threads"]) {
                object(
                    aliases,
                    &format!("Threads for project {} must be tables", quoted(&project)),
                )?;
                for (alias, row) in self.rows(aliases, &["threads", &project]) {
                    object(
                        row,
                        &format!("Thread {}.{} must be a table", project, quoted(&alias)),
                    )?;
                    let keys = ["ssh_alias", "thread_id", "session_id", "app_server_version"];
                    let missing: Vec<_> = keys
                        .iter()
                        .filter(|k| !truth(&row[**k]) || string(&row[**k]).trim().is_empty())
                        .copied()
                        .collect();
                    if !missing.is_empty() {
                        return Err(format!(
                            "Thread {project}.{alias} missing required fields: {}",
                            missing.join(", ")
                        ));
                    }
                    let mut thread = json!({"alias":alias,"project_alias":project,"project_id":optional(&row["project_id"]),"target_host":optional(&row["host"]).as_str().unwrap_or("")});
                    for k in keys {
                        thread[k] = json!(string(&row[k]));
                    }
                    threads.push(thread);
                }
            }
        }
        if threads.is_empty() {
            for target in &targets {
                let alias = projects
                    .iter()
                    .find(|p| p["target_alias"] == target["alias"])
                    .map(project_alias)
                    .unwrap_or_else(|| string(&target["alias"]));
                let mut thread = json!({"alias":"current","project_alias":alias});
                for k in [
                    "ssh_alias",
                    "thread_id",
                    "session_id",
                    "project_id",
                    "app_server_version",
                    "target_host",
                ] {
                    thread[k] = target[k].clone();
                }
                threads.push(thread);
            }
        }
        let mut workspaces = vec![];
        if truth(&workspace_tables) {
            object(
                &workspace_tables,
                "[workspaces.<alias>] entries must be tables",
            )?;
            for (alias, row) in self.rows(&workspace_tables, &["workspaces"]) {
                object(
                    row,
                    &format!("Workspace {} must be a table", quoted(&alias)),
                )?;
                let root = text(row, "root", "").trim().to_owned();
                if root.is_empty() {
                    return Err(format!("Workspace {} requires root", quoted(&alias)));
                }
                let ids = strings(
                    &get(row, "codex_host_ids", json!([])),
                    &format!(
                        "Workspace {} codex_host_ids must be a string array",
                        quoted(&alias)
                    ),
                    false,
                    false,
                )?;
                let path = self.path(&json!(root), true, true)?;
                workspaces.push(json!({"alias":alias,"root":path["path"],"allow_existing_projects":truth(&get(row,"allow_existing_projects",json!(true))),"allow_new_projects":truth(&row["allow_new_projects"]),"codex_host_ids":ids,"host":optional(&row["host"]),"ssh_alias":optional(&row["ssh_alias"])}));
            }
        }
        let task_db = if truth(&runtime["task_db_path"]) {
            self.path(&json!(string(&runtime["task_db_path"])), true, true)?["path"].clone()
        } else {
            Value::Null
        };
        let configured = optional(&runtime["runtime_host"]);
        let runtime_host = canonical(if truth(&configured) {
            configured.as_str().unwrap()
        } else {
            self.a["hostname"].as_str().unwrap_or("")
        })?;
        let host_executor = self.host(&host, &runtime_host)?;
        let log = self.path(
            &get(
                &runtime,
                "log_dir",
                json!("~/.local/state/linear-local-codex-bridge"),
            ),
            true,
            true,
        )?;
        let mut result = json!({"poll_interval_seconds":poll,"max_batch":max_batch,"codex_binary":text(&codex,"binary","codex"),"sandbox":text(&codex,"sandbox","workspace-write"),"approval":text(&codex,"approval","never"),"log_dir":log["path"],"projects":projects,"targets":targets,"threads":threads,"workspaces":workspaces,"task_db_path":task_db,"runtime_host":runtime_host,"host_executor":host_executor,"app_server":{"local_socket":app["local_socket"],"native_socket":app["native_socket"],"native_home":native_home,"transport":transport,"command":command.as_array().unwrap().iter().map(string).collect::<Vec<_>>(),"ssh_binary":text(&app,"ssh_binary","ssh"),"ssh_alias":optional(&app["ssh_alias"]),"ssh_args":ssh_args.as_array().unwrap().iter().map(string).collect::<Vec<_>>(),"remote_command":remote.as_array().unwrap().iter().map(string).collect::<Vec<_>>(),"request_timeout_seconds":float_wire(timeout),"client_name":text(&app,"client_name","linear-local-codex-bridge"),"client_title":text(&app,"client_title","Linear Local Codex Bridge"),"client_version":text(&app,"client_version","1.0.0-m6")}});
        for key in fields {
            result[key] = json!(string(&linear[key]));
        }
        Ok(result)
    }
    fn host(&mut self, h: &Value, runtime_host: &str) -> Result<Value> {
        object(h, "[host_executor] must be a table")?;
        let enabled = truth(&h["enabled"]);
        let host = text(h, "host", runtime_host).trim().to_owned();
        let timeout = number(&get(h, "default_timeout_seconds", json!(30)))?;
        let max_timeout = number(&get(h, "max_timeout_seconds", json!(120)))?;
        let max_output = integer(&get(h, "max_output_bytes", json!(65536)))?;
        let err = "[host_executor].trusted_workspace_roots must be absolute non-root paths";
        let roots = get(h, "trusted_workspace_roots", json!([]));
        let mut trusted = vec![];
        for root in array(&roots, err)? {
            if !root.is_string() || self.path(root, false, false)?["absolute"] != true {
                return Err(err.into());
            }
            let path = self.path(root, false, true)?["path"].clone();
            if path == "/" {
                return Err(err.into());
            }
            if !trusted.contains(&path) {
                trusted.push(path);
            }
        }
        if timeout <= 0. || max_timeout < timeout {
            return Err("host executor timeout bounds are invalid".into());
        }
        if max_output
            .as_u64()
            .is_none_or(|n| !(1024..=1048576).contains(&n))
        {
            return Err("host executor max_output_bytes must be between 1024 and 1048576".into());
        }
        if enabled && canonical(&host)? != canonical(runtime_host)? {
            return Err("HOST_EXECUTOR must run on the configured runtime host".into());
        }
        let target_list = |name: &str, classes: Value| -> Result<Vec<Value>> {
            Ok(strings(&get(h,name,json!([])),&format!("[host_executor].{name} must be a string array"),false,true)?.into_iter().map(|alias|json!({"alias":alias,"operation_classes":classes,"dns_name":null,"url":null,"commands":[]})).collect())
        };
        let services = target_list(
            "services",
            json!(["READ_ONLY_HOST", "DEVELOPMENT_MUTATION"]),
        )?;
        let mut ssh = vec![];
        if h["ssh_target_commands"].is_null() {
            ssh = target_list("ssh_targets", json!(["PRODUCTION_READ_ONLY"]))?;
        } else {
            object(
                &h["ssh_target_commands"],
                "[host_executor.ssh_target_commands] must contain tables",
            )?;
            for (alias, row) in self.rows(
                &h["ssh_target_commands"],
                &["host_executor", "ssh_target_commands"],
            ) {
                object(
                    row,
                    &format!("ssh target {} must be a table", quoted(&alias)),
                )?;
                let classes = strings(
                    &get(row, "operation_classes", json!(["PRODUCTION_READ_ONLY"])),
                    "ssh target operation_classes must be a string array",
                    false,
                    true,
                )?;
                let commands = get(row, "commands", json!({}));
                object(&commands, "ssh target commands must be a table")?;
                let mut parsed = vec![];
                for (name, argv) in self.rows(
                    &commands,
                    &["host_executor", "ssh_target_commands", &alias, "commands"],
                ) {
                    let err = "ssh target command must be a named non-empty argv array";
                    if name.trim().is_empty() {
                        return Err(err.into());
                    }
                    parsed.push(json!([name.trim(), strings(argv, err, true, false)?]));
                }
                ssh.push(json!({"alias":alias,"operation_classes":classes.iter().map(|s|s.to_uppercase()).collect::<Vec<_>>(),"commands":parsed,"dns_name":null,"url":null}));
            }
        }
        let mut local = vec![];
        let locals = get(h, "local_commands", json!({}));
        object(
            &locals,
            "[host_executor.local_commands] must contain tables",
        )?;
        for (alias, row) in self.rows(&locals, &["host_executor", "local_commands"]) {
            object(
                row,
                &format!("local command {} must be a table", quoted(&alias)),
            )?;
            let classes = strings(
                &get(row, "operation_classes", json!([])),
                "local command operation_classes must be a non-empty string array",
                true,
                true,
            )?;
            let argv = strings(
                &row["argv"],
                "local command argv must be a non-empty string array",
                true,
                false,
            )?;
            local.push(json!({"alias":alias,"operation_classes":classes.iter().map(|s|s.to_uppercase()).collect::<Vec<_>>(),"commands":[[alias,argv]],"dns_name":null,"url":null}));
        }
        let mut network = vec![];
        let nets = get(h, "network_targets", json!({}));
        object(&nets, "[host_executor.network_targets] must contain tables")?;
        for (alias, row) in self.rows(&nets, &["host_executor", "network_targets"]) {
            object(
                row,
                &format!(
                    "host executor network target {} must be a table",
                    quoted(&alias)
                ),
            )?;
            let dns = optional(&json!(text(row, "dns_name", "").trim()));
            let url = optional(&json!(text(row, "url", "").trim()));
            if dns.is_null() && url.is_null() {
                return Err(format!(
                    "host executor network target {} is empty",
                    quoted(&alias)
                ));
            }
            if url.as_str().is_some_and(|s| !s.starts_with("https://")) {
                return Err("host executor network targets require https URLs".into());
            }
            let classes = strings(
                &get(row, "operation_classes", json!(["READ_ONLY_HOST"])),
                "network target operation_classes must be a string array",
                false,
                true,
            )?;
            network.push(json!({"alias":alias,"operation_classes":classes.iter().map(|s|s.to_uppercase()).collect::<Vec<_>>(),"commands":[],"dns_name":dns,"url":url}));
        }
        let workflows = self.workflows(&get(h, "workflows", json!({})))?;
        Ok(
            json!({"enabled":enabled,"host":host,"default_timeout_seconds":float_wire(timeout),"max_timeout_seconds":float_wire(max_timeout),"max_output_bytes":max_output,"trusted_workspace_roots":trusted,"services":services,"ssh_targets":ssh,"local_commands":local,"network_targets":network,"workflows":workflows}),
        )
    }
    fn workflows(&self, rows: &Value) -> Result<Vec<Value>> {
        let failure = |s: &str| format!("VALUE_ERROR:{s}");
        object(
            rows,
            &failure("host_executor.workflows must contain tables"),
        )?;
        let safe = Regex::new(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$").unwrap();
        let name_re = Regex::new(r"^[a-z][a-z0-9_]{0,63}$").unwrap();
        let placeholder = Regex::new(r"\{([a-z][a-z0-9_]{0,63})\}").unwrap();
        let fixed = Regex::new(r"^/[A-Za-z0-9_./-]+$").unwrap();
        let mut result = vec![];
        let mut identities = BTreeSet::new();
        let mut resources = BTreeMap::new();
        for (_, r) in self.rows(rows, &["host_executor", "workflows"]) {
            let keys = [
                "identity",
                "target",
                "action",
                "operation_class",
                "argv",
                "parameters",
                "resource",
            ];
            if r.as_object()
                .is_none_or(|o| o.len() != keys.len() || keys.iter().any(|k| !o.contains_key(*k)))
            {
                return Err(failure(
                    "workflow registration requires exact identity, target, action, class, argv, parameters, resource",
                ));
            }
            for k in ["identity", "target", "action", "resource"] {
                if r[k].as_str().is_none_or(|s| !safe.is_match(s)) {
                    return Err(failure("invalid workflow registration identity"));
                }
            }
            if !matches!(
                r["operation_class"].as_str(),
                Some("PRODUCTION_READ_ONLY" | "PRODUCTION_MUTATION")
            ) {
                return Err(failure(
                    "workflow class must be an explicit production class",
                ));
            }
            let argv = strings(
                &r["argv"],
                &failure("workflow argv must be a non-empty fixed command"),
                true,
                false,
            )?;
            if argv.iter().any(|s| s.contains('\0')) {
                return Err(failure("workflow argv must be a non-empty fixed command"));
            }
            if !argv[0].starts_with('/') || argv[0].contains(['{', '}']) {
                return Err(failure(
                    "workflow executable must be a registered absolute path",
                ));
            }
            let params = r["parameters"]
                .as_object()
                .filter(|o| o.len() <= 32)
                .ok_or_else(|| failure("invalid workflow parameter schema"))?;
            let mut parsed = vec![];
            for (name, spec) in params {
                if !name_re.is_match(name) || !spec.is_object() {
                    return Err(failure("invalid workflow parameter name/schema"));
                }
                if spec.as_object().unwrap().len() == 1 && spec.get("enum").is_some() {
                    let choices = strings(
                        &spec["enum"],
                        &failure("workflow enum must contain safe identities"),
                        true,
                        false,
                    )?;
                    if choices.iter().any(|s| !safe.is_match(s)) {
                        return Err(failure("workflow enum must contain safe identities"));
                    }
                    parsed.push(json!([name, "enum", choices]));
                } else if spec.as_object().unwrap().len() == 1
                    && matches!(
                        spec["format"].as_str(),
                        Some("source_sha" | "sha256" | "identity")
                    )
                {
                    parsed.push(json!([name, spec["format"], []]));
                } else {
                    return Err(failure(
                        "unsupported workflow parameter schema; paths, hosts and argv are forbidden",
                    ));
                }
            }
            let mut used = BTreeSet::new();
            for token in argv.iter().skip(1).filter(|s| s.contains(['{', '}'])) {
                let names: Vec<_> = placeholder
                    .captures_iter(token)
                    .map(|c| c[1].to_owned())
                    .collect();
                let literal = placeholder.replace_all(token, "identity");
                let whole = placeholder
                    .find(token)
                    .is_some_and(|m| m.start() == 0 && m.end() == token.len());
                let path =
                    token.starts_with('/') && !token.starts_with("/{") && fixed.is_match(&literal);
                if names.is_empty() || literal.contains(['{', '}']) || !(whole || path) {
                    return Err(failure(
                        "workflow placeholder must be an identity token or a registered absolute path template",
                    ));
                }
                if literal.split('/').any(|p| p == "..") {
                    return Err(failure("workflow path template traversal"));
                }
                used.extend(names);
            }
            if used.iter().any(|n| !params.contains_key(n))
                || params.iter().any(|(n, s)| {
                    !used.contains(n) && s["enum"].as_array().is_none_or(|a| a.len() != 1)
                })
            {
                return Err(failure("workflow template/schema mismatch"));
            }
            let key = (
                string(&r["identity"]),
                string(&r["target"]),
                string(&r["action"]),
            );
            if !identities.insert(key) {
                return Err(failure("duplicate workflow registration"));
            }
            let target = string(&r["target"]);
            let resource = string(&r["resource"]);
            if resources.get(&target).is_some_and(|v| v != &resource) {
                return Err(failure(
                    "one production target must share one resource fence",
                ));
            }
            resources.insert(target, resource);
            let mut workflow = r.clone();
            workflow["parameters"] = json!(parsed);
            result.push(workflow);
        }
        Ok(result)
    }
}

pub fn invoke<F: FnMut(&str, Value) -> Result<Value>>(
    operation: &str,
    a: &Value,
    io: F,
) -> Result<Value> {
    let mut source = Source { a, io };
    match operation {
        "native_load" => native_load(std::path::Path::new(a["path"].as_str().ok_or("CONFIG_PATH_INVALID")?)),
        "api_config" => serde_json::to_value(api_config(&a["config"])?).map_err(|_| "API_CONFIG_INVALID".into()),
        "load" => source.load(),
        "workflows" => source.workflows(&a["rows"]).map(|v| json!(v)),
        "canonical_host" => {
            canonical(a["value"].as_str().ok_or("host identity is required")?).map(|v| json!(v))
        }
        "resolve_transport" => {
            if canonical(a["runtime_host"].as_str().unwrap_or(""))?
                == canonical(a["target_host"].as_str().unwrap_or(""))?
            {
                Ok(json!("local"))
            } else if a["remote_transport"] == "ssh" {
                Ok(json!("ssh"))
            } else {
                Err(
                    "cross-host app-server dispatch requires configured remote transport 'ssh'"
                        .into(),
                )
            }
        }
        "project_alias" => Ok(json!(project_alias(&a["project"]))),
        "workflow_schema" | "workflow_render" => {
            let w: crate::host_executor::Workflow =
                serde_json::from_value(a["workflow"].clone())
                    .map_err(|_| "VALUE_ERROR:invalid workflow registration")?;
            if operation == "workflow_schema" {
                return Ok(w.schema());
            }
            let values = &a["parameters"];
            if values.as_object().is_none_or(|o| {
                o.len() != w.parameters.len()
                    || w.parameters.iter().any(|(n, _, _)| !o.contains_key(n))
            }) {
                return Err("VALUE_ERROR:INVALID_ARGUMENTS: workflow parameters must match the registered schema".into());
            }
            for (name, kind, choices) in &w.parameters {
                let valid = values[name].as_str().is_some_and(|v| {
                    if kind == "enum" {
                        choices.iter().any(|c| c == v)
                    } else {
                        let pattern = match kind.as_str() {
                            "source_sha" => "[0-9a-f]{40}",
                            "sha256" => "[0-9a-f]{64}",
                            "identity" => "[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",
                            _ => "(?!)",
                        };
                        Regex::new(&format!("^{pattern}$")).is_ok_and(|re| re.is_match(v))
                    }
                });
                if !valid {
                    return Err(format!(
                        "VALUE_ERROR:INVALID_ARGUMENTS: invalid workflow parameter {name}"
                    ));
                }
            }
            w.render(values).map(|v| json!(v))
        }
        _ => Err("CONFIG_OPERATION_UNKNOWN".into()),
    }
}
