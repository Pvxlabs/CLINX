"""Authority transitions against durable SQLite and real harmless Host processes."""
import dataclasses
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from execution_policy import build_execution_policy
from execution_semantics import build_routing_identity
from host_executor import HostExecutor, HostExecutorConfig, HostExecutionRequest, AuthorityDenied, InvalidArguments, TargetNotRegistered
from production_workflows import load_workflows
from task_registry import TaskRegistry, TaskRegistryError
from tool_delivery import ToolDeliveryLedger
from m9_integration import ClinxIntegration, M9IntegrationError
from mcp_server import ClinxMCPServer
import bridge


def workflows(root):
    controller = root/'controller.py'
    controller.write_text('#!/usr/bin/python3\nimport json,sys\nfrom pathlib import Path\np=Path("calls.jsonl")\nwith p.open("a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\nprint("SAFE_CONTROLLER_RECEIPT", sys.argv[1])\n')
    controller.chmod(0o700)
    return load_workflows({'test': {'identity':'SAFE_DEPLOY', 'target':'safe-data', 'action':'apply',
        'operation_class':'PRODUCTION_MUTATION', 'argv':[str(controller), '{source_sha}', '{scope}', '{artifact_digest}'],
        'parameters':{'source_sha':{'format':'source_sha'}, 'scope':{'enum':['DATA']}, 'artifact_digest':{'format':'sha256'}},
        'resource':'safe-data'}})


def scope(target='safe-data'):
    return {'capability':'LOCAL_HOST_PROCESS', 'operation':'workflow:SAFE_DEPLOY:apply',
            'operation_class':'PRODUCTION_MUTATION', 'target':target}


def target_policy():
    return dict(required_capabilities=['LOCAL_HOST_PROCESS'], operation_classes=['PRODUCTION_MUTATION'],
                production_mutation_intent=True, operation_scopes=[scope()])


@pytest.fixture
def authority(tmp_path):
    from test_m6 import dispatcher_fixture
    dispatcher, root = dispatcher_fixture(tmp_path, tmp_path/'tasks.sqlite3', [])
    cfg = dataclasses.replace(dispatcher.cfg, host_executor=HostExecutorConfig(enabled=True, workflows=workflows(root)))
    dispatcher.cfg = cfg
    dispatcher.host_executor = HostExecutor(cfg.host_executor, dispatcher.tasks)
    registry = dispatcher.tasks
    policy = build_execution_policy()
    workspace, desc, mapping = dispatcher.resolve_project('pilot',host='p620',project_mode='existing')
    route = dispatcher._routing_identity(workspace=workspace, project=mapping, conversation_bound=False,
        network_access=False, execution_policy=policy)
    task = registry.create_task(host='p620', workspace_alias='p620', project_alias='pilot', project_name='Pilot',
        cwd=str(root), repository_origin=desc.repository_origin, branch=desc.branch,
        title='safe transition', summary='safe transition', execution_policy=policy, routing_identity=route)
    integration = ClinxIntegration(cfg, registry, dispatcher, bridge.TaskContextReader(cfg,registry),None)
    yield integration, registry, task, root, dispatcher
    dispatcher.stop_completion_runtime()


def prepare_change(integration, task):
    identity = integration.get_effective_authority(task_ref=task.task_id)
    return integration.prepare_policy_reauthorization(approved=True, task_ref=task.task_id,
        expected_policy_hash=identity['policy_hash'], target_policy=target_policy(), reason='Explicit safe DATA test authorization')


def apply_change(integration, prepared):
    return integration.apply_policy_reauthorization(approved=True,
        prepared_reauthorization_ref=prepared['prepared_reauthorization_ref'])


def test_immutable_history_cas_idempotency_and_no_execution(authority):
    integration, registry, task, root, _ = authority
    with registry.execution(task.task_id, execution_ref='exec_dev'):
        pass
    old = registry.get_execution_policy('exec_dev')
    first = prepare_change(integration, task)
    stale = prepare_change(integration, task)
    result = apply_change(integration, first)
    assert result['policy_version'] == 1 and not result['execution_started']
    assert apply_change(integration, first)['idempotent']
    with pytest.raises(TaskRegistryError, match='POLICY_IDENTITY_CONFLICT'):
        apply_change(integration, stale)
    assert registry.get_execution_policy('exec_dev') == old
    assert not registry.get_active_execution('exec_dev')
    assert not (root/'calls.jsonl').exists()
    with registry._connect() as conn:
        versions = conn.execute('SELECT * FROM task_policy_versions ORDER BY version').fetchall()
        assert len(versions) == 2
        assert versions[1]['actor'] == 'MCP_OPERATOR_SUBJECT_UNAVAILABLE'
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('UPDATE task_policy_versions SET actor="forged"')
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


