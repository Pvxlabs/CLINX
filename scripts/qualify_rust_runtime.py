#!/usr/bin/env python3
"""Reproducible, opt-in Stage-3 gates; no live runtime imports or external calls."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.qualification.runtime_lab.reference import CONTRACT, PROTOCOL, Reference, run  # noqa: E402
from scripts import check_execution_core_contract as v  # noqa: E402


def benchmark(binary, output):
    events = []
    for i in range(64):
        r = dict(object="RuntimeWorker", id="bench-worker", version=i, next_version=i + 1,
                 action="heartbeat", actor="registry", to="HEALTHY", facts=dict(
                     current_incarnation="bench-incarnation", presented_incarnation="bench-incarnation"),
                 **{"from": "REGISTERED" if i == 0 else "HEALTHY"})
        events.append(dict(event_id=f"bench-event-{i}", family=v.FAMILY, contract_version="1.0.0",
                           schema_version=1, aggregate_type="RuntimeWorker", aggregate_id="bench-worker",
                           sequence=i + 1, aggregate_version=i + 1, actor="registry",
                           causation_id=f"bench-command-{i}", correlation_id="bench",
                           transaction_id=f"bench-transaction-{i}", idempotency_scope="bench",
                           idempotency_key=str(i), payload=r, payload_hash=v.digest(r),
                           occurred_at="2026-10-22T00:00:00.000000Z",
                           recorded_at="2026-10-22T00:00:01.000000Z"))
    requests = [dict(protocol_version=PROTOCOL, request_id=f"bench-{i}", operation="append",
                     payload={"events": [e]}) for i, e in enumerate(events)]
    reference = Reference()
    expected = [reference.execute(r) for r in requests]
    assert all(r["verdict"] == "APPLY" for r in expected)
    samples = []
    for round_id in range(6):
        with tempfile.TemporaryDirectory(prefix="bench-", dir=output) as parent:
            directory = Path(parent) / "journal"
            start = time.perf_counter_ns()
            process, responses = run(binary, directory, requests, initialize=True)
            append_ns = time.perf_counter_ns() - start
            assert process.returncode == 0 and responses == expected, process.stderr
            inspect = dict(protocol_version=PROTOCOL, request_id="inspect", operation="inspect", payload={})
            start = time.perf_counter_ns()
            process, recovered = run(binary, directory, [inspect])
            recovery_ns = time.perf_counter_ns() - start
            assert process.returncode == 0 and recovered[0]["snapshot"] == expected[-1]["snapshot"]
            samples.append(dict(round=round_id, warmup=round_id == 0, append_batch_ns=append_ns,
                                reopen_replay_ns=recovery_ns,
                                journal_bytes=(directory / "events.ndjson").stat().st_size))
    measured = samples[1:]
    result = dict(kind="LOCAL_RELEASE_SUBPROCESS_WALL", operations_per_round=64,
                  input_sha256=v.digest(requests), binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                  platform=platform.platform(), python=sys.version, samples=samples,
                  median_append_batch_ms=statistics.median(s["append_batch_ns"] for s in measured) / 1e6,
                  median_amortized_append_us=statistics.median(s["append_batch_ns"] for s in measured) / 64e3,
                  median_reopen_replay_ms=statistics.median(s["reopen_replay_ns"] for s in measured) / 1e6,
                  includes="process startup, lab initialization, JSON/stdio, state cloning, validation and per-command sync_all; reopen includes startup and full journal replay",
                  limits="local filesystem only; no isolated core timings, P99, power-loss proof, production throughput or capacity claim")
    (output / "benchmark.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new task-owned artifact directory")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    env["CARGO_TARGET_DIR"] = str(output / "target")
    env["TMPDIR"] = str(output / "temp")
    Path(env["TMPDIR"]).mkdir()
    manifest = ["--manifest-path", "kernel/Cargo.toml"]
    commands = [
        ("contract", [sys.executable, "scripts/check_execution_core_contract.py"]),
        ("fmt", ["cargo", "fmt", *manifest, "--", "--check"]),
        ("rust-unit", ["cargo", "test", "--locked", "--offline", *manifest, "--lib"]),
        ("clippy", ["cargo", "clippy", "--locked", "--offline", *manifest, "--lib", "--bin",
                    "clinx-runtime-lab", "--", "-D", "warnings"]),
        ("build", ["cargo", "build", "--release", "--locked", "--offline", *manifest,
                   "--bin", "clinx-runtime-lab"]),
        ("differential", [sys.executable, "-m", "pytest", "-q", "tests/test_rust_runtime_lab.py",
                          "-o", "cache_dir=" + str(output / "pytest-cache"),
                          "--basetemp=" + str(output / "pytest-tmp")]),
        ("ruff", ["ruff", "check", "--config", "ruff-discovery.toml", "tools/qualification/runtime_lab",
                  "scripts/qualify_rust_runtime.py", "tests/test_rust_runtime_lab.py"]),
        ("diff", ["git", "diff", "--check", "--", "kernel/Cargo.toml", "kernel/src/lib.rs"]),
    ]
    env["CLINX_RUNTIME_LAB_BINARY"] = str(output / "target/release/clinx-runtime-lab")
    for name, command in commands:
        print(name + ": " + " ".join(command), flush=True)
        process = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
        (output / (name + ".log")).write_text(process.stdout + process.stderr)
        if process.returncode:
            print(process.stdout + process.stderr, file=sys.stderr)
            raise SystemExit(process.returncode)
    bindir = output / "bin"
    bindir.mkdir()
    binary = bindir / "clinx-runtime-lab"
    shutil.copy2(env["CLINX_RUNTIME_LAB_BINARY"], binary)
    baseline = benchmark(binary, output)
    hashes = {str(v.CONTRACT.relative_to(ROOT)): hashlib.sha256(v.CONTRACT.read_bytes()).hexdigest(),
              "binary": hashlib.sha256(binary.read_bytes()).hexdigest()}
    (output / "qualification.json").write_text(json.dumps(dict(
        status="PASS", protocol=PROTOCOL, contract=v.FAMILY, version="1.0.0",
        authority="NON_AUTHORITATIVE", golden_cases=len(CONTRACT["cases"]), commands=commands,
        hashes=hashes, full_python_suite="NOT_RUN", live_provider="NOT_RUN",
        production_mutation="NONE", median_append_batch_ms=baseline["median_append_batch_ms"]), indent=2) + "\n")
    # Delete only regenerable outputs created by this invocation. Retain qualified binary and logs.
    for name in ("target", "temp", "pytest-tmp", "pytest-cache"):
        shutil.rmtree(output / name)
    print("PASS " + str(output / "qualification.json"), flush=True)


if __name__ == "__main__":
    main()
