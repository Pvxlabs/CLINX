"""Native interop contract: real SQLite/files; scripted RPC tests are labelled."""
from concurrent.futures import ThreadPoolExecutor
import dataclasses
import hashlib
import gc
import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock, patch

import pytest
import jsonschema

import app_server
import bridge
from m9_integration import ClinxIntegration
from mcp_server import ClinxMCPServer, tool_definitions
from native_history import NativeHistory, SCAN_BYTES, message
from native_provider import select_writer_client
from test_m6 import dispatcher_fixture, FakeClient
from thread_identity import ThreadIdentityReader, resolve_selector, ThreadLookupError

TID = '01a0fa4e-11e7-7540-91c8-1eee45d1a4d0'
URI = 'codex://threads/' + TID + '?hostId=remote-ssh-discovered%3Ap620'


@pytest.fixture
def native(tmp_path):
    dispatcher, repo = dispatcher_fixture(tmp_path, tmp_path/'tasks.db', [])
    cfg = dataclasses.replace(dispatcher.cfg, workspaces=tuple(dataclasses.replace(
        w, codex_host_ids=('remote-ssh-discovered:p620',)) for w in dispatcher.cfg.workspaces))
    dispatcher.cfg = cfg
    root = tmp_path/'native'; root.mkdir()
    rollout = root/('rollout-fixture-' + TID + '.jsonl')
    rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': TID, 'cwd': str(repo), 'cli_version': '0.156.1'}})+'\n')
    with sqlite3.connect(root/'state_5.sqlite') as c:
        c.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT,rollout_path TEXT,history_mode TEXT,name TEXT,archived INT,cli_version TEXT)')
        c.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?)', (TID,str(repo),str(rollout),'paginated','Native fixture',0,'0.156.1'))
    with sqlite3.connect(root/'thread_history_1.sqlite') as c:
        c.executescript('''CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,rollout_ordinal INT,status TEXT,started_at INT,completed_at INT,PRIMARY KEY(thread_id,turn_id));
        CREATE INDEX turns_page ON thread_turns(thread_id,rollout_ordinal);
        CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,rollout_ordinal INT,created_at_ms INT,item_type TEXT,item_json TEXT,PRIMARY KEY(thread_id,turn_id,item_id));
        CREATE INDEX items_page ON thread_items(thread_id,turn_id,rollout_ordinal);
        CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY,next_rollout_byte_offset INT);
        ''')
        for i in range(3):
            turn = f'turn-{i}'
            c.execute('INSERT INTO thread_turns VALUES(?,?,?,?,?,?)',(TID,turn,i,'inProgress' if i==2 else 'completed',i,None if i==2 else i+1))
            for j, item in enumerate([{'type':'userMessage','content':[{'type':'text','text':f'User {i}'}]},
                {'type':'agentMessage','text':f'Answer {i}','phase':'final_answer'},
                {'type':'reasoning','text':'NEVER_RETURN_REASONING'},
                {'type':'agentMessage','text':'NEVER_RETURN_ANALYSIS','channel':'analysis'},
                {'type':'commandExecution','aggregatedOutput':'LARGE_TOOL_SECRET'*200000}]):
                c.execute('INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)',(TID,turn,f'{i}-{j}',i*10+j,i,item['type'],json.dumps(item)))
        c.execute('INSERT INTO thread_history_projection_state VALUES(?,?)',(TID,rollout.stat().st_size))
    context = bridge.TaskContextReader(cfg,dispatcher.tasks,client_factory=Mock(side_effect=AssertionError('no provider startup')))
    reader = ThreadIdentityReader(cfg,dispatcher.tasks.path,context,native_root=root)
    integration = ClinxIntegration(cfg,dispatcher.tasks,dispatcher,context,Mock(side_effect=AssertionError('no Linear')))
    return type('Fixture', (), dict(cfg=cfg, dispatcher=dispatcher, repo=repo, root=root,
        reader=reader, integration=integration, rollout=rollout, registry=dispatcher.tasks))()


def hashes(f):
    # Flush fixture setup connections before hashing SQLite/WAL physical files.
    gc.collect()
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in
            [f.root/'state_5.sqlite', f.root/'thread_history_1.sqlite', f.registry.path, f.rollout]}


def fork_rollout(native):
    path = native.rollout.with_name(native.rollout.stem + '_01a10a7a-fbd5-7e62-a051-50c66e71ebf5.jsonl')
    native.rollout.rename(path)
    native.rollout = path
    with sqlite3.connect(native.root / 'state_5.sqlite') as conn:
        conn.execute('UPDATE threads SET rollout_path=? WHERE id=?', (str(path), TID))
    history = NativeHistory(native.root)
    return history, history.metadata(TID)


