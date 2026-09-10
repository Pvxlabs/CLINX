"""Project identity and policy scope for CLINX V2."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from ._model import DomainModel, JsonDocument, json_document, require_text, string_tuple


@dataclasses.dataclass(frozen=True)
class ProjectState(DomainModel):
    lifecycle: str = "ACTIVE"
    policy_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "lifecycle", require_text("lifecycle", self.lifecycle))
        if self.policy_version < 1:
            raise ValueError("policy_version must be positive")


@dataclasses.dataclass(frozen=True)
class Project(DomainModel):
    project_id: str
    name: str
    product_scope: str
    repository_origins: tuple[str, ...] = ()
    default_policies: JsonDocument | Mapping[str, Any] = dataclasses.field(
        default_factory=JsonDocument.empty
    )
    provider_constraints: tuple[str, ...] = ()
    state: ProjectState = dataclasses.field(default_factory=ProjectState)

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", require_text("project_id", self.project_id))
        object.__setattr__(self, "name", require_text("name", self.name))
        object.__setattr__(
            self, "product_scope", require_text("product_scope", self.product_scope)
        )
        object.__setattr__(
            self,
            "repository_origins",
            string_tuple("repository_origins", self.repository_origins),
        )
        object.__setattr__(self, "default_policies", json_document(self.default_policies))
        object.__setattr__(
            self,
            "provider_constraints",
            string_tuple("provider_constraints", self.provider_constraints),
        )
