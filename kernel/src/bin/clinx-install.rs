//! Administrator-run installation only; normal releases do not contain this tool.
#[path = "../install_tools.rs"]
mod install;
use install::{Account, Result, System};
use std::collections::BTreeMap;
#[path = "../devops_command.rs"]
mod command;
struct Native;
impl System for Native {
    fn groups(&mut self, name: &str, gid: u32) -> Result<Vec<u32>> {
        let name = std::ffi::CString::new(name).map_err(|_| "ACCOUNT_INVALID")?;
        nix::unistd::getgrouplist(&name, nix::unistd::Gid::from_raw(gid))
            .map(|v| v.iter().map(|g| g.as_raw()).collect())
            .map_err(|e| e.to_string())
    }
    fn non_login(&mut self, name: &str) -> Result<bool> {
        let a = nix::unistd::User::from_name(name)
            .map_err(|e| e.to_string())?
            .ok_or("ACCOUNT_REQUIRED")?;
        Ok([
            std::path::Path::new("/usr/sbin/nologin"),
            std::path::Path::new("/bin/false"),
        ]
        .contains(&a.shell.as_path()))
    }
    fn account(&mut self, name: &str) -> Result<Account> {
        let a = nix::unistd::User::from_name(name)
            .map_err(|e| e.to_string())?
            .ok_or("ACCOUNT_REQUIRED")?;
        Ok(Account {
            name: a.name,
            uid: a.uid.as_raw(),
            gid: a.gid.as_raw(),
        })
    }
    fn account_uid(&mut self, uid: u32) -> Result<Account> {
        let a = nix::unistd::User::from_uid(nix::unistd::Uid::from_raw(uid))
            .map_err(|e| e.to_string())?
            .ok_or("ACCOUNT_REQUIRED")?;
        Ok(Account {
            name: a.name,
            uid: a.uid.as_raw(),
            gid: a.gid.as_raw(),
        })
    }
    fn group(&mut self, name: &str) -> Result<u32> {
        Ok(nix::unistd::Group::from_name(name)
            .map_err(|e| e.to_string())?
            .ok_or("GROUP_REQUIRED")?
            .gid
            .as_raw())
    }
    fn run(&mut self, args: &[String]) -> Result<String> {
        command::run(args, std::time::Duration::from_secs(45))
    }
}
fn run(args: Vec<String>) -> Result<serde_json::Value> {
    if !nix::unistd::geteuid().is_root() {
        return Err("ADMINISTRATOR_REQUIRED".into());
    }
    let mode = args.first().ok_or("MODE_REQUIRED")?;
    let mut values = BTreeMap::new();
    let mut i = 1;
    while i < args.len() {
        let key = args[i].strip_prefix("--").ok_or("INVALID_ARGUMENT")?;
        let val = args.get(i + 1).ok_or("INVALID_ARGUMENT")?;
        if values.insert(key, val.clone()).is_some() {
            return Err("DUPLICATE_ARGUMENT".into());
        }
        i += 2;
    }
    let get = |key| {
        values
            .get(key)
            .cloned()
            .ok_or_else(|| format!("ARGUMENT_REQUIRED:{key}"))
    };
    let l = install::Layout::default();
    let mut sys = Native;
    match mode.as_str() {
        "provision" if values.is_empty() => install::provision(&l, &mut sys, 0),
        "services" => {
            if values.keys().any(|k| {
                ![
                    "release",
                    "operator-home",
                    "bridge-config",
                    "desktop-socket",
                    "desktop-thread-id",
                ]
                .contains(k)
            }) {
                return Err("INVALID_ARGUMENT".into());
            }
            install::install_services(
                &l,
                &install::Services {
                    release: get("release")?.into(),
                    operator_home: get("operator-home")?.into(),
                    bridge_config: get("bridge-config")?.into(),
                    desktop_socket: get("desktop-socket")?.into(),
                    desktop_thread_id: get("desktop-thread-id")?,
                },
                0,
                &mut sys,
            )
        }
        "callers" => {
            if values
                .keys()
                .any(|k| !["release", "legacy-node-state", "operator", "output"].contains(k))
            {
                return Err("INVALID_ARGUMENT".into());
            }
            install::stage_callers(
                &l,
                &install::Callers {
                    release: get("release")?.into(),
                    legacy_node_state: get("legacy-node-state")?.into(),
                    operator: values.get("operator").cloned().unwrap_or("pvxlabs".into()),
                    output: get("output")?.into(),
                },
                0,
                &mut sys,
            )
        }
        _ => Err("MODE_REQUIRED".into()),
    }
}
fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args.is_empty() || args.iter().any(|a| a == "--help") {
        println!(
            "clinx-install provision\nclinx-install services --release PATH --operator-home PATH --bridge-config PATH --desktop-socket PATH --desktop-thread-id ID\nclinx-install callers --release PATH --legacy-node-state PATH --output PATH [--operator USER]\nAdministrator-run maintenance. Installs definitions, never starts services or imports historical databases."
        );
        return;
    }
    match run(args) {
        Ok(v) => println!("{v}"),
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(1);
        }
    }
}
