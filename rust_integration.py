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
    rust_authority = True
    def __init__(self, commands):
        self.commands = commands

    def tasks(self):
        tasks, after = [], ''
        while True:
            page = self.commands.call('list_tasks', dict(projection='summary', after=after))
            tasks.extend(page['tasks'])
            cursor = page.get('next_after')
            if cursor is None:
                return tasks
            if not isinstance(cursor, str) or cursor <= after:
                raise RustAuthorityError('INVALID_TASK_CURSOR')
            after = cursor

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

    def _native_cwd(self, thread_id):
        from native_provider import existing_client, endpoints
        with existing_client(endpoints(self.cfg)[0], self.cfg.app_server.request_timeout_seconds) as client:
            client.initialize(client_name=self.cfg.app_server.client_name, client_title=self.cfg.app_server.client_title,
                              client_version=self.cfg.app_server.client_version)
            native = client.thread_read(thread_id)
        if native.get('id') != thread_id:
            raise RustAuthorityError('NATIVE_OBSERVATION_IDENTITY_CONFLICT')
        cwd = native.get('cwd')
        if not isinstance(cwd, str) or not cwd.startswith('/') or '\0' in cwd:
            raise RustAuthorityError('WORKSPACE_CWD_UNREADABLE')
        return cwd

    def adopt_conversation(self, *, host=None, project=None, thread_id=None, title=None,
                           summary=None, conversation_ref=None, query=None, codex_uri=None, exact_turn_id=None, workspace_contract=None):
        if codex_uri is not None:
            from thread_identity import resolve_selector, ThreadLookupError
            try:
                thread_id, host, _ = resolve_selector(self.cfg, thread_id=thread_id, codex_uri=codex_uri, host=host)
            except ThreadLookupError as exc:
                raise RustAuthorityError(exc.code) from None
        if conversation_ref or query or not thread_id or not project:
            raise RustAuthorityError('EXACT_NATIVE_THREAD_AND_PROJECT_REQUIRED')
        if host not in (None, self.cfg.runtime_host):
            raise RustAuthorityError('NATIVE_HOST_UNAVAILABLE')
        workspace_contract = dict(cwd=self._native_cwd(thread_id))
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
                          requested_operations=None, codex_uri=None, thread_id=None):
        if (query or execution_mode != 'normal'
                or execution_surface not in (None, 'CODEX_NATIVE', 'HOST_EXECUTOR')):
            raise RustAuthorityError('RUST_REQUEST_CAPABILITY_NOT_IMPLEMENTED')
        if task_action not in (None, 'create', 'continue', 'reopen') or task_mode not in ('new', 'continue'):
            raise RustAuthorityError('RUST_TASK_ACTION_NOT_IMPLEMENTED')
        create = task_action == 'create' or task_mode == 'new'
        if thread_id is not None or codex_uri is not None:
            from thread_identity import resolve_selector, ThreadLookupError
            try:
                thread_id, resolved_host, _ = resolve_selector(self.cfg, thread_id=thread_id, codex_uri=codex_uri, host=host)
            except ThreadLookupError as exc:
                raise RustAuthorityError(exc.code) from None
            if resolved_host != self.cfg.runtime_host:
                raise RustAuthorityError('NATIVE_HOST_UNAVAILABLE')
            host = resolved_host
            matches = [t for t in self.registry.tasks() if t['thread_id'] == thread_id and t['host'] == host]
            if len(matches) > 1:
                raise RustAuthorityError('THREAD_IDENTITY_CONFLICT')
            if matches:
                if task_ref not in (None, matches[0]['task_id']):
                    raise RustAuthorityError('TASK_ROUTE_MISMATCH')
                task_ref, create = matches[0]['task_id'], False
            elif task_ref is None:
                create = True
        if create == bool(task_ref):
            raise RustAuthorityError('EXACT_TASK_OR_CREATE_REQUIRED')
        if task_ref:
            task = self.registry.get_task(task_ref)
            if host not in (None, task.host) or project not in (None, task.project_alias):
                raise RustAuthorityError('TASK_ROUTE_MISMATCH')
            host, project = task.host, task.project_alias
            if thread_id not in (None, task.thread_id):
                raise RustAuthorityError('TASK_ROUTE_MISMATCH')
            thread_id = task.thread_id
        if not project:
            raise RustAuthorityError('EXACT_PROJECT_REQUIRED')
        if reasoning and reasoning_effort and reasoning != reasoning_effort:
            raise RustAuthorityError('REASONING_CONFLICT')
        execution_policy = None
        # Explicit native forwarding treats legacy Host fields as context,
        # just as native forwarding without those fields. They must not select
        # a Host policy or be passed to the legacy surface parser. Leave the
        # policy unset so Rust retains the task's current policy and grants.
        if execution_surface != 'CODEX_NATIVE' and (
                execution_surface == 'HOST_EXECUTOR' or requested_operations):
            from execution_policy import build_execution_policy
            policy = build_execution_policy(execution_surface=execution_surface,
                required_capabilities=required_capabilities, operation_classes=operation_classes,
                production_mutation_intent=bool(production_mutation_intent),
                operation_scopes=requested_operations)
            execution_policy = self._policy(policy.as_dict())
        elif execution_surface != 'CODEX_NATIVE' and (
                required_capabilities or operation_classes or production_mutation_intent):
            raise RustAuthorityError('EXACT_HOST_POLICY_REQUIRED')
        if thread_id:
            workspace_contract = dict(cwd=self._native_cwd(thread_id))
        result = self.commands.call('prepare', dict(prompt=prompt, approved=approved, project=project,
            host=host or self.cfg.runtime_host, task_ref=task_ref, title=title or summary or prompt[:120],
            workspace_contract=workspace_contract, model=model, reasoning_effort=reasoning_effort or reasoning,
            execution_policy=execution_policy, thread_id=thread_id))
        return dict({k: v for k, v in result.items() if k not in {'prompt', 'id', 'new_task'}},
                    execution_started=False, read_only=False)

    def start_execution(self, *, prepared_execution_ref, approved=False,
                        expected_request_hash=None, expected_task_ref=None):
        # A second start returns the existing execution and cannot enqueue another turn.
        prepared = self.commands.call('get_prepared', dict(prepared_execution_ref=prepared_execution_ref))
        native_cwd = None
        if not prepared.get('execution_ref'):
            tid = prepared.get('thread_id')
            if not tid and not prepared.get('new_task', True):
                tid = self.registry.get_task(prepared['task_ref']).thread_id
            if tid:
                native_cwd = self._native_cwd(tid)
        return self.commands.call('start', dict(prepared_execution_ref=prepared_execution_ref, approved=approved,
            expected_request_hash=expected_request_hash, expected_task_ref=expected_task_ref, native_cwd=native_cwd))

    def cancel_execution(self, *, execution_ref=None, task_ref=None, native_turn_id=None, approved=False):
        if not execution_ref or task_ref is not None or native_turn_id is not None:
            raise RustAuthorityError('EXACT_RUST_EXECUTION_REQUIRED')
        return self.commands.call('cancel', dict(execution_ref=execution_ref, approved=approved))

    def _read_target(self, *, execution_ref=None, task_ref=None, thread_id=None, codex_uri=None,
                     host=None, project=None, node_id=None, query=None):
        from thread_identity import resolve_selector, ThreadLookupError
        if query is not None:
            raise RustAuthorityError('EXACT_THREAD_SELECTOR_REQUIRED')
        selected = None
        task = None
        if execution_ref:
            selected = self.commands.call('get_status', dict(execution_ref=execution_ref))
            if task_ref not in (None, selected['task_ref']):
                raise RustAuthorityError('TASK_ROUTE_MISMATCH')
            task_ref = selected['task_ref']
        if task_ref:
            task = self.registry.get_task(task_ref)
        if thread_id is not None or codex_uri is not None:
            try:
                thread_id, resolved, _ = resolve_selector(self.cfg, thread_id=thread_id,
                                                         codex_uri=codex_uri, host=host)
            except ThreadLookupError as exc:
                raise RustAuthorityError(exc.code) from None
            host = resolved
            if selected and thread_id != selected['thread_id']:
                raise RustAuthorityError('EXECUTION_TURN_IDENTITY_CONFLICT')
            if not task:
                matches = [t for t in self.registry.tasks() if t['thread_id'] == thread_id]
                if len(matches) > 1:
                    raise RustAuthorityError('THREAD_IDENTITY_CONFLICT')
                if matches:
                    task = self.registry.get_task(matches[0]['task_id'])
        elif selected:
            thread_id = selected['thread_id']
        elif task:
            thread_id = task.thread_id
        if host not in (None, self.cfg.runtime_host) or node_id not in (None, self.cfg.runtime_host):
            raise RustAuthorityError('NATIVE_HOST_UNAVAILABLE')
        if task:
            if host not in (None, task.host) or project not in (None, task.project_alias):
                raise RustAuthorityError('TASK_ROUTE_MISMATCH')
            if not selected and thread_id != task.thread_id:
                raise RustAuthorityError('TASK_ROUTE_MISMATCH')
        if not thread_id:
            raise RustAuthorityError('EXACT_THREAD_SELECTOR_REQUIRED')
        return thread_id, task, selected

    def get_status(self, *, execution_ref=None, task_ref=None, thread_id=None, codex_uri=None,
                   host=None, project=None, node_id=None, query=None):
        if thread_id is None and codex_uri is None and all(v is None for v in (host, project, node_id, query)):
            if execution_ref:
                result = self.commands.call('get_status', dict(execution_ref=execution_ref))
                if task_ref and result['task_ref'] != task_ref:
                    raise RustAuthorityError('TASK_ROUTE_MISMATCH')
                return self._host_readback(result)
            if task_ref:
                return self._task_public(vars(self.registry.get_task(task_ref)))
        tid, task, selected = self._read_target(execution_ref=execution_ref, task_ref=task_ref,
            thread_id=thread_id, codex_uri=codex_uri, host=host, project=project, node_id=node_id, query=query)
        return self._read_native(tid, task, selected, project=project)

    def _host_readback(self, result):
        tid = result.get('thread_id')
        turn_id = result.get('native_turn_id') or result.get('execution_turn_ref')
        if not tid or not turn_id:
            return result
        from app_server import AppServerError
        from native_provider import existing_client, endpoints
        from rust_transport import RustDispatcher
        try:
            with existing_client(endpoints(self.cfg)[0], self.cfg.app_server.request_timeout_seconds) as client:
                client.initialize(client_name=self.cfg.app_server.client_name,
                                  client_title=self.cfg.app_server.client_title,
                                  client_version=self.cfg.app_server.client_version)
                turn = RustDispatcher.exact_turn(client, tid, turn_id)
            return self._native_host_readback(result, turn)
        except (AppServerError, RustAuthorityError, OSError, ValueError, KeyError):
            return result

    @staticmethod
    def _native_host_readback(result, turn):
        import json
        rows = []
        for item in turn.get('items', []):
            if (item.get('type') != 'dynamicToolCall' or item.get('namespace') != 'clinx'
                    or item.get('tool') != 'clinx_host_operation'):
                continue
            for part in item.get('contentItems', []):
                try:
                    body = json.loads(part.get('text', ''))
                except (ValueError, TypeError):
                    continue
                if not isinstance(body, dict):
                    continue
                fields = ('execution_state', 'delivery_state', 'result_state', 'reconciliation_required',
                          'host_execution_ref', 'exit_code', 'stdout', 'stderr', 'error', 'delivery_state_scope')
                rows.append(dict({k: body[k] for k in fields if k in body}, call_id=item.get('id'),
                                 source='NATIVE_DYNAMIC_TOOL_RECEIPT', success=item.get('success')))
        if not rows:
            return result
        result = dict(result, host_executions=rows)
        pending = [row for row in rows if row.get('reconciliation_required')]
        if pending:
            finalized = (result.get('resource_state') == 'RELEASED'
                         or result.get('execution_state') in ('SUCCEEDED', 'FAILED', 'CANCELLED'))
            result.update(reconciliation_required=not finalized,
                          current_stage='FINALIZED' if finalized else 'HOST_RECONCILIATION_REQUIRED',
                          failure_code=pending[0].get('error'))
        return result

    def _read_native(self, tid, task, selected, *, context=False, project=None,
                     recent_turns=8, max_bytes=32000, cursor=None):
        from rust_reads import desktop_read, stamp
        if task is None:
            result = desktop_read(tid, context=context, recent_turns=recent_turns, max_bytes=max_bytes, cursor=cursor)
            if project is not None and project != result.get('project'):
                raise RustAuthorityError('THREAD_SCOPE_MISMATCH')
            return result
        from native_provider import existing_client, endpoints
        from native_history import context_result
        exact = selected.get('native_turn_id') if selected else None
        if selected and context and not exact:
            raise RustAuthorityError('EXACT_NATIVE_TURN_UNAVAILABLE')
        if exact and cursor is not None:
            raise RustAuthorityError('EXECUTION_CONTEXT_CURSOR_NOT_SUPPORTED')
        # Managed threads always use the configured production Provider. Never
        # route a business read to Desktop based on its latest visible turn.
        with existing_client(endpoints(self.cfg)[0], self.cfg.app_server.request_timeout_seconds) as client:
            client.initialize(client_name=self.cfg.app_server.client_name, client_title=self.cfg.app_server.client_title,
                              client_version=self.cfg.app_server.client_version)
            native = client.thread_read(tid)
            if native.get('id') != tid:
                raise RustAuthorityError('NATIVE_OBSERVATION_IDENTITY_CONFLICT')
            page = None
            if context:
                if exact:
                    from rust_transport import RustDispatcher
                    page = dict(data=[RustDispatcher.exact_turn(client, tid, exact)], nextCursor=None)
                else:
                    page = client.thread_turns_list(tid, limit=recent_turns, items_view='full', cursor=cursor)
        state = native.get('status', {}).get('type', 'UNKNOWN')
        result = dict(queried_thread_id=tid, thread_id=tid, host=task.host, project=task.project_alias,
            task_ref=task.task_id, execution_ref=selected['execution_ref'] if selected else None,
            execution_turn_ref=exact, execution_state=selected['status'] if selected else None,
            execution_result=selected.get('execution_result') if selected else None,
            lookup_status='RESOLVED', read_only=True, observed_at=stamp(), status_source='RUST_AUTHORITY',
            selection_scope='EXACT_EXECUTION' if selected else 'QUERIED_THREAD',
            task_current_projection=self._task_public(vars(task)),
            current_thread_id=task.thread_id, is_current_thread=tid == task.thread_id,
            native_thread=dict(id=tid, cwd=native.get('cwd'), status=native.get('status')),
            provider_observation=dict(state=state, source='LIVE_THREAD_READ', writer_claimed=False),
            native_status_source='LIVE_THREAD_READ', native_display_state='RUNNING' if state == 'active' else 'IDLE' if state == 'idle' else 'UNKNOWN')
        if context:
            # Project only user-visible messages; preserve original final Markdown.
            entries = [dict(item=item, turn_id=turn['id']) for turn in reversed(page['data'])
                       for item in turn.get('items', [])]
            result.update(context_result(tid, entries, source='LIVE_PROVIDER_TURNS', max_bytes=max_bytes,
                turns=[t['id'] for t in page['data']], next_cursor=page.get('nextCursor')))
            result['context_scope'] = 'EXACT_EXECUTION_TURN' if exact else 'THREAD_RECENT'
            result['provenance']['execution_attribution'] = selected['execution_ref'] if selected else 'NOT_CLAIMED'
            import json
            # Retain the former page field only within the requested budget;
            # large turns still have a bounded public-message projection.
            if len(json.dumps(page).encode()) <= max_bytes:
                result['context'] = page
            else:
                result['context_truncated'] = True
        if selected:
            result = self._host_readback(result)
        return result

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
                    target=dict(project=p['project'], host=p['host'], workspace_contract=p['workspace_contract'],
                                workspace_resolution=p.get('workspace_resolution'),
                                requested_workspace_contract=p.get('requested_workspace_contract')),
                    prompt_sha256=hashlib.sha256(p['prompt'].encode()).hexdigest(), read_only=True)

    def find_task(self, *, task_ref=None, query=None, project=None, host=None, status=None):
        tasks = self.registry.tasks()
        return dict(tasks=[self._task_public(t) for t in tasks if (not task_ref or t['task_id'] == task_ref)
            and (not project or t['project'] == project)
            and (not host or t['host'] == host)
            and (not status or t['lifecycle'] == status)
            and (not query or query.casefold() in t['title'].casefold())], read_only=True)

    @staticmethod
    def _task_public(task):
        return dict({k:v for k,v in task.items() if k in ('title', 'project', 'host', 'workspace_contract',
            'thread_id', 'lifecycle', 'policy_hash', 'execution_policy', 'active_execution',
            'repository_identity', 'workspace_resolution')},
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
                    recent_turns=None, max_bytes=None, cursor=None, codex_uri=None, query=None, node_id=None):
        from rust_reads import limits
        recent_turns, max_bytes = limits(recent_turns, max_bytes)
        tid, task, selected = self._read_target(execution_ref=execution_ref, task_ref=task_ref,
            thread_id=thread_id, codex_uri=codex_uri, host=host, project=project, query=query, node_id=node_id)
        return self._read_native(tid, task, selected, context=True, project=project,
                                 recent_turns=recent_turns, max_bytes=max_bytes, cursor=cursor)

    def execute(self, **arguments):
        raise RustAuthorityError('SEALED_PREPARATION_REQUIRED')


def stable_key(prefix, *parts):
    return prefix + ':' + hashlib.sha256('\0'.join(parts).encode()).hexdigest()
