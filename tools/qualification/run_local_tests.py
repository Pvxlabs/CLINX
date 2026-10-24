"""Run an unchanged local suite through recorded import relocation in private state."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from tools.qualification.relocate_tests import ROOT, prepare


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--collect-only', action='store_true')
    parser.add_argument('files', nargs='*', help='Local test basenames, for example test_rust_request_contract.py')
    args = parser.parse_args()
    if not args.collect_only and not args.files:
        parser.error('select test files or --collect-only')
    if any(Path(name).name != name or not name.startswith('test_') or not name.endswith('.py') for name in args.files):
        parser.error('test files must be local test basenames')
    root = args.work_dir.absolute()
    if root.exists() or root.is_relative_to(ROOT):
        parser.error('use a new task-owned directory outside the repository')
    prepare(ROOT / 'tests', root / 'tests')
    env = {key: value for key, value in os.environ.items() if key in ('PATH', 'LANG', 'LC_ALL', 'TZ')}
    for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_DATA_HOME', 'XDG_RUNTIME_DIR', 'TMPDIR'):
        directory = root / 'state' / key.lower()
        directory.mkdir(parents=True, mode=0o700)
        env[key] = str(directory)
    env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1')
    command = [sys.executable, '-B', '-m', 'pytest', '-c', 'tests/pytest.ini', '-p', 'no:cacheprovider', '-q',
               '--basetemp=' + str(root/'pytest-temp'), '--junitxml=' + str(root/'result.xml')]
    if args.collect_only:
        command.append('--collect-only')
    command += ['tests/' + name for name in args.files] or ['tests']
    with (root/'pytest.log').open('w') as log:
        result = subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
    print(json.dumps({'exit_code': result.returncode, 'log': str(root/'pytest.log'),
                      'xml': str(root/'result.xml'), 'original_tests_modified': False}))
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
