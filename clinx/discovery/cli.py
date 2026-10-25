"""Compatibility launcher: production device operations execute only in Rust."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

COMMANDS = {'devices', 'pair', 'serve'}


def _binary():
    explicit = os.environ.get('CLINX_DEVICE_BINARY')
    node = os.environ.get('CLINX_NODE_BINARY')
    root = Path(__file__).resolve().parents[2]
    candidates = [Path(node).with_name('clinx-device')] if node else []
    candidates += [root.parent/'clinx-device', root.parent/'Helpers/clinx-device']
    value = explicit or next((str(p) for p in candidates if p.is_file()), None) or shutil.which('clinx-device')
    if not value or not Path(value).is_absolute() or not os.access(value, os.X_OK):
        raise ValueError('RUST_DEVICE_BINARY_REQUIRED')
    return value


def add_commands(sub):
    # Argument compatibility only; Rust rejects the retired development flag.
    for name in ('devices', 'pair', 'serve'):
        parser = sub.add_parser(name)
        parser.add_argument('--state-dir', type=Path)
        parser.add_argument('--display-name')
        parser.add_argument('--allow-dev-pairing', action='store_true')
        parser.add_argument('--wait', type=float, default=2.0)
        parser.add_argument('--json', action='store_true')
        if name == 'pair':
            parser.add_argument('device', nargs='?')
        if name in {'pair', 'serve'}:
            parser.add_argument('--observer-config', type=Path)
            parser.add_argument('--port', type=int, default=0)


def handle(args):
    argv = ['--config', str(args.config), args.command, '--wait', str(args.wait)]
    if args.allow_dev_pairing:
        argv.append('--allow-dev-pairing')
    for attr, flag in [('state_dir', '--state-dir'), ('display_name', '--display-name'),
                       ('observer_config', '--observer-config'), ('port', '--port')]:
        value = getattr(args, attr, None)
        if value is not None:
            argv += [flag, str(value)]
    if args.json:
        argv += ['--json']
    if getattr(args, 'device', None):
        argv += [args.device]
    try:
        return subprocess.call([_binary(), *argv])
    except (OSError, ValueError):
        print('DEVICE_ERROR: RUST_DEVICE_BINARY_REQUIRED', file=sys.stderr)
        return 2


def main():
    try:
        os.execv(_binary(), ['clinx', *sys.argv[1:]])
    except (OSError, ValueError):
        print('DEVICE_ERROR: RUST_DEVICE_BINARY_REQUIRED', file=sys.stderr)
        return 2
