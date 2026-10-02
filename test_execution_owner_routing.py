"""Regression: a non-owning daemon's interrupted history is not live cancellation."""
import dataclasses
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import native_provider
from app_server import AppServerProtocolError
from task_registry import TaskRegistry
import test_m13 as lifecycle_tests
from test_m13 import make_task, route

TID = "thread-lifecycle"
TURN = "turn-lifecycle"
EXEC = "exec_owner_routing"


def observation(states):
    rows = [{"endpoint": ep, "state": state} for ep, state in zip(("/managed", "/native"), states)]
    loaded = [r for r in rows if r["state"] not in ("UNKNOWN", "OFFLINE", "notLoaded", "unloaded")]
    return {"observations": rows, "ownership_conflict": len(loaded) > 1}


class Provider(lifecycle_tests.M13LifecycleRoutingTests.FakeProvider):
    def __init__(self, state, status, calls, result=None):
        super().__init__(None, turn_status=status)
        self.state, self.calls, self.result = state, calls, result

    def thread_read(self, thread_id):
        assert thread_id == TID
        return {"id": TID, "status": {"type": self.state}}

    def thread_turns_list(self, thread_id, **kwargs):
        page = super().thread_turns_list(thread_id, **kwargs)
        if self.result:
            page["data"][0]["items"] = [{"type": "agentMessage", "text": self.result}]
        return page

    def turn_interrupt(self, thread_id, turn_id):
        self.calls.append((thread_id, turn_id))
        self.state, self.turn_status = "idle", "interrupted"
        return True


def fixture(tmp_path, monkeypatch, states=("notLoaded", "active"), status="inProgress", result=None):
    registry = TaskRegistry(tmp_path / "tasks.sqlite3")
    task = make_task(registry, routing=route(conversation_binding=TID, transport="local"))
    registry.bind_conversation(task_id=task.task_id, thread_id=TID, session_id="session-lifecycle",
                              project_id=None, app_server_version="test")
    with registry.execution(task.task_id, execution_ref=EXEC, retain=True):
        registry.set_execution_state(task.task_id, "CODEX_RUNNING", current_stage="Codex turn",
                                     turn_id=TURN, codex_running=True)
    dispatcher = lifecycle_tests.M13LifecycleRoutingTests()._dispatcher(tmp_path, registry, [])
    dispatcher.cfg.app_server = dataclasses.replace(dispatcher.cfg.app_server,
        local_socket="/managed", native_socket="/native")
    dispatcher._uses_default_client_factory = True
    dispatcher.linear = None
    calls = {"/managed": [], "/native": []}
    providers = {"/managed": Provider(states[0], "interrupted", calls["/managed"]),
                 "/native": Provider(states[1], status, calls["/native"], result)}
    dispatcher.client_factory = Mock(return_value=providers["/managed"])
    monkeypatch.setattr(native_provider, "observe_thread", lambda cfg, tid: observation(states))
    monkeypatch.setattr(native_provider, "existing_client", lambda ep, *a, **kw: providers[ep])
    return dispatcher, registry, task, calls


def test_nonowner_interrupted_cannot_cancel_live_owner(tmp_path, monkeypatch):
    dispatcher, registry, task, calls = fixture(tmp_path, monkeypatch)
    result = dispatcher.reconcile_execution(EXEC, reclaim_stale=False)
    assert result["state"] == "CODEX_RUNNING"
    assert registry.get_active_execution(EXEC) is not None
    assert registry.get_task(task.task_id).codex_running
    dispatcher.client_factory.assert_not_called()
    assert calls == {"/managed": [], "/native": []}


def test_unloaded_interrupted_is_uncertainty_not_cancellation(tmp_path, monkeypatch):
    dispatcher, registry, task, calls = fixture(tmp_path, monkeypatch, states=("notLoaded", "notLoaded"))
    result = dispatcher.reconcile_execution(EXEC, reclaim_stale=False)
    assert result["state"] == "TRANSPORT_UNCERTAIN"
    assert not result["authoritative"]
    assert registry.get_active_execution(EXEC) is not None
    assert not registry.get_task(task.task_id).codex_running


def test_cancel_targets_actual_owner_without_resuming(tmp_path, monkeypatch):
    dispatcher, registry, task, calls = fixture(tmp_path, monkeypatch)
    result = dispatcher.cancel_execution(EXEC)
    assert result["status"] == "CANCELLED"
    assert calls == {"/managed": [], "/native": [(TID, TURN)]}
    dispatcher.client_factory.assert_not_called()


def test_cancel_with_no_owner_retains_lease(tmp_path, monkeypatch):
    dispatcher, registry, task, calls = fixture(tmp_path, monkeypatch, states=("notLoaded", "notLoaded"))
    result = dispatcher.cancel_execution(EXEC)
    assert result["status"] == "CANCELLATION_PENDING"
    assert registry.get_active_execution(EXEC) is not None
    assert calls == {"/managed": [], "/native": []}


def test_completed_owner_still_finalizes(tmp_path, monkeypatch):
    report = "CLINX_EXECUTION_RESULT\nSTATUS=PASS\nSUMMARY=owner completed\nCHANGED_FILES=NONE\nVALIDATION=test\nBLOCKERS=NONE\nNEXT_STATE=COMPLETED"
    dispatcher, registry, task, calls = fixture(tmp_path, monkeypatch,
        states=("notLoaded", "idle"), status="completed", result=report)
    result = dispatcher.reconcile_execution(EXEC, reclaim_stale=False)
    assert result["state"] == "COMPLETED"
    assert registry.get_execution_result(EXEC).summary == "owner completed"
    assert registry.get_active_execution(EXEC) is None


@pytest.mark.parametrize("states,read_only,expected", [
    (("notLoaded", "active"), True, "/native"),
    (("active", "notLoaded"), True, "/managed"),
    (("notLoaded", "idle"), True, "/native"),
    (("notLoaded", "notLoaded"), True, "/managed"),
    (("notLoaded", "active"), False, "/native"),
    (("notLoaded", "notLoaded"), False, None),
    (("active", "active"), True, None),
    (("idle", "UNKNOWN"), True, None),
    (("OFFLINE", "OFFLINE"), True, None),
])
def test_lifecycle_selection_is_bounded_and_fail_closed(monkeypatch, states, read_only, expected):
    cfg = SimpleNamespace(app_server=SimpleNamespace(request_timeout_seconds=3))
    monkeypatch.setattr(native_provider, "observe_thread", lambda cfg, tid: observation(states))
    create = Mock()
    monkeypatch.setattr(native_provider, "existing_client", create)
    if expected is None:
        with pytest.raises(AppServerProtocolError):
            native_provider.select_execution_client(cfg, TID, read_only=read_only)
        create.assert_not_called()
    else:
        native_provider.select_execution_client(cfg, TID, read_only=read_only)
        create.assert_called_once_with(expected, 3, read_only=read_only)
