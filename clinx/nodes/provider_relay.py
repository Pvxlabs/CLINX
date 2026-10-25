"""Paired Provider protocol relay; no task DB, finalizer, input retry or Owner.

The local gateway accepts only the existing Transport UID. Every remote chunk
uses current mTLS trust and the owner/centre scope intersection. Provider JSON
is read from the configured existing daemon, never synthesized from node status.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from clinx.nodes.node_protocol import NodeProtocolError

MAX_REQUEST = 1_048_576
MAX_RESPONSE = 16 * 1024 * 1024
CHUNK = 48 * 1024


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def fail(code):
    raise NodeProtocolError(code, 'Provider relay contract rejected')


class NodeProviderRelay:
    """Private Rust buffer handle; Python holds no submission state or sockets."""
    def __init__(self, cfg, node_id):
        import weakref

        from clinx.nodes.network_worker import Worker
        self.cfg, self.node_id = cfg, node_id
        reference = weakref.ref(self)
        def callback(event):
            relay = reference()
            if relay is None or event['operation'] != 'relay.exchange':
                fail('PROVIDER_DELIVERY_UNKNOWN')
            args = event['arguments']
            return relay.exchange(args['context'], args['body'])
        self.worker = Worker(callback=callback)

    @property
    def buffers(self):
        # Read-only diagnostic; all rows and transitions belong to Rust.
        return self.worker.call('relay.count')

    def handle(self, request, scope):
        from clinx.authority.rust_authority import RustAuthorityError
        try:
            return self.worker.call('relay.handle', node_id=self.node_id,
                request=request, scope=scope.as_dict(),
                context_wire=encoded(request.get('context')).decode())
        except RustAuthorityError as exc:
            fail(str(exc))

    def close(self):
        self.worker.close()

    def __del__(self):
        if hasattr(self, 'worker'):
            self.close()

    def exchange(self, context, body):
        # The existing paired channel checks current owner/Centre grants above.
        # Rust validates exact method/input identity before opening the Provider;
        # this process transports opaque bytes and never retries an unknown call.
        configured = []
        for value in (self.cfg.app_server.local_socket,
                      getattr(self.cfg.app_server, 'native_socket', None)):
            if value:
                path = Path(value).expanduser()
                if not path.is_absolute():
                    path = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / path
                if str(path) not in configured:
                    configured.append(str(path))
        if len(configured) != 1:
            fail('EXACT_DESKTOP_ENDPOINT_REQUIRED')
        root = Path(__file__).resolve().parents[2]
        explicit = os.environ.get('CLINX_PROVIDER_BINARY')
        candidates = ([Path(explicit)] if explicit else
                      [root / 'clinx-provider', root.parent / 'clinx-provider',
                       root.parent / 'Helpers/clinx-provider'])
        binary = next((p for p in candidates if p.is_absolute() and p.is_file()
                       and os.access(p, os.X_OK)), None)
        if binary is None:
            fail('RUST_PROVIDER_BINARY_REQUIRED')
        timeout = self.cfg.app_server.request_timeout_seconds if hasattr(
            self.cfg.app_server, 'request_timeout_seconds') else 3
        from clinx.authority.rust_authority import RustAuthorityError
        try:
            return self.worker.call('relay.exchange', endpoint=configured[0], program=str(binary),
                timeout=timeout, context=context, body=body)
        except RustAuthorityError as exc:
            fail(str(exc))


def registry_callback(registry, event):
    args = event['arguments']
    if event['operation'] == 'registry.get':
        record = registry.get(args['node_id'])
        return record.as_dict() if record else None
    if event['operation'] == 'registry.scope':
        scope = registry.scope(**args)
        return scope.as_dict() if scope else None
    fail('NETWORK_CALLBACK_INVALID')


def paired_exchange(tls, registry, approvals, node_id, context, body):
    """Rust owns scope validation, chunks and evidence identity; never retries."""
    from clinx.authority.rust_authority import RustAuthorityError
    from clinx.nodes.network_worker import Worker
    worker = Worker(callback=lambda event: registry_callback(registry, event))
    try:
        return worker.call('paired.exchange', state=str(tls.identity.store.root),
            node_id=node_id, context=context, body_wire=encoded(body).decode())
    except RustAuthorityError as exc:
        fail(str(exc))
    finally:
        worker.close()


class ProviderGateway:
    """Own a Rust Centre-local IPC listener; Python owns no network socket."""
    def __init__(self, endpoint, transport_uid, tls, registry, approvals, *, observer_uid=None,
                 socket_gid=None):
        from clinx.authority.rust_authority import RustAuthorityError
        from clinx.nodes.network_worker import Worker
        self.endpoint = Path(endpoint)
        self.worker = Worker(callback=lambda event: registry_callback(registry, event))
        try:
            self.worker.call('gateway.listen', endpoint=str(endpoint), transport_uid=transport_uid,
                observer_uid=observer_uid, socket_gid=socket_gid, state=str(tls.identity.store.root))
        except RustAuthorityError as exc:
            self.worker.close()
            fail(str(exc))

    def close(self):
        self.worker.close()


def gateway_request(endpoint, expected_uid, context, method, params):
    """Rust observer IPC; remote identity remains bound to existing paired mTLS."""
    from clinx.authority.rust_authority import RustAuthorityError
    from clinx.nodes.network_worker import Worker
    worker = Worker()
    try:
        return worker.call('gateway.request', endpoint=str(endpoint), expected_uid=expected_uid,
            context=context, method=method, params=params)
    except RustAuthorityError as exc:
        fail(str(exc))
    finally:
        worker.close()
