"""Codex implementation of the provider-neutral adapter contract."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from app_server import (
    AppServerError,
    AppServerProtocolError,
    AppServerRemoteError,
    AppServerTransportError,
    CodexAppServerClient,
    JSONRPCTransport,
    transport_lifecycle_state,
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
    LifecycleState,
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
        self._request_records: deque[_RequestSend] = deque(maxlen=128)
        self._request_sequence = 0

    def connect(self) -> None:
        connect = getattr(self.inner, "connect", None)
        if callable(connect):
            connect()

    def transport_lifecycle_state(self) -> str:
        """Preserve the wrapped transport's local lifecycle evidence."""
        return transport_lifecycle_state(self.inner)

    def send(self, message: dict[str, Any]) -> None:
        self.last_method = message.get("method") if isinstance(message.get("method"), str) else None
        self.last_send_started = True
        self.last_send_completed = False
        is_request = isinstance(message.get("method"), str) and "id" in message
        request_id = str(message.get("id")) if is_request else None
        record = _RequestSend(request_id, message.get("method") if is_request else None)
        if is_request:
            self._request_sequence += 1
            record.sequence = self._request_sequence
            self._request_records.append(record)
            record.state = "sending"
        try:
            self.inner.send(message)
        except Exception as exc:
            record.state = "definitely_unsent" if self._definitely_unsent(exc) else "possibly_sent"
            record.error = str(exc)
            raise
        else:
            self.last_send_completed = True
            record.state = "sent_awaiting_response"

    @staticmethod
    def _definitely_unsent(exc: Exception) -> bool:
        text = str(exc).casefold()
        return any(marker in text for marker in ("not connected", "not open", "connection is closed", "transport is closed"))

    def mark_request_result(self, request_id: str, state: str) -> None:
        for record in reversed(self._request_records):
            if record.request_id == str(request_id):
                record.state = state
                return

    def checkpoint(self) -> int:
        return self._request_sequence

    def request_state(self, method: str, *, after: int | None = None) -> str | None:
        for record in reversed(self._request_records):
            if record.method == method and (after is None or record.sequence > after):
                return record.state

    def request_was_sent(self, method: str) -> bool:
        """Return whether the latest request crossed or may have crossed send()."""
        return self.request_state(method) in {
            "possibly_sent",
            "sent_awaiting_response",
            "accepted",
            "explicit_rejection",
            "unknown",
        }

    def receive(self, timeout_seconds: float) -> dict[str, Any]:
        return self.inner.receive(timeout_seconds)

    def close(self) -> None:
        self.inner.close()


