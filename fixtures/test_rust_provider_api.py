"""Python transport/exception/tuple/patch binding smoke; wire/process tests are Rust."""
from unittest.mock import Mock
import pytest
from clinx.provider.app_server import (AppServerRemoteError, AppServerTransportError,
    AppServerWriterConflict, CodexAppServerClient, ModelCapabilityError,
    ProcessStdioTransport, extract_version, versions_compatible, transport_lifecycle_state)


def test_python_process_handle_and_postclose_lifecycle_binding():
    transport = ProcessStdioTransport(("/bin/cat",))
    try:
        transport.connect()
        assert transport._process is None  # Normal Python never owns the child.
    finally:
        transport.close()
    assert transport_lifecycle_state(transport) == 'unavailable'


def test_rust_process_exec_failure_is_synchronous():
    transport = ProcessStdioTransport(('/missing-clinx-provider-contract-fixture',))
    with pytest.raises(AppServerTransportError, match='failed to start'):
        transport.connect()
    assert transport._session is None


def test_unsubscribe_failure_is_recorded_without_replay_or_active_turn_release():
    transport = Mock()
    client = CodexAppServerClient(transport)
    client._owned_thread_ids = {'idle', 'live'}
    client._owned_turns = {'live': 'running-turn'}
    client._request = Mock(side_effect=AppServerTransportError('ACK lost'))
    client.close()
    client._request.assert_called_once_with('thread/unsubscribe', {'threadId': 'idle'})
    assert client.writer_release == {'idle': 'UNCONFIRMED:AppServerTransportError'}
    assert client._owned_thread_ids == {'live'}
    assert client._owned_turns == {'live': 'running-turn'}
    transport.close.assert_called_once()


@pytest.mark.parametrize('other', [False, True])
def test_writer_conflict_is_exact_and_original_unknown_error_identity_survives(other):
    client = CodexAppServerClient(Mock())
    remote = AppServerRemoteError('thread/resume', {'code': -32600,
        'message': 'thread other already has an active writer' if other else 'thread exact already has an active writer'})
    client._request = Mock(side_effect=remote)
    with pytest.raises(AppServerRemoteError) as caught:
        client.thread_resume('exact')
    if other:
        assert caught.value is remote
    else:
        assert isinstance(caught.value, AppServerWriterConflict)
        assert caught.value.__cause__ is remote
    client._request.assert_called_once_with('thread/resume', {'threadId': 'exact', 'excludeTurns': True})


def test_model_alias_ambiguity_and_malformed_alias_are_not_resolved_by_guessing():
    client = CodexAppServerClient(Mock())
    client.model_list = lambda: {'data': [
        {'id': 'gpt-bad', 'model': 'Terra', 'supportedReasoningEfforts': None},
        {'id': 'gpt-good', 'model': 'Terra', 'supportedReasoningEfforts': ['high']},
        {'id': 'gpt-straße', 'model': 'Straße', 'supportedReasoningEfforts': ['medium']},
    ]}
    with pytest.raises(ModelCapabilityError, match='malformed capability'):
        client.resolve_model('Terra', 'high')
    assert client.resolve_model('STRASSE', 'medium') == ('gpt-straße', 'medium')
    assert client.resolve_model('gpt-good', 'high') == ('gpt-good', 'high')


def test_version_soft_guard_keeps_unbounded_decimal_components():
    huge = '99999999999999999999999999999999999999999999'
    assert extract_version('codex/٣.٢.١') == (3, 2, 1)
    assert not versions_compatible(huge + '.1.0', str(int(huge) + 1) + '.1.0')
    assert versions_compatible(None, '3.0.0')
    assert versions_compatible('3.0.0', '3.999.1')


def test_concurrent_public_api_returns_merge_distinct_owned_turns():
    from concurrent.futures import ThreadPoolExecutor
    import threading
    client = CodexAppServerClient(Mock())
    ready = threading.Barrier(2)
    def request(method, params):
        ready.wait(timeout=3)
        return {'turn': {'id': params['threadId'] + '-turn'}}
    client._request = request
    with ThreadPoolExecutor(2) as pool:
        turns = list(pool.map(lambda thread: client.turn_start(thread, 'run', cwd='/project'), ['a', 'b']))
    assert {turn.turn_id for turn in turns} == {'a-turn', 'b-turn'}
    assert client._owned_turns == {'a': 'a-turn', 'b': 'b-turn'}
