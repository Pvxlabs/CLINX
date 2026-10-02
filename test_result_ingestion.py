"""结果收取与同 execution 对账：只使用隔离 SQLite 和 scripted Provider。"""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from m9_integration import ResultParseError, parse_execution_result
from test_execution_owner_routing import fixture as owner_fixture, EXEC, TID, TURN
from test_native_completion_report import install_items, final_entry
from test_pvx1812_completion import RESULT

def fixture(tmp_path, monkeypatch, **kwargs):
    # The older routing fixture uses a display-only worktree key. Build the
    # canonical key before enrollment for tests that exercise repair authority.
    import dataclasses
    from task_registry import TaskRegistry
    from test_m13 import route
    key = TaskRegistry.worktree_key(host="p620", cwd=str(tmp_path),
                                   repository_origin="git@github.com:example/clinx.git")
    def canonical_route(**kw):
        value = route(**kw)
        return dataclasses.replace(value, workspace=dataclasses.replace(value.workspace, worktree_key=key))
    monkeypatch.setattr("test_execution_owner_routing.route", canonical_route)
    return owner_fixture(tmp_path, monkeypatch, **kwargs)


CONFLICT = RESULT.replace('BLOCKERS=NONE', 'BLOCKERS=本轮工程范围无未闭合阻塞；外层生产授权、当前共享IO与冷恢复、实际DATA部署及30分钟观察尚未完成，RELEASE_READY=NO。')

def test_conflict_is_ingestion_error_not_missing_marker(tmp_path, monkeypatch):
    d, r, task, calls = fixture(tmp_path, monkeypatch, states=('notLoaded', 'idle'), status='completed')
    install_items(monkeypatch, [final_entry(CONFLICT)])
    out = d.reconcile_execution(EXEC, reclaim_stale=False)
    assert out['state'] == 'BLOCKED'
    record = r.get_execution_record(EXEC)
    assert record['failure_stage'] == 'result_ingestion'
    assert record['failure_code'] == 'RESULT_STATUS_BLOCKERS_CONFLICT'
    assert 'PASS results must use BLOCKERS=NONE' in record['failure_evidence']
    assert not r.get_task(task.task_id).retry_required
    assert calls == {'/managed': [], '/native': []}

def test_duplicate_conflicting_blocks_are_invalid():
    blocked = RESULT.replace('STATUS=PASS', 'STATUS=BLOCKED').replace('NEXT_STATE=COMPLETED', 'NEXT_STATE=BLOCKED')
    with pytest.raises(ResultParseError):
        parse_execution_result(RESULT + '\n' + blocked)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def evidence(r):
    with r._connect() as conn:
        return [json.loads(row[0]) for row in conn.execute('SELECT evidence_json FROM result_ingestions')]


def finalize_fixture(tmp_path, monkeypatch, report=CONFLICT):
    d, r, task, calls = fixture(tmp_path, monkeypatch, states=('notLoaded', 'idle'), status='completed')
    entry = final_entry(report)
    entry['item']['id'] = 'message-final'
    install_items(monkeypatch, [entry])
    d.reconcile_execution(EXEC, reclaim_stale=False)
    return d, r, task, calls, entry


def expected(r, task, source):
    return dict(task_id=task.task_id, thread_id=TID, turn_id=TURN,
                result_sha256=sha(r.get_execution_result(EXEC).raw_result),
                source_sha256=sha(source), message_id='message-final')


def test_evidence_keeps_source_identity_error_and_scope(tmp_path, monkeypatch):
    d, r, task, _, _ = finalize_fixture(tmp_path, monkeypatch)
    ev = evidence(r)[0]
    assert (ev['execution_ref'], ev['task_id'], ev['thread_id'], ev['turn_id']) == (EXEC, task.task_id, TID, TURN)
    assert ev['message_id'] == 'message-final' and ev['phase'] == 'final_answer'
    assert ev['raw_sha256'] == sha(CONFLICT) and ev['raw_excerpt'] == CONFLICT
    assert ev['parse_error'] == 'PASS results must use BLOCKERS=NONE'
    assert r.get_execution_result(EXEC).raw_result == CONFLICT
    d.reconcile_execution(EXEC, reclaim_stale=False)
    assert len(evidence(r)) == 1


