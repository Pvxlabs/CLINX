"""Fixed read-only writer evidence through the existing Desktop daemon."""
import json
import os
from pathlib import Path
import socket
import struct
import sys


def inspect(request):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from native_provider import observe_native_writer
    from thread_identity import parse_selector
    root = Path(request['root'])
    expected = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    if not root.is_absolute() or root.resolve(strict=True) != expected.resolve(strict=True):
        raise ValueError('NATIVE_HOME_IDENTITY_CONFLICT')
    parse_selector(thread_id=request['thread_id'])
    return dict(observe_native_writer(root, request['thread_id'], request['turn_id'], request['project']),
                observer_uid=os.getuid())


def observe(endpoint, root, thread_id, turn_id, project):
    from app_server import AppServerError
    from native_provider import existing_client
    from rust_worker_identity import worker_account
    request = dict(root=str(root), thread_id=thread_id, turn_id=turn_id, project=project)
    unknown = dict(liveness='UNKNOWN', reason='NATIVE_WRITER_PROBE_UNAVAILABLE')
    try:
        # PrivateUsers hides root's identity from the user-owned Observer.
        # Its own native home establishes its UID without reading root policy.
        native_home = Path.home() / '.codex'
        own_home = (os.getuid() != 0 and Path(root).resolve() == native_home.resolve()
                    and native_home.stat().st_uid == os.getuid())
        expected_uid = os.getuid() if own_home else worker_account().pw_uid
        with existing_client(endpoint, timeout=3) as client:
            peer = client.transport._byte_transport.sock
            _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != expected_uid:
                return unknown
            client.initialize(client_name='clinx-native-writer-observer',
                              client_title='CLINX read-only writer evidence', client_version='1')
            params = dict(command=['/usr/bin/python3', '-I', str(Path(__file__).resolve()),
                                   json.dumps(request)], cwd='/', timeoutMs=2500, outputBytesCap=4096,
                          sandboxPolicy=dict(type='dangerFullAccess'))
            client._workspace_metadata_request = params
            try:
                result = client._request('command/exec', params)
            finally:
                client._workspace_metadata_request = None
        if result.get('exitCode') != 0 or len(result.get('stdout', '')) > 4096:
            return unknown
        value = json.loads(result['stdout'])
        if (value.get('observer_uid') != expected_uid or value.get('liveness') not in ('LIVE', 'UNKNOWN')
                or (value['liveness'] == 'LIVE'
                    and (type(value.get('writer_pid')) is not int or value['writer_pid'] <= 0))):
            return unknown
        return {k: value[k] for k in ('liveness', 'reason', 'writer_pid') if k in value}
    except (AppServerError, OSError, ValueError, KeyError, RuntimeError):
        return unknown


if __name__ == '__main__':
    try:
        result = inspect(json.loads(sys.argv[1]))
    except (OSError, ValueError, KeyError, IndexError):
        result = dict(liveness='UNKNOWN', reason='NATIVE_WRITER_UNPROVEN', observer_uid=os.getuid())
    print(json.dumps(result))
