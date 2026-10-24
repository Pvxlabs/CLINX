"""Recover a lost native input ACK by exact readback, never by replay."""
import dataclasses
import json
import time
import uuid

from completion_runtime import CompletionIdentity, CompletionRuntime, serialized_execution
from execution_policy import HOST_EXECUTOR, parse_execution_policy
from task_registry import TaskRegistryError, _now


def read_input(cfg, thread_id, execution_ref, prompt, cwd):
    from native_provider import existing_client
    endpoint = cfg.app_server.native_socket
    if not endpoint:
        raise TaskRegistryError('NATIVE_INPUT_RECOVERY_DESKTOP_UNAVAILABLE')
    client_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
        'clinx-native-input:' + thread_id + ':' + execution_ref))
    matches = []
    # Only the configured Desktop endpoint; no subscribe, resume or start RPC.
    with existing_client(endpoint, 5) as client:
        client.initialize(client_name=cfg.app_server.client_name,
            client_title=cfg.app_server.client_title, client_version=cfg.app_server.client_version)
        thread = client.thread_read(thread_id)
        if thread.get('id') != thread_id or thread.get('cwd') != cwd:
            raise TaskRegistryError('NATIVE_INPUT_RECOVERY_THREAD_CONFLICT')
        turns = client.thread_turns_list(thread_id, limit=5, sort_direction='desc', items_view='notLoaded')
        for turn in turns['data']:
            cursor, seen = None, set()
            for _ in range(5):
                page = client.thread_items_list(thread_id, turn_id=turn['id'], limit=100,
                    sort_direction='asc', **({'cursor': cursor} if cursor else {}))
                for row in page['data']:
                    item = row.get('item', {})
                    if (row.get('turnId') == turn['id'] and item.get('type') == 'userMessage'
                            and item.get('clientId') == client_id):
                        content = item.get('content')
                        if (not isinstance(item.get('id'), str) or not item['id']
                                or not isinstance(content, list) or len(content) != 1
                                or content[0].get('type') != 'text' or content[0].get('text') != prompt):
                            raise TaskRegistryError('NATIVE_INPUT_RECOVERY_CONTENT_CONFLICT')
                        matches.append(dict(mode='NATIVE_INPUT_FORWARDING', exclusive_writer=False,
                            thread_id=thread_id, turn_id=turn['id'], client_message_id=client_id,
                            native_item_id=item['id'], input_confirmed=True))
                cursor = page.get('nextCursor')
                if not cursor:
                    break
                if cursor in seen:
                    raise TaskRegistryError('NATIVE_INPUT_RECOVERY_PAGINATION_CONFLICT')
                seen.add(cursor)
            else:
                # A bounded incomplete scan cannot prove unique attribution.
                raise TaskRegistryError('NATIVE_INPUT_RECOVERY_SCAN_INCOMPLETE')
    if len(matches) != 1:
        raise TaskRegistryError('NATIVE_INPUT_RECOVERY_IDENTITY_UNPROVEN')
    return matches[0]


@serialized_execution
def recover(dispatcher, execution_ref):
    registry = dispatcher.tasks
    prepared_ref = 'prepared_' + execution_ref.removeprefix('exec_')
    prepared = registry.verify_prepared_execution(prepared_ref)
    policy = parse_execution_policy(prepared.execution_policy_json)
    active = registry.get_active_execution(execution_ref)
    if (not active or not policy or policy.execution_surface == HOST_EXECUTOR
            or prepared.status != 'RUNNING' or prepared.task_ref != active['task_id']
            or active.get('execution_owned_turn') is not None
            or active.get('execution_owned_state') not in {'RECOVERY_REQUIRED', 'TRANSPORT_UNCERTAIN'}
            or any((prepared.resulting_turn_id, prepared.resulting_thread_id,
                    prepared.resulting_task_id, prepared.resulting_execution_ref))):
        raise TaskRegistryError('NATIVE_INPUT_RECOVERY_OWNER_CONFLICT')
    task = registry.get_task(active['task_id'])
    binding = registry.get_binding(task.task_id)
    route = registry.get_execution_routing_identity(execution_ref)
    workspace = json.loads(prepared.workspace_contract_json)
    if (not binding or not route
            or route.conversation.binding != binding.thread_id
            or active['routing_identity_json'] != prepared.routing_identity_json
            or active['execution_policy_json'] != prepared.execution_policy_json
            or route.workspace.worktree_key != registry.worktree_key(
                host=task.host, cwd=task.cwd, repository_origin=task.repository_origin)
            or (workspace and (workspace['cwd'] != task.cwd or workspace['branch'] != task.branch))):
        raise TaskRegistryError('NATIVE_INPUT_RECOVERY_ROUTE_CONFLICT')
    receipt = read_input(dispatcher.cfg, binding.thread_id, execution_ref, prepared.prompt, task.cwd)
    # An explicit repair enrolls only this execution. It must not start a second
    # background observer over unrelated tasks owned by the live service.
    runtime = getattr(dispatcher, '_completion_runtime', None) or CompletionRuntime(
        registry, dispatcher.reconcile_execution,
        owner_instance=getattr(dispatcher, 'completion_owner_instance', None))
    arguments = dict(execution_ref=execution_ref, prepared_ref=prepared_ref,
        task_snapshot=dataclasses.asdict(task), binding_snapshot=dataclasses.asdict(binding),
        integrity_hash=prepared.integrity_hash, receipt=receipt)
    result = commit_receipt(registry, **arguments)
    # The ordinary completion observer/finalizer remains the only terminal authority.
    runtime.register(CompletionIdentity(execution_ref, task.task_id, binding.thread_id, receipt['turn_id']))
    return {**result, 'completion': dispatcher.reconcile_execution(execution_ref, reclaim_stale=False)}


