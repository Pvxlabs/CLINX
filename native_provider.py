"""Exact existing-daemon observation and continuation routing.

Endpoints come from host configuration, never a selector. No discovery scan,
proxy startup, resume, unsubscribe or dynamic-tool response on the read plane.
"""
import os
from pathlib import Path

from app_server import AppServerError, AppServerProtocolError, CodexAppServerClient, UnixSocketTransport


class NativeWriterError(AppServerProtocolError):
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
                page = client.thread_turns_list(tid, limit=1, items_view='notLoaded')
                turns = page['data']
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


def select_writer_client(cfg, tid):
    """Select a proved idle owner before one authorized resume; never retry resume."""
    evidence = observe_thread(cfg, tid)
    rows = evidence['observations']
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
    client = existing_client(endpoint, cfg.app_server.request_timeout_seconds, read_only=False)
    client.writer_route_evidence = evidence
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
