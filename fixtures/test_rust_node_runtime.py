"""Rust Helper request process, package closure and lost callback evidence."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from clinx.authority.request_pipe import api_config
from clinx.authority.rust_authority import Commands, RustAuthorityError
from clinx.nodes.rust_node_adapter import RustNodeExecutionAdapter
from tools.build.runtime_sources import copy_monitor_sources, data_paths, source_paths


def test_normal_helper_never_constructs_python_integration_and_preserves_unknown_key(monkeypatch):
    from clinx.authority import rust_integration
    monkeypatch.setattr(rust_integration, 'RustIntegration', lambda *a, **k: pytest.fail('Python business API constructed'))
    sent = []
    def lost(operation, payload, *, request_id):
        sent.append((operation, payload, request_id))
        raise RustAuthorityError('DELIVERY_UNKNOWN')
    cfg = SimpleNamespace(runtime_host='air', projects=[])
    helper = RustNodeExecutionAdapter.from_config(cfg, Commands(SimpleNamespace(request=lost)), 'air')
    with pytest.raises(RustAuthorityError, match='DELIVERY_UNKNOWN') as error:
        helper.prepare(project='project', task_action='create', prompt='Exact input')
    assert len(sent) == 1
    assert sent[0][0] == 'business.prepare'
    assert len(sent[0][2]) == 32 and error.value.operation_id == sent[0][2]
    assert sent[0][1]['prompt'] == 'Exact input'


def test_route_serialization_deduplicates_host_aliases():
    cfg = SimpleNamespace(runtime_host='air', workspaces=[SimpleNamespace(alias='AIR', host='air', codex_host_ids=['desktop-air'])])
    routes = api_config(cfg)
    assert routes['host_aliases']['air'] == ['air']
    assert routes['codex_host_ids'] == {'desktop-air': ['air']}


@pytest.mark.parametrize('profile', ['rust', 'monitor'])
def test_packaged_node_calls_rust_without_checkout_or_path(tmp_path, profile):
    source = Path(__file__).resolve().parents[1]
    binaries = Path(os.environ['CLINX_TEST_RUST_BIN_DIR'])
    root = tmp_path/'release'
    if profile == 'monitor':
        package = root/'Contents/Resources'
        copy_monitor_sources(source, package)
        binary = root/'Contents/Helpers/clinx-node'
    else:
        package = root/'control'
        for p in source_paths(source, profile) + data_paths(source, profile):
            target = package/p.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, target)
        binary = root/'clinx-node'
    binary.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(binaries/'clinx-node', binary)
    binary.chmod(0o755)
    script = '''
import sys,json
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
from clinx.authority.rust_integration import RustIntegration
from clinx.authority.request_pipe import binary
api=RustIntegration(SimpleNamespace(runtime_host='air'),SimpleNamespace())
print(json.dumps(dict(binary=str(binary()),result=api.get_capabilities())))
'''
    env = {k:v for k,v in os.environ.items() if k in ('LANG','HOME','PATH')}
    result = subprocess.run([os.sys.executable, '-I', '-B', '-c', script, str(package)],
        cwd=root, env=env, text=True, capture_output=True, timeout=10, check=True)
    receipt = json.loads(result.stdout)
    assert receipt['binary'] == str(binary)
    assert receipt['result']['authority'] == 'RUST'
    assert receipt['result']['host_tools'] is True
    assert hashlib.sha256(binary.read_bytes()).digest() == hashlib.sha256((binaries/'clinx-node').read_bytes()).digest()