def test_native_fork_suffix_reads_original_session_without_mutation(native):
    history, metadata = fork_rollout(native)
    before = hashes(native)
    result = history.context(TID, 'p620', metadata, 4, 16000)
    assert result['context_status'] == 'AVAILABLE'
    assert result['last_codex_result'] == 'Answer 2'
    assert hashes(native) == before


@pytest.mark.parametrize('conflict', ['thread', 'project', 'outside-root', 'header-shape'])
def test_native_fork_suffix_keeps_identity_and_root_guards(native, tmp_path, conflict):
    history, metadata = fork_rollout(native)
    if conflict == 'outside-root':
        outside = tmp_path / native.rollout.name
        native.rollout.rename(outside)
        native.rollout.symlink_to(outside)
    elif conflict == 'header-shape':
        native.rollout.write_text('[]\n')
    else:
        header = json.loads(native.rollout.read_text())
        header['payload']['id' if conflict == 'thread' else 'cwd'] = 'wrong-identity'
        native.rollout.write_text(json.dumps(header) + '\n')
    with pytest.raises(ThreadLookupError) as error:
        history.context(TID, 'p620', metadata, 4, 16000)
    assert error.value.code == 'NATIVE_IDENTITY_CONFLICT'


def test_original_uri_schema_and_exact_route(native):
    server = ClinxMCPServer(native.integration)
    before = hashes(native)
    for name in ('clinx_get_context','clinx_get_status'):
        definition = next(t for t in tool_definitions() if t['name']==name)
        jsonschema.validate({'codex_uri': URI},definition['inputSchema'])
        with patch('thread_identity.ThreadIdentityReader',return_value=native.reader):
            result = server.handle({'id':1,'method':'tools/call','params':{'name':name,'arguments':{'codex_uri':URI}}})['result']
        assert not result['isError'], result
        body = result['structuredContent']
        jsonschema.validate(body,definition['outputSchema'])
        assert body['codex_uri'] == URI and body['host']=='p620'
        assert body['native_thread']['cwd'] == str(native.repo)
        assert body['native_status']['turn_id']=='turn-2'
        if name=='clinx_get_context': assert body['last_codex_result']=='Answer 2'
    assert before == hashes(native)
    assert resolve_selector(native.cfg,codex_uri=URI)[0:2] == resolve_selector(native.cfg,thread_id=TID,host='p620')[0:2]


@pytest.mark.parametrize('query', ['hostId=x&hostId=x','hostId=x&x=1','hostId=%','hostId=%253Ap620',
    'hostId=%FF','hostId=remote-ssh-discovered%3Ap620%0a','hostId=p620@attacker','hostId=abc+def','x=1',''])
def test_uri_ambiguity_is_structured(native, query):
    r=native.reader.read(codex_uri='codex://threads/'+TID+'?'+query)
    assert r['error_code']=='INVALID_CODEX_THREAD_URI'


def test_unknown_host_and_host_conflict(native):
    assert native.reader.read(codex_uri=URI.replace('p620','air'))['error_code']=='UNKNOWN_CODEX_HOST_ID'
    assert native.reader.read(thread_id=TID,host='attacker.example')['error_code']=='UNKNOWN_THREAD_HOST'
    remote = dataclasses.replace(native.cfg.workspaces[0], alias='air',host='air',codex_host_ids=())
    cfg = dataclasses.replace(native.cfg,workspaces=(*native.cfg.workspaces,remote))
    with pytest.raises(ThreadLookupError, match='conflicts'):
        resolve_selector(cfg,codex_uri=URI,host='air')


def test_unregistered_project_history_and_pagination(native):
    native.reader.cfg = dataclasses.replace(native.cfg,projects=())
    before=hashes(native)
    r=native.reader.read(codex_uri=URI,context=True,recent_turns=1,max_bytes=1024)
    assert r['lookup_status']=='THREAD_UNBOUND' and r['context_status']=='AVAILABLE'
    assert r['last_user_intent']=='User 2' and r['last_codex_result']=='Answer 2'
    assert r['task_ref'] is None and r['execution_ref'] is None
    assert 'NEVER_RETURN' not in json.dumps(r) and 'LARGE_TOOL_SECRET' not in json.dumps(r)
    page=native.reader.read(thread_id=TID,context=True,recent_turns=1,cursor=r['next_cursor'])
    assert page['last_user_intent']=='User 1'
    assert before==hashes(native)
    with sqlite3.connect(native.root/'state_5.sqlite') as c: c.execute('UPDATE threads SET archived=1')
    assert native.reader.read(thread_id=TID,context=True)['native_thread']['archived']==1


