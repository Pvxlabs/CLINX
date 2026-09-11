"""Typed provider adapter errors.

An exception describes the operation boundary; it never changes CLINX task or
execution state.  Callers must decide whether an observation is authorized.
"""

from __future__ import annotations

from .contracts import OutcomeCode


class AdapterError(RuntimeError):
    outcome_code = OutcomeCode.PROTOCOL_ERROR

    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message)
        self.detail = detail or message


class UnsupportedCapability(AdapterError):
    outcome_code = OutcomeCode.UNSUPPORTED


class ProtocolError(AdapterError):
    outcome_code = OutcomeCode.PROTOCOL_ERROR


class TransportLoss(AdapterError):
    outcome_code = OutcomeCode.TRANSPORT_LOSS


class TimeoutError(AdapterError):
    outcome_code = OutcomeCode.TIMEOUT


class SideEffectUnknown(AdapterError):
    outcome_code = OutcomeCode.SIDE_EFFECT_UNKNOWN


class CorrelationError(AdapterError):
    outcome_code = OutcomeCode.CORRELATION_FAILURE


class AdapterBusy(AdapterError):
    outcome_code = OutcomeCode.PROTOCOL_ERROR
