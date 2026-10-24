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
        if 'rust' in row['profiles'] and row['role'] == 'COMPAT':
            raise ValueError('LEGACY_CORE_IN_RUST_PACKAGE:' + row['path'])
    return rows


def source_paths(source: Path, profile: str) -> list[Path]:
    if profile not in {'rust', 'monitor'}:
        raise ValueError('UNKNOWN_SOURCE_PROFILE')
    paths = [source / row['path'] for row in inventory(source) if profile in row['profiles']]
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise ValueError('RUNTIME_SOURCE_MISSING:' + str(path.relative_to(source)))
    return sorted(paths)


def copy_monitor_sources(source: Path, resources: Path) -> None:
    paths = source_paths(source, 'monitor')
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
        {'schema': 1, 'profile': 'monitor', 'files': hashes}, indent=2) + '\n')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--monitor-resources', type=Path, required=True)
    args = parser.parse_args()
    copy_monitor_sources(SOURCE_ROOT, args.monitor_resources)


if __name__ == '__main__':
    main()
