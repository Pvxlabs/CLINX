"""Existing Host runner and delivery transport with all durable writes in Rust."""
from __future__ import annotations

import os
import stat
from pathlib import Path

from clinx.contracts.execution_policy import build_execution_policy
from clinx.contracts.execution_semantics import build_routing_identity
from clinx.host.host_executor import HostExecutionRequest, HostExecutor
from clinx.authority.rust_authority import RustAuthorityError
from clinx.authority.rust_integration import RustRegistryView, stable_key
from clinx.contracts.runtime_contracts import worktree_key


class RustHostRegistry(RustRegistryView):
    rust_authority = True
    worktree_key = staticmethod(worktree_key)

    def spawn_argv(self, argv):
        health = self.commands.client.request('health', {}, request_id='host-runtime-identity')
        if health['qualification']:
            return list(argv)
        launcher = Path(os.environ.get('CLINX_RUST_WORKER_LAUNCHER', '/var/lib/clinx-rust/current/host-worker'))
        # Resolve no user-controlled link; the release location is explicit.
        if launcher.resolve() != launcher:
            raise RustAuthorityError('ROOT_CONTROLLED_WORKER_LAUNCHER_REQUIRED')
        for path in launcher.parents:
            info = path.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise RustAuthorityError('ROOT_CONTROLLED_WORKER_LAUNCHER_REQUIRED')
        info = launcher.stat()
        if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode) or not info.st_mode & stat.S_ISUID:
            raise RustAuthorityError('ROOT_CONTROLLED_WORKER_LAUNCHER_REQUIRED')
        return [str(launcher), *argv]

    def get_task(self, ref):
        task = super().get_task(ref)
        task.branch = task.workspace_contract["branch"]
        task.workspace_alias = task.project_alias
        task.repository_origin = getattr(task, 'repository_origin', None)
        return task

    def resolve_derived_git_target(self, *, task_id, target_id, trusted_roots):
        # Root policy and filesystem identity are validated inside Rust.
        return self.commands.call('resolve_git_target', dict(task_ref=task_id, target=target_id))

    def reconcile_stale_host_executions(self, _instance):
        # Rust restart preserves unresolved RUNNING deliveries; no timeout release.
        return 0

    def validate_host_operation_binding(self, *, task_id, execution_ref, **_kwargs):
        result = self.commands.call('get_status', dict(execution_ref=execution_ref))
        if result['task_ref'] != task_id or result['resource_state'] != 'HELD':
            raise RustAuthorityError('HOST_EXECUTION_BINDING_CONFLICT')

    def begin_host_execution(self, **evidence):
        result = self.commands.call('host_begin', dict(execution_ref=evidence['execution_ref'],
            call_id=evidence['tool_call_id'], host_ref=evidence['host_execution_ref'], evidence=evidence),
            key=stable_key('host-begin', evidence['execution_ref'], evidence['tool_call_id']))
        if result.get('send_permitted') is not True:
            raise RustAuthorityError('HOST_PERMIT_ALREADY_CONSUMED')

    def complete_host_execution(self, host_ref, **result):
        # The runner supplies correlation; Rust validates it against the stored
        # dispatch and accepts a completion only once.
        self.commands.call('host_complete', dict(execution_ref=self.execution_ref,
            call_id=self.call_id, host_ref=host_ref, result=result))

    def request_host_execution_cancellation(self, execution_ref):
        status = self.commands.call('get_status', dict(execution_ref=execution_ref))
        if not status['cancel_requested']:
            raise RustAuthorityError('RUST_CANCELLATION_INTENT_REQUIRED')
        return sum(r['call_state'] == 'RUNNING' for r in self.commands.call('host_rows',
            dict(execution_ref=execution_ref))['rows'])


