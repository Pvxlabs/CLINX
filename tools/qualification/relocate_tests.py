"""Copy historical tests and update only module/path references for qualification.

Original local tests are never edited. No tests, assertions or skips are removed.
The output records each changed source hash and is always outside the repository.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[2]


def rewrite(text: str, modules: dict, paths: dict, symbols: dict | None = None) -> str:
    symbols = symbols or {}
    def mapped(name):
        for symbol, target in symbols.items():
            if name == symbol or name.startswith(symbol + '.'):
                return target + name[len(symbol):]
        first, sep, rest = name.partition('.')
        return modules.get(first, first) + (sep + rest if sep else '')

    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    changes = []
    tree = ast.parse(text)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    for node in ast.walk(tree):
        value = None
        if isinstance(node, ast.ImportFrom) and not node.level and node.module:
            if any(node.module + '.' + a.name in symbols for a in node.names):
                statements = []
                for alias in node.names:
                    target = mapped(node.module + '.' + alias.name)
                    parent, _, leaf = target.rpartition('.')
                    statements.append('from ' + parent + ' import ' + leaf +
                                      (' as ' + alias.asname if alias.asname else ''))
                value = '; '.join(statements)
            elif mapped(node.module) != node.module:
                value = ast.get_source_segment(text, node).replace(
                    'from ' + node.module + ' import', 'from ' + mapped(node.module) + ' import', 1)
        elif isinstance(node, ast.Import) and any(mapped(a.name) != a.name for a in node.names):
            statements = []
            for alias in node.names:
                target = mapped(alias.name)
                if target == alias.name or alias.asname:
                    statements.append('import ' + target + (' as ' + alias.asname if alias.asname else ''))
                elif '.' not in alias.name:
                    parent, _, leaf = target.rpartition('.')
                    statements.append('from ' + parent + ' import ' + leaf +
                                      (' as ' + alias.name if alias.name != leaf else ''))
                else:
                    parent = mapped(alias.name.split('.')[0])
                    statements.extend(['import ' + target, 'import ' + parent + ' as ' + alias.name.split('.')[0]])
            value = '; '.join(statements)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            original = node.value
            updated = original
            parent = parents.get(node)
            path_context = (isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div)
                or isinstance(parent, ast.Call) and (
                    isinstance(parent.func, ast.Name) and parent.func.id == 'Path'
                    or isinstance(parent.func, ast.Attribute) and parent.func.attr == 'joinpath'))
            if original in paths and path_context:
                updated = paths[original]
            elif original not in paths and re.fullmatch(r'[a-zA-Z_]\w*(?:\.\w+)+', original):
                updated = mapped(original)
            elif '\n' in original and ('import ' in original):
                try:
                    updated = rewrite(original, modules, paths, symbols)
                except (SyntaxError, IndentationError):
                    pass  # Prose/partial snippets are not executable modules.
            if updated != original:
                value = repr(updated)
        if value is not None:
            start = offsets[node.lineno - 1] + len(lines[node.lineno - 1].encode()[:node.col_offset].decode())
            end = offsets[node.end_lineno - 1] + len(lines[node.end_lineno - 1].encode()[:node.end_col_offset].decode())
            changes.append((start, end, value))
    # A rewritten code-string is a single AST leaf; ranges never overlap.
    for start, end, value in sorted(changes, reverse=True):
        text = text[:start] + value + text[end:]
    return text


def prepare(source: Path, destination: Path, repository: Path = ROOT) -> dict:
    source, destination = source.resolve(), destination.absolute()
    if destination.exists() or destination.is_relative_to(repository.resolve()):
        raise ValueError('NEW_EXTERNAL_TEST_DIRECTORY_REQUIRED')
    moves = json.loads((repository / 'architecture/module-moves.json').read_text())
    destination.mkdir(parents=True)
    records = {}
    for path in sorted(source.rglob('*')):
        if not path.is_file() or '__pycache__' in path.parts:
            continue
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = path.read_bytes()
        if path.suffix == '.py':
            target.write_text(rewrite(raw.decode(), moves['modules'], moves['paths'], moves.get('symbols')))
        else:
            shutil.copyfile(path, target)
        records[str(path.relative_to(source))] = {
            'original_sha256': hashlib.sha256(raw).hexdigest(),
            'qualified_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
        }
    (destination.parent / 'test-relocation.json').write_text(json.dumps(records, indent=2) + '\n')
    # Tests that resolve ../bin or ../fixtures need the same source tree. These
    # are explicit links, not another Git checkout; no runtime state is linked.
    for name in ('clinx', 'tools', 'scripts', 'fixtures', 'kernel', 'MonitorApp', 'bin',
                 'systemd', 'architecture', 'bridge.toml', 'requirements-dev.txt',
                 'requirements-discovery-dev.txt', 'requirements-discovery.txt', 'ruff-discovery.toml'):
        target = destination.parent / name
        original = (repository / name).resolve()
        if target.exists() or target.is_symlink():
            if target.resolve() != original:
                raise ValueError('QUALIFICATION_SOURCE_LINK_CONFLICT:' + name)
        else:
            target.symlink_to(original)
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'tests')
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps({'files': len(prepare(args.source, args.destination))}))


if __name__ == '__main__':
    main()
