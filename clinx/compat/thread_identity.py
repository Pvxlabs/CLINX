"""Legacy Python-registry Thread projection for the Mac compatibility route.

No TaskRegistry construction, reconciliation, provider startup or discovery.
Indexes are installed by the normal registry initialization migration, never here.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3

from clinx.contracts.runtime_contracts import TERMINAL_EXECUTION_STAGES
from clinx.nodes.node_protocol import NodeProtocolError

from clinx.contracts.thread_identity import (
    ID_PATTERN as ID_PATTERN,
    ThreadLookupError as ThreadLookupError,
    parse_selector as parse_selector,
    _parse_uri as _parse_uri,
    resolve_selector as resolve_selector,
    canonical_codex_uri as canonical_codex_uri,
    readonly as readonly,
    rows as rows,
    stamp as stamp,
    native_display_state as native_display_state,
)

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


class ThreadIdentityReader:
    def __init__(self, cfg, registry_path, context_reader, *, native_root=None, live_reader=None):
        self.cfg, self.path, self.reader = cfg, Path(registry_path), context_reader
        self.native_root = Path(native_root or getattr(cfg.app_server, 'native_home', None) or Path.home() / ".codex")
        if native_root is None and live_reader is None:
            from clinx.provider.native_provider import observe_thread
            live_reader = observe_thread
        self.live_reader = live_reader

    def _scope(self, task, host=None, project=None):
        from clinx.contracts.execution_semantics import normalize_host
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
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='native_task_bindings'").fetchone():
                facts['native_registry'] = rows(c, "SELECT * FROM native_task_bindings WHERE host=? AND provider='codex_app_server' AND thread_id=?", (host, tid))
            ids = {r.get('task_id') or r.get('resulting_task_id') for group in facts.values() for r in group}
            ids.discard(None)
            if not ids:
                return None
            tasks = [dict(c.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone() or {}) for task_id in ids]
            if facts.get('native_registry') or len(tasks) > 1:
                # A native UUID is namespaced by its paired node. Unrelated
                # legacy managed relations on another host are not its owners.
                from clinx.contracts.execution_semantics import normalize_host
                tasks = [task for task in tasks if not task or normalize_host(task['host']).stable_identifier == host]
                scoped_ids = {task['task_id'] for task in tasks if task}
                facts = {name: [row for row in group if (row.get('task_id') or row.get('resulting_task_id')) in scoped_ids]
                         for name, group in facts.items()}
            # Authorize before returning any identity or association facts.
            for task in tasks:
                if not task:
                    raise ThreadLookupError('THREAD_IDENTITY_CONFLICT', 'Relation references a missing task')
                self._scope(task, host, project)
            if len(tasks) != 1:
                raise ThreadLookupError('THREAD_IDENTITY_CONFLICT', 'Exact thread has multiple authorized task owners')
            task = tasks[0]
            current = c.execute('SELECT * FROM conversation_bindings WHERE task_id=?', (task['task_id'],)).fetchone()
            current = dict(current) if current else ({'thread_id': tid} if facts.get('native_registry') else None)
            lineage = c.execute('SELECT * FROM conversation_binding_lineage WHERE task_id=? ORDER BY migrated_at DESC LIMIT 1', (task['task_id'],)).fetchone()
            if has_policy_lineage:
                policy_lineage = c.execute('SELECT * FROM conversation_policy_lineage WHERE task_id=? ORDER BY policy_version DESC LIMIT 1',
                                           (task['task_id'],)).fetchone()
                if policy_lineage and (lineage is None or policy_lineage['migrated_at'] > lineage['migrated_at']):
                    lineage = policy_lineage
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
                from clinx.host.tool_delivery import certainty
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='host_tool_deliveries'").fetchone():
                    deliveries = [{**dict(row), **certainty(row)} for row in c.execute(
                        'SELECT * FROM host_tool_deliveries WHERE execution_ref=? ORDER BY admitted_at',
                        (selected['execution_ref'],))]
                hosts = rows(c,
                    'SELECT host_execution_ref,execution_ref,tool_call_id,operation,capability,'
                    'result_state,exit_code,started_at,completed_at FROM host_executions '
                    'WHERE execution_ref=? ORDER BY started_at', (selected['execution_ref'],))
            snapshot = dict(deliveries=deliveries, hosts=hosts, task=task, current=current, lineage=lineage, facts=facts, executions=executions, selected=selected, reason=reason, result=result, provider=next(iter(providers), 'codex_app_server' if facts.get('native_registry') else None))
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
        from clinx.provider.native_history import NativeHistory
        return NativeHistory(self.native_root).metadata(tid)

    def _local_context(self, tid, metadata, recent_turns, max_bytes, anchor=None, *, host=None, cursor=None):
        if metadata is None:
            return None
        from clinx.provider.native_history import NativeHistory
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
                    if host is not None or uri_host is not None:
                        _, resolved, _ = resolve_selector(self.cfg, thread_id=thread_id, codex_uri=codex_uri, host=host)
                        if node_id is not None and node_router._node_for(node_id=node_id).node_id != resolved:
                            raise ThreadLookupError('THREAD_HOST_CONFLICT', 'node_id conflicts with host selector')
                    else:
                        resolved = None
                    if execution_ref or project:
                        target = node_router._node_for(node_id=node_id, host=resolved)
                        return node_router.execute(node_id=target.node_id, operation='execution.context', request={
                            'thread_id': tid, 'host': target.node_id, 'execution_ref': execution_ref,
                            'project': project, 'recent_turns': recent_turns, 'max_bytes': max_bytes, 'cursor': cursor})
                    routed = node_router.read(thread_id=tid, node_id=node_id, host=resolved, provider='codex_app_server',
                        cursor=cursor, recent_turns=recent_turns, max_bytes=max_bytes)
                    routed = dict(routed)
                    routed.setdefault('queried_thread_id', tid)
                    routed.setdefault('codex_uri', codex_uri or 'codex://threads/' + tid)
                    routed.setdefault('read_only', True)
                    routed.setdefault('observed_at', stamp())
                    if routed.get('lookup_status') == 'THREAD_UNBOUND' and not routed.get('error_code'):
                        from clinx.monitor.native_associations import binding
                        associated = binding(self.path, node=routed.get('source_node_id'), thread=tid,
                                             user=getattr(node_router, 'user_scope', None))
                        if associated:
                            routed.update(lookup_status='RESOLVED', binding_status='NATIVE_REGISTERED',
                                task_ref=associated['task_id'], source='CODEX_NATIVE',
                                current_thread_id=tid, is_current_thread=True,
                                execution_ref=None, status_source='NATIVE_OBSERVATION')
                    return routed
            tid, host, host_id = resolve_selector(self.cfg, host=host, thread_id=thread_id, codex_uri=codex_uri, task_ref=task_ref, query=query)
            if type(recent_turns) is not int or not 1 <= recent_turns <= 20 or type(max_bytes) is not int or not 1024 <= max_bytes <= 128000:
                raise ThreadLookupError('INVALID_CONTEXT_LIMIT', 'recent_turns must be 1..20 and max_bytes 1024..128000')
            from clinx.contracts.execution_semantics import normalize_host
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
                if snapshot['facts'].get('native_registry') and not selected:
                    output.update(source='CODEX_NATIVE', status_source='NATIVE_OBSERVATION',
                        binding_status='NATIVE_REGISTERED', execution_state=task['execution_state'],
                        observation_provenance=json.loads(snapshot['facts']['native_registry'][0]['latest_provenance_json']))
                from clinx.compat.m9_integration import public_execution_result
                output['execution_result'] = public_execution_result(snapshot['result'])
                from clinx.host.tool_delivery import delivery_summary
                output.update(dynamic_tool_deliveries=snapshot['deliveries'], host_executions=snapshot['hosts'],
                    provider_delivery=delivery_summary(snapshot['deliveries'],
                        snapshot['result']['raw_result'] if snapshot['result'] else ''))
                output.update({key: selected.get(key) if selected else None
                               for key in ('failure_stage', 'failure_code', 'failure_evidence')})
                output['failure_source'] = ('EXECUTION_RECORD' if selected and selected.get('failure_code') else
                    'UNAVAILABLE_LEGACY' if selected and selected['stage'] in ('FAILED', 'BLOCKED', 'RECOVERY_REQUIRED') else 'NONE')
            from clinx.compat.execution_liveness import public_status
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
                from clinx.compat.bridge import _context_text
                from clinx.provider.native_history import NativeHistory
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
            return dict(output, queried_thread_id=tid,
                codex_uri=canonical_codex_uri(tid, cfg=self.cfg, host=host, host_id=host_id),
                read_only=True, observed_at=stamp())
        except (ThreadLookupError, NodeProtocolError) as e:
            return dict(error_code=e.code, lookup_status=e.code, unavailable_reason=e.reason, queried_thread_id=tid, read_only=True, observed_at=stamp())
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
            return dict(error_code='THREAD_LOOKUP_UNAVAILABLE', lookup_status='THREAD_LOOKUP_UNAVAILABLE', unavailable_reason='Identity storage unavailable or incompatible', queried_thread_id=tid, read_only=True, observed_at=stamp())
