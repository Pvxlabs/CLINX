"""Restricted, versioned device credentials and atomic peer trust records.

Reuse HostIdentity.stable_identifier as node_id. Addresses never enter identity
or authority. Existing task/routing databases are deliberately not opened here.
"""

from __future__ import annotations

import base64
import contextlib
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Iterator

from clinx.contracts.execution_semantics import HostIdentity, normalize_host

from . import PROTOCOL_VERSION as PROTOCOL_VERSION


class DeviceError(ValueError):
    """Safe, fixed error codes only; never echo untrusted wire values or PINs."""


_runtime_lock = threading.Lock()
_runtime_worker = None
_runtime_callbacks = {}
_enrollment_callbacks = {}


def _state_callback(event):
    if event['operation'].startswith('discovery.enrollment.'):
        with _runtime_lock:
            owner = _enrollment_callbacks.get(event['arguments']['token'])
        if owner is None:
            raise DeviceError('DEVICE_UNAVAILABLE')
        if event['operation'] == 'discovery.enrollment.clock':
            return owner.window.clock()
        if event['operation'] == 'discovery.enrollment.observer':
            return owner.observer_bootstrap.connection()
        raise DeviceError('DEVICE_CALLBACK_INVALID')
    with _runtime_lock:
        context = _runtime_callbacks.get(event['arguments']['token'])
    if event['operation'] != 'discovery.commit' or context is None:
        raise DeviceError('PAIRING_REPLAY')
    try:
        context['commit']()
    except Exception as exc:
        context['error'] = exc
        raise
    return None


def _state_call(rpc_method, *, _commit=None, **arguments):
    from clinx.authority.rust_authority import RustAuthorityError
    global _runtime_worker
    token, context = uuid.uuid4().hex, {'commit': _commit, 'error': None}
    with _runtime_lock:
        if _runtime_worker is None:
            import atexit
            from clinx.nodes.network_worker import Worker
            _runtime_worker = Worker(callback=_state_callback)
            atexit.register(_runtime_worker.close)
        worker = _runtime_worker
        if _commit is not None:
            _runtime_callbacks[token] = context
    try:
        return worker.call('discovery.' + rpc_method, callback_token=token, **arguments)
    except RustAuthorityError as exc:
        if context['error'] is not None:
            raise context['error']
        raise DeviceError(str(exc)) from None
    finally:
        with _runtime_lock:
            _runtime_callbacks.pop(token, None)


