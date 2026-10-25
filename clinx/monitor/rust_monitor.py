"""Existing Observer -> existing MCP -> Rust Centre read projections.

No database, business commands, provider startup or legacy task-history import.
"""

from __future__ import annotations

import os

from clinx.nodes.node_protocol import NodeProtocolError

READ_TOOLS = frozenset(
    {
        "clinx_get_capabilities",
        "clinx_list_observations",
        "clinx_get_observation",
        "clinx_get_observation_context",
        "clinx_get_observation_activity",
    }
)
MAX_REPLY = 2 * 1024 * 1024


class RustObservationDirectory:
    """Existing directory/Authority IO for the Rust exact-context read policy."""

    def __init__(self, directory, integration):
        from clinx.monitor.native_observation_bridge import _Client

        self.directory, self.integration = directory, integration
        self._client = _Client(self._callback)

    def __getattr__(self, name):
        return getattr(self.directory, name)

    def _callback(self, event):
        from clinx.authority.rust_authority import RustAuthorityError
        from clinx.authority.rust_reads import desktop_read

        operation, args = event["operation"], event["arguments"]
        try:
            if operation == "managed.detail":
                return self.directory.detail(args.pop("observation_id"), **args)
            if operation == "managed.remote_context":
                return self.directory.context(args.pop("observation_id"), **args)
            if operation == "managed.desktop_context":
                return desktop_read(args.pop("thread_id"), **args)
            if operation == "managed.exact_context":
                return self.integration.get_context(**args)
        except RustAuthorityError as exc:
            raise NodeProtocolError(str(exc), str(exc)) from None
        raise NodeProtocolError(
            "NATIVE_OBSERVER_CALLBACK_INVALID", "Unexpected managed read callback"
        )

    @staticmethod
    def _identity(item):
        from clinx.monitor.native_observation_bridge import _Client

        client = _Client()
        try:
            return tuple(client._call("managed_identity", item=item))
        finally:
            client.close()

    def context(self, observation_id, *, cursor=None):
        return self._client._call(
            "managed_context",
            observation_id=observation_id,
            cursor=cursor,
            runtime_host=self.integration.cfg.runtime_host,
        )

    def _recheck(self, observation_id, item):
        self._client._call("managed_recheck", observation_id=observation_id, item=item)

    def close(self):
        self._client.close()


class MCPReader:
    """Rust-owned existing Unix MCP read transport and bounded response checks."""

    def __init__(self, endpoint):
        from clinx.monitor.native_observation_bridge import _Client

        self.endpoint = str(endpoint)
        self._client = _Client()

    def call(self, name, arguments=None):
        return self._client._call(
            "monitor_mcp", endpoint=self.endpoint, name=name, arguments=arguments or {}
        )

    def close(self):
        self._client.close()


class RustObservationProxy:
    def __init__(self, reader):
        self.reader = reader

    def list(self, **arguments):
        return self.reader.call("clinx_list_observations", arguments)

    def detail(self, observation_id, **arguments):
        return self.reader.call(
            "clinx_get_observation", dict(observation_id=observation_id, **arguments)
        )

    def context(self, observation_id, **arguments):
        return self.reader.call(
            "clinx_get_observation_context",
            dict(observation_id=observation_id, **arguments),
        )

    def activity(self, observation_id, **arguments):
        return self.reader.call(
            "clinx_get_observation_activity",
            dict(observation_id=observation_id, **arguments),
        )


class RustMonitorStore:
    """Keep v1 handshakes compatible; current rows come only from the v2 directory.

    The shipping App already merges v2 managed/native observations. Returning no
    legacy v1 rows prevents old Python records from masquerading as Rust state.
    This is a live projection and never initializes a replacement task database.
    """

    observation_directory = None

    def __init__(self, reader):
        from clinx.monitor.native_observation_bridge import _Client

        self.reader = reader
        self._client = _Client(self._callback)

    def _callback(self, event):
        args = event["arguments"]
        return self.reader.call(args["name"], args["arguments"])

    def health(self):
        return self._client._call("monitor_health")

    def list_tasks(self, state, offset=0):
        return self._client._call("monitor_list")

    def close(self):
        self._client.close()

    def task(self, task_ref):
        from clinx.monitor.observer_server import APIError

        raise APIError(404, "RUST_TASKS_USE_OBSERVATIONS_V2")

    def task_events(self, task_ref, after=None):
        return self.task(task_ref)

    def task_activity(self, task_ref, execution_ref, after=None, before=None):
        return self.task(task_ref)


def from_env():
    endpoint = os.environ.get("CLINX_OBSERVER_RUST_MCP")
    if not endpoint:
        return None
    reader = MCPReader(endpoint)
    return RustMonitorStore(reader), RustObservationProxy(reader)
