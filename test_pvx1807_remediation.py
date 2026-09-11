"""Repository-resident PVX-1807 residual contract qualification.

Only the transport is scripted.  The tests exercise the real
CodexAppServerClient, adapter, normalizer, and bounded event surface.
"""

from __future__ import annotations

import dataclasses
from collections import deque
from typing import Any

import pytest

from app_server import AppServerTransportError, CodexAppServerClient
from provider_adapters import (
    AdapterBusy,
    CodexProviderAdapter,
    CorrelationError,
    CorrelationStatus,
    DynamicToolConfiguration,
    ProtocolError,
    ScriptedTransport,
    SideEffectUnknown,
    TransportLoss,
)
from provider_adapters.contracts import OperationContext


class StrictPVX1807Transport(ScriptedTransport):
    """Deterministic JSON-RPC peer with explicit wire-side failure modes."""

    def __init__(self) -> None:
        self.thread = "provider-thread-a"
        self.starts = 0
        self.drop_start = False
        self.send_error = False
        self.unsent_error = False
        self.remote_reject = False
        self.malformed_reply = False
        self.server_request_before_drop = False
        self.tool_before_reply = False
        self.stale_second_tool = False
        super().__init__(self._on_send)

    def _on_send(self, message: dict[str, Any], transport: ScriptedTransport) -> None:
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            self.queue({"id": request_id, "result": {"serverInfo": {"name": "fixture", "version": "test"}}})
        elif method == "thread/start":
            self.queue({"id": request_id, "result": {"thread": {"id": self.thread}}})
        elif method == "thread/resume":
            self.queue({"id": request_id, "result": {"thread": {"id": self.thread}}})
        elif method == "model/list":
            self.queue({"id": request_id, "result": {"data": []}})
        elif method == "turn/start":
            self.starts += 1
            if self.unsent_error:
                raise AppServerTransportError("transport not connected")
            if self.send_error:
                raise AppServerTransportError("fixture send raised after write acceptance")
            if self.remote_reject:
                self.queue({"id": request_id, "error": {"code": -32001, "message": "provider rejected turn"}})
                return
            if self.server_request_before_drop:
                self.queue({"id": "approval-1", "method": "approval/request", "params": {}})
            if self.tool_before_reply:
                if self.starts == 2 and self.stale_second_tool:
                    self.queue(self.tool("turn-1", "old-request-in-new-turn"))
                self.queue(self.tool(f"turn-{self.starts}", f"tool-{self.starts}"))
            if self.malformed_reply:
                self.queue({"id": request_id, "result": {"unexpected": True}})
            elif not self.drop_start:
                self.queue({"id": request_id, "result": {"turn": {"id": f"turn-{self.starts}"}}})
        elif method == "turn/interrupt":
            if message["params"]["threadId"] != self.thread:
                self.queue({"id": request_id, "error": {"code": -32602, "message": "wrong provider thread"}})
            else:
                self.queue({"id": request_id, "result": {}})

    def tool(self, turn_id: str, request_id: str) -> dict[str, Any]:
        return {
            "id": request_id,
            "method": "item/tool/call",
            "params": {
                "namespace": "fixture",
                "tool": "execute",
                "threadId": self.thread,
                "turnId": turn_id,
                "arguments": {},
            },
        }


class EOFAfterQueuedTransport(StrictPVX1807Transport):
    def receive(self, timeout_seconds: float) -> dict[str, Any]:
        if self.incoming:
            return self.incoming.popleft()
        raise AppServerTransportError("connection closed")


def make_fixture(tmp_path, **flags: Any):
    transport = StrictPVX1807Transport()
    for name, value in flags.items():
        setattr(transport, name, value)
    adapter = CodexProviderAdapter(transport, timeout_seconds=0.01)
    session = adapter.create_session(cwd=str(tmp_path), clinx_session_id="clinx-session-a")
    context = OperationContext("execution-a", "attempt-a", session.clinx_session_id, "op-a", session.connection_generation)
    return adapter, transport, session, context


