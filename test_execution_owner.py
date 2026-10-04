import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from execution_owner import ExecutionOwner, ExecutionOwnerClient, ExecutionOwnerError, serve, socket_path
from task_registry import TaskRegistry


@pytest.fixture
def integration(tmp_path):
    registry = TaskRegistry(tmp_path / 'tasks.sqlite3')
    # These tests isolate process/socket ownership. Sealed preparation,
    # canonical policy and real Provider round trips have separate coverage.
    registry.verify_prepared_execution = lambda ref: SimpleNamespace(prepared_execution_ref=ref)
    calls = []
    entered, release = threading.Event(), threading.Event()

    def start(**request):
        calls.append(request)
        entered.set()
        assert release.wait(5)
        return {'execution_started': True, 'execution_ref': 'exec_' + 'a' * 32}

    yield SimpleNamespace(registry=registry, start_execution=start, calls=calls,
                          entered=entered, release=release,
                          dispatcher=SimpleNamespace(stop_completion_runtime=lambda: None))


def request(integration):
    return dict(prepared_execution_ref='prepared_' + 'a' * 32, approved=True,
                database=str(integration.registry.path.resolve()))


def test_requester_disconnect_does_not_terminate_owner_or_repeat_dispatch(integration):
    ready, stop = threading.Event(), threading.Event()
    thread = threading.Thread(target=serve, args=(integration,), kwargs={'ready': ready, 'stop': stop})
    thread.start()
    try:
        assert ready.wait(5)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path(integration.registry)))
            client.sendall(json.dumps(request(integration)).encode() + b'\n')
            assert integration.entered.wait(5)
        # The MCP requester is now gone while canonical dispatch is running.
        integration.release.set()
        result = ExecutionOwnerClient(integration.registry).start(
            prepared_execution_ref=request(integration)['prepared_execution_ref'], approved=True)
        assert result['execution_started'] is True
        assert result['execution_owner']['lifetime'] == 'PERSISTENT_CONTROL_RUNTIME'
        assert len(integration.calls) == 1
        assert thread.is_alive()
    finally:
        integration.release.set()
        stop.set()
        thread.join(5)
    assert not thread.is_alive()


def test_owner_crash_after_claim_is_not_a_dispatch_retry(integration):
    owner = ExecutionOwner(integration)
    with integration.registry._connect() as conn:
        conn.execute('INSERT INTO clinx_execution_owners VALUES (?,?,?,?,?,?,?,?,NULL)',
                     ('exec_' + 'a' * 32, 'prepared_' + 'a' * 32, 'dead-owner', 1, '/prior', 'CLAIMED', 1, 1))
    with pytest.raises(ExecutionOwnerError, match='RECOVERY_REQUIRED'):
        owner.start(request(integration))
    assert integration.calls == []


def test_missing_owner_fails_before_any_local_dispatch(integration):
    with pytest.raises(ExecutionOwnerError, match='EXECUTION_OWNER_UNAVAILABLE'):
        ExecutionOwnerClient(integration.registry).start(
            prepared_execution_ref=request(integration)['prepared_execution_ref'], approved=True)
    assert integration.calls == []


def test_socket_is_private_and_independent_of_shared_db_directory(integration):
    integration.registry.path.parent.chmod(0o775)
    before = integration.registry.path.parent.stat().st_mode
    ready, stop = threading.Event(), threading.Event()
    thread = threading.Thread(target=serve, args=(integration,), kwargs={'ready': ready, 'stop': stop})
    thread.start()
    try:
        assert ready.wait(5)
        endpoint = socket_path(integration.registry)
        assert endpoint.parent != integration.registry.path.parent
        assert endpoint.parent.stat().st_mode & 0o077 == 0
        assert endpoint.stat().st_mode & 0o077 == 0
        assert integration.registry.path.parent.stat().st_mode == before
        assert len(str(endpoint).encode()) < 104
    finally:
        stop.set()
        thread.join(5)


@pytest.mark.parametrize('change', [{'approved': False}, {'database': '/foreign.sqlite3'},
                                    {'prepared_execution_ref': '../arbitrary'}, {'prepared_execution_ref': []},
                                    {'prompt': 'extra authority'}])
def test_owner_rejects_unsealed_or_foreign_request(integration, change):
    owner = ExecutionOwner(integration)
    with pytest.raises(ExecutionOwnerError, match='INVALID_REQUEST'):
        owner.start({**request(integration), **change})
    assert integration.calls == []


def test_second_owner_cannot_replace_live_socket(integration):
    ready, stop = threading.Event(), threading.Event()
    thread = threading.Thread(target=serve, args=(integration,), kwargs={'ready': ready, 'stop': stop})
    thread.start()
    try:
        assert ready.wait(5)
        identity = socket_path(integration.registry).stat().st_ino
        with pytest.raises(BlockingIOError):
            serve(integration)
        assert socket_path(integration.registry).stat().st_ino == identity
    finally:
        stop.set()
        thread.join(5)


def test_shutdown_drains_dispatch_and_rejects_new_start(integration):
    ready, stop, active = threading.Event(), threading.Event(), threading.Event()
    active.set()
    integration.registry.get_execution_record = lambda _: {'stage': 'RUNNING' if active.is_set() else 'COMPLETED'}
    thread = threading.Thread(target=serve, args=(integration,), kwargs={'ready': ready, 'stop': stop})
    thread.start()
    try:
        assert ready.wait(5)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path(integration.registry)))
            client.sendall(json.dumps(request(integration)).encode() + b'\n')
            assert integration.entered.wait(5)
            stop.set()
            integration.release.set()
            response = json.loads(client.makefile('rb').readline())
            assert response['result']['execution_started']
        time.sleep(0.3)
        assert thread.is_alive()
        with pytest.raises(ExecutionOwnerError, match='DRAINING'):
            ExecutionOwnerClient(integration.registry).start(
                prepared_execution_ref=request(integration)['prepared_execution_ref'], approved=True)
        assert len(integration.calls) == 1
    finally:
        integration.release.set()
        active.clear()
        stop.set()
        thread.join(5)
    assert not thread.is_alive()


def test_owner_completion_scope_never_adopts_old_handoffs(integration):
    from completion_runtime import CompletionRuntime
    owner = ExecutionOwner(integration)
    runtime = CompletionRuntime(integration.registry, lambda **_: None, owner_instance=owner.instance)
    with integration.registry._connect() as conn:
        for ref in ('owned', 'unrelated'):
            conn.execute("INSERT INTO clinx_completion_handoffs (execution_ref,task_id,thread_id,turn_id,state,updated_at) VALUES (?,?,?,?,'PENDING',0)",
                         (ref, 'task', 'thread', 'turn'))
        conn.execute('INSERT INTO clinx_execution_owners VALUES (?,?,?,?,?,?,?,?,NULL)',
                     ('owned', 'prepared', owner.instance, 1, '/source', 'DISPATCHED', 0, 0))
    calls = []
    runtime.process = calls.append
    assert runtime.run_once() == 1
    assert calls == ['owned']
