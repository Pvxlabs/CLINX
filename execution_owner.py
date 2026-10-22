"""Keep canonical dispatch and its Provider connection outside MCP request lifetime.

This local service accepts only an already sealed preparation. It delegates all
authority, leases, dispatch and completion to the existing integration. It never
resumes an uncertain turn or replays a Host command on restart.
"""
from __future__ import annotations

import fcntl
import dataclasses
import functools
import inspect
import hashlib
import json
import os
import re
import signal
import socket
import socketserver
import stat
import struct
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from task_registry import TaskRegistryError

MAX_MESSAGE = 256 * 1024

# Orchestration stays in this existing process. These are domain entrypoints,
# never a registry/SQL proxy. Their original transaction and external-I/O
# boundaries remain intact; only deliver/commit/read_commit issue receipts.
INTEGRATION_CALLS = frozenset({
    'adopt_conversation', 'prepare_execution', 'prepare_policy_reauthorization',
    'apply_policy_reauthorization', 'register_derived_git_target',
    'revoke_derived_git_target', 'reconcile_host_delivery', 'execute',
    'cancel_execution',
})
DISPATCHER_CALLS = frozenset({
    'dispatch', 'adopt_existing_conversation', 'adopt_or_reuse_existing_conversation',
    'task_action', 'migrate_provider_thread', 'cancel_execution',
    'reconcile_execution', 'recover_execution_completion',
})

# Exact existing execution writes, not a registry/SQL RPC. Metadata without
# an execution resource remains in the original Owner transaction paths.
EXECUTION_WRITES = frozenset({
    'set_execution_state', 'set_execution_models', 'bind_conversation',
    'bind_routing_identity', 'update_execution_routing_identity',
    'bind_dynamic_tool_upgrade', 'bind_reauthorized_provider',
    'request_cancellation', 'mark_cancellation_pending', 'finalize_cancellation',
    'confirm_pre_turn_absence', 'reconcile_undispatched_model_failure',
    'reconcile_terminal', 'reconcile_host_delivery_failure',
    'reconcile_orphaned_terminal', 'record_execution_result',
    'begin_host_execution', 'complete_host_execution',
    'request_host_execution_cancellation', 'release_execution',
})
EXECUTION_PROJECTIONS = frozenset({
    'update_metadata', 'update_routing_identity', 'set_status', 'reset_execution',
    'mark_execution_result_writeback',
})


def owner_caller(domain):
    """Forward a finite domain entrypoint when using a read-only registry."""
    def decorate(method):
        signature = inspect.signature(method)

        @functools.wraps(method)
        def call(self, *args, **kwargs):
            registry = getattr(self, 'registry', None) or getattr(self, 'tasks', None)
            if not getattr(registry, 'read_only', False):
                return method(self, *args, **kwargs)
            bound = signature.bind(self, *args, **kwargs)
            arguments = dict(bound.arguments)
            arguments.pop('self')
            for key in ('routing_identity', 'execution_policy'):
                if hasattr(arguments.get(key), 'to_json'):
                    arguments[key] = arguments[key].to_json()
            # LinearTaskIndex contains a client and a registry; transport only
            # the intent, reconstructed with the Owner's existing dependencies.
            if 'task_index' in arguments:
                arguments['task_index'] = arguments['task_index'] is not None
            result = ExecutionOwnerClient(registry).control(domain, method.__name__, arguments)
            if domain == 'dispatcher':
                from bridge import DispatchResult
                from task_registry import ConversationBinding, TaskIndexRecord, TaskRecord
                if method.__name__ == 'dispatch':
                    self.last_task_id = result.get('task_id')
                    return DispatchResult(**result)
                if method.__name__ == 'migrate_provider_thread':
                    return ConversationBinding(**result)
                if method.__name__ in {'adopt_existing_conversation', 'adopt_or_reuse_existing_conversation'}:
                    return (TaskRecord(**result[0]), ConversationBinding(**result[1]),
                            TaskIndexRecord(**result[2]) if result[2] else None, *result[3:])
            return result
        return call
    return decorate


def _wire_value(value):
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError('unsupported Owner value')


class ExecutionOwnerError(TaskRegistryError):
    def __init__(self, message, *, details=None):
        super().__init__(message)
        self.details = details or {}


def configured_authority(registry):
    """Load only the existing shared authority; never create/migrate tables."""
    from authority_mapping import SharedAuthority

    with registry._connect() as conn:
        for table in ('authority_schema', 'authority_mapping_schema'):
            if (not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                    or [row[0] for row in conn.execute('SELECT version FROM ' + table)] != [1]):
                raise ExecutionOwnerError('EXECUTION_OWNER_SHARED_AUTHORITY_SCHEMA_REQUIRED')
    return SharedAuthority(registry.path, registry=registry)


