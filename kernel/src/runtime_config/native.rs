//! Native configuration IO; validation remains in the existing config contract.
use crate::{mcp_api, node_tls::Result};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    path::{Component, Path, PathBuf},
};
use unicode_casefold::UnicodeCaseFold;

pub fn hostname() -> String {
    rustix::system::uname()
        .nodename()
        .to_string_lossy()
        .into_owned()
}

pub fn expanduser(path: &Path) -> Result<PathBuf> {
    let Some(s) = path.to_str() else {
        return Err("CONFIG_PATH_INVALID".into());
    };
    if !s.starts_with('~') {
        return Ok(path.to_path_buf());
    }
    let (user, suffix) = s.split_once('/').unwrap_or((s, ""));
    let home = if user == "~" {
        std::env::var_os("HOME").map(PathBuf::from).or_else(|| {
            nix::unistd::User::from_uid(nix::unistd::getuid())
                .ok()
                .flatten()
                .map(|u| u.dir)
        })
    } else {
        nix::unistd::User::from_name(&user[1..])
            .ok()
            .flatten()
            .map(|u| u.dir)
    }
    .ok_or("Could not determine home directory.")?;
    Ok(if suffix.is_empty() {
        home
    } else {
        home.join(suffix.trim_start_matches('/'))
    })
}

// Resolve existing links before '..', retaining nonexistent suffixes exactly as
// Path.resolve(strict=False). No subprocess, shell expansion or path policy.
fn resolve(path: &Path) -> Result<PathBuf> {
    let path = if path.is_absolute() {
        path.to_owned()
    } else {
        std::env::current_dir()
            .map_err(|_| "CONFIG_CWD_UNAVAILABLE")?
            .join(path)
    };
    enum Step {
        Part(std::ffi::OsString),
        Leave(PathBuf),
    }
    let mut pending: VecDeque<_> = path
        .components()
        .map(|c| Step::Part(c.as_os_str().to_owned()))
        .collect();
    let mut out = PathBuf::new();
    let mut expanding = BTreeSet::new();
    while let Some(step) = pending.pop_front() {
        let part = match step {
            Step::Part(part) => part,
            Step::Leave(path) => {
                expanding.remove(&path);
                continue;
            }
        };
        match Path::new(&part).components().next() {
            Some(Component::RootDir) => out = PathBuf::from("/"),
            Some(Component::ParentDir) => {
                out.pop();
            }
            Some(Component::CurDir) => (),
            _ => {
                out.push(part);
                if !expanding.contains(&out)
                    && let Ok(target) = std::fs::read_link(&out)
                {
                    expanding.insert(out.clone());
                    pending.push_front(Step::Leave(out.clone()));
                    out.pop();
                    for c in target.components().rev() {
                        pending.push_front(Step::Part(c.as_os_str().to_owned()));
                    }
                }
            }
        }
    }
    Ok(out)
}

fn wire(value: &toml::Value, path: &[String], orders: &mut Vec<Value>) -> Result<Value> {
    Ok(match value {
        toml::Value::Table(rows) => {
            orders.push(json!([path, rows.keys().collect::<Vec<_>>()]));
            let mut out = serde_json::Map::new();
            for (key, value) in rows {
                let mut p = path.to_vec();
                p.push(key.clone());
                out.insert(key.clone(), wire(value, &p, orders)?);
            }
            Value::Object(out)
        }
        toml::Value::Array(rows) => Value::Array(
            rows.iter()
                .map(|v| wire(v, path, &mut vec![]))
                .collect::<Result<_>>()?,
        ),
        toml::Value::String(s) => json!(s),
        toml::Value::Integer(n) => json!(n),
        toml::Value::Float(n) => super::float_wire(*n),
        toml::Value::Boolean(b) => json!(b),
        toml::Value::Datetime(_) => return Err("CONFIG_DATETIME_UNSUPPORTED".into()),
    })
}

pub fn native_load(path: &Path) -> Result<Value> {
    let input = std::fs::read_to_string(path).map_err(|_| "CONFIG_READ_FAILED")?;
    let raw: toml::Value = toml::from_str(&input).map_err(|_| "CONFIG_TOML_INVALID")?;
    let mut orders = vec![];
    let raw = wire(&raw, &[], &mut orders)?;
    super::invoke(
        "load",
        &json!({"raw":raw,"orders":orders,"hostname":hostname()}),
        |op, a| {
            if op != "path" {
                return Err("CONFIG_CALLBACK_INVALID".into());
            }
            let mut path = PathBuf::from(a["value"].as_str().ok_or("CONFIG_PATH_INVALID")?);
            if a["expand"] == true {
                path = expanduser(&path)?;
            }
            if a["resolve"] == true {
                path = resolve(&path)?;
            }
            Ok(json!({"path":path,"absolute":path.is_absolute()}))
        },
    )
}

