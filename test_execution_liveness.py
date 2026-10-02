"""Ten required scenarios exercise the real reconciler, finalizer and SQLite."""
import json
import time
from types import SimpleNamespace

import pytest

import native_provider
import execution_liveness as live
from app_server import AppServerProtocolError
from completion_runtime import CompletionRuntime
from m9_integration import ClinxIntegration
from task_registry import TaskRegistry, TaskRegistryError
from test_execution_owner_routing import fixture, EXEC, TID, TURN
from tool_delivery import ToolDeliveryLedger

RESULT = ("CLINX_EXECUTION_RESULT\nSTATUS=PASS\nSUMMARY=owner completed\n"
          "CHANGED_FILES=NONE\nVALIDATION=exact terminal\nBLOCKERS=NONE\nNEXT_STATE=COMPLETED")


def status(dispatcher, registry):
    api = ClinxIntegration(dispatcher.cfg, registry, dispatcher, None, None)
    return api.get_status(execution_ref=EXEC)


def retain(registry):
    assert registry.has_execution_lease(EXEC)
    assert registry.get_execution_result(EXEC) is None
    assert registry.reclaim_stale_worktree_leases() == 0
    assert registry.has_execution_lease(EXEC)


def unavailable(monkeypatch, endpoints=None):
    original = native_provider.existing_client
    def client(endpoint, *args, **kwargs):
        if endpoints is None or endpoint in endpoints:
            raise AppServerProtocolError("app-server returned malformed websocket JSON")
        return original(endpoint, *args, **kwargs)
    monkeypatch.setattr(native_provider, "existing_client", client)


def test_01_live_owner_beats_nonowner_interrupted(tmp_path, monkeypatch):
    d, r, t, _ = fixture(tmp_path, monkeypatch)
    out = d.reconcile_execution(EXEC)
    assert out["state"] == "CODEX_RUNNING"
    assert out["provider_liveness"] == "LIVE"
    assert status(d, r)["codex_running"]
    retain(r)


def test_02_completed_owner_beats_notloaded(tmp_path, monkeypatch):
    d, r, t, _ = fixture(tmp_path, monkeypatch, states=("notLoaded", "idle"), status="completed", result=RESULT)
    assert d.reconcile_execution(EXEC)["state"] == "COMPLETED"
    assert not r.has_execution_lease(EXEC)
    assert status(d, r)["provider_liveness"] == "TERMINAL"


def test_03_malformed_peer_does_not_cancel_live_owner(tmp_path, monkeypatch):
    d, r, t, _ = fixture(tmp_path, monkeypatch)
    unavailable(monkeypatch, {"/managed"})
    out = d.reconcile_execution(EXEC)
    assert (out["state"], out["transport_health"], out["codex_running"]) == ("CODEX_RUNNING", "DEGRADED", True)
    retain(r)


def test_04_short_unknown_clears_old_running_retains_lease(tmp_path, monkeypatch):
    d, r, t, _ = fixture(tmp_path, monkeypatch)
    unavailable(monkeypatch)
    out = d.reconcile_execution(EXEC)
    assert (out["state"], out["provider_liveness"], out["codex_running"]) == ("TRANSPORT_UNCERTAIN", "UNKNOWN", False)
    assert not r.get_task(t.task_id).codex_running
    retain(r)


def age(r):
    with r._connect() as conn:
        conn.execute("UPDATE execution_liveness SET unknown_since=?,last_provider_activity_at=NULL,last_live_owner_at=NULL",
                     (time.time() - live.LIVENESS_GRACE_SECONDS - 10,))


def test_05_expired_unknown_recovery_survives_restart_and_release(tmp_path, monkeypatch):
    d, r, t, _ = fixture(tmp_path, monkeypatch)
    unavailable(monkeypatch)
    d.reconcile_execution(EXEC)
    age(r)
    out = d.reconcile_execution(EXEC)
    assert out["state"] == "RECOVERY_REQUIRED"
    assert not out["codex_running"]
    retain(r)
    r2 = TaskRegistry(r.path)
    retain(r2)
    with pytest.raises(TaskRegistryError, match="lease release"):
        r2.release_execution(t.task_id, EXEC)


def test_06_recovery_rediscovers_owner(tmp_path, monkeypatch):
    d, r, t, _ = fixture(tmp_path, monkeypatch)
    client = native_provider.existing_client
    unavailable(monkeypatch)
    d.reconcile_execution(EXEC)
    age(r)
    assert d.reconcile_execution(EXEC)["state"] == "RECOVERY_REQUIRED"
    monkeypatch.setattr(native_provider, "existing_client", client)
    assert d.reconcile_execution(EXEC)["state"] == "CODEX_RUNNING"
    assert r.get_task(t.task_id).codex_running
    retain(r)
    with pytest.raises(TaskRegistryError, match="lease release"):
        r.release_execution(t.task_id, EXEC)


