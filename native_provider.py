"""Exact existing-daemon observation and continuation routing.

Endpoints come from host configuration, never a selector. No discovery scan,
proxy startup, resume, unsubscribe or dynamic-tool response on the read plane.
"""
import os
from pathlib import Path
import shlex
import subprocess
import sys

from app_server import AppServerError, AppServerProtocolError, AppServerRemoteError, CodexAppServerClient, UnixSocketTransport


class NativeWriterError(AppServerProtocolError):
    side_effect = 'NONE'
    def __init__(self, code, reason):
        self.code, self.method = code, 'writer_route'
        super().__init__(code + ': ' + reason)


def endpoints(cfg):
    result = []
    for value in (cfg.app_server.local_socket, getattr(cfg.app_server, 'native_socket', None)):
        if not value:
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / path
        if str(path) not in result:
            result.append(str(path))
    return result


def existing_client(endpoint, timeout=3, *, read_only=True):
    client = CodexAppServerClient(UnixSocketTransport(endpoint, timeout_seconds=timeout),
        timeout_seconds=timeout, strict_dynamic_tool_binding=True)
    client.provider_endpoint = endpoint
    client.read_only_observer = read_only
    return client


def new_desktop_client(cfg, cwd):
    """Create one ordinary native chat on the configured Desktop daemon.

    Admission is scoped to one thread/start in this exact workspace. Once
    created, the connection relays input to that returned thread only.
    """
    native = getattr(cfg.app_server, 'native_socket', None)
    if not native:
        raise NativeWriterError('NATIVE_DESKTOP_NOT_CONFIGURED',
                                'ordinary native tasks require the Desktop endpoint')
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise NativeWriterError('NATIVE_DESKTOP_INVALID_WORKSPACE', 'absolute workspace required')
    from types import SimpleNamespace
    endpoint = endpoints(SimpleNamespace(app_server=SimpleNamespace(local_socket=None, native_socket=native)))[0]
    client = existing_client(endpoint, cfg.app_server.request_timeout_seconds, read_only=False)
    client.shared_native_writer = True
    client.native_creation_cwd = cwd
    return client


def observe_thread(cfg, tid):
    observations = []
    for endpoint in endpoints(cfg):
        try:
            with existing_client(endpoint) as client:
                client.initialize(client_name=cfg.app_server.client_name, client_title=cfg.app_server.client_title,
                                  client_version=cfg.app_server.client_version)
                thread = client.thread_read(tid)
                if thread.get('id') != tid:
                    raise AppServerProtocolError('exact thread identity mismatch')
                status = thread.get('status', {})
                state = status.get('type') if isinstance(status, dict) else status
                try:
                    turns = client.thread_turns_list(tid, limit=1, items_view='notLoaded')['data']
                except AppServerRemoteError as exc:
                    # Legacy history has no paged-turn reader. Its exact live
                    # thread state is still valid routing evidence.
                    if exc.error.get('code') != -32601:
                        raise
                    turns = []
                turn = turns[0] if turns else {}
                observations.append({'endpoint': endpoint, 'state': state or 'UNKNOWN',
                    'thread_id': tid, 'session_id': thread.get('sessionId'), 'cwd': thread.get('cwd'),
                    'turn_id': turn.get('id'), 'turn_status': turn.get('status'), 'source': 'LIVE_THREAD_READ'})
        except FileNotFoundError:
            observations.append({'endpoint': endpoint, 'state': 'OFFLINE', 'reason': 'SOCKET_NOT_PRESENT'})
        except PermissionError:
            observations.append({'endpoint': endpoint, 'state': 'UNKNOWN', 'reason': 'PROVIDER_ACCESS_DENIED'})
        except (AppServerError, OSError):
            observations.append({'endpoint': endpoint, 'state': 'UNKNOWN', 'reason': 'PROVIDER_READ_UNAVAILABLE'})
    loaded = [r for r in observations if r['state'] not in ('UNKNOWN', 'OFFLINE', 'notLoaded', 'unloaded')]
    return {'state': loaded[0]['state'] if len(loaded) == 1 else 'UNKNOWN',
            'owner_endpoint': loaded[0]['endpoint'] if len(loaded) == 1 else None,
            'ownership_conflict': len(loaded) > 1, 'observations': observations,
            'source': 'LIVE_PROVIDER_OBSERVATION', 'writer_claimed': False}


