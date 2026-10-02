"""Native final reports reconcile without reading unrelated command history."""
import pytest

import native_provider
from app_server import AppServerProtocolError
from bridge import TaskDispatcher
from completion_runtime import CompletionRuntime
from m9_integration import ResultParseError, parse_codex_result
from test_execution_owner_routing import fixture, EXEC, TID, TURN

REPORT = '''Local implementation and validation completed; production was not deployed.

An earlier benchmark failed; its evidence is retained.

```text
IMPLEMENTATION=PASS
FINAL_STATUS=PASS（本轮本地范围）
PRODUCTION_DEPLOYMENT=NOT_RUN
```
'''


def test_exact_report_preserves_original_and_scope():
    normalized = TaskDispatcher._exact_final_result(REPORT)
    result = parse_codex_result(normalized)
    assert normalized.startswith(REPORT)
    assert result.status == 'PASS'
    assert '本轮本地范围' in result.validation
    assert 'PRODUCTION_DEPLOYMENT=NOT_RUN' in result.raw_result
    assert result.changed_files.startswith('UNKNOWN')


@pytest.mark.parametrize('report', [
    'The task passed.',
    'FINAL_STATUS=PASS',  # prose without a bounded result block
    REPORT.replace('FINAL_STATUS=PASS（本轮本地范围）', 'FINAL_STATUS=BLOCKED'),
    REPORT.replace('FINAL_STATUS=PASS（本轮本地范围）', 'FINAL_STATUS=PASS?'),
    REPORT + '\nFINAL_STATUS=BLOCKED',
    REPORT + '\nCLINX_EXECUTION_RESULT\nSTATUS=PASS',  # malformed strict contract
])
def test_ambiguous_or_missing_final_status_is_not_promoted(report):
    with pytest.raises(ResultParseError):
        parse_codex_result(TaskDispatcher._exact_final_result(report))


def install_items(monkeypatch, rows):
    original = native_provider.existing_client
    requests = []
    def client(endpoint, *a, **kw):
        provider = original(endpoint, *a, **kw)
        def items(thread_id, *, turn_id, limit, sort_direction, cursor=None):
            requests.append((thread_id, turn_id, limit, cursor))
            assert (thread_id, turn_id) == (TID, TURN)
            if limit != 1:
                raise AppServerProtocolError('app-server returned malformed websocket JSON')
            index = int(cursor or 0)
            return {'data': [rows[index]], 'nextCursor': str(index + 1) if index + 1 < len(rows) else None}
        provider.thread_items_list = items
        return provider
    monkeypatch.setattr(native_provider, 'existing_client', client)
    return requests


def final_entry(text=REPORT, turn=TURN):
    return {'turnId': turn, 'item': {'type': 'agentMessage', 'phase': 'final_answer', 'text': text}}


def test_completion_persists_final_report_and_releases_exact_lease(tmp_path, monkeypatch):
    d, r, task, _ = fixture(tmp_path, monkeypatch, states=('notLoaded', 'idle'), status='completed')
    requests = install_items(monkeypatch, [final_entry(), {'item': {'type': 'commandExecution'}}])
    runtime = CompletionRuntime(r, d.reconcile_execution)
    runtime.register(runtime.identity_for(EXEC))
    out = runtime.process(EXEC)
    assert out['state'] == 'COMPLETED'
    assert out['completion_delivery'] == 'DONE'
    result = r.get_execution_result(EXEC)
    assert result.status == 'PASS' and result.raw_result.startswith(REPORT)
    assert result.turn_id == TURN
    assert not r.has_execution_lease(EXEC)
    assert len(requests) == 1  # the bad older command response is never requested
    assert runtime.process(EXEC)['completion_delivery'] == 'DONE'


def test_commentary_and_tools_cannot_supply_final_pass(tmp_path, monkeypatch):
    d, r, task, _ = fixture(tmp_path, monkeypatch, states=('notLoaded', 'idle'), status='completed')
    monkeypatch.setattr('bridge.read_codex_completion_result', lambda *a: None)
    install_items(monkeypatch, [
        {'turnId': TURN, 'item': {'type': 'agentMessage', 'phase': 'commentary', 'text': REPORT}},
        {'turnId': TURN, 'item': {'type': 'commandExecution', 'aggregatedOutput': REPORT}},
    ])
    assert d.reconcile_execution(EXEC)['state'] == 'BLOCKED'
    assert r.get_execution_result(EXEC).status == 'BLOCKED'


def test_mismatched_turn_retains_lease_and_result_is_absent(tmp_path, monkeypatch):
    d, r, task, _ = fixture(tmp_path, monkeypatch, states=('notLoaded', 'idle'), status='completed')
    install_items(monkeypatch, [final_entry(turn='other-turn')])
    assert not d.reconcile_execution(EXEC)['authoritative']
    assert r.has_execution_lease(EXEC)
    assert r.get_execution_result(EXEC) is None


def test_owner_reactivating_after_final_read_prevents_finalization(tmp_path, monkeypatch):
    d, r, task, _ = fixture(tmp_path, monkeypatch, states=('notLoaded', 'idle'), status='completed')
    original = native_provider.existing_client
    install_items(monkeypatch, [final_entry()])
    wrapped = native_provider.existing_client
    def client(endpoint, *a, **kw):
        p = wrapped(endpoint, *a, **kw)
        items = p.thread_items_list
        def reactivate(*a, **kw):
            page = items(*a, **kw)
            owner = original('/native')
            owner.state, owner.turn_status = 'active', 'inProgress'
            return page
        p.thread_items_list = reactivate
        return p
    monkeypatch.setattr(native_provider, 'existing_client', client)
    assert d.reconcile_execution(EXEC)['state'] == 'CODEX_RUNNING'
    assert r.has_execution_lease(EXEC)
    assert r.get_execution_result(EXEC) is None
