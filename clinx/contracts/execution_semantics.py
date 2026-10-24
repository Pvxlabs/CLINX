"""Canonical, bounded execution location and capability semantics.

The values in this module are identities, not permissions.  In particular,
host and transport describe where/how a provider is reached; authority and
network policy are separate, explicit fields.
"""

from __future__ import annotations

import dataclasses
import hashlib
import ipaddress
import json
import re
from typing import Any


class SemanticsError(ValueError):
    """A requested semantic identity is unknown or unsupported."""


KNOWN_HOST_STATUSES = {"KNOWN", "UNKNOWN"}
KNOWN_COMPONENT_STATUSES = {"SUPPORTED", "UNKNOWN", "UNSUPPORTED"}
KNOWN_AUTHORITY_STATUSES = {"BOUNDED", "UNKNOWN", "UNSUPPORTED"}
KNOWN_CONVERSATION_STATUSES = {"BOUND", "UNBOUND", "UNKNOWN"}


def _text(name: str, value: Any, *, required: bool = True) -> str:
    if not isinstance(value, str):
        if not required and value is None:
            return ""
        raise SemanticsError(f"{name} must be a string")
    result = value.strip()
    if required and not result:
        raise SemanticsError(f"{name} is required")
    return result


def _unknown(value: str) -> bool:
    return value.casefold() in {"", "unknown", "unset", "none", "null", "?", "-"}


@dataclasses.dataclass(frozen=True)
class HostIdentity:
    stable_identifier: str
    display: str
    machine_id: str | None
    alias: str
    status: str = "KNOWN"

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class SurfaceIdentity:
    stable_identifier: str
    display: str
    capability_boundary: str
    status: str = "SUPPORTED"

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ProviderIdentity:
    stable_identifier: str
    display: str
    bounded_task_backend: bool = True
    status: str = "SUPPORTED"

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class TransportIdentity:
    stable_identifier: str
    display: str
    remote: bool
    status: str = "SUPPORTED"

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class WorkspaceIdentity:
    workspace_alias: str
    project_alias: str
    worktree_key: str

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ConversationIdentity:
    binding: str
    status: str = "BOUND"

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class AuthorityIdentity:
    stable_identifier: str
    scopes: tuple[str, ...]
    status: str = "BOUNDED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "stable_identifier": self.stable_identifier,
            "scopes": list(self.scopes),
            "status": self.status,
            "identity_is_authority": False,
        }


@dataclasses.dataclass(frozen=True)
class NetworkPolicyIdentity:
    network_access: bool
    stable_identifier: str = "provider_turn_network_opt_in"
    remote_execution: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "stable_identifier": self.stable_identifier,
            "network_access": self.network_access,
            "mode": "ENABLED" if self.network_access else "DISABLED",
            "remote_execution": self.remote_execution,
        }


@dataclasses.dataclass(frozen=True)
class RoutingIdentity:
    host: HostIdentity
    surface: SurfaceIdentity
    provider: ProviderIdentity
    transport: TransportIdentity
    workspace: WorkspaceIdentity
    conversation: ConversationIdentity
    authority: AuthorityIdentity
    network_policy: NetworkPolicyIdentity
    project_identity: str

    def as_dict(self, *, public: bool = False) -> dict[str, Any]:
        conversation = self.conversation.as_dict()
        if public:
            # Preserve whether the route is bound without exposing the
            # provider thread identifier that proves the binding.
            conversation = {"status": conversation["status"], "binding": "REDACTED"}
        return {
            "host": self.host.as_dict(),
            "surface": self.surface.as_dict(),
            "provider": self.provider.as_dict(),
            "transport": self.transport.as_dict(),
            "workspace": self.workspace.as_dict(),
            "conversation": conversation,
            "authority": self.authority.as_dict(),
            "network_policy": self.network_policy.as_dict(),
            "project_identity": self.project_identity,
            "routing_contract": "execution -> host -> surface -> provider -> transport",
            "separation": {
                "host_is_authority": False,
                "surface_is_authority": False,
                "provider_is_authority": False,
                "transport_is_authority": False,
                "network_is_authority": False,
                "network_is_remote_execution": False,
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))

    def public_dict(self) -> dict[str, Any]:
        return self.as_dict(public=True)

    @property
    def executable(self) -> bool:
        """Whether this route is complete enough to reach the provider."""
        return (
            self.host.status == "KNOWN"
            and self.surface.status == "SUPPORTED"
            and self.provider.status == "SUPPORTED"
            and self.transport.status == "SUPPORTED"
            and self.authority.status == "BOUNDED"
            and not self.network_policy.remote_execution
        )