def observe_native_writer(root, tid, turn_id, project):
    """Observe a same-user native writer without connecting to its RPC.

    Both the exact thread lock and its indexed rollout must be open for writing
    by one Codex app-server. Recheck the exact unfinished native turn afterwards.
    File existence or a living process alone never establishes running state.
    """
    from native_history import NativeHistory, ThreadLookupError
    unknown = {'liveness': 'UNKNOWN', 'reason': 'NATIVE_WRITER_UNPROVEN'}
    if sys.platform not in ('darwin', 'linux'):
        return unknown
    history = NativeHistory(root)
    try:
        metadata = history.metadata(tid)
        if not metadata or metadata['cwd'] != project:
            return unknown
        before = history.status(tid, metadata)
        if before.get('turn_id') != turn_id or before['state'] != 'RUNNING' or before.get('completed_at') is not None:
            return unknown
        rollout = history._path(tid, metadata)
        lock = history.root / 'thread-writer-locks' / (tid + '.lock')
        if not lock.is_file() or lock.is_symlink():
            return unknown
        if sys.platform == 'linux':
            pid = _linux_native_writer(lock, rollout)
            if pid is None:
                return unknown
            current_metadata = history.metadata(tid)
            if (not current_metadata or any(current_metadata.get(k) != metadata.get(k)
                    for k in ('id', 'cwd', 'rollout_path', 'history_mode'))
                    or history.status(tid, metadata) != before):
                return unknown
            return {'liveness': 'LIVE', 'reason': 'NATIVE_WRITER_PROCESS', 'writer_pid': pid}
        expected = {str(lock.resolve()), str(rollout)}
        result = subprocess.run(['/usr/sbin/lsof', '-n', '-P', '-Fpcuftan', '--', str(lock), str(rollout)],
                                capture_output=True, text=True, timeout=2)
        if result.returncode or len(result.stdout) > 32768:
            return unknown
        processes = []; current = None; access = None
        for line in result.stdout.splitlines():
            key, value = line[:1], line[1:]
            if key == 'p':
                current = {'pid': int(value), 'files': set()}; processes.append(current)
            elif current is not None:
                if key in ('c', 'u'):
                    current[key] = value
                elif key == 'f':
                    access = None
                elif key == 'a':
                    access = value
                elif key == 'n' and access in ('u', 'w'):
                    current['files'].add(value)
        owners = [p for p in processes if p.get('u') == str(os.getuid())
                  and p.get('c') == 'codex' and expected <= p['files']]
        if len(owners) != 1:
            return unknown
        pid = owners[0]['pid']
        command = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'command='],
                                 capture_output=True, text=True, timeout=2)
        args = shlex.split(command.stdout)
        if command.returncode or not args or Path(args[0]).name != 'codex' or 'app-server' not in args:
            return unknown
        current_metadata = history.metadata(tid)
        identity_fields = ('id', 'cwd', 'rollout_path', 'history_mode')
        if (not current_metadata or any(current_metadata.get(k) != metadata.get(k) for k in identity_fields)
                or history.status(tid, metadata) != before):
            return unknown
        return {'liveness': 'LIVE', 'reason': 'NATIVE_WRITER_PROCESS', 'writer_pid': pid}
    except (OSError, ValueError, IndexError, StopIteration, subprocess.SubprocessError, ThreadLookupError):
        return unknown


def _linux_native_writer(lock, rollout, proc_root=Path('/proc')):
    """Kernel lock owner + exact writable inodes + process identity, all read-only."""
    def identity(path):
        s = path.stat()
        return s.st_dev, s.st_ino

    def owners():
        with (proc_root / 'locks').open() as f:
            data = f.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            return set()
        result = set()
        dev, inode = identity(lock)
        for line in data.splitlines():
            parts = line.split()
            if len(parts) != 8 or parts[1:4] != ['FLOCK', 'ADVISORY', 'WRITE'] or parts[6:] != ['0', 'EOF']:
                continue
            major, minor, number = parts[5].split(':')
            if (int(major, 16), int(minor, 16), int(number)) == (os.major(dev), os.minor(dev), inode):
                result.add(int(parts[4]))
        return result

    pids = owners()
    if len(pids) != 1 or next(iter(pids)) <= 0:
        return None
    pid = next(iter(pids))
    proc = proc_root / str(pid)
    start = (proc / 'stat').read_bytes()
    if proc.stat().st_uid != os.getuid() or (proc / 'exe').resolve().name != 'codex':
        return None
    args = (proc / 'cmdline').read_bytes().split(b'\0')
    if not args or Path(os.fsdecode(args[0])).name != 'codex' or not {b'exec', b'app-server'}.intersection(args):
        return None
    expected = {identity(lock), identity(rollout)}
    writable = set()
    for i, fd in enumerate((proc / 'fd').iterdir()):
        if i >= 1024:
            return None
        try:
            info = (proc / 'fdinfo' / fd.name).read_text()
            flags = next(int(line.split()[1], 8) for line in info.splitlines() if line.startswith('flags:'))
            if flags & os.O_ACCMODE in (os.O_WRONLY, os.O_RDWR):
                writable.add(identity(fd))
        except (FileNotFoundError, ProcessLookupError):
            continue
    if (not expected <= writable or owners() != pids or (proc / 'stat').read_bytes().split(b')', 1)[1].split()[19]
            != start.split(b')', 1)[1].split()[19]):
        return None
    return pid