@pytest.mark.parametrize('report,code', [
    ('Engineering finished but no report', 'RESULT_MARKER_MISSING'),
    ('CLINX_EXECUTION_RESULT\nSTATUS=PASS', 'RESULT_INCOMPLETE'),
    (RESULT + '\nSTATUS=BLOCKED', 'RESULT_DUPLICATE_CONFLICT'),
    (RESULT.replace('BLOCKERS=NONE', 'BLOCKERS=none'), 'RESULT_STATUS_BLOCKERS_CONFLICT'),
])
def test_classified_invalid_final(tmp_path, monkeypatch, report, code):
    d, r, task, _, _ = finalize_fixture(tmp_path, monkeypatch, report)
    assert r.get_execution_record(EXEC)['failure_code'] == code
    assert not r.get_task(task.task_id).retry_required


def test_valid_scoped_pass_retains_downstream_not_run(tmp_path, monkeypatch):
    report = RESULT.replace('VALIDATION=scripted protocol fixture',
                            'VALIDATION=local tests PASS; Provider E2E=NOT_RUN; production=NOT_RUN')
    d, r, task, _, _ = finalize_fixture(tmp_path, monkeypatch, report)
    result = r.get_execution_result(EXEC)
    assert result.status == 'PASS'
    assert result.raw_result == report
    assert result.blockers == 'NONE' and 'production=NOT_RUN' in result.validation
    assert evidence(r)[0]['failure_code'] is None


def test_long_output_parsed_before_bounded_storage(tmp_path, monkeypatch):
    report = '范围保留。' * 8000 + '\n' + CONFLICT
    _, r, _, _, _ = finalize_fixture(tmp_path, monkeypatch, report)
    ev = evidence(r)[0]
    assert ev['truncated'] and ev['raw_sha256'] == sha(report)
    assert ev['raw_bytes'] == len(report.encode())
    assert len(ev['raw_excerpt']) < 16000 and 'RELEASE_READY=NO' in ev['raw_excerpt']
    assert r.get_execution_record(EXEC)['failure_code'] == 'RESULT_STATUS_BLOCKERS_CONFLICT'


def test_credentials_redacted_without_changing_digest(tmp_path, monkeypatch):
    report = CONFLICT.replace('SUMMARY=', 'SUMMARY=token=super-secret ')
    _, r, _, _, _ = finalize_fixture(tmp_path, monkeypatch, report)
    assert evidence(r)[0]['raw_sha256'] == sha(report)
    assert 'super-secret' not in evidence(r)[0]['raw_excerpt']
    assert 'super-secret' not in r.get_execution_result(EXEC).raw_result


def test_same_execution_invalid_reconciliation_idempotent_and_audited(tmp_path, monkeypatch):
    d, r, task, calls, _ = finalize_fixture(tmp_path, monkeypatch)
    args = expected(r, task, CONFLICT)
    old = r.get_execution_result(EXEC)
    first = d.recover_execution_completion(EXEC, result_reconciliation=args)
    second = d.recover_execution_completion(EXEC, result_reconciliation=args)
    assert first['result_reconciliation'] == 'APPLIED'
    assert second['result_reconciliation'] == 'ALREADY_APPLIED'
    assert first['failure_code'] == 'RESULT_STATUS_BLOCKERS_CONFLICT'
    assert first['state'] == second['state'] == 'BLOCKED'
    assert not first['retry_required']
    with r._connect() as conn:
        rows = conn.execute('SELECT * FROM result_reconciliation_audit').fetchall()
        assert len(rows) == 1
        assert json.loads(rows[0]['previous_result_json'])['raw_result'] == old.raw_result
    assert calls == {'/managed': [], '/native': []}


