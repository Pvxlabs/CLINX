#!/usr/bin/env python3
"""Read pinned Bridge evidence without Warmup, RPC, process control or DB writes."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.qualification.embedded_bridge_readiness import collect

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--tunnel-pid', required=True, type=int)
    parser.add_argument('--bridge-pid', required=True, type=int)
    parser.add_argument('--observe-seconds', default=10, type=int)
    args = parser.parse_args()
    result = collect(args.base_url, tunnel_pid=args.tunnel_pid,
                     bridge_pid=args.bridge_pid, observe_seconds=args.observe_seconds)
    print(json.dumps(result, sort_keys=True))
    sys.exit(0 if result['safe_to_recycle'] else 2)
