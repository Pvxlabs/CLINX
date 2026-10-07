"""Read the existing preparation records without applying or consuming them."""
from __future__ import annotations

import hashlib
import json
import re

from execution_policy import legacy_policy_for_route, parse_execution_policy
from execution_semantics import parse_routing_identity
from task_registry import TaskRegistryError, _now, policy_identity


def request_hash(request):
    return hashlib.sha256(json.dumps(dict(request), sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def check_review(actual_hash, task_ref, *, expected_request_hash=None, expected_task_ref=None):
    if expected_task_ref is not None and expected_task_ref != task_ref:
        raise TaskRegistryError('PREPARED_REQUEST_TARGET_MISMATCH')
    if expected_request_hash is not None and expected_request_hash != actual_hash:
        raise TaskRegistryError('PREPARED_REQUEST_CONTENT_MISMATCH')


def policy_dict(value, route):
    policy = parse_execution_policy(value)
    parsed = parse_routing_identity(route)
    if policy is None and parsed:
        policy = legacy_policy_for_route(parsed)
    return policy.as_dict() if policy else None


def policy_difference(previous, proposed):
    previous, proposed = previous or {}, proposed or {}
    changes = {}
    for key in sorted(previous.keys() | proposed.keys()):
        if previous.get(key) != proposed.get(key):
            changes[key] = {'previous': previous.get(key), 'proposed': proposed.get(key)}
    return changes


def read_prepared_request(registry, request_ref):
    if not isinstance(request_ref, str) or not re.fullmatch(r'(prepared|reauth)_[0-9a-f]{32}', request_ref):
        raise TaskRegistryError('PREPARED_REQUEST_INVALID_REF')
    with registry._connect() as conn:
        conn.execute('BEGIN')
        if request_ref.startswith('reauth_'):
            row = conn.execute('SELECT * FROM policy_reauthorizations WHERE reauthorization_ref=?',
                               (request_ref,)).fetchone()
            if row is None:
                raise TaskRegistryError('PREPARED_REQUEST_NOT_FOUND')
            task = conn.execute('SELECT * FROM tasks WHERE task_id=?', (row['task_id'],)).fetchone()
            identity = registry._task_policy_identity(conn, task)
            applied = conn.execute('SELECT version,applied_at FROM task_policy_versions WHERE reauthorization_ref=?',
                                   (request_ref,)).fetchone()
            current = policy_dict(task['execution_policy_json'], task['routing_identity_json'])
            proposed = policy_dict(row['new_policy_json'], row['new_route_json'])
            previous = policy_dict(row['previous_policy_json'], row['previous_route_json'])
            versions = [0, *[r[0] for r in conn.execute('SELECT version FROM task_policy_versions WHERE task_id=?', (row['task_id'],))]]
            baseline_version = next((v for v in versions if policy_identity(
                row['previous_policy_json'], row['previous_route_json'], v) == row['expected_policy_hash']), None)
            state = ('APPLIED' if applied else 'EXPIRED' if row['expires_at'] < _now()
                     else 'INVALIDATED' if identity['policy_hash'] != row['expected_policy_hash'] else 'PREPARED')
            return {'request_ref': request_ref, 'request_type': 'POLICY_REAUTHORIZATION',
                    'request_hash': request_hash(row), 'request_state': state,
                    'task_ref': row['task_id'], 'target': {'host': task['host'],
                    'project': task['project_alias'], 'repository_root': task['cwd']},
                    'created_at': row['created_at'], 'expires_at': row['expires_at'],
                    'baseline_policy_hash': row['expected_policy_hash'],
                    'baseline_policy_version': baseline_version,
                    'current_policy_hash': identity['policy_hash'], 'current_policy_version': identity['policy_version'],
                    'previous_policy': previous, 'current_policy': current, 'proposed_policy': proposed,
                    'policy_difference': policy_difference(previous, proposed),
                    'previous_network_access': bool(parse_routing_identity(row['previous_route_json']).network_policy.network_access),
                    'current_network_access': bool(parse_routing_identity(task['routing_identity_json']).network_policy.network_access),
                    'network_access': bool(parse_routing_identity(row['new_route_json']).network_policy.network_access),
                    'applied_policy_version': applied['version'] if applied else None,
                    'applied_at': applied['applied_at'] if applied else None,
                    'execution_started': False, 'read_only': True}
        prepared = registry.verify_prepared_execution(request_ref)
        # Stable ref is allocated before dispatch. Never substitute a task's latest execution/turn.
        execution_ref = 'exec_' + request_ref.removeprefix('prepared_')
        execution = registry.get_execution_record(execution_ref)
        route = parse_routing_identity(prepared.routing_identity_json)
        proposed = policy_dict(prepared.execution_policy_json, prepared.routing_identity_json)
        task_ref = prepared.task_ref or prepared.resulting_task_id
        task = conn.execute('SELECT * FROM tasks WHERE task_id=?', (task_ref,)).fetchone() if task_ref else None
        current = policy_dict(task['execution_policy_json'], task['routing_identity_json']) if task else None
        identity = registry._task_policy_identity(conn, task) if task else None
        stale = bool(task and prepared.status != 'DISPATCHED' and (
            (task['execution_policy_json'] != '{}' and task['execution_policy_json'] != prepared.execution_policy_json)
            or parse_routing_identity(task['routing_identity_json']) != route))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='clinx_execution_owners'")}
        owner = conn.execute('SELECT state FROM clinx_execution_owners WHERE execution_ref=?',
                             (execution_ref,)).fetchone() if tables else None
        return {'request_ref': request_ref, 'request_type': 'EXECUTION',
                'request_hash': prepared.integrity_hash,
                'request_state': 'INVALIDATED' if stale else prepared.status,
                'task_action': prepared.task_action, 'task_ref': task_ref,
                'target': {'host': prepared.host, 'project': prepared.project,
                           'repository_root': task['cwd'] if task else None},
                'routing_identity': route.public_dict() if route else None,
                'created_at': prepared.created_at, 'expires_at': None,
                'expiry_semantics': 'NO_TIME_EXPIRY; policy/route seal checked before dispatch',
                'current_policy_hash': identity['policy_hash'] if identity else None,
                'current_policy_version': identity['policy_version'] if identity else None,
                'current_policy': current, 'proposed_policy': proposed,
                'policy_difference': policy_difference(current, proposed),
                'network_access': bool(prepared.network_access),
                'prompt_sha256': hashlib.sha256(prepared.prompt.encode()).hexdigest(),
                'execution_ref': execution_ref, 'execution_present': execution is not None,
                'execution_state': execution.get('execution_owned_state') if execution else None,
                'execution_stage': execution.get('stage') if execution else None,
                'thread_id': prepared.resulting_thread_id or (parse_routing_identity(execution['routing_identity_json']).conversation.binding if execution and parse_routing_identity(execution['routing_identity_json']) else None),
                'turn_id': prepared.resulting_turn_id or (execution.get('execution_owned_turn') if execution else None),
                'execution_owner_state': owner['state'] if owner else None,
                'provider_running': 'NOT_OBSERVED', 'read_only': True}
