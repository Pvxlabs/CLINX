"""Public adaptation for Rust authority. No TaskRegistry or Python Owner writes."""
from __future__ import annotations

import hashlib
import os
import uuid
from types import SimpleNamespace

from rust_authority import RustAuthorityClient, RustAuthorityError


def enabled():
    mode = os.environ.get('CLINX_AUTHORITY', 'python')
    if mode not in {'python', 'rust'}:
        raise RustAuthorityError('INVALID_AUTHORITY_SELECTION')
    return mode == 'rust'


class Commands:
    def __init__(self, client):
        self.client = client

    @classmethod
    def from_env(cls):
        try:
            endpoint = os.environ['CLINX_RUST_SOCKET']
            uid = int(os.environ['CLINX_RUST_UID'])
        except (KeyError, ValueError) as exc:
            raise RustAuthorityError('RUST_AUTHORITY_CONFIG_REQUIRED') from exc
        result = cls(RustAuthorityClient(endpoint, uid))
        health = result.client.request('health', {}, request_id='health')
        if health.get('business_commands') is not True:
            raise RustAuthorityError('RUST_BUSINESS_AUTHORITY_REQUIRED')
        return result

    def call(self, operation, payload=None, *, key=None):
        key = key or uuid.uuid4().hex
        try:
            return self.client.request('business.' + operation, payload or {}, request_id=key)
        except RustAuthorityError as exc:
            # Preserve the receipt identity on an unknown response. No retry and
            # no fallback to any old registry/Owner operation.
            exc.operation_id = key
            raise


class RustRegistryView:
    def __init__(self, commands):
        self.commands = commands

    def tasks(self):
        return self.commands.call('list_tasks')['tasks']

    def get_task(self, ref):
        task = next((t for t in self.tasks() if t['task_id'] == ref), None)
        if task is None:
            raise RustAuthorityError('UNKNOWN_TASK')
        return SimpleNamespace(**dict(task, project_alias=task['project'], cwd=task['workspace_contract']['cwd']))

    def get_binding(self, ref):
        task = self.get_task(ref)
        return SimpleNamespace(thread_id=task.thread_id) if task.thread_id else None

    def verify_prepared_execution(self, ref):
        p = self.commands.call('get_prepared', dict(prepared_execution_ref=ref))
        return SimpleNamespace(**p)


