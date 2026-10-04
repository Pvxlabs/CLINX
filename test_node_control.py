"""Formal MCP -> paired mTLS -> canonical node adapter regression."""
import dataclasses
import json
from unittest.mock import Mock, patch

import bridge
import pytest
from m9_integration import ClinxIntegration
from mcp_server import ClinxMCPServer
from node_execution_adapter import CanonicalNodeExecutionAdapter
from node_protocol import NodeAuthorizationStore, NodeRecord, NodeRegistry, NodeRouter, NodeRPCServer, NodeService, SharingScope, _now
from node_runtime import CentreService, NodeRegistrationClient, PairedTLS, node_request_handler, remote_client_factory
from task_registry import TaskRegistry
from test_native_interop import native, TID
from test_node_runtime import paired
from test_m6 import FakeClient


def test_formal_remote_canonical_control(native, tmp_path):
    node, np, centre, cp = paired(tmp_path/'pair')
    native.cfg = dataclasses.replace(native.cfg, threads=(), runtime_host='air', app_server=dataclasses.replace(native.cfg.app_server, native_home=str(native.root)),
        workspaces=tuple(dataclasses.replace(w, host='air', codex_host_ids=()) for w in native.cfg.workspaces))
    native.integration.cfg = native.reader.cfg = native.dispatcher.cfg = native.cfg
    native.dispatcher.workspaces = bridge.WorkspaceRegistry(native.cfg.workspaces)
    native.dispatcher.projects = bridge.DynamicProjectResolver(native.dispatcher.workspaces, native.cfg.projects)
    native.integration.linear = None
    adapter = CanonicalNodeExecutionAdapter(native.integration, 'air', allowed_threads=[TID], allowed_projects=['pilot'])
    ns, cs = NodeAuthorizationStore(node.store.root), NodeAuthorizationStore(centre.store.root)
    ns.grant('p620', user_scope='test', read_sessions=True, execute_tasks=True)
    cs.grant('air', user_scope='test', read_sessions=True, execute_tasks=True)
    nr, cr = NodeRegistry(':memory:'), NodeRegistry(tmp_path/'routes.db')
    active = [ns.get('p620')]
    service = NodeService(NodeRecord('air',user_scope='test',state='ONLINE',last_seen=_now()), nr, scope=active[0],
        read_thread=lambda tid,**kw: adapter.context(thread_id=tid,host='air',**kw),
        adopt_conversation=adapter.adopt, prepare_execution=adapter.prepare, start_execution=adapter.start,
        status_execution=adapter.status, context_execution=adapter.context, cancel_execution=adapter.cancel)
    nsrv = NodeRPCServer(service,address='127.0.0.1',port=0,ssl_context_factory=PairedTLS(node,np).server_context,
        request_handler=node_request_handler(service,PairedTLS(node,np),ns,'p620',lambda:active[0]))
    csrv = CentreService(PairedTLS(centre,cp),cr,cs).server(address='127.0.0.1',port=0)
    try:
        registration=NodeRegistrationClient(PairedTLS(node,np),'p620',f'tls://127.0.0.1:{csrv.address[1]}')
        reg=registration.register(node,ns.get('p620'),port=nsrv.address[1],capabilities=('session.read','execution.adopt','execution.prepare','execution.start'))
        assert reg['registered']; active[0]=SharingScope(**reg['scope'])
        router=NodeRouter(cr,user_scope='test',local_node_id='p620',remote_client_factory=remote_client_factory(centre.store.root,'p620',cr))
        cfg=dataclasses.replace(native.cfg,runtime_host='p620',node_router=router,workspaces=tuple(dataclasses.replace(w,host='p620',codex_host_ids=('remote-ssh-discovered:p620',)) for w in native.cfg.workspaces))
        local=TaskRegistry(tmp_path/'centre-tasks.db')
        api=ClinxIntegration(cfg,local,Mock(),Mock(),None)
        server=ClinxMCPServer(api,allow_execute=True)
        def tool(name,args):
            reply=server.handle({'id':1,'method':'tools/call','params':{'name':'clinx_'+name,'arguments':args}})['result']
            assert not reply.get('isError'), reply
            return reply['structuredContent']
        with patch('thread_identity.ThreadIdentityReader',return_value=native.reader):
            a=tool('adopt_conversation',dict(thread_id=TID,host='air'))
            assert a.get('adoption_status')=='ADOPTED' and a['thread_id']==TID and not a['execution_started'], a
            b=tool('adopt_conversation',dict(thread_id=TID,host='air'))
            assert b['task_ref']==a['task_ref'] and b['adoption_status']=='ALREADY_ADOPTED'
            denied=tool('adopt_conversation',dict(thread_id=TID,host='bogus'))
            assert denied['error_code']=='UNKNOWN_THREAD_HOST'
            conflict=tool('adopt_conversation',dict(codex_uri='codex://threads/'+TID+'?hostId=remote-ssh-discovered%3Ap620',host='air'))
            assert conflict['error_code']=='THREAD_HOST_CONFLICT'
        prep=tool('prepare_execution',dict(approved=True,task_ref=a['task_ref'],host='air',prompt='bounded acceptance'))
        assert prep['host']=='air' and not native.registry.get_active_execution(a['task_ref'])
        # Centre restart retains only reference routes, not canonical tasks.
        cr.close(); cr=NodeRegistry(tmp_path/'routes.db')
        router=NodeRouter(cr,user_scope='test',local_node_id='p620',remote_client_factory=remote_client_factory(centre.store.root,'p620',cr))
        api.cfg=dataclasses.replace(cfg,node_router=router)
        assert cr.reference_node(prep['prepared_execution_ref'],'test')=='air'
        fake=FakeClient(thread_id=TID,session_id=TID,existing={'id':TID,'sessionId':TID,'cwd':str(native.repo),
            'ephemeral':False,'gitInfo':{'originUrl':'https://example.invalid/pilot.git','branch':'main'},'status':{'type':'idle'},'canAcceptDirectInput':True})
        native.dispatcher.client_factory=lambda target:fake
        started=tool('start_execution',dict(approved=True,prepared_execution_ref=prep['prepared_execution_ref']))
        assert started['execution_started'] and started['node_id']=='air'
        replay=tool('start_execution',dict(approved=True,prepared_execution_ref=prep['prepared_execution_ref']))
        assert replay['execution_ref']==started['execution_ref'] and replay['idempotent']
        assert len([c for c in fake.calls if c[0]=='turn/start'])==1
        assert not [c for c in fake.calls if c[0]=='thread/start']
        assert native.registry.get_binding(a['task_ref']).thread_id==TID
        status=tool('get_status',dict(execution_ref=started['execution_ref']))
        assert status['task_ref']==a['task_ref'] and status['execution_ref']==started['execution_ref']
        with patch('thread_identity.ThreadIdentityReader',return_value=native.reader):
            context=tool('get_context',dict(task_ref=a['task_ref']))
            assert context['task_ref']==a['task_ref']
        with local._connect() as c:
            assert c.execute('SELECT count(*) FROM tasks').fetchone()[0]==0
        cs.grant('air',user_scope='test',read_sessions=True,execute_tasks=False)
        denied=tool('start_execution',dict(approved=True,prepared_execution_ref=prep['prepared_execution_ref']))
        assert denied['error_code']=='SHARING_SCOPE_DENIED'
        assert len([c for c in fake.calls if c[0]=='turn/start'])==1
    finally:
        nsrv.close();csrv.close();nr.close();cr.close()


