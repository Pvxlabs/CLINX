"""Exact pre-turn model failure reconciliation; never owns a Provider writer.

New failures are attested at the resolver call site. Legacy UNKNOWN responses
qualify only against the reviewed, byte-exact implementation that produced
them. Missing turn ids, idle Providers and interrupt errors are not absence
proof. Both paths use the canonical Finalizer, recovery audit and lease.
"""
import contextlib
import datetime as dt
import hashlib
import json
import re
import time
from pathlib import Path

from task_registry import TaskRegistryError

# Reviewed during the 2026-10-18 RCA: resolve_model raises before turn_start.
# A different build requires a new review, not an operator-supplied assertion.
LEGACY_SOURCE = {
    'bridge.py': '6510958daf754f3da4d5497a6cb8b6caf0007a1354492f0113c488a62ec42317',
    'app_server.py': '16f783cedb103949ced59ec9ed39c17459e0eb2d88b0c4018bf27685ca5d9f66',
    'execution_owner.py': 'bc7a6d61dbf079dd69d4d40886078c564b6211f70c31469cf64f4dd220d0851b',
    'dispatch_errors.py': '9a347e266746b5687009777a250ad02fc93e89342a852cffa37f9e167ba7f242',
}
EXPECTED_FIELDS = ('task_id', 'execution_ref', 'prepared_execution_ref', 'thread_id',
                   'request_hash', 'owner_instance', 'owner_pid', 'worktree_key', 'acquired_at')
PENDING = {'CLAIMED', 'DISPATCHING', 'RECOVERY_REQUIRED', 'TRANSPORT_UNCERTAIN',
           'CANCEL_REQUESTED', 'CANCELLATION_PENDING'}
KIND = 'MODEL_RESOLUTION_TURN_NOT_DISPATCHED_V1'


def deny(reason):
    raise TaskRegistryError('UNSTARTED_MODEL_FAILURE_' + reason)


def timestamp(value):
    try:
        stamp = dt.datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            deny('TIMESTAMP_UNPROVEN')
        return stamp.timestamp()
    except (TypeError, ValueError):
        deny('TIMESTAMP_UNPROVEN')


def _row(conn, table, key, value):
    if not conn.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone():
        return None
    row = conn.execute(f'SELECT * FROM {table} WHERE {key}=?', (value,)).fetchone()
    return dict(row) if row else None


def snapshot(registry, execution_ref, conn=None):
    """One read snapshot, also used inside the commit's BEGIN IMMEDIATE CAS."""
    if not isinstance(execution_ref, str) or not re.fullmatch(r'exec_[0-9a-f]{32}', execution_ref):
        deny('INVALID_REF')
    if conn is None:
        with contextlib.closing(registry._connect()) as opened:
            opened.execute('BEGIN')
            return snapshot(registry, execution_ref, opened)
    execution = _row(conn, 'executions', 'execution_ref', execution_ref)
    history = _row(conn, 'execution_history', 'execution_ref', execution_ref)
    record = execution or history
    if not record or (execution and history):
        deny('EXECUTION_IDENTITY')
    task_id = record['task_id']
    prepared_ref = 'prepared_' + execution_ref.removeprefix('exec_')
    rows = dict(execution=execution, history=history,
        task=_row(conn, 'tasks', 'task_id', task_id),
        prepared=_row(conn, 'prepared_executions', 'prepared_execution_ref', prepared_ref),
        owner=_row(conn, 'clinx_execution_owners', 'execution_ref', execution_ref),
        lease=_row(conn, 'worktree_leases', 'worktree_key', record['worktree_key']),
        binding=_row(conn, 'conversation_bindings', 'task_id', task_id),
        liveness=_row(conn, 'execution_liveness', 'execution_ref', execution_ref),
        audit=_row(conn, 'execution_recovery_audit', 'execution_ref', execution_ref))
    rows['effects'] = {}
    for table in ('clinx_completion_handoffs', 'execution_results', 'host_executions',
                  'host_tool_deliveries', 'result_ingestions', 'production_resource_leases'):
        exists = conn.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone()
        rows['effects'][table] = bool(exists and conn.execute(
            f'SELECT 1 FROM {table} WHERE execution_ref=? LIMIT 1', (execution_ref,)).fetchone())
    historical = conn.execute('SELECT * FROM execution_history WHERE task_id=? AND turn_id=?',
                             (task_id, rows['task']['turn_id'] if rows['task'] else None)).fetchall()
    rows['historical_turn'] = [dict(r) for r in historical]
    return rows


