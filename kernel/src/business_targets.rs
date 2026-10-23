//! Task-owned Git targets. Identity and authorization are checked by the writer,
//! including immediately before the one-use Host execution permit.
use super::*;
use std::os::unix::fs::MetadataExt;

const FIELDS: &[&str] = &[
    "path",
    "common_dir",
    "git_dir",
    "git_dir_device",
    "git_dir_inode",
    "branch",
    "origin",
];

fn inspect(policy: &Policy, task: &Value, path: &str) -> Result<Value> {
    let candidate = Path::new(path);
    let parent = string(&task["workspace_contract"], "cwd")?;
    if !candidate.is_absolute()
        || candidate
            .canonicalize()
            .map_err(|_| "GIT_TARGET_UNAVAILABLE")?
            != candidate
        || path == parent
        || !policy.derived_git_roots.iter().any(|root| {
            Path::new(root)
                .canonicalize()
                .is_ok_and(|r| candidate.starts_with(r))
        })
    {
        return Err("GIT_TARGET_ROOT_DENIED".into());
    }
    let marker = candidate.join(".git");
    if !marker.is_file()
        || marker.is_symlink()
        || git(path, &["rev-parse", "--show-toplevel"])? != path
    {
        return Err("LINKED_WORKTREE_REQUIRED".into());
    }
    let common = git(
        parent,
        &["rev-parse", "--path-format=absolute", "--git-common-dir"],
    )?;
    if git(
        path,
        &["rev-parse", "--path-format=absolute", "--git-common-dir"],
    )? != common
    {
        return Err("GIT_TARGET_REPOSITORY_CONFLICT".into());
    }
    let dir = git(path, &["rev-parse", "--path-format=absolute", "--git-dir"])?;
    if !Path::new(&dir).starts_with(Path::new(&common).join("worktrees"))
        || !git(parent, &["worktree", "list", "--porcelain", "-z"])?
            .split('\0')
            .any(|s| s == format!("worktree {path}"))
    {
        return Err("LINKED_WORKTREE_REQUIRED".into());
    }
    let origin = git(parent, &["remote", "get-url", "--all", "origin"])?;
    if task["repository_origin"] != origin
        || origin.is_empty()
        || origin.contains('\n')
        || git(path, &["remote", "get-url", "--all", "origin"])? != origin
        || git(path, &["remote", "get-url", "--push", "--all", "origin"])? != origin
    {
        return Err("GIT_TARGET_ORIGIN_CONFLICT".into());
    }
    let branch = git(path, &["symbolic-ref", "--quiet", "--short", "HEAD"])?;
    git(path, &["check-ref-format", "--branch", &branch])?;
    let remote_head = git(
        path,
        &[
            "for-each-ref",
            "--format=%(symref)",
            "refs/remotes/origin/HEAD",
        ],
    )?;
    let config = git(path, &["config", "--list"])?;
    if ["main", "master"].contains(&branch.as_str())
        || remote_head == format!("refs/remotes/origin/{branch}")
        || config
            .lines()
            .any(|l| l.strip_prefix("clinx.protectedbranch=") == Some(&branch))
    {
        return Err("GIT_TARGET_PROTECTED_BRANCH".into());
    }
    for marker in [
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "REBASE_HEAD",
        "rebase-merge",
        "rebase-apply",
    ] {
        if Path::new(&git(
            path,
            &["rev-parse", "--path-format=absolute", "--git-path", marker],
        )?)
        .exists()
        {
            return Err("GIT_TARGET_UNRESOLVED_OPERATION".into());
        }
    }
    let metadata = Path::new(&dir)
        .metadata()
        .map_err(|_| "GIT_TARGET_UNAVAILABLE")?;
    Ok(
        json!({"path":path,"common_dir":common,"git_dir":dir,"git_dir_device":metadata.dev(),
        "git_dir_inode":metadata.ino(),"branch":branch,"origin":origin}),
    )
}

pub(super) fn resolve(policy: &Policy, task: &Value, id: &str) -> Result<Value> {
    let target = task["derived_git_targets"]
        .get(id)
        .ok_or("GIT_TARGET_NOT_REGISTERED")?;
    if !target["revoked_at"].is_null() || task["lifecycle"] == "CLOSED" {
        return Err("GIT_TARGET_REVOKED".into());
    }
    let current = inspect(policy, task, string(target, "path")?)?;
    if FIELDS.iter().any(|k| target[k] != current[k]) {
        return Err("GIT_TARGET_IDENTITY_CHANGED".into());
    }
    Ok(target.clone())
}

pub(super) fn validate(
    policy: &Policy,
    task: Option<&Value>,
    execution_policy: &Value,
) -> Result<()> {
    validate_policy(policy, execution_policy)?;
    for scope in execution_policy["operation_scopes"]
        .as_array()
        .into_iter()
        .flatten()
    {
        if scope["target"]
            .as_str()
            .is_some_and(|s| s.starts_with("gitwt_"))
        {
            resolve(
                policy,
                task.ok_or("TASK_OWNED_GIT_TARGET_REQUIRED")?,
                string(scope, "target")?,
            )?;
        }
    }
    Ok(())
}

pub(super) fn template(scope: &Value) -> Value {
    let mut template = scope.clone();
    if scope["capability"] == "GIT"
        && scope["operation"] == "push_current_branch"
        && scope["operation_class"] == "DEVELOPMENT_MUTATION"
        && scope["target"]
            .as_str()
            .is_some_and(|s| s.starts_with("gitwt_"))
    {
        template["target"] = json!("$TASK_DERIVED_GIT");
    }
    template
}

