//! Legacy public identity/policy codec contracts. These do not grant task authority.
//! Production task admission remains in mcp_api and the canonical Authority.
use crate::{
    node_tls::Result,
    read_projection::{text, truth},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{collections::BTreeSet, net::IpAddr};
use unicode_casefold::UnicodeCaseFold;

const CONTRACT: &str = "execution -> host -> surface -> provider -> transport";
const CAPS: &[&str] = &[
    "LOCAL_HOST_PROCESS",
    "SYSTEMD_USER",
    "OUTBOUND_NETWORK",
    "SSH",
    "HOST_FILESYSTEM",
    "AWS_CLI",
    "CLOUDFLARE_CLI",
    "CLOUD_API",
    "GIT",
    "DOCKER",
    "POSTGRES",
    "TAILSCALE",
];
const CLASSES: &[&str] = &[
    "READ_ONLY_HOST",
    "DEVELOPMENT_MUTATION",
    "PRODUCTION_READ_ONLY",
    "PRODUCTION_MUTATION",
    "BUSINESS_ACTION",
];
fn pystr(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(b) => (if *b { "True" } else { "False" }).into(),
        _ => v.to_string(),
    }
}
fn s(name: &str, v: &Value, required: bool) -> Result<String> {
    let Some(v) = v.as_str() else {
        return if !required && v.is_null() {
            Ok(String::new())
        } else {
            Err(format!("{name} must be a string"))
        };
    };
    let value = v.trim();
    if required && value.is_empty() {
        return Err(format!("{name} is required"));
    }
    Ok(value.into())
}
fn fold(s: &str) -> String {
    s.case_fold().collect()
}
fn unknown(s: &str) -> bool {
    matches!(
        fold(s).as_str(),
        "" | "unknown" | "unset" | "none" | "null" | "?" | "-"
    )
}
fn default(a: &Value, k: &str, v: Value) -> Value {
    a.get(k).cloned().unwrap_or(v)
}
fn boolean(name: &str, v: &Value, nullable: bool) -> Result<bool> {
    if nullable && v.is_null() {
        Ok(false)
    } else {
        v.as_bool().ok_or(format!("{name} must be a boolean"))
    }
}
fn host(a: &Value) -> Result<Value> {
    let raw = s("host", &a["value"], true)?;
    if unknown(&raw) {
        return Err("unknown host identity is not executable".into());
    }
    let lowered = fold(&raw);
    let mut stable = lowered
        .strip_prefix("workstation-")
        .unwrap_or(&lowered)
        .to_owned();
    let (address, scope) = stable
        .split_once('%')
        .map(|(ip, z)| (ip, Some(z)))
        .unwrap_or((&stable, None));
    if let Ok(ip) = address.parse::<IpAddr>()
        && (scope.is_none()
            || matches!(ip, IpAddr::V6(_))
                && scope.is_some_and(|s| !s.is_empty() && !s.contains('%')))
    {
        let compressed = ip.to_string();
        stable = format!(
            "ip:{compressed}{}",
            scope.map(|v| format!("%{v}")).unwrap_or_default()
        );
    }
    let mut result = String::new();
    let mut invalid = false;
    for c in stable.chars() {
        if c.is_ascii_lowercase() || c.is_ascii_digit() || "_.:-".contains(c) {
            result.push(c);
            invalid = false;
        } else if !invalid {
            result.push('-');
            invalid = true;
        }
    }
    stable = result.trim_matches('-').into();
    if stable.is_empty() || unknown(&stable) {
        return Err("unknown host identity is not executable".into());
    }
    let display = s("host display", &a["display"], false)?;
    let mid = s("machine_id", &a["machine_id"], false)?;
    Ok(
        json!({"stable_identifier":stable,"display":if display.is_empty(){raw}else{display},"machine_id":if mid.is_empty(){Value::Null}else{json!(mid)},"alias":stable,"status":"KNOWN"}),
    )
}
fn surface(v: &Value) -> Result<Value> {
    let raw = fold(&s("execution surface", v, true)?);
    match raw.as_str() {
        "codex_app_server" | "codex-app-server" | "app_server" | "app-server" => Ok(
            json!({"stable_identifier":"codex_app_server","display":"Codex app-server","capability_boundary":"bounded_codex_turn","status":"SUPPORTED"}),
        ),
        "host_executor" | "host-executor" => Ok(
            json!({"stable_identifier":"host_executor","display":"Trusted Host Executor","capability_boundary":"bounded_p620_host_operations","status":"SUPPORTED"}),
        ),
        _ => Err(format!("unsupported execution surface: {}", pystr(v))),
    }
}
fn provider(v: &Value) -> Result<Value> {
    if !matches!(
        fold(&s("provider", v, true)?).as_str(),
        "codex_app_server" | "codex-app-server" | "codex" | "app_server"
    ) {
        return Err(format!("unsupported provider: {}", pystr(v)));
    }
    Ok(
        json!({"stable_identifier":"codex_app_server","display":"Codex app-server","bounded_task_backend":true,"status":"SUPPORTED"}),
    )
}
fn transport(v: &Value) -> Result<Value> {
    let local = match fold(&s("transport", v, true)?).as_str() {
        "local" | "stdio" | "local_stdio" => true,
        "ssh" | "ssh_stdio" => false,
        _ => return Err(format!("unsupported transport: {}", pystr(v))),
    };
    Ok(
        json!({"stable_identifier":if local{"local_stdio"}else{"ssh_stdio"},"display":if local{"local stdio"}else{"SSH stdio"},"remote":!local,"status":"SUPPORTED"}),
    )
}
fn authority(v: &Value, scopes: &Value) -> Result<Value> {
    let raw = s("authority", v, true)?;
    if unknown(&raw) {
        return Err("authority must be explicit and bounded".into());
    }
    if fold(&raw) != "clinx_task_bounded" {
        return Err(format!("unsupported authority: {}", pystr(v)));
    }
    let clean = scopes
        .as_array()
        .ok_or("authority scopes must be an array")?
        .iter()
        .map(|v| s("authority scope", v, true).map(|s| fold(&s)))
        .collect::<Result<BTreeSet<_>>>()?;
    if clean.is_empty() {
        return Err("authority requires at least one bounded scope".into());
    }
    if clean.iter().any(|s| {
        matches!(
            s.as_str(),
            "shell" | "generic_shell" | "arbitrary_command" | "remote_execute" | "host_control"
        )
    }) {
        return Err("authority scope is outside the bounded CLINX surface".into());
    }
    Ok(
        json!({"stable_identifier":fold(&raw),"scopes":clean,"status":"BOUNDED","identity_is_authority":false}),
    )
}
fn network(v: &Value) -> Result<Value> {
    let on = boolean("network_access", v, false)?;
    Ok(
        json!({"network_access":on,"stable_identifier":"provider_turn_network_opt_in","remote_execution":false,"mode":if on{"ENABLED"}else{"DISABLED"}}),
    )
}
fn separation() -> Value {
    json!({"host_is_authority":false,"surface_is_authority":false,"provider_is_authority":false,"transport_is_authority":false,"network_is_authority":false,"network_is_remote_execution":false})
}
fn compatible(surface: &Value, provider: &Value, transport: &Value) -> Result<()> {
    if provider["stable_identifier"] != "codex_app_server"
        || !matches!(
            text(&surface["stable_identifier"]),
            "codex_app_server" | "host_executor"
        )
    {
        return Err("execution surface/provider compatibility is unsupported".into());
    }
    if surface["stable_identifier"] == "host_executor" && truth(&transport["remote"]) {
        return Err("HOST_EXECUTOR requires local provider transport on its host".into());
    }
    Ok(())
}
fn workspace(a: &Value) -> Result<Value> {
    Ok(
        json!({"workspace_alias":s("workspace_alias",&a["workspace_alias"],true)?,"project_alias":s("project_alias",&a["project_alias"],true)?,"worktree_key":s("worktree_key",&a["worktree_key"],true)?}),
    )
}
fn build_route(a: &Value) -> Result<Value> {
    let host = host(&json!({"value":a["host"]}))?;
    let surface = surface(&default(a, "surface", json!("codex_app_server")))?;
    if let Some(supported) = a["supported_hosts"].as_array()
        && !supported
            .iter()
            .any(|h| host_id(h).is_ok_and(|s| s == host["stable_identifier"]))
    {
        return Err(format!(
            "unsupported host/surface pair: {}/{}",
            text(&host["stable_identifier"]),
            text(&surface["stable_identifier"])
        ));
    }
    let provider = provider(&default(a, "provider", json!("codex_app_server")))?;
    let transport = transport(&default(a, "transport", json!("local_stdio")))?;
    compatible(&surface, &provider, &transport)?;
    let binding = default(a, "conversation_binding", json!("UNBOUND"));
    let binding_text = s("conversation binding", &binding, false)?;
    Ok(
        json!({"host":host,"surface":surface,"provider":provider,"transport":transport,"workspace":workspace(a)?,"conversation":{"binding":if binding_text.is_empty(){"UNBOUND"}else{&binding_text},"status":if truth(&binding)&&binding!="UNBOUND"{"BOUND"}else{"UNBOUND"}},"authority":authority(&default(a,"authority",json!("clinx_task_bounded")),&default(a,"authority_scopes",json!(["workspace_write"])))?,"network_policy":network(&default(a,"network_access",json!(false)))?,"project_identity":s("project_identity",&a["project_identity"],true)?,"routing_contract":CONTRACT,"separation":separation()}),
    )
}
fn host_id(v: &Value) -> Result<String> {
    Ok(text(&host(&json!({"value":v}))?["stable_identifier"]).into())
}
fn legacy_route(a: &Value) -> Result<Value> {
    let raw = s("host", &a["host"], false)?;
    let host = if unknown(&raw) {
        json!({"stable_identifier":"unknown","display":"unknown","machine_id":null,"alias":"unknown","status":"UNKNOWN"})
    } else {
        host(&json!({"value":raw}))?
    };
    let mut worktree = s("worktree_key", &a["worktree_key"], false)?;
    if worktree.is_empty() {
        let bytes = format!(
            "{raw}\u{1f}{}\u{1f}{}\u{1f}{}",
            pystr(&a["workspace_alias"]),
            pystr(&a["project_alias"]),
            pystr(&a["cwd"])
        );
        worktree = format!(
            "legacy:{}",
            &format!("{:x}", Sha256::digest(bytes.as_bytes()))[..32]
        );
    }
    let mut workspace_args = a.clone();
    workspace_args["worktree_key"] = json!(worktree);
    let bound = truth(&a["conversation_bound"]);
    Ok(
        json!({"host":host,"surface":{"stable_identifier":"unknown","display":"unknown","capability_boundary":"unknown","status":"UNKNOWN"},"provider":{"stable_identifier":"unknown","display":"unknown","bounded_task_backend":false,"status":"UNKNOWN"},"transport":{"stable_identifier":"unknown","display":"unknown","remote":false,"status":"UNKNOWN"},"workspace":workspace(&workspace_args)?,"conversation":{"binding":if bound{"BOUND"}else{"UNBOUND"},"status":if bound{"BOUND"}else{"UNKNOWN"}},"authority":{"stable_identifier":"unknown","scopes":[],"status":"UNKNOWN","identity_is_authority":false},"network_policy":network(&json!(false))?,"project_identity":s("project_identity",&a["project_identity"],true)?,"routing_contract":CONTRACT,"separation":separation()}),
    )
}
fn exact_keys(v: &Value, keys: &[&str]) -> bool {
    v.as_object().is_some_and(|o| {
        o.keys().map(String::as_str).collect::<BTreeSet<_>>() == keys.iter().copied().collect()
    })
}
fn parse_route(raw: &Value) -> Result<Value> {
    if !truth(raw) || raw == "{}" {
        return Ok(Value::Null);
    }
    let raw: Value = serde_json::from_str(raw.as_str().ok_or("malformed routing identity")?)
        .map_err(|_| "malformed routing identity")?;
    parse_route_inner(&raw).map_err(|_| "malformed routing identity".into())
}
fn parse_route_inner(r: &Value) -> Result<Value> {
    if !exact_keys(
        r,
        &[
            "host",
            "surface",
            "provider",
            "transport",
            "workspace",
            "conversation",
            "authority",
            "network_policy",
            "project_identity",
            "routing_contract",
            "separation",
        ],
    ) || r["routing_contract"] != CONTRACT
        || r["separation"] != separation()
    {
        return Err("malformed routing identity".into());
    }
    for (key, fields) in [
        (
            "host",
            vec![
                "stable_identifier",
                "display",
                "machine_id",
                "alias",
                "status",
            ],
        ),
        (
            "surface",
            vec![
                "stable_identifier",
                "display",
                "capability_boundary",
                "status",
            ],
        ),
        (
            "provider",
            vec![
                "stable_identifier",
                "display",
                "bounded_task_backend",
                "status",
            ],
        ),
        (
            "transport",
            vec!["stable_identifier", "display", "remote", "status"],
        ),
        (
            "workspace",
            vec!["workspace_alias", "project_alias", "worktree_key"],
        ),
        ("conversation", vec!["binding", "status"]),
        (
            "authority",
            vec![
                "stable_identifier",
                "scopes",
                "status",
                "identity_is_authority",
            ],
        ),
        (
            "network_policy",
            vec![
                "stable_identifier",
                "network_access",
                "mode",
                "remote_execution",
            ],
        ),
    ] {
        if !exact_keys(&r[key], &fields) {
            return Err("malformed routing identity".into());
        }
    }
    if r["authority"]["identity_is_authority"] != false {
        return Err("malformed routing identity".into());
    }
    let mut out = r.clone();
    let h = &r["host"];
    let hs = pystr(&h["status"]).to_uppercase();
    if hs == "UNKNOWN" && unknown(&pystr(&h["stable_identifier"])) {
        out["host"] = json!({"stable_identifier":"unknown","display":"unknown","machine_id":null,"alias":"unknown","status":"UNKNOWN"});
    } else {
        if hs != "KNOWN" {
            return Err("malformed routing identity".into());
        }
        let normal = host(
            &json!({"value":h["stable_identifier"],"display":h["display"],"machine_id":h["machine_id"]}),
        )?;
        if h["alias"] != normal["alias"] {
            return Err("malformed routing identity".into());
        }
        out["host"] = normal;
    }
    for key in ["surface", "provider", "transport"] {
        let value = &r[key];
        let status = pystr(&value["status"]).to_uppercase();
        out[key] = match status.as_str() {
            "UNKNOWN" => match key {
                "surface" => {
                    json!({"stable_identifier":"unknown","display":"unknown","capability_boundary":"unknown","status":"UNKNOWN"})
                }
                "provider" => {
                    json!({"stable_identifier":"unknown","display":"unknown","bounded_task_backend":false,"status":"UNKNOWN"})
                }
                _ => {
                    json!({"stable_identifier":"unknown","display":"unknown","remote":false,"status":"UNKNOWN"})
                }
            },
            "SUPPORTED" => match key {
                "surface" => surface(&value["stable_identifier"])?,
                "provider" => provider(&value["stable_identifier"])?,
                _ => transport(&value["stable_identifier"])?,
            },
            "UNSUPPORTED" => match key {
                "surface" => {
                    let mut v = value.clone();
                    v["status"] = json!(status);
                    v
                }
                "provider" => {
                    json!({"stable_identifier":pystr(&value["stable_identifier"]),"display":pystr(&value["display"]),"bounded_task_backend":false,"status":"UNSUPPORTED"})
                }
                _ => {
                    json!({"stable_identifier":pystr(&value["stable_identifier"]),"display":pystr(&value["display"]),"remote":truth(&value["remote"]),"status":"UNSUPPORTED"})
                }
            },
            _ => return Err("malformed routing identity".into()),
        };
    }
    if out["surface"]["status"] != "UNKNOWN" {
        compatible(&out["surface"], &out["provider"], &out["transport"])?;
    }
    let scopes = &r["authority"]["scopes"];
    if !scopes.is_array() {
        return Err("malformed routing identity".into());
    }
    out["authority"] = match pystr(&r["authority"]["status"]).to_uppercase().as_str() {
        "UNKNOWN" => {
            json!({"stable_identifier":"unknown","scopes":[],"status":"UNKNOWN","identity_is_authority":false})
        }
        "BOUNDED" | "UNSUPPORTED" => authority(&r["authority"]["stable_identifier"], scopes)?,
        _ => return Err("malformed routing identity".into()),
    };
    let binding = s("conversation binding", &r["conversation"]["binding"], false)?;
    let binding = if binding.is_empty() {
        "UNBOUND"
    } else {
        &binding
    };
    let status = pystr(&r["conversation"]["status"]).to_uppercase();
    if !match status.as_str() {
        "BOUND" => binding != "UNBOUND",
        "UNBOUND" => binding == "UNBOUND",
        "UNKNOWN" => matches!(binding, "UNBOUND" | "UNKNOWN"),
        _ => false,
    } {
        return Err("malformed routing identity".into());
    }
    let net = &r["network_policy"];
    if boolean("remote_execution", &net["remote_execution"], true)?
        || net["stable_identifier"] != "provider_turn_network_opt_in"
    {
        return Err("malformed routing identity".into());
    }
    out["workspace"] = workspace(&r["workspace"])?;
    out["conversation"] = json!({"binding":binding,"status":status});
    out["network_policy"] = network(&json!(boolean(
        "network_access",
        &net["network_access"],
        true
    )?))?;
    out["project_identity"] = json!(s("project_identity", &r["project_identity"], true)?);
    Ok(out)
}