def terminal(transport: StrictPVX1807Transport, turn_id: str = "turn-1", event_id: str = "terminal-1") -> dict[str, Any]:
    return {
        "method": "turn/completed",
        "params": {"threadId": transport.thread, "turnId": turn_id, "status": "completed", "eventId": event_id},
    }


def test_pa01_handle_routing_and_generation_continuity(tmp_path):
    transports: list[StrictPVX1807Transport] = []

    def factory():
        transport = StrictPVX1807Transport()
        transports.append(transport)
        return transport

    adapter = CodexProviderAdapter(transport_factory=factory, timeout_seconds=0.01)
    try:
        session = adapter.create_session(cwd=str(tmp_path), clinx_session_id="clinx-session-a")
        context = OperationContext("execution-a", "attempt-a", session.clinx_session_id, "op-a", session.connection_generation)
        adapter.start_turn(session, context, "A", cwd=str(tmp_path))
        interrupt = adapter.interrupt(context)
        interrupt_wire = [m for m in transports[0].sent if m.get("method") == "turn/interrupt"][0]
        assert interrupt_wire["params"]["threadId"] == session.provider_handle
        assert interrupt.outcome.code.value == "cancel_requested"
        rebound = adapter.reconnect(session)
        assert rebound.provider_handle == session.provider_handle
        assert rebound.connection_generation == adapter.connection_generation
        assert adapter._operations["op-a"][0].execution_id == "execution-a"
    finally:
        adapter.close()


def test_pa02_possible_send_is_quarantined_across_reconnect_and_new_id(tmp_path):
    transports: list[StrictPVX1807Transport] = []

    def factory():
        transport = StrictPVX1807Transport()
        transports.append(transport)
        return transport

    adapter = CodexProviderAdapter(transport_factory=factory, timeout_seconds=0.01)
    try:
        session = adapter.create_session(cwd=str(tmp_path), clinx_session_id="clinx-session-a")
        context = OperationContext("execution-a", "attempt-a", session.clinx_session_id, "op-a", session.connection_generation)
        transports[0].send_error = True
        with pytest.raises(SideEffectUnknown):
            adapter.start_turn(session, context, "possibly sent", cwd=str(tmp_path))
        rebound = adapter.reconnect(session)
        retry = dataclasses.replace(context, connection_generation=rebound.connection_generation)
        with pytest.raises((SideEffectUnknown, AdapterBusy)):
            adapter.start_turn(rebound, retry, "same or new id", cwd=str(tmp_path))
        with pytest.raises((SideEffectUnknown, AdapterBusy)):
            adapter.start_turn(rebound, dataclasses.replace(retry, operation_id="op-new"), "bypass", cwd=str(tmp_path))
        assert sum(t.starts for t in transports) == 1
    finally:
        adapter.close()


def test_pa02_definitely_unsent_and_remote_rejection_remain_recoverable(tmp_path):
    adapter, transport, session, context = make_fixture(tmp_path, unsent_error=True)
    try:
        with pytest.raises(TransportLoss):
            adapter.start_turn(session, context, "not sent", cwd=str(tmp_path))
        transport.unsent_error = False
        assert adapter.start_turn(session, context, "recover", cwd=str(tmp_path)).outcome.code.value == "accepted"
    finally:
        adapter.close()

    adapter, transport, session, context = make_fixture(tmp_path, remote_reject=True)
    try:
        with pytest.raises(ProtocolError):
            adapter.start_turn(session, context, "rejected", cwd=str(tmp_path))
        transport.remote_reject = False
        assert adapter.start_turn(session, context, "recover", cwd=str(tmp_path)).outcome.code.value == "accepted"
    finally:
        adapter.close()


