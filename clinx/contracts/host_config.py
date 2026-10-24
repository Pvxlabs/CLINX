"""Host configuration values consumed by remaining Python config readers.

Execution and delivery live in Rust. These types contain no runner or state.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class RegisteredTarget:
    alias: str
    operation_classes: tuple[str, ...]
    dns_name: str | None = None
    url: str | None = None
    commands: tuple[tuple[str, tuple[str, ...]], ...] = ()


@dataclasses.dataclass(frozen=True)
class HostExecutorConfig:
    enabled: bool = False
    host: str = "p620"
    default_timeout_seconds: float = 30.0
    max_timeout_seconds: float = 120.0
    max_output_bytes: int = 65536
    services: tuple[RegisteredTarget, ...] = ()
    ssh_targets: tuple[RegisteredTarget, ...] = ()
    network_targets: tuple[RegisteredTarget, ...] = ()
    local_commands: tuple[RegisteredTarget, ...] = ()
    trusted_workspace_roots: tuple[Path, ...] = ()
    workflows: tuple = ()