def socket_path(registry):
    configured = os.environ.get('CLINX_OWNER_SOCKET')
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise ExecutionOwnerError('EXECUTION_OWNER_ABSOLUTE_ENDPOINT_REQUIRED')
        return path
    database = str(Path(registry.path).resolve())
    # Never require changing permissions on an existing shared task-DB
    # directory. Keep the socket/lock in a separate same-user private directory.
    # Hash the full canonical DB identity to also bound AF_UNIX path length.
    key = hashlib.sha256(database.encode()).hexdigest()[:32]
    runtime = Path('/run/user') / str(os.getuid())
    parent = runtime if runtime.is_dir() else Path(tempfile.gettempdir())
    return parent / ('clinx-owner-' + str(os.getuid())) / (key + '.sock')


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
        if not ExecutionOwner._native_rejection_retryable(conn, prior, prepared):
            return {}
    return {'retry_allowed': True, 'retry_required': True,
            'recovery_action': 'RETRY_ORIGINAL_PREPARATION_AFTER_ROUTE_REPAIR',
            'prepared_request': {'prepared_execution_ref': prepared.prepared_execution_ref,
                'recovery_instructions': 'Start this original prepared request directly. No exclusive Desktop handoff, new task or new chat is required. The prior refusal sent no native input.'}}


class ExecutionOwnerClient:
    def __init__(self, registry, *, timeout=90):
        self.registry, self.timeout = registry, timeout

    def start(self, *, prepared_execution_ref, approved=False):
        if (approved is not True or not isinstance(prepared_execution_ref, str)
                or not re.fullmatch(r'prepared_[0-9a-f]{32}', prepared_execution_ref)):
            raise ExecutionOwnerError('EXECUTION_OWNER_INVALID_REQUEST')
        # Verify the seal locally as well as at the persistent owner. No claim
        # or Provider side effect occurs on this requesting process.
        self.registry.verify_prepared_execution(prepared_execution_ref)
        return self._request(dict(prepared_execution_ref=prepared_execution_ref, approved=True))

    def cancel(self, *, execution_ref):
        if not isinstance(execution_ref, str) or not re.fullmatch(r'exec_[0-9a-f]{32}', execution_ref):
            raise ExecutionOwnerError('EXECUTION_OWNER_INVALID_REQUEST')
        return self._request(dict(execution_ref=execution_ref, action='cancel'))

    def deliver(self, event):
        return self._request(dict(action='deliver', payload=event))

    def commit(self, delivery_id, resource_id, epoch, operation_id):
        return self._request(dict(action='commit', payload=dict(
            delivery_id=delivery_id, resource_id=resource_id, epoch=epoch,
            operation_id=operation_id)))

    def read_commit(self, resource_id, epoch, operation_id):
        return self._request(dict(action='read_commit', payload=dict(
            resource_id=resource_id, epoch=epoch, operation_id=operation_id)))

    def control(self, domain, operation, arguments):
        return self._request(dict(action='control', domain=domain,
                                  operation=operation, arguments=arguments))

    def _request(self, payload):
        payload = {**payload, 'database': str(Path(self.registry.path).resolve())}
        path = socket_path(self.registry)
        sent = False
        try:
            owner_uid = int(os.environ.get('CLINX_OWNER_UID', os.getuid()))
            info = path.lstat()
            directory = path.parent.lstat()
            forbidden = 0o077 if owner_uid == os.getuid() else 0o007
            if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != owner_uid
                    or info.st_mode & forbidden or not stat.S_ISDIR(directory.st_mode)
                    or directory.st_uid != owner_uid or directory.st_mode & 0o022):
                raise ExecutionOwnerError('EXECUTION_OWNER_ENDPOINT_UNTRUSTED')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(str(path))
                if hasattr(socket, 'SO_PEERCRED'):
                    _, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != owner_uid:
                        raise ExecutionOwnerError('EXECUTION_OWNER_PEER_UNTRUSTED')
                # A partial write is uncertain too; never silently fall back.
                data = json.dumps(payload, default=_wire_value).encode() + b'\n'
                if len(data) > MAX_MESSAGE:
                    raise ExecutionOwnerError('EXECUTION_OWNER_REQUEST_TOO_LARGE')
                sent = True
                sock.sendall(data)
                with sock.makefile('rb') as stream:
                    data = stream.readline(MAX_MESSAGE + 1)
                if not data.endswith(b'\n') or len(data) > MAX_MESSAGE:
                    raise ValueError('incomplete owner response')
                response = json.loads(data)
                if not isinstance(response, dict) or not ({'error', 'result'} & response.keys()):
                    raise ValueError('invalid owner response')
        except (OSError, ValueError) as exc:
            code = 'EXECUTION_OWNER_DELIVERY_UNKNOWN' if sent else 'EXECUTION_OWNER_UNAVAILABLE'
            raise ExecutionOwnerError(code + ': inspect the exact prepared execution; no fallback or replay') from exc
        if 'error' in response:
            raise ExecutionOwnerError(response['error'], details=response.get('failure_details'))
        return response['result']