pub fn provider_endpoints(config: &Value) -> Result<Vec<PathBuf>> {
    let app = &config["app_server"];
    let runtime = std::env::var_os("XDG_RUNTIME_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(format!("/run/user/{}", nix::unistd::getuid())));
    let mut out = vec![];
    for key in ["local_socket", "native_socket"] {
        if let Some(s) = app[key].as_str().filter(|s| !s.is_empty()) {
            let mut path = expanduser(Path::new(s))?;
            if !path.is_absolute() {
                path = runtime.join(path);
            }
            if !out.contains(&path) {
                out.push(path);
            }
        }
    }
    Ok(out)
}

pub fn api_config(config: &Value) -> Result<mcp_api::Config> {
    mcp_routes(config, true)
}

// Installed MCP routes historically contain configured workspaces only. The
// Helper API also recognizes its own runtime host; keep that distinction while
// sharing normalization, endpoint selection and desktop routing.
fn mcp_routes(config: &Value, include_runtime_host: bool) -> Result<mcp_api::Config> {
    let host = config["runtime_host"]
        .as_str()
        .ok_or("API_CONFIG_INVALID")?;
    let mut aliases: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    if include_runtime_host {
        aliases.insert(host.case_fold().collect(), BTreeSet::from([host.into()]));
    }
    let mut desktops: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for w in config["workspaces"]
        .as_array()
        .ok_or("API_CONFIG_INVALID")?
    {
        let raw = w["host"]
            .as_str()
            .filter(|s| !s.is_empty())
            .or(w["alias"].as_str())
            .ok_or("API_CONFIG_INVALID")?;
        let canonical =
            crate::execution_contracts::command("execution_contract.host", &json!({"value":raw}))?
                ["stable_identifier"]
                .as_str()
                .ok_or("API_CONFIG_INVALID")?
                .to_owned();
        let workstation = format!("workstation-{canonical}");
        for name in [
            w["alias"].as_str(),
            w["host"].as_str(),
            Some(&canonical),
            Some(&workstation),
        ]
        .into_iter()
        .flatten()
        .filter(|s| !s.is_empty())
        {
            aliases
                .entry(name.case_fold().collect())
                .or_default()
                .insert(canonical.clone());
        }
        for name in w["codex_host_ids"].as_array().ok_or("API_CONFIG_INVALID")? {
            desktops
                .entry(name.as_str().ok_or("API_CONFIG_INVALID")?.into())
                .or_default()
                .insert(canonical.clone());
        }
    }
    Ok(mcp_api::Config {
        runtime_host: host.into(),
        provider_endpoint: provider_endpoints(config)?
            .into_iter()
            .next()
            .ok_or("PROVIDER_ENDPOINT_REQUIRED")?,
        provider_timeout_seconds: config["app_server"]["request_timeout_seconds"]
            .as_f64()
            .ok_or("API_CONFIG_INVALID")?,
        host_aliases: aliases
            .into_iter()
            .map(|(k, v)| (k, v.into_iter().collect()))
            .collect(),
        codex_host_ids: desktops
            .into_iter()
            .map(|(k, v)| (k, v.into_iter().collect()))
            .collect(),
    })
}

/// Offline build/install projection. Reads the supplied TOML only; no state,
/// Provider, task, service or output-file mutation is performed here.
pub fn compile_config(kind: &str, path: &Path) -> Result<Value> {
    if !matches!(kind, "host" | "mcp") {
        return Err("CONFIG_COMPILE_KIND_INVALID".into());
    }
    let config = native_load(path)?;
    // Legacy Python json.dumps emitted NaN/Infinity for these values. Neither
    // Rust consumer can parse that JSON; never emit the private numeric wire
    // object as if it were a valid worker timeout.
    let timeouts = if kind == "mcp" {
        vec![&config["app_server"]["request_timeout_seconds"]]
    } else {
        vec![
            &config["host_executor"]["default_timeout_seconds"],
            &config["host_executor"]["max_timeout_seconds"],
        ]
    };
    if timeouts.iter().any(|v| v.get("__config_float").is_some()) {
        return Err("NONFINITE_RUNTIME_CONFIG".into());
    }
    if kind == "mcp" {
        return serde_json::to_value(mcp_routes(&config, false)?)
            .map_err(|_| "API_CONFIG_INVALID".into());
    }
    let mut host = config["host_executor"].clone();
    host["host"] = json!(super::canonical(
        host["host"].as_str().ok_or("host identity is required")?
    )?);
    Ok(host)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn existing_link_parent_precedes_lexical_parent_and_missing_suffix() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(dir.path().join("real/deep")).unwrap();
        std::os::unix::fs::symlink("real/deep", dir.path().join("alias")).unwrap();
        assert_eq!(
            resolve(&dir.path().join("alias/../missing/./leaf")).unwrap(),
            dir.path().join("real/missing/leaf")
        );
    }
    #[test]
    fn native_parser_keeps_configured_insertion_order_and_special_floats() {
        let raw: toml::Value = toml::from_str("[z]\na=nan\n[b]\nx=2\n").unwrap();
        let mut order = vec![];
        let out = wire(&raw, &[], &mut order).unwrap();
        assert_eq!(order[0], json!([[], ["z", "b"]]));
        assert_eq!(out["z"]["a"], json!({"__config_float":"nan"}));
    }
    #[test]
    fn finite_long_link_chains_resolve_and_cycles_remain_nonstrict() {
        let dir = tempfile::tempdir().unwrap();
        for i in 0..64 {
            std::os::unix::fs::symlink(
                format!("link{}", i + 1),
                dir.path().join(format!("link{i}")),
            )
            .unwrap();
        }
        std::os::unix::fs::symlink("destination", dir.path().join("link64")).unwrap();
        assert_eq!(
            resolve(&dir.path().join("link0/child")).unwrap(),
            dir.path().join("destination/child")
        );
        std::os::unix::fs::symlink("cycle", dir.path().join("cycle")).unwrap();
        assert_eq!(
            resolve(&dir.path().join("cycle/child")).unwrap(),
            dir.path().join("cycle/child")
        );
        assert_eq!(
            expanduser(Path::new("~//repo")).unwrap(),
            expanduser(Path::new("~/repo")).unwrap()
        );
    }
}
