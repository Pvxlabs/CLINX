use super::*;
pub type PreparedChange = (PathBuf, Value, Option<Vec<u8>>);
pub fn rooted(root: &Path, name: &str) -> Result<PathBuf> {
    let name = Path::new(name);
    if !name.is_absolute() || name.components().any(|c| matches!(c, Component::ParentDir)) {
        return Err("INVALID_ABSOLUTE_PATH".into());
    }
    let target = root.join(name.strip_prefix("/").unwrap());
    for p in target.ancestors() {
        if p == root {
            break;
        }
        if fs::symlink_metadata(p).is_ok_and(|m| m.file_type().is_symlink()) {
            return Err("SYMLINK_REFUSED".into());
        }
    }
    Ok(target)
}
pub fn fixture_root(root: &Path) -> Result<PathBuf> {
    let root = std::path::absolute(root).map_err(|e| e.to_string())?;
    if root == Path::new("/")
        || !root.starts_with("/data/artifacts")
        || root.components().any(|c| matches!(c, Component::ParentDir))
    {
        return Err("OFFLINE_FIXTURE_ROOT_REQUIRED".into());
    }
    for p in root.ancestors() {
        if fs::symlink_metadata(p).is_ok_and(|m| m.file_type().is_symlink()) {
            return Err("FIXTURE_SYMLINK_REFUSED".into());
        }
    }
    let marker = root.join(".clinx-p1-fixture");
    if regular(&marker).is_err()
        || read(&marker)? != b"ISOLATED_CONFIG_ONLY\n"
        || fs::metadata(&root).map_err(|e| e.to_string())?.uid()
            != rustix::process::getuid().as_raw()
    {
        return Err("FIXTURE_OWNERSHIP_MARKER_REQUIRED".into());
    }
    Ok(root)
}
pub fn check(
    plan: &Value,
    capsule: &Path,
    root: &Path,
    direction: &str,
    guard_root: &Path,
) -> Result<Vec<PreparedChange>> {
    let (before, after) = match direction {
        "forward" => ("before", "after"),
        "rollback" => ("after", "before"),
        _ => return Err("CONFIG_ONLY_SCOPE_REQUIRED".into()),
    };
    let home = plan["home"].as_str().ok_or("CONFIG_ONLY_SCOPE_REQUIRED")?;
    let exact: BTreeSet<_> = [
        "clinx-tunnel.service",
        "clinx-tunnel-health.service",
        "clinx-tunnel.service.d/99-p1-reliability.conf",
    ]
    .iter()
    .map(|n| format!("{home}/.config/systemd/user/{n}"))
    .collect();
    let allowed: BTreeSet<_> = plan["managed_paths"]
        .as_array()
        .ok_or("CONFIG_ONLY_SCOPE_REQUIRED")?
        .iter()
        .map(|v| v.as_str().unwrap_or("").to_owned())
        .collect();
    if allowed != exact {
        return Err("CONFIG_ONLY_SCOPE_REQUIRED".into());
    }
    let changes = plan["changes"].as_object().ok_or("CHANGE_SET_MISMATCH")?;
    if changes.keys().cloned().collect::<BTreeSet<_>>() != allowed {
        return Err("CHANGE_SET_MISMATCH".into());
    }
    for (dir, names) in plan["directory_entries"]
        .as_object()
        .ok_or("INVALID_PLAN")?
    {
        let target = rooted(root, dir)?;
        let actual: BTreeSet<_> = if target.exists() {
            fs::read_dir(target)
                .map_err(|e| e.to_string())?
                .map(|e| {
                    e.map(|v| v.file_name().to_string_lossy().into_owned())
                        .map_err(|e| e.to_string())
                })
                .collect::<Result<_>>()?
        } else {
            BTreeSet::new()
        };
        let mut wanted: BTreeSet<_> = names
            .as_array()
            .ok_or("INVALID_PLAN")?
            .iter()
            .map(|v| v.as_str().unwrap_or("").to_owned())
            .collect();
        for name in &allowed {
            if Path::new(name).parent() == Some(Path::new(dir)) {
                let leaf = Path::new(name)
                    .file_name()
                    .unwrap()
                    .to_string_lossy()
                    .into_owned();
                if changes[name][before].is_null() {
                    wanted.remove(&leaf);
                } else {
                    wanted.insert(leaf);
                }
            }
        }
        if actual != wanted {
            return Err(format!("DIRECTORY_DRIFT:{dir}"));
        }
    }
    for (name, guard) in plan["guards"].as_object().ok_or("INVALID_PLAN")? {
        if allowed.contains(name) {
            return Err("GUARD_CHANGE_OVERLAP".into());
        }
        if state(&rooted(guard_root, name)?)? != *guard {
            return Err(format!("DEPENDENCY_DRIFT:{name}"));
        }
    }
    let mut result = vec![];
    for name in allowed {
        let path = rooted(root, &name)?;
        if state(&path)? != changes[&name][before] {
            return Err(format!("STALE_STATE:{name}"));
        }
        let desired = changes[&name][after].clone();
        let bytes = if desired.is_null() {
            None
        } else {
            let mode = desired["mode"].as_u64().ok_or("INVALID_MODE")?;
            if mode > 0o7777 {
                return Err("INVALID_MODE".into());
            }
            let digest = desired["sha256"].as_str().ok_or("INVALID_BLOB_DIGEST")?;
            if digest.len() != 64
                || !digest
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            {
                return Err("INVALID_BLOB_DIGEST".into());
            }
            let bytes = read(&capsule.join("blobs").join(digest))?;
            if sha(&bytes) != digest {
                return Err("BLOB_HASH_MISMATCH".into());
            }
            Some(bytes)
        };
        result.push((path, desired, bytes));
    }
    Ok(result)
}
pub fn apply(
    plan: &Value,
    capsule: &Path,
    root: &Path,
    direction: &str,
    guard_root: &Path,
) -> Result<Value> {
    let root = fixture_root(root)?;
    let changes = check(plan, capsule, &root, direction, guard_root)?;
    for (path, state, bytes) in changes {
        let parent = path.parent().ok_or("INVALID_PATH")?;
        if let Some(bytes) = bytes {
            fs::create_dir_all(parent).map_err(|e| e.to_string())?;
            let mut tmp = tempfile::NamedTempFile::new_in(parent).map_err(|e| e.to_string())?;
            tmp.as_file()
                .set_permissions(fs::Permissions::from_mode(
                    state["mode"].as_u64().ok_or("INVALID_MODE")? as u32,
                ))
                .map_err(|e| e.to_string())?;
            tmp.write_all(&bytes)
                .and_then(|_| tmp.as_file().sync_all())
                .map_err(|e| e.to_string())?;
            tmp.persist(&path).map_err(|e| e.to_string())?;
        } else {
            fs::remove_file(&path).map_err(|e| e.to_string())?
        }
        fs::File::open(parent)
            .and_then(|f| f.sync_all())
            .map_err(|e| e.to_string())?;
    }
    check(
        plan,
        capsule,
        &root,
        if direction == "forward" {
            "rollback"
        } else {
            "forward"
        },
        guard_root,
    )?;
    Ok(json!({"status":"PASS","direction":direction,"scope":"OFFLINE_CONFIG_ONLY"}))
}
pub fn cli(args: &[String]) -> Result<Value> {
    let (action, rest) = args.split_first().ok_or("CAS_ACTION_REQUIRED")?;
    let o = Options::parse(
        rest,
        &["--capsule", "--root", "--guard-root", "--direction"],
    )?;
    let root = o.path("--root")?;
    let guard = Path::new(o.get("--guard-root").unwrap_or(o.required("--root")?));
    let capsule = o.path("--capsule")?;
    let plan = value(&capsule.join("plan.json"))?;
    let dir = o.required("--direction")?;
    match action.as_str() {
        "check" => {
            check(&plan, &capsule, &root, dir, guard)?;
            Ok(json!({"status":"PASS","scope":"READ_ONLY_CAS"}))
        }
        "fixture-apply" => apply(&plan, &capsule, &root, dir, guard),
        _ => Err("CAS_ACTION_INVALID".into()),
    }
}
