"""Private clients for Rust native read checkpoints and remote observation policy.

Python binds the existing directory/source objects and fixed loopback HTTP codec.
Checkpoint identity, signatures, scope rechecks, cooldown and remote evidence
validation are owned by the companion Rust Node process.
"""

import os
import threading
import weakref
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from clinx.authority.rust_authority import RustAuthorityError
from clinx.monitor.observation_source import CodexObservationSource
from clinx.nodes.network_worker import Worker
from clinx.nodes.node_protocol import NodeProtocolError, SharingScope


class _Client:
    def __init__(self, callback=None):
        self._worker = None
        self._lock = threading.Lock()
        self._handler = callback

    def _callback(self, event):
        if self._handler is None:
            raise NodeProtocolError(
                "NATIVE_OBSERVER_CALLBACK_INVALID", "Unexpected callback"
            )
        return self._handler(event)

    def _call(self, operation, **arguments):
        with self._lock:
            if self._worker is None:
                import atexit

                reference = weakref.ref(self)

                def callback(event):
                    client = reference()
                    if client is None:
                        raise NodeProtocolError(
                            "NATIVE_OBSERVER_UNAVAILABLE", "Client closed"
                        )
                    return client._callback(event)

                self._worker = Worker(callback=callback)
                atexit.register(self._worker.close)
            worker = self._worker
        try:
            return worker.call("native_observer." + operation, **arguments)
        except RustAuthorityError as exc:
            if str(exc) == "INVALID_THREAD_ID":
                from clinx.contracts.thread_identity import ThreadLookupError

                raise ThreadLookupError(str(exc), str(exc)) from None
            raise NodeProtocolError(str(exc), str(exc)) from None

    def close(self):
        if self._worker is not None:
            self._worker.close()


class NativeCollectorBridge(_Client):
    def __init__(self, directory):
        super().__init__()
        self.directory, self.source = directory, None

    def _source(self):
        if self.source is None:
            self.source = CodexObservationSource(
                self.directory.native_home,
                self.directory.local_identity["node_id"],
                native_writer_observation=True,
            )
        return self.source

    def _callback(self, event):
        operation, args = event["operation"], event["arguments"]
        if operation == "native_observer.facts":
            d = self.directory
            identity = d.local_identity if d else None
            scope = (
                d._scope(identity["node_id"]) if identity and d.native_home else None
            )
            return dict(
                local_identity=identity,
                native_home=str(d.native_home) if d and d.native_home else None,
                scope=scope.as_dict() if scope else None,
            )
        source = self._source()
        if operation == "native_observer.generation":
            return source.generation
        if operation == "native_observer.metadata":
            return source.history.metadata(args["tid"])
        if operation == "native_observer.inventory":
            return source.inventory(
                SharingScope(**args["scope"]), args["after"], audit=args["audit"]
            )
        if operation == "native_observer.sample":
            return source.sample(args["metadata"], args["state"])
        raise NodeProtocolError(
            "NATIVE_OBSERVER_CALLBACK_INVALID", "Unexpected native read callback"
        )

    @property
    def secret(self):
        return bytes.fromhex(self._call("key"))

    @secret.setter
    def secret(self, value):
        self._call("key", value=value.hex())

    def scope(self):
        scope = SharingScope(**self._call("scope"))
        self._source()
        return scope

    def binding(self, tid, scope):
        return self._call("binding", tid=tid, scope=scope.as_dict())

    def inventory(self, *, after=None, audit=False):
        return self._call("inventory", after=after, audit=audit)

    def sample(self, tid, checkpoint=None):
        return self._call("sample", tid=tid, checkpoint=checkpoint)

    def request(self, route, query):
        return self._call("route", route=route, query=query)


class RemoteNativeSource(_Client):
    def __init__(self, credential_file):
        super().__init__()
        self.credential_file = Path(credential_file)

    @classmethod
    def from_env(cls):
        value = os.environ.get("CLINX_NATIVE_OBSERVER_TOKEN_FILE")
        return cls(value) if value else None

    def _callback(self, event):
        operation, args = event["operation"], event["arguments"]
        if operation == "native_observer.read":
            return self.read(args["path"], args["params"])
        if operation != "native_observer.http":
            raise NodeProtocolError(
                "NATIVE_OBSERVER_CALLBACK_INVALID", "Unexpected HTTP callback"
            )
        try:
            token = self.credential_file.read_text().strip()
            request = Request(
                "http://127.0.0.1:8766/v2/native-collector/"
                + args["path"]
                + "?"
                + urlencode(args["params"] or {}),
                headers={"Authorization": "Bearer " + token},
            )
            with urlopen(request, timeout=8) as response:
                return dict(
                    status=200, body=response.read(1024 * 1024 + 1).decode("utf-8")
                )
        except HTTPError as exc:
            try:
                return dict(status=exc.code, body=exc.read(4096).decode("utf-8"))
            except (ValueError, OSError):
                return dict(transport_error=True)
        except (OSError, URLError, ValueError):
            return dict(transport_error=True)

    def read(self, path, params=None):
        return self._call("remote_read", path=path, params=params or {})

    def inventory(self, scope, after=None, *, audit=False):
        return self._call(
            "remote_inventory", scope=scope.as_dict(), after=after, audit=audit
        )

    def native_turns(self, metadata, state):
        turns, coverage, updated = self._call(
            "remote_turns", metadata=metadata, state=state
        )
        state.clear()
        state.update(updated)
        return turns, coverage