def test_unknown_start_reconciles_without_replay():
    registry=NodeRegistry(':memory:'); calls=[]
    service=NodeService(NodeRecord('air'),registry,scope=SharingScope('test',True,True),read_thread=Mock(),
        start_execution=lambda **kw: calls.append(kw) or (_ for _ in ()).throw(TimeoutError()),
        status_execution=lambda **kw: {'execution_ref':'exec_same','task_ref':'task_same','execution_state':'COMPLETED'})
    request=dict(operation='execution.start',execution_ref='exec_same',prepared_execution_ref='prepared_same',request_id='same',user_scope='test')
    assert service.handle(request)['side_effect']=='UNKNOWN'
    r=service.handle(request)
    assert r['operation_state']=='RECONCILED' and len(calls)==1
    assert service.handle(dict(request,approved=True))['error_code']=='IDEMPOTENCY_KEY_REUSE'


def test_scoped_adapter_and_busy_writer(native):
    adapter=CanonicalNodeExecutionAdapter(native.integration,'p620',allowed_threads=[TID],allowed_projects=['clinx'],allowed_new_projects=[])
    with pytest.raises(Exception,match='outside'):
        adapter.adopt(thread_id='01a10557-4fbc-7262-be1b-f32ca788f2ee')
    with pytest.raises(Exception,match='approved original'):
        adapter.prepare(approved=True,project='clinx',task_action='create',prompt='bad')
    prepared=Mock(host='p620',task_ref='task_test',project='clinx',status='PREPARED')
    native.integration.registry=Mock();native.integration.registry.verify_prepared_execution.return_value=prepared
    native.integration.registry.get_task.return_value=Mock(host='p620',task_id='task_test',project_alias='clinx')
    native.integration.registry.get_binding.return_value=Mock(thread_id=TID)
    native.dispatcher._uses_default_client_factory=True
    native.integration.start_execution=Mock()
    from native_provider import NativeWriterError
    with patch('native_provider.select_writer_client',side_effect=NativeWriterError('NATIVE_ACTIVE_OWNER','busy')):
        with pytest.raises(NativeWriterError):adapter.start(prepared_execution_ref='prepared_test',approved=True)
    native.integration.start_execution.assert_not_called()


