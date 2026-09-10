"""Framework-free primitives shared by the CLINX V2 domain contracts."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from typing import Any


class DomainValidationError(ValueError):
    """A domain snapshot contains an invalid identity or state value."""


def require_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{name} must be a non-empty string")
    return value.strip()


def optional_text(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    return require_text(name, value)


def string_tuple(name: str, values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        values = tuple(values)
    normalized = tuple(require_text(f"{name} entry", value) for value in values)
    if len(set(normalized)) != len(normalized):
        raise DomainValidationError(f"{name} must not contain duplicates")
    return normalized


@dataclasses.dataclass(frozen=True)
class JsonDocument:
    """An immutable, canonical JSON object used for policy and evidence data."""

    _canonical_json: str = dataclasses.field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self._canonical_json, str):
            raise DomainValidationError("canonical JSON must be a string")
        try:
            decoded = json.loads(self._canonical_json)
        except (TypeError, ValueError) as exc:
            raise DomainValidationError("canonical JSON is invalid") from exc
        if not isinstance(decoded, dict):
            raise DomainValidationError("JSON document must be an object")
        try:
            canonical = json.dumps(
                decoded,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise DomainValidationError(
                "canonical JSON must contain finite JSON values"
            ) from exc
        object.__setattr__(self, "_canonical_json", canonical)

    @classmethod
    def empty(cls) -> JsonDocument:
        return cls("{}")

    @classmethod
    def from_value(cls, value: JsonDocument | Mapping[str, Any] | None) -> JsonDocument:
        if isinstance(value, cls):
            return value
        if value is None:
            return cls.empty()
        if not isinstance(value, Mapping):
            raise DomainValidationError("JSON document must be an object")
        try:
            encoded = json.dumps(
                dict(value),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise DomainValidationError("JSON document must contain JSON values") from exc
        return cls(encoded)

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._canonical_json)

    def to_json(self) -> str:
        return self._canonical_json


def json_document(
    value: JsonDocument | Mapping[str, Any] | None,
) -> JsonDocument:
    return JsonDocument.from_value(value)


def _primitive(value: Any) -> Any:
    if isinstance(value, JsonDocument):
        return value.to_dict()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _primitive(getattr(value, field.name))
            for field in dataclasses.fields(value)
            if not field.name.startswith("_")
        }
    if isinstance(value, tuple):
        return [_primitive(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _primitive(item) for key, item in value.items()}
    return value


class DomainModel:
    """Serialization contract for immutable entity and state snapshots."""

    def to_dict(self) -> dict[str, Any]:
        value = _primitive(self)
        if not isinstance(value, dict):
            raise TypeError("domain model must serialize to an object")
        return value

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