def test_07_two_active_claims_conflict(tmp_path, monkeypatch):
    d, r, t, _ = fixture(tmp_path, monkeypatch, states=("active", "active"))
    # A thread claiming active still conflicts even if its turn projection lags.
    out = d.reconcile_execution(EXEC)
    assert out["provider_liveness"] == "UNKNOWN"
    assert out["liveness_reason"] == "OWNERSHIP_CONFLICT"
    assert not out["codex_running"]
    retain(r)


def test_08_status_never_reuses_historical_true(tmp_path, monkeypatch):
    d, r, t, _ = fixture(tmp_path, monkeypatch)
    with r._connect() as conn:
        conn.execute("UPDATE tasks SET codex_running=1 WHERE task_id=?", (t.task_id,))
    assert r.get_task(t.task_id).codex_running  # legacy persisted projection
    out = status(d, r)
    assert not out["codex_running"] and not out["CODEX_RUNNING"]
    assert out["provider_liveness"] == "UNKNOWN"
    retain(r)


def test_09_host_delivery_updates_separate_clock_not_liveness(tmp_path, monkeypatch):
    d, r, t, _ = fixture(tmp_path, monkeypatch)
    unavailable(monkeypatch)
    d.reconcile_execution(EXEC)
    age(r)
    ledger = ToolDeliveryLedger(r, EXEC)
    # Exercise the actual ACK handler with exact tool/thread/turn correlation.
    identity = dict(thread_id=TID, turn_id=TURN, namespace="clinx", tool="clinx_host_operation")
    ledger.admit({"id": 1, "params": {"callId": "call", "arguments": {}}}, identity)
    with r._connect() as conn:
        conn.execute("UPDATE host_tool_deliveries SET host_execution_ref='host',host_exit_code=0")
    ledger.sent("call", 2)
    ledger.observe({"method": "item/completed", "params": {"threadId": TID, "turnId": TURN,
        "item": {"type": "dynamicToolCall", "id": "call", "namespace": "clinx",
                 "tool": "clinx_host_operation", "success": True,
                 "contentItems": [{"text": json.dumps({"host_execution_ref": "host", "execution_ref": EXEC})}]}}})
    out = d.reconcile_execution(EXEC)
    assert out["state"] == "TRANSPORT_UNCERTAIN"
    out = status(d, r)
    assert out["last_host_delivery_at"] is not None
    assert out["last_provider_activity_at"] is None
    assert out["last_live_owner_at"] is None
    assert not out["codex_running"]
    retain(r)


def test_10_exact_completion_handoff_finalizes(tmp_path, monkeypatch):
    d, r, t, _ = fixture(tmp_path, monkeypatch, states=("notLoaded", "idle"), status="completed", result=RESULT)
    runtime = CompletionRuntime(r, d.reconcile_execution)
    identity = runtime.identity_for(EXEC)
    runtime.register(identity)
    runtime.notify(identity, {"method": "turn/completed", "params": {"threadId": TID, "turn": {"id": TURN}}})
    out = runtime.process(EXEC)
    assert out["state"] == "COMPLETED"
    assert out["completion_delivery"] == "DONE"
    assert r.get_execution_result(EXEC).raw_result == RESULT
    assert not r.has_execution_lease(EXEC)


def test_runtime_recover_unknown_keeps_pending(tmp_path, monkeypatch):
    d, r, t, _ = fixture(tmp_path, monkeypatch)
    unavailable(monkeypatch)
    runtime = CompletionRuntime(r, d.reconcile_execution)
    assert runtime.recover(EXEC)["completion_delivery"] == "PENDING"
    age(r)
    assert runtime.process(EXEC)["state"] == "RECOVERY_REQUIRED"
    assert runtime.inspect(EXEC)["state"] == "PENDING"
    retain(r)


def test_terminal_owner_with_unavailable_peer_cannot_release(tmp_path, monkeypatch):
    d, r, t, _ = fixture(tmp_path, monkeypatch, states=("notLoaded", "idle"), status="completed", result=RESULT)
    unavailable(monkeypatch, {"/managed"})
    out = d.reconcile_execution(EXEC)
    assert out["provider_liveness"] == "TERMINAL"
    assert out["transport_health"] == "DEGRADED"
    assert not out["authoritative"]
    retain(r)


def test_exact_turn_mismatch_and_stale_status_are_unknown(tmp_path, monkeypatch):
    d, r, t, _ = fixture(tmp_path, monkeypatch)
    d.reconcile_execution(EXEC)
    with r._connect() as conn:
        conn.execute("UPDATE execution_liveness SET observed_at=?", (time.time() - 121,))
    assert status(d, r)["provider_liveness"] == "UNKNOWN"
    assert not status(d, r)["codex_running"]
    rows = [dict(endpoint="a", state="active", thread_id=TID, turn_id="different", turn_status="inProgress")]
    assert live.classify(rows, TID, TURN)["provider_liveness"] == "UNKNOWN"


