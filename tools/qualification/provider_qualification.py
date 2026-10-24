"""Disposable real-Codex endpoint for opt-in qualification; never the shared daemon."""
from contextlib import contextmanager
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import time
import tempfile
import shutil


@contextmanager
def isolated_provider(cfg, root, *, native_home=None):
    # Codex refuses to create its helper binaries below /tmp. Keep the
    # disposable provider home outside that special path when the caller did
    # not explicitly supply a native home, while still cleaning it on exit.
    qualification_home = None
    home = native_home or root / 'codex-home'
    if native_home is None and str(home).startswith('/tmp/'):
        qualification_home = tempfile.TemporaryDirectory(
            prefix='clinx-qual-home-', dir=Path(__file__).resolve().parent)
        home = Path(qualification_home.name)
    home.mkdir(exist_ok=True)
    source = Path(os.environ.get('CODEX_HOME', Path.home()/'.codex'))
    # Reuse machine authentication, but keep external MCP registrations out
    # of the disposable endpoint. Those connectors are outside this local
    # qualification and can otherwise hold a turn open behind the Host network
    # boundary. Preserve only model/provider settings plus the configured
    # authentication field required by the local provider. Never print or copy
    # this value outside the disposable home, and never load MCP registrations.
    auth = source / 'auth.json'
    if native_home is None and auth.exists():
        (home / 'auth.json').symlink_to(auth)
    config = source / 'config.toml'
    if config.exists():
        import tomllib
        values = tomllib.loads(config.read_text())
        lines = []
        for key in ('model_provider', 'model', 'model_reasoning_effort',
                    'personality', 'model_verbosity', 'model_reasoning_summary',
                    'approval_policy', 'approvals_reviewer', 'sandbox_mode',
                    'service_tier'):
            value = values.get(key)
            if value is not None:
                lines.append(f'{key} = {json.dumps(value)}')
        for name, provider in values.get('model_providers', {}).items():
            lines.append(f'[model_providers.\"{name}\"]')
            for key in ('name', 'base_url', 'wire_api', 'requires_openai_auth', 'experimental_bearer_token'):
                value = provider.get(key)
                if value is not None:
                    lines.append(f'{key} = {json.dumps(value)}')
        (home / 'config.toml').write_text('\n'.join(lines) + '\n')
    socket_dir = tempfile.TemporaryDirectory(prefix='clinx-qual-')
    endpoint = Path(socket_dir.name) / 'provider.sock'
    log = (root/'provider.log').open('wb')
    binary = (shutil.which('codex-raw') or cfg.codex_binary) if cfg.codex_binary == 'codex' else cfg.codex_binary
    provider_env = {**os.environ, 'CODEX_HOME': str(home)}
    original_path = os.environ.get('PATH')
    path_overridden = False
    # codex-raw resolves to a JavaScript app-server entrypoint whose shebang
    # uses env node.  Host service environments may omit the interactive NVM
    # PATH even when the registered machine has a valid node installation. Add
    # only that existing sibling bin directory; do not download or mutate the
    # provider/runtime installation.
    if shutil.which('node', path=provider_env.get('PATH')) is None:
        node_roots = sorted((Path.home() / '.nvm/versions/node').glob('*/bin'), reverse=True)
        node_bin = next((candidate for candidate in node_roots if (candidate / 'node').is_file()), None)
        if node_bin is not None:
            provider_env['PATH'] = str(node_bin) + os.pathsep + provider_env.get('PATH', '')
            if shutil.which('node', path=original_path) is None:
                os.environ['PATH'] = provider_env['PATH']
                path_overridden = True
    process = subprocess.Popen([binary, 'app-server', '--listen', 'unix://' + str(endpoint)],
        cwd=root, env=provider_env, stdout=log, stderr=log)
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
        if qualification_home is not None:
            qualification_home.cleanup()
        if path_overridden:
            if original_path is None:
                os.environ.pop('PATH', None)
            else:
                os.environ['PATH'] = original_path
