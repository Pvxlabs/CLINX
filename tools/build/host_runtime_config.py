"""Compile existing TOML Host configuration into the Rust worker JSON contract.

This build/install tool performs no dispatch, process execution or state writes.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path

from clinx.contracts.runtime_config import BridgeConfig, canonical_host


def compile_config(config: Path) -> dict:
    value = dataclasses.asdict(BridgeConfig.load(config).host_executor)
    value['host'] = canonical_host(value['host'])
    return json.loads(json.dumps(value, default=lambda item: str(item) if isinstance(item, Path) else item))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    value = compile_config(args.config)
    with args.output.open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


if __name__ == '__main__':
    main()
