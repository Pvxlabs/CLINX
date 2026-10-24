#!/usr/bin/env python3
"""Install dedicated identities' service definitions without selecting production.

Starts nothing and imports no historical task, lease, or runtime database.
"""

import argparse
import grp
import json
import os
import pwd
import shutil
import subprocess
import tomllib
from pathlib import Path

ROOT = Path("/var/lib/clinx-rust")


def toml(data, prefix=()):
    def scalar(value):
        if isinstance(value, bool):
            return str(value).lower()
        return json.dumps(value, ensure_ascii=False)

    lines = []
    if prefix:
        lines.append("[" + ".".join(json.dumps(k) for k in prefix) + "]")
    lines += [
        json.dumps(k) + " = " + scalar(v)
        for k, v in data.items()
        if not isinstance(v, dict)
    ]
    for k, v in data.items():
        if isinstance(v, dict):
            lines.extend(toml(v, (*prefix, k)))
    return lines


def owned_dir(path, user, mode=0o700, group=None):
    p = Path(path)
    a = pwd.getpwnam(user)
    p.mkdir(mode=mode, parents=True, exist_ok=True)
    if p.is_symlink():
        raise RuntimeError("SYMLINK_DIRECTORY_DENIED")
    os.chown(p, a.pw_uid, grp.getgrnam(group).gr_gid if group else a.pw_gid)
    p.chmod(mode)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--release", type=Path, required=True)
    p.add_argument("--operator-home", type=Path, required=True)
    p.add_argument("--bridge-config", type=Path, required=True)
    args = p.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("ADMINISTRATOR_REQUIRED")
    for name in ('authority', 'provider', 'dispatcher'):
        if Path('/etc/systemd/system', 'clinx-rust-' + name + '.service').exists():
            raise RuntimeError('SERVICE_ALREADY_EXISTS:' + name)
    release = args.release.resolve(strict=True)
    manifest = json.loads((release / "manifest.json").read_text())
    if release.name != manifest["version"]:
        raise RuntimeError("RELEASE_IDENTITY_MISMATCH")
    import hashlib

    for file, digest in manifest["files"].items():
        path = release / file
        if (
            path.stat().st_uid != 0
            or path.stat().st_mode & 0o022
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest
        ):
            raise RuntimeError("RELEASE_CONTENT_MISMATCH:" + file)
    cfgdir = ROOT / "config"
    cfgdir.mkdir(mode=0o755, exist_ok=True)
    worker = pwd.getpwuid(args.operator_home.stat().st_uid)
    if worker.pw_uid == 0 or worker.pw_name in ('clinx-authority', 'clinx-api', 'clinx-transport'):
        raise RuntimeError('WORKER_AUTHORITY_IDENTITY_CONFLICT')
    identity = cfgdir / 'worker-user'
    identity.write_text(worker.pw_name + '\n')
    identity.chmod(0o644)
    for user, name in [
        (worker.pw_name, "worker"),
        ("clinx-api", "api"),
        ("clinx-transport", "transport"),
    ]:
        owned_dir(ROOT / name, user)
    owned_dir("/run/clinx-rust-provider", worker.pw_name, 0o2750, "clinx-ipc")
    owned_dir("/run/clinx-rust-provider/native", worker.pw_name, 0o700, "clinx-provider-ipc")
    codexhome = ROOT / "worker/codex"
    owned_dir(codexhome, worker.pw_name)
    old = tomllib.loads((args.operator_home / ".codex/config.toml").read_text())
    minimal = {
        k: old[k]
        for k in (
            "model_provider",
            "model",
            "model_reasoning_effort",
            "model_providers",
        )
        if k in old
    }
    minimal.update(approval_policy="never", sandbox_mode="danger-full-access")
    private = codexhome / "config.toml"
    private.write_text("\n".join(toml(minimal)) + "\n")
    auth = codexhome / "auth.json"
    shutil.copyfile(args.operator_home / ".codex/auth.json", auth)
    for path in (private, auth):
        os.chown(path, worker.pw_uid, worker.pw_gid)
        path.chmod(0o600)
    config = tomllib.loads(args.bridge_config.read_text())
    config["app_server"]["local_socket"] = "/run/clinx-rust-provider/provider.sock"
    config["app_server"].pop("native_socket", None)
    config["runtime"]["log_dir"] = str(ROOT / "transport/logs")
    config["runtime"].pop("task_db_path", None)
    # Preserve configured routes and Host command definitions; no legacy DB path.
    (cfgdir / "bridge.toml").write_text("\n".join(toml(config)) + "\n")
    policy = dict(
        projects=[
            dict(alias=k, host="p620", cwd=v["cwd"])
            for k, v in config["projects"].items()
        ],
        workspaces=[
            {k: w[k] for k in ("host", "root", "allow_existing_projects")}
            for w in config.get("workspaces", {}).values()
        ],
        endpoints=["/run/clinx-rust-provider/provider.sock"],
        lease_ms=120000,
        host_operations=[
            dict(
                capability="LOCAL_HOST_PROCESS",
                operation="user_identity",
                operation_class="READ_ONLY_HOST",
                target="",
            )
        ],
    )
    (cfgdir / "policy.json").write_text(json.dumps(policy, indent=2) + "\n")
    a = pwd.getpwnam("clinx-authority")
    api = pwd.getpwnam("clinx-api")
    transport = pwd.getpwnam("clinx-transport")
    common = """\nProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes
PrivateDevices=yes
ProtectProc=invisible
ProcSubset=pid
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes
UMask=0077
KillMode=control-group
Restart=on-failure
RestartSec=3
"""
    execution_common = common.replace("ProtectSystem=strict", "ProtectSystem=no").replace("ProtectHome=read-only", "ProtectHome=no")
    env = f"Environment=PYTHONPATH={release}/control:{release}/deps\nEnvironment=PYTHONDONTWRITEBYTECODE=1\nEnvironment=CLINX_AUTHORITY=rust\nEnvironment=CLINX_RUST_SOCKET=/run/clinx-rust/authority.sock\nEnvironment=CLINX_RUST_UID={a.pw_uid}\n"
    command = f"{release}/clinx-runtime --state-dir {ROOT}/authority --socket /run/clinx-rust/authority.sock --policy {cfgdir}/policy.json --caller-uid {api.pw_uid} --transport-uid {transport.pw_uid}"
    units = {
        "clinx-rust-authority.service": f"""[Unit]
Description=CLINX sole Rust business authority (staged)
After=network.target
[Service]
User=clinx-authority
Group=clinx-authority
SupplementaryGroups=clinx-ipc clinx-provider-ipc
ExecStart={command}
BindReadOnlyPaths=/run/clinx-rust-provider/native:/tmp/codex-daemon-{worker.pw_uid}
ReadWritePaths={ROOT}/authority /run/clinx-rust
NoNewPrivileges=yes
CapabilityBoundingSet=
{common.replace("ProcSubset=pid", "")}
""",
        "clinx-rust-provider.service": f"""[Unit]
Description=CLINX confined Codex Provider
After=network-online.target
[Service]
User={worker.pw_name}
Group={worker.pw_gid}
SupplementaryGroups=clinx-provider-ipc
Environment=HOME={args.operator_home}
Environment=CODEX_HOME={codexhome}
Environment=TMPDIR=/run/clinx-rust-provider
Environment=PATH={release}/provider/bin:{release}/provider/codex-path:/usr/bin:/bin
ExecStartPre=/usr/bin/chmod 00700 /run/clinx-rust-provider/native
ExecStart={release}/provider/bin/codex app-server --listen unix:///run/clinx-rust-provider/provider.sock
ExecStartPost=/usr/bin/python3 {release}/control/rust_provider_socket.py
BindPaths=/run/clinx-rust-provider/native:/tmp/codex-daemon-{worker.pw_uid}
ReadWritePaths={ROOT}/worker /run/clinx-rust-provider
InaccessiblePaths={ROOT}/authority /run/clinx-rust /run/clinx-rust-api /run/user/1001 -/run/docker.sock -/run/containerd
NoNewPrivileges=yes
CapabilityBoundingSet=
{execution_common}
""",
        "clinx-rust-dispatcher.service": f"""[Unit]
Description=CLINX Rust authority transport adapter
After=clinx-rust-authority.service clinx-rust-provider.service
Requires=clinx-rust-authority.service clinx-rust-provider.service
[Service]
User=clinx-transport
Group=clinx-transport
SupplementaryGroups=clinx-ipc
{env}Environment=CLINX_RUST_WORKER_LAUNCHER={release}/host-worker
ExecStart=/usr/bin/python3 {release}/control/rust_transport.py --config {cfgdir}/bridge.toml
BindReadOnlyPaths=/run/clinx-rust-provider/native:/tmp/codex-daemon-{worker.pw_uid}
ReadWritePaths={ROOT}/transport
InaccessiblePaths={ROOT}/authority /run/clinx-rust-api /run/user/1001 -/run/docker.sock -/run/containerd
CapabilityBoundingSet=CAP_SETUID CAP_SETGID
{execution_common}
""",
    }
    for name, content in units.items():
        path = Path("/etc/systemd/system") / name
        if path.exists():
            raise RuntimeError("SERVICE_ALREADY_EXISTS:" + name)
        path.write_text(content)
        path.chmod(0o644)
    # Precise traversal only. No ownership or mode changes to any project files.
    for parent in (args.operator_home, args.operator_home / "dev"):
        subprocess.run(
            [
                "setfacl",
                "-m",
                ",".join(
                    "u:" + u + ":--x"
                    for u in (
                        "clinx-authority",
                        "clinx-api",
                        "clinx-transport",
                        worker.pw_name,
                    )
                ),
                str(parent),
            ],
            check=True,
        )
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    print(
        json.dumps(
            dict(
                status="INSTALLED_NOT_STARTED",
                release=str(release),
                units=list(units),
                init_command=command + " --init-only",
            )
        )
    )


if __name__ == "__main__":
    main()