def select_writer_client(cfg, tid, *, host_tools=False):
    """Route ordinary native input, or select an isolated Host tool writer."""
    evidence = observe_thread(cfg, tid)
    rows = evidence['observations']
    native = getattr(cfg.app_server, 'native_socket', None)
    native_endpoint = None
    if native:
        from types import SimpleNamespace
        native_endpoint = endpoints(SimpleNamespace(app_server=SimpleNamespace(local_socket=None, native_socket=native)))[0]
    if not host_tools:
        if not native_endpoint:
            raise NativeWriterError('NATIVE_DESKTOP_NOT_CONFIGURED',
                                    'ordinary native tasks require the Desktop endpoint')
        desktop = next((r for r in rows if Path(r['endpoint']).resolve() == Path(native_endpoint).resolve()
                        and r['state'] in {'idle', 'active', 'notLoaded', 'unloaded'}), None)
        # Desktop is the native input owner, not a competing CLINX writer.
        # Other daemons' idle/unknown projections cannot veto this route.
        if desktop and (desktop['state'] in {'idle', 'active'} or all(
                r['state'] in {'notLoaded', 'unloaded', 'OFFLINE'} for r in rows)):
            client = existing_client(desktop['endpoint'], cfg.app_server.request_timeout_seconds, read_only=False)
            client.writer_route_evidence = evidence
            client.host_successor_required = False
            client.shared_native_writer = True
            client.native_forwarding_thread_id = tid
            return client
        # Never continue an ordinary task on the old dedicated Provider.
        # Its existing active/idle thread must finish and release naturally;
        # routing to it would recreate the Desktop writer-lock conflict.
        if any(r['state'] == 'active' for r in rows):
            raise NativeWriterError('NATIVE_ACTIVE_OWNER', 'the existing native turn is still running')
        if any(r['state'] == 'idle' for r in rows):
            raise NativeWriterError('NATIVE_LEGACY_OWNER_NOT_RELEASED',
                                    'the legacy Provider has not yet released this thread')
        raise NativeWriterError('NATIVE_DESKTOP_UNAVAILABLE',
                                'the configured Desktop endpoint could not be observed')
    # Persisted turn status can lag a live idle observation. Only live status
    # proves an active owner; notLoaded is never an execution failure.
    if any(r['state'] == 'active' for r in rows):
        raise NativeWriterError('NATIVE_ACTIVE_OWNER', 'continuation cannot interrupt the native turn')
    if evidence['ownership_conflict'] or any(r['state'] == 'UNKNOWN' for r in rows):
        raise NativeWriterError('NATIVE_WRITER_OWNER_UNPROVEN', 'exact endpoint observation is incomplete or conflicting')
    loaded = [r for r in rows if r['state'] not in ('OFFLINE', 'notLoaded', 'unloaded')]
    if loaded and loaded[0]['state'] != 'idle':
        raise NativeWriterError('NATIVE_WRITER_NOT_IDLE', 'loaded owner is not idle')
    available = [r for r in rows if r['state'] in ('notLoaded', 'unloaded')]
    endpoint = loaded[0]['endpoint'] if loaded else available[0]['endpoint'] if available else None
    if endpoint is None:
        raise NativeWriterError('NATIVE_WRITER_ENDPOINT_UNAVAILABLE', 'no configured existing endpoint is available')
    # A second client on Desktop's daemon can receive the existing turn id
    # from turn/start (implicit steer). A CLINX lease cannot fence that writer.
    # HOST execution may use a successor thread, but that is still the same
    # Canonical Task. An idle Desktop owner has not handed off its authority;
    # switching daemons must not bypass the shared-writer refusal.
    shared = bool(native_endpoint and Path(endpoint).resolve() == Path(native_endpoint).resolve())
    if shared and host_tools:
        raise NativeWriterError('NATIVE_SHARED_WRITER_NOT_QUALIFIED',
            'Desktop owns this daemon; idle observation is not an exclusive Task handoff')
    successor_required = False
    if host_tools:
        # Desktop broadcasts dynamic calls to clients which do not own CLINX's
        # handler. Never send a Host turn there, even when it owns an idle chat.
        dedicated = endpoints(cfg)[0] if cfg.app_server.local_socket else None
        row = next((r for r in rows if r['endpoint'] == dedicated), None)
        if row is None or row['state'] == 'OFFLINE':
            raise NativeWriterError('HOST_PROVIDER_UNAVAILABLE', 'configured dedicated Provider is unavailable')
        successor_required = endpoint != dedicated
        endpoint = dedicated
    if host_tools and native_endpoint and Path(endpoint).resolve() == Path(native_endpoint).resolve():
        raise NativeWriterError('NATIVE_SHARED_WRITER_NOT_QUALIFIED',
            'Configured Provider aliases the shared Desktop daemon')
    client = existing_client(endpoint, cfg.app_server.request_timeout_seconds, read_only=False)
    client.writer_route_evidence = evidence
    client.host_successor_required = successor_required
    if shared:
        # Ordinary native forwarding sends an authorized user message through
        # the existing Desktop owner. It does not acquire exclusive ownership.
        client.shared_native_writer = True
        client.native_forwarding_thread_id = tid
    return client


