"""K2 review reproductions for the ownership input contract."""

from __future__ import annotations

import copy
import json

import pytest

from kernel_lab.protocol import run_once
from kernel_lab.reference import KernelReferenceError, evaluate_ownership_reference
from kernel_lab.traces import DEFAULT_BINARY, FIXTURE_DIR, run_seed


def _base() -> dict:
    document = json.loads((FIXTURE_DIR / "ownership_golden.json").read_text(encoding="utf-8"))
    return copy.deepcopy(document["base"])


def _assert_invalid(payload: dict, code: str) -> None:
    with pytest.raises(KernelReferenceError) as error:
        evaluate_ownership_reference(payload)
    assert error.value.code == code
    rust = run_once(DEFAULT_BINARY, "evaluate_ownership", payload, request_id=f"candidate-{code}")
    assert rust["ok"] is False
    assert rust["error"]["code"] == code


def test_unicode_text_limit_is_the_same_utf8_byte_boundary():
    payload = _base()
    owner = "界" * 171
    payload["snapshot"]["execution"]["execution_id"] = owner
    payload["snapshot"]["attempt"]["execution_id"] = owner
    payload["request"]["execution_id"] = owner
    _assert_invalid(payload, "INVALID_INPUT")


def test_unknown_lifecycle_is_invalid_not_a_live_candidate():
    payload = _base()
    payload["snapshot"]["execution"]["lifecycle"] = "MYSTERY"
    _assert_invalid(payload, "INVALID_INPUT")


def test_zero_assignment_epoch_is_invalid_for_active_ownership():
    payload = _base()
    payload["snapshot"]["assignment"]["resource_epoch"] = 0
    payload["snapshot"]["allocation"]["resource_epoch"] = 0
    payload["snapshot"]["resource"]["fencing_epoch"] = 0
    payload["snapshot"]["safety_handoff"]["resource_epoch"] = 0
    payload["request"]["resource_epoch"] = 0
    _assert_invalid(payload, "INVALID_INPUT")


def test_valid_owner_still_matches_both_reference_and_rust():
    payload = _base()
    reference = evaluate_ownership_reference(payload)
    rust = run_once(DEFAULT_BINARY, "evaluate_ownership", payload, request_id="candidate-valid")
    assert rust["ok"] is True
    assert rust["result"] == reference
    assert reference["decision"] == "ALLOW_CANDIDATE"


def test_runtime_control_readback_and_trace_outcomes_are_independent():
    report = run_seed(1808, binary=DEFAULT_BINARY, operations=256)
    evidence = report["ownership_evidence"]
    assert evidence["source"] == "actual_runtime_control_store"
    assert evidence["handoff"]["pending_readback"]["handoff_token"]
    assert evidence["handoff"]["completion"] == "COMMITTED_AND_CLEARED"
    assert evidence["synthetic_fixture_fields"] == []
    operations = report["operations"]["operations"]
    assert len(operations) == 256
    assert all(item["actual"] == item["expected"] for item in operations)
    assert all(
        item.get("error_type") != "AssertionError" and item.get("error_type") != "KeyError"
        for item in operations
    )
