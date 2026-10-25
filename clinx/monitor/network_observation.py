"""Private clients for the Rust observation store, queries and projections.

Python supplies existing identity/configuration objects and opaque remote calls.
It owns no observation SQL, cursor validation or presentation state machine.
"""

from __future__ import annotations

import contextlib
import json
import os
import weakref
from pathlib import Path

from clinx.authority.rust_authority import RustAuthorityError
from clinx.nodes.network_worker import Worker, read_command
from clinx.nodes.node_protocol import NodeProtocolError, SharingScope

VERSION = "clinx-observation-v1"
MAX_BATCH = 32
TERMINAL = {
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "TIMED_OUT",
    "INTERRUPTED",
    "DISCONNECTED",
}


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _error(exc):
    code = str(exc)
    reason = {
        "OBSERVATION_SEQUENCE_CONFLICT": "Same sequence has different content",
        "INVALID_NATIVE_TURN_MODEL": "Invalid native turn model identity",
        "INVALID_OBSERVATION_PROOF": "Live proof requires exact thread, turn and timestamp",
    }.get(code, code)
    return NodeProtocolError(
        "INVALID_OBSERVATION"
        if code in {"INVALID_NATIVE_TURN_MODEL", "INVALID_OBSERVATION_PROOF"}
        else code,
        reason,
    )


def _read(operation, **arguments):
    try:
        return read_command("observation." + operation, **arguments)
    except RustAuthorityError as exc:
        raise _error(exc) from None


def observation_id(node, user, provider, thread):
    return _read("id", node=node, user=user, provider=provider, thread=thread)


def clean(value, limit=1000):
    return _read("clean", value=value, limit=limit)


def validate_event(event, node, scope):
    return _read("validate", event=event, node=node, scope=scope.as_dict())


class ObservationWriter:
    def __init__(self, path, lifecycle=None):
        self.path, self.lifecycle = Path(path).resolve(), lifecycle
        _read("init", path=str(self.path))

    def accept(self, node, scope, request):
        result = _read(
            "accept",
            path=str(self.path),
            node=node,
            scope=scope.as_dict(),
            request=request,
        )
        if result.get("accepted") and self.lifecycle:
            # Existing Authority registration callback is idempotent and follows
            # the committed observation receipt; no task writes occur here.
            result["canonical_registration"] = self.lifecycle.accepted(
                node, scope, request["events"]
            )
        return result


class ObservationDirectory:
    def __init__(
        self,
        path,
        registry,
        approvals,
        peers,
        user_scope,
        remote_factory=None,
        local_identity=None,
        native_home=None,
        canonical_path=None,
    ):
        self.path, self.registry, self.approvals, self.peers = (
            Path(path),
            registry,
            approvals,
            peers,
        )
        self.user_scope, self.remote_factory = user_scope, remote_factory
        self.local_identity, self.native_home, self.canonical_path = (
            local_identity,
            native_home,
            canonical_path,
        )
        reference = weakref.ref(self)

        def callback(event):
            directory = reference()
            if directory is None:
                raise NodeProtocolError(
                    "OBSERVATION_UNAVAILABLE", "Directory is closed"
                )
            return directory._callback(event)

        self.worker = Worker(callback=callback)

    def _callback(self, event):
        operation, args = event["operation"], event["arguments"]
        if operation == "directory.nodes":
            return [
                r.as_dict()
                for r in self.registry.list_nodes(user_scope=self.user_scope)
            ]
        if operation == "directory.node":
            node = args["node_id"]
            record = self.registry.get(node)
            scope = self.registry.scope(node, self.user_scope)
            approval = self.approvals.get(node)
            return dict(
                record=record.as_dict() if record else None,
                scope=scope.as_dict() if scope else None,
                approval=approval.as_dict() if approval else None,
                peer=self.peers.all().get(node),
            )
        if operation == "directory.remote" and self.remote_factory:
            from clinx.nodes.node_protocol import NodeRecord

            return self.remote_factory(
                NodeRecord(
                    **{
                        k: v
                        for k, v in args["record"].items()
                        if k not in {"stale", "age_seconds"}
                    }
                )
            ).call(args["request"])
        raise NodeProtocolError(
            "OBSERVATION_CALLBACK_INVALID", "Unexpected read callback"
        )

    def _read(self, operation, request=None, **arguments):
        try:
            return self.worker.call(
                "observation." + operation,
                path=str(self.path.resolve()),
                user_scope=self.user_scope,
                local_identity=self.local_identity,
                native_home=str(self.native_home) if self.native_home else None,
                canonical_path=str(self.canonical_path)
                if self.canonical_path
                else None,
                remote=bool(self.remote_factory),
                request=request or {},
                **arguments,
            )
        except RustAuthorityError as exc:
            raise _error(exc) from None

    def _scope(self, node):
        value = self._read("scope", node=node)
        return SharingScope(**value) if value else None

    def list(
        self,
        *,
        node=None,
        project=None,
        state=None,
        kind=None,
        native_thread_id=None,
        cursor=None,
        limit=50,
    ):
        return self._read(
            "list",
            dict(
                node=node,
                project=project,
                state=state,
                kind=kind,
                native_thread_id=native_thread_id,
                cursor=cursor,
                limit=limit,
            ),
        )

    def detail(self, observation_id, *, cursor=None, limit=32):
        return self._read(
            "detail", dict(observation_id=observation_id, cursor=cursor, limit=limit)
        )

    def activity(self, observation_id, *, cursor=None, limit=50):
        return self._read(
            "activity", dict(observation_id=observation_id, cursor=cursor, limit=limit)
        )

    def context(self, observation_id, *, cursor=None):
        return self._read("context", dict(observation_id=observation_id, cursor=cursor))

    def close(self):
        if hasattr(self, "worker"):
            self.worker.close()

    def __del__(self):
        self.close()


