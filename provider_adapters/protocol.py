"""Structural provider-neutral adapter protocol.

The protocol is deliberately expressed in CLINX terms.  Implementations may
use provider thread/turn names internally, but callers only depend on these
opaque references and normalized observations.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from .contracts import (
    AdapterOperation,
    Capabilities,
    DynamicToolConfiguration,
    NormalizedEvent,
    OperationContext,
    ProviderSessionRef,
)


class ProviderAdapter(Protocol):
    def discover_capabilities(self) -> Capabilities: ...

    def create_session(self, **kwargs: Any) -> ProviderSessionRef: ...

    def resume_session(self, session: ProviderSessionRef) -> ProviderSessionRef: ...

    def start_turn(
        self,
        session: ProviderSessionRef,
        context: OperationContext,
        prompt: str,
        **kwargs: Any,
    ) -> AdapterOperation: ...

    def continue_turn(
        self,
        session: ProviderSessionRef,
        context: OperationContext,
        prompt: str,
        **kwargs: Any,
    ) -> AdapterOperation: ...

    def observe(self, context: OperationContext, **kwargs: Any) -> AdapterOperation: ...

    def interrupt(self, context: OperationContext) -> AdapterOperation: ...

    def normalize_event(
        self,
        raw: Mapping[str, Any],
        context: OperationContext | None = None,
    ) -> NormalizedEvent: ...

    def configure_dynamic_tool(
        self,
        session: ProviderSessionRef,
        configuration: DynamicToolConfiguration,
    ) -> None: ...

    def close(self) -> None: ...

