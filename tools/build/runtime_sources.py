"""Build the exact source profiles declared in architecture/python-sources.json.

This is build tooling, not an application dependency. Runtime packages contain
qualified clinx modules; neither flat import aliases nor a checkout are needed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2]


def inventory(source: Path) -> list[dict]:
    data = json.loads((source / 'architecture/python-sources.json').read_text())
    if data.get('schema') != 1:
        raise ValueError('UNKNOWN_SOURCE_MANIFEST_SCHEMA')
    rows = data['modules']
    names = [row['path'] for row in rows]
    if len(names) != len(set(names)):
        raise ValueError('DUPLICATE_SOURCE_MODULE')
    for row in rows:
        path = Path(row['path'])
        if path.is_absolute() or '..' in path.parts or path.suffix != '.py':
            raise ValueError('INVALID_SOURCE_PATH')
        if row['role'] not in {'ACTIVE_RUNTIME', 'COMPAT', 'DEVOPS', 'QUALIFICATION', 'DEAD'}:
            raise ValueError('UNKNOWN_SOURCE_ROLE')
        if set(row['profiles']) - {'rust', 'monitor'}:
            raise ValueError('UNKNOWN_SOURCE_PROFILE')
        if row['profiles'] and (not row['path'].startswith('clinx/')
                or row['role'] not in {'ACTIVE_RUNTIME', 'COMPAT'}):
            raise ValueError('NON_RUNTIME_SOURCE_IN_PACKAGE:' + row['path'])
        if row['profiles'] and row['role'] == 'COMPAT':
            raise ValueError('LEGACY_CORE_IN_RUNTIME_PACKAGE:' + row['path'])
    return rows


def source_paths(source: Path, profile: str) -> list[Path]:
    if profile not in {'rust', 'monitor'}:
        raise ValueError('UNKNOWN_SOURCE_PROFILE')
    paths = [source / row['path'] for row in inventory(source) if profile in row['profiles']]
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise ValueError('RUNTIME_SOURCE_MISSING:' + str(path.relative_to(source)))
    return sorted(paths)


def data_paths(source: Path, profile: str) -> list[Path]:
    """Non-Python runtime data shares the same explicit release boundary."""
    if profile not in {'rust', 'monitor'}:
        raise ValueError('UNKNOWN_SOURCE_PROFILE')
    rows = json.loads((source / 'architecture/runtime-data.json').read_text())
    paths = []
    if len({row['path'] for row in rows}) != len(rows):
        raise ValueError('DUPLICATE_RUNTIME_DATA')
    for row in rows:
        if set(row['profiles']) - {'rust', 'monitor'}:
            raise ValueError('UNKNOWN_SOURCE_PROFILE')
        path = Path(row['path'])
        if path.is_absolute() or '..' in path.parts or path.suffix != '.json':
            raise ValueError('INVALID_RUNTIME_DATA_PATH')
        if profile in row['profiles']:
            target = source / path
            if not target.is_file() or target.is_symlink():
                raise ValueError('RUNTIME_DATA_MISSING')
            paths.append(target)
    return sorted(paths)


def copy_monitor_sources(source: Path, resources: Path) -> None:
    """Retained compatibility package fixture; current App uses native resources.

    Existing adapter contract qualifications still exercise this old layout.
    It is not the installed Monitor source closure.
    """
    paths = source_paths(source, 'monitor') + data_paths(source, 'monitor')
    resources.mkdir(parents=True, exist_ok=True)
    # Remove only old copied Python sources; keep the interpreter, Swift assets,
    # native pairing wheel and all other application resources.
    for path in resources.glob('*.py'):
        path.unlink()
    for name in ('clinx', 'Discovery', 'local_discovery', 'domain', 'shadow_ledger',
                 'runtime_control', 'provider_adapters'):
        target = resources / name
        if target.is_symlink():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
    hashes = {}
    for path in paths:
        relative = path.relative_to(source)
        target = resources / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        hashes[str(relative)] = hashlib.sha256(target.read_bytes()).hexdigest()
    (resources / 'python-sources.json').write_text(json.dumps(
        {'schema': 1, 'profile': 'monitor', 'node_network': 'RUST_NODE_NETWORK_V1',
         'observation_runtime': 'RUST_OBSERVATION_V1',
         'collector_runtime': 'RUST_COLLECTOR_V1',
         'client_runtime': 'RUST_CLIENT_RUNTIME_V1',
         'thin_runtime': 'RUST_THIN_RUNTIME_V1',
         'required_helpers': ['clinx-node', 'clinx-provider'], 'files': hashes}, indent=2) + '\n')


def copy_native_monitor_resources(source: Path, resources: Path) -> None:
    """Clean only known build-owned runtime paths; preserve unrelated assets."""
    resources.mkdir(parents=True, exist_ok=True)
    for path in resources.glob('*.py'):
        path.unlink()
    for name in ('clinx', 'Discovery', 'local_discovery', 'domain', 'shadow_ledger',
                 'runtime_control', 'provider_adapters', 'DiscoveryRuntime'):
        path = resources / name
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            if name == 'DiscoveryRuntime':
                # Previous builder made its private Python tree read-only.
                for directory in [path, *(p for p in path.rglob('*') if p.is_dir() and not p.is_symlink())]:
                    directory.chmod(directory.stat().st_mode | 0o700)
            shutil.rmtree(path)
    manifest = json.loads((source / 'architecture/native-runtime.json').read_text())
    (resources / 'python-sources.json').write_text(json.dumps(
        {'schema': 1, 'profile': 'monitor-native', 'files': {},
         'device_runtime': manifest['device_runtime'],
         'required_helpers': manifest['monitor_helpers']}, indent=2) + '\n')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--monitor-resources', type=Path, help='Compatibility qualification layout only')
    group.add_argument('--native-monitor-resources', type=Path, help='Current Swift/Rust application resources')
    args = parser.parse_args()
    if args.native_monitor_resources:
        copy_native_monitor_resources(SOURCE_ROOT, args.native_monitor_resources)
    else:
        copy_monitor_sources(SOURCE_ROOT, args.monitor_resources)


if __name__ == '__main__':
    main()