def commit_receipt(registry, *, execution_ref, prepared_ref, task_snapshot,
                   binding_snapshot, integrity_hash, receipt):
    """Commit exact native input evidence in the existing compatibility transaction."""
    from task_registry import ConversationBinding, TaskRecord
    task = TaskRecord(**task_snapshot)
    binding = ConversationBinding(**binding_snapshot)
    stamp = _now()
    with registry._write_connection(atomic=True) as conn:
        current = conn.execute('SELECT * FROM executions WHERE task_id=?', (task.task_id,)).fetchone()
        current_task = conn.execute('SELECT * FROM tasks WHERE task_id=?', (task.task_id,)).fetchone()
        sealed = conn.execute('SELECT * FROM prepared_executions WHERE prepared_execution_ref=?', (prepared_ref,)).fetchone()
        lease = conn.execute('SELECT * FROM worktree_leases WHERE task_id=? AND execution_ref=?',
                             (task.task_id, execution_ref)).fetchone()
        has_legacy_owner = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='clinx_execution_owners'"
        ).fetchone() is not None
        owner = (conn.execute('SELECT * FROM clinx_execution_owners WHERE execution_ref=?',
                              (execution_ref,)).fetchone() if has_legacy_owner else None)
        bound = conn.execute('SELECT * FROM conversation_bindings WHERE task_id=?', (task.task_id,)).fetchone()
        if (not current or not current_task or not sealed or not lease or not bound
                or current['execution_ref'] != execution_ref or current['turn_id'] is not None
                or current['execution_state'] not in {'RECOVERY_REQUIRED', 'TRANSPORT_UNCERTAIN'}
                or current_task['turn_id'] != task.turn_id or current_task['cwd'] != task.cwd
                or current_task['routing_identity_json'] != task.routing_identity_json
                or current_task['execution_policy_json'] != task.execution_policy_json
                or bound['thread_id'] != binding.thread_id or sealed['status'] != 'RUNNING'
                or sealed['integrity_hash'] != integrity_hash
                or current['routing_identity_json'] != sealed['routing_identity_json']
                or current['execution_policy_json'] != sealed['execution_policy_json']
                or lease['worktree_key'] != current['worktree_key']
                or (has_legacy_owner and (not owner or owner['prepared_execution_ref'] != prepared_ref
                                         or owner['state'] != 'UNKNOWN'))):
            raise TaskRegistryError('NATIVE_INPUT_RECOVERY_OWNER_CHANGED')
        if conn.execute('SELECT 1 FROM host_executions WHERE execution_ref=?', (execution_ref,)).fetchone():
            raise TaskRegistryError('NATIVE_INPUT_RECOVERY_HOST_EFFECT_CONFLICT')
        audit = dict(receipt=receipt, prior_execution=dict(current),
                     prior_owner=dict(owner) if owner is not None else None,
                     source='DESKTOP_EXACT_INPUT_READBACK', replay_performed=False)
        conn.execute('''INSERT INTO context_checkpoints
            (checkpoint_id,task_id,execution_id,thread_id,turn_id,timestamp,prompt_summary,
             result_summary,changed_files,validation_summary,blockers,next_state,source,provenance)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            ('checkpoint_' + uuid.uuid4().hex, task.task_id, execution_ref, binding.thread_id,
             receipt['turn_id'], stamp, 'Original authorized native input',
             'Recovered lost input ACK without replay', '', 'Exact clientId, prompt, thread and item',
             '', 'Reconcile exact native turn', 'NATIVE_INPUT_FORWARDING',
             json.dumps({**receipt, 'recovery': audit}, sort_keys=True)))
        conn.execute('UPDATE executions SET turn_id=? WHERE execution_ref=?', (receipt['turn_id'], execution_ref))
        conn.execute('UPDATE tasks SET turn_id=?,updated_at=? WHERE task_id=?', (receipt['turn_id'], stamp, task.task_id))
        conn.execute('''UPDATE prepared_executions SET status='DISPATCHED',updated_at=?,
            resulting_task_id=?,resulting_thread_id=?,resulting_turn_id=?,resulting_execution_ref=?
            WHERE prepared_execution_ref=?''',
            (stamp, task.task_id, binding.thread_id, receipt['turn_id'], execution_ref, prepared_ref))
        result = dict(execution_started=True, execution_ref=execution_ref, task_ref=task.task_id,
            prepared_execution_ref=prepared_ref, native_forwarding=receipt,
            dispatch_status='RECONCILED_NATIVE_INPUT', reconciled=True, replay_performed=False)
        if owner is not None:
            conn.execute("UPDATE clinx_execution_owners SET state='DISPATCHED',updated_at=?,response_json=? WHERE execution_ref=?",
                         (time.time(), json.dumps({'result': result}), execution_ref))
        conn.execute('''INSERT INTO clinx_completion_handoffs
            (execution_ref,task_id,thread_id,turn_id,state,updated_at) VALUES (?,?,?,?,'PENDING',?)''',
            (execution_ref, task.task_id, binding.thread_id, receipt['turn_id'], time.time()))
    return result


# Immutable compatibility readback; the Python Owner service is retired.
def owner_record(registry, execution_ref):
    """Read-only projection; never create tables or adopt an execution."""
    with registry._connect() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='clinx_execution_owners'").fetchone():
            return None
        row = conn.execute('SELECT * FROM clinx_execution_owners WHERE execution_ref=?',
                           (execution_ref,)).fetchone()
    if row is None:
        return None
    result = {k: row[k] for k in row.keys() if k != 'response_json'}
    record = registry.get_execution_record(execution_ref)
    result['execution_state'] = record.get('execution_owned_state') if record else None
    return result


def native_rejection_recovery(registry, execution_ref):
    """Expose the original retry path without modifying historical evidence."""
    with registry._connect() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='clinx_execution_owners'").fetchone():
            return {}
        prior = conn.execute('SELECT * FROM clinx_execution_owners WHERE execution_ref=?',
                             (execution_ref,)).fetchone()
        if prior is None:
            return {}
        prepared = registry.verify_prepared_execution(prior['prepared_execution_ref'])
        if not _native_rejection_retryable(conn, prior, prepared):
            return {}
    return {'retry_allowed': True, 'retry_required': True,
            'recovery_action': 'RETRY_ORIGINAL_PREPARATION_AFTER_ROUTE_REPAIR',
            'prepared_request': {'prepared_execution_ref': prepared.prepared_execution_ref,
                'recovery_instructions': 'Start this original prepared request directly. No exclusive Desktop handoff, new task or new chat is required. The prior refusal sent no native input.'}}


def _native_rejection_retryable(conn, prior, prepared):
    """Reevaluate a proved pre-RPC refusal, preserving its attempt evidence.

    This is not recovery of a lost start ACK. The old immutable preparation
    must be back in PREPARED, with no Turn, effect, completion or live lease.
    """
    if (prior is None or getattr(prepared, 'status', None) != 'PREPARED'
            or not prior['response_json']):
        return False
    response = json.loads(prior['response_json'])
    if response.get('failure_details', {}).get('failure_code') != 'NATIVE_SHARED_WRITER_NOT_QUALIFIED':
        return False
    ref = prior['execution_ref']
    old = conn.execute('SELECT * FROM execution_history WHERE execution_ref=?', (ref,)).fetchone()
    if old is None:
        return False
    from execution_policy import legacy_policy_for_route, parse_execution_policy
    from execution_semantics import parse_routing_identity
    old_policy = parse_execution_policy(old['execution_policy_json']) or legacy_policy_for_route(
        parse_routing_identity(old['routing_identity_json']))
    sealed_policy = parse_execution_policy(prepared.execution_policy_json)
    if (old is None or old['task_id'] != prepared.task_ref or old['stage'] != 'BLOCKED'
            or old['execution_state'] != 'BLOCKED' or old['turn_id'] is not None
            or old['failure_code'] != 'NATIVE_SHARED_WRITER_NOT_QUALIFIED'
            or old['routing_identity_json'] != prepared.routing_identity_json
            or old_policy != sealed_policy):
        return False
    for table, column, value in (
            ('executions', 'task_id', old['task_id']),
            ('worktree_leases', 'task_id', old['task_id']),
            ('execution_results', 'execution_ref', ref),
            ('host_executions', 'execution_ref', ref),
            ('host_tool_deliveries', 'execution_ref', ref),
            ('clinx_completion_handoffs', 'execution_ref', ref)):
        if (table in {'host_tool_deliveries', 'clinx_completion_handoffs'} and not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()):
            continue
        if conn.execute(f'SELECT 1 FROM {table} WHERE {column}=? LIMIT 1', (value,)).fetchone():
            return False
    binding = conn.execute('SELECT thread_id FROM conversation_bindings WHERE task_id=?',
                           (old['task_id'],)).fetchone()
    if binding is None:
        return False
    return True
