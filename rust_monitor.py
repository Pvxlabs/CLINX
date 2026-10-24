"""Existing Observer -> existing MCP -> Rust Centre read projections.

No database, business commands, provider startup or legacy task-history import.
"""
from __future__ import annotations

import json
import os
import socket
import uuid

from node_protocol import NodeProtocolError

READ_TOOLS = frozenset({'clinx_get_capabilities', 'clinx_list_observations', 'clinx_get_observation',
                        'clinx_get_observation_context', 'clinx_get_observation_activity'})
MAX_REPLY = 2 * 1024 * 1024


class RustObservationDirectory:
    """Read managed content through its exact production Provider identity.

    Centre projections authorize scope; the Rust integration selects the actual
    execution and turn. Neither an API-home history index nor latest-turn reads
    may supply content for a managed execution.
    """
    def __init__(self, directory, integration):
        self.directory = directory
        self.integration = integration

    def __getattr__(self, name):
        return getattr(self.directory, name)

    @staticmethod
    def _identity(item):
        turn = item['turn']
        return (item['node_id'], item['project'], item['native_thread_id'],
                item.get('task_ref'), turn.get('execution_ref'), turn.get('turn_id'),
                item['source'], item['binding_evidence'])

    def context(self, observation_id, *, cursor=None):
        item = self.directory.detail(observation_id, limit=1)['item']
        if item['node_id'] != self.integration.cfg.runtime_host:
            return self.directory.context(observation_id, cursor=cursor)
        if item['source'] != 'CLINX_MANAGED':
            # Existing Desktop Observer applies its own current native grants.
            # The API identity never opens a different user's native index.
            from rust_reads import desktop_read
            from rust_authority import RustAuthorityError
            try:
                result = desktop_read(item['native_thread_id'], context=True,
                                      recent_turns=4, max_bytes=16000, cursor=cursor)
            except RustAuthorityError as exc:
                raise NodeProtocolError(str(exc), 'Native owner context unavailable') from None
            if result.get('project') != item['project']:
                raise NodeProtocolError('THREAD_SCOPE_MISMATCH', 'Native project differs')
            self._recheck(observation_id, item)
            return dict(result, schema_version='clinx-observation-v1', read_only=True)
        node, project, thread, task, execution, turn, _, _ = self._identity(item)
        if item['binding_evidence'] != 'CANONICAL_ROUTE' or not all((thread, task, execution, turn)):
            raise NodeProtocolError('EXACT_RUST_EXECUTION_REQUIRED', 'Managed context requires an exact execution and turn')
        from rust_authority import RustAuthorityError
        try:
            result = self.integration.get_context(task_ref=task, thread_id=thread,
                host=node, execution_ref=execution, recent_turns=4, max_bytes=16000, cursor=cursor)
        except RustAuthorityError as exc:
            raise NodeProtocolError(str(exc), 'Exact Rust execution context unavailable') from None
        if (result.get('read_only') is not True or result.get('thread_id') != thread
                or result.get('task_ref') != task or result.get('execution_ref') != execution
                or result.get('execution_turn_ref') != turn
                or result.get('context_scope') != 'EXACT_EXECUTION_TURN'):
            raise NodeProtocolError('EXECUTION_TURN_IDENTITY_CONFLICT', 'Managed content identity differs')
        # Revalidate current trust/project grants after the Provider call, then
        # reject a changed projection instead of returning another turn's page.
        self._recheck(observation_id, item)
        return dict(result, schema_version='clinx-observation-v1', read_only=True)

    def _recheck(self, observation_id, item):
        current = self.directory.detail(observation_id, limit=1)['item']
        if self._identity(current) != self._identity(item):
            raise NodeProtocolError('EXECUTION_TURN_IDENTITY_CONFLICT', 'Observation changed during context read')


