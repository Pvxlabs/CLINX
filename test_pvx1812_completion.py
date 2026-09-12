"""PVX-1812: real CLINX classes/SQLite, scripted wire (not live Provider E2E)."""
from __future__ import annotations

from contextlib import closing
import dataclasses
from pathlib import Path
import queue
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

import app_server
import bridge
from m9_integration import ClinxIntegration, ExecutionFinalizer
from task_registry import TaskRegistry
import test_m13b

RESULT = '''CLINX_EXECUTION_RESULT
STATUS=PASS
SUMMARY=Deterministic completion fixture
CHANGED_FILES=NONE
VALIDATION=scripted protocol fixture
BLOCKERS=NONE
NEXT_STATE=COMPLETED'''


class Wire:
    """A synchronous RPC wire with a separate connection for observation."""
    def __init__(self, server):
        self.server = server
        self.messages = queue.Queue()
        self.sent = []
        self.closed = False

    def send(self, message):
        self.sent.append(message)
        method, params = message.get('method'), message.get('params', {})
        if method is None or 'id' not in message:
            return
        if method == 'initialize':
            result = {'serverInfo': {'name': 'codex', 'version': 'test'}}
        elif method == 'model/list':
            result = {'data': [{'id': 'model', 'supportedReasoningEfforts': []}]}
        elif method in {'thread/start','thread/read','thread/resume'}:
            if method == 'thread/start':
                self.server.thread_id = 'thread-new'
            result = {'thread': self.server.thread()}
        elif method == 'turn/start':
            self.server.starts += 1
            self.server.turn_id = f'turn-{self.server.starts}'
            result = {'turn': {'id': self.server.turn_id}}
            event = self.server.terminal()
            if self.server.early:
                self.messages.put(event)
        elif method == 'thread/turns/list':
            if self.server.observation_error:
                raise app_server.AppServerTransportError('transport closed')
            result = {'data': [self.server.turn()], 'nextCursor': None}
        elif method == 'thread/items/list':
            result = {'data': self.server.turn()['items'], 'nextCursor': None}
        else:
            raise AssertionError(method)
        self.messages.put({'id': message['id'], 'result': result})
        if method == 'turn/start' and not self.server.early:
            self.messages.put(self.server.terminal())

    def receive(self, timeout):
        try:
            item = self.messages.get(timeout=min(timeout, 2))
        except queue.Empty as exc:
            raise app_server.AppServerTransportError('timed out') from exc
        if item is None:
            raise app_server.AppServerTransportError('transport closed')
        return item

    def close(self):
        self.closed = True
        self.messages.put(None)


class Server:
    def __init__(self, root, early):
        self.root = root
        self.early = early
        self.starts = 0
        self.turn_id = ''
        self.thread_id = 'thread-exact'
        self.clients = []
        self.observation_error = False
        self.status = 'completed'

    def thread(self):
        return {'id': self.thread_id, 'sessionId': self.thread_id, 'projectId': None,
                'cwd': str(self.root), 'ephemeral': False,
                'gitInfo': {'originUrl': None, 'branch': 'main'},
                'canAcceptDirectInput': True, 'status': {'type': 'idle'}}

    def turn(self):
        return {'id': self.turn_id, 'status': self.status,
                'items': [{'type': 'agentMessage', 'text': RESULT}]}

    def terminal(self):
        return {'method': 'turn/completed', 'params': {'threadId': self.thread_id, 'turn': self.turn()}}

    def client(self, _target):
        client = app_server.CodexAppServerClient(Wire(self), timeout_seconds=0.2)
        self.clients.append(client)
        return client