class NativeExecutionError(AppServerProtocolError):
    def __init__(self, code, reason):
        self.code, self.method = code, 'execution_owner'
        super().__init__(code + ': ' + reason)


def select_execution_client(cfg, tid, *, read_only=True, turn_id=None):
    """Observe/cancel on the actual configured owner, never a second daemon.

    With no loaded owner, only read-only history access is allowed. The caller
    must not treat an unloaded daemon's interrupted projection as cancellation.
    No resume, provider startup, subscription or authority change occurs here.
    """
    if turn_id:
        evidence = observe_execution(cfg, tid, turn_id)
        if evidence["owner_endpoint"] is None:
            error = NativeExecutionError("NATIVE_EXECUTION_OWNER_UNPROVEN", evidence["liveness_reason"])
            error.liveness_evidence = evidence
            raise error
        client = existing_client(evidence["owner_endpoint"], cfg.app_server.request_timeout_seconds,
                                 read_only=read_only)
        client.execution_route_evidence = evidence
        return client
    evidence = observe_thread(cfg, tid)
    rows = evidence['observations']
    loaded = [r for r in rows if r['state'] not in ('UNKNOWN', 'OFFLINE', 'notLoaded', 'unloaded')]
    if (evidence['ownership_conflict'] or len(loaded) > 1
            or any(r['state'] == 'UNKNOWN' for r in rows)):
        raise NativeExecutionError('NATIVE_EXECUTION_OWNER_UNPROVEN',
                                   'configured endpoint observations are incomplete or conflicting')
    if loaded:
        if loaded[0]['state'] not in ('active', 'idle'):
            raise NativeExecutionError('NATIVE_EXECUTION_OWNER_UNPROVEN', 'owner state is not recognized')
        endpoint = loaded[0]['endpoint']
    else:
        available = [r for r in rows if r['state'] in ('notLoaded', 'unloaded')]
        if not read_only or not available:
            raise NativeExecutionError('NATIVE_EXECUTION_OWNER_UNPROVEN', 'no live owner is available')
        endpoint = available[0]['endpoint']
    client = existing_client(endpoint, cfg.app_server.request_timeout_seconds, read_only=read_only)
    client.execution_route_evidence = evidence
    return client


