use super::*;
pub const COMMIT: &str = "0f870e50a973fa820d4c409000059e181e8d242b";
pub const SOURCE_SHA: &str = "0281b17c0979f9db1ae2b41b0b66df697d646e726995756a003c6ffc51a64c10";
pub const GO_SHA: &str = "65efbc80fdc1fa2cd2631cad5b481777a68e8725f34e89f50fc6ac714e379b40";
pub const PREFIX: &str = "github.com/openai/tunnel-client/";
pub const ENTRYPOINT: &str = "./cmd/client-runtime";
pub const REQUIRED: &[&str] = &[
    "cmd/client-runtime",
    "pkg/runtimeapp",
    "pkg/runtimehealth",
    "pkg/mcpclient",
    "pkg/controlplane",
    "pkg/metrics",
];
pub const FORBIDDEN: &[&str] = &[
    "cmd/client",
    "pkg/app",
    "pkg/adminui",
    "pkg/codexappserver",
    "pkg/codexplugin",
    "pkg/cloudflared",
];
pub fn dependencies(text: &str) -> Result<()> {
    let packages: BTreeSet<_> = text.lines().collect();
    if REQUIRED
        .iter()
        .any(|p| !packages.contains(format!("{PREFIX}{p}").as_str()))
    {
        return Err("TRANSPORT_DEPENDENCY_MISSING".into());
    }
    if packages.iter().any(|p| {
        FORBIDDEN
            .iter()
            .any(|f| *p == format!("{PREFIX}{f}") || p.starts_with(&format!("{PREFIX}{f}/")))
    }) {
        return Err("EMBEDDED_EXECUTOR_OR_FULL_CLIENT_LINKED".into());
    }
    Ok(())
}
// Only immutable module acquisition may retry. Each Go attempt retains its
// private cache and verifies downloads against the unchanged upstream go.sum.
// Compiler/test failures and integrity errors never enter this retry path.
pub fn acquire(
    command: &mut Command,
    output: &Path,
    name: &str,
    budget: Duration,
) -> Result<String> {
    let deadline = Instant::now() + budget;
    let mut attempts = vec![];
    for attempt in 1..=3 {
        let remaining = deadline.saturating_duration_since(Instant::now()).as_secs();
        if remaining == 0 {
            return Err("BUILD_COMMAND_TIMEOUT".into());
        }
        let stdout = output.join(format!("{name}-attempt-{attempt}.stdout.log"));
        let stderr = output.join(format!("{name}-attempt-{attempt}.stderr.log"));
        let result = run_logged(command, remaining, &stdout, &stderr);
        let retry = result.as_ref().err().is_some_and(|error| {
            error.starts_with("BUILD_COMMAND_FAILED:")
                && !error.contains("checksum mismatch")
                && !error.contains("SECURITY ERROR")
                && (error.contains("unexpected EOF") || error.contains("connection reset by peer"))
        });
        attempts.push(json!({"attempt":attempt,"status":if result.is_ok(){"PASS"}else{"FAILED"},"transient_network_error":retry,"stdout":stdout.file_name().unwrap().to_string_lossy(),"stderr":stderr.file_name().unwrap().to_string_lossy()}));
        save(
            &output.join(format!("{name}-attempts.json")),
            &json!(attempts),
        )?;
        match result {
            Ok((out, err)) => {
                write(&output.join(format!("{name}.txt")), &out, 0o644)?;
                write(&output.join(format!("{name}.stderr.log")), &err, 0o644)?;
                return String::from_utf8(out).map_err(|e| e.to_string());
            }
            Err(_) if retry && attempt < 3 => {
                let pause =
                    Duration::from_secs(1).min(deadline.saturating_duration_since(Instant::now()));
                std::thread::sleep(pause);
                // Retain the exact command, module versions, proxy and sumdb.
            }
            Err(error) => return Err(error),
        }
    }
    unreachable!()
}
pub fn qualified(path: &Path) -> Result<PathBuf> {
    let m = value(path)?;
    if m["schema"] != "CLINX_TRANSPORT_RUNTIME_CANDIDATE_V1"
        || m["status"] != "BUILT_OFFLINE_NOT_DEPLOYED"
        || m["upstream_commit"] != COMMIT
        || m["upstream_archive_sha256"] != SOURCE_SHA
        || m["go_archive_sha256"] != GO_SHA
        || m["entrypoint"] != ENTRYPOINT
        || !m["upstream_patch"].is_null()
        || m["embedded_codex"] != "NOT_LINKED"
        || m["reproducible_across_source_paths"] != true
        || m["transient_removed"] != true
    {
        return Err("UNQUALIFIED_TRANSPORT_RUNTIME".into());
    }
    let parent = path.parent().ok_or("INVALID_MANIFEST_PATH")?;
    let binary = parent.join("tunnel-client-runtime");
    let deps = parent.join("dependencies.txt");
    let builder = parent.join(if m["builder_language"] == "RUST" {
        "builder-source.rs"
    } else {
        "builder-source.py"
    });
    for (p, h) in [
        (&binary, &m["binary"]["sha256"]),
        (&deps, &m["dependencies_sha256"]),
        (&builder, &m["builder_sha256"]),
        (
            &parent.join("upstream-source-files.json"),
            &m["source_files_manifest_sha256"],
        ),
    ] {
        if digest(p)? != h.as_str().ok_or("INVALID_DIGEST")? {
            return Err("TRANSPORT_RUNTIME_ARTIFACT_DRIFT".into());
        }
    }
    if m["builder_language"] == "RUST"
        && digest(&parent.join("builder-shared-sources.json"))?
            != m["builder_shared_sources_sha256"]
                .as_str()
                .ok_or("INVALID_DIGEST")?
    {
        return Err("TRANSPORT_RUNTIME_ARTIFACT_DRIFT".into());
    }
    dependencies(&String::from_utf8(read(&deps)?).map_err(|e| e.to_string())?)?;
    Ok(binary)
}
pub fn build(source_archive: &Path, go_archive: &Path, output: &Path) -> Result<Value> {
    let output = task_output(output, "NEW_TASK_ARTIFACT_DIRECTORY_REQUIRED")?;
    fs::create_dir_all(&output).map_err(|e| e.to_string())?;
    fs::set_permissions(&output, fs::Permissions::from_mode(0o700)).map_err(|e| e.to_string())?;
    write(
        &output.join("builder-source.rs"),
        include_bytes!("build_transport.rs"),
        0o644,
    )?;
    save(
        &output.join("builder-shared-sources.json"),
        &json!({"build_release.rs":sha(include_bytes!("build_release.rs")),"build_archive.rs":sha(include_bytes!("build_archive.rs"))}),
    )?;
    let transient = output.join("transient");
    fs::create_dir(&transient).map_err(|e| e.to_string())?;
    let outcome = (|| -> Result<Value> {
        archive::extract(source_archive, &transient, SOURCE_SHA)?;
        archive::extract(go_archive, &transient, GO_SHA)?;
        let source = transient.join(format!("tunnel-client-{COMMIT}"));
        let go = transient.join("go/bin/go");
        for name in ["home", "tmp"] {
            fs::create_dir(transient.join(name)).map_err(|e| e.to_string())?;
        }
        let mut envs: BTreeMap<String, String> = [
            (
                "PATH",
                format!("{}:/usr/bin:/bin", go.parent().unwrap().display()),
            ),
            ("HOME", transient.join("home").display().to_string()),
            ("TMPDIR", transient.join("tmp").display().to_string()),
            ("GOTOOLCHAIN", "local".into()),
            ("CGO_ENABLED", "0".into()),
            ("GOOS", "linux".into()),
            ("GOARCH", "amd64".into()),
            ("GOAMD64", "v1".into()),
            ("GOMAXPROCS", "2".into()),
            (
                "GOMODCACHE",
                transient.join("modcache").display().to_string(),
            ),
            (
                "GOCACHE",
                transient.join("buildcache").display().to_string(),
            ),
            ("GOPROXY", "https://proxy.golang.org".into()),
            ("GOSUMDB", "sum.golang.org".into()),
        ]
        .into_iter()
        .map(|(k, v)| (k.into(), v))
        .collect();
        for key in [
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
        ] {
            if let Ok(v) = std::env::var(key) {
                envs.insert(key.into(), v);
            }
        }
        let execute = |name: &str, args: Vec<String>, cwd: &Path| -> Result<String> {
            let mut command = Command::new(&go);
            command.args(&args).current_dir(cwd).env_clear().envs(&envs);
            if matches!(name, "dependencies" | "test-dependencies") {
                return acquire(&mut command, &output, name, Duration::from_secs(900));
            }
            if matches!(name, "upstream-tests" | "build-a" | "build-b") {
                command.env("GOPROXY", "off");
            }
            match run_logged(
                &mut command,
                300,
                &output.join(format!("{name}.txt")),
                &output.join(format!("{name}.stderr.log")),
            ) {
                Ok((out, _)) => String::from_utf8(out).map_err(|e| e.to_string()),
                Err(e) => {
                    save(
                        &output.join(format!("{name}.failure.json")),
                        &json!({"error":e}),
                    )?;
                    Err(e)
                }
            }
        };
        let version = execute("go-version", vec!["version".into()], &source)?
            .trim()
            .to_owned();
        if version != "go version go1.27.3 linux/amd64" {
            return Err("UNEXPECTED_TOOLCHAIN".into());
        }
        let files: BTreeMap<_, _> = walk(&source)?
            .into_iter()
            .map(|p| Ok((p.display().to_string(), digest(&source.join(p))?)))
            .collect::<Result<_>>()?;
        save(&output.join("upstream-source-files.json"), &json!(files))?;
        let deps = execute(
            "dependencies",
            ["list", "-mod=readonly", "-deps", ENTRYPOINT]
                .into_iter()
                .map(str::to_owned)
                .collect(),
            &source,
        )?;
        dependencies(&deps)?;
        execute(
            "test-dependencies",
            [
                "list",
                "-mod=readonly",
                "-deps",
                "-test",
                "./cmd/client-runtime",
                "./pkg/runtimeapp",
                "./pkg/runtimehealth",
                "./pkg/runtimeconfig",
            ]
            .into_iter()
            .map(str::to_owned)
            .collect(),
            &source,
        )?;
        execute(
            "module-cache-verify",
            vec!["mod".into(), "verify".into()],
            &source,
        )?;
        execute(
            "upstream-tests",
            [
                "test",
                "-mod=readonly",
                "-count=1",
                "-timeout=90s",
                "./cmd/client-runtime",
                "./pkg/runtimeapp",
                "./pkg/runtimehealth",
                "./pkg/runtimeconfig",
            ]
            .into_iter()
            .map(str::to_owned)
            .collect(),
            &source,
        )?;
        let flags = vec![
            "-trimpath".to_owned(),
            "-buildvcs=false".into(),
            "-mod=readonly".into(),
            "-ldflags".into(),
            format!("-X {PREFIX}pkg/version.GitSHA={COMMIT} -X {PREFIX}pkg/version.Flavor=runtime"),
        ];
        let binary = output.join("tunnel-client-runtime");
        let build_args = |target: &Path| {
            let mut a = vec!["build".into()];
            a.extend(flags.clone());
            a.extend(["-o".into(), target.display().to_string(), ENTRYPOINT.into()]);
            a
        };
        execute("build-a", build_args(&binary), &source)?;
        let second = transient.join("second-source");
        for name in files.keys() {
            let p = source.join(name);
            write(&second.join(name), &read(&p)?, regular(&p)?.mode() & 0o777)?;
        }
        let b = transient.join("binary-b");
        execute("build-b", build_args(&b), &second)?;
        if digest(&binary)? != digest(&b)? {
            return Err("REPRODUCIBLE_BUILD_MISMATCH".into());
        }
        execute(
            "module-cache-verify-after",
            vec!["mod".into(), "verify".into()],
            &source,
        )?;
        for (name, hash) in &files {
            if digest(&source.join(name))? != *hash {
                return Err("UPSTREAM_SOURCE_MODIFIED".into());
            }
        }
        execute(
            "binary-build-info",
            vec!["version".into(), "-m".into(), binary.display().to_string()],
            &source,
        )?;
        Ok(
            json!({"schema":"CLINX_TRANSPORT_RUNTIME_CANDIDATE_V1","status":"BUILT_OFFLINE_NOT_DEPLOYED","upstream_commit":COMMIT,"upstream_archive_sha256":SOURCE_SHA,"upstream_patch":null,"go_archive_sha256":GO_SHA,"go_version":version,"entrypoint":ENTRYPOINT,"build_flags":flags,"binary":{"path":binary,"sha256":digest(&binary)?},"dependencies_sha256":digest(&output.join("dependencies.txt"))?,"source_files_manifest_sha256":digest(&output.join("upstream-source-files.json"))?,"builder_sha256":digest(&output.join("builder-source.rs"))?,"builder_language":"RUST","builder_shared_sources_sha256":digest(&output.join("builder-shared-sources.json"))?,"reproducible_across_source_paths":true,"embedded_codex":"NOT_LINKED","deployed":false,"production_acceptance":"NOT_RUN","activation_authority":"NOT_GRANTED_FOR_THIS_ARTIFACT"}),
        )
    })();
    let cleanup = archive::remove_transient(&transient);
    let mut receipt = outcome
        .clone()
        .unwrap_or_else(|e| json!({"status":"BUILD_FAILED","deployed":false,"error":e}));
    receipt["transient_removed"] = json!(!transient.exists());
    save(&output.join("manifest.json"), &receipt)?;
    cleanup?;
    outcome.map(|_| receipt)
}
pub fn cli(args: &[String]) -> Result<Value> {
    let o = Options::parse(args, &["--source-archive", "--go-archive", "--output"])?;
    build(
        &o.path("--source-archive")?,
        &o.path("--go-archive")?,
        &o.path("--output")?,
    )
}