@pytest.fixture
def setup(tmp_path):
    built = []
    def build(*, early=False, start_runtime=True):
        dispatcher, registry, task, _ = test_m13b.ProviderThreadMigrationTests().fixture(tmp_path, thread_id='thread-exact')
        dispatcher._completion_runtime = None
        dispatcher.linear = None
        dispatcher.cfg.host_executor = dispatcher.host_executor.config
        dispatcher.cfg.team_id = 'team'
        dispatcher.cfg.trigger_label = 'local-codex'
        dispatcher.cfg.todo_state = 'Todo'
        dispatcher.cfg.review_state = 'In Review'
        dispatcher.cfg.projects = (bridge.ProjectMapping(linear_name='Pilot', alias='pilot', repo=tmp_path,
                                                           workspace_alias='p620', branch='main'),)
        dispatcher.projects = bridge.DynamicProjectResolver(dispatcher.workspaces, dispatcher.cfg.projects)
        server = Server(tmp_path, early)
        dispatcher.client_factory = server.client
        if not start_runtime:
            from completion_runtime import CompletionRuntime
            runtime = CompletionRuntime(registry, dispatcher.reconcile_execution, interval_seconds=0.01)
            dispatcher._completion_runtime = runtime
            dispatcher.start_completion_runtime = lambda: runtime
        built.append((dispatcher, server))
        return dispatcher, registry, task, server
    yield build
    # Always stop test-owned consumers without touching any real provider.
    for dispatcher, server in built:
        runtime = getattr(dispatcher, "_completion_runtime", None)
        if runtime is not None:
            runtime.stop()
        for client in server.clients:
            if client._supervisor is not None:
                client._supervisor.join(1)


def dispatch(dispatcher, task, reference, *, new=False):
    return dispatcher.dispatch(project_ref='pilot', host='p620', project_mode='existing',
          task_mode='new' if new else 'continue', task_id=None if new else task.task_id,
          prompt='Complete the deterministic fixture; do not invoke nested control-plane tools.',
          title='fixture-'+reference, summary='fixture', model='model', reasoning_effort=None,
          execution_ref=reference)


def wait_for_result(registry, reference, timeout=3):
    # No get_status/reconcile here: this must observe already committed truth.
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with closing(sqlite3.connect(registry.path)) as conn:
            result = conn.execute('SELECT status FROM execution_results WHERE execution_ref=?', (reference,)).fetchone()
            lease = conn.execute('SELECT 1 FROM worktree_leases WHERE execution_ref=?', (reference,)).fetchone()
        if result and not lease:
            return result[0]
        time.sleep(0.005)
    return None


@pytest.mark.parametrize('early', [False, True])
def test_completed_wire_turn_finalizes_without_status_query(setup, early):
    dispatcher, registry, task, server = setup(early=early)
    try:
        dispatch(dispatcher, task, 'exec_autonomous')
        assert wait_for_result(registry, 'exec_autonomous') == 'PASS'
        assert registry.get_task(task.task_id).execution_state == 'COMPLETED'
        assert server.starts == 1
    finally:
        runtime = getattr(dispatcher, '_completion_runtime', None)
        if runtime is not None:
            runtime.stop()


def test_new_and_continuation_use_autonomous_completion(setup):
    dispatcher, registry, task, server = setup(early=True)
    try:
        result = dispatch(dispatcher, task, 'exec_new', new=True)
        assert wait_for_result(registry, 'exec_new') == 'PASS'
        created = registry.get_task(result.task_id)
        dispatch(dispatcher, created, 'exec_continue')
        assert wait_for_result(registry, 'exec_continue') == 'PASS'
        assert registry.get_execution_result('exec_new').turn_id != registry.get_execution_result('exec_continue').turn_id
        assert server.starts == 2
    finally:
        dispatcher.stop_completion_runtime()


def test_real_prepare_start_cannot_overwrite_fast_terminal(setup):
    dispatcher, registry, task, server = setup(early=True)
    context = SimpleNamespace(resolve_task=lambda **_: registry.get_task(task.task_id))
    integration = ClinxIntegration(dispatcher.cfg, registry, dispatcher, context, linear=None)
    try:
        prepared = integration.prepare_execution(approved=True, task_ref=task.task_id, model='model', reasoning='medium',
                                                prompt='Do the scripted work.')
        # The scripted model advertises no reasoning effort; use the existing
        # dispatcher resolver only to isolate the lifecycle, not model policy.
        original = server.client
        def client(target):
            value = original(target)
            value.resolve_model = lambda model, reasoning: ('model', None)
            return value
        dispatcher.client_factory = client
        output = integration.start_execution(prepared_execution_ref=prepared['prepared_execution_ref'], approved=True)
        reference = output['execution_ref']
        assert wait_for_result(registry, reference) == 'PASS'
        assert registry.get_task(task.task_id).execution_state == 'COMPLETED'
    finally:
        dispatcher.stop_completion_runtime()


