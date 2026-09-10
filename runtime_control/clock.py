"""Coordinator clock contracts used by runtime leases."""

from __future__ import annotations

import datetime as _datetime
import threading
from typing import Protocol


UTC = _datetime.timezone.utc


class Clock(Protocol):
    def now(self) -> _datetime.datetime:
        """Return an aware UTC coordinator timestamp."""


class SystemClock:
    def now(self) -> _datetime.datetime:
        return _datetime.datetime.now(UTC)


class ManualClock:
    """Thread-safe deterministic clock for bounded qualification fixtures."""

    def __init__(self, value: _datetime.datetime | str):
        self._lock = threading.Lock()
        self._value = normalize_time(value)

    def now(self) -> _datetime.datetime:
        with self._lock:
            return self._value

    def set(self, value: _datetime.datetime | str) -> None:
        with self._lock:
            self._value = normalize_time(value)

    def advance(self, seconds: float) -> _datetime.datetime:
        with self._lock:
            self._value += _datetime.timedelta(seconds=seconds)
            return self._value


def normalize_time(value: _datetime.datetime | str) -> _datetime.datetime:
    if isinstance(value, str):
        value = _datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, _datetime.datetime):
        raise TypeError("clock value must be datetime or ISO timestamp")
    if value.tzinfo is None:
        raise ValueError("clock values must be timezone-aware")
    return value.astimezone(UTC)


def encode_time(value: _datetime.datetime | str) -> str:
    return normalize_time(value).isoformat(timespec="microseconds")


def decode_time(value: str) -> _datetime.datetime:
    return normalize_time(value)
