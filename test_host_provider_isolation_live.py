"""Real cross-daemon Host continuation; both Providers and workspaces are disposable."""
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import time

import pytest
import bridge
from provider_qualification import isolated_provider
from task_registry import TaskRegistry, WorkspaceConfig
from m9_integration import ClinxIntegration
from tool_delivery import ToolDeliveryLedger

pytestmark = pytest.mark.skipif(os.environ.get('CLINX_LIVE_AUTHORITY') != '1', reason='isolated real Provider opt-in')


def test_idle_shared_owner_gets_dedicated_successor(tmp_path):
    root = tmp_path/'project'; root.mkdir()
    subprocess.run(['git','init','-q','-b','main',str(root)], check=True)
    subprocess.run(['git','-C',str(root),'-c','user.name=CLINX acceptance','-c',
                    'user.email=acceptance@example.invalid','commit','-q','--allow-empty','-m','Disposable routing acceptance'], check=True)
    cfg = bridge.BridgeConfig.load(Path(__file__).with_name('bridge.toml'))
    primary = tmp_path/'shared'; primary.mkdir()
    dedicated = tmp_path/'dedicated'; dedicated.mkdir()
    with isolated_provider(cfg, primary) as shared_cfg:
        cfg = dataclasses.replace(shared_cfg, task_db_path=tmp_path/'tasks.sqlite3', log_dir=tmp_path/'logs',
            projects=(bridge.ProjectMapping(linear_name='Host isolation acceptance', alias='host-test',repo=root,
                                           branch='main',workspace_alias='p620'),), targets=(), threads=(),
            workspaces=(WorkspaceConfig(alias='p620',root=tmp_path,host='p620'),))
        registry = TaskRegistry(cfg.task_db_path)
        dispatcher = bridge.TaskDispatcher(cfg, task_registry=registry)
        integration = ClinxIntegration(cfg,registry,dispatcher,bridge.TaskContextReader(cfg,registry),None)
        prompt = ('Call clinx.clinx_host_operation exactly once with operation_class=READ_ONLY_HOST, '
            'capability=LOCAL_HOST_PROCESS, operation=working_directory, arguments={}. No other tools. '
            'Return the CLINX result with actual validation and PASS if successful.')
        def execute(**kwargs):
            prepared = integration.prepare_execution(approved=True, prompt=prompt, **kwargs)
            result = integration.start_execution(approved=True, prepared_execution_ref=prepared['prepared_execution_ref'])
            deadline = time.monotonic()+120
            while time.monotonic()<deadline:
                final = registry.get_execution_result(result['execution_ref'])
                if final and not registry.has_execution_lease(result['execution_ref']): break
                time.sleep(.2)
            assert final and final.status == 'PASS', final
            deliveries = ToolDeliveryLedger.records(registry,result['execution_ref'])
            assert len(deliveries) == 1 and deliveries[0]['delivery_state']=='DELIVERED'
            return result, deliveries[0]
        try:
            a, ad = execute(task_action='create',host='p620',project='host-test',title='Host endpoint isolation',
                summary='Disposable same Task Host routing',execution_surface='HOST_EXECUTOR',
                required_capabilities=['LOCAL_HOST_PROCESS'],operation_classes=['READ_ONLY_HOST'])
            original = registry.get_binding(a['task_ref'])
            dispatcher.stop_completion_runtime()
            with isolated_provider(cfg, dedicated, native_home=Path(cfg.app_server.native_home)) as isolated:
                cfg = dataclasses.replace(isolated,app_server=dataclasses.replace(isolated.app_server,
                    native_socket=shared_cfg.app_server.local_socket))
                dispatcher = bridge.TaskDispatcher(cfg,task_registry=registry)
                integration = ClinxIntegration(cfg,registry,dispatcher,bridge.TaskContextReader(cfg,registry),None)
                b, bd = execute(task_ref=a['task_ref'])
                successor = registry.get_binding(a['task_ref'])
                assert successor.thread_id != original.thread_id
                assert registry.get_binding_lineage(a['task_ref']).migration_reason == 'HOST_PROVIDER_ISOLATION'
                c, cd = execute(task_ref=a['task_ref'])
                assert registry.get_binding(a['task_ref']).thread_id == successor.thread_id
                assert a['task_ref']==b['task_ref']==c['task_ref']
                assert json.loads(bd['identity_json'])['provider_endpoint']['path'] == cfg.app_server.local_socket
                assert registry.get_execution_routing_identity(a['execution_ref']).conversation.binding == original.thread_id
                assert len(registry.list_host_executions(execution_ref=b['execution_ref'])) == 1
                evidence = {'original':a,'successor':b,'continued':c,'lineage':dataclasses.asdict(registry.get_binding_lineage(a['task_ref'])),
                            'deliveries':[ad,bd,cd]}
                (tmp_path/'acceptance.json').write_text(json.dumps(evidence,indent=2))
                print('HOST_ISOLATION_EVIDENCE',tmp_path/'acceptance.json',flush=True)
                dispatcher.stop_completion_runtime()
        finally:
            dispatcher.stop_completion_runtime()
