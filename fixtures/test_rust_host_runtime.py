"""Real Rust Authority/worker processes, Host processes and Provider frames.

All repositories, commands and databases belong to this fixture. No production
service, Provider, task, launcher or credentials are used.
"""
import base64
import hashlib
import json
import os
import signal
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest
from clinx.authority.rust_authority import RustAuthorityClient
from fixtures.test_native_transport_binary import read_exact, read_message, send_message


def eventually(read, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = read()
        if predicate(value):
            return value
        time.sleep(.03)
    raise AssertionError(f'condition did not become true: {value!r}')


class HostProvider:
    def __init__(self, path):
        self.path = path
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.bind(str(path))
        self.socket.listen()
        self.peers, self.workers = [], []
        self.threads, self.turns, self.requests = {}, {}, {}
        self.responses, self.starts, self.interrupts, self.errors = [], [], [], []
        self.creates = []
        self.lock = threading.RLock()
        self.stopped = False
        self.lose_input = self.lose_ack = False
        self.lose_create = False
        self.duplicates = False
        self.calls = 1
        self.operation = dict(operation_class='DEVELOPMENT_MUTATION',
                              capability='LOCAL_HOST_PROCESS', operation='development_command',
                              arguments=dict(argv=['/usr/bin/true']))
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while not self.stopped:
            try:
                peer, _ = self.socket.accept()
            except OSError:
                return
            self.peers.append(peer)
            worker = threading.Thread(target=self.serve, args=(peer,), daemon=True)
            self.workers.append(worker)
            worker.start()

    def tool_requests(self, tid, turn):
        return [dict(id=f'request-{turn["id"]}-{i}', method='item/tool/call', params=dict(
            threadId=tid, turnId=turn['id'], callId=f'call-{turn["id"]}-{i}',
            namespace='clinx', tool='clinx_host_operation', arguments=self.operation))
            for i in range(self.calls)]

    def serve(self, peer):
        try:
            peer.settimeout(30)
            header = b''
            while not header.endswith(b'\r\n\r\n'):
                header += read_exact(peer, 1)
            key = next(line.split(':', 1)[1].strip() for line in header.decode().splitlines()
                       if line.startswith('Sec-WebSocket-Key:'))
            accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
            peer.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
            while True:
                message = read_message(peer)
                if 'method' not in message:
                    with self.lock:
                        self.responses.append(message)
                        if 'error' in message:
                            continue
                        request = self.requests[message['id']]
                        p = request['params']
                        turn = next(t for t in self.turns[p['threadId']] if t['id'] == p['turnId'])
                        item = dict(id=p['callId'], type='dynamicToolCall', namespace='clinx',
                                    tool='clinx_host_operation', **message['result'])
                        if not any(i['id'] == item['id'] for i in turn['items']):
                            turn['items'].append(item)
                        if len([i for i in turn['items'] if i['type'] == 'dynamicToolCall']) == self.calls:
                            if turn['status'] == 'inProgress':
                                turn['status'] = 'completed'
                            turn['items'].append(dict(id='answer', type='agentMessage',
                                                      phase='final_answer', text='Host result'))
                            self.threads[p['threadId']]['status'] = dict(type='idle')
                    if self.lose_ack:
                        return
                    send_message(peer, dict(method='item/completed', params=dict(
                        threadId=p['threadId'], turnId=p['turnId'], item=item)))
                    continue
                method, p = message['method'], message.get('params', {})
                if method == 'initialized':
                    continue
                staged = []
                with self.lock:
                    if method == 'initialize':
                        result = {}
                    elif method == 'thread/start':
                        self.creates.append(p)
                        tid = f'thread-{len(self.threads) + 1}'
                        self.threads[tid] = dict(id=tid, cwd=p['cwd'], name=None, status=dict(type='idle'))
                        assert p['dynamicTools'][0]['tools'][0]['name'] == 'clinx_host_operation'
                        result = dict(thread=self.threads[tid])
                        if self.lose_create:
                            return
                    elif method in ('thread/read', 'thread/resume'):
                        tid = p['threadId']
                        result = dict(thread=self.threads[tid])
                        if method == 'thread/resume':
                            for turn in self.turns.get(tid, []):
                                if turn['status'] == 'inProgress':
                                    staged.extend(self.tool_requests(tid, turn))
                    elif method == 'thread/name/set':
                        self.threads[p['threadId']]['name'] = p['name']
                        result = {}
                    elif method == 'thread/turns/list':
                        result = dict(data=self.turns.get(p['threadId'], []), nextCursor=None)
                    elif method == 'thread/items/list':
                        turn = next(t for t in self.turns[p['threadId']] if t['id'] == p['turnId'])
                        result = dict(data=[dict(turnId=turn['id'], item=i) for i in turn['items']], nextCursor=None)
                    elif method == 'turn/start':
                        self.starts.append(p)
                        turn = dict(id=f'turn-{len(self.starts)}', status='inProgress', items=[dict(
                            id=f'input-{len(self.starts)}', type='userMessage', clientId=p['clientUserMessageId'], content=p['input'])])
                        self.turns.setdefault(p['threadId'], []).insert(0, turn)
                        self.threads[p['threadId']]['status'] = dict(type='active')
                        result = dict(turn=turn)
                        staged = self.tool_requests(p['threadId'], turn)
                        if self.lose_input:
                            return
                    elif method == 'turn/interrupt':
                        self.interrupts.append(p)
                        next(t for t in self.turns[p['threadId']] if t['id'] == p['turnId'])['status'] = 'interrupted'
                        result = {}
                    else:
                        raise AssertionError(method)
                    for request in staged:
                        self.requests[request['id']] = request
                # Exercise requests arriving before the turn/start ACK, plus
                # broadcasts which belong to Desktop or another execution.
                if staged:
                    send_message(peer, dict(id='approval', method='item/commandExecution/requestApproval',
                                           params=staged[0]['params']))
                    foreign = {**staged[0], 'id': 'foreign', 'params': {**staged[0]['params'], 'turnId': 'foreign'}}
                    send_message(peer, foreign)
                    for request in staged:
                        params = request['params']
                        send_message(peer, dict(method='item/started', params=dict(
                            threadId=params['threadId'], turnId=params['turnId'], item=dict(
                            id=params['callId'], type='dynamicToolCall', namespace=params['namespace'],
                            tool=params['tool'], arguments=params['arguments']))))
                        send_message(peer, request)
                        if self.duplicates:
                            send_message(peer, request)
                            send_message(peer, {**request, 'params': {**request['params'], 'operationId': 'conflict'}})
                send_message(peer, dict(id=message['id'], result=result))
        except (OSError, EOFError):
            pass
        except BaseException as exc:
            self.errors.append(repr(exc))
        finally:
            peer.close()

    def close(self):
        self.stopped = True
        self.socket.close()
        for p in self.peers:
            try:
                p.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for worker in self.workers:
            worker.join(timeout=2)


@pytest.fixture
def runtime(tmp_path):
    binaries = Path(os.environ['CLINX_TEST_RUST_BIN_DIR'])
    root = Path(tempfile.mkdtemp(prefix='clinx-host-'))
    repo = tmp_path/'repo'
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=CLINX Qualification', '-c',
                    'user.email=qualification@example.invalid', 'commit', '--allow-empty', '-qm', 'fixture'], check=True)
    repo2 = tmp_path/'repo2'
    subprocess.run(['git', 'init', '-q', str(repo2)], check=True)
    subprocess.run(['git', '-C', str(repo2), '-c', 'user.name=CLINX Qualification', '-c',
                    'user.email=qualification@example.invalid', 'commit', '--allow-empty', '-qm', 'fixture'], check=True)
    state = tmp_path/'state'
    state.mkdir(mode=0o700)
    endpoint = root/'a.sock'
    provider = HostProvider(root/'p.sock')
    scope = dict(capability='LOCAL_HOST_PROCESS', operation='development_command',
                 operation_class='DEVELOPMENT_MUTATION', target='')
    policy = tmp_path/'policy.json'
    policy.write_text(json.dumps(dict(projects=[dict(alias='fixture', host='p620', cwd=str(repo)), dict(alias='fixture2', host='p620', cwd=str(repo2))],
        endpoints=[str(provider.path)], lease_ms=120000, host_operations=[scope])))
    cfg = tmp_path/'host.json'
    cfg.write_text(json.dumps(dict(enabled=True, host='p620', trusted_workspace_roots=[str(repo), str(repo2)])))
    client = RustAuthorityClient(endpoint, os.getuid())
    counter = 0
    processes = []
    logs = []
    def call(op, payload=None):
        nonlocal counter
        counter += 1
        return client.request('business.' + op, payload or {}, request_id=f'host-fixture-{counter}')
    def launch_authority(init=False):
        log = (tmp_path/f'authority-{len(logs)}.log').open('wb')
        logs.append(log)
        process = subprocess.Popen([str(binaries/'clinx-runtime'), '--state-dir', str(state),
            '--socket', str(endpoint), '--policy', str(policy), '--qualification',
            '--caller-uid', str(os.getuid()), '--transport-uid', str(os.getuid()), *(['--init'] if init else [])], stdout=log, stderr=log)
        processes.append(process)
        def ready():
            assert process.poll() is None, (tmp_path/f'authority-{len(logs)-1}.log').read_text()
            try:
                return client.request('health', {}, request_id='health')['business_commands']
            except Exception:
                return False
        eventually(ready, bool)
        return process
    def launch_worker():
        log = (tmp_path/f'worker-{len(logs)}.log').open('wb')
        logs.append(log)
        process = subprocess.Popen([str(binaries/'clinx-native-transport'), '--socket', str(endpoint),
            '--authority-uid', str(os.getuid()), '--host-config', str(cfg), '--timeout-seconds', '2'], stdout=log, stderr=log)
        processes.append(process)
        return process
    def start(task=None, project='fixture'):
        payload = dict(host='p620', project=project, prompt='isolated Host operation', title='Host fixture',
                       approved=True, execution_policy=dict(execution_surface='HOST_EXECUTOR', operation_scopes=[scope]))
        if task:
            payload['task_ref'] = task
        p = call('prepare', payload)
        return call('start', dict(prepared_execution_ref=p['prepared_execution_ref'], approved=True))
    authority = launch_authority(True)
    class Harness:
        def status(self, e):
            return call('get_status', dict(execution_ref=e['execution_ref']))
        def rows(self, e):
            return call('host_rows', dict(execution_ref=e['execution_ref']))['rows']
        def wait(self, e, state='SUCCEEDED'):
            return eventually(lambda: self.status(e), lambda s: s['status'] == state)
    h = Harness()
    h.provider, h.root, h.repo, h.call, h.start = provider, root, repo, call, start
    h.repo2 = repo2
    h.launch_worker, h.launch_authority, h.authority, h.config = launch_worker, launch_authority, authority, cfg
    try:
        yield h
        assert not provider.errors, provider.errors
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        provider.close()
        for log in logs:
            log.close()
        import shutil
        shutil.rmtree(root)