def test_active_lease_and_unresolved_delivery_reject(authority):
    integration, registry, task, _, _ = authority
    prepared = prepare_change(integration, task)
    with registry.execution(task.task_id, execution_ref='exec_dev'):
        with pytest.raises(TaskRegistryError, match='ownership'):
            apply_change(integration, prepared)
    ledger = ToolDeliveryLedger(registry, 'exec_dev')
    ledger.admit({'id':1,'params':{'callId':'unknown'}}, {})
    with pytest.raises(TaskRegistryError, match='reconciliation'):
        apply_change(integration, prepared)
    assert registry.get_task_policy_identity(task.task_id)['policy_version'] == 0
    with pytest.raises(TaskRegistryError, match='reconciliation'):
        with registry.execution(task.task_id, execution_ref='exec_cannot_bypass_unknown'):
            pytest.fail('unresolved side effect admitted a new execution')


def test_concurrent_apply_is_cas(authority):
    integration, registry, task, _, _ = authority
    requests = [prepare_change(integration, task) for _ in range(2)]
    def apply(p):
        try:
            return apply_change(integration,p)['policy_version']
        except TaskRegistryError as e:
            return str(e)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, requests))
    assert results.count(1) == 1
    assert sum('POLICY_IDENTITY_CONFLICT' in str(x) for x in results) == 1


def test_one_workflow_two_candidates_and_negative_boundaries(authority):
    integration, registry, task, root, dispatcher = authority
    apply_change(integration, prepare_change(integration,task))
    task = registry.get_task(task.task_id)
    from execution_policy import parse_execution_policy
    from execution_semantics import parse_routing_identity
    policy, route = parse_execution_policy(task.execution_policy_json), parse_routing_identity(task.routing_identity_json)
    with registry.execution(task.task_id, execution_ref='exec_workflow'):
        def request(candidate='a'*40, **changes):
            arguments={'target':'safe-data','parameters':{'source_sha':candidate,'scope':'DATA','artifact_digest':'f'*64}}
            return dataclasses.replace(HostExecutionRequest(task.task_id,'exec_workflow',route,policy,
                'PRODUCTION_MUTATION','LOCAL_HOST_PROCESS','workflow:SAFE_DEPLOY:apply',arguments,root), **changes)
        for candidate in ('a'*40,'b'*40):
            result = dispatcher.host_executor.execute(request(candidate))
            assert result['exit_code'] == 0
        assert len((root/'calls.jsonl').read_text().splitlines()) == 2
        for invalid in ('abc', 'a'*40+'; touch injected', '../escape', '--help'):
            with pytest.raises(InvalidArguments):
                dispatcher.host_executor.execute(request(invalid))
        with pytest.raises(AuthorityDenied):
            dispatcher.host_executor.execute(request(operation_class='READ_ONLY_HOST'))
        with pytest.raises(TargetNotRegistered):
            dispatcher.host_executor.execute(request(arguments={'target':'core','parameters':{}}))
        with pytest.raises(InvalidArguments):
            dispatcher.host_executor.execute(request(arguments={'target':'safe-data','parameters':{'source_sha':'c'*40,'scope':'CORE','artifact_digest':'f'*64}}))
        assert len((root/'calls.jsonl').read_text().splitlines()) == 2
        with registry._connect() as conn:
            assert conn.execute('SELECT fencing_epoch FROM production_resource_leases').fetchone()[0] == 2


def test_networked_continuation_default_does_not_downgrade():
    # Covered with real provider in test_execution_authority_live. Builder retains
    # explicit network grants and prohibits native/host scope contradictions.
    with pytest.raises(ValueError):
        build_execution_policy(execution_surface='SANDBOX_WORKSPACE', **target_policy())