def encoded(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decoded(data: str) -> bytes:
    try:
        return base64.b64decode(data, validate=True)
    except (ValueError, TypeError) as exc:
        raise DeviceError("INVALID_ENCODING") from exc


def fingerprint(public_key: str) -> str:
    return _state_call('fingerprint', value=public_key)


def valid_name(name: str) -> str:
    return _state_call('name', value=name)


def valid_node(node_id: str) -> str:
    return _state_call('node', value=node_id)


def public_record(raw: dict[str, Any]) -> dict[str, Any]:
    return _state_call('public', value=raw)


def certificate_public(pem: str) -> str:
    return _state_call('certificate', value=pem)


class PrivateStore:
    """Existing owner-only file API; Rust performs all storage and lock IO."""
    def __init__(self, root: Path):
        self.root = root.expanduser().absolute()
        _state_call('store.open', root=str(self.root))

    @staticmethod
    def _check(path: Path, *, directory: bool = False) -> None:
        _state_call('store.check', path=str(path), directory=directory)

    @contextlib.contextmanager
    def lock(self, name: str = 'store.lock', *, nonblocking: bool = False) -> Iterator[None]:
        handle = _state_call('store.lock', root=str(self.root), name=name, nonblocking=nonblocking)
        try:
            yield
        finally:
            _state_call('store.unlock', lock=handle)

    def read(self, name: str) -> dict[str, Any] | None:
        return _state_call('store.read', root=str(self.root), name=name)

    def write(self, name: str, raw: dict[str, Any]) -> None:
        _state_call('store.write', root=str(self.root), name=name, value=raw)


class NodeIdentity:
    def __init__(self, store: PrivateStore, host: str, display_name: str | None = None):
        self.store = store
        host_id = valid_node(normalize_host(host).stable_identifier)
        if type(store) is PrivateStore:
            raw = _state_call('identity.open', root=str(store.root), host=host_id, display_name=display_name)
            self.public, self.certificate = raw['public'], raw['certificate']
        else:
            # Preserve explicit read/write qualification seams; crypto and
            # record validation still execute the same Rust implementation.
            with store.lock():
                raw = store.read('identity.json')
                if raw is None:
                    raw = _state_call('identity.generate', host=host_id, display_name=display_name)
                    store.write('identity.json', raw)
                self.public = _state_call('identity.validate', value=raw, host=host_id)
                self.certificate = raw['certificate']

    @property
    def _private_pem(self):
        # Explicit Python SSL qualification only. Normal production TLS reads
        # and uses this key entirely inside the Rust process.
        return _state_call('identity.credentials', root=str(self.store.root))['private_key']

    @property
    def host_identity(self) -> HostIdentity:
        return normalize_host(
            self.public["node_id"],
            display=self.public["display_name"],
            machine_id=self.public["fingerprint"],
        )

    def load_tls_credentials(self, context: Any) -> None:
        # OpenSSL's path API reads private process pipes through /dev/fd.
        # No transient PEM file is staged on disk (Linux/macOS POSIX runtime).
        readers: list[int] = []
        try:
            for content in (self.certificate, self._private_pem):
                read_fd, write_fd = os.pipe()
                readers.append(read_fd)
                try:
                    data = content.encode("ascii")
                    os.set_blocking(write_fd, False)
                    if len(data) > 4096:
                        raise DeviceError("TLS_CREDENTIAL_TOO_LARGE")
                    if os.write(write_fd, data) != len(data):
                        raise DeviceError("TLS_CREDENTIAL_LOAD_FAILED")
                finally:
                    os.close(write_fd)
            context.load_cert_chain(*(f"/dev/fd/{fd}" for fd in readers))
        finally:
            for fd in readers:
                os.close(fd)


class TrustedPeerStore:
    """Rust validates and atomically commits the existing peer trust schema."""
    def __init__(self, store: PrivateStore):
        self.store = store

    def all(self) -> dict[str, dict[str, Any]]:
        # Snapshot read only: even ReadOnlyPrivateStore must not create a lock.
        return _state_call('peers.all', root=str(self.store.root))

    def _read(self) -> dict[str, dict[str, Any]]:
        return _state_call('peers.validate', value=self.store.read('peers.json'))

    def _transition(self, operation, **arguments):
        if type(self.store) is PrivateStore:
            return _state_call('peers.' + operation, root=str(self.store.root), **arguments)
        # Custom IO stores retain their write refusal/qualification boundary;
        # the same Rust transition validates the complete locked snapshot.
        with self.store.lock():
            reply = _state_call('peers.transition', operation=operation, arguments=arguments,
                                state=self.store.read('peers.json'))
            self.store.write('peers.json', reply['state'])
            return reply['value']

    def trust(self, public: dict[str, Any], certificate: str, *, security_status: str,
              pairing_protocol: str | None = None, fresh_pairing: bool = False) -> None:
        return self._transition('trust', public=public, certificate=certificate,
            security_status=security_status, pairing_protocol=pairing_protocol, fresh_pairing=fresh_pairing)

    def authenticate(self, node_id: str, public_key: str, *, allow_dev: bool = False) -> dict[str, Any]:
        return self._transition('authenticate', node_id=node_id, public_key=public_key, allow_dev=allow_dev)

    def revoke(self, node_id: str) -> None:
        return self._transition('revoke', node_id=node_id)