def identity(rows):
    e, p, o, b = rows['execution'] or rows['history'], rows['prepared'], rows['owner'], rows['binding']
    if not p:
        deny('PREPARED_OR_BINDING_MISSING')
    return dict(task_id=e['task_id'], execution_ref=e['execution_ref'],
        prepared_execution_ref=p['prepared_execution_ref'], thread_id=b['thread_id'] if b else None,
        request_hash=p['integrity_hash'], owner_instance=o['owner_instance'] if o else None,
        owner_pid=o['owner_pid'] if o else None, worktree_key=e['worktree_key'], acquired_at=e['acquired_at'])


def validate(registry, rows, *, live_callsite=False):
    e, t, p, b, lease = rows['execution'], rows['task'], rows['prepared'], rows['binding'], rows['lease']
    if not e or not t or not p or not lease or (not b and p['task_action'] != 'create'):
        deny('IDENTITY_MISSING')
    registry.verify_prepared_execution(p['prepared_execution_ref'])
    payload_keys = ('task_action', 'task_ref', 'host', 'project', 'title', 'summary', 'prompt',
                    'model', 'logical_model', 'resolved_executable_model', 'reasoning_effort',
                    'execution_mode', 'network_access', 'routing_identity_json', 'execution_policy_json')
    payload = {k: p[k] for k in payload_keys}
    payload['workspace_contract_json'] = p.get('workspace_contract_json', '{}')
    payload['network_access'] = bool(payload['network_access'])
    if registry._prepared_hash(registry._prepared_payload(**payload)) != p['integrity_hash']:
        deny('UNSEALED_PREPARATION')
    if rows['owner'] and (rows['owner']['prepared_execution_ref'] != p['prepared_execution_ref']
            or rows['owner']['execution_ref'] != e['execution_ref']):
        deny('OWNER_PREPARED_CONFLICT')
    if (e['turn_id'] is not None or t['codex_running'] or t['status'] != 'ACTIVE'
            or e['stage'] in {'COMPLETED', 'FAILED', 'BLOCKED', 'CANCELLED', 'STOPPED'}
            or e['execution_state'] not in PENDING or t['execution_state'] != e['execution_state']
            or p['status'] not in {'RUNNING', 'PREPARED'}
            or p['task_ref'] not in {None, e['task_id']}
            or (p['task_ref'] is None and p['task_action'] != 'create')
            or any(p['resulting_' + name] is not None for name in
                   ('task_id', 'thread_id', 'turn_id', 'execution_ref'))):
        deny('DISPATCH_OR_TASK_CONFLICT')
    from execution_semantics import parse_routing_identity
    route = parse_routing_identity(e['routing_identity_json'])
    policy_matches = p['execution_policy_json'] == e['execution_policy_json']
    if live_callsite and route and e['execution_policy_json'] == t['execution_policy_json'] == '{}':
        from execution_policy import legacy_policy_for_route
        policy_matches = p['execution_policy_json'] == legacy_policy_for_route(route).to_json()
    if (route is None or (b and route.conversation.binding != b['thread_id'])
            or (not b and route.conversation.status != 'UNBOUND')
            or (b and b['task_id'] != e['task_id']) or lease['task_id'] != e['task_id']
            or lease['execution_ref'] != e['execution_ref']
            or lease['worktree_key'] != registry.worktree_key(
                host=t['host'], cwd=t['cwd'], repository_origin=t['repository_origin'])
            or t['routing_identity_json'] != e['routing_identity_json']
            or p['routing_identity_json'] != e['routing_identity_json']
            or t['execution_policy_json'] != e['execution_policy_json'] or not policy_matches
            or p['host'].casefold() != t['host'].casefold() or p['project'] != t['project_alias']):
        deny('ROUTE_OR_LEASE_CONFLICT')
    live = rows['liveness']
    if any(rows['effects'].values()) or (live and (live['turn_id'] or live['provider_liveness'] == 'LIVE'
            or (b and live['thread_id'] != b['thread_id']))):
        deny('TURN_OR_SIDE_EFFECT_PRESENT')
    acquired = timestamp(e['acquired_at'])
    if (not 0 <= timestamp(lease['acquired_at']) - acquired <= 1
            or timestamp(p['created_at']) > acquired or lease['stage'] not in {'CLAIMED', 'RECOVERY_REQUIRED'}):
        deny('ACQUISITION_CONFLICT')
    if t['turn_id'] is not None:
        past = rows['historical_turn']
        if (len(past) != 1 or past[0]['stage'] != 'COMPLETED'
                or past[0]['execution_state'] != 'COMPLETED' or not past[0]['released_at']
                or not timestamp(past[0]['acquired_at']) <= timestamp(past[0]['released_at']) < acquired
                or past[0]['routing_identity_json'] != e['routing_identity_json']):
            deny('HISTORICAL_TURN_UNPROVEN')


