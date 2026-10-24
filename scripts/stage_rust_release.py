#!/usr/bin/env python3
"""Stage an immutable runtime release. Does not select or stop production."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lab", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--source", type=Path, help="Exact committed checkout to package; defaults to this repository")
    p.add_argument("--provider-binary", type=Path, required=True)
    p.add_argument("--provider-vendor", type=Path, required=True, help="Matching version vendor directory; all Provider helpers come from this artifact")
    args = p.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("ADMINISTRATOR_REQUIRED")
    source = (args.source or Path(__file__).resolve().parents[1]).resolve(strict=True)
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
