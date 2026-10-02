"""Isolated Activity contract tests; no production DBs or provider connections."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from observer_server import ActivityReader, APIError, ACTIVITY_LIMIT, ACTIVITY_TEXT_LIMIT
import test_observer_server as fixtures

THREAD = '00000000-0000-0000-0000-000000000001'
TURN = '00000000-0000-0000-0000-000000000002'
ROUTE = {'host': {'stable_identifier': 'p620'}, 'provider': {'stable_identifier': 'codex_app_server'},
         'conversation': {'binding': THREAD}}
EXEC = {'execution_ref': 'exec_fixture', 'turn_id': TURN, 'routing_identity_json': json.dumps(ROUTE)}


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        with sqlite3.connect(self.root / 'state_5.sqlite') as c:
            c.execute('CREATE TABLE threads(id TEXT PRIMARY KEY, history_mode TEXT)')
            c.execute('INSERT INTO threads VALUES (?,?)', (THREAD, 'paginated'))
        self.path = self.root / 'thread_history_1.sqlite'
        with sqlite3.connect(self.path) as c:
            c.executescript('''CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT);
                CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,rollout_ordinal INT,
                updated_at_ordinal INT,created_at_ms INT,item_type TEXT,item_json TEXT);
                CREATE INDEX items_page ON thread_items(thread_id,turn_id,rollout_ordinal);
                CREATE INDEX items_update ON thread_items(thread_id,turn_id,updated_at_ordinal);''')
            c.execute('INSERT INTO thread_turns VALUES (?,?)', (THREAD, TURN))
        self.reader = ActivityReader(self.root)

    def add(self, ordinal, text=None, phase='commentary', channel=None, kind='agentMessage', thread=THREAD, turn=TURN):
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO thread_items VALUES (?,?,?,?,?,?,?,?)',
                      (thread, turn, f'item{ordinal}', ordinal, ordinal, 1000 * ordinal, kind,
                       json.dumps(dict(text=text or f'Feedback {ordinal}', phase=phase, channel=channel))))

    def read(self, **kwargs):
        return self.reader.read('task_fixture', EXEC, **kwargs)

    def test_latest_older_and_delta_include_revision_without_duplicates(self):
        for i in range(1, 92): self.add(i)
        first = self.read()
        self.assertEqual([i['ordinal'] for i in first['items']], list(range(52, 92)))
        older = self.read(before=first['older_cursor'])
        self.assertEqual([i['ordinal'] for i in older['items']], list(range(12, 52)))
        last = self.read(before=older['older_cursor'])
        self.assertEqual(len(last['items']), 11)
        self.assertIsNone(last['older_cursor'])
        self.assertEqual(self.read(after=first['next_cursor'])['items'], [])
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE thread_items SET item_json=?,updated_at_ordinal=92 WHERE item_id='item91'",
                      (json.dumps(dict(text='Revised', phase='commentary')),))
        self.add(93)
        delta = self.read(after=first['next_cursor'])
        self.assertEqual([i['id'] for i in delta['items']], ['item91', 'item93'])
        self.assertEqual(delta['items'][0]['text'], 'Revised')
        for i in range(94, 144): self.add(i)
        delta2 = self.read(after=delta['next_cursor'])
        self.assertTrue(delta2['has_more'])
        self.assertEqual(len(self.read(after=delta2['next_cursor'])['items']), 10)

    def test_exact_scope_and_cursor_validation(self):
        self.add(1)
        self.add(2, 'wrong turn', turn='other')
        self.add(3, 'wrong thread', thread='other')
        page = self.read()
        self.assertEqual(len(page['items']), 1)
        for kwargs in [dict(after='garbage'), dict(before=page['next_cursor'])]:
            with self.assertRaises(APIError): self.read(**kwargs)
        for execution in [dict(EXEC, execution_ref='exec_other'), dict(EXEC, turn_id=None)]:
            if execution['turn_id']:
                with self.assertRaises(APIError):
                    self.reader.read('task_fixture', execution, after=page['next_cursor'])
            else:
                self.assertEqual(self.reader.read('task_fixture', execution)['reason'], 'EXACT_TURN_UNAVAILABLE')
        with sqlite3.connect(self.path) as c: c.execute('DELETE FROM thread_items')
        with self.assertRaises(APIError): self.read(after=page['next_cursor'])

    def test_display_allowlist_redaction_bounds_and_read_only(self):
        self.add(1, 'visible')
        self.add(2, 'PRIVATE_REASONING', phase='analysis')
        self.add(3, 'PRIVATE_REASONING', channel='analysis')
        self.add(4, 'RAW_TOOL_OUTPUT', kind='dynamicToolCall')
        self.add(5, 'secret=NEVER_SHOW Bearer BEARER_SECRET sk-abcdefghijklmnopqrstuvwxyz '+ '中' * 20000)
        self.add(6, '-----BEGIN PRIVATE KEY-----\nPRIVATE_CONTENT' + 'x' * 20000)
        self.add(7, 'Done', phase='final_answer')
        hashes = [p.read_bytes() for p in [self.root / 'state_5.sqlite', self.path]]
        page = self.read()
        from jsonschema import Draft202012Validator
        Draft202012Validator(json.loads(Path("docs/monitor/observer-v1.schema.json").read_text())).validate(page)
        encoded = json.dumps(page)
        for value in ['PRIVATE_REASONING', 'RAW_TOOL_OUTPUT', 'NEVER_SHOW', 'BEARER_SECRET',
                      'sk-abcdefghijklmnopqrstuvwxyz', 'PRIVATE_CONTENT']:
            self.assertNotIn(value, encoded)
        self.assertTrue(any(i['truncated'] for i in page['items']))
        self.assertTrue(all(len(i['text'].encode()) <= ACTIVITY_TEXT_LIMIT for i in page['items']))
        self.assertEqual(page['items'][-1]['kind'], 'result')
        self.assertEqual(hashes, [p.read_bytes() for p in [self.root / 'state_5.sqlite', self.path]])

    def test_unavailable_does_not_fallback_to_other_history(self):
        wrong_host = dict(ROUTE, host={'stable_identifier': 'another-host'})
        self.assertEqual(self.reader.read('task_fixture', dict(EXEC, routing_identity_json=json.dumps(wrong_host)))['reason'],
                         'NATIVE_HOST_UNAVAILABLE')
        with sqlite3.connect(self.root / 'state_5.sqlite') as c:
            c.execute("UPDATE threads SET history_mode='legacy'")
        self.assertEqual(self.read()['reason'], 'NATIVE_HISTORY_MODE_UNSUPPORTED')
        self.path.unlink()
        with sqlite3.connect(self.root / 'state_5.sqlite') as c:
            c.execute("UPDATE threads SET history_mode='paginated'")
        self.assertEqual(self.read()['availability'], 'UNAVAILABLE')
        self.assertFalse(self.path.exists())


class ActivityAPITests(unittest.TestCase):
    setUp = fixtures.ObserverTests.setUp
    claim = fixtures.ObserverTests.claim
    request = fixtures.ObserverTests.request
    sql = fixtures.ObserverTests.sql

    def test_activity_endpoint_execution_and_query_guards(self):
        self.claim()
        endpoint = '/v1/tasks/' + self.ref + '/activity'
        for suffix in ['', '?execution_ref=x', '?execution_ref=exec_fixture&after=a&before=b', '?execution_ref=exec_fixture&before=']:
            with self.assertRaises(APIError): self.request(endpoint + suffix)
        page = self.request(endpoint + '?execution_ref=exec_fixture')
        self.assertEqual(page['reason'], 'EXACT_TURN_UNAVAILABLE')
        self.assertEqual(page['items'], [])
        with self.assertRaises(APIError) as error:
            self.request(endpoint + '?execution_ref=exec_fixture', auth='Bearer wrong')
        self.assertEqual(error.exception.status, 401)
