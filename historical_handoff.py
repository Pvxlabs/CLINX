"""CAS audit of committed historical completion; never a finalizer or replay.

Only the handoff delivery marker changes. Current task/turn identity protection
in CompletionRuntime remains mandatory for ordinary recovery.
"""
import contextlib
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from task_registry import TaskRegistryError


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def evidence(conn, ref):
    def rows(table, column='execution_ref', value=ref):
        return [dict(r) for r in conn.execute(
            f'SELECT * FROM {table} WHERE {column}=? ORDER BY rowid', (value,))]
    data = {t: rows(t) for t in (
        'execution_history', 'execution_results', 'clinx_completion_handoffs',
        'clinx_execution_owners', 'executions', 'worktree_leases', 'execution_liveness')}
    data['prepared_executions'] = rows('prepared_executions', 'resulting_execution_ref')
    if len(data['execution_history']) != 1:
        raise TaskRegistryError('HISTORICAL_COMPLETION_UNPROVEN')
    task = data['execution_history'][0]['task_id']
    data['tasks'] = rows('tasks', 'task_id', task)
    data['conversation_bindings'] = rows('conversation_bindings', 'task_id', task)
    data['current_executions'] = rows('executions', 'task_id', task)
    data['task_history'] = rows('execution_history', 'task_id', task)
    return data


def validate(data):
    if any(len(data[t]) != 1 for t in (
            'execution_history', 'execution_results', 'clinx_completion_handoffs',
            'prepared_executions', 'tasks', 'conversation_bindings', 'execution_liveness')):
        raise TaskRegistryError('HISTORICAL_COMPLETION_AMBIGUOUS')
    h, r, d, p, t, b = (data[k][0] for k in (
        'execution_history', 'execution_results', 'clinx_completion_handoffs',
        'prepared_executions', 'tasks', 'conversation_bindings'))
    live = data['execution_liveness'][0]
    try:
        route = json.loads(h['routing_identity_json'])
        thread = route['conversation']['binding']
        prepared_route = json.loads(p['routing_identity_json'])
    except (TypeError, ValueError, KeyError) as exc:
        raise TaskRegistryError('HISTORICAL_COMPLETION_IDENTITY_CONFLICT') from exc
    if (not thread or not h['turn_id'] or route['conversation']['status'] != 'BOUND'
            or h['stage'] != 'COMPLETED' or h['execution_state'] != 'COMPLETED'
            or not h['released_at'] or h['failure_code']
            or r['status'] not in {'PASS', 'COMPLETED'} or not r['raw_result']
            or (r['task_id'], r['turn_id']) != (h['task_id'], h['turn_id'])
            or (d['task_id'], d['thread_id'], d['turn_id']) != (h['task_id'], thread, h['turn_id'])
            or d['state'] not in {'PENDING', 'HOLD'} or d['terminal_observed'] != 1
            or (p['resulting_task_id'], p['resulting_thread_id'], p['resulting_turn_id'])
                != (h['task_id'], thread, h['turn_id'])
            or prepared_route != route or p['execution_policy_json'] != h['execution_policy_json']
            or (live['thread_id'], live['turn_id']) != (thread, h['turn_id'])
            or live['provider_liveness'] != 'TERMINAL'
            or live['liveness_reason'] != 'EXACT_OWNER_TERMINAL'
            or live['ownership_conflict'] or live['release_safe'] != 1
            or t['turn_id'] == h['turn_id'] or not t['turn_id']
            or not any(e['turn_id'] == t['turn_id'] and e['acquired_at'] > h['acquired_at']
                       for e in data['task_history'] + data['current_executions'])
            or b['thread_id'] != thread or data['executions'] or data['worktree_leases']):
        raise TaskRegistryError('HISTORICAL_COMPLETION_UNPROVEN')


def reconcile(registry, execution_ref, *, expected_sha256=None, check_only=False):
    # check-only does not acquire a writer transaction or initialize any schema.
    uri = registry.path.resolve().as_uri() + ('?mode=ro' if check_only else '?mode=rw')
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        if check_only:
            conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN' if check_only else 'BEGIN IMMEDIATE')
        try:
            data = evidence(conn, execution_ref)
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='historical_handoff_audit'").fetchone()
            prior = conn.execute('SELECT * FROM historical_handoff_audit WHERE execution_ref=?',
                                 (execution_ref,)).fetchone() if exists else None
            if prior:
                # Repeated calls may observe further current rounds. Audit identity
                # and the exact postimage, rather than impersonating those rounds.
                before = json.loads(prior['evidence_json'])
                immutable = ('execution_history', 'execution_results', 'prepared_executions',
                             'clinx_execution_owners', 'executions', 'worktree_leases', 'execution_liveness')
                if (any(data[k] != before[k] for k in immutable)
                        or digest(data['clinx_completion_handoffs']) != prior['post_sha256']
                        or expected_sha256 not in {None, prior['expected_sha256']}):
                    raise TaskRegistryError('HISTORICAL_HANDOFF_AUDIT_CONFLICT')
                return {**json.loads(prior['receipt_json']), 'idempotent': True, 'check_only': check_only}
            validate(data)
            sha = digest(data)
            receipt = dict(execution_ref=execution_ref, evidence_sha256=sha,
                           completion_delivery='DONE', source='COMMITTED_HISTORICAL_COMPLETION',
                           replay_performed=False, current_task_mutated=False,
                           lease_mutated=False, owner_mutated=False, business_outcome='NOT_EVALUATED',
                           idempotent=False, check_only=check_only)
            if check_only:
                return receipt
            if expected_sha256 != sha:
                raise TaskRegistryError('HISTORICAL_HANDOFF_STALE_CAS')
            conn.execute('''CREATE TABLE IF NOT EXISTS historical_handoff_audit (
                execution_ref TEXT PRIMARY KEY, expected_sha256 TEXT NOT NULL,
                post_sha256 TEXT NOT NULL, evidence_json TEXT NOT NULL,
                receipt_json TEXT NOT NULL, reconciled_at TEXT NOT NULL)''')
            for operation in ('UPDATE', 'DELETE'):
                conn.execute(f'''CREATE TRIGGER IF NOT EXISTS historical_handoff_no_{operation.lower()}
                    BEFORE {operation} ON historical_handoff_audit
                    BEGIN SELECT RAISE(ABORT, 'immutable historical handoff audit'); END''')
            # One serialized transaction covers the entire evidence preimage,
            # marker CAS and append-only receipt. No provider/native RPC exists.
            old = data['clinx_completion_handoffs'][0]
            changed = conn.execute('''UPDATE clinx_completion_handoffs SET state='DONE'
                WHERE execution_ref=? AND state=? AND updated_at=? AND attempts=?
                AND task_id=? AND thread_id=? AND turn_id=? AND terminal_observed=1''',
                tuple(old[k] for k in ('execution_ref','state','updated_at','attempts',
                                      'task_id','thread_id','turn_id'))).rowcount
            if changed != 1:
                raise TaskRegistryError('HISTORICAL_HANDOFF_STALE_CAS')
            post = [{**old, 'state': 'DONE'}]
            conn.execute('INSERT INTO historical_handoff_audit VALUES (?,?,?,?,?,?)',
                         (execution_ref, sha, digest(post), encoded(data), encoded(receipt),
                          datetime.now(timezone.utc).isoformat()))
            conn.commit()
            return receipt
        finally:
            if conn.in_transaction:
                conn.rollback()
