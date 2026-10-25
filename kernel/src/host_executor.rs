//! Host command construction and process execution. This module never opens a
//! business database. Every physical dispatch requires a fresh Authority permit.
use crate::{native_transport::Authority, provider::Result};
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    io::{Read, Seek, SeekFrom},
    os::unix::{
        fs::MetadataExt,
        process::{CommandExt, ExitStatusExt},
    },
    path::{Component, Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};

pub const DEVELOPMENT: &[&str] = &[
    "LOCAL_HOST_PROCESS",
    "SYSTEMD_USER",
    "HOST_FILESYSTEM",
    "GIT",
    "DOCKER",
    "POSTGRES",
];
const READ_CLASSES: &[&str] = &[
    "READ_ONLY_HOST",
    "DEVELOPMENT_MUTATION",
    "PRODUCTION_READ_ONLY",
    "PRODUCTION_MUTATION",
];
pub const CAPS: &[(&str, &str)] = &[
    ("LOCAL_HOST_PROCESS", "host_process"),
    ("SYSTEMD_USER", "systemd_user"),
    ("OUTBOUND_NETWORK", "network"),
    ("SSH", "ssh"),
    ("HOST_FILESYSTEM", "filesystem"),
    ("AWS_CLI", "aws_cli"),
    ("CLOUDFLARE_CLI", "cloudflare_cli"),
    ("CLOUD_API", "cloud_api"),
    ("GIT", "git"),
    ("DOCKER", "docker"),
    ("POSTGRES", "postgres"),
    ("TAILSCALE", "tailscale"),
];

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Target {
    pub alias: String,
    pub operation_classes: Vec<String>,
    pub dns_name: Option<String>,
    pub url: Option<String>,
    #[serde(default)]
    pub commands: Vec<(String, Vec<String>)>,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Workflow {
    pub identity: String,
    pub target: String,
    pub action: String,
    pub operation_class: String,
    pub argv: Vec<String>,
    pub parameters: Vec<(String, String, Vec<String>)>,
    pub resource: String,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct Config {
    pub enabled: bool,
    pub host: String,
    pub default_timeout_seconds: f64,
    pub max_timeout_seconds: f64,
    pub max_output_bytes: usize,
    pub services: Vec<Target>,
    pub ssh_targets: Vec<Target>,
    pub network_targets: Vec<Target>,
    pub local_commands: Vec<Target>,
    pub trusted_workspace_roots: Vec<PathBuf>,
    pub workflows: Vec<Workflow>,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            enabled: false,
            host: "p620".into(),
            default_timeout_seconds: 30.,
            max_timeout_seconds: 120.,
            max_output_bytes: 65536,
            services: vec![],
            ssh_targets: vec![],
            network_targets: vec![],
            local_commands: vec![],
            trusted_workspace_roots: vec![],
            workflows: vec![],
        }
    }
}
fn matches(pattern: &str, value: &str) -> bool {
    Regex::new(pattern).expect("constant regex").is_match(value)
}
fn text<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    v[key]
        .as_str()
        .filter(|s| !s.trim().is_empty() && !s.contains('\0'))
        .map(str::trim)
        .ok_or_else(|| "INVALID_ARGUMENTS".into())
}
pub fn call<A: Authority>(authority: &mut A, op: &str, payload: Value) -> Result<Value> {
    authority.call(op, payload, &crate::authority_store::random_id()?)
}
fn now() -> String {
    chrono::DateTime::<chrono::Utc>::from(std::time::SystemTime::now())
        .to_rfc3339_opts(chrono::SecondsFormat::Micros, true)
}

