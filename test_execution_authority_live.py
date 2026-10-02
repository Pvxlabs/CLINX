"""Real Codex parity and same Task policy transition in a disposable workspace."""
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
from mcp_server import ClinxMCPServer
from native_provider import existing_client
from provider_qualification import isolated_provider
from task_registry import TaskRegistry, WorkspaceConfig
from tool_delivery import ToolDeliveryLedger
from test_execution_authority import workflows, scope, target_policy

pytestmark = pytest.mark.skipif(os.environ.get('CLINX_LIVE_AUTHORITY') != '1', reason='isolated real Provider opt-in')


def test_native_parity_same_task_and_parameterized_workflow(tmp_path):
    root=tmp_path/'project';root.mkdir()
    subprocess.run(['git','init','-q','-b','main',str(root)],check=True)
    subprocess.run(['git','-C',str(root),'-c','user.name=CLINX acceptance','-c','user.email=acceptance@example.invalid',
                    'commit','-q','--allow-empty','-m','Disposable authority acceptance'],check=True)
    registered=workflows(root)
    (root/'parity_probe.py').write_text('''import json,os,subprocess,sys,urllib.request
from pathlib import Path
p=Path('native-marker'); p.write_text('test'); assert p.read_text()=='test'; p.unlink()
with urllib.request.urlopen('https://example.com',timeout=20) as r: status=r.status
result={'uid':os.getuid(),'gid':os.getgid(),'cwd':str(Path.cwd()),'filesystem':True,
        'git_head':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'https_status':status}
Path(sys.argv[1]).write_text(json.dumps(result,sort_keys=True))
print(json.dumps(result,sort_keys=True))
''')
    cfg=bridge.BridgeConfig.load(Path(__file__).with_name('bridge.toml'))
    cfg=dataclasses.replace(cfg,codex_binary='/home/pvxlabs/.local/bin/codex-raw')
    with isolated_provider(cfg,tmp_path) as cfg:
        cfg=dataclasses.replace(cfg,task_db_path=tmp_path/'tasks.sqlite3',log_dir=tmp_path/'logs',targets=(),threads=(),
            projects=(bridge.ProjectMapping(linear_name='Authority acceptance',alias='authority-test',repo=root,branch='main',workspace_alias='p620'),),
            workspaces=(WorkspaceConfig(alias='p620',root=tmp_path,host='p620'),),
            host_executor=dataclasses.replace(cfg.host_executor,workflows=registered,local_commands=(),ssh_targets=(),services=()))
        done=threading.Event()
        with existing_client(cfg.app_server.local_socket,60,read_only=False) as direct:
            direct.initialize(client_name='clinx-authority-direct',client_title='Isolated direct parity',client_version='1')
            model, effort=direct.resolve_model('gpt-5.6-luna','high')
            thread=direct.thread_start(cwd=str(root),sandbox='workspace-write',model=model)
            turn=direct.turn_start(thread['id'], 'Run exactly python3 parity_probe.py direct.json using your native shell tool in this workspace. '
                'This performs a harmless local filesystem/Git probe and an authorized HTTPS GET. No other actions. Report its result.',
                cwd=str(root),approval_policy='never',network_access=True,writable_roots=[str(root)],model=model,reasoning_effort=effort)
            direct.configure_completion_handoff(thread['id'],turn.turn_id,lambda _:done.set())
            direct.supervise_turn(thread['id'],turn.turn_id)
            assert done.wait(120),'direct Codex probe timeout'
            direct._supervisor.join(timeout=5)
        assert (root/'direct.json').exists(),'direct native probe did not execute'
        registry=TaskRegistry(cfg.task_db_path)
        dispatcher=bridge.TaskDispatcher(cfg,task_registry=registry)
        integration=ClinxIntegration(cfg,registry,dispatcher,bridge.TaskContextReader(cfg,registry),None)
        server=ClinxMCPServer(integration)
        evidence={'source':str(Path(__file__).parent),'direct_thread':thread['id'], 'native':{}, 'host':{}}
        def tool(name, args):
            result=server.handle({'id':name,'method':'tools/call','params':{'name':name,'arguments':args}})['result']
            assert not result['isError'], result
            return result['structuredContent']
        def wait(ref):
            deadline=time.monotonic()+180
            while time.monotonic()<deadline:
                result=registry.get_execution_result(ref)
                if result and not registry.get_active_execution(ref):
                    return result
                time.sleep(.2)
            raise AssertionError('managed completion timeout: '+ref)
        try:
            native=tool('clinx_prepare_execution',dict(approved=True,task_action='create',host='p620',project='authority-test',
                title='Native to safe DATA authority transition',summary='Isolated real native to Host acceptance',network_access=True,
                prompt='Run exactly python3 parity_probe.py managed.json using your native shell tool in this workspace. '
                    'This is an authorized harmless filesystem/Git and HTTPS probe. No other actions. '
                    'Then return a multiline CLINX_EXECUTION_RESULT, STATUS=PASS if exit 0 otherwise BLOCKED, '
                    'SUMMARY=Native parity probe, CHANGED_FILES=managed.json, VALIDATION=actual command result, '
                    'BLOCKERS=NONE on success, NEXT_STATE=COMPLETED. Each field on its own line.'))
            a=tool('clinx_start_execution',dict(approved=True,prepared_execution_ref=native['prepared_execution_ref']))
            print('AUTHORITY_LIVE_NATIVE',a['task_ref'],a['execution_ref'],tmp_path,flush=True)
            ar=wait(a['execution_ref']); assert ar.status=='PASS',ar
            first_policy=registry.get_execution_policy(a['execution_ref'])
            first_binding=registry.get_binding(a['task_ref'])
            assert json.loads((root/'direct.json').read_text()) == json.loads((root/'managed.json').read_text())
            evidence['native']={'task_ref':a['task_ref'],'execution_ref':a['execution_ref'], 'result':dataclasses.asdict(ar),
                'parity':json.loads((root/'managed.json').read_text()),'policy':first_policy.as_dict()}
            identity=tool('clinx_get_effective_authority',{'task_ref':a['task_ref']})
            change=tool('clinx_prepare_policy_reauthorization',dict(approved=True,task_ref=a['task_ref'],
                expected_policy_hash=identity['policy_hash'],target_policy=target_policy(),reason='Explicitly approved isolated DATA-like controller calls'))
            changed=tool('clinx_apply_policy_reauthorization',dict(approved=True,prepared_reauthorization_ref=change['prepared_reauthorization_ref']))
            assert not changed['execution_started']
            assert tool('clinx_apply_policy_reauthorization',dict(approved=True,prepared_reauthorization_ref=change['prepared_reauthorization_ref']))['idempotent']
            requests=[]
            for candidate in ('not-a-sha','a'*40,'b'*40):
                requests.append(dict(operation_class='PRODUCTION_MUTATION',capability='LOCAL_HOST_PROCESS',
                    operation='workflow:SAFE_DEPLOY:apply',arguments={'target':'safe-data','parameters':{
                    'source_sha':candidate,'scope':'DATA','artifact_digest':'f'*64}}))
            bprep=tool('clinx_prepare_execution',dict(approved=True,task_ref=a['task_ref'],requested_operations=[scope()],
                prompt='This is explicitly authorized isolated negative/positive workflow acceptance. Use ONLY clinx.clinx_host_operation. '
                    'Call these three requests exactly once each, sequentially: '+json.dumps(requests)+'. '
                    'The first intentionally invalid candidate must be rejected before dispatch, and this expected rejection is a successful check. '
                    'Then continue immediately with the two legal candidates. No native shell or other tools, no retries, no self reauthorization. '
                    'Return exactly a multiline CLINX_EXECUTION_RESULT with STATUS=PASS if the invalid candidate was rejected and both valid '
                    'candidates exited 0, SUMMARY=Same Task workflow accepted, CHANGED_FILES=calls.jsonl, VALIDATION=actual Host refs and expected rejection, '
                    'BLOCKERS=NONE, NEXT_STATE=COMPLETED. Each field on its own line.'))
            assert bprep['network_access'] is True,'omitted continuation network must preserve authorization'
            b=tool('clinx_start_execution',dict(approved=True,prepared_execution_ref=bprep['prepared_execution_ref']))
            print('AUTHORITY_LIVE_HOST',b['task_ref'],b['execution_ref'],flush=True)
            br=wait(b['execution_ref']); assert br.status=='PASS',br
            assert b['task_ref']==a['task_ref']
            assert registry.get_execution_policy(a['execution_ref'])==first_policy
            lineage=registry.get_binding_lineage(a['task_ref'])
            assert lineage.predecessor_thread==first_binding.thread_id
            deliveries=ToolDeliveryLedger.records(registry,b['execution_ref'])
            hosts=registry.list_host_executions(execution_ref=b['execution_ref'])
            assert len(deliveries)==3 and len(hosts)==2
            assert deliveries[0]['execution_state']=='COMMAND_NOT_DISPATCHED'
            assert all(h['exit_code']==0 for h in hosts)
            assert all(r['delivery_state']=='DELIVERED' for r in deliveries[1:])
            assert len((root/'calls.jsonl').read_text().splitlines())==2
            old=tool('clinx_get_status',{'thread_id':first_binding.thread_id})
            assert old['task_ref']==a['task_ref'] and old['execution_ref']==a['execution_ref']
            evidence['host']={'task_ref':b['task_ref'],'execution_ref':b['execution_ref'],'result':dataclasses.asdict(br),
                'deliveries':deliveries,'host_count':len(hosts),'lineage':dataclasses.asdict(lineage),'policy_change':changed}
            with registry._connect() as conn:
                assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0]==1
                assert conn.execute('SELECT count(*) FROM worktree_leases').fetchone()[0]==0
        finally:
            (tmp_path/'authority-acceptance.json').write_text(json.dumps(evidence,indent=2,default=str))
            print('AUTHORITY_LIVE_EVIDENCE',tmp_path/'authority-acceptance.json',flush=True)
            dispatcher.stop_completion_runtime()