@dataclass
class _RequestSend:
    request_id: str | None
    method: str | None
    state: str = "not_started"
    error: str | None = None
    sequence: int = 0


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
        max_seen_native_ids: int = 4096,
        max_operation_history: int = 1024,
        max_sessions: int = 64,
    ):
        if transport is None and transport_factory is None:
            raise ValueError("transport or transport_factory is required")
        if transport is not None and transport_factory is not None:
            raise ValueError("transport and transport_factory are mutually exclusive")
        if not 1 <= int(max_events) <= 100:
            raise ValueError("max_events must be between 1 and 100")
        if int(max_payload_bytes) < 256:
            raise ValueError("max_payload_bytes must be at least 256")
        if not 1 <= int(max_seen_native_ids) <= 100_000:
            raise ValueError("max_seen_native_ids must be between 1 and 100000")
        if not 1 <= int(max_operation_history) <= 100_000:
            raise ValueError("max_operation_history must be between 1 and 100000")
        if not 1 <= int(max_sessions) <= 10_000:
            raise ValueError("max_sessions must be between 1 and 10000")
        self._initial_transport = transport
        self._transport_factory = transport_factory
        self._client_factory = client_factory
        self.timeout_seconds = float(timeout_seconds)
        self.client_name = client_name
        self.client_title = client_title
        self.client_version = client_version
        self.max_events = int(max_events)
        self.max_payload_bytes = int(max_payload_bytes)
        self.max_seen_native_ids = int(max_seen_native_ids)
        self.max_operation_history = int(max_operation_history)
        self.max_sessions = int(max_sessions)
        self._client: CodexAppServerClient | None = None
        self._recording_transport: _RecordingTransport | None = None
        self._initialized = False
        self._closed = False
        self._lifecycle = LifecycleState.CREATED
        self._generation = 0
        self._lock = threading.RLock()
        self._sessions: dict[str, ProviderSessionRef] = {}
        self._operations: dict[str, tuple[OperationContext, str]] = {}
        self._terminal_operations: set[str] = set()
        self._operation_order: deque[str] = deque()
        self._ambiguous_operations: set[str] = set()
        self._ambiguous_sessions: set[str] = set()
        self._active_operation_id: str | None = None
        self._cancel_requested: set[str] = set()
        self._event_sequence = 0
        self._seen_native_ids: set[str] = set()
        self._seen_native_order: deque[str] = deque()
        self._dynamic_configurations: dict[str, DynamicToolConfiguration] = {}
        self._operation_dynamic_configurations: dict[str, DynamicToolConfiguration] = {}
        self._pending_dynamic_batches: dict[str, tuple[ProviderSessionRef, OperationContext, str]] = {}

    @property
    def connection_generation(self) -> int:
        return self._generation

    @property
    def lifecycle_state(self) -> LifecycleState:
        return self._lifecycle

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
                if self._lifecycle is not LifecycleState.CONNECTED:
                    raise TransportLoss("provider adapter connection is not available")
                return self._client
            self._lifecycle = LifecycleState.CONNECTING
            try:
                recording = self._new_transport()
            except TransportLoss:
                self._lifecycle = LifecycleState.DISCONNECTED
                raise
            try:
                recording.connect()
                if self._client_factory is CodexAppServerClient:
                    client = self._client_factory(
                        recording,
                        self.timeout_seconds,
                        max_received_events=self.max_events,
                        strict_dynamic_tool_binding=True,
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
                self._lifecycle = LifecycleState.DISCONNECTED
                raise TransportLoss(str(exc)) from exc
            except (AppServerProtocolError, AppServerRemoteError) as exc:
                recording.close()
                self._lifecycle = LifecycleState.DISCONNECTED
                raise ProtocolError(str(exc)) from exc
            self._recording_transport = recording
            self._client = client
            self._generation += 1
            self._initialized = True
            self._lifecycle = LifecycleState.CONNECTED
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
        # Return the caller's validated reference so a reconnect can expose
        # the new generation while retaining the same opaque provider handle.
        return session

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
    def _map_error(
        exc: AppServerError,
        recording: _RecordingTransport | None,
        method: str,
        *,
        after: int | None = None,
    ) -> AdapterError:
        state = recording.request_state(method, after=after) if recording is not None else None
        if isinstance(exc, AppServerRemoteError):
            return ProtocolError(str(exc))
        if isinstance(exc, AppServerProtocolError) and state in {
            "possibly_sent",
            "sent_awaiting_response",
            "unknown",
            "accepted",
        }:
            return SideEffectUnknown(
                f"{method} response was malformed after the request was sent; provider outcome is unknown"
            )
        if isinstance(exc, AppServerTransportError):
            if state in {"possibly_sent", "sent_awaiting_response", "unknown"}:
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
            request_checkpoint = self._recording_transport.checkpoint() if self._recording_transport is not None else None
            try:
                payload = client.model_list()
            except AppServerError as exc:
                mapped = self._map_error(exc, self._recording_transport, "model/list", after=request_checkpoint)
                if isinstance(exc, AppServerTransportError) and "timed out" not in str(exc).casefold():
                    self._mark_disconnected()
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

    def _mark_disconnected(self) -> None:
        """Fence a dead connection without discarding session/operation identity."""
        self._lifecycle = LifecycleState.DISCONNECTED
        client, self._client = self._client, None
        self._recording_transport = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _remember_operation(self, context: OperationContext, turn_id: str) -> None:
        if context.operation_id not in self._operations and len(self._operations) >= self.max_operation_history:
            raise AdapterBusy(
                "operation admission capacity exhausted; retired identities are not replayable"
            )
        self._operations[context.operation_id] = (context, turn_id)
        self._terminal_operations.discard(context.operation_id)
        try:
            self._operation_order.remove(context.operation_id)
        except ValueError:
            pass
        self._operation_order.append(context.operation_id)
        # Operation identities are admission protection, not expendable
        # diagnostics. Once the finite guarantee window is full, callers get
        # an explicit rejection instead of allowing an old id to replay.

    def _rebind_operations(self, session_id: str, generation: int) -> None:
        """Keep execution/attempt/operation ids while moving to a new connection."""
        for operation_id, (context, turn_id) in tuple(self._operations.items()):
            if context.session_id != session_id:
                continue
            self._operations[operation_id] = (replace(context, connection_generation=generation), turn_id)

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
            if clinx_session_id is not None and clinx_session_id in self._sessions:
                raise CorrelationError("provider session id is already registered")
            if len(self._sessions) >= self.max_sessions:
                raise AdapterBusy("session admission capacity exhausted")
            client = self._ensure_client()
            request_checkpoint = self._recording_transport.checkpoint() if self._recording_transport is not None else None
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
                mapped = self._map_error(exc, self._recording_transport, "thread/start", after=request_checkpoint)
                if isinstance(exc, AppServerTransportError) and "timed out" not in str(exc).casefold():
                    self._mark_disconnected()
                raise mapped from exc
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
            request_checkpoint = self._recording_transport.checkpoint() if self._recording_transport is not None else None
            try:
                client.thread_resume(session.provider_handle)
            except AppServerError as exc:
                mapped = self._map_error(exc, self._recording_transport, "thread/resume", after=request_checkpoint)
                if isinstance(mapped, SideEffectUnknown):
                    self._ambiguous_sessions.add(session.clinx_session_id)
                if isinstance(exc, AppServerTransportError) and "timed out" not in str(exc).casefold():
                    self._mark_disconnected()
                raise mapped from exc
            return session

    def reconnect(self, session: ProviderSessionRef) -> ProviderSessionRef:
        """Create a new connection generation and resume one exact session."""
        with self._lock:
            if not isinstance(session, ProviderSessionRef):
                raise TypeError("session must be ProviderSessionRef")
            session = self._sessions.get(session.clinx_session_id, session)
            if self._transport_factory is None:
                raise TransportLoss("reconnect requires a transport factory")
            old_client = self._client
            self._client = None
            self._recording_transport = None
            # Strict staged requests belong to the old live client/binding;
            # they are not silently transferred across a reconnect.
            self._pending_dynamic_batches.clear()
            self._initialized = False
            self._lifecycle = LifecycleState.DISCONNECTED
            self._closed = False
            if old_client is not None:
                try:
                    old_client.close()
                except Exception:
                    pass
            # _ensure_client increments the generation only after initialize.
            self._ensure_client()
            rebound = ProviderSessionRef(
                session.clinx_session_id,
                session.provider_id,
                session.provider_handle,
                self._generation,
                session.metadata,
            )
            resumed = self.resume_session(rebound)
            self._rebind_operations(resumed.clinx_session_id, self._generation)
            configuration = self._dynamic_configurations.get(resumed.clinx_session_id)
            if configuration is not None:
                active_entry = self._operations.get(self._active_operation_id or "")
                operation = (
                    active_entry[0]
                    if active_entry is not None
                    and active_entry[0].session_id == resumed.clinx_session_id
                    and active_entry[0].operation_id in self._operation_dynamic_configurations
                    else None
                )
                self._configure_dynamic_tool(resumed, configuration, operation=operation)
                if operation is not None and active_entry is not None:
                    self._client.attach_dynamic_tool_turn(resumed.provider_handle or "", active_entry[1])
            self._sessions[resumed.clinx_session_id] = resumed
            return resumed

    def configure_dynamic_tool(
        self,
        session: ProviderSessionRef,
        configuration: DynamicToolConfiguration,
    ) -> None:
        with self._lock:
            session = self._require_session(session)
            clear = getattr(self._ensure_client(), "clear_dynamic_tool", None)
            if callable(clear):
                clear()
            self._configure_dynamic_tool(session, configuration, operation=None)

    def _configure_dynamic_tool(
        self,
        session: ProviderSessionRef,
        configuration: DynamicToolConfiguration,
        *,
        operation: OperationContext | None,
    ) -> None:
        if not isinstance(configuration, DynamicToolConfiguration):
            raise TypeError("configuration must be DynamicToolConfiguration")
        self._dynamic_configurations[session.clinx_session_id] = configuration
        if operation is not None:
            self._operation_dynamic_configurations[operation.operation_id] = configuration

        def bound_handler(params: dict[str, Any]) -> dict[str, Any]:
            if operation is not None:
                supplied_operation = params.get("operationId") or params.get("operation_id")
                if supplied_operation is not None and supplied_operation != operation.operation_id:
                    raise CorrelationError("dynamic tool operation identity changed")
                supplied_generation = params.get("connectionGeneration")
                if supplied_generation is not None and supplied_generation != self._generation:
                    raise CorrelationError("dynamic tool connection generation changed")
            return configuration.handler(params)

        if not session.provider_handle:
            raise ProtocolError("dynamic tools require a provider thread handle")
        try:
            self._ensure_client().configure_dynamic_tool(
                namespace=configuration.namespace,
                name=configuration.name,
                thread_id=session.provider_handle,
                handler=bound_handler,
                operation_id=operation.operation_id if operation is not None else None,
                connection_generation=self._generation if operation is not None else None,
            )
        except AppServerError as exc:
            if isinstance(exc, AppServerTransportError) and "timed out" not in str(exc).casefold():
                self._mark_disconnected()
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
            if session.clinx_session_id in self._ambiguous_sessions:
                raise SideEffectUnknown("provider session outcome is unknown; admission is quarantined")
            if self._ambiguous_operations:
                raise SideEffectUnknown("provider connection has an ambiguous operation; admission is quarantined")
            if self._active_operation_id is not None and self._active_operation_id != context.operation_id:
                raise AdapterBusy("one active turn is allowed per provider connection")
            if context.operation_id in self._operations:
                raise AdapterBusy("turn start would duplicate an existing operation")
            if context.operation_id in self._ambiguous_operations:
                raise SideEffectUnknown("ambiguous turn start cannot be resent without provider recovery")
            if len(self._operations) >= self.max_operation_history:
                raise AdapterBusy(
                    "operation admission capacity exhausted; retired identities are not replayable"
                )
            if dynamic_tool is not None:
                clear = getattr(self._ensure_client(), "clear_dynamic_tool", None)
                if callable(clear):
                    clear()
                self._configure_dynamic_tool(session, dynamic_tool, operation=context)
            else:
                self._dynamic_configurations.pop(session.clinx_session_id, None)
                client = self._ensure_client()
                clear = getattr(client, "clear_dynamic_tool", None)
                if callable(clear):
                    clear()
            client = self._ensure_client()
            request_checkpoint = self._recording_transport.checkpoint() if self._recording_transport is not None else None
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
                mapped = self._map_error(exc, self._recording_transport, "turn/start", after=request_checkpoint)
                if isinstance(mapped, SideEffectUnknown):
                    self._ambiguous_operations.add(context.operation_id)
                else:
                    self._operation_dynamic_configurations.pop(context.operation_id, None)
                if (
                    isinstance(exc, AppServerTransportError)
                    and "timed out" not in str(exc).casefold()
                    and not (
                        self._recording_transport is not None
                        and self._recording_transport.request_state("turn/start", after=request_checkpoint) == "definitely_unsent"
                    )
                ):
                    self._mark_disconnected()
                raise mapped from exc
            if dynamic_tool is not None:
                try:
                    client.attach_dynamic_tool_turn(session.provider_handle or "", info.turn_id)
                except AppServerError as exc:
                    self._ambiguous_operations.add(context.operation_id)
                    self._pending_dynamic_batches[context.operation_id] = (session, context, info.turn_id)
                    raise SideEffectUnknown(
                        "turn was accepted but dynamic-tool turn attachment failed; provider outcome is unknown"
                    ) from exc
            self._remember_operation(context, info.turn_id)
            self._active_operation_id = context.operation_id
            return AdapterOperation(
                Outcome(OutcomeCode.ACCEPTED, provider_reference=info.turn_id, side_effect_state="request_accepted"),
                session=session,
                context=context,
            )

    def resume_dynamic_tool_batch(
        self,
        session: ProviderSessionRef,
        context: OperationContext,
    ) -> AdapterOperation:
        """Continue an interrupted strict-tool batch on the same binding.

        The initial turn/start is never resent.  The client first redelivers
        any cached response that is still pending, then flushes the remaining
        staged requests.  Operation admission is recorded only after that
        continuation succeeds, so an unresolved transport result remains
        explicitly quarantined.
        """
        with self._lock:
            session = self._require_session(session)
            pending = self._pending_dynamic_batches.get(context.operation_id)
            if pending is None:
                raise CorrelationError("dynamic tool batch continuation is not registered")
            pending_session, pending_context, turn_id = pending
            if pending_session.clinx_session_id != session.clinx_session_id or pending_context != context:
                raise CorrelationError("dynamic tool batch continuation context changed")
            try:
                self._ensure_client().attach_dynamic_tool_turn(session.provider_handle or "", turn_id)
            except AppServerTransportError as exc:
                raise SideEffectUnknown(
                    "dynamic-tool batch continuation response delivery is unresolved"
                ) from exc
            except AppServerError as exc:
                raise ProtocolError(str(exc)) from exc
            self._remember_operation(context, turn_id)
            self._active_operation_id = context.operation_id
            self._pending_dynamic_batches.pop(context.operation_id, None)
            self._ambiguous_operations.discard(context.operation_id)
            return AdapterOperation(
                Outcome(
                    OutcomeCode.ACCEPTED,
                    provider_reference=turn_id,
                    side_effect_state="request_accepted",
                    detail="strict dynamic-tool batch continuation completed",
                ),
                session=session,
                context=context,
            )

    def continue_turn(
        self,
        session: ProviderSessionRef,
        context: OperationContext,
        prompt: str,
        **kwargs: Any,
    ) -> AdapterOperation:
        """Start a subsequent provider turn while preserving CLINX identity.

        A continuation gets a fresh operation id/context from the caller but
        keeps the exact provider session and current connection generation.
        Reusing an existing operation id remains rejected to prevent duplicate
        provider side effects.
        """
        return self.start_turn(session, context, prompt, **kwargs)

    def observe(
        self,
        context: OperationContext,
        *,
        max_events: int | None = None,
        timeout_seconds: float = 0.0,
    ) -> AdapterOperation:
        with self._lock:
            registered, _turn_id = self._require_context(context)
            if self._active_operation_id is not None and self._active_operation_id != registered.operation_id:
                raise AdapterBusy("observer does not own the active provider operation")
            client = self._ensure_client()
            limit = self.max_events if max_events is None else max_events
            try:
                raw_events = client.drain_events(max_events=limit, timeout_seconds=timeout_seconds)
            except AppServerTransportError as exc:
                partial = getattr(exc, "events", ())
                if partial:
                    normalized = self._finish_observation(registered, partial)
                    mapped = TransportLoss(str(exc))
                    mapped.events = normalized.events
                    if "timed out" not in str(exc).casefold():
                        self._mark_disconnected()
                    raise mapped from exc
                if "timed out" in str(exc).casefold() and timeout_seconds > 0:
                    raise TimeoutError(str(exc)) from exc
                if "timed out" not in str(exc).casefold():
                    self._mark_disconnected()
                raise TransportLoss(str(exc)) from exc
            except AppServerProtocolError as exc:
                raise ProtocolError(str(exc)) from exc
            return self._finish_observation(registered, raw_events)

    def _finish_observation(
        self,
        registered: OperationContext,
        raw_events: Any,
    ) -> AdapterOperation:
        normalized = tuple(self.normalize_event(raw, registered) for raw in raw_events)
        if any(event.event_type == "clinx/observation_overflow" for event in normalized):
            raise ProtocolError("bounded provider observation buffer overflowed")
        exact_terminal = [event for event in normalized if event.terminal_observed and event.authority_eligible]
        if exact_terminal:
            self._active_operation_id = None
            self._terminal_operations.add(registered.operation_id)
            clear = getattr(self._client, "clear_dynamic_tool", None)
            if callable(clear):
                clear()
        confirmed_cancel = any(event.cancel_state is CancelState.CONFIRMED for event in exact_terminal)
        outcome = Outcome(
            OutcomeCode.CANCEL_CONFIRMED if confirmed_cancel else OutcomeCode.OBSERVED,
            side_effect_state="provider_evidence_only",
            detail="no events observed" if not normalized else None,
        )
        return AdapterOperation(outcome, context=registered, events=normalized)

    def interrupt(self, context: OperationContext) -> AdapterOperation:
        with self._lock:
            registered, turn_id = self._require_context(context)
            if registered.operation_id in self._terminal_operations:
                raise CorrelationError("cannot interrupt a terminal operation")
            session = self._sessions.get(registered.session_id)
            if session is None or not session.provider_handle:
                raise CorrelationError("operation session has no provider handle")
            if context.operation_id in self._cancel_requested:
                return AdapterOperation(
                    Outcome(OutcomeCode.CANCEL_REQUESTED, provider_reference=turn_id, side_effect_state="request_already_delivered"),
                    context=registered,
                )
            client = self._ensure_client()
            request_checkpoint = self._recording_transport.checkpoint() if self._recording_transport is not None else None
            try:
                client.turn_interrupt(session.provider_handle, turn_id)
            except AppServerError as exc:
                mapped = self._map_error(exc, self._recording_transport, "turn/interrupt", after=request_checkpoint)
                if isinstance(mapped, SideEffectUnknown):
                    self._cancel_requested.add(context.operation_id)
                    raise TimeoutError("interrupt response was lost; cancellation is not confirmed") from exc
                if isinstance(exc, AppServerTransportError) and "timed out" not in str(exc).casefold():
                    self._mark_disconnected()
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
            if context is not None and not isinstance(context, OperationContext):
                raise TypeError("context must be OperationContext")
            validated_registered = None
            if context is not None:
                # Validate the caller's complete registered identity before
                # consuming sequence/dedup state or interpreting the event.
                validated_registered, _ = self._require_context(context)
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
            supplied_generation = params.get("connectionGeneration")
            generation_valid = supplied_generation is None or (
                isinstance(supplied_generation, int) and not isinstance(supplied_generation, bool)
            )
            event_generation = supplied_generation if generation_valid and supplied_generation is not None else self._generation
            correlation = CorrelationStatus.UNKNOWN
            event_operation: OperationContext | None = None
            identity_mismatch = False
            if context is not None:
                registered_entry = self._operations.get(context.operation_id)
                registered = validated_registered if validated_registered is not None else (
                    registered_entry[0] if registered_entry else None
                )
                expected_turn = registered_entry[1] if registered_entry else None
                expected_session = self._sessions.get(context.session_id)
                expected_thread = expected_session.provider_handle if expected_session else None
                supplied_context = params.get("context") if isinstance(params.get("context"), Mapping) else {}
                supplied_assignment = params.get("assignmentId") or params.get("assignment_id")
                if supplied_assignment is None:
                    supplied_assignment = supplied_context.get("assignmentId") or supplied_context.get("assignment_id")
                if supplied_assignment is None:
                    assignment = params.get("assignment")
                    if isinstance(assignment, Mapping):
                        supplied_assignment = assignment.get("id") or assignment.get("assignmentId") or assignment.get("assignment_id")
                    elif assignment is not None:
                        supplied_assignment = assignment
                identity_fields = (
                    (("executionId", "execution_id"), "execution_id"),
                    (("attemptId", "attempt_id"), "attempt_id"),
                    (("sessionId", "session_id"), "session_id"),
                    (("operationId", "operation_id"), "operation_id"),
                )
                for aliases, field in identity_fields:
                    supplied = next((params.get(key) for key in aliases if params.get(key) is not None), None)
                    if supplied is None:
                        supplied = next((supplied_context.get(key) for key in aliases if supplied_context.get(key) is not None), None)
                    if supplied is not None and supplied != getattr(context, field):
                        identity_mismatch = True
                if supplied_assignment is not None and supplied_assignment != context.assignment_id:
                    identity_mismatch = True
                if not generation_valid:
                    correlation = CorrelationStatus.UNKNOWN
                elif event_generation != context.connection_generation:
                    correlation = CorrelationStatus.STALE_GENERATION
                elif identity_mismatch:
                    correlation = CorrelationStatus.MISMATCH
                elif registered is None or expected_thread is None:
                    correlation = CorrelationStatus.MISMATCH
                elif not isinstance(thread_id, str) or not thread_id.strip() or not isinstance(turn_id, str) or not turn_id.strip():
                    correlation = CorrelationStatus.UNKNOWN
                elif thread_id == expected_thread and turn_id == expected_turn:
                    correlation = (
                        CorrelationStatus.HISTORICAL
                        if context.operation_id in self._terminal_operations
                        else CorrelationStatus.EXACT
                    )
                    event_operation = registered
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
            known_terminal_methods = {
                "turn/completed",
                "turn/complete",
                "turn/failed",
                "turn/cancelled",
                "turn/canceled",
            }
            known_terminal_statuses = {"completed", "failed", "interrupted", "cancelled", "canceled"}
            terminal_status = status_lower if status_lower in known_terminal_statuses else None
            terminal = method in known_terminal_methods and terminal_status is not None
            cancel_state = CancelState.NOT_REQUESTED
            if (
                event_operation is not None
                and correlation is CorrelationStatus.EXACT
                and event_operation.operation_id in self._cancel_requested
            ):
                cancel_state = CancelState.CONFIRMED if terminal_status in {"interrupted", "cancelled", "canceled"} else CancelState.REQUESTED
            error = params.get("error")
            error_code = None
            if isinstance(error, Mapping):
                value = error.get("code") or error.get("type") or error.get("message")
                error_code = str(value) if value is not None else None
            elif status_lower in {"failed", "error"}:
                error_code = str(params.get("code") or status or "provider_error")
            payload = self._bounded_payload(params)
            duplicate = native_id is not None and native_id in self._seen_native_ids
            if native_id is not None and not (context is not None and identity_mismatch):
                self._seen_native_ids.add(native_id)
                self._seen_native_order.append(native_id)
                while len(self._seen_native_order) > self.max_seen_native_ids:
                    self._seen_native_ids.discard(self._seen_native_order.popleft())
            receipt = f"{self.provider_id}:{self._generation}:{sequence}"
            if native_id is not None:
                receipt = f"{self.provider_id}:{self._generation}:native:{native_id}"
            event = NormalizedEvent(
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
                terminal_status=terminal_status,
                cancel_state=cancel_state,
                error_code=error_code,
                evidence_reference=receipt,
                duplicate=duplicate,
                payload=payload,
            )
            if event.authority_eligible and event.terminal_observed and event.operation is not None:
                self._terminal_operations.add(event.operation.operation_id)
            return event

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
            self._initialized = False
            self._lifecycle = LifecycleState.CLOSED
            self._pending_dynamic_batches.clear()
            client, self._client = self._client, None
            self._recording_transport = None
            if client is not None:
                try:
                    client.close()
                except Exception:
                    # close is idempotent and cleanup failures do not become
                    # a terminal CLINX execution verdict.
                    pass
