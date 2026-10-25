"""Rust Node exchange and Provider-owned metadata; no real user task or socket."""
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
from types import SimpleNamespace

import pytest
from clinx.authority.rust_authority import RustAuthorityClient
from clinx.nodes.provider_relay import NodeProviderRelay
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


def inspect(binary, request):
    return json.loads(subprocess.check_output([str(binary), '--inspect', json.dumps(request)]))


def test_rust_metadata_matches_git_and_shares_identity_across_linked_worktrees(binary, repo, tmp_path, monkeypatch):
    worktree = tmp_path / 'linked'
    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-q', '-b', 'linked', str(worktree)], check=True)
    # A caller cannot redirect the metadata read into another GIT_DIR/worktree.
    monkeypatch.setenv('GIT_DIR', '/missing/caller-git-dir')
    monkeypatch.setenv('GIT_WORK_TREE', '/missing/caller-worktree')
    a = inspect(binary, dict(mode='workspace', cwd=str(repo)))
    b = inspect(binary, dict(mode='workspace', cwd=str(worktree)))
    assert a['repository_identity'] == b['repository_identity']
    info = (repo / '.git').stat()
    assert a['repository_identity'] == f'git-common:{info.st_dev}:{info.st_ino}'
    assert a['branch'] == 'main' and b['branch'] == 'linked'
    assert a['observer_uid'] == os.getuid()
    assert b['repository_common_dir'] == str(repo / '.git')
    assert b['worktree'] != a['worktree']
    assert a['head'] == b['head']
    assert inspect(binary, dict(mode='repository', cwd=str(repo)))['repository_identity'] == a['repository_identity']
    assert inspect(binary, dict(mode='git', cwd=str(worktree), args=['branch', '--show-current']))['value'] == 'linked'


@pytest.mark.parametrize('case,error', [
    ({'mode': 'workspace', 'cwd': 'relative'}, 'WORKSPACE_CONTRACT_INVALID'),
    ({'mode': 'workspace', 'cwd': '/missing-fixture-cwd'}, 'WORKSPACE_CWD_UNREADABLE'),
    ({'mode': 'workspace', 'cwd': '/bad\0cwd'}, 'WORKSPACE_CONTRACT_INVALID'),
    ({'mode': 'unexpected'}, 'WORKSPACE_PROBE_INVALID'),
    ({'mode': 'git', 'args': ['reset', '--hard']}, 'WORKSPACE_PROBE_INVALID'),
    ({'mode': 'git', 'args': ['config', 'user.email', 'mutation']}, 'WORKSPACE_PROBE_INVALID'),
    ({'mode': 'git', 'args': ['rev-parse', None]}, 'WORKSPACE_PROBE_INVALID'),
])
def test_metadata_rejects_mutation_and_invalid_cwd_before_git(binary, repo, case, error):
    assert inspect(binary, {'cwd': str(repo), **case}) == dict(error=error)


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


