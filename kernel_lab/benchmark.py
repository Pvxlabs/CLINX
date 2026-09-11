"""Controlled cost measurements for the PVX-1808 prototype.

Measurements are intentionally kept in separate workloads.  No result in
this module is a production throughput, tail-latency, or migration claim.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import resource
import shutil
import statistics
import subprocess
import time
from typing import Any, Callable

from .protocol import KernelSession, encode_request, run_once, strict_json_loads
from .traces import DEFAULT_BINARY, FIXTURE_DIR, canonical_json, load_fixture_events, materialize_events
from .reference import evaluate_ownership_reference, replay_assignment_reference


ROOT = Path(__file__).resolve().parents[1]
KERNEL_DIR = ROOT / "kernel"


def _percentile(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    if not ordered:
        return float("nan")
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _summary(samples: list[float], unit: str = "ns") -> dict[str, Any]:
    return {
        "unit": unit,
        "samples": len(samples),
        "min": min(samples),
        "median": statistics.median(samples),
        "p95": _percentile(samples, 0.95),
        "max": max(samples),
    }


def _timed(function: Callable[[], Any], rounds: int = 5) -> list[float]:
    function()  # required warmup
    samples: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter_ns()
        function()
        samples.append(float(time.perf_counter_ns() - started))
    return samples


def _load_inputs() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    golden = json.loads((FIXTURE_DIR / "ownership_golden.json").read_text(encoding="utf-8"))
    base = golden["base"]
    events = load_fixture_events("assignment_golden.json", "current_grant_renew_release")
    return {"snapshot": base["snapshot"], "request": base["request"]}, events


def _bench_rust_binary(binary: Path, mode: str, payload: dict[str, Any], iterations: int = 2_000) -> dict[str, Any]:
    bench = binary.with_name("clinx-kernel-bench")
    if not bench.is_file():
        raise FileNotFoundError(f"Rust benchmark binary is required: {bench}")
    completed = subprocess.run(
        [str(bench), mode, str(iterations)],
        input=(canonical_json(payload) + "\n").encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        timeout=30,
    )
    return json.loads(completed.stdout.decode("utf-8"))


def _rss_sample(binary: Path, payload: dict[str, Any]) -> int | None:
    time_command = shutil.which("/usr/bin/time") or shutil.which("time")
    if time_command is None:
        return None
    bench = binary.with_name("clinx-kernel-bench")
    completed = subprocess.run(
        [time_command, "-f", "%M", str(bench), "core", "1"],
        input=(canonical_json(payload) + "\n").encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        timeout=30,
    )
    lines = completed.stderr.decode("utf-8", errors="replace").splitlines()
    try:
        return int(lines[-1])
    except (IndexError, ValueError):
        return None


def run_benchmark(*, binary: Path = DEFAULT_BINARY, rounds: int = 5) -> dict[str, Any]:
    if rounds < 5:
        raise ValueError("benchmark requires one warmup plus at least five measured rounds")
    if not binary.is_file():
        raise FileNotFoundError(f"release binary is required: {binary}")
    bench = binary.with_name("clinx-kernel-bench")
    if not bench.is_file():
        raise FileNotFoundError(f"Rust benchmark binary is required: {bench}")
    ownership, events = _load_inputs()
    replay_payload = {"events": events}

    compile_samples: list[float] = []
    subprocess.run(["cargo", "build", "--release", "--locked"], cwd=KERNEL_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=120)
    for _ in range(rounds):
        started = time.perf_counter_ns()
        subprocess.run(["cargo", "build", "--release", "--locked"], cwd=KERNEL_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=120)
        compile_samples.append(float(time.perf_counter_ns() - started))

    python_eval = _timed(lambda: evaluate_ownership_reference(ownership), rounds)
    python_replay = _timed(lambda: replay_assignment_reference(events), rounds)
    rust_core = _timed(lambda: _bench_rust_binary(binary, "core", ownership)["elapsed_ns"], rounds)
    rust_serde = _timed(lambda: _bench_rust_binary(binary, "serde", ownership)["elapsed_ns"], rounds)

    def round_trip() -> None:
        result = run_once(binary, "evaluate_ownership", ownership, request_id="benchmark-roundtrip")
        if not result["ok"]:
            raise AssertionError(result)

    python_to_rust = _timed(round_trip, rounds)

    def cold_start() -> None:
        run_once(binary, "replay_assignment", replay_payload, request_id="benchmark-cold")

    cold = _timed(cold_start, rounds)

    def hot_batch() -> None:
        with KernelSession(binary, max_requests=32) as session:
            for index in range(16):
                response = session.request("evaluate_ownership", ownership, request_id=f"benchmark-hot-{index}")
                if not response["ok"]:
                    raise AssertionError(response)

    hot = _timed(hot_batch, rounds)

    serialization = _timed(
        lambda: strict_json_loads(encode_request({"request_id": "benchmark", "protocol_version": "CLINX_KERNEL_V1", "operation": "evaluate_ownership", "payload": ownership})[:-1]),
        rounds,
    )
    rss_samples = [_rss_sample(binary, ownership) for _ in range(rounds)]
    rss_values = [value for value in rss_samples if value is not None]
    children_rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss

    return {
        "contract_version": "CLINX_KERNEL_V1",
        "binary": str(binary),
        "environment": {
            "python": subprocess.run(["python3", "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True, text=True).stdout.strip(),
            "rustc": subprocess.run(["rustc", "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True, text=True).stdout.strip(),
            "cargo": subprocess.run(["cargo", "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True, text=True).stdout.strip(),
            "os": __import__("platform").platform(),
            "cpu": __import__("platform").processor() or "UNKNOWN",
        },
        "method": "one warmup followed by five measured rounds; fixed ownership fixture and 16-request hot batch; elapsed wall-clock perf_counter_ns",
        "rounds": rounds,
        "metrics": {
            "compile": _summary(compile_samples),
            "artifact_size": {"unit": "bytes", "kernel_eval": binary.stat().st_size, "kernel_bench": bench.stat().st_size},
            "python_reference_ownership": _summary(python_eval),
            "python_reference_replay": _summary(python_replay),
            "rust_core": _summary(rust_core),
            "rust_serde": _summary(rust_serde),
            "python_to_rust_round_trip": _summary(python_to_rust),
            "cold_start": _summary(cold),
            "hot_batch_16_requests": _summary(hot),
            "python_serialization_and_parsing": _summary(serialization),
            "rss": ({"unit": "KB", "samples": len(rss_values), "median": statistics.median(rss_values), "max": max(rss_values)} if rss_values else {"status": "NOT_MEASURED"}),
            "python_children_ru_maxrss": {"unit": "KB", "value": children_rss},
        },
        "interpretation": "Workloads are reported separately; no speedup, production throughput, P99, capacity, or long-run stability claim is made.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run PVX-1808 controlled cost measurements")
    parser.add_argument("--binary", type=Path, default=DEFAULT_BINARY)
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()
    print(json.dumps(run_benchmark(binary=args.binary, rounds=args.rounds), ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