def verify_legacy_source(source):
    root = Path(source)
    if not root.is_absolute():
        deny('SOURCE_UNTRUSTED')
    try:
        hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in LEGACY_SOURCE}
    except OSError:
        deny('SOURCE_UNAVAILABLE')
    if hashes != LEGACY_SOURCE:
        deny('SOURCE_UNREVIEWED')
    return hashes


def legacy_proof(cfg, registry, execution_ref, expected):
    rows = snapshot(registry, execution_ref)
    if not isinstance(expected, dict) or set(expected) != set(EXPECTED_FIELDS) or identity(rows) != expected:
        deny('EXPECTED_IDENTITY_CONFLICT')
    if rows['audit']:
        return validate_replay(registry, rows, expected)
    validate(registry, rows)
    e, p, o = rows['execution'], rows['prepared'], rows['owner']
    if not o or o['state'] != 'UNKNOWN' or p['status'] != 'PREPARED' or p['task_action'] != 'continue':
        deny('OWNER_OR_PREPARED_CONFLICT')
    if (e['execution_state'] not in {'RECOVERY_REQUIRED', 'TRANSPORT_UNCERTAIN', 'CANCELLATION_PENDING'}
            or e['stage'] not in {e['execution_state'], 'dispatch'}
            or not isinstance(o['owner_pid'], int) or o['owner_pid'] <= 0
            or not isinstance(o['owner_instance'], str) or not re.fullmatch(r'[0-9a-f]{32}', o['owner_instance'])):
        deny('OWNER_OR_STATE_UNPROVEN')
    from execution_policy import SANDBOX_WORKSPACE, parse_execution_policy
    policy = parse_execution_policy(p['execution_policy_json'])
    if (policy is None or policy.execution_surface != SANDBOX_WORKSPACE or policy.production_mutation_intent
            or json.loads(p['execution_policy_json']).get('business_action_authority') is not False
            or policy.required_capabilities or policy.operation_classes):
        deny('LEGACY_POLICY_UNREVIEWED')
    source_hashes = verify_legacy_source(o['source_path'])
    try:
        response = json.loads(o['response_json'])
        details = response['failure_details']
        correlation = details['correlation']
        required = dict(request_ref=p['prepared_execution_ref'], tool='clinx_start_execution',
                        task_ref=e['task_id'], execution_ref=e['execution_ref'], request_hash=p['integrity_hash'])
        if (correlation != required or details['failure_code'] != 'MODEL_CAPABILITY_UNAVAILABLE'
                or details['failure_stage'] != 'MODEL_RESOLUTION' or details['failure_source'] != 'PROVIDER'
                or details['server_received'] is not True or details['retry_allowed'] is not False
                or response['error'] != f"model {p['model']!r} is unsupported or ambiguous"
                or details['failure_reason'] != response['error']
                or not timestamp(p['created_at']) <= o['registered_at'] <= timestamp(e['acquired_at']) <= o['updated_at']):
            deny('OWNER_RESPONSE_UNPROVEN')
    except (KeyError, TypeError, ValueError):
        deny('OWNER_RESPONSE_UNPROVEN')
    if e['execution_state'] == 'CANCELLATION_PENDING' and (e['failure_stage'] != 'cancel'
            or e['failure_code'] != 'PROVIDER_UNAVAILABLE'
            or e['failure_evidence'] != 'turn/interrupt failed (code -32600): no active turn to interrupt'):
        deny('CANCELLATION_EVIDENCE_CONFLICT')
    from native_provider import observe_thread
    observed = observe_thread(cfg, expected['thread_id'])
    peers = observed['observations']
    if (observed['ownership_conflict'] or not peers or any(
            r.get('thread_id') != expected['thread_id'] or r.get('state') not in {'idle', 'notLoaded', 'unloaded'}
            or r.get('turn_status') != 'completed' or not r.get('turn_id') for r in peers)
            or len({r['turn_id'] for r in peers}) != 1):
        deny('PROVIDER_CONFLICT')
    return dict(kind=KIND, snapshot=rows, identity=expected, original_error=response['error'], observed_at=time.time(),
                source_hashes=source_hashes, observations=peers)