class RustIntegration:
    """Native forwarding commands only; unsupported legacy control is explicit."""
    rust_authority = True

    def __init__(self, cfg, commands=None):
        self.cfg = cfg
        self.commands = commands or Commands.from_env()
        self.registry = RustRegistryView(self.commands)
        # Only the dedicated transport service polls work. MCP cannot acquire
        # transport evidence rights or start a local completion worker.
        self.dispatcher = None

    def adopt_conversation(self, *, host=None, project=None, thread_id=None, title=None,
                           summary=None, conversation_ref=None, query=None, codex_uri=None, exact_turn_id=None, workspace_contract=None):
        if conversation_ref or query or codex_uri or not thread_id or not project:
            raise RustAuthorityError('EXACT_NATIVE_THREAD_AND_PROJECT_REQUIRED')
        result = self.commands.call('adopt', dict(host=host or self.cfg.runtime_host,
            project=project, thread_id=thread_id, title=title or summary or 'Native Codex conversation',
            workspace_contract=workspace_contract, exact_turn_id=exact_turn_id))
        return dict({k:v for k,v in result.items() if k in ('adoption_ref', 'adoption_status',
            'task_ref', 'thread_id', 'workspace_contract')}, read_only=False)

    @staticmethod
    def _policy(target_policy):
        if target_policy.get('execution_surface') in (None, 'CODEX_NATIVE', 'NATIVE_FULL_ACCESS'):
            if target_policy.get('operation_scopes') or target_policy.get('required_capabilities'):
                raise RustAuthorityError('INVALID_NATIVE_POLICY')
            return dict(execution_surface='CODEX_NATIVE', operation_scopes=[])
        return dict(execution_surface=target_policy['execution_surface'],
                    operation_scopes=target_policy.get('operation_scopes') or [])

    def register_derived_git_target(self, *, approved=False, task_ref, path,
                                    ownership_evidence, creation_host_execution_ref=None):
        return self.commands.call('register_git_target', dict(approved=approved, task_ref=task_ref,
            path=path, ownership_evidence=ownership_evidence,
            creation_host_execution_ref=creation_host_execution_ref))

    def revoke_derived_git_target(self, *, approved=False, task_ref, target):
        return self.commands.call('revoke_git_target', dict(approved=approved, task_ref=task_ref, target=target))

    def prepare_policy_reauthorization(self, *, approved=False, task_ref, expected_policy_hash,
                                       target_policy, reason, network_access=None):
        if network_access is not None:
            raise RustAuthorityError('NETWORK_POLICY_CHANGE_NOT_SUPPORTED')
        result = self.commands.call('prepare_policy', dict(approved=approved, task_ref=task_ref,
            expected_policy_hash=expected_policy_hash, target_policy=self._policy(target_policy), reason=reason))
        return self._policy_result(result)

    def apply_policy_reauthorization(self, *, prepared_reauthorization_ref, approved=False,
                                    expected_request_hash=None, expected_task_ref=None):
        result = self.commands.call('apply_policy', dict(prepared_reauthorization_ref=prepared_reauthorization_ref,
            approved=approved, expected_request_hash=expected_request_hash, expected_task_ref=expected_task_ref))
        return self._policy_result(result)

    @staticmethod
    def _policy_result(result):
        return dict({k: result[k] for k in ('prepared_reauthorization_ref', 'task_ref',
            'expected_policy_hash', 'request_hash', 'operation_id', 'status')},
            future_execution_policy=result['target_policy'], execution_started=False)

    def prepare_execution(self, *, prompt, approved=False, task_mode='continue', task_action=None,
                          task_ref=None, host=None, project=None, title=None, summary=None,
                          workspace_contract=None, model=None, reasoning_effort=None, reasoning=None,
                          query=None, execution_mode='normal', network_access=None, execution_surface=None,
                          required_capabilities=None, operation_classes=None, production_mutation_intent=None,
                          requested_operations=None, codex_uri=None):
        if (query or codex_uri or execution_mode != 'normal' or network_access is not None
                or execution_surface not in (None, 'CODEX_NATIVE', 'HOST_EXECUTOR')):
            raise RustAuthorityError('RUST_REQUEST_CAPABILITY_NOT_IMPLEMENTED')
        if task_action not in (None, 'create', 'continue') or task_mode not in ('new', 'continue'):
            raise RustAuthorityError('RUST_TASK_ACTION_NOT_IMPLEMENTED')
        create = task_action == 'create' or task_mode == 'new'
        if create == bool(task_ref):
            raise RustAuthorityError('EXACT_TASK_OR_CREATE_REQUIRED')
        if task_ref:
            task = self.registry.get_task(task_ref)
            if host not in (None, task.host) or project not in (None, task.project_alias):
                raise RustAuthorityError('TASK_ROUTE_MISMATCH')
            host, project = task.host, task.project_alias
        if not project:
            raise RustAuthorityError('EXACT_PROJECT_REQUIRED')
        if reasoning and reasoning_effort and reasoning != reasoning_effort:
            raise RustAuthorityError('REASONING_CONFLICT')
        execution_policy = None
        if execution_surface == 'HOST_EXECUTOR' or requested_operations:
            from execution_policy import build_execution_policy
            policy = build_execution_policy(execution_surface=execution_surface,
                required_capabilities=required_capabilities, operation_classes=operation_classes,
                production_mutation_intent=bool(production_mutation_intent),
                operation_scopes=requested_operations)
            execution_policy = self._policy(policy.as_dict())
        elif required_capabilities or operation_classes or production_mutation_intent:
            raise RustAuthorityError('EXACT_HOST_POLICY_REQUIRED')
        result = self.commands.call('prepare', dict(prompt=prompt, approved=approved, project=project,
            host=host or self.cfg.runtime_host, task_ref=task_ref, title=title or summary or prompt[:120],
            workspace_contract=workspace_contract, model=model, reasoning_effort=reasoning_effort or reasoning,
            execution_policy=execution_policy))
        return dict({k: v for k, v in result.items() if k not in {'prompt', 'id', 'new_task'}},
                    execution_started=False, read_only=False)

    def start_execution(self, *, prepared_execution_ref, approved=False,
                        expected_request_hash=None, expected_task_ref=None):
        # A second start returns the existing execution and cannot enqueue another turn.
        return self.commands.call('start', dict(prepared_execution_ref=prepared_execution_ref, approved=approved,
            expected_request_hash=expected_request_hash, expected_task_ref=expected_task_ref))

    def cancel_execution(self, *, execution_ref=None, task_ref=None, native_turn_id=None, approved=False):
        if not execution_ref or task_ref is not None or native_turn_id is not None:
            raise RustAuthorityError('EXACT_RUST_EXECUTION_REQUIRED')
        return self.commands.call('cancel', dict(execution_ref=execution_ref, approved=approved))

    def get_status(self, *, execution_ref=None, task_ref=None, **selectors):
        if any(v is not None for v in selectors.values()):
            raise RustAuthorityError('EXACT_RUST_EXECUTION_REQUIRED')
        if execution_ref:
            result = self.commands.call('get_status', dict(execution_ref=execution_ref))
            if task_ref and result['task_ref'] != task_ref:
                raise RustAuthorityError('TASK_ROUTE_MISMATCH')
            return result
        if task_ref:
            return self._task_public(vars(self.registry.get_task(task_ref)))
        raise RustAuthorityError('EXACT_RUST_EXECUTION_REQUIRED')

    def get_effective_authority(self, *, task_ref, requested_operations=None):
        effective = self.commands.call('effective_policy', dict(task_ref=task_ref))
        task, scopes = effective['task'], effective['operation_scopes']
        missing = [s for s in (requested_operations or []) if s not in scopes]
        return dict(task_ref=task_ref, future_execution_policy=task['execution_policy'], policy_hash=task['policy_hash'],
                    effective_authority=dict(authority='RUST', operation_scopes=scopes,
                        denied_scopes=effective['denied_scopes']),
                    requirements_evaluated=requested_operations is not None,
                    requirements_covered=not missing if requested_operations is not None else None,
                    missing_operations=missing, read_only=True)

    def get_prepared_request(self, *, request_ref):
        p = self.commands.call('get_prepared', dict(prepared_execution_ref=request_ref))
        if p.get('kind') == 'POLICY':
            task = self.registry.get_task(p['task_ref'])
            return dict(request_ref=request_ref, request_type='POLICY_REAUTHORIZATION',
                request_hash=p['request_hash'], request_state=p['status'], task_ref=p['task_ref'],
                target=dict(project=task.project_alias, host=task.host, workspace_contract=task.workspace_contract),
                baseline_policy_hash=p['expected_policy_hash'], current_policy_hash=task.policy_hash,
                proposed_policy=p['target_policy'], current_policy=task.execution_policy,
                execution_started=False, read_only=True)
        return dict(request_ref=request_ref, request_type='EXECUTION', request_hash=p['request_hash'],
                    request_state=p['status'], task_ref=p['task_ref'], execution_ref=p.get('execution_ref'),
                    target=dict(project=p['project'], host=p['host'], workspace_contract=p['workspace_contract']),
                    prompt_sha256=hashlib.sha256(p['prompt'].encode()).hexdigest(), read_only=True)

    def find_task(self, *, task_ref=None, query=None, project=None, **unused):
        if unused:
            raise RustAuthorityError('UNSUPPORTED_TASK_SELECTOR')
        tasks = self.registry.tasks()
        return dict(tasks=[self._task_public(t) for t in tasks if (not task_ref or t['task_id'] == task_ref)
            and (not project or t['project'] == project)
            and (not query or query.casefold() in t['title'].casefold())], read_only=True)

    @staticmethod
    def _task_public(task):
        return dict({k:v for k,v in task.items() if k in ('title', 'project', 'host', 'workspace_contract',
            'thread_id', 'lifecycle', 'policy_hash', 'execution_policy', 'active_execution')},
            task_ref=task['task_id'], read_only=True, authority='RUST')

    def list_projects(self, *, host=None, query=None):
        result = self.commands.call('list_projects')
        return dict(projects=[dict(project=p['alias'], cwd=p['cwd'], host=p['host'])
                              for p in result['projects']
                              if (not host or p['host'] == host)
                              and (not query or query.casefold() in p['alias'].casefold())], read_only=True)

    def get_capabilities(self, **unused):
        return dict(authority='RUST', capabilities=['prepare', 'start', 'cancel', 'receipt_readback', 'native_result', 'native_adoption', 'policy_reauthorization'],
                    legacy_history=False, host_tools=True, read_only=True)

    def get_context(self, *, task_ref=None, thread_id=None, host=None, project=None, execution_ref=None,
                    recent_turns=8, max_bytes=32000, cursor=None, codex_uri=None):
        if cursor or codex_uri:
            raise RustAuthorityError('EXACT_THREAD_SELECTOR_REQUIRED')
        exact_turn_id = None
        if execution_ref:
            execution = self.commands.call('get_status', dict(execution_ref=execution_ref))
            if task_ref not in (None, execution['task_ref']) or thread_id not in (None, execution['thread_id']):
                raise RustAuthorityError('EXECUTION_TURN_IDENTITY_CONFLICT')
            task_ref, thread_id = execution['task_ref'], execution['thread_id']
            task = self.registry.get_task(task_ref)
            if host not in (None, task.host) or project not in (None, task.project_alias):
                raise RustAuthorityError('TASK_ROUTE_MISMATCH')
            host = task.host
            exact_turn_id = execution['native_turn_id']
            if not exact_turn_id:
                raise RustAuthorityError('EXACT_NATIVE_TURN_UNAVAILABLE')
        elif task_ref:
            task = self.registry.get_task(task_ref)
            if thread_id not in (None, task.thread_id) or host not in (None, task.host):
                raise RustAuthorityError('TASK_ROUTE_MISMATCH')
            thread_id, host = task.thread_id, task.host
        if not thread_id or host not in (None, self.cfg.runtime_host):
            raise RustAuthorityError('LOCAL_EXACT_THREAD_REQUIRED')
        from native_provider import select_execution_client
        with select_execution_client(self.cfg, thread_id) as client:
            client.initialize(client_name=self.cfg.app_server.client_name, client_title=self.cfg.app_server.client_title,
                              client_version=self.cfg.app_server.client_version)
            if exact_turn_id:
                from rust_transport import RustDispatcher
                page = dict(data=[RustDispatcher.exact_turn(client, thread_id, exact_turn_id)], nextCursor=None)
            else:
                page = client.thread_turns_list(thread_id, limit=min(20, max(1, recent_turns)), items_view='full')
        import json
        if len(json.dumps(page).encode()) > min(524288, max_bytes):
            raise RustAuthorityError('CONTEXT_CAPACITY_EXCEEDED')
        return dict(thread_id=thread_id, task_ref=task_ref, context=page, read_only=True)

    def execute(self, **arguments):
        raise RustAuthorityError('SEALED_PREPARATION_REQUIRED')


def stable_key(prefix, *parts):
    return prefix + ':' + hashlib.sha256('\0'.join(parts).encode()).hexdigest()
