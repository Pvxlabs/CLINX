"""Shared Provider oracle and the remaining Python Node facade wiring smoke.

The nine IPC/MCP/Node x local/paired/routed fault sequences now live in
kernel/tests/native_transport_qualification.rs. This fixture retains the actual
Python exception mapping and callback boundary; it is not Mac execution proof.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
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


def test_python_node_facade_real_authority_callback_and_pending_error(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import uuid
    from clinx.nodes.rust_node_adapter import RustNodeExecutionAdapter
    from clinx.nodes.node_protocol import NodeProtocolError

    binaries = Path(os.environ['CLINX_TEST_RUST_BIN_DIR'])
    monkeypatch.setenv('CLINX_NODE_BINARY', str(binaries/'clinx-node'))
    repo = tmp_path/'repo'
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=CLINX Qualification', '-c',
                    'user.email=qualification@example.invalid', 'commit', '--allow-empty', '-qm', 'fixture'], check=True)
    state = tmp_path/'authority'
    state.mkdir(mode=0o700)
    with tempfile.TemporaryDirectory(prefix='ipc-') as sockets:
        endpoint = Path(sockets)/'a.sock'
        provider = Path(sockets)/'p.sock'
        policy = tmp_path/'policy.json'
        policy.write_text(json.dumps(dict(projects=[dict(alias='fixture', host='air', cwd=str(repo))],
            endpoints=[str(provider)], provider_routes={'air': [str(provider)]}, lease_ms=120000)))
        client = RustAuthorityClient(endpoint, os.getuid())
        process = subprocess.Popen([str(binaries/'clinx-runtime'), '--state-dir', str(state),
            '--socket', str(endpoint), '--policy', str(policy), '--qualification',
            '--caller-uid', str(os.getuid()), '--transport-uid', str(os.getuid()), '--init'], stdout=subprocess.DEVNULL)
        calls = []
        def call(operation, payload, *, key):
            calls.append((operation, payload, key))
            return client.request('business.' + operation, payload, request_id=key)
        try:
            for _ in range(200):
                assert process.poll() is None
                try:
                    if client.request('health', {}, request_id='health')['business_commands']:
                        break
                except Exception:
                    time.sleep(.01)
            else:
                raise AssertionError('Authority did not start')
            cfg = SimpleNamespace(runtime_host='air', workspaces=[], projects=[SimpleNamespace(
                project_alias='fixture', repo=repo)], app_server=SimpleNamespace(local_socket=str(provider),
                native_socket=None, request_timeout_seconds=2))
            helper = RustNodeExecutionAdapter.from_config(cfg, SimpleNamespace(call=call), 'air', allowed_projects=['fixture'])
            prepared = helper.prepare(project='fixture', task_action='create', prompt='Python facade', approved=True)
            execution = helper.start(prepared_execution_ref=prepared['prepared_execution_ref'], approved=True)
            assert execution['prepared_execution_ref'] == prepared['prepared_execution_ref']
            with pytest.raises(NodeProtocolError):
                helper.cancel(execution_ref=execution['execution_ref'], project='another-project', approved=True)
            tid = '11111111-2222-3333-4444-555555555555'
            canonical = dict(node_id='air', host='air', provider='codex_app_server', thread_id=tid,
                user_scope='fixture', origin='CODEX_NATIVE', title='Canonical fixture', cwd=str(repo),
                task_id='task_' + uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(
                    ['air', 'codex_app_server', tid], separators=(',', ':'))).hex,
                first_provenance_json=json.dumps(dict(node='air', node_fingerprint='isolated-key',
                    observation_id='isolated-observation', digest='isolated-digest')))
            handoff = dict(canonical_identity=canonical, exact_turn_id='predecessor',
                _canonical_owner=dict(owner='air', user='fixture', fingerprint='isolated-key'))
            assert helper.adopt(**handoff)['adoption_status'] == 'PENDING'
            with pytest.raises(NodeProtocolError, match='Transport') as error:
                helper.prepare(**handoff, prompt='Exact canonical input', approved=True)
            assert error.value.code == 'CANONICAL_ADOPTION_PENDING'
            assert all(len(key) == 32 for _, _, key in calls)
            assert any(op == 'prepare' and payload['prompt'] == 'Python facade' for op, payload, _ in calls)
        finally:
            process.terminate()
            process.wait(timeout=5)
