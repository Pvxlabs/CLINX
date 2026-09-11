"""Provider-neutral adapters for the CLINX control plane.

The package is intentionally not imported by the V1 bridge or MCP server.  A
caller must explicitly construct an adapter, so adding these contracts cannot
silently change the V1 execution path.
"""

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
    UnsupportedCapability,
)
from .codex import CodexProviderAdapter
from .fixtures import ScriptedTransport, SyntheticProviderAdapter
from .protocol import ProviderAdapter

__all__ = [
    "AdapterOperation",
    "AdapterBusy",
    "AdapterError",
    "Capability",
    "CapabilityStatus",
    "Capabilities",
    "CancelState",
    "CodexProviderAdapter",
    "CorrelationError",
    "CorrelationStatus",
    "DynamicToolConfiguration",
    "EventKind",
    "LifecycleState",
    "NormalizedEvent",
    "OperationContext",
    "Outcome",
    "OutcomeCode",
    "ProtocolError",
    "ProviderSessionRef",
    "ProviderAdapter",
    "SideEffectUnknown",
    "ScriptedTransport",
    "SyntheticProviderAdapter",
    "TimeoutError",
    "TransportLoss",
    "UnsupportedCapability",
]
