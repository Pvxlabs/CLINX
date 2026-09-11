"""Fail-closed PVX-1808 qualification entry point.

This module is intentionally an evaluator, not an application dependency.  It
requires the real Rust release artifacts and reports each contract gate from
independent golden, differential, and store-backed checks.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable

from .protocol import run_once
from .reference import KernelReferenceError, evaluate_ownership_reference, replay_assignment_reference
from .traces import (
    DEFAULT_BINARY,
    FIXTURE_DIR,
    SEEDS,
    canonical_json,
    load_fixture_events,
    materialize_events,
    run_traces,
)


ROOT = Path(__file__).resolve().parents[1]
KERNEL_DIR = ROOT / "kernel"
BENCH_BINARY = DEFAULT_BINARY.with_name("clinx-kernel-bench")


class QualificationBlocked(RuntimeError):
    """A required toolchain or artifact is unavailable."""


def _patch(root: Any, path: str, value: Any) -> None:
    parts = path.split(".")
    current = root
    for part in parts[:-1]:
        current = current[int(part)] if isinstance(current, list) else current[part]
    final = parts[-1]
    if isinstance(current, list):
        current[int(final)] = value
    else:
        current[final] = value


def _load_json(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _versions() -> dict[str, str]:
    missing = [tool for tool in ("rustc", "cargo") if shutil.which(tool) is None]
    if missing:
        raise QualificationBlocked(f"BLOCKED_TOOLCHAIN: missing {', '.join(missing)}")
    versions: dict[str, str] = {}
    for tool in ("rustc", "cargo"):
        completed = subprocess.run(
            [tool, "--version"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        versions[tool] = completed.stdout.strip()
    return versions


def _require_artifacts(binary: Path) -> None:
    missing = [str(path) for path in (binary, binary.with_name("clinx-kernel-bench")) if not path.is_file()]
    if missing:
        raise QualificationBlocked(f"BLOCKED_ARTIFACTS: missing {', '.join(missing)}")


def _rust_result(binary: Path, operation: str, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
    response = run_once(binary, operation, payload, request_id=request_id)
    if not response["ok"]:
        raise AssertionError(f"Rust rejected expected-valid input: {response}")
    return response["result"]


def _ownership_gate(binary: Path) -> dict[str, Any]:
    document = _load_json("ownership_golden.json")
    base = document["base"]
    results: list[dict[str, Any]] = []
    for case in document["cases"]:
        payload = copy.deepcopy(base)
        for patch in case["patches"]:
            _patch(payload, patch["path"], patch["value"])
        reference = evaluate_ownership_reference(payload)
        rust = _rust_result(binary, "evaluate_ownership", payload, f"golden-{case['name']}")
        if reference != case["expected"] or rust != case["expected"] or rust != reference:
            raise AssertionError(
                f"ownership golden mismatch for {case['name']}: expected={case['expected']} "
                f"reference={reference} rust={rust}"
            )
        results.append({"name": case["name"], "matched": True})
    return {"cases": len(results), "matched": len(results), "results": results}


def _prefix_gate(binary: Path, filename: str, trace_name: str) -> dict[str, Any]:
    document = _load_json(filename)
    trace = next(item for item in document["traces"] if item["name"] == trace_name)
    events = materialize_events(trace["events"])
    prefixes = []
    for length in range(1, len(events) + 1):
        prefix = events[:length]
        reference = replay_assignment_reference(prefix)
        rust = _rust_result(binary, "replay_assignment", {"events": prefix}, f"{trace_name}-{length}")
        expected = trace["expected_prefixes"][length - 1]
        state = rust["state"]
        observed = {
            "event_type": prefix[-1]["event_type"],
            "assignment_lifecycle": state["assignment"]["lifecycle"],
            "assignment_version": state["assignment"]["version"],
            "allocation_lifecycle": state["allocation"]["lifecycle"],
            "allocation_version": state["allocation"]["version"],
            "attempt_lifecycle": state["attempt"]["lifecycle"],
            "attempt_version": state["attempt"]["version"],
            "recovery_state": state["recovery"]["state"] if state["recovery"] else None,
            "recovery_attempts": state["recovery"]["attempts"] if state["recovery"] else None,
        }
        if reference != rust or observed != expected:
            raise AssertionError(
                f"replay prefix mismatch {trace_name}/{length}: expected={expected} "
                f"observed={observed} reference={reference} rust={rust}"
            )
        prefixes.append({"length": length, "matched": True})
    return {"trace": trace_name, "events": len(events), "prefixes": prefixes}


def _legacy_gate(binary: Path) -> dict[str, Any]:
    document = _load_json("legacy_events.json")
    traces = [_prefix_gate(binary, "legacy_events.json", trace["name"]) for trace in document["traces"]]
    return {"traces": traces, "matched": True}


def _negative_gate(binary: Path) -> dict[str, Any]:
    document = _load_json("replay_negative.json")
    results = []
    for case in document["cases"]:
        source = load_fixture_events("assignment_golden.json", "current_grant_renew_release")
        if case["source"] != "current_grant_renew_release":
            source = load_fixture_events("legacy_events.json", case["source"])
        events = [copy.deepcopy(source[index]) for index in case["event_indexes"]]
        for patch in case["patches"]:
            _patch(events, patch["path"], patch["value"])
        if case.get("rehash"):
            for event in events:
                event["payload_hash"] = __import__("hashlib").sha256(
                    canonical_json(event["payload"]).encode("utf-8")
                ).hexdigest()

        try:
            replay_assignment_reference(events)
        except KernelReferenceError as error:
            reference_code = error.code
        else:
            raise AssertionError(f"Python reference accepted negative case {case['name']}")
        rust = run_once(binary, "replay_assignment", {"events": events}, request_id=f"negative-{case['name']}")
        if rust["ok"] or rust["error"]["code"] != case["expected_error"] or reference_code != case["expected_error"]:
            raise AssertionError(
                f"negative mismatch for {case['name']}: expected={case['expected_error']} "
                f"reference={reference_code} rust={rust}"
            )
        results.append({"name": case["name"], "error": case["expected_error"]})
    return {"cases": len(results), "matched": len(results), "results": results}


def run_qualification(*, binary: Path = DEFAULT_BINARY, seeds: tuple[int, ...] = SEEDS, operations: int = 256) -> dict[str, Any]:
    """Run all semantic gates and fail closed when Rust is unavailable."""
    versions = _versions()
    _require_artifacts(binary)
    ownership = _ownership_gate(binary)
    current = _prefix_gate(binary, "assignment_golden.json", "current_grant_renew_release")
    legacy = _legacy_gate(binary)
    negative = _negative_gate(binary)
    traces = run_traces(binary=binary, seeds=seeds, operations=operations)
    return {
        "contract_version": "CLINX_KERNEL_V1",
        "binary": str(binary),
        "toolchain": versions,
        "gates": {
            "RUST_BINARY_BUILT_AND_EXECUTED": "PASS",
            "OWNERSHIP_DECISION_DIFFERENTIAL": "PASS",
            "ASSIGNMENT_REPLAY_DIFFERENTIAL": "PASS",
            "INDEPENDENT_GOLDEN_INVARIANTS": "PASS",
            "INVALID_INPUT_FAILS_CLOSED": "PASS",
            "LEGACY_EVENT_COMPATIBILITY": "PASS",
            "MULTI_EXECUTION_IDENTITY_ISOLATION": "PASS",
            "REPOSITORY_RESIDENT_CONFORMANCE": "PASS",
        },
        "ownership": ownership,
        "current_assignment": current,
        "legacy": legacy,
        "negative": negative,
        "traces": traces,
        "passed": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run fail-closed PVX-1808 Rust kernel qualification")
    parser.add_argument("--binary", type=Path, default=DEFAULT_BINARY)
    parser.add_argument("--seed", type=int, action="append", dest="seeds")
    parser.add_argument("--operations", type=int, default=256)
    args = parser.parse_args()
    try:
        report = run_qualification(binary=args.binary, seeds=tuple(args.seeds or SEEDS), operations=args.operations)
    except (QualificationBlocked, AssertionError, FileNotFoundError, KernelReferenceError) as error:
        print(json.dumps({"passed": False, "status": "BLOCKED" if isinstance(error, QualificationBlocked) else "FAIL", "error": str(error)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