def cancel_native_turn(cfg, tid, turn_id):
    """Interrupt only the explicitly selected live native turn. Never resume.

    Authorization and durable request audit are owned by NodeService. An RPC
    acknowledgement is a request receipt; terminal state remains observational.
    """
    if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 256:
        raise NativeWriterError('INVALID_NATIVE_TURN_ID', 'A bounded exact turn id is required')
    evidence = observe_execution(cfg, tid, turn_id)
    if evidence.get('provider_liveness') != 'LIVE' or not evidence.get('owner_endpoint'):
        error = NativeWriterError('NATIVE_EXECUTION_OWNER_UNPROVEN', 'Exact live owner must be proved before cancellation')
        error.liveness_evidence = evidence
        raise error
    endpoint = evidence['owner_endpoint']
    with existing_client(endpoint, cfg.app_server.request_timeout_seconds, read_only=False) as client:
        client.initialize(client_name=cfg.app_server.client_name, client_title=cfg.app_server.client_title,
                          client_version=cfg.app_server.client_version)
        # Repeat at the write boundary. The exact turn parameter also prevents
        # a delayed request from interrupting a successor turn.
        current = observe_execution(cfg, tid, turn_id)
        if current.get('provider_liveness') != 'LIVE' or current.get('owner_endpoint') != endpoint:
            raise NativeWriterError('NATIVE_EXECUTION_OWNER_CHANGED', 'Native owner changed before interrupt')
        client.turn_interrupt(tid, turn_id)
    return {'thread_id': tid, 'native_turn_id': turn_id, 'cancel_requested': True,
                'cancel_confirmed': False, 'execution_cancelled': False, 'operation_state': 'CANCELLATION_PENDING',
                'status': 'CANCELLATION_PENDING', 'retry_required': True, 'read_only': False,
                'provider_observation': current}


def observe_execution(cfg, tid, turn_id):
    """Read every configured endpoint, bounded to five pages of exact turns."""
    from execution_liveness import classify
    observations = []
    for endpoint in endpoints(cfg):
        state = "UNKNOWN"
        try:
            with existing_client(endpoint) as client:
                client.initialize(client_name=cfg.app_server.client_name,
                    client_title=cfg.app_server.client_title, client_version=cfg.app_server.client_version)
                thread = client.thread_read(tid)
                if thread.get("id") != tid:
                    raise AppServerProtocolError("exact thread identity mismatch")
                status = thread.get("status", {})
                state = status.get("type") if isinstance(status, dict) else status
                cursor, seen, turn = None, set(), {}
                for _ in range(5):
                    page = client.thread_turns_list(tid, limit=20, sort_direction="desc",
                                                   items_view="notLoaded", **({"cursor": cursor} if cursor else {}))
                    turn = next((r for r in page["data"] if r.get("id") == turn_id), {})
                    cursor = page.get("nextCursor")
                    if turn or not cursor or cursor in seen:
                        break
                    seen.add(cursor)
                observations.append(dict(endpoint=endpoint, state=state or "UNKNOWN",
                    thread_id=tid, turn_id=turn.get("id"), turn_status=turn.get("status")))
        except (AppServerError, OSError, ValueError, KeyError, TypeError) as exc:
            observations.append(dict(endpoint=endpoint, state=state, transport_error=True,
                reason="PROVIDER_READ_UNAVAILABLE", error_type=type(exc).__name__))
    return classify(observations, tid, turn_id)