def test_legacy_error_repaired_only_from_exact_valid_final(tmp_path, monkeypatch):
    from m9_integration import ExecutionFinalizer
    d, r, task, _, entry = finalize_fixture(tmp_path, monkeypatch)
    legacy = ExecutionFinalizer._canonical_blocked(
        summary='Provider terminal output omitted the result marker and no successful host execution evidence was recorded.',
        validation='legacy', blockers='legacy', terminal_state='BLOCKED')
    # Isolated fixture reproduces the old committed result. Never a runtime patch.
    with r._connect() as conn:
        conn.execute('UPDATE execution_results SET summary=?,raw_result=? WHERE execution_ref=?',
                     (legacy.summary, legacy.raw_result, EXEC))
        conn.execute('UPDATE execution_history SET failure_stage=NULL,failure_code=NULL WHERE execution_ref=?', (EXEC,))
    entry['item']['text'] = RESULT
    args = expected(r, task, RESULT)
    out = d.recover_execution_completion(EXEC, result_reconciliation=args)
    assert out['state'] == 'COMPLETED'
    assert r.get_execution_result(EXEC).raw_result == RESULT
    assert r.get_task(task.task_id).execution_state == 'COMPLETED'
    assert d.recover_execution_completion(EXEC, result_reconciliation=args)['result_reconciliation'] == 'ALREADY_APPLIED'


@pytest.mark.parametrize('key,value', [('task_id','wrong'),('thread_id','wrong'),('turn_id','wrong'),
    ('result_sha256','0'*64),('source_sha256','0'*64),('message_id','wrong')])
def test_repair_cas_conflicts_do_not_replace_result(tmp_path, monkeypatch, key, value):
    from task_registry import TaskRegistryError
    d, r, task, _, _ = finalize_fixture(tmp_path, monkeypatch)
    args = expected(r, task, CONFLICT)
    old = r.get_execution_result(EXEC)
    args[key] = value
    with pytest.raises(TaskRegistryError):
        d.recover_execution_completion(EXEC, result_reconciliation=args)
    assert r.get_execution_result(EXEC) == old
    with r._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM result_reconciliation_audit').fetchone()[0] == 0


def test_concurrent_reconciliation_is_single_audit(tmp_path, monkeypatch):
    d, r, task, _, _ = finalize_fixture(tmp_path, monkeypatch)
    args = expected(r, task, CONFLICT)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: d.recover_execution_completion(EXEC, result_reconciliation=args), range(2)))
    assert sorted(v['result_reconciliation'] for v in results) == ['ALREADY_APPLIED', 'APPLIED']
    with r._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM result_reconciliation_audit').fetchone()[0] == 1


@pytest.mark.parametrize('state', ['active', 'notLoaded'])
def test_repair_live_or_unknown_owner_changes_no_projection(tmp_path, monkeypatch, state):
    import native_provider
    d, r, task, _, _ = finalize_fixture(tmp_path, monkeypatch)
    args = expected(r, task, CONFLICT)
    before = r.get_task(task.task_id)
    owner = native_provider.existing_client('/native')
    owner.state = state
    owner.turn_status = 'inProgress'
    result = d.recover_execution_completion(EXEC, result_reconciliation=args)
    assert not result['authoritative']
    assert r.get_task(task.task_id) == before


def test_repair_newer_execution_refused(tmp_path, monkeypatch):
    from task_registry import TaskRegistryError
    d, r, task, _, _ = finalize_fixture(tmp_path, monkeypatch)
    args = expected(r, task, CONFLICT)
    with r.execution(task.task_id, execution_ref='exec_newer', retain=True):
        r.set_execution_state(task.task_id, 'CODEX_RUNNING', turn_id='turn_newer', codex_running=True)
    before = r.get_task(task.task_id)
    with pytest.raises(TaskRegistryError):
        d.recover_execution_completion(EXEC, result_reconciliation=args)
    assert r.get_task(task.task_id) == before
    assert r.has_execution_lease('exec_newer')


