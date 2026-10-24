"""Compatibility failures for the existing Desktop metadata event store."""


class ShadowLedgerError(RuntimeError):
    pass


class LedgerSchemaError(ShadowLedgerError):
    pass


class LedgerValidationError(ShadowLedgerError):
    pass


class PayloadSizeExceeded(LedgerValidationError):
    """An append payload exceeds the configured single-event write budget."""

    pass


class TransactionOwnershipError(ShadowLedgerError):
    pass


class AggregateVersionConflict(ShadowLedgerError):
    pass


class IdempotencyConflict(ShadowLedgerError):
    pass


class LedgerBusy(ShadowLedgerError):
    pass
