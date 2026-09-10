"""Provider-owned continuity contracts for CLINX V2."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from ._model import DomainModel, JsonDocument, json_document, optional_text, require_text


@dataclasses.dataclass(frozen=True)
class ProviderSessionState(DomainModel):
    lifecycle: str = "DISCOVERED"
    thread_reference: str | None = None
    session_reference: str | None = None
    resume_token_reference: str | None = None
    provider_cursor: str | None = None
    provider_data: JsonDocument | Mapping[str, Any] = dataclasses.field(
        default_factory=JsonDocument.empty
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "lifecycle", require_text("lifecycle", self.lifecycle))
        for name in (
            "thread_reference",
            "session_reference",
            "resume_token_reference",
            "provider_cursor",
        ):
            object.__setattr__(self, name, optional_text(name, getattr(self, name)))
        object.__setattr__(self, "provider_data", json_document(self.provider_data))


@dataclasses.dataclass(frozen=True)
class ProviderSession(DomainModel):
    provider_session_id: str
    provider_id: str
    adapter_kind: str
    created_at: str | None = None
    state: ProviderSessionState = dataclasses.field(default_factory=ProviderSessionState)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_session_id",
            require_text("provider_session_id", self.provider_session_id),
        )
        object.__setattr__(self, "provider_id", require_text("provider_id", self.provider_id))
        object.__setattr__(
            self, "adapter_kind", require_text("adapter_kind", self.adapter_kind)
        )
        object.__setattr__(self, "created_at", optional_text("created_at", self.created_at))