def test_wrong_item_thread_does_not_finalize(tmp_path, monkeypatch):
    d, r, _, _ = fixture(tmp_path, monkeypatch, states=('notLoaded', 'idle'), status='completed')
    entry = final_entry(RESULT)
    entry['threadId'] = 'other'
    install_items(monkeypatch, [entry])
    assert not d.reconcile_execution(EXEC, reclaim_stale=False)['authoritative']
    assert r.get_execution_result(EXEC) is None and r.has_execution_lease(EXEC)


def test_cli_reconciliation_is_wired():
    from bridge import build_parser
    args = build_parser().parse_args(['recover-execution', EXEC, '--reconcile-result',
        '--expected-task-id', 'task', '--expected-thread-id', TID, '--expected-turn-id', TURN,
        '--expected-result-sha256', '0'*64, '--expected-source-sha256', '1'*64])
    assert args.reconcile_result and args.expected_turn_id == TURN


def host_record(r, task, *, completed=True):
    r.begin_host_execution(host_execution_ref='hostexec_fixture',task_id=task.task_id,execution_ref=EXEC,
        routing_identity_json=task.routing_identity_json,execution_policy_json='{}',host='p620',
        surface='host_executor',operation_class='DEVELOPMENT_MUTATION',capability='HOST_FILESYSTEM',
        operation='development_command',argv_json='["fixture"]',cwd_identity='fixture',
        started_at='2026-01-01T00:00:00+00:00',result_state='RUNNING',timeout_seconds=30,executor_instance='fixture')
    if completed:
        r.complete_host_execution('hostexec_fixture',completed_at='2026-01-01T00:00:01+00:00',
                                  duration_ms=1,exit_code=0,result_state='SUCCEEDED')


def test_invalid_final_is_not_promoted_by_successful_host(tmp_path, monkeypatch):
    d, r, task, _ = fixture(tmp_path, monkeypatch, states=('notLoaded', 'idle'), status='completed')
    host_record(r, task)
    install_items(monkeypatch, [final_entry(CONFLICT)])
    out = d.reconcile_execution(EXEC, reclaim_stale=False)
    assert out['state'] == 'BLOCKED' and out['failure_code'] == 'RESULT_STATUS_BLOCKERS_CONFLICT'
    assert r.get_execution_result(EXEC).raw_result == CONFLICT


def test_unknown_host_side_effect_retains_lease_and_forbids_repair(tmp_path, monkeypatch):
    from task_registry import TaskRegistryError
    d, r, task, _ = fixture(tmp_path, monkeypatch, states=('notLoaded', 'idle'), status='completed')
    host_record(r, task, completed=False)
    install_items(monkeypatch, [final_entry(RESULT)])
    out = d.reconcile_execution(EXEC, reclaim_stale=False)
    assert out['state'] == 'RECOVERY_REQUIRED'
    assert out['failure_code'] == 'HOST_SIDE_EFFECT_UNRESOLVED'
    assert not out['retry_required'] and r.has_execution_lease(EXEC)
    with pytest.raises(TaskRegistryError):
        d.recover_execution_completion(EXEC, result_reconciliation=expected(r, task, RESULT))
    assert r.has_execution_lease(EXEC)


