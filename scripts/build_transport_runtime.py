"""Build the pinned upstream transport-only entrypoint, without installing it.

Inputs are hash-pinned archives. Output and all disposable build state must live
in a new task-owned /data/artifacts directory. No upstream patch, task database,
Provider, service operation, profile loading or production consumer is involved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

COMMIT = "0f870e50a973fa820d4c409000059e181e8d242b"
SOURCE_SHA256 = "0281b17c0979f9db1ae2b41b0b66df697d646e726995756a003c6ffc51a64c10"
GO_SHA256 = "65efbc80fdc1fa2cd2631cad5b481777a68e8725f34e89f50fc6ac714e379b40"
PREFIX = "github.com/openai/tunnel-client/"
ENTRYPOINT = "./cmd/client-runtime"
REQUIRED = {"cmd/client-runtime", "pkg/runtimeapp", "pkg/runtimehealth",
            "pkg/mcpclient", "pkg/controlplane", "pkg/metrics"}
FORBIDDEN = {"cmd/client", "pkg/app", "pkg/adminui", "pkg/codexappserver",
             "pkg/codexplugin", "pkg/cloudflared"}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dependency_gate(dependencies):
    packages = set(dependencies.splitlines())
    if not {PREFIX + p for p in REQUIRED} <= packages:
        raise ValueError("TRANSPORT_DEPENDENCY_MISSING")
    if any(p == PREFIX + f or p.startswith(PREFIX + f + "/")
           for p in packages for f in FORBIDDEN):
        raise ValueError("EMBEDDED_EXECUTOR_OR_FULL_CLIENT_LINKED")


def qualified_runtime(manifest_path):
    """Verify local candidate provenance; this is not an activation grant."""
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    if (manifest.get("schema") != "CLINX_TRANSPORT_RUNTIME_CANDIDATE_V1"
            or manifest.get("status") != "BUILT_OFFLINE_NOT_DEPLOYED"
            or manifest.get("upstream_commit") != COMMIT
            or manifest.get("upstream_archive_sha256") != SOURCE_SHA256
            or manifest.get("go_archive_sha256") != GO_SHA256
            or manifest.get("entrypoint") != ENTRYPOINT
            or manifest.get("upstream_patch") is not None
            or manifest.get("embedded_codex") != "NOT_LINKED"
            or manifest.get("reproducible_across_source_paths") is not True
            or manifest.get("transient_removed") is not True):
        raise ValueError("UNQUALIFIED_TRANSPORT_RUNTIME")
    binary = manifest_path.parent / "tunnel-client-runtime"
    deps = manifest_path.parent / "dependencies.txt"
    if (sha(binary) != manifest["binary"]["sha256"]
            or sha(deps) != manifest["dependencies_sha256"]
            or sha(manifest_path.parent / "builder-source.py") != manifest["builder_sha256"]
            or sha(manifest_path.parent / "upstream-source-files.json")
            != manifest["source_files_manifest_sha256"]):
        raise ValueError("TRANSPORT_RUNTIME_ARTIFACT_DRIFT")
    dependency_gate(deps.read_text())
    return binary


def extract(archive, destination, expected_sha):
    if sha(archive) != expected_sha:
        raise ValueError("PINNED_ARCHIVE_HASH_MISMATCH")
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            path = Path(member.name)
            if (path.is_absolute() or ".." in path.parts
                    or not (member.isfile() or member.isdir())):
                raise ValueError("ARCHIVE_NON_REGULAR_OR_TRAVERSAL")
        tar.extractall(destination, filter="data")


def remove_build_state(transient):
    # Go's module cache deliberately uses read-only directories. Only this
    # builder-created tree is made writable; do not chmod external dependencies.
    for directory, _, _ in os.walk(transient):
        os.chmod(directory, 0o700)
    shutil.rmtree(transient)


def build(source_archive, go_archive, output):
    output = Path(output).absolute()
    if (not output.is_relative_to("/data/artifacts") or output.exists()
            or output.resolve() != output):
        raise ValueError("NEW_TASK_ARTIFACT_DIRECTORY_REQUIRED")
    output.mkdir(mode=0o700, parents=True)
    (output / "builder-source.py").write_bytes(Path(__file__).read_bytes())
    transient = output / "transient"
    transient.mkdir()
    receipt = {"status": "BUILD_FAILED", "deployed": False}
    try:
        extract(source_archive, transient, SOURCE_SHA256)
        extract(go_archive, transient, GO_SHA256)
        source = transient / ("tunnel-client-" + COMMIT)
        go = transient / "go/bin/go"
        (transient / "home").mkdir()
        (transient / "tmp").mkdir()
        env = {"PATH": str(go.parent) + ":/usr/bin:/bin",
               "HOME": str(transient / "home"), "TMPDIR": str(transient / "tmp"),
               "GOTOOLCHAIN": "local", "CGO_ENABLED": "0", "GOOS": "linux",
               "GOARCH": "amd64", "GOAMD64": "v1", "GOMAXPROCS": "2",
               "GOMODCACHE": str(transient / "modcache"),
               "GOCACHE": str(transient / "buildcache"),
               "GOPROXY": "https://proxy.golang.org", "GOSUMDB": "sum.golang.org"}
        # Network proxy settings are build-only; no runtime credentials inherited.
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
            if key in os.environ:
                env[key] = os.environ[key]

        def run(name, args, cwd=source):
            with (output / (name + ".stderr.log")).open("wb") as errors:
                result = subprocess.run([str(go), *args], cwd=cwd, env=env,
                                        stdout=subprocess.PIPE, stderr=errors,
                                        timeout=300, check=True)
            (output / (name + ".txt")).write_bytes(result.stdout)
            return result.stdout.decode()

        version = run("go-version", ["version"]).strip()
        if version != "go version go1.27.3 linux/amd64":
            raise ValueError("UNEXPECTED_TOOLCHAIN")
        source_files = {str(p.relative_to(source)): sha(p)
                        for p in sorted(source.rglob("*")) if p.is_file()}
        (output / "upstream-source-files.json").write_text(json.dumps(source_files, indent=2) + "\n")
        deps = run("dependencies", ["list", "-mod=readonly", "-deps", ENTRYPOINT])
        dependency_gate(deps)
        run("upstream-tests", ["test", "-mod=readonly", "-count=1", "-timeout=90s",
                               "./cmd/client-runtime", "./pkg/runtimeapp",
                               "./pkg/runtimehealth", "./pkg/runtimeconfig"])
        flags = ["-trimpath", "-buildvcs=false", "-mod=readonly", "-ldflags",
                 "-X " + PREFIX + "pkg/version.GitSHA=" + COMMIT
                 + " -X " + PREFIX + "pkg/version.Flavor=runtime"]
        binary = output / "tunnel-client-runtime"
        run("build-a", ["build", *flags, "-o", str(binary), ENTRYPOINT])
        # Different source directory, identical bytes and flags, no source edits.
        second = transient / "second-source"
        shutil.copytree(source, second)
        run("build-b", ["build", *flags, "-o", str(transient / "binary-b"), ENTRYPOINT], second)
        if sha(binary) != sha(transient / "binary-b"):
            raise ValueError("REPRODUCIBLE_BUILD_MISMATCH")
        if any(sha(source / p) != digest for p, digest in source_files.items()):
            raise ValueError("UPSTREAM_SOURCE_MODIFIED")
        run("binary-build-info", ["version", "-m", str(binary)])
        receipt = {"schema": "CLINX_TRANSPORT_RUNTIME_CANDIDATE_V1",
                   "status": "BUILT_OFFLINE_NOT_DEPLOYED", "upstream_commit": COMMIT,
                   "upstream_archive_sha256": SOURCE_SHA256, "upstream_patch": None,
                   "go_archive_sha256": GO_SHA256, "go_version": version,
                   "entrypoint": ENTRYPOINT, "build_flags": flags,
                   "binary": {"path": str(binary), "sha256": sha(binary)},
                   "dependencies_sha256": sha(output / "dependencies.txt"),
                   "source_files_manifest_sha256": sha(output / "upstream-source-files.json"),
                   "builder_sha256": sha(__file__), "reproducible_across_source_paths": True,
                   "embedded_codex": "NOT_LINKED", "deployed": False,
                   "production_acceptance": "NOT_RUN", "activation_authority": "NOT_GRANTED_FOR_THIS_ARTIFACT"}
        return receipt
    finally:
        remove_build_state(transient)
        receipt["transient_removed"] = not transient.exists()
        (output / "manifest.json").write_text(json.dumps(receipt, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-archive", type=Path, required=True)
    parser.add_argument("--go-archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source_archive, args.go_archive, args.output), indent=2))


if __name__ == "__main__":
    main()
