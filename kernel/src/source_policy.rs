//! Repository-only gates. No runtime imports, network calls, or state mutation.
use regex::Regex;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Component, Path},
    process::Command,
};
type Result<T> = std::result::Result<T, String>;
fn read(root: &Path, p: &str) -> Result<String> {
    fs::read_to_string(root.join(p)).map_err(|e| format!("SOURCE_READ:{p}:{e}"))
}
fn value(root: &Path, p: &str) -> Result<Value> {
    serde_json::from_str(&read(root, p)?).map_err(|e| format!("MANIFEST_INVALID:{p}:{e}"))
}
fn re(p: &str) -> Regex {
    Regex::new(p).expect("static policy expression")
}
fn command(root: &Path, name: &str, args: &[&str]) -> Result<Vec<u8>> {
    let o = Command::new(name)
        .args(args)
        .current_dir(root)
        .output()
        .map_err(|e| format!("POLICY_COMMAND:{name}:{e}"))?;
    if !o.status.success() {
        return Err(format!(
            "POLICY_COMMAND_FAILED:{name}:{}",
            String::from_utf8_lossy(&o.stderr)
        ));
    }
    Ok(o.stdout)
}
fn names(root: &Path) -> Result<BTreeSet<String>> {
    command(
        root,
        "git",
        &[
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
    )?
    .split(|b| *b == 0)
    .filter(|s| !s.is_empty())
    .map(|s| String::from_utf8(s.to_vec()).map_err(|_| "NON_UTF8_PATH".into()))
    .collect()
}
fn relative(p: &str) -> bool {
    !p.is_empty()
        && Path::new(p)
            .components()
            .all(|c| matches!(c, Component::Normal(_)))
}
fn strings(v: &Value) -> Result<BTreeSet<String>> {
    let a = v.as_array().ok_or("POLICY_ARRAY_REQUIRED")?;
    let set: BTreeSet<_> = a
        .iter()
        .map(|s| {
            s.as_str()
                .map(str::to_owned)
                .ok_or("POLICY_STRING_REQUIRED".into())
        })
        .collect::<Result<_>>()?;
    if a.len() != set.len() {
        return Err("POLICY_DUPLICATE_ENTRY".into());
    }
    Ok(set)
}
fn sha(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn without_fences(s: &str) -> String {
    let mut fence = None;
    s.lines()
        .map(|line| {
            let t = line.trim_start();
            if t.starts_with("```") || t.starts_with("~~~") {
                let ch = t.as_bytes()[0];
                if fence == Some(ch) {
                    fence = None;
                } else if fence.is_none() {
                    fence = Some(ch);
                }
                return "";
            }
            if fence.is_some() { "" } else { line }
        })
        .collect::<Vec<_>>()
        .join("\n")
}
fn anchors(s: &str) -> BTreeSet<String> {
    let mut counts = BTreeMap::<String, usize>::new();
    let mut result = BTreeSet::new();
    for cap in re(r"(?m)^#{1,6}\s+(.+?)\s*#*\s*$").captures_iter(&without_fences(s)) {
        let base: String = cap[1]
            .to_lowercase()
            .chars()
            .filter(|c| c.is_alphanumeric() || *c == '-' || *c == '_' || c.is_whitespace())
            .map(|c| if c.is_whitespace() { '-' } else { c })
            .collect();
        let count = counts.entry(base.clone()).or_default();
        result.insert(if *count == 0 {
            base
        } else {
            format!("{base}-{count}")
        });
        *count += 1;
    }
    for cap in re(r#"(?:id|name)=["']([^"']+)["']"#).captures_iter(s) {
        result.insert(cap[1].into());
    }
    result
}
fn unescape_url(s: &str) -> Result<String> {
    let mut bytes = Vec::new();
    let b = s.as_bytes();
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' {
            let h = s.get(i + 1..i + 3).ok_or("INVALID_LINK_ENCODING")?;
            bytes.push(u8::from_str_radix(h, 16).map_err(|_| "INVALID_LINK_ENCODING")?);
            i += 3;
        } else {
            bytes.push(b[i]);
            i += 1;
        }
    }
    String::from_utf8(bytes).map_err(|_| "INVALID_LINK_ENCODING".into())
}
fn link(root: &Path, doc: &str, raw: &str) -> Result<()> {
    let raw = raw.trim();
    let target = if raw.starts_with('<') {
        raw.split('>').next().unwrap_or("").trim_start_matches('<')
    } else {
        raw.split_whitespace().next().unwrap_or("")
    };
    if target.is_empty() {
        return Err(format!("EMPTY_DOC_LINK:{doc}"));
    }
    if target.starts_with('/') || re(r"^[a-zA-Z][a-zA-Z0-9+.-]*:").is_match(target) {
        return Ok(());
    }
    let target = unescape_url(target)?;
    let (p, anchor) = target
        .split_once('#')
        .map_or((target.as_str(), None), |(p, a)| (p, Some(a)));
    let file = if p.is_empty() {
        root.join(doc)
    } else {
        root.join(doc).parent().unwrap().join(p)
    };
    let actual = fs::canonicalize(&file).map_err(|_| format!("BROKEN_DOC_LINK:{doc}:{target}"))?;
    if !actual.starts_with(root) {
        return Err(format!("DOC_LINK_ESCAPES_REPOSITORY:{doc}:{target}"));
    }
    if let Some(a) = anchor.filter(|a| !a.is_empty())
        && actual.extension().is_some_and(|e| e == "md")
        && !anchors(&fs::read_to_string(&actual).map_err(|e| e.to_string())?).contains(a)
    {
        return Err(format!("BROKEN_DOC_ANCHOR:{doc}:{target}"));
    }
    Ok(())
}
fn docs(root: &Path, files: &BTreeSet<String>) -> Result<usize> {
    let index = read(root, "docs/README.md")?;
    let mut count = 0;
    let mut errors = Vec::new();
    for doc in files.iter().filter(|n| n.ends_with(".md")) {
        let source = read(root, doc)?;
        let body = without_fences(&source);
        let body = re(r"`+[^`\n]*`+").replace_all(&body, "");
        for cap in re(r"\[[^\]\n]*\]\(([^\n]+?)\)").captures_iter(&body) {
            if let Err(e) = link(root, doc, &cap[1]) {
                errors.push(e);
            }
        }
        let mut refs = BTreeSet::new();
        for cap in re(r"(?m)^\s*\[([^\]]+)\]:\s*(.+)$").captures_iter(&body) {
            refs.insert(cap[1].to_lowercase());
            if let Err(e) = link(root, doc, &cap[2]) {
                errors.push(e);
            }
        }
        for cap in re(r"\[([^\]\n]+)\]\[([^\]\n]*)\]").captures_iter(&body) {
            let key = if cap[2].is_empty() { &cap[1] } else { &cap[2] };
            if !refs.contains(&key.to_lowercase()) {
                return Err(format!("UNDEFINED_DOC_REFERENCE:{doc}:{key}"));
            }
        }
        let row = index
            .lines()
            .find(|l| l.starts_with(&format!("| [{doc}](")))
            .ok_or_else(|| format!("UNINDEXED_DOCUMENT:{doc}"))?;
        let role = row.split('|').nth(2).unwrap_or("").trim();
        if !["CURRENT", "REFERENCE", "HISTORICAL", "SUPERSEDED"].contains(&role) {
            return Err(format!("DOCUMENT_ROLE_INVALID:{doc}"));
        }
        if role == "CURRENT" {
            for field in ["用途：", "适用范围：", "状态：CURRENT", "验证"] {
                if !source.contains(field) {
                    return Err(format!("DOCUMENT_METADATA_MISSING:{doc}:{field}"));
                }
            }
        }
        count += 1;
    }
    for entry in [
        "README.md",
        "AGENTS.md",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
        "docs/CONTRACTS.md",
        "docs/OPERATIONS.md",
        "docs/DOCUMENTATION.md",
        "docs/REPOSITORY_ARCHITECTURE.md",
        "docs/PRODUCT_POSITIONING.md",
        "docs/TESTING.md",
    ] {
        if !files.contains(entry) {
            return Err(format!("DOCUMENT_ENTRY_MISSING:{entry}"));
        }
    }
    let contracts = read(root, "docs/CONTRACTS.md")?;
    for p in [
        "execution_core_v1.json",
        "authority_store.rs",
        "provider.rs",
        "host_transport.rs",
        "node_api.rs",
        "mcp.rs",
        "native_transport.rs",
    ] {
        if !contracts.contains(p) {
            return Err(format!("CORE_CONTRACT_REFERENCE_MISSING:{p}"));
        }
    }
    if !errors.is_empty() {
        return Err(errors.join("\n"));
    }
    Ok(count)
}
fn code_file(path: &str, bytes: &[u8]) -> bool {
    let ext = Path::new(path)
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("");
    matches!(
        ext,
        "rs" | "swift"
            | "c"
            | "h"
            | "cc"
            | "cpp"
            | "go"
            | "sh"
            | "bash"
            | "zsh"
            | "py"
            | "pyc"
            | "pyo"
            | "pyw"
            | "js"
            | "ts"
            | "tsx"
            | "jsx"
            | "rb"
            | "pl"
            | "java"
            | "kt"
            | "lua"
            | "ps1"
            | "wasm"
            | "so"
            | "dylib"
            | "exe"
    ) || bytes.starts_with(b"#!") && !path.ends_with(".md")
}
// Resolve direct crate roots, grouped use roots, #[path], mod, and literal includes.
// This is a conservative source gate, not a semantic Rust parser or a dynamic I/O proof.
fn dependencies(root: &Path, file: &str, s: &str) -> BTreeSet<String> {
    let mut result = BTreeSet::new();
    let mut add_root = |name: &str| {
        for p in [
            format!("kernel/src/{name}.rs"),
            format!("kernel/src/{name}/mod.rs"),
        ] {
            if root.join(&p).is_file() && p != file {
                result.insert(p);
            }
        }
    };
    for cap in re(r"(?:crate|clinx_decision_kernel)::\s*([A-Za-z_][A-Za-z0-9_]*)").captures_iter(s)
    {
        add_root(&cap[1]);
    }
    for cap in re(r"(?s)use\s+(?:crate|clinx_decision_kernel)::\s*\{(.*?)\};").captures_iter(s) {
        let mut depth = 0;
        let mut token = String::new();
        let mut first = true;
        for c in cap[1].chars().chain(std::iter::once(',')) {
            if depth == 0 && first && (c.is_ascii_alphanumeric() || c == '_') {
                token.push(c);
                continue;
            }
            if !token.is_empty() {
                add_root(&token);
                token.clear();
                first = false;
            }
            match c {
                '{' => depth += 1,
                '}' => depth -= 1,
                ',' if depth == 0 => first = true,
                _ => (),
            }
        }
    }
    let parent = root.join(file).parent().unwrap().to_path_buf();
    for cap in re(r#"(?:path\s*=\s*|include!\s*\(\s*)"([^"]+\.rs)""#).captures_iter(s) {
        if let Ok(p) = fs::canonicalize(parent.join(&cap[1]))
            && let Ok(p) = p.strip_prefix(root)
        {
            result.insert(p.to_string_lossy().into_owned());
        }
    }
    for cap in re(r"\bmod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;").captures_iter(s) {
        for dir in [
            parent.clone(),
            parent.join(Path::new(file).file_stem().unwrap()),
        ] {
            for p in [
                dir.join(format!("{}.rs", &cap[1])),
                dir.join(&cap[1]).join("mod.rs"),
            ] {
                if p.is_file()
                    && let Ok(p) = p.strip_prefix(root)
                {
                    result.insert(p.to_string_lossy().into_owned());
                }
            }
        }
    }
    result.remove(file);
    result
}
fn sources(root: &Path, files: &BTreeSet<String>, policy: &Value) -> Result<usize> {
    if policy["schema"] != 1 {
        return Err("SOURCE_POLICY_SCHEMA".into());
    }
    let rows = policy["sources"].as_object().ok_or("SOURCE_POLICY_ROWS")?;
    for (p, row) in rows {
        if !relative(p) || !files.contains(p) || !root.join(p).is_file() {
            return Err(format!("CLASSIFIED_SOURCE_MISSING:{p}"));
        }
        let text = read(root, p)?;
        let role = row["role"].as_str().ok_or("SOURCE_ROLE_REQUIRED")?;
        if p == "kernel/src/lib.rs" && role != "runtime" {
            return Err("RUNTIME_ROOT_ROLE_INVALID".into());
        }
        if text.starts_with("#!")
            && !p.ends_with(".rs")
            && !(p.ends_with(".swift") && text.lines().next() == Some("#!/usr/bin/env swift"))
            && ![
                "#!/bin/sh",
                "#!/bin/bash",
                "#!/usr/bin/env bash",
                "#!/usr/bin/env zsh",
            ]
            .iter()
            .any(|prefix| text.lines().next() == Some(prefix))
        {
            return Err(format!("SCRIPT_LANGUAGE_FORBIDDEN:{p}"));
        }
        if ![
            "runtime",
            "tool",
            "qualification",
            "ui",
            "glue",
            "historical",
            "legacy_locked",
        ]
        .contains(&role)
        {
            return Err(format!("SOURCE_ROLE_INVALID:{p}"));
        }
        if role == "legacy_locked" {
            if ![
                "runtime/host_worker.c",
                "fixtures/check_transport_profile.go",
            ]
            .contains(&p.as_str())
                || row["sha256"] != sha(text.as_bytes())
            {
                return Err(format!("LEGACY_LANGUAGE_CHANGE_REQUIRES_APPROVAL:{p}"));
            }
        } else if p.ends_with(".rs") {
            if !p.starts_with("kernel/") && !p.starts_with("native/opaque_pairing/") {
                return Err(format!("RUST_SOURCE_OUTSIDE_MODULE_ROOT:{p}"));
            }
        } else if p.ends_with(".swift") {
            if !p.starts_with("MonitorApp/")
                && !(role == "historical" && p.starts_with("docs/monitor/"))
            {
                return Err(format!("SWIFT_BACKEND_FORBIDDEN:{p}"));
            }
        } else if role != "glue" || !(p.starts_with("bin/") || p.starts_with("MonitorApp/Scripts/"))
        {
            return Err(format!("IMPLEMENTATION_LANGUAGE_FORBIDDEN:{p}"));
        }
        if p.ends_with(".rs") {
            let actual = dependencies(root, p, &text);
            let declared = strings(&row["dependencies"])?;
            if actual != declared {
                return Err(format!(
                    "MODULE_DEPENDENCY_DRIFT:{p}:actual={actual:?}:declared={declared:?}"
                ));
            }
            for dep in actual {
                let target = rows
                    .get(&dep)
                    .ok_or_else(|| format!("UNCLASSIFIED_DEPENDENCY:{p}:{dep}"))?;
                if role == "runtime" && target["role"] != "runtime" {
                    return Err(format!("RUNTIME_DEPENDENCY_DIRECTION:{p}:{dep}"));
                }
            }
        }
        if ["runtime", "tool", "ui", "glue"].contains(&role) {
            let active = text
                .lines()
                .filter(|l| !l.trim_start().starts_with("//") && !l.trim_start().starts_with('#'))
                .collect::<Vec<_>>()
                .join("\n");
            if re(r#"(?i)(Command::new\s*\(\s*"(?:[^"\n]*/)?(?:python[0-9.]*|pypy[0-9.]*|pip[0-9.]*|pytest)"|(?:^|[;\n]|\bexec)\s*(?:/\S*/)?(?:python[0-9.]*|pypy[0-9.]*|pip[0-9.]*|pytest)\s)"#).is_match(&active) {return Err(format!("PYTHON_EXECUTION_FORBIDDEN:{p}"));}
        }
    }
    for p in files {
        let basename = Path::new(p).file_name().unwrap().to_string_lossy();
        if [
            "pyproject.toml",
            "Pipfile",
            "Pipfile.lock",
            "setup.py",
            "setup.cfg",
            "tox.ini",
        ]
        .contains(&basename.as_ref())
            || basename.starts_with("requirements") && basename.ends_with(".txt")
        {
            return Err(format!("PYTHON_BUILD_DEPENDENCY_FORBIDDEN:{p}"));
        }
        let data = fs::read(root.join(p)).map_err(|e| format!("SOURCE_READ:{p}:{e}"))?;
        if root.join(p).is_symlink() {
            return Err(format!("CONTROLLED_SYMLINK_FORBIDDEN:{p}"));
        }
        let controlled_root = [
            "kernel/src/",
            "native/opaque_pairing/src/",
            "MonitorApp/Sources/",
            "runtime/",
            "bin/",
        ]
        .iter()
        .any(|prefix| p.starts_with(prefix));
        let data_extension = Path::new(p)
            .extension()
            .and_then(|e| e.to_str())
            .is_some_and(|e| ["json", "sql", "service", "conf", "in", "socket", "md"].contains(&e));
        if (code_file(p, &data) || controlled_root && !data_extension) && !rows.contains_key(p) {
            return Err(format!("UNCLASSIFIED_SOURCE:{p}"));
        }
        if (p.ends_with(".yml") || p.ends_with(".yaml"))
            && re(r"(?im)(setup-python|(?:^|\s)(?:python[0-9.]*|pip[0-9.]*|pytest)(?:\s|$))")
                .is_match(&String::from_utf8_lossy(&data))
        {
            return Err(format!("PYTHON_CI_DEPENDENCY_FORBIDDEN:{p}"));
        }
        if (p.ends_with("Cargo.toml") || p.ends_with("Cargo.lock"))
            && re(r#"(?im)(?:name\s*=\s*")?(pyo3[^"\s=]*|cpython|rustpython[^"\s=]*|python[0-9.-]*)(?:"|\s*=)"#).is_match(&String::from_utf8_lossy(&data))
        {
            return Err(format!("PYTHON_DEPENDENCY_FORBIDDEN:{p}"));
        }
    }
    Ok(rows.len())
}
fn release_members(root: &Path, policy: &Value) -> Result<BTreeSet<String>> {
    let declared = strings(&policy["release_binaries"])?;
    let build = read(root, "kernel/src/build_release.rs")?;
    let list = build
        .split("pub const BINARIES:")
        .nth(1)
        .and_then(|s| s.split("];").next())
        .ok_or("RELEASE_LIST_MISSING")?;
    let actual: BTreeSet<String> = re(r#"\("(clinx-[^"]+)","#)
        .captures_iter(list)
        .map(|c| c[1].into())
        .collect();
    if declared != actual {
        return Err("RELEASE_BINARY_LIST_DRIFT".into());
    }
    let native = value(root, "architecture/native-runtime.json")?;
    let helpers = strings(&native["monitor_helpers"])?;
    let excluded = strings(&native["excluded_development_binaries"])?;
    if !helpers.is_subset(&declared) || !excluded.is_disjoint(&declared) {
        return Err("RELEASE_ROLE_CONFLICT".into());
    }
    let app = read(root, "MonitorApp/Scripts/build-app.sh")?;
    let helper_loop = re(r"for helper in ([^;\n]+); do");
    let helper_names = helper_loop
        .captures(&app)
        .ok_or("MONITOR_HELPER_LOOP_MISSING")?;
    let built_helpers = helper_names[1]
        .split_whitespace()
        .map(str::to_owned)
        .collect::<BTreeSet<_>>();
    if built_helpers != helpers {
        return Err("MONITOR_HELPER_BUILD_DRIFT".into());
    }
    for binary in &excluded {
        let p = format!("kernel/src/bin/{binary}.rs");
        if policy["sources"][&p]["role"] != "tool" {
            return Err(format!("DEVELOPMENT_ENTRY_ROLE_INVALID:{p}"));
        }
    }
    for binary in declared
        .iter()
        .chain(strings(&policy["standalone_binaries"])?.iter())
    {
        let p = format!("kernel/src/bin/{binary}.rs");
        if policy["sources"][&p]["role"] != "runtime" {
            return Err(format!("RUNTIME_ENTRY_ROLE_INVALID:{p}"));
        }
    }
    let mut members = declared;
    members.insert("host-worker".into());
    for row in value(root, "architecture/runtime-data.json")?
        .as_array()
        .ok_or("RUNTIME_DATA_INVALID")?
    {
        if row["profiles"]
            .as_array()
            .is_some_and(|a| a.contains(&json!("rust")))
        {
            let p = row["path"].as_str().ok_or("RUNTIME_DATA_INVALID")?;
            if !relative(p) {
                return Err("RUNTIME_DATA_INVALID".into());
            }
            members.insert(format!("control/{p}"));
        }
    }
    Ok(members)
}
pub fn project(root: &Path) -> Result<Value> {
    let root = fs::canonicalize(root).map_err(|e| e.to_string())?;
    let files = names(&root)?;
    let policy = value(&root, "architecture/source-policy.json")?;
    let documents = docs(&root, &files)?;
    let sources = sources(&root, &files, &policy)?;
    let members = release_members(&root, &policy)?;
    let metadata: Value = serde_json::from_slice(&command(
        &root,
        "cargo",
        &[
            "metadata",
            "--locked",
            "--offline",
            "--no-deps",
            "--format-version",
            "1",
            "--manifest-path",
            "kernel/Cargo.toml",
        ],
    )?)
    .map_err(|_| "CARGO_METADATA_INVALID")?;
    let targets: BTreeSet<String> = metadata["packages"][0]["targets"]
        .as_array()
        .ok_or("CARGO_TARGETS_MISSING")?
        .iter()
        .filter(|t| {
            t["kind"]
                .as_array()
                .is_some_and(|a| a.contains(&json!("bin")))
        })
        .filter_map(|t| t["name"].as_str().map(str::to_owned))
        .collect();
    let native = value(&root, "architecture/native-runtime.json")?;
    let mut expected = strings(&policy["release_binaries"])?;
    expected.extend(strings(&native["excluded_development_binaries"])?);
    expected.extend(strings(&policy["standalone_binaries"])?);
    if targets != expected {
        return Err(format!(
            "CARGO_RELEASE_CLASSIFICATION_DRIFT:actual={targets:?}:declared={expected:?}"
        ));
    }
    Ok(
        json!({"status":"PASS","documents":documents,"classified_sources":sources,"cargo_binaries":targets.len(),"release_members":members.len(),"scope":"CURRENT_SOURCE_NOT_DEPLOYMENT"}),
    )
}
fn walk(root: &Path, dir: &Path, out: &mut BTreeSet<String>) -> Result<()> {
    for entry in fs::read_dir(dir).map_err(|e| e.to_string())? {
        let p = entry.map_err(|e| e.to_string())?.path();
        let m = fs::symlink_metadata(&p).map_err(|e| e.to_string())?;
        if m.is_symlink() || !m.is_dir() && !m.is_file() {
            return Err(format!("ARTIFACT_SPECIAL_FILE:{}", p.display()));
        }
        if m.is_dir() {
            walk(root, &p, out)?;
        } else {
            out.insert(p.strip_prefix(root).unwrap().to_string_lossy().into_owned());
        }
    }
    Ok(())
}
pub fn artifact(root: &Path, dir: &Path) -> Result<Value> {
    let policy = value(root, "architecture/source-policy.json")?;
    let expected = release_members(root, &policy)?;
    let manifest = value(dir, "manifest.json")?;
    for key in ["python_sources", "python_dependencies"] {
        if manifest[key] != json!([]) {
            return Err(format!("PYTHON_IN_ARTIFACT:{key}"));
        }
    }
    let rows = manifest["files"]
        .as_object()
        .ok_or("ARTIFACT_MANIFEST_FILES_REQUIRED")?;
    let declared = rows.keys().cloned().collect::<BTreeSet<_>>();
    if declared != expected {
        return Err("ARTIFACT_MEMBERS_MISMATCH".into());
    }
    let mut actual = BTreeSet::new();
    walk(dir, dir, &mut actual)?;
    actual.remove("manifest.json");
    if actual != expected {
        return Err("ARTIFACT_UNDECLARED_OR_MISSING_FILE".into());
    }
    for (p, hash) in rows {
        if !relative(p) {
            return Err("ARTIFACT_PATH_INVALID".into());
        }
        let bytes = fs::read(dir.join(p)).map_err(|e| e.to_string())?;
        if hash != &sha(&bytes) {
            return Err(format!("ARTIFACT_HASH_MISMATCH:{p}"));
        }
        if let Some(source) = p.strip_prefix("control/")
            && fs::read(root.join(source)).map_err(|e| e.to_string())? != bytes
        {
            return Err(format!("ARTIFACT_DATA_SOURCE_MISMATCH:{p}"));
        }
        if !p.starts_with("control/") {
            let native = bytes.starts_with(b"\x7fELF")
                || [
                    [0xcf, 0xfa, 0xed, 0xfe],
                    [0xce, 0xfa, 0xed, 0xfe],
                    [0xca, 0xfe, 0xba, 0xbe],
                ]
                .iter()
                .any(|m| bytes.starts_with(m));
            if !native {
                return Err(format!("ARTIFACT_NATIVE_BINARY_REQUIRED:{p}"));
            }
            if bytes.windows(9).any(|w| w == b"libpython") {
                return Err(format!("PYTHON_LINKAGE_IN_ARTIFACT:{p}"));
            }
        }
    }
    Ok(
        json!({"status":"PASS","members":expected.len(),"scope":"RUST_STAGE_ARTIFACT_NOT_DEPLOYMENT"}),
    )
}