def test_cursor_identity_and_unknown_do_not_create(native):
    r=native.reader.read(thread_id=TID,context=True,recent_turns=1)
    cursor=r['next_cursor']
    assert native.reader.read(thread_id=TID,context=True,cursor=cursor+'!')['error_code']=='INVALID_CONTEXT_CURSOR'
    missing='00000000-0000-7000-8000-000000000000'
    before=hashes(native)
    with patch('thread_identity.ThreadIdentityReader',return_value=native.reader):
        r=native.integration.adopt_conversation(thread_id=missing)
    assert r['adoption_status']=='BLOCKED' and r['lookup_status']=='THREAD_NOT_FOUND'
    assert hashes(native)==before


def test_explicit_adoption_active_is_idempotent_and_no_turn(native):
    native.reader.live_reader=lambda cfg,tid: {'state':'active','owner_endpoint':'fixture-owner','observations':[]}
    before=hashes(native)
    native.integration.linear=Mock()
    with patch('thread_identity.ThreadIdentityReader',return_value=native.reader):
        first=native.integration.adopt_conversation(codex_uri=URI)
        second=native.integration.adopt_conversation(thread_id=TID,host='p620')
    assert first['adoption_status']=='ADOPTED',first
    assert second['task_ref']==first['task_ref'] and second['adoption_status']=='ALREADY_ADOPTED'
    assert first['native_name']=='Native fixture' and first['session_id']==TID
    assert not first['execution_started'] and not first['control_transferred']
    native.integration.linear.assert_not_called()
    for path,digest in before.items():
        if path != str(native.registry.path): assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest
    with sqlite3.connect(native.registry.path) as c:
        for table in ('executions','execution_history','worktree_leases','context_checkpoints'):
            assert c.execute('SELECT count(*) FROM '+table).fetchone()[0]==0


def test_unregistered_adoption_uses_existing_trusted_project_resolver(native):
    cfg=dataclasses.replace(native.cfg,projects=())
    native.integration.cfg=native.reader.cfg=cfg
    native.dispatcher.cfg=cfg
    native.dispatcher.projects=bridge.DynamicProjectResolver(native.dispatcher.workspaces,())
    with patch('thread_identity.ThreadIdentityReader',return_value=native.reader):
        adopted=native.integration.adopt_conversation(codex_uri=URI)
    assert adopted['adoption_status']=='ADOPTED' and adopted['cwd']==str(native.repo)
    assert not adopted['control_transferred']


def test_native_read_survives_unavailable_clinx_indexes(native):
    with sqlite3.connect(native.registry.path) as c:c.execute('DROP INDEX idx_thread_lineage_predecessor')
    before=hashes(native)
    result=native.reader.read(thread_id=TID,context=True)
    assert result['context_status']=='AVAILABLE' and result['binding_status']=='UNAVAILABLE'
    assert hashes(native)==before


def test_failure_before_turn_is_latest_and_old_success_stays_exact(native):
    with patch('thread_identity.ThreadIdentityReader',return_value=native.reader):
        task=native.integration.adopt_conversation(thread_id=TID)['task_ref']
    registry=native.registry
    with registry.execution(task,None,execution_ref='exec_old',retain=True):
        registry.set_execution_state(task,'CODEX_RUNNING',turn_id='old-turn')
        registry.record_execution_result(execution_ref='exec_old',task_id=task,turn_id='old-turn',
            status='PASS',summary='old success',changed_files='NONE',validation='old',blockers='NONE',
            next_state='COMPLETED',raw_result='old success')
        registry.reconcile_terminal('exec_old','COMPLETED')
    with registry.execution(task,None,execution_ref='exec_failed',retain=True):
        registry.reconcile_terminal('exec_failed','BLOCKED',failure_stage='thread/resume',
            failure_code='NATIVE_WRITER_OWNER_UNPROVEN',evidence='writer refused; no new turn')
    current=native.integration.get_status(task_ref=task)
    old=native.integration.get_status(execution_ref='exec_old')
    assert current['execution_ref']=='exec_failed' and current['execution_result'] is None
    assert current['failure_stage']=='thread/resume' and current['failure_evidence']=='writer refused; no new turn'
    assert not current['TURN_PRESENT']
    assert old['execution_result']['status']=='PASS' and old['failure_code'] is None
    result=native.reader.read(thread_id=TID,execution_ref='exec_failed',context=True)
    assert result['execution_state']=='BLOCKED' and result['failure_stage']=='thread/resume'
    assert result['context_status']=='CONTEXT_UNAVAILABLE'


