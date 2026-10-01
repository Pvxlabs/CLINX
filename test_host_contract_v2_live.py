"""Opt-in official CLINX prepare/start with real P620 Provider and Host operations.

CLINX_LIVE_HOST_CONTRACT_V2=1 python3 -m pytest -q -s test_host_contract_v2_live.py
Tasks, leases, Git checkouts and results are disposable; no shared-state injection.
"""
import dataclasses
import datetime
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

import bridge
from provider_qualification import isolated_provider
from m9_integration import ClinxIntegration
from mcp_server import ClinxMCPServer
from task_registry import TaskRegistry, WorkspaceConfig
from tool_delivery import ToolDeliveryLedger


@pytest.mark.skipif(os.environ.get('CLINX_LIVE_HOST_CONTRACT_V2') != '1', reason='real Provider opt-in required')
@pytest.mark.parametrize('case', ['normal_sequence', 'multiple_predispatch_failures'])
def test_real_host_contract_v2(request, tmp_path, case):
    cfg = bridge.BridgeConfig.load(Path(__file__).with_name('bridge.toml'))
    isolated = isolated_provider(cfg, tmp_path)
    cfg = isolated.__enter__()
    request.addfinalizer(lambda: isolated.__exit__(None, None, None))
    canonical = next(p for p in cfg.projects if p.project_alias == 'clinx')
    root = tmp_path/'project'
    subprocess.run(['git', 'clone', '--quiet', '--single-branch', '--branch', canonical.branch,
                    canonical.repository_origin, str(root)], check=True, timeout=90)
    cfg = dataclasses.replace(cfg, task_db_path=tmp_path/'tasks.sqlite3',
        projects=(bridge.ProjectMapping(linear_name='Host Contract v2 acceptance', alias='host-v2',
            repo=root, branch=canonical.branch, repository_origin=canonical.repository_origin,
            workspace_alias='p620'),), targets=(), threads=(),
        workspaces=(WorkspaceConfig(alias='p620', root=tmp_path, host='p620'),), log_dir=tmp_path/'logs')
    registry = TaskRegistry(cfg.task_db_path)
    dispatcher = bridge.TaskDispatcher(cfg, task_registry=registry)
    integration = ClinxIntegration(cfg, registry, dispatcher, bridge.TaskContextReader(cfg, registry), None)
    server = ClinxMCPServer(integration, allow_execute=True)
    tool_calls = []

    def tool(name, arguments):
        tool_calls.append(name)
        result = server.handle({'jsonrpc': '2.0', 'id': name, 'method': 'tools/call',
            'params': {'name': name, 'arguments': arguments}})['result']
        assert not result['isError'], result['structuredContent']
        return result['structuredContent']

    sequence = [('GIT', 'nonexistent_operation', {})]
    if case == 'normal_sequence':
        sequence += [('LOCAL_HOST_PROCESS', 'working_directory', {}), ('GIT', 'status', {}),
            ('GIT', 'head', {}), ('GIT', 'fetch_origin', {}), ('GIT', 'remote_main_head', {}),
            ('GIT', 'ahead_behind', {}), ('SYSTEMD_USER', 'service_is_active', {'target': 'clinx.service'}),
            ('SYSTEMD_USER', 'service_status', {'target': 'clinx.service'})]
        rejected = 1
    else:
        sequence += [('SYSTEMD_USER', 'service_is_active', {'target': 'unknown.service'}),
            ('LOCAL_HOST_PROCESS', 'working_directory', {'unexpected_argument': True}),
            ('LOCAL_HOST_PROCESS', 'working_directory', {})]
        rejected = 3
    requests = [{'operation_class': 'READ_ONLY_HOST', 'capability': cap, 'operation': op, 'arguments': args}
                for cap, op, args in sequence]
    prepared_at = time.time()
    prepared = tool('clinx_prepare_execution', dict(approved=True, task_action='create', host='p620',
        project='host-v2', title='HOST_CONTRACT_V2_ACCEPTANCE ' + case,
        summary='Explicitly authorized negative contract checks followed by normal reads in ONE execution',
        execution_surface='HOST_EXECUTOR', operation_classes=['READ_ONLY_HOST'],
        required_capabilities=['LOCAL_HOST_PROCESS', 'SYSTEMD_USER', 'GIT'], production_mutation_intent=False,
        prompt='This is the explicitly authorized Host Contract v2 negative/positive acceptance. '
            'Use ONLY clinx.clinx_host_operation; invoke these exact requests sequentially, one at a time: '
            + json.dumps(requests) + '. The first ' + str(rejected) + ' requests are INTENTIONAL negative tests. '
            'Actually invoke them once; do not replace or skip them. They MUST return COMMAND_NOT_DISPATCHED '
            'with execution_can_continue=true. These expected errors are acceptance success, not blockers. '
            'Then continue immediately with the remaining legal requests in this SAME execution. '
            'No shell tools, no file changes, no nested preparation/start/cancel/reconciliation, no extra tools, '
            'no automatic retries. All remaining requests must exit 0. If results match, return exactly '
            'CLINX_EXECUTION_RESULT\nSTATUS=PASS\nSUMMARY=Host v2 acceptance completed\nCHANGED_FILES=NONE\n'
            'VALIDATION=<list each expected rejection and each Host ref with exit 0>\nBLOCKERS=NONE\nNEXT_STATE=COMPLETED. '
            'If unexpected failure occurs, report BLOCKED truthfully.'))
    started = tool('clinx_start_execution', {'approved': True, 'prepared_execution_ref': prepared['prepared_execution_ref']})
    ref = started['execution_ref']
    print('HOST_V2_LIVE_EXECUTION', case, ref, tmp_path, flush=True)
    try:
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            result = registry.get_execution_result(ref)
            if result is not None and not registry.get_active_execution(ref):
                break
            time.sleep(0.2)
        hosts = registry.list_host_executions(execution_ref=ref)
        rows = ToolDeliveryLedger.records(registry, ref)
        ordered = sorted(hosts, key=lambda h: h['started_at'])
        expected = sequence[rejected:]
        evidence = {
            'case': case, 'prepared_at': prepared_at, 'prepared_execution_ref': prepared['prepared_execution_ref'],
            'task_ref': started['task_ref'], 'execution_ref': ref,
            'result': dataclasses.asdict(result) if result else None,
            'deliveries': rows,
            'hosts': [{k: h[k] for k in ('host_execution_ref', 'tool_call_id', 'operation', 'capability',
                'exit_code', 'result_state', 'started_at', 'completed_at', 'duration_ms')} for h in ordered],
            'host_operation_count': len(hosts), 'invocation_count': len(rows),
            'reconciliation_count': sum(r['reconciliation_required'] for r in rows),
            'unexpected_block_count': int(result is None or result.status != 'PASS'),
            'nested_execution_count': max(0, len(registry.list_tasks()) - 1) if hasattr(registry, 'list_tasks') else None,
            'manual_intervention_count': 0,
            'prepare_to_first_host_seconds': (datetime.datetime.fromisoformat(ordered[0]['started_at']).timestamp() - prepared_at) if ordered else None,
            'host_call_seconds': [round((r['host_completed_at'] or r['admitted_at']) - r['admitted_at'], 6) for r in rows],
            'provider_delivery_seconds': [round(r['acknowledged_at'] - r['host_completed_at'], 6)
                for r in rows if r['host_completed_at'] and r['acknowledged_at']],
        }
        (tmp_path/'acceptance.json').write_text(json.dumps(evidence, indent=2, default=str))
        print('HOST_V2_LIVE_EVIDENCE', tmp_path/'acceptance.json', flush=True)
        assert result is not None and result.status == 'PASS', evidence['result']
        # writeback_state is the optional Linear audit projection, not local
        # execution-result persistence. This isolated acceptance sends no messages.
        status = tool('clinx_get_status', {'execution_ref': ref})
        assert status['execution_result']['status'] == 'PASS'
        assert status['execution_result']['summary'] == result.summary
        assert len(rows) == len(sequence)
        assert len(hosts) == len(expected)
        assert [(h['capability'], h['operation']) for h in ordered] == [(c, o) for c, o, _ in expected]
        assert all(h['exit_code'] == 0 for h in hosts)
        assert all(r['execution_state'] == 'COMMAND_NOT_DISPATCHED' and r['host_execution_ref'] is None
                   and r['side_effect_certainty'] == 'NOT_EXECUTED' for r in rows[:rejected])
        assert all(r['delivery_state'] == 'DELIVERED' for r in rows[rejected:])
        assert not any(r['reconciliation_required'] for r in rows)
        assert tool_calls == ['clinx_prepare_execution', 'clinx_start_execution', 'clinx_get_status']
        with registry._connect() as conn:
            assert conn.execute('SELECT count(*) FROM prepared_executions').fetchone()[0] == 1
            assert conn.execute('SELECT count(*) FROM execution_results').fetchone()[0] == 1
            assert conn.execute('SELECT count(*) FROM executions').fetchone()[0] == 0
            assert conn.execute('SELECT count(*) FROM worktree_leases').fetchone()[0] == 0
        assert subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain']).strip() == b''
    finally:
        dispatcher.stop_completion_runtime()
