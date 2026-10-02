"""Disposable real-Codex endpoint for opt-in qualification; never the shared daemon."""
from contextlib import contextmanager
import dataclasses
import os
from pathlib import Path
import subprocess
import time
import tempfile
import shutil


@contextmanager
def isolated_provider(cfg, root, *, native_home=None):
    home = native_home or root / 'codex-home'
    home.mkdir(exist_ok=True)
    source = Path(os.environ.get('CODEX_HOME', Path.home()/'.codex'))
    # Reuse existing machine authentication/configuration without copying secrets.
    for name in ('config.toml', 'auth.json'):
        if native_home is None and (source/name).exists():
            (home/name).symlink_to(source/name)
    socket_dir = tempfile.TemporaryDirectory(prefix='clinx-qual-')
    endpoint = Path(socket_dir.name) / 'provider.sock'
    log = (root/'provider.log').open('wb')
    binary = (shutil.which('codex-raw') or cfg.codex_binary) if cfg.codex_binary == 'codex' else cfg.codex_binary
    process = subprocess.Popen([binary, 'app-server', '--listen', 'unix://' + str(endpoint)],
        cwd=root, env={**os.environ, 'CODEX_HOME': str(home)}, stdout=log, stderr=log)
    try:
        deadline = time.monotonic() + 15
        while not endpoint.exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError('isolated qualification endpoint did not start')
            time.sleep(0.02)
        yield dataclasses.replace(cfg, app_server=dataclasses.replace(cfg.app_server,
            local_socket=str(endpoint), native_socket=None, native_home=str(home), client_name='clinx-qualification',
            client_title='CLINX isolated qualification'))
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()
        socket_dir.cleanup()
