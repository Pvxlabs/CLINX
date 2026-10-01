"""Exact, read-only Thread identity over existing CLINX and Codex records.

No TaskRegistry construction, reconciliation, provider startup or discovery.
Indexes are installed by the normal registry initialization migration, never here.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from task_registry import TERMINAL_EXECUTION_STAGES

ID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
ROUTE_THREAD = "json_extract(routing_identity_json, '$.conversation.binding')"
INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_thread_lineage_predecessor ON conversation_binding_lineage(predecessor_thread)",
    f"CREATE INDEX IF NOT EXISTS idx_thread_execution ON executions({ROUTE_THREAD})",
    f"CREATE INDEX IF NOT EXISTS idx_thread_history ON execution_history({ROUTE_THREAD}, acquired_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_thread_prepared ON prepared_executions(resulting_thread_id)",
    "CREATE INDEX IF NOT EXISTS idx_thread_checkpoint ON context_checkpoints(thread_id, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_thread_anchor ON context_anchors(thread_id)",
)
INDEX_NAMES = tuple(sql.split()[5] for sql in INDEX_SQL)


class ThreadLookupError(ValueError):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code, self.reason = code, reason


def parse_selector(*, thread_id=None, codex_uri=None, task_ref=None, query=None):
    if (thread_id is not None and codex_uri is not None) or task_ref is not None or query is not None:
        raise ThreadLookupError("IDENTITY_SELECTOR_CONFLICT", "Thread selector cannot be combined with task_ref/query or another thread selector")
    if codex_uri is not None:
        if not isinstance(codex_uri, str) or not re.fullmatch("codex://threads/" + ID_PATTERN, codex_uri):
            raise ThreadLookupError("INVALID_CODEX_THREAD_URI", "Expected exactly codex://threads/<thread ID>")
        return codex_uri[len("codex://threads/"):]
    if not isinstance(thread_id, str) or not re.fullmatch(ID_PATTERN, thread_id):
        raise ThreadLookupError("INVALID_THREAD_ID", "Expected a canonical hyphenated Codex UUID (not restricted to v4)")
    return thread_id


@contextlib.contextmanager
def readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def rows(conn, sql, args):
    result = [dict(r) for r in conn.execute(sql + " LIMIT 257", args)]
    if len(result) > 256:
        raise ThreadLookupError("THREAD_LOOKUP_UNAVAILABLE", "Exact thread relation exceeds bounded lookup limit (256)")
    return result


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


class ThreadIdentityReader:
    def __init__(self, cfg, registry_path, context_reader, *, native_root=None):
        self.cfg, self.path, self.reader = cfg, Path(registry_path), context_reader
        self.native_root = Path(native_root or Path.home() / ".codex")

    def _scope(self, task, host=None, project=None):
        from execution_semantics import normalize_host
        actual_host = normalize_host(task['host']).stable_identifier
        if host is not None and normalize_host(host).stable_identifier != actual_host:
            raise ThreadLookupError("THREAD_SCOPE_MISMATCH", "Host does not match the exact thread")
        if project is not None and project.casefold() not in {task['project_alias'].casefold(), task['project_name'].casefold()}:
            raise ThreadLookupError("THREAD_SCOPE_MISMATCH", "Project does not match the exact thread")
        ws = next((w for w in self.cfg.workspaces if w.alias == task['workspace_alias'] and normalize_host(w.host or w.alias).stable_identifier == actual_host), None)
        if ws is None:
            raise ThreadLookupError("THREAD_ACCESS_DENIED", "Workspace is not registered for this host")
        cwd = Path(task['cwd']).resolve()
        mapping = next((m for m in self.cfg.projects if m.project_alias == task['project_alias'] and m.workspace_alias == ws.alias), None)
        if mapping is not None:
            allowed = cwd == Path(mapping.repo).resolve()
        else:
            allowed = ws.allow_existing_projects and cwd.parent == Path(ws.root).resolve() and cwd.name == task['project_alias']
        if not allowed:
            raise ThreadLookupError("THREAD_ACCESS_DENIED", "Persisted workspace is outside the current project scope")
        return actual_host

    def _snapshot(self, tid, host, project, execution_ref):
        with readonly(self.path) as c:
            indexes = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            if not set(INDEX_NAMES) <= indexes:
                raise ThreadLookupError("THREAD_LOOKUP_UNAVAILABLE", "Thread lookup indexes require the normal startup migration")
            facts = {}
            for name, sql, args in (
                ('bindings', 'SELECT * FROM conversation_bindings WHERE thread_id=?', (tid,)),
                ('predecessors', 'SELECT * FROM conversation_binding_lineage WHERE predecessor_thread=?', (tid,)),
                ('successors', 'SELECT * FROM conversation_binding_lineage WHERE successor_thread=?', (tid,)),
                ('adoptions', 'SELECT * FROM conversation_adoptions WHERE thread_id=?', (tid,)),
                ('active', f'SELECT * FROM executions WHERE {ROUTE_THREAD}=?', (tid,)),
                ('history', f'SELECT * FROM execution_history WHERE {ROUTE_THREAD}=?', (tid,)),
                ('prepared', 'SELECT * FROM prepared_executions WHERE resulting_thread_id=?', (tid,)),
                ('checkpoints', 'SELECT * FROM context_checkpoints WHERE thread_id=? ORDER BY timestamp DESC', (tid,)),
                ('anchors', 'SELECT * FROM context_anchors WHERE thread_id=?', (tid,)),
            ):
                facts[name] = rows(c, sql, args)
            ids = {r.get('task_id') or r.get('resulting_task_id') for group in facts.values() for r in group}
            ids.discard(None)
            if not ids:
                return None
            tasks = [dict(c.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone() or {}) for task_id in ids]
            # Authorize before returning any identity or association facts.
            for task in tasks:
                if not task:
                    raise ThreadLookupError('THREAD_IDENTITY_CONFLICT', 'Relation references a missing task')
                self._scope(task, host, project)
            if len(tasks) != 1:
                raise ThreadLookupError('THREAD_IDENTITY_CONFLICT', 'Exact thread has multiple authorized task owners')
            task = tasks[0]
            current = c.execute('SELECT * FROM conversation_bindings WHERE task_id=?', (task['task_id'],)).fetchone()
            current = dict(current) if current else None
            lineage = c.execute('SELECT * FROM conversation_binding_lineage WHERE task_id=?', (task['task_id'],)).fetchone()
            lineage = dict(lineage) if lineage else None
            executions = {r['execution_ref']: dict(r, association_source='EXECUTION_ROUTE', active_record=True) for r in facts['active'] if r['execution_ref']}
            for r in facts['history']:
                ref = r['execution_ref']
                if ref in executions:
                    raise ThreadLookupError('THREAD_IDENTITY_CONFLICT', 'Execution exists in both active and historical storage')
                executions[ref] = dict(r, association_source='EXECUTION_ROUTE', active_record=False)
            # Prepared dispatch records provide exact associations for legacy route gaps.
            for p in facts['prepared']:
                ref = p['resulting_execution_ref']
                if not ref or ref in executions:
                    continue
                active = c.execute('SELECT * FROM executions WHERE execution_ref=?', (ref,)).fetchone()
                hist = c.execute('SELECT * FROM execution_history WHERE execution_ref=?', (ref,)).fetchone()
                record = active or hist
                if record:
                    r = dict(record)
                    bound = json.loads(r['routing_identity_json']).get('conversation', {}).get('binding')
                    if r['task_id'] != task['task_id'] or bound not in (None, 'UNBOUND', 'UNKNOWN', 'BOUND', tid):
                        raise ThreadLookupError('THREAD_IDENTITY_CONFLICT', 'Prepared and execution identities disagree')
                    executions[ref] = dict(r, association_source='PREPARED_DISPATCH', active_record=active is not None)
            providers = set()
            for r in executions.values():
                route = json.loads(r['routing_identity_json'])
                provider = route.get('provider', {}).get('stable_identifier')
                if provider:
                    providers.add(provider)
                route_host = route.get('host', {}).get('stable_identifier')
                if route_host and route_host != self._scope(task, host, project):
                    raise ThreadLookupError('THREAD_IDENTITY_CONFLICT', 'Execution host disagrees with task owner')
            if len(providers) > 1 or providers - {'codex_app_server'}:
                raise ThreadLookupError('THREAD_IDENTITY_CONFLICT', 'Thread provider identity cannot be uniquely resolved')
            selected, reason = self._select(list(executions.values()), execution_ref)
            result = None
            if selected:
                result = c.execute('SELECT * FROM execution_results WHERE execution_ref=? AND task_id=?', (selected['execution_ref'], task['task_id'])).fetchone()
                result = dict(result) if result else None
                if result and result['turn_id'] != selected['turn_id']:
                    raise ThreadLookupError('THREAD_IDENTITY_CONFLICT', 'Result turn does not match selected execution')
            deliveries, hosts = [], []
            if selected:
                from tool_delivery import certainty
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='host_tool_deliveries'").fetchone():
                    deliveries = [{**dict(row), **certainty(row)} for row in c.execute(
                        'SELECT * FROM host_tool_deliveries WHERE execution_ref=? ORDER BY admitted_at',
                        (selected['execution_ref'],))]
                hosts = rows(c,
                    'SELECT host_execution_ref,execution_ref,tool_call_id,operation,capability,'
                    'result_state,exit_code,started_at,completed_at FROM host_executions '
                    'WHERE execution_ref=? ORDER BY started_at', (selected['execution_ref'],))
            snapshot = dict(deliveries=deliveries, hosts=hosts, task=task, current=current, lineage=lineage, facts=facts, executions=executions, selected=selected, reason=reason, result=result, provider=next(iter(providers), None))
            snapshot['fingerprint'] = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
            return snapshot

    @staticmethod
    def _select(executions, ref):
        if ref is not None:
            selected = next((r for r in executions if r['execution_ref'] == ref), None)
            if selected is None:
                raise ThreadLookupError('THREAD_SCOPE_MISMATCH', 'execution_ref does not belong to the queried thread')
            return selected, 'EXPLICIT_EXECUTION_REF'
        active = [r for r in executions if r['active_record'] and r['stage'] not in TERMINAL_EXECUTION_STAGES]
        if len(active) > 1:
            raise ThreadLookupError('THREAD_IDENTITY_CONFLICT', 'Multiple active executions for one thread')
        if active:
            return active[0], 'UNIQUE_ACTIVE_EXECUTION'
        if not executions:
            return None, 'NO_PROVEN_EXECUTION_ASSOCIATION'
        try:
            ordered = sorted(executions, key=lambda r: dt.datetime.fromisoformat(r['acquired_at']))
            if any(dt.datetime.fromisoformat(r['acquired_at']).tzinfo is None for r in ordered):
                raise ValueError()
            if len(ordered) > 1 and dt.datetime.fromisoformat(ordered[-1]['acquired_at']) == dt.datetime.fromisoformat(ordered[-2]['acquired_at']):
                raise ValueError()
        except (ValueError, TypeError):
            raise ThreadLookupError('THREAD_IDENTITY_CONFLICT', 'No reliable unique execution ordering')
        return ordered[-1], 'LATEST_THREAD_EXECUTION_BY_ACQUIRED_AT'

    def _native_metadata(self, tid):
        try:
            with readonly(self.native_root / 'state_5.sqlite') as c:
                r = c.execute('SELECT id, cwd, rollout_path FROM threads WHERE id=?', (tid,)).fetchone()
                return dict(r) if r else None
        except sqlite3.Error:
            return None

    def _local_context(self, tid, metadata, recent_turns, max_bytes, anchor=None):
        """Read only an indexed exact file and validate its session header and cwd."""
        if metadata is None:
            return None
        path = Path(metadata['rollout_path']).resolve()
        if not path.is_relative_to(self.native_root.resolve()) or not path.name.endswith('-' + tid + '.jsonl'):
            return None
        try:
            with path.open('rb') as f:
                head = f.readline(131072)
                header = json.loads(head)
                meta = header.get('payload', {})
                if header.get('type') != 'session_meta' or meta.get('id') != tid or Path(meta.get('cwd', '')).resolve() != Path(metadata['cwd']).resolve():
                    return None
                f.seek(0, 2)
                size = f.tell()
                offset = max(0, size - max_bytes)
                f.seek(offset)
                data = f.read(max_bytes)
            if offset:
                data = data.split(b'\n', 1)[1] if b'\n' in data else b''
            records = []
            turns = []
            for line in data.splitlines():
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if row.get('type') == 'turn_context':
                    turn = row.get('payload', {}).get('turn_id')
                    if turn and turn not in turns:
                        turns.append(turn)
                records.append(row)
            allowed = {anchor} if anchor else set(turns[-recent_turns:])
            if anchor and anchor not in turns:
                return None
            selected = []
            current = None
            for row in records:
                if row.get('type') == 'turn_context':
                    current = row.get('payload', {}).get('turn_id')
                if allowed and current not in allowed:
                    continue
                if row.get('type') != 'response_item':
                    continue
                payload = row.get('payload', {})
                # Only explicit user/assistant messages; never tool results or reasoning.
                if payload.get('type') == 'message' and payload.get('role') in ('user', 'assistant'):
                    selected.append(dict(payload, type=payload['role']))
            users, agents, texts, _, roles = self.reader._collect_item_text(selected)
            if not texts:
                return None
            users, agents, _, truncated = self.reader._bounded_messages(texts, roles, max_bytes)
            return dict(context_source='CODEX_LOCAL_SESSION', context_scope='THREAD_RECENT', context_range='bounded-tail', context_truncated=bool(offset or truncated or len(turns) > recent_turns), last_user_intent=users[-1] if users else None, last_codex_result=agents[-1] if agents else None, provenance={'queried_thread_id': tid, 'context_turn_refs': [anchor] if anchor else turns[-recent_turns:], 'execution_attribution': 'NOT_CLAIMED', 'source_bytes': len(data), 'indexed_source': 'CODEX_STATE_THREADS_PRIMARY_KEY'})
        except (OSError, ValueError, TypeError):
            return None

    def read(self, *, context=False, thread_id=None, codex_uri=None, task_ref=None, query=None, host=None, project=None, execution_ref=None, recent_turns=8, max_bytes=32000):
        tid = None
        try:
            tid = parse_selector(thread_id=thread_id, codex_uri=codex_uri, task_ref=task_ref, query=query)
            if type(recent_turns) is not int or not 1 <= recent_turns <= 20 or type(max_bytes) is not int or not 1024 <= max_bytes <= 128000:
                raise ThreadLookupError('INVALID_CONTEXT_LIMIT', 'recent_turns must be 1..20 and max_bytes 1024..128000')
            snapshot = self._snapshot(tid, host, project, execution_ref)
            metadata = None
            # The local index is only an authority for this machine's native files.
            native_host = self.cfg.runtime_host.casefold()
            resolved_host = snapshot['task']['host'].casefold() if snapshot else (host or native_host).casefold()
            if resolved_host in (native_host, 'workstation-' + native_host):
                metadata = self._native_metadata(tid)
            if snapshot is None:
                if metadata:
                    matches = [m for m in self.cfg.projects if Path(m.repo).resolve() == Path(metadata['cwd']).resolve()]
                    if len(matches) != 1:
                        raise ThreadLookupError('THREAD_ACCESS_DENIED', 'Native thread is outside a unique registered project')
                    mapping = matches[0]
                    task = dict(host=self.cfg.runtime_host, workspace_alias=mapping.workspace_alias, project_alias=mapping.project_alias, project_name=mapping.linear_name, cwd=mapping.repo)
                    self._scope(task, host, project)
                    if execution_ref is not None:
                        raise ThreadLookupError('THREAD_SCOPE_MISMATCH', 'No CLINX execution association for this native thread')
                    output = dict(lookup_status='THREAD_UNBOUND', binding_status='NOT_FOUND', provider_existence='CONFIRMED', task_ref=None, project=mapping.project_alias, host=self.cfg.runtime_host, execution_state='UNKNOWN')
                else:
                    output = dict(lookup_status='THREAD_LOOKUP_UNAVAILABLE', binding_status='NOT_FOUND', provider_existence='UNKNOWN', unavailable_reason='No binding; the local native index does not prove absence across all authorized provider history')
            else:
                task, selected = snapshot['task'], snapshot['selected']
                current, lineage = snapshot['current'], snapshot['lineage']
                if metadata and Path(metadata['cwd']).resolve() != Path(task['cwd']).resolve():
                    raise ThreadLookupError('THREAD_IDENTITY_CONFLICT', 'Native thread workspace disagrees with persisted owner')
                output = dict(lookup_status='RESOLVED', task_ref=task['task_id'], task_key=task['task_key'], host=task['host'], project=task['project_alias'], workspace=task['workspace_alias'], provider=snapshot['provider'], binding_sources=[k for k, v in snapshot['facts'].items() if v], current_thread_id=current['thread_id'] if current else None, is_current_thread=bool(current and current['thread_id'] == tid), relationship='HISTORICAL' if not current or current['thread_id'] != tid else 'CURRENT', predecessor_thread_id=lineage['predecessor_thread'] if lineage else None, successor_thread_id=lineage['successor_thread'] if lineage else None, adoption_source=snapshot['facts']['adoptions'][0]['adoption_source'] if snapshot['facts']['adoptions'] else None, execution_ref=selected['execution_ref'] if selected else None, execution_state=(selected['execution_state'] or selected['stage']) if selected else 'UNKNOWN', selection_reason=snapshot['reason'], selection_scope='QUERIED_THREAD', other_execution_refs=[ref for ref in snapshot['executions'] if not selected or ref != selected['execution_ref']], execution_turn_ref=selected['turn_id'] if selected else None, status_source='PERSISTED_EXECUTION' if selected else 'UNAVAILABLE', provider_observation={'state': 'UNKNOWN', 'reason': 'No provider process probe on read plane'}, task_current_projection={'execution_state': task['execution_state'], 'updated_at': task['updated_at'], 'current_thread_id': current['thread_id'] if current else None}, execution_result={k: snapshot['result'][k] for k in ('status', 'received_at', 'writeback_state')} if snapshot['result'] else None)
                from tool_delivery import delivery_summary
                output.update(dynamic_tool_deliveries=snapshot['deliveries'], host_executions=snapshot['hosts'],
                    provider_delivery=delivery_summary(snapshot['deliveries'],
                        snapshot['result']['raw_result'] if snapshot['result'] else ''))
            if context and (snapshot or output['lookup_status'] == 'THREAD_UNBOUND'):
                anchor = snapshot['facts']['anchors'][0]['turn_id'] if snapshot and snapshot['facts']['anchors'] else None
                selected_context = self._local_context(tid, metadata, recent_turns, max_bytes, anchor)
                if selected_context is None and snapshot:
                    checkpoints = snapshot['facts']['checkpoints']
                    if anchor:
                        checkpoints = [p for p in checkpoints if p['turn_id'] == anchor]
                    if execution_ref is not None:
                        checkpoints = [p for p in checkpoints if p['execution_id'] == execution_ref and p['turn_id'] == snapshot['selected']['turn_id']]
                    if checkpoints:
                        p = checkpoints[0]
                        texts = [p['prompt_summary'], p['result_summary']]
                        users, agents, _, truncated = self.reader._bounded_messages(texts, ['user', 'assistant'], max_bytes)
                        selected_context = dict(context_source='CLINX_CHECKPOINT', context_scope='THREAD_CHECKPOINT', context_range=p['timestamp'], context_truncated=truncated, last_user_intent=users[-1] if users else None, last_codex_result=agents[-1] if agents else None, provenance={'queried_thread_id': tid, 'context_turn_refs': [p['turn_id']] if p['turn_id'] else [], 'context_execution_ref': p['execution_id'], 'checkpoint_ref': p['checkpoint_id'], 'execution_attribution': 'CHECKPOINT_RECORDED_ASSOCIATION'})
                if selected_context:
                    output.update(selected_context, context_status='AVAILABLE')
                else:
                    output.update(context_status='CONTEXT_UNAVAILABLE', context_source=None, context_unavailable_reason='No exact authorized indexed native file or same-thread checkpoint; provider transport is not started by this reader')
            after = self._snapshot(tid, host, project, execution_ref)
            if bool(after) != bool(snapshot) or (snapshot and after['fingerprint'] != snapshot['fingerprint']):
                raise ThreadLookupError('THREAD_LOOKUP_UNAVAILABLE', 'THREAD_IDENTITY_CHANGED_DURING_READ')
            return dict(output, queried_thread_id=tid, codex_uri='codex://threads/' + tid, read_only=True, observed_at=stamp())
        except ThreadLookupError as e:
            return dict(error_code=e.code, lookup_status=e.code, unavailable_reason=e.reason, queried_thread_id=tid, read_only=True, observed_at=stamp())
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
            return dict(error_code='THREAD_LOOKUP_UNAVAILABLE', lookup_status='THREAD_LOOKUP_UNAVAILABLE', unavailable_reason='Identity storage unavailable or incompatible', queried_thread_id=tid, read_only=True, observed_at=stamp())
