"""Repository-resident conformance tests for the PVX-1808 experimental kernel."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from kernel_lab.protocol import (
    AUTHORITY,
    MAX_FRAME_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_STDERR_BYTES,
    KernelProcessError,
    KernelProtocolError,
    KernelSession,
    PROTOCOL_VERSION,
    run_once,
    strict_json_loads,
)
from kernel_lab.reference import KernelReferenceError, evaluate_ownership_reference, replay_assignment_reference
from kernel_lab.traces import (
    DEFAULT_BINARY,
    FIXTURE_DIR,
    load_fixture_events,
    materialize_events,
    run_seed,
)


ROOT = Path(__file__).resolve().parent


def _patch(root, path, value):
    parts = path.split(".")
    current = root
    for part in parts[:-1]:
        current = current[int(part)] if isinstance(current, list) else current[part]
    final = parts[-1]
    if isinstance(current, list):
        current[int(final)] = value
    else:
        current[final] = value


def _load(name):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _binary():
    if not DEFAULT_BINARY.is_file():
        pytest.fail(f"release Rust binary is required: {DEFAULT_BINARY}")
    return DEFAULT_BINARY


def _raw_exchange(command, frame: bytes, timeout: float = 2.0):
    completed = subprocess.run(command, input=frame, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=True)
    return json.loads(completed.stdout.splitlines()[0])


def _fake_process(mode: str):
    script = textwrap.dedent(
        f"""
        import sys, time
        mode = {mode!r}
        line = sys.stdin.buffer.readline()
        if mode == 'huge_response':
            sys.stdout.write('{{"request_id":"x","protocol_version":"CLINX_KERNEL_V1","ok":true,"authority":"NON_AUTHORITATIVE","result":{{"data":"' + ('x' * {MAX_RESPONSE_BYTES + 1}) + '"}}}}\\n')
            sys.stdout.flush()
        elif mode == 'huge_stderr':
            sys.stderr.write('x' * {MAX_STDERR_BYTES + 1})
            sys.stderr.flush()
        elif mode == 'truncated':
            sys.stdout.write('{{"request_id":"x"}}')
            sys.stdout.flush()
        elif mode == 'wrong_id':
            sys.stdout.write('{{"request_id":"wrong","protocol_version":"CLINX_KERNEL_V1","ok":true,"authority":"NON_AUTHORITATIVE","result":{{}}}}\\n')
            sys.stdout.flush()
        elif mode == 'timeout':
            time.sleep(10)
        elif mode == 'exit_before':
            raise SystemExit(7)
        elif mode == 'exit_after':
            sys.stdout.write('{{"request_id":"x","protocol_version":"CLINX_KERNEL_V1","ok":true,"authority":"NON_AUTHORITATIVE","result":{{"event_family":"RUNTIME_WORKER_V1","event_types":[],"not_covered_fields":[],"state":null,"states":{{}},"stream_version":0}}}}\\n')
            sys.stdout.flush()
            raise SystemExit(0)
        else:
            sys.stdout.write('{{"request_id":"x","protocol_version":"CLINX_KERNEL_V1","ok":true,"authority":"NON_AUTHORITATIVE","result":{{}}}}\\n')
            sys.stdout.flush()
        """
    )
    return (sys.executable, "-u", "-c", script)


def test_ownership_golden_and_independent_expected_values():
    binary = _binary()
    document = _load("ownership_golden.json")
    for case in document["cases"]:
        payload = copy.deepcopy(document["base"])
        for patch in case["patches"]:
            _patch(payload, patch["path"], patch["value"])
        assert evaluate_ownership_reference(payload) == case["expected"]
        response = run_once(binary, "evaluate_ownership", payload, request_id=f"ownership-{case['name']}")
        assert response["ok"] is True
        assert response["result"] == case["expected"]


@pytest.mark.parametrize(
    ("filename", "trace_name"),
    [
        ("assignment_golden.json", "current_grant_renew_release"),
        ("legacy_events.json", "legacy_expire_recover"),
        ("legacy_events.json", "legacy_orphan_recover"),
        ("legacy_events.json", "legacy_revoke"),
    ],
)
def test_assignment_prefixes_match_python_reference(filename, trace_name):
    binary = _binary()
    events = load_fixture_events(filename, trace_name)
    for length in range(1, len(events) + 1):
        prefix = events[:length]
        expected = replay_assignment_reference(prefix)
        response = run_once(binary, "replay_assignment", {"events": prefix}, request_id=f"{trace_name}-{length}")
        assert response["ok"] is True
        assert response["result"] == expected


def test_negative_fixtures_fail_closed_with_same_stable_error():
    binary = _binary()
    for case in _load("replay_negative.json")["cases"]:
        source = load_fixture_events("assignment_golden.json", "current_grant_renew_release")
        if case["source"] != "current_grant_renew_release":
            source = load_fixture_events("legacy_events.json", case["source"])
        events = [copy.deepcopy(source[index]) for index in case["event_indexes"]]
        for patch in case["patches"]:
            _patch(events, patch["path"], patch["value"])
        if case.get("rehash"):
            import hashlib
            from kernel_lab.traces import canonical_json

            for event in events:
                event["payload_hash"] = hashlib.sha256(canonical_json(event["payload"]).encode()).hexdigest()
        with pytest.raises(KernelReferenceError) as reference_error:
            replay_assignment_reference(events)
        response = run_once(binary, "replay_assignment", {"events": events}, request_id=f"negative-{case['name']}")
        assert response["ok"] is False
        assert reference_error.value.code == case["expected_error"]
        assert response["error"]["code"] == case["expected_error"]


def test_real_sqlite_trace_differential():
    report = run_seed(1808, binary=_binary(), operations=256)
    assert report["operations"]["attempted"] == 256
    assert report["operations"]["assignment_streams"] >= 5
    assert report["ownership"]["decision"] == "ALLOW_CANDIDATE"


def test_actual_binary_protocol_rejects_bad_json_duplicate_and_unknown_fields():
    command = (str(_binary()),)
    invalid = _raw_exchange(command, b"not-json\n")
    assert invalid["ok"] is False and invalid["error"]["code"] == "INVALID_JSON"
    duplicate = _raw_exchange(command, b'{"request_id":"x","request_id":"y","protocol_version":"CLINX_KERNEL_V1","operation":"replay_assignment","payload":{"events":[]}}\n')
    assert duplicate["ok"] is False and duplicate["error"]["code"] == "INVALID_JSON"
    unknown = _raw_exchange(command, b'{"request_id":"x","protocol_version":"CLINX_KERNEL_V1","operation":"replay_assignment","payload":{"events":[]},"extra":1}\n')
    assert unknown["ok"] is False and unknown["error"]["code"] == "INVALID_JSON"


def test_actual_binary_rejects_version_unknown_operation_and_integer_bounds():
    binary = _binary()
    with KernelSession(binary, max_requests=4) as session:
        response = session.send_envelope({"request_id": "version", "protocol_version": "CLINX_KERNEL_V0", "operation": "replay_assignment", "payload": {"events": []}})
        assert response["error"]["code"] == "UNSUPPORTED_PROTOCOL"
        response = session.request("unsupported", {}, request_id="operation")
        assert response["error"]["code"] == "UNKNOWN_OPERATION"
        response = session.request("replay_assignment", {"events": [{"event_id": "x", "stream_type": "assignment", "stream_id": "a", "sequence": 9007199254740992, "event_family": "RUNTIME_WORKER_V1", "event_type": "AssignmentGranted", "schema_version": 1, "occurred_at": "2026-09-11T12:00:00.000000+00:00", "recorded_at": "2026-09-11T12:00:00.000000+00:00", "payload": {}, "payload_hash": "0" * 64}]}, request_id="integer")
        assert response["error"]["code"] == "INTEGER_OUT_OF_RANGE"


def test_actual_binary_enforces_frame_bound():
    command = (str(_binary()),)
    response = _raw_exchange(command, b"{" + b"x" * MAX_FRAME_BYTES + b"}\n")
    assert response["ok"] is False and response["error"]["code"] == "FRAME_TOO_LARGE"


@pytest.mark.parametrize(
    ("mode", "error_type"),
    [
        ("huge_response", KernelProtocolError),
        ("huge_stderr", KernelProcessError),
        ("truncated", KernelProtocolError),
        ("wrong_id", KernelProtocolError),
        ("timeout", KernelProcessError),
        ("exit_before", KernelProcessError),
    ],
)
def test_bounded_child_failures(mode, error_type):
    session = KernelSession(_fake_process(mode), timeout_seconds=0.05)
    with pytest.raises(error_type):
        session.request("replay_assignment", {"events": []}, request_id="x")
    session.close()


def test_child_exit_after_response_and_restart_replay():
    session = KernelSession(_fake_process("exit_after"), timeout_seconds=0.2, max_requests=2)
    assert session.request("replay_assignment", {"events": []}, request_id="x")["ok"] is True
    with pytest.raises(KernelProcessError):
        session.request("replay_assignment", {"events": []}, request_id="y")
    session.close()
    binary = _binary()
    payload = {"events": []}
    first = run_once(binary, "replay_assignment", payload, request_id="restart-1")
    second = run_once(binary, "replay_assignment", payload, request_id="restart-2")
    assert first["result"] == second["result"]


def test_request_correlation_batch_isolation_and_authority():
    binary = _binary()
    document = _load("ownership_golden.json")
    good = document["base"]
    bad = copy.deepcopy(good)
    _patch(bad, "request.worker_id", "wrong-worker")
    with KernelSession(binary, max_requests=3) as session:
        first = session.request("evaluate_ownership", good, request_id="batch-good")
        second = session.request("evaluate_ownership", bad, request_id="batch-bad")
        third = session.request("replay_assignment", {"events": []}, request_id="batch-empty")
    assert first["request_id"] == "batch-good" and first["result"]["authority"] == AUTHORITY
    assert second["request_id"] == "batch-bad" and second["result"]["decision"] == "REJECT_CANDIDATE"
    assert third["request_id"] == "batch-empty" and third["result"]["stream_version"] == 0


def test_protocol_request_bounds_are_enforced_client_side():
    with pytest.raises(KernelProtocolError):
        run_once(_binary(), "replay_assignment", {"events": [], "padding": "x" * MAX_FRAME_BYTES}, request_id="too-large")
    assert PROTOCOL_VERSION == "CLINX_KERNEL_V1"


def test_python_response_depth_bound_is_finite():
    raw = (b"[" * 41) + b"0" + (b"]" * 41)
    with pytest.raises(KernelProtocolError):
        strict_json_loads(raw)