impl Workflow {
    pub fn operation(&self) -> String {
        format!("workflow:{}:{}", self.identity, self.action)
    }
    pub(crate) fn schema(&self) -> Value {
        let properties:BTreeMap<_,_>=self.parameters.iter().map(|(n,k,c)| (n.clone(),if k=="enum" {json!({"type":"string","enum":c})} else {json!({"type":"string","pattern":format!("^{}$",format_pattern(k).unwrap_or("(?!)"))})})).collect();
        json!({"type":"object","additionalProperties":false,"required":self.parameters.iter().map(|p|&p.0).collect::<Vec<_>>(),"properties":properties})
    }
    pub(crate) fn render(&self, values: &Value) -> Result<Vec<String>> {
        let obj = values.as_object().ok_or("INVALID_ARGUMENTS")?;
        if obj.len() != self.parameters.len() {
            return Err("INVALID_ARGUMENTS".into());
        }
        for (n, k, c) in &self.parameters {
            let v = text(values, n)?;
            if values[n] != v
                || if k == "enum" {
                    !c.iter().any(|x| x == v)
                } else {
                    !matches(&format!("^{}$", format_pattern(k)?), v)
                }
            {
                return Err("INVALID_ARGUMENTS".into());
            }
        }
        let re = Regex::new(r"\{([a-z][a-z0-9_]*)\}").unwrap();
        Ok(self
            .argv
            .iter()
            .map(|a| {
                re.replace_all(a, |c: &regex::Captures| {
                    values[&c[1]].as_str().unwrap_or("")
                })
                .into_owned()
            })
            .collect())
    }
}
fn format_pattern(k: &str) -> Result<&'static str> {
    match k {
        "source_sha" => Ok("[0-9a-f]{40}"),
        "sha256" => Ok("[0-9a-f]{64}"),
        "identity" => Ok("[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"),
        _ => Err("INVALID_WORKFLOW_SCHEMA".into()),
    }
}
impl Config {
    pub fn load(path: &Path) -> Result<Self> {
        let c: Self = serde_json::from_slice(&fs::read(path).map_err(|_| "HOST_CONFIG_REQUIRED")?)
            .map_err(|_| "INVALID_HOST_CONFIG")?;
        c.validate()?;
        Ok(c)
    }
    pub fn validate(&self) -> Result<()> {
        if self.host.is_empty()
            || !self.default_timeout_seconds.is_finite()
            || !self.max_timeout_seconds.is_finite()
            || self.default_timeout_seconds <= 0.
            || self.max_timeout_seconds < self.default_timeout_seconds
            || !(1024..=1048576).contains(&self.max_output_bytes)
            || self
                .trusted_workspace_roots
                .iter()
                .any(|p| !p.is_absolute() || p == Path::new("/"))
        {
            return Err("INVALID_HOST_CONFIG".into());
        }
        for t in self
            .services
            .iter()
            .chain(&self.ssh_targets)
            .chain(&self.network_targets)
            .chain(&self.local_commands)
        {
            if t.alias.trim().is_empty()
                || t.operation_classes
                    .iter()
                    .any(|s| !READ_CLASSES.contains(&s.as_str()))
                || t.commands.iter().any(|(n, a)| {
                    n.is_empty()
                        || a.is_empty()
                        || a.iter().any(|s| s.is_empty() || s.contains('\0'))
                })
                || t.url.as_ref().is_some_and(|s| !s.starts_with("https://"))
            {
                return Err("INVALID_HOST_CONFIG".into());
            }
        }
        let mut keys = std::collections::BTreeSet::new();
        let mut resources = BTreeMap::new();
        for w in &self.workflows {
            if [&w.identity, &w.target, &w.action, &w.resource]
                .iter()
                .any(|s| !matches("^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", s))
                || !["PRODUCTION_READ_ONLY", "PRODUCTION_MUTATION"]
                    .contains(&w.operation_class.as_str())
                || w.argv.is_empty()
                || !Path::new(&w.argv[0]).is_absolute()
                || w.argv[0].contains(['{', '}'])
                || w.argv.iter().any(|s| s.is_empty() || s.contains('\0'))
                || w.parameters.len() > 32
                || !keys.insert((&w.identity, &w.target, &w.action))
            {
                return Err("INVALID_WORKFLOW_CONFIG".into());
            }
            if resources
                .insert(&w.target, &w.resource)
                .is_some_and(|r| r != &w.resource)
            {
                return Err("INVALID_WORKFLOW_CONFIG".into());
            }
            let mut names = std::collections::BTreeSet::new();
            for (n, k, c) in &w.parameters {
                if !matches("^[a-z][a-z0-9_]{0,63}$", n)
                    || !names.insert(n)
                    || if k == "enum" {
                        c.is_empty()
                            || c.iter()
                                .any(|s| !matches("^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", s))
                    } else {
                        format_pattern(k).is_err() || !c.is_empty()
                    }
                {
                    return Err("INVALID_WORKFLOW_CONFIG".into());
                }
            }
            let re = Regex::new(r"\{([a-z][a-z0-9_]{0,63})\}").unwrap();
            let mut used = std::collections::BTreeSet::new();
            for a in w.argv.iter().skip(1).filter(|s| s.contains(['{', '}'])) {
                for m in re.captures_iter(a) {
                    used.insert(m[1].to_owned());
                }
                let literal = re.replace_all(a, "identity");
                if literal.contains(['{', '}'])
                    || !(matches(r"^\{[a-z][a-z0-9_]{0,63}\}$", a)
                        || (a.starts_with('/')
                            && !a.starts_with("/{")
                            && matches("^/[A-Za-z0-9_./-]+$", &literal)))
                    || Path::new(literal.as_ref())
                        .components()
                        .any(|c| c == Component::ParentDir)
                {
                    return Err("INVALID_WORKFLOW_CONFIG".into());
                }
            }
            if used.iter().any(|n| !names.contains(n))
                || w.parameters
                    .iter()
                    .any(|(n, k, c)| !used.contains(n) && (k != "enum" || c.len() != 1))
            {
                return Err("INVALID_WORKFLOW_CONFIG".into());
            }
        }
        Ok(())
    }
    pub fn catalog(&self) -> Value {
        let mut out = json!({});
        for (cap, _) in CAPS {
            out[*cap] = json!({});
        }
        let services: Vec<_> = self.services.iter().map(target_public).collect();
        add(
            &mut out,
            "LOCAL_HOST_PROCESS",
            &[
                "host_identity",
                "user_identity",
                "working_directory",
                "uptime",
            ],
            READ_CLASSES,
            &[],
            &[],
            false,
            "NONE",
            None,
        );
        add(
            &mut out,
            "LOCAL_HOST_PROCESS",
            &["service_process"],
            READ_CLASSES,
            &["target"],
            &[],
            false,
            "NONE",
            Some(&services),
        );
        add(
            &mut out,
            "SYSTEMD_USER",
            &["service_is_active", "service_status"],
            READ_CLASSES,
            &["target"],
            &[],
            false,
            "NONE",
            Some(&services),
        );
        add(
            &mut out,
            "SYSTEMD_USER",
            &["service_journal"],
            READ_CLASSES,
            &["target"],
            &["lines"],
            false,
            "NONE",
            Some(&services),
        );
        add(
            &mut out,
            "SYSTEMD_USER",
            &["service_restart"],
            &["DEVELOPMENT_MUTATION", "PRODUCTION_MUTATION"],
            &["target"],
            &[],
            true,
            "SERVICE_RESTART",
            Some(&services),
        );
        add(
            &mut out,
            "GIT",
            &["head", "status", "remote_main_head", "ahead_behind"],
            READ_CLASSES,
            &[],
            &[],
            false,
            "NONE",
            None,
        );
        add(
            &mut out,
            "GIT",
            &["fetch_origin"],
            READ_CLASSES,
            &[],
            &[],
            true,
            "LOCAL_OBJECTS_AND_REMOTE_TRACKING_REFS_ONLY",
            None,
        );
        add(
            &mut out,
            "GIT",
            &["push_current_branch"],
            &["DEVELOPMENT_MUTATION"],
            &[],
            &["remote", "target"],
            true,
            "REGISTERED_ORIGIN_NON_FORCE_CURRENT_BRANCH",
            None,
        );
        out["GIT"]["push_current_branch"]["target_kind"] = json!("TASK_OWNED_GIT_WORKTREE");
        add(
            &mut out,
            "HOST_FILESYSTEM",
            &["git_head", "git_status"],
            READ_CLASSES,
            &[],
            &[],
            false,
            "NONE",
            None,
        );
        add(
            &mut out,
            "HOST_FILESYSTEM",
            &["path_read"],
            READ_CLASSES,
            &["path"],
            &["lines"],
            false,
            "NONE",
            None,
        );
        add(
            &mut out,
            "HOST_FILESYSTEM",
            &["marker_create", "marker_remove"],
            &["DEVELOPMENT_MUTATION"],
            &["name"],
            &[],
            true,
            "PROJECT_MARKER",
            None,
        );
        for cap in DEVELOPMENT {
            add(
                &mut out,
                cap,
                &["development_command"],
                &["DEVELOPMENT_MUTATION"],
                &["argv"],
                &[],
                true,
                "TRUSTED_WORKSPACE_DEVELOPMENT",
                None,
            );
        }
        for (name, dns) in [("dns_lookup", true), ("https_head", false)] {
            let ts: Vec<_> = self
                .network_targets
                .iter()
                .filter(|t| {
                    if dns {
                        t.dns_name.is_some()
                    } else {
                        t.url.is_some()
                    }
                })
                .map(target_public)
                .collect();
            add(
                &mut out,
                "OUTBOUND_NETWORK",
                &[name],
                READ_CLASSES,
                &["target"],
                &[],
                false,
                "NONE",
                Some(&ts),
            );
        }
        let mut ssh =
            std::collections::BTreeSet::from(["remote_hostname", "remote_uptime", "remote_true"]);
        for t in &self.ssh_targets {
            for (n, _) in &t.commands {
                ssh.insert(n);
            }
        }
        for name in ssh {
            let ts: Vec<_> = self
                .ssh_targets
                .iter()
                .filter(|t| {
                    ["remote_hostname", "remote_uptime", "remote_true"].contains(&name)
                        || t.commands.iter().any(|(n, _)| n == name)
                })
                .map(target_public)
                .collect();
            let mutation = ts.iter().any(|t| {
                t["operation_classes"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|c| c == "DEVELOPMENT_MUTATION" || c == "PRODUCTION_MUTATION")
            });
            add(
                &mut out,
                "SSH",
                &[name],
                READ_CLASSES,
                &["target"],
                &[],
                mutation,
                if mutation {
                    "REGISTERED_REMOTE_COMMAND"
                } else {
                    "REMOTE_READ"
                },
                Some(&ts),
            );
        }
        for t in &self.local_commands {
            if t.commands
                .iter()
                .any(|(n, a)| n == &t.alias && !a.is_empty())
            {
                let mutation = t
                    .operation_classes
                    .iter()
                    .any(|c| c == "DEVELOPMENT_MUTATION" || c == "PRODUCTION_MUTATION");
                add(
                    &mut out,
                    "LOCAL_HOST_PROCESS",
                    &[&format!("registered_command:{}", t.alias)],
                    &t.operation_classes
                        .iter()
                        .map(String::as_str)
                        .collect::<Vec<_>>(),
                    &[],
                    &[],
                    mutation,
                    if mutation {
                        "REGISTERED_LOCAL_COMMAND"
                    } else {
                        "LOCAL_READ"
                    },
                    Some(&[target_public(t)]),
                );
            }
        }
        for w in &self.workflows {
            let name = w.operation();
            let t = json!({"identity":w.target,"operation_classes":[w.operation_class],"workflow":w.identity,"parameter_schema":w.schema(),"task_target_authorized":null});
            if !out["LOCAL_HOST_PROCESS"][&name].is_null() {
                let spec = &mut out["LOCAL_HOST_PROCESS"][&name];
                if !spec["operation_classes"]
                    .as_array()
                    .unwrap()
                    .contains(&json!(w.operation_class))
                {
                    spec["operation_classes"]
                        .as_array_mut()
                        .unwrap()
                        .push(json!(w.operation_class));
                }
                spec["registered_targets"].as_array_mut().unwrap().push(t);
            } else {
                add(
                    &mut out,
                    "LOCAL_HOST_PROCESS",
                    &[&name],
                    &[&w.operation_class],
                    &["target", "parameters"],
                    &[],
                    w.operation_class == "PRODUCTION_MUTATION",
                    "CONTROLLER_OWNED_WORKFLOW",
                    Some(&[t]),
                );
                out["LOCAL_HOST_PROCESS"][&name]["argument_schema"]["properties"]["parameters"] =
                    json!({"type":"object"});
            }
        }
        out
    }
    pub fn dynamic_tool(&self, policy: &Value) -> Value {
        let mut catalog = self.catalog();
        let scopes = policy["operation_scopes"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        let mut caps = json!({});
        for (cap, probe) in CAPS {
            let mut operations = json!({});
            for (name, spec) in catalog[*cap].as_object_mut().unwrap() {
                if !scopes.iter().any(|s| s["capability"] == *cap)
                    || !spec["operation_classes"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .any(|class| scopes.iter().any(|s| s["operation_class"] == *class))
                {
                    continue;
                }
                let targets = spec["registered_targets"]
                    .as_array()
                    .cloned()
                    .filter(|ts| !ts.is_empty())
                    .unwrap_or_else(|| vec![json!({})]);
                spec["task_authorized"] = json!(targets.iter().any(|t| scopes.iter().any(|s| {
                    s["capability"] == *cap
                        && s["operation"] == *name
                        && s["target"].as_str() == Some(t["identity"].as_str().unwrap_or(""))
                        && spec["operation_classes"]
                            .as_array()
                            .unwrap()
                            .contains(&s["operation_class"])
                })));
                if let Some(ts) = spec["registered_targets"].as_array_mut() {
                    for t in ts {
                        t["task_target_authorized"] = json!(scopes.iter().any(|s| {
                            s["capability"] == *cap
                                && s["operation"] == *name
                                && s["target"] == t["identity"]
                                && t["operation_classes"]
                                    .as_array()
                                    .unwrap()
                                    .contains(&s["operation_class"])
                        }));
                    }
                }
                operations[name] = spec.clone();
            }
            if scopes.iter().any(|s| s["capability"] == *cap) {
                caps[*cap] = json!({"request_capability":cap,"probe_key":probe,"probe":"NOT_PROBED","operations":operations});
            }
        }
        let contract = json!({"contract":"CLINX_HOST_EXECUTION_CONTRACT_V2","authority_granted":false,"effective_authority":{"source":"SEALED_EXECUTION_POLICY","operation_scopes":scopes},"trusted_workspace_roots":self.trusted_workspace_roots,"capabilities":caps});
        json!({"type":"namespace","name":"clinx","description":"Request a structured operation from the CLINX-managed trusted host executor.","tools":[{"type":"function","name":"clinx_host_operation","description":format!("Request one structured Host operation. Exact executable contract: {contract}"),"inputSchema":{"type":"object","properties":{"operation_class":{"type":"string","enum":["READ_ONLY_HOST","DEVELOPMENT_MUTATION","PRODUCTION_READ_ONLY","PRODUCTION_MUTATION","BUSINESS_ACTION"]},"capability":{"type":"string","enum":caps.as_object().unwrap().keys().collect::<Vec<_>>()},"operation":{"type":"string"},"arguments":{"type":"object"},"timeout_seconds":{"type":"number","minimum":0.1,"maximum":120}},"required":["arguments"],"additionalProperties":false}}]})
    }
}
fn target_public(t: &Target) -> Value {
    json!({"identity":t.alias,"operation_classes":t.operation_classes,"task_target_authorized":null})
}
#[allow(clippy::too_many_arguments)]
fn add(
    out: &mut Value,
    cap: &str,
    names: &[&str],
    classes: &[&str],
    required: &[&str],
    optional: &[&str],
    mutation: bool,
    effects: &str,
    targets: Option<&[Value]>,
) {
    let classes: Vec<_> = classes
        .iter()
        .filter(|c| {
            targets.is_none_or(|ts| {
                ts.is_empty()
                    || ts.iter().any(|t| {
                        t["operation_classes"]
                            .as_array()
                            .unwrap()
                            .contains(&json!(c))
                    })
            })
        })
        .copied()
        .collect();
    for name in names {
        let props: BTreeMap<_, _> = required
            .iter()
            .chain(optional)
            .map(|k| {
                (
                    *k,
                    match *k {
                        "argv" => json!({"type":"array","items":{"type":"string"},"minItems":1}),
                        "lines" => json!({"type":"integer","minimum":1,"maximum":500}),
                        _ => json!({"type":"string","minLength":1}),
                    },
                )
            })
            .collect();
        let mut spec = json!({"capability":cap,"operation":name,"description":format!("{}; {effects}",name.replace('_'," ")),"target_kind":if ["path_read","development_command"].contains(name){"TRUSTED_WORKSPACE"}else if targets.is_some(){"REGISTERED_TARGET"}else if ["GIT","HOST_FILESYSTEM"].contains(&cap){"REGISTERED_PROJECT"}else{"LOCAL_HOST"},"argument_schema":{"type":"object","required":required,"properties":props,"additionalProperties":false},"operation_class":classes.first(),"operation_classes":classes,"required_arguments":required,"optional_arguments":optional,"mutating":mutation,"side_effects":effects,"implemented":true,"client_exposed":"NOT_OBSERVED","runtime_health":"NOT_PROBED","task_authorized":null});
        if let Some(ts) = targets {
            spec["registered_targets"] = json!(ts);
        }
        out[cap][*name] = spec;
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Plan {
    pub argv: Vec<String>,
    pub target: Option<String>,
    pub mutating: bool,
    pub resource: Option<String>,
    pub cwd: PathBuf,
}
impl Plan {
    fn new(args: &[&str], cwd: &Path) -> Self {
        Self {
            argv: args.iter().map(|s| s.to_string()).collect(),
            target: None,
            mutating: false,
            resource: None,
            cwd: cwd.into(),
        }
    }
}
// Path.resolve(strict=False), including existing symlink prefixes. Missing
// output paths must still remain under their configured workspace root.
fn resolve(path: &Path) -> Result<PathBuf> {
    if let Ok(p) = path.canonicalize() {
        return Ok(p);
    }
    let mut current = PathBuf::new();
    for c in path.components() {
        match c {
            Component::ParentDir => {
                current.pop();
            }
            Component::CurDir => (),
            _ => current.push(c),
        }
        if let Ok(m) = current.symlink_metadata() {
            if m.file_type().is_symlink() {
                current = current
                    .canonicalize()
                    .map_err(|_| "TARGET_NOT_REGISTERED")?;
            }
        }
    }
    Ok(current)
}
fn home() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/"))
}
fn executor_path() -> String {
    format!(
        "{}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        home().display()
    )
}
fn which(binary: &str) -> Option<PathBuf> {
    let candidates: Vec<_> = if binary.contains('/') {
        vec![PathBuf::from(binary)]
    } else {
        executor_path()
            .split(':')
            .map(|d| Path::new(d).join(binary))
            .collect()
    };
    candidates.into_iter().find(|p| {
        p.metadata()
            .is_ok_and(|m| m.is_file() && m.mode() & 0o111 != 0)
    })
}
fn environment(command: &mut Command) {
    let uid = rustix::process::getuid().as_raw();
    let user = std::env::var("USER").unwrap_or_default();
    let runtime = std::env::var("XDG_RUNTIME_DIR").unwrap_or_else(|_| format!("/run/user/{uid}"));
    command
        .env_clear()
        .env("HOME", home())
        .env("PATH", executor_path())
        .env(
            "LANG",
            std::env::var("LANG").unwrap_or_else(|_| "C.UTF-8".into()),
        )
        .env(
            "LC_ALL",
            std::env::var("LC_ALL").unwrap_or_else(|_| "C.UTF-8".into()),
        )
        .env("USER", &user)
        .env("LOGNAME", user)
        .env("XDG_RUNTIME_DIR", &runtime)
        .env(
            "DBUS_SESSION_BUS_ADDRESS",
            std::env::var("DBUS_SESSION_BUS_ADDRESS")
                .unwrap_or_else(|_| format!("unix:path={runtime}/bus")),
        )
        .env("AWS_PROFILE", "p620");
    if let Some(s) = std::env::var_os("SSH_AUTH_SOCK") {
        command.env("SSH_AUTH_SOCK", s);
    }
}
fn git(cwd: &Path, args: &[&str]) -> Result<String> {
    // Read-only preflight; no shell, input or credentials in errors.
    let mut cmd = Command::new("git");
    environment(&mut cmd);
    cmd.args(args)
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    let mut child = cmd.spawn().map_err(|_| "TARGET_NOT_REGISTERED")?;
    let mut pipe = child.stdout.take().unwrap();
    let reader = std::thread::spawn(move || {
        let mut b = vec![];
        pipe.by_ref().take(65537).read_to_end(&mut b).ok();
        b
    });
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if let Some(status) = child.try_wait().map_err(|_| "TARGET_NOT_REGISTERED")? {
            let b = reader.join().map_err(|_| "TARGET_NOT_REGISTERED")?;
            if !status.success() || b.len() > 65536 {
                return Err("TARGET_NOT_REGISTERED".into());
            }
            return String::from_utf8(b)
                .map(|s| s.trim().to_owned())
                .map_err(|_| "TARGET_NOT_REGISTERED".into());
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            let _ = reader.join();
            return Err("TARGET_NOT_REGISTERED".into());
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}
fn branch_valid(branch: &str) -> bool {
    matches("^[A-Za-z0-9][A-Za-z0-9._/-]*$", branch)
        && !["..", "//", "@{"].iter().any(|s| branch.contains(s))
        && !branch.ends_with(['/', '.'])
        && !branch.ends_with(".lock")
}
impl Config {
    fn workspace(&self, root: &Path, value: &str, development: bool) -> Result<PathBuf> {
        let path = if value == "~" {
            home()
        } else if let Some(p) = value.strip_prefix("~/") {
            home().join(p)
        } else {
            root.join(value)
        };
        let path = resolve(&path)?;
        let roots = if self.trusted_workspace_roots.is_empty() {
            vec![root.into()]
        } else {
            self.trusted_workspace_roots.clone()
        };
        if !roots
            .iter()
            .any(|r| resolve(r).is_ok_and(|r| path.starts_with(r)))
        {
            return Err("TARGET_NOT_REGISTERED".into());
        }
        if development
            && self
                .local_commands
                .iter()
                .filter(|t| {
                    t.operation_classes
                        .iter()
                        .any(|c| c == "PRODUCTION_MUTATION")
                })
                .flat_map(|t| &t.commands)
                .any(|(_, a)| {
                    a.first().is_some_and(|p| {
                        Path::new(p).is_absolute() && resolve(Path::new(p)).is_ok_and(|p| p == path)
                    })
                })
        {
            return Err("AUTHORITY_DENIED".into());
        }
        Ok(path)
    }
    fn target<'a>(&self, targets: &'a [Target], alias: &str, class: &str) -> Result<&'a Target> {
        let t = targets
            .iter()
            .find(|t| t.alias.eq_ignore_ascii_case(alias))
            .ok_or("TARGET_NOT_REGISTERED")?;
        if !t.operation_classes.iter().any(|c| c == class) {
            return Err("AUTHORITY_DENIED".into());
        }
        Ok(t)
    }
    pub fn plan<A: Authority>(
        &self,
        authority: &mut A,
        p: &Value,
        task: &Value,
        args: &Value,
    ) -> Result<Plan> {
        if !self.enabled || self.host != p["host"] {
            return Err("HOST_EXECUTOR_UNAVAILABLE".into());
        }
        if p["execution_policy"]["execution_surface"] != "HOST_EXECUTOR"
            || task["host"] != p["host"]
            || task["project"] != p["project"]
            || resolve(Path::new(text(&task["workspace_contract"], "cwd")?))?
                != resolve(Path::new(text(&p["workspace_contract"], "cwd")?))?
        {
            return Err("HOST_EXECUTION_BINDING_CONFLICT".into());
        }
        let cap = text(args, "capability")?;
        let op = text(args, "operation")?;
        let class = text(args, "operation_class")?;
        let values = &args["arguments"];
        let cwd = Path::new(text(&p["workspace_contract"], "cwd")?);
        if class == "BUSINESS_ACTION" {
            return Err("AI_DOES_NOT_DECIDE_LIVE_BUSINESS_ACTION".into());
        }
        let spec = &self.catalog()[cap][op];
        if spec.is_null() {
            return Err(
                if cap == "LOCAL_HOST_PROCESS" && op.starts_with("registered_command:") {
                    "TARGET_NOT_REGISTERED"
                } else {
                    "OPERATION_NOT_SUPPORTED"
                }
                .into(),
            );
        }
        if !spec["operation_classes"]
            .as_array()
            .unwrap()
            .contains(&json!(class))
        {
            return Err("AUTHORITY_DENIED".into());
        }
        let obj = values.as_object().ok_or("INVALID_ARGUMENTS")?;
        let required = spec["required_arguments"].as_array().unwrap();
        let optional = spec["optional_arguments"].as_array().unwrap();
        if required
            .iter()
            .any(|k| !obj.contains_key(k.as_str().unwrap()))
            || obj
                .keys()
                .any(|k| !required.contains(&json!(k)) && !optional.contains(&json!(k)))
        {
            return Err("INVALID_ARGUMENTS".into());
        }
        for (k, v) in obj {
            match spec["argument_schema"]["properties"][k]["type"].as_str() {
                Some("string") => {
                    text(values, k)?;
                }
                Some("integer") => {
                    if !v.as_u64().is_some_and(|n| (1..=500).contains(&n)) {
                        return Err("INVALID_ARGUMENTS".into());
                    }
                }
                Some("array") => {
                    if !v.as_array().is_some_and(|a| {
                        !a.is_empty()
                            && a.iter().all(|s| {
                                s.as_str()
                                    .is_some_and(|s| !s.trim().is_empty() && !s.contains('\0'))
                            })
                    }) {
                        return Err("INVALID_ARGUMENTS".into());
                    }
                }
                Some("object") => {
                    if !v.is_object() {
                        return Err("INVALID_ARGUMENTS".into());
                    }
                }
                _ => return Err("INVALID_ARGUMENTS".into()),
            }
        }
        let target = values["target"].as_str().unwrap_or("");
        let scope_target = if spec.get("registered_targets").is_some()
            || (cap == "GIT" && op == "push_current_branch")
        {
            target
        } else {
            ""
        };
        let scope =
            json!({"capability":cap,"operation":op,"operation_class":class,"target":scope_target});
        if !p["execution_policy"]["operation_scopes"]
            .as_array()
            .is_some_and(|a| a.contains(&scope))
        {
            return Err("AUTHORITY_DENIED".into());
        }
        let mut cmd = Plan::new(&[], cwd);
        cmd.mutating = spec["mutating"] == true;
        if cap == "LOCAL_HOST_PROCESS" && op.starts_with("workflow:") {
            let w = self
                .workflows
                .iter()
                .find(|w| w.operation() == op && w.target == target)
                .ok_or("TARGET_NOT_REGISTERED")?;
            if w.operation_class != class {
                return Err("AUTHORITY_DENIED".into());
            }
            cmd.argv = w.render(&values["parameters"])?;
            cmd.target = Some(target.into());
            cmd.resource = Some(w.resource.clone());
            return self.available(cmd);
        }
        if DEVELOPMENT.contains(&cap) && op == "development_command" {
            cmd.argv = values["argv"]
                .as_array()
                .unwrap()
                .iter()
                .map(|s| s.as_str().unwrap().trim().to_owned())
                .collect();
            let executable = Path::new(&cmd.argv[0]);
            let binary = executable
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("")
                .to_ascii_lowercase();
            if cmd.argv[0].contains('/') {
                let p = resolve(&cwd.join(executable))?;
                if which(&binary).and_then(|s| resolve(&s).ok()) != Some(p) {
                    self.workspace(cwd, &cmd.argv[0], true)?;
                }
            }
            if [
                "sudo",
                "su",
                "ssh",
                "scp",
                "rsync",
                "aws",
                "wrangler",
                "kubectl",
                "terraform",
                "cloudflared",
                "systemctl",
                "env",
                "command",
                "exec",
                "busybox",
            ]
            .contains(&binary.as_str())
            {
                return Err("AUTHORITY_DENIED".into());
            }
            if binary == "git"
                && cmd.argv.iter().skip(1).any(|a| {
                    [
                        "push",
                        "fetch",
                        "pull",
                        "clone",
                        "submodule",
                        "config",
                        "remote",
                    ]
                    .contains(&a.to_ascii_lowercase().as_str())
                        || ["-c", "--config-env", "--git-dir", "--work-tree"]
                            .iter()
                            .any(|s| a.starts_with(s))
                })
            {
                return Err("AUTHORITY_DENIED".into());
            }
            if ["sh", "bash", "zsh", "fish", "dash", "ksh"].contains(&binary.as_str())
                && cmd
                    .argv
                    .iter()
                    .skip(1)
                    .any(|a| a.starts_with('-') && a[1..].contains('c'))
            {
                return Err("AUTHORITY_DENIED".into());
            }
            let mut git_cwd = cwd.to_path_buf();
            for (i, a) in cmd.argv.iter().enumerate().skip(1) {
                let value = if a.starts_with('-') {
                    a.split_once('=').map(|(_, s)| s).unwrap_or(a)
                } else {
                    a
                };
                if binary == "git" && cmd.argv[i - 1] == "-C" {
                    git_cwd = self.workspace(&git_cwd, a, false)?;
                    continue;
                }
                if binary == "git" && a.starts_with("-C") && a.len() > 2 {
                    git_cwd = self.workspace(&git_cwd, &a[2..], false)?;
                    continue;
                }
                if ["/", "./", "../", "~"].iter().any(|s| value.starts_with(s))
                    || [".", ".."].contains(&value)
                    || (!value.starts_with('-')
                        && (value.contains('/') || git_cwd.join(value).symlink_metadata().is_ok()))
                {
                    self.workspace(&git_cwd, value, true)?;
                }
            }
            cmd.target = Some(text(p, "project")?.into());
            return self.available(cmd);
        }
        match (cap, op) {
            ("LOCAL_HOST_PROCESS", "host_identity") => cmd.argv = vec!["hostname".into()],
            ("LOCAL_HOST_PROCESS", "user_identity") => cmd.argv = vec!["whoami".into()],
            ("LOCAL_HOST_PROCESS", "working_directory") => cmd.argv = vec!["pwd".into()],
            ("LOCAL_HOST_PROCESS", "uptime") => cmd.argv = vec!["uptime".into()],
            ("LOCAL_HOST_PROCESS", "service_process") | ("SYSTEMD_USER", _) => {
                let t = self.target(&self.services, target, class)?;
                let alias = t.alias.as_str();
                cmd.argv = match op {
                    "service_process" => vec![
                        "systemctl",
                        "--user",
                        "show",
                        alias,
                        "--property=MainPID",
                        "--property=ExecStart",
                        "--no-pager",
                    ],
                    "service_is_active" => vec!["systemctl", "--user", "is-active", alias],
                    "service_status" => vec!["systemctl", "--user", "status", alias, "--no-pager"],
                    "service_restart" => vec!["systemctl", "--user", "restart", alias],
                    "service_journal" => {
                        vec!["journalctl", "--user", "-u", alias, "-n", "", "--no-pager"]
                    }
                    _ => return Err("OPERATION_NOT_SUPPORTED".into()),
                }
                .iter()
                .map(|s| s.to_string())
                .collect();
                if op == "service_journal" {
                    cmd.argv[5] = values["lines"].as_u64().unwrap_or(50).to_string();
                }
                cmd.target = Some(alias.into());
            }
            ("LOCAL_HOST_PROCESS", _) if op.starts_with("registered_command:") => {
                let alias = &op[19..];
                let t = self.target(&self.local_commands, alias, class)?;
                cmd.argv = t
                    .commands
                    .iter()
                    .find(|(n, _)| n == alias)
                    .ok_or("TARGET_NOT_REGISTERED")?
                    .1
                    .clone();
                cmd.target = Some(alias.into());
            }
            ("OUTBOUND_NETWORK", _) => {
                let t = self.target(&self.network_targets, target, class)?;
                cmd.argv = match op {
                    "dns_lookup" => vec![
                        "getent",
                        "hosts",
                        t.dns_name.as_deref().ok_or("TARGET_NOT_REGISTERED")?,
                    ],
                    "https_head" => vec![
                        "curl",
                        "--head",
                        "--location",
                        "--silent",
                        "--show-error",
                        "--max-time",
                        "20",
                        t.url.as_deref().ok_or("TARGET_NOT_REGISTERED")?,
                    ],
                    _ => return Err("OPERATION_NOT_SUPPORTED".into()),
                }
                .iter()
                .map(|s| s.to_string())
                .collect();
                cmd.target = Some(t.alias.clone());
            }
            ("SSH", _) => {
                let t = self.target(&self.ssh_targets, target, class)?;
                let remote = t
                    .commands
                    .iter()
                    .find(|(n, _)| n == op)
                    .map(|(_, a)| a.clone())
                    .unwrap_or_else(|| {
                        vec![
                            match op {
                                "remote_hostname" => "hostname",
                                "remote_uptime" => "uptime",
                                _ => "true",
                            }
                            .into(),
                        ]
                    });
                let timeout = args["timeout_seconds"]
                    .as_f64()
                    .unwrap_or(self.default_timeout_seconds) as u64;
                cmd.argv = vec![
                    "ssh".into(),
                    "-T".into(),
                    "-o".into(),
                    "BatchMode=yes".into(),
                    "-o".into(),
                    format!("ConnectTimeout={}", timeout.clamp(1, 30)),
                    "--".into(),
                    t.alias.clone(),
                ];
                cmd.argv.extend(remote);
                cmd.target = Some(t.alias.clone());
            }
            ("HOST_FILESYSTEM", "path_read") => {
                let relative = text(values, "path")?;
                let path = self.workspace(cwd, relative, false)?;
                cmd.argv = vec![
                    "head".into(),
                    "-n".into(),
                    values["lines"].as_u64().unwrap_or(100).to_string(),
                    "--".into(),
                    path.to_string_lossy().into(),
                ];
                cmd.target = Some(relative.into());
            }
            ("HOST_FILESYSTEM", "marker_create" | "marker_remove") => {
                let name = text(values, "name")?;
                if !matches(r"^\.clinx-host-executor-[a-z0-9-]{1,80}$", name) {
                    return Err("TARGET_NOT_REGISTERED".into());
                }
                let path = resolve(&cwd.join(name))?;
                if !path.starts_with(resolve(cwd)?) {
                    return Err("TARGET_NOT_REGISTERED".into());
                }
                cmd.argv = if op == "marker_create" {
                    vec!["touch".into(), "--".into()]
                } else {
                    vec!["rm".into(), "-f".into(), "--".into()]
                };
                cmd.argv.push(path.to_string_lossy().into());
                cmd.target = Some(name.into());
            }
            ("GIT", "head") | ("HOST_FILESYSTEM", "git_head") => {
                cmd.argv = vec!["git".into(), "rev-parse".into(), "HEAD".into()]
            }
            ("GIT", "status") | ("HOST_FILESYSTEM", "git_status") => {
                cmd.argv = vec![
                    "git".into(),
                    "status".into(),
                    "--short".into(),
                    "--branch".into(),
                ]
            }
            ("GIT", _) => {
                if op == "push_current_branch"
                    && values.get("remote").is_some_and(|s| s != "origin")
                {
                    return Err("TARGET_NOT_REGISTERED".into());
                }
                let mut branch = text(&task["workspace_contract"], "branch")?.to_owned();
                if op == "push_current_branch" && !target.is_empty() {
                    if !target.starts_with("gitwt_") {
                        return Err("TARGET_NOT_REGISTERED".into());
                    }
                    let t = call(
                        authority,
                        "resolve_git_target",
                        json!({"task_ref":p["task_ref"],"target":target}),
                    )?;
                    branch = text(&t, "branch")?.into();
                    cmd.cwd = PathBuf::from(text(&t, "path")?);
                }
                if !branch_valid(&branch) {
                    return Err("TARGET_NOT_REGISTERED".into());
                }
                if op == "ahead_behind" {
                    cmd.argv = vec![
                        "git".into(),
                        "rev-list".into(),
                        "--left-right".into(),
                        "--count".into(),
                        format!("refs/heads/{branch}...refs/remotes/origin/{branch}"),
                        "--".into(),
                    ];
                    cmd.target = Some("origin".into());
                    return self.available(cmd);
                }
                let origin = text(task, "repository_origin")?;
                if git(&cmd.cwd, &["remote", "get-url", "--all", "origin"])? != origin
                    || (op == "push_current_branch"
                        && git(
                            &cmd.cwd,
                            &["remote", "get-url", "--push", "--all", "origin"],
                        )? != origin)
                {
                    return Err("TARGET_NOT_REGISTERED".into());
                }
                cmd.argv = vec![
                    "git".into(),
                    "-c".into(),
                    "core.hooksPath=/dev/null".into(),
                    "-c".into(),
                    "protocol.ext.allow=never".into(),
                ];
                match op {
                    "fetch_origin" => cmd.argv.extend([
                        "fetch".into(),
                        "--no-tags".into(),
                        "--no-recurse-submodules".into(),
                        "origin".into(),
                        format!("refs/heads/{branch}:refs/remotes/origin/{branch}"),
                    ]),
                    "remote_main_head" => cmd.argv.extend([
                        "ls-remote".into(),
                        "--exit-code".into(),
                        "--refs".into(),
                        "origin".into(),
                        format!("refs/heads/{branch}"),
                    ]),
                    "push_current_branch" => {
                        validate_push(&cmd.cwd, origin, &branch)?;
                        cmd.argv.extend([
                            "-c".into(),
                            "remote.origin.mirror=false".into(),
                            "-c".into(),
                            "push.followTags=false".into(),
                            "push".into(),
                            "--no-verify".into(),
                            "--no-force".into(),
                            "origin".into(),
                            format!("refs/heads/{branch}:refs/heads/{branch}"),
                        ]);
                    }
                    _ => return Err("OPERATION_NOT_SUPPORTED".into()),
                }
                cmd.target = Some(
                    if op == "push_current_branch" {
                        target
                    } else {
                        "origin"
                    }
                    .into(),
                );
            }
            _ => return Err("CAPABILITY_UNAVAILABLE".into()),
        }
        if cap == "HOST_FILESYSTEM" && ["git_head", "git_status"].contains(&op) {
            cmd.target = Some(text(p, "project")?.into());
        }
        self.available(cmd)
    }
    fn available(&self, cmd: Plan) -> Result<Plan> {
        let executable = if cmd.argv[0].contains('/') {
            cmd.cwd.join(&cmd.argv[0])
        } else {
            PathBuf::from(&cmd.argv[0])
        };
        if which(executable.to_str().ok_or("CAPABILITY_UNAVAILABLE")?).is_none() {
            return Err("CAPABILITY_UNAVAILABLE".into());
        }
        Ok(cmd)
    }
}
fn validate_push(cwd: &Path, origin: &str, branch: &str) -> Result<()> {
    if git(cwd, &["symbolic-ref", "--quiet", "--short", "HEAD"])? != branch
        || !branch_valid(branch)
        || ["main", "master"].contains(&branch)
        || git(cwd, &["remote", "get-url", "--all", "origin"])? != origin
        || git(cwd, &["remote", "get-url", "--push", "--all", "origin"])? != origin
    {
        return Err("TARGET_NOT_REGISTERED".into());
    }
    let remote = git(
        cwd,
        &[
            "for-each-ref",
            "--format=%(symref)",
            "refs/remotes/origin/HEAD",
        ],
    )?;
    let config = git(cwd, &["config", "--list"])?;
    if remote == format!("refs/remotes/origin/{branch}")
        || config
            .lines()
            .any(|s| s.strip_prefix("clinx.protectedbranch=") == Some(branch))
    {
        return Err("TARGET_NOT_REGISTERED".into());
    }
    for marker in [
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "REBASE_HEAD",
        "rebase-merge",
        "rebase-apply",
    ] {
        let p = git(cwd, &["rev-parse", "--git-path", marker])?;
        if cwd.join(p).exists() {
            return Err("TARGET_NOT_REGISTERED".into());
        }
    }
    Ok(())
}
pub fn redact(s: &str) -> String {
    let mut out = Regex::new(
        r"(?i)\b(token|secret|password|api[_-]?key|authorization)\b(\s*[:=]\s*)([^\s,;]+)",
    )
    .unwrap()
    .replace_all(s, "${1}${2}[REDACTED]")
    .into_owned();
    for p in [
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}",
        r"\b(?:sk|rk|pk)_[A-Za-z0-9_-]{16,}\b",
        r"\bAKIA[0-9A-Z]{16}\b",
    ] {
        out = Regex::new(p)
            .unwrap()
            .replace_all(&out, "[REDACTED]")
            .into_owned();
    }
    out
}
fn capture(file: &mut fs::File, cap: usize) -> Result<Value> {
    file.seek(SeekFrom::Start(0))
        .map_err(|_| "HOST_OUTPUT_UNAVAILABLE")?;
    let mut hash = Sha256::new();
    let mut retained = vec![];
    let mut size = 0usize;
    let mut buf = [0; 65536];
    loop {
        let n = file.read(&mut buf).map_err(|_| "HOST_OUTPUT_UNAVAILABLE")?;
        if n == 0 {
            break;
        }
        hash.update(&buf[..n]);
        size += n;
        let keep = n.min(cap.saturating_sub(retained.len()));
        retained.extend_from_slice(&buf[..keep]);
    }
    Ok(
        json!({"text":redact(&String::from_utf8_lossy(&retained)),"bytes":size,"sha256":format!("{:x}",hash.finalize()),"truncated":size>retained.len()}),
    )
}
fn launcher(qualification: bool) -> Result<Option<PathBuf>> {
    if qualification {
        return Ok(None);
    }
    let p = PathBuf::from(
        std::env::var_os("CLINX_RUST_WORKER_LAUNCHER")
            .unwrap_or_else(|| "/var/lib/clinx-rust/current/host-worker".into()),
    );
    if !p.is_absolute() || p.canonicalize().ok().as_ref() != Some(&p) {
        return Err("ROOT_CONTROLLED_WORKER_LAUNCHER_REQUIRED".into());
    }
    for ancestor in p.ancestors() {
        let m = ancestor
            .symlink_metadata()
            .map_err(|_| "ROOT_CONTROLLED_WORKER_LAUNCHER_REQUIRED")?;
        if m.uid() != 0
            || m.mode() & 0o022 != 0
            || if ancestor == p {
                !m.is_file() || m.mode() & 0o4000 == 0
            } else {
                !m.is_dir()
            }
        {
            return Err("ROOT_CONTROLLED_WORKER_LAUNCHER_REQUIRED".into());
        }
    }
    Ok(Some(p))
}
impl Config {
    pub fn execute<A: Authority>(
        &self,
        authority: &mut A,
        eid: &str,
        call_id: &str,
        p: &Value,
        task: &Value,
        args: &Value,
    ) -> Result<Value> {
        let cmd = self.plan(authority, p, task, args)?;
        let timeout = args
            .get("timeout_seconds")
            .map(|v| v.as_f64().ok_or("INVALID_ARGUMENTS"))
            .transpose()?
            .unwrap_or(self.default_timeout_seconds);
        if !timeout.is_finite() || timeout <= 0. {
            return Err("INVALID_ARGUMENTS".into());
        }
        let timeout = timeout.min(self.max_timeout_seconds);
        let health = call(authority, "health", json!({}))?;
        let qualification = health["qualification"]
            .as_bool()
            .ok_or("AUTHORITY_IDENTITY_REQUIRED")?;
        let launcher = launcher(qualification)?;
        let host_ref = format!("hostexec_{}", crate::authority_store::random_id()?);
        let start = now();
        let status = call(authority, "get_status", json!({"execution_ref":eid}))?;
        if status["task_ref"] != p["task_ref"] || status["resource_state"] != "HELD" {
            return Err("HOST_EXECUTION_BINDING_CONFLICT".into());
        }
        let evidence = json!({"resource_key":cmd.resource,"host_execution_ref":host_ref,"task_id":p["task_ref"],"execution_ref":eid,"host":p["host"],"surface":"host_executor","operation_class":args["operation_class"],"capability":args["capability"],"operation":args["operation"],"argv_json":serde_json::to_string(&cmd.argv.iter().map(|s|redact(s)).collect::<Vec<_>>()).unwrap(),"mutating":u8::from(cmd.mutating),"cwd_identity":p["project"],"target_identity":cmd.target,"started_at":start,"result_state":"RUNNING","timeout_seconds":timeout,"executor_instance":format!("rust-host-{}",std::process::id()),"tool_call_id":call_id});
        // Random command ID: an old successful receipt is never a new permit.
        if call(
            authority,
            "host_begin",
            json!({"execution_ref":eid,"call_id":call_id,"host_ref":host_ref,"evidence":evidence}),
        )?["send_permitted"]
            != true
        {
            return Err("HOST_PERMIT_ALREADY_CONSUMED".into());
        }
        let mut stdout = tempfile::tempfile().map_err(|_| "HOST_PROCESS_UNKNOWN")?;
        let mut stderr = tempfile::tempfile().map_err(|_| "HOST_PROCESS_UNKNOWN")?;
        let mut process = if let Some(path) = launcher {
            let mut c = Command::new(path);
            c.args(&cmd.argv);
            c
        } else {
            let mut c = Command::new(&cmd.argv[0]);
            c.args(&cmd.argv[1..]);
            c
        };
        environment(&mut process);
        process
            .current_dir(&cmd.cwd)
            .stdin(Stdio::null())
            .stdout(stdout.try_clone().map_err(|_| "HOST_PROCESS_UNKNOWN")?)
            .stderr(stderr.try_clone().map_err(|_| "HOST_PROCESS_UNKNOWN")?)
            .process_group(0);
        let began = Instant::now();
        let mut timed_out = false;
        let mut cancelled = false;
        let mut child = process.spawn().map_err(|_| "HOST_PROCESS_UNKNOWN")?;
        let pgid = rustix::process::Pid::from_child(&child);
        let exit = loop {
            if let Some(s) = child.try_wait().map_err(|_| "HOST_PROCESS_UNKNOWN")? {
                break s.code().unwrap_or_else(|| -s.signal().unwrap_or(1));
            }
            cancelled = call(authority, "get_status", json!({"execution_ref":eid}))
                .is_ok_and(|v| v["cancel_requested"] == true);
            timed_out = began.elapsed().as_secs_f64() >= timeout;
            if cancelled || timed_out {
                let _ = rustix::process::kill_process_group(pgid, rustix::process::Signal::TERM);
                let deadline = Instant::now() + Duration::from_secs(2);
                let status = loop {
                    if let Some(s) = child.try_wait().map_err(|_| "HOST_PROCESS_UNKNOWN")? {
                        break s;
                    }
                    if Instant::now() >= deadline {
                        let _ = rustix::process::kill_process_group(
                            pgid,
                            rustix::process::Signal::KILL,
                        );
                        let _ = child.kill();
                        break child.wait().map_err(|_| "HOST_PROCESS_UNKNOWN")?;
                    }
                    std::thread::sleep(Duration::from_millis(10));
                };
                break status
                    .code()
                    .unwrap_or_else(|| -status.signal().unwrap_or(1));
            }
            std::thread::sleep(Duration::from_millis(50));
        };
        let out = capture(&mut stdout, self.max_output_bytes)?;
        let err = capture(&mut stderr, self.max_output_bytes)?;
        let state = if cancelled {
            "CANCELLED"
        } else if timed_out {
            "TIMEOUT"
        } else if exit == 0 {
            "SUCCEEDED"
        } else {
            "COMMAND_FAILED"
        };
        let mut result = json!({"host_execution_ref":host_ref,"task_ref":p["task_ref"],"execution_ref":eid,"host":p["host"],"surface":"HOST_EXECUTOR","operation_class":args["operation_class"],"capability":args["capability"],"operation":args["operation"],"mutating":cmd.mutating,"target_identity":cmd.target,"started_at":start,"completed_at":now(),"duration_ms":began.elapsed().as_millis() as u64,"exit_code":exit,"result_state":state,"timeout_seconds":timeout,"timed_out":timed_out,"cancel_requested":cancelled});
        for (prefix, captured) in [("stdout", out), ("stderr", err)] {
            result[prefix] = captured["text"].clone();
            for key in ["bytes", "sha256", "truncated"] {
                result[format!("{prefix}_{key}")] = captured[key].clone();
            }
        }
        call(
            authority,
            "host_complete",
            json!({"execution_ref":eid,"call_id":call_id,"host_ref":host_ref,"result":result}),
        )?;
        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    struct NoAuthority;
    impl Authority for NoAuthority {
        fn call(&mut self, _: &str, _: Value, _: &str) -> Result<Value> {
            Err("UNEXPECTED_AUTHORITY_CALL".into())
        }
    }
    fn fixture() -> (tempfile::TempDir, Value, Config, Value, Value) {
        let root = std::env::var_os("CLINX_QUALIFICATION_ROOT")
            .map(PathBuf::from)
            .unwrap_or_else(std::env::temp_dir);
        let dir = tempfile::tempdir_in(root).unwrap();
        let cwd = dir.path();
        for args in [
            vec!["init", "-q", "-b", "feature/fixture"],
            vec![
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "--allow-empty",
                "-qm",
                "fixture",
            ],
            vec![
                "remote",
                "add",
                "origin",
                "https://example.invalid/fixture.git",
            ],
        ] {
            assert!(
                Command::new("git")
                    .args(args)
                    .current_dir(cwd)
                    .status()
                    .unwrap()
                    .success()
            );
        }
        let f: Value = serde_json::from_str(
            &include_str!("../../fixtures/host_execution_contract_v2.json")
                .replace("${ROOT}", cwd.to_str().unwrap()),
        )
        .unwrap();
        let config: Config = serde_json::from_value(f["config"].clone()).unwrap();
        config.validate().unwrap();
        let workspace = json!({"cwd":cwd,"branch":"feature/fixture"});
        let task = json!({"host":"p620","project":"fixture","workspace_contract":workspace,"repository_origin":"https://example.invalid/fixture.git"});
        let prepared = json!({"host":"p620","project":"fixture","task_ref":"task","workspace_contract":workspace,"execution_policy":{"execution_surface":"HOST_EXECUTOR"}});
        (dir, f, config, task, prepared)
    }
    #[test]
    fn catalog_matches_python_contract_at_retirement() {
        let (_dir, f, config, _, _) = fixture();
        assert_eq!(config.catalog(), f["catalog"]);
    }
    #[test]
    fn all_34_command_vectors_match_retired_python() {
        let (_dir, f, config, task, mut p) = fixture();
        let mut authority = NoAuthority;
        for v in f["vectors"].as_array().unwrap() {
            p["execution_policy"]["operation_scopes"] = json!([v["scope"]]);
            let s = &v["scope"];
            let args = json!({"capability":s["capability"],"operation":s["operation"],"operation_class":s["operation_class"],"arguments":v["arguments"]});
            let actual = config
                .plan(&mut authority, &p, &task, &args)
                .unwrap_or_else(|e| panic!("{}: {e}", s));
            assert_eq!(
                json!({"argv":actual.argv,"cwd":actual.cwd,"target":actual.target,"mutating":actual.mutating,"resource":actual.resource}),
                v["expected"],
                "{s}"
            );
        }
    }
    #[test]
    fn workspace_symlink_production_command_and_argv_escapes_are_denied() {
        let (dir, _, mut config, task, mut p) = fixture();
        let cwd = dir.path();
        std::os::unix::fs::symlink("/etc", cwd.join("escape")).unwrap();
        let scope = json!({"capability":"LOCAL_HOST_PROCESS","operation":"development_command","operation_class":"DEVELOPMENT_MUTATION","target":""});
        p["execution_policy"]["operation_scopes"] = json!([scope]);
        for argv in [
            json!(["git", "push"]),
            json!(["sh", "-c", "true"]),
            json!(["env", "true"]),
            json!(["head", "escape/passwd"]),
            json!(["git", "-C", "/etc", "status"]),
            json!(["head", "--file=/etc/passwd"]),
        ] {
            let a = json!({"capability":"LOCAL_HOST_PROCESS","operation":"development_command","operation_class":"DEVELOPMENT_MUTATION","arguments":{"argv":argv}});
            assert!(config.plan(&mut NoAuthority, &p, &task, &a).is_err(), "{a}");
        }
        let executable = cwd.join("production");
        fs::write(&executable, b"fixed controller").unwrap();
        config.local_commands.push(Target {
            alias: "prod".into(),
            operation_classes: vec!["PRODUCTION_MUTATION".into()],
            commands: vec![("prod".into(), vec![executable.to_str().unwrap().into()])],
            ..Default::default()
        });
        assert!(config.workspace(cwd, "./production", true).is_err());
    }
    #[test]
    fn malformed_workflows_timeouts_and_roots_are_rejected() {
        let (_dir, _, config, _, _) = fixture();
        let mut c = config.clone();
        c.max_timeout_seconds = f64::NAN;
        assert!(c.validate().is_err());
        let mut c = config.clone();
        c.trusted_workspace_roots.push("/".into());
        assert!(c.validate().is_err());
        for argv in [
            vec!["/usr/bin/{name}".into()],
            vec!["/usr/bin/true".into(), "--flag={name}".into()],
            vec!["/usr/bin/true".into(), "/fixed/../{name}".into()],
        ] {
            let mut c = config.clone();
            c.workflows[0].argv = argv;
            assert!(c.validate().is_err());
        }
        let mut values = json!({"name":"../../escape"});
        assert!(config.workflows[0].render(&values).is_err());
        values["name"] = json!("valid");
        assert_eq!(
            config.workflows[0].render(&values).unwrap(),
            vec!["/usr/bin/true", "valid"]
        );
    }
}
