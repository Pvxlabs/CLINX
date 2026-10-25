"""Release inheritance and incremental App copies must remove retired cores."""
import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.build.runtime_sources import SOURCE_ROOT, copy_monitor_sources, data_paths, source_paths


def test_current_monitor_builder_uses_native_tool_without_python(tmp_path):
    binary = Path(os.environ.get('CLINX_BUILD_BINARY') or
                  str(Path(os.environ['CLINX_TEST_RUST_BIN_DIR']) / 'clinx-build'))
    resources = tmp_path/'App/Contents/Resources'
    resources.mkdir(parents=True)
    (resources/'bridge.py').write_text('retired')
    (resources/'AppIcon.icns').write_bytes(b'swift asset')
    # No interpreter or command lookup is available to the actual cleanup tool.
    result = subprocess.run([str(binary), 'native-monitor-resources', str(SOURCE_ROOT), str(resources)],
                            env={'PATH': '/nonexistent'}, text=True, capture_output=True, check=True, timeout=10)
    assert json.loads(result.stdout)['contract'] == 'RUST_MONITOR_BUILD_V1'
    assert json.loads((resources/'python-sources.json').read_text())['files'] == {}
    assert not (resources/'bridge.py').exists()
    assert (resources/'AppIcon.icns').read_bytes() == b'swift asset'
    script = (SOURCE_ROOT/'MonitorApp/Scripts/build-app.sh').read_text()
    assert 'python3' not in script and 'runtime_sources.py' not in script
    assert 'clinx-build" native-monitor-resources' in script
    assert 'for helper in clinx-provider clinx-node clinx-node-daemon;' in script


