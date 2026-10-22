"""Connection borrowing for an explicit canonical authority transaction.

No connection, migration or feature activation occurs on import. A borrowed
connection never commits/closes its owner's transaction.
"""

import functools
import inspect
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_current = ContextVar("clinx_authority_transaction", default=None)


def owner_mutation(method):
    """Join an existing Owner's finite execution mutation interface.

    Unenrolled task metadata keeps its existing transactions. A borrowed
    canonical transaction calls the original method without another commit.
    This is local wiring, never a transport for SQL or arbitrary method names.
    """
    signature = inspect.signature(method)

    @functools.wraps(method)
    def call(registry, *args, **kwargs):
        owner = getattr(registry, '_execution_owner', None)
        if owner is None or borrowed(registry.path) is not None:
            return method(registry, *args, **kwargs)
        bound = signature.bind(registry, *args, **kwargs)
        arguments = dict(bound.arguments)
        arguments.pop('self')
        for name, parameter in signature.parameters.items():
            if parameter.kind == inspect.Parameter.VAR_KEYWORD:
                arguments.update(arguments.pop(name, {}))
        for key in ('routing_identity', 'execution_policy'):
            if hasattr(arguments.get(key), 'to_json'):
                arguments[key] = arguments[key].to_json()
        result = owner.registry_mutation(method.__name__, arguments)
        if result is NotImplemented:
            return method(registry, *args, **kwargs)
        # Preserve the existing Python interface for local orchestration.
        import task_registry
        annotation = method.__annotations__.get('return', '')
        record = getattr(task_registry, str(annotation).split(' | ')[0], None)
        if result is not None and hasattr(record, '__dataclass_fields__'):
            return record(**result)
        if method.__name__ == 'bind_reauthorized_provider':
            from execution_semantics import parse_routing_identity
            return (task_registry.ConversationBinding(**result[0]),
                    parse_routing_identity(result[1]))
        return result

    return call


class BorrowedConnection:
    def __init__(self, conn):
        self.conn = conn

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def close(self):
        pass

    def commit(self):
        raise RuntimeError("authority owns COMMIT")

    def rollback(self):
        raise RuntimeError("authority owns ROLLBACK")


def borrowed(path):
    current = _current.get()
    if current and current[0] == Path(path).resolve():
        return BorrowedConnection(current[1])
    return None


def configure(conn):
    for index, name in enumerate(("resource", "epoch", "execution", "operation")):
        conn.create_function(
            "clinx_fence_" + name,
            0,
            lambda i=index: _current.get()[2][i] if _current.get() else None,
        )


@contextmanager
def bind(path, conn, resource, epoch, execution, operation=None):
    if _current.get() is not None:
        raise RuntimeError("nested authority transaction")
    token = _current.set(
        (Path(path).resolve(), conn, (resource, epoch, execution, operation))
    )
    try:
        yield
    finally:
        _current.reset(token)
