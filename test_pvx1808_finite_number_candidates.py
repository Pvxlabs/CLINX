"""Review candidates for the existing K1 finite-number response contract."""

from __future__ import annotations

import math
import sys

import pytest

from kernel_lab.protocol import (
    KernelProcessError,
    KernelProtocolError,
    KernelSession,
    strict_json_loads,
)


@pytest.mark.parametrize("token", ["1e309", "-1e9999"])
def test_exponent_overflow_cannot_be_a_successful_response(token):
    raw = (
        '{"request_id":"overflow","protocol_version":"CLINX_KERNEL_V1",'
        '"ok":true,"authority":"NON_AUTHORITATIVE",'
        '"result":{"authority":"NON_AUTHORITATIVE",'
        '"decision":"ALLOW_CANDIDATE","reason_code":"OWNERSHIP_SNAPSHOT_MATCHES",'
        '"details":{"nested":[{"numeric":' + token + '}]}}}\n'
    ).encode("utf-8")
    script = (
        "import sys; sys.stdin.buffer.readline(); "
        f"sys.stdout.buffer.write({raw!r}); sys.stdout.buffer.flush(); "
        "sys.stdin.buffer.read()"
    )
    session = KernelSession(
        (sys.executable, "-S", "-u", "-c", script), timeout_seconds=1.0
    )
    try:
        with pytest.raises(KernelProtocolError):
            session.request("evaluate_ownership", {}, request_id="overflow")
        with pytest.raises(KernelProcessError):
            session.request("evaluate_ownership", {}, request_id="after-invalid")
    finally:
        session.close()


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_constants_are_still_rejected(token):
    with pytest.raises(KernelProtocolError):
        strict_json_loads(("{\"nested\":[" + token + "]}").encode("utf-8"))


@pytest.mark.parametrize("token", ["1e308", "-1e308", "1.5e-12"])
def test_finite_scientific_notation_is_not_blanket_rejected(token):
    parsed = strict_json_loads(("{\"value\":" + token + "}").encode("utf-8"))
    assert math.isfinite(parsed["value"])
    assert parsed["value"] == float(token)
