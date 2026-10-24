"""Source inventories for the Rust release and the installed macOS helper.

The Rust profile contains adapters only. The Monitor profile additionally keeps
its existing configurable Python execution route until that device contract is
retired. Neither profile publishes experiments or qualification runners.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

RUST_MODULES = (
    "app_server",
    "consultations",
    "derived_git_targets",
    "desktop_socket",
    "dispatch_errors",
    "execution_liveness",
    "execution_policy",
    "execution_semantics",
    "host_contract",
    "host_executor",
    "mcp_server",
    "native_history",
    "native_observation_bridge",
    "native_provider",
    "native_writer_probe",
    "network_observation",
    "node_centre_entrypoint",
    "node_protocol",
    "node_runtime",
    "observation_mcp",
    "observation_source",
    "observer_server",
    "production_workflows",
    "runtime_config",
    "runtime_contracts",
    "rust_authority",
    "rust_host",
    "rust_integration",
    "rust_mcp",
    "rust_monitor",
    "rust_node_adapter",
    "rust_observation",
    "rust_reads",
    "rust_stdio",
    "rust_transport",
    "rust_worker_identity",
    "rust_workspace_probe",
    "thread_identity",
    "tool_delivery",
    "workspace_contract",
)
DISCOVERY_MODULES = (
    "__init__",
    "discovery",
    "identity",
    "monitor_helper",
    "observer_bootstrap",
    "pairing",
    "transport",
)
# Still reachable from CanonicalNodeExecutionAdapter's configured local route.
# Keep this explicit; these modules must never enter the Rust release profile.
MONITOR_COMPAT_MODULES = (
    "authority_mapping",
    "authority_transactions",
    "authority_writer",
    "bridge",
    "canonical_authority",
    "completion_runtime",
    "desktop_publication",
    "execution_owner",
    "historical_handoff",
    "m9_integration",
    "native_input_recovery",
    "native_lifecycle",
    "node_execution_adapter",
    "prepared_requests",
    "result_ingestion",
    "task_projection",
    "task_registry",
    "unstarted_recovery",
)
MONITOR_COMPAT_PACKAGES = ("domain", "shadow_ledger", "runtime_control")
# P620 daemon/launcher entrypoints are never launched by the bundled user helper.
SERVER_ONLY_MODULES = {
    "desktop_socket", "node_centre_entrypoint", "rust_observation", "rust_stdio",
    "rust_workspace_probe",
}


def source_paths(source: Path, profile: str) -> list[Path]:
    if profile not in ("rust", "monitor"):
        raise ValueError("UNKNOWN_SOURCE_PROFILE")
    names = set(RUST_MODULES)
    if profile == "monitor":
        names.difference_update(SERVER_ONLY_MODULES)
        names.update(MONITOR_COMPAT_MODULES)
    paths = [source / (name + ".py") for name in sorted(names)]
    paths += [source / "local_discovery" / (name + ".py") for name in DISCOVERY_MODULES]
    if profile == "monitor":
        for package in MONITOR_COMPAT_PACKAGES:
            paths += sorted((source / package).glob("*.py"))
        # CLI qualification is not used by the helper or by runtime stores.
        paths = [p for p in paths if p.name not in ("__main__.py", "qualification.py")]
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise ValueError("RUNTIME_SOURCE_MISSING:" + str(path.relative_to(source)))
    return sorted(paths)


def copy_monitor_sources(source: Path, resources: Path) -> None:
    # Clean only this build's old copied Python modules, never DiscoveryRuntime
    # or resources maintained by the Swift build. This prevents stale files in
    # an incremental app bundle from silently surviving retirement.
    for path in resources.glob("*.py"):
        path.unlink()
    for name in ("Discovery", *MONITOR_COMPAT_PACKAGES, "provider_adapters"):
        target = resources / name
        if target.exists():
            shutil.rmtree(target)
    for path in source_paths(source, "monitor"):
        relative = path.relative_to(source)
        if (
            relative.parts[0] == "local_discovery"
            or relative.name == "execution_semantics.py"
        ):
            relative = Path("Discovery") / relative
        target = resources / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--monitor-resources", type=Path, required=True)
    args = parser.parse_args()
    copy_monitor_sources(Path(__file__).resolve().parent, args.monitor_resources)


if __name__ == "__main__":
    main()
