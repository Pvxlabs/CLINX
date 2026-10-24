"""Immutable serialization models used by the Desktop metadata outbox."""

from ._model import DomainValidationError, JsonDocument
from .event import Event, EventActor

__all__ = ("DomainValidationError", "Event", "EventActor", "JsonDocument")