def test_restart_recovers_registered_handoff_without_new_turn(setup):
    from completion_runtime import CompletionRuntime
    dispatcher, registry, task, server = setup(start_runtime=False)
    dispatch(dispatcher, task, 'exec_restart')
    for client in server.clients:
        if client._supervisor:
            client._supervisor.join(1)
    assert registry.get_execution_result('exec_restart') is None
    reopened = TaskRegistry(registry.path)
    recovered = CompletionRuntime(reopened, dispatcher.reconcile_execution)
    assert recovered.run_once() == 1
    assert wait_for_result(registry, 'exec_restart') == 'PASS'
    assert recovered.inspect('exec_restart')['state'] == 'DONE'
    assert server.starts == 1


def test_partial_terminal_commit_recovers_exact_lease(setup, monkeypatch):
    dispatcher, registry, task, server = setup(start_runtime=False)
    dispatch(dispatcher, task, 'exec_partial')
    original = registry.release_execution
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('injected release fault')
    monkeypatch.setattr(registry, 'release_execution', fail)
    first = dispatcher._completion_runtime.process('exec_partial')
    assert first['completion_delivery'] == 'PENDING'
    assert registry.get_execution_result('exec_partial') is not None
    assert registry.has_execution_lease('exec_partial')
    monkeypatch.setattr(registry, 'release_execution', original)
    second = dispatcher._completion_runtime.process('exec_partial')
    assert second['completion_delivery'] == 'DONE'
    assert not registry.has_execution_lease('exec_partial')
    assert server.starts == 1


def test_observation_disconnect_does_not_release_or_finalize(setup):
    dispatcher, registry, task, server = setup(start_runtime=False)
    dispatch(dispatcher, task, 'exec_disconnected')
    server.observation_error = True
    result = dispatcher._completion_runtime.process('exec_disconnected')
    assert result['completion_delivery'] == 'PENDING'
    assert registry.get_execution_result('exec_disconnected') is None
    assert registry.has_execution_lease('exec_disconnected')
    assert server.starts == 1


