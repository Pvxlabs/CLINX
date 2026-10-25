"""Exact existing-daemon observation and continuation routing.

Endpoints come from host configuration, never a selector. No discovery scan,
proxy startup, resume, unsubscribe or dynamic-tool response on the read plane.
"""
import os
import shlex
import subprocess
import sys
from pathlib import Path

from clinx.provider.app_server import (
    AppServerProtocolError,
    AppServerRemoteError,
    CodexAppServerClient,
    UnixSocketTransport,
)


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
        timeout_seconds=timeout)
    client.provider_endpoint = endpoint
    client.read_only_observer = read_only
    return client


def exact_turn(client, thread_id, turn_id):
    """Bounded exact history read for remaining Python read/maintenance callers."""
    cursor, seen = None, set()
    for _ in range(100):
        page = client.thread_turns_list(thread_id, limit=20, items_view='full',
            **({'cursor': cursor} if cursor else {}))
        matches = [turn for turn in page['data'] if turn.get('id') == turn_id]
        if len(matches) == 1:
            return matches[0]
        cursor = page.get('nextCursor')
        if matches or not cursor or cursor in seen:
            break
        seen.add(cursor)
    raise AppServerProtocolError('EXACT_NATIVE_TURN_UNAVAILABLE')


_default_existing_client = existing_client


def _observation_call(operation, cfg=None, **arguments):
    """Private IO injection boundary; Rust owns all evidence decisions."""
    from clinx.provider.app_server import _provider_with_callback
    clients = {}
    if cfg is not None:
        arguments.update(client_info=dict(name=cfg.app_server.client_name,
            title=cfg.app_server.client_title, version=cfg.app_server.client_version),
            native_transport=existing_client is _default_existing_client)
        arguments.setdefault('endpoints', endpoints(cfg))

    def callback(event):
        operation, args = event['arguments']['operation'], event['arguments']['arguments']
        try:
            if operation == 'command':
                command = args['command']
                if command[0] not in ('/usr/sbin/lsof', '/bin/ps'):
                    raise ValueError('NATIVE_WRITER_PROBE_INVALID')
                result = subprocess.run(command, capture_output=True, text=True, timeout=2)
                value = dict(returncode=result.returncode, stdout=result.stdout,
                    argv=shlex.split(result.stdout) if command[0] == '/bin/ps' else [])
            elif operation == 'read_bytes':
                import base64
                value = base64.b64encode(Path(args['path']).read_bytes()).decode('ascii')
            elif operation == 'open':
                context = existing_client(args['endpoint'])
                client = context.__enter__()
                clients[args['endpoint']] = (context, client)
                client.initialize(client_name=cfg.app_server.client_name,
                    client_title=cfg.app_server.client_title, client_version=cfg.app_server.client_version)
                value = None
            elif operation == 'close':
                context, _ = clients.pop(args['endpoint'])
                value = context.__exit__(None, None, None)
            elif operation == 'rpc':
                client = clients[args['endpoint']][1]
                params = args['params']
                if args['method'] == 'thread/read':
                    value = {'thread': client.thread_read(params['threadId'])}
                else:
                    kw = dict(limit=params['limit'], sort_direction=params['sortDirection'], items_view=params['itemsView'])
                    if 'cursor' in params:
                        kw['cursor'] = params['cursor']
                    value = client.thread_turns_list(params['threadId'], **kw)
            else:
                raise ValueError('PROVIDER_OBSERVATION_CALLBACK_INVALID')
            return {'result': value}
        except Exception as exc:
            kind = ('not_found' if isinstance(exc, FileNotFoundError) else
                    'permission' if isinstance(exc, PermissionError) else
                    'remote' if isinstance(exc, AppServerRemoteError) else
                    'protocol' if isinstance(exc, AppServerProtocolError) else 'transport')
            return {'error': dict(kind=kind, message=str(exc), remote=getattr(exc, 'error', None))}

    try:
        return _provider_with_callback('observation.' + operation, callback, **arguments)
    finally:
        for context, _ in clients.values():
            context.__exit__(None, None, None)


def observe_thread(cfg, tid):
    return _observation_call('threads', cfg, thread_id=tid)


def observe_native_writer(root, tid, turn_id, project):
    return _observation_call('writer', root=str(root), thread_id=tid,
                            turn_id=turn_id, project=project, platform=sys.platform)


def _linux_native_writer(lock, rollout, proc_root=Path('/proc')):
    return _observation_call('linux_writer', lock=str(lock), rollout=str(rollout),
                            proc_root=str(proc_root), stat_callback=True)


class NativeExecutionError(AppServerProtocolError):
    def __init__(self, code, reason):
        self.code, self.method = code, 'execution_owner'
        super().__init__(code + ': ' + reason)


def observe_execution(cfg, tid, turn_id, *, route_endpoints=None):
    return _observation_call('execution', cfg, thread_id=tid, turn_id=turn_id,
        endpoints=endpoints(cfg) if route_endpoints is None else route_endpoints)
