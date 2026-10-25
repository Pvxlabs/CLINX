#!/usr/bin/env python3
"""Install the existing MCP and Centre adapters under the API identity.

Does not modify the active tunnel launcher or the legacy Centre port.
"""

import argparse
import grp
import hashlib
import json
import os
import pwd
import shutil
import subprocess
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--release", type=Path, required=True)
    p.add_argument("--legacy-node-state", type=Path, required=True)
    p.add_argument("--operator", default="pvxlabs")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("ADMINISTRATOR_REQUIRED")
    for name in ('clinx-rust-mcp.socket', 'clinx-rust-mcp@.service', 'clinx-rust-centre.service'):
        if Path('/etc/systemd/system', name).exists():
            raise RuntimeError('CALLER_UNIT_EXISTS:' + name)
    release = args.release.resolve(strict=True)
    manifest = json.loads((release / "manifest.json").read_text())
    if manifest.get('config_compiler') != 'RUST_CONFIG_COMPILER_V1':
        raise RuntimeError('QUALIFIED_RUST_CONFIG_COMPILER_REQUIRED')
    if manifest.get('source_layout') != 'qualified-clinx-v1':
        raise RuntimeError('QUALIFIED_PACKAGE_LAYOUT_REQUIRED')
    if (manifest.get('provider_exchange') != 'RUST_PROVIDER_V1'
            or 'clinx-provider' not in manifest['files']):
        raise RuntimeError('QUALIFIED_RUST_PROVIDER_REQUIRED')
    if manifest.get('observation_runtime') != 'RUST_OBSERVATION_V1':
        raise RuntimeError('QUALIFIED_RUST_OBSERVATION_REQUIRED')
    if manifest.get('client_runtime') != 'RUST_CLIENT_RUNTIME_V1':
        raise RuntimeError('QUALIFIED_RUST_CLIENT_RUNTIME_REQUIRED')
    if (manifest.get('device_runtime') != 'RUST_DEVICE_RUNTIME_V1'
            or manifest.get('node_daemon') != 'RUST_NODE_DAEMON_V1'
            or 'clinx-node-daemon' not in manifest['files']):
        raise RuntimeError('QUALIFIED_RUST_DEVICE_RUNTIME_REQUIRED')
    if manifest.get('thin_runtime') != 'RUST_THIN_RUNTIME_V1':
        raise RuntimeError('QUALIFIED_RUST_THIN_RUNTIME_REQUIRED')
    if manifest.get('collector_runtime') != 'RUST_COLLECTOR_V1':
        raise RuntimeError('QUALIFIED_RUST_COLLECTOR_REQUIRED')
    if manifest.get('node_network') != 'RUST_NODE_NETWORK_V1':
        raise RuntimeError('QUALIFIED_RUST_NODE_NETWORK_REQUIRED')
    if (manifest.get('node_api') != 'RUST_NODE_API_V1'
            or 'clinx-node' not in manifest['files']):
        raise RuntimeError('QUALIFIED_RUST_NODE_API_REQUIRED')
    if (manifest.get('mcp_runtime') != 'RUST_MCP_V1'
            or 'clinx-mcp' not in manifest['files']):
        raise RuntimeError('QUALIFIED_RUST_MCP_REQUIRED')
    for name, digest in manifest["files"].items():
        path = release / name
        if (
            path.stat().st_uid != 0
            or path.stat().st_mode & 0o022
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest
        ):
            raise RuntimeError("IMMUTABLE_RELEASE_REQUIRED")
    root = Path("/var/lib/clinx-rust")
    api = pwd.getpwnam("clinx-api")
    worker = pwd.getpwnam((root / "config/worker-user").read_text().strip())
    authority = pwd.getpwnam("clinx-authority")
    mcp_config = root / "config/mcp-runtime.json"
    compiled = subprocess.run([str(release / 'clinx-node'), '--compile-config', 'mcp',
                               str(root / 'config/bridge.toml')], check=True, capture_output=True, text=True)
    compiled_mcp = json.loads(compiled.stdout)
    if mcp_config.exists():
        if json.loads(mcp_config.read_text()) != compiled_mcp:
            raise RuntimeError('MCP_CONFIG_IDENTITY_MISMATCH')
    else:
        with mcp_config.open('x') as stream:
            json.dump(compiled_mcp, stream, indent=2)
            stream.write('\n')
        mcp_config.chmod(0o644)
    state = root / "api/node-centre"
    state.mkdir(mode=0o700, exist_ok=False)
    os.chown(state, api.pw_uid, api.pw_gid)
    copied = {}
    for name in ("identity.json", "peers.json", "sharing.json"):
        source = args.legacy_node_state / name
        target = state / name
        shutil.copyfile(source, target)
        os.chown(target, api.pw_uid, api.pw_gid)
        target.chmod(0o600)
        copied[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    common = f"""Environment=HOME={root}/api
Environment=PYTHONPATH={release}/control:{release}/deps
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=CLINX_AUTHORITY=rust
Environment=CLINX_RUST_SOCKET=/run/clinx-rust/authority.sock
Environment=CLINX_RUST_UID={authority.pw_uid}
User=clinx-api
Group=clinx-api
SupplementaryGroups=clinx-ipc clinx-provider-ipc
NoNewPrivileges=yes
CapabilityBoundingSet=
ProtectSystem=strict
ProtectHome=read-only
ProtectProc=invisible
ProcSubset=pid
PrivateDevices=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
BindReadOnlyPaths=/run/clinx-rust-provider/native:/tmp/codex-daemon-{worker.pw_uid}
ReadWritePaths={root}/api
InaccessiblePaths={root}/authority /home/{args.operator}/.codex /home/{args.operator}/.ssh /run/user/1001 -/run/docker.sock -/run/containerd
UMask=0077
KillMode=control-group
"""
    units = {
        "clinx-rust-mcp.socket": f"""[Unit]
Description=CLINX existing MCP transport socket for dedicated API identity
[Socket]
ListenStream=/run/clinx-rust-api/mcp.sock
Accept=yes
SocketUser={args.operator}
SocketGroup={pwd.getpwnam(args.operator).pw_gid}
SocketMode=0660
DirectoryMode=0711
RemoveOnStop=yes
""",
        "clinx-rust-mcp@.service": f"""[Unit]
Description=CLINX MCP API adapter
After=clinx-rust-authority.service
Requires=clinx-rust-authority.service
[Service]
{common}ExecStart={release}/clinx-mcp --config {mcp_config} --stdio
StandardInput=socket
StandardOutput=inherit
StandardError=journal
""",
        "clinx-rust-centre.service": f"""[Unit]
Description=CLINX Centre Rust projection (pre-cutover loopback)
After=clinx-rust-authority.service
Requires=clinx-rust-authority.service
[Service]
{common}ExecStart={release}/clinx-node-daemon --state {state} --node-id p620 serve --registry {state}/registry.sqlite3 --observations {state}/observations.sqlite3 --bind 127.0.0.1 --port 18774 --rust-projection --local-owner-host p620 --local-provider-config {root}/config/bridge.toml --stale-after 45
Restart=on-failure
RestartSec=3
""",
    }
    for name, value in units.items():
        target = Path("/etc/systemd/system") / name
        if target.exists():
            raise RuntimeError("CALLER_UNIT_EXISTS:" + name)
        target.write_text(value)
    tmpfiles = f"""d /run/clinx-rust 2750 clinx-authority clinx-ipc -
d /run/clinx-rust-provider 2750 {worker.pw_name} clinx-provider-ipc -
d /run/clinx-rust-provider/native 0700 {worker.pw_name} clinx-provider-ipc -
d /run/clinx-rust-api 0711 root root -
"""
    Path("/etc/tmpfiles.d/clinx-rust.conf").write_text(tmpfiles)
    # Do not chmod a running native socket directory; creation at boot is enough.
    Path("/run/clinx-rust-api").mkdir(mode=0o711, exist_ok=True)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    result = dict(
        status="STAGED_NOT_SELECTED",
        release=str(release),
        units=list(units),
        preserved_config_sha256=copied,
        legacy_databases_imported=False,
        centre_port=18774,
        existing_tunnel_launcher="UNCHANGED",
        ipc_group=grp.getgrnam("clinx-ipc").gr_gid,
    )
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