class RustHostDelivery:
    def __init__(self, commands, execution_ref, recover_input):
        self.commands, self.execution_ref = commands, execution_ref
        self.registry = self
        self.recover_input = recover_input

    def call(self, op, call_id, **payload):
        return self.commands.call(op, dict(execution_ref=self.execution_ref, call_id=call_id, **payload))

    @staticmethod
    def records(registry, execution_ref):
        return registry.commands.call('host_rows', dict(execution_ref=execution_ref))['rows']

    @staticmethod
    def ingress_id(request):
        return request['params']['callId']

    def receive(self, request, identity):
        status = self.commands.call('get_status', dict(execution_ref=self.execution_ref))
        if status['dispatch_state'] == 'SENDING':
            self.recover_input(self.execution_ref)
        return self.call('host_receive', self.ingress_id(request), request=request, identity=identity)

    def admit(self, request, _identity):
        call_id = self.ingress_id(request)
        result = self.call('host_take', call_id)
        if result.get('send_permitted') is not True:
            raise RustAuthorityError('HOST_PERMIT_ALREADY_CONSUMED')
        return call_id

    def reject_received(self, request, failure_code):
        call_id = self.ingress_id(request)
        row = next((r for r in self.records(self, self.execution_ref) if r['call_id'] == call_id), None)
        if row and row['call_state'] == 'RECEIVED':
            self.failed(call_id, failure_code=failure_code)
            return call_id
        return None

    def host_result(self, call_id, result):
        row = next(r for r in self.records(self, self.execution_ref) if r['call_id'] == call_id)
        if (row['host_ref'] != result.get('host_execution_ref') or row['host_exit_code'] != result.get('exit_code')
                or row['host_completion'] != 'KNOWN'):
            raise RustAuthorityError('HOST_RESULT_NOT_COMMITTED')

    def sent(self, call_id, request_id):
        self.call('host_sent', call_id, response_id=request_id)

    def failed(self, call_id, *, not_dispatched=False, failure_code=None):
        # The flag is deliberately not forwarded. Rust knows whether host_begin
        # committed; caller exception types do not prove physical non-dispatch.
        return self.call('host_failed', call_id, failure_code=failure_code or 'TRANSPORT_LOST')['execution_state']

    def disconnected(self):
        for row in self.records(self, self.execution_ref):
            if row['delivery_state'] == 'PENDING':
                self.failed(row['call_id'])

    def observe(self, message, *, owner=None):
        if message.get('method') != 'item/completed':
            return
        item = message.get('params', {}).get('item', {})
        if item.get('type') not in ('dynamicToolCall', 'DynamicToolCall'):
            return
        self.call('host_ack', item['id'], message=message)


def configure(client, cfg, commands, prepared, execution_ref, tid, recover_input):
    policy = host_policy(prepared)
    registry = RustHostRegistry(commands)
    task = registry.get_task(prepared['task_ref'])
    route = build_routing_identity(host=task.host, surface='host_executor', workspace_alias=task.workspace_alias,
        project_alias=task.project_alias, worktree_key=registry.worktree_key(host=task.host, cwd=task.cwd,
        repository_origin=task.repository_origin), project_identity=task.project_alias,
        conversation_binding=tid, authority_scopes=policy.authority_scopes)
    runner = HostExecutor(cfg.host_executor, registry)
    ledger = RustHostDelivery(commands, execution_ref, recover_input)

    def handle(params):
        args = params['arguments']
        registry.execution_ref, registry.call_id = execution_ref, params['callId']
        return runner.execute(HostExecutionRequest(task_ref=prepared['task_ref'], execution_ref=execution_ref,
            route=route, policy=policy, operation_class=args['operation_class'], capability=args['capability'],
            operation=args['operation'], arguments=args['arguments'], project_root=Path(task.cwd),
            timeout_seconds=args.get('timeout_seconds'), tool_call_id=params['callId']))

    client.configure_dynamic_tool(namespace=HostExecutor.DYNAMIC_TOOL_NAMESPACE,
        name=HostExecutor.DYNAMIC_TOOL_NAME, thread_id=tid, handler=handle,
        operation_id=execution_ref, delivery_ledger=ledger)
    return runner


def host_policy(prepared):
    scopes = prepared['execution_policy']['operation_scopes']
    return build_execution_policy(execution_surface='HOST_EXECUTOR',
        required_capabilities=sorted({s['capability'] for s in scopes}),
        operation_classes=sorted({s['operation_class'] for s in scopes}),
        production_mutation_intent=any(s['operation_class'] == 'PRODUCTION_MUTATION' for s in scopes),
        operation_scopes=scopes)