def test_host_process_exact_result_duplicate_requests_ack_queue_and_broadcasts(runtime):
    h = runtime
    h.provider.duplicates = True
    h.provider.calls = 2
    marker = h.repo/'calls'
    script = h.repo/'call.sh'
    script.write_text('printf x >> calls\nprintf "token=fixture-secret\\n"\n')
    h.provider.operation['arguments'] = dict(argv=['/usr/bin/sh', str(script)])
    e = h.start()
    h.launch_worker()
    h.wait(e)
    assert marker.read_text() == 'xx'
    assert len(h.provider.starts) == 1
    rows = h.rows(e)
    assert len(rows) == 2
    assert all(r['delivery_state'] == 'DELIVERED' and r['host_completion'] == 'KNOWN' for r in rows)
    assert all('fixture-secret' not in r['result']['stdout'] for r in rows)
    assert all(r['result']['stdout_sha256'] == hashlib.sha256(b'token=fixture-secret\n').hexdigest() for r in rows)
    assert all(r['id'] not in ('approval', 'foreign') for r in h.provider.responses)
    assert h.provider.starts[0]['sandboxPolicy'] == dict(type='workspaceWrite', writableRoots=[str(h.repo)], networkAccess=False)


@pytest.mark.parametrize('lost', ['input', 'result_ack'])
def test_host_lost_ack_exact_history_restart_and_no_process_replay(runtime, lost):
    h = runtime
    marker = h.repo/'calls'
    script = h.repo/'call.sh'
    script.write_text('printf x >> calls\n')
    h.provider.operation['arguments'] = dict(argv=['/usr/bin/sh', str(script)])
    h.provider.lose_input = lost == 'input'
    h.provider.lose_ack = lost == 'result_ack'
    e = h.start()
    worker = h.launch_worker()
    if lost == 'input':
        eventually(lambda: h.status(e), lambda s: s['dispatch_state'] == 'SENDING')
        # SENDING is committed before physical send. This case specifically
        # loses an ACK after Provider acceptance, rather than killing before
        # input exists (which must correctly remain UNKNOWN).
        eventually(lambda: h.provider.turns, bool)
    else:
        eventually(lambda: h.rows(e), lambda rows: bool(rows) and rows[0]['response_sent'])
    worker.kill()
    worker.wait(timeout=5)
    h.authority.kill()
    h.authority.wait(timeout=5)
    h.launch_authority()
    h.provider.lose_input = h.provider.lose_ack = False
    h.launch_worker()
    h.wait(e)
    assert marker.read_text() == 'x'
    assert len(h.provider.starts) == 1
    assert h.rows(e)[0]['delivery_state'] == 'DELIVERED'