class ExecutionOwner:
    def __init__(self, integration, *, authority=None):
        self.integration = integration
        self.registry = integration.registry
        # Explicitly reuse the already configured authority and its database.
        # Construction must not migrate/enroll history or allocate another owner.
        if authority is not None and authority.path.resolve() != self.registry.path.resolve():
            raise ExecutionOwnerError('EXECUTION_OWNER_AUTHORITY_DATABASE_MISMATCH')
        self.authority = authority
        self.instance = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.source = str(Path(__file__).resolve().parent)
        self.execution_tokens = {}
        self.task_executions = {}
        self.renewal_failed = set()
        if authority is not None:
            # The existing Owner and registry share the SAME authority object.
            authority.owner = self
            self.registry._execution_owner = self
        integration.dispatcher.completion_owner_instance = self.instance
        with self.registry._connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS clinx_execution_owners (
                execution_ref TEXT PRIMARY KEY, prepared_execution_ref TEXT NOT NULL UNIQUE,
                owner_instance TEXT NOT NULL, owner_pid INTEGER NOT NULL, source_path TEXT NOT NULL,
                state TEXT NOT NULL, registered_at REAL NOT NULL, updated_at REAL NOT NULL,
                response_json TEXT)''')

    def _submit(self, resource, epoch, operation, *, operation_id=None, **payload):
        """One stable operation ID per intent; no retry or local write fallback."""
        operation_id = operation_id or 'owner:' + uuid.uuid4().hex
        event = dict(stream_id='execution-owner', event_id=operation_id,
                     payload=dict(operation=operation, **payload))
        delivered = self.authority.deliver(event)
        receipt = self.authority.commit(delivered['delivery_id'], resource, epoch, operation_id)
        if receipt.get('commit') != 'RECORDED':
            raise ExecutionOwnerError('EXECUTION_OWNER_COMMIT_REJECTED', details={
                'delivery_receipt': delivered, 'commit_receipt': receipt})
        return receipt['result']

    def _execution_token(self, arguments, *, projection=False):
        reference = (arguments.get('execution_ref') or arguments.get('_shadow_execution_ref')
                     or arguments.get('execution_id'))
        task_id = arguments.get('task_id')
        if not reference and arguments.get('host_execution_ref'):
            with self.registry._connect() as conn:
                row = conn.execute('SELECT execution_ref FROM host_executions WHERE host_execution_ref=?',
                    (arguments['host_execution_ref'],)).fetchone()
                reference = row[0] if row else None
        if not reference and task_id:
            reference = self.task_executions.get(task_id)
        if reference in self.execution_tokens:
            return reference, self.execution_tokens[reference]
        # A restarted Owner may update a completed task's audit projection,
        # but must never adopt/replay an active or historical execution.
        with self.registry._connect() as conn:
            row = conn.execute('''SELECT r.* FROM authority_resources r
                WHERE (r.execution_ref=? OR (? IS NULL AND r.task_id=?))''',
                (reference, reference, task_id)).fetchone()
            enrolled = conn.execute('''SELECT 1 FROM authority_bindings
                WHERE execution_ref=? OR task_id=? LIMIT 1''', (reference, task_id)).fetchone()
        if row and projection and row['state'] == 'RELEASED':
            return row['execution_ref'], (row['resource_id'], row['epoch'], row['task_id'])
        if enrolled:
            raise ExecutionOwnerError('EXECUTION_OWNER_RECOVERY_REQUIRED: no captured execution epoch')
        return None

    def registry_mutation(self, method, arguments):
        if method not in EXECUTION_WRITES | EXECUTION_PROJECTIONS:
            raise ExecutionOwnerError('EXECUTION_OWNER_INVALID_EXECUTION_OPERATION')
        token = self._execution_token(arguments, projection=method in EXECUTION_PROJECTIONS)
        if token is None:
            return NotImplemented  # Unenrolled history/metadata keeps original semantics.
        reference, (resource, epoch, task_id) = token
        result = self._submit(resource, epoch,
            'owner_projection' if method in EXECUTION_PROJECTIONS else 'owner_update',
            args=dict(method=method, arguments=arguments, execution_ref=reference, task_id=task_id))
        return result

    def liveness_mutation(self, method, arguments):
        token = self._execution_token(arguments)
        if token is None:
            return NotImplemented
        _, (resource, epoch, _) = token
        return self._submit(resource, epoch, 'owner_liveness',
            args=dict(method=method, arguments=arguments))

    def apply_liveness_mutation(self, execution, task, args):
        from completion_runtime import CompletionIdentity
        from execution_liveness import completion_activity, invalidate, require_reobservation
        method, values = args['method'], dict(args['arguments'])
        if values.pop('execution_ref', None) != execution:
            raise ExecutionOwnerError('EXECUTION_IDENTITY_CONFLICT')
        if method == 'completion_activity':
            if values.pop('task_id', None) != task:
                raise ExecutionOwnerError('EXECUTION_IDENTITY_CONFLICT')
            identity = CompletionIdentity(execution, task, **values)
            record = self.registry.get_execution_record(execution)
            binding = self.registry.get_binding(task)
            if (record.get('execution_owned_turn') != identity.turn_id
                    or not binding or binding.thread_id != identity.thread_id):
                raise ExecutionOwnerError('EXECUTION_IDENTITY_CONFLICT')
            return completion_activity(self.registry, identity)
        if method == 'invalidate':
            return invalidate(self.registry, execution, **values)
        if method == 'require_reobservation' and not values:
            return require_reobservation(self.registry, execution)
        raise ExecutionOwnerError('EXECUTION_OWNER_INVALID_EXECUTION_OPERATION')

    def apply_registry_mutation(self, conn, execution, task, operation, args):
        """Called only inside the existing canonical commit/savepoint."""
        method, arguments = args['method'], dict(args['arguments'])
        allowed = EXECUTION_PROJECTIONS if operation == 'owner_projection' else EXECUTION_WRITES
        if (method not in allowed or args['execution_ref'] != execution or args['task_id'] != task
                or any(arguments.get(k, v) not in (None, v) for k, v in (
                    ('execution_ref', execution), ('execution_id', execution),
                    ('_shadow_execution_ref', execution), ('task_id', task)))):
            raise ExecutionOwnerError('EXECUTION_IDENTITY_CONFLICT')
        if arguments.get('host_execution_ref') and method == 'complete_host_execution':
            row = conn.execute('SELECT execution_ref FROM host_executions WHERE host_execution_ref=?',
                (arguments['host_execution_ref'],)).fetchone()
            if row is None or row[0] != execution:
                raise ExecutionOwnerError('EXECUTION_IDENTITY_CONFLICT')
        if method == 'set_execution_state' and arguments['state'] in {
                'COMPLETED', 'FAILED', 'BLOCKED', 'CANCELLED', 'STOPPED', 'IN_REVIEW'}:
            raise ExecutionOwnerError('TERMINAL_REQUIRES_FINALIZER_BUNDLE')
        result = getattr(self.registry, method)(**arguments)
        if method == 'bind_reauthorized_provider':
            result = (dataclasses.asdict(result[0]), result[1].to_json())
        return json.loads(json.dumps(result, default=_wire_value))

    @contextmanager
    def execution(self, task_id, *, issue_id, execution_ref, retain,
                  preserve_uncertain, expected_policy_json, expected_route_json):
        """Admit a NEW execution via existing shared allocator and commit.

        Identity registration creates no grant/epoch. Claim owns the resource
        CAS, canonical lease, runtime allocation, mapping and receipt together.
        """
        if not execution_ref:
            raise ExecutionOwnerError('EXECUTION_OWNER_EXECUTION_REFERENCE_REQUIRED')
        task = self.registry.get_task(task_id)
        resource = self.registry.worktree_key(host=task.host, cwd=task.cwd,
            repository_origin=task.repository_origin)
        with self.registry._connect() as conn:
            previous = conn.execute('SELECT current_epoch FROM runtime_resource_counters WHERE resource_key=?',
                (resource,)).fetchone()
        epoch = (previous[0] if previous else 0) + 1
        store = self.authority.runtime_store
        worker = 'owner:' + self.instance
        incarnation = 'owner-inc:' + self.instance
        attempt = 'owner-attempt:' + execution_ref
        store.register_task_reference(task_id, source_system='clinx', command_id='owner-task:' + task_id)
        store.register_execution(task_id, execution_ref, command_id='owner-exec:' + execution_ref)
        store.register_attempt(execution_ref, attempt, 0, command_id='owner-attempt:' + execution_ref)
        store.register_worker(worker, worker_kind='execution_owner',
            host_reference=getattr(getattr(self.integration, 'cfg', None), 'runtime_host', socket.gethostname()),
            capacity=1024, command_id=worker)
        store.register_incarnation(worker, incarnation_id=incarnation, generation=1,
            command_id=incarnation)
        self._submit(resource, epoch, 'claim', operation_id='owner-claim:' + execution_ref,
            execution_ref=execution_ref, task_id=task_id, expected_epoch=epoch - 1,
            issue_id=issue_id, expected_policy_json=expected_policy_json,
            expected_route_json=expected_route_json,
            expires_at=self.authority.clock() + store.max_lease_seconds,
            runtime=dict(attempt_id=attempt, worker_id=worker, incarnation_id=incarnation))
        self.execution_tokens[execution_ref] = (resource, epoch, task_id)
        self.task_executions[task_id] = execution_ref
        completed = False
        try:
            yield self.registry.get_task(task_id)
            completed = True
        finally:
            # The exact original outcome decides release. An accepted Turn or
            # an uncertain dispatch remains held; no historical lease is reaped.
            active = self.registry.get_active_execution(execution_ref)
            if active is not None:
                from execution_liveness import release_allowed
                with self.registry._connect() as conn:
                    safe = release_allowed(conn, execution_ref)
                terminal = active['stage'] in {'COMPLETED', 'FAILED', 'BLOCKED', 'CANCELLED', 'STOPPED'}
                uncertain = preserve_uncertain and active.get('turn_id') and not terminal
                if safe and not uncertain and (terminal or not retain or not completed):
                    self.registry.release_execution(task_id, execution_ref, retain_history=True)

    def finalize(self, finalizer, arguments):
        # A released tuple may only read back its exact durable finalization.
        # A different operation still meets the canonical inactive-epoch check.
        token = self._execution_token(arguments, projection=True)
        if token is None:
            return NotImplemented
        from m9_integration import ExecutionResult, ExecutionResultService
        reference, (resource, epoch, _) = token
        projection_keys = {'issue_id', 'review_state_id', 'blocked_state_id',
                           'team_id', 'review_state', 'blocked_state'}
        core = {k: v for k, v in arguments.items() if k not in projection_keys}
        # Same Turn and immutable final source use the same operation ID.
        identity = hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()
        result = ExecutionResult(**self._submit(resource, epoch, 'finalize',
            operation_id='owner-finalize:' + reference + ':' + identity, args=core))
        if finalizer.linear is not None:
            try:
                review, blocked = arguments.get('review_state_id'), arguments.get('blocked_state_id')
                if arguments.get('team_id') and (review is None or blocked is None):
                    states = finalizer.linear.team_states(arguments['team_id'])
                    review = review or states.get(arguments.get('review_state'))
                    blocked = blocked or states.get(arguments.get('blocked_state'))
                ExecutionResultService(self.registry, finalizer.linear).project(
                    execution_ref=reference, task_id=arguments['task_id'],
                    turn_id=arguments['turn_id'], result=result,
                    issue_id=arguments.get('issue_id', ''),
                    review_state_id=review, blocked_state_id=blocked)
            except Exception:
                self.registry.mark_execution_result_writeback(reference, state='FAILED')
        return result

    def renew_executions(self):
        """Bounded renewal in the existing Owner loop, using its captured epoch."""
        now = self.authority.clock() if self.authority is not None else 0
        for reference, (resource, epoch, _) in tuple(self.execution_tokens.items()):
            if reference in self.renewal_failed:
                continue
            with self.registry._connect() as conn:
                row = conn.execute('SELECT * FROM authority_resources WHERE resource_id=?', (resource,)).fetchone()
            if not row or row['state'] != 'ACTIVE' or row['expires_at'] - now > 300:
                continue
            try:
                self._submit(resource, epoch, 'renew', args=dict(
                    execution_ref=reference, expires_at=now + self.authority.runtime_store.max_lease_seconds))
            except ExecutionOwnerError:
                # Keep the delivery/rejection, do not refresh the captured epoch
                # or blindly repeat a denied operation on every loop iteration.
                self.renewal_failed.add(reference)

    def dispatch(self, request):
        if isinstance(request, dict) and request.get('action') == 'control':
            return self._caller_request(request)
        if isinstance(request, dict) and request.get('action') in {'deliver', 'commit', 'read_commit'}:
            return self._authority_request(request)
        if not isinstance(request, dict) or request.get('action') != 'cancel':
            return self.start(request)
        if (set(request) != {'action', 'execution_ref', 'database'}
                or request['database'] != str(Path(self.registry.path).resolve())
                or not isinstance(request['execution_ref'], str)
                or not re.fullmatch(r'exec_[0-9a-f]{32}', request['execution_ref'])):
            raise ExecutionOwnerError('EXECUTION_OWNER_INVALID_REQUEST')
        record = owner_record(self.registry, request['execution_ref'])
        if not record or record['owner_instance'] != self.instance:
            raise ExecutionOwnerError('EXECUTION_OWNER_RECOVERY_REQUIRED: execution belongs to another owner')
        # Cancellation must reach the same HostExecutor process registry; a
        # requester-local executor cannot terminate this owner's Host process.
        return {'result': self.integration.cancel_execution(execution_ref=request['execution_ref'])}

    def _caller_request(self, request):
        if (set(request) != {'action', 'domain', 'operation', 'arguments', 'database'}
                or request['database'] != str(self.registry.path.resolve())
                or not isinstance(request['arguments'], dict)):
            raise ExecutionOwnerError('EXECUTION_OWNER_INVALID_REQUEST')
        domain, operation = request['domain'], request['operation']
        arguments = dict(request['arguments'])
        if domain == 'integration' and operation in INTEGRATION_CALLS:
            result = getattr(self.integration, operation)(**arguments)
        elif domain == 'dispatcher' and operation in DISPATCHER_CALLS:
            if arguments.get('task_index') is True:
                from bridge import LinearTaskIndex
                arguments['task_index'] = LinearTaskIndex(
                    self.integration.linear, self.registry, self.integration.cfg.team_id)
            elif arguments.get('task_index') is False:
                arguments['task_index'] = None
            if operation == 'dispatch':
                return self._dispatcher_start(arguments)
            result = getattr(self.integration.dispatcher, operation)(**arguments)
        elif domain == 'publication' and operation in {'enqueue', 'process', 'run_once'}:
            from bridge import TargetConfig
            from desktop_publication import DesktopPublicationOutbox
            if operation == 'enqueue':
                arguments['target'] = TargetConfig(**arguments['target'])
            result = getattr(DesktopPublicationOutbox(self.registry, self.integration.cfg), operation)(**arguments)
        elif domain == 'native' and operation in {'control', 'reconcile', 'backfill'}:
            lifecycle = getattr(self.integration, 'native_lifecycle', None)
            if lifecycle is None:
                raise ExecutionOwnerError('EXECUTION_OWNER_NATIVE_LIFECYCLE_NOT_CONFIGURED')
            result = getattr(lifecycle, operation)(**arguments)
        elif domain == 'consultation':
            from consultations import NAMES, call
            if operation not in NAMES:
                raise ExecutionOwnerError('EXECUTION_OWNER_INVALID_REQUEST')
            result = call(self.registry, operation, arguments)
        elif domain == 'task_index' and operation == 'sync':
            from bridge import LinearTaskIndex
            result = LinearTaskIndex(self.integration.linear, self.registry,
                self.integration.cfg.team_id).sync(**arguments)
        elif domain == 'result' and operation == 'receive_and_writeback':
            from m9_integration import ExecutionResultService
            result = ExecutionResultService(self.registry, self.integration.linear).receive_and_writeback(**arguments)
        else:
            raise ExecutionOwnerError('EXECUTION_OWNER_INVALID_REQUEST')
        return {'result': result}

    def _dispatcher_start(self, arguments):
        """Reuse the Owner's existing durable start record for Linear ingress."""
        identity = 'dispatcher:' + hashlib.sha256(json.dumps(arguments,
            default=_wire_value, sort_keys=True).encode()).hexdigest()
        supplied = arguments.get('execution_ref')
        reference = (supplied if isinstance(supplied, str) and supplied.startswith('exec_')
                     else 'exec_' + identity[-32:])
        arguments = dict(arguments, execution_ref=reference)
        with self.lock:
            if self.stopping.is_set():
                raise ExecutionOwnerError('EXECUTION_OWNER_DRAINING')
            with self.registry._connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                prior = conn.execute('SELECT * FROM clinx_execution_owners WHERE execution_ref=?',
                    (reference,)).fetchone()
                if prior:
                    if prior['prepared_execution_ref'] != identity:
                        raise ExecutionOwnerError('EXECUTION_OWNER_REQUEST_ID_CONFLICT')
                    if prior['response_json']:
                        return json.loads(prior['response_json'])
                    raise ExecutionOwnerError('EXECUTION_OWNER_RECOVERY_REQUIRED: dispatch outcome unknown; no replay')
                now = time.time()
                conn.execute('INSERT INTO clinx_execution_owners VALUES (?,?,?,?,?,?,?,?,NULL)',
                    (reference, identity, self.instance, os.getpid(), self.source, 'CLAIMED', now, now))
            try:
                result = self.integration.dispatcher.dispatch(**arguments)
                response = {'result': dataclasses.asdict(result)}
                state = 'DISPATCHED'
            except Exception as exc:
                from dispatch_errors import failure_details, safe_reason
                response = {'error': safe_reason(exc), 'failure_details': failure_details(
                    'dispatcher', {}, exc, self.registry)}
                state = 'UNKNOWN'
            with self.registry._connect() as conn:
                conn.execute('UPDATE clinx_execution_owners SET state=?,updated_at=?,response_json=? WHERE execution_ref=?',
                    (state, time.time(), json.dumps(response), reference))
            return response

    def _authority_request(self, request):
        if (set(request) != {'action', 'payload', 'database'}
                or request['database'] != str(self.registry.path.resolve())
                or not isinstance(request['payload'], dict)):
            raise ExecutionOwnerError('EXECUTION_OWNER_INVALID_REQUEST')
        if self.authority is None:
            raise ExecutionOwnerError('EXECUTION_OWNER_AUTHORITY_NOT_CONFIGURED')
        method, payload = request['action'], request['payload']
        if method != 'deliver':
            keys = {'resource_id', 'epoch', 'operation_id'}
            if method == 'commit':
                keys.add('delivery_id')
            if (set(payload) != keys or type(payload['epoch']) is not int
                    or payload['epoch'] < 1
                    or any(not isinstance(payload[k], str) or not payload[k]
                           for k in keys - {'epoch'})):
                raise ExecutionOwnerError('EXECUTION_OWNER_INVALID_REQUEST')
        from authority_writer import dispatch
        # No receipt projection or extra pre-commit epoch check: existing commit
        # owns BEGIN IMMEDIATE, resource CAS, effects, rejection and receipt.
        return {'result': dispatch(self.authority, method, payload)}

    @staticmethod
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

    def _rearm_native_rejection(self, conn, prior, prepared):
        if not self._native_rejection_retryable(conn, prior, prepared):
            return False
        ref = prior['execution_ref']
        old = conn.execute('SELECT * FROM execution_history WHERE execution_ref=?', (ref,)).fetchone()
        binding = conn.execute('SELECT thread_id FROM conversation_bindings WHERE task_id=?',
                               (old['task_id'],)).fetchone()
        from task_registry import _now
        evidence = {k: old[k] for k in ('execution_ref', 'task_id', 'stage', 'execution_state',
            'turn_id', 'failure_stage', 'failure_code', 'failure_evidence', 'acquired_at', 'released_at')}
        evidence.update(prior_owner_instance=prior['owner_instance'],
            prior_response_sha256=hashlib.sha256(prior['response_json'].encode()).hexdigest(),
            disposition='RETRY_PROVEN_PRE_RPC_REFUSAL_SAME_SEALED_REQUEST')
        conn.execute('''INSERT INTO context_checkpoints
            (checkpoint_id,task_id,execution_id,thread_id,turn_id,timestamp,prompt_summary,
             result_summary,changed_files,validation_summary,blockers,next_state,source,provenance)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            ('checkpoint_' + uuid.uuid4().hex, old['task_id'], ref, binding[0], None, _now(),
             'Original native input request', 'Prior refusal occurred before Provider RPC', '',
             'PREPARED; no Turn, lease, result, Host effect or completion', old['failure_code'],
             'Reevaluate the same sealed preparation', 'NATIVE_PREWIRE_REJECTION', json.dumps(evidence)))
        conn.execute('DELETE FROM execution_history WHERE execution_ref=?', (ref,))
        conn.execute('''UPDATE clinx_execution_owners SET owner_instance=?,owner_pid=?,source_path=?,
            state='CLAIMED',updated_at=?,response_json=NULL WHERE execution_ref=?''',
            (self.instance, os.getpid(), self.source, time.time(), ref))
        return True

    def start(self, request):
        if (not isinstance(request, dict)
                or set(request) != {'prepared_execution_ref', 'approved', 'database'}
                or request['approved'] is not True
                or request['database'] != str(Path(self.registry.path).resolve())
                or not isinstance(request['prepared_execution_ref'], str)
                or not re.fullmatch(r'prepared_[0-9a-f]{32}', request['prepared_execution_ref'])):
            raise ExecutionOwnerError('EXECUTION_OWNER_INVALID_REQUEST')
        prepared = request['prepared_execution_ref']
        reference = 'exec_' + prepared.removeprefix('prepared_')
        with self.lock:
            if self.stopping.is_set():
                raise ExecutionOwnerError('EXECUTION_OWNER_DRAINING')
            sealed = self.registry.verify_prepared_execution(prepared)
            with self.registry._connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                prior = conn.execute('SELECT * FROM clinx_execution_owners WHERE execution_ref=?',
                                     (reference,)).fetchone()
                if prior:
                    execution = self.registry.get_execution_record(reference)
                    if (prior['owner_instance'] != self.instance and execution
                            and execution.get('stage') not in {'COMPLETED', 'FAILED', 'CANCELLED', 'BLOCKED'}):
                        raise ExecutionOwnerError('EXECUTION_OWNER_RECOVERY_REQUIRED: prior owner lost; no replay')
                    rearmed = self._rearm_native_rejection(conn, prior, sealed)
                    if prior['response_json'] and not rearmed:
                        return json.loads(prior['response_json'])
                    if not rearmed:
                        raise ExecutionOwnerError('EXECUTION_OWNER_RECOVERY_REQUIRED: start outcome is unknown; no replay')
                else:
                    now = time.time()
                    conn.execute('INSERT INTO clinx_execution_owners VALUES (?,?,?,?,?,?,?,?,NULL)',
                             (reference, prepared, self.instance, os.getpid(), self.source,
                              'CLAIMED', now, now))
            # From this point neither socket EOF nor requester death cancels
            # dispatch or the attached Provider supervisor.
            try:
                result = self.integration.start_execution(prepared_execution_ref=prepared, approved=True)
                response = {'result': {**result, 'execution_owner': {
                    'instance': self.instance, 'pid': os.getpid(), 'source_path': self.source,
                    'lifetime': 'PERSISTENT_CONTROL_RUNTIME'}}}
                state = 'DISPATCHED' if result.get('execution_started') else 'REJECTED'
            except Exception as exc:
                from dispatch_errors import failure_details, safe_reason
                response = {'error': safe_reason(exc), 'failure_details': failure_details(
                    'clinx_start_execution', {'prepared_execution_ref': prepared}, exc, self.registry)}
                record = self.registry.get_execution_record(reference)
                known_unstarted = (response['failure_details'].get('side_effect_certainty') == 'NOT_EXECUTED'
                    and record and record.get('stage') == 'BLOCKED' and not record.get('execution_owned_turn'))
                state = 'REJECTED' if known_unstarted or not record else 'UNKNOWN'
            with self.registry._connect() as conn:
                conn.execute('UPDATE clinx_execution_owners SET state=?,updated_at=?,response_json=? WHERE execution_ref=?',
                             (state, time.time(), json.dumps(response), reference))
            return response

    def has_active_executions(self):
        # Serialize with admission so shutdown cannot miss a dispatch that has
        # accepted its request but has not yet persisted its execution record.
        with self.lock:
            return self._has_active_executions()

    def _has_active_executions(self):
        with self.registry._connect() as conn:
            references = conn.execute('SELECT execution_ref FROM clinx_execution_owners WHERE owner_instance=?',
                                      (self.instance,)).fetchall()
        for row in references:
            record = self.registry.get_execution_record(row['execution_ref'])
            if record and (record.get('stage') not in {'COMPLETED', 'FAILED', 'CANCELLED', 'BLOCKED'}
                           or record.get('codex_running')):
                return True
        return False


