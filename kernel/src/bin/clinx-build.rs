//! Repository build tooling only. Never copied into an App or runtime release.
use serde_json::{Value, json};
use std::{
    fs,
    io::Write,
    os::unix::{ffi::OsStrExt, fs::PermissionsExt},
    path::Path,
    process::Command,
};

#[path = "../build_release.rs"]
mod release_tools;

type Result<T> = std::result::Result<T, String>;
const RETIRED_DIRECTORIES: &[&str] = &[
    "clinx",
    "Discovery",
    "local_discovery",
    "domain",
    "shadow_ledger",
    "runtime_control",
    "provider_adapters",
    "DiscoveryRuntime",
];

fn io<T>(result: std::io::Result<T>, action: &str, path: &Path) -> Result<T> {
    result.map_err(|e| format!("{action}: {}: {e}", path.display()))
}

// The old private interpreter tree used read-only directories. Grant its owner
// traversal/removal access, without following symlinks into unrelated assets.
fn writable_directories(path: &Path) -> Result<()> {
    let metadata = io(fs::symlink_metadata(path), "READ_RUNTIME_METADATA", path)?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Ok(());
    }
    io(
        fs::set_permissions(
            path,
            fs::Permissions::from_mode(metadata.permissions().mode() | 0o700),
        ),
        "MAKE_OLD_RUNTIME_REMOVABLE",
        path,
    )?;
    for entry in io(fs::read_dir(path), "READ_OLD_RUNTIME", path)? {
        let entry = io(entry, "READ_OLD_RUNTIME_ENTRY", path)?;
        writable_directories(&entry.path())?;
    }
    Ok(())
}

fn monitor_manifest(source: &Path) -> Result<Value> {
    let path = source.join("architecture/native-runtime.json");
    let value: Value = serde_json::from_slice(&io(fs::read(&path), "READ_NATIVE_MANIFEST", &path)?)
        .map_err(|e| format!("INVALID_NATIVE_MANIFEST: {e}"))?;
    if value["schema"] != 1
        || value["device_runtime"].as_str().is_none_or(str::is_empty)
        || value["monitor_helpers"].as_array().is_none_or(|helpers| {
            helpers.is_empty()
                || helpers.iter().any(|helper| {
                    helper.as_str().is_none_or(|name| {
                        !name.starts_with("clinx-")
                            || !name.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-')
                    })
                })
        })
        || value["monitor_python_sources"] != json!([])
        || value["monitor_python_dependencies"] != json!([])
    {
        return Err("INVALID_NATIVE_MONITOR_MANIFEST".into());
    }
    Ok(json!({"schema":1,"profile":"monitor-native","files":{},
        "device_runtime":value["device_runtime"],"required_helpers":value["monitor_helpers"]}))
}