_SURFACE_ALIASES = {
    "codex_app_server": "codex_app_server",
    "codex-app-server": "codex_app_server",
    "app_server": "codex_app_server",
    "app-server": "codex_app_server",
    "host_executor": "host_executor",
    "host-executor": "host_executor",
}
_TRANSPORT_ALIASES = {
    "local": "local_stdio",
    "stdio": "local_stdio",
    "local_stdio": "local_stdio",
    "ssh": "ssh_stdio",
    "ssh_stdio": "ssh_stdio",
}


def normalize_host(value: Any, *, display: str | None = None, machine_id: str | None = None) -> HostIdentity:
    raw = _text("host", value)
    if _unknown(raw):
        raise SemanticsError("unknown host identity is not executable")
    stable = raw.casefold()
    if stable.startswith("workstation-"):
        stable = stable.removeprefix("workstation-")
    try:
        address = ipaddress.ip_address(stable)
    except ValueError:
        address = None
    if address is not None:
        stable = f"ip:{address.compressed}"
    stable = re.sub(r"[^a-z0-9_.:-]+", "-", stable).strip("-")
    if not stable or _unknown(stable):
        raise SemanticsError("unknown host identity is not executable")
    shown = _text("host display", display, required=False) or raw
    mid = _text("machine_id", machine_id, required=False) or None
    return HostIdentity(stable, shown, mid, stable)


def normalize_surface(value: Any) -> SurfaceIdentity:
    raw = _text("execution surface", value).casefold()
    stable = _SURFACE_ALIASES.get(raw)
    if stable is None:
        raise SemanticsError(f"unsupported execution surface: {value}")
    if stable == "host_executor":
        return SurfaceIdentity(
            stable,
            "Trusted Host Executor",
            "bounded_p620_host_operations",
        )
    return SurfaceIdentity(stable, "Codex app-server", "bounded_codex_turn")


def _surface_provider_compatible(
    surface: SurfaceIdentity, provider: ProviderIdentity
) -> bool:
    return (
        provider.stable_identifier == "codex_app_server"
        and surface.stable_identifier in {"codex_app_server", "host_executor"}
    )


def normalize_provider(value: Any = "codex_app_server") -> ProviderIdentity:
    raw = _text("provider", value).casefold()
    if raw not in {"codex_app_server", "codex-app-server", "codex", "app_server"}:
        raise SemanticsError(f"unsupported provider: {value}")
    return ProviderIdentity("codex_app_server", "Codex app-server")


def normalize_transport(value: Any) -> TransportIdentity:
    raw = _text("transport", value).casefold()
    stable = _TRANSPORT_ALIASES.get(raw)
    if stable is None:
        raise SemanticsError(f"unsupported transport: {value}")
    return TransportIdentity(stable, "local stdio" if stable == "local_stdio" else "SSH stdio", stable == "ssh_stdio")


def normalize_authority(value: Any = "clinx_task_bounded", scopes: tuple[str, ...] | list[str] = ("workspace_write",)) -> AuthorityIdentity:
    raw = _text("authority", value)
    if _unknown(raw):
        raise SemanticsError("authority must be explicit and bounded")
    if raw.casefold() != "clinx_task_bounded":
        raise SemanticsError(f"unsupported authority: {value}")
    clean_scopes = tuple(sorted({_text("authority scope", item).casefold() for item in scopes}))
    if not clean_scopes:
        raise SemanticsError("authority requires at least one bounded scope")
    forbidden = {"shell", "generic_shell", "arbitrary_command", "remote_execute", "host_control"}
    if forbidden.intersection(clean_scopes):
        raise SemanticsError("authority scope is outside the bounded CLINX surface")
    return AuthorityIdentity(raw.casefold(), clean_scopes)


def normalize_network_policy(value: Any = False) -> NetworkPolicyIdentity:
    if not isinstance(value, bool):
        raise SemanticsError("network_access must be a boolean")
    return NetworkPolicyIdentity(value)