def test_dispatch_resume_rejection_records_exact_failure_before_turn(native):
    with patch('thread_identity.ThreadIdentityReader',return_value=native.reader):
        task=native.integration.adopt_conversation(thread_id=TID)['task_ref']
    client=FakeClient(existing={'id':TID,'sessionId':TID,'projectId':None,'cwd':str(native.repo),
        'ephemeral':False,'gitInfo':{'originUrl':'https://example.invalid/pilot.git','branch':'main'},
        'canAcceptDirectInput':True,'status':{'type':'idle'}})
    def refuse(*args,**kwargs):
        raise app_server.AppServerRemoteError('thread/resume',{'code':-32600,'message':'already has an active writer'})
    client.thread_resume=refuse
    native.dispatcher.client_factory=lambda target:client
    with pytest.raises(app_server.AppServerRemoteError):
        native.dispatcher.dispatch(project_ref='pilot',host='p620',project_mode='existing',task_mode='continue',
            task_id=task,prompt='New authorized fixture request',title='Native fixture',summary=None,
            model=None,reasoning_effort=None,execution_ref='exec_resume_failed')
    status=native.integration.get_status(task_ref=task)
    assert status['execution_ref']=='exec_resume_failed' and not status['TURN_PRESENT']
    assert status['failure_stage']=='thread/resume' and 'active writer' in status['failure_evidence']
    assert not any(call[0]=='turn/start' for call in client.calls)


def test_concurrent_adoption_uses_single_canonical_binding(native):
    with patch('thread_identity.ThreadIdentityReader',return_value=native.reader):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _: native.integration.adopt_conversation(thread_id=TID),range(2)))
    assert len({r['task_ref'] for r in results})==1,results
    with sqlite3.connect(native.registry.path) as c:
        assert c.execute('SELECT count(*) FROM conversation_bindings WHERE thread_id=?',(TID,)).fetchone()[0]==1


