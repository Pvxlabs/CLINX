"""Compile existing TOML routes for the Rust MCP executable; build/install only."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from clinx.contracts.execution_semantics import normalize_host
from clinx.contracts.runtime_config import BridgeConfig
from clinx.provider.native_provider import endpoints


def compile_config(config: Path) -> dict:
    cfg = BridgeConfig.load(config)
    routes, desktop = {}, {}
    for workspace in cfg.workspaces:
        canonical = normalize_host(workspace.host or workspace.alias).stable_identifier
        for alias in (workspace.alias, workspace.host, canonical, 'workstation-' + canonical):
            if alias:
                routes.setdefault(alias.casefold(), set()).add(canonical)
        for alias in workspace.codex_host_ids:
            desktop.setdefault(alias, set()).add(canonical)
    # Static MCP host routes only. Dynamic paired node authorization stays in
    # the live observation/Node directory.
    return dict(runtime_host=cfg.runtime_host, provider_endpoint=endpoints(cfg)[0],
                provider_timeout_seconds=cfg.app_server.request_timeout_seconds,
                host_aliases={k: sorted(v) for k, v in routes.items()},
                codex_host_ids={k: sorted(v) for k, v in desktop.items()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with args.output.open('x') as stream:
        json.dump(compile_config(args.config), stream, indent=2)
        stream.write('\n')


if __name__ == '__main__':
    main()
