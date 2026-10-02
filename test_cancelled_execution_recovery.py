"""Explicit maintenance for a legacy false cancellation, with real SQLite guards."""
import json

import pytest

from completion_runtime import CompletionRuntime, CompletionIdentityError
from task_registry import TaskRegistry, TaskRegistryError
from test_execution_owner_routing import fixture, EXEC, TID, TURN
import execution_liveness as live
import native_provider


def cancelled(tmp_path, monkeypatch, **kwargs):
    d, r, task, calls = fixture(tmp_path, monkeypatch, **kwargs)
    runtime = CompletionRuntime(r, d.reconcile_execution)
    identity = runtime.identity_for(EXEC)
    runtime.register(identity)
    r.finalize_cancellation(EXEC)
    with r._connect() as conn:
        conn.execute("UPDATE clinx_completion_handoffs SET state='DONE' WHERE execution_ref=?", (EXEC,))
    return d, r, task, calls, runtime, identity


def test_explicit_recovery_restores_original_identity_and_lease(tmp_path, monkeypatch):
    d, r, task, calls, runtime, identity = cancelled(tmp_path, monkeypatch)
    retained = r.get_execution_record(EXEC)
    result = d.recover_execution_completion(EXEC, restore_cancelled=True)
    assert result['state'] == 'CODEX_RUNNING'
    assert result['provider_liveness'] == 'LIVE'
    assert result['completion_delivery'] == 'PENDING'
    assert r.get_task(task.task_id).codex_running
    assert r.has_execution_lease(EXEC)
    current = r.get_execution_record(EXEC)
    for field in ('execution_ref', 'execution_owned_turn', 'acquired_at', 'routing_identity_json', 'execution_policy_json'):
        assert current[field] == retained[field]
    with r._connect() as conn:
        assert conn.execute('SELECT count(*) FROM execution_history WHERE execution_ref=?', (EXEC,)).fetchone()[0] == 0
        audit = conn.execute('SELECT * FROM execution_recovery_audit WHERE execution_ref=?', (EXEC,)).fetchone()
        assert json.loads(audit['previous_execution_json'])['stage'] == 'CANCELLED'
    assert calls == {'/managed': [], '/native': []}
    d.client_factory.assert_not_called()
    # Repeating maintenance observes the same execution, without another claim/audit.
    assert d.recover_execution_completion(EXEC, restore_cancelled=True)['state'] == 'CODEX_RUNNING'
    assert runtime.inspect(EXEC)['state'] == 'PENDING'


def test_normal_recovery_cannot_reopen_cancellation(tmp_path, monkeypatch):
    d, r, task, *_ = cancelled(tmp_path, monkeypatch)
    assert d.recover_execution_completion(EXEC)['state'] == 'UNKNOWN'
    assert r.get_task(task.task_id).execution_state == 'CANCELLED'
    assert not r.has_execution_lease(EXEC)


@pytest.mark.parametrize('states,status', [
    (('notLoaded', 'notLoaded'), 'interrupted'),
    (('notLoaded', 'idle'), 'completed'),
    (('active', 'active'), 'inProgress'),
])
def test_no_unique_live_owner_never_restores(tmp_path, monkeypatch, states, status):
    d, r, task, *_ = cancelled(tmp_path, monkeypatch, states=states, status=status)
    with pytest.raises(TaskRegistryError):
        d.recover_execution_completion(EXEC, restore_cancelled=True)
    assert r.get_task(task.task_id).execution_state == 'CANCELLED'
    assert not r.has_execution_lease(EXEC)


@pytest.mark.parametrize('conflict', ['turn', 'binding', 'lease', 'archived', 'handoff'])
def test_conflicting_identity_or_ownership_never_changes_record(tmp_path, monkeypatch, conflict):
    d, r, task, _, runtime, _ = cancelled(tmp_path, monkeypatch)
    with r._connect() as conn:
        if conflict == 'turn':
            conn.execute("UPDATE tasks SET turn_id='newer-turn' WHERE task_id=?", (task.task_id,))
        elif conflict == 'binding':
            conn.execute("UPDATE conversation_bindings SET thread_id='another-thread' WHERE task_id=?", (task.task_id,))
        elif conflict == 'lease':
            row = r.get_execution_record(EXEC)
            conn.execute("INSERT INTO worktree_leases VALUES (?,?,?,'CODEX_RUNNING',?)",
                         (row['worktree_key'], task.task_id, 'exec_other', row['acquired_at']))
        elif conflict == 'archived':
            conn.execute("UPDATE tasks SET status='ARCHIVED' WHERE task_id=?", (task.task_id,))
        elif conflict == 'handoff':
            conn.execute("UPDATE clinx_completion_handoffs SET turn_id='other' WHERE execution_ref=?", (EXEC,))
    with pytest.raises((TaskRegistryError, CompletionIdentityError)):
        d.recover_execution_completion(EXEC, restore_cancelled=True)
    assert r.get_task(task.task_id).execution_state == 'CANCELLED'
    assert not r.has_execution_lease(EXEC)
    assert r.get_execution_record(EXEC)['stage'] == 'CANCELLED'


def test_crash_before_second_observation_retains_lease_and_pending_handoff(tmp_path, monkeypatch):
    d, r, task, _, runtime, identity = cancelled(tmp_path, monkeypatch)
    live.restore_cancelled_execution(r, identity, native_provider.observe_execution(d.cfg, TID, TURN))
    reopened = TaskRegistry(r.path)
    assert reopened.get_task(task.task_id).execution_state == 'RECOVERY_REQUIRED'
    assert reopened.has_execution_lease(EXEC)
    assert runtime.inspect(EXEC)['state'] == 'PENDING'
    assert runtime.process(EXEC)['state'] == 'CODEX_RUNNING'