def directory_from_env():
    """Explicit shared configuration; no identity creation or writable locks."""
    import threading

    from clinx.discovery.identity import (
        DeviceError,
        NodeIdentity,
        PrivateStore,
        TrustedPeerStore,
    )
    from clinx.nodes.node_protocol import NodeAuthorizationStore
    from clinx.nodes.node_runtime import AuthorizedRemoteClient, PairedTLS

    path = os.environ.get("CLINX_OBSERVATION_DB")
    if not path:
        return None

    class ReadOnlyPrivateStore(PrivateStore):
        def __init__(self, root):
            self.root = Path(root).expanduser().absolute()
            self._check(self.root, directory=True)

        @contextlib.contextmanager
        def lock(self, *args, **kwargs):
            # Owner atomically replaces complete JSON files. Reads validate each
            # new snapshot without creating a lock, identity or registry.
            yield

        def write(self, *args, **kwargs):
            raise DeviceError("READ_ONLY_OBSERVATION_IDENTITY")

    state = Path(os.environ["CLINX_OBSERVATION_STATE"])
    private = ReadOnlyPrivateStore(state)
    registry = NodeRegistryReadOnly(os.environ["CLINX_OBSERVATION_REGISTRY"])
    peers = TrustedPeerStore(private)
    approvals = NodeAuthorizationStore.__new__(NodeAuthorizationStore)
    approvals._store = private
    approvals._lock = threading.RLock()
    approvals.filename = "sharing.json"

    def remote(record):
        if private.read("identity.json") is None:
            raise NodeProtocolError(
                "PAIRING_REQUIRED", "Existing centre identity is required"
            )
        identity = NodeIdentity(private, os.environ["CLINX_OBSERVATION_NODE_ID"])
        return AuthorizedRemoteClient(
            PairedTLS(identity, peers), registry, approvals, record.node_id
        )

    from clinx.discovery.identity import public_record

    identity_raw = private.read("identity.json")
    local_identity = public_record(identity_raw) if identity_raw else None
    return ObservationDirectory(
        path,
        registry,
        approvals,
        peers,
        os.environ["CLINX_OBSERVATION_USER_SCOPE"],
        remote,
        local_identity,
        os.environ.get("CLINX_OBSERVATION_NATIVE_HOME") or Path.home() / ".codex",
        canonical_path=os.environ.get("CLINX_CANONICAL_TASK_DB"),
    )


class NodeRegistryReadOnly:
    """Rust mode=ro reads; never initialize a registry or acquire a writer lock."""

    def __init__(self, path):
        self.path = Path(path)

    def _read(self, method, **arguments):
        try:
            return read_command(
                "registry.read",
                path=str(self.path),
                method="registry." + method,
                arguments=arguments,
            )
        except RustAuthorityError as exc:
            raise _error(exc) from None

    def get(self, node):
        from clinx.nodes.node_protocol import NodeRecord

        value = self._read("get", node_id=node)
        return NodeRecord(**value) if value else None

    def list_nodes(self, *, user_scope=None):
        from clinx.nodes.node_protocol import NodeRecord

        return [
            NodeRecord(**r) for r in self._read("list_nodes", user_scope=user_scope)
        ]

    def scope(self, node, user):
        value = self._read("scope", node_id=node, user_scope=user)
        return SharingScope(**value) if value else None