def test_unknown_cannot_enter_finalizer_or_cancellation(tmp_path, monkeypatch):
    from m9_integration import ExecutionFinalizer
    d, r, t, _ = fixture(tmp_path, monkeypatch)
    unavailable(monkeypatch)
    d.reconcile_execution(EXEC)
    with pytest.raises(TaskRegistryError, match="fresh exact owner"):
        ExecutionFinalizer(r).finalize(execution_ref=EXEC, task_id=t.task_id, turn_id=TURN, raw_result=RESULT)
    with pytest.raises(TaskRegistryError, match="fresh exact owner"):
        r.finalize_cancellation(EXEC)
    assert r.get_task(t.task_id).execution_state == "TRANSPORT_UNCERTAIN"
    retain(r)


def test_active_owner_reappears_between_terminal_read_and_release(tmp_path, monkeypatch):
    d, r, t, _ = fixture(tmp_path, monkeypatch, states=("notLoaded", "idle"), status="completed", result=RESULT)
    original = native_provider.observe_execution
    count = 0
    def changing(cfg, tid, turn_id):
        nonlocal count
        count += 1
        if count == 2:
            owner = native_provider.existing_client("/native")
            owner.state, owner.turn_status = "active", "inProgress"
        return original(cfg, tid, turn_id)
    monkeypatch.setattr(native_provider, "observe_execution", changing)
    out = d.reconcile_execution(EXEC)
    assert out["provider_liveness"] == "LIVE"
    assert r.get_task(t.task_id).execution_state == "CODEX_RUNNING"
    retain(r)


def test_incomplete_active_claim_cannot_be_hidden_by_transport_error(tmp_path, monkeypatch):
    d, r, t, _ = fixture(tmp_path, monkeypatch, states=("active", "active"))
    owner = native_provider.existing_client("/managed")
    def broken(*args, **kwargs):
        raise AppServerProtocolError("malformed JSON")
    owner.thread_turns_list = broken
    out = d.reconcile_execution(EXEC)
    assert out["ownership_conflict"]
    assert out["transport_health"] == "DEGRADED"
    assert not out["codex_running"]
    retain(r)


def test_notify_activity_is_separate_from_owner_and_host(tmp_path, monkeypatch):
    d, r, t, _ = fixture(tmp_path, monkeypatch)
    runtime = CompletionRuntime(r, d.reconcile_execution)
    identity = runtime.identity_for(EXEC)
    runtime.register(identity)
    runtime.notify(identity, {"method": "turn/completed", "params": {"threadId": TID, "turn": {"id": TURN}}})
    out = status(d, r)
    assert out["last_provider_activity_at"]
    assert out["last_host_delivery_at"] is None
    assert out["last_live_owner_at"] is None
    assert out["provider_liveness"] == "UNKNOWN"
    assert runtime.inspect(EXEC)["state"] == "PENDING"
    retain(r)


def test_non_authoritative_result_does_not_consume_handoff(tmp_path, monkeypatch):
    d, r, t, _ = fixture(tmp_path, monkeypatch)
    runtime = CompletionRuntime(r, lambda *a, **kw: {"authoritative": False})
    identity = runtime.identity_for(EXEC)
    runtime.register(identity)
    assert runtime.process(EXEC)["completion_delivery"] == "PENDING"
    retain(r)


def test_exact_turn_on_later_page(tmp_path, monkeypatch):
    d, r, t, _ = fixture(tmp_path, monkeypatch)
    owner = native_provider.existing_client("/native")
    original = owner.thread_turns_list
    calls = []
    def paged(tid, **kw):
        calls.append(kw.get("cursor"))
        if not kw.get("cursor"):
            return {"data": [{"id": "another-turn", "status": "completed"}], "nextCursor": "second"}
        return original(tid, **kw)
    owner.thread_turns_list = paged
    assert d.reconcile_execution(EXEC)["codex_running"]
    assert calls == [None, "second"]
    retain(r)


def test_legacy_recovery_cannot_bypass_reobservation_with_result(tmp_path, monkeypatch):
    from m9_integration import ExecutionFinalizer
    d, r, t, _ = fixture(tmp_path, monkeypatch)
    r.set_execution_state(t.task_id, "RECOVERY_REQUIRED", current_stage="RECOVERY_REQUIRED")
    with pytest.raises(TaskRegistryError, match="fresh exact owner"):
        ExecutionFinalizer(r).finalize(execution_ref=EXEC, task_id=t.task_id, turn_id=TURN, raw_result=RESULT)
    r.request_cancellation(EXEC)
    with pytest.raises(TaskRegistryError, match="fresh exact owner"):
        r.finalize_cancellation(EXEC)
    retain(r)
