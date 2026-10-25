#!/usr/bin/env python3
"""Stage an immutable runtime release. Does not select or stop production."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.build.runtime_sources import data_paths, source_paths


def inherited_native_files(files, source):
    """Only the existing privileged native launcher survives the old release.

    All runtime binaries/data are supplied explicitly. Native Desktop socket
    maintenance removes the final Python dependency, so deps overlays have no
    current consumer and must not carry retired modules or wheels forward.
    """
    return {name: digest for name, digest in files.items() if name == 'host-worker'}


def stage_desktop(args, source):
    """Package the declared adapter sources; preserve installed native dependencies."""
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
    # No private Codex daemon/home is introduced; Provider uses the existing Desktop.
    if source_paths(source, 'rust'):
        raise SystemExit('PYTHON_FREE_RUST_RELEASE_REQUIRED')
    allowed = {'control/' + str(path.relative_to(source)): path
               for path in source_paths(source, 'rust') + data_paths(source, 'rust')}
    # Preserve only native dependencies from the old artifact. Python sources
    # always come from the current profile, even if an older release placed
    # retired modules outside control/. Never inherit an old business core.
    files = inherited_native_files(manifest['files'], source)
    # Package a coherent source closure, including the newly separated shared
    # contracts. Mixing old adapter imports with a pruned package is invalid.
    replacements = {name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for name, path in allowed.items()}
    for name in args.replace_source:
        path = source / name
        if 'control/' + name not in allowed or path.is_symlink():
            raise SystemExit('DECLARED_RUNTIME_SOURCE_REQUIRED')
        replacements['control/' + name] = hashlib.sha256(path.read_bytes()).hexdigest()
    files.update(replacements)
    binaries = {}
    for name, attribute in [('clinx-runtime', 'runtime_binary'), ('clinx-mcp-relay', 'mcp_relay_binary'), ('clinx-native-transport', 'native_transport_binary'), ('clinx-provider', 'provider_binary'), ('clinx-mcp', 'mcp_binary'), ('clinx-node', 'node_binary'), ('clinx-node-daemon', 'node_daemon_binary'), ('clinx-observer', 'observer_binary'), ('clinx-device', 'device_binary')]:
        binary = getattr(args, attribute, None)
        if binary is None:
            raise SystemExit('TESTED_LAYOUT_BINARY_REQUIRED:' + name)
        binary = binary.resolve(strict=True)
        if not binary.is_file() or not binary.stat().st_mode & 0o111:
            raise SystemExit('RUNTIME_EXECUTABLE_REQUIRED')
        if name == 'clinx-node':
            try:
                contract = subprocess.run([str(binary), '--network-contract'], input='', text=True,
                    capture_output=True, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                raise SystemExit('QUALIFIED_RUST_NODE_NETWORK_REQUIRED') from None
            if contract.returncode or contract.stdout.strip() != 'RUST_NODE_NETWORK_V1':
                raise SystemExit('QUALIFIED_RUST_NODE_NETWORK_REQUIRED')
            try:
                observation = subprocess.run([str(binary), '--observation-contract'], input='', text=True,
                    capture_output=True, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                raise SystemExit('QUALIFIED_RUST_OBSERVATION_REQUIRED') from None
            if observation.returncode or observation.stdout.strip() != 'RUST_OBSERVATION_V1':
                raise SystemExit('QUALIFIED_RUST_OBSERVATION_REQUIRED')
            try:
                collector = subprocess.run([str(binary), '--collector-contract'], input='', text=True,
                    capture_output=True, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                raise SystemExit('QUALIFIED_RUST_COLLECTOR_REQUIRED') from None
            if collector.returncode or collector.stdout.strip() != 'RUST_COLLECTOR_V1':
                raise SystemExit('QUALIFIED_RUST_COLLECTOR_REQUIRED')

            try:
                client = subprocess.run([str(binary), '--client-runtime-contract'], input='', text=True,
                    capture_output=True, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                raise SystemExit('QUALIFIED_RUST_CLIENT_RUNTIME_REQUIRED') from None
            if client.returncode or client.stdout.strip() != 'RUST_CLIENT_RUNTIME_V1':
                raise SystemExit('QUALIFIED_RUST_CLIENT_RUNTIME_REQUIRED')
            try:
                thin = subprocess.run([str(binary), '--thin-runtime-contract'], input='', text=True,
                    capture_output=True, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                raise SystemExit('QUALIFIED_RUST_THIN_RUNTIME_REQUIRED') from None
            if thin.returncode or thin.stdout.strip() != 'RUST_THIN_RUNTIME_V1':
                raise SystemExit('QUALIFIED_RUST_THIN_RUNTIME_REQUIRED')

        if name in {'clinx-node', 'clinx-node-daemon'}:
            option, marker = ('--device-runtime-contract', 'RUST_DEVICE_RUNTIME_V1') if name == 'clinx-node' else ('--daemon-contract', 'RUST_NODE_DAEMON_V1')
            try:
                result = subprocess.run([str(binary), option], input='', text=True, capture_output=True, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                raise SystemExit('QUALIFIED_RUST_DEVICE_RUNTIME_REQUIRED') from None
            if result.returncode or result.stdout.strip() != marker:
                raise SystemExit('QUALIFIED_RUST_DEVICE_RUNTIME_REQUIRED')
        native_contracts = {
            'clinx-provider': ('--desktop-socket-contract', 'RUST_DESKTOP_SOCKET_V1'),
            'clinx-mcp': ('--observation-runtime-contract', 'RUST_OBSERVATION_DIRECTORY_V1'),
            'clinx-observer': ('--observer-contract', 'RUST_OBSERVER_V1'),
            'clinx-device': ('--device-cli-contract', 'RUST_DEVICE_CLI_V1'),
        }
        if name in native_contracts:
            option, marker = native_contracts[name]
            try:
                result = subprocess.run([str(binary), option], input='', text=True,
                    capture_output=True, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                raise SystemExit('QUALIFIED_NATIVE_ENTRYPOINT_REQUIRED:' + name) from None
            if result.returncode or result.stdout.strip() != marker:
                raise SystemExit('QUALIFIED_NATIVE_ENTRYPOINT_REQUIRED:' + name)
        binaries[name] = binary
        files[name] = hashlib.sha256(binary.read_bytes()).hexdigest()
    version = 'rust-' + hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:20]
    release = base.parent / version
    release.mkdir(mode=0o755, exist_ok=False)
    for name, digest in files.items():
        target = release / name
        target.parent.mkdir(parents=True, exist_ok=True)
        original = (binaries[name] if name in binaries else
                    allowed[name] if name in replacements else base / name)
        shutil.copyfile(original, target)
        target.chmod(0o755 if original.stat().st_mode & 0o111 else 0o644)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise SystemExit('STAGED_CONTENT_MISMATCH:' + name)
    import grp
    os.chown(release / 'host-worker', 0, grp.getgrnam('clinx-transport').gr_gid)
    (release / 'host-worker').chmod(0o4750)
    result = dict(version=version, source_cwd=str(source),
                  head=subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip(),
                  base_release=str(base), execution_daemon='EXISTING_DESKTOP_ONLY', native_transport='RUST_NATIVE_V1', host_transport='RUST_HOST_V1', mcp_runtime='RUST_MCP_V1', node_api='RUST_NODE_API_V1', node_network='RUST_NODE_NETWORK_V1', observation_runtime='RUST_OBSERVATION_V1', collector_runtime='RUST_COLLECTOR_V1', client_runtime='RUST_CLIENT_RUNTIME_V1', thin_runtime='RUST_THIN_RUNTIME_V1', device_runtime='RUST_DEVICE_RUNTIME_V1', node_daemon='RUST_NODE_DAEMON_V1', provider_exchange='RUST_PROVIDER_V1', workspace_observer='RUST_PROVIDER_V1',
                  observation_directory='RUST_OBSERVATION_DIRECTORY_V1', observer_runtime='RUST_OBSERVER_V1', device_cli='RUST_DEVICE_CLI_V1',
                  desktop_socket_maintenance='RUST_DESKTOP_SOCKET_V1', python_sources=[], python_dependencies=[],
                  source_replacements=replacements, runtime_source_profile='rust', source_layout='qualified-clinx-v1',
                  retired_files=sorted(set(manifest['files']) - set(files)),
                  files=files, status='STAGED_NOT_SELECTED')
    (release / 'manifest.json').write_text(json.dumps(result, indent=2) + '\n')
    args.output.write_text(json.dumps(dict(release=str(release), **result), indent=2) + '\n')
    print(json.dumps(dict(version=version, release=str(release), status='STAGED_NOT_SELECTED')))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--source", type=Path, help="Exact committed checkout to package; defaults to this repository")
    p.add_argument('--desktop-base-release', type=Path, required=True)
    p.add_argument('--runtime-binary', type=Path, required=True, help='Tested authority binary matching the package layout')
    p.add_argument('--native-transport-binary', type=Path, required=True, help='Tested Rust native execution/Provider worker')
    p.add_argument('--provider-binary', type=Path, required=True, help='Tested Rust Node Provider exchange and workspace observer')
    p.add_argument('--observer-binary', type=Path, required=True, help='Tested native Observer HTTP service')
    p.add_argument('--device-binary', type=Path, required=True, help='Tested native devices/pair/serve CLI')
    p.add_argument('--node-daemon-binary', type=Path, required=True, help='Tested Rust Helper/Centre assembly binary')
    p.add_argument('--node-binary', type=Path, required=True, help='Tested Rust Node selectors, handoff and execution API')
    p.add_argument('--mcp-binary', type=Path, required=True, help='Tested Rust business MCP server')
    p.add_argument('--mcp-relay-binary', type=Path, required=True, help='Tested Rust stdio relay binary')
    p.add_argument('--replace-source', action='append', default=[],
                   help='Compatibility assertion that a named source belongs to the packaged profile; all adapters are refreshed')
    args = p.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("ADMINISTRATOR_REQUIRED")
    source = (args.source or Path(__file__).resolve().parents[1]).resolve(strict=True)
    return stage_desktop(args, source)


if __name__ == "__main__":
    main()
