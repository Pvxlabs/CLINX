"""Exact Host routing and receipt recovery; no shared services or business work."""
import dataclasses
import json
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import pytest

from native_provider import select_writer_client, NativeWriterError
from tool_delivery import DeliveryReconciliationRequired
from test_tool_delivery import delivery, rows


@pytest.mark.parametrize('states,successor', [(['notLoaded', 'idle'], True),
    (['idle', 'notLoaded'], False), (['notLoaded', 'notLoaded'], False)])
def test_host_writer_stays_dedicated(states, successor):
    cfg = SimpleNamespace(app_server=SimpleNamespace(local_socket='/dedicated', native_socket='/desktop', request_timeout_seconds=5))
    evidence = {'observations': [{'endpoint': p, 'state': s} for p, s in zip(['/dedicated', '/desktop'], states)],
                'ownership_conflict': False}
    with patch('native_provider.observe_thread', return_value=evidence), patch('native_provider.existing_client') as create:
        client = select_writer_client(cfg, 'thread', host_tools=True)
        assert create.call_args.args[0] == '/dedicated'
        assert client.host_successor_required is successor


@pytest.mark.parametrize('states', [['OFFLINE', 'idle'], ['notLoaded', 'active'], ['idle', 'idle'], ['UNKNOWN', 'idle']])
def test_host_writer_never_falls_back_or_takes_over(states):
    cfg = SimpleNamespace(app_server=SimpleNamespace(local_socket='/dedicated', native_socket='/desktop', request_timeout_seconds=5))
    evidence = {'observations': [{'endpoint': p, 'state': s} for p, s in zip(['/dedicated', '/desktop'], states)],
                'ownership_conflict': states == ['idle', 'idle']}
    with patch('native_provider.observe_thread', return_value=evidence), patch('native_provider.existing_client') as create:
        with pytest.raises(NativeWriterError):
            select_writer_client(cfg, 'thread', host_tools=True)
        create.assert_not_called()


def proof_for(d):
    row = rows(d)[0]
    identity = json.loads(row['identity_json'])
    with d.host.registry._connect() as conn:
        host = dict(conn.execute('SELECT * FROM host_executions WHERE host_execution_ref=?',
                                (row['host_execution_ref'],)).fetchone())
    return dict(task_id=host['task_id'], thread_id=identity['thread_id'], turn_id=identity['turn_id'],
                namespace=identity['namespace'], tool=identity['tool'], host_execution_ref=host['host_execution_ref'],
                operation_class=host['operation_class'], capability=host['capability'], operation=host['operation'],
                argv=json.loads(host['argv_json']), mutating=bool(host['mutating']),
                provider_failure_sha256=row['provider_failure_sha256'], completion_review=dict(
                    approved=True, disposition='ACCEPT_RECORDED_COMPLETION_NO_REPLAY',
                    reason='Read exact counter receipt; accepted one completed DEV increment, no replay.',
                    stdout_sha256=host['stdout_sha256'], stderr_sha256=host['stderr_sha256']))


@pytest.mark.parametrize('invalid', ['missing', 'approval', 'hash', 'identity', 'argv', 'production', 'unknown', None])
def test_dev_completion_review_does_not_forge_delivery_or_replay(delivery, invalid):
    d = delivery
    d.client._send_server_response(d.request)
    d.acknowledge(success=False)
    proof = proof_for(d)
    if invalid == 'missing': proof.pop('completion_review')
    if invalid == 'approval': proof['completion_review']['approved'] = False
    if invalid == 'hash': proof['completion_review']['stdout_sha256'] = 'bad'
    if invalid == 'identity': proof['host_execution_ref'] = 'foreign'
    if invalid == 'argv': proof['argv'] = ['true']
    if invalid in ('production', 'unknown'):
        with d.host.registry._connect() as conn:
            if invalid == 'production':
                conn.execute("UPDATE host_executions SET operation_class='PRODUCTION_MUTATION'")
                proof['operation_class'] = 'PRODUCTION_MUTATION'
            else:
                conn.execute('UPDATE host_executions SET completed_at=NULL')
    if invalid:
        with pytest.raises(DeliveryReconciliationRequired):
            d.ledger.reconcile_failed('call-1', proof=proof)
        assert rows(d)[0]['reconciliation_state'] == 'OPEN'
    else:
        before = rows(d)[0]['provider_item_json']
        assert d.ledger.reconcile_failed('call-1', proof=proof)['state'] == 'RESOLVED'
        after = rows(d)[0]
        assert after['delivery_state'] == 'FAILED'
        assert after['provider_item_json'] == before
        with pytest.raises(DeliveryReconciliationRequired):
            d.ledger.admit({**d.request, 'params': {**d.request['params'], 'callId': 'another'}}, {})
    assert d.calls == ['call-1']
    assert len(d.host.registry.list_host_executions(execution_ref=d.ref)) == 1


