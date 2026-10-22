"""Package parser + paired P1 Tunnel/health using existing release primitives.

Offline packaging only. No authorization service, credential, database, service
action or production writer is included. The denylist also rejects retired
experimental modules if they are encountered in an older installed source.
"""
from __future__ import annotations

import argparse
import ast
import json
import shutil
from pathlib import Path

if __package__:
    from .build_tunnel_release import archive, encoded
    from .build_tunnel_release import build as build_tunnel
    from .tunnel_release_cas import sha, state
else:
    from build_tunnel_release import archive, encoded
    from build_tunnel_release import build as build_tunnel
    from tunnel_release_cas import sha, state

EXPERIMENTS = frozenset({
    'maintenance_authorization', 'maintenance_operator', 'maintenance_witness',
    'policy_maintenance',
})


def check_source(name, data):
    """Do not silently omit a module that the selected installed source needs."""
    if Path(name).stem in EXPERIMENTS:
        raise ValueError('EXPERIMENTAL_MODULE_IN_SELECTED_SOURCE: ' + name)
    for node in ast.walk(ast.parse(data, filename=name)):
        imports = []
        if isinstance(node, ast.Import):
            imports = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imports = [node.module or ''] + [alias.name for alias in node.names]
        if any(set(item.split('.')) & EXPERIMENTS for item in imports):
            raise ValueError('EXPERIMENTAL_IMPORT_IN_SELECTED_SOURCE: ' + name)


def build(repo, installed, output, runtime_manifest, guards, evidence, python, tunnel):
    repo, installed, output = (Path(p).absolute() for p in (repo, installed, output))
    if (not output.is_relative_to('/data/artifacts') or output.exists()
            or output.resolve() != output or installed.resolve() != installed):
        raise ValueError('NEW_ARTIFACT_OUTPUT_AND_EXACT_INSTALLED_SOURCE_REQUIRED')
    for p, digest in {**guards, **evidence}.items():
        if sha(Path(p).read_bytes()) != digest:
            raise ValueError('INPUT_HASH_DRIFT: ' + p)
    launcher = installed.parents[1] / 'launch-mcp'
    old = launcher.read_bytes()
    marker = ('repo_root=' + str(installed)).encode()
    py_marker = b'$repo_root/.venv-node/bin/python'
    if old.count(marker) != 1 or old.count(py_marker) != 1:
        raise ValueError('EXACT_INSTALLED_LAUNCHER_REQUIRED')
    base = {}
    contents = {}
    for path in sorted(installed.rglob('*.py')):
        relative = path.relative_to(installed)
        if any(part.startswith(('.', 'test', '__pycache__')) for part in relative.parts):
            continue
        base[str(relative)] = state(path)
        contents[str(relative)] = path.read_bytes()
    if 'mcp_server.py' not in contents or 'workspace_contract.py' not in contents:
        raise ValueError('INSTALLED_CONTROL_SOURCE_REQUIRED')
    contents['workspace_contract.py'] = (repo / 'workspace_contract.py').read_bytes()
    for name, data in contents.items():
        check_source(name, data)
    output.mkdir(mode=0o700, parents=True)
    tunnel_receipt = build_tunnel(
        repo, output / 'tunnel-package', Path.home(), python, tunnel, guards,
        evidence, runtime_manifest=runtime_manifest,
    )
    inputs = dict(base_source=str(installed), base=base, launcher_before=state(launcher),
                  parser_sha256=sha(contents['workspace_contract.py']),
                  builder_sha256=sha(Path(__file__).read_bytes()), tunnel=tunnel_receipt)
    release_id = 'forwarding-p1-' + sha(encoded(inputs))[:20]
    control_target = str(installed.parent / release_id / 'control')
    new = old.replace(marker, ('repo_root=' + control_target).encode()).replace(
        py_marker, str(installed / '.venv-node/bin/python').encode())
    release, rollback = output / 'release', output / 'rollback'
    release.mkdir()
    rollback.mkdir()
    shutil.copytree(output / 'tunnel-package/release', release / 'tunnel')
    shutil.copytree(output / 'tunnel-package/rollback-capsule', rollback / 'tunnel')
    for name, data in contents.items():
        path = release / 'control' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(base[name]['mode'])
    (release / 'launch-mcp').write_bytes(new)
    (release / 'launch-mcp').chmod(state(launcher)['mode'])
    for suffix, data in (('before', old), ('after', new)):
        path = rollback / ('loader.' + suffix)
        path.write_bytes(data)
        path.chmod(state(launcher)['mode'])
    tunnel_plan = json.loads((rollback / 'tunnel/plan.json').read_text())
    # The bundle records the current human instruction, not an invented grant.
    # Retain the old builder's frozen receipt as provenance, while retiring its
    # obsolete approval-platform and global-drain checklist in this new plan.
    tunnel_plan['production_authorization'] = 'DIRECT_USER_REQUEST_CURRENT_CHAT'
    tunnel_plan['preconditions'] = [
        'existing installer accepts exact paired scope',
        'fresh backup and exact before/after rollback',
        'service and advancing poll verification',
    ]
    (rollback / 'tunnel/plan.json').write_bytes(encoded(tunnel_plan))
    plan = dict(schema='CLINX_FORWARDING_P1_PACKAGE_V1', release_id=release_id,
                installs={'control': control_target, 'tunnel': tunnel_receipt['install_path']},
                changes={**tunnel_plan['changes'], str(launcher): dict(
                    before=state(launcher), after=state(release / 'launch-mcp'))},
                control_base_guards=base, base_source=str(installed),
                tunnel_plan_sha256=sha((rollback / 'tunnel/plan.json').read_bytes()),
                existing_installer_required=True, production_driver_included=False,
                never_restore=['database', 'execution facts', 'lease', 'Owner', 'Provider',
                               'fencing epoch', 'credentials', 'supervisor budget'])
    (rollback / 'plan.json').write_bytes(encoded(plan))
    manifest = dict(schema='CLINX_FORWARDING_P1_RELEASE_V1', release_id=release_id,
                    inputs=inputs, excluded_experiments=sorted(EXPERIMENTS),
                    rollback_plan_sha256=sha((rollback / 'plan.json').read_bytes()),
                    files={str(p.relative_to(release)): state(p)
                           for p in sorted(release.rglob('*')) if p.is_file()})
    (release / 'manifest.json').write_bytes(encoded(manifest))
    archive(release, output / 'candidate.tar.gz')
    archive(rollback, output / 'rollback.tar.gz')
    receipt = dict(release_id=release_id,
                   candidate_sha256=sha((output / 'candidate.tar.gz').read_bytes()),
                   rollback_sha256=sha((output / 'rollback.tar.gz').read_bytes()),
                   manifest_sha256=sha((release / 'manifest.json').read_bytes()),
                   plan_sha256=sha((rollback / 'plan.json').read_bytes()),
                   managed_paths=sorted(plan['changes']), production_changed=False)
    (output / 'receipt.json').write_bytes(encoded(receipt))
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('repo', 'installed', 'output', 'runtime-manifest', 'guards', 'evidence',
                 'python', 'tunnel'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.repo, args.installed, args.output, args.runtime_manifest,
                           json.loads(args.guards.read_text()), json.loads(args.evidence.read_text()),
                           args.python, args.tunnel), indent=2))
