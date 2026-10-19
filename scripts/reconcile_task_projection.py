#!/usr/bin/env python3
"""Exact projection maintenance; no dispatcher, provider start or lease reclaim."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from m9_integration import ExecutionFinalizer
from task_registry import TaskRegistry

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--execution-ref", required=True)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    registry = TaskRegistry(args.database, initialize=False)
    print(
        json.dumps(
            ExecutionFinalizer(registry).reconcile_task_projection(
                args.execution_ref, check_only=args.check_only
            ),
            sort_keys=True,
        )
    )