def test_repeated_host_successors_preserve_exact_history(tmp_path):
    from test_m13b import ProviderThreadMigrationTests
    from task_registry import TaskRegistry
    dispatcher, registry, task, provider = ProviderThreadMigrationTests().fixture(tmp_path)
    # Build a historical schema to exercise the normal startup migration.
    with registry._connect() as conn:
        conn.execute('ALTER TABLE conversation_binding_lineage RENAME TO new_lineage')
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='new_lineage'").fetchone()[0]
        sql = sql.replace('"new_lineage"', 'conversation_binding_lineage').replace(
            'task_id TEXT NOT NULL REFERENCES tasks(task_id)', 'task_id TEXT PRIMARY KEY REFERENCES tasks(task_id)').replace(
            ",\n                    PRIMARY KEY(task_id, predecessor_thread)", '')
        conn.execute(sql)
        conn.execute('DROP TABLE new_lineage')
        conn.execute('''INSERT INTO conversation_binding_lineage
            (task_id,predecessor_thread,successor_thread,migration_reason,migrated_at,predecessor_session_id)
            VALUES (?,?,?,?,?,?)''', (task.task_id, 'older-thread', 'legacy-thread',
                                     'DYNAMIC_TOOL_SCHEMA_UPGRADE', '2020-01-01', 'older-thread'))
        retained = dict(conn.execute('SELECT * FROM conversation_binding_lineage').fetchone())
    registry = TaskRegistry(registry.path)
    previous = registry.get_binding(task.task_id).thread_id
    for i in range(2):
        with registry.execution(task.task_id, execution_ref=f'exec_lineage_{i}', retain=True):
            registry.set_execution_state(task.task_id, 'CLAIMED', current_stage='claim')
            binding = registry.bind_dynamic_tool_upgrade(task_id=task.task_id, execution_ref=f'exec_lineage_{i}',
                expected_thread=previous, successor={'id': f'successor-{i}', 'sessionId': f'successor-{i}'},
                reason='HOST_PROVIDER_ISOLATION')
            assert registry.get_binding_lineage(task.task_id).predecessor_thread == previous
            previous = binding.thread_id
            registry.reconcile_terminal(f'exec_lineage_{i}', 'COMPLETED')
    with registry._connect() as conn:
        history = [dict(r) for r in conn.execute('SELECT * FROM conversation_binding_lineage ORDER BY migrated_at')]
        assert len(history) == 3
        assert history[0] == retained
        assert history[1]['predecessor_thread'] == 'legacy-thread'
        assert history[2]['predecessor_thread'] == 'successor-0'
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
    assert registry.get_execution_routing_identity('exec_lineage_0').conversation.binding == 'successor-0'
    assert registry.get_execution_policy('exec_lineage_0') == registry.get_execution_policy('exec_lineage_1')


def test_host_dispatch_creates_successor_on_dedicated_provider(tmp_path):
    import bridge
    from test_m13b import ProviderThreadMigrationTests
    dispatcher, registry, task, provider = ProviderThreadMigrationTests().fixture(tmp_path)
    dispatcher.cfg = dataclasses.replace(bridge.BridgeConfig.load(Path(__file__).with_name('bridge.toml')),
        task_db_path=registry.path, projects=(), targets=(), threads=())
    dispatcher._completion_runtime = None
    dispatcher._uses_default_client_factory = True
    provider.host_successor_required = True
    try:
        with patch('native_provider.select_writer_client', return_value=provider) as select:
            outcome = dispatcher.dispatch(project_ref='pilot', host='p620', project_mode='existing',
                task_mode='continue', task_id=task.task_id, prompt='Current authorized development',
                title=task.title, summary=task.summary, model='model', reasoning_effort=None,
                execution_mode='normal', execution_ref='exec_isolation')
        assert select.call_args.kwargs == {'host_tools': True}
        assert outcome.thread_id == 'new-thread'
        assert not any(c[0] == 'thread/resume' for c in provider.calls)
        assert registry.get_binding_lineage(task.task_id).migration_reason == 'HOST_PROVIDER_ISOLATION'
        assert any(c[0] == 'turn/start' and c[1] == 'new-thread' for c in provider.calls)
        assert registry.get_binding(task.task_id).thread_id == 'new-thread'
    finally:
        dispatcher.stop_completion_runtime()
