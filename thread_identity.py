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
from urllib.parse import urlsplit, unquote, quote

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
        return _parse_uri(codex_uri)[0]
    if not isinstance(thread_id, str) or not re.fullmatch(ID_PATTERN, thread_id):
        raise ThreadLookupError("INVALID_THREAD_ID", "Expected a canonical hyphenated Codex UUID (not restricted to v4)")
    return thread_id


def _parse_uri(uri):
    error = ThreadLookupError('INVALID_CODEX_THREAD_URI', 'Expected codex://threads/<UUID> with at most one hostId parameter')
    if not isinstance(uri, str) or any(ord(c) <= 32 or ord(c) == 127 for c in uri):
        raise error
    try:
        parts = urlsplit(uri)
    except ValueError:
        raise error from None
    if (not uri.startswith('codex://threads/') or parts.netloc != 'threads'
            or parts.fragment or '#' in uri or not re.fullmatch('/' + ID_PATTERN, parts.path)):
        raise error
    host_id = None
    if '?' in uri:
        if not parts.query or '&' in parts.query or not parts.query.startswith('hostId='):
            raise error
        encoded = parts.query[len('hostId='):]
        if not encoded or re.search(r'%(?![0-9A-Fa-f]{2})', encoded):
            raise error
        try:
            host_id = unquote(encoded, encoding='utf-8', errors='strict')
        except UnicodeError:
            raise error from None
        # One decoding only. No plus-as-space, double encoding, userinfo or address syntax.
        if not re.fullmatch(r'[A-Za-z0-9_.:-]+', host_id) or '%' in host_id:
            raise error
    return parts.path[1:], host_id


def resolve_selector(cfg, *, host=None, **selectors):
    """Resolve only configured host identities, never a URI-supplied SSH address."""
    from execution_semantics import normalize_host
    tid = parse_selector(**selectors)
    host_id = _parse_uri(selectors['codex_uri'])[1] if selectors.get('codex_uri') is not None else None
    routes = {}
    desktop = {}
    for workspace in cfg.workspaces:
        canonical = normalize_host(workspace.host or workspace.alias).stable_identifier
        for alias in (workspace.alias, workspace.host, canonical, 'workstation-' + canonical):
            if alias:
                routes.setdefault(alias.casefold(), set()).add(canonical)
        for alias in getattr(workspace, 'codex_host_ids', ()):
            desktop.setdefault(alias, set()).add(canonical)
    def unique(values, code):
        if not values:
            raise ThreadLookupError(code, 'Host is not an authorized configured route')
        if len(values) != 1:
            raise ThreadLookupError('THREAD_HOST_CONFLICT', 'Host has multiple configured routes')
        return next(iter(values))
    explicit = unique(routes.get(host.casefold()), 'UNKNOWN_THREAD_HOST') if isinstance(host, str) else None
    if host is not None and not isinstance(host, str):
        raise ThreadLookupError('UNKNOWN_THREAD_HOST', 'Host must be a configured route name')
    routed = unique(desktop.get(host_id), 'UNKNOWN_CODEX_HOST_ID') if host_id is not None else None
    if routed and explicit and routed != explicit:
        raise ThreadLookupError('THREAD_HOST_CONFLICT', 'URI hostId conflicts with explicit host')
    resolved = routed or explicit or unique(routes.get(cfg.runtime_host.casefold()), 'UNKNOWN_THREAD_HOST')
    return tid, resolved, host_id


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