def test_repeated_delivery_and_concurrent_reconcile_are_idempotent(setup):
    dispatcher, registry, task, server = setup(start_runtime=False)
    dispatch(dispatcher, task, 'exec_duplicate')
    errors=[]
    def run():
        try:
            dispatcher._completion_runtime.process('exec_duplicate')
        except Exception as exc:
            errors.append(exc)
    threads=[threading.Thread(target=run) for _ in range(4)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(3)
    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert wait_for_result(registry, 'exec_duplicate') == 'PASS'
    with closing(sqlite3.connect(registry.path)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM execution_results WHERE execution_ref=?', ('exec_duplicate',)).fetchone()[0] == 1
    assert server.starts == 1


@pytest.mark.parametrize('thread_id,turn_id', [('wrong','turn'),('thread',None),('thread','old')])
def test_supervisor_ignores_wrong_and_unattributed_terminal(thread_id, turn_id):
    server=Server(Path('/tmp'), False)
    wire=Wire(server)
    client=app_server.CodexAppServerClient(wire, timeout_seconds=0.2)
    client.configure_dynamic_tool(namespace='clinx',name='clinx_host_operation',thread_id='thread',handler=lambda _: {})
    seen=[]
    client.configure_completion_handoff('thread','turn',seen.append)
    wire.messages.put({'method':'turn/completed','params':{'threadId':thread_id,'turn':{'id':turn_id}}})
    wire.messages.put({'method':'turn/completed','params':{'threadId':'thread','turn':{'id':'turn'}}})
    client.supervise_turn('thread','turn')
    client._supervisor.join(2)
    assert len(seen)==1
    assert seen[0]['params']['turn']['id']=='turn'


def test_exact_recovery_never_sweeps_other_leases(setup, monkeypatch):
    dispatcher, registry, task, server = setup(start_runtime=False)
    dispatch(dispatcher, task, 'exec_scoped')
    def forbidden():
        raise AssertionError('global lease sweep must not be called')
    monkeypatch.setattr(registry,'reclaim_stale_worktree_leases',forbidden)
    result=dispatcher.recover_execution_completion('exec_scoped')
    assert result['completion_delivery']=='DONE'
    assert server.starts == 1


def test_process_exit_after_result_commit_is_recoverable(setup):
    import os
    import subprocess
    import sys
    dispatcher, registry, task, server = setup(start_runtime=False)
    dispatch(dispatcher, task, 'exec_crash')
    for client in server.clients:
        if client._supervisor:
            client._supervisor.join(1)
    code = '''import os, sys
from task_registry import TaskRegistry
from m9_integration import ExecutionFinalizer
registry = TaskRegistry(sys.argv[1])
registry.release_execution = lambda *a, **k: os._exit(79)
ExecutionFinalizer(registry).finalize(execution_ref='exec_crash',task_id=sys.argv[2],turn_id=sys.argv[3],raw_result=sys.argv[4])
'''
    process = subprocess.run([sys.executable, '-c', code, str(registry.path), task.task_id, server.turn_id, RESULT],
                             cwd=Path(__file__).parent, capture_output=True, text=True, timeout=5)
    assert process.returncode == 79, process.stderr
    assert registry.get_execution_result('exec_crash') is not None
    assert registry.has_execution_lease('exec_crash')
    assert dispatcher.recover_execution_completion('exec_crash')['completion_delivery'] == 'DONE'
    assert server.starts == 1


def test_post_start_handoff_failure_keeps_execution_recoverable(setup, monkeypatch):
    dispatcher, registry, task, server = setup(start_runtime=False)
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('injected journal failure')
    monkeypatch.setattr(dispatcher,'_register_managed_completion',fail)
    with pytest.raises(sqlite3.OperationalError):
        dispatch(dispatcher,task,'exec_handoff_fail')
    assert registry.get_task(task.task_id).execution_state == 'RECOVERY_REQUIRED'
    assert registry.has_execution_lease('exec_handoff_fail')
    assert registry.get_execution_result('exec_handoff_fail') is None
    assert dispatcher.recover_execution_completion('exec_handoff_fail')['completion_delivery'] == 'DONE'
    assert server.starts == 1


def test_bad_host_parameters_rejected_before_executor(setup, monkeypatch):
    dispatcher, registry, task, server = setup(start_runtime=False)
    dispatch(dispatcher,task,'exec_tools')
    from execution_policy import build_development_policy
    policy=build_development_policy()
    spec=dispatcher._managed_host_spec(policy)
    properties=spec['tools'][0]['inputSchema']['properties']
    assert properties['operation_class']['enum'] == ['DEVELOPMENT_MUTATION']
    assert 'filesystem' not in properties['capability']['enum']
    client=app_server.CodexAppServerClient(Wire(server))
    def forbidden(*args,**kwargs):
        raise AssertionError('invalid request reached HostExecutor')
    monkeypatch.setattr(dispatcher.host_executor,'execute',forbidden)
    project=dispatcher.cfg.projects[0]
    dispatcher._configure_host_turn(client=client,task_id=task.task_id,execution_ref='exec_tools',
        route=registry.get_execution_routing_identity('exec_tools'),policy=policy,project=project,
        thread_id=server.thread_id)
    for fields in [{'operation_class':'LOCAL_HOST_PROCESS'}, {'capability':'filesystem'}, {'capability':'host_process'},
                   {'operation_class':['DEVELOPMENT_MUTATION']}, {'execution_ref':'other'}]:
        with pytest.raises(bridge.DispatchContractError):
            client._dynamic_tool_handler({'arguments':{**fields,'arguments':{}}})
    prompt=dispatcher._managed_host_prompt('write a test',policy)
    assert 'Do not call clinx_prepare_execution' in prompt
    assert 'development_command' in prompt
    client.close()


def test_linear_failure_cannot_hold_local_completion(setup):
    dispatcher, registry, task, server=setup(start_runtime=False)
    dispatch(dispatcher,task,'exec_linear_failure')
    class BrokenLinear:
        def __getattr__(self,name):
            raise RuntimeError('projection failure')
    dispatcher.linear=BrokenLinear()
    dispatcher.cfg.team_id='team'
    result=dispatcher._completion_runtime.process('exec_linear_failure')
    assert result['completion_delivery']=='DONE'
    assert registry.get_execution_result('exec_linear_failure') is not None
    assert not registry.has_execution_lease('exec_linear_failure')
    assert server.starts == 1
