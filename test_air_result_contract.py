"""Air 缺陷回归：隔离 fixtures、scripted Provider、内存 RPC 帧，无真实任务。"""
import dataclasses
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app_server
import bridge
import node_protocol as node
from test_bridge import dispatcher_fixture as config_fixture, target_fixture
from test_native_workspace_policy import local, prepare
from test_native_interop import native, TID
from test_node_protocol import record, scope


@pytest.mark.parametrize('flags', [(), ('--sock', 'owned.sock'), ('--sock=owned.sock',),
    ('--sock', 'owned.sock', '--sock=./owned.sock')])
@pytest.mark.parametrize('configured', [True, False])
def test_local_socket_normalized_once(tmp_path, monkeypatch, flags, configured):
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path))
    cfg, _ = config_fixture()
    cfg = dataclasses.replace(cfg, app_server=dataclasses.replace(cfg.app_server,
        command=('codex', 'app-server', 'proxy', *flags),
        local_socket=str(tmp_path / 'owned.sock') if configured else None))
    client = bridge._default_app_server_client(cfg, target_fixture())
    expected = ('codex', 'app-server', 'proxy')
    endpoint = str(tmp_path / 'owned.sock') if flags or configured else None
    if endpoint:
        expected += ('--sock', endpoint)
    assert client.transport.command == expected
    assert client.provider_endpoint == endpoint


@pytest.mark.parametrize('flags,configured', [
    (('--sock', 'first', '--sock=second'), None),
    (('--sock=first',), 'second'), (('--sock',), None), (('--sock=',), None),
    (('--sock', '--other'), None)])
def test_conflicting_or_missing_socket_rejected_before_transport(monkeypatch, flags, configured):
    cfg, _ = config_fixture()
    cfg = dataclasses.replace(cfg, app_server=dataclasses.replace(cfg.app_server,
        command=('codex', 'app-server', 'proxy', *flags), local_socket=configured))
    transport = Mock(side_effect=AssertionError('不得启动传输'))
    monkeypatch.setattr(bridge, 'LocalStdioTransport', transport)
    with pytest.raises(bridge.BridgeError, match='socket|--sock'):
        bridge._default_app_server_client(cfg, target_fixture())
    transport.assert_not_called()


def test_remote_command_keeps_remote_endpoint():
    cfg, _ = config_fixture()
    remote = ('codex', 'app-server', 'proxy', '--sock', '/remote/owned')
    cfg = dataclasses.replace(cfg, runtime_host='air', app_server=dataclasses.replace(
        cfg.app_server, transport='ssh', local_socket='/local/owned', remote_command=remote))
    client = bridge._default_app_server_client(cfg, target_fixture(target_host='p620'))
    assert isinstance(client.transport, app_server.SSHStdioTransport)
    assert client.transport.remote_command == remote
    assert client.provider_endpoint is None


class Wire:
    def __init__(self):
        self.sent = []
    def send(self, message):
        self.sent.append(message)
    def receive(self, _timeout):
        message = self.sent[-1]
        if message['method'] == 'thread/resume':
            result = {'thread': {'id': message['params']['threadId']}}
        else:
            result = {'turn': {'id': 'wire-turn'}}
        return {'id': message['id'], 'result': result}


@pytest.mark.parametrize('network', [False, True])
@pytest.mark.parametrize('transport', ['local', 'ssh'])
def test_native_continuation_contract_survives_loaded_resume(local, network, transport):
    integration, dispatcher, provider, repo = local
    cfg = dataclasses.replace(dispatcher.cfg, runtime_host='p620' if transport == 'local' else 'air',
        app_server=dataclasses.replace(dispatcher.cfg.app_server, transport=transport))
    dispatcher.cfg = integration.cfg = cfg
    first = integration.start_execution(prepared_execution_ref=prepare(
        integration, network_access=network)['prepared_execution_ref'], approved=True)
    task_id = first['task_ref']
    original = dispatcher.tasks.get_binding(task_id)
    dispatcher.tasks.reconcile_terminal(first['execution_ref'], 'COMPLETED')
    provider.calls.clear()
    wire = Wire()
    client = app_server.CodexAppServerClient(wire)
    # 模拟已加载线程：resume 接受请求但忽略 developer override。
    provider.thread_resume = lambda thread_id, **kw: client.thread_resume(thread_id, **kw)
    provider.turn_start = lambda thread_id, prompt, **kw: client.turn_start(thread_id, prompt, **kw)
    continued = integration.prepare_execution(approved=True, task_ref=task_id, prompt='继续原任务')
    result = integration.start_execution(prepared_execution_ref=continued['prepared_execution_ref'], approved=True)
    resume, turn = wire.sent
    assert resume['method'] == 'thread/resume'
    assert 'CLINX_EXECUTION_RESULT\nSTATUS=' in resume['params']['developerInstructions']
    assert turn['method'] == 'turn/start'
    text = turn['params']['input'][0]['text']
    assert text.count('CLINX_EXECUTION_RESULT\nSTATUS=') == 1
    assert '继续原任务' in text
    assert 'PASS requires BLOCKERS=NONE exactly' in text
    assert turn['params']['threadId'] == original.thread_id
    assert turn['params']['sandboxPolicy']['networkAccess'] == network
    assert turn['params']['approvalPolicy'] == 'never'
    assert dispatcher.tasks.get_binding(task_id).thread_id == original.thread_id
    assert not any(c[0] == 'thread/start' for c in provider.calls)
    assert result['task_ref'] == task_id


