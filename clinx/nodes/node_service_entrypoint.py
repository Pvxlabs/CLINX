#!/usr/bin/env python3
"""Legacy CLI shim and explicit observation qualification fixture API."""
from __future__ import annotations
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from clinx.nodes.node_centre_entrypoint import daemon_binary


def configured_observation_source(native_home, node_id, config):
    """Load an explicit read-only provider route, independent of execution grants."""
    from clinx.monitor.observation_source import CodexObservationSource
    provider = None
    path = config.get("observation_provider_config")
    native_writer = config.get("observation_native_writer", False)
    if type(native_writer) is not bool or (native_writer and path is not None):
        raise ValueError("Invalid native writer observation route")
    if path is not None:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Invalid observation_provider_config")
        from clinx.contracts.runtime_config import BridgeConfig
        provider = BridgeConfig.load(Path(path).expanduser())
    return CodexObservationSource(native_home, node_id,
        config.get("observation_canonical_db"), config.get("observation_owner_host"),
        provider_config=provider,native_writer_observation=native_writer)



def main() -> int:
    binary = daemon_binary()
    os.execv(binary, [binary, 'helper', *sys.argv[1:]])


if __name__ == '__main__':
    main()
