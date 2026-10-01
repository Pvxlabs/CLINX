"""Real Provider qualification uses only disposable state and endpoints."""
from contextlib import ExitStack
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest
import bridge
from m9_integration import ClinxIntegration
from provider_qualification import isolated_provider
from task_registry import TaskRegistry, WorkspaceConfig
from tool_delivery import ToolDeliveryLedger

pytestmark = pytest.mark.skipif(os.environ.get('CLINX_LIVE_CONTINUATION') != '1',
                                reason='isolated real Provider opt-in required')


def test_process_initialization_identity(tmp_path):
    cfg = bridge.BridgeConfig.load(Path(__file__).with_name('bridge.toml'))
    with isolated_provider(cfg, tmp_path) as cfg:
        target = SimpleNamespace(target_host='p620', transport='local', alias='qualification')
        agents = []
        for name in ['clinx-qualification-first', 'clinx-qualification-second']:
            with bridge._default_app_server_client(cfg, target) as client:
                agents.append(client.initialize(client_name=name, client_title=name, client_version='1').user_agent)
        assert all(a.startswith('clinx-qualification-first/') for a in agents)
        assert 'clinx-qualification-second' in agents[1]
        (tmp_path/'identity.json').write_text(json.dumps(agents, indent=2))
        print('PROCESS_IDENTITY_EVIDENCE', tmp_path/'identity.json', flush=True)


def test_real_normal_batch_and_continuation(tmp_path):
    cfg = bridge.BridgeConfig.load(Path(__file__).with_name('bridge.toml'))
    project = tmp_path/'project'
    project.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(project)], check=True)
    subprocess.run(['git', '-C', str(project), '-c', 'user.name=CLINX qualification', '-c',
                    'user.email=qualification@example.invalid', 'commit', '-q', '--allow-empty', '-m', 'Fixture'], check=True)
    with isolated_provider(cfg, tmp_path) as cfg:
        cfg = dataclasses.replace(cfg, task_db_path=tmp_path/'tasks.sqlite3',
            projects=(bridge.ProjectMapping(linear_name='Qualification', alias='qualification', repo=project,
                                             branch='main', workspace_alias='p620'),), targets=(), threads=(),
            workspaces=(WorkspaceConfig(alias='p620', root=tmp_path, host='p620'),), log_dir=tmp_path/'logs')
        registry = TaskRegistry(cfg.task_db_path)
        dispatcher = bridge.TaskDispatcher(cfg, task_registry=registry)
        integration = ClinxIntegration(cfg, registry, dispatcher, bridge.TaskContextReader(cfg, registry), None)
        title = 'CLINX qualification: Host reads and continuation'
        prompt = ('Read the host identity, working directory, uptime, Git head, and Git status in one tool batch. '
                  'Then read the working directory once more separately. Summarize the six read results. Do not change files.')
        prepared = integration.prepare_execution(approved=True, task_action='create', host='p620',
            project='qualification', title=title, summary='Read-only Host qualification', prompt=prompt, execution_surface='HOST_EXECUTOR',
            operation_classes=['READ_ONLY_HOST'], required_capabilities=['LOCAL_HOST_PROCESS', 'GIT'])
        evidence=[]
        watchers = ExitStack()
        peer_root = tmp_path/'peer'
        peer_root.mkdir()
        peer_cfg = watchers.enter_context(isolated_provider(cfg, peer_root, native_home=tmp_path/'codex-home'))
        target = SimpleNamespace(target_host='p620',transport='local',alias='read-only-observer')
        observers = {}
        for name, observer_cfg in [('same_endpoint',cfg),('other_endpoint',peer_cfg)]:
            client = watchers.enter_context(bridge._default_app_server_client(observer_cfg,target))
            client.initialize(client_name='clinx-qualification-observer', client_title='CLINX qualification observer', client_version='1')
            observers[name] = client
        try:
            for turn in range(2):
                if turn:
                    prompt = 'Read the host identity and working directory again and summarize this follow-up. Do not change files.'
                    prepared=integration.prepare_execution(approved=True,task_action=('continue' if registry.get_task(started['task_ref']).status == 'ACTIVE' else 'reopen'),task_ref=started['task_ref'],prompt=prompt)
                started=integration.start_execution(approved=True, prepared_execution_ref=prepared['prepared_execution_ref'])
                ref=started['execution_ref']
                print('LIVE_NORMAL_EXECUTION', turn, ref, flush=True)
                binding=registry.get_binding(started['task_ref'])
                initial_status={name: client.thread_read(binding.thread_id)['status'] for name,client in observers.items()}
                deadline=time.monotonic()+150
                while time.monotonic()<deadline:
                    result=registry.get_execution_result(ref)
                    if result and not registry.get_active_execution(ref):break
                    for client in observers.values():
                        client.drain_events(max_events=100,timeout_seconds=0)
                    time.sleep(0.1)
                rows=ToolDeliveryLedger.records(registry,ref)
                evidence.append({'started':started, 'result':dataclasses.asdict(result) if result else None,'deliveries':rows,
                    'readonly_observers': {name: {'initial_status': initial_status[name], 'event_methods': sorted(set(client.events)),
                        'resumed': False} for name,client in observers.items()}})
                (tmp_path/'acceptance.json').write_text(json.dumps(evidence,indent=2))
                assert result and result.status=='PASS', evidence[-1]
                assert len(rows)==(6 if not turn else 2)
                assert all(r['delivery_state']=='DELIVERED' and r['host_exit_code']==0 for r in rows)
                by_task=integration.get_status(task_ref=started['task_ref'])
                by_exec=integration.get_status(execution_ref=ref)
                assert by_task['dynamic_tool_deliveries']==by_exec['dynamic_tool_deliveries']
                binding=registry.get_binding(started['task_ref'])
                with bridge._default_app_server_client(cfg,SimpleNamespace(target_host='p620',transport='local',alias='read')) as client:
                    client.initialize(client_name=cfg.app_server.client_name,client_title=cfg.app_server.client_title,client_version='1')
                    assert client.thread_read(binding.thread_id)['name']==title
                    listed=client._request('thread/list',{'cwd':str(project),'searchTerm':title,'limit':100})
                    assert any(t['id']==binding.thread_id for t in listed['data'])
                    items=client.thread_items_list(binding.thread_id, turn_id=registry.get_execution_record(ref)['execution_owned_turn'], limit=100)
                    texts=[str(entry['item']) for entry in items['data'] if entry['item'].get('type')=='userMessage']
                    assert any(prompt in t for t in texts), texts
                    assert not any('CLINX MANAGED EXECUTION CONTRACT' in t for t in texts)
                    evidence[-1]['native_user_items'] = texts
                    (tmp_path/'acceptance.json').write_text(json.dumps(evidence, indent=2))
            print('LIVE_NORMAL_EVIDENCE', tmp_path/'acceptance.json', flush=True)
        finally:
            watchers.close()
            dispatcher.stop_completion_runtime()
