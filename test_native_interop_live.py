"""Outer-only real native acceptance. No managed worker may enable this suite.

CLINX_LIVE_NATIVE_INTEROP=1 python3 -m pytest -q -s test_native_interop_live.py
All provider homes, sockets, tasks and projects are disposable. No shared daemon
is restarted and the user's ORION thread is never passed to a mutation API.
"""
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import threading
import time

import pytest

import bridge
from m9_integration import ClinxIntegration
from native_provider import existing_client
from provider_qualification import isolated_provider
from task_registry import TaskRegistry, WorkspaceConfig

pytestmark = pytest.mark.skipif(os.environ.get('CLINX_LIVE_NATIVE_INTEROP') != '1',
                                reason='outer-only real native execution acceptance')


def test_real_unregistered_native_read_adopt_and_two_continuations(tmp_path):
    project=tmp_path/'unregistered';project.mkdir()
    subprocess.run(['git','init','-q','-b','main',str(project)],check=True)
    subprocess.run(['git','-C',str(project),'-c','user.name=CLINX acceptance','-c',
        'user.email=acceptance@example.invalid','commit','-q','--allow-empty','-m','Native fixture'],check=True)
    cfg=bridge.BridgeConfig.load(Path(__file__).with_name('bridge.toml'))
    # The preinstalled raw entrypoint avoids unrelated lifecycle wrappers.
    cfg=dataclasses.replace(cfg,codex_binary='/home/pvxlabs/.local/bin/codex-raw')
    with isolated_provider(cfg,tmp_path) as cfg:
        cfg=dataclasses.replace(cfg,projects=(),threads=(),targets=(),task_db_path=tmp_path/'tasks.db',
            log_dir=tmp_path/'logs',workspaces=(WorkspaceConfig(alias='p620',host='p620',root=tmp_path,
                codex_host_ids=('remote-ssh-discovered:p620',)),))
        finished=threading.Event()
        with existing_client(cfg.app_server.local_socket,30,read_only=False) as native:
            native.initialize(client_name='clinx-native-acceptance',client_title='CLINX native acceptance',client_version='1')
            thread=native.thread_start(cwd=str(project),sandbox='workspace-write')
            tid=thread['id'];native.thread_name_set(tid,'Unregistered native acceptance')
            turn=native.turn_start(tid,'Reply with exactly NATIVE_HISTORY_READY. Do not use tools.',cwd=str(project),approval_policy='never')
            native.configure_completion_handoff(tid,turn.turn_id,lambda _:finished.set())
            native.supervise_turn(tid,turn.turn_id)
            assert finished.wait(60),'native seed turn did not complete'
            native._supervisor.join(timeout=5)
        registry=TaskRegistry(cfg.task_db_path)
        dispatcher=bridge.TaskDispatcher(cfg,task_registry=registry)
        integration=ClinxIntegration(cfg,registry,dispatcher,bridge.TaskContextReader(cfg,registry),None)
        uri='codex://threads/'+tid+'?hostId=remote-ssh-discovered%3Ap620'
        evidence={'thread_id':tid,'initial_turn':turn.turn_id,'executions':[]}
        try:
            read=integration.get_context(codex_uri=uri)
            assert read['task_ref'] is None and read['context_status']=='AVAILABLE',read
            assert 'NATIVE_HISTORY_READY' in read['last_codex_result']
            first=integration.adopt_conversation(codex_uri=uri)
            repeated=integration.adopt_conversation(thread_id=tid,host='p620')
            assert first['task_ref']==repeated['task_ref'] and not first['control_transferred']
            evidence['adoption']=first
            for index in range(2):
                task=registry.get_task(first['task_ref'])
                preparation=integration.prepare_execution(approved=True,task_ref=task.task_id,
                    task_action='continue' if task.status=='ACTIVE' else 'reopen',
                    prompt='Reply with a PASS CLINX execution result. No tools or file changes. This is isolated native continuation '+str(index))
                started=integration.start_execution(approved=True,prepared_execution_ref=preparation['prepared_execution_ref'])
                ref=started['execution_ref'];deadline=time.monotonic()+60
                result=None
                while time.monotonic()<deadline:
                    result=registry.get_execution_result(ref)
                    if result and not registry.get_active_execution(ref):break
                    time.sleep(.1)
                assert result and result.status=='PASS'
                assert registry.get_binding(task.task_id).thread_id==tid
                evidence['executions'].append({'execution_ref':ref,'turn_id':result.turn_id,'status':result.status})
            (tmp_path/'native-acceptance.json').write_text(json.dumps(evidence,indent=2))
            print('NATIVE_INTEROP_EVIDENCE',tmp_path/'native-acceptance.json',flush=True)
        finally:
            dispatcher.stop_completion_runtime()
