"""Existing native/HTTP IO callbacks; Rust owns exact read and display policy."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from clinx.authority.rust_authority import RustAuthorityError


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _read(operation, callback=None, **arguments):
    from clinx.monitor.native_observation_bridge import _Client
    from clinx.nodes.node_protocol import NodeProtocolError

    client = _Client(callback)
    try:
        return client._call(operation, **arguments)
    except NodeProtocolError as exc:
        raise RustAuthorityError(exc.code) from None
    finally:
        client.close()


def limits(recent_turns, max_bytes):
    return tuple(
        _read("desktop_limits", recent_turns=recent_turns, max_bytes=max_bytes)
    )


def desktop_read(
    thread_id, *, context=False, recent_turns=8, max_bytes=32000, cursor=None
):
    """Fixed existing loopback HTTP codec; token never crosses the private pipe."""

    def http(event):
        args = event["arguments"]
        try:
            token = Path(args["credential"]).read_text().strip()
            query = dict(
                context=int(args["context"]),
                recent_turns=args["recent_turns"],
                max_bytes=args["max_bytes"],
            )
            if args["cursor"] is not None:
                query["cursor"] = args["cursor"]
            request = Request(
                "http://127.0.0.1:8766/v2/native/"
                + args["thread_id"]
                + "?"
                + urlencode(query),
                headers={"Authorization": "Bearer " + token},
            )
            with urlopen(request, timeout=12) as response:
                return dict(status=200, body=response.read(1048577).decode("utf-8"))
        except HTTPError as exc:
            try:
                return dict(status=exc.code, body=exc.read(4096).decode("utf-8"))
            except (ValueError, OSError):
                return dict(transport_error=True)
        except (OSError, URLError, ValueError):
            return dict(transport_error=True)

    return _read(
        "desktop_read",
        http,
        credential=os.environ.get("CLINX_NATIVE_OBSERVER_TOKEN_FILE"),
        thread_id=thread_id,
        context=context,
        recent_turns=recent_turns,
        max_bytes=max_bytes,
        cursor=cursor,
    )


def observe_desktop(
    directory, thread_id, *, context=False, recent_turns=8, max_bytes=32000, cursor=None
):
    """Bind configured directory and existing Provider reads to Rust policy."""
    from clinx.monitor.native_observation_bridge import _Client
    from clinx.nodes.node_protocol import NodeProtocolError
    from clinx.provider.native_history import NativeHistory

    history = None

    def callback(event):
        nonlocal history
        operation, args = event["operation"], event["arguments"]
        if operation == "desktop.facts":
            identity = directory.local_identity if directory else None
            scope = (
                directory._scope(identity["node_id"])
                if identity and directory.native_home
                else None
            )
            return dict(
                local_identity=identity,
                native_home=str(directory.native_home)
                if directory and directory.native_home
                else None,
                scope=scope.as_dict() if scope else None,
            )
        if operation == "desktop.scope":
            scope = directory._scope(args["node_id"])
            return scope.as_dict() if scope else None
        if history is None:
            history = NativeHistory(directory.native_home)
        if operation == "desktop.metadata":
            return history.metadata(args["thread_id"])
        if operation == "desktop.status":
            return history.status(args["thread_id"], args["metadata"])
        if operation == "desktop.context":
            return history.context(
                args["thread_id"],
                args["node_id"],
                args["metadata"],
                args["recent_turns"],
                args["max_bytes"],
                cursor=args["cursor"],
            )
        if operation == "desktop.live":
            from clinx.provider.app_server import AppServerError
            from clinx.provider.native_provider import existing_client

            try:
                with existing_client(args["endpoint"], timeout=3) as client:
                    client.initialize(
                        client_name="clinx-native-observer",
                        client_title="CLINX read-only Observer",
                        client_version="1",
                    )
                    return dict(thread=client.thread_read(args["thread_id"]))
            except (AppServerError, OSError):
                return dict(unavailable=True)
        if operation == "desktop.writer":
            from clinx.provider.native_writer_probe import writer

            return writer(
                directory.native_home,
                args["thread_id"],
                args["turn_id"],
                args["project"],
            )
        if operation == "desktop.probe":
            from clinx.provider.native_writer_probe import observe

            return observe(
                args["endpoint"],
                directory.native_home,
                args["thread_id"],
                args["turn_id"],
                args["project"],
            )
        raise NodeProtocolError(
            "NATIVE_OBSERVER_CALLBACK_INVALID", "Unexpected desktop read callback"
        )

    client = _Client(callback)
    try:
        return client._call(
            "desktop_observe",
            thread_id=thread_id,
            context=context,
            recent_turns=recent_turns,
            max_bytes=max_bytes,
            cursor=cursor,
            endpoint=os.environ.get("CLINX_NATIVE_OBSERVER_SOCKET"),
        )
    except NodeProtocolError as exc:
        if exc.code == "INVALID_CONTEXT_LIMIT":
            raise RustAuthorityError(exc.code) from None
        raise
    finally:
        client.close()
