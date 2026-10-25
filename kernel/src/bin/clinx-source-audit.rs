//! Stable source-size acceptance gate. Never rewrites files or language metadata.
use serde::Serialize;
use serde_json::{Value, json};
use std::{collections::BTreeMap, path::Path, process::Command};

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
                    "clinx-source-audit [--revision COMMIT] [--max-python-percent 5]\nRead-only source census. Exit 0 only when Python bytes are strictly below the limit; exit 2 for an unmet target."
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
