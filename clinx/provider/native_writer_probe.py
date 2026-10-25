"""Private binding for the single Rust writer probe and its read-only evidence."""

import json
import os
import sys
from pathlib import Path


def inspect(request):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from clinx.nodes.network_worker import read_command

    return read_command("source_runtime.inspect_writer", **request)


def writer(root, thread_id, turn_id, project):
    from clinx.nodes.network_worker import read_command
    from clinx.provider import native_provider

    # Existing explicit qualification callbacks remain object injections.
    probe = native_provider.observe_native_writer
    if getattr(probe, "__module__", "") != "clinx.provider.native_provider":
        return probe(root, thread_id, turn_id, project)
    return read_command(
        "source_runtime.native_writer",
        root=str(root),
        thread_id=thread_id,
        turn_id=turn_id,
        project=project,
    )


def observe(endpoint, root, thread_id, turn_id, project):
    from clinx.authority.request_pipe import binary
    from clinx.nodes.network_worker import Worker
    from clinx.provider import native_provider

    injected = (
        native_provider.existing_client is not native_provider._default_existing_client
    )
    context = client = None

    def callback(event):
        nonlocal context, client
        operation, args = event["operation"], event["arguments"]
        if operation == "writer_probe.worker_uid":
            from clinx.authority.rust_worker_identity import worker_account

            return worker_account().pw_uid
        if operation == "writer_probe.open":
            from clinx.provider.app_server import UnixSocketTransport

            context = native_provider.existing_client(args["endpoint"], timeout=3)
            client = context.__enter__()
            if isinstance(client.transport, UnixSocketTransport):
                return client.transport.local_peer_uid()
            import socket
            import struct

            return struct.unpack(
                "3i",
                client.transport._byte_transport.sock.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, 12
                ),
            )[1]
        if operation == "writer_probe.initialize":
            client.initialize(
                client_name=args["name"],
                client_title=args["title"],
                client_version=args["version"],
            )
            return None
        if operation == "writer_probe.request":
            client._workspace_metadata_request = args
            try:
                return client._request("command/exec", args)
            finally:
                client._workspace_metadata_request = None
        raise ValueError("NATIVE_WRITER_PROBE_INVALID")

    worker = None
    try:
        worker = Worker(callback=callback)
        worker.call(
            "source_runtime.init",
            config=dict(
                native_home=str(root),
                native_observer_socket=str(endpoint),
                probe_program=str(binary()),
                injected_probe=injected,
                injected_home=str(Path.home()) if injected else None,
                injected_legacy_script=str(Path(__file__).resolve())
                if injected
                else None,
            ),
        )
        return worker.call(
            "source_runtime.writer_probe",
            tid=thread_id,
            turn_id=turn_id,
            project=project,
        )
    except (OSError, ValueError, RuntimeError):
        return {"liveness": "UNKNOWN", "reason": "NATIVE_WRITER_PROBE_UNAVAILABLE"}
    finally:
        if context is not None:
            context.__exit__(None, None, None)
        if worker is not None:
            worker.close()


if __name__ == "__main__":
    try:
        result = inspect(json.loads(sys.argv[1]))
    except (OSError, ValueError, KeyError, IndexError):
        result = dict(
            liveness="UNKNOWN",
            reason="NATIVE_WRITER_UNPROVEN",
            observer_uid=os.getuid(),
        )
    print(json.dumps(result))
