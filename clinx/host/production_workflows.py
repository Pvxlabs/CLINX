"""Registered argv templates for existing controllers, not a deployment engine.

Config owns executables, hosts, paths and flags. Request values are typed
identities. The controller owns artifact/plan semantics and business idempotency.
"""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class RegisteredWorkflow:
    identity: str
    target: str
    action: str
    operation_class: str
    argv: tuple[str, ...]
    parameters: tuple[tuple[str, str, tuple[str, ...]], ...]
    resource: str

    @property
    def operation(self):
        return 'workflow:' + self.identity + ':' + self.action

    @property
    def parameter_schema(self):
        from clinx.contracts.runtime_config import _call
        return _call('workflow_schema', workflow=dataclasses.asdict(self))

    def render(self, parameters):
        from clinx.contracts.runtime_config import _call
        return tuple(_call('workflow_render', workflow=dataclasses.asdict(self), parameters=parameters))


def _decode(rows):
    return tuple(RegisteredWorkflow(**dict(row, argv=tuple(row['argv']),
        parameters=tuple((name, kind, tuple(choices)) for name, kind, choices in row['parameters'])))
        for row in rows)


def load_workflows(rows):
    from clinx.contracts.runtime_config import _call, _orders
    return _decode(_call('workflows', rows=rows,
        orders=_orders(rows, ('host_executor', 'workflows'))))
