"""Node callbacks delegate to the existing canonical CLINX authority."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from node_protocol import NodeProtocolError


class CanonicalNodeExecutionAdapter:
    def __init__(self, integration: Any, node_id: str, *, allowed_threads=None, allowed_projects=None, allowed_new_projects=None, allowed_cancel_projects=None):
        self.integration, self.node_id = integration, node_id
        self.allowed_threads, self.allowed_projects = allowed_threads, allowed_projects
        self.allowed_new_projects, self.allowed_cancel_projects = allowed_new_projects, allowed_cancel_projects

    def _target(self, request):
        if request.get('host') not in (None, self.node_id):
            raise NodeProtocolError('NODE_TARGET_MISMATCH', 'Host must match the authenticated node')
        project = request.get('project')
        thread = request.get('thread_id')
        if request.get('task_ref'):
            task = self.integration.registry.get_task(request['task_ref'])
            binding = self.integration.registry.get_binding(task.task_id)
            if task.host != self.node_id:
                raise NodeProtocolError('NODE_TARGET_MISMATCH', 'Task belongs to another node')
            project, thread = task.project_alias, binding.thread_id if binding else None
        if self.allowed_projects is not None and project and project.casefold() not in self.allowed_projects:
            raise NodeProtocolError('EXECUTION_TARGET_DENIED', 'Project is outside the owner approved scope')
        if self.allowed_threads is not None and thread and thread not in self.allowed_threads:
            # New dedicated tasks are allowed only in their explicitly approved project.
            if not request.get('task_ref') or not project or self.allowed_new_projects is None or project.casefold() not in self.allowed_new_projects:
                raise NodeProtocolError('EXECUTION_TARGET_DENIED', 'Native thread is outside the owner approved scope')

    def adopt(self, **request):
        self._target(request)
        fields = inspect.signature(self.integration.adopt_conversation).parameters
        arguments = {key: value for key, value in request.items() if key in fields}
        arguments['host'] = self.node_id
        return dict(self.integration.adopt_conversation(**arguments), node_id=self.node_id)

    def context(self, **request):
        if request.get('task_ref') and not request.get('thread_id') and not request.get('codex_uri'):
            binding = self.integration.registry.get_binding(request['task_ref'])
            if binding is None:
                raise NodeProtocolError('UNKNOWN_TASK', 'Canonical conversation binding is unavailable')
            request = dict(request, thread_id=binding.thread_id, host=self.node_id)
            request.pop('task_ref')
        fields = inspect.signature(self.integration.get_context).parameters
        return dict(self.integration.get_context(**{k: v for k, v in request.items() if k in fields}), node_id=self.node_id)


    @classmethod
    def from_config(cls, path: Path, node_id: str, **scope):
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
        return cls(integration, node_id, **scope)

    def prepare(self, **request: Any) -> dict[str, Any]:
        if request.get("host") not in (None, self.node_id):
            raise NodeProtocolError("NODE_TARGET_MISMATCH", "Execution host must be the authenticated node")
        self._target(request)
        if self.allowed_new_projects is not None and not request.get('task_ref') and request.get('project', '').casefold() not in self.allowed_new_projects:
            raise NodeProtocolError('EXECUTION_TARGET_DENIED', 'Only an approved original thread or new task project may execute')
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
        self._target(dict(task_ref=prepared.task_ref, project=prepared.project, host=prepared.host))
        # Selection observes real active turns and all configured writers before
        # any resume. Canonical dispatch repeats this guard at the write boundary.
        if prepared.status != 'DISPATCHED' and prepared.task_ref and getattr(self.integration.dispatcher, '_uses_default_client_factory', False):
            from native_provider import select_writer_client
            binding = self.integration.registry.get_binding(prepared.task_ref)
            if binding:
                select_writer_client(self.integration.cfg, binding.thread_id)
        result = self.integration.start_execution(prepared_execution_ref=ref, approved=request.get("approved", False))
        start_completion = getattr(self.integration.dispatcher, "start_completion_runtime", None)
        if result.get("execution_started") and callable(start_completion):
            start_completion()
        return dict(result, node_id=self.node_id, operation_state="STARTED")

    def status(self, **request: Any) -> dict[str, Any]:
        return dict(self.integration.get_status(execution_ref=request.get("execution_ref"), task_ref=request.get("task_ref")), node_id=self.node_id)

    def cancel(self, **request: Any) -> dict[str, Any]:
        record = self.integration.registry.get_execution_record(request.get('execution_ref'))
        if record is None:
            raise NodeProtocolError('UNKNOWN_EXECUTION', 'Execution is not owned by this node')
        self._target(dict(task_ref=record['task_id'], host=self.node_id))
        task = self.integration.registry.get_task(record['task_id'])
        if self.allowed_cancel_projects is not None and task.project_alias.casefold() not in self.allowed_cancel_projects:
            raise NodeProtocolError('EXECUTION_TARGET_DENIED', 'Cancellation is outside the owner approved project scope')
        result = self.integration.cancel_execution(execution_ref=request.get("execution_ref"))
        return dict(result, node_id=self.node_id,
                    operation_state="CANCELLED" if result.get("cancel_confirmed") else "CANCELLATION_PENDING")

    def close(self) -> None:
        stop = getattr(self.integration.dispatcher, "stop_completion_runtime", None)
        if callable(stop):
            stop()
