from __future__ import annotations

import unittest

from app_server import AppServerTransportError
from provider_adapters import (
    AdapterBusy,
    CapabilityStatus,
    CodexProviderAdapter,
    CorrelationStatus,
    DynamicToolConfiguration,
    OutcomeCode,
    ProviderSessionRef,
    ScriptedTransport,
    SideEffectUnknown,
    SyntheticProviderAdapter,
    TransportLoss,
)
from provider_adapters.contracts import OperationContext


class CodexAdapterFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.turn_number = 0

        def on_send(message: dict, transport: ScriptedTransport) -> None:
            method = message.get("method")
            request_id = message.get("id")
            if method == "initialize":
                transport.queue({"id": request_id, "result": {"serverInfo": {"name": "fixture-codex", "version": "test"}}})
            elif method == "thread/start":
                transport.queue({"id": request_id, "result": {"thread": {"id": "thread-a"}}})
            elif method == "thread/resume":
                transport.queue({"id": request_id, "result": {"thread": {"id": "thread-a"}}})
            elif method == "model/list":
                transport.queue({"id": request_id, "result": {"data": [{"id": "fixture-model", "supportedReasoningEfforts": ["low"], "defaultReasoningEffort": "low"}]}})
            elif method == "turn/start":
                self.turn_number += 1
                transport.queue({"id": request_id, "result": {"turn": {"id": f"turn-{self.turn_number}"}, "model": "fixture-model", "reasoningEffort": "low"}})
            elif method == "turn/interrupt":
                transport.queue({"id": request_id, "result": {}})

        self.transport = ScriptedTransport(on_send)
        self.adapter = CodexProviderAdapter(self.transport, timeout_seconds=0.01)

    def tearDown(self) -> None:
        self.adapter.close()

    def _session_context(self, operation_id: str = "op-1") -> tuple[ProviderSessionRef, OperationContext]:
        session = self.adapter.create_session(cwd="/tmp/fixture", model="fixture-model")
        context = OperationContext("execution-1", "attempt-1", session.clinx_session_id, operation_id, session.connection_generation)
        return session, context

    def test_real_client_adapter_preserves_exact_trace_and_capabilities(self) -> None:
        session, context = self._session_context()
        capabilities = self.adapter.discover_capabilities()
        self.assertEqual(capabilities.get("model_resolution").status, CapabilityStatus.SUPPORTED)
        started = self.adapter.start_turn(
            session,
            context,
            "continue",
            cwd="/tmp/fixture",
            model="fixture-model",
            reasoning_effort="low",
        )
        self.assertEqual(started.outcome.code, OutcomeCode.ACCEPTED)
        self.transport.queue({
            "method": "turn/completed",
            "params": {"threadId": "thread-a", "turnId": "turn-1", "status": "completed", "eventId": "evt-1"},
        })
        observed = self.adapter.observe(context)
        self.assertEqual(len(observed.events), 1)
        event = observed.events[0]
        self.assertEqual(event.correlation, CorrelationStatus.EXACT)
        self.assertTrue(event.terminal_observed)
        self.assertTrue(event.authority_eligible)
        self.assertEqual(event.operation, context)
        # The adapter is an evidence source, not a finalizer.
        self.assertFalse(hasattr(self.adapter, "finalize_execution"))

    def test_dynamic_tool_request_keeps_request_identity_and_turn_attachment(self) -> None:
        session, context = self._session_context()
        calls: list[dict] = []

        def handler(params: dict) -> dict:
            calls.append(params)
            return {"result_state": "PASS"}

        # The fixture server request is delivered while turn/start is waiting
        # for its response.  The real client answers it synchronously.
        original = self.transport.on_send

        def on_send(message: dict, transport: ScriptedTransport) -> None:
            if message.get("method") == "turn/start":
                transport.queue({
                    "id": "tool-request-1",
                    "method": "item/tool/call",
                    "params": {"namespace": "fixture", "tool": "execute", "threadId": "thread-a", "turnId": "turn-1", "arguments": {}},
                })
            assert original is not None
            original(message, transport)

        self.transport.on_send = on_send
        self.adapter.start_turn(
            session,
            context,
            "tool",
            cwd="/tmp/fixture",
            dynamic_tool=DynamicToolConfiguration("fixture", "execute", handler),
        )
        self.assertEqual(calls[0]["turnId"], "turn-1")
        sent_responses = [item for item in self.transport.sent if item.get("id") == "tool-request-1"]
        self.assertEqual(len(sent_responses), 1)

    def test_same_connection_rejects_multiplex_and_duplicate_native_id_is_visible(self) -> None:
        session, context = self._session_context()
        self.adapter.start_turn(session, context, "one", cwd="/tmp/fixture")
        second = ProviderSessionRef("session-b", "codex_app_server", "thread-b", session.connection_generation)
        self.adapter._sessions[second.clinx_session_id] = second  # explicit fixture registration
        with self.assertRaises(AdapterBusy):
            self.adapter.start_turn(second, OperationContext("e2", "a2", "session-b", "op-2", session.connection_generation), "two", cwd="/tmp/fixture")
        self.transport.queue(
            {"method": "turn/completed", "params": {"threadId": "thread-a", "turnId": "turn-1", "status": "completed", "eventId": "same"}},
            {"method": "turn/completed", "params": {"threadId": "thread-a", "turnId": "turn-1", "status": "completed", "eventId": "same"}},
        )
        events = self.adapter.observe(context).events + self.adapter.observe(context).events
        self.assertEqual(len(events), 2)
        self.assertFalse(events[0].duplicate)
        self.assertTrue(events[1].duplicate)

    def test_ambiguous_start_is_not_retried(self) -> None:
        def on_send(message: dict, transport: ScriptedTransport) -> None:
            if message.get("method") == "initialize":
                transport.queue({"id": message.get("id"), "result": {"serverInfo": {"version": "test"}}})
            elif message.get("method") == "thread/start":
                transport.queue({"id": message.get("id"), "result": {"thread": {"id": "thread-a"}}})

        transport = ScriptedTransport(on_send)
        adapter = CodexProviderAdapter(transport, timeout_seconds=0.01)
        try:
            session = adapter.create_session(cwd="/tmp/fixture")
            context = OperationContext("e", "a", session.clinx_session_id, "ambiguous", session.connection_generation)
            with self.assertRaises(SideEffectUnknown):
                adapter.start_turn(session, context, "may have run", cwd="/tmp/fixture")
            starts = [item for item in transport.sent if item.get("method") == "turn/start"]
            self.assertEqual(len(starts), 1)
            with self.assertRaises(SideEffectUnknown):
                adapter.start_turn(session, context, "do not duplicate", cwd="/tmp/fixture")
            self.assertEqual(len([item for item in transport.sent if item.get("method") == "turn/start"]), 1)
        finally:
            adapter.close()

    def test_cancel_is_request_observation_until_terminal_confirmation(self) -> None:
        session, context = self._session_context()
        self.adapter.start_turn(session, context, "cancel", cwd="/tmp/fixture")
        requested = self.adapter.interrupt(context)
        self.assertEqual(requested.outcome.code, OutcomeCode.CANCEL_REQUESTED)
        self.transport.queue({"method": "turn/completed", "params": {"threadId": "thread-a", "turnId": "turn-1", "status": "interrupted", "eventId": "cancelled"}})
        confirmation = self.adapter.observe(context)
        self.assertEqual(confirmation.outcome.code, OutcomeCode.CANCEL_CONFIRMED)
        confirmed = confirmation.events[0]
        self.assertEqual(confirmed.cancel_state.value, "confirmed")
        self.assertTrue(confirmed.terminal_observed)

    def test_stale_generation_is_not_authoritative(self) -> None:
        session, context = self._session_context()
        self.adapter.start_turn(session, context, "stale", cwd="/tmp/fixture")
        event = self.adapter.normalize_event(
            {"method": "turn/completed", "params": {"threadId": "thread-a", "turnId": "turn-1", "connectionGeneration": 99}},
            context,
        )
        self.assertEqual(event.correlation, CorrelationStatus.STALE_GENERATION)
        self.assertFalse(event.authority_eligible)

    def test_continuation_uses_same_session_with_new_exact_operation(self) -> None:
        session, first = self._session_context("op-1")
        self.adapter.start_turn(session, first, "first", cwd="/tmp/fixture")
        self.transport.queue({"method": "turn/completed", "params": {"threadId": "thread-a", "turnId": "turn-1", "status": "completed", "eventId": "first"}})
        self.assertEqual(self.adapter.observe(first).outcome.code, OutcomeCode.OBSERVED)
        second = OperationContext("execution-1", "attempt-1", session.clinx_session_id, "op-2", session.connection_generation)
        self.adapter.continue_turn(session, second, "second", cwd="/tmp/fixture")
        self.transport.queue({"method": "turn/completed", "params": {"threadId": "thread-a", "turnId": "turn-2", "status": "completed", "eventId": "second"}})
        observed = self.adapter.observe(second)
        self.assertEqual(observed.events[0].operation, second)
        self.assertEqual(len([item for item in self.transport.sent if item.get("method") == "turn/start"]), 2)

    def test_separate_connections_keep_interleaved_sessions_isolated(self) -> None:
        def on_send(message: dict, transport: ScriptedTransport) -> None:
            method = message.get("method")
            if method == "initialize":
                transport.queue({"id": message.get("id"), "result": {"serverInfo": {"version": "test"}}})
            elif method == "thread/start":
                transport.queue({"id": message.get("id"), "result": {"thread": {"id": "thread-b"}}})
            elif method == "turn/start":
                transport.queue({"id": message.get("id"), "result": {"turn": {"id": "turn-b"}}})

        other_transport = ScriptedTransport(on_send)
        other = CodexProviderAdapter(other_transport, timeout_seconds=0.01)
        try:
            left_session, left_context = self._session_context("left")
            right_session = other.create_session(cwd="/tmp/fixture")
            right_context = OperationContext("e-right", "a-right", right_session.clinx_session_id, "right", right_session.connection_generation)
            self.adapter.start_turn(left_session, left_context, "left", cwd="/tmp/fixture")
            other.start_turn(right_session, right_context, "right", cwd="/tmp/fixture")
            self.transport.queue({"method": "turn/completed", "params": {"threadId": "thread-a", "turnId": "turn-1", "status": "completed", "eventId": "left"}})
            other_transport.queue({"method": "turn/completed", "params": {"threadId": "thread-b", "turnId": "turn-b", "status": "completed", "eventId": "right"}})
            self.assertEqual(self.adapter.observe(left_context).events[0].correlation, CorrelationStatus.EXACT)
            self.assertEqual(other.observe(right_context).events[0].correlation, CorrelationStatus.EXACT)
        finally:
            other.close()

    def test_event_buffer_overflow_is_visible_as_protocol_interruption(self) -> None:
        session, context = self._session_context()
        # Recreate the explicit adapter with a small raw buffer while keeping
        # the same scripted transport contract.
        self.adapter.close()
        self.transport = ScriptedTransport(self.transport.on_send)
        self.adapter = CodexProviderAdapter(self.transport, timeout_seconds=0.01, max_events=2)
        session = self.adapter.create_session(cwd="/tmp/fixture")
        context = OperationContext("e-overflow", "a-overflow", session.clinx_session_id, "overflow", session.connection_generation)
        original = self.transport.on_send

        def queue_during_start(message: dict, transport: ScriptedTransport) -> None:
            if message.get("method") == "turn/start":
                transport.queue(
                    {"method": "turn/started", "params": {"threadId": "thread-a", "turnId": "turn-1"}},
                    {"method": "turn/completed", "params": {"threadId": "thread-a", "turnId": "turn-1", "status": "completed"}},
                )
            assert original is not None
            original(message, transport)

        self.transport.on_send = queue_during_start
        self.adapter.start_turn(session, context, "overflow", cwd="/tmp/fixture")
        from provider_adapters import ProtocolError
        with self.assertRaises(ProtocolError):
            self.adapter.observe(context)


class ProviderContractTests(unittest.TestCase):
    def test_synthetic_provider_has_explicit_unsupported_capability(self) -> None:
        adapter = SyntheticProviderAdapter()
        self.assertEqual(adapter.discover_capabilities().get("interrupt").status, CapabilityStatus.UNSUPPORTED)
        session = adapter.create_session()
        context = OperationContext("e", "a", session.clinx_session_id, "op", 1)
        result = adapter.start_turn(session, context, "hello")
        self.assertEqual(result.outcome.code, OutcomeCode.ACCEPTED)
        self.assertEqual(adapter.observe(context).events[0].provider_id, "synthetic_events")


if __name__ == "__main__":
    unittest.main()
