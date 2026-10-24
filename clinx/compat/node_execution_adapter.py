"""Node callbacks delegate to the existing canonical CLINX authority."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from clinx.nodes.node_protocol import NodeProtocolError


class CanonicalNodeExecutionAdapter:
    def __init__(self, integration: Any, node_id: str, *, allowed_threads=None, allowed_projects=None, allowed_new_projects=None, allowed_cancel_projects=None):
        self.integration, self.node_id = integration, node_id
        self.allowed_threads, self.allowed_projects = allowed_threads, allowed_projects
        self.allowed_new_projects, self.allowed_cancel_projects = allowed_new_projects, allowed_cancel_projects

    def _qualify(self, request):
        envelope = request.get('canonical_identity')
        if envelope is None:
            return
        from clinx.provider.native_history import NativeHistory
        from clinx.compat.native_lifecycle import qualify_handoff
        scope = request.get('_canonical_owner')
        if not scope or scope['owner'] != self.node_id:
            raise NodeProtocolError('CANONICAL_HANDOFF_SCOPE_DENIED', 'Authenticated owner scope is required')
        if request.get('task_ref') not in (None, envelope.get('task_id')):
            raise NodeProtocolError('CANONICAL_HANDOFF_IDENTITY_CONFLICT', 'Task selector conflicts with handoff')
        tid = envelope.get('thread_id')
        if request.get('thread_id') not in (None, tid):
            raise NodeProtocolError('CANONICAL_HANDOFF_IDENTITY_CONFLICT', 'Thread selector conflicts with handoff')
        matches = [p for p in self.integration.cfg.projects if str(p.repo) == envelope.get('cwd')]
        if len(matches) != 1:
            raise NodeProtocolError('EXECUTION_TARGET_DENIED', 'Exact native cwd must map to one local project')
        project = matches[0]
        if request.get('project') and request['project'].casefold() not in (project.project_alias.casefold(), project.linear_name.casefold()):
            raise NodeProtocolError('EXECUTION_TARGET_DENIED', 'Project selector conflicts with handoff')
        # Check the original-thread allowlist before importing any identity.
        self._target({'host': self.node_id, 'project': project.project_alias, 'thread_id': tid})
        root = self.integration.cfg.app_server.native_home or Path.home()/'.codex'
        native = NativeHistory(root)
        metadata = native.metadata(tid)
        if not metadata:
            raise NodeProtocolError('NATIVE_THREAD_NOT_FOUND', 'Exact local native index record is required')
        identity = native.identity(tid, metadata)
        qualify_handoff(self.integration.registry, envelope, project=project, identity=identity, **scope)

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
            if not thread:
                from clinx.compat.native_lifecycle import binding as native_binding
                native = native_binding(self.integration.registry.path, node=self.node_id, task=task.task_id)
                thread = native['thread_id'] if native else None
        if self.allowed_projects is not None and project and project.casefold() not in self.allowed_projects:
            raise NodeProtocolError('EXECUTION_TARGET_DENIED', 'Project is outside the owner approved scope')
        if self.allowed_threads is not None and thread and thread not in self.allowed_threads:
            # New dedicated tasks are allowed only in their explicitly approved project.
            if not request.get('task_ref') or not project or self.allowed_new_projects is None or project.casefold() not in self.allowed_new_projects:
                raise NodeProtocolError('EXECUTION_TARGET_DENIED', 'Native thread is outside the owner approved scope')

    def adopt(self, **request):
        self._qualify(request)
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
        from clinx.contracts.runtime_config import BridgeConfig

        cfg = BridgeConfig.load(path)
        from clinx.authority.rust_integration import RustIntegration, enabled
        if enabled():
            from clinx.nodes.rust_node_adapter import RustNodeExecutionAdapter
            if cfg.runtime_host != node_id:
                raise NodeProtocolError('NODE_TARGET_MISMATCH', 'Rust authority host differs')
            return RustNodeExecutionAdapter(RustIntegration(cfg), node_id, **scope)
        from clinx.compat import bridge
        from clinx.compat.m9_integration import ClinxIntegration
        from clinx.compat.task_registry import TaskRegistry

        if cfg.runtime_host != node_id or cfg.task_db_path is None:
            raise NodeProtocolError("NODE_EXECUTION_CONFIG_INVALID", "Execution requires the owning host's explicit canonical task database")
        registry = TaskRegistry(cfg.task_db_path)
        dispatcher = bridge.TaskDispatcher(cfg, task_registry=registry)
        reader = bridge.TaskContextReader(cfg, registry)
        integration = ClinxIntegration(cfg, registry, dispatcher, reader, linear=None,
                                       topic_reader=bridge.TopicStatusReader(cfg, registry))
        if cfg.app_server.native_socket:
            dispatcher.start_completion_runtime()
        return cls(integration, node_id, **scope)

    def prepare(self, **request: Any) -> dict[str, Any]:
        if request.get("host") not in (None, self.node_id):
            raise NodeProtocolError("NODE_TARGET_MISMATCH", "Execution host must be the authenticated node")
        if request.get('approved') is not True:
            raise NodeProtocolError('APPROVAL_REQUIRED', 'Explicit preparation approval is required')
        self._qualify(request)
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
            from clinx.provider.native_provider import select_writer_client
            binding = self.integration.registry.get_binding(prepared.task_ref)
            if binding:
                select_writer_client(self.integration.cfg, binding.thread_id)
        result = self.integration.start_execution(prepared_execution_ref=ref, approved=request.get("approved", False))
        start_completion = getattr(self.integration.dispatcher, "start_completion_runtime", None)
        if result.get("execution_started") and callable(start_completion):
            start_completion()
        return dict(result, node_id=self.node_id, operation_state="STARTED")

    def status(self, **request: Any) -> dict[str, Any]:
        if request.get('native_turn_id') is not None:
            from clinx.provider.native_provider import observe_execution
            record, thread_id, turn_id = self._managed_native_execution(request)
            return dict(execution_ref=record['execution_ref'], task_ref=record['task_id'],
                        thread_id=thread_id, native_turn_id=turn_id, node_id=self.node_id,
                        provider_observation=observe_execution(self.integration.cfg, thread_id, turn_id),
                        read_only=True)
        return dict(self.integration.get_status(execution_ref=request.get("execution_ref"), task_ref=request.get("task_ref")), node_id=self.node_id)

    def _managed_native_execution(self, request):
        """Resolve only the immutable managed execution, never the latest turn."""
        registry = self.integration.registry
        record = registry.get_execution_record(request.get('execution_ref'))
        if record is None:
            raise NodeProtocolError('UNKNOWN_EXECUTION', 'Exact managed execution is required')
        turn_id = request.get('native_turn_id')
        if (not isinstance(turn_id, str) or not turn_id or len(turn_id) > 256
                or turn_id != record.get('execution_owned_turn')):
            raise NodeProtocolError('EXECUTION_TURN_IDENTITY_CONFLICT', 'Turn must match the managed execution')
        if request.get('task_ref') not in (None, record['task_id']):
            raise NodeProtocolError('EXECUTION_TURN_IDENTITY_CONFLICT', 'Task must match the managed execution')
        self._target(dict(request, task_ref=record['task_id']))
        task = registry.get_task(record['task_id'])
        route = registry.get_execution_routing_identity(record['execution_ref'])
        binding = registry.get_binding(record['task_id'])
        if (task.turn_id != turn_id or route is None or binding is None
                or route.host.stable_identifier != self.node_id
                or route.conversation.binding != binding.thread_id
                or request.get('thread_id') not in (None, binding.thread_id)):
            raise NodeProtocolError('EXECUTION_TURN_IDENTITY_CONFLICT', 'Exact task/thread/turn binding is required')
        prepared = registry.get_prepared_execution_for_execution(record['execution_ref'])
        if prepared is not None:
            prepared = registry.verify_prepared_execution(prepared.prepared_execution_ref)
            if (prepared.host != self.node_id or prepared.resulting_execution_ref != record['execution_ref']
                    or prepared.resulting_task_id != record['task_id']
                    or prepared.resulting_thread_id != binding.thread_id
                    or prepared.resulting_turn_id != turn_id):
                raise NodeProtocolError('EXECUTION_TURN_IDENTITY_CONFLICT', 'Sealed preparation differs from execution')
        return record, binding.thread_id, turn_id

    def reconcile(self, **request: Any) -> dict[str, Any]:
        """Read back through the existing Owner/Finalizer; never send an interrupt."""
        record, thread_id, turn_id = self._managed_native_execution(request)
        result = self.integration.dispatcher.reconcile_execution(
            record['execution_ref'], reclaim_stale=False, expected_turn_id=turn_id)
        return dict(result, execution_ref=record['execution_ref'], task_ref=record['task_id'],
                    thread_id=thread_id, native_turn_id=turn_id, node_id=self.node_id,
                    operation_state=result['state'], read_only=False)

    def cancel(self, **request: Any) -> dict[str, Any]:
        if request.get('native_turn_id') is not None:
            return self._cancel_native(request)
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

    def _cancel_native(self, request):
        from clinx.compat.native_lifecycle import binding as native_binding
        from clinx.provider.native_provider import cancel_native_turn
        if request.get('approved') is not True or request.get('execution_ref') is not None:
            raise NodeProtocolError('APPROVAL_REQUIRED', 'Native cancellation requires explicit approval and no managed execution reference')
        envelope = request.get('canonical_identity')
        if envelope:
            # Check cancellation scope before the optional identity handoff.
            matches = [p for p in self.integration.cfg.projects if str(p.repo) == envelope.get('cwd')]
            project = matches[0].project_alias if len(matches) == 1 else ''
        else:
            project = self.integration.registry.get_task(request['task_ref']).project_alias
        if self.allowed_cancel_projects is not None and project.casefold() not in self.allowed_cancel_projects:
            raise NodeProtocolError('EXECUTION_TARGET_DENIED', 'Cancellation is outside the owner approved project scope')
        self._qualify(request)
        self._target(request)
        association = native_binding(self.integration.registry.path, node=self.node_id, task=request['task_ref'])
        if not association or association['origin'] != 'CODEX_NATIVE':
            raise NodeProtocolError('NATIVE_IDENTITY_UNAVAILABLE', 'An exact native canonical association is required')
        active = self.integration.registry.get_latest_execution_for_task(request['task_ref'])
        if active and self.integration.registry.get_active_execution(active['execution_ref']):
            raise NodeProtocolError('MANAGED_EXECUTION_OWNER', 'Use the existing managed execution cancellation contract')
        return dict(cancel_native_turn(self.integration.cfg, association['thread_id'], request['native_turn_id']),
                    task_ref=request['task_ref'], node_id=self.node_id, source='CODEX_NATIVE', execution_ref=None)

    def close(self) -> None:
        stop = getattr(self.integration.dispatcher, "stop_completion_runtime", None)
        if callable(stop):
            stop()
