"""Isolated release and durable identity acceptance for the Rust supervisor."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from clinx.transport.tunnel_reliability import Policy
from scripts.build_tunnel_release import build

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ['CLINX_TUNNEL_BINARY']).resolve()


def test_staged_release_is_reproducible_rust_only_runtime(tmp_path):
    tunnel = tmp_path / 'transport-binary'
    tunnel.write_text('# transport fixture, never launched\n')
    args = (ROOT, tmp_path / 'first', '/home/clinx-tunnel-fixture', Path(sys.executable).resolve(), tunnel, [], {})
    first = build(*args, supervisor_binary=BINARY)
    second = build(ROOT, tmp_path / 'second', *args[2:], supervisor_binary=BINARY)
    assert first == second
    for name in ('release.tar.gz', 'rollback-capsule.tar.gz'):
        assert (tmp_path / 'first' / name).read_bytes() == (tmp_path / 'second' / name).read_bytes()
    release = tmp_path / 'first/release'
    manifest = json.loads((release / 'manifest.json').read_text())
    assert manifest['status'] == 'NOT_DEPLOYED'
    assert 'tunnel_reliability.py' not in manifest['files']
    assert 'clinx-node' not in manifest['files']
    assert manifest['files']['clinx-tunnel']['sha256'] == hashlib.sha256(BINARY.read_bytes()).hexdigest()
    assert manifest['inputs']['supervisor_runtime']['contract'] == 'RUST_TUNNEL_SUPERVISOR_V1'
    for name, mode in [('clinx-tunnel.service', 'run'), ('clinx-tunnel-health.service', 'status')]:
        unit = (release / 'units' / name).read_text()
        start = next(line for line in unit.splitlines() if line.startswith('ExecStart='))
        assert '/clinx-tunnel ' + mode + ' --state-dir ' in start
        assert 'python' not in start and '@' not in start
    result = subprocess.run([str(release / 'clinx-tunnel'), 'status', '--state-dir', str(tmp_path / 'unused')],
                            env={'PATH': '/nonexistent'}, capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert json.loads(result.stdout)['currently_healthy'] is False


def test_native_launch_reuses_legacy_python_identity_and_budget(tmp_path):
    directory = tmp_path / 'state'
    directory.mkdir(mode=0o700)
    policy = Policy(interval=0.01, cooldown=0.01, max_restarts=2)
    command = ['/bin/true', 'Unicode-中😀']
    identity = hashlib.sha256(json.dumps([command, asdict(policy)], sort_keys=True).encode()).hexdigest()
    (directory / 'state.json').write_text(json.dumps(dict(identity=identity, launches=2,
        launch_sequence=7, reason='CHILD_EXITED', child_pid=None)))
    result = subprocess.run([str(BINARY), 'run', '--state-dir', str(directory), '--interval', '0.01',
        '--cooldown', '0.01', '--max-restarts', '2', '--', *command], capture_output=True, text=True, timeout=5)
    assert result.returncode == 78
    receipt = json.loads((directory / 'state.json').read_text())
    assert receipt['identity'] == identity
    assert receipt['launches'] == 3 and receipt['launch_sequence'] == 8
    assert receipt['reason'] == 'RECOVERY_BUDGET_EXHAUSTED'