@pytest.mark.parametrize('operation', ['execution.status', 'execution.context', 'execution.start'])
def test_router_read_failure_is_not_unknown_side_effect(operation):
    registry = node.NodeRegistry(':memory:')
    router = node.NodeRouter(registry, user_scope='user-a')
    executor = Mock(side_effect=TimeoutError('PRIVATE token=secret'))
    router.attach(record('air'), reader=lambda _: {}, executor=executor, scope=scope(execute=True))
    result = router.execute(node_id='air', operation=operation, request={'execution_ref':'exec_fixture'})
    assert 'PRIVATE' not in json.dumps(result) and 'secret' not in json.dumps(result)
    assert executor.call_count == 1
    if operation == 'execution.start':
        assert result['side_effect'] == 'UNKNOWN'
        assert result['retry'] == 'RECONCILIATION_REQUIRED'
    else:
        assert result['error_code'] == 'NODE_READ_UNAVAILABLE'
        assert result['side_effect'] == 'NONE' and result['read_only']
        assert result['retry'] == 'READ_ONLY_RETRY'
        assert result['failure_stage'] == 'rpc_transport'


def rpc_frame(payload, callback):
    handler = object.__new__(node._RPCHandler)
    handler.rfile = io.BytesIO(payload)
    handler.wfile = io.BytesIO()
    handler.server = SimpleNamespace(owner=SimpleNamespace(handle_request=callback))
    handler.request = None
    handler.client_address = ('fixture', 0)
    handler.handle()
    return json.loads(handler.wfile.getvalue())


@pytest.mark.parametrize('failure', ['dispatch', 'serialize'])
def test_valid_read_rpc_internal_error_is_diagnostic_and_redacted(failure):
    def callback(*_):
        if failure == 'dispatch':
            raise TypeError('PRIVATE token=secret')
        return {'not_json': object()}
    result = rpc_frame(b'{"operation":"execution.status"}\n', callback)
    assert result['error_code'] == 'NODE_READ_FAILED'
    assert result['failure_stage'] == 'rpc_' + failure
    assert result['error_type'] == 'TypeError'
    assert result['read_only'] and result['side_effect'] == 'NONE'
    assert 'PRIVATE' not in json.dumps(result) and 'secret' not in json.dumps(result)


def test_invalid_rpc_json_remains_invalid_request():
    callback = Mock(side_effect=AssertionError('不得处理无效帧'))
    result = rpc_frame(b'not-json\n', callback)
    assert result['error_code'] == 'INVALID_NODE_REQUEST'
    callback.assert_not_called()


@pytest.mark.parametrize('spelling', ['separate', 'equals'])
def test_new_task_uses_normalized_default_factory(local, monkeypatch, tmp_path, spelling):
    integration, dispatcher, provider, _ = local
    endpoint = str(tmp_path / 'owned.sock')
    flag = ('--sock', endpoint) if spelling == 'separate' else ('--sock=' + endpoint,)
    cfg = dataclasses.replace(dispatcher.cfg, app_server=dataclasses.replace(
        dispatcher.cfg.app_server, command=('codex', 'app-server', 'proxy', *flag), local_socket=endpoint))
    created = []
    def client(transport, **kwargs):
        created.append(transport.command)
        return provider
    monkeypatch.setattr(bridge, 'CodexAppServerClient', client)
    dispatcher.cfg = integration.cfg = cfg
    dispatcher.client_factory = lambda target: bridge._default_app_server_client(cfg, target)
    dispatcher._uses_default_client_factory = True
    result = integration.start_execution(prepared_execution_ref=prepare(integration)['prepared_execution_ref'], approved=True)
    assert created == [('codex', 'app-server', 'proxy', '--sock', endpoint)]
    assert result['execution_started']
    assert len([c for c in provider.calls if c[0] == 'thread/start']) == 1
    assert len([c for c in provider.calls if c[0] == 'turn/start']) == 1
    assert dispatcher.tasks.get_binding(result['task_ref']).thread_id == provider.thread_id


