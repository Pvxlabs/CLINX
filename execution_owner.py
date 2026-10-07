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
            self.registry.verify_prepared_execution(prepared)
            with self.registry._connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                prior = conn.execute('SELECT * FROM clinx_execution_owners WHERE execution_ref=?',
                                     (reference,)).fetchone()
                if prior:
                    execution = self.registry.get_execution_record(reference)
                    if (prior['owner_instance'] != self.instance and execution
                            and execution.get('stage') not in {'COMPLETED', 'FAILED', 'CANCELLED', 'BLOCKED'}):
                        raise ExecutionOwnerError('EXECUTION_OWNER_RECOVERY_REQUIRED: prior owner lost; no replay')
                    if prior['response_json']:
                        return json.loads(prior['response_json'])
                    raise ExecutionOwnerError('EXECUTION_OWNER_RECOVERY_REQUIRED: start outcome is unknown; no replay')
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
                state = 'UNKNOWN' if self.registry.get_execution_record(reference) else 'REJECTED'
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
