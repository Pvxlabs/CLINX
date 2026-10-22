#!/usr/bin/env python3
"""Audit one exact historical completion marker without replay or task mutation."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from m9_integration import ExecutionFinalizer
from task_registry import TaskRegistry

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--execution-ref', required=True)
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--expected-sha256')
    args = parser.parse_args()
    if not args.check_only and not args.expected_sha256:
        parser.error('apply requires the exact check-only evidence SHA256')
    registry = TaskRegistry(args.database, initialize=False)
    print(json.dumps(ExecutionFinalizer(registry).reconcile_historical_handoff(
        args.execution_ref, check_only=args.check_only,
        expected_sha256=args.expected_sha256), sort_keys=True))