def test_local_observer_and_node_exchange_use_rust_probe_with_exact_provider_uid(binary, repo, tmp_path, monkeypatch, ipc):
    provider = MetadataProvider(ipc / 'provider.sock', binary)
    request = dict(host='air', mode='workspace', cwd=str(repo))
    try:
        result = json.loads(subprocess.check_output([str(binary), '--observe', str(provider.path),
            json.dumps(request), '--qualification-provider-uid', str(os.getuid())]))
        assert result['observer_source'] == 'CODEX_PROVIDER_COMMAND_EXEC'
        assert result['cwd'] == str(repo) and result['observer_uid'] == os.getuid()
        monkeypatch.setenv('CLINX_PROVIDER_BINARY', str(binary))
        cfg = SimpleNamespace(app_server=SimpleNamespace(local_socket=str(provider.path), native_socket=None, request_timeout_seconds=2))
        relay = NodeProviderRelay(cfg, 'air')
        actual = relay.exchange(dict(mode='workspace', host='air', cwd=str(repo)),
                                dict(method='workspace/inspect', params=request))
        assert actual == result
        # Check a forged metadata UID and a different local peer independently.
        provider.uid = os.getuid() + 1
        conflict = json.loads(subprocess.check_output([str(binary), '--observe', str(provider.path),
            json.dumps(request), '--qualification-provider-uid', str(os.getuid())]))
        assert conflict == dict(error='WORKSPACE_OBSERVER_IDENTITY_CONFLICT')
        count = len(provider.calls)
        conflict = json.loads(subprocess.check_output([str(binary), '--observe', str(provider.path),
            json.dumps(request), '--qualification-provider-uid', str(os.getuid() + 1)]))
        assert conflict == dict(error='WORKSPACE_OBSERVER_IDENTITY_CONFLICT')
        assert len(provider.calls) == count  # No initialize/metadata request on a wrong peer.
        provider.uid = os.getuid()
        for stdout, error in [('{}', 'WORKSPACE_OBSERVER_IDENTITY_CONFLICT'),
            ('{"error":"WORKSPACE_CWD_UNREADABLE"}', 'WORKSPACE_CWD_UNREADABLE'),
            ('{"error":"UNTRUSTED_ERROR"}', 'WORKSPACE_OBSERVER_INVALID'),
            ('x' * 65537, 'WORKSPACE_OBSERVER_UNAVAILABLE')]:
            provider.override = stdout
            data = json.loads(subprocess.check_output([str(binary), '--observe', str(provider.path),
                json.dumps(request), '--qualification-provider-uid', str(os.getuid())]))
            assert data == dict(error=error)
    finally:
        provider.close()


