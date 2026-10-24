#!/usr/bin/env python3
"""Stage an immutable runtime release. Does not select or stop production."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


def stage_desktop(args, source):
    """Overlay named source files; preserve all other deployed file identities."""
    base = args.desktop_base_release.resolve(strict=True)
    if base.parent != Path('/var/lib/clinx-rust/releases'):
        raise SystemExit('INSTALLED_BASE_RELEASE_REQUIRED')
    root_info = base.parent.stat()
    if root_info.st_uid != 0 or root_info.st_gid != 0 or root_info.st_mode & 0o002:
        raise SystemExit('ROOT_CONTROLLED_RELEASE_DIRECTORY_REQUIRED')
    # The Host launcher's trust check includes every ancestor. Some older
    # installations left this root:root directory group-writable (0775).
    base.parent.chmod(0o755)
    manifest = json.loads((base / 'manifest.json').read_text())
    for name, digest in manifest['files'].items():
        path = base / name
        if path.stat().st_uid != 0 or path.stat().st_mode & 0o022 or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise SystemExit('BASE_RELEASE_CONTENT_MISMATCH:' + name)
    # The new release contains no separate Codex Provider binary or home.
    files = {name: digest for name, digest in manifest['files'].items()
             if not name.startswith('provider/')}
    replacements = {}
    for name in args.replace_source:
        path = source / name
        if Path(name).name != name or not name.endswith('.py') or path.is_symlink():
            raise SystemExit('ROOT_PYTHON_SOURCE_REQUIRED')
        replacements['control/' + name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not replacements:
        raise SystemExit('EXPLICIT_REPLACEMENT_REQUIRED')
    files.update(replacements)
    runtime_binary = getattr(args, 'runtime_binary', None)
    if runtime_binary:
        runtime_binary = runtime_binary.resolve(strict=True)
        if not runtime_binary.is_file() or not runtime_binary.stat().st_mode & 0o111:
            raise SystemExit('RUNTIME_EXECUTABLE_REQUIRED')
        files['clinx-runtime'] = hashlib.sha256(runtime_binary.read_bytes()).hexdigest()
    version = 'rust-' + hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:20]
    release = base.parent / version
    release.mkdir(mode=0o755, exist_ok=False)
    for name, digest in files.items():
        target = release / name
        target.parent.mkdir(parents=True, exist_ok=True)
        original = (runtime_binary if name == 'clinx-runtime' and runtime_binary else
                    source / Path(name).name if name in replacements else base / name)
        shutil.copyfile(original, target)
        target.chmod(0o755 if original.stat().st_mode & 0o111 else 0o644)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise SystemExit('STAGED_CONTENT_MISMATCH:' + name)
    import grp
    os.chown(release / 'host-worker', 0, grp.getgrnam('clinx-transport').gr_gid)
    (release / 'host-worker').chmod(0o4750)
    result = dict(version=version, source_cwd=str(source),
                  head=subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip(),
                  base_release=str(base), execution_daemon='EXISTING_DESKTOP_ONLY',
                  source_replacements=replacements, files=files, status='STAGED_NOT_SELECTED')
    (release / 'manifest.json').write_text(json.dumps(result, indent=2) + '\n')
    args.output.write_text(json.dumps(dict(release=str(release), **result), indent=2) + '\n')
    print(json.dumps(dict(version=version, release=str(release), status='STAGED_NOT_SELECTED')))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lab", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--source", type=Path, help="Exact committed checkout to package; defaults to this repository")
    p.add_argument("--provider-binary", type=Path)
    p.add_argument("--provider-vendor", type=Path, help="Matching version vendor directory; all Provider helpers come from this artifact")
    p.add_argument('--desktop-base-release', type=Path)
    p.add_argument('--runtime-binary', type=Path, help='Explicit tested authority binary for this release')
    p.add_argument('--replace-source', action='append', default=[])
    args = p.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("ADMINISTRATOR_REQUIRED")
    source = (args.source or Path(__file__).resolve().parents[1]).resolve(strict=True)
    if args.desktop_base_release:
        return stage_desktop(args, source)
    if not args.lab or not args.provider_binary or not args.provider_vendor:
        raise SystemExit('LEGACY_RELEASE_INPUTS_REQUIRED')
    paths = sorted([*source.glob("*.py"), *source.glob("requirements*.txt")])
    for package in (
        "domain",
        "runtime_control",
        "provider_adapters",
        "shadow_ledger",
        "local_discovery",
    ):
        paths.extend(sorted((source / package).glob("*.py")))
    files = {
        str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths
    }
    binary = args.lab / "target/release/clinx-runtime"
    helper = args.lab / "candidate/host-worker"
    files["clinx-runtime"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    files["host-worker"] = hashlib.sha256(helper.read_bytes()).hexdigest()
    files["provider/bin/codex"] = hashlib.sha256(args.provider_binary.read_bytes()).hexdigest()
    vendor = args.provider_vendor.resolve(strict=True)
    if hashlib.sha256((vendor / "bin/codex").read_bytes()).hexdigest() != files["provider/bin/codex"]:
        raise SystemExit("PROVIDER_VENDOR_BINARY_MISMATCH")
    for relative in ("bin/codex-code-mode-host", "codex-resources/bwrap", "codex-path/rg"):
        files["provider/" + relative] = hashlib.sha256((vendor / relative).read_bytes()).hexdigest()
    version = (
        "rust-"
        + hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:20]
    )
    release = Path("/var/lib/clinx-rust/releases") / version
    release.mkdir(parents=True, mode=0o755, exist_ok=False)
    control = release / "control"
    control.mkdir()
    for path in paths:
        target = control / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        target.chmod(0o644)
    for path in (binary, helper):
        shutil.copyfile(path, release / path.name)
        (release / path.name).chmod(0o755)
    import grp

    os.chown(release / "host-worker", 0, grp.getgrnam("clinx-transport").gr_gid)
    (release / "host-worker").chmod(0o4750)
    shutil.copytree(
        args.lab / "production-deps",
        release / "deps",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        args.lab / "test-dependencies/_clinx_opaque",
        release / "deps/_clinx_opaque",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for path in (release / "deps", *(release / "deps").rglob("*")):
        path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
    for relative in (
        "bin/codex",
        "bin/codex-code-mode-host",
        "codex-resources/bwrap",
        "codex-path/rg",
    ):
        target = release / "provider" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.provider_binary if relative == 'bin/codex' else vendor / relative, target)
        target.chmod(0o755)
    manifest = {
        "version": version,
        "source_cwd": str(source),
        "head": subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip(),
        "status": "STAGED_NOT_SELECTED",
        "files": {
            str(p.relative_to(release)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(release.rglob("*"))
            if p.is_file()
        },
    }
    (release / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    args.output.write_text(
        json.dumps({"release": str(release), **manifest}, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "version": version,
                "release": str(release),
                "status": "STAGED_NOT_SELECTED",
            }
        )
    )


if __name__ == "__main__":
    main()
