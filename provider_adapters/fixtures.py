"""Deterministic transports and synthetic adapter fixtures for qualification."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from app_server import AppServerTransportError

from .contracts import (
    AdapterOperation,
    Capability,
    CapabilityStatus,
    Capabilities,
    CorrelationStatus,
    EventKind,
    NormalizedEvent,
    OperationContext,
    Outcome,
    OutcomeCode,
    ProviderSessionRef,
)


class ScriptedTransport:
    """Line-independent JSON-RPC transport with explicit response scripting."""

    def __init__(self, on_send: Callable[[dict[str, Any], "ScriptedTransport"], None] | None = None):
        self.on_send = on_send
        self.sent: list[dict[str, Any]] = []
        self.incoming: deque[dict[str, Any]] = deque()
        self.connected = False
        self.closed = False

    def connect(self) -> None:
        if self.closed:
            raise AppServerTransportError("scripted transport is closed")
        self.connected = True

    def send(self, message: dict[str, Any]) -> None:
        if self.closed or not self.connected:
            raise AppServerTransportError("scripted transport is not connected")
        self.sent.append(dict(message))
        if self.on_send is not None:
            self.on_send(message, self)

    def receive(self, timeout_seconds: float) -> dict[str, Any]:
        if self.closed or not self.connected:
            raise AppServerTransportError("scripted transport is closed")
        if self.incoming:
            return self.incoming.popleft()
        raise AppServerTransportError("timed out waiting for scripted message")

    def queue(self, *messages: Mapping[str, Any]) -> None:
        self.incoming.extend(dict(message) for message in messages)

    def close(self) -> None:
        self.closed = True
        self.connected = False


class SyntheticProviderAdapter:
    """A deliberately different in-memory provider used only for contract tests."""

    provider_id = "synthetic_events"

    def __init__(self, *, supports_interrupt: bool = False):
        self.supports_interrupt = supports_interrupt
        self._generation = 1
        self._session = ProviderSessionRef("synthetic-session", self.provider_id, "opaque:room-1", 1)
        self._events: deque[NormalizedEvent] = deque()
        self._turn = 0

    def discover_capabilities(self) -> Capabilities:
        return Capabilities(self.provider_id, (
            Capability("session_resume", CapabilityStatus.SUPPORTED, "synthetic_fixture"),
            Capability("turn_start", CapabilityStatus.SUPPORTED, "synthetic_fixture"),
            Capability("interrupt", CapabilityStatus.SUPPORTED if self.supports_interrupt else CapabilityStatus.UNSUPPORTED, "synthetic_fixture"),
        ))

    def create_session(self, **_kwargs: Any) -> ProviderSessionRef:
        return self._session

    def resume_session(self, session: ProviderSessionRef) -> ProviderSessionRef:
        if session != self._session:
            raise ValueError("unknown synthetic session")
        return session

    def reconnect(self, session: ProviderSessionRef) -> ProviderSessionRef:
        if session != self._session:
            raise ValueError("unknown synthetic session")
        self._generation += 1
        self._session = replace(self._session, connection_generation=self._generation)
        return self._session

    def start_turn(self, session: ProviderSessionRef, context: OperationContext, _prompt: str, **_kwargs: Any) -> AdapterOperation:
        if session != self._session:
            raise ValueError("unknown synthetic session")
        self._turn += 1
        provider_turn = f"turn-{self._turn}"
        self._events.append(NormalizedEvent(
            receipt_id=f"synthetic:{self._turn}:start",
            provider_id=self.provider_id,
            connection_generation=self._generation,
            kind=EventKind.TURN_STARTED,
            event_type="room.started",
            operation=context,
            correlation=CorrelationStatus.EXACT,
            native_event_id=f"evt-{self._turn}-start",
            local_sequence=self._turn * 2 - 1,
            payload={"room": "opaque:room-1", "turn": provider_turn},
        ))
        return AdapterOperation(Outcome(OutcomeCode.ACCEPTED, provider_reference=provider_turn, side_effect_state="request_accepted"), session=session, context=context)

    continue_turn = start_turn

    def observe(self, _context: OperationContext, **_kwargs: Any) -> AdapterOperation:
        events = tuple(self._events.popleft() for _ in range(len(self._events)))
        return AdapterOperation(Outcome(OutcomeCode.OBSERVED, side_effect_state="provider_evidence_only"), events=events)

    def interrupt(self, _context: OperationContext) -> AdapterOperation:
        if not self.supports_interrupt:
            return AdapterOperation(Outcome(OutcomeCode.UNSUPPORTED, side_effect_state="not_attempted"))
        return AdapterOperation(Outcome(OutcomeCode.CANCEL_REQUESTED, side_effect_state="request_delivered"))

    def normalize_event(self, event: NormalizedEvent, _context: OperationContext | None = None) -> NormalizedEvent:
        return event

    def configure_dynamic_tool(self, _session: ProviderSessionRef, _configuration: Any) -> None:
        return None

    def close(self) -> None:
        self._events.clear()
