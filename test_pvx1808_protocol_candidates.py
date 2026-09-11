"""K1 review reproductions for the bounded kernel protocol."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time

import pytest

from kernel_lab.protocol import KernelProcessError, KernelProtocolError, KernelSession
from kernel_lab.protocol import run_once
from kernel_lab.traces import DEFAULT_BINARY


ROOT = Path(__file__).resolve().parent


def _child_script(mode: str, marker: Path | None = None) -> tuple[str, str, str, str, str, str]:
    return (
        sys.executable,
        "-u",
        "-c",
        textwrap.dedent(
            """
            import json
            import pathlib
            import sys
            import time

            mode = sys.argv[1]
            marker = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else None
            if mode == "blocked_write":
                time.sleep(10)
            elif mode == "burst":
                first = json.loads(sys.stdin.buffer.readline())
                if marker is not None and not marker.exists():
                    marker.write_text("first")
                    sys.stdout.write(json.dumps({
                        "request_id": first["request_id"],
                        "protocol_version": "CLINX_KERNEL_V1",
                        "ok": True,
                        "authority": "NON_AUTHORITATIVE",
                        "result": {"event_family": "RUNTIME_WORKER_V1", "event_types": [], "not_covered_fields": [], "state": {"source": "first"}, "states": {}, "stream_version": 0},
                    }) + "\\n")
                    sys.stdout.write(json.dumps({
                        "request_id": "stale-second",
                        "protocol_version": "CLINX_KERNEL_V1",
                        "ok": True,
                        "authority": "NON_AUTHORITATIVE",
                        "result": {"event_family": "RUNTIME_WORKER_V1", "event_types": [], "not_covered_fields": [], "state": {"source": "stale"}, "states": {}, "stream_version": 0},
                    }) + "\\n")
                else:
                    sys.stdout.write(json.dumps({
                        "request_id": first["request_id"],
                        "protocol_version": "CLINX_KERNEL_V1",
                        "ok": True,
                        "authority": "NON_AUTHORITATIVE",
                        "result": {"event_family": "RUNTIME_WORKER_V1", "event_types": [], "not_covered_fields": [], "state": {"source": "fresh"}, "states": {}, "stream_version": 0},
                    }) + "\\n")
                sys.stdout.flush()
            elif mode == "nan":
                sys.stdin.buffer.readline()
                sys.stdout.write(
                    '{"request_id":"nan","protocol_version":"CLINX_KERNEL_V1",'
                    '"ok":true,"authority":"NON_AUTHORITATIVE",'
                    '"result":{"value":NaN}}\\n'
                )
                sys.stdout.flush()
            """
        ),
        mode,
        str(marker) if marker is not None else "",
    )


def test_deadline_covers_blocking_stdin_write_and_cleanup():
    code = textwrap.dedent(
        """
        import sys
        from kernel_lab.protocol import KernelProcessError, KernelSession

        session = KernelSession(tuple(sys.argv[1:]), timeout_seconds=0.05)
        try:
            session.request("replay_assignment", {"events": [], "padding": "x" * 262144}, request_id="blocked")
        except KernelProcessError:
            raise SystemExit(0)
        finally:
            session.close()
        raise SystemExit(1)
        """
    )
    started = time.monotonic()
    try:
        subprocess.run(
            [sys.executable, "-c", code, *_child_script("blocked_write")],
            cwd=ROOT,
            env={"PYTHONPATH": str(ROOT), **os.environ},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=1.0,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"request deadline did not cover stdin write: {exc}")
    assert time.monotonic() - started < 1.0


def test_extra_output_is_rejected_and_restart_discards_old_buffers():
    with tempfile.TemporaryDirectory(prefix="pvx1808-protocol-") as directory:
        marker = Path(directory) / "generation"
        session = KernelSession(_child_script("burst", marker), timeout_seconds=0.2, max_requests=1)
        with pytest.raises(KernelProtocolError):
            session.request("replay_assignment", {"events": []}, request_id="first")
        session.close()
        session.restart()
        assert session.request("replay_assignment", {"events": []}, request_id="second")["result"]["state"] == {"source": "fresh"}
        session.close()


def test_response_validation_rejects_non_finite_json():
    session = KernelSession(_child_script("nan"), timeout_seconds=0.2)
    with pytest.raises(KernelProtocolError):
        session.request("replay_assignment", {"events": []}, request_id="nan")
    session.close()


def test_actual_binary_normal_progress_remains_available():
    response = run_once(DEFAULT_BINARY, "replay_assignment", {"events": []}, request_id="normal")
    assert response["ok"] is True
    assert response["result"]["stream_version"] == 0
