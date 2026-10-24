"""Exact read adapters. No registry construction or execution commands."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from clinx.authority.rust_authority import RustAuthorityError


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def limits(recent_turns, max_bytes):
    recent_turns = 8 if recent_turns is None else recent_turns
    max_bytes = 32000 if max_bytes is None else max_bytes
    if (type(recent_turns) is not int or not 1 <= recent_turns <= 20
            or type(max_bytes) is not int or not 1024 <= max_bytes <= 128000):
        raise RustAuthorityError('INVALID_CONTEXT_LIMIT')
    return recent_turns, max_bytes


def desktop_read(thread_id, *, context=False, recent_turns=8, max_bytes=32000, cursor=None):
    """Use the existing authenticated loopback Observer; never expose its token."""
    credential = os.environ.get('CLINX_NATIVE_OBSERVER_TOKEN_FILE')
    if not credential:
        raise RustAuthorityError('NATIVE_OBSERVER_NOT_CONFIGURED')
    try:
        token = Path(credential).read_text().strip()
        args = dict(context=int(context), recent_turns=recent_turns, max_bytes=max_bytes)
        if cursor is not None:
            args['cursor'] = cursor
        request = Request('http://127.0.0.1:8766/v2/native/' + thread_id + '?' + urlencode(args),
                          headers={'Authorization': 'Bearer ' + token})
        with urlopen(request, timeout=12) as response:
            data = response.read(1048577)
        if len(data) > 1048576:
            raise RustAuthorityError('CONTEXT_CAPACITY_EXCEEDED')
        result = json.loads(data)
        if result.get('queried_thread_id') != thread_id or result.get('read_only') is not True:
            raise RustAuthorityError('NATIVE_OBSERVATION_IDENTITY_CONFLICT')
        return result
    except HTTPError as exc:
        # Only known public codes, never reflect credentials or upstream bodies.
        code = 'NATIVE_OBSERVER_UNAVAILABLE'
        try:
            value = json.loads(exc.read(4096)).get('error')
            if value in {'PROJECT_SCOPE_DENIED', 'SHARING_SCOPE_DENIED', 'NATIVE_THREAD_NOT_FOUND',
                         'INVALID_NATIVE_CONTEXT_CURSOR', 'INVALID_QUERY'}:
                code = value
        except (ValueError, OSError):
            pass
        raise RustAuthorityError(code) from None
    except (OSError, URLError, ValueError):
        raise RustAuthorityError('NATIVE_OBSERVER_UNAVAILABLE') from None


def observe_desktop(directory, thread_id, *, context=False, recent_turns=8, max_bytes=32000, cursor=None):
    """Existing Observer's native read route, authorized by current sharing grants."""
    from clinx.provider.native_history import NativeHistory
    from clinx.provider.native_provider import existing_client
    from clinx.provider.app_server import AppServerError
    from clinx.contracts.thread_identity import parse_selector
    from clinx.nodes.node_protocol import NodeProtocolError

    parse_selector(thread_id=thread_id)
    recent_turns, max_bytes = limits(recent_turns, max_bytes)
    if directory is None or not directory.local_identity or not directory.native_home:
        raise NodeProtocolError('SHARING_SCOPE_DENIED', 'Local native observation is unavailable')
    node = directory.local_identity['node_id']
    scope = directory._scope(node)
    if scope is None:
        raise NodeProtocolError('SHARING_SCOPE_DENIED', 'Native reads require current read_sessions grants')
    history = NativeHistory(directory.native_home)
    metadata = history.metadata(thread_id)
    if not metadata:
        raise NodeProtocolError('NATIVE_THREAD_NOT_FOUND', 'Exact native thread is absent')
    if not scope.allows_project(metadata['cwd']):
        raise NodeProtocolError('PROJECT_SCOPE_DENIED', 'Native project is outside current sharing grants')
    result = dict(queried_thread_id=thread_id, thread_id=thread_id, task_ref=None, execution_ref=None,
                  host=node, node_id=node, project=metadata['cwd'], lookup_status='THREAD_UNBOUND',
                  binding_status='NOT_FOUND', execution_state=None, read_only=True,
                  selection_scope='QUERIED_THREAD', status_source='NATIVE_OBSERVATION',
                  native_status=history.status(thread_id, metadata), observed_at=stamp())
    result['native_status_source'] = 'NATIVE_PERSISTED_HISTORY'
    result['native_turn_state'] = result['native_status']['state']
    result['provider_observation'] = dict(state='UNKNOWN', source='LIVE_THREAD_READ', writer_claimed=False)
    endpoint = os.environ.get('CLINX_NATIVE_OBSERVER_SOCKET')
    if endpoint:
        try:
            with existing_client(endpoint, timeout=3) as client:
                client.initialize(client_name='clinx-native-observer', client_title='CLINX read-only Observer', client_version='1')
                thread = client.thread_read(thread_id)
                if thread.get('id') != thread_id or thread.get('cwd') != metadata['cwd']:
                    raise NodeProtocolError('NATIVE_OBSERVATION_IDENTITY_CONFLICT', 'Native live identity differs')
                state = thread.get('status', {}).get('type', 'UNKNOWN')
                result['provider_observation'].update(state=state, observed_at=stamp())
                result['native_thread'] = dict(id=thread_id, cwd=thread['cwd'], status=thread.get('status'))
        except (AppServerError, OSError):
            result['provider_observation']['reason'] = 'LIVE_THREAD_READ_UNAVAILABLE'
    # A persisted terminal turn is history. Only the current owner's live read
    # establishes current thread activity; do not promote old summaries to it.
    live = result['provider_observation']['state']
    if live not in ('active', 'idle') and result['native_turn_state'] == 'RUNNING':
        from clinx.provider.native_provider import observe_native_writer
        proof = observe_native_writer(directory.native_home, thread_id,
            result['native_status']['turn_id'], metadata['cwd'])
        if proof['liveness'] != 'LIVE' and endpoint:
            from clinx.provider.native_writer_probe import observe
            proof = observe(endpoint, directory.native_home, thread_id,
                result['native_status']['turn_id'], metadata['cwd'])
        result['native_writer_observation'] = dict(proof, observed_at=stamp())
        if proof['liveness'] == 'LIVE':
            live = 'active'
    result['native_display_state'] = 'RUNNING' if live == 'active' else 'IDLE' if live == 'idle' else 'UNKNOWN'
    result['codex_running'] = live == 'active'
    if context:
        result.update(history.context(thread_id, node, metadata, recent_turns, max_bytes, cursor=cursor))
    # Recheck revocation before returning native content.
    current = directory._scope(node)
    if current is None or not current.allows_project(metadata['cwd']):
        raise NodeProtocolError('SHARING_SCOPE_DENIED', 'Native read grant was revoked')
    return result
