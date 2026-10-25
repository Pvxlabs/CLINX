"""Public adaptation for Rust authority. No TaskRegistry or Python Owner writes."""
from __future__ import annotations

from types import SimpleNamespace
from clinx.authority.request_pipe import api_config, invoke

from clinx.authority.rust_authority import Commands, RustAuthorityError, enabled as enabled  # compatibility export


class RustRegistryView:
    """Compatibility object views of Rust-selected records; never a registry writer."""
    rust_authority = True
    def __init__(self, commands):
        self.api = RustIntegration(SimpleNamespace(runtime_host='compat-view'), commands, _registry=False)
    def tasks(self): return self.api._invoke('internal.tasks', {})
    def get_task(self, ref):
        t = self.api._invoke('internal.task', dict(reference=ref))
        return SimpleNamespace(**dict(t, project_alias=t['project'], cwd=t['workspace_contract']['cwd']))
    def get_binding(self, ref):
        t = self.get_task(ref)
        return SimpleNamespace(thread_id=t.thread_id) if t.thread_id else None
    def verify_prepared_execution(self, ref):
        return SimpleNamespace(**self.api.commands.call('get_prepared', dict(prepared_execution_ref=ref)))


class RustIntegration:
    """Compatibility API client. Request selection/projection runs in Rust.

    Normal Helper requests bypass this class and use Rust NativeReads directly.
    The read callback remains for existing in-process read-only Provider clients.
    """
    rust_authority = True
    def __init__(self, cfg, commands=None, *, _registry=True):
        self.cfg, self.commands = cfg, commands or Commands.from_env()
        self.registry = RustRegistryView(self.commands) if _registry else None
        self.dispatcher = None

    def _invoke(self, operation, arguments):
        def callback(message):
            if message['kind'] == 'authority':
                method = self.commands.call
                # Existing injected command transports may not expose the
                # optional receipt keyword; actual Commands always preserves it.
                import inspect
                fields = inspect.signature(method).parameters
                kwargs = {'key': message['operation_id']} if ('key' in fields or any(p.kind == p.VAR_KEYWORD for p in fields.values())) else {}
                return method(message['operation'], message['arguments'], **kwargs)
            if message['kind'] == 'read':
                a, op = message['arguments'], message['operation']
                if op == 'cwd': return self._native_cwd(a['thread_id'])
                if op == 'desktop':
                    from clinx.authority.rust_reads import desktop_read
                    return desktop_read(**a)
                from clinx.provider.native_provider import endpoints, existing_client
                with existing_client(endpoints(self.cfg)[0], self.cfg.app_server.request_timeout_seconds) as client:
                    client.initialize(client_name=self.cfg.app_server.client_name, client_title=self.cfg.app_server.client_title,
                                      client_version=self.cfg.app_server.client_version)
                    if op == 'thread': return client.thread_read(a['thread_id'])
                    if op == 'page': return client.thread_turns_list(a['thread_id'], limit=a['limit'], items_view='full', cursor=a['cursor'])
            raise RustAuthorityError('REQUEST_PIPE_INVALID')
        try:
            return invoke(dict(mode='api_compat', config=api_config(self.cfg), operation=operation, arguments=arguments), callback)
        except RustAuthorityError as exc:
            if str(exc) == 'INVALID_HOST_POLICY':
                from clinx.contracts.execution_policy import ExecutionPolicyError
                raise ExecutionPolicyError('Invalid Host policy: operation scope exceeds capability/class authority or is malformed') from None
            raise

    def _native_cwd(self, thread_id):
        from clinx.provider.native_provider import endpoints, existing_client
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

    def adopt_conversation(self, *, host=None, project=None, thread_id=None, title=None, summary=None, conversation_ref=None, query=None, codex_uri=None, exact_turn_id=None, workspace_contract=None, canonical_identity=None):
        a = dict(locals()); a.pop("self")
        return self._invoke("clinx_adopt_conversation", a)

    def register_derived_git_target(self, *, approved=False, task_ref, path, ownership_evidence, creation_host_execution_ref=None):
        a = dict(locals()); a.pop("self")
        return self._invoke("clinx_register_derived_git_target", a)

    def revoke_derived_git_target(self, *, approved=False, task_ref, target):
        a = dict(locals()); a.pop("self")
        return self._invoke("clinx_revoke_derived_git_target", a)

    def prepare_policy_reauthorization(self, *, approved=False, task_ref, expected_policy_hash, target_policy, reason, network_access=None):
        a = dict(locals()); a.pop("self")
        return self._invoke("clinx_prepare_policy_reauthorization", a)

    def apply_policy_reauthorization(self, *, prepared_reauthorization_ref, approved=False, expected_request_hash=None, expected_task_ref=None):
        a = dict(locals()); a.pop("self")
        return self._invoke("clinx_apply_policy_reauthorization", a)

    def prepare_execution(self, *, prompt, approved=False, task_mode='continue', task_action=None, task_ref=None, host=None, project=None, title=None, summary=None, workspace_contract=None, model=None, reasoning_effort=None, reasoning=None, query=None, execution_mode='normal', network_access=None, execution_surface=None, required_capabilities=None, operation_classes=None, production_mutation_intent=None, requested_operations=None, codex_uri=None, thread_id=None):
        a = dict(locals()); a.pop("self")
        return self._invoke("clinx_prepare_execution", a)

    def start_execution(self, *, prepared_execution_ref, approved=False, expected_request_hash=None, expected_task_ref=None):
        a = dict(locals()); a.pop("self")
        return self._invoke("clinx_start_execution", a)

    def cancel_execution(self, *, execution_ref=None, task_ref=None, native_turn_id=None, approved=False):
        a = dict(locals()); a.pop("self")
        return self._invoke("clinx_cancel_execution", a)

    def get_status(self, *, execution_ref=None, task_ref=None, thread_id=None, codex_uri=None, host=None, project=None, node_id=None, query=None):
        a = dict(locals()); a.pop("self")
        return self._invoke("clinx_get_status", a)

    def get_effective_authority(self, *, task_ref, requested_operations=None):
        a = dict(locals()); a.pop("self")
        return self._invoke("clinx_get_effective_authority", a)

    def get_prepared_request(self, *, request_ref):
        a = dict(locals()); a.pop("self")
        return self._invoke("clinx_get_prepared_request", a)

    def find_task(self, *, task_ref=None, query=None, project=None, host=None, status=None):
        a = dict(locals()); a.pop("self")
        return self._invoke("clinx_find_task", a)

    def list_projects(self, *, host=None, query=None):
        a = dict(locals()); a.pop("self")
        return self._invoke("clinx_list_projects", a)

    def get_capabilities(self, **unused):
        a = dict(locals()); a.pop("self")
        a = unused
        return self._invoke("clinx_get_capabilities", a)

    def get_context(self, *, task_ref=None, thread_id=None, host=None, project=None, execution_ref=None, recent_turns=None, max_bytes=None, cursor=None, codex_uri=None, query=None, node_id=None):
        a = dict(locals()); a.pop("self")
        return self._invoke("clinx_get_context", a)

    @staticmethod
    def _native_host_readback(result, turn):
        return RustIntegration(SimpleNamespace(runtime_host='compat-view'), SimpleNamespace())._invoke(
            'internal.host_readback', dict(receipt=result, turn=turn))

    def execute(self, **arguments):
        raise RustAuthorityError('SEALED_PREPARATION_REQUIRED')