def test_pa02_malformed_post_send_reply_and_nested_request_stay_unknown(tmp_path):
    adapter, transport, session, context = make_fixture(tmp_path, malformed_reply=True)
    try:
        with pytest.raises(SideEffectUnknown):
            adapter.start_turn(session, context, "malformed", cwd=str(tmp_path))
        assert transport.starts == 1
    finally:
        adapter.close()

    adapter, transport, session, context = make_fixture(tmp_path, drop_start=True, server_request_before_drop=True)
    try:
        with pytest.raises(SideEffectUnknown):
            adapter.start_turn(session, context, "nested", cwd=str(tmp_path))
        with pytest.raises((SideEffectUnknown, AdapterBusy)):
            adapter.start_turn(session, context, "retry", cwd=str(tmp_path))
        assert transport.starts == 1
    finally:
        adapter.close()


def test_pa03_historical_observer_cannot_consume_active_event(tmp_path):
    adapter, transport, session, context = make_fixture(tmp_path)
    try:
        adapter.start_turn(session, context, "A", cwd=str(tmp_path))
        transport.queue(terminal(transport))
        adapter.observe(context)
        second = dataclasses.replace(context, operation_id="op-b")
        adapter.start_turn(session, second, "B", cwd=str(tmp_path))
        transport.queue(terminal(transport, "turn-2", "terminal-b"))
        with pytest.raises(AdapterBusy):
            adapter.observe(context)
        observed = adapter.observe(second)
        assert observed.events[0].operation == second
        assert observed.events[0].authority_eligible
    finally:
        adapter.close()


def test_pa03_full_context_rejection_has_no_state_effect_then_valid_observe(tmp_path):
    adapter, transport, session, context = make_fixture(tmp_path)
    try:
        adapter.start_turn(session, context, "A", cwd=str(tmp_path))
        before_sequence = adapter._event_sequence
        before_ids = set(adapter._seen_native_ids)
        replaced = dataclasses.replace(context, execution_id="foreign-execution", attempt_id="foreign-attempt", assignment_id="assignment-a")
        with pytest.raises(CorrelationError):
            adapter.normalize_event(terminal(transport), replaced)
        assert adapter._event_sequence == before_sequence
        assert adapter._seen_native_ids == before_ids
        assert adapter._active_operation_id == context.operation_id
        transport.queue(terminal(transport))
        event = adapter.observe(context).events[0]
        assert event.correlation is CorrelationStatus.EXACT
        assert event.authority_eligible
    finally:
        adapter.close()


def test_pa03_unknown_status_does_not_release_active_operation(tmp_path):
    adapter, transport, session, context = make_fixture(tmp_path)
    try:
        adapter.start_turn(session, context, "A", cwd=str(tmp_path))
        transport.queue({"method": "telemetry/custom", "params": {"threadId": transport.thread, "turnId": "turn-1", "status": "completed"}})
        event = adapter.observe(context).events[0]
        assert not event.terminal_observed
        assert adapter._active_operation_id == context.operation_id
    finally:
        adapter.close()


def test_pa04_continuous_tool_binding_rejects_old_turn_and_runs_new_once(tmp_path):
    adapter, transport, session, context = make_fixture(tmp_path, tool_before_reply=True, stale_second_tool=True)
    calls: list[tuple[str, str]] = []

    def first(params: dict[str, Any]) -> dict[str, Any]:
        calls.append(("A", params["turnId"]))
        return {"ok": True}

    def second_handler(params: dict[str, Any]) -> dict[str, Any]:
        calls.append(("B", params["turnId"]))
        return {"ok": True}

    try:
        adapter.start_turn(session, context, "A", cwd=str(tmp_path), dynamic_tool=DynamicToolConfiguration("fixture", "execute", first))
        transport.queue(terminal(transport))
        for _ in range(4):
            adapter.observe(context)
            if adapter._active_operation_id is None:
                break
        second = dataclasses.replace(context, operation_id="op-b")
        adapter.start_turn(session, second, "B", cwd=str(tmp_path), dynamic_tool=DynamicToolConfiguration("fixture", "execute", second_handler))
        adapter.observe(second)
        assert ("B", "turn-1") not in calls
        assert calls.count(("B", "turn-2")) == 1
        responses = [m for m in transport.sent if m.get("id") in {"old-request-in-new-turn", "tool-2"}]
        assert len([m for m in responses if m.get("id") == "old-request-in-new-turn"]) == 1
    finally:
        adapter.close()


