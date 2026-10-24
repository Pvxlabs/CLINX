"""Read existing native associations; never register a task or initialize a schema."""
from pathlib import Path
from clinx.monitor.network_observation import database
from clinx.nodes.node_protocol import SUPPORTED_PROVIDER, NodeProtocolError

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
    """Read exact associations without schema migration or write locks."""
    if not path or not Path(path).exists():
        return None
    with database(path) as c:
        if not c.execute(
            "SELECT 1 FROM sqlite_master WHERE name='native_task_bindings'"
        ).fetchone():
            return None
        predicates, args = [], []
        for column, value in (
            ("node_id", node),
            ("host", host),
            ("provider", provider),
            ("thread_id", thread),
            ("task_id", task),
            ("user_scope", user),
        ):
            if value is not None:
                predicates.append(column + "=?")
                args.append(value)
        rows = c.execute(
            "SELECT * FROM native_task_bindings WHERE "
            + " AND ".join(predicates)
            + " LIMIT 2",
            args,
        ).fetchall()
        if len(rows) > 1:
            raise NodeProtocolError(
                "NATIVE_IDENTITY_AMBIGUOUS", "Use an exact node and thread"
            )
        return dict(rows[0]) if rows else None
