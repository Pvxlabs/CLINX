"""Restricted, versioned device credentials and atomic peer trust records.

Reuse HostIdentity.stable_identifier as node_id. Addresses never enter identity
or authority. Existing task/routing databases are deliberately not opened here.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterator

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.x509.oid import NameOID

from execution_semantics import HostIdentity, normalize_host

from . import PROTOCOL_VERSION


class DeviceError(ValueError):
    """Safe, fixed error codes only; never echo untrusted wire values or PINs."""


def encoded(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decoded(data: str) -> bytes:
    try:
        return base64.b64decode(data, validate=True)
    except (ValueError, TypeError) as exc:
        raise DeviceError("INVALID_ENCODING") from exc


def fingerprint(public_key: str) -> str:
    raw = decoded(public_key)
    if len(raw) != 32:
        raise DeviceError("INVALID_PUBLIC_KEY")
    return hashlib.sha256(raw).hexdigest()


def valid_name(name: str) -> str:
    if not isinstance(name, str) or not 1 <= len(name.encode("utf-8")) <= 120:
        raise DeviceError("INVALID_DISPLAY_NAME")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise DeviceError("INVALID_DISPLAY_NAME")
    return name


def valid_node(node_id: str) -> str:
    if not isinstance(node_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,62}", node_id):
        raise DeviceError("INVALID_NODE_ID")
    # Preserve the existing routing normalization; don't silently rename peers.
    if normalize_host(node_id).stable_identifier != node_id:
        raise DeviceError("NON_CANONICAL_NODE_ID")
    if node_id.replace(".", "").isdigit():
        raise DeviceError("ADDRESS_IS_NOT_IDENTITY")
    return node_id


def public_record(raw: dict[str, Any]) -> dict[str, Any]:
    try:
        node_id = valid_node(raw["node_id"])
        name = valid_name(raw["display_name"])
        fp = fingerprint(raw["public_key"])
        if raw["fingerprint"] != fp or raw["protocol_version"] != PROTOCOL_VERSION:
            raise DeviceError("IDENTITY_OR_PROTOCOL_MISMATCH")
        return dict(
            node_id=node_id,
            display_name=name,
            public_key=raw["public_key"],
            fingerprint=fp,
            protocol_version=PROTOCOL_VERSION,
        )
    except (KeyError, TypeError) as exc:
        raise DeviceError("INVALID_IDENTITY") from exc


def certificate_public(pem: str) -> str:
    try:
        cert = x509.load_pem_x509_certificate(pem.encode("ascii"))
        now = dt.datetime.now(dt.timezone.utc)
        if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
            raise DeviceError("CERTIFICATE_EXPIRED_OR_NOT_YET_VALID")
        key = cert.public_key()
        if not isinstance(key, Ed25519PublicKey):
            raise DeviceError("INVALID_CERTIFICATE_KEY")
        key.verify(cert.signature, cert.tbs_certificate_bytes)
        return encoded(key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    except (ValueError, TypeError, UnicodeError) as exc:
        raise DeviceError("INVALID_CERTIFICATE") from exc


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
        paths: list[str] = []
        try:
            for content in (self.certificate, self._private_pem):
                fd, path = tempfile.mkstemp(prefix=".tls-", dir=self.store.root)
                paths.append(path)
                with os.fdopen(fd, "w") as stream:
                    stream.write(content)
            context.load_cert_chain(paths[0], paths[1])
        finally:
            for path in paths:
                os.unlink(path)


class TrustedPeerStore:
    def __init__(self, store: PrivateStore):
        self.store = store

    def all(self) -> dict[str, dict[str, Any]]:
        with self.store.lock():
            return self._read()

    def _read(self) -> dict[str, dict[str, Any]]:
        raw = self.store.read("peers.json")
        if raw is None:
            return {}
        peers = raw.get("peers")
        if not isinstance(peers, dict):
            raise DeviceError("INVALID_PEER_STORE")
        for node_id, peer in peers.items():
            pub = public_record(peer)
            if pub["node_id"] != node_id or peer.get("trust_state") not in {"TRUSTED", "REVOKED"}:
                raise DeviceError("INVALID_PEER_STORE")
            if certificate_public(peer["certificate"]) != pub["public_key"]:
                raise DeviceError("PEER_KEY_MISMATCH")
            if peer.get("security_status") not in {"DEV_ONLY", "QUALIFIED"}:
                raise DeviceError("INVALID_PEER_SECURITY_STATUS")
        return peers

    def trust(self, public: dict[str, Any], certificate: str, *, security_status: str) -> None:
        peer = public_record(public)
        if security_status not in {"DEV_ONLY", "QUALIFIED"}:
            raise DeviceError("INVALID_PEER_SECURITY_STATUS")
        if certificate_public(certificate) != peer["public_key"]:
            raise DeviceError("PEER_KEY_MISMATCH")
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with self.store.lock():
            peers = self._read()
            old = peers.get(peer["node_id"])
            if any(
                n != peer["node_id"] and p["public_key"] == peer["public_key"]
                for n, p in peers.items()
            ):
                raise DeviceError("PUBLIC_KEY_ALREADY_BOUND")
            if old and (old["public_key"] != peer["public_key"] or old["trust_state"] == "REVOKED"):
                raise DeviceError("IDENTITY_MISMATCH")
            peers[peer["node_id"]] = dict(
                peer,
                certificate=certificate,
                friendly_name=peer["display_name"],
                trust_state="TRUSTED",
                security_status=security_status,
                paired_at=old["paired_at"] if old else now,
                updated_at=now,
                last_authenticated_at=old.get("last_authenticated_at") if old else None,
            )
            self.store.write("peers.json", dict(schema_version=1, peers=peers))

    def authenticate(
        self, node_id: str, public_key: str, *, allow_dev: bool = False
    ) -> dict[str, Any]:
        with self.store.lock():
            peers = self._read()
            peer = peers.get(node_id)
            if not peer or peer["trust_state"] != "TRUSTED":
                raise DeviceError("UNTRUSTED_DEVICE")
            if peer["public_key"] != public_key:
                raise DeviceError("IDENTITY_MISMATCH")
            if peer["security_status"] == "DEV_ONLY" and not allow_dev:
                raise DeviceError("SECURITY_BLOCKER_DEV_ONLY")
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            peer.update(last_authenticated_at=now, updated_at=now)
            self.store.write("peers.json", dict(schema_version=1, peers=peers))
            return peer

    def revoke(self, node_id: str) -> None:
        with self.store.lock():
            peers = self._read()
            if node_id not in peers:
                raise DeviceError("UNKNOWN_DEVICE")
            peers[node_id]["trust_state"] = "REVOKED"
            peers[node_id]["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            self.store.write("peers.json", dict(schema_version=1, peers=peers))
