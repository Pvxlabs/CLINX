"""Qualify the generation fence on pinned real Bridge source, offline only.

No runtime binary is built or installed. No CLINX database/profile is loaded.
Only fake JSON-RPC pipes are used. Source archives, toolchain and caches are
owned by the new output directory and removed after qualification.
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

from build_transport_runtime import COMMIT, GO_SHA256, SOURCE_SHA256, extract, remove_build_state


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def qualify(source_archive, go_archive, output):
    repo = Path(__file__).resolve().parent.parent
    output = Path(output).absolute()
    if (not output.is_relative_to('/data/artifacts') or output.exists()
            or output.resolve() != output):
        raise ValueError('NEW_TASK_OWNED_OUTPUT_REQUIRED')
    output.mkdir(mode=0o700, parents=True)
    transient = output / 'transient'
    transient.mkdir()
    receipt = dict(schema='CLINX_BRIDGE_TRANSPORT_CONTROL_QUALIFICATION_V1',
                   state='FAILED', deployed=False, production_mutations=0,
                   current_old_generation_qualification='NOT_POSSIBLE_RETROACTIVELY',
                   activation_authority='NOT_GRANTED', runtime_binary_built=False,
                   upstream_commit=COMMIT, upstream_archive_sha256=SOURCE_SHA256,
                   go_archive_sha256=GO_SHA256)
    inputs = ['fixtures/bridge_transport_control.go',
              'fixtures/bridge_transport_control_test.go',
              'fixtures/bridge_transport_control.patch',
              'scripts/qualify_bridge_transport.py', 'scripts/build_transport_runtime.py']
    try:
        extract(source_archive, transient, SOURCE_SHA256)
        extract(go_archive, transient, GO_SHA256)
        source = transient / ('tunnel-client-' + COMMIT)
        for name in inputs:
            target = output / name
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(repo / name, target)
        receipt['inputs'] = {name: sha(output / name) for name in inputs}
        # A second reconstruction never accepts fuzz or positional offset.
        patch = subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i',
                                str(output / inputs[2])], cwd=source,
                               capture_output=True, timeout=10, check=True)
        patch_text = (patch.stdout + patch.stderr).decode()
        (output / 'patch.log').write_text(patch_text)
        if 'offset' in patch_text or 'fuzz' in patch_text:
            raise ValueError('PATCH_NOT_EXACT')
        for name in inputs[:2]:
            shutil.copyfile(output / name, source / 'pkg/codexappserver' /
                            Path(name).name.replace('bridge_', '', 1))
        for directory in ('home', 'tmp', 'gocache', 'gopath'):
            (transient / directory).mkdir()
        go = transient / 'go/bin/go'
        env = dict(PATH=str(go.parent) + ':/usr/bin:/bin', HOME=str(transient / 'home'),
                   TMPDIR=str(transient / 'tmp'), GOTOOLCHAIN='local', CGO_ENABLED='1',
                   GOOS='linux', GOARCH='amd64', GOMAXPROCS='2',
                   GOCACHE=str(transient / 'gocache'), GOPATH=str(transient / 'gopath'))
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY',
                    'http_proxy', 'https_proxy', 'no_proxy'):
            if key in os.environ:
                env[key] = os.environ[key]
        version = subprocess.run([str(go), 'version'], env=env, capture_output=True,
                                 timeout=10, check=True).stdout.decode().strip()
        if version != 'go version go1.27.3 linux/amd64':
            raise ValueError('PINNED_GO_VERSION_MISMATCH')
        args = [str(go), 'test', '-mod=readonly', '-race', '-count=1', '-v',
                '-timeout=45s', '-run', '^TestBridgeControl', './pkg/codexappserver']
        with (output / 'tests.log').open('wb') as log:
            subprocess.run(args, cwd=source, env=env, stdout=log,
                           stderr=subprocess.STDOUT, timeout=300, check=True)
        receipt.update(state='PASS_ISOLATED_PROTOCOL_ONLY', toolchain=version,
                       command=args[1:], test_log_sha256=sha(output / 'tests.log'),
                       patched_bridge_sha256=sha(source / 'pkg/codexappserver/bridge.go'))
        with tarfile.open(output / 'source-overlay.tar.gz', 'w:gz') as tar:
            for name in inputs:
                tar.add(output / name, arcname=name)
        receipt['source_overlay_sha256'] = sha(output / 'source-overlay.tar.gz')
        return receipt
    finally:
        remove_build_state(transient)
        receipt['transient_removed'] = not transient.exists()
        (output / 'manifest.json').write_text(json.dumps(receipt, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-archive', type=Path, required=True)
    parser.add_argument('--go-archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(qualify(args.source_archive, args.go_archive, args.output), indent=2))


if __name__ == '__main__':
    main()
