"""Keep canonical dispatch and its Provider connection outside MCP request lifetime.

This local service accepts only an already sealed preparation. It delegates all
authority, leases, dispatch and completion to the existing integration. It never
resumes an uncertain turn or replays a Host command on restart.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
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

from task_registry import TaskRegistryError

MAX_MESSAGE = 256 * 1024


class ExecutionOwnerError(TaskRegistryError):
    def __init__(self, message, *, details=None):
        super().__init__(message)
        self.details = details or {}


def socket_path(registry):
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

    def _request(self, payload):
        payload = {**payload, 'database': str(Path(self.registry.path).resolve())}
        path = socket_path(self.registry)
        sent = False
        try:
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ExecutionOwnerError('EXECUTION_OWNER_ENDPOINT_UNTRUSTED')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(str(path))
                if hasattr(socket, 'SO_PEERCRED'):
                    _, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != os.getuid():
                        raise ExecutionOwnerError('EXECUTION_OWNER_PEER_UNTRUSTED')
                # A partial write is uncertain too; never silently fall back.
                sent = True
                sock.sendall(json.dumps(payload).encode() + b'\n')
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
    def __init__(self, integration):
        self.integration = integration
        self.registry = integration.registry
        self.instance = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.source = str(Path(__file__).resolve().parent)
        integration.dispatcher.completion_owner_instance = self.instance
        with self.registry._connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS clinx_execution_owners (
                execution_ref TEXT PRIMARY KEY, prepared_execution_ref TEXT NOT NULL UNIQUE,
                owner_instance TEXT NOT NULL, owner_pid INTEGER NOT NULL, source_path TEXT NOT NULL,
                state TEXT NOT NULL, registered_at REAL NOT NULL, updated_at REAL NOT NULL,
                response_json TEXT)''')

    def dispatch(self, request):
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


def serve(integration, *, ready=None, stop=None):
    """One same-user socket and one durable owner, independent of MCP children."""
    path = socket_path(integration.registry)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = path.parent.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
            or directory.st_mode & 0o022):
        raise ExecutionOwnerError('EXECUTION_OWNER_DIRECTORY_UNTRUSTED')
    lock_fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        owner = ExecutionOwner(integration)
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
                if uid != os.getuid():
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
                self.wfile.write(json.dumps(response).encode() + b'\n')
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
        finally:
            os.umask(old_umask)
        server.timeout = 0.2
        integration.dispatcher.start_completion_runtime()
        if ready:
            ready.set()
        while not stopping.is_set() or owner.has_active_executions():
            server.handle_request()
    finally:
        if server:
            server.server_close()
            path.unlink(missing_ok=True)
        integration.dispatcher.stop_completion_runtime()
        for signum, handler in previous_signals.items():
            signal.signal(signum, handler)
        os.close(lock_fd)
