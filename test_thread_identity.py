import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import bridge
from m9_integration import ClinxIntegration
from mcp_server import ClinxMCPServer, tool_definitions
from task_registry import TaskRegistry
from test_m6 import dispatcher_fixture
from thread_identity import ThreadIdentityReader, INDEX_NAMES, ROUTE_THREAD, readonly

A = '01a0a673-d5d6-7761-b9bf-97db5bc7e50c'
B = '01a0a673-d5d6-7761-b9bf-97db5bc7e50d'
C = '01a0a673-d5d6-7761-b9bf-97db5bc7e50e'


class ThreadIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dispatcher, self.repo = dispatcher_fixture(self.root, self.root/'tasks.db', [])
        self.cfg = self.dispatcher.cfg
        self.registry = self.dispatcher.tasks
        self.task = self.registry.create_task(host='p620', workspace_alias='p620', project_alias='pilot', project_name='Pilot', cwd=str(self.root/'pilot'), repository_origin='https://example.invalid/pilot.git', branch='main', title='thread fixture')
        self.registry.bind_conversation(task_id=self.task.task_id, thread_id=A, session_id=A, project_id=None, app_server_version='0.152.1')
        self.context = bridge.TaskContextReader(self.cfg, self.registry, client_factory=Mock(side_effect=AssertionError('provider connection forbidden')))
        self.reader = ThreadIdentityReader(self.cfg, self.registry.path, self.context, native_root=self.root/'native')
        self.integration = ClinxIntegration(self.cfg, self.registry, self.dispatcher, self.context, linear=Mock(side_effect=AssertionError('Linear forbidden')))

    def sql(self, sql, args=()):
        with sqlite3.connect(self.registry.path) as c:
            return c.execute(sql, args).fetchall()

    def execution(self, tid, ref, state='COMPLETED', at='2026-10-01T00:00:00+00:00', active=False):
        route = json.dumps({'conversation': {'binding':tid}, 'provider': {'stable_identifier':'codex_app_server'}, 'host': {'stable_identifier':'p620'}})
        table = 'executions' if active else 'execution_history'
        cols = 'task_id,execution_ref,stage,execution_state,turn_id,acquired_at,routing_identity_json'
        vals = [self.task.task_id, ref, state, state, 'turn-'+ref, at, route]
        if not active:
            cols += ',released_at'; vals.append(at)
        self.sql('INSERT INTO '+table+' ('+cols+') VALUES ('+','.join('?' for _ in vals)+')', vals)

    def checkpoint(self, tid, text='A context', ref='exec-a'):
        return self.registry.save_context_checkpoint(task_id=self.task.task_id, execution_id=ref, thread_id=tid, turn_id='turn-'+ref, prompt_summary='user', result_summary=text, changed_files='', validation_summary='', blockers='', next_state='COMPLETED', source='test', provenance='fixture')

    def migrate(self):
        self.registry.migrate_conversation_binding(task_id=self.task.task_id, successor_thread=B, successor_session_id=B, successor_project_id=None, successor_app_server_version='0.152.1', reason='DYNAMIC_TOOL_SCHEMA_UPGRADE')

    def native(self, tid=A, cwd=None, header_id=None, turns=2):
        root = self.root/'native'; root.mkdir(exist_ok=True)
        path = root/('rollout-2026-10-01-'+tid+'.jsonl')
        cwd = str(cwd or self.root/'pilot')
        records = [dict(type='session_meta', payload={'id':header_id or tid, 'cwd':cwd, 'instructions':'x'*22000})]
        for i in range(turns):
            records += [dict(type='turn_context',payload={'turn_id':'turn-'+str(i)}), dict(type='response_item',payload={'type':'message','role':'user','content':[{'type':'input_text','text':'prompt '+str(i)}]}), dict(type='response_item',payload={'type':'message','role':'assistant','content':[{'type':'output_text','text':'answer '+str(i)}]})]
        path.write_text('\n'.join(json.dumps(r) for r in records)+'\n')
        with sqlite3.connect(root/'state_5.sqlite') as c:
            c.execute('CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY,cwd TEXT,rollout_path TEXT)')
            c.execute('INSERT OR REPLACE INTO threads VALUES(?,?,?)',(tid,cwd,str(path)))

    def test_current_active_and_uri_equivalence(self):
        self.execution(A,'exec-a','CODEX_RUNNING',active=True);self.checkpoint(A)
        a=self.reader.read(thread_id=A,context=True); b=self.reader.read(codex_uri='codex://threads/'+A,context=True)
        for key in a.keys()-{'observed_at'}: self.assertEqual(a[key],b[key])
        self.assertEqual(a['execution_state'],'CODEX_RUNNING');self.assertEqual(a['last_codex_result'],'A context')
        self.assertEqual(a['provider_observation']['state'],'UNKNOWN')

    def test_historical_status_context_and_successor_isolation(self):
        self.execution(A,'exec-a','FAILED');self.checkpoint(A);self.migrate()
        self.execution(B,'exec-b','CODEX_RUNNING',active=True);self.checkpoint(B,'B context','exec-b')
        a=self.reader.read(thread_id=A,context=True);b=self.reader.read(thread_id=B,context=True)
        self.assertEqual(a['execution_state'],'FAILED');self.assertEqual(a['last_codex_result'],'A context')
        self.assertFalse(a['is_current_thread']);self.assertEqual(a['successor_thread_id'],B)
        self.assertEqual(b['execution_state'],'CODEX_RUNNING');self.assertEqual(b['last_codex_result'],'B context')

    def test_wrong_thread_fallback_rejected(self):
        self.migrate();self.checkpoint(B,'never A')
        a=self.reader.read(thread_id=A,context=True)
        self.assertEqual(a['context_status'],'CONTEXT_UNAVAILABLE');self.assertNotIn('never A',json.dumps(a))

    def test_multi_execution_selection(self):
        self.execution(A,'old','FAILED');self.execution(A,'latest','COMPLETED','2026-10-01T01:00:00+00:00')
        self.assertEqual(self.reader.read(thread_id=A)['execution_ref'],'latest')
        self.assertEqual(self.reader.read(thread_id=A,execution_ref='old')['execution_state'],'FAILED')
        self.execution(A,'active','CODEX_RUNNING',active=True)
        self.assertEqual(self.reader.read(thread_id=A)['selection_reason'],'UNIQUE_ACTIVE_EXECUTION')
        self.assertEqual(self.reader.read(thread_id=A,execution_ref='other')['error_code'],'THREAD_SCOPE_MISMATCH')

    def test_terminal_states(self):
        for i,state in enumerate(('COMPLETED','FAILED','BLOCKED','CANCELLED')):
            self.execution(A,str(i),state)
            self.assertEqual(self.reader.read(thread_id=A,execution_ref=str(i))['execution_state'],state)

    def test_equal_order_fails_closed(self):
        self.execution(A,'one');self.execution(A,'two')
        self.assertEqual(self.reader.read(thread_id=A)['error_code'],'THREAD_IDENTITY_CONFLICT')

    def test_invalid_inputs(self):
        for uri in ('', ' '+ 'codex://threads/'+A,'codex://threads/'+A+'/', 'codex://threads/'+A+'?x=1','codex://threads/'+A+'#f','codex://u@threads/'+A,'codex://threads:80/'+A,'codex://threads/%30'+A[1:],'codex://threads/'+A+'\n','CODEx://threads/'+A):
            with self.subTest(uri=uri): self.assertEqual(self.reader.read(codex_uri=uri)['error_code'],'INVALID_CODEX_THREAD_URI')
        for tid in ('', 'please '+A, 'abc', A+'\n'):
            self.assertEqual(self.reader.read(thread_id=tid)['error_code'],'INVALID_THREAD_ID')
        for kwargs in ({'codex_uri':'codex://threads/'+A},{'task_ref':self.task.task_id},{'query':A}):
            self.assertEqual(self.reader.read(thread_id=A,**kwargs)['error_code'],'IDENTITY_SELECTOR_CONFLICT')

    def test_scope(self):
        self.assertEqual(self.reader.read(thread_id=A,project='other')['error_code'],'THREAD_SCOPE_MISMATCH')
        self.assertEqual(self.reader.read(thread_id=A,host='other')['error_code'],'UNKNOWN_THREAD_HOST')
        self.sql('UPDATE tasks SET cwd=?',(str(self.root/'outside'),))
        self.assertEqual(self.reader.read(thread_id=A)['lookup_status'],'RESOLVED')

    def test_unknown_unbound_and_unavailable(self):
        r=self.reader.read(thread_id=C)
        self.assertEqual(r['lookup_status'],'NATIVE_INDEX_UNAVAILABLE')
        self.native(C)
        self.assertEqual(self.reader.read(thread_id=C,context=True)['lookup_status'],'THREAD_UNBOUND')
        self.assertEqual(self.reader.read(thread_id=A,context=True)['context_status'],'CONTEXT_UNAVAILABLE')

    def native_turn_status(self, status_rows, *, live_reader=None):
        """Build a bounded native-only fixture with no CLINX task binding."""
        root = self.root / 'native-terminal'
        root.mkdir(exist_ok=True)
        rollout = root / ('rollout-terminal-' + C + '.jsonl')
        rollout.write_text(json.dumps({
            'type': 'session_meta', 'payload': {'id': C, 'cwd': str(self.root / 'pilot')}
        }) + '\n')
        with sqlite3.connect(root / 'state_5.sqlite') as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY,cwd TEXT,rollout_path TEXT,history_mode TEXT)')
            conn.execute('DELETE FROM threads')
            conn.execute('INSERT INTO threads VALUES(?,?,?,?)', (C, str(self.root / 'pilot'), str(rollout), 'paginated'))
        with sqlite3.connect(root / 'thread_history_1.sqlite') as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS thread_turns(thread_id TEXT,turn_id TEXT,rollout_ordinal INT,status TEXT,started_at INT,completed_at INT,PRIMARY KEY(thread_id,turn_id))')
            conn.execute('DELETE FROM thread_turns')
            for row in status_rows:
                conn.execute('INSERT INTO thread_turns VALUES(?,?,?,?,?,?)', row)
        return ThreadIdentityReader(self.cfg, self.registry.path, self.context,
                                    native_root=root, live_reader=live_reader)

    def test_unbound_native_completed_turn_is_terminal_for_display(self):
        reader = self.native_turn_status([
            (C, 'turn-complete', 1, 'completed', 100, 101),
        ], live_reader=lambda _cfg, _tid: {
            'state': 'UNKNOWN', 'owner_endpoint': None, 'ownership_conflict': False,
            'observations': [{'state': 'notLoaded', 'turn_status': 'completed'}],
        })
        result = reader.read(thread_id=C)
        self.assertEqual(result['lookup_status'], 'THREAD_UNBOUND')
        self.assertIsNone(result['task_ref'])
        self.assertEqual(result['execution_state'], 'UNKNOWN')
        self.assertEqual(result['native_status']['status'], 'completed')
        self.assertEqual(result['native_status']['state'], 'COMPLETED')
        self.assertTrue(result['native_status']['terminal'])
        self.assertEqual(result['native_turn_state'], 'COMPLETED')
        self.assertEqual(result['native_display_state'], 'COMPLETED')
        self.assertEqual(result['native_status_source'], 'NATIVE_PERSISTED_HISTORY')
        self.assertFalse(result['codex_running'])

    def test_native_latest_turn_wins_and_old_completed_does_not_hide_active(self):
        reader = self.native_turn_status([
            (C, 'turn-old', 1, 'completed', 100, 101),
            (C, 'turn-new', 2, 'inProgress', 200, None),
        ])
        result = reader.read(thread_id=C)
        self.assertEqual(result['native_status']['turn_id'], 'turn-new')
        self.assertEqual(result['native_display_state'], 'RUNNING')
        self.assertEqual(result['native_status']['state'], 'RUNNING')

    def test_native_live_active_turn_overrides_persisted_terminal_only_when_proved(self):
        reader = self.native_turn_status([
            (C, 'turn-complete', 1, 'completed', 100, 101),
        ], live_reader=lambda _cfg, _tid: {
            'state': 'active', 'owner_endpoint': 'fixture-owner', 'ownership_conflict': False,
            'observations': [{'state': 'active', 'turn_id': 'turn-new', 'turn_status': 'inProgress'}],
        })
        result = reader.read(thread_id=C)
        self.assertEqual(result['native_status']['state'], 'COMPLETED')
        self.assertEqual(result['native_display_state'], 'RUNNING')

    def test_native_owner_conflict_does_not_guess_over_terminal_history(self):
        reader = self.native_turn_status([
            (C, 'turn-complete', 1, 'completed', 100, 101),
        ], live_reader=lambda _cfg, _tid: {
            'state': 'UNKNOWN', 'owner_endpoint': None, 'ownership_conflict': True,
            'observations': [
                {'state': 'active', 'turn_status': 'inProgress'},
                {'state': 'active', 'turn_status': 'inProgress'},
            ],
        })
        result = reader.read(thread_id=C)
        self.assertEqual(result['native_status']['state'], 'COMPLETED')
        self.assertEqual(result['native_display_state'], 'UNKNOWN')

    def test_native_status_missing_history_is_explicitly_unknown(self):
        root = self.root / 'native-terminal-missing'
        root.mkdir(exist_ok=True)
        rollout = root / ('rollout-terminal-' + C + '.jsonl')
        rollout.write_text(json.dumps({
            'type': 'session_meta', 'payload': {'id': C, 'cwd': str(self.root / 'pilot')}
        }) + '\n')
        with sqlite3.connect(root / 'state_5.sqlite') as conn:
            conn.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT,rollout_path TEXT,history_mode TEXT)')
            conn.execute('INSERT INTO threads VALUES(?,?,?,?)', (C, str(self.root / 'pilot'), str(rollout), 'paginated'))
        reader = ThreadIdentityReader(self.cfg, self.registry.path, self.context,
                                      native_root=root, live_reader=lambda _cfg, _tid: {
                                          'state': 'notLoaded', 'owner_endpoint': None,
                                          'ownership_conflict': False, 'observations': [],
                                      })
        result = reader.read(thread_id=C)
        self.assertEqual(result['native_status']['state'], 'UNKNOWN')
        self.assertFalse(result['native_status']['terminal'])
        self.assertEqual(result['native_display_state'], 'UNKNOWN')

    def test_native_terminal_provider_states_keep_actual_mapping(self):
        for status, expected in (('failed', 'FAILED'), ('interrupted', 'INTERRUPTED'),
                                 ('cancelled', 'CANCELLED')):
            with self.subTest(status=status):
                reader = self.native_turn_status([
                    (C, 'turn-terminal', 1, status, 100, 101),
                ])
                result = reader.read(thread_id=C)
                self.assertEqual(result['native_status']['state'], expected)
                self.assertEqual(result['native_display_state'], expected)

    def test_local_header_and_scope(self):
        self.native(header_id=B)
        self.assertEqual(self.reader.read(thread_id=A,context=True)['context_status'],'CONTEXT_UNAVAILABLE')
        self.native(cwd=self.root/'elsewhere')
        self.assertEqual(self.reader.read(thread_id=A,context=True)['error_code'],'THREAD_IDENTITY_CONFLICT')

    def test_local_recent_turns_and_anchor(self):
        self.native(turns=4)
        r=self.reader.read(thread_id=A,context=True,recent_turns=1)
        self.assertEqual(r['provenance']['context_turn_refs'],['turn-3']);self.assertEqual(r['last_codex_result'],'answer 3')
        self.registry.set_context_anchor(task_id=self.task.task_id,thread_id=A,turn_id='turn-1')
        r=self.reader.read(thread_id=A,context=True)
        self.assertEqual(r['last_codex_result'],'answer 1')

    def test_read_only_no_provider_no_linear_no_fuzzy(self):
        self.checkpoint(A)
        before=self.sql('SELECT * FROM conversation_bindings')
        with patch('thread_identity.ThreadIdentityReader', return_value=self.reader), patch.object(self.registry,'find_tasks',side_effect=AssertionError('fuzzy')), patch.object(self.registry,'reclaim_stale_worktree_leases',side_effect=AssertionError('lease')), patch.object(self.dispatcher,'reconcile_execution',side_effect=AssertionError('reconcile')):
            self.assertEqual(self.integration.get_context(thread_id=A)['context_status'],'AVAILABLE')
            self.assertEqual(self.integration.get_status(thread_id=A)['lookup_status'],'RESOLVED')
        self.assertEqual(before,self.sql('SELECT * FROM conversation_bindings'))
        self.context.client_factory.assert_not_called()
        with readonly(self.registry.path) as c:
            with self.assertRaises(sqlite3.OperationalError):c.execute('DELETE FROM tasks')

    def test_migration_during_context_returns_unavailable(self):
        self.checkpoint(A)
        original=self.reader._local_context
        def migrate(*a,**kw):self.migrate();return original(*a,**kw)
        with patch.object(self.reader,'_local_context',side_effect=migrate):
            result=self.reader.read(thread_id=A,context=True)
        self.assertEqual(result['unavailable_reason'],'THREAD_IDENTITY_CHANGED_DURING_READ');self.assertNotIn('last_codex_result',result)

    def test_mcp_schema_and_calls(self):
        import jsonschema
        self.checkpoint(A)
        server=ClinxMCPServer(self.integration)
        for tool in tool_definitions():
            if tool['name'] not in ('clinx_get_context','clinx_get_status'):continue
            for selector in ({'thread_id':A},{'codex_uri':'codex://threads/'+A},{'task_ref':self.task.task_id},{'project':'pilot','query':'fixture'}):jsonschema.validate(selector,tool['inputSchema'])
            if tool['name']=='clinx_get_status':jsonschema.validate({'execution_ref':'exec'},tool['inputSchema'])
            with self.assertRaises(jsonschema.ValidationError):jsonschema.validate({'thread_id':A,'query':'x'},tool['inputSchema'])
            for args in ({'thread_id':A},{'codex_uri':'codex://threads/'+A}):
                with patch('thread_identity.ThreadIdentityReader', return_value=self.reader):
                    r=server.handle({'id':1,'method':'tools/call','params':{'name':tool['name'],'arguments':args}})['result']
                self.assertFalse(r['isError']);jsonschema.validate(r['structuredContent'],tool['outputSchema'])
                self.assertEqual(r['structuredContent']['queried_thread_id'],A)

    def test_indexed_plans_and_large_unrelated_history(self):
        with sqlite3.connect(self.registry.path) as c:
            c.executemany('INSERT INTO execution_history(execution_ref,task_id,stage,acquired_at,released_at,routing_identity_json) VALUES(?,?,?,?,?,?)',[(str(i),self.task.task_id,'COMPLETED','2026-10-01','2026-10-01',json.dumps({'conversation':{'binding':'unrelated-'+str(i)}})) for i in range(10000)])
            for table in ('executions','execution_history'):
                plan=c.execute('EXPLAIN QUERY PLAN SELECT * FROM '+table+' WHERE '+ROUTE_THREAD+'=? LIMIT 257',(A,)).fetchall()
                self.assertTrue(any('SEARCH' in r[3] and 'INDEX' in r[3] for r in plan),plan)
        self.assertEqual(self.reader.read(thread_id=A)['lookup_status'],'RESOLVED')

    def test_missing_migration_is_unavailable_without_writes(self):
        self.sql('DROP INDEX '+INDEX_NAMES[0])
        r=self.reader.read(thread_id=A)
        self.assertEqual(r['error_code'],'THREAD_LOOKUP_UNAVAILABLE')
        self.assertFalse(self.sql('SELECT name FROM sqlite_master WHERE name=?',(INDEX_NAMES[0],)))

    def test_adopted_source_is_independent_of_lifecycle(self):
        self.sql('INSERT INTO conversation_adoptions VALUES(?,?,?,?,?,?,?)',(self.task.task_id,A,'CODEX_LOCAL_SESSION','2026-10-01','COMPLETED','fixture','exact'))
        self.execution(A,'failed','FAILED')
        r=self.reader.read(thread_id=A)
        self.assertEqual(r['adoption_source'],'CODEX_LOCAL_SESSION');self.assertEqual(r['execution_state'],'FAILED')

    def test_provider_conflict(self):
        self.execution(A,'first')
        self.execution(A,'second',at='2026-10-01T01:00:00+00:00')
        self.sql("UPDATE execution_history SET routing_identity_json=json_set(routing_identity_json,'$.provider.stable_identifier','other') WHERE execution_ref='second'")
        self.assertEqual(self.reader.read(thread_id=A)['error_code'],'THREAD_IDENTITY_CONFLICT')

    def test_legacy_missing_execution_relation_not_inferred(self):
        self.execution(B,'unrelated','FAILED')
        r=self.reader.read(thread_id=A)
        self.assertIsNone(r['execution_ref']);self.assertEqual(r['execution_state'],'UNKNOWN')

    def test_checkpoint_explicit_execution_cannot_borrow(self):
        self.execution(A,'one');self.execution(A,'two',at='2026-10-01T01:00:00+00:00')
        self.checkpoint(A,'only one','one')
        self.assertEqual(self.reader.read(thread_id=A,execution_ref='two',context=True)['context_status'],'CONTEXT_UNAVAILABLE')

    def test_output_budget_and_no_tool_payloads(self):
        self.native(turns=30)
        r=self.reader.read(thread_id=A,context=True,recent_turns=2,max_bytes=1024)
        self.assertLessEqual(r['provenance']['source_bytes'],512*1024)
        self.assertLessEqual(len(((r.get('last_user_intent') or '')+(r.get('last_codex_result') or '')).encode()),1024)
        self.assertLessEqual(len(r['provenance']['context_turn_refs']),2)
        self.assertTrue(r['context_truncated'])

    def test_all_relation_queries_use_indexes(self):
        queries = [
            ('conversation_bindings','thread_id'),('conversation_binding_lineage','predecessor_thread'),
            ('conversation_binding_lineage','successor_thread'),('conversation_adoptions','thread_id'),
            ('prepared_executions','resulting_thread_id'),('context_checkpoints','thread_id'),('context_anchors','thread_id')]
        with readonly(self.registry.path) as c:
            for table,col in queries:
                plan=c.execute('EXPLAIN QUERY PLAN SELECT * FROM '+table+' WHERE '+col+'=? LIMIT 257',(A,)).fetchall()
                self.assertTrue(any('SEARCH' in r[3] and 'INDEX' in r[3] for r in plan),[tuple(r) for r in plan])

    def test_repeated_migration_is_compatible(self):
        before=self.sql('SELECT * FROM conversation_bindings')
        TaskRegistry(self.registry.path)
        self.assertEqual(before,self.sql('SELECT * FROM conversation_bindings'))
        self.assertEqual(self.reader.read(thread_id=A)['lookup_status'],'RESOLVED')