def build_routing_identity(
    *,
    host: Any,
    surface: Any = "codex_app_server",
    provider: Any = "codex_app_server",
    transport: Any = "local_stdio",
    workspace_alias: Any,
    project_alias: Any,
    worktree_key: Any,
    project_identity: Any,
    conversation_binding: str = "UNBOUND",
    authority: Any = "clinx_task_bounded",
    authority_scopes: tuple[str, ...] | list[str] = ("workspace_write",),
    network_access: bool = False,
    supported_hosts: set[str] | None = None,
) -> RoutingIdentity:
    host_id = normalize_host(host)
    surface_id = normalize_surface(surface)
    normalized_supported_hosts = None
    if supported_hosts is not None:
        normalized_supported_hosts = set()
        for item in supported_hosts:
            try:
                normalized_supported_hosts.add(normalize_host(item).stable_identifier)
            except SemanticsError:
                continue
    if normalized_supported_hosts is not None and host_id.stable_identifier not in normalized_supported_hosts:
        raise SemanticsError(
            f"unsupported host/surface pair: {host_id.stable_identifier}/{surface_id.stable_identifier}"
        )
    provider_id = normalize_provider(provider)
    transport_id = normalize_transport(transport)
    if not _surface_provider_compatible(surface_id, provider_id):
        raise SemanticsError("execution surface/provider compatibility is unsupported")
    if surface_id.stable_identifier == "host_executor" and transport_id.remote:
        raise SemanticsError("HOST_EXECUTOR requires local provider transport on its host")
    return RoutingIdentity(
        host=host_id,
        surface=surface_id,
        provider=provider_id,
        transport=transport_id,
        workspace=WorkspaceIdentity(
            _text("workspace_alias", workspace_alias),
            _text("project_alias", project_alias),
            _text("worktree_key", worktree_key),
        ),
        conversation=ConversationIdentity(
            _text("conversation binding", conversation_binding, required=False) or "UNBOUND",
            "BOUND" if conversation_binding and conversation_binding != "UNBOUND" else "UNBOUND",
        ),
        authority=normalize_authority(authority, authority_scopes),
        network_policy=normalize_network_policy(network_access),
        project_identity=_text("project_identity", project_identity),
    )


def legacy_routing_identity(
    *, host: Any, workspace_alias: Any, project_alias: Any, cwd: Any,
    project_identity: Any, worktree_key: Any, conversation_bound: bool = False,
) -> RoutingIdentity:
    """Normalize pre-M13 rows without guessing transport or remote target."""
    raw_host = _text("host", host, required=False)
    if _unknown(raw_host):
        host_id = HostIdentity("unknown", "unknown", None, "unknown", "UNKNOWN")
    else:
        host_id = normalize_host(raw_host)
    worktree = _text("worktree_key", worktree_key, required=False)
    if not worktree:
        # The value is only a deterministic legacy marker.  It is never used
        # to claim an executable worktree or infer a remote target.
        worktree = "legacy:" + hashlib.sha256(
            f"{raw_host}\x1f{workspace_alias}\x1f{project_alias}\x1f{cwd}".encode()
        ).hexdigest()[:32]
    return RoutingIdentity(
        host=host_id,
        surface=SurfaceIdentity("unknown", "unknown", "unknown", "UNKNOWN"),
        provider=ProviderIdentity("unknown", "unknown", False, "UNKNOWN"),
        transport=TransportIdentity("unknown", "unknown", False, "UNKNOWN"),
        workspace=WorkspaceIdentity(_text("workspace_alias", workspace_alias), _text("project_alias", project_alias), worktree),
        conversation=ConversationIdentity("BOUND" if conversation_bound else "UNBOUND", "BOUND" if conversation_bound else "UNKNOWN"),
        authority=AuthorityIdentity("unknown", (), "UNKNOWN"),
        network_policy=NetworkPolicyIdentity(False),
        project_identity=_text("project_identity", project_identity),
    )