def test_mcp_tools_and_structured_scope_reject_before_prepare(authority):
    integration, registry, task, _, _ = authority
    server=ClinxMCPServer(integration)
    names={x['name'] for x in server.handle({'id':1,'method':'tools/list'})['result']['tools']}
    assert {'clinx_get_effective_authority','clinx_prepare_policy_reauthorization','clinx_apply_policy_reauthorization'} <= names
    with pytest.raises(M9IntegrationError, match='explicit'):
        integration.prepare_policy_reauthorization(task_ref=task.task_id, expected_policy_hash='a'*64,
            target_policy=target_policy(),reason='unapproved')
    with pytest.raises(M9IntegrationError, match='OPERATION_NOT_IMPLEMENTED'):
        integration.prepare_execution(approved=True, task_action='create',host='p620',project='pilot',
            title='bad operation',prompt='deploy',production_mutation_intent=True,
            requested_operations=[{**scope(), 'operation':'workflow:UNKNOWN:apply'}])
    with registry._connect() as conn:
        assert conn.execute('SELECT count(*) FROM prepared_executions').fetchone()[0] == 0


def test_registered_path_templates_cannot_inject_path_or_shell(tmp_path):
    from production_workflows import load_workflows
    import bridge
    cfg=bridge.BridgeConfig.load(Path(__file__).with_name('bridge.toml'))
    workflow=next(w for w in cfg.host_executor.workflows if w.identity=='ORION_DEPLOY' and w.action=='apply')
    params={'source_sha':'a'*40,'scope':'DATA','artifact_set':'l25-phase-a-safe','request_id':'safe-01'}
    argv=workflow.render(params)
    assert '/opt/orion-data-node/release/l25-phase-a-safe/data-node' in argv
    assert argv[4]=='orion-data' and 'CORE' not in argv
    for key, bad in [('source_sha','../x'),('scope','CORE'),('request_id','a;id'),('request_id','a/b'),('artifact_set','../escape')]:
        with pytest.raises(ValueError):
            workflow.render({**params,key:bad})
    with pytest.raises(ValueError):
        workflow.render({**params,'host':'orion-core'})


def test_resource_uncertainty_fences_another_workspace(authority, tmp_path):
    integration, registry, task, root, dispatcher = authority
    apply_change(integration, prepare_change(integration,task))
    task=registry.get_task(task.task_id)
    from execution_policy import parse_execution_policy
    from execution_semantics import parse_routing_identity
    policy=parse_execution_policy(task.execution_policy_json)
    route=parse_routing_identity(task.routing_identity_json)
    with registry.execution(task.task_id,execution_ref='exec_owner'):
        request=HostExecutionRequest(task.task_id,'exec_owner',route,policy,'PRODUCTION_MUTATION',
            'LOCAL_HOST_PROCESS','workflow:SAFE_DEPLOY:apply',{'target':'safe-data','parameters':{
            'source_sha':'a'*40,'scope':'DATA','artifact_digest':'b'*64}},root,tool_call_id='uncertain')
        ledger=ToolDeliveryLedger(registry,'exec_owner')
        ledger.admit({'id':1,'params':{'callId':'uncertain'}},{})
        result=dispatcher.host_executor.execute(request)
        ledger.host_result('uncertain',result)  # no ACK: process done, delivery not established
        other_root=tmp_path/'other';other_root.mkdir()
        other_route=dataclasses.replace(route,workspace=dataclasses.replace(route.workspace,
            worktree_key=registry.worktree_key(host='p620',cwd=str(other_root),repository_origin=None)))
        other=registry.create_task(host='p620',workspace_alias='p620',project_alias='pilot',project_name='Pilot',
            cwd=str(other_root),repository_origin=None,branch='main',title='Other',summary='Other workspace',
            routing_identity=other_route,execution_policy=policy)
        with registry.execution(other.task_id,execution_ref='exec_competing'):
            with pytest.raises(TaskRegistryError,match='PRODUCTION_RESOURCE_BUSY_OR_UNCERTAIN') as caught:
                dispatcher.host_executor.execute(dataclasses.replace(request,task_ref=other.task_id,
                    execution_ref='exec_competing',route=other_route,project_root=other_root,tool_call_id=None))
            assert caught.value.command_not_dispatched
            assert not (other_root/'calls.jsonl').exists()
        assert len((root/'calls.jsonl').read_text().splitlines())==1


def test_expired_approval_cannot_apply(authority):
    from unittest.mock import patch
    integration, registry, task, _, _=authority
    prepared=prepare_change(integration,task)
    with patch('task_registry._now',return_value='2999-01-01T00:00:00+00:00'):
        with pytest.raises(TaskRegistryError,match='EXPIRED'):
            apply_change(integration,prepared)
    assert registry.get_task_policy_identity(task.task_id)['policy_version']==0
