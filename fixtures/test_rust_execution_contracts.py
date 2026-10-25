"""Rust identity/policy compatibility; no task admission or execution side effects."""

import copy
import hashlib
import json

import pytest

from clinx.contracts.execution_policy import (
    ExecutionPolicyError,
    build_development_policy,
    build_execution_policy,
    legacy_policy_for_route,
    normalize_operation_scopes,
    parse_execution_policy,
    policy_identity,
)
from clinx.contracts.execution_semantics import (
    SemanticsError,
    build_routing_identity,
    legacy_routing_identity,
    normalize_host,
    parse_routing_identity,
    with_conversation_binding,
)


def route(**kwargs):
    return build_routing_identity(
        **dict(
            host="WORKSTATION-P620",
            workspace_alias="p620",
            project_alias="线程",
            worktree_key="worktree:fixture",
            project_identity="repo",
            **kwargs,
        )
    )


@pytest.mark.parametrize(
    "raw,stable",
    [
        ("WORKSTATION-P620", "p620"),
        ("Straße", "strasse"),
        ("2001:0db8::1", "ip:2001:db8::1"),
        ("192.0.2.1", "ip:192.0.2.1"),
        ("fe80::1%eth0", "ip:fe80::1-eth0"),
    ],
)
def test_deployed_host_identity_is_preserved(raw, stable):
    host = normalize_host(raw, display="Fixture", machine_id="fingerprint")
    assert host.stable_identifier == stable and host.alias == stable
    assert host.display == "Fixture" and host.machine_id == "fingerprint"


def test_canonical_route_roundtrip_and_one_way_verified_binding():
    original = route()
    assert parse_routing_identity(original.to_json()) == original
    assert original.executable and not original.network_policy.remote_execution
    assert original.public_dict()["conversation"] == dict(
        binding="REDACTED", status="UNBOUND"
    )
    bound = with_conversation_binding(original, "native-thread")
    assert with_conversation_binding(bound, "native-thread") == bound
    assert parse_routing_identity(bound.to_json()) == bound
    with pytest.raises(SemanticsError, match="conversation changed"):
        with_conversation_binding(bound, "another-thread")
    with pytest.raises(SemanticsError, match="local provider transport"):
        route(surface="host_executor", transport="ssh")


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("host", "alias", "spoofed"),
        ("separation", "host_is_authority", True),
        ("authority", "identity_is_authority", True),
        ("network_policy", "remote_execution", True),
        ("network_policy", "network_access", "true"),
        ("conversation", "status", "BOUND"),
        ("provider", "stable_identifier", "unknown-provider"),
    ],
)
def test_mutated_route_cannot_silently_claim_an_executable_identity(
    section, key, value
):
    raw = copy.deepcopy(route().as_dict())
    raw[section][key] = value
    with pytest.raises(SemanticsError, match="malformed routing identity"):
        parse_routing_identity(json.dumps(raw))


def test_legacy_unknown_route_does_not_infer_a_host_or_binding():
    old = legacy_routing_identity(
        host="unknown",
        workspace_alias="p620",
        project_alias="repo",
        cwd="/tmp/fixture",
        project_identity="repo",
        worktree_key=None,
    )
    expected = (
        "legacy:"
        + hashlib.sha256(b"unknown\x1fp620\x1frepo\x1f/tmp/fixture").hexdigest()[:32]
    )
    assert old.workspace.worktree_key == expected and not old.executable
    assert parse_routing_identity(old.to_json()) == old
    with pytest.raises(SemanticsError, match="not bindable"):
        with_conversation_binding(old, "native-thread")
    assert legacy_policy_for_route(old).execution_surface == "SANDBOX_WORKSPACE"


def test_exact_policy_scopes_remain_sealed_and_default_native_is_full_access():
    assert (
        build_development_policy(network_access=False).execution_surface
        == "NATIVE_FULL_ACCESS"
    )
    assert build_execution_policy().execution_surface == "SANDBOX_WORKSPACE"
    exact = dict(
        capability="ssh",
        operation="remote_status",
        operation_class="production_read_only",
        target="fixture-host",
    )
    policy = build_execution_policy(
        required_capabilities=["SSH"],
        operation_classes=["PRODUCTION_READ_ONLY"],
        operation_scopes=[exact, exact],
    )
    policy.validate_new_task_authority()
    assert policy.permits_operation(
        "SSH", "remote_status", "PRODUCTION_READ_ONLY", "fixture-host"
    )
    assert not policy.permits_operation(
        "SSH", "remote_status", "PRODUCTION_READ_ONLY", "another-host"
    )
    assert not policy.permits("PRODUCTION_MUTATION", "SSH")
    assert parse_execution_policy(policy.to_json()) == policy
    identity = route(surface="host_executor", authority_scopes=policy.authority_scopes)
    policy.validate_route(identity)
    assert policy.authority_scopes == ("production_read_only", "workspace_write")
    assert len(normalize_operation_scopes([exact, exact])) == 1
    unscoped = build_execution_policy(
        required_capabilities=["SSH"], operation_classes=["PRODUCTION_READ_ONLY"]
    )
    with pytest.raises(ExecutionPolicyError, match="PRODUCTION_SCOPE_REQUIRED"):
        unscoped.validate_new_task_authority()
    with pytest.raises(ExecutionPolicyError, match="exact identities"):
        normalize_operation_scopes([dict(exact, target="*")])


def test_policy_cas_hash_preserves_deployed_json_unicode_and_generation_bytes():
    policy = build_execution_policy().to_json()
    identity = route(conversation_binding="线程\x7f").to_json()
    wire = json.dumps(
        [json.loads(policy), json.loads(identity), 7],
        sort_keys=True,
        separators=(",", ":"),
    )
    assert (
        policy_identity(policy, identity, 7)
        == hashlib.sha256(wire.encode()).hexdigest()
    )
    assert policy_identity(policy, identity, 7) != policy_identity(policy, identity, 8)


@pytest.mark.parametrize(
    "parameters,error",
    [
        (dict(execution_surface="HOST_EXECUTOR"), "explicit capabilities"),
        (dict(required_capabilities=["SSH"]), "operation classes"),
        (
            dict(required_capabilities=["SSH"], operation_classes=["BUSINESS_ACTION"]),
            "BUSINESS_ACTION",
        ),
        (
            dict(
                required_capabilities=["SSH"], operation_classes=["PRODUCTION_MUTATION"]
            ),
            "production_mutation_intent",
        ),
        (dict(execution_surface="NETWORKED_SANDBOX"), "network_access=true"),
        (dict(network_access="true"), "must be a boolean"),
    ],
)
def test_existing_invalid_policy_semantics_stay_rejected(parameters, error):
    with pytest.raises(ExecutionPolicyError, match=error):
        build_execution_policy(**parameters)