def test_pa04_duplicate_tool_request_id_gets_one_response_and_one_call(tmp_path):
    adapter, transport, session, context = make_fixture(tmp_path)
    calls: list[str] = []

    def handler(params: dict[str, Any]) -> dict[str, Any]:
        calls.append(params["turnId"])
        return {"ok": True}

    try:
        adapter.start_turn(session, context, "A", cwd=str(tmp_path), dynamic_tool=DynamicToolConfiguration("fixture", "execute", handler))
        transport.queue(transport.tool("turn-1", "duplicate"), transport.tool("turn-1", "duplicate"))
        adapter.observe(context)
        assert calls == ["turn-1"]
        assert len([m for m in transport.sent if m.get("id") == "duplicate"]) == 1
    finally:
        adapter.close()


def test_pa05_capacity_rejection_preserves_no_replay(tmp_path):
    adapter, transport, session, context = make_fixture(tmp_path)
    adapter.close()
    transport = StrictPVX1807Transport()
    adapter = CodexProviderAdapter(transport, timeout_seconds=0.01, max_operation_history=1)
    try:
        session = adapter.create_session(cwd=str(tmp_path), clinx_session_id="clinx-session-a")
        first = OperationContext("execution-a", "attempt-a", session.clinx_session_id, "op-a", session.connection_generation)
        adapter.start_turn(session, first, "A", cwd=str(tmp_path))
        transport.queue(terminal(transport))
        adapter.observe(first)
        before = transport.starts
        with pytest.raises(AdapterBusy):
            adapter.start_turn(session, dataclasses.replace(first, operation_id="op-b"), "B", cwd=str(tmp_path))
        with pytest.raises(AdapterBusy):
            adapter.start_turn(session, first, "old retry", cwd=str(tmp_path))
        assert transport.starts == before
    finally:
        adapter.close()


def test_pa05_consumed_terminal_survives_hard_eof(tmp_path):
    transport = EOFAfterQueuedTransport()
    adapter = CodexProviderAdapter(transport, timeout_seconds=0.01)
    try:
        session = adapter.create_session(cwd=str(tmp_path), clinx_session_id="clinx-session-a")
        context = OperationContext("execution-a", "attempt-a", session.clinx_session_id, "op-a", session.connection_generation)
        adapter.start_turn(session, context, "A", cwd=str(tmp_path))
        transport.queue(terminal(transport))
        with pytest.raises(TransportLoss) as raised:
            adapter.observe(context, timeout_seconds=0.01)
        assert any(event.terminal_observed and event.operation == context for event in raised.value.events)
        assert adapter._active_operation_id is None
    finally:
        adapter.close()


def test_pa05_lifetime_histories_are_bounded_and_empty_poll_is_not_eof(tmp_path):
    transport = StrictPVX1807Transport()
    client = CodexAppServerClient(transport, timeout_seconds=0.01, max_received_events=8, max_method_history=8)
    transport.connect()
    client.initialize(client_name="test", client_title="test", client_version="test")
    for n in range(100):
        transport.queue({"method": "telemetry/custom", "params": {"eventId": str(n)}})
    client.drain_events(max_events=8)
    assert len(client.events) <= 8
    assert len(client.received_events) <= 8

    adapter, transport, session, context = make_fixture(tmp_path)
    try:
        adapter.start_turn(session, context, "A", cwd=str(tmp_path))
        empty = adapter.observe(context)
        assert empty.events == ()
        assert adapter.lifecycle_state.value == "connected"
        transport.closed = True
        with pytest.raises(TransportLoss):
            adapter.observe(context)
        assert adapter.lifecycle_state.value == "disconnected"
    finally:
        adapter.close()