def test_stored_error_with_unconfirmed_delivery_cannot_be_repaired(tmp_path, monkeypatch):
    from task_registry import TaskRegistryError
    from tool_delivery import ToolDeliveryLedger
    d, r, task, _, _ = finalize_fixture(tmp_path, monkeypatch)
    args = expected(r, task, CONFLICT)
    ToolDeliveryLedger(r, EXEC)
    with r._connect() as conn:
        conn.execute("""INSERT INTO host_tool_deliveries
            (execution_ref,tool_call_id,request_id,identity_json,request_hash,
             execution_state,delivery_state,admitted_at) VALUES (?,?,?,'{}','hash',
             'COMMAND_DISPATCHED','PENDING',1)""", (EXEC,'call','request'))
    old = r.get_execution_result(EXEC)
    with pytest.raises(TaskRegistryError, match='SIDE_EFFECT_UNRESOLVED'):
        d.recover_execution_completion(EXEC, result_reconciliation=args)
    assert r.get_execution_result(EXEC) == old


@pytest.mark.parametrize('provider_status,report,expected_state', [
    ('failed', RESULT, 'FAILED'),
    ('completed', RESULT.replace('STATUS=PASS', 'STATUS=BLOCKED').replace(
        'BLOCKERS=NONE', 'BLOCKERS=engineering test failed').replace('NEXT_STATE=COMPLETED','NEXT_STATE=BLOCKED'), 'BLOCKED'),
])
def test_real_failure_cannot_be_repaired_as_ingestion(tmp_path, monkeypatch, provider_status, report, expected_state):
    from task_registry import TaskRegistryError
    d, r, task, _ = fixture(tmp_path, monkeypatch, states=('notLoaded', 'idle'), status=provider_status)
    install_items(monkeypatch, [final_entry(report)])
    assert d.reconcile_execution(EXEC, reclaim_stale=False)['state'] == expected_state
    assert r.get_execution_result(EXEC).status == 'BLOCKED'
    assert evidence(r)[0]['raw_sha256'] == sha(report)
    with pytest.raises(TaskRegistryError, match='NOT_INGESTION_FAILURE'):
        d.recover_execution_completion(EXEC, result_reconciliation=expected(r, task, report))


def test_failure_mid_repair_rolls_back_audit_and_projection(tmp_path, monkeypatch):
    d, r, task, _, _ = finalize_fixture(tmp_path, monkeypatch)
    args = expected(r, task, CONFLICT)
    before = r.get_execution_result(EXEC)
    with r._connect() as conn:
        conn.execute("""CREATE TRIGGER fixture_abort BEFORE UPDATE ON execution_results
                        BEGIN SELECT RAISE(ABORT, 'fixture crash'); END""")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError, match='fixture crash'):
        d.recover_execution_completion(EXEC, result_reconciliation=args)
    assert r.get_execution_result(EXEC) == before
    with r._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM result_reconciliation_audit').fetchone()[0] == 0


def test_repair_owner_reactivating_after_read_changes_no_task(tmp_path, monkeypatch):
    import native_provider
    d, r, task, _, _ = finalize_fixture(tmp_path, monkeypatch)
    args = expected(r, task, CONFLICT)
    before = r.get_task(task.task_id)
    provider = native_provider.existing_client('/native')
    original = provider.thread_items_list
    def read(*a, **kw):
        page = original(*a, **kw)
        provider.state, provider.turn_status = 'active', 'inProgress'
        return page
    # existing_client's test wrapper installs a reader each call; wrap its factory last.
    factory = native_provider.existing_client
    def client(ep, *a, **kw):
        p = factory(ep, *a, **kw)
        if ep == '/native':
            p.thread_items_list = read
        return p
    monkeypatch.setattr(native_provider, 'existing_client', client)
    out = d.recover_execution_completion(EXEC, result_reconciliation=args)
    assert not out['authoritative']
    assert r.get_task(task.task_id) == before


