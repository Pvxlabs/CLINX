"""Exact native selection and public URI contracts; fixtures are not Desktop acceptance."""
import dataclasses
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

import jsonschema
import pytest
import app_server

from m9_integration import M9IntegrationError
from mcp_server import ClinxMCPServer, tool_definitions
from observer_server import ObserverStore
from test_m6 import FakeClient
from test_native_interop import native, hashes, TID, URI
from thread_identity import canonical_codex_uri, ThreadLookupError

NEW_TID = '01a12eee-1234-7890-abcd-123456789abc'


def exact_reader(native, state='idle'):
    native.reader.live_reader = lambda *_: {'state': state, 'observations': [
        {'state': state, 'thread_id': TID, 'session_id': TID, 'cwd': str(native.repo)}]}
    return patch('thread_identity.ThreadIdentityReader', return_value=native.reader)


def test_explicit_uri_create_adopts_once_and_prepares_same_thread(native):
    before = hashes(native)
    with exact_reader(native):
        first = native.integration.prepare_execution(approved=True, task_action='create',
            codex_uri=URI, prompt='bounded native continuation')
        second = native.integration.prepare_execution(approved=True, task_action='create',
            codex_uri=URI, prompt='second bounded continuation')
    assert first['task_action'] == second['task_action'] == 'continue'
    assert first['task_ref'] == second['task_ref']
    assert first['codex_uri'] == second['codex_uri'] == URI
    prepared = native.registry.verify_prepared_execution(first['prepared_execution_ref'])
    assert json.loads(prepared.routing_identity_json)['conversation']['binding'] == TID
    assert native.registry.get_latest_execution_for_task(first['task_ref']) is None
    assert native.registry.get_binding(first['task_ref']).session_id == TID
    for path, digest in before.items():
        if path != str(native.registry.path):
            assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
    schema = next(t for t in tool_definitions() if t['name'] == 'clinx_prepare_execution')
    jsonschema.validate(first, schema['outputSchema'])


@pytest.mark.parametrize('uri,extra,code', [
    (URI + '&hostId=local', {}, 'INVALID_CODEX_THREAD_URI'),
    (URI.replace('p620', 'unknown'), {}, 'UNKNOWN_CODEX_HOST_ID'),
    (URI, {'query': 'similar name'}, 'IDENTITY_SELECTOR_CONFLICT'),
    (URI, {'task_ref': 'task-unrelated'}, 'THREAD_IDENTITY_CONFLICT'),
    (URI, {'project': 'unrelated'}, 'THREAD_SCOPE_MISMATCH'),
    (URI, {'execution_surface': 'HOST_EXECUTOR'}, 'AUTHORITY_REAUTHORIZATION_REQUIRED'),
])
def test_rejected_exact_requests_do_not_adopt_or_dispatch(native, uri, extra, code):
    before = hashes(native)
    with exact_reader(native), pytest.raises((M9IntegrationError, ThreadLookupError)) as error:
        native.integration.prepare_execution(approved=True, codex_uri=uri, prompt='probe', **extra)
    assert code in str(error.value) or getattr(error.value, 'code', None) == code
    assert hashes(native) == before


def test_active_owner_uri_does_not_create_preparation_or_change_history(native):
    before = hashes(native)
    with exact_reader(native, 'active'), pytest.raises(M9IntegrationError, match='NATIVE_ACTIVE_OWNER'):
        native.integration.prepare_execution(approved=True, task_action='create', codex_uri=URI, prompt='probe')
    assert hashes(native) == before


def test_native_adoption_uses_verified_session_version_not_clinx_client_version(native):
    cfg = dataclasses.replace(native.cfg, threads=(), app_server=dataclasses.replace(
        native.cfg.app_server, client_version='1.0.0-m6'))
    native.integration.cfg = native.reader.cfg = native.dispatcher.cfg = cfg
    with exact_reader(native):
        prepared = native.integration.prepare_execution(approved=True, codex_uri=URI, prompt='probe')
    assert native.registry.get_binding(prepared['task_ref']).app_server_version == '0.156.1'


def test_uri_preparation_rejects_persisted_active_execution_even_with_idle_provider(native):
    with exact_reader(native):
        adopted = native.integration.adopt_conversation(codex_uri=URI)
    with native.registry.execution(adopted['task_ref'], execution_ref='exec_fixture', retain=True):
        before = hashes(native)
        with exact_reader(native), pytest.raises(M9IntegrationError, match='ACTIVE_EXECUTION_CONFLICT'):
            native.integration.prepare_execution(approved=True, codex_uri=URI, prompt='probe')
        assert hashes(native) == before


def test_historical_uri_does_not_silently_continue_successor(native):
    observed = {'lookup_status': 'RESOLVED', 'is_current_thread': False,
        'task_ref': 'task_fixture', 'current_thread_id': NEW_TID}
    reader = Mock(); reader.read.return_value = observed
    before = hashes(native)
    with patch('thread_identity.ThreadIdentityReader', return_value=reader), pytest.raises(
            M9IntegrationError, match='THREAD_SUCCESSOR_REQUIRED'):
        native.integration.prepare_execution(approved=True, codex_uri=URI, prompt='probe')
    assert hashes(native) == before


