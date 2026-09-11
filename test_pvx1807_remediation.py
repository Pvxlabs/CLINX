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
        self.batch_tool_ids: tuple[str, ...] = ()
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
                request_ids = self.batch_tool_ids or (f"tool-{self.starts}",)
                self.queue(*(self.tool(f"turn-{self.starts}", request_id) for request_id in request_ids))
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
                "arguments": {"request": request_id},
            },
        }


class EOFAfterQueuedTransport(StrictPVX1807Transport):
    def receive(self, timeout_seconds: float) -> dict[str, Any]:
        if self.incoming:
            return self.incoming.popleft()
        raise AppServerTransportError("connection closed")


class ResponseFailureTransport(ScriptedTransport):
    """Fail exactly one tool response after the handler has run."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_response_once = True

    def send(self, message: dict[str, Any]) -> None:
        self.sent.append(dict(message))
        if "result" in message and self.fail_response_once:
            self.fail_response_once = False
            raise AppServerTransportError("fixture response failed after callback")


class StagedBatchResponseFailureTransport(StrictPVX1807Transport):
    """Fail selected staged-tool responses once, preserving the wire trace."""

    def __init__(self, failed_response_ids: set[str] | None = None) -> None:
        self.failed_response_ids = set(failed_response_ids or ())
        self.failed_responses: set[str] = set()
        super().__init__()

    def send(self, message: dict[str, Any]) -> None:
        self.sent.append(dict(message))
        request_id = message.get("id")
        if (
            "result" in message
            and isinstance(request_id, str)
            and request_id in self.failed_response_ids
            and request_id not in self.failed_responses
        ):
            self.failed_responses.add(request_id)
            raise AppServerTransportError(f"fixture response failed for {request_id}")
        if self.on_send is not None:
            self.on_send(message, self)


def make_staged_client(
    transport: StagedBatchResponseFailureTransport,
    calls: list[str],
) -> CodexAppServerClient:
    transport.connect()
    client = CodexAppServerClient(
        transport,
        timeout_seconds=0.01,
        strict_dynamic_tool_binding=True,
    )
    client.configure_dynamic_tool(
        namespace="fixture",
        name="execute",
        thread_id=transport.thread,
        handler=lambda params: calls.append(params["arguments"]["request"]) or {"ok": True},
    )
    return client


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


def test_pa04_pre_response_turn_mismatch_has_no_callback_side_effect(tmp_path):
    adapter, transport, session, context = make_fixture(tmp_path)
    calls: list[str] = []
    original_on_send = transport.on_send

    def queue_wrong_turn(message: dict[str, Any], peer: ScriptedTransport) -> None:
        if message.get("method") == "turn/start":
            peer.queue(peer.tool("turn-other", "wrong-before-reply"))
        assert original_on_send is not None
        original_on_send(message, peer)

    transport.on_send = queue_wrong_turn
    try:
        adapter.start_turn(
            session,
            context,
            "A",
            cwd=str(tmp_path),
            dynamic_tool=DynamicToolConfiguration(
                "fixture", "execute", lambda params: calls.append(params["turnId"]) or {"ok": True}
            ),
        )
        assert calls == []
        responses = [item for item in transport.sent if item.get("id") == "wrong-before-reply"]
        assert len(responses) == 1
        assert responses[0]["result"]["success"] is False
    finally:
        adapter.close()


def test_pa04_response_delivery_failure_never_replays_callback():
    transport = ResponseFailureTransport()
    client = CodexAppServerClient(transport, timeout_seconds=0.01)
    calls: list[str] = []
    client.configure_dynamic_tool(
        namespace="fixture",
        name="execute",
        thread_id="thread-a",
        handler=lambda params: calls.append(params["arguments"]["request"]) or {"ok": True},
    )
    client.attach_dynamic_tool_turn("thread-a", "turn-1")
    request = {
        "id": "response-lost",
        "method": "item/tool/call",
        "params": {
            "namespace": "fixture",
            "tool": "execute",
            "threadId": "thread-a",
            "turnId": "turn-1",
            "arguments": {"request": "response-lost"},
        },
    }
    try:
        with pytest.raises(AppServerTransportError):
            client._send_server_response(request)
        client._send_server_response(dict(request))
        assert calls == ["response-lost"]
        assert len([item for item in transport.sent if item.get("id") == "response-lost"]) == 2
    finally:
        client.close()


def test_pa04_request_id_scope_conflict_and_capacity_preserve_no_replay():
    transport = StrictPVX1807Transport()
    client = CodexAppServerClient(transport, timeout_seconds=0.01, max_received_events=2)
    transport.connect()
    calls: list[str] = []
    client.configure_dynamic_tool(
        namespace="fixture",
        name="execute",
        thread_id=transport.thread,
        handler=lambda params: calls.append(params["arguments"]["request"]) or {"ok": True},
    )
    client.attach_dynamic_tool_turn(transport.thread, "turn-1")

    def request(request_id: Any, value: str) -> dict[str, Any]:
        item = transport.tool("turn-1", request_id)
        item["params"]["arguments"] = {"request": value}
        return item

    try:
        client._send_server_response(request(1, "integer"))
        client._send_server_response(request("1", "string"))
        # Same typed id with different semantics is a conflict, not a new call.
        client._send_server_response(request(1, "changed"))
        # Capacity rejects r3 and retains r1/r2 as the replay fence.
        client._send_server_response(request("r3", "capacity"))
        client._send_server_response(request(1, "integer"))
        assert calls == ["integer", "string"]
    finally:
        client.close()


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


def test_pa04_staged_single_and_double_request_progress(tmp_path):
    for request_ids in (("r1",), ("r1", "r2")):
        transport = StagedBatchResponseFailureTransport()
        calls: list[str] = []
        client = make_staged_client(transport, calls)
        try:
            requests = [transport.tool("turn-1", request_id) for request_id in request_ids]
            for request in requests:
                client._send_server_response(request)
            assert calls == []
            assert [request[1]["id"] for request in client._dynamic_staged_requests] == list(request_ids)
            client.attach_dynamic_tool_turn(transport.thread, "turn-1")
            assert calls == list(request_ids)
            assert not client._dynamic_staged_requests
            assert all(
                len([message for message in transport.sent if message.get("id") == request_id and "result" in message]) == 1
                for request_id in request_ids
            )
        finally:
            client.close()


@pytest.mark.parametrize("failed_id", ["r1", "r2", "r3"])
def test_pa04_staged_batch_failure_preserves_tail_and_response_cache(failed_id):
    request_ids = ("r1", "r2", "r3")
    transport = StagedBatchResponseFailureTransport({failed_id})
    calls: list[str] = []
    client = make_staged_client(transport, calls)
    requests = {request_id: transport.tool("turn-1", request_id) for request_id in request_ids}
    try:
        for request_id in request_ids:
            client._send_server_response(requests[request_id])
        with pytest.raises(AppServerTransportError):
            client.attach_dynamic_tool_turn(transport.thread, "turn-1")

        failed_index = request_ids.index(failed_id)
        assert calls == list(request_ids[:failed_index + 1])
        assert [request[1]["id"] for request in client._dynamic_staged_requests] == list(request_ids[failed_index + 1:])
        failed_record = client._server_request_records[(str, failed_id)]
        assert failed_record.state == "response_pending"
        assert failed_record.response is not None

        # Retry delivery of the failed result, then explicitly continue the
        # remaining staged tail. Neither path may re-enter the callback.
        client._send_server_response(dict(requests[failed_id]))
        client.attach_dynamic_tool_turn(transport.thread, "turn-1")
        assert calls == list(request_ids)
        assert not client._dynamic_staged_requests
        for request_id in request_ids:
            assert len([message for message in transport.sent if message.get("id") == request_id and "result" in message]) == (2 if request_id == failed_id else 1)
    finally:
        client.close()


def test_pa04_staged_wrong_turn_failure_is_visible_and_retryable():
    transport = StagedBatchResponseFailureTransport({"wrong"})
    calls: list[str] = []
    client = make_staged_client(transport, calls)
    request = transport.tool("turn-other", "wrong")
    try:
        client._send_server_response(request)
        with pytest.raises(AppServerTransportError):
            client.attach_dynamic_tool_turn(transport.thread, "turn-1")
        assert calls == []
        assert not client._dynamic_staged_requests
        record = client._server_request_records[(str, "wrong")]
        assert record.state == "response_pending"
        client._send_server_response(dict(request))
        assert calls == []
        responses = [message for message in transport.sent if message.get("id") == "wrong" and "result" in message]
        assert len(responses) == 2
        assert responses[-1]["result"]["success"] is False
    finally:
        client.close()


def test_pa04_staged_batch_does_not_callback_while_transport_is_unavailable():
    transport = StagedBatchResponseFailureTransport({"r1"})
    calls: list[str] = []
    client = make_staged_client(transport, calls)
    try:
        requests = [transport.tool("turn-1", request_id) for request_id in ("r1", "r2")]
        for request in requests:
            client._send_server_response(request)
        with pytest.raises(AppServerTransportError):
            client.attach_dynamic_tool_turn(transport.thread, "turn-1")
        assert calls == ["r1"]

        transport.closed = True
        transport.connected = False
        with pytest.raises(AppServerTransportError):
            client.attach_dynamic_tool_turn(transport.thread, "turn-1")
        assert calls == ["r1"]
        assert [request[1]["id"] for request in client._dynamic_staged_requests] == ["r2"]

        transport.closed = False
        transport.connected = True
        client.attach_dynamic_tool_turn(transport.thread, "turn-1")
        assert calls == ["r1", "r2"]
    finally:
        client.close()


def test_pa04_repeated_attach_retries_pending_result_then_flushes_tail():
    transport = StagedBatchResponseFailureTransport({"r1"})
    calls: list[str] = []
    client = make_staged_client(transport, calls)
    try:
        for request_id in ("r1", "r2"):
            client._send_server_response(transport.tool("turn-1", request_id))
        with pytest.raises(AppServerTransportError):
            client.attach_dynamic_tool_turn(transport.thread, "turn-1")
        assert calls == ["r1"]

        transport.failed_response_ids.clear()
        client.attach_dynamic_tool_turn(transport.thread, "turn-1")
        assert calls == ["r1", "r2"]
        assert not client._dynamic_staged_requests
        assert len([message for message in transport.sent if message.get("id") == "r1" and "result" in message]) == 2
    finally:
        client.close()


def test_pa04_staged_batch_adapter_failure_has_explicit_continue_entry(tmp_path):
    transport = StagedBatchResponseFailureTransport({"r1"})
    transport.tool_before_reply = True
    transport.batch_tool_ids = ("r1", "r2")
    adapter = CodexProviderAdapter(transport, timeout_seconds=0.01)
    calls: list[str] = []
    session = adapter.create_session(cwd=str(tmp_path), clinx_session_id="clinx-session-batch")
    context = OperationContext("execution-batch", "attempt-batch", session.clinx_session_id, "op-batch", session.connection_generation)
    try:
        with pytest.raises(SideEffectUnknown):
            adapter.start_turn(
                session,
                context,
                "batch",
                cwd=str(tmp_path),
                dynamic_tool=DynamicToolConfiguration(
                    "fixture", "execute", lambda params: calls.append(params["arguments"]["request"]) or {"ok": True}
                ),
            )
        assert calls == ["r1"]
        assert [request[1]["id"] for request in adapter.client._dynamic_staged_requests] == ["r2"]

        # The adapter surfaced the uncertain attach, then exposes an explicit
        # same-binding continuation without resending turn/start.
        transport.failed_response_ids.clear()
        resumed = adapter.resume_dynamic_tool_batch(session, context)
        assert resumed.outcome.code.value == "accepted"
        assert calls == ["r1", "r2"]
        assert not adapter.client._dynamic_staged_requests
        assert adapter._operations[context.operation_id][1] == "turn-1"
        assert len([message for message in transport.sent if message.get("method") == "turn/start"]) == 1
    finally:
        adapter.close()


def test_pa04_retire_and_close_fence_staged_requests_from_new_handlers():
    transport = StagedBatchResponseFailureTransport()
    calls: list[str] = []
    client = make_staged_client(transport, calls)
    old_request = transport.tool("turn-old", "old")
    try:
        client._send_server_response(old_request)
        client.clear_dynamic_tool()
        client.configure_dynamic_tool(
            namespace="fixture",
            name="execute",
            thread_id=transport.thread,
            handler=lambda params: calls.append("new:" + params["arguments"]["request"]) or {"ok": True},
        )
        client.attach_dynamic_tool_turn(transport.thread, "turn-new")
        client._send_server_response(dict(old_request))
        assert calls == []
        assert transport.sent[-1]["result"]["success"] is False

        client.close()
        replacement_transport = StagedBatchResponseFailureTransport()
        replacement = make_staged_client(replacement_transport, calls)
        try:
            replacement.attach_dynamic_tool_turn(replacement_transport.thread, "turn-new")
            replacement._send_server_response(dict(old_request))
            assert calls == []
        finally:
            replacement.close()
    finally:
        if not transport.closed:
            client.close()


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