def with_conversation_binding(
    route: RoutingIdentity, binding: str,
) -> RoutingIdentity:
    """Return the same route with one verified conversation binding."""
    conversation_binding = _text("conversation binding", binding)
    if route.conversation.status == "BOUND":
        if route.conversation.binding != conversation_binding:
            raise SemanticsError("routing identity conversation changed")
        return route
    if route.conversation.status != "UNBOUND":
        raise SemanticsError("routing identity conversation is not bindable")
    return dataclasses.replace(
        route,
        conversation=ConversationIdentity(conversation_binding, "BOUND"),
    )


def parse_routing_identity(value: str | None) -> RoutingIdentity | None:
    if not value or value == "{}":
        return None
    try:
        raw = json.loads(value)
        if not isinstance(raw, dict):
            raise SemanticsError("malformed routing identity")
        required_top = {
            "host", "surface", "provider", "transport", "workspace",
            "conversation", "authority", "network_policy", "project_identity",
        }
        allowed_top = required_top | {"routing_contract", "separation"}
        if set(raw) != allowed_top:
            raise SemanticsError("routing identity has non-canonical top-level keys")
        if raw.get("routing_contract") != "execution -> host -> surface -> provider -> transport":
            raise SemanticsError("invalid routing contract")
        separation = raw.get("separation")
        if not isinstance(separation, dict):
            raise SemanticsError("malformed routing separation")
        expected_separation = {
            "host_is_authority": False,
            "surface_is_authority": False,
            "provider_is_authority": False,
            "transport_is_authority": False,
            "network_is_authority": False,
            "network_is_remote_execution": False,
        }
        if separation != expected_separation:
            raise SemanticsError("invalid routing separation")
        host = raw["host"]
        surface = raw["surface"]
        provider = raw["provider"]
        transport = raw["transport"]
        workspace = raw["workspace"]
        conversation = raw["conversation"]
        authority = raw["authority"]
        network = raw["network_policy"]
        if not isinstance(host, dict) or not isinstance(surface, dict):
            raise SemanticsError("malformed routing identity")
        if not isinstance(provider, dict) or not isinstance(transport, dict):
            raise SemanticsError("malformed routing identity")
        if not isinstance(workspace, dict) or not isinstance(conversation, dict):
            raise SemanticsError("malformed routing identity")
        if not isinstance(authority, dict) or not isinstance(network, dict):
            raise SemanticsError("malformed routing identity")
        if set(host) != {"stable_identifier", "display", "machine_id", "alias", "status"}:
            raise SemanticsError("non-canonical host identity")
        if set(surface) != {"stable_identifier", "display", "capability_boundary", "status"}:
            raise SemanticsError("non-canonical surface identity")
        if set(provider) != {"stable_identifier", "display", "bounded_task_backend", "status"}:
            raise SemanticsError("non-canonical provider identity")
        if set(transport) != {"stable_identifier", "display", "remote", "status"}:
            raise SemanticsError("non-canonical transport identity")
        if set(workspace) != {"workspace_alias", "project_alias", "worktree_key"}:
            raise SemanticsError("non-canonical workspace identity")
        if set(conversation) != {"binding", "status"}:
            raise SemanticsError("non-canonical conversation identity")
        if set(authority) != {"stable_identifier", "scopes", "status", "identity_is_authority"}:
            raise SemanticsError("non-canonical authority identity")
        if set(network) != {"stable_identifier", "network_access", "mode", "remote_execution"}:
            raise SemanticsError("non-canonical network policy")
        if authority.get("identity_is_authority") is not False:
            raise SemanticsError("identity cannot be authority")
        host_status = str(host.get("status", "KNOWN")).upper()
        if host_status not in KNOWN_HOST_STATUSES:
            raise SemanticsError("invalid host status")
        if host_status == "UNKNOWN" and _unknown(str(host.get("stable_identifier", ""))):
            host_id = HostIdentity("unknown", "unknown", None, "unknown", "UNKNOWN")
        else:
            host_id = normalize_host(
                host.get("stable_identifier"),
                display=host.get("display"),
                machine_id=host.get("machine_id"),
            )
            if host_status != "KNOWN":
                raise SemanticsError("known host identity must have status KNOWN")
            if host.get("alias", host_id.alias) != host_id.alias:
                raise SemanticsError("host alias is not deterministic")
        def strict_bool(name: str, item: Any, default: bool = False) -> bool:
            if item is None:
                return default
            if not isinstance(item, bool):
                raise SemanticsError(f"{name} must be a boolean")
            return item
        surface_status = str(surface.get("status", "UNKNOWN")).upper()
        provider_status = str(provider.get("status", "UNKNOWN")).upper()
        transport_status = str(transport.get("status", "UNKNOWN")).upper()
        authority_status = str(authority.get("status", "UNKNOWN")).upper()
        if surface_status not in KNOWN_COMPONENT_STATUSES or provider_status not in KNOWN_COMPONENT_STATUSES or transport_status not in KNOWN_COMPONENT_STATUSES:
            raise SemanticsError("invalid routing component status")
        if authority_status not in KNOWN_AUTHORITY_STATUSES:
            raise SemanticsError("invalid authority status")
        network_remote = strict_bool("remote_execution", network.get("remote_execution"))
        if network_remote:
            raise SemanticsError("remote_execution must remain false")
        surface_id = SurfaceIdentity("unknown", "unknown", "unknown", "UNKNOWN") if surface_status == "UNKNOWN" else (
            SurfaceIdentity(surface.get("stable_identifier", ""), surface.get("display", ""), surface.get("capability_boundary", ""), surface_status)
            if surface_status == "UNSUPPORTED" else normalize_surface(surface.get("stable_identifier"))
        )
        provider_id = ProviderIdentity("unknown", "unknown", False, "UNKNOWN") if provider_status == "UNKNOWN" else (
            ProviderIdentity(str(provider.get("stable_identifier")), str(provider.get("display")), False, "UNSUPPORTED")
            if provider_status == "UNSUPPORTED" else normalize_provider(provider.get("stable_identifier"))
        )
        transport_id = TransportIdentity("unknown", "unknown", False, "UNKNOWN") if transport_status == "UNKNOWN" else (
            TransportIdentity(str(transport.get("stable_identifier")), str(transport.get("display")), bool(transport.get("remote")), "UNSUPPORTED")
            if transport_status == "UNSUPPORTED" else normalize_transport(transport.get("stable_identifier"))
        )
        if surface_status != "UNKNOWN" and not _surface_provider_compatible(surface_id, provider_id):
            raise SemanticsError("execution surface/provider compatibility is unsupported")
        if surface_id.stable_identifier == "host_executor" and transport_id.remote:
            raise SemanticsError("HOST_EXECUTOR requires local provider transport on its host")
        scopes = authority.get("scopes", ())
        if not isinstance(scopes, (list, tuple)):
            raise SemanticsError("authority scopes must be an array")
        if authority_status == "UNKNOWN":
            authority_id = AuthorityIdentity("unknown", (), "UNKNOWN")
        else:
            authority_id = normalize_authority(authority.get("stable_identifier"), scopes)
        conversation_binding = _text("conversation binding", conversation.get("binding", "UNBOUND"), required=False) or "UNBOUND"
        conversation_status = str(conversation.get("status", "UNKNOWN")).upper()
        if conversation_status not in KNOWN_CONVERSATION_STATUSES:
            raise SemanticsError("invalid conversation status")
        if conversation_status == "BOUND" and conversation_binding == "UNBOUND":
            raise SemanticsError("BOUND conversation requires a binding")
        if conversation_status == "UNBOUND" and conversation_binding != "UNBOUND":
            raise SemanticsError("UNBOUND conversation cannot have a binding")
        if conversation_status == "UNKNOWN" and conversation_binding not in {"UNBOUND", "UNKNOWN"}:
            raise SemanticsError("UNKNOWN conversation has an unexpected binding")
        network_id = str(network.get("stable_identifier", "provider_turn_network_opt_in"))
        if network_id != "provider_turn_network_opt_in":
            raise SemanticsError("unsupported network policy")
        return RoutingIdentity(
            host_id,
            surface_id,
            provider_id,
            transport_id,
            WorkspaceIdentity(_text("workspace_alias", workspace.get("workspace_alias")), _text("project_alias", workspace.get("project_alias")), _text("worktree_key", workspace.get("worktree_key"))),
            ConversationIdentity(conversation_binding, conversation_status),
            authority_id,
            NetworkPolicyIdentity(strict_bool("network_access", network.get("network_access")), network_id, network_remote),
            _text("project_identity", raw.get("project_identity")),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise SemanticsError("malformed routing identity")
