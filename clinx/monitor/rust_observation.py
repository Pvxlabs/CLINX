"""Existing Authority/native IO for Rust-owned managed observation projection."""

from pathlib import Path

from clinx.authority.rust_authority import Commands, RustAuthorityError
from clinx.monitor.native_observation_bridge import _Client
from clinx.monitor.observation_source import CodexObservationSource, order
from clinx.nodes.node_protocol import NodeProtocolError, SharingScope


class RustObservationSource(CodexObservationSource):
    def __init__(
        self,
        root,
        node_id,
        owner_host=None,
        provider_config=None,
        commands=None,
        native_source=None,
    ):
        self.rust_only = root is None
        self.native_source = native_source
        self.managed_inventory_unavailable = False
        super().__init__(
            root or Path("/nonexistent"),
            node_id,
            owner_host=owner_host,
            provider_config=provider_config,
        )
        self.commands = commands or Commands.from_env()
        self._managed_client = _Client(self._managed_callback)

    def _managed(self, operation, **arguments):
        try:
            return self._managed_client._call(
                "managed_source_" + operation, **arguments
            )
        except NodeProtocolError as exc:
            if exc.code.startswith("SOURCE_AUTHORITY_ERROR:"):
                raise RustAuthorityError(exc.code.split(":", 1)[1]) from None
            raise

    def _managed_callback(self, event):
        operation, args = event["operation"], event["arguments"]
        try:
            if operation == "managed.health":
                return self.commands.client.request(
                    "health", {}, request_id="observation-generation"
                )
            if operation == "managed.base_generation":
                return super().generation
            if operation in {"managed.native_inventory", "managed.base_inventory"}:
                method = (
                    self.native_source.inventory
                    if operation == "managed.native_inventory"
                    else super().inventory
                )
                return method(
                    SharingScope(**args["scope"]), args["after"], audit=args["audit"]
                )
            if operation in {
                "managed.native_turns",
                "managed.base_native_turns",
                "managed.base_sample",
            }:
                method = {
                    "managed.native_turns": lambda: self.native_source.native_turns,
                    "managed.base_native_turns": lambda: (
                        super(RustObservationSource, self).native_turns
                    ),
                    "managed.base_sample": lambda: (
                        super(RustObservationSource, self).sample
                    ),
                }[operation]()
                state = args["state"]
                result = method(args["metadata"], state)
                return (
                    (*result, state) if operation != "managed.base_sample" else result
                )
            if operation == "managed.command":
                result = self.commands.call(args["operation"], args["arguments"])
                if args["operation"] == "observation_turns":
                    # Preserve the deployed Python timestamp wire codec, including
                    # naive local-time strings, before Rust projects the rows.
                    result = dict(
                        result,
                        executions=[
                            dict(row, created_at_ordinal=order(row["created_at"]))
                            for row in result["executions"]
                        ],
                    )
                return result
        except RustAuthorityError as exc:
            raise NodeProtocolError(
                "SOURCE_AUTHORITY_ERROR:" + str(exc), str(exc)
            ) from None
        raise NodeProtocolError(
            "NATIVE_OBSERVER_CALLBACK_INVALID", "Unexpected managed source callback"
        )

    @property
    def generation(self):
        return self._managed("generation", rust_only=self.rust_only)

    def inventory(self, scope, after=None, *, audit=False):
        return self._managed(
            "inventory",
            rust_only=self.rust_only,
            remote=self.native_source is not None,
            scope=scope.as_dict(),
            after=after,
            audit=audit,
        )

    def native_turns(self, metadata, state):
        turns, coverage, updated = self._managed(
            "native_turns",
            remote=self.native_source is not None,
            metadata=metadata,
            state=state,
        )
        state.clear()
        state.update(updated)
        return turns, coverage

    def canonical_inventory(self, scope, after=""):
        rows, unavailable = self._managed(
            "canonical_inventory",
            owner_host=self.owner_host,
            scope=scope.as_dict() if hasattr(scope, "as_dict") else None,
            after=after,
        )
        self.managed_inventory_unavailable = unavailable
        return rows

    def canonical_turns(self, tid, project, state):
        turns, updated = self._managed(
            "canonical_turns",
            owner_host=self.owner_host,
            tid=tid,
            project=project,
            state=state,
        )
        state.clear()
        state.update(updated)
        return turns

    def sample(self, metadata, state):
        events, updated = self._managed(
            "sample",
            metadata=metadata,
            state=state,
            managed_inventory_unavailable=self.managed_inventory_unavailable,
        )
        state.clear()
        state.update(updated)
        return events, state

    def close(self):
        self._managed_client.close()
        if self._source_worker is not None:
            self._source_worker.close()
