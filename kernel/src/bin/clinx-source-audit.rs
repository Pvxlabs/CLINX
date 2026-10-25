//! Stable source-size acceptance gate. Never rewrites files or language metadata.
use serde::Serialize;
use serde_json::{Value, json};
use std::{collections::BTreeMap, path::Path, process::Command};

fn structure() -> Result<Value, String> {
    let inventory: Value =
        serde_json::from_slice(&source(None, "architecture/python-sources.json")?)
            .map_err(|_| "INVALID_SOURCE_MANIFEST")?;
    if inventory["schema"] != 1 {
        return Err("UNKNOWN_SOURCE_MANIFEST_SCHEMA".into());
    }
    let rows = inventory["modules"]
        .as_array()
        .ok_or("INVALID_SOURCE_MANIFEST")?;
    let mut seen = std::collections::BTreeSet::new();
    for row in rows {
        let name = row["path"].as_str().ok_or("INVALID_SOURCE_PATH")?;
        let p = Path::new(name);
        if p.is_absolute()
            || p.components()
                .any(|c| !matches!(c, std::path::Component::Normal(_)))
            || p.extension().and_then(|s| s.to_str()) != Some("py")
        {
            return Err("INVALID_SOURCE_PATH".into());
        }
        if !seen.insert(name) {
            return Err("DUPLICATE_SOURCE_MODULE".into());
        }
        if row["role"] != "DEAD" || row["profiles"] != json!([]) {
            return Err(format!("PYTHON_RETIREMENT_INCOMPLETE:{name}"));
        }
        if std::fs::symlink_metadata(p).is_ok() {
            return Err(format!("RETIRED_SOURCE_PRESENT:{name}"));
        }
    }
    let names = git(&[
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    ])?;
    for raw in names.split(|b| *b == 0).filter(|b| !b.is_empty()) {
        let name = std::str::from_utf8(raw).map_err(|_| "NON_UTF8_SOURCE_PATH")?;
        if name.ends_with(".py") && std::fs::symlink_metadata(name).is_ok() {
            return Err(format!("UNDECLARED_PYTHON_SOURCE:{name}"));
        }
    }
    let native: Value = serde_json::from_slice(&source(None, "architecture/native-runtime.json")?)
        .map_err(|_| "INVALID_NATIVE_MANIFEST")?;
    for key in [
        "monitor_python_sources",
        "monitor_python_dependencies",
        "release_python_sources",
        "release_python_dependencies",
    ] {
        if native[key] != json!([]) {
            return Err(format!("PYTHON_IN_RUNTIME:{key}"));
        }
    }
    if native["monitor_helpers"] != json!(["clinx-provider", "clinx-node", "clinx-node-daemon"]) {
        return Err("NATIVE_HELPER_MANIFEST_INVALID".into());
    }
    let data: Value = serde_json::from_slice(&source(None, "architecture/runtime-data.json")?)
        .map_err(|_| "INVALID_RUNTIME_DATA")?;
    let mut seen = std::collections::BTreeSet::new();
    for row in data.as_array().ok_or("INVALID_RUNTIME_DATA")? {
        let name = row["path"].as_str().ok_or("INVALID_RUNTIME_DATA_PATH")?;
        let p = Path::new(name);
        if p.is_absolute()
            || p.components()
                .any(|c| !matches!(c, std::path::Component::Normal(_)))
            || p.extension().and_then(|s| s.to_str()) != Some("json")
        {
            return Err("INVALID_RUNTIME_DATA_PATH".into());
        }
        if !seen.insert(name) {
            return Err("DUPLICATE_RUNTIME_DATA".into());
        }
        if !p.is_file() || p.is_symlink() {
            return Err(format!("RUNTIME_DATA_MISSING:{name}"));
        }
        serde_json::from_slice::<Value>(&source(None, name)?)
            .map_err(|_| format!("RUNTIME_DATA_INVALID:{name}"))?;
    }
    Ok(
        json!({"status":"PASS","python_modules":0,"root_python_modules":0,"runtime_python_sources":0,"retired_python_records":rows.len(),"monitor_native_helpers":native["monitor_helpers"]}),
    )
}

