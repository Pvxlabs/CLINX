#!/usr/bin/env python3
"""Offline P4 shadow gate. Existing frozen source and P3 evidence stay unchanged.

Use --output NEW --prior P3_ARTIFACT --receipts SANITIZED_JSON.
--worker is a pure Python NDJSON peer.
No TaskRegistry/Provider/Host imports, production data access, or execution dispatch.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import platform
import select
import shutil
import statistics
import subprocess
import sys
import tarfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from runtime_lab.reference import CONTRACT, PROTOCOL, Reference  # noqa: E402
from scripts import check_execution_core_contract as v  # noqa: E402
from test_rust_runtime_lab import (  # noqa: E402
    ack_row,
    allocation_trace,
    event,
    record,
    req,
    trace,
)


def dump(path, obj):
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def same(a, b):
    # Python equality conflates True/1 and 1/1.0; compare complete JSON semantics.
    return v.canonical(a) == v.canonical(b)


def worker():
    reference = Reference()
    for line in sys.stdin:
        r = json.loads(line)
        print(
            v.canonical(
                dict(
                    protocol_version=PROTOCOL,
                    contract_family=v.FAMILY,
                    contract_version="1.0.0",
                    authority="NON_AUTHORITATIVE",
                    request_id=r["request_id"],
                    result=reference.execute(r),
                )
            ),
            flush=True,
        )


def sandbox(output, command):
    return [
        "bwrap",
        "--unshare-net",
        "--die-with-parent",
        "--ro-bind",
        "/",
        "/",
        "--tmpfs",
        "/home/pvxlabs/.local/state",
        "--tmpfs",
        "/run",
        "--tmpfs",
        "/tmp",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--bind",
        str(output),
        str(output),
        "--chdir",
        str(output),
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "PYTHONDONTWRITEBYTECODE",
        "1",
        "--",
        *map(str, command),
    ]


class Peer:
    def __init__(self, output, command):
        self.executable = Path(command[0]).resolve()
        self.command = sandbox(output, command)
        self.started = time.perf_counter_ns()
        self.p = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.buffer = b""
        self.latencies = []

    def call(self, request):
        wire = (v.canonical(request) + "\n").encode()
        start = time.perf_counter_ns()
        self.p.stdin.write(wire)
        while b"\n" not in self.buffer:
            if not select.select([self.p.stdout], [], [], 30)[0]:
                self.p.kill()
                raise TimeoutError("offline peer timeout")
            chunk = os.read(self.p.stdout.fileno(), 1024 * 1024)
            if not chunk:
                raise RuntimeError(self.p.stderr.read().decode())
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        response = json.loads(line)
        elapsed = time.perf_counter_ns() - start
        if (
            response["protocol_version"] != PROTOCOL
            or response["contract_family"] != v.FAMILY
            or response["contract_version"] != "1.0.0"
            or response["authority"] != "NON_AUTHORITATIVE"
            or response["request_id"] != request["request_id"]
        ):
            raise RuntimeError("response identity/authority mismatch")
        self.latencies.append(elapsed)
        return response["result"]

    def close(self):
        # wait4 maxrss may include the coordinator's inherited pre-exec RSS.
        # Read the exact runtime process after exec for meaningful memory evidence.
        pending = [self.p.pid]
        memory = []
        while pending:
            pid = pending.pop()
            process_dir = Path(f"/proc/{pid}")
            try:
                pending.extend(
                    int(p)
                    for p in (process_dir / f"task/{pid}/children").read_text().split()
                )
                if (process_dir / "exe").resolve() != self.executable:
                    continue
                status_rows = dict(
                    line.split(":", 1)
                    for line in (process_dir / "status").read_text().splitlines()
                )
                memory.append(
                    dict(
                        pid=pid,
                        executable=str(self.executable),
                        vm_rss_kib=int(status_rows["VmRSS"].split()[0]),
                        vm_hwm_kib=int(status_rows["VmHWM"].split()[0]),
                    )
                )
            except (FileNotFoundError, ProcessLookupError):
                continue
        self.p.stdin.close()
        # wait4 records per-process kernel accounting; no cumulative child subtraction.
        _, status, usage = os.wait4(self.p.pid, 0)
        self.p.returncode = os.waitstatus_to_exitcode(status)
        stderr = self.p.stderr.read().decode()
        self.p.stdout.close()
        self.p.stderr.close()
        if self.p.returncode != 0:
            raise RuntimeError(f"peer exit {self.p.returncode}: {stderr}")
        return dict(
            command=self.command,
            wait4_max_rss_kib_including_preexec=usage.ru_maxrss,
            runtime_memory_after_exec=memory,
            user_cpu_s=usage.ru_utime,
            system_cpu_s=usage.ru_stime,
            minor_faults=usage.ru_minflt,
            major_faults=usage.ru_majflt,
            voluntary_switches=usage.ru_nvcsw,
            involuntary_switches=usage.ru_nivcsw,
            process_wall_ns=time.perf_counter_ns() - self.started,
        )


def samples(receipts):
    es, alloc = trace(full=True, final=True), allocation_trace(release=True)
    normal = [copy.deepcopy(es[i]["payload"]) for i in (0, 1, 4, 5)]
    for i, r in enumerate(normal):
        r.update(version=i, next_version=i + 1)
    normal = [event(r, i + 1, full=True) for i, r in enumerate(normal)]
    row, ack = ack_row()
    checkpoint = copy.deepcopy(es[5]["payload"])
    checkpoint["facts"].update(
        finalization_phase="RESULT_COMMITTED", commit_bundle=["result"]
    )
    checkpoint["facts"].pop("observation")
    bad_terminal = copy.deepcopy(es[5])
    bad_terminal["payload"]["facts"]["commit_bundle"].remove("receipt")
    bad_terminal["payload_hash"] = v.digest(bad_terminal["payload"])
    output = []

    def add(name, commands, provenance="P2/P3 fixed deterministic sample"):
        output.append(dict(name=name, provenance=provenance, requests=commands))

    def app(events):
        return [
            req("append", {"events": [e]}, f"event-{i}") for i, e in enumerate(events)
        ]

    add("normal_terminal_then_exact_release", app(normal + alloc))
    add("owner_loss_same_attempt_observation", app(es))
    add(
        "duplicate_request_event_and_conflict",
        [req("append", {"events": [es[0]]}, "first")] * 2
        + [
            req("append", {"events": [es[0]]}, "new-id"),
            req("append", {"events": [es[1]]}, "first"),
        ],
    )
    add(
        "missing_ack_terminal_holds",
        app(es[:5])
        + [
            req("record_delivery", {"row": row}, "delivery"),
            req("append", {"events": [es[5]]}, "missing-ack"),
        ],
    )
    add(
        "late_ack_old_generation_and_duplicates",
        app(es[:5])
        + [
            req("record_delivery", {"row": row}, "delivery"),
            req("late_ack", {"ack": {**ack, "success": False}}, "unknown"),
            req("late_ack", {"ack": {**ack, "generation": 4}}, "wrong-generation"),
            req("append", {"events": [es[5]]}, "pending-terminal"),
            req("late_ack", {"ack": ack}, "exact-late"),
            req("late_ack", {"ack": ack}, "duplicate-late"),
            req("append", {"events": [es[5]]}, "original-terminal"),
        ],
    )
    add(
        "partial_finalizer_result_terminal_release",
        app(es[:5] + alloc[:2])
        + [
            req("checkpoint", {"record": checkpoint}, "result"),
            req("recovery", {"record": checkpoint}, "classify-result"),
            req("append", {"events": [bad_terminal]}, "partial-bundle"),
            req("append", {"events": [es[5]]}, "terminal"),
            req("recovery", {"record": alloc[2]["payload"]}, "classify-release"),
            req("append", {"events": [alloc[2]]}, "release-pending"),
            req("append", {"events": [alloc[3]]}, "release-commit"),
        ],
    )
    add("lease_expiry_quarantine", app(allocation_trace()))
    old = copy.deepcopy(alloc[1])
    for key in ("stored_owner", "presented_owner"):
        old["payload"]["facts"][key]["resource_epoch"] = 1
    old["payload_hash"] = v.digest(old["payload"])
    add("old_epoch_rejected", app([alloc[0], old]))
    for name, transform in [
        ("lease_at_expiry", lambda f: f.update(now=f["expires_at"])),
        ("pending_safety_fence", lambda f: f.update(safety_pending=True)),
        (
            "foreign_attempt",
            lambda f: f["presented_owner"].update(attempt_id="foreign"),
        ),
    ]:
        e = copy.deepcopy(es[1])
        transform(e["payload"]["facts"])
        e["payload_hash"] = v.digest(e["payload"])
        add(name, app([es[0], e]))
    f = CONTRACT["contexts"]
    attempt_records = [
        record(
            "Attempt",
            "a1",
            "PENDING",
            "assign",
            "ASSIGNED",
            "coordinator",
            0,
            f["fenced"],
        ),
        record(
            "Attempt",
            "a1",
            "ASSIGNED",
            "start",
            "PROVIDER_STARTED",
            "worker",
            1,
            {**f["fenced"], **f["not_sent"]},
        ),
        record("Attempt", "a1", "PROVIDER_STARTED", "lost", "LOST", "coordinator", 2),
        record(
            "Attempt",
            "a1",
            "LOST",
            "observe_same",
            "OBSERVING",
            "reconciler",
            3,
            f["bound"],
        ),
        record(
            "Attempt",
            "a1",
            "OBSERVING",
            "finish",
            "TERMINAL",
            "finalizer",
            4,
            {**f["terminal"], **f["delivered"]},
        ),
    ]
    add(
        "attempt_lost_observe_same_no_retry",
        app([event(r, i + 1, full=True) for i, r in enumerate(attempt_records)]),
    )
    for change in ("hash", "gap", "schema", "unknown", "atomic_batch"):
        damaged = copy.deepcopy(es[:2])
        if change == "hash" or change == "atomic_batch":
            damaged[1]["payload_hash"] = "0" * 64
        elif change == "gap":
            damaged[1]["sequence"] += 1
        elif change == "schema":
            damaged[1]["schema_version"] = 2
        else:
            damaged[1]["unknown_control"] = True
        commands = (
            [req("append", {"events": damaged}, "atomic")]
            if change == "atomic_batch"
            else app(damaged)
        )
        add("illegal_" + change, commands)
    # Boundary samples are additional qualification inputs, never changed goldens.
    for value in (1, "true", [], None):
        add(
            "ack_response_sent_type_" + type(value).__name__,
            [
                req(
                    "ack",
                    {"row": {**row, "response_sent": value}, "ack": ack},
                    "typed-ack",
                )
            ],
            "P4 invalid-type boundary mutation of P3 exact ACK fixture",
        )
    for field in ("aggregate_version", "schema_version", "sequence"):
        e = copy.deepcopy(es[0])
        e[field] = 1.0
        add("event_float_" + field, app([e]), "P4 numeric-envelope boundary mutation")
    for case in CONTRACT["cases"]:
        add(
            "golden_" + case["name"],
            [req("evaluate", {"record": v.materialize(CONTRACT, case["record"])})],
        )
    for receipt in receipts:
        add(
            "legacy_receipt_boundary_" + receipt["fault"],
            [req("append", {"events": [receipt["sample"]]}, "legacy-envelope")],
            "existing sanitized protocol receipt; not a V1 Event; negative admission only; semantic migration NOT_COVERED",
        )
    return output


def distribution(values):
    ordered = sorted(values)

    def percentile(p):
        return ordered[math.ceil(len(ordered) * p) - 1] / 1000

    return dict(
        n=len(values),
        median_us=statistics.median(values) / 1000,
        p95_us=percentile(0.95) if len(values) >= 100 else None,
        p99_us=percentile(0.99) if len(values) >= 1000 else None,
        active_time_ops_s=len(values) * 1e9 / sum(values),
        method="empirical nearest rank; p95 requires n>=100, p99 n>=1000; no IID or production capacity claim",
    )


def run_gate(
    output,
    prior,
    receipts_path,
    *,
    approved_reference_sha256=None,
    approved_rust_sources=None,
    candidate_binary=None,
    candidate_binary_sha256=None,
    conformance_only=False,
):
    output.mkdir(parents=True, exist_ok=False)
    logs = output / "logs"
    logs.mkdir()
    scratch = output / "scratch"
    scratch.mkdir()
    binary = output / "bin/clinx-runtime-lab"
    binary.parent.mkdir()
    receipt = json.loads((prior / "receipt.json").read_text())
    manifest = {**receipt["frozen_p2_hashes"], **receipt["source_files"]}
    baseline_manifest = manifest.copy()
    if approved_reference_sha256 is not None:
        # Explicit identity for the separately authorized Python repair.
        manifest["runtime_lab/reference.py"] = approved_reference_sha256
    approved_rust = {}
    if approved_rust_sources is not None:
        approved_rust = json.loads(approved_rust_sources.read_text())
        if set(approved_rust) != {
            "kernel/src/execution_core.rs", "kernel/src/runtime_lab.rs"
        }:
            raise RuntimeError("D002 source approval must pin exactly guard and regression sources")
        manifest.update(approved_rust)
    if bool(approved_rust) != (candidate_binary is not None) or (
        (candidate_binary is None) != (candidate_binary_sha256 is None)
    ):
        raise RuntimeError("D002 source and candidate binary identities must be supplied together")
    drift = {
        f: dict(expected=h, current=sha(ROOT / f))
        for f, h in manifest.items()
        if sha(ROOT / f) != h
    }
    if drift:
        dump(output / "source-drift.json", drift)
        raise RuntimeError("frozen/P3 sources drifted; evidence reuse refused")
    binary_source = candidate_binary or prior / "qualification-final-v3/bin/clinx-runtime-lab"
    binary_expected = candidate_binary_sha256 or receipt["qualified_binary_sha256"]
    shutil.copy2(binary_source, binary)
    if sha(binary) != binary_expected:
        raise RuntimeError("pinned binary identity mismatch")
    reused = {}
    for name in (
        "receipt.json",
        "crash-recovery.json",
        "crash-journals.tar.gz",
        "qualification-final-v3/qualification.json",
        "qualification-final-v3/benchmark.json",
        "qualification-final-v3/differential.log",
        "qualification-final-v3/rust-unit.log",
    ):
        source = prior / name
        target = output / "reused" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        reused[str(source)] = sha(source)
    dump(
        output / "source-identity.json",
        dict(
            source_hashes=manifest,
            baseline_source_hashes=baseline_manifest,
            approved_reference_sha256=approved_reference_sha256,
            approved_rust_sources=approved_rust,
            candidate_binary_source=str(binary_source),
            baseline_binary_sha256=receipt["qualified_binary_sha256"],
            reused_evidence_scope="historical P3; not current candidate crash or performance qualification",
            binary_sha256=sha(binary),
            head=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT)
            .decode()
            .strip(),
            reused=reused,
            python=sys.version,
            platform=platform.platform(),
            cpu=platform.processor(),
            cpu_count=os.cpu_count(),
            loadavg=os.getloadavg(),
            clock=time.get_clock_info("perf_counter")._asdict()
            if hasattr(time.get_clock_info("perf_counter"), "_asdict")
            else str(time.get_clock_info("perf_counter")),
        ),
    )
    receipts = json.loads(receipts_path.read_text())
    shutil.copy2(receipts_path, output / "protocol-receipts.sanitized.json")
    corpus = samples(receipts)
    dump(output / "corpus.json", corpus)
    counter = 0
    resources, comparisons, differences = [], [], []

    def peer(directory, initialize):
        return Peer(
            output,
            [binary, "--lab-dir", directory] + (["--init"] if initialize else []),
        )

    def compare_sequence(commands, *, retain=False, label="minimize"):
        nonlocal counter
        counter += 1
        directory = scratch / f"lab-{counter}"
        reference = Reference()
        p = peer(directory, True)
        rows = []
        for i, r in enumerate(commands):
            expected, actual = reference.execute(r), p.call(r)
            check = req("inspect", {}, f"prefix-inspect-{i}")
            expected_state, actual_state = reference.execute(check), p.call(check)
            rows.append(
                dict(
                    prefix=i + 1,
                    request=r,
                    python=expected,
                    rust=actual,
                    python_snapshot=expected_state,
                    rust_snapshot=actual_state,
                    equal=same(expected, actual) and same(expected_state, actual_state),
                )
            )
        resources.append(dict(label=label, phase="commands", **p.close()))
        # Each prefix is also recovered by a fresh Rust process. The original
        # command sequence is rebuilt in Python, with no new business commands.
        if retain:
            p = peer(directory, False)
            check = req("inspect", {}, "recovered")
            expected, actual = reference.execute(check), p.call(check)
            rows.append(
                dict(
                    prefix=len(commands),
                    recovery=True,
                    python=expected,
                    rust=actual,
                    equal=same(expected, actual),
                )
            )
            # Audit durable command keys and their original complete receipt.
            for rid, (_h, _result) in list(reference.receipts.items()):
                original = next(r for r in commands if r["request_id"] == rid)
                a, b = reference.execute(original), p.call(original)
                rows.append(
                    dict(receipt_request_id=rid, python=a, rust=b, equal=same(a, b))
                )
            resources.append(dict(label=label, phase="reopen-receipt", **p.close()))
            with tarfile.open(output / f"journal-{label}.tar.gz", "w:gz") as archive:
                archive.add(directory, arcname=label)
        shutil.rmtree(directory)
        return rows

    for case in corpus:
        # New process recovery at EVERY prefix, including rejected candidates.
        for length in range(1, len(case["requests"]) + 1):
            rows = compare_sequence(
                case["requests"][:length], retain=True, label=f"{case['name']}-{length}"
            )
            comparisons.append(dict(scenario=case["name"], length=length, rows=rows))
            failed = next((r for r in rows if not r["equal"]), None)
            if failed and not any(d["scenario"] == case["name"] for d in differences):
                minimal = case["requests"][:length]
                # Delete-one fixed point: 1-minimal command sequence, original
                # event contents retained. This is not a claim of byte-minimal JSON.
                changed = True
                while changed and len(minimal) > 1:
                    changed = False
                    for index in range(len(minimal)):
                        candidate = minimal[:index] + minimal[index + 1 :]
                        if any(not x["equal"] for x in compare_sequence(candidate)):
                            minimal = candidate
                            changed = True
                            break
                reproduction = compare_sequence(minimal)
                differences.append(
                    dict(
                        id=f"D{len(differences) + 1:03}",
                        scenario=case["name"],
                        classification="INPUT_TYPE_SEMANTICS"
                        if "type_" in case["name"] or "float_" in case["name"]
                        else "STATE_OR_VERDICT_DIVERGENCE",
                        first=failed,
                        minimal_requests=minimal,
                        reproduction=reproduction,
                        minimization="1-minimal sequence by deletion; fixed event payloads",
                    )
                )
    dump(output / "prefix-comparisons.json", comparisons)
    dump(output / "differences.json", differences)
    for difference in differences:
        directory = output / "differences" / difference["id"]
        directory.mkdir(parents=True)
        (directory / "input.ndjson").write_text(
            "".join(v.canonical(r) + "\n" for r in difference["minimal_requests"])
        )
        dump(directory / "expected-actual.json", difference["reproduction"])
    coverage = {}
    expected_paths = {
        "normal_terminal_then_exact_release": ["APPLY"] * 8,
        "owner_loss_same_attempt_observation": ["APPLY"] * 6,
        "duplicate_request_event_and_conflict": [
            "APPLY",
            "DUPLICATE",
            "DUPLICATE",
            "REJECT",
        ],
        "missing_ack_terminal_holds": ["APPLY"] * 6 + ["HOLD_UNKNOWN"],
        "late_ack_old_generation_and_duplicates": ["APPLY"] * 6
        + ["HOLD_UNKNOWN", "REJECT", "HOLD_UNKNOWN", "APPLY", "DUPLICATE", "APPLY"],
        "partial_finalizer_result_terminal_release": ["APPLY"] * 7
        + ["HOLD_UNKNOWN", "HOLD_UNKNOWN", "REJECT"]
        + ["APPLY"] * 4,
        "lease_expiry_quarantine": ["APPLY"] * 5,
        "old_epoch_rejected": ["APPLY", "REJECT"],
        "lease_at_expiry": ["APPLY", "REJECT"],
        "pending_safety_fence": ["APPLY", "REJECT"],
        "foreign_attempt": ["APPLY", "REJECT"],
        "attempt_lost_observe_same_no_retry": ["APPLY"] * 5,
    }
    for case in CONTRACT["cases"]:
        expected_paths["golden_" + case["name"]] = [case["expected"]]
    for receipt_sample in receipts:
        expected_paths["legacy_receipt_boundary_" + receipt_sample["fault"]] = [
            "REJECT"
        ]
    for name, expected in expected_paths.items():
        comparison = next(c for c in reversed(comparisons) if c["scenario"] == name)
        rows = [r for r in comparison["rows"] if "request" in r]
        py, rs = ([r[k]["verdict"] for r in rows] for k in ("python", "rust"))
        coverage[name] = dict(
            expected=expected,
            python=py,
            rust=rs,
            pass_gate=py == expected and rs == expected,
        )
    dump(output / "coverage.json", coverage)
    dump(output / "resources-conformance.json", resources)
    print(
        f"conformance scenarios={len(corpus)} differences={len(differences)}",
        flush=True,
    )
    if conformance_only:
        dump(output / "commands.json", [r["command"] for r in resources])
        dump(
            output / "summary.json",
            dict(
                status="BLOCKED"
                if differences or not all(c["pass_gate"] for c in coverage.values())
                else "PUBLIC_SNAPSHOT_CONFORMANCE_PASS",
                authority="NON_AUTHORITATIVE",
                contract=v.FAMILY,
                version="1.0.0",
                scenarios=len(corpus),
                candidate_prefix_runs=len(comparisons),
                comparison_rows=sum(len(c["rows"]) for c in comparisons),
                request_occurrences_in_corpus=sum(len(c["requests"]) for c in corpus),
                distinct_full_requests=len(
                    {v.canonical(r) for c in corpus for r in c["requests"]}
                ),
                differences=len(differences),
                coverage_expectation_failures=[
                    name for name, c in coverage.items() if not c["pass_gate"]
                ],
                performance="NOT_RERUN; prior P4 is PRE_FIX_BASELINE only",
                full_ledger="PENDING_INDEPENDENT_JOURNAL_AUDIT",
                P1_retest="NOT_RUN",
                full_suite="NOT_RUN",
            ),
        )
        shutil.rmtree(scratch)
        print(
            v.canonical(json.loads((output / "summary.json").read_text())), flush=True
        )
        return

    # Same supplied record corpus for direct core timings and persistent NDJSON.
    records = [v.materialize(CONTRACT, c["record"]) for c in CONTRACT["cases"]]
    dump(output / "benchmark-records.json", records)
    commands = []
    env = os.environ.copy()
    env["CARGO_TARGET_DIR"] = str(output / "target")
    env["CARGO_BUILD_JOBS"] = "2"
    build = [
        "cargo",
        "build",
        "--release",
        "--locked",
        "--offline",
        "--manifest-path",
        "kernel/Cargo.toml",
        "--lib",
    ]
    commands.append(build)
    result = subprocess.run(
        build, cwd=ROOT, env=env, text=True, capture_output=True, timeout=180
    )
    (logs / "build-core.log").write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError("core probe build failed")
    dependency = output / "target/release/deps"

    def lib(name):
        return next(dependency.glob(f"lib{name}-*.rlib"))

    compile_probe = [
        "rustc",
        "--edition=2024",
        "-C",
        "opt-level=3",
        str(ROOT / "runtime_lab/shadow_probe.rs"),
        "--extern",
        f"clinx_decision_kernel={lib('clinx_decision_kernel')}",
        "--extern",
        f"serde_json={lib('serde_json')}",
        "-L",
        f"dependency={dependency}",
        "-o",
        str(output / "bin/shadow-core-probe"),
    ]
    commands.append(compile_probe)
    result = subprocess.run(
        compile_probe, cwd=ROOT, text=True, capture_output=True, timeout=60
    )
    (logs / "compile-probe.log").write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError("core timing adapter build failed")
    command = sandbox(
        output, [output / "bin/shadow-core-probe", output / "benchmark-records.json"]
    )
    commands.append(command)
    result = subprocess.run(
        command, text=True, capture_output=True, timeout=30, check=True
    )
    rust_core = json.loads(result.stdout)
    dump(output / "raw-rust-core.json", rust_core)
    for i in range(350):
        v.check_transition(CONTRACT, records[i % len(records)])
    python_ns, python_verdicts = [], []
    for i in range(20000):
        r = records[i % len(records)]
        start = time.perf_counter_ns()
        verdict = v.check_transition(CONTRACT, r)
        python_ns.append(time.perf_counter_ns() - start)
        python_verdicts.append(verdict)
    dump(output / "raw-python-core.json", dict(ns=python_ns, verdicts=python_verdicts))
    if python_verdicts != rust_core["verdicts"]:
        raise RuntimeError("core benchmark inputs differ semantically")
    perf, perf_resources = {}, []
    perf["python_core_transition"] = distribution(python_ns)
    perf["rust_core_transition"] = distribution(rust_core["ns"])
    for name in ("python", "rust"):
        directory = scratch / (name + "-protocol")
        p = (
            peer(directory, True)
            if name == "rust"
            else Peer(
                output,
                [
                    sys.executable,
                    ROOT / "scripts/qualify_runtime_shadow.py",
                    "--worker",
                ],
            )
        )
        p.call(req("inspect", {}, "startup"))
        startup = time.perf_counter_ns() - p.started
        for i in range(100):
            p.call(req("evaluate", {"record": records[i % 35]}, f"warm-{i}"))
        loop_start = time.perf_counter_ns()
        for i in range(2000):
            r = req("evaluate", {"record": records[i % 35]}, f"measure-{i}")
            if p.call(r) != {"verdict": python_verdicts[i]}:
                raise RuntimeError("protocol benchmark mismatch")
        loop_ns = time.perf_counter_ns() - loop_start
        measured = p.latencies[-2000:]
        dump(
            output / f"raw-{name}-protocol.json",
            dict(
                ns=measured,
                startup_to_first_inspect_ns=startup,
                measured_loop_ns=loop_ns,
            ),
        )
        perf[name + "_protocol_evaluate_rtt"] = distribution(measured)
        perf[name + "_protocol_evaluate_rtt"]["measured_wall_throughput_ops_s"] = (
            2000e9 / loop_ns
        )
        perf_resources.append(dict(runtime=name, phase="protocol", **p.close()))
    # Durable append workload: 128 increasing-prefix heartbeats per fresh journal,
    # eight measured rounds, one discarded warmup. Python has no equivalent durable
    # journal, so this is a Rust-only measurement with Python correctness oracle.
    heartbeats = []
    for i in range(128):
        r = record(
            "RuntimeWorker",
            "bench-worker",
            "REGISTERED" if i == 0 else "HEALTHY",
            "heartbeat",
            "HEALTHY",
            "registry",
            i,
            dict(
                current_incarnation="bench-incarnation",
                presented_incarnation="bench-incarnation",
            ),
        )
        heartbeats.append(
            req("append", {"events": [event(r, i + 1, full=True)]}, f"hb-{i}")
        )
    dump(output / "benchmark-append-requests.json", heartbeats)
    rounds, recovery, startups = [], [], []
    for round_id in range(9):
        directory = scratch / f"bench-{round_id}"
        p = peer(directory, True)
        p.call(req("inspect", {}, "startup"))
        startups.append(time.perf_counter_ns() - p.started)
        reference = Reference()
        for r in heartbeats:
            expected = reference.execute(r)
            if not same(p.call(r), expected):
                raise RuntimeError("append benchmark correctness mismatch")
        durations = p.latencies[-128:]
        perf_resources.append(
            dict(runtime="rust", phase="durable-append", round=round_id, **p.close())
        )
        rounds.append(
            dict(
                round=round_id,
                warmup=round_id == 0,
                ns=durations,
                journal_bytes=(directory / "events.ndjson").stat().st_size,
            )
        )
        for repeat in range(5):
            p = peer(directory, False)
            out = p.call(req("inspect", {}, "reopen"))
            elapsed = time.perf_counter_ns() - p.started
            if not same(out["snapshot"], reference.snapshot()):
                raise RuntimeError("recovery benchmark state mismatch")
            recovery.append(
                dict(round=round_id, repeat=repeat, ns=elapsed, warmup=round_id == 0)
            )
            perf_resources.append(
                dict(runtime="rust", phase="full-replay", **p.close())
            )
    dump(output / "raw-rust-append.json", rounds)
    dump(output / "raw-rust-recovery.json", recovery)
    measured = [n for row in rounds[1:] for n in row["ns"]]
    perf["rust_durable_append_rtt_depth_1_128"] = distribution(measured)
    perf["rust_reopen_replay_128_events_including_startup"] = distribution(
        [r["ns"] for r in recovery if not r["warmup"]]
    )
    perf["rust_fresh_process_init_first_inspect"] = distribution(startups[1:])
    perf["methodology"] = dict(
        core="direct frozen transition only; pre-parsed same 35 records; 350 warmup, 20000 samples; no serialization/fsync/startup",
        protocol="same 35 evaluate records; one long-lived peer each, 100 warmup, 2000 serialized sequential RTTs; preencoded request, response decode included; startup excluded",
        append="Rust-only fsync per command; includes JSON/pipes/validation/state cloning/full-prefix replay; varying depth 1..128; 8 rounds, 1024 samples; warmup round excluded",
        recovery="40 samples of same 128-event journal; includes process and bwrap startup, full replay and inspect JSON; no p95/p99 at n<100",
        comparability="Python reference has no durable journal: no Python/Rust persistence speedup claimed. No startup subtraction used to infer kernel cost.",
        resource="Linux wait4 per child for CPU, faults and switches, includes process init. wait4 maxrss can inherit coordinator pre-exec RSS; use exact executable /proc VmHWM/VmRSS after exec for runtime memory. Resources include bwrap init; no cumulative child subtraction.",
        limits="local shared host, no CPU pinning; no production capacity/SLO claim; p99 empirical, not confidence bound; causal core/protocol layers timed separately; active_time_ops_s is reciprocal mean sampled latency, not sustained throughput. Protocol measured_wall_throughput includes coordinator loop and request encoding. Append has Python oracle think-time outside RTT and no sustained throughput claim.",
    )
    dump(output / "performance.json", perf)
    dump(output / "resources-performance.json", perf_resources)
    # Syscall audit inside the same sandbox, outside timed runs.
    audit_dir = scratch / "audit-journal"
    audit_command = sandbox(
        output,
        [
            "strace",
            "-f",
            "-o",
            output / "syscalls.log",
            "-e",
            "trace=network,process,openat",
            binary,
            "--lab-dir",
            audit_dir,
            "--init",
        ],
    )
    commands.append(audit_command)
    audited = subprocess.run(
        audit_command,
        input=v.canonical(
            req("append", {"events": trace(full=True, final=True)}, "audit")
        )
        + "\n",
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    dump(output / "audit-response.json", json.loads(audited.stdout))
    dump(output / "commands.json", commands)
    dump(
        output / "summary.json",
        dict(
            status="BLOCKED"
            if differences or not all(c["pass_gate"] for c in coverage.values())
            else "SHADOW_CONFORMANCE_PASS",
            authority="NON_AUTHORITATIVE",
            contract=v.FAMILY,
            version="1.0.0",
            scenarios=len(corpus),
            candidate_prefix_runs=len(comparisons),
            comparison_rows=sum(len(x["rows"]) for x in comparisons),
            request_occurrences_in_corpus=sum(len(x["requests"]) for x in corpus),
            distinct_full_requests=len(
                {v.canonical(r) for x in corpus for r in x["requests"]}
            ),
            differences=len(differences),
            coverage_expectation_failures=[
                name for name, c in coverage.items() if not c["pass_gate"]
            ],
            verdict_counts=Counter(
                r["python"].get("verdict") for c in comparisons for r in c["rows"]
            ),
            production_execution_trace="NOT_COVERED; existing deterministic protocol receipts and fixed V1 samples only",
            prior_crash_evidence="REUSED_BY_EXACT_SOURCE_AND_BINARY_HASH",
            full_suite="NOT_RUN",
            P1_retest="NOT_RUN",
        ),
    )
    # Retain compressed semantic journals, source recovery and raw measurements.
    with tarfile.open(output / "source-snapshot.tar.gz", "w:gz") as archive:
        for f in [
            *manifest,
            "kernel/Cargo.lock",
            "scripts/qualify_runtime_shadow.py",
            "runtime_lab/shadow_probe.rs",
        ]:
            archive.add(ROOT / f, arcname=f)
    shutil.rmtree(output / "target")
    shutil.rmtree(scratch)
    print(v.canonical(json.loads((output / "summary.json").read_text())), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--prior", type=Path)
    parser.add_argument(
        "--approved-reference-sha256",
        help="exact SHA-256 of authorized Python reference repair; no other drift is permitted",
    )
    parser.add_argument(
        "--approved-rust-sources",
        type=Path,
        help="exact SHA-256 manifest for D002 guard and regression sources only",
    )
    parser.add_argument("--candidate-binary", type=Path)
    parser.add_argument("--candidate-binary-sha256")
    parser.add_argument(
        "--conformance-only",
        action="store_true",
        help="run existing corpus/prefix/recovery/receipt comparisons; skip build and performance; full ledger audit remains required",
    )
    parser.add_argument(
        "--receipts",
        type=Path,
        help="allowlisted sanitized protocol receipt input JSON",
    )
    args = parser.parse_args()
    if args.worker:
        worker()
    elif args.output and args.prior and args.receipts:
        run_gate(
            args.output.resolve(),
            args.prior.resolve(),
            args.receipts.resolve(),
            approved_reference_sha256=args.approved_reference_sha256,
            approved_rust_sources=args.approved_rust_sources,
            candidate_binary=args.candidate_binary,
            candidate_binary_sha256=args.candidate_binary_sha256,
            conformance_only=args.conformance_only,
        )
    else:
        parser.error(
            "--output NEW, --prior P3_ARTIFACT and --receipts SANITIZED_JSON are required"
        )


if __name__ == "__main__":
    main()