def test_host_cancel_kills_running_process_and_preserves_exact_receipts(runtime):
    h = runtime
    script = h.repo/'cancel.sh'
    script.write_text('printf ready > ready\nexec sleep 20\n')
    h.provider.operation['arguments'] = dict(argv=['/usr/bin/sh', str(script)])
    e = h.start()
    h.launch_worker()
    eventually(lambda: (h.repo/'ready').exists(), bool)
    h.call('cancel', dict(execution_ref=e['execution_ref'], approved=True))
    h.wait(e, 'CANCELLED')
    row = h.rows(e)[0]
    assert row['host_completion'] == 'KNOWN' and row['delivery_state'] == 'DELIVERED'
    assert row['result']['cancel_requested'] is True
    assert row['result']['exit_code'] < 0
    assert len(h.provider.starts) == len(h.provider.interrupts) == 1


def test_host_pre_dispatch_rejection_is_durable_and_never_executes(runtime):
    h = runtime
    h.provider.operation['arguments'] = dict(argv=['/usr/bin/env', 'touch', str(h.repo/'forbidden')])
    e = h.start()
    h.launch_worker()
    h.wait(e)
    row = h.rows(e)[0]
    assert row['execution_state'] == 'COMMAND_NOT_DISPATCHED'
    assert row['host_ref'] is None and row['delivery_state'] == 'DELIVERED'
    assert not (h.repo/'forbidden').exists()


