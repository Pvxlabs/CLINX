"""Helper protocol façade. All Node execution decisions run in Rust."""
from clinx.authority.request_pipe import api_config, binary, integration_callback, invoke
from clinx.nodes.node_protocol import NodeProtocolError
from clinx.authority.rust_authority import RustAuthorityError


class RustNodeExecutionAdapter:
    authority_owns_execution = True

    def __init__(self, integration, node_id, *, allowed_threads=None, allowed_projects=None,
                 allowed_new_projects=None, allowed_cancel_projects=None):
        # Explicit injected API interface retained for existing callers. Normal
        # Helper construction uses from_config and never constructs integration.
        self.integration = integration
        self.node_id = node_id
        self.config = dict(node_id=node_id,
            projects=[dict(project_alias=p.project_alias, repo=str(p.repo))
                      for p in getattr(getattr(integration, 'cfg', None), 'projects', ())],
            allowed_threads=allowed_threads, allowed_projects=allowed_projects,
            allowed_new_projects=allowed_new_projects, allowed_cancel_projects=allowed_cancel_projects)
        self.api = None

    @classmethod
    def from_config(cls, cfg, commands, node_id, **scopes):
        binary()  # Fail startup closed; never silently fall back to Python.
        result = cls(None, node_id, **scopes)
        result.config['projects'] = [dict(project_alias=p.project_alias, repo=str(p.repo)) for p in cfg.projects]
        result.api, result.commands = api_config(cfg), commands
        if result.api['runtime_host'] != node_id:
            raise RustAuthorityError('NODE_TARGET_MISMATCH')
        return result

    def _invoke(self, operation, arguments):
        request = dict(mode='node' if self.api is not None else 'delegated_node',
                       node=self.config, operation=operation, arguments=arguments)
        if self.api is not None:
            request['config'] = self.api
        def callback(message):
            if self.api is None:
                return integration_callback(self.integration, message)
            if message['kind'] != 'authority':
                raise RustAuthorityError('REQUEST_PIPE_INVALID')
            return self.commands.call(message['operation'], message['arguments'], key=message['operation_id'])
        try:
            return invoke(request, callback)
        except RustAuthorityError as exc:
            # Transport/Authority errors retain exact receipt identity. Selector
            # errors retain the existing NodeProtocolError public interface.
            if str(exc) in {'NODE_TARGET_MISMATCH', 'EXECUTION_TARGET_DENIED',
                'EXECUTION_TURN_IDENTITY_CONFLICT', 'CANONICAL_HANDOFF_REQUIRES_ADOPTION',
                'CANONICAL_HANDOFF_SCOPE_DENIED', 'CANONICAL_HANDOFF_IDENTITY_CONFLICT',
                'EXACT_PREDECESSOR_TURN_REQUIRED', 'CANONICAL_ADOPTION_PENDING'}:
                detail = 'Read adoption until Transport has committed exact Provider evidence' if str(exc) == 'CANONICAL_ADOPTION_PENDING' else str(exc)
                raise NodeProtocolError(str(exc), detail) from None
            raise

    def prepare(self, **request): return self._invoke('prepare', request)
    def start(self, **request): return self._invoke('start', request)
    def status(self, **request): return self._invoke('status', request)
    def cancel(self, **request): return self._invoke('cancel', request)
    def reconcile(self, **request): return self._invoke('reconcile', request)
    def context(self, **request): return self._invoke('context', request)
    def adopt(self, **request): return self._invoke('adopt', request)
    def close(self): pass  # Each call reaps its own child; no worker or lease.
