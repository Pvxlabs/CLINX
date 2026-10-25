"""Canonical, bounded execution location and capability semantics.

The values in this module are identities, not permissions.  In particular,
host and transport describe where/how a provider is reached; authority and
network policy are separate, explicit fields.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any


class SemanticsError(ValueError):
    """A requested semantic identity is unknown or unsupported."""


KNOWN_HOST_STATUSES = {"KNOWN", "UNKNOWN"}
KNOWN_COMPONENT_STATUSES = {"SUPPORTED", "UNKNOWN", "UNSUPPORTED"}
KNOWN_AUTHORITY_STATUSES = {"BOUNDED", "UNKNOWN", "UNSUPPORTED"}
KNOWN_CONVERSATION_STATUSES = {"BOUND", "UNBOUND", "UNKNOWN"}






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
        return _contract('executable', route=self.as_dict())




def normalize_host(value: Any, *, display: str | None = None, machine_id: str | None = None) -> HostIdentity:
    return HostIdentity(**_contract('host', value=value, display=display, machine_id=machine_id))


def normalize_surface(value: Any) -> SurfaceIdentity:
    return SurfaceIdentity(**_contract('surface', value=value))




def normalize_provider(value: Any = "codex_app_server") -> ProviderIdentity:
    return ProviderIdentity(**_contract('provider', value=value))


def normalize_transport(value: Any) -> TransportIdentity:
    return TransportIdentity(**_contract('transport', value=value))


def normalize_authority(value: Any = "clinx_task_bounded", scopes: tuple[str, ...] | list[str] = ("workspace_write",)) -> AuthorityIdentity:
    return _authority(_contract('authority', value=value, scopes=list(scopes)))


def normalize_network_policy(value: Any = False) -> NetworkPolicyIdentity:
    return _network(_contract('network', value=value))


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
    arguments = dict(locals())
    arguments['supported_hosts'] = list(supported_hosts) if supported_hosts is not None else None
    return _route(_contract('build_route', **arguments))


def legacy_routing_identity(
    *, host: Any, workspace_alias: Any, project_alias: Any, cwd: Any,
    project_identity: Any, worktree_key: Any, conversation_bound: bool = False,
) -> RoutingIdentity:
    arguments = dict(locals())
    arguments['cwd'] = str(cwd)
    return _route(_contract('legacy_route', **arguments))


def with_conversation_binding(
    route: RoutingIdentity, binding: str,
) -> RoutingIdentity:
    return _route(_contract('bind', route=route.as_dict(), binding=binding))


def parse_routing_identity(value: str | None) -> RoutingIdentity | None:
    return _route(_contract('parse_route', value=value))


def _contract(operation, **arguments):
    from clinx.nodes.network_worker import read_command
    from clinx.authority.rust_authority import RustAuthorityError
    try:
        return read_command('execution_contract.' + operation, **arguments)
    except RustAuthorityError as exc:
        raise SemanticsError(str(exc)) from None


def _authority(value):
    return AuthorityIdentity(value['stable_identifier'], tuple(value['scopes']), value['status'])


def _network(value):
    return NetworkPolicyIdentity(value['network_access'], value['stable_identifier'], value['remote_execution'])


def _route(value):
    if value is None:
        return None
    return RoutingIdentity(
        HostIdentity(**value['host']), SurfaceIdentity(**value['surface']),
        ProviderIdentity(**value['provider']), TransportIdentity(**value['transport']),
        WorkspaceIdentity(**value['workspace']), ConversationIdentity(**value['conversation']),
        _authority(value['authority']), _network(value['network_policy']), value['project_identity'])
