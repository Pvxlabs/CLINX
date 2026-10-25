"""CLINX Monitor v1: a read-only projection of the canonical registry.

No TaskRegistry construction, migrations, reconciliation or business writes.
Optional exact native reads use an existing daemon with a read-only client.
Run separately from the authority. The HTTP listener is always loopback-only.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1"
TASK_LIMIT, EVENT_LIMIT, OP_LIMIT = 50, 100, 20
MAX_RESPONSE = 2 * 1024 * 1024


class APIError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def now():
    return datetime.now(timezone.utc).isoformat()


def _legacy_command(operation, **arguments):
    from clinx.monitor.native_observation_bridge import _Client
    from clinx.nodes.node_protocol import NodeProtocolError

    client = _Client()
    try:
        return client._call("legacy_" + operation, **arguments)
    except NodeProtocolError as exc:
        raise APIError(400 if exc.code == "INVALID_CURSOR" else 503, exc.code) from None
    finally:
        client.close()


def safe_text(value, limit=256):
    return _legacy_command("safe_text", value=value, limit=limit)


def object_json(value):
    return _legacy_command("object", value=value)


def timestamp(value):
    return _legacy_command("timestamp", value=value)


def elapsed(start, end):
    return _legacy_command("elapsed", start=start, end=end)


def menu_state(state, running, retry, result):
    return _legacy_command(
        "menu", state=state, running=running, retry=retry, result=result
    )


# Exact native activity reads, cursor identity and redaction are owned by Rust.
ACTIVITY_LIMIT, ACTIVITY_TEXT_LIMIT = 40, 16384


def _activity_command(operation, **arguments):
    from clinx.monitor.native_observation_bridge import _Client
    from clinx.nodes.node_protocol import NodeProtocolError

    client = _Client()
    try:
        return client._call(operation, **arguments)
    except NodeProtocolError as exc:
        raise APIError(
            409 if exc.code == "ACTIVITY_CURSOR_RESET_REQUIRED" else 503, exc.code
        ) from None
    finally:
        client.close()


def activity_text(value):
    return _activity_command("activity_text", value=value)


class ActivityReader:
    """Private process client; native history decisions and reads live in Rust."""

    def __init__(self, root):
        from clinx.monitor.native_observation_bridge import _Client

        self.root = Path(root).expanduser().resolve()
        self._client = _Client()

    @property
    def generation(self):
        return self._client._call("activity_generation")

    @staticmethod
    def encode(scope, mode, position):
        return _activity_command(
            "activity_encode", scope=scope, mode=mode, position=position
        )

    @staticmethod
    def decode(value, scope, mode):
        return tuple(
            _activity_command("activity_decode", value=value, scope=scope, mode=mode)
        )

    def read(self, task_ref, execution, after=None, before=None):
        from clinx.nodes.node_protocol import NodeProtocolError

        try:
            return self._client._call(
                "activity_read",
                root=str(self.root),
                task_ref=task_ref,
                execution=execution,
                after=after,
                before=before,
            )
        except NodeProtocolError as exc:
            raise APIError(
                409 if exc.code == "ACTIVITY_CURSOR_RESET_REQUIRED" else 503, exc.code
            ) from None

    def close(self):
        self._client.close()


class ObserverStore:
    """Rust historical read projection; no Python task-state interpretation."""

    def __init__(self, path, native_home=None, *, codex_host_ids=None):
        from clinx.monitor.native_observation_bridge import _Client

        self.path = Path(path).expanduser().resolve()
        self.activity_reader = ActivityReader(native_home or Path.home() / ".codex")
        self.codex_host_ids = dict(codex_host_ids or {})
        self.observation_directory = None
        self._client = _Client(self._callback)

    @contextlib.contextmanager
    def snapshot(self):
        # Explicit read-only inspection compatibility. Normal Observer requests
        # open and query their snapshots inside Rust, without this Python handle.
        conn = sqlite3.connect(
            self.path.as_uri() + "?mode=ro",
            uri=True,
            timeout=0.25,
            check_same_thread=False,
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            steps = [0]

            def budget():
                steps[0] += 1000
                return steps[0] > 10_000_000

            conn.set_progress_handler(budget, 1000)
            conn.execute("BEGIN")
            yield conn
        finally:
            conn.close()

    def _callback(self, event, connection=None):
        from clinx.nodes.node_protocol import NodeProtocolError

        operation, args = event["operation"], event["arguments"]
        if operation == "legacy.codex_uri":
            from clinx.contracts.thread_identity import canonical_codex_uri

            return canonical_codex_uri(args["thread_id"], host_id=args["host_id"])
        if operation == "legacy.grant":
            directory = self.observation_directory
            if directory is None:
                return dict(scope=None, record=None, user_scope=None)
            scope = directory._scope(args["node_id"])
            record = directory.registry.get(args["node_id"])
            return dict(
                scope=scope.as_dict() if scope else None,
                record=dict(public_key_fingerprint=record.public_key_fingerprint)
                if record
                else None,
                user_scope=directory.user_scope,
            )
        if operation == "legacy.sql" and connection is not None:
            try:
                return [
                    dict(row)
                    for row in connection.execute(args["sql"], args["arguments"])
                ]
            except sqlite3.Error:
                raise NodeProtocolError(
                    "LEGACY_SOURCE_UNAVAILABLE", "Read snapshot unavailable"
                ) from None
        raise NodeProtocolError(
            "NATIVE_OBSERVER_CALLBACK_INVALID", "Unexpected historical read callback"
        )

    def _call(self, operation, connection=None, **arguments):
        from clinx.monitor.native_observation_bridge import _Client
        from clinx.nodes.node_protocol import NodeProtocolError

        client = (
            self._client
            if connection is None
            else _Client(lambda event: self._callback(event, connection))
        )
        try:
            return client._call(
                "legacy_" + operation,
                path=str(self.path),
                codex_host_ids=self.codex_host_ids,
                external_snapshot=connection is not None,
                **arguments,
            )
        except NodeProtocolError as exc:
            if exc.code in {
                "LEGACY_SOURCE_UNAVAILABLE",
                "NATIVE_HISTORY_BUDGET_EXHAUSTED",
            }:
                raise sqlite3.OperationalError(exc.code) from None
            status = {
                "TASK_NOT_FOUND": 404,
                "INVALID_CURSOR": 400,
                "CURSOR_RESET_REQUIRED": 409,
                "ACTIVITY_EXECUTION_CHANGED": 409,
            }.get(exc.code)
            if status:
                raise APIError(status, exc.code) from None
            raise
        finally:
            if connection is not None:
                client.close()

    def health(self):
        return self._call("health")

    def native_visible(self, conn, task_ref):
        return self._call("visible", conn, task_ref=task_ref)

    def task_row(self, conn, task_ref):
        return self._call("task_row", conn, task_ref=task_ref)

    @staticmethod
    def event_cursor(task_ref, cursor):
        return _legacy_command("event_cursor", task_ref=task_ref, cursor=cursor)

    @classmethod
    def parse_cursor(cls, task_ref, after):
        return _legacy_command("parse_cursor", task_ref=task_ref, after=after)

    def events(self, conn, task_ref, after=None, recent=False):
        return self._call("events", conn, task_ref=task_ref, after=after, recent=recent)

    def project(self, conn, task, observed):
        return self._call("project", conn, task=task, observed=observed)

    def task(self, task_ref):
        return self._call("task", task_ref=task_ref)

    def list_tasks(self, state, offset=0):
        return self._call("list", state=state, offset=offset)

    def task_activity(self, task_ref, execution_ref, after=None, before=None):
        execution = self._call(
            "activity_execution", task_ref=task_ref, execution_ref=execution_ref
        )
        return self.activity_reader.read(task_ref, execution, after, before)

    def task_events(self, task_ref, after):
        return self._call("task_events", task_ref=task_ref, after=after)

    def close(self):
        self._client.close()
        self.activity_reader.close()


class _HTTPClient:
    """Private process pipe; HTTP parsing, auth, routes and sockets live in Rust."""

    def __init__(self, callback):
        self._handler = callback
        self._worker = None
        self._lock = threading.Lock()

    def call(self, operation, **arguments):
        with self._lock:
            if self._worker is None:
                import atexit
                import weakref

                from clinx.nodes.network_worker import Worker

                reference = weakref.ref(self)

                def callback(event):
                    client = reference()
                    if client is None:
                        raise RuntimeError("OBSERVER_HTTP_UNAVAILABLE")
                    return client._handler(event)

                self._worker = Worker(callback=callback)
                atexit.register(self._worker.close)
            worker = self._worker
        return worker.call("observer_http." + operation, **arguments)

    def close(self):
        with self._lock:
            if self._worker is not None:
                self._worker.close()
                self._worker = None


def _decimal_argument(value):
    # JSON has no arbitrary precision integer type. Decode the Rust wire tag
    # without applying any route or read-limit policy in the Python binding.
    if isinstance(value, dict) and set(value) == {"__observer_decimal"}:
        return int(value["__observer_decimal"])
    return value


class ObserverAPI:
    def __init__(self, store, credential, observations=None, network_observations=None):
        self.store, self.credential = store, credential
        self.observations = observations
        self.network_observations = (
            network_observations if network_observations is not None else observations
        )
        self.store.observation_directory = observations
        self.native_collector = None
        self._http = _HTTPClient(self._callback)

    def _callback(self, event):
        from clinx.authority.rust_authority import RustAuthorityError
        from clinx.contracts.thread_identity import ThreadLookupError
        from clinx.nodes.node_protocol import NodeProtocolError

        try:
            op, args = event["operation"], event["arguments"]
            if op == "observer_http.context":
                value = {
                    "credential": self.credential(),
                    "network_configured": self.network_observations is not None,
                }
            elif op == "observer_http.credential":
                value = self.credential()
            elif op == "observer_http.audit":
                print(json.dumps(args), flush=True)
                value = None
            elif op == "observer_http.read":
                value = self._read(args)
            else:
                raise RuntimeError("OBSERVER_HTTP_CALLBACK_INVALID")
            return {"value": value}
        except APIError as exc:
            return {"fault": {"kind": "api", "code": exc.code, "status": exc.status}}
        except NodeProtocolError as exc:
            return {"fault": {"kind": "coded", "code": exc.code}}
        except ThreadLookupError as exc:
            return {"fault": {"kind": "thread", "code": exc.code}}
        except RustAuthorityError as exc:
            return {"fault": {"kind": "authority", "code": str(exc)}}
        except (sqlite3.Error, OSError):
            return {"fault": {"kind": "source"}}
        except (ValueError, TypeError):
            return {"fault": {"kind": "invalid"}}
        except Exception:  # noqa: BLE001 - wire fault contains no source details.
            return {"fault": {"kind": "other"}}

    def _read(self, request):
        target, method = request["target"], request["method"]
        args = {
            key: _decimal_argument(value) for key, value in request["arguments"].items()
        }
        if target == "store":
            return getattr(self.store, method)(**args)
        if target == "network":
            operation = getattr(self.network_observations, method)
            if "observation_id" in args:
                return operation(args.pop("observation_id"), **args)
            return operation(**args)
        if target == "native":
            from clinx.authority.rust_reads import observe_desktop

            return observe_desktop(self.observations, args.pop("thread_id"), **args)
        if target == "collector":
            from clinx.monitor.native_observation_bridge import NativeCollectorBridge

            if self.native_collector is None:
                self.native_collector = NativeCollectorBridge(self.observations)
            return self.native_collector.request(args["route"], args["query"])
        raise RuntimeError("OBSERVER_HTTP_CALLBACK_INVALID")

    def request(self, method, target, authorization):
        result = self._http.call(
            "request", method=method, target=target, authorization=authorization
        )
        if "fault" in result:
            fault = result["fault"]
            if fault["kind"] == "api":
                raise APIError(fault["status"], fault["code"])
            if fault["kind"] == "source":
                raise sqlite3.OperationalError("SOURCE_UNAVAILABLE")
            if fault["kind"] == "invalid":
                raise ValueError("INVALID_QUERY")
            raise RuntimeError(fault["code"] or "PROJECTION_UNAVAILABLE")
        return result["value"]

    def close(self):
        self._http.close()


class ObserverHTTPServer:
    """Compatibility lifecycle for the Rust loopback HTTP listener."""

    def __init__(self, port, api, *, audit_methods=False):
        self.api, self.audit_methods = api, audit_methods
        result = api._http.call("bind", port=port, audit_methods=audit_methods)
        self._server = result["server"]
        self.server_address = tuple(result["address"])
        self._closed = False

    def serve_forever(self, poll_interval=0.5):
        self.api._http.call("start", server=self._server)
        while not self.api._http.call("wait", server=self._server):
            pass

    def shutdown(self):
        if not self._closed:
            self.api._http.call("shutdown", server=self._server)

    def server_close(self):
        if not self._closed:
            self.api._http.call("close", server=self._server)
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.server_close()


def main():
    """Historical module invocation forwards to the standalone Rust service."""
    import sys
    root = Path(__file__).resolve().parents[2]
    binary = os.environ.get("CLINX_OBSERVER_BINARY", str(root / "clinx-observer"))
    os.execv(binary, [binary, *sys.argv[1:]])


if __name__ == "__main__":
    main()
