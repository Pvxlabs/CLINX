"""Verify source roles, package imports and the Rust/compat release boundary."""
from __future__ import annotations

import ast
import json
from pathlib import Path

from tools.build.runtime_sources import SOURCE_ROOT, inventory, source_paths


def check(source: Path = SOURCE_ROOT) -> dict:
    assert not list(source.glob('*.py')), 'Root Python modules are not project entrypoints'
    rows = inventory(source)
    declared = {row['path'] for row in rows if row['role'] != 'DEAD'}
    actual = {str(p.relative_to(source)) for folder in ('clinx', 'tools', 'scripts', 'fixtures')
              for p in (source / folder).rglob('*.py') if '__pycache__' not in p.parts}
    assert actual == declared, {'undeclared': sorted(actual-declared), 'missing': sorted(declared-actual)}
    rust = {str(p.relative_to(source)) for p in source_paths(source, 'rust')}
    edges = []
    for path in sorted(actual):
        for node in ast.walk(ast.parse((source / path).read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    package = path.removesuffix('.py').split('/')[:-1]
                    names = ['.'.join(package[:len(package)-node.level+1] +
                                      ([node.module] if node.module else []))]
                elif node.module:
                    names = [node.module]
            for name in names:
                if not name.startswith(('clinx.', 'tools.')):
                    continue
                stem = name.replace('.', '/')
                targets = [stem+'.py', stem+'/__init__.py']
                target = next((candidate for candidate in targets if candidate in actual), None)
                assert target, (path, node.lineno, 'unresolved internal import', name)
                if path in rust:
                    assert target in rust and not target.startswith('clinx/compat/'), (
                        path, node.lineno, 'Rust release imports outside its profile', name)
                edges.append({'source': path, 'line': node.lineno, 'target': target})
    return {'status': 'PASS', 'python_modules': len(actual), 'root_python_modules': 0,
            'rust_sources': len(rust), 'monitor_sources': len(source_paths(source, 'monitor')),
            'edges': edges}


if __name__ == '__main__':
    print(json.dumps(check(), indent=2))