def archived_upstream_failure(root, path, tid, turn_id, cwd):
    """Read one explicitly selected native archive; never use its latest turn.

    This narrow recovery source proves a provider error with no tool items. It
    does not establish release permission or replace live owner observation.
    """
    import datetime as dt
    import hashlib
    import json
    import stat

    path = Path(path)
    archive = (Path(root).expanduser() / 'archived_sessions').resolve()
    if (path.is_symlink() or path.resolve().parent != archive
            or not path.name.endswith('-' + tid + '.jsonl')):
        raise ValueError('ARCHIVE_IDENTITY_CONFLICT')
    before = path.stat()
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
            or before.st_size > 64 * 1024 * 1024):
        raise ValueError('ARCHIVE_SOURCE_UNAVAILABLE')
    data = path.read_bytes()
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('ARCHIVE_CHANGED_DURING_READ')
    rows = [json.loads(line) for line in data.splitlines() if line.strip()]
    meta = rows[0] if rows else {}
    if (meta.get('type') != 'session_meta' or meta['payload'].get('id') != tid
            or meta['payload'].get('cwd') != cwd):
        raise ValueError('ARCHIVE_IDENTITY_CONFLICT')
    selected = False
    started = completed = context = None
    for row in rows[1:]:
        payload = row.get('payload', {})
        kind = payload.get('type')
        ref = payload.get('turn_id')
        if row.get('type') == 'event_msg' and kind == 'task_started':
            selected = ref == turn_id
            if selected:
                if started is not None:
                    raise ValueError('ARCHIVE_DUPLICATE_TURN')
                started = row['timestamp']
        if not selected:
            if ref == turn_id:
                raise ValueError('ARCHIVE_TURN_BOUNDARY_CONFLICT')
            continue
        if ref is not None and ref != turn_id:
            raise ValueError('ARCHIVE_TURN_BOUNDARY_CONFLICT')
        if completed is not None:
            raise ValueError('ARCHIVE_POST_TERMINAL_ACTIVITY')
        if row.get('type') == 'turn_context':
            if payload.get('cwd') != cwd or ref != turn_id:
                raise ValueError('ARCHIVE_WORKSPACE_CONFLICT')
            context = True
        elif row.get('type') == 'response_item':
            if kind != 'message' or payload.get('role') != 'user':
                raise ValueError('ARCHIVE_TOOL_OR_PROVIDER_ACTIVITY')
        elif row.get('type') == 'event_msg':
            if kind == 'task_complete':
                error = payload.get('error', {})
                upstream = json.loads(error.get('message', '{}')).get('error', {})
                if (ref != turn_id or upstream.get('type') != 'upstream_error'
                        or upstream.get('message') != 'Upstream request failed'
                        or payload.get('last_agent_message')):
                    raise ValueError('ARCHIVE_NOT_UPSTREAM_FAILURE')
                completed = row['timestamp']
            elif kind == 'item_completed':
                if payload.get('item', {}).get('type') != 'UserMessage':
                    raise ValueError('ARCHIVE_TOOL_OR_PROVIDER_ACTIVITY')
            elif kind != 'task_started':
                raise ValueError('ARCHIVE_UNCONFIRMED_ACTIVITY')
        else:
            raise ValueError('ARCHIVE_UNCONFIRMED_ACTIVITY')
    if not started or not completed or not context:
        raise ValueError('ARCHIVE_EXACT_TERMINAL_MISSING')
    if dt.datetime.fromisoformat(completed) < dt.datetime.fromisoformat(started):
        raise ValueError('ARCHIVE_TIMESTAMP_CONFLICT')
    return dict(source='native_archive_task_complete', path=str(path.resolve()),
                archive_sha256=hashlib.sha256(data).hexdigest(), thread_id=tid,
                turn_id=turn_id, cwd=cwd, started_at=started, completed_at=completed,
                error='Upstream request failed', provider_outcome='PROVIDER_FAILED')


def confirm_unstarted_execution(cfg, registry, execution_ref):
    """Negative reconciliation of a failed pre-turn attempt; never resume."""
    import datetime as dt
    from native_history import NativeHistory
    from task_registry import TaskRegistryError
    execution = registry.get_active_execution(execution_ref)
    if (not execution or execution.get('execution_owned_turn') is not None
            or execution.get('execution_owned_state') not in {'RECOVERY_REQUIRED', 'TRANSPORT_UNCERTAIN'}):
        raise TaskRegistryError('UNSTARTED_RECONCILIATION_DENIED')
    task = registry.get_task(execution['task_id'])
    binding = registry.get_binding(task.task_id)
    prepared = registry.verify_prepared_execution('prepared_' + execution_ref.removeprefix('exec_'))
    if (not binding or task.turn_id is not None or prepared.status != 'PREPARED'
            or prepared.task_ref != task.task_id or prepared.resulting_turn_id is not None):
        raise TaskRegistryError('UNSTARTED_IDENTITY_UNPROVEN')
    native = NativeHistory(cfg.app_server.native_home or Path.home()/'.codex')
    metadata = native.metadata(binding.thread_id)
    status = native.status(binding.thread_id, metadata) if metadata else {}
    acquired = dt.datetime.fromisoformat(execution['acquired_at']).timestamp()
    if (status.get('state') != 'COMPLETED' or not status.get('completed_at')
            or status['completed_at'] >= acquired):
        raise TaskRegistryError('UNSTARTED_NATIVE_ABSENCE_UNPROVEN')
    observation = observe_thread(cfg, binding.thread_id)
    rows = observation['observations']
    if observation.get('ownership_conflict') or not rows or any(
            row.get('state') not in {'idle','notLoaded','unloaded','OFFLINE'} for row in rows):
        raise TaskRegistryError('UNSTARTED_PROVIDER_ABSENCE_UNPROVEN')
    available = [r for r in rows if r.get('state') != 'OFFLINE']
    if not available or any(r.get('turn_id') != status['turn_id'] or
                            r.get('turn_status') != 'completed' for r in available):
        raise TaskRegistryError('UNSTARTED_PROVIDER_TURN_CONFLICT')
    return {'execution_ref': execution_ref, 'task_id': task.task_id, 'thread_id': binding.thread_id,
            'baseline_turn_id': status['turn_id'], 'observations': rows,
            'observed_at': dt.datetime.now(dt.timezone.utc).isoformat()}
