use super::*;
pub const SOURCES: &[&str] = &[
    "kernel/src/tunnel_supervisor.rs",
    "kernel/src/tunnel_runtime.rs",
    "kernel/src/bin/clinx-tunnel.rs",
    "kernel/src/build_tunnel.rs",
    "kernel/src/build_cas.rs",
    "kernel/src/build_archive.rs",
    "systemd/clinx-tunnel-reliability.service.in",
    "systemd/clinx-tunnel-health-reliability.service.in",
    "systemd/clinx-tunnel-reliability-final.conf",
    "systemd/tunnel-reliability-policy.json",
];
fn hash_manifest(path: Option<&str>, error: &str) -> Result<Value> {
    let values = if let Some(path) = path {
        value(Path::new(path))?
    } else {
        json!({})
    };
    for (name, hash) in values.as_object().ok_or("INVALID_HASH_MANIFEST")? {
        if digest(Path::new(name))? != hash.as_str().ok_or("INVALID_DIGEST")? {
            return Err(format!("{error}:{name}"));
        }
    }
    Ok(values)
}
fn safe_token(v: &str) -> Result<()> {
    if !v.starts_with('/')
        || v.chars()
            .any(|c| c.is_whitespace() || "%@$;\\\"'".contains(c))
    {
        return Err("UNSUPPORTED_PATH".into());
    }
    Ok(())
}
pub fn build(o: &Options) -> Result<Value> {
    let output = task_output(&o.path("--output")?, "NEW_TASK_ARTIFACT_OUTPUT_REQUIRED")?;
    let source = fs::canonicalize(o.required("--source")?).map_err(|e| e.to_string())?;
    if o.get("--control-overlay-base").is_some() {
        return Err("LEGACY_CONTROL_OVERLAY_RETIRED_USE_STAGE_RUST_RELEASE".into());
    }
    let home = o.required("--home")?;
    let python = o.get("--python");
    let tunnel = o.required("--tunnel")?;
    for v in [Some(home), python, Some(tunnel)].into_iter().flatten() {
        safe_token(v)?;
    }
    let guards = hash_manifest(o.get("--guard-manifest"), "INSTALLED_GUARD_DRIFT")?;
    let evidence = hash_manifest(o.get("--evidence-manifest"), "REUSED_EVIDENCE_DRIFT")?;
    let policy = value(&source.join("systemd/tunnel-reliability-policy.json"))?;
    let (head, _) = run(
        Command::new("git")
            .arg("-C")
            .arg(&source)
            .args(["rev-parse", "HEAD"]),
        5,
    )?;
    let hashes: BTreeMap<_, _> = SOURCES
        .iter()
        .map(|p| Ok((*p, digest(&source.join(p))?)))
        .collect::<Result<_>>()?;
    let mut inputs = json!({"source_head":String::from_utf8_lossy(&head).trim(),"source_sha256":hashes,"tunnel":{"path":tunnel,"sha256":digest(Path::new(tunnel))?,"mode":regular(Path::new(tunnel))?.mode()&0o7777},"home":home,"reused_evidence_sha256":evidence});
    if let Some(py) = python {
        let mut identity = state(Path::new(py))?;
        identity["path"] = json!(py);
        inputs["python"] = identity;
    }
    let supervisor = o
        .get("--supervisor-binary")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("CLINX_TUNNEL_BINARY").map(PathBuf::from))
        .or_else(|| {
            o.get("--node-binary")
                .map(|p| Path::new(p).with_file_name("clinx-tunnel"))
        })
        .or_else(|| {
            std::env::var_os("CLINX_NODE_BINARY")
                .map(|p| Path::new(&p).with_file_name("clinx-tunnel"))
        })
        .ok_or("QUALIFIED_RUST_TUNNEL_SUPERVISOR_REQUIRED")?;
    let supervisor = fs::canonicalize(supervisor).map_err(|e| e.to_string())?;
    let supervisor_bytes = read(&supervisor)?;
    marker(
        &supervisor,
        "--supervisor-contract",
        "RUST_TUNNEL_SUPERVISOR_V1",
        "QUALIFIED_RUST_TUNNEL_SUPERVISOR_REQUIRED",
    )?;
    if read(&supervisor)? != supervisor_bytes {
        return Err("RUST_TUNNEL_SUPERVISOR_DRIFT".into());
    }
    inputs["supervisor_runtime"] =
        json!({"contract":"RUST_TUNNEL_SUPERVISOR_V1","sha256":sha(&supervisor_bytes)});
    let cas_binary = o
        .get("--cas-binary")
        .map(PathBuf::from)
        .unwrap_or(std::env::current_exe().map_err(|e| e.to_string())?);
    marker(
        &cas_binary,
        "--build-contract",
        "RUST_RELEASE_BUILD_V1",
        "QUALIFIED_RUST_BUILD_TOOL_REQUIRED",
    )?;
    let cas_bytes = read(&cas_binary)?;
    inputs["offline_cas_tool"] =
        json!({"contract":"RUST_RELEASE_BUILD_V1","sha256":sha(&cas_bytes)});
    let runtime = if let Some(manifest) = o.get("--transport-runtime-manifest") {
        let p = Path::new(manifest);
        let b = transport::qualified(p)?;
        inputs["transport_runtime"] = json!({"manifest_sha256":digest(p)?,"binary_sha256":digest(&b)?,"builder_sha256":digest(&source.join("kernel/src/build_transport.rs"))?});
        Some((p.to_path_buf(), b))
    } else {
        None
    };
    let release_id = format!("tunnel-reliability-p1-{}", &sha(encoded(&inputs))[..20]);
    let install = format!("{home}/.local/lib/clinx-control/releases/{release_id}");
    let policy_args = policy["policy"]
        .as_object()
        .ok_or("INVALID_POLICY")?
        .iter()
        .map(|(k, v)| {
            format!(
                "--{} {}",
                k.replace('_', "-"),
                v.as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| v.to_string())
            )
        })
        .collect::<Vec<_>>()
        .join(" ");
    let substitutions = BTreeMap::from([
        ("@HOME@", home.to_owned()),
        ("@RELEASE@", install.clone()),
        (
            "@TUNNEL@",
            if runtime.is_some() {
                format!("{install}/tunnel-client-runtime")
            } else {
                tunnel.into()
            },
        ),
        (
            "@CODEX_PATH@",
            format!("{home}/.nvm/versions/node/v24.19.0/bin:/usr/local/bin:/usr/bin:/bin"),
        ),
        ("@POLICY_ARGS@", policy_args),
    ]);
    let mut contents = BTreeMap::from([
        ("policy.json".to_owned(), encoded(&policy)),
        (
            "units/99-p1-reliability.conf".into(),
            read(&source.join("systemd/clinx-tunnel-reliability-final.conf"))?,
        ),
        ("clinx-tunnel".into(), supervisor_bytes),
    ]);
    let mut modes = BTreeMap::from([("clinx-tunnel".to_owned(), 0o755)]);
    if let Some((manifest, binary)) = &runtime {
        contents.insert("tunnel-client-runtime".into(), read(binary)?);
        contents.insert("transport-runtime-manifest.json".into(), read(manifest)?);
        contents.insert(
            "transport-runtime-dependencies.txt".into(),
            read(&binary.parent().unwrap().join("dependencies.txt"))?,
        );
        modes.insert("tunnel-client-runtime".into(), 0o755);
    }
    for (template, target) in [
        (
            "systemd/clinx-tunnel-reliability.service.in",
            "units/clinx-tunnel.service",
        ),
        (
            "systemd/clinx-tunnel-health-reliability.service.in",
            "units/clinx-tunnel-health.service",
        ),
    ] {
        let mut text =
            String::from_utf8(read(&source.join(template))?).map_err(|e| e.to_string())?;
        for (token, replacement) in &substitutions {
            text = text.replace(token, replacement);
        }
        if text.contains('@') {
            return Err("UNRENDERED_TEMPLATE".into());
        }
        contents.insert(target.into(), text.into_bytes());
    }
    fs::create_dir_all(&output).map_err(|e| e.to_string())?;
    fs::set_permissions(&output, fs::Permissions::from_mode(0o700)).map_err(|e| e.to_string())?;
    let release = output.join("release");
    fs::create_dir(&release).map_err(|e| e.to_string())?;
    for (name, bytes) in &contents {
        write(
            &release.join(name),
            bytes,
            *modes.get(name).unwrap_or(&0o644),
        )?;
    }
    let files: BTreeMap<_, _> = contents
        .iter()
        .map(|(n, b)| {
            (
                n,
                json!({"sha256":sha(b),"mode":modes.get(n).copied().unwrap_or(0o644)}),
            )
        })
        .collect();
    let manifest = json!({"schema":"CLINX_P1_RELEASE_V1","release_id":release_id,"install_path":install,"inputs":inputs,"files":files,"status":"NOT_DEPLOYED","production_slo_approval":policy["slo"]["approval"]});
    save(&release.join("manifest.json"), &manifest)?;
    let capsule = output.join("rollback-capsule");
    fs::create_dir_all(capsule.join("blobs")).map_err(|e| e.to_string())?;
    let unit_root = format!("{home}/.config/systemd/user/");
    let mapping = BTreeMap::from([
        (
            format!("{unit_root}clinx-tunnel.service"),
            "units/clinx-tunnel.service",
        ),
        (
            format!("{unit_root}clinx-tunnel-health.service"),
            "units/clinx-tunnel-health.service",
        ),
        (
            format!("{unit_root}clinx-tunnel.service.d/99-p1-reliability.conf"),
            "units/99-p1-reliability.conf",
        ),
    ]);
    let mut changes = serde_json::Map::new();
    for (name, member) in &mapping {
        let before = state(Path::new(name))?;
        if !before.is_null() {
            write(
                &capsule
                    .join("blobs")
                    .join(before["sha256"].as_str().unwrap()),
                &read(Path::new(name))?,
                0o600,
            )?;
        }
        let after = json!({"sha256":sha(&contents[*member]),"mode":0o644});
        write(
            &capsule
                .join("blobs")
                .join(after["sha256"].as_str().unwrap()),
            &contents[*member],
            0o600,
        )?;
        changes.insert(name.clone(), json!({"before":before,"after":after}));
    }
    let mut guarded: BTreeSet<String> = guards
        .as_object()
        .unwrap()
        .keys()
        .cloned()
        .chain(std::iter::once(tunnel.to_owned()))
        .collect();
    if let Some(py) = python {
        guarded.insert(py.into());
    }
    let mut directories = serde_json::Map::new();
    for name in ["clinx-tunnel.service.d", "clinx-tunnel-health.service.d"] {
        let path = PathBuf::from(format!("{unit_root}{name}"));
        let mut names = vec![];
        if path.exists() {
            for entry in fs::read_dir(&path).map_err(|e| e.to_string())? {
                let e = entry.map_err(|e| e.to_string())?;
                names.push(e.file_name().to_string_lossy().into_owned());
                guarded.insert(e.path().display().to_string());
            }
            names.sort();
        }
        directories.insert(path.display().to_string(), json!(names));
    }
    guarded.insert(format!("{unit_root}clinx-tunnel-health.timer"));
    let guard_states: BTreeMap<_, _> = guarded
        .into_iter()
        .filter(|p| !mapping.contains_key(p))
        .map(|p| Ok((p.clone(), state(Path::new(&p))?)))
        .collect::<Result<_>>()?;
    let plan = json!({"schema":"CLINX_P1_CONFIG_CAS_V1","home":home,"release_id":release_id,"release_manifest_sha256":digest(&release.join("manifest.json"))?,"managed_paths":mapping.keys().collect::<Vec<_>>(),"changes":changes,"guards":guard_states,"directory_entries":directories,"production_authorization":"NOT_GRANTED","preconditions":["independently authorized activation","admission drained including this task","no active execution/lease","all transport consumers proven safe to stop","fresh consistency backup","SLO policy approved"],"never_restore":["database","lease","terminal facts","profile","credentials","supervisor budget","Owner","Provider","MCP launcher"]});
    write(&capsule.join("plan.json"), &encoded(&plan), 0o600)?;
    write(&capsule.join("clinx-build"), &cas_bytes, 0o700)?;
    archive::archive(&release, &output.join("release.tar.gz"))?;
    archive::archive(&capsule, &output.join("rollback-capsule.tar.gz"))?;
    let receipt = json!({"release_id":release_id,"install_path":install,"release_tar_sha256":digest(&output.join("release.tar.gz"))?,"release_manifest_sha256":digest(&release.join("manifest.json"))?,"rollback_tar_sha256":digest(&output.join("rollback-capsule.tar.gz"))?,"plan_sha256":digest(&capsule.join("plan.json"))?,"managed_file_count":changes.len(),"guard_count":guard_states.len(),"deployed":false});
    save(&output.join("build-receipt.json"), &receipt)?;
    Ok(receipt)
}
pub fn cli(args: &[String]) -> Result<Value> {
    let mut o = Options::parse(
        args,
        &[
            "--source",
            "--output",
            "--home",
            "--python",
            "--tunnel",
            "--guard-manifest",
            "--evidence-manifest",
            "--transport-runtime-manifest",
            "--node-binary",
            "--supervisor-binary",
            "--cas-binary",
            "--control-overlay-base",
        ],
    )?;
    o.required("--guard-manifest")?;
    o.required("--evidence-manifest")?;
    o.0.entry("--source".into()).or_insert_with(|| {
        vec![
            Path::new(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .display()
                .to_string(),
        ]
    });
    let home = std::env::var("HOME").unwrap_or_default();
    o.0.entry("--home".into())
        .or_insert_with(|| vec![home.clone()]);
    o.0.entry("--tunnel".into())
        .or_insert_with(|| vec![format!("{home}/.local/bin/tunnel-client")]);
    build(&o)
}
