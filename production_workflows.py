"""Registered argv templates for existing controllers, not a deployment engine.

Config owns executables, hosts, paths and flags. Request values are typed
identities. The controller owns artifact/plan semantics and business idempotency.
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from execution_policy import PRODUCTION_MUTATION, PRODUCTION_READ_ONLY

_FORMATS = {
    'source_sha': r'[0-9a-f]{40}',
    'sha256': r'[0-9a-f]{64}',
    'identity': r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',
}


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
        return {'type': 'object', 'additionalProperties': False,
                'required': [name for name, _, _ in self.parameters],
                'properties': {name: {'type': 'string', **(
                    {'enum': list(choices)} if kind == 'enum' else {'pattern': '^' + _FORMATS[kind] + '$'})}
                    for name, kind, choices in self.parameters}}

    def render(self, parameters):
        if not isinstance(parameters, dict) or set(parameters) != {p[0] for p in self.parameters}:
            raise ValueError('INVALID_ARGUMENTS: workflow parameters must match the registered schema')
        for name, kind, choices in self.parameters:
            value = parameters[name]
            if not isinstance(value, str) or (value not in choices if kind == 'enum'
                                              else re.fullmatch(_FORMATS[kind], value) is None):
                raise ValueError('INVALID_ARGUMENTS: invalid workflow parameter ' + name)
        return tuple(re.sub(r'\{([a-z][a-z0-9_]*)\}', lambda m: parameters[m[1]], token) for token in self.argv)


def load_workflows(rows):
    if not isinstance(rows, dict):
        raise ValueError('host_executor.workflows must contain tables')
    result = []
    identities = set()
    target_resources = {}
    for row in rows.values():
        if not isinstance(row, dict) or set(row) != {
                'identity', 'target', 'action', 'operation_class', 'argv', 'parameters', 'resource'}:
            raise ValueError('workflow registration requires exact identity, target, action, class, argv, parameters, resource')
        for field in ('identity', 'target', 'action', 'resource'):
            if not isinstance(row[field], str) or re.fullmatch(_FORMATS['identity'], row[field]) is None:
                raise ValueError('invalid workflow registration identity')
        if row['operation_class'] not in {PRODUCTION_READ_ONLY, PRODUCTION_MUTATION}:
            raise ValueError('workflow class must be an explicit production class')
        argv = row['argv']
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x or '\x00' in x for x in argv):
            raise ValueError('workflow argv must be a non-empty fixed command')
        if not Path(argv[0]).is_absolute() or '{' in argv[0] or '}' in argv[0]:
            raise ValueError('workflow executable must be a registered absolute path')
        parameters = row['parameters']
        if not isinstance(parameters, dict) or len(parameters) > 32:
            raise ValueError('invalid workflow parameter schema')
        parsed = []
        for name, spec in parameters.items():
            if re.fullmatch(r'[a-z][a-z0-9_]{0,63}', name) is None or not isinstance(spec, dict):
                raise ValueError('invalid workflow parameter name/schema')
            if set(spec) == {'enum'}:
                choices = spec['enum']
                if not isinstance(choices, list) or not choices or any(
                        not isinstance(c, str) or re.fullmatch(_FORMATS['identity'], c) is None for c in choices):
                    raise ValueError('workflow enum must contain safe identities')
                parsed.append((name, 'enum', tuple(choices)))
            elif set(spec) == {'format'} and spec['format'] in _FORMATS:
                parsed.append((name, spec['format'], ()))
            else:
                raise ValueError('unsupported workflow parameter schema; paths, hosts and argv are forbidden')
        placeholders = set()
        for token in argv[1:]:
            if '{' in token or '}' in token:
                names = re.findall(r'\{([a-z][a-z0-9_]{0,63})\}', token)
                literal = re.sub(r'\{[a-z][a-z0-9_]{0,63}\}', 'identity', token)
                # Config may derive a path beneath its fixed absolute root from
                # safe single-component identities. Callers still cannot supply
                # a path, slash, executable, flag, remote host or shell fragment.
                whole = re.fullmatch(r'\{[a-z][a-z0-9_]{0,63}\}', token) is not None
                fixed_path = token.startswith('/') and not token.startswith('/{') and re.fullmatch(r'/[A-Za-z0-9_./-]+', literal) is not None
                if not names or '{' in literal or '}' in literal or not (whole or fixed_path):
                    raise ValueError('workflow placeholder must be an identity token or a registered absolute path template')
                if '..' in Path(literal).parts:
                    raise ValueError('workflow path template traversal')
                placeholders.update(names)
        unused = set(parameters) - placeholders
        if placeholders - set(parameters) or any(
                'enum' not in parameters[name] or len(parameters[name]['enum']) != 1 for name in unused):
            raise ValueError('workflow template/schema mismatch')
        # Singleton enums may constrain intent (e.g. DATA scope) without adding
        # unsupported flags to an existing controller's CLI.
        key = (row['identity'], row['target'], row['action'])
        if key in identities:
            raise ValueError('duplicate workflow registration')
        identities.add(key)
        previous_resource = target_resources.setdefault(row['target'], row['resource'])
        if previous_resource != row['resource']:
            raise ValueError('one production target must share one resource fence')
        result.append(RegisteredWorkflow(row['identity'], row['target'], row['action'],
            row['operation_class'], tuple(argv), tuple(sorted(parsed)), row['resource']))
    return tuple(result)
