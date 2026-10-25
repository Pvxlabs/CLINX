//! Native metadata reads use the existing Provider's filesystem identity.
//! The immutable adapter returns observations only; Rust keeps all decisions.
use super::*;
use std::{path::PathBuf, sync::OnceLock};

static OBSERVER: OnceLock<(PathBuf, BTreeMap<String, String>, Option<u32>)> = OnceLock::new();

pub fn configure(program: PathBuf, endpoint: String) -> Result<()> {
    configure_routes(program, BTreeMap::from([("*".into(), endpoint)]))
}

pub fn configure_routes(program: PathBuf, endpoints: BTreeMap<String, String>) -> Result<()> {
    configure_inner(program, endpoints, None)
}

pub fn configure_qualification(
    program: PathBuf,
    endpoints: BTreeMap<String, String>,
    uid: u32,
) -> Result<()> {
    configure_inner(program, endpoints, Some(uid))
}

fn configure_inner(
    program: PathBuf,
    endpoints: BTreeMap<String, String>,
    uid: Option<u32>,
) -> Result<()> {
    if !program.is_absolute()
        || endpoints.is_empty()
        || endpoints
            .iter()
            .any(|(host, path)| host.is_empty() || !Path::new(path).is_absolute())
    {
        return Err("INVALID_WORKSPACE_OBSERVER".into());
    }
    OBSERVER
        .set((program, endpoints, uid))
        .map_err(|_| "WORKSPACE_OBSERVER_ALREADY_CONFIGURED".into())
}

pub(super) fn read(host: &str, mode: &str, cwd: &str, args: &[&str]) -> Result<Option<Value>> {
    let Some((program, endpoints, uid)) = OBSERVER.get() else {
        // Direct Store tests and qualification retain their own local identity.
        // Production configures the immutable Provider adapter before serving.
        return Ok(None);
    };
    let endpoint = endpoints
        .get(host)
        .or_else(|| endpoints.get("*"))
        .ok_or("WORKSPACE_PROVIDER_ROUTE_REQUIRED")?;
    let request = json!({"host":host,"mode":mode,"cwd":cwd,"args":args});
    let mut command = Command::new("/usr/bin/timeout");
    command
        .arg("4")
        .arg(program)
        .arg("--observe")
        .arg(endpoint)
        .arg(core::canonical(&request));
    if let Some(uid) = uid {
        command.args(["--qualification-provider-uid", &uid.to_string()]);
    }
    let out = command
        .output()
        .map_err(|_| "WORKSPACE_OBSERVER_UNAVAILABLE")?;
    if !out.status.success() || out.stdout.len() > 65536 {
        return Err("WORKSPACE_OBSERVER_UNAVAILABLE".into());
    }
    let value: Value =
        serde_json::from_slice(&out.stdout).map_err(|_| "WORKSPACE_OBSERVER_INVALID")?;
    if let Some(error) = value["error"].as_str() {
        return Err(match error {
            "WORKSPACE_CWD_UNREADABLE"
            | "WORKSPACE_IDENTITY_UNAVAILABLE"
            | "WORKSPACE_CONTRACT_INVALID"
            | "WORKSPACE_PROBE_INVALID"
            | "WORKSPACE_OBSERVER_IDENTITY_CONFLICT"
            | "WORKSPACE_OBSERVER_UNAVAILABLE" => error.into(),
            _ => "WORKSPACE_OBSERVER_INVALID".into(),
        });
    }
    if value["observer_source"] != "CODEX_PROVIDER_COMMAND_EXEC"
        || value["observer_uid"].as_u64().is_none_or(|uid| uid == 0)
    {
        return Err("WORKSPACE_OBSERVER_INVALID".into());
    }
    if mode == "git" {
        if value["value"].as_str().is_none() {
            return Err("WORKSPACE_OBSERVER_INVALID".into());
        }
    } else {
        let identity =
            string(&value, "repository_identity").map_err(|_| "WORKSPACE_OBSERVER_INVALID")?;
        let components: Vec<_> = identity.split(':').collect();
        if components.len() != 3
            || components[0] != "git-common"
            || components[1..].iter().any(|s| s.parse::<u64>().is_err())
        {
            return Err("WORKSPACE_OBSERVER_INVALID".into());
        }
        if mode == "workspace" {
            for key in [
                "cwd",
                "repository_common_dir",
                "repository_root",
                "worktree",
            ] {
                let path = string(&value, key).map_err(|_| "WORKSPACE_OBSERVER_INVALID")?;
                if !Path::new(path).is_absolute() || path.contains('\0') {
                    return Err("WORKSPACE_OBSERVER_INVALID".into());
                }
            }
            let head = string(&value, "head").map_err(|_| "WORKSPACE_OBSERVER_INVALID")?;
            if ![40, 64].contains(&head.len())
                || !head.bytes().all(|b| b.is_ascii_hexdigit())
                || value["branch"]
                    .as_str()
                    .is_none_or(|s| s.len() > 4096 || s.contains('\0'))
            {
                return Err("WORKSPACE_OBSERVER_INVALID".into());
            }
        }
    }
    Ok(Some(value))
}
