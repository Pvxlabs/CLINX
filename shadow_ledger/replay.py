"""Bounded offline replay and comparison for shadow event streams."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .errors import ReplayError
from .models import StoredEvent
from .store import EventStore


SUPPORTED_EVENT_SCHEMA_VERSION = 1
BASELINE_EVENT_TYPES = {
    "V1ExecutionClaimObserved",
    "V1UnattributedExecutionClaimObserved",
    "V1SnapshotBaselineImported",
}
SUPPORTED_EVENT_TYPES = BASELINE_EVENT_TYPES | {
    "V1ExecutionProgressObserved",
    "V1HostExecutionStartedObserved",
    "V1HostEvidenceObserved",
    "V1ExecutionResultPersistedObserved",
    "V1TerminalStateObserved",
    "V1LeaseReleasedObserved",
    "V1UnattributedLeaseReleasedObserved",
}
COMPARISON_FIELDS = (
    "aggregate_type",
    "task_id",
    "execution_id",
    "source_task_id",
    "source_execution_ref",
    "execution_state",
    "current_stage",
    "exact_result_ref",
    "turn_id",
    "lease_state",
    "stream_version",
    "attribution",
)
EXCLUDED_FIELDS = (
    "timestamps",
    "database_cursor",
    "route_and_policy_payloads",
    "raw_provider_identifiers",
    "raw_host_output",
    "linear_projection_state",
    "pre_baseline_history",
)


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclasses.dataclass(frozen=True)
class ReplayResult:
    aggregate_type: str
    aggregate_id: str
    task_id: str
    execution_id: str | None
    source_task_id: str
    source_execution_ref: str | None
    execution_state: str
    current_stage: str
    exact_result_ref: str | None
    turn_id: str | None
    lease_state: str
    stream_version: int
    attribution: str
    evidence_references: tuple[str, ...]
    history_before_baseline: str
    event_count: int
    first_cursor: int
    last_cursor: int
    summary_hash: str

    def to_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["evidence_references"] = list(self.evidence_references)
        return value

    def to_json(self) -> str:
        return _canonical(self.to_dict())


@dataclasses.dataclass(frozen=True)
class ReplayDifference:
    field: str
    expected: Any
    actual: Any


@dataclasses.dataclass(frozen=True)
class CompareResult:
    status: str
    differences: tuple[ReplayDifference, ...]
    compared_fields: tuple[str, ...] = COMPARISON_FIELDS
    excluded_fields: tuple[str, ...] = EXCLUDED_FIELDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "differences": [dataclasses.asdict(item) for item in self.differences],
            "compared_fields": list(self.compared_fields),
            "excluded_fields": list(self.excluded_fields),
        }


class ReplayReducer:
    """Pure reducer over one versioned stream and its explicit baseline."""

    def replay(self, events: Iterable[StoredEvent]) -> ReplayResult:
        state: dict[str, Any] | None = None
        expected_version = 1
        first_cursor = 0
        last_cursor = 0
        event_count = 0
        evidence: list[str] = []

        for stored in events:
            event = stored.event
            if event.schema_version != SUPPORTED_EVENT_SCHEMA_VERSION:
                raise ReplayError(
                    f"unsupported event schema_version {event.schema_version}"
                )
            if event.event_type not in SUPPORTED_EVENT_TYPES:
                raise ReplayError(f"unsupported event_type {event.event_type}")
            if event.aggregate_version != expected_version:
                raise ReplayError(
                    f"stream version gap or order error: expected {expected_version}, "
                    f"received {event.aggregate_version}"
                )
            if event_count == 0:
                first_cursor = stored.cursor
                if event.event_type not in BASELINE_EVENT_TYPES:
                    raise ReplayError("stream is missing an explicit baseline")
            elif state is not None and (
                event.aggregate_type != state["aggregate_type"]
                or event.aggregate_id != state["aggregate_id"]
            ):
                raise ReplayError("replay input contains more than one aggregate stream")

            payload = event.payload.to_dict()
            if state is None:
                self._validate_baseline(event.event_type, payload)
                state = {
                    "aggregate_type": event.aggregate_type,
                    "aggregate_id": event.aggregate_id,
                    "task_id": payload["task_id"],
                    "execution_id": payload.get("execution_id"),
                    "source_task_id": payload["source_task_id"],
                    "source_execution_ref": payload.get("source_execution_ref"),
                    "execution_state": payload.get("execution_state", "UNKNOWN"),
                    "current_stage": payload.get("current_stage", "UNKNOWN"),
                    "exact_result_ref": payload.get("exact_result_ref"),
                    "turn_id": payload.get("turn_id"),
                    "lease_state": payload.get("lease_state", "UNKNOWN"),
                    "attribution": payload["attribution"],
                    "history_before_baseline": payload.get(
                        "history_before_baseline", "UNKNOWN"
                    ),
                }
            else:
                self._validate_correlation(state, payload)
                self._apply(state, event.event_type, payload, evidence)

            evidence_ref = payload.get("evidence_ref")
            if evidence_ref is not None and evidence_ref not in evidence:
                evidence.append(evidence_ref)
            event_count += 1
            last_cursor = stored.cursor
            expected_version += 1

        if state is None:
            raise ReplayError("stream has no events")
        state["stream_version"] = expected_version - 1
        state["evidence_references"] = tuple(evidence)
        state["event_count"] = event_count
        state["first_cursor"] = first_cursor
        state["last_cursor"] = last_cursor
        summary_input = {
            key: value
            for key, value in state.items()
            if key not in {"first_cursor", "last_cursor"}
        }
        state["summary_hash"] = hashlib.sha256(
            _canonical(summary_input).encode("utf-8")
        ).hexdigest()
        return ReplayResult(**state)

    @staticmethod
    def _validate_baseline(event_type: str, payload: dict[str, Any]) -> None:
        required = {
            "task_id",
            "source_task_id",
            "execution_state",
            "current_stage",
            "lease_state",
            "attribution",
        }
        missing = sorted(required.difference(payload))
        if missing:
            raise ReplayError("baseline is missing fields: " + ", ".join(missing))
        if payload["attribution"] == "EXACT":
            for name in ("execution_id", "source_execution_ref"):
                if not payload.get(name):
                    raise ReplayError(f"exact baseline is missing {name}")
        if event_type == "V1UnattributedExecutionClaimObserved" and (
            payload["attribution"] != "UNATTRIBUTED"
            or payload.get("execution_id") is not None
        ):
            raise ReplayError("legacy claim baseline invented execution identity")

    @staticmethod
    def _validate_correlation(state: dict[str, Any], payload: dict[str, Any]) -> None:
        for name in ("task_id", "source_task_id", "execution_id", "source_execution_ref"):
            if name in payload and payload[name] != state.get(name):
                raise ReplayError(f"correlation conflict for {name}")

    @staticmethod
    def _apply(
        state: dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
        evidence: list[str],
    ) -> None:
        if event_type in BASELINE_EVENT_TYPES:
            raise ReplayError("baseline event must be first in a stream")
        if event_type in {"V1ExecutionProgressObserved", "V1TerminalStateObserved"}:
            for name in ("execution_state", "current_stage", "turn_id"):
                if name in payload:
                    state[name] = payload[name]
            return
        if event_type == "V1ExecutionResultPersistedObserved":
            result_ref = payload.get("exact_result_ref")
            if not result_ref:
                raise ReplayError("result observation lacks exact_result_ref")
            if state["exact_result_ref"] not in {None, result_ref}:
                raise ReplayError("a different exact result already owns this stream")
            if state["source_execution_ref"] != result_ref:
                raise ReplayError("result does not match the stream execution identity")
            state["exact_result_ref"] = result_ref
            if "turn_id" in payload:
                existing_turn = state.get("turn_id")
                if existing_turn not in {None, payload["turn_id"]}:
                    raise ReplayError("result turn correlation conflicts with progress")
                state["turn_id"] = payload["turn_id"]
            return
        if event_type in {
            "V1LeaseReleasedObserved",
            "V1UnattributedLeaseReleasedObserved",
        }:
            if state["lease_state"] == "RELEASED":
                raise ReplayError("lease release observed more than once")
            state["lease_state"] = "RELEASED"
            return
        if event_type in {
            "V1HostExecutionStartedObserved",
            "V1HostEvidenceObserved",
        }:
            evidence_ref = payload.get("evidence_ref")
            if not evidence_ref:
                raise ReplayError("host observation lacks evidence_ref")
            return
        raise ReplayError(f"event order is not supported for {event_type}")


class ReplayService:
    def __init__(self, store: EventStore):
        self.store = store

    def replay_stream(
        self,
        aggregate_type: str,
        aggregate_id: str,
        *,
        batch_size: int = 100,
        max_events: int = 10_000,
        max_page_payload_bytes: int = 1_048_576,
    ) -> ReplayResult:
        batch_size = max(1, min(int(batch_size), 1000))
        max_events = max(1, int(max_events))

        def pages() -> Iterable[StoredEvent]:
            cursor = 0
            seen = 0
            while True:
                page = self.store.read_events(
                    after_cursor=cursor,
                    limit=batch_size,
                    max_payload_bytes=max_page_payload_bytes,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                )
                if not page:
                    return
                for item in page:
                    seen += 1
                    if seen > max_events:
                        raise ReplayError(
                            f"stream exceeds replay event limit {max_events}"
                        )
                    yield item
                cursor = page[-1].cursor

        return ReplayReducer().replay(pages())

    @staticmethod
    def compare(
        replayed: ReplayResult, expected: Mapping[str, Any]
    ) -> CompareResult:
        actual = replayed.to_dict()
        differences = tuple(
            ReplayDifference(field, expected.get(field), actual.get(field))
            for field in COMPARISON_FIELDS
            if expected.get(field) != actual.get(field)
        )
        return CompareResult("PASS" if not differences else "FAIL", differences)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay one CLINX shadow event stream")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--aggregate-type", required=True)
    parser.add_argument("--aggregate-id", required=True)
    parser.add_argument("--expected-json", type=Path)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-events", type=int, default=10_000)
    args = parser.parse_args(argv)

    service = ReplayService(EventStore(args.db))
    replayed = service.replay_stream(
        args.aggregate_type,
        args.aggregate_id,
        batch_size=args.batch_size,
        max_events=args.max_events,
    )
    output: dict[str, Any] = {"replay": replayed.to_dict()}
    exit_code = 0
    if args.expected_json is not None:
        expected = json.loads(args.expected_json.read_text(encoding="utf-8"))
        comparison = service.compare(replayed, expected)
        output["comparison"] = comparison.to_dict()
        exit_code = 0 if comparison.status == "PASS" else 1
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
