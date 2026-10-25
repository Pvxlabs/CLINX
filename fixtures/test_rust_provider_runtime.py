"""Packaged Python relay/import boundary; full Provider qualification is Rust.

Metadata, UID, Authority and paired mTLS behavior is covered by
kernel/tests/provider_runtime_qualification.rs using private real IO."""

from __future__ import annotations
import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from pathlib import Path
import pytest
from fixtures.test_native_transport_binary import read_exact, read_message, send_message


@pytest.fixture
def binary():
    path = Path(os.environ['CLINX_TEST_RUST_BIN_DIR']) / 'clinx-provider'
    assert path.is_file() and os.access(path, os.X_OK)
    return path


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / 'repo'
    subprocess.run(['git', 'init', '-q', '--initial-branch=main', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c',
                    'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-qm', 'fixture'], check=True)
    return repo


@pytest.fixture
def ipc(tmp_path):
    root = Path(tempfile.mkdtemp(prefix='i-', dir=tmp_path.parent))
    yield root
    shutil.rmtree(root)


class MetadataProvider:
    """Only command/exec is implemented; any thread/turn mutation is a failure."""
    def __init__(self, path, binary):
        self.path, self.binary = path, binary
        self.calls, self.errors, self.workers = [], [], []
        self.uid = os.getuid()
        self.override = None
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(path))
        self.server.listen()
        self.server.settimeout(.1)
        self.stop = False
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def accept(self):
        while not self.stop:
            try:
                peer, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            worker = threading.Thread(target=self.serve, args=(peer,), daemon=True)
            self.workers.append(worker)
            worker.start()

    def serve(self, peer):
        try:
            with peer:
                peer.settimeout(5)
                header = b''
                while not header.endswith(b'\r\n\r\n'):
                    header += read_exact(peer, 1)
                key = next(line.split(':', 1)[1].strip() for line in header.decode().splitlines()
                           if line.startswith('Sec-WebSocket-Key:'))
                accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
                peer.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
                while True:
                    message = read_message(peer)
                    method, p = message['method'], message.get('params', {})
                    if method == 'initialized':
                        continue
                    if method == 'initialize':
                        send_message(peer, dict(id=message['id'], result={}))
                        continue
                    assert method == 'command/exec'
                    assert p['command'][0] == str(self.binary)
                    assert p['command'][1] == '--inspect'
                    assert p['cwd'] == '/' and p['timeoutMs'] == 2500 and p['outputBytesCap'] == 65536
                    assert p['sandboxPolicy'] == dict(type='dangerFullAccess')
                    self.calls.append(p)
                    # Unsolicited approvals/tools are ignored, never answered.
                    send_message(peer, dict(id='unsolicited', method='item/tool/requestUserInput', params={}))
                    result = subprocess.run(p['command'], capture_output=True, text=True, check=True)
                    data = json.loads(result.stdout)
                    data['observer_uid'] = self.uid
                    stdout = json.dumps(data) if self.override is None else self.override
                    send_message(peer, dict(id=message['id'], result=dict(exitCode=0, stdout=stdout)))
        except (OSError, EOFError):
            pass
        except BaseException as exc:
            self.errors.append(type(exc).__name__)

    def close(self):
        self.stop = True
        self.server.close()
        self.thread.join(timeout=2)
        for worker in self.workers:
            worker.join(timeout=6)
        assert not self.errors


@pytest.mark.parametrize('profile', ['rust', 'monitor'])
def test_packaged_node_finds_companion_rust_binary_without_checkout_or_pythonpath(binary, repo, tmp_path, ipc, profile):
    from tools.build.runtime_sources import (
        SOURCE_ROOT,
        copy_monitor_sources,
        data_paths,
        source_paths,
    )
    package = tmp_path / 'release'
    if profile == 'monitor':
        root = package / 'Contents/Resources'
        copy_monitor_sources(SOURCE_ROOT, root)
        companion = root.parent / 'Helpers/clinx-provider'
        companion.parent.mkdir()
    else:
        root = package / 'control'
        for source in source_paths(SOURCE_ROOT, profile) + data_paths(SOURCE_ROOT, profile):
            dest = root / source.relative_to(SOURCE_ROOT)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
        companion = package / 'clinx-provider'
    shutil.copyfile(binary, companion)
    companion.chmod(0o755)
    # Node now owns TLS, IPC and disposable relay state in the same bundle.
    node_binary = companion.with_name('clinx-node')
    shutil.copyfile(binary.with_name('clinx-node'), node_binary)
    node_binary.chmod(0o755)
    provider = MetadataProvider(ipc / 'p.sock', companion)
    code = '''
import sys,json,importlib
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
for name in json.loads(sys.argv[3]): importlib.import_module(name)
from clinx.nodes.provider_relay import NodeProviderRelay
cfg=SimpleNamespace(app_server=SimpleNamespace(local_socket=sys.argv[2],native_socket=None,request_timeout_seconds=2))
cwd=sys.argv[4]
print(json.dumps(NodeProviderRelay(cfg,'air').exchange(dict(mode='workspace',host='air',cwd=cwd),dict(method='workspace/inspect',params=dict(mode='workspace',host='air',cwd=cwd)))))
'''
    if profile == 'rust':
        # Same Provider exchange contract and assertions via the native entry;
        # no retired Python relay is required in the production source closure.
        code = """
import sys,json,importlib,subprocess
from pathlib import Path
sys.path.insert(0,sys.argv[1])
for name in json.loads(sys.argv[3]): importlib.import_module(name)
program=Path(sys.argv[1]).parent/'clinx-provider'
cwd=sys.argv[4]
request=dict(context=dict(mode='workspace',host='air',cwd=cwd),
 body=dict(method='workspace/inspect',params=dict(mode='workspace',host='air',cwd=cwd)))
p=subprocess.run([str(program),'--exchange',sys.argv[2],'2'],input=json.dumps(request),
 text=True,capture_output=True,check=True)
print(json.dumps(json.loads(p.stdout)['result']))
"""
    names = [str(p.relative_to(SOURCE_ROOT)).removesuffix('.py').replace('/', '.').removesuffix('.__init__')
             for p in source_paths(SOURCE_ROOT, profile)]
    try:
        result = subprocess.run([os.sys.executable, '-I', '-B', '-c', code, str(root), str(provider.path),
            json.dumps(names), str(repo)], capture_output=True, text=True, check=True,
            env={k: v for k, v in os.environ.items() if not k.startswith(('CLINX_', 'PYTHON'))}, cwd=tmp_path)
        value = json.loads(result.stdout)
        assert value['observer_source'] == 'CODEX_PROVIDER_COMMAND_EXEC' and value['cwd'] == str(repo)
        assert not list(root.rglob('rust_workspace_probe.py'))
        assert len(provider.calls) == 1
    finally:
        provider.close()
