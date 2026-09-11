"""Small regression tests for benchmark field provenance."""

from __future__ import annotations

from kernel_lab.benchmark import _rust_metric


def test_rust_metric_keeps_returned_elapsed_separate_from_outer_wall():
    samples = {
        "iterations": 2_000,
        "measured": [
            {"elapsed_ns_returned": 11, "outer_wall_ns": 101},
            {"elapsed_ns_returned": 13, "outer_wall_ns": 103},
            {"elapsed_ns_returned": 17, "outer_wall_ns": 107},
            {"elapsed_ns_returned": 19, "outer_wall_ns": 109},
            {"elapsed_ns_returned": 23, "outer_wall_ns": 113},
        ],
    }
    metric = _rust_metric(samples)
    assert metric["iterations"] == 2_000
    assert metric["returned_elapsed_ns"]["median"] == 17
    assert metric["outer_subprocess_wall_ns"]["median"] == 107
    assert metric["returned_elapsed_ns"]["median"] != metric["outer_subprocess_wall_ns"]["median"]
