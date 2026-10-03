"""Node callbacks delegate to the existing canonical CLINX authority."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from node_protocol import NodeProtocolError


class CanonicalNodeExecutionAdapter:
    def __init__(self, integration: Any, node_id: str):
        self.integration, self.node_id = integration, node_id

    @classmethod
    def from_config(cls, path: Path, node_id: str):
        import bridge
        from m9_integration import ClinxIntegration
        from task_registry import TaskRegistry

        cfg = bridge.BridgeConfig.load(path)
        if cfg.runtime_host != node_id or cfg.task_db_path is None:
            raise NodeProtocolError("NODE_EXECUTION_CONFIG_INVALID", "Execution requires the owning host's explicit canonical task database")
        registry = TaskRegistry(cfg.task_db_path)
        dispatcher = bridge.TaskDispatcher(cfg, task_registry=registry)
        reader = bridge.TaskContextReader(cfg, registry)
        integration = ClinxIntegration(cfg, registry, dispatcher, reader, linear=None,
                                       topic_reader=bridge.TopicStatusReader(cfg, registry))
        return cls(integration, node_id)

    def prepare(self, **request: Any) -> dict[str, Any]:
        if request.get("host") not in (None, self.node_id):
            raise NodeProtocolError("NODE_TARGET_MISMATCH", "Execution host must be the authenticated node")
        method = self.integration.prepare_execution
        fields = inspect.signature(method).parameters
        arguments = {key: value for key, value in request.items() if key in fields}
        arguments["host"] = self.node_id
        return dict(method(**arguments), node_id=self.node_id, execution_started=False)

    def start(self, **request: Any) -> dict[str, Any]:
        ref = request.get("prepared_execution_ref")
        prepared = self.integration.registry.verify_prepared_execution(ref)
        if prepared.host != self.node_id:
            raise NodeProtocolError("NODE_TARGET_MISMATCH", "Preparation belongs to another execution host")
        result = self.integration.start_execution(prepared_execution_ref=ref, approved=request.get("approved", False))
        start_completion = getattr(self.integration.dispatcher, "start_completion_runtime", None)
        if result.get("execution_started") and callable(start_completion):
            start_completion()
        return dict(result, node_id=self.node_id, operation_state="STARTED")

    def status(self, **request: Any) -> dict[str, Any]:
        return dict(self.integration.get_status(execution_ref=request.get("execution_ref")), node_id=self.node_id)

    def cancel(self, **request: Any) -> dict[str, Any]:
        result = self.integration.cancel_execution(execution_ref=request.get("execution_ref"))
        return dict(result, node_id=self.node_id,
                    operation_state="CANCELLED" if result.get("cancel_confirmed") else "CANCELLATION_PENDING")

    def close(self) -> None:
        stop = getattr(self.integration.dispatcher, "stop_completion_runtime", None)
        if callable(stop):
            stop()
