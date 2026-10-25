"""Restricted, versioned device credentials and atomic peer trust records.

Reuse HostIdentity.stable_identifier as node_id. Addresses never enter identity
or authority. Existing task/routing databases are deliberately not opened here.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import fcntl
import json
import os
import stat
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Iterator

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from clinx.contracts.execution_semantics import HostIdentity, normalize_host

from . import PROTOCOL_VERSION


class DeviceError(ValueError):
    """Safe, fixed error codes only; never echo untrusted wire values or PINs."""


_runtime_lock = threading.Lock()
_runtime_worker = None
_runtime_callbacks = {}


def _state_callback(event):
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
    def __init__(self, root: Path):
        self.root = root.expanduser().absolute()
        if self.root.is_symlink():
            raise DeviceError("UNSAFE_STATE_DIRECTORY")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check(self.root, directory=True)

    @staticmethod
    def _check(path: Path, *, directory: bool = False) -> None:
        info = path.lstat()
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise DeviceError("UNSAFE_STATE_PERMISSIONS")

    @contextlib.contextmanager
    def lock(self, name: str = "store.lock", *, nonblocking: bool = False) -> Iterator[None]:
        path = self.root / name
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            self._check(path)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
            except BlockingIOError as exc:
                raise DeviceError("DEVICE_ALREADY_SERVING") from exc
            yield
        finally:
            os.close(fd)

    def read(self, name: str) -> dict[str, Any] | None:
        path = self.root / name
        if not path.exists() and not path.is_symlink():
            return None
        self._check(path)
        if path.stat().st_size > 2_000_000:
            raise DeviceError("STATE_TOO_LARGE")
        try:
            raw = json.loads(path.read_text())
            if not isinstance(raw, dict) or raw.get("schema_version") != 1:
                raise DeviceError("UNSUPPORTED_STATE_SCHEMA")
            return raw
        except (ValueError, TypeError) as exc:
            raise DeviceError("INVALID_STATE") from exc

    def write(self, name: str, raw: dict[str, Any]) -> None:
        target = self.root / name
        if target.exists() or target.is_symlink():
            self._check(target)
        fd, tmp = tempfile.mkstemp(prefix=".atomic-", dir=self.root)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(raw, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, target)
            parent = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


class NodeIdentity:
    def __init__(self, store: PrivateStore, host: str, display_name: str | None = None):
        self.store = store
        host_id = valid_node(normalize_host(host).stable_identifier)
        with store.lock():
            raw = store.read("identity.json")
            if raw is None:
                key = Ed25519PrivateKey.generate()
                pub = encoded(
                    key.public_key().public_bytes(
                        serialization.Encoding.Raw, serialization.PublicFormat.Raw
                    )
                )
                now = dt.datetime.now(dt.timezone.utc)
                subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host_id)])
                cert = (
                    x509.CertificateBuilder()
                    .subject_name(subject)
                    .issuer_name(subject)
                    .public_key(key.public_key())
                    .serial_number(x509.random_serial_number())
                    .not_valid_before(now - dt.timedelta(minutes=5))
                    .not_valid_after(now + dt.timedelta(days=3650))
                    .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                    .sign(key, algorithm=None)
                )
                raw = dict(
                    schema_version=1,
                    node_id=host_id,
                    display_name=valid_name(display_name or host_id),
                    protocol_version=PROTOCOL_VERSION,
                    public_key=pub,
                    fingerprint=fingerprint(pub),
                    created_at=now.isoformat(),
                    private_key=key.private_bytes(
                        serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption(),
                    ).decode(),
                    certificate=cert.public_bytes(serialization.Encoding.PEM).decode(),
                )
                store.write("identity.json", raw)
            self.public = public_record(raw)
            if self.public["node_id"] != host_id:
                raise DeviceError("CONFIGURED_HOST_IDENTITY_CHANGED")
            self._private_pem = raw["private_key"]
            self.certificate = raw["certificate"]
            loaded_key = serialization.load_pem_private_key(
                self._private_pem.encode(), password=None
            )
            if not isinstance(loaded_key, Ed25519PrivateKey):
                raise DeviceError("INVALID_PRIVATE_KEY")
            actual = encoded(
                loaded_key.public_key().public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw
                )
            )
            if (
                actual != self.public["public_key"]
                or certificate_public(self.certificate) != actual
            ):
                raise DeviceError("LOCAL_KEY_MISMATCH")

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