fn native_monitor_resources(source: &Path, resources: &Path) -> Result<Value> {
    // Validate source before removing any previous build output.
    let manifest = monitor_manifest(source)?;
    match fs::symlink_metadata(resources) {
        Ok(metadata) if !metadata.is_dir() || metadata.file_type().is_symlink() => {
            return Err("MONITOR_RESOURCES_MUST_BE_DIRECTORY".into());
        }
        Ok(_) => (),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            io(fs::create_dir_all(resources), "CREATE_RESOURCES", resources)?;
        }
        Err(e) => return Err(format!("READ_RESOURCES: {e}")),
    }
    let mut removed = Vec::new();
    for entry in io(fs::read_dir(resources), "READ_RESOURCES", resources)? {
        let entry = io(entry, "READ_RESOURCE_ENTRY", resources)?;
        if entry.file_name().as_bytes().ends_with(b".py") {
            io(
                fs::remove_file(entry.path()),
                "REMOVE_RETIRED_SOURCE",
                &entry.path(),
            )?;
            removed.push(entry.file_name().to_string_lossy().into_owned());
        }
    }
    for name in RETIRED_DIRECTORIES {
        let path = resources.join(name);
        let metadata = match fs::symlink_metadata(&path) {
            Ok(metadata) => metadata,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(e) => return Err(format!("READ_RETIRED_RUNTIME: {}: {e}", path.display())),
        };
        if metadata.file_type().is_symlink() {
            io(fs::remove_file(&path), "REMOVE_RETIRED_LINK", &path)?;
        } else {
            if *name == "DiscoveryRuntime" {
                writable_directories(&path)?;
            }
            io(fs::remove_dir_all(&path), "REMOVE_RETIRED_RUNTIME", &path)?;
        }
        removed.push((*name).into());
    }
    // Atomic replacement also prevents an old manifest symlink from redirecting
    // writes outside Resources. Other Swift assets are never opened for writes.
    let path = resources.join("python-sources.json");
    let mut temporary = io(
        tempfile::NamedTempFile::new_in(resources),
        "CREATE_MANIFEST",
        resources,
    )?;
    let mut bytes = serde_json::to_vec_pretty(&manifest).map_err(|e| e.to_string())?;
    bytes.push(b'\n');
    io(
        temporary.write_all(&bytes),
        "WRITE_MANIFEST",
        temporary.path(),
    )?;
    io(temporary.flush(), "FLUSH_MANIFEST", temporary.path())?;
    io(
        temporary
            .as_file()
            .set_permissions(fs::Permissions::from_mode(0o644)),
        "SET_MANIFEST_MODE",
        temporary.path(),
    )?;
    temporary
        .persist(&path)
        .map_err(|e| format!("COMMIT_MANIFEST: {e}"))?;
    removed.sort();
    Ok(
        json!({"contract":"RUST_MONITOR_BUILD_V1","resources":resources,
        "removed":removed,"manifest":manifest}),
    )
}

fn target_directory(manifest: &Path) -> Result<String> {
    let output = Command::new("cargo")
        .args([
            "metadata",
            "--locked",
            "--no-deps",
            "--format-version",
            "1",
            "--manifest-path",
        ])
        .arg(manifest)
        .output()
        .map_err(|e| format!("CARGO_METADATA_UNAVAILABLE: {e}"))?;
    if !output.status.success() {
        return Err(format!(
            "CARGO_METADATA_FAILED: {}",
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    let value: Value = serde_json::from_slice(&output.stdout)
        .map_err(|e| format!("INVALID_CARGO_METADATA: {e}"))?;
    value["target_directory"]
        .as_str()
        .filter(|path| Path::new(path).is_absolute())
        .map(str::to_owned)
        .ok_or_else(|| "CARGO_TARGET_DIRECTORY_REQUIRED".into())
}

fn run() -> Result<()> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    match args.as_slice() {
        [command] if command == "--contract" => println!("RUST_MONITOR_BUILD_V1"),
        [command] if command == "--build-contract" => println!("RUST_RELEASE_BUILD_V1"),
        [command] if command == "--help" => println!("clinx-build native-monitor-resources SOURCE_ROOT RESOURCES | cargo-target-directory CARGO_MANIFEST | stage-rust-release OPTIONS | build-tunnel-release OPTIONS | build-transport-runtime OPTIONS | tunnel-cas check|fixture-apply OPTIONS; release builders stage only, CAS writes only marked /data/artifacts fixtures"),
        [command, rest @ ..] if ["stage-rust-release", "build-tunnel-release", "build-transport-runtime", "tunnel-cas"].iter().any(|v| command == v) => {
            let command=command.to_str().ok_or("INVALID_BUILD_ARGUMENT")?;
            let args=rest.iter().map(|v|v.to_str().map(str::to_owned).ok_or_else(||"INVALID_BUILD_ARGUMENT".to_owned())).collect::<Result<Vec<_>>>()?;
            println!("{}",release_tools::cli(command,&args)?);
        }
        [command, source, resources] if command == "native-monitor-resources" => {
            println!("{}", native_monitor_resources(Path::new(source), Path::new(resources))?);
        }
        [command, manifest] if command == "cargo-target-directory" => {
            println!("{}", target_directory(Path::new(manifest))?);
        }
        _ => return Err("Usage: clinx-build native-monitor-resources SOURCE_ROOT RESOURCES | cargo-target-directory CARGO_MANIFEST | --contract".into()),
    }
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}
