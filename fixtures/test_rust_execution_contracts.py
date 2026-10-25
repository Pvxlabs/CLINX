"""Python dataclass/tuple bindings only; domain cases are Rust integration tests."""
from clinx.contracts.execution_policy import build_development_policy, build_execution_policy, normalize_operation_scopes, parse_execution_policy
from clinx.contracts.execution_semantics import build_routing_identity, parse_routing_identity, with_conversation_binding


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


def test_python_route_dataclass_roundtrip_and_public_redaction():
    original = route()
    assert parse_routing_identity(original.to_json()) == original
    assert original.executable and not original.network_policy.remote_execution
    assert original.public_dict()["conversation"] == dict(
        binding="REDACTED", status="UNBOUND"
    )
    bound = with_conversation_binding(original, "native-thread")
    assert with_conversation_binding(bound, "native-thread") == bound
    assert parse_routing_identity(bound.to_json()) == bound


def test_python_policy_tuple_and_development_default_bindings():
    assert build_development_policy(network_access=False).execution_surface == "NATIVE_FULL_ACCESS"
    exact = dict(capability="ssh", operation="remote_status", operation_class="production_read_only", target="fixture-host")
    policy = build_execution_policy(required_capabilities=["SSH"], operation_classes=["PRODUCTION_READ_ONLY"], operation_scopes=[exact, exact])
    assert parse_execution_policy(policy.to_json()) == policy
    assert policy.authority_scopes == ("production_read_only", "workspace_write")
    assert len(normalize_operation_scopes([exact, exact])) == 1