fn normalized(name: &str, v: &Value, allowed: &[&str]) -> Result<Vec<String>> {
    if v.is_null() {
        return Ok(vec![]);
    }
    let values = v.as_array().ok_or(format!("{name} must be an array"))?;
    let mut selected = BTreeSet::new();
    for v in values {
        let item = v
            .as_str()
            .filter(|s| !s.trim().is_empty())
            .ok_or(format!("{name} entries must be non-empty strings"))?
            .trim()
            .to_uppercase();
        if !allowed.contains(&item.as_str()) {
            return Err(format!("unsupported {name} entry: {}", pystr(v)));
        }
        selected.insert(item);
    }
    Ok(allowed
        .iter()
        .filter(|v| selected.contains(**v))
        .map(|s| s.to_string())
        .collect())
}
fn operation_scopes(v: &Value) -> Result<Value> {
    if v.is_null() {
        return Ok(Value::Null);
    }
    let rows = v
        .as_array()
        .filter(|v| v.len() <= 128)
        .ok_or("operation_scopes must be a bounded array")?;
    let mut out = BTreeSet::new();
    for row in rows {
        if !exact_keys(
            row,
            &["capability", "operation", "operation_class", "target"],
        ) {
            return Err(
                "operation scope requires capability, operation, operation_class, target".into(),
            );
        }
        if row.as_object().unwrap().values().any(|v| {
            !v.as_str()
                .is_some_and(|s| s.chars().count() <= 160 && !s.chars().any(|c| c < ' '))
        }) {
            return Err("invalid operation scope identity".into());
        }
        let cap = normalized("required_capabilities", &json!([row["capability"]]), CAPS)?;
        let class = normalized(
            "operation_classes",
            &json!([row["operation_class"]]),
            CLASSES,
        )?;
        let op = text(&row["operation"]);
        let target = text(&row["target"]);
        if op.trim().is_empty() || op.contains('*') || target.contains('*') {
            return Err("operation scopes require exact identities".into());
        }
        out.insert([cap[0].clone(), op.into(), class[0].clone(), target.into()]);
    }
    Ok(json!(
        out.into_iter()
            .map(
                |r| json!({"capability":r[0],"operation":r[1],"operation_class":r[2],"target":r[3]})
            )
            .collect::<Vec<_>>()
    ))
}
fn policy_surface(v: &Value, network: bool) -> Result<String> {
    if v.is_null() {
        return Ok(if network {
            "NETWORKED_SANDBOX"
        } else {
            "SANDBOX_WORKSPACE"
        }
        .into());
    }
    let raw = v
        .as_str()
        .filter(|s| !s.trim().is_empty())
        .ok_or("execution_surface must be a non-empty string")?
        .trim();
    let surface = match fold(raw).as_str() {
        "sandbox_workspace" | "sandbox-workspace" | "codex_app_server" => {
            "SANDBOX_WORKSPACE".into()
        }
        "networked_sandbox" | "networked-sandbox" => "NETWORKED_SANDBOX".into(),
        "host_executor" | "host-executor" => "HOST_EXECUTOR".into(),
        _ => raw.to_uppercase(),
    };
    if !matches!(
        surface.as_str(),
        "SANDBOX_WORKSPACE" | "NETWORKED_SANDBOX" | "NATIVE_FULL_ACCESS" | "HOST_EXECUTOR"
    ) {
        return Err(format!("unsupported execution surface: {}", pystr(v)));
    }
    Ok(surface)
}
fn build_policy(a: &Value) -> Result<Value> {
    let net = boolean(
        "network_access",
        &default(a, "network_access", json!(false)),
        false,
    )?;
    let production = boolean(
        "production_mutation_intent",
        &default(a, "production_mutation_intent", json!(false)),
        false,
    )?;
    let caps = normalized("required_capabilities", &a["required_capabilities"], CAPS)?;
    let classes = normalized("operation_classes", &a["operation_classes"], CLASSES)?;
    let selected = if a["execution_surface"].is_null()
        && (!caps.is_empty() || !classes.is_empty() || production)
    {
        json!("HOST_EXECUTOR")
    } else {
        a["execution_surface"].clone()
    };
    let surface = policy_surface(&selected, net)?;
    if surface == "SANDBOX_WORKSPACE" && net {
        return Err("SANDBOX_WORKSPACE cannot enable network_access".into());
    }
    if matches!(surface.as_str(), "NETWORKED_SANDBOX" | "NATIVE_FULL_ACCESS") && !net {
        return Err(format!("{surface} requires network_access=true"));
    }
    if surface != "HOST_EXECUTOR" {
        if !caps.is_empty() || !classes.is_empty() || production {
            return Err("host capabilities and operation classes require HOST_EXECUTOR".into());
        }
    } else {
        if caps.is_empty() {
            return Err("HOST_EXECUTOR requires explicit capabilities".into());
        }
        if classes.is_empty() {
            return Err("HOST_EXECUTOR requires explicit operation classes".into());
        }
        if classes.iter().any(|s| s == "BUSINESS_ACTION") {
            return Err(
                "AI_DOES_NOT_DECIDE_LIVE_BUSINESS_ACTION: BUSINESS_ACTION is not supported".into(),
            );
        }
        if classes.iter().any(|s| s == "PRODUCTION_MUTATION") && !production {
            return Err(
                "PRODUCTION_MUTATION requires explicit production_mutation_intent=true".into(),
            );
        }
    }
    let scopes = operation_scopes(&a["operation_scopes"])?;
    if scopes.as_array().is_some_and(|rows| {
        rows.iter().any(|row| {
            !caps.iter().any(|c| row["capability"] == *c)
                || !classes.iter().any(|c| row["operation_class"] == *c)
        })
    }) {
        return Err("operation scope exceeds capability/class authority".into());
    }
    let mut result = json!({"contract":"CLINX_EXECUTION_POLICY_V1","execution_surface":surface,"required_capabilities":caps,"operation_classes":classes,"production_mutation_intent":production,"host_executor_default":false,"business_action_authority":false});
    if !scopes.is_null() {
        result["operation_scopes"] = scopes;
    }
    Ok(result)
}
fn parse_policy(v: &Value) -> Result<Value> {
    if !truth(v) || v == "{}" {
        return Ok(Value::Null);
    }
    let mut raw: Value = serde_json::from_str(v.as_str().ok_or("malformed execution policy")?)
        .map_err(|_| "malformed execution policy")?;
    if !raw.is_object() {
        return Err("malformed execution policy".into());
    }
    let mut fields = raw.clone();
    fields.as_object_mut().unwrap().remove("operation_scopes");
    if !exact_keys(
        &fields,
        &[
            "contract",
            "execution_surface",
            "required_capabilities",
            "operation_classes",
            "production_mutation_intent",
            "host_executor_default",
            "business_action_authority",
        ],
    ) {
        return Err("non-canonical execution policy".into());
    }
    if raw["contract"] != "CLINX_EXECUTION_POLICY_V1" {
        return Err("unsupported execution policy contract".into());
    }
    if raw["host_executor_default"] != false {
        return Err("HOST_EXECUTOR cannot be the default".into());
    }
    if raw["business_action_authority"] != false {
        return Err("business action authority cannot be implicit".into());
    }
    if raw["execution_surface"].is_array() {
        return Err("EXECUTION_CONTRACT_UNHASHABLE_LIST".into());
    }
    if raw["execution_surface"].is_object() {
        return Err("EXECUTION_CONTRACT_UNHASHABLE_DICT".into());
    }
    raw["network_access"] = json!(matches!(
        text(&raw["execution_surface"]),
        "NETWORKED_SANDBOX" | "NATIVE_FULL_ACCESS"
    ));
    build_policy(&raw)
}
fn permits(p: &Value, class: &Value, cap: &Value) -> bool {
    p["execution_surface"] == "HOST_EXECUTOR"
        && class != "BUSINESS_ACTION"
        && p["operation_classes"]
            .as_array()
            .is_some_and(|v| v.contains(class))
        && p["required_capabilities"]
            .as_array()
            .is_some_and(|v| v.contains(cap))
}
fn authority_scopes(p: &Value) -> Result<Value> {
    let mut scopes = BTreeSet::from(["workspace_write".to_string()]);
    for v in p["operation_classes"]
        .as_array()
        .ok_or("invalid operation classes")?
    {
        let class = text(v);
        if !CLASSES.contains(&class) {
            return Err(class.into());
        }
        scopes.insert(class.to_lowercase());
    }
    Ok(json!(scopes))
}
fn route_surface(p: &Value) -> &'static str {
    if p["execution_surface"] == "HOST_EXECUTOR" {
        "host_executor"
    } else {
        "codex_app_server"
    }
}
fn ascii_json(v: &Value) -> String {
    let mut out = String::new();
    for c in v.to_string().chars() {
        if c.is_ascii() && c != '\u{7f}' {
            out.push(c)
        } else {
            for unit in c.encode_utf16(&mut [0; 2]) {
                out.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    out
}

pub fn command(op: &str, a: &Value) -> Result<Value> {
    match op.strip_prefix("execution_contract.").unwrap_or(op) {
        "host" => host(a),
        "surface" => surface(&a["value"]),
        "provider" => provider(&a["value"]),
        "transport" => transport(&a["value"]),
        "authority" => authority(&a["value"], &a["scopes"]),
        "network" => network(&a["value"]),
        "build_route" => build_route(a),
        "legacy_route" => legacy_route(a),
        "parse_route" => parse_route(&a["value"]),
        "bind" => {
            let mut r = a["route"].clone();
            let binding = s("conversation binding", &a["binding"], true)?;
            match text(&r["conversation"]["status"]) {
                "BOUND" => {
                    if r["conversation"]["binding"] != binding {
                        return Err("routing identity conversation changed".into());
                    }
                }
                "UNBOUND" => r["conversation"] = json!({"binding":binding,"status":"BOUND"}),
                _ => return Err("routing identity conversation is not bindable".into()),
            }
            Ok(r)
        }
        "executable" => {
            let r = &a["route"];
            Ok(json!(
                r["host"]["status"] == "KNOWN"
                    && r["surface"]["status"] == "SUPPORTED"
                    && r["provider"]["status"] == "SUPPORTED"
                    && r["transport"]["status"] == "SUPPORTED"
                    && r["authority"]["status"] == "BOUNDED"
                    && !truth(&r["network_policy"]["remote_execution"])
            ))
        }
        "build_policy" => build_policy(a),
        "parse_policy" => parse_policy(&a["value"]),
        "operation_scopes" => operation_scopes(&a["value"]),
        "policy_surface" => {
            policy_surface(&a["value"], truth(&a["network_access"])).map(Value::from)
        }
        "normalized" => {
            let allowed = a["allowed"]
                .as_array()
                .ok_or("invalid allowed values")?
                .iter()
                .map(text)
                .collect::<Vec<_>>();
            normalized(text(&a["name"]), &a["values"], &allowed).map(|v| json!(v))
        }
        "permits" => Ok(json!(permits(
            &a["policy"],
            &a["operation_class"],
            &a["capability"]
        ))),
        "permits_operation" => {
            let p = &a["policy"];
            Ok(json!(
                permits(p, &a["operation_class"], &a["capability"])
                    && (p["operation_scopes"].is_null()
                        || p["operation_scopes"]
                            .as_array()
                            .is_some_and(|v| v.iter().any(|s| s["capability"] == a["capability"]
                                && s["operation"] == a["requested_operation"]
                                && s["operation_class"] == a["operation_class"]
                                && s["target"] == a["target"])))
            ))
        }
        "authority_scopes" => authority_scopes(&a["policy"]),
        "route_surface" => Ok(json!(route_surface(&a["policy"]))),
        "validate_new" => {
            let p = &a["policy"];
            if p["operation_classes"].as_array().is_some_and(|v| {
                v.iter()
                    .any(|v| v == "PRODUCTION_READ_ONLY" || v == "PRODUCTION_MUTATION")
            }) && !truth(&p["operation_scopes"])
            {
                return Err("PRODUCTION_SCOPE_REQUIRED: new production tasks require exact operation_scopes (requested_operations in preparation)".into());
            }
            Ok(Value::Null)
        }
        "validate_route" => {
            let p = &a["policy"];
            let r = &a["route"];
            if r["surface"]["stable_identifier"] != route_surface(p) {
                return Err("execution policy surface does not match routing identity".into());
            }
            let net = matches!(
                text(&p["execution_surface"]),
                "NETWORKED_SANDBOX" | "NATIVE_FULL_ACCESS"
            );
            if p["execution_surface"] != "HOST_EXECUTOR"
                && r["network_policy"]["network_access"] != net
            {
                return Err("execution policy network mode does not match routing identity".into());
            }
            if authority_scopes(p)?.as_array().unwrap().iter().any(|s| {
                !r["authority"]["scopes"]
                    .as_array()
                    .is_some_and(|v| v.contains(s))
            }) {
                return Err("execution policy authority is not sealed in routing identity".into());
            }
            Ok(Value::Null)
        }
        "policy_identity" => {
            let p: Value = serde_json::from_str(text(&a["policy_json"]))
                .map_err(|_| "malformed execution policy")?;
            let r: Value = serde_json::from_str(text(&a["route_json"]))
                .map_err(|_| "malformed routing identity")?;
            Ok(json!(format!(
                "{:x}",
                Sha256::digest(ascii_json(&json!([p, r, a["version"]])).as_bytes())
            )))
        }
        "legacy_policy" => {
            let r = &a["route"];
            match text(&r["surface"]["stable_identifier"]) {
                "host_executor" => Err("legacy route cannot prove a host executor policy".into()),
                "codex_app_server" | "unknown" => {
                    build_policy(&json!({"network_access":r["network_policy"]["network_access"]}))
                }
                _ => Err("legacy route has an unsupported execution surface".into()),
            }
        }
        _ => Err("EXECUTION_CONTRACT_OPERATION_UNKNOWN".into()),
    }
}
