"""Sealed execution-surface and host-operation policy for CLINX M13-B."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from clinx.contracts.execution_semantics import RoutingIdentity


class ExecutionPolicyError(ValueError):
    """A requested execution policy is incomplete or exceeds CLINX bounds."""


SANDBOX_WORKSPACE = "SANDBOX_WORKSPACE"
NETWORKED_SANDBOX = "NETWORKED_SANDBOX"
NATIVE_FULL_ACCESS = "NATIVE_FULL_ACCESS"
HOST_EXECUTOR = "HOST_EXECUTOR"

EXECUTION_SURFACES = (
    SANDBOX_WORKSPACE,
    NETWORKED_SANDBOX,
    NATIVE_FULL_ACCESS,
    HOST_EXECUTOR,
)

READ_ONLY_HOST = "READ_ONLY_HOST"
DEVELOPMENT_MUTATION = "DEVELOPMENT_MUTATION"
PRODUCTION_READ_ONLY = "PRODUCTION_READ_ONLY"
PRODUCTION_MUTATION = "PRODUCTION_MUTATION"
BUSINESS_ACTION = "BUSINESS_ACTION"

OPERATION_CLASSES = (
    READ_ONLY_HOST,
    DEVELOPMENT_MUTATION,
    PRODUCTION_READ_ONLY,
    PRODUCTION_MUTATION,
    BUSINESS_ACTION,
)

HOST_CAPABILITIES = (
    "LOCAL_HOST_PROCESS",
    "SYSTEMD_USER",
    "OUTBOUND_NETWORK",
    "SSH",
    "HOST_FILESYSTEM",
    "AWS_CLI",
    "CLOUDFLARE_CLI",
    "CLOUD_API",
    "GIT",
    "DOCKER",
    "POSTGRES",
    "TAILSCALE",
)

# Probe labels are informational discovery keys, not request capability names.
# Keep the mapping shared by discovery and preparation validation.
HOST_CAPABILITY_PROBES = {
    "LOCAL_HOST_PROCESS": "host_process",
    "SYSTEMD_USER": "systemd_user",
    "OUTBOUND_NETWORK": "network",
    "SSH": "ssh",
    "HOST_FILESYSTEM": "filesystem",
    "AWS_CLI": "aws_cli",
    "CLOUDFLARE_CLI": "cloudflare_cli",
    "CLOUD_API": "cloud_api",
    "GIT": "git",
    "DOCKER": "docker",
    "POSTGRES": "postgres",
    "TAILSCALE": "tailscale",
}

# Compatibility envelope for explicitly authorized Host development tasks.
# Native workspace development does not request host capabilities.
DEVELOPMENT_CAPABILITIES = (
    "LOCAL_HOST_PROCESS",
    "SYSTEMD_USER",
    "HOST_FILESYSTEM",
    "GIT",
    "DOCKER",
    "POSTGRES",
)




def _normalize_surface(value: Any, *, network_access: bool) -> str:
    return _contract('policy_surface', value=value, network_access=network_access)


def _normalized_values(
    name: str,
    values: Any,
    allowed: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(_contract('normalized', name=name, values=values, allowed=allowed))


@dataclasses.dataclass(frozen=True)
class ExecutionPolicy:
    execution_surface: str
    required_capabilities: tuple[str, ...]
    operation_classes: tuple[str, ...]
    production_mutation_intent: bool = False
    # None preserves historical V1 policies. An explicit tuple is an exact
    # operation/target grant, never a wildcard or a controller parameter.
    operation_scopes: tuple[tuple[str, str, str, str], ...] | None = None

    def as_dict(self) -> dict[str, Any]:
        value = {
            "contract": "CLINX_EXECUTION_POLICY_V1",
            "execution_surface": self.execution_surface,
            "required_capabilities": list(self.required_capabilities),
            "operation_classes": list(self.operation_classes),
            "production_mutation_intent": self.production_mutation_intent,
            "host_executor_default": False,
            "business_action_authority": False,
        }
        if self.operation_scopes is not None:
            value["operation_scopes"] = [dict(zip(
                ("capability", "operation", "operation_class", "target"), scope
            )) for scope in self.operation_scopes]
        return value

    def permits_operation(self, capability: str, operation: str,
                          operation_class: str, target: str = "") -> bool:
        return _contract('permits_operation', policy=self.as_dict(), capability=capability, requested_operation=operation, operation_class=operation_class, target=target)

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))

    def validate_new_task_authority(self) -> None:
        _contract('validate_new', policy=self.as_dict())

    @property
    def authority_scopes(self) -> tuple[str, ...]:
        return tuple(_contract('authority_scopes', policy=self.as_dict()))

    @property
    def route_surface(self) -> str:
        return _contract('route_surface', policy=self.as_dict())

    def permits(self, operation_class: str, capability: str) -> bool:
        return _contract('permits', policy=self.as_dict(), operation_class=operation_class, capability=capability)

    def validate_route(self, route: RoutingIdentity) -> None:
        _contract('validate_route', policy=self.as_dict(), route=route.as_dict())


def build_execution_policy(
    *,
    execution_surface: Any = None,
    required_capabilities: Any = None,
    operation_classes: Any = None,
    production_mutation_intent: bool = False,
    network_access: bool = False,
    development_workspace: bool = False,
    operation_scopes: Any = None,
) -> ExecutionPolicy:
    return _policy(_contract('build_policy', **locals()))


def normalize_operation_scopes(values: Any) -> tuple[tuple[str, str, str, str], ...] | None:
    values = _contract('operation_scopes', value=values)
    return _scopes(values)


def policy_identity(policy_json: str, route_json: str, version: int = 0) -> str:
    json.loads(policy_json)
    json.loads(route_json)
    return _contract('policy_identity', policy_json=policy_json, route_json=route_json, version=version)


def build_development_policy(*, network_access: bool = False) -> ExecutionPolicy:
    return _policy(_contract('build_policy', execution_surface=NATIVE_FULL_ACCESS, network_access=True))


def parse_execution_policy(value: str | None) -> ExecutionPolicy | None:
    return _policy(_contract('parse_policy', value=value))


def legacy_policy_for_route(route: RoutingIdentity) -> ExecutionPolicy:
    return _policy(_contract('legacy_policy', route=route.as_dict()))


def _contract(operation, **arguments):
    from clinx.nodes.network_worker import read_command
    from clinx.authority.rust_authority import RustAuthorityError
    try:
        return read_command('execution_contract.' + operation, **arguments)
    except RustAuthorityError as exc:
        # Preserve the public Python codec's exception types for malformed JSON fields.
        if str(exc) == 'EXECUTION_CONTRACT_UNHASHABLE_LIST':
            return [] in {None}
        if str(exc) == 'EXECUTION_CONTRACT_UNHASHABLE_DICT':
            return {} in {None}
        raise ExecutionPolicyError(str(exc)) from None


def _scopes(values):
    return None if values is None else tuple(tuple(v[k] for k in
        ('capability','operation','operation_class','target')) for v in values)


def _policy(value):
    if value is None:
        return None
    return ExecutionPolicy(value['execution_surface'], tuple(value['required_capabilities']),
        tuple(value['operation_classes']), value['production_mutation_intent'],
        _scopes(value.get('operation_scopes')))