pub(super) fn command(
    c: &Connection,
    policy: &Policy,
    op: &str,
    p: &Value,
    changes: &mut Vec<Value>,
) -> Result<Value> {
    let task_id = string(p, "task_ref")?;
    let mut task = row(c, "task", task_id)?;
    if op == "resolve_git_target" {
        if !core::keys(p, "task_ref target") {
            return Err("INVALID_GIT_TARGET_REQUEST".into());
        }
        return resolve(policy, &task, string(p, "target")?);
    }
    if p["approved"] != true {
        return Err("GIT_TARGET_APPROVAL_REQUIRED".into());
    }
    if op == "revoke_git_target" {
        if !core::keys(p, "task_ref target approved") {
            return Err("INVALID_GIT_TARGET_REQUEST".into());
        }
        let id = string(p, "target")?;
        let target = task["derived_git_targets"]
            .get_mut(id)
            .ok_or("GIT_TARGET_NOT_REGISTERED")?;
        if target["revoked_at"].is_null() {
            target["revoked_at"] = json!(utc());
        }
        put(c, "task", task_id, &task, changes)?;
        return Ok(json!({"task_ref":task_id,"target":id,"revoked":true}));
    }
    if !core::keys(
        p,
        "task_ref path ownership_evidence creation_host_execution_ref approved",
    ) || task["lifecycle"] == "CLOSED"
        || string(p, "ownership_evidence")?.len() > 2000
    {
        return Err("INVALID_GIT_TARGET_REQUEST".into());
    }
    let mut target = inspect(policy, &task, string(p, "path")?)?;
    let mut provenance =
        json!({"mode":"EXPLICIT_OPERATOR_ADOPTION","operator_evidence":p["ownership_evidence"]});
    if !p["creation_host_execution_ref"].is_null() {
        let executions = rows(c, "execution")?;
        let delivery = executions
            .iter()
            .filter(|e| e["task_ref"] == task_id)
            .flat_map(|e| {
                e["host_deliveries"]
                    .as_object()
                    .into_iter()
                    .flat_map(|m| m.values())
            })
            .find(|d| d["host_ref"] == p["creation_host_execution_ref"])
            .ok_or("GIT_CREATION_RECEIPT_INVALID")?;
        let ev = &delivery["start_evidence"];
        let argv = ev["argv"]
            .as_array()
            .ok_or("GIT_CREATION_RECEIPT_INVALID")?;
        let mut args = argv.as_slice();
        if args.first() != Some(&json!("git")) {
            return Err("GIT_CREATION_RECEIPT_INVALID".into());
        }
        args = &args[1..];
        if args.first() == Some(&json!("-C")) {
            if args.get(1) != Some(&task["workspace_contract"]["cwd"]) {
                return Err("GIT_CREATION_RECEIPT_INVALID".into());
            }
            args = &args[2..];
        }
        if delivery["host_completion"] != "KNOWN"
            || delivery["host_exit_code"] != 0
            || ev["capability"] != "GIT"
            || ev["operation"] != "development_command"
            || args.len() < 5
            || args[..3] != [json!("worktree"), json!("add"), json!("-b")]
            || args[3] != target["branch"]
            || args[4] != target["path"]
        {
            return Err("GIT_CREATION_RECEIPT_INVALID".into());
        }
        provenance["mode"] = json!("TASK_HOST_EXECUTION");
        provenance["host_execution_ref"] = p["creation_host_execution_ref"].clone();
    }
    for other in rows(c, "task")? {
        if other["id"] != task_id && other["workspace_contract"]["cwd"] == target["path"] {
            return Err("GIT_TARGET_OTHER_TASK".into());
        }
        for old in other["derived_git_targets"]
            .as_object()
            .into_iter()
            .flat_map(|m| m.values())
        {
            if old["path"] == target["path"]
                || old["git_dir"] == target["git_dir"] && old["revoked_at"].is_null()
            {
                if other["id"] != task_id {
                    return Err("GIT_TARGET_OTHER_TASK".into());
                }
                if old["revoked_at"].is_null() {
                    if FIELDS.iter().any(|k| old[k] != target[k]) {
                        return Err("GIT_TARGET_IDENTITY_CHANGED".into());
                    }
                    return Ok(old.clone());
                }
            }
        }
    }
    let id = format!("gitwt_{}", &random_id()?[..32]);
    use sha2::{Digest, Sha256};
    let key = format!(
        "{}\u{1f}{}\u{1f}{}",
        string(&task, "host")?.to_lowercase(),
        string(&target, "path")?,
        string(&target, "origin")?.to_lowercase()
    );
    target["worktree_key"] = json!(format!("{:x}", Sha256::digest(key.as_bytes())));
    target["target_id"] = json!(id);
    target["task_id"] = json!(task_id);
    target["registered_at"] = json!(utc());
    target["revoked_at"] = Value::Null;
    target["provenance"] = json!(core::canonical(&provenance));
    if task["derived_git_targets"].is_null() {
        task["derived_git_targets"] = json!({});
    }
    task["derived_git_targets"][&id] = target.clone();
    task["repository_origin"] = target["origin"].clone();
    put(c, "task", task_id, &task, changes)?;
    Ok(target)
}