def native_display_state(native_status, provider_observation=None):
    """Project one native turn for presentation without changing CLINX state.

    A live exact active owner outranks persisted history.  Unknown, offline and
    ``notLoaded`` observations never turn a proved terminal turn back into
    running; owner conflicts remain UNKNOWN.  The returned value is deliberately
    separate from ``execution_state`` because an unbound native thread has no
    managed execution to complete.
    """
    from native_history import normalize_native_status

    native_state = normalize_native_status((native_status or {}).get('status')
                                           if isinstance(native_status, dict) else None)
    observation = provider_observation if isinstance(provider_observation, dict) else {}
    if observation.get('ownership_conflict'):
        return 'UNKNOWN'
    rows = observation.get('observations') or []
    active_owner = any(
        normalize_native_status(row.get('state')) == 'RUNNING'
        and normalize_native_status(row.get('turn_status')) == 'RUNNING'
        for row in rows if isinstance(row, dict)
    )
    if active_owner:
        return 'RUNNING'
    if native_state in {'RUNNING', 'COMPLETED', 'FAILED', 'TIMED_OUT',
                        'CANCELLED', 'INTERRUPTED', 'DISCONNECTED'}:
        return native_state
    return 'UNKNOWN'


class ThreadIdentityReader:
    def __init__(self, cfg, registry_path, context_reader, *, native_root=None, live_reader=None):
        self.cfg, self.path, self.reader = cfg, Path(registry_path), context_reader
        self.native_root = Path(native_root or getattr(cfg.app_server, 'native_home', None) or Path.home() / ".codex")
        if native_root is None and live_reader is None:
            from native_provider import observe_thread
            live_reader = observe_thread
        self.live_reader = live_reader

    def _scope(self, task, host=None, project=None):
        from execution_semantics import normalize_host
        actual_host = normalize_host(task['host']).stable_identifier
        if host is not None and normalize_host(host).stable_identifier != actual_host:
            raise ThreadLookupError("THREAD_SCOPE_MISMATCH", "Host does not match the exact thread")
        if project is not None and project.casefold() not in {task['project_alias'].casefold(), task['project_name'].casefold()}:
            raise ThreadLookupError("THREAD_SCOPE_MISMATCH", "Project does not match the exact thread")
        # Workspace registration authorizes execution, not native history reads.
        return actual_host

    def _snapshot(self, tid, host, project, execution_ref):
        if not self.path.exists():
            return None
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
            has_policy_lineage = c.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_policy_lineage'").fetchone()
            if has_policy_lineage:
                facts['predecessors'] += rows(c, 'SELECT * FROM conversation_policy_lineage WHERE predecessor_thread=?', (tid,))
                facts['successors'] += rows(c, 'SELECT * FROM conversation_policy_lineage WHERE successor_thread=?', (tid,))
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
            if has_policy_lineage:
                lineage = c.execute('SELECT * FROM conversation_policy_lineage WHERE task_id=? ORDER BY policy_version DESC LIMIT 1',
                                    (task['task_id'],)).fetchone() or lineage
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
        from native_history import NativeHistory
        return NativeHistory(self.native_root).metadata(tid)

    def _local_context(self, tid, metadata, recent_turns, max_bytes, anchor=None, *, host=None, cursor=None):
        if metadata is None:
            return None
        from native_history import NativeHistory
        return NativeHistory(self.native_root).context(tid, host or self.cfg.runtime_host,
            metadata, recent_turns, max_bytes, anchor=anchor, cursor=cursor)

    def read(self, *, context=False, thread_id=None, codex_uri=None, task_ref=None, query=None, host=None, node_id=None, project=None, execution_ref=None, recent_turns=8, max_bytes=32000, cursor=None):
        tid = None
        try:
            # A configured centre router is the only cross-device read path.
            # It is consulted for exact native selectors before the legacy
            # single-host resolver.  Task/query selectors remain local CLINX
            # registry reads and never trigger an unbounded node scan.
            node_router = getattr(self.cfg, "node_router", None)
            if node_router is not None and (thread_id is not None or codex_uri is not None) and task_ref is None and query is None:
                tid = parse_selector(thread_id=thread_id, codex_uri=codex_uri)
                uri_host = _parse_uri(codex_uri)[1] if codex_uri is not None else None
                nodes = getattr(getattr(node_router, "registry", None), "list_nodes", lambda **_: [])(user_scope=getattr(node_router, "user_scope", None))
                if node_id is not None or host is not None or uri_host is not None or len(nodes) > 1:
                    if type(recent_turns) is not int or not 1 <= recent_turns <= 20 or type(max_bytes) is not int or not 1024 <= max_bytes <= 128000:
                        raise ThreadLookupError('INVALID_CONTEXT_LIMIT', 'recent_turns must be 1..20 and max_bytes 1024..128000')
                    routed = node_router.read(thread_id=tid, node_id=node_id, host=host or uri_host, provider='codex_app_server',
                        cursor=cursor, recent_turns=recent_turns, max_bytes=max_bytes)
                    routed = dict(routed)
                    routed.setdefault('queried_thread_id', tid)
                    routed.setdefault('codex_uri', codex_uri or 'codex://threads/' + tid)
                    routed.setdefault('read_only', True)
                    routed.setdefault('observed_at', stamp())
                    return routed
            tid, host, host_id = resolve_selector(self.cfg, host=host, thread_id=thread_id, codex_uri=codex_uri, task_ref=task_ref, query=query)
            if type(recent_turns) is not int or not 1 <= recent_turns <= 20 or type(max_bytes) is not int or not 1024 <= max_bytes <= 128000:
                raise ThreadLookupError('INVALID_CONTEXT_LIMIT', 'recent_turns must be 1..20 and max_bytes 1024..128000')
            from execution_semantics import normalize_host
            if host != normalize_host(self.cfg.runtime_host).stable_identifier:
                raise ThreadLookupError('NATIVE_HOST_UNAVAILABLE', 'Configured remote native reader is unavailable; no local fallback')
            metadata, native_error = None, None
            try:
                metadata = self._native_metadata(tid)
            except ThreadLookupError as exc:
                native_error = exc
            association_error = None
            try:
                snapshot = self._snapshot(tid, host, project, execution_ref)
            except (sqlite3.Error, OSError) as exc:
                if metadata is None or execution_ref is not None:
                    raise
                snapshot, association_error = None, 'CLINX_IDENTITY_STORAGE_UNAVAILABLE'
            except ThreadLookupError as exc:
                if metadata is None or execution_ref is not None or exc.code != 'THREAD_LOOKUP_UNAVAILABLE':
                    raise
                snapshot, association_error = None, exc.code
            if snapshot is None:
                if metadata:
                    if execution_ref is not None:
                        raise ThreadLookupError('THREAD_SCOPE_MISMATCH', 'No CLINX execution association for this native thread')
                    if project is not None:
                        matches = [m for m in self.cfg.projects if Path(m.repo).resolve() == Path(metadata['cwd']).resolve()
                                   and project.casefold() in (m.project_alias.casefold(), m.linear_name.casefold())]
                        if not matches and project != metadata['cwd'] and project != Path(metadata['cwd']).name:
                            raise ThreadLookupError('THREAD_SCOPE_MISMATCH', 'Project selector disagrees with native cwd')
                    output = dict(lookup_status='THREAD_UNBOUND', binding_status='NOT_FOUND', provider_existence='CONFIRMED',
                        task_ref=None, execution_ref=None, project=None, host=host, execution_state='UNKNOWN')
                    if association_error:
                        output.update(binding_status='UNAVAILABLE', unavailable_reason=association_error)
                elif native_error:
                    raise native_error
                else:
                    output = dict(lookup_status='THREAD_NOT_FOUND', binding_status='NOT_FOUND', provider_existence='NOT_FOUND',
                        unavailable_reason='Exact current-user native index has no row', absence_scope='CURRENT_USER_NATIVE_INDEX', task_ref=None, execution_ref=None)
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
                output.update({key: selected.get(key) if selected else None
                               for key in ('failure_stage', 'failure_code', 'failure_evidence')})
                output['failure_source'] = ('EXECUTION_RECORD' if selected and selected.get('failure_code') else
                    'UNAVAILABLE_LEGACY' if selected and selected['stage'] in ('FAILED', 'BLOCKED', 'RECOVERY_REQUIRED') else 'NONE')
            from execution_liveness import public_status
            if self.path.exists():
                with readonly(self.path) as conn:
                    output.update(public_status(conn, output.get("execution_ref")))
            if context and (snapshot or output['lookup_status'] == 'THREAD_UNBOUND'):
                anchor = snapshot['facts']['anchors'][0]['turn_id'] if snapshot and snapshot['facts']['anchors'] else None
                if execution_ref is not None and snapshot:
                    anchor = snapshot['selected']['turn_id']
                selected_context = None
                try:
                    if execution_ref is None or anchor:
                        selected_context = self._local_context(tid, metadata, recent_turns, max_bytes, anchor, host=host, cursor=cursor)
                except ThreadLookupError as exc:
                    if exc.code == 'INVALID_CONTEXT_CURSOR':
                        raise
                    output['context_unavailable_reason'] = exc.code
                except (OSError, ValueError):
                    output['context_unavailable_reason'] = 'NATIVE_HISTORY_UNAVAILABLE'
                if selected_context and selected_context['context_status'] != 'AVAILABLE':
                    output.update(selected_context)
                    selected_context = None
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
                    output.update(selected_context)
                    output['context_status'] = 'AVAILABLE'
                else:
                    output.update(context_status='CONTEXT_UNAVAILABLE', context_source=output.get('context_source'), context_unavailable_reason=output.get('context_unavailable_reason', 'No display messages in this page or same-thread checkpoint'))
            after = self._snapshot(tid, host, project, execution_ref) if association_error is None else None
            if bool(after) != bool(snapshot) or (snapshot and after['fingerprint'] != snapshot['fingerprint']):
                raise ThreadLookupError('THREAD_LOOKUP_UNAVAILABLE', 'THREAD_IDENTITY_CHANGED_DURING_READ')
            if metadata:
                from bridge import _context_text
                from native_history import NativeHistory
                output['native_thread'] = {k: metadata.get(k) for k in ('cwd', 'history_mode', 'archived', 'created_at', 'updated_at')}
                output['native_thread'].update(thread_id=tid,
                    name=_context_text(metadata.get('name') or metadata.get('title') or '', 512))
                output['status_source'] = output.get('status_source', 'NATIVE_PERSISTED_HISTORY')
                output['native_status'] = NativeHistory(self.native_root).status(tid, metadata)
            if self.live_reader is not None:
                output['provider_observation'] = self.live_reader(self.cfg, tid)
                if metadata is None and any(r.get('source') == 'LIVE_THREAD_READ' for r in output['provider_observation'].get('observations', [])):
                    output.update(provider_existence='CONFIRMED')
                    if snapshot is None:
                        output.update(lookup_status='THREAD_LOOKUP_UNAVAILABLE', absence_scope=None,
                                      unavailable_reason='Native index and live provider disagree; no association inferred')
            else:
                output.setdefault('provider_observation', {'state': 'UNKNOWN', 'reason': 'Live observation not configured'})
            if metadata:
                native_status = output.get('native_status') or {}
                output['native_turn_state'] = native_status.get('state', 'UNKNOWN')
                output['native_status_source'] = native_status.get('source', 'NATIVE_PERSISTED_HISTORY')
                output['native_display_state'] = native_display_state(
                    native_status, output.get('provider_observation'))
            output['host'] = host
            if context:
                output.setdefault('context_status', 'CONTEXT_UNAVAILABLE')
            return dict(output, queried_thread_id=tid, codex_uri='codex://threads/' + tid +
                ('?hostId=' + quote(host_id, safe='') if host_id else ''), read_only=True, observed_at=stamp())
        except ThreadLookupError as e:
            return dict(error_code=e.code, lookup_status=e.code, unavailable_reason=e.reason, queried_thread_id=tid, read_only=True, observed_at=stamp())
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
            return dict(error_code='THREAD_LOOKUP_UNAVAILABLE', lookup_status='THREAD_LOOKUP_UNAVAILABLE', unavailable_reason='Identity storage unavailable or incompatible', queried_thread_id=tid, read_only=True, observed_at=stamp())
