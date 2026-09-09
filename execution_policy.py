"""Sealed execution-surface and host-operation policy for CLINX M13-B."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from execution_semantics import RoutingIdentity


class ExecutionPolicyError(ValueError):
    """A requested execution policy is incomplete or exceeds CLINX bounds."""


SANDBOX_WORKSPACE = "SANDBOX_WORKSPACE"
NETWORKED_SANDBOX = "NETWORKED_SANDBOX"
HOST_EXECUTOR = "HOST_EXECUTOR"

EXECUTION_SURFACES = (
    SANDBOX_WORKSPACE,
    NETWORKED_SANDBOX,
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

# A registered local development workspace gets one stable authority envelope.
# Individual commands are still executed with argv/cwd validation by the host
# executor; callers do not need to enumerate every tool used by a project.
DEVELOPMENT_CAPABILITIES = (
    "LOCAL_HOST_PROCESS",
    "SYSTEMD_USER",
    "HOST_FILESYSTEM",
    "GIT",
    "DOCKER",
    "POSTGRES",
)

_SURFACE_ALIASES = {
    "sandbox_workspace": SANDBOX_WORKSPACE,
    "sandbox-workspace": SANDBOX_WORKSPACE,
    "codex_app_server": SANDBOX_WORKSPACE,
    "networked_sandbox": NETWORKED_SANDBOX,
    "networked-sandbox": NETWORKED_SANDBOX,
    "host_executor": HOST_EXECUTOR,
    "host-executor": HOST_EXECUTOR,
}

_AUTHORITY_SCOPES = {
    READ_ONLY_HOST: "read_only_host",
    DEVELOPMENT_MUTATION: "development_mutation",
    PRODUCTION_READ_ONLY: "production_read_only",
    PRODUCTION_MUTATION: "production_mutation",
    BUSINESS_ACTION: "business_action",
}


def _normalize_surface(value: Any, *, network_access: bool) -> str:
    if value is None:
        return NETWORKED_SANDBOX if network_access else SANDBOX_WORKSPACE
    if not isinstance(value, str) or not value.strip():
        raise ExecutionPolicyError("execution_surface must be a non-empty string")
    raw = value.strip()
    normalized = _SURFACE_ALIASES.get(raw.casefold(), raw.upper())
    if normalized not in EXECUTION_SURFACES:
        raise ExecutionPolicyError(f"unsupported execution surface: {value}")
    return normalized


def _normalized_values(
    name: str,
    values: Any,
    allowed: tuple[str, ...],
) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, (list, tuple)):
        raise ExecutionPolicyError(f"{name} must be an array")
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ExecutionPolicyError(f"{name} entries must be non-empty strings")
        item = value.strip().upper()
        if item not in allowed:
            raise ExecutionPolicyError(f"unsupported {name} entry: {value}")
        normalized.add(item)
    return tuple(item for item in allowed if item in normalized)


@dataclasses.dataclass(frozen=True)
class ExecutionPolicy:
    execution_surface: str
    required_capabilities: tuple[str, ...]
    operation_classes: tuple[str, ...]
    production_mutation_intent: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract": "CLINX_EXECUTION_POLICY_V1",
            "execution_surface": self.execution_surface,
            "required_capabilities": list(self.required_capabilities),
            "operation_classes": list(self.operation_classes),
            "production_mutation_intent": self.production_mutation_intent,
            "host_executor_default": False,
            "business_action_authority": False,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))

    @property
    def authority_scopes(self) -> tuple[str, ...]:
        scopes = {"workspace_write"}
        scopes.update(_AUTHORITY_SCOPES[item] for item in self.operation_classes)
        return tuple(sorted(scopes))

    @property
    def route_surface(self) -> str:
        return "host_executor" if self.execution_surface == HOST_EXECUTOR else "codex_app_server"

    def permits(self, operation_class: str, capability: str) -> bool:
        return (
            self.execution_surface == HOST_EXECUTOR
            and operation_class in self.operation_classes
            and capability in self.required_capabilities
            and operation_class != BUSINESS_ACTION
        )

    def validate_route(self, route: RoutingIdentity) -> None:
        if route.surface.stable_identifier != self.route_surface:
            raise ExecutionPolicyError("execution policy surface does not match routing identity")
        expected_network = self.execution_surface == NETWORKED_SANDBOX
        if self.execution_surface != HOST_EXECUTOR and route.network_policy.network_access != expected_network:
            raise ExecutionPolicyError("execution policy network mode does not match routing identity")
        if not set(self.authority_scopes).issubset(set(route.authority.scopes)):
            raise ExecutionPolicyError("execution policy authority is not sealed in routing identity")


def build_execution_policy(
    *,
    execution_surface: Any = None,
    required_capabilities: Any = None,
    operation_classes: Any = None,
    production_mutation_intent: bool = False,
    network_access: bool = False,
    development_workspace: bool = False,
) -> ExecutionPolicy:
    if not isinstance(network_access, bool):
        raise ExecutionPolicyError("network_access must be a boolean")
    if not isinstance(production_mutation_intent, bool):
        raise ExecutionPolicyError("production_mutation_intent must be a boolean")
    capabilities = _normalized_values(
        "required_capabilities", required_capabilities, HOST_CAPABILITIES
    )
    classes = _normalized_values("operation_classes", operation_classes, OPERATION_CLASSES)

    # Explicit host requirements select the existing trusted host executor
    # contract when no surface was supplied.  An explicitly requested sandbox
    # remains invalid for host capabilities and fails closed below.
    selected_surface = execution_surface
    if (
        selected_surface is None
        and development_workspace
        and not network_access
        and required_capabilities is None
        and operation_classes is None
        and not production_mutation_intent
    ):
        selected_surface = HOST_EXECUTOR
        capabilities = DEVELOPMENT_CAPABILITIES
        classes = (DEVELOPMENT_MUTATION,)
    if selected_surface is None and (capabilities or classes or production_mutation_intent):
        selected_surface = HOST_EXECUTOR
    surface = _normalize_surface(selected_surface, network_access=network_access)

    if surface == SANDBOX_WORKSPACE and network_access:
        raise ExecutionPolicyError("SANDBOX_WORKSPACE cannot enable network_access")
    if surface == NETWORKED_SANDBOX and not network_access:
        raise ExecutionPolicyError("NETWORKED_SANDBOX requires network_access=true")
    if surface != HOST_EXECUTOR:
        if capabilities or classes or production_mutation_intent:
            raise ExecutionPolicyError(
                "host capabilities and operation classes require HOST_EXECUTOR"
            )
    else:
        if not capabilities:
            raise ExecutionPolicyError("HOST_EXECUTOR requires explicit capabilities")
        if not classes:
            raise ExecutionPolicyError("HOST_EXECUTOR requires explicit operation classes")
        if BUSINESS_ACTION in classes:
            raise ExecutionPolicyError(
                "AI_DOES_NOT_DECIDE_LIVE_BUSINESS_ACTION: BUSINESS_ACTION is not supported"
            )
        if PRODUCTION_MUTATION in classes and not production_mutation_intent:
            raise ExecutionPolicyError(
                "PRODUCTION_MUTATION requires explicit production_mutation_intent=true"
            )
    return ExecutionPolicy(surface, capabilities, classes, production_mutation_intent)


def build_development_policy() -> ExecutionPolicy:
    """Return the default authority for a registered local dev project."""
    return build_execution_policy(development_workspace=True)


def parse_execution_policy(value: str | None) -> ExecutionPolicy | None:
    if not value or value == "{}":
        return None
    try:
        raw = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ExecutionPolicyError("malformed execution policy") from exc
    if not isinstance(raw, dict):
        raise ExecutionPolicyError("malformed execution policy")
    expected = {
        "contract",
        "execution_surface",
        "required_capabilities",
        "operation_classes",
        "production_mutation_intent",
        "host_executor_default",
        "business_action_authority",
    }
    if set(raw) != expected:
        raise ExecutionPolicyError("non-canonical execution policy")
    if raw.get("contract") != "CLINX_EXECUTION_POLICY_V1":
        raise ExecutionPolicyError("unsupported execution policy contract")
    if raw.get("host_executor_default") is not False:
        raise ExecutionPolicyError("HOST_EXECUTOR cannot be the default")
    if raw.get("business_action_authority") is not False:
        raise ExecutionPolicyError("business action authority cannot be implicit")
    network_access = raw.get("execution_surface") == NETWORKED_SANDBOX
    return build_execution_policy(
        execution_surface=raw.get("execution_surface"),
        required_capabilities=raw.get("required_capabilities"),
        operation_classes=raw.get("operation_classes"),
        production_mutation_intent=raw.get("production_mutation_intent"),
        network_access=network_access,
    )


def legacy_policy_for_route(route: RoutingIdentity) -> ExecutionPolicy:
    """Map an explicit pre-M13-B Codex route to the least-capable policy."""
    if route.surface.stable_identifier == "host_executor":
        raise ExecutionPolicyError("legacy route cannot prove a host executor policy")
    if route.surface.stable_identifier not in {"codex_app_server", "unknown"}:
        raise ExecutionPolicyError("legacy route has an unsupported execution surface")
    return build_execution_policy(
        execution_surface=(
            NETWORKED_SANDBOX
            if route.network_policy.network_access
            else SANDBOX_WORKSPACE
        ),
        network_access=route.network_policy.network_access,
    )
