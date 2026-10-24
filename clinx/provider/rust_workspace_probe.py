"""Read-only workspace observations through the existing native Provider.

No task state, threads, turns, credentials or project permissions are changed.
The authority selects this immutable helper and the configured Provider endpoint.
"""
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys


class ProbeError(Exception):
    pass


def git(cwd, args):
    allowed = (args[:1] == ['rev-parse'] or args == ['branch', '--show-current']
               or args[:2] in (['remote', 'get-url'], ['worktree', 'list'],
                               ['symbolic-ref', '--quiet'], ['check-ref-format', '--branch'],
                               ['for-each-ref', '--format=%(symref)'])
               or args == ['config', '--list'])
    if not allowed:
        raise ProbeError('WORKSPACE_PROBE_INVALID')
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    result = subprocess.run(['/usr/bin/git', '-c', 'core.fsmonitor=false', '-c',
                             'safe.directory=' + cwd, '-C', cwd, *args],
                            capture_output=True, timeout=2, env=env)
    if result.returncode or len(result.stdout) > 65536:
        raise ProbeError('WORKSPACE_IDENTITY_UNAVAILABLE')
    return result.stdout.decode('utf-8').strip()


def inspect(request):
    cwd, mode = request['cwd'], request['mode']
    if not isinstance(cwd, str) or not cwd.startswith('/') or '\0' in cwd or len(cwd) > 4096:
        raise ProbeError('WORKSPACE_CONTRACT_INVALID')
    try:
        cwd = str(Path(cwd).resolve(strict=True))
        if not Path(cwd).is_dir() or not os.access(cwd, os.X_OK):
            raise OSError('unusable cwd')
    except OSError:
        raise ProbeError('WORKSPACE_CWD_UNREADABLE') from None
    if mode == 'git':
        return dict(value=git(cwd, request['args']), observer_uid=os.getuid())
    if mode not in ('repository', 'workspace'):
        raise ProbeError('WORKSPACE_PROBE_INVALID')
    common = git(cwd, ['rev-parse', '--path-format=absolute', '--git-common-dir'])
    try:
        info = Path(common).stat()
        if not Path(common).is_dir():
            raise OSError('invalid Git common directory')
    except OSError:
        raise ProbeError('WORKSPACE_IDENTITY_UNAVAILABLE') from None
    result = dict(repository_identity=f'git-common:{info.st_dev}:{info.st_ino}',
                  observer_uid=os.getuid())
    if mode == 'workspace':
        result.update(cwd=cwd, repository_common_dir=common,
                      repository_root=git(cwd, ['rev-parse', '--show-toplevel']),
                      worktree=git(cwd, ['rev-parse', '--path-format=absolute', '--git-dir']),
                      branch=git(cwd, ['branch', '--show-current']),
                      head=git(cwd, ['rev-parse', 'HEAD']))
    return result


def observe(endpoint, request):
    # -I excludes caller Python paths; only the selected release is imported.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from clinx.provider.app_server import CodexAppServerClient, UnixSocketTransport
    from clinx.authority.rust_worker_identity import worker_account
    expected_uid = worker_account().pw_uid
    with CodexAppServerClient(UnixSocketTransport(endpoint, timeout_seconds=3),
                             timeout_seconds=3) as client:
        peer = client.transport._byte_transport.sock
        _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != expected_uid:
            raise ProbeError('WORKSPACE_OBSERVER_IDENTITY_CONFLICT')
        client.read_only_observer = True
        client.initialize(client_name='clinx-workspace-observer',
                          client_title='Read-only workspace metadata', client_version='1')
        # One fixed, bounded metadata command, with no model or native turn.
        # Remain an observer for unsolicited dynamic-tool and approval requests.
        params = dict(
            command=['/usr/bin/python3', '-I', str(Path(__file__).resolve()),
                     '--inspect', json.dumps(request)], cwd='/', timeoutMs=2500,
            outputBytesCap=65536, sandboxPolicy=dict(type='dangerFullAccess'))
        client._workspace_metadata_request = params
        try:
            result = client._request('command/exec', params)
        finally:
            client._workspace_metadata_request = None
    if result.get('exitCode') != 0 or len(result.get('stdout', '')) > 65536:
        raise ProbeError('WORKSPACE_OBSERVER_UNAVAILABLE')
    data = json.loads(result['stdout'])
    if data.get('error'):
        if data['error'] not in {'WORKSPACE_CWD_UNREADABLE', 'WORKSPACE_IDENTITY_UNAVAILABLE',
                                 'WORKSPACE_CONTRACT_INVALID', 'WORKSPACE_PROBE_INVALID'}:
            raise ProbeError('WORKSPACE_OBSERVER_INVALID')
        raise ProbeError(data['error'])
    if data.get('observer_uid') != expected_uid:
        raise ProbeError('WORKSPACE_OBSERVER_IDENTITY_CONFLICT')
    data['observer_source'] = 'CODEX_PROVIDER_COMMAND_EXEC'
    return data


def main():
    try:
        if len(sys.argv) != 3:
            raise ProbeError('WORKSPACE_PROBE_INVALID')
        request = json.loads(sys.argv[2])
        result = inspect(request) if sys.argv[1] == '--inspect' else observe(sys.argv[1], request)
    except ProbeError as exc:
        result = dict(error=str(exc))
    except Exception:
        result = dict(error='WORKSPACE_OBSERVER_UNAVAILABLE')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
