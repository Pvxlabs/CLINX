"""Controlled, provenance-preserving cost measurements for PVX-1808.

The measurements are local engineering evidence only.  They do not establish
production throughput, tail latency, capacity, or migration suitability.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import tempfile
import time
from typing import Any, Callable

from .protocol import KernelSession, encode_request, run_once, strict_json_loads
from .traces import DEFAULT_BINARY, FIXTURE_DIR, canonical_json, load_fixture_events
from .reference import evaluate_ownership_reference, replay_assignment_reference


ROOT = Path(__file__).resolve().parents[1]
KERNEL_DIR = ROOT / "kernel"
ROUNDS = 5
RUST_ITERATIONS = 2_000


def _percentile(samples: list[int], percentile: float) -> float:
    ordered = sorted(samples)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _summary(samples: list[int], unit: str = "ns") -> dict[str, Any]:
    if not samples:
        return {"unit": unit, "samples": 0, "status": "NOT_MEASURED"}
    return {
        "unit": unit,
        "samples": len(samples),
        "min": min(samples),
        "median": statistics.median(samples),
        "p95": _percentile(samples, 0.95),
        "max": max(samples),
    }


def _environment(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "python": subprocess.run(
            ["python3", "--version"], check=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
        ).stdout.strip(),
        "rustc": subprocess.run(
            ["rustc", "--version"], check=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
        ).stdout.strip(),
        "cargo": subprocess.run(
            ["cargo", "--version"], check=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
        ).stdout.strip(),
        "os": platform.platform(),
        "cpu": platform.processor() or "UNKNOWN",
        "cwd": str(ROOT),
    }
    if extra:
        result.update(extra)
    return result


def _input_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _time_call(
    label: str,
    function: Callable[[], Any],
    input_hash: str,
    *,
    rounds: int = ROUNDS,
    process_scope: str = "python_parent_process",
) -> dict[str, Any]:
    def sample(warmup: bool) -> dict[str, Any]:
        started = time.perf_counter_ns()
        function()
        ended = time.perf_counter_ns()
        return {
            "label": label,
            "warmup": warmup,
            "perf_counter_start_ns": started,
            "perf_counter_end_ns": ended,
            "elapsed_ns": ended - started,
            "input_sha256": input_hash,
            "command": ["<in-process>", label],
            "environment": _environment(),
            "process_scope": process_scope,
        }

    warmup = sample(True)
    measured = [sample(False) for _ in range(rounds)]
    return {"warmup": warmup, "measured": measured}


def _run_rust_bench(
    binary: Path,
    mode: str,
    payload: dict[str, Any],
    *,
    iterations: int = RUST_ITERATIONS,
) -> dict[str, Any]:
    bench = binary.with_name("clinx-kernel-bench")
    if not bench.is_file():
        raise FileNotFoundError(f"Rust benchmark binary is required: {bench}")
    command = [str(bench), mode, str(iterations)]
    encoded = (canonical_json(payload) + "\n").encode("utf-8")
    started = time.perf_counter_ns()
    completed = subprocess.run(
        command,
        input=encoded,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        timeout=30,
        cwd=ROOT,
    )
    ended = time.perf_counter_ns()
    returned = strict_json_loads(completed.stdout.rstrip(b"\n"))
    if not isinstance(returned, dict):
        raise AssertionError("Rust benchmark did not return an object")
    if returned.get("mode") != mode or returned.get("iterations") != iterations:
        raise AssertionError(f"Rust benchmark provenance mismatch: {returned}")
    elapsed = returned.get("elapsed_ns")
    if isinstance(elapsed, bool) or not isinstance(elapsed, int) or elapsed <= 0:
        raise AssertionError(f"Rust benchmark elapsed_ns is invalid: {returned}")
    return {
        "mode": mode,
        "iterations": iterations,
        "elapsed_ns_returned": elapsed,
        "outer_wall_ns": ended - started,
        "perf_counter_start_ns": started,
        "perf_counter_end_ns": ended,
        "input_sha256": _input_hash(payload),
        "command": command,
        "environment": _environment(),
        "process_scope": "one fresh Rust benchmark subprocess; returned elapsed is in-process loop only",
    }


def _rust_samples(binary: Path, mode: str, payload: dict[str, Any], rounds: int) -> dict[str, Any]:
    warmup = _run_rust_bench(binary, mode, payload)
    measured = [_run_rust_bench(binary, mode, payload) for _ in range(rounds)]
    return {"mode": mode, "iterations": RUST_ITERATIONS, "warmup": warmup, "measured": measured}


def _rust_metric(samples: dict[str, Any]) -> dict[str, Any]:
    measured = samples["measured"]
    return {
        "iterations": samples["iterations"],
        "returned_elapsed_ns": _summary([item["elapsed_ns_returned"] for item in measured]),
        "outer_subprocess_wall_ns": _summary([item["outer_wall_ns"] for item in measured]),
        "meaning": "returned elapsed_ns is the in-process Rust loop; outer wall is a separate subprocess metric",
    }


def _build_sample(command: list[str], environment: dict[str, Any], cwd: Path) -> dict[str, Any]:
    started = time.perf_counter_ns()
    env = os.environ.copy()
    if "CARGO_TARGET_DIR" in environment:
        env["CARGO_TARGET_DIR"] = str(environment["CARGO_TARGET_DIR"])
    subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
    )
    ended = time.perf_counter_ns()
    return {
        "perf_counter_start_ns": started,
        "perf_counter_end_ns": ended,
        "elapsed_ns": ended - started,
        "command": command,
        "environment": _environment(environment),
    }


def _build_measurements(rounds: int) -> dict[str, Any]:
    command = ["cargo", "build", "--release", "--locked"]
    subprocess.run(
        command,
        cwd=KERNEL_DIR,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
    )
    up_to_date = []
    for _ in range(rounds):
        sample = _build_sample(command, {"build_mode": "UP_TO_DATE_BUILD"}, KERNEL_DIR)
        sample["build_status"] = "UP_TO_DATE_BUILD"
        up_to_date.append(sample)
    with tempfile.TemporaryDirectory(prefix="pvx1808-target-") as target_dir:
        clean = _build_sample(
            command,
            {"CARGO_TARGET_DIR": target_dir, "build_mode": "CLEAN_BUILD"},
            KERNEL_DIR,
        )
        clean["build_status"] = "CLEAN_BUILD"
    return {
        "up_to_date_build": {
            "status": "UP_TO_DATE_BUILD",
            "measured": up_to_date,
            "elapsed_ns": _summary([item["elapsed_ns"] for item in up_to_date]),
        },
        "clean_build": {"status": "CLEAN_BUILD", "sample": clean},
        "incremental_build": {"status": "NOT_MEASURED", "samples": []},
    }


def _time_command() -> str | None:
    return "/usr/bin/time" if Path("/usr/bin/time").is_file() else shutil.which("time")


def _rss_sample(
    command: list[str],
    payload: dict[str, Any],
    environment: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    time_command = _time_command()
    if time_command is None:
        return None
    full_command = [time_command, "-f", "%M", *command]
    encoded = (canonical_json(payload) + "\n").encode("utf-8")
    started = time.perf_counter_ns()
    completed = subprocess.run(
        full_command,
        input=encoded,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        timeout=30,
        cwd=ROOT,
        env={**os.environ, **(environment or {})},
    )
    ended = time.perf_counter_ns()
    lines = completed.stderr.decode("utf-8", errors="replace").splitlines()
    try:
        rss_kb = int(lines[-1])
    except (IndexError, ValueError) as exc:
        raise AssertionError(f"/usr/bin/time did not return max RSS: {lines}") from exc
    return {
        "rss_kb": rss_kb,
        "perf_counter_start_ns": started,
        "perf_counter_end_ns": ended,
        "outer_wall_ns": ended - started,
        "input_sha256": _input_hash(payload),
        "command": full_command,
        "environment": _environment(environment),
        "process_scope": "one fresh child process measured by /usr/bin/time %M",
    }


def _rss_samples(binary: Path, payload: dict[str, Any], rounds: int) -> dict[str, Any]:
    rust_command = [str(binary.with_name("clinx-kernel-bench")), "core", "1"]
    python_code = (
        "import json, sys; "
        "from kernel_lab.reference import evaluate_ownership_reference; "
        "evaluate_ownership_reference(json.load(sys.stdin))"
    )
    python_command = ["python3", "-c", python_code]
    rust_warmup = _rss_sample(rust_command, payload)
    python_warmup = _rss_sample(python_command, payload, {"PYTHONPATH": str(ROOT)})
    rust_measured = [_rss_sample(rust_command, payload) for _ in range(rounds)]
    python_measured = [_rss_sample(python_command, payload, {"PYTHONPATH": str(ROOT)}) for _ in range(rounds)]
    if rust_warmup is None or python_warmup is None or any(item is None for item in rust_measured + python_measured):
        return {"status": "NOT_MEASURED", "rust": [], "python": []}
    return {
        "status": "MEASURED",
        "workload": "same ownership JSON input, one evaluator operation, one fresh child process",
        "rust": {"warmup": rust_warmup, "measured": rust_measured},
        "python": {"warmup": python_warmup, "measured": python_measured},
    }


def _load_inputs() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    golden = json.loads((FIXTURE_DIR / "ownership_golden.json").read_text(encoding="utf-8"))
    events = load_fixture_events("assignment_golden.json", "current_grant_renew_release")
    return golden["base"], events


def run_benchmark(*, binary: Path = DEFAULT_BINARY, rounds: int = ROUNDS) -> dict[str, Any]:
    if rounds < 5:
        raise ValueError("benchmark requires one warmup plus at least five measured rounds")
    if not binary.is_file() or not binary.with_name("clinx-kernel-bench").is_file():
        raise FileNotFoundError("release evaluator and benchmark binaries are required")
    ownership, events = _load_inputs()
    replay_payload = {"events": events}
    ownership_hash = _input_hash(ownership)
    replay_hash = _input_hash(replay_payload)

    builds = _build_measurements(rounds)
    python_eval = _time_call("python_reference_ownership", lambda: evaluate_ownership_reference(ownership), ownership_hash, rounds=rounds)
    python_replay = _time_call("python_reference_replay", lambda: replay_assignment_reference(events), replay_hash, rounds=rounds)
    rust_core = _rust_samples(binary, "core", ownership, rounds)
    rust_full = _rust_samples(binary, "parse_evaluate_serialize", ownership, rounds)
    rust_serde = _rust_samples(binary, "serde", ownership, rounds)

    def round_trip() -> None:
        response = run_once(binary, "evaluate_ownership", ownership, request_id="benchmark-roundtrip")
        if not response["ok"]:
            raise AssertionError(response)

    round_trip_samples = _time_call(
        "python_to_rust_round_trip",
        round_trip,
        ownership_hash,
        rounds=rounds,
        process_scope="one fresh Rust evaluator subprocess per call",
    )

    def cold_start() -> None:
        response = run_once(binary, "replay_assignment", replay_payload, request_id="benchmark-cold")
        if not response["ok"]:
            raise AssertionError(response)

    cold_samples = _time_call(
        "cold_process_request",
        cold_start,
        replay_hash,
        rounds=rounds,
        process_scope="one fresh Rust evaluator subprocess per call",
    )

    hot_session = KernelSession(binary, max_requests=rounds * 16 + 1)
    hot_session.start()
    hot_session.request("evaluate_ownership", ownership, request_id="benchmark-hot-warmup")
    hot_warmup = {
        "session_generation": hot_session.generation,
        "requests": 1,
        "process_scope": "one started and warmed KernelSession",
    }
    hot_measured = []
    for round_index in range(rounds):
        started = time.perf_counter_ns()
        for request_index in range(16):
            response = hot_session.request(
                "evaluate_ownership",
                ownership,
                request_id=f"benchmark-hot-{round_index}-{request_index}",
            )
            if not response["ok"]:
                raise AssertionError(response)
        ended = time.perf_counter_ns()
        hot_measured.append({
            "perf_counter_start_ns": started,
            "perf_counter_end_ns": ended,
            "elapsed_ns": ended - started,
            "requests": 16,
            "session_generation": hot_session.generation,
            "input_sha256": ownership_hash,
            "command": [str(binary)],
            "environment": _environment(),
            "process_scope": "one already-started, one-request-warmed KernelSession",
        })
    hot_session.close()
    hot_samples = {"warmup": hot_warmup, "measured": hot_measured}

    serialization = _time_call(
        "python_bounded_encode_decode",
        lambda: strict_json_loads(encode_request({
            "request_id": "benchmark",
            "protocol_version": "CLINX_KERNEL_V1",
            "operation": "evaluate_ownership",
            "payload": ownership,
        })[:-1]),
        ownership_hash,
        rounds=rounds,
    )
    rss = _rss_samples(binary, ownership, rounds)

    def wall_metric(samples: dict[str, Any]) -> dict[str, Any]:
        return _summary([item["elapsed_ns"] for item in samples["measured"]])

    if rss["status"] != "MEASURED":
        rss_metric: dict[str, Any] = {"status": "NOT_MEASURED"}
    else:
        rss_metric = {
            "status": "MEASURED",
            "unit": "KB",
            "workload": rss["workload"],
            "rust": _summary([item["rss_kb"] for item in rss["rust"]["measured"]], "KB"),
            "python": _summary([item["rss_kb"] for item in rss["python"]["measured"]], "KB"),
        }

    return {
        "contract_version": "CLINX_KERNEL_V1",
        "binary": str(binary),
        "benchmark_binary": str(binary.with_name("clinx-kernel-bench")),
        "environment": _environment(),
        "method": "one warmup followed by five measured rounds; fixed input hashes; Rust returned elapsed_ns and iterations are retained separately from outer wall time",
        "rounds": rounds,
        "metrics": {
            "build": builds,
            "python_reference_ownership": wall_metric(python_eval),
            "python_reference_replay": wall_metric(python_replay),
            "rust_core": _rust_metric(rust_core),
            "rust_parse_evaluate_serialize": _rust_metric(rust_full),
            "rust_serde": _rust_metric(rust_serde),
            "python_to_rust_round_trip": wall_metric(round_trip_samples),
            "cold_process_request": wall_metric(cold_samples),
            "hot_batch_16_requests": wall_metric(hot_samples),
            "python_bounded_encode_decode": wall_metric(serialization),
            "rss": rss_metric,
        },
        "raw_samples": {
            "python_reference_ownership": python_eval,
            "python_reference_replay": python_replay,
            "rust_core": rust_core,
            "rust_parse_evaluate_serialize": rust_full,
            "rust_serde": rust_serde,
            "python_to_rust_round_trip": round_trip_samples,
            "cold_process_request": cold_samples,
            "hot_batch_16_requests": hot_samples,
            "python_bounded_encode_decode": serialization,
            "rss": rss,
        },
        "input_hashes": {"ownership": ownership_hash, "replay": replay_hash},
        "interpretation": "Workloads are reported separately. No speedup, production throughput, P99, capacity, multi-machine consistency, or long-run stability claim is made.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run PVX-1808 controlled cost measurements")
    parser.add_argument("--binary", type=Path, default=DEFAULT_BINARY)
    parser.add_argument("--rounds", type=int, default=ROUNDS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_benchmark(binary=args.binary, rounds=args.rounds)
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
