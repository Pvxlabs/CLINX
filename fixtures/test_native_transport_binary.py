"""Fresh Rust processes, real IPC/WebSocket and faulted Provider history.

The Provider here is a protocol fixture, not physical Mac execution evidence.
Requires CLINX_TEST_RUST_BIN_DIR pointing to the binaries under qualification.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest
from clinx.authority.rust_authority import RustAuthorityClient


def read_exact(peer, n):
    data = b''
    while len(data) < n:
        part = peer.recv(n - len(data))
        if not part:
            raise EOFError
        data += part
    return data


def read_message(peer):
    first, second = read_exact(peer, 2)
    assert first == 0x81 and second & 0x80
    size = second & 127
    if size == 126:
        size = struct.unpack('!H', read_exact(peer, 2))[0]
    elif size == 127:
        size = struct.unpack('!Q', read_exact(peer, 8))[0]
    assert size <= 16 * 1024 * 1024
    mask = read_exact(peer, 4)
    return json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(read_exact(peer, size))))


def send_message(peer, message):
    data = json.dumps(message).encode()
    header = bytes([0x81])
    header += (bytes([len(data)]) if len(data) < 126 else
               bytes([126]) + struct.pack('!H', len(data)) if len(data) <= 65535 else
               bytes([127]) + struct.pack('!Q', len(data)))
    peer.sendall(header + data)


class Provider:
    def __init__(self, path):
        self.path = path
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(path))
        self.listener.listen()
        self.threads, self.turns = {}, {}
        self.inputs, self.interrupts = [], []
        self.lose_input = self.lose_cancel = False
        self.connections, self.workers = [], []
        self.stopped = False
        self.acceptor = threading.Thread(target=self.accept, daemon=True)
        self.acceptor.start()

    def accept(self):
        while not self.stopped:
            try:
                peer, _ = self.listener.accept()
            except OSError:
                break
            self.connections.append(peer)
            worker = threading.Thread(target=self.serve, args=(peer,), daemon=True)
            self.workers.append(worker)
            worker.start()

    def serve(self, peer):
        try:
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
                result = {}
                if method == 'thread/start':
                    tid = f'thread-{len(self.threads) + 1}'
                    self.threads[tid] = dict(id=tid, cwd=p['cwd'], name=None, status=dict(type='idle'))
                    result = dict(thread=self.threads[tid])
                elif method in ('thread/read', 'thread/resume'):
                    result = dict(thread=self.threads[p['threadId']])
                elif method == 'thread/name/set':
                    self.threads[p['threadId']]['name'] = p['name']
                elif method == 'thread/turns/list':
                    result = dict(data=self.turns.get(p['threadId'], []), nextCursor=None)
                elif method == 'thread/items/list':
                    turn = next(t for t in self.turns[p['threadId']] if t['id'] == p['turnId'])
                    result = dict(data=[dict(turnId=turn['id'], item=i) for i in turn['items']], nextCursor=None)
                elif method == 'turn/start':
                    self.inputs.append(p)
                    turn = dict(id=f'turn-{len(self.inputs)}', status='inProgress' if p['input'][0]['text'] == 'running' else 'completed', items=[
                        dict(id=f'input-{len(self.inputs)}', type='userMessage', clientId=p['clientUserMessageId'], content=p['input']),
                        dict(id='answer', type='agentMessage', phase='final_answer', text='Exact Rust result')])
                    self.turns.setdefault(p['threadId'], []).insert(0, turn)
                    result = dict(turn=turn)
                    if self.lose_input:
                        return
                elif method == 'turn/interrupt':
                    self.interrupts.append(p)
                    next(t for t in self.turns[p['threadId']] if t['id'] == p['turnId'])['status'] = 'interrupted'
                    if self.lose_cancel:
                        return
                elif method != 'initialize':
                    raise AssertionError(method)
                send_message(peer, dict(id=message['id'], result=result))
        except (OSError, EOFError):
            pass
        finally:
            peer.close()

    def close(self):
        self.stopped = True
        self.listener.close()
        for peer in self.connections:
            try:
                peer.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for worker in self.workers:
            worker.join(timeout=2)


@pytest.mark.parametrize("mcp_entry", [False, True, "node"])
@pytest.mark.parametrize("paired", [False, True, "routed"])
def test_rust_binary_ipc_provider_completion_ack_loss_cancel_and_restart(tmp_path, paired, mcp_entry):
    binaries = Path(os.environ['CLINX_TEST_RUST_BIN_DIR'])
    runtime, worker = binaries/'clinx-runtime', binaries/'clinx-native-transport'
    assert runtime.is_file() and worker.is_file()
    repo = tmp_path/'repo'
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=CLINX Qualification', '-c',
                    'user.email=qualification@example.invalid', 'commit', '--allow-empty', '-qm', 'fixture'], check=True)
    state = tmp_path/'authority'
    state.mkdir(mode=0o700)
    # Keep Unix sockets below sun_path's limit even with a long artifact root.
    # The repository and state stay under the task's normal pytest directory.
    socket_root = Path(tempfile.mkdtemp(prefix='ipc-', dir=tmp_path.parent))
    endpoint = socket_root/'a.sock'
    provider = Provider(socket_root/'p.sock')
    resources = []
    provider_endpoint = provider.path
    remote_arguments = []
    if paired:
        from types import SimpleNamespace

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
        from clinx.nodes.provider_relay import NodeProviderRelay, ProviderGateway
        node = NodeIdentity(PrivateStore(tmp_path/'node'), 'air')
        centre = NodeIdentity(PrivateStore(tmp_path/'centre'), 'centre')
        np, cp = TrustedPeerStore(node.store), TrustedPeerStore(centre.store)
        with DeviceServer(centre, cp, address='127.0.0.1') as pair_server:
            candidate = Candidate('centre', 'centre', centre.public['fingerprint'], (('127.0.0.1', pair_server.port),))
            LanTransport(node, np).pair(candidate, pair_server.window.open())
        sharing = SharingScope(user_scope='fixture', execute_tasks=True, projects=(str(repo),))
        approvals = NodeAuthorizationStore(centre.store.root)
        approvals.grant('air', user_scope='fixture', execute_tasks=True, projects=(str(repo),))
        owner = NodeAuthorizationStore(node.store.root)
        owner.grant('centre', user_scope='fixture', execute_tasks=True, projects=(str(repo),))
        registry = NodeRegistry(tmp_path/'registry.sqlite3')
        local_registry = NodeRegistry(tmp_path/'local.sqlite3')
        centre_service = CentreService(PairedTLS(centre, cp), registry, approvals)
        centre_server = centre_service.server(address='127.0.0.1', port=0)
        node_tls = PairedTLS(node, np)
        node_service = NodeService(NodeRecord(node_id='air', user_scope='fixture',
            public_key_fingerprint=node.public['fingerprint'], display_name='fixture'), local_registry, scope=sharing, read_thread=lambda **kw: {})
        cfg = SimpleNamespace(app_server=SimpleNamespace(local_socket=str(provider.path), native_socket=None, request_timeout_seconds=2))
        node_server = NodeRPCServer(node_service, address='127.0.0.1', port=0,
            ssl_context_factory=node_tls.server_context,
            request_handler=node_request_handler(node_service, node_tls, owner, 'centre', lambda: sharing,
                NodeProviderRelay(cfg, 'air')))
        registration = NodeRegistrationClient(node_tls, 'centre', f'tls://127.0.0.1:{centre_server.address[1]}')
        assert registration.register(node, sharing, port=node_server.address[1], capabilities=())['registered']
        provider_endpoint = socket_root/'g.sock'
        gateway = ProviderGateway(provider_endpoint, os.getuid(), centre_service.tls, registry, approvals)
        resources = [gateway, node_server, centre_server, local_registry, registry]
        remote_arguments = ['--provider-gateway', str(provider_endpoint), '--provider-gateway-uid', str(os.getuid()), '--remote-host', 'air']
        if paired == 'routed':
            remote_arguments = ['--provider-route', 'another-node', str(socket_root/'wrong.sock'),
                '--provider-route', 'air', str(provider_endpoint), '--provider-gateway-uid', str(os.getuid())]
    policy = tmp_path/'policy.json'
    policy.write_text(json.dumps(dict(projects=[dict(alias='fixture', host='air', cwd=str(repo))],
        endpoints=[str(provider_endpoint)], provider_routes={'air': [str(provider_endpoint)]}, lease_ms=120000)))
    args = [str(runtime), '--state-dir', str(state), '--socket', str(endpoint), '--policy', str(policy),
            '--qualification', '--caller-uid', str(os.getuid()), '--transport-uid', str(os.getuid())]
    client = RustAuthorityClient(endpoint, os.getuid())
    sequence = 0
    process = None
    def call(op, payload=None, *, key=None):
        nonlocal sequence
        sequence += 1
        return client.request('business.' + op, payload or {}, request_id=key or f'fixture-{sequence}')
    def launch(init=False):
        nonlocal process
        process = subprocess.Popen(args + (['--init'] if init else []), stdout=subprocess.DEVNULL)
        for _ in range(200):
            if endpoint.exists():
                try:
                    if client.request('health', {}, request_id='health')['business_commands']:
                        return
                except Exception:
                    pass
            assert process.poll() is None
            time.sleep(.01)
        raise AssertionError('Authority did not start')
    def tick():
        return json.loads(subprocess.check_output([str(worker), '--socket', str(endpoint),
            '--authority-uid', str(os.getuid()), '--timeout-seconds', '2', '--once', *remote_arguments], text=True))
    mcp_config = tmp_path/'mcp.json'
    mcp_config.write_text(json.dumps(dict(runtime_host='air', provider_endpoint=str(provider.path),
        provider_timeout_seconds=2, host_aliases={'air': ['air']}, codex_host_ids={})))
    observation_env = {}
    if mcp_entry is True:
        from clinx.discovery.identity import PrivateStore
        from clinx.monitor.network_observation import ObservationWriter
        from clinx.nodes.node_protocol import NodeRegistry

        private = PrivateStore(tmp_path/'observation-state')
        NodeRegistry(tmp_path/'observation-registry.sqlite3').close()
        ObservationWriter(tmp_path/'observations.sqlite3')
        observation_env = dict(CLINX_OBSERVATION_STATE=str(private.root),
            CLINX_OBSERVATION_DB=str(tmp_path/'observations.sqlite3'),
            CLINX_OBSERVATION_REGISTRY=str(tmp_path/'observation-registry.sqlite3'),
            CLINX_OBSERVATION_USER_SCOPE='fixture', CLINX_OBSERVATION_NODE_ID='air',
            CLINX_OBSERVATION_PYTHON=os.sys.executable)
    def mcp_call(name, arguments):
        request = dict(jsonrpc='2.0', id='fixture', method='tools/call',
                       params=dict(name=name, arguments=arguments))
        result = subprocess.run([str(binaries/'clinx-mcp'), '--config', str(mcp_config), '--stdio'],
            input=json.dumps(request)+'\n', text=True, capture_output=True, timeout=15, check=True,
            env=dict(os.environ, CLINX_RUST_SOCKET=str(endpoint), CLINX_RUST_UID=str(os.getuid()),
                     CLINX_RUST_OBSERVATION_READS='1' if mcp_entry else '0', **observation_env))
        response = json.loads(result.stdout)
        assert response['id'] == 'fixture' and 'error' not in response
        assert response['result']['isError'] is False, response
        return response['result']['structuredContent']
    def prepare_start(prompt, *, task=None, key='start'):
        p = dict(host='air', project='fixture', title='Native title', prompt=prompt, approved=True)
        if task:
            p['task_ref'] = task
        if mcp_entry is True:
            prepared = mcp_call('clinx_prepare_execution', dict(p, task_action='continue' if task else 'create'))
            return mcp_call('clinx_start_execution',
                dict(prepared_execution_ref=prepared['prepared_execution_ref'], approved=True))
        if mcp_entry == 'node':
            prepared = helper.prepare(**dict(p, task_action='continue' if task else 'create'))
            return helper.start(prepared_execution_ref=prepared['prepared_execution_ref'], approved=True)
        prepared = call('prepare', p)
        return call('start', dict(prepared_execution_ref=prepared['prepared_execution_ref'], approved=True), key=key)
    try:
        launch(True)
        if mcp_entry == 'node':
            from types import SimpleNamespace
            from clinx.authority.rust_integration import Commands
            from clinx.nodes.rust_node_adapter import RustNodeExecutionAdapter
            if paired:
                from clinx.nodes.node_runtime import NodeAuthorityClient
                centre_service.authority = SimpleNamespace(call=call)
                commands = Commands(NodeAuthorityClient(registration, sharing))
            else:
                commands = SimpleNamespace(call=call)
            cfg = SimpleNamespace(runtime_host='air', workspaces=[], projects=[SimpleNamespace(
                project_alias='fixture', repo=repo)], app_server=SimpleNamespace(local_socket=str(provider.path),
                native_socket=None, request_timeout_seconds=2))
            helper = RustNodeExecutionAdapter.from_config(cfg, commands, 'air', allowed_projects=['fixture'])
        first = prepare_start('exact input')
        tick()
        tick()
        assert call('get_status', dict(execution_ref=first['execution_ref']))['status'] == 'SUCCEEDED'
        if mcp_entry is True:
            observations = mcp_call('clinx_list_observations', dict(limit=1))
            assert observations['read_only'] is True and observations['items'] == []
            exact = mcp_call('clinx_get_context', dict(execution_ref=first['execution_ref']))
            assert exact['execution_ref'] == first['execution_ref']
            assert exact['context_scope'] == 'EXACT_EXECUTION_TURN'
            assert exact['execution_turn_ref'] == call('get_status', dict(execution_ref=first['execution_ref']))['native_turn_id']
            assert exact['last_user_intent'] == 'exact input'
        if mcp_entry == 'node':
            exact = helper.context(execution_ref=first['execution_ref'], task_ref=first['task_ref'])
            assert exact['last_user_intent'] == 'exact input'
            assert exact['context_scope'] == 'EXACT_EXECUTION_TURN'
            assert exact['execution_ref'] == first['execution_ref']
            from clinx.nodes.node_protocol import NodeProtocolError
            with pytest.raises(NodeProtocolError):
                helper.cancel(execution_ref=first['execution_ref'], project='another-project', approved=True)
            assert len(provider.interrupts) == 0
        assert provider.threads['thread-1']['name'] == 'Native title'
        assert len(provider.inputs) == 1
        assert provider.inputs[0]['input'] == [dict(type='text', text='exact input')]
        assert provider.inputs[0]['sandboxPolicy'] == dict(type='dangerFullAccess')
        provider.lose_input = True
        lost = prepare_start('lost ACK', task=first['task_ref'], key='lost-start')
        tick()
        assert call('get_status', dict(execution_ref=lost['execution_ref']))['dispatch_state'] == 'SENDING'
        process.kill()
        process.wait(timeout=5)
        launch()
        tick()
        tick()
        assert call('get_status', dict(execution_ref=lost['execution_ref']))['status'] == 'SUCCEEDED'
        assert len(provider.inputs) == 2
        if mcp_entry is True:
            duplicate = mcp_call('clinx_start_execution', dict(prepared_execution_ref=lost['prepared_execution_ref'], approved=True))
            # A fresh MCP command has a fresh operation id. The original
            # prepared ref resolves to the CURRENT receipt, never a new turn.
            assert duplicate == call('get_status', dict(execution_ref=lost['execution_ref']))
            assert duplicate['execution_ref'] == lost['execution_ref']
            assert len(provider.inputs) == 2
        elif mcp_entry == 'node':
            # Fresh Helper processes read the current exact prepared receipt.
            duplicate = helper.start(prepared_execution_ref=lost['prepared_execution_ref'], approved=True)
            assert duplicate['execution_ref'] == lost['execution_ref']
            assert duplicate['status'] == 'SUCCEEDED'
            assert len(provider.inputs) == 2
        else:
            assert call('start', dict(prepared_execution_ref=lost['prepared_execution_ref'], approved=True), key='lost-start') == lost
        provider.lose_input = False
        running = prepare_start('running', task=first['task_ref'], key='running-start')
        tick()
        if mcp_entry is True:
            mcp_call('clinx_cancel_execution', dict(execution_ref=running['execution_ref'], approved=True))
        elif mcp_entry == 'node':
            pending = helper.cancel(execution_ref=running['execution_ref'], approved=True)
            assert pending['operation_state'] == 'CANCELLATION_PENDING'
        else:
            call('cancel', dict(execution_ref=running['execution_ref'], approved=True))
        provider.lose_cancel = True
        tick()
        process.kill()
        process.wait(timeout=5)
        launch()
        tick()
        assert call('get_status', dict(execution_ref=running['execution_ref']))['status'] == 'CANCELLED'
        if mcp_entry == 'node':
            receipt = helper.reconcile(execution_ref=running['execution_ref'])
            assert receipt['status'] == 'CANCELLED'
            assert receipt['resource_state'] == 'RELEASED'
            assert receipt['reconciliation'] == 'RUST_TRANSPORT_PENDING'
        assert len(provider.interrupts) == 1
        assert len(provider.inputs) == 3
        assert call('adoption_endpoints', dict(host='air'))['endpoints'] == [str(provider_endpoint)]
        if mcp_entry == 'node':
            import uuid
            from concurrent.futures import ThreadPoolExecutor
            tid = '11111111-2222-3333-4444-555555555555'
            fingerprint = node.public['fingerprint'] if paired else 'isolated-key'
            canonical = dict(node_id='air', host='air', provider='codex_app_server', thread_id=tid,
                user_scope='fixture', origin='CODEX_NATIVE', title='Canonical fixture', cwd=str(repo),
                task_id='task_' + uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(
                    ['air', 'codex_app_server', tid], separators=(',', ':'))).hex,
                first_provenance_json=json.dumps(dict(node='air', node_fingerprint=fingerprint,
                    observation_id='isolated-observation', digest='isolated-digest')))
            handoff = dict(canonical_identity=canonical, exact_turn_id='predecessor',
                _canonical_owner=dict(owner='air', user='fixture', fingerprint=fingerprint))
            provider.threads[tid] = dict(id=tid, cwd=str(repo), name=None, status=dict(type='idle'))
            provider.turns[tid] = [dict(id='predecessor', status='completed', items=[])]
            adopted = helper.adopt(**handoff)
            assert adopted['adoption_status'] == 'PENDING'
            with pytest.raises(NodeProtocolError, match='Transport'):
                helper.prepare(**handoff, prompt='Exact canonical input', approved=True)
            assert len(provider.inputs) == 3
            tick()
            committed = helper.adopt(**handoff)
            assert committed['adoption_status'] == 'ADOPTED'
            assert committed['task_ref'] == canonical['task_id'] == adopted['task_ref']
            prepared = helper.prepare(**handoff, prompt='Exact canonical input', approved=True)
            assert prepared['task_ref'] == canonical['task_id']
            canonical_execution = helper.start(prepared_execution_ref=prepared['prepared_execution_ref'], approved=True)
            tick()
            tick()
            assert helper.status(execution_ref=canonical_execution['execution_ref'])['status'] == 'SUCCEEDED'
            assert len(provider.inputs) == 4 and provider.inputs[-1]['threadId'] == tid
            assert helper.adopt(**handoff)['task_ref'] == committed['task_ref']
            assert len(provider.inputs) == 4
            # Independent calls own no local worker/fence and may share a cwd.
            with ThreadPoolExecutor(max_workers=2) as pool:
                concurrent = list(pool.map(prepare_start, ['parallel one', 'parallel two']))
            assert len({r['task_ref'] for r in concurrent}) == len({r['execution_ref'] for r in concurrent}) == 2
            tick()
            tick()
            assert all(helper.status(execution_ref=r['execution_ref'])['status'] == 'SUCCEEDED' for r in concurrent)
            assert len(provider.inputs) == 6

    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        for resource in resources:
            resource.close()
        provider.close()
        shutil.rmtree(socket_root)
