"""Release inheritance and incremental App copies must remove retired cores."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

from tools.build.runtime_sources import SOURCE_ROOT, copy_monitor_sources, data_paths, source_paths


def test_release_cannot_inherit_retired_python_from_any_old_root(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('stage_release_fixture', SOURCE_ROOT/'scripts/stage_rust_release.py')
    stage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stage)
    root = tmp_path/'releases'
    base = root/'base'
    base.mkdir(parents=True)
    previous = {'host-worker': b'native helper', 'deps/keep.dat': b'native dependency',
        'bridge.py': b'old root core', 'clinx/compat/task_registry.py': b'old root package',
        'control/clinx/compat/m9_integration.py': b'old qualified core',
        'control/provider_adapters/codex.py': b'old adapter', 'provider/bin/codex': b'old provider'}
    for name, data in previous.items():
        path = base/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o644)
    (base/'manifest.json').write_text(json.dumps(dict(files={
        p: hashlib.sha256(data).hexdigest() for p, data in previous.items()})))
    binaries = {}
    for key in ('runtime', 'mcp_relay', 'native_transport', 'provider', 'mcp', 'node'):
        path = tmp_path/key
        path.write_bytes(key.encode())
        path.chmod(0o755)
        binaries[key+'_binary'] = path
    # This packaging unit fixture uses synthetic executables. Stub only their
    # protocol probe; binary/protocol execution is covered by the Rust fixtures.
    real_run = stage.subprocess.run
    def fixture_run(command, *args, **kwargs):
        if command == [str(binaries['node_binary']), '--network-contract']:
            return stage.subprocess.CompletedProcess(command, 0, stdout='RUST_NODE_NETWORK_V1\n', stderr='')
        if command == [str(binaries['node_binary']), '--observation-contract']:
            return stage.subprocess.CompletedProcess(command, 0, stdout='RUST_OBSERVATION_V1\n', stderr='')
        return real_run(command, *args, **kwargs)
    monkeypatch.setattr(stage.subprocess, 'run', fixture_run)
    real_stat = Path.stat
    def fixture_stat(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        return SimpleNamespace(st_uid=0, st_gid=0, st_mode=info.st_mode) if path == root or path.is_relative_to(base) else info
    monkeypatch.setattr(stage, 'Path', lambda value: root if str(value) == '/var/lib/clinx-rust/releases' else Path(value))
    monkeypatch.setattr(Path, 'stat', fixture_stat)
    monkeypatch.setattr(os, 'chown', lambda *args: None)
    import grp
    monkeypatch.setattr(grp, 'getgrnam', lambda _: SimpleNamespace(gr_gid=os.getgid()))
    args = SimpleNamespace(desktop_base_release=base, replace_source=[], output=tmp_path/'receipt.json', **binaries)
    stage.stage_desktop(args, SOURCE_ROOT)
    receipt = json.loads(args.output.read_text())
    release = Path(receipt['release'])
    assert receipt['node_api'] == 'RUST_NODE_API_V1'
    assert (release/'clinx-node').read_bytes() == b'node'
    assert receipt['mcp_runtime'] == 'RUST_MCP_V1'
    assert (release/'clinx-mcp').read_bytes() == b'mcp'
    assert (release/'control/clinx/mcp/registry.json').is_file()
    assert receipt['native_transport'] == 'RUST_NATIVE_V1'
    assert set(receipt['retired_files']) == set(previous)-{'host-worker', 'deps/keep.dat'}
    assert not (release/'clinx').exists() and not (release/'bridge.py').exists()
    assert not (release/'control/clinx/compat').exists()
    for name, digest in receipt['files'].items():
        assert hashlib.sha256((release/name).read_bytes()).hexdigest() == digest
    assert (release/'clinx-native-transport').read_bytes() == b'native_transport'
    assert (release/'clinx-provider').read_bytes() == b'provider'
    assert receipt['provider_exchange'] == receipt['workspace_observer'] == 'RUST_PROVIDER_V1'
    assert not (release/'control/clinx/provider/rust_workspace_probe.py').exists()


def test_incremental_monitor_copy_removes_legacy_and_matches_declared_closure(tmp_path):
    resources = tmp_path/'Resources'
    for old in ('bridge.py', 'clinx/compat/task_registry.py', 'domain/event.py', 'Discovery/bridge.py'):
        path = resources/old
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('retired')
    runtime = resources/'DiscoveryRuntime/keep'
    runtime.parent.mkdir(parents=True)
    runtime.write_text('interpreter')
    copy_monitor_sources(SOURCE_ROOT, resources)
    expected = {str(path.relative_to(SOURCE_ROOT)) for path in source_paths(SOURCE_ROOT, 'monitor') + data_paths(SOURCE_ROOT, 'monitor')}
    assert set(json.loads((resources/'python-sources.json').read_text())['files']) == expected
    assert not (resources/'clinx/compat').exists()
    assert not (resources/'bridge.py').exists() and not (resources/'Discovery').exists()
    assert runtime.read_text() == 'interpreter'


def test_dependency_overlay_cannot_smuggle_retired_host_or_second_clinx_package():
    spec = importlib.util.spec_from_file_location('stage_host_dependency_fixture', SOURCE_ROOT/'scripts/stage_rust_release.py')
    stage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stage)
    files = {name: 'digest' for name in (
        'host-worker', 'deps/keep.dat', 'deps/httpx/client.py', 'deps/httpx/__init__.py',
        'deps/httpx/_model.py',
        'deps/clinx/host/host_executor.py', 'deps/site-packages/clinx/transport/rust_transport.py',
        'deps/site-packages/task_registry.py', 'deps/__pycache__/bridge.cpython-313.pyc', 'deps/rust_workspace_probe.py')}
    assert set(stage.inherited_native_files(files, SOURCE_ROOT)) == {
        'host-worker', 'deps/keep.dat', 'deps/httpx/client.py', 'deps/httpx/__init__.py',
        'deps/httpx/_model.py'}
