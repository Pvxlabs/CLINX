"""Deterministic, stdlib-only P1 release and config-only rollback capsule builder.

Reads the current installation; writes only a new task-owned /data/artifacts path.
Does not run Tunnel, systemctl, SQLite, activation, recovery or task APIs.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import stat
import subprocess
import tarfile
from pathlib import Path

if __package__:
    from .build_transport_runtime import qualified_runtime
    from .tunnel_release_cas import sha, state
else:
    from build_transport_runtime import qualified_runtime
    from tunnel_release_cas import sha, state

SOURCES = (
    "clinx/transport/tunnel_reliability.py",
    "scripts/build_tunnel_release.py",
    "scripts/tunnel_release_cas.py",
    "systemd/clinx-tunnel-reliability.service.in",
    "systemd/clinx-tunnel-health-reliability.service.in",
    "systemd/clinx-tunnel-reliability-final.conf",
    "systemd/tunnel-reliability-policy.json",
)
CONTROL_OVERLAY_FILES = ("workspace_contract.py", "policy_maintenance.py")


def encoded(data):
    return (json.dumps(data, sort_keys=True, indent=2) + "\n").encode()


def archive(directory, destination):
    """Stable paths, metadata, order, gzip header and compression level."""
    with destination.open("xb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0, compresslevel=9) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
                for path in sorted(directory.rglob("*")):
                    if not path.is_file():
                        continue
                    content = path.read_bytes()
                    member = tarfile.TarInfo(path.relative_to(directory).as_posix())
                    member.mode = stat.S_IMODE(path.stat().st_mode)
                    member.size = len(content)
                    member.mtime = member.uid = member.gid = 0
                    member.uname = member.gname = ""
                    tar.addfile(member, io.BytesIO(content))


def build(source, output, home, python, tunnel, guard_paths, evidence, *, runtime_manifest=None,
          control_overlay_base=None, node_binary=None):
    output = Path(output).absolute()
    if not output.is_relative_to("/data/artifacts") or output.exists():
        raise ValueError("NEW_TASK_ARTIFACT_OUTPUT_REQUIRED")
    if output.resolve() != output:
        raise ValueError("OUTPUT_SYMLINK_REFUSED")
    source = Path(source).resolve()
    if control_overlay_base is not None and (source / 'architecture/python-sources.json').is_file():
        raise ValueError('LEGACY_CONTROL_OVERLAY_RETIRED_USE_STAGE_RUST_RELEASE')
    home = str(Path(home))
    # P620 systemd ExecStart paths are unquoted tokens: reject ambiguous inputs.
    for value in (home, str(python), str(tunnel)):
        if not value.startswith("/") or any(c.isspace() or c in "%@$;\\\"'" for c in value):
            raise ValueError("UNSUPPORTED_PATH")
    policy = json.loads((source / "systemd/tunnel-reliability-policy.json").read_text())
    inputs = {
        "source_head": subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip(),
        "source_sha256": {p: sha((source / p).read_bytes()) for p in SOURCES},
        "python": {"path": str(python), **state(Path(python))},
        "tunnel": {"path": str(tunnel), **state(Path(tunnel))},
        "home": home,
        "reused_evidence_sha256": evidence,
    }
    node_value = node_binary or os.environ.get('CLINX_NODE_BINARY')
    if not node_value:
        raise ValueError('QUALIFIED_RUST_CLIENT_RUNTIME_REQUIRED')
    node = Path(node_value).resolve(strict=True)
    checked = subprocess.run([str(node), '--client-runtime-contract'], input='',
                             text=True, capture_output=True, timeout=5, check=False)
    if checked.returncode or checked.stdout.strip() != 'RUST_CLIENT_RUNTIME_V1':
        raise ValueError('QUALIFIED_RUST_CLIENT_RUNTIME_REQUIRED')
    inputs['client_runtime'] = {'contract': 'RUST_CLIENT_RUNTIME_V1', 'sha256': sha(node.read_bytes())}
    runtime = None
    if runtime_manifest is not None:
        runtime_manifest = Path(runtime_manifest)
        runtime = qualified_runtime(runtime_manifest)
        inputs["transport_runtime"] = {
            "manifest_sha256": sha(runtime_manifest.read_bytes()),
            "binary_sha256": sha(runtime.read_bytes()),
            "builder_sha256": sha((source / "scripts/build_transport_runtime.py").read_bytes()),
        }
    overlay = None
    overlay_guards = set()
    if control_overlay_base is not None:
        base = Path(control_overlay_base).absolute()
        if base.resolve(strict=True) != base or not (base / "mcp_server.py").is_file():
            raise ValueError("EXACT_CONTROL_SOURCE_REQUIRED")
        # Stage only these two source changes. Never copy canonical m9 or other
        # dirty work into the currently installed control release.
        overlay = {
            "schema": "CLINX_P1_DEFERRED_CONTROL_OVERLAY_V1",
            "base_source": str(base),
            "activation": "NOT_WIRED_SEPARATE_OPERATOR_PUBLICATION_REQUIRED",
            "native_permissions_changed": False,
            "current_execution_policy_changed": False,
            "files": {},
        }
        for name in CONTROL_OVERLAY_FILES:
            data = (source / name).read_bytes()
            overlay["files"][name] = {
                "before": state(base / name),
                "after": {"sha256": sha(data), "mode": 0o644},
            }
        # Freeze the Python dependencies against which the overlay qualifies.
        overlay_guards = {str(p) for p in base.rglob("*.py")
                          if not any(part.startswith((".", "test")) for part in p.relative_to(base).parts)}
        overlay_guards.update(str(base / name) for name in CONTROL_OVERLAY_FILES)
        inputs["control_overlay"] = overlay
        inputs["control_dependency_states"] = {
            p: state(Path(p)) for p in sorted(overlay_guards)
        }
    release_id = "tunnel-reliability-p1-" + sha(encoded(inputs))[:20]
    install = home + "/.local/lib/clinx-control/releases/" + release_id
    output.mkdir(parents=True, mode=0o700)
    release = output / "release"
    release.mkdir(mode=0o755)
    substitutions = {
        "@HOME@": home,
        "@RELEASE@": install,
        "@PYTHON@": str(python),
        "@TUNNEL@": install + "/tunnel-client-runtime" if runtime else str(tunnel),
        "@CODEX_PATH@": home + "/.nvm/versions/node/v24.19.0/bin:/usr/local/bin:/usr/bin:/bin",
        "@POLICY_ARGS@": " ".join(
            "--" + k.replace("_", "-") + " " + str(v) for k, v in sorted(policy["policy"].items())
        ),
    }
    contents = {
        "tunnel_reliability.py": (source / "clinx/transport/tunnel_reliability.py").read_bytes(),
        "tunnel_release_cas.py": (source / "scripts/tunnel_release_cas.py").read_bytes(),
        "policy.json": encoded(policy),
        "units/99-p1-reliability.conf": (
            source / "systemd/clinx-tunnel-reliability-final.conf"
        ).read_bytes(),
    }
    contents["clinx-node"] = node.read_bytes()
    modes = {"clinx-node": 0o755}
    if overlay:
        contents["control-overlay/manifest.json"] = encoded(overlay)
        for name in CONTROL_OVERLAY_FILES:
            contents["control-overlay/" + name] = (source / name).read_bytes()
            if sha(contents["control-overlay/" + name]) != overlay["files"][name]["after"]["sha256"]:
                raise ValueError("CONTROL_OVERLAY_SOURCE_DRIFT")
    if runtime:
        contents["tunnel-client-runtime"] = runtime.read_bytes()
        contents["transport-runtime-manifest.json"] = runtime_manifest.read_bytes()
        contents["transport-runtime-dependencies.txt"] = (runtime.parent / "dependencies.txt").read_bytes()
        modes["tunnel-client-runtime"] = 0o755
    for template, target in (
        ("systemd/clinx-tunnel-reliability.service.in", "units/clinx-tunnel.service"),
        ("systemd/clinx-tunnel-health-reliability.service.in", "units/clinx-tunnel-health.service"),
    ):
        text = (source / template).read_text()
        for token, replacement in substitutions.items():
            text = text.replace(token, replacement)
        if "@" in text:
            raise ValueError("UNRENDERED_TEMPLATE")
        contents[target] = text.encode()
    for name, data in contents.items():
        p = release / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        p.chmod(modes.get(name, 0o644))
    manifest = {
        "schema": "CLINX_P1_RELEASE_V1",
        "release_id": release_id,
        "install_path": install,
        "inputs": inputs,
        "files": {p: {"sha256": sha(b), "mode": modes.get(p, 0o644)} for p, b in sorted(contents.items())},
        "status": "NOT_DEPLOYED",
        "production_slo_approval": policy["slo"]["approval"],
    }
    (release / "manifest.json").write_bytes(encoded(manifest))
    (release / "manifest.json").chmod(0o644)
    capsule = output / "rollback-capsule"
    (capsule / "blobs").mkdir(parents=True)
    if overlay:
        # Recovery material is staged, never an instruction to overwrite the
        # immutable installed source or broaden the three-path config CAS.
        (capsule / "control-overlay-restore.json").write_bytes(encoded(overlay))
        for name, entry in overlay["files"].items():
            if entry["before"]:
                (capsule / "blobs" / entry["before"]["sha256"]).write_bytes(
                    (Path(overlay["base_source"]) / name).read_bytes())
            (capsule / "blobs" / entry["after"]["sha256"]).write_bytes(
                contents["control-overlay/" + name])
    unit_root = home + "/.config/systemd/user/"
    mapping = {
        unit_root + "clinx-tunnel.service": "units/clinx-tunnel.service",
        unit_root + "clinx-tunnel-health.service": "units/clinx-tunnel-health.service",
        unit_root + "clinx-tunnel.service.d/99-p1-reliability.conf": "units/99-p1-reliability.conf",
    }
    changes = {}
    for name, member in mapping.items():
        p = Path(name)
        before = state(p)
        if before is not None:
            (capsule / "blobs" / before["sha256"]).write_bytes(p.read_bytes())
        after = {"sha256": sha(contents[member]), "mode": 0o644}
        (capsule / "blobs" / after["sha256"]).write_bytes(contents[member])
        changes[name] = {"before": before, "after": after}
    directories = {}
    guards = set(guard_paths) | {str(python), str(tunnel)} | overlay_guards
    for name in ("clinx-tunnel.service.d", "clinx-tunnel-health.service.d"):
        path = Path(unit_root + name)
        directories[str(path)] = sorted(p.name for p in path.iterdir()) if path.exists() else []
        if path.exists():
            guards.update(str(p) for p in path.iterdir())
    # The existing timer's cadence is part of the paired health contract.
    guards.add(unit_root + "clinx-tunnel-health.timer")
    guard_states = {p: state(Path(p)) for p in sorted(guards - set(mapping))}
    if overlay and any(guard_states[p] != s for p, s in inputs["control_dependency_states"].items()):
        raise ValueError("CONTROL_OVERLAY_BASE_DRIFT")
    plan = {
        "schema": "CLINX_P1_CONFIG_CAS_V1",
        "home": home,
        "release_id": release_id,
        "release_manifest_sha256": sha((release / "manifest.json").read_bytes()),
        "managed_paths": sorted(mapping),
        "changes": changes,
        "guards": guard_states,
        "directory_entries": directories,
        "production_authorization": "NOT_GRANTED",
        "preconditions": [
            "independently authorized activation",
            "admission drained including this task",
            "no active execution/lease",
            "all transport consumers proven safe to stop",
            "fresh consistency backup",
            "SLO policy approved",
        ],
        "never_restore": [
            "database",
            "lease",
            "terminal facts",
            "profile",
            "credentials",
            "supervisor budget",
            "Owner",
            "Provider",
            "MCP launcher",
        ],
    }
    (capsule / "plan.json").write_bytes(encoded(plan))
    (capsule / "tunnel_release_cas.py").write_bytes(
        (source / "scripts/tunnel_release_cas.py").read_bytes()
    )
    for p in capsule.rglob("*"):
        if p.is_file():
            p.chmod(0o600)
    archive(release, output / "release.tar.gz")
    archive(capsule, output / "rollback-capsule.tar.gz")
    receipt = {
        "release_id": release_id,
        "install_path": install,
        "release_tar_sha256": sha((output / "release.tar.gz").read_bytes()),
        "release_manifest_sha256": sha((release / "manifest.json").read_bytes()),
        "rollback_tar_sha256": sha((output / "rollback-capsule.tar.gz").read_bytes()),
        "plan_sha256": sha((capsule / "plan.json").read_bytes()),
        "managed_file_count": len(changes),
        "guard_count": len(guard_states),
        "deployed": False,
    }
    (output / "build-receipt.json").write_bytes(encoded(receipt))
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--python", type=Path, default=Path("/usr/bin/python3.12"))
    parser.add_argument("--tunnel", type=Path, default=Path.home() / ".local/bin/tunnel-client")
    parser.add_argument("--guard-manifest", type=Path, required=True)
    parser.add_argument("--evidence-manifest", type=Path, required=True)
    parser.add_argument("--transport-runtime-manifest", type=Path)
    parser.add_argument("--node-binary", type=Path, help="Qualified Rust supervisor policy binary; never inherited from a prior release")
    parser.add_argument("--control-overlay-base", type=Path,
                        help="Stage parser/operator sources and exact recovery data; does not wire or authorize activation")
    args = parser.parse_args()
    evidence = json.loads(args.evidence_manifest.read_text())
    for path, digest in evidence.items():
        if sha(Path(path).read_bytes()) != digest:
            raise ValueError("REUSED_EVIDENCE_DRIFT")
    guards = json.loads(args.guard_manifest.read_text())
    for path, digest in guards.items():
        if sha(Path(path).read_bytes()) != digest:
            raise ValueError("INSTALLED_GUARD_DRIFT")
    print(
        json.dumps(
            build(args.source, args.output, args.home, args.python, args.tunnel, guards, evidence,
                  runtime_manifest=args.transport_runtime_manifest,
                  control_overlay_base=args.control_overlay_base, node_binary=args.node_binary),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
