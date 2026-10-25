#!/usr/bin/env python3
"""Legacy CLI name for the standalone Rust Centre service and control CLI."""
from __future__ import annotations
import os
import shutil
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
if not __package__:
    sys.path.insert(0, str(ROOT))


def daemon_binary():
    explicit = os.environ.get('CLINX_NODE_DAEMON_BINARY')
    node = os.environ.get('CLINX_NODE_BINARY')
    candidates = [explicit] if explicit else [
        str(Path(node).with_name('clinx-node-daemon')) if node else None,
        str(ROOT / 'clinx-node-daemon'), str(ROOT.parent / 'clinx-node-daemon'),
        str(ROOT.parent / 'Helpers/clinx-node-daemon'), shutil.which('clinx-node-daemon')]
    for candidate in candidates:
        if (candidate and Path(candidate).is_absolute() and Path(candidate).is_file()
                and os.access(candidate, os.X_OK)):
            return str(candidate)
    raise RuntimeError('RUST_NODE_DAEMON_REQUIRED')


def main(argv=None) -> int:
    binary = daemon_binary()
    os.execv(binary, [binary, *(sys.argv[1:] if argv is None else argv)])


if __name__ == '__main__':
    main()
