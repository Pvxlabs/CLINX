"""Private Rust child protocol; carries existing IPC or paired mTLS identity.

No task state, selector decisions, execution retry, listener, or worker lives here.
"""
from __future__ import annotations

import inspect
import json
import os
import select
import shutil
import subprocess
import time
from pathlib import Path

from clinx.authority.rust_authority import RustAuthorityError

LIMIT = 1_048_576


def binary():
    root = Path(__file__).resolve().parents[2]
    candidates = (root.parent/'clinx-node', root.parent/'Helpers/clinx-node')
    value = os.environ.get('CLINX_NODE_BINARY') or next((str(p) for p in candidates if p.is_file()), None) or shutil.which('clinx-node')
    if not value:
        raise RustAuthorityError('RUST_NODE_BINARY_REQUIRED')
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise RustAuthorityError('RUST_NODE_BINARY_REQUIRED')
    return path


def api_config(cfg):
    # Serialize configured routes only; Rust validates selectors and exact reads.
    from clinx.provider.native_provider import endpoints
    endpoint = endpoints(cfg)[0] if hasattr(cfg, 'app_server') else '/unconfigured-provider'
    from clinx.contracts.execution_semantics import normalize_host
    aliases, desktops = {cfg.runtime_host.casefold(): {cfg.runtime_host}}, {}
    for w in getattr(cfg, 'workspaces', ()):
        canonical = normalize_host(w.host or w.alias).stable_identifier
        for name in (w.alias, w.host, canonical, 'workstation-' + canonical):
            if name:
                aliases.setdefault(name.casefold(), set()).add(canonical)
        for name in w.codex_host_ids:
            desktops.setdefault(name, set()).add(canonical)
    return dict(runtime_host=cfg.runtime_host, provider_endpoint=endpoint,
                provider_timeout_seconds=getattr(getattr(cfg, 'app_server', None), 'request_timeout_seconds', 3),
                host_aliases={k: sorted(v) for k, v in aliases.items()},
                codex_host_ids={k: sorted(v) for k, v in desktops.items()})


def invoke(request, callback):
    """One request, bounded frames, exact callback IDs, never retry a child/send."""
    child = subprocess.Popen([str(binary())], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, bufsize=0)
    deadline, pending, last_id, operation_id = time.monotonic() + 120, b'', 0, None

    def send(value):
        wire = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode() + b'\n'
        if len(wire) > LIMIT:
            raise RustAuthorityError('REQUEST_TOO_LARGE')
        # Unbuffered FileIO.write can be short even on a blocking pipe.
        view = memoryview(wire)
        while view:
            n = child.stdin.write(view)
            if not n:
                raise RustAuthorityError('DELIVERY_UNKNOWN')
            view = view[n:]

    def frame():
        nonlocal pending
        while b'\n' not in pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([child.stdout], [], [], remaining)[0]:
                raise RustAuthorityError('DELIVERY_UNKNOWN')
            part = os.read(child.stdout.fileno(), min(65536, LIMIT + 1 - len(pending)))
            if not part or len(pending) + len(part) > LIMIT:
                raise RustAuthorityError('DELIVERY_UNKNOWN')
            pending += part
        line, pending = pending.split(b'\n', 1)
        return json.loads(line)

    try:
        send(dict(protocol='CLINX_REQUEST_PIPE_V1', **request))
        while True:
            message = frame()
            if not isinstance(message, dict):
                raise RustAuthorityError('DELIVERY_UNKNOWN')
            if message.get('kind') == 'result':
                if ('result' in message) == ('error' in message):
                    raise RustAuthorityError('DELIVERY_UNKNOWN')
                if 'error' in message:
                    e = message['error']
                    error = RustAuthorityError(e.get('code', 'DELIVERY_UNKNOWN'))
                    error.operation_id = e.get('operation_id')
                    raise error
                child.stdin.close()
                if child.wait(timeout=max(.01, deadline - time.monotonic())) != 0 or pending:
                    raise RustAuthorityError('DELIVERY_UNKNOWN')
                return message['result']
            if type(message.get('id')) is not int or message['id'] != last_id + 1:
                raise RustAuthorityError('DELIVERY_UNKNOWN')
            last_id = message['id']
            operation_id = message.get('operation_id') or operation_id
            try:
                result = callback(message)
            except Exception as exc:
                code = getattr(exc, 'code', None) or (str(exc) if isinstance(exc, RustAuthorityError) else 'CALLBACK_UNAVAILABLE')
                send(dict(id=last_id, error=dict(code=code, operation_id=getattr(exc, 'operation_id', None))))
            else:
                send(dict(id=last_id, result=result))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        error = RustAuthorityError('DELIVERY_UNKNOWN')
        error.operation_id = operation_id
        raise error from exc
    except RustAuthorityError as exc:
        if not getattr(exc, 'operation_id', None) and operation_id:
            exc.operation_id = operation_id
        raise
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        child.stdout.close()
        if not child.stdin.closed:
            child.stdin.close()


def integration_callback(integration, message):
    """Explicit injected interface for existing clients; no production fallback."""
    if message['kind'] != 'integration':
        raise RustAuthorityError('REQUEST_PIPE_INVALID')
    operation, args = message['operation'], message['arguments']
    if operation == 'task':
        task = vars(integration.registry.get_task(args['reference']))
        return dict(task, project=task.get('project', task.get('project_alias')))
    if operation == 'prepared':
        return integration.commands.call('get_prepared', dict(prepared_execution_ref=args['reference']))
    method = getattr(integration, operation)
    fields = inspect.signature(method).parameters
    return method(**args) if any(p.kind == p.VAR_KEYWORD for p in fields.values()) else method(**{k: v for k, v in args.items() if k in fields})