def current_proof(registry, execution_ref, error):
    if not getattr(error, 'turn_not_dispatched', False):
        deny('CALLSITE_UNPROVEN')
    rows = snapshot(registry, execution_ref)
    validate(registry, rows, live_callsite=True)
    if rows['execution']['execution_state'] not in {'CLAIMED', 'DISPATCHING'}:
        deny('CALLSITE_STATE_CONFLICT')
    if rows['owner'] and rows['owner']['state'] != 'CLAIMED':
        deny('OWNER_CONFLICT')
    return dict(kind=KIND, snapshot=rows, identity=identity(rows), original_error=str(error), observed_at=time.time(),
                source='LIVE_RESOLVER_CALLSITE', observations=[])


def replay_proof(rows, expected):
    try:
        proof = json.loads(rows['audit']['owner_evidence_json'])
        if proof['kind'] != KIND or proof['identity'] != expected:
            deny('AUDIT_IDENTITY_CONFLICT')
    except (KeyError, TypeError, ValueError):
        deny('AUDIT_IDENTITY_CONFLICT')
    return proof


def validate_replay(registry, rows, expected):
    proof = replay_proof(rows, expected)
    prior = proof['snapshot']
    e, old, p = rows['execution'] or rows['history'], prior['execution'], rows['prepared']
    if (e['stage'] != 'BLOCKED' or e['execution_state'] != 'BLOCKED'
            or e['failure_code'] != 'TURN_NOT_DISPATCHED' or e['turn_id'] is not None
            or any(e[k] != old[k] for k in ('routing_identity_json', 'execution_policy_json',
                'task_id', 'execution_ref', 'worktree_key', 'acquired_at'))
            or rows['binding'] != prior['binding']
            or any(rows['task'][k] != prior['task'][k] for k in ('turn_id', 'host', 'cwd',
                'repository_origin', 'project_alias', 'workspace_alias', 'routing_identity_json', 'execution_policy_json'))
            or rows['task']['execution_state'] != 'BLOCKED' or rows['task']['codex_running']
            or not p or p['status'] != 'FAILED'
            or any(p[k] != v for k, v in prior['prepared'].items() if k not in {'status', 'updated_at'})
            or (proof.get('source_hashes') and rows['owner'] != prior['owner'])
            or any(rows['effects'].values())
            or (rows['lease'] is not None and rows['lease'] != prior['lease'])):
        deny('REPLAY_CONFLICT')
    registry.verify_prepared_execution(expected['prepared_execution_ref'])
    return proof


def recover(cfg, registry, execution_ref, expected, *, check_only=False):
    """Explicit maintenance entry; the JSON argument supplies identity, not proof."""
    from completion_runtime import finalization_lock
    from m9_integration import ExecutionFinalizer
    with contextlib.nullcontext() if check_only else finalization_lock(registry, execution_ref):
        proof = legacy_proof(cfg, registry, execution_ref, expected)
        if check_only:
            return dict(qualified=True, identity=expected, recovery_kind=KIND,
                        source_hashes=proof.get('source_hashes'), observations=proof.get('observations'),
                        read_only=True, execution_started=False)
        return ExecutionFinalizer(registry).finalize_undispatched_model_failure(execution_ref, proof)
