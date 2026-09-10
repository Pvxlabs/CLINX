"""Explicit failure classes for the CLINX V2 shadow ledger."""


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


class IdentityMappingConflict(ShadowLedgerError):
    pass


class LedgerBusy(ShadowLedgerError):
    pass


class ReplayError(ShadowLedgerError):
    pass


class ReplayBoundaryExceeded(ReplayError):
    pass