def test_remote_uri_routes_before_local_adoption_without_extra_serialized_locals(native):
    workspace = dataclasses.replace(native.cfg.workspaces[0], alias='air', host='air',
        codex_host_ids=('local-air',))
    router = SimpleNamespace(local_node_id='p620', user_scope='fixture-user', registry=SimpleNamespace(list_nodes=lambda **_: []),
        _node_for=lambda **_: SimpleNamespace(node_id='air'), execute=Mock(return_value={'remote': True}))
    cfg = dataclasses.replace(native.cfg, workspaces=(*native.cfg.workspaces, workspace))
    # BridgeConfig's router is an attached runtime dependency.
    object.__setattr__(cfg, 'node_router', router)
    native.integration.cfg = cfg
    before = hashes(native)
    result = native.integration.prepare_execution(approved=True,
        codex_uri='codex://threads/' + TID + '?hostId=local-air', prompt='remote probe')
    assert result == {'remote': True}
    request = router.execute.call_args.kwargs['request']
    assert request['host'] == 'air'
    assert request['codex_uri'].endswith('hostId=local-air')
    json.dumps(request)  # No self, functions, imports or internal helper locals.
    assert hashes(native) == before


def test_read_only_thread_list_does_not_scan_and_repair_native_metadata():
    class Transport:
        def __init__(self): self.sent = []
        def send(self, request): self.sent.append(request)
        def receive(self, timeout_seconds):
            return {'id': self.sent[-1]['id'], 'result': {'data': [], 'nextCursor': None}}
    transport = Transport()
    client = app_server.CodexAppServerClient(transport)
    client.read_only_observer = True
    assert client.thread_list(limit=10)['data'] == []
    assert transport.sent[-1]['params'] == {'limit': 10, 'useStateDbOnly': True}
    with pytest.raises(app_server.AppServerProtocolError):
        client._request('thread/list', {'limit': 10})
    assert len(transport.sent) == 1


def test_no_uri_create_does_not_reuse_similar_bound_task(native):
    with exact_reader(native):
        adopted = native.integration.adopt_conversation(codex_uri=URI)
    task = native.registry.get_task(adopted['task_ref'])
    client = FakeClient(thread_id=NEW_TID, session_id=NEW_TID)
    native.dispatcher.client_factory = lambda _: client
    native.integration.linear = None
    prepared = native.integration.prepare_execution(approved=True, task_action='create', host='p620',
        project='pilot', title=task.title, summary=task.summary, prompt='probe')
    started = native.integration.start_execution(approved=True, prepared_execution_ref=prepared['prepared_execution_ref'])
    assert started['task_ref'] != task.task_id
    assert native.registry.get_binding(task.task_id).thread_id == TID
    assert started['codex_uri'] == URI.replace(TID, NEW_TID)
    assert any(c[0] == 'thread/start' for c in client.calls)
    assert not any(c[0] == 'thread/resume' for c in client.calls)
    replay = native.integration.start_execution(approved=True, prepared_execution_ref=prepared['prepared_execution_ref'])
    assert replay['codex_uri'] == started['codex_uri']
    assert sum(c[0] == 'thread/start' for c in client.calls) == 1
    server = ClinxMCPServer(native.integration)
    response = server.handle({'id': 1, 'method': 'tools/call', 'params': {
        'name': 'clinx_get_status', 'arguments': {'task_ref': started['task_ref']}}})['result']
    public = json.loads(response['content'][0]['text'])
    assert public['codex_uri'] == started['codex_uri']
    assert 'thread_id' not in public


def test_observer_uri_preserves_selected_execution_and_is_read_only(native):
    test_no_uri_create_does_not_reuse_similar_bound_task(native)
    with native.registry._connect() as c:
        ref = c.execute('SELECT task_id FROM executions').fetchone()[0]
        # A successor does not change the captured thread of this execution.
        c.execute('UPDATE conversation_bindings SET thread_id=? WHERE task_id=?', (NEW_TID[:-1] + 'd', ref))
    before = hashes(native)
    store = ObserverStore(native.registry.path, codex_host_ids={'p620': 'remote-ssh-discovered:p620'})
    projected = store.task(ref)
    assert projected['codex_uri'] == URI.replace(TID, NEW_TID)
    assert projected['native_conversation']['thread_id'] == NEW_TID
    status = native.integration.get_status(task_ref=ref,
        execution_ref=projected['execution_ref'])
    assert status['codex_uri'] == projected['codex_uri']
    assert hashes(native) == before


def test_uri_uses_only_configured_unambiguous_host_alias(native):
    assert canonical_codex_uri(TID, cfg=native.cfg, host='p620') == URI
    cfg = dataclasses.replace(native.cfg, workspaces=tuple(dataclasses.replace(w,
        codex_host_ids=('local', 'remote-ssh-discovered:p620')) for w in native.cfg.workspaces))
    assert canonical_codex_uri(TID, cfg=cfg, host='p620') == 'codex://threads/' + TID


@pytest.mark.parametrize('uri', [None, 'codex://threads/' + TID, URI])
def test_observer_schema_accepts_canonical_uri_and_legacy_response(uri):
    root = Path(__file__).resolve().parent
    schema = json.loads((root / 'docs/monitor/observer-v1.schema.json').read_text())
    examples = json.loads((root / 'docs/monitor/api-examples.json').read_text())
    task = next(e['response'] for e in examples['examples'] if e['name'] == 'pass_detail')
    jsonschema.validate(task, schema)
    task['codex_uri'] = uri
    jsonschema.validate(task, schema)


@pytest.mark.parametrize('uri', ['https://threads/' + TID, 'codex://threads/not-a-thread',
    URI + '&hostId=local', 'codex://threads/' + TID + '?token=private', URI + '#fragment'])
def test_observer_schema_rejects_noncanonical_uri(uri):
    root = Path(__file__).resolve().parent
    schema = json.loads((root / 'docs/monitor/observer-v1.schema.json').read_text())
    examples = json.loads((root / 'docs/monitor/api-examples.json').read_text())
    task = next(e['response'] for e in examples['examples'] if e['name'] == 'pass_detail')
    task['codex_uri'] = uri
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(task, schema)