def serve(integration, *, ready=None, stop=None, authority=None):
    """One same-user socket and one durable owner, independent of MCP children."""
    path = socket_path(integration.registry)
    worker_uid = int(os.environ.get('CLINX_OWNER_CLIENT_UID', os.getuid()))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = path.parent.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
            or directory.st_mode & 0o022):
        raise ExecutionOwnerError('EXECUTION_OWNER_DIRECTORY_UNTRUSTED')
    lock_fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        owner = ExecutionOwner(integration, authority=authority)
    except BaseException:
        os.close(lock_fd)
        raise
    stopping = stop or threading.Event()
    owner.stopping = stopping
    previous_signals = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_signals[signum] = signal.signal(signum, lambda *_: stopping.set())

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            if hasattr(socket, 'SO_PEERCRED'):
                _, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid not in {os.getuid(), worker_uid}:
                    return
            self.request.settimeout(10)
            data = self.rfile.readline(MAX_MESSAGE + 1)
            if not data.endswith(b'\n') or len(data) > MAX_MESSAGE:
                return
            try:
                response = owner.dispatch(json.loads(data))
            except Exception as exc:
                from dispatch_errors import failure_details, safe_reason
                response = {'error': safe_reason(exc), 'failure_details': failure_details(
                    'execution_owner', {}, exc, integration.registry)}
            try:
                self.wfile.write(json.dumps(response, default=_wire_value).encode() + b'\n')
            except OSError:
                pass  # The durable result and Provider owner outlive this client.

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = False

    server = None
    try:
        if path.exists():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ExecutionOwnerError('EXECUTION_OWNER_ENDPOINT_UNTRUSTED')
            path.unlink()  # Exclusive service lock proves this is a stale endpoint.
        old_umask = os.umask(0o077)
        try:
            server = Server(str(path), Handler)
            if worker_uid != os.getuid():
                # Admin pre-creates the setgid endpoint directory for these
                # two service principals. Requests never choose credentials.
                path.chmod(0o660)
        finally:
            os.umask(old_umask)
        server.timeout = 0.2
        integration.dispatcher.start_completion_runtime()
        if ready:
            ready.set()
        while not stopping.is_set() or owner.has_active_executions():
            server.handle_request()
            if authority is not None:
                owner.renew_executions()
    finally:
        if server:
            server.server_close()
            path.unlink(missing_ok=True)
        integration.dispatcher.stop_completion_runtime()
        for signum, handler in previous_signals.items():
            signal.signal(signum, handler)
        os.close(lock_fd)