def test_parallel_stdio_preserves_existing_completion_owner():
    import io
    from mcp_server import serve_stdio
    server=Mock()
    serve_stdio(server,stdin=io.StringIO(''),stdout=io.StringIO(),recover_existing=False)
    server.integration.dispatcher.start_completion_runtime.assert_not_called()


def test_inflight_start_and_late_receipt_are_one_execution():
    from concurrent.futures import ThreadPoolExecutor
    import threading
    entered, release = threading.Event(), threading.Event()
    calls=[];registry=NodeRegistry(':memory:')
    def start(**request):
        calls.append(request['execution_ref']);entered.set()
        assert release.wait(5)
        return {'execution_ref':request['execution_ref'],'execution_started':True}
    service=NodeService(NodeRecord('air'),registry,scope=SharingScope('test',True,True),read_thread=Mock(),start_execution=start)
    request=dict(operation='execution.start',execution_ref='exec_late',prepared_execution_ref='prepared_late',request_id='late',user_scope='test')
    with ThreadPoolExecutor() as pool:
        pending=pool.submit(service.handle,request);assert entered.wait(5)
        unknown=service.handle(request)
        assert unknown['operation_state']=='UNKNOWN'
        release.set();assert pending.result()['execution_ref']=='exec_late'
    assert service.handle(request)['idempotent'] and calls==['exec_late']


def test_native_unix_socket_reads_split_large_websocket_frame():
    import socket, struct, threading, time
    from app_server import UnixSocketTransport, AppServerTransportError
    client, server = socket.socketpair()
    transport=UnixSocketTransport('/unused')
    transport._byte_transport.sock=client
    body=json.dumps({'id':1,'result':{'models':['model']*5000}}).encode()
    frame=b'\x81\x7e'+struct.pack('!H',len(body))+body
    def send():
        for offset in range(0,len(frame),37):
            server.sendall(frame[offset:offset+37])
            if offset<200:time.sleep(0.005)
        server.close()
    writer=threading.Thread(target=send);writer.start()
    try:
        assert transport.receive(5)==json.loads(body)
        with pytest.raises(AppServerTransportError,match='disconnected'):
            transport._byte_transport.receive_bytes(2,1)
    finally:
        transport.close();writer.join(5)


