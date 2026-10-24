"""Authenticated Node callbacks to the same Rust authority used by MCP."""
from __future__ import annotations

import inspect
import json

from clinx.nodes.node_protocol import NodeProtocolError


class RustNodeExecutionAdapter:
    # Internal callback capability, never accepted from the network envelope.
    # The Node registry records protocol deduplication, not task writer leases.
    authority_owns_execution = True

    def __init__(self, integration, node_id, *, allowed_threads=None, allowed_projects=None,
                 allowed_new_projects=None, allowed_cancel_projects=None):
        self.integration, self.node_id = integration, node_id
        self.allowed_threads, self.allowed_projects = allowed_threads, allowed_projects
        self.allowed_new_projects, self.allowed_cancel_projects = allowed_new_projects, allowed_cancel_projects

    def _target(self, request, *, new=False, cancel=False):
        if request.get('canonical_identity'):
            raise NodeProtocolError('CANONICAL_HANDOFF_REQUIRES_ADOPTION', 'Use the exact canonical adoption contract')
        if request.get('host') not in (None, self.node_id):
            raise NodeProtocolError('NODE_TARGET_MISMATCH', 'Authenticated host differs')
        project, thread = request.get('project'), request.get('thread_id')
        if request.get('task_ref'):
            task = self.integration.registry.get_task(request['task_ref'])
            if task.host != self.node_id or project not in (None, task.project_alias) or thread not in (None, task.thread_id):
                raise NodeProtocolError('EXECUTION_TARGET_DENIED', 'Task route differs')
            project, thread = task.project_alias, task.thread_id
        if not project:
            raise NodeProtocolError('EXECUTION_TARGET_DENIED', 'Exact project scope is required')
        def permits(scope):
            return scope is None or project.casefold() in {x.casefold() for x in scope}
        if not permits(self.allowed_projects) or (new and not permits(self.allowed_new_projects)) or (cancel and not permits(self.allowed_cancel_projects)):
            raise NodeProtocolError('EXECUTION_TARGET_DENIED', 'Project outside approved scope')
        if (thread and self.allowed_threads is not None and thread not in self.allowed_threads
                and (not request.get('task_ref') or self.allowed_new_projects is None or not permits(self.allowed_new_projects))):
            raise NodeProtocolError('EXECUTION_TARGET_DENIED', 'Thread outside approved scope')

    def _canonical_adoption(self, request):
        """Map the existing authenticated Node envelope; never write local state.

        Provenance only identifies the route. Rust Transport must independently
        read the exact Provider turn before the authority can commit adoption.
        """
        envelope, owner = request.get('canonical_identity'), request.get('_canonical_owner')
        if not isinstance(envelope, dict) or not isinstance(owner, dict):
            raise NodeProtocolError('CANONICAL_HANDOFF_SCOPE_DENIED', 'Authenticated owner scope is required')
        try:
            proof = json.loads(envelope['first_provenance_json'])
        except (KeyError, TypeError, ValueError):
            raise NodeProtocolError('CANONICAL_HANDOFF_IDENTITY_CONFLICT', 'Invalid provenance') from None
        if (not isinstance(proof, dict) or owner.get('owner') != self.node_id
                or envelope.get('node_id') != self.node_id or envelope.get('host') != self.node_id
                or envelope.get('user_scope') != owner.get('user') or not owner.get('user')
                or not owner.get('fingerprint') or proof.get('node_fingerprint') != owner['fingerprint']
                or proof.get('node') != self.node_id
                or request.get('host') not in (None, self.node_id)
                or request.get('thread_id') not in (None, envelope.get('thread_id'))
                or request.get('task_ref') not in (None, envelope.get('task_id'))):
            raise NodeProtocolError('CANONICAL_HANDOFF_IDENTITY_CONFLICT', 'Paired owner or selector differs')
        projects = [p for p in self.integration.cfg.projects if str(p.repo) == envelope.get('cwd')]
        if len(projects) != 1:
            raise NodeProtocolError('EXECUTION_TARGET_DENIED', 'Exact native cwd must map to one configured project')
        project = projects[0].project_alias
        if request.get('project') is not None and request['project'].casefold() != project.casefold():
            raise NodeProtocolError('EXECUTION_TARGET_DENIED', 'Project selector differs')
        target = dict(host=self.node_id, project=project, thread_id=envelope.get('thread_id'))
        self._target(target)
        turn = request.get('exact_turn_id')
        if not isinstance(turn, str) or not turn or request.get('native_turn_id') not in (None, turn):
            raise NodeProtocolError('EXACT_PREDECESSOR_TURN_REQUIRED', 'An exact predecessor turn is required')
        return self.integration.adopt_conversation(**target, title=envelope.get('title'),
            exact_turn_id=turn, canonical_identity=envelope)

    def prepare(self, **request):
        if request.get('canonical_identity') is not None:
            adoption = self._canonical_adoption(request)
            if adoption.get('adoption_status') != 'ADOPTED':
                raise NodeProtocolError('CANONICAL_ADOPTION_PENDING', 'Read adoption until Transport has committed exact Provider evidence')
            request = dict(request, task_ref=adoption['task_ref'])
            request.pop('canonical_identity')
        self._target(request, new=not request.get('task_ref'))
        fields = inspect.signature(self.integration.prepare_execution).parameters
        arguments = {k: v for k, v in request.items() if k in fields}
        arguments['host'] = self.node_id
        return dict(self.integration.prepare_execution(**arguments), node_id=self.node_id)

    def start(self, **request):
        ref = request.get('prepared_execution_ref')
        prepared = self.integration.commands.call('get_prepared', dict(prepared_execution_ref=ref))
        if prepared['host'] != self.node_id:
            raise NodeProtocolError('NODE_TARGET_MISMATCH', 'Prepared host differs')
        target = dict(project=prepared['project'], host=prepared['host'])
        if not prepared['new_task'] or prepared.get('execution_ref'):
            target['task_ref'] = prepared['task_ref']
        self._target(target, new=prepared['new_task'])
        expected_execution = 'exec_' + ref.removeprefix('prepared_')
        if request.get('execution_ref') not in (None, expected_execution):
            raise NodeProtocolError('EXECUTION_TARGET_DENIED', 'Execution reference differs')
        result = self.integration.start_execution(prepared_execution_ref=ref, approved=request.get('approved', False),
            expected_request_hash=request.get('expected_request_hash'), expected_task_ref=request.get('expected_task_ref'))
        return dict(result, node_id=self.node_id, operation_state='QUEUED')

    def status(self, **request):
        result = self.integration.get_status(execution_ref=request.get('execution_ref'), task_ref=request.get('task_ref'))
        # Validate the caller's complete selector against the exact Rust receipt.
        # Do not replace a supplied host/thread with the local route first.
        self._target(dict({k: v for k, v in request.items() if k != 'thread_id'},
                          task_ref=result['task_ref']))
        if request.get('thread_id') not in (None, result.get('thread_id')):
            raise NodeProtocolError('EXECUTION_TURN_IDENTITY_CONFLICT', 'Exact thread differs')
        if request.get('native_turn_id') not in (None, result.get('native_turn_id')):
            raise NodeProtocolError('EXECUTION_TURN_IDENTITY_CONFLICT', 'Exact turn differs')
        return dict(result, node_id=self.node_id)

    def cancel(self, **request):
        status = self.status(**request)
        self._target(dict(task_ref=status['task_ref']), cancel=True)
        result = self.integration.cancel_execution(execution_ref=status['execution_ref'], approved=request.get('approved', False))
        confirmed = result.get('status') == 'CANCELLED' and result.get('resource_state') == 'RELEASED'
        return dict(result, node_id=self.node_id,
                    operation_state='CANCELLED' if confirmed else 'CANCELLATION_PENDING')

    def reconcile(self, **request):
        # The transport service polls exact outstanding executions; callers have
        # no trusted-evidence UID and cannot make a local Finalizer decision.
        return dict(self.status(**request), reconciliation='RUST_TRANSPORT_PENDING')

    def context(self, **request):
        self._target(request)
        fields = inspect.signature(self.integration.get_context).parameters
        return dict(self.integration.get_context(**{k: v for k, v in request.items() if k in fields}), node_id=self.node_id)

    def adopt(self, **request):
        if request.get('canonical_identity') is not None:
            return dict(self._canonical_adoption(request), node_id=self.node_id)
        self._target(request, new=True)
        fields = inspect.signature(self.integration.adopt_conversation).parameters
        result = self.integration.adopt_conversation(**{k: v for k, v in request.items() if k in fields})
        return dict(result, node_id=self.node_id)

    def close(self):
        """Match the Helper lifecycle; the Rust adapter owns no local worker."""