def test_host_competing_workers_dispatch_one_process(runtime):
    h = runtime
    script = h.repo/'concurrent.sh'
    script.write_text('sleep 1\nprintf x >> calls\n')
    h.provider.operation['arguments'] = dict(argv=['/usr/bin/sh', str(script)])
    a = h.start()
    h.launch_worker()
    h.launch_worker()
    h.wait(a)
    assert (h.repo/'calls').read_text() == 'x'
    assert len(h.provider.starts) == 1
    assert h.rows(a)[0]['delivery_state'] == 'DELIVERED'


def test_host_parallel_projects_keep_distinct_execution_and_turn_evidence(runtime):
    h = runtime
    script = h.repo/'parallel.sh'
    script.write_text('printf ready > ready\nsleep 1\nprintf x >> calls\n')
    h.provider.operation['arguments'] = dict(argv=['/usr/bin/sh', str(script)])
    a, b = h.start(), h.start(project='fixture2')
    h.launch_worker()
    eventually(lambda: (h.repo/'ready').exists() and (h.repo2/'ready').exists(), bool)
    h.wait(a)
    h.wait(b)
    assert (h.repo/'calls').read_text() == (h.repo2/'calls').read_text() == 'x'
    ar, br = h.rows(a)[0], h.rows(b)[0]
    assert ar['host_ref'] != br['host_ref']
    assert ar['identity']['thread_id'] != br['identity']['thread_id']
    assert ar['identity']['turn_id'] != br['identity']['turn_id']


def test_host_unknown_process_result_after_restart_is_never_replayed(runtime):
    h = runtime
    script = h.repo/'unknown.sh'
    script.write_text('printf x >> calls\nprintf "%s" "$$" > pid\nexec sleep 30\n')
    h.provider.operation['arguments'] = dict(argv=['/usr/bin/sh', str(script)])
    e = h.start()
    worker = h.launch_worker()
    eventually(lambda: (h.repo/'pid').exists() and bool((h.repo/'pid').read_text()), bool)
    pid = int((h.repo/'pid').read_text())
    try:
        worker.kill()
        worker.wait(timeout=5)
        # This is the process group created by this fixture's own worker.
        os.killpg(pid, signal.SIGTERM)
        h.authority.kill()
        h.authority.wait(timeout=5)
        h.launch_authority()
        h.launch_worker()
        time.sleep(3)
        assert (h.repo/'calls').read_text() == 'x'
        row = h.rows(e)[0]
        assert row['host_completion'] == 'UNKNOWN'
        assert row['call_state'] == 'RUNNING'
        assert row['reconciliation_required'] is True
        assert h.status(e)['resource_state'] == 'HELD'
        assert len(h.provider.starts) == 1
    finally:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_host_timeout_and_bounded_output_preserve_raw_evidence(runtime):
    h = runtime
    config = json.loads(h.config.read_text())
    config['max_output_bytes'] = 1024
    h.config.write_text(json.dumps(config))
    script = h.repo/'timeout.sh'
    script.write_text('printf "%02000d" 0\nprintf ready > ready\nexec sleep 20\n')
    h.provider.operation['arguments'] = dict(argv=['/usr/bin/sh', str(script)])
    h.provider.operation['timeout_seconds'] = .3
    e = h.start()
    h.launch_worker()
    h.wait(e)
    row = h.rows(e)[0]
    assert row['host_completion'] == 'KNOWN' and row['delivery_state'] == 'DELIVERED'
    result = row['result']
    assert result['timed_out'] is True and result['exit_code'] < 0
    assert result['stdout'] == '0' * 1024
    assert result['stdout_bytes'] == 2000 and result['stdout_truncated'] is True
    assert result['stdout_sha256'] == hashlib.sha256(b'0' * 2000).hexdigest()