def test_release_cannot_inherit_retired_python_from_any_old_root(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('stage_release_fixture', SOURCE_ROOT/'scripts/stage_rust_release.py')
    stage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stage)
    root = tmp_path/'releases'
    base = root/'base'
    base.mkdir(parents=True)
    previous = {'host-worker': b'native helper', 'deps/keep.dat': b'native dependency',
        'clinx-build': b'development only', 'clinx-maintenance': b'operator tool only',
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
    for key in ('runtime', 'mcp_relay', 'native_transport', 'provider', 'mcp', 'node', 'node_daemon', 'observer', 'device'):
        path = tmp_path/key
        path.write_bytes(key.encode())
        path.chmod(0o755)
        binaries[key+'_binary'] = path
    # This packaging unit fixture uses synthetic executables. Stub only their
    # protocol probe; binary/protocol execution is covered by the Rust fixtures.
    real_run = stage.subprocess.run
    compiler_marker = 'RUST_CONFIG_COMPILER_V1\n'
    compiler_probes = []
    def fixture_run(command, *args, **kwargs):
        if command == [str(binaries['node_binary']), '--network-contract']:
            return stage.subprocess.CompletedProcess(command, 0, stdout='RUST_NODE_NETWORK_V1\n', stderr='')
        if command == [str(binaries['node_binary']), '--config-compile-contract']:
            compiler_probes.append(command)
            return stage.subprocess.CompletedProcess(command, 0, stdout=compiler_marker, stderr='')
        if command == [str(binaries['node_binary']), '--observation-contract']:
            return stage.subprocess.CompletedProcess(command, 0, stdout='RUST_OBSERVATION_V1\n', stderr='')
        if command == [str(binaries['node_binary']), '--collector-contract']:
            return stage.subprocess.CompletedProcess(command, 0, stdout='RUST_COLLECTOR_V1\n', stderr='')
        if command == [str(binaries['node_binary']), '--client-runtime-contract']:
            return stage.subprocess.CompletedProcess(command, 0, stdout='RUST_CLIENT_RUNTIME_V1\n', stderr='')
        if command == [str(binaries['node_binary']), '--thin-runtime-contract']:
            return stage.subprocess.CompletedProcess(command, 0, stdout='RUST_THIN_RUNTIME_V1\n', stderr='')
        if command == [str(binaries['node_binary']), '--device-runtime-contract']:
            return stage.subprocess.CompletedProcess(command, 0, stdout='RUST_DEVICE_RUNTIME_V1\n', stderr='')
        if command == [str(binaries['node_daemon_binary']), '--daemon-contract']:
            return stage.subprocess.CompletedProcess(command, 0, stdout='RUST_NODE_DAEMON_V1\n', stderr='')
        for key, option, marker in (
            ('provider', '--desktop-socket-contract', 'RUST_DESKTOP_SOCKET_V1'),
            ('mcp', '--observation-runtime-contract', 'RUST_OBSERVATION_DIRECTORY_V1'),
            ('observer', '--observer-contract', 'RUST_OBSERVER_V1'),
            ('device', '--device-cli-contract', 'RUST_DEVICE_CLI_V1'),
        ):
            if command == [str(binaries[key + '_binary']), option]:
                return stage.subprocess.CompletedProcess(command, 0, stdout=marker + '\n', stderr='')
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
    # Installation now uses the native maintenance contract. This replaces
    # the old source-presence requirement with absence of all Python payload.
    assert receipt['desktop_socket_maintenance'] == 'RUST_DESKTOP_SOCKET_V1'
    assert receipt['python_sources'] == receipt['python_dependencies'] == []
    assert not list(release.rglob('*.py')) and not (release/'deps').exists()
    assert not (release/'clinx-build').exists() and not (release/'clinx-maintenance').exists()
    assert not (release/'control/clinx/provider/desktop_socket.py').exists()
    assert not (release/'control/clinx/monitor').exists()
    assert not (release/'control/clinx/discovery').exists()
    assert (release/'clinx-observer').read_bytes() == b'observer'
    assert (release/'clinx-device').read_bytes() == b'device'
    assert receipt['node_api'] == 'RUST_NODE_API_V1'
    assert receipt['config_compiler'] == 'RUST_CONFIG_COMPILER_V1'
    assert compiler_probes == [[str(binaries['node_binary']), '--config-compile-contract']]
    assert (release/'clinx-node').read_bytes() == b'node'
    assert receipt['mcp_runtime'] == 'RUST_MCP_V1'
    assert (release/'clinx-mcp').read_bytes() == b'mcp'
    assert (release/'control/clinx/mcp/registry.json').is_file()
    assert receipt['native_transport'] == 'RUST_NATIVE_V1'
    assert set(receipt['retired_files']) == set(previous)-{'host-worker'}
    assert not (release/'clinx').exists() and not (release/'bridge.py').exists()
    assert not (release/'control/clinx/compat').exists()
    for name, digest in receipt['files'].items():
        assert hashlib.sha256((release/name).read_bytes()).hexdigest() == digest
    assert (release/'clinx-native-transport').read_bytes() == b'native_transport'
    assert (release/'clinx-provider').read_bytes() == b'provider'
    assert receipt['provider_exchange'] == receipt['workspace_observer'] == 'RUST_PROVIDER_V1'
    assert not (release/'control/clinx/provider/rust_workspace_probe.py').exists()
    # Old Node binaries can exit successfully with empty stdout for unknown
    # options. They must not acquire the new release capability accidentally.
    compiler_marker = ''
    args.output = tmp_path/'rejected-receipt.json'
    with pytest.raises(SystemExit, match='QUALIFIED_RUST_CONFIG_COMPILER_REQUIRED'):
        stage.stage_desktop(args, SOURCE_ROOT)
    assert not args.output.exists()


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
        'deps/cryptography/x.py', 'deps/cryptography-50.0.2.dist-info/METADATA',
        'deps/_clinx_opaque/__init__.py', 'deps/_cffi_backend.cpython-312-x86_64-linux-gnu.so',
        'deps/zeroconf/abc.py', 'deps/bin/jsonschema', 'deps/bin/cffi-gen-src', 'deps/.lock',
        'host-worker', 'deps/keep.dat', 'deps/httpx/client.py', 'deps/httpx/__init__.py',
        'deps/httpx/_model.py',
        'deps/clinx/host/host_executor.py', 'deps/site-packages/clinx/transport/rust_transport.py',
        'deps/site-packages/task_registry.py', 'deps/__pycache__/bridge.cpython-313.pyc', 'deps/rust_workspace_probe.py')}
    assert set(stage.inherited_native_files(files, SOURCE_ROOT)) == {'host-worker'}
