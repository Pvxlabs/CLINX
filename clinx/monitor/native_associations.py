"""Private Rust read client for existing native association evidence."""

from clinx.monitor.network_observation import _read
from clinx.nodes.node_protocol import SUPPORTED_PROVIDER


def binding(
    path,
    *,
    node=None,
    host=None,
    provider=SUPPORTED_PROVIDER,
    thread=None,
    task=None,
    user=None,
):
    return _read(
        "binding",
        path=str(path) if path else None,
        node=node,
        host=host,
        provider=provider,
        thread=thread,
        task=task,
        user=user,
    )