def test_unstarted_reconciliation_rejects_busy_or_new_turn(native):
    from native_provider import confirm_unstarted_execution
    native.integration.linear=None
    native.cfg=dataclasses.replace(native.cfg,app_server=dataclasses.replace(native.cfg.app_server,native_home=str(native.root)))
    native.integration.cfg=native.dispatcher.cfg=native.reader.cfg=native.cfg
    with patch('thread_identity.ThreadIdentityReader',return_value=native.reader):
        adopted=native.integration.adopt_conversation(thread_id=TID)
    prepared=native.integration.prepare_execution(approved=True,task_ref=adopted['task_ref'],prompt='read identity')
    ref='exec_'+prepared['prepared_execution_ref'].removeprefix('prepared_')
    with native.registry.execution(adopted['task_ref'],None,execution_ref=ref,retain=True,preserve_uncertain=True):
        native.registry.set_execution_state(adopted['task_ref'],'RECOVERY_REQUIRED')
    native.registry.reconcile_terminal(ref,'RECOVERY_REQUIRED')
    import sqlite3
    with sqlite3.connect(native.root/'thread_history_1.sqlite') as c:
        c.execute("UPDATE thread_turns SET status='completed',completed_at=3 WHERE thread_id=?",(TID,))
    observation={'ownership_conflict':False,'observations':[{'state':'active','turn_id':'turn-2','turn_status':'inProgress'}]}
    with patch('native_provider.observe_thread',return_value=observation):
        with pytest.raises(Exception,match='PROVIDER_ABSENCE'):confirm_unstarted_execution(native.cfg,native.registry,ref)
    observation['observations']=[{'state':'idle','turn_id':'new-turn','turn_status':'completed'}]
    with patch('native_provider.observe_thread',return_value=observation):
        with pytest.raises(Exception,match='TURN_CONFLICT'):confirm_unstarted_execution(native.cfg,native.registry,ref)
    observation['observations']=[{'state':'idle','turn_id':'turn-2','turn_status':'completed'}]
    with patch('native_provider.observe_thread',return_value=observation):
        result=native.dispatcher.recover_execution_completion(ref,reconcile_unstarted=True)
    assert result['reconciled'] and not result['replay_performed']
    assert native.registry.get_execution_record(ref)['stage']=='BLOCKED'
    assert not native.registry.has_execution_lease(ref)


@pytest.mark.parametrize('rejected_thread', [TID, 'another-thread'])
def test_resume_writer_rejection_requires_exact_native_identity(rejected_thread):
    from app_server import CodexAppServerClient, AppServerRemoteError, AppServerWriterConflict
    error=AppServerRemoteError('thread/resume', {'code':-32600,
        'message':f'thread {rejected_thread} already has an active writer'})
    client=Mock();client._request.side_effect=error
    expected=AppServerWriterConflict if rejected_thread==TID else AppServerRemoteError
    with pytest.raises(expected) as caught:
        CodexAppServerClient.thread_resume(client,TID)
    assert (getattr(caught.value,'side_effect',None)=='NONE') == (rejected_thread==TID)


def test_native_resume_conflict_finalizes_only_unstarted_attempt(native):
    from app_server import CodexAppServerClient, AppServerRemoteError
    native.integration.linear=None
    with patch('thread_identity.ThreadIdentityReader',return_value=native.reader):
        adopted=native.integration.adopt_conversation(thread_id=TID)
    prep=native.integration.prepare_execution(approved=True,task_ref=adopted['task_ref'],prompt='read identity')
    fake=FakeClient(thread_id=TID,session_id=TID,existing={'id':TID,'sessionId':TID,'cwd':str(native.repo),
        'ephemeral':False,'gitInfo':{'originUrl':'https://example.invalid/pilot.git','branch':'main'},
        'status':{'type':'notLoaded'},'canAcceptDirectInput':True})
    fake._request=Mock(side_effect=AppServerRemoteError('thread/resume',
        {'code':-32600,'message':f'thread {TID} already has an active writer'}))
    fake.thread_resume=lambda tid,**kw: CodexAppServerClient.thread_resume(fake,tid,**kw)
    native.dispatcher.client_factory=lambda target:fake
    service=NodeService(NodeRecord('p620'),NodeRegistry(':memory:'),scope=SharingScope('test',True,True),
        read_thread=Mock(),start_execution=CanonicalNodeExecutionAdapter(native.integration,'p620').start)
    ref='exec_'+prep['prepared_execution_ref'].removeprefix('prepared_')
    request=dict(operation='execution.start',execution_ref=ref,prepared_execution_ref=prep['prepared_execution_ref'],
        approved=True,request_id='writer-conflict',user_scope='test')
    denied=service.handle(request)
    assert denied['error_code']=='NATIVE_ACTIVE_WRITER' and denied['side_effect']=='NONE'
    assert denied['operation_state']=='BLOCKED' and denied['execution_ref']==ref
    assert service.handle(request)['idempotent']
    assert fake._request.call_count==1
    assert not [c for c in fake.calls if c[0] in {'turn/start','thread/start','turn/interrupt'}]
    record=native.registry.get_execution_record(ref)
    assert record['stage']=='BLOCKED' and record['failure_code']=='NATIVE_ACTIVE_WRITER'
    assert not native.registry.has_execution_lease(ref)
    assert native.registry.get_binding(adopted['task_ref']).thread_id==TID
