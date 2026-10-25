"""CLINX Monitor v1: a read-only projection of the canonical registry.

No TaskRegistry construction, migrations, reconciliation or business writes.
Optional exact native reads use an existing daemon with a read-only client.
Run separately from the authority. The HTTP listener is always loopback-only.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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


class ObserverAPI:
    def __init__(self, store, credential, observations=None, network_observations=None):
        self.store, self.credential = store, credential
        self.observations = observations
        self.network_observations = (
            network_observations if network_observations is not None else observations
        )
        self.store.observation_directory = observations
        self.native_collector = None

    def request(self, method, target, authorization):
        token = self.credential()
        if not isinstance(token, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{32,256}", token
        ):
            raise APIError(503, "AUTH_UNAVAILABLE")
        expected = ("Bearer " + token).encode()
        if not isinstance(authorization, str) or not hmac.compare_digest(
            hashlib.sha256(authorization.encode()).digest(),
            hashlib.sha256(expected).digest(),
        ):
            raise APIError(401, "UNAUTHORIZED")
        if method != "GET":
            raise APIError(405, "READ_ONLY")
        if len(target) > (
            16384 if target.startswith("/v2/native-collector/") else 2048
        ):
            raise APIError(400, "INVALID_REQUEST")
        url = urlsplit(target)
        if url.scheme or url.netloc or url.fragment or "%" in url.path:
            raise APIError(400, "INVALID_REQUEST")
        try:
            query = parse_qs(
                url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8
            )
        except ValueError:
            raise APIError(400, "INVALID_QUERY") from None
        if any(len(v) != 1 for v in query.values()):
            raise APIError(400, "INVALID_QUERY")
        collector = re.fullmatch(
            r"/v2/native-collector/(inventory|sample/([0-9a-f-]{36}))", url.path
        )
        if collector:
            from clinx.contracts.thread_identity import ThreadLookupError
            from clinx.monitor.native_observation_bridge import NativeCollectorBridge
            from clinx.nodes.node_protocol import NodeProtocolError

            try:
                if self.native_collector is None:
                    self.native_collector = NativeCollectorBridge(self.observations)
                return self.native_collector.request(
                    collector[1], {k: v[0] for k, v in query.items()}
                )
            except (NodeProtocolError, ThreadLookupError) as exc:
                raise APIError(
                    409 if exc.code == "NATIVE_COLLECTOR_CURSOR_RESET" else 400,
                    exc.code,
                ) from None
            except (TypeError, ValueError):
                raise APIError(400, "INVALID_QUERY") from None
        native = re.fullmatch(r"/v2/native/([0-9a-f-]{36})", url.path)
        if native:
            from clinx.authority.rust_authority import RustAuthorityError
            from clinx.authority.rust_reads import observe_desktop
            from clinx.contracts.thread_identity import ThreadLookupError
            from clinx.nodes.node_protocol import NodeProtocolError

            if set(query) - {"context", "recent_turns", "max_bytes", "cursor"}:
                raise APIError(400, "INVALID_QUERY")
            try:
                context = query.get("context", ["0"])[0]
                if context not in ("0", "1"):
                    raise ValueError()
                return observe_desktop(
                    self.observations,
                    native[1],
                    context=context == "1",
                    recent_turns=int(query.get("recent_turns", ["8"])[0]),
                    max_bytes=int(query.get("max_bytes", ["32000"])[0]),
                    cursor=query.get("cursor", [None])[0],
                )
            except (NodeProtocolError, ThreadLookupError) as exc:
                raise APIError(400, exc.code) from None
            except RustAuthorityError as exc:
                raise APIError(400, str(exc)) from None
            except (TypeError, ValueError):
                raise APIError(400, "INVALID_QUERY") from None
        if url.path.startswith("/v2/observations"):
            from clinx.nodes.node_protocol import NodeProtocolError

            if self.network_observations is None:
                raise APIError(503, "OBSERVATION_NOT_CONFIGURED")
            arguments = {k: v[0] for k, v in query.items()}
            try:
                if "limit" in arguments:
                    arguments["limit"] = int(arguments["limit"])
                if url.path == "/v2/observations" and not set(arguments) - {
                    "node",
                    "project",
                    "state",
                    "kind",
                    "native_thread_id",
                    "cursor",
                    "limit",
                }:
                    return self.network_observations.list(**arguments)
                match = re.fullmatch(
                    r"/v2/observations/(obs_[a-f0-9]{40})(/context|/activity)?",
                    url.path,
                )
                if match and not set(arguments) - (
                    {"cursor"} if match[2] == "/context" else {"cursor", "limit"}
                ):
                    operation = (
                        self.network_observations.context
                        if match[2] == "/context"
                        else self.network_observations.activity
                        if match[2] == "/activity"
                        else self.network_observations.detail
                    )
                    return operation(match[1], **arguments)
                raise APIError(400, "INVALID_QUERY")
            except NodeProtocolError as exc:
                raise APIError(
                    404 if exc.code == "OBSERVATION_NOT_FOUND" else 400, exc.code
                ) from None
            except (TypeError, ValueError):
                raise APIError(400, "INVALID_QUERY") from None
        if url.path == "/v1/health" and not query:
            return self.store.health()
        if url.path == "/v1/tasks":
            if set(query) - {"state", "offset"} or query.get("state") not in (
                ["active"],
                ["recent"],
            ):
                raise APIError(400, "INVALID_QUERY")
            raw_offset = query.get("offset", ["0"])[0]
            if not re.fullmatch(r"[0-9]{1,6}", raw_offset) or int(raw_offset) > 100000:
                raise APIError(400, "INVALID_QUERY")
            return self.store.list_tasks(query["state"][0], int(raw_offset))
        activity = re.fullmatch(r"/v1/tasks/([A-Za-z0-9_-]{1,128})/activity", url.path)
        if activity:
            eref = query.get("execution_ref", [""])[0]
            if (
                set(query) - {"execution_ref", "after", "before"}
                or ("after" in query and "before" in query)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", eref)
                or any(not query[k][0] for k in ("after", "before") if k in query)
            ):
                raise APIError(400, "INVALID_QUERY")
            return self.store.task_activity(
                activity[1],
                eref,
                query.get("after", [None])[0],
                query.get("before", [None])[0],
            )
        match = re.fullmatch(r"/v1/tasks/([A-Za-z0-9_-]{1,128})(/events)?", url.path)
        if match:
            if match[2] and not set(query) - {"after"}:
                return self.store.task_events(match[1], query.get("after", [None])[0])
            if not match[2] and not query:
                return self.store.task(match[1])
            raise APIError(400, "INVALID_QUERY")
        raise APIError(404, "NOT_FOUND")


class ObserverHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16

    def __init__(self, port, api, *, audit_methods=False):
        self.api = api
        self.audit_methods = audit_methods
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(("127.0.0.1", port), ObserverHandler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass  # Never emit request data, authorization, tracebacks or source payloads.


class ObserverHandler(BaseHTTPRequestHandler):
    server_version = "CLINXObserver/1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self.respond
        raise AttributeError(name)

    def respond(self):
        status = 200
        try:
            auth = self.headers.get_all("Authorization", [])
            if len(auth) != 1:
                raise APIError(401, "UNAUTHORIZED")
            if (
                self.headers.get("Transfer-Encoding")
                or self.headers.get("Content-Length", "0") != "0"
            ):
                raise APIError(400, "BODY_NOT_ALLOWED")
            payload = self.server.api.request(self.command, self.path, auth[0])
        except APIError as error:
            status, payload = (
                error.status,
                {"schema_version": VERSION, "error": error.code},
            )
        except (sqlite3.Error, OSError):
            status, payload = (
                503,
                {"schema_version": VERSION, "error": "SOURCE_UNAVAILABLE"},
            )
        except Exception:
            status, payload = (
                503,
                {"schema_version": VERSION, "error": "PROJECTION_UNAVAILABLE"},
            )
        body = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode()
        token = self.server.api.credential()
        if token:
            body = body.replace(token.encode(), b"[REDACTED]")
        if len(body) > MAX_RESPONSE:
            status, body = (
                503,
                b'{"schema_version":"1","error":"RESPONSE_BOUND_EXCEEDED"}',
            )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        if status == 405:
            self.send_header("Allow", "GET")
        self.end_headers()
        if self.server.audit_methods:
            method = (
                self.command
                if self.command
                in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
                else "OTHER"
            )
            print(
                json.dumps({"observer_http_method": method, "status": status}),
                flush=True,
            )
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    do_GET = respond


def main():
    # -I excludes cwd and script directory: add only the installed package.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    token = os.environ.get("CLINX_OBSERVER_TOKEN", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise SystemExit(
            "CLINX_OBSERVER_TOKEN must be a 32..256 character URL-safe credential"
        )
    path = os.environ.get("CLINX_OBSERVER_DB")
    from clinx.monitor.network_observation import directory_from_env

    observations = directory_from_env()
    desktop_routes = json.loads(os.environ.get("CLINX_OBSERVER_CODEX_HOST_IDS", "{}"))
    if not isinstance(desktop_routes, dict) or any(
        not isinstance(k, str)
        or not isinstance(v, str)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]+", v)
        for k, v in desktop_routes.items()
    ):
        raise SystemExit(
            "CLINX_OBSERVER_CODEX_HOST_IDS must map canonical hosts to configured Desktop host IDs"
        )
    from clinx.monitor.rust_monitor import from_env as rust_monitor_from_env

    rust_monitor = rust_monitor_from_env()
    if not rust_monitor and not path:
        raise SystemExit(
            "CLINX_OBSERVER_DB must identify the existing canonical registry"
        )
    store = (
        rust_monitor[0]
        if rust_monitor
        else ObserverStore(path, codex_host_ids=desktop_routes)
    )
    store.health()  # Fail closed; never initialize a missing registry.
    server = ObserverHTTPServer(
        int(os.environ.get("CLINX_OBSERVER_PORT", "8766")),
        ObserverAPI(
            store,
            lambda: os.environ.get("CLINX_OBSERVER_TOKEN"),
            observations,
            network_observations=rust_monitor[1] if rust_monitor else None,
        ),
        audit_methods=os.environ.get("CLINX_OBSERVER_AUDIT_METHODS") == "1",
    )
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
