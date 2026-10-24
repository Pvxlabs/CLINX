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
from tools.build.runtime_sources import source_paths


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
    # The new release contains no separate Codex Provider binary or home.
    allowed = {'control/' + str(path.relative_to(source)): path
               for path in source_paths(source, 'rust')}
    # Preserve only native dependencies from the old artifact. Python sources
    # always come from the current profile, even if an older release placed
    # retired modules outside control/. Never inherit an old business core.
    files = {name: digest for name, digest in manifest['files'].items()
             if name == 'host-worker' or name.startswith('deps/')}
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
    for name, attribute in [('clinx-runtime', 'runtime_binary'), ('clinx-mcp-relay', 'mcp_relay_binary'), ('clinx-native-transport', 'native_transport_binary')]:
        binary = getattr(args, attribute, None)
        if binary is None:
            raise SystemExit('TESTED_LAYOUT_BINARY_REQUIRED:' + name)
        binary = binary.resolve(strict=True)
        if not binary.is_file() or not binary.stat().st_mode & 0o111:
            raise SystemExit('RUNTIME_EXECUTABLE_REQUIRED')
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
                  base_release=str(base), execution_daemon='EXISTING_DESKTOP_ONLY', native_transport='RUST_NATIVE_V1',
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
