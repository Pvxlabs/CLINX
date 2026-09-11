"""Codex implementation of the provider-neutral adapter contract."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from app_server import (
    AppServerError,
    AppServerProtocolError,
    AppServerRemoteError,
    AppServerTransportError,
    CodexAppServerClient,
    JSONRPCTransport,
)

from .contracts import (
    AdapterOperation,
    Capability,
    CapabilityStatus,
    Capabilities,
    CancelState,
    CorrelationStatus,
    DynamicToolConfiguration,
    EventKind,
    NormalizedEvent,
    OperationContext,
    Outcome,
    OutcomeCode,
    ProviderSessionRef,
)
from .errors import (
    AdapterBusy,
    AdapterError,
    CorrelationError,
    ProtocolError,
    SideEffectUnknown,
    TimeoutError,
    TransportLoss,
)


class _RecordingTransport:
    """Track send boundaries without changing the real transport protocol."""

    def __init__(self, inner: JSONRPCTransport):
        self.inner = inner
        self.last_method: str | None = None
        self.last_send_started = False
        self.last_send_completed = False

    def connect(self) -> None:
        connect = getattr(self.inner, "connect", None)
        if callable(connect):
            connect()

    def send(self, message: dict[str, Any]) -> None:
        self.last_method = message.get("method") if isinstance(message.get("method"), str) else None
        self.last_send_started = True
        self.last_send_completed = False
        self.inner.send(message)
        self.last_send_completed = True

    def receive(self, timeout_seconds: float) -> dict[str, Any]:
        return self.inner.receive(timeout_seconds)

    def close(self) -> None:
        self.inner.close()


class CodexProviderAdapter:
    """Explicit, default-off adapter around the existing real Codex client.

    One adapter owns one provider connection.  A connection serializes one
    active turn; callers that need two concurrent sessions must construct two
    adapters/connections, which keeps server notifications from crossing
    operation boundaries.
    """

    provider_id = "codex_app_server"
    DEFAULT_MAX_EVENTS = 32
    DEFAULT_MAX_PAYLOAD_BYTES = 262_144

    def __init__(
        self,
        transport: JSONRPCTransport | None = None,
        *,
        transport_factory: Callable[[], JSONRPCTransport] | None = None,
        client_factory: Callable[[JSONRPCTransport, float], CodexAppServerClient] = CodexAppServerClient,
        timeout_seconds: float = 30.0,
        client_name: str = "clinx-provider-adapter",
        client_title: str = "CLINX Provider Adapter",
        client_version: str = "pvx-1807",
        max_events: int = DEFAULT_MAX_EVENTS,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    ):
        if transport is None and transport_factory is None:
            raise ValueError("transport or transport_factory is required")
        if transport is not None and transport_factory is not None:
            raise ValueError("transport and transport_factory are mutually exclusive")
        if not 1 <= int(max_events) <= 100:
            raise ValueError("max_events must be between 1 and 100")
        if int(max_payload_bytes) < 256:
            raise ValueError("max_payload_bytes must be at least 256")
        self._initial_transport = transport
        self._transport_factory = transport_factory
        self._client_factory = client_factory
        self.timeout_seconds = float(timeout_seconds)
        self.client_name = client_name
        self.client_title = client_title
        self.client_version = client_version
        self.max_events = int(max_events)
        self.max_payload_bytes = int(max_payload_bytes)
        self._client: CodexAppServerClient | None = None
        self._recording_transport: _RecordingTransport | None = None
        self._initialized = False
        self._closed = False
        self._generation = 0
        self._lock = threading.RLock()
        self._sessions: dict[str, ProviderSessionRef] = {}
        self._operations: dict[str, tuple[OperationContext, str]] = {}
        self._ambiguous_operations: set[str] = set()
        self._ambiguous_sessions: set[str] = set()
        self._active_operation_id: str | None = None
        self._cancel_requested: set[str] = set()
        self._event_sequence = 0
        self._seen_native_ids: set[str] = set()

    @property
    def connection_generation(self) -> int:
        return self._generation

    @property
    def client(self) -> CodexAppServerClient | None:
        """Expose the real client for diagnostics, never for core consumers."""
        return self._client

    def _new_transport(self) -> _RecordingTransport:
        if self._initial_transport is not None:
            transport, self._initial_transport = self._initial_transport, None
        elif self._transport_factory is not None:
            transport = self._transport_factory()
        else:
            raise TransportLoss("no transport factory is available for this connection")
        return _RecordingTransport(transport)

    def _ensure_client(self) -> CodexAppServerClient:
        with self._lock:
            if self._closed:
                raise TransportLoss("provider adapter is closed")
            if self._client is not None:
                return self._client
            recording = self._new_transport()
            try:
                recording.connect()
                if self._client_factory is CodexAppServerClient:
                    client = self._client_factory(
                        recording,
                        self.timeout_seconds,
                        max_received_events=self.max_events,
                    )
                else:
                    client = self._client_factory(recording, self.timeout_seconds)
                client.initialize(
                    client_name=self.client_name,
                    client_title=self.client_title,
                    client_version=self.client_version,
                )
            except AppServerTransportError as exc:
                recording.close()
                raise TransportLoss(str(exc)) from exc
            except (AppServerProtocolError, AppServerRemoteError) as exc:
                recording.close()
                raise ProtocolError(str(exc)) from exc
            self._recording_transport = recording
            self._client = client
            self._generation += 1
            self._initialized = True
            return client

    def _require_session(self, session: ProviderSessionRef) -> ProviderSessionRef:
        if not isinstance(session, ProviderSessionRef):
            raise TypeError("session must be ProviderSessionRef")
        known = self._sessions.get(session.clinx_session_id)
        if known is None:
            raise CorrelationError("provider session is not owned by this adapter")
        if session.provider_id != self.provider_id or known.provider_id != self.provider_id:
            raise CorrelationError("provider session belongs to a different provider")
        if known.provider_handle != session.provider_handle:
            raise CorrelationError("provider session handle changed")
        if session.connection_generation != self._generation:
            raise CorrelationError("provider session belongs to an old connection generation")
        return known

    def _require_context(self, context: OperationContext) -> tuple[OperationContext, str]:
        if not isinstance(context, OperationContext):
            raise TypeError("context must be OperationContext")
        operation = self._operations.get(context.operation_id)
        if operation is None:
            if context.operation_id in self._ambiguous_operations:
                raise SideEffectUnknown("operation outcome is unknown; retry requires recovery evidence")
            raise CorrelationError("operation is not registered by this adapter")
        registered, turn_id = operation
        if registered != context:
            raise CorrelationError("operation context does not exactly match the registered operation")
        if context.connection_generation != self._generation:
            raise CorrelationError("operation belongs to an old connection generation")
        return registered, turn_id

    @staticmethod
    def _map_error(exc: AppServerError, recording: _RecordingTransport | None, method: str) -> AdapterError:
        if isinstance(exc, AppServerTransportError):
            if recording is not None and recording.last_method == method and recording.last_send_completed:
                return SideEffectUnknown(
                    f"{method} response was lost after the request was sent; provider outcome is unknown"
                )
            if "timed out" in str(exc).casefold():
                return TimeoutError(str(exc))
            return TransportLoss(str(exc))
        if isinstance(exc, AppServerProtocolError) or isinstance(exc, AppServerRemoteError):
            return ProtocolError(str(exc))
        return ProtocolError(str(exc))

    def discover_capabilities(self) -> Capabilities:
        with self._lock:
            client = self._ensure_client()
            try:
                payload = client.model_list()
            except AppServerError as exc:
                mapped = self._map_error(exc, self._recording_transport, "model/list")
                return Capabilities(
                    self.provider_id,
                    tuple(
                        Capability(name, CapabilityStatus.UNKNOWN, "model/list_error", details={"error": mapped.outcome_code.value})
                        for name in ("model_resolution", "thread_start", "thread_resume", "turn_start", "observe", "interrupt", "dynamic_tool")
                    ),
                )
            data = payload.get("data") if isinstance(payload, Mapping) else None
            model_status = CapabilityStatus.SUPPORTED if isinstance(data, list) else CapabilityStatus.UNKNOWN
            return Capabilities(
                self.provider_id,
                (
                    Capability("model_resolution", model_status, "model/list", self._server_version(), {"models": len(data) if isinstance(data, list) else 0}),
                    Capability("thread_start", CapabilityStatus.SUPPORTED, "CodexAppServerClient.thread_start"),
                    Capability("thread_resume", CapabilityStatus.SUPPORTED, "CodexAppServerClient.thread_resume"),
                    Capability("turn_start", CapabilityStatus.SUPPORTED, "CodexAppServerClient.turn_start"),
                    Capability("observe", CapabilityStatus.SUPPORTED, "CodexAppServerClient.drain_events"),
                    Capability("interrupt", CapabilityStatus.SUPPORTED, "CodexAppServerClient.turn_interrupt"),
                    Capability("dynamic_tool", CapabilityStatus.SUPPORTED, "CodexAppServerClient.configure_dynamic_tool"),
                ),
            )

    def _server_version(self) -> str | None:
        info = self._client.initialize_info if self._client is not None else None
        return info.server_version if info is not None else None

    def create_session(
        self,
        *,
        cwd: str,
        model: str | None = None,
        project_id: str | None = None,
        sandbox: str | None = None,
        ephemeral: bool = False,
        thread_source: str | None = None,
        dynamic_tools: list[dict[str, Any]] | None = None,
        clinx_session_id: str | None = None,
    ) -> ProviderSessionRef:
        with self._lock:
            client = self._ensure_client()
            try:
                thread = client.thread_start(
                    cwd=cwd,
                    model=model,
                    project_id=project_id,
                    sandbox=sandbox,
                    ephemeral=ephemeral,
                    thread_source=thread_source,
                    dynamic_tools=dynamic_tools,
                )
            except AppServerError as exc:
                raise self._map_error(exc, self._recording_transport, "thread/start") from exc
            handle = thread.get("id") or thread.get("threadId")
            if not isinstance(handle, str) or not handle.strip():
                raise ProtocolError("thread/start did not return an opaque thread handle")
            session = ProviderSessionRef(
                clinx_session_id=clinx_session_id or f"clinx-session-{uuid.uuid4().hex}",
                provider_id=self.provider_id,
                provider_handle=handle,
                connection_generation=self._generation,
                metadata={"thread_source": thread_source} if thread_source else {},
            )
            self._sessions[session.clinx_session_id] = session
            return session

    def resume_session(self, session: ProviderSessionRef) -> ProviderSessionRef:
        with self._lock:
            session = self._require_session(session)
            if session.clinx_session_id in self._ambiguous_sessions:
                raise SideEffectUnknown("session resume outcome is unknown; do not blindly repeat it")
            client = self._ensure_client()
            if not session.provider_handle:
                raise ProtocolError("provider session has no resumable handle")
            try:
                client.thread_resume(session.provider_handle)
            except AppServerError as exc:
                mapped = self._map_error(exc, self._recording_transport, "thread/resume")
                if isinstance(mapped, SideEffectUnknown):
                    self._ambiguous_sessions.add(session.clinx_session_id)
                raise mapped from exc
            return session

    def reconnect(self, session: ProviderSessionRef) -> ProviderSessionRef:
        """Create a new connection generation and resume one exact session."""
        with self._lock:
            session = self._sessions.get(session.clinx_session_id, session)
            if self._client is None or self._transport_factory is None:
                raise TransportLoss("reconnect requires a transport factory")
            old_client = self._client
            self._client = None
            self._recording_transport = None
            self._initialized = False
            try:
                old_client.close()
            finally:
                self._closed = False
            # _ensure_client increments the generation only after initialize.
            self._ensure_client()
            resumed = self.resume_session(
                ProviderSessionRef(
                    session.clinx_session_id,
                    session.provider_id,
                    session.provider_handle,
                    self._generation,
                    session.metadata,
                )
            )
            self._sessions[resumed.clinx_session_id] = resumed
            return resumed

    def configure_dynamic_tool(
        self,
        session: ProviderSessionRef,
        configuration: DynamicToolConfiguration,
    ) -> None:
        with self._lock:
            session = self._require_session(session)
            if not session.provider_handle:
                raise ProtocolError("dynamic tools require a provider thread handle")
            try:
                self._ensure_client().configure_dynamic_tool(
                    namespace=configuration.namespace,
                    name=configuration.name,
                    thread_id=session.provider_handle,
                    handler=configuration.handler,
                )
            except AppServerError as exc:
                raise self._map_error(exc, self._recording_transport, "dynamic-tool/configure") from exc

    def start_turn(
        self,
        session: ProviderSessionRef,
        context: OperationContext,
        prompt: str,
        *,
        cwd: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        approval_policy: str | None = None,
        network_access: bool = False,
        writable_roots: list[str] | None = None,
        dynamic_tool: DynamicToolConfiguration | None = None,
    ) -> AdapterOperation:
        with self._lock:
            session = self._require_session(session)
            if context.session_id != session.clinx_session_id or context.connection_generation != self._generation:
                raise CorrelationError("turn context does not belong to the exact provider session")
            if self._active_operation_id is not None and self._active_operation_id != context.operation_id:
                raise AdapterBusy("one active turn is allowed per provider connection")
            if context.operation_id in self._operations:
                raise AdapterBusy("turn start would duplicate an existing operation")
            if context.operation_id in self._ambiguous_operations:
                raise SideEffectUnknown("ambiguous turn start cannot be resent without provider recovery")
            if dynamic_tool is not None:
                self.configure_dynamic_tool(session, dynamic_tool)
            client = self._ensure_client()
            try:
                info = client.turn_start(
                    session.provider_handle or "",
                    prompt,
                    cwd=cwd,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    approval_policy=approval_policy,
                    network_access=network_access,
                    writable_roots=writable_roots,
                )
            except AppServerError as exc:
                mapped = self._map_error(exc, self._recording_transport, "turn/start")
                if isinstance(mapped, SideEffectUnknown):
                    self._ambiguous_operations.add(context.operation_id)
                raise mapped from exc
            if dynamic_tool is not None:
                try:
                    client.attach_dynamic_tool_turn(session.provider_handle or "", info.turn_id)
                except AppServerError as exc:
                    self._ambiguous_operations.add(context.operation_id)
                    raise SideEffectUnknown(
                        "turn was accepted but dynamic-tool turn attachment failed; provider outcome is unknown"
                    ) from exc
            self._operations[context.operation_id] = (context, info.turn_id)
            self._active_operation_id = context.operation_id
            return AdapterOperation(
                Outcome(OutcomeCode.ACCEPTED, provider_reference=info.turn_id, side_effect_state="request_accepted"),
                session=session,
                context=context,
            )

    # Explicit alias makes the provider-neutral term available without
    # changing the Codex-specific implementation semantics.
    continue_turn = start_turn

    def observe(
        self,
        context: OperationContext,
        *,
        max_events: int | None = None,
        timeout_seconds: float = 0.0,
    ) -> AdapterOperation:
        with self._lock:
            registered, _turn_id = self._require_context(context)
            client = self._ensure_client()
            limit = self.max_events if max_events is None else max_events
            try:
                raw_events = client.drain_events(max_events=limit, timeout_seconds=timeout_seconds)
            except AppServerTransportError as exc:
                if timeout_seconds > 0 and "timed out" in str(exc).casefold():
                    raise TimeoutError(str(exc)) from exc
                raise TransportLoss(str(exc)) from exc
            except AppServerProtocolError as exc:
                raise ProtocolError(str(exc)) from exc
            normalized = tuple(self.normalize_event(raw, registered) for raw in raw_events)
            if any(event.event_type == "clinx/observation_overflow" for event in normalized):
                raise ProtocolError("bounded provider observation buffer overflowed")
            exact_terminal = [event for event in normalized if event.terminal_observed and event.authority_eligible]
            if exact_terminal:
                self._active_operation_id = None
            confirmed_cancel = any(event.cancel_state is CancelState.CONFIRMED for event in exact_terminal)
            outcome = Outcome(
                OutcomeCode.CANCEL_CONFIRMED if confirmed_cancel else OutcomeCode.OBSERVED,
                side_effect_state="provider_evidence_only",
                detail="no events observed" if not normalized else None,
            )
            return AdapterOperation(outcome, context=context, events=normalized)

    def interrupt(self, context: OperationContext) -> AdapterOperation:
        with self._lock:
            registered, turn_id = self._require_context(context)
            if context.operation_id in self._cancel_requested:
                return AdapterOperation(
                    Outcome(OutcomeCode.CANCEL_REQUESTED, provider_reference=turn_id, side_effect_state="request_already_delivered"),
                    context=registered,
                )
            client = self._ensure_client()
            try:
                client.turn_interrupt(registered.session_id, turn_id)
            except AppServerError as exc:
                mapped = self._map_error(exc, self._recording_transport, "turn/interrupt")
                if isinstance(mapped, SideEffectUnknown):
                    self._cancel_requested.add(context.operation_id)
                    raise TimeoutError("interrupt response was lost; cancellation is not confirmed") from exc
                raise mapped from exc
            self._cancel_requested.add(context.operation_id)
            return AdapterOperation(
                Outcome(OutcomeCode.CANCEL_REQUESTED, provider_reference=turn_id, side_effect_state="request_delivered"),
                context=registered,
            )

    def normalize_event(self, raw: Mapping[str, Any], context: OperationContext | None = None) -> NormalizedEvent:
        if not isinstance(raw, Mapping):
            raise ProtocolError("provider event must be an object")
        with self._lock:
            self._event_sequence += 1
            sequence = self._event_sequence
            method = raw.get("method")
            method = method.strip() if isinstance(method, str) and method.strip() else "unknown"
            params = raw.get("params")
            params = dict(params) if isinstance(params, Mapping) else {}
            turn = params.get("turn") if isinstance(params.get("turn"), Mapping) else {}
            thread_id = params.get("threadId") or params.get("thread_id") or turn.get("threadId")
            turn_id = params.get("turnId") or params.get("turn_id") or turn.get("id")
            native_id = raw.get("eventId") or raw.get("event_id") or params.get("eventId") or raw.get("id")
            native_id = str(native_id) if isinstance(native_id, (str, int)) else None
            event_generation = params.get("connectionGeneration")
            if not isinstance(event_generation, int):
                event_generation = self._generation
            correlation = CorrelationStatus.UNKNOWN
            event_operation: OperationContext | None = None
            if context is not None:
                registered = self._operations.get(context.operation_id)
                expected_turn = registered[1] if registered else None
                expected_session = self._sessions.get(context.session_id)
                expected_thread = expected_session.provider_handle if expected_session else None
                if event_generation != context.connection_generation:
                    correlation = CorrelationStatus.STALE_GENERATION
                elif thread_id is None or turn_id is None:
                    correlation = CorrelationStatus.UNKNOWN
                elif thread_id == expected_thread and turn_id == expected_turn:
                    correlation = CorrelationStatus.EXACT
                    event_operation = context
                else:
                    correlation = CorrelationStatus.MISMATCH
            kind = EventKind.UNKNOWN if method == "unknown" else EventKind.NOTIFICATION
            if method == "item/tool/call" or ("id" in raw and method not in {"unknown"}):
                kind = EventKind.TOOL_REQUEST
            elif method in {"turn/started", "turn/start"}:
                kind = EventKind.TURN_STARTED
            elif method in {"turn/completed", "turn/complete", "turn/failed", "turn/cancelled", "turn/canceled"}:
                kind = EventKind.TURN_COMPLETED
            status = params.get("status") or turn.get("status")
            if not isinstance(status, str):
                status = ""
            status_lower = status.casefold()
            terminal = kind is EventKind.TURN_COMPLETED or status_lower in {"completed", "failed", "interrupted", "cancelled", "canceled"}
            cancel_state = CancelState.NOT_REQUESTED
            if context is not None and context.operation_id in self._cancel_requested:
                cancel_state = CancelState.CONFIRMED if terminal and status_lower in {"interrupted", "cancelled", "canceled"} else CancelState.REQUESTED
            error = params.get("error")
            error_code = None
            if isinstance(error, Mapping):
                value = error.get("code") or error.get("type") or error.get("message")
                error_code = str(value) if value is not None else None
            elif status_lower in {"failed", "error"}:
                error_code = str(params.get("code") or status or "provider_error")
            payload = self._bounded_payload(params)
            duplicate = native_id is not None and native_id in self._seen_native_ids
            if native_id is not None:
                self._seen_native_ids.add(native_id)
            receipt = f"{self.provider_id}:{self._generation}:{sequence}"
            if native_id is not None:
                receipt = f"{self.provider_id}:{self._generation}:native:{native_id}"
            return NormalizedEvent(
                receipt_id=receipt,
                provider_id=self.provider_id,
                connection_generation=event_generation,
                kind=kind,
                event_type=method,
                operation=event_operation,
                correlation=correlation,
                native_event_id=native_id,
                local_sequence=sequence,
                cursor=str(params["cursor"]) if params.get("cursor") is not None else None,
                terminal_observed=terminal,
                cancel_state=cancel_state,
                error_code=error_code,
                evidence_reference=receipt,
                duplicate=duplicate,
                payload=payload,
            )

    def _bounded_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return {"schema_error": "non_json_payload"}
        if len(encoded.encode("utf-8")) <= self.max_payload_bytes:
            return json.loads(encoded)
        return {
            "truncated": True,
            "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "bytes": len(encoded.encode("utf-8")),
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            client, self._client = self._client, None
            self._recording_transport = None
            if client is not None:
                try:
                    client.close()
                except Exception:
                    # close is idempotent and cleanup failures do not become
                    # a terminal CLINX execution verdict.
                    pass
