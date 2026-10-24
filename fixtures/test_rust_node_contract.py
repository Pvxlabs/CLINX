"""Node selector and cancellation receipts against the Rust adapter boundary."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from clinx.nodes.node_protocol import NodeProtocolError
from clinx.nodes.rust_node_adapter import RustNodeExecutionAdapter


@pytest.fixture
def node():
    task = SimpleNamespace(host="air", project_alias="project", thread_id="current-thread")
    receipt = dict(execution_ref="exec_exact", task_ref="task_exact", thread_id="historic-thread",
                   native_turn_id="exact-turn", status="RUNNING", resource_state="HELD")
    integration = SimpleNamespace(
        registry=SimpleNamespace(get_task=Mock(return_value=task)),
        get_status=Mock(return_value=receipt),
        cancel_execution=Mock(return_value=dict(receipt, status="CANCELLED", resource_state="RELEASED")),
    )
    return RustNodeExecutionAdapter(integration, "air", allowed_projects=["project"])


@pytest.mark.parametrize("selector", [
    {"host": "another-node"},
    {"project": "another-project"},
    {"thread_id": "another-thread"},
    {"native_turn_id": "another-turn"},
])
def test_cancel_rejects_conflicting_selector_before_mutation(node, selector):
    with pytest.raises(NodeProtocolError):
        node.cancel(execution_ref="exec_exact", approved=True, **selector)
    node.integration.cancel_execution.assert_not_called()


def test_historical_execution_keeps_its_exact_thread_after_successor(node):
    result = node.status(execution_ref="exec_exact", thread_id="historic-thread",
                         native_turn_id="exact-turn", host="air")
    assert result["thread_id"] == "historic-thread"
    assert result["native_turn_id"] == "exact-turn"
    assert result["task_ref"] == "task_exact"


@pytest.mark.parametrize("status,resource,expected", [
    ("CANCELLED", "RELEASED", "CANCELLED"),
    ("CANCELLED", "HELD", "CANCELLATION_PENDING"),
    ("RUNNING", "HELD", "CANCELLATION_PENDING"),
])
def test_cancel_reports_only_the_authoritative_terminal_receipt(node, status, resource, expected):
    node.integration.cancel_execution.return_value.update(status=status, resource_state=resource)
    result = node.cancel(execution_ref="exec_exact", approved=True)
    assert result["operation_state"] == expected
    assert result["status"] == status and result["resource_state"] == resource
    node.integration.cancel_execution.assert_called_once_with(execution_ref="exec_exact", approved=True)


def test_helper_close_has_no_authority_or_provider_lifecycle_side_effect(node):
    node.close()
    node.integration.get_status.assert_not_called()
    node.integration.cancel_execution.assert_not_called()


def test_mcp_factory_never_falls_back_to_a_python_registry(monkeypatch):
    import builtins
    from clinx.mcp import mcp_server
    from clinx.authority import rust_integration

    cfg = SimpleNamespace(runtime_host="air")
    monkeypatch.setattr(mcp_server.BridgeConfig, "load", lambda _: cfg)
    original_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert name not in {"bridge", "task_registry", "m9_integration", "execution_owner"}
        assert not name.startswith('clinx.compat')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    monkeypatch.setenv("CLINX_AUTHORITY", "python")
    with pytest.raises(ValueError, match="RUST_AUTHORITY_REQUIRED"):
        mcp_server.build_server("unused.toml")
    with pytest.raises(ValueError, match="PYTHON_OWNER_RETIRED"):
        mcp_server.build_server("unused.toml", own_executions=True)
    monkeypatch.setenv("CLINX_AUTHORITY", "rust")
    monkeypatch.delenv("CLINX_RUST_OBSERVATION_READS", raising=False)
    api = SimpleNamespace(cfg=cfg, rust_authority=True, execute=Mock())
    monkeypatch.setattr(rust_integration, "RustIntegration", lambda loaded: api)
    assert mcp_server.build_server("unused.toml").integration is api
    api.execute.assert_not_called()