class MCPReader:
    def __init__(self, endpoint):
        self.endpoint = str(endpoint)

    def call(self, name, arguments=None):
        if name not in READ_TOOLS:
            raise NodeProtocolError('OBSERVER_READ_ONLY', 'Only existing observation reads are permitted')
        ident = uuid.uuid4().hex
        request = dict(jsonrpc='2.0', id=ident, method='tools/call',
                       params=dict(name=name, arguments=arguments or {}))
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(15)
                peer.connect(self.endpoint)
                peer.sendall(json.dumps(request).encode() + b'\n')
                with peer.makefile('rb') as stream:
                    wire = stream.readline(MAX_REPLY + 1)
            if len(wire) > MAX_REPLY or not wire.endswith(b'\n'):
                raise ValueError()
            reply = json.loads(wire)
            if reply.get('id') != ident or reply.get('error'):
                raise ValueError()
            result = reply['result']
            if result.get('isError'):
                # Preserve public source/scope failures; no transport or secret text.
                payload = result.get('structuredContent') or {}
                code = payload.get('error_code', 'RUST_OBSERVATION_UNAVAILABLE')
                raise NodeProtocolError(code, 'Rust observation read was refused')
            payload = result['structuredContent']
            if payload.get('read_only') is not True:
                raise ValueError()
            return payload
        except NodeProtocolError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise NodeProtocolError('RUST_OBSERVATION_UNAVAILABLE', 'Existing Rust MCP read endpoint unavailable') from None


class RustObservationProxy:
    def __init__(self, reader):
        self.reader = reader

    def list(self, **arguments):
        return self.reader.call('clinx_list_observations', arguments)

    def detail(self, observation_id, **arguments):
        return self.reader.call('clinx_get_observation', dict(observation_id=observation_id, **arguments))

    def context(self, observation_id, **arguments):
        return self.reader.call('clinx_get_observation_context', dict(observation_id=observation_id, **arguments))

    def activity(self, observation_id, **arguments):
        return self.reader.call('clinx_get_observation_activity', dict(observation_id=observation_id, **arguments))


class RustMonitorStore:
    """Keep v1 handshakes compatible; current rows come only from the v2 directory.

    The shipping App already merges v2 managed/native observations. Returning no
    legacy v1 rows prevents old Python records from masquerading as Rust state.
    This is a live projection and never initializes a replacement task database.
    """
    observation_directory = None

    def __init__(self, reader):
        self.reader = reader

    def health(self):
        from observer_server import now
        health = self.reader.call('clinx_get_capabilities')
        if health.get('authority') != 'RUST':
            raise NodeProtocolError('RUST_AUTHORITY_REQUIRED', 'Rust authority must be connected')
        self.reader.call('clinx_list_observations', dict(limit=1))
        return dict(schema_version='1', status='OK', read_only=True, observed_at=now(),
                    authority_liveness='UNKNOWN', auth_mode='OBSERVER_BEARER', events_transport='POLLING',
                    authority='RUST', observation_source='RUST_CENTRE_V2')

    def list_tasks(self, state, offset=0):
        from observer_server import now
        # Check the live directory rather than allowing a healthy but disconnected
        # empty compatibility page to conceal a broken observation path.
        self.reader.call('clinx_list_observations', dict(limit=1))
        return dict(schema_version='1', observed_at=now(), items=[], next_offset=None, has_more=False,
                    source='RUST_CENTRE_V2', legacy_history=False)

    def task(self, task_ref):
        from observer_server import APIError
        raise APIError(404, 'RUST_TASKS_USE_OBSERVATIONS_V2')

    def task_events(self, task_ref, after=None):
        return self.task(task_ref)

    def task_activity(self, task_ref, execution_ref, after=None, before=None):
        return self.task(task_ref)


def from_env():
    endpoint = os.environ.get('CLINX_OBSERVER_RUST_MCP')
    if not endpoint:
        return None
    reader = MCPReader(endpoint)
    return RustMonitorStore(reader), RustObservationProxy(reader)