def test_commentary_summary_and_tool_text_are_not_final_evidence(tmp_path, monkeypatch):
    d, r, task, _ = fixture(tmp_path, monkeypatch, states=('notLoaded', 'idle'), status='completed', result=RESULT)
    monkeypatch.setattr('bridge.read_codex_completion_result', lambda *a: None)
    install_items(monkeypatch, [
        {'turnId': TURN, 'item': {'type':'agentMessage', 'phase':'commentary', 'text':RESULT}},
        {'turnId': TURN, 'item': {'type':'commandExecution', 'text':RESULT}},
        {'turnId': TURN, 'item': {'type':'reasoning', 'text':'private reasoning sentinel'}},
    ])
    d.reconcile_execution(EXEC, reclaim_stale=False)
    assert r.get_execution_record(EXEC)['failure_code'] == 'RESULT_MARKER_MISSING'
    assert 'private reasoning sentinel' not in json.dumps(evidence(r))


def test_concurrent_finalization_records_one_result_and_one_source(tmp_path, monkeypatch):
    d, r, task, _ = fixture(tmp_path, monkeypatch, states=('notLoaded', 'idle'), status='completed')
    install_items(monkeypatch, [final_entry(CONFLICT)])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: d.reconcile_execution(EXEC, reclaim_stale=False), range(2)))
    assert all(out['state'] == 'BLOCKED' for out in results)
    assert len(evidence(r)) == 1
    with r._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM execution_results').fetchone()[0] == 1


def test_item_without_final_phase_cannot_supply_pass(tmp_path, monkeypatch):
    d, r, task, _ = fixture(tmp_path, monkeypatch, states=('notLoaded','idle'), status='completed')
    monkeypatch.setattr('bridge.read_codex_completion_result', lambda *a: None)
    entry = final_entry(RESULT)
    del entry['item']['phase']
    install_items(monkeypatch, [entry])
    d.reconcile_execution(EXEC, reclaim_stale=False)
    assert r.get_execution_record(EXEC)['failure_code'] == 'RESULT_MARKER_MISSING'


def test_explicit_truncation_cannot_supply_pass(tmp_path, monkeypatch):
    d, r, task, _ = fixture(tmp_path, monkeypatch, states=('notLoaded','idle'), status='completed')
    entry = final_entry(RESULT)
    entry['item']['truncated'] = True
    install_items(monkeypatch, [entry])
    out = d.reconcile_execution(EXEC, reclaim_stale=False)
    assert out['state'] == 'BLOCKED' and out['failure_code'] == 'RESULT_TRUNCATED'
    assert evidence(r)[0]['source_incomplete']


def test_pass_cannot_declare_blocked_next_state():
    with pytest.raises(ResultParseError, match='NEXT_STATE=BLOCKED') as exc:
        parse_execution_result(RESULT.replace('NEXT_STATE=COMPLETED','NEXT_STATE=BLOCKED'))
    assert exc.value.code == 'RESULT_STATE_CONFLICT'


@pytest.mark.parametrize('session,turn,valid', [(TID,TURN,True),('wrong',TURN,False),(TID,'wrong',False)])
def test_native_fallback_requires_session_and_turn(tmp_path, monkeypatch, session, turn, valid):
    from pathlib import Path
    from bridge import read_codex_completion_result
    root = tmp_path / '.codex' / 'sessions'
    root.mkdir(parents=True)
    (root / ('rollout-' + TID + '.jsonl')).write_text('\n'.join(json.dumps(row) for row in [
        {'type':'session_meta','payload':{'id':session}},
        {'type':'event_msg','payload':{'type':'task_complete','turn_id':turn,'last_agent_message':CONFLICT}},
    ]))
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    assert (read_codex_completion_result(TID, TURN) == CONFLICT) == valid


def test_changed_worktree_identity_refuses_repair(tmp_path, monkeypatch):
    from task_registry import TaskRegistryError
    d, r, task, _, _ = finalize_fixture(tmp_path, monkeypatch)
    args = expected(r, task, CONFLICT)
    with r._connect() as conn:
        conn.execute("UPDATE tasks SET cwd=? WHERE task_id=?", (str(tmp_path/'changed'),task.task_id))
    with pytest.raises(TaskRegistryError, match='ROUTE_CONFLICT'):
        d.recover_execution_completion(EXEC, result_reconciliation=args)
