"""Durable, exact-execution completion delivery for the local CLINX runtime.

This is not a scheduler or a second finalizer. Only explicitly registered
executions are observed. No task discovery, provider starts, or lease reclaim
is performed here. The existing Finalizer continues to own terminal truth.
"""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import functools
import hashlib
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable, Iterator

_LOG = logging.getLogger(__name__)
_LOCKS = tuple(threading.RLock() for _ in range(64))
_LOCAL = threading.local()


class CompletionIdentityError(ValueError):
    """Only a proven identity mismatch is quarantined rather than retried."""


@contextlib.contextmanager
def finalization_lock(registry: Any, execution_ref: str) -> Iterator[None]:
    """Serialize local finalizers across threads/processes; not authorization.

    flock is released by the OS after process exit. Lock files are never
    removed while the database exists, avoiding split-lock inode races.
    All durable execution/turn/lease ownership checks remain mandatory.
    """
    database = str(Path(registry.path).resolve())
    key = hashlib.sha256((database + '\0' + execution_ref).encode()).hexdigest()
    lock = _LOCKS[int(key[:8], 16) % len(_LOCKS)]
    with lock:
        held = getattr(_LOCAL, 'held', None)
        if held is None:
            held = _LOCAL.held = set()
        if key in held:
            yield
            return
        directory = Path(database).parent / (Path(database).name + '.completion-locks')
        directory.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(directory / key, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            held.add(key)
            try:
                yield
            finally:
                held.remove(key)
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def serialized_execution(method: Callable[..., Any]) -> Callable[..., Any]:
    """Share the exact-execution lock between observation and finalization."""
    @functools.wraps(method)
    def call(self: Any, *args: Any, **kwargs: Any) -> Any:
        reference = kwargs.get('execution_ref', args[0] if args else None)
        registry = getattr(self, 'registry', None) or self.tasks
        if not reference:
            return method(self, *args, **kwargs)  # existing legacy null-ref path
        with finalization_lock(registry, reference):
            return method(self, *args, **kwargs)
    return call


def serialized_prepared_start(method: Callable[..., Any]) -> Callable[..., Any]:
    """Prevent a fast completion racing post-dispatch preparation/Linear state."""
    @functools.wraps(method)
    def call(self: Any, *args: Any, **kwargs: Any) -> Any:
        prepared = kwargs.get('prepared_execution_ref')
        if not isinstance(prepared, str) or not prepared.strip():
            return method(self, *args, **kwargs)
        reference = 'exec_' + prepared.removeprefix('prepared_')
        with finalization_lock(self.registry, reference):
            return method(self, *args, **kwargs)
    return call


@dataclasses.dataclass(frozen=True)
class CompletionIdentity:
    execution_ref: str
    task_id: str
    thread_id: str
    turn_id: str

    def __post_init__(self) -> None:
        for value in dataclasses.astuple(self):
            if not isinstance(value, str) or not value.strip() or len(value) > 512:
                raise CompletionIdentityError('completion identity must contain bounded exact identifiers')


class CompletionRuntime:
    """One bounded observer loop, recovering only durable enrolled executions."""
    def __init__(self, registry: Any, reconcile: Callable[..., dict[str, Any]], *,
                 interval_seconds: float = 5.0):
        if interval_seconds <= 0:
            raise ValueError('completion interval must be positive')
        self.registry = registry
        self.reconcile = reconcile
        self.interval_seconds = interval_seconds
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        with self._connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS clinx_completion_handoffs (
                execution_ref TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('PENDING','DONE','HOLD')),
                terminal_observed INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0,
                last_error TEXT, updated_at REAL NOT NULL)''')

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.registry.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def identity_for(self, execution_ref: str) -> CompletionIdentity:
        record = self.registry.get_execution_record(execution_ref)
        if record is None:
            raise CompletionIdentityError('unknown execution; completion cannot invent an owner')
        task_id = record['task_id']
        prepared = self.registry.get_prepared_execution_for_execution(execution_ref)
        route = self.registry.get_execution_routing_identity(execution_ref)
        thread_id = route.conversation.binding if route and route.conversation.status == 'BOUND' else None
        turn_id = record.get('execution_owned_turn')
        if prepared is not None:
            if prepared.resulting_task_id not in {None, task_id}:
                raise CompletionIdentityError('prepared task identity differs')
            if prepared.resulting_thread_id not in {None, thread_id}:
                raise CompletionIdentityError('prepared thread identity differs')
            if turn_id and prepared.resulting_turn_id not in {None, turn_id}:
                raise CompletionIdentityError('prepared turn identity differs')
            turn_id = turn_id or prepared.resulting_turn_id
        identity = CompletionIdentity(execution_ref, task_id, thread_id, turn_id)
        task = self.registry.get_task(task_id)
        binding = self.registry.get_binding(task_id)
        if task.turn_id != identity.turn_id or binding is None or binding.thread_id != identity.thread_id:
            raise CompletionIdentityError('completion does not own current task/conversation')
        return identity

    def register(self, identity: CompletionIdentity) -> None:
        """Persist the handoff before a supervisor can consume completion."""
        with finalization_lock(self.registry, identity.execution_ref):
            if self.identity_for(identity.execution_ref) != identity:
                raise CompletionIdentityError('completion identity is not the durable execution identity')
            with self._connect() as conn:
                row = conn.execute('SELECT * FROM clinx_completion_handoffs WHERE execution_ref=?',
                                   (identity.execution_ref,)).fetchone()
                if row is not None:
                    if tuple(row[key] for key in ('execution_ref','task_id','thread_id','turn_id')) != dataclasses.astuple(identity):
                        raise CompletionIdentityError('completion identity is immutable')
                    return
                conn.execute('''INSERT INTO clinx_completion_handoffs
                    (execution_ref,task_id,thread_id,turn_id,state,updated_at)
                    VALUES (?,?,?,?,'PENDING',?)''', (*dataclasses.astuple(identity), time.time()))

    def recover(self, execution_ref: str) -> dict[str, Any]:
        """Explicitly enroll one old execution; never scan/reclaim other leases."""
        identity = self.identity_for(execution_ref)
        self.register(identity)
        with self._connect() as conn:
            conn.execute("UPDATE clinx_completion_handoffs SET state='PENDING',next_attempt=0 WHERE execution_ref=?",
                         (execution_ref,))
        return self.process(execution_ref)

    def notify(self, identity: CompletionIdentity, message: dict[str, Any]) -> None:
        method = message.get('method')
        params = message.get('params')
        turn = params.get('turn') if isinstance(params, dict) else None
        observed = turn.get('id') if isinstance(turn, dict) else params.get('turnId') if isinstance(params, dict) else None
        exact = isinstance(params, dict) and params.get('threadId') == identity.thread_id and observed == identity.turn_id
        if method == 'turn/completed' and not exact:
            raise CompletionIdentityError('completion notification lacks exact thread/turn identity')
        error = None if method == 'turn/completed' else 'OBSERVATION_INTERRUPTED'
        with self._connect() as conn:
            updated = conn.execute('''UPDATE clinx_completion_handoffs
                SET terminal_observed=MAX(terminal_observed,?),next_attempt=0,
                    last_error=?,updated_at=?
                WHERE execution_ref=? AND task_id=? AND thread_id=? AND turn_id=? AND state='PENDING' ''',
                (int(method == 'turn/completed'), error, time.time(), *dataclasses.astuple(identity))).rowcount
            if updated == 0:
                row = conn.execute('SELECT state FROM clinx_completion_handoffs WHERE execution_ref=?',
                                   (identity.execution_ref,)).fetchone()
                if row is None:
                    raise CompletionIdentityError('completion handoff was not durably registered')
        self._wake.set()

    def inspect(self, execution_ref: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM clinx_completion_handoffs WHERE execution_ref=?',
                               (execution_ref,)).fetchone()
        return dict(row) if row else None

    def process(self, execution_ref: str) -> dict[str, Any]:
        with finalization_lock(self.registry, execution_ref):
            row = self.inspect(execution_ref)
            if row is None or row['state'] != 'PENDING':
                return {'completion_delivery': row['state'] if row else 'UNKNOWN'}
            identity = CompletionIdentity(*(row[k] for k in ('execution_ref','task_id','thread_id','turn_id')))
            state, error = 'PENDING', None
            try:
                # A new observation connection may have a new generation. The
                # durable execution/thread/turn binding, not its old socket,
                # establishes the identity for restart recovery.
                if self.identity_for(execution_ref) != identity:
                    raise CompletionIdentityError('durable completion identity changed')
                result = self.reconcile(execution_ref, reclaim_stale=False)
                record = self.registry.get_execution_result(execution_ref)
                with self.registry._connect() as conn:
                    lease = conn.execute('SELECT 1 FROM worktree_leases WHERE execution_ref=? LIMIT 1',
                                         (execution_ref,)).fetchone()
                execution = self.registry.get_execution_record(execution_ref)
                if (record is not None and record.task_id == identity.task_id and record.turn_id == identity.turn_id
                        and lease is None and execution is not None
                        and execution['stage'] in {'COMPLETED','BLOCKED','FAILED','CANCELLED'}):
                    state = 'DONE'
                elif (lease is None and execution is not None and execution['stage'] == 'CANCELLED'):
                    state = 'DONE'  # cancellation has its existing separate result contract
                elif not result.get('authoritative'):
                    error = 'TERMINAL_NOT_YET_PROVEN'
            except CompletionIdentityError:
                state, error = 'HOLD', 'COMPLETION_IDENTITY_MISMATCH'
                result = {'state': 'HOLD', 'authoritative': False, 'failure_code': error}
            except Exception as exc:
                # Error type only: remote payloads/credentials do not enter the
                # journal. Full internal diagnostics belong in local logs.
                error = 'COMPLETION_RETRY:' + type(exc).__name__
                _LOG.error('completion delivery failed for %s: %s', execution_ref, type(exc).__name__)
                result = {'state': 'PENDING', 'authoritative': False, 'failure_code': error}
            delay = min(60.0, self.interval_seconds * (2 ** min(row['attempts'], 4))) if error else self.interval_seconds
            with self._connect() as conn:
                conn.execute('''UPDATE clinx_completion_handoffs SET state=?,attempts=attempts+1,
                    next_attempt=?,last_error=?,updated_at=? WHERE execution_ref=?''',
                    (state, time.time() + delay, error, time.time(), execution_ref))
            return {**result, 'completion_delivery': state}

    def run_once(self, *, limit: int = 16) -> int:
        if not 1 <= limit <= 100:
            raise ValueError('completion batch must be between 1 and 100')
        with self._connect() as conn:
            rows = conn.execute("SELECT execution_ref FROM clinx_completion_handoffs WHERE state='PENDING' AND next_attempt<=? ORDER BY next_attempt,execution_ref LIMIT ?",
                                (time.time(), limit)).fetchall()
        for row in rows:
            self.process(row['execution_ref'])
        return len(rows)

    def start(self) -> None:
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            def observe() -> None:
                while not self._stop.is_set():
                    self._wake.clear()
                    try:
                        self.run_once()
                    except Exception as exc:
                        _LOG.error('completion observer retry required: %s', type(exc).__name__)
                    self._wake.wait(self.interval_seconds)
            self._thread = threading.Thread(target=observe, name='clinx-completion', daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)