def test_legacy_large_tail_can_page_back_to_display(native):
    header=native.rollout.read_text()
    records=[{'type':'turn_context','payload':{'turn_id':'legacy-turn'}},
        {'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'Legacy user'}]}},
        {'type':'response_item','payload':{'type':'message','role':'assistant','channel':'final','content':[{'type':'output_text','text':'Legacy answer'}]}},
        {'type':'response_item','payload':{'type':'function_call_output','output':'x'*(SCAN_BYTES*3)}}]
    native.rollout.write_text(header+'\n'.join(json.dumps(r) for r in records)+'\n')
    with sqlite3.connect(native.root/'state_5.sqlite') as c:c.execute("UPDATE threads SET history_mode='legacy'")
    cursor=None
    for i in range(5):
        r=native.reader.read(thread_id=TID,context=True,max_bytes=1024,cursor=cursor)
        assert r['provenance']['source_bytes']<=SCAN_BYTES
        if r['context_status']=='AVAILABLE':break
        cursor=r['next_cursor'];assert cursor
    assert r['last_user_intent']=='Legacy user' and r['last_codex_result']=='Legacy answer'


def test_display_budget_redaction_and_projection_lag(native):
    with sqlite3.connect(native.root/'thread_history_1.sqlite') as c:
        c.execute('UPDATE thread_items SET item_json=? WHERE item_id=?',
            (json.dumps({'type':'agentMessage','text':'api_key=sampleSECRET '+('中'*5000),'phase':'final_answer'}),'2-1'))
        c.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=0')
    r=native.reader.read(thread_id=TID,context=True,max_bytes=1024)
    assert len((r['last_user_intent']+r['last_codex_result']).encode())<=1024
    assert 'sampleSECRET' not in json.dumps(r) and r['context_truncated']
    assert r['provenance']['projection_incomplete']


def test_native_context_preserves_markdown_structure_and_redaction(native):
    text = ('## Result\n\nFirst paragraph.\n\n'
            '| Item | Result |\n| --- | --- |\n| Repo | **PASS** |\n\n'
            '- One\n- Two\n\n```python\nif ready:\n    run()\n```\n\n'
            'api_key=sampleSECRET\nlin_api_sampleSECRET\n'
            '<in-app-browser-context>private\nambient browser state</in-app-browser-context>')
    with sqlite3.connect(native.root/'thread_history_1.sqlite') as c:
        c.execute('UPDATE thread_items SET item_json=? WHERE item_id=?',
                  (json.dumps({'type': 'agentMessage', 'text': text, 'phase': 'final_answer'}), '2-1'))
    result = native.reader.read(thread_id=TID, context=True, max_bytes=16000)
    assert result['context_status'] == 'AVAILABLE'
    body = result['last_codex_result']
    assert body.startswith('## Result\n\nFirst paragraph.\n\n| Item | Result |\n')
    assert '- One\n- Two\n\n```python\nif ready:\n    run()\n```' in body
    assert body.endswith('api_key=[REDACTED]\n[REDACTED]\n')
    assert 'sampleSECRET' not in body and 'ambient browser state' not in body
    assert not result['context_truncated']


def test_display_retains_leading_code_indentation_and_blank_lines():
    text = '    first()\n    second()\n\nFinal paragraph.\n'
    assert message({'type': 'agentMessage', 'text': text}) == ('assistant', text)
    assert message({'type': 'message', 'role': 'user', 'content': [
        {'type': 'input_text', 'text': text + '\x00'}]}) == ('user', text)


@pytest.mark.parametrize('states,expected', [(['notLoaded','idle'],'public'),(['idle','notLoaded'],'managed'),
    (['notLoaded','notLoaded'],'managed'),(['idle','active'],None),(['idle','idle'],None),(['idle','UNKNOWN'],None)])
def test_writer_routes_exact_idle_owner_no_blind_fallback(native,states,expected):
    observation={'observations':[{'endpoint':p,'state':s} for p,s in zip(['managed','public'],states)],
                 'ownership_conflict':states==['idle','idle']}
    with patch('native_provider.observe_thread',return_value=observation),patch('native_provider.existing_client') as create:
        if expected is None:
            with pytest.raises(app_server.AppServerProtocolError):select_writer_client(native.cfg,TID)
            create.assert_not_called()
        else:
            select_writer_client(native.cfg,TID)
            assert create.call_args.args[0]==expected


def test_reader_and_nonowner_never_unsubscribe_or_answer_tools():
    wire=Mock();client=app_server.CodexAppServerClient(wire)
    client._release_owned_subscription(TID)
    client._send_server_response({'id':'foreign','method':'item/tool/call','params':{'threadId':TID}})
    client.close();wire.send.assert_not_called()


def test_observer_refuses_every_mutation_and_all_server_requests():
    wire=Mock();client=app_server.CodexAppServerClient(wire);client.read_only_observer=True
    for method in ('thread/resume','thread/start','thread/unsubscribe','turn/start','turn/interrupt'):
        with pytest.raises(app_server.AppServerProtocolError):client._request(method,{'threadId':TID})
    for method in ('item/tool/call','item/commandExecution/requestApproval','account/chatgptAuthTokens/refresh'):
        client._send_server_response({'id':1,'method':method,'params':{'threadId':TID}})
    wire.send.assert_not_called()


def test_tool_broadcast_on_same_thread_different_turn_has_no_response():
    wire=Mock();handler=Mock();client=app_server.CodexAppServerClient(wire,strict_dynamic_tool_binding=True)
    client.configure_dynamic_tool(namespace='clinx',name='clinx_host_operation',thread_id=TID,handler=handler)
    client.attach_dynamic_tool_turn(TID,'owned')
    client._send_server_response({'id':1,'method':'item/tool/call','params':{'threadId':TID,'turnId':'foreign',
        'namespace':'clinx','tool':'clinx_host_operation','callId':'call','arguments':{}}})
    wire.send.assert_not_called();handler.assert_not_called()


def test_exact_completion_releases_own_subscription_before_handoff():
    client=app_server.CodexAppServerClient(Mock())
    client._owned_thread_ids={TID,'other-active'};client._owned_turns={TID:'own-turn','other-active':'other-turn'}
    client.received_events=[{'method':'turn/completed','params':{'threadId':TID,'turn':{'id':'own-turn'}}}]
    calls=[]
    client._request=lambda method,params: (calls.append((method,params)) or {'status':'unsubscribed'})
    client.configure_completion_handoff(TID,'own-turn',lambda event:calls.append(('handoff',event)))
    client.supervise_turn(TID,'own-turn');client._supervisor.join(timeout=2)
    assert not client._supervisor.is_alive()
    assert calls[0]==('thread/unsubscribe',{'threadId':TID}) and calls[1][0]=='handoff'
    assert len(calls)==2 and 'other-active' in client._owned_thread_ids