#[derive(Default, Serialize)]
struct Size {
    files: usize,
    lines: usize,
    bytes: usize,
}
impl Size {
    fn add(&mut self, data: &[u8]) {
        self.files += 1;
        self.bytes += data.len();
        self.lines += data.iter().filter(|b| **b == b'\n').count()
            + usize::from(!data.is_empty() && data.last() != Some(&b'\n'));
    }
}
fn git(args: &[&str]) -> Result<Vec<u8>, String> {
    let output = Command::new("git")
        .args(args)
        .output()
        .map_err(|e| e.to_string())?;
    if !output.status.success() {
        return Err("GIT_SOURCE_READ_FAILED".into());
    }
    Ok(output.stdout)
}
fn source(revision: Option<&str>, path: &str) -> Result<Vec<u8>, String> {
    if let Some(rev) = revision {
        git(&["show", &format!("{rev}:{path}")])
    } else {
        std::fs::read(path).map_err(|e| format!("{path}: {e}"))
    }
}
fn audit(revision: Option<&str>, limit: f64) -> Result<Value, String> {
    let rev = revision
        .map(|r| git(&["rev-parse", "--verify", &format!("{r}^{{commit}}")]))
        .transpose()?
        .map(|b| String::from_utf8_lossy(&b).trim().to_owned());
    let names = if let Some(r) = rev.as_deref() {
        git(&["ls-tree", "-rz", "--name-only", r])?
    } else {
        git(&[
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ])?
    };
    let manifest: Value =
        serde_json::from_slice(&source(rev.as_deref(), "architecture/python-sources.json")?)
            .map_err(|e| e.to_string())?;
    let roles: BTreeMap<_, _> = manifest["modules"]
        .as_array()
        .ok_or("INVALID_MANIFEST")?
        .iter()
        .filter_map(|r| Some((r["path"].as_str()?, r["role"].as_str()?)))
        .collect();
    let mut languages: BTreeMap<&str, Size> = ["Python", "Rust", "Swift", "Go", "C", "Shell"]
        .into_iter()
        .map(|n| (n, Size::default()))
        .collect();
    let mut python_roles: BTreeMap<&str, Size> = BTreeMap::new();
    let mut roots = vec![];
    let mut seen = std::collections::BTreeSet::new();
    for raw in names.split(|b| *b == 0).filter(|b| !b.is_empty()) {
        let path = std::str::from_utf8(raw).map_err(|_| "NON_UTF8_SOURCE_PATH")?;
        if !seen.insert(path) || rev.is_none() && !Path::new(path).is_file() {
            continue;
        }
        let data = source(rev.as_deref(), path)?;
        let language = match Path::new(path).extension().and_then(|s| s.to_str()) {
            Some("py") => Some("Python"),
            Some("rs") => Some("Rust"),
            Some("swift") => Some("Swift"),
            Some("go") => Some("Go"),
            Some("c") => Some("C"),
            Some("sh") => Some("Shell"),
            _ if [
                b"#!/bin/sh".as_slice(),
                b"#!/usr/bin/env bash",
                b"#!/bin/bash",
                b"#!/usr/bin/env zsh",
            ]
            .iter()
            .any(|p| data.starts_with(p)) =>
            {
                Some("Shell")
            }
            _ => None,
        };
        if let Some(language) = language {
            languages.get_mut(language).unwrap().add(&data);
        }
        if language == Some("Python") {
            python_roles
                .entry(roles.get(path).copied().unwrap_or("UNCLASSIFIED"))
                .or_default()
                .add(&data);
            if !path.contains('/') {
                roots.push(path.to_owned());
            }
        }
    }
    let bytes: usize = languages.values().map(|s| s.bytes).sum();
    let lines: usize = languages.values().map(|s| s.lines).sum();
    let python = &languages["Python"];
    let share = 100. * python.bytes as f64 / bytes.max(1) as f64;
    Ok(
        json!({"schema":"CLINX_SOURCE_SIZE_V1","scope":"all tracked and nonignored source; includes qualification and build tools; six-language physical bytes","revision":rev,"languages":languages,"python_roles":python_roles,"root_python":roots,"python_byte_percent":share,"python_line_percent":100.*python.lines as f64/lines.max(1) as f64,"maximum_python_byte_percent":limit,"target_pass":share<limit}),
    )
}
fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args == ["--check-structure"] {
        match structure() {
            Ok(v) => println!("{v}"),
            Err(e) => {
                eprintln!("{e}");
                std::process::exit(1);
            }
        }
        return;
    }
    let mut rev = None;
    let mut limit = 5.;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--revision" if i + 1 < args.len() => {
                i += 1;
                rev = Some(args[i].as_str());
            }
            "--max-python-percent" if i + 1 < args.len() => {
                i += 1;
                limit = args[i].parse().unwrap_or(f64::NAN);
            }
            "--help" => {
                println!(
                    "clinx-source-audit [--revision COMMIT] [--max-python-percent 5]\nclinx-source-audit --check-structure\nRead-only source census. Exit 0 only when Python bytes are strictly below the limit; exit 2 for an unmet target. Structure mode rejects Python source or runtime dependencies and validates retained data."
                );
                return;
            }
            _ => {
                eprintln!("INVALID_ARGUMENTS");
                std::process::exit(1);
            }
        }
        i += 1;
    }
    if !limit.is_finite() || limit <= 0. || limit > 100. {
        eprintln!("INVALID_PERCENT_LIMIT");
        std::process::exit(1);
    }
    match audit(rev, limit) {
        Ok(v) => {
            println!("{}", serde_json::to_string_pretty(&v).unwrap());
            if v["target_pass"] != true {
                std::process::exit(2);
            }
        }
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(1);
        }
    }
}