def test_equivalent_socket_symlink_and_home_are_one(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    root = tmp_path / 'real'
    root.mkdir()
    (tmp_path / 'alias').symlink_to(root, target_is_directory=True)
    cfg, _ = config_fixture()
    cfg = dataclasses.replace(cfg, app_server=dataclasses.replace(cfg.app_server,
        command=('codex', 'app-server', 'proxy', '--sock=~/alias/owned.sock'),
        local_socket=str(root / 'owned.sock')))
    client = bridge._default_app_server_client(cfg, target_fixture())
    assert client.transport.command == ('codex', 'app-server', 'proxy', '--sock', str(root / 'owned.sock'))


@pytest.mark.parametrize('payload', [b'[]\n', b'{"operation":[]}\n', b'{}\n'])
def test_invalid_rpc_shape_does_not_dispatch(payload):
    callback = Mock(side_effect=AssertionError('不得处理无效帧'))
    assert rpc_frame(payload, callback)['error_code'] == 'INVALID_NODE_REQUEST'
    callback.assert_not_called()


def test_mutating_rpc_failure_remains_unknown():
    result = rpc_frame(b'{"operation":"execution.start"}\n', Mock(side_effect=TypeError('PRIVATE')))
    assert result['error_code'] == 'NODE_REQUEST_FAILED'
    assert result['side_effect'] == 'UNKNOWN' and not result['read_only']
    assert result['retry'] == 'RECONCILIATION_REQUIRED'


def test_terminal_concurrent_opaque_status_and_exact_context(native, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from m9_integration import ClinxIntegration, ExecutionFinalizer
    from node_execution_adapter import CanonicalNodeExecutionAdapter
    from task_registry import TaskRegistry
    from unittest.mock import patch
    from test_pvx1812_completion import RESULT

    cfg = dataclasses.replace(native.cfg, app_server=dataclasses.replace(
        native.cfg.app_server, native_home=str(native.root)))
    native.integration.cfg = native.dispatcher.cfg = native.reader.cfg = cfg
    native.integration.linear = None
    with patch('thread_identity.ThreadIdentityReader', return_value=native.reader):
        task = native.integration.adopt_conversation(thread_id=TID)['task_ref']
    ref = 'exec_terminal_fixture'
    with native.registry.execution(task, execution_ref=ref, retain=True):
        native.registry.set_execution_state(task, 'CODEX_RUNNING', turn_id='turn-2', codex_running=True)
    adapter = CanonicalNodeExecutionAdapter(native.integration, 'p620')
    service = node.NodeService(record('p620'), node.NodeRegistry(':memory:'), scope=scope(),
        read_thread=Mock(side_effect=AssertionError('精确 context 不得走泛查')),
        status_execution=adapter.status, context_execution=adapter.context)
    registry = node.NodeRegistry(tmp_path / 'routes.db')
    router = node.NodeRouter(registry, user_scope='user-a', local_node_id='centre')
    calls = []
    def execute(operation, request):
        calls.append(operation)
        payload = json.dumps(dict(request, operation=operation)).encode() + b'\n'
        return rpc_frame(payload, lambda req, *_: service.handle(req))
    router.attach(record('p620'), reader=Mock(), executor=execute, scope=scope())
    registry.bind_reference(ref, 'p620', 'user-a')
    centre_registry = TaskRegistry(tmp_path / 'centre.db')
    centre = ClinxIntegration(dataclasses.replace(cfg, runtime_host='centre', node_router=router),
        centre_registry, Mock(), Mock(), None)
    barrier = threading.Barrier(3)
    def read(kind):
        barrier.wait(timeout=5)
        results = []
        for _ in range(20):
            result = (centre.get_status(execution_ref=ref) if kind == 'status' else
                      centre.get_context(thread_id=TID, host='p620', execution_ref=ref, recent_turns=1, max_bytes=8000))
            if kind == 'context' and result.get('error_code'):
                # 既有读前/读后 fingerprint 守卫会拒绝终态写入期间的不一致快照。
                # 保留此保护；它不是 INVALID_NODE_REQUEST 或执行副作用 UNKNOWN。
                assert result['error_code'] == 'THREAD_LOOKUP_UNAVAILABLE', result
                assert result['unavailable_reason'] == 'THREAD_IDENTITY_CHANGED_DURING_READ'
                assert result['read_only']
            else:
                assert not result.get('error_code'), result
                assert result['execution_ref'] == ref and result['task_ref'] == task
            json.dumps(result)
            results.append(result)
        return results
    with ThreadPoolExecutor(max_workers=2) as pool:
        readers = [pool.submit(read, kind) for kind in ('status', 'context')]
        barrier.wait(timeout=5)
        ExecutionFinalizer(native.registry, None).finalize(
            execution_ref=ref, task_id=task, turn_id='turn-2', raw_result=RESULT)
        for future in readers:
            assert len(future.result(timeout=15)) == 20
    final = centre.get_status(execution_ref=ref)
    assert final['execution_result']['status'] == 'PASS'
    assert final['EXECUTION_STATE'] == 'COMPLETED'
    context = centre.get_context(thread_id=TID, host='p620', execution_ref=ref, recent_turns=1, max_bytes=8000)
    assert not context.get('error_code'), context
    assert context['execution_ref'] == ref and context['task_ref'] == task
    assert context['context_status'] == 'AVAILABLE'
    assert set(calls) == {'execution.status', 'execution.context'}
    with centre_registry._connect() as conn:
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    # 错误 host 不得绕过 opaque 引用路由。
    router.attach(record('other'), reader=Mock(), executor=Mock(), scope=scope())
    with pytest.raises(node.NodeProtocolError, match='conflicts'):
        centre.get_status(execution_ref=ref, host='other')