def test_authority_workspace_contract_uses_rust_provider_without_python_probe(binary, repo, tmp_path, ipc):
    provider = MetadataProvider(ipc / 'p.sock', binary)
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    endpoint = ipc / 'a.sock'
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps(dict(projects=[dict(alias='fixture', host='air', cwd=str(repo))],
        endpoints=[str(provider.path)], lease_ms=120000)))
    process = subprocess.Popen([str(binary.parent / 'clinx-runtime'), '--qualification', '--init',
        '--qualification-workspace-observer', str(binary), '--state-dir', str(state), '--socket', str(endpoint),
        '--policy', str(policy), '--caller-uid', str(os.getuid()), '--transport-uid', str(os.getuid())],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        import time
        for _ in range(200):
            if endpoint.exists():
                break
            assert process.poll() is None
            time.sleep(.01)
        client = RustAuthorityClient(endpoint, os.getuid())
        prepared = client.request('business.prepare', dict(host='air', project='fixture', title='Fixture', prompt='fixture', approved=True), request_id='prepare')
        assert prepared['workspace_resolution']['observer_source'] == 'CODEX_PROVIDER_COMMAND_EXEC'
        assert prepared['workspace_resolution']['observer_uid'] == os.getuid()
        started = client.request('business.start', dict(prepared_execution_ref=prepared['prepared_execution_ref'], approved=True), request_id='start')
        assert started['workspace_resolution']['repository_identity'] == prepared['workspace_resolution']['repository_identity']
        repeated = client.request('business.start', dict(prepared_execution_ref=prepared['prepared_execution_ref'], approved=True), request_id='duplicate')
        assert repeated['execution_ref'] == started['execution_ref']
        assert provider.calls
    finally:
        process.terminate()
        process.wait(timeout=5)
        process.stderr.close()
        provider.close()


def test_remote_workspace_observer_reuses_paired_mtls_and_exact_host_route(binary, repo, tmp_path, ipc, monkeypatch):
    from clinx.discovery.discovery import Candidate
    from clinx.discovery.identity import NodeIdentity, PrivateStore, TrustedPeerStore
    from clinx.discovery.transport import DeviceServer, LanTransport
    from clinx.nodes.node_protocol import (
        NodeAuthorizationStore,
        NodeRecord,
        NodeRegistry,
        NodeRPCServer,
        NodeService,
        SharingScope,
    )
    from clinx.nodes.node_runtime import (
        CentreService,
        NodeRegistrationClient,
        PairedTLS,
        node_request_handler,
    )
    from clinx.nodes.provider_relay import ProviderGateway

    monkeypatch.setenv('CLINX_PROVIDER_BINARY', str(binary))
    provider = MetadataProvider(ipc / 'p.sock', binary)
    node = NodeIdentity(PrivateStore(tmp_path / 'node'), 'air')
    centre = NodeIdentity(PrivateStore(tmp_path / 'centre'), 'centre')
    np, cp = TrustedPeerStore(node.store), TrustedPeerStore(centre.store)
    with DeviceServer(centre, cp, address='127.0.0.1') as server:
        candidate = Candidate('centre', 'centre', centre.public['fingerprint'], (('127.0.0.1', server.port),))
        LanTransport(node, np).pair(candidate, server.window.open())
    sharing = SharingScope(user_scope='fixture', execute_tasks=True, projects=(str(repo),))
    approvals = NodeAuthorizationStore(centre.store.root)
    approvals.grant('air', user_scope='fixture', execute_tasks=True, projects=(str(repo),))
    owner = NodeAuthorizationStore(node.store.root)
    owner.grant('centre', user_scope='fixture', execute_tasks=True, projects=(str(repo),))
    registry, local_registry = NodeRegistry(tmp_path / 'centre.sqlite3'), NodeRegistry(tmp_path / 'node.sqlite3')
    service = CentreService(PairedTLS(centre, cp), registry, approvals)
    server = service.server(address='127.0.0.1', port=0)
    node_tls = PairedTLS(node, np)
    node_service = NodeService(NodeRecord(node_id='air', user_scope='fixture',
        public_key_fingerprint=node.public['fingerprint'], display_name='fixture'),
        local_registry, scope=sharing, read_thread=lambda **kw: {})
    cfg = SimpleNamespace(app_server=SimpleNamespace(local_socket=str(provider.path), native_socket=None, request_timeout_seconds=2))
    node_server = NodeRPCServer(node_service, address='127.0.0.1', port=0,
        ssl_context_factory=node_tls.server_context,
        request_handler=node_request_handler(node_service, node_tls, owner, 'centre', lambda: sharing, NodeProviderRelay(cfg, 'air')))
    gateway = None
    try:
        registration = NodeRegistrationClient(node_tls, 'centre', f'tls://127.0.0.1:{server.address[1]}')
        assert registration.register(node, sharing, port=node_server.address[1], capabilities=())['registered']
        gateway = ProviderGateway(ipc / 'g.sock', os.getuid(), service.tls, registry, approvals)
        env = dict(os.environ, CLINX_RUST_PROVIDER_GATEWAYS=json.dumps({'air': str(gateway.endpoint), 'imac': str(ipc / 'wrong.sock')}),
            CLINX_RUST_PROVIDER_GATEWAY_UID=str(os.getuid()))
        def observe(request):
            return json.loads(subprocess.check_output([str(binary), '--observe', str(gateway.endpoint), json.dumps(request)], env=env))
        request = dict(host='air', cwd=str(repo), mode='workspace')
        result = observe(request)
        assert result['cwd'] == str(repo) and result['observer_source'] == 'CODEX_PROVIDER_COMMAND_EXEC'
        assert result['observer_uid'] == os.getuid() and len(provider.calls) == 1
        assert observe(dict(request, host='imac')) == dict(error='REMOTE_PROVIDER_ENDPOINT_CONFLICT')
        assert observe(dict(request, cwd=str(tmp_path / 'outside-grant'))) == dict(error='PROVIDER_SCOPE_DENIED')
        assert len(provider.calls) == 1
        # A Node cannot convert a self-asserted UID into Provider proof.
        provider.uid = os.getuid() + 1
        assert observe(request) == dict(error='WORKSPACE_OBSERVER_IDENTITY_CONFLICT')
    finally:
        if gateway:
            gateway.close()
        node_server.close()
        server.close()
        registry.close()
        local_registry.close()
        provider.close()


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