def test_host_thread_creation_ack_unknown_survives_restart_without_recreate(runtime):
    h = runtime
    h.provider.lose_create = True
    e = h.start()
    worker = h.launch_worker()
    eventually(lambda: h.provider.creates, bool)
    worker.kill()
    worker.wait(timeout=5)
    h.authority.kill()
    h.authority.wait(timeout=5)
    h.launch_authority()
    h.provider.lose_create = False
    h.launch_worker()
    time.sleep(3)
    assert len(h.provider.creates) == 1
    assert not h.provider.starts
    assert h.status(e)['resource_state'] == 'HELD'
    assert not h.rows(e)


def test_host_canonical_checkpoint_successor_runs_once_and_keeps_task_identity(runtime):
    h = runtime
    tid = '11111111-2222-3333-4444-555555555555'
    prior = dict(id='predecessor-exact', status='completed', items=[dict(
        id='original-result', type='agentMessage', text='Exact original predecessor')])
    with h.provider.lock:
        h.provider.threads[tid] = dict(id=tid, cwd=str(h.repo), name='Native', status=dict(type='idle'))
        h.provider.turns[tid] = [prior]
    task = 'task_' + uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(
        ['p620', 'codex_app_server', tid], separators=(',', ':'))).hex
    identity = dict(node_id='p620', host='p620', provider='codex_app_server', thread_id=tid,
                    user_scope='fixture-user', task_id=task, origin='CODEX_NATIVE', title='Native',
                    cwd=str(h.repo), first_provenance_json=json.dumps(dict(node='p620',
                    node_fingerprint='fixture', observation_id='fixture-observation', digest='fixture-digest')))
    adoption = h.call('adopt', dict(host='p620', project='fixture', thread_id=tid, title='Native',
        workspace_contract=dict(cwd=str(h.repo)), exact_turn_id=prior['id'], canonical_identity=identity))
    h.launch_worker()
    tasks = eventually(lambda: h.call('list_tasks')['tasks'], lambda rows: bool(rows))
    assert len(tasks) == 1 and tasks[0]['task_id'] == task == adoption['task_ref']
    current = tasks[0]
    scope = dict(capability='LOCAL_HOST_PROCESS', operation='development_command',
                 operation_class='DEVELOPMENT_MUTATION', target='')
    change = h.call('prepare_policy', dict(task_ref=task, expected_policy_hash=current['policy_hash'],
        target_policy=dict(execution_surface='HOST_EXECUTOR', operation_scopes=[scope]),
        reason='Host continuation qualification', approved=True))
    h.call('apply_policy', dict(prepared_reauthorization_ref=change['prepared_reauthorization_ref'],
        expected_request_hash=change['request_hash'], expected_task_ref=task, approved=True))
    e = h.start(task)
    h.wait(e)
    final = h.call('list_tasks')['tasks'][0]
    assert final['task_id'] == task and final['thread_id'] != tid
    assert final['canonical_handoff']['identity'] == identity
    assert final['lineage'][0]['source']['turn'] == prior
    assert final['lineage'][0]['state'] == 'COMMITTED'
    eventually(lambda: h.provider.threads[final['thread_id']]['name'], lambda title: title == 'Host fixture')
    assert h.provider.threads[tid]['name'] == 'Native'
    assert len(h.provider.creates) == len(h.provider.starts) == 1
    context = h.provider.creates[0]['developerInstructions']
    assert tid in context and prior['id'] in context and 'Exact original predecessor' in context
    assert h.rows(e)[0]['delivery_state'] == 'DELIVERED'
