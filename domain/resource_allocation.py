"""Controlled resource ownership contracts for CLINX V2."""

from __future__ import annotations

import dataclasses

from ._model import DomainModel, optional_text, require_text


@dataclasses.dataclass(frozen=True)
class ResourceAllocationState(DomainModel):
    lifecycle: str = "REQUESTED"
    owner_attempt_id: str | None = None
    worker_id: str | None = None
    fencing_epoch: int = 0
    expires_at: str | None = None
    renewed_at: str | None = None
    release_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "lifecycle", require_text("lifecycle", self.lifecycle))
        for name in (
            "owner_attempt_id",
            "worker_id",
            "expires_at",
            "renewed_at",
            "release_reason",
        ):
            object.__setattr__(self, name, optional_text(name, getattr(self, name)))
        if self.fencing_epoch < 0:
            raise ValueError("fencing_epoch must not be negative")


@dataclasses.dataclass(frozen=True)
class ResourceAllocation(DomainModel):
    allocation_id: str
    resource_type: str
    resource_key: str
    capacity_units: int = 1
    state: ResourceAllocationState = dataclasses.field(
        default_factory=ResourceAllocationState
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allocation_id",
            require_text("allocation_id", self.allocation_id),
        )
        object.__setattr__(
            self, "resource_type", require_text("resource_type", self.resource_type)
        )
        object.__setattr__(
            self, "resource_key", require_text("resource_key", self.resource_key)
        )
        if self.capacity_units < 1:
            raise ValueError("capacity_units must be positive")
