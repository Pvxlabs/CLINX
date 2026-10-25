"""Rust CLI qualification with empty interfaces/unit loopback; no LAN announces."""
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from clinx.discovery.discovery import _command
from clinx.discovery.identity import DeviceError
from clinx.discovery.observer_bootstrap import validate_connection

ROOT = Path(__file__).resolve().parents[1]
BIN = Path(os.environ.get('CLINX_DEVICE_BINARY', Path(os.environ.get('CLINX_NODE_BINARY', '/missing/clinx-node')).with_name('clinx-device')))


def test_native_cli_contract_and_launchers_need_no_python_runtime(tmp_path):
    result = subprocess.run([str(BIN), '--device-cli-contract'], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and result.stdout.strip() == 'RUST_DEVICE_CLI_V1'
    result = subprocess.run([str(ROOT/'bin/clinx'), '--help'], cwd=tmp_path,
        env=dict(os.environ, CLINX_DEVICE_BINARY=str(BIN), CLINX_PYTHON='/absent-python'),
        capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert 'devices|pair' in result.stdout
    result = subprocess.run([sys.executable, '-B', '-c', '''
import importlib.abc, sys
class NoRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, name, *args):
        if name.startswith(('clinx.discovery.identity','clinx.discovery.transport','clinx.discovery.pairing','tools.qualification')):
            raise AssertionError('Python runtime imported: '+name)
sys.meta_path.insert(0,NoRuntime())
from clinx.discovery.cli import main
sys.argv=['clinx','--help']
raise SystemExit(main())
'''], env=dict(os.environ, CLINX_DEVICE_BINARY=str(BIN)), capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert 'devices|pair' in result.stdout


@pytest.mark.parametrize('arguments,code', [
    (['pair','accept','--json'], 'PAIRING_REQUIRES_LOCAL_TTY'),
    (['pair','accept'], 'PAIRING_REQUIRES_LOCAL_TTY'),
    (['devices','--wait','nan'], 'INVALID_DISCOVERY_INTERVAL'),
    (['devices','--wait','31'], 'INVALID_DISCOVERY_INTERVAL'),
    (['devices','--allow-dev-pairing'], 'SECURITY_BLOCKER_DEV_ONLY'),
])
def test_native_cli_rejects_invalid_inputs_before_state_or_network(tmp_path, arguments, code):
    state = tmp_path/'not-created'
    result = subprocess.run([str(BIN),'--config',str(tmp_path/'missing.toml'),*arguments,'--state-dir',str(state)],
        capture_output=True,text=True,timeout=5)
    assert result.returncode == 2
    assert result.stderr.strip() == 'DEVICE_ERROR: '+code
    assert not result.stdout and not state.exists()


def test_shell_launcher_preserves_argument_values_without_python(tmp_path):
    recorder = tmp_path/'record-cli'
    recorder.write_text(f'#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
    recorder.chmod(0o700)
    args = ['--config', str(tmp_path/'a config.toml'), 'devices', '--display-name', 'A device', '--json']
    result = subprocess.run([str(ROOT/'bin/clinx'),*args],cwd=tmp_path,
        env=dict(os.environ,CLINX_DEVICE_BINARY=str(recorder),CLINX_PYTHON='/absent-python'),capture_output=True,text=True,timeout=5)
    assert result.returncode == 0,result.stderr
    assert json.loads(result.stdout)==['--config',str(ROOT/'bridge.toml'),*args]


def old_url_codec(raw):
    try:
        url=urlsplit(raw['endpoint'])
        return _command('validate_connection',endpoint=raw['endpoint'],credential=raw['credential'],
            url=dict(scheme=url.scheme,hostname=url.hostname,username=url.username,password=url.password,
                     path=url.path,query=url.query,fragment=url.fragment,port=url.port))
    except (ValueError,KeyError,TypeError,AttributeError):
        raise DeviceError('INVALID_OBSERVER_CONNECTION') from None


def outcome(call, value):
    try:return ('ok',call(value))
    except DeviceError as error:return ('error',str(error))


def test_url_codec_matches_original_urllib_fields_and_existing_policy():
    hosts=['p620.ts.net','P620.TS.NET','台式机.ts.net','abc.local','127.0.0.1','0177.0.0.1','0x7f000001',
           '10.0.0.1','100.64.0.1','[::1]','[fe80::1%En0]','[::ffff:127.0.0.1]','[v1.device.ts.net]',
           '[127.0.0.1]','before[::1]','[::1]after','[::1','::1]','p620.ts.net\uff0fother','p620.ts.net\u2100']
    endpoints=[scheme+host+tail for scheme in ['https://','HTTPS://','http://',' \r\nhttps://']
        for host in hosts for tail in ['', '/', ':443', ':', ':0', ':bad', ':65536', '?', '#', '/path']]
    endpoints += ['https://u@p620.ts.net','https://:p@p620.ts.net','https://p620.ts.net\t',
                  'https://p620.ts.net#?x','https://p620.ts.net?x#','https://[fe80::1%]','https://[fe80::1%x%y]']
    for endpoint in endpoints:
        raw=dict(endpoint=endpoint,credential='x'*48)
        assert outcome(validate_connection,raw)==outcome(old_url_codec,raw),repr(endpoint)
    for raw in [None, [], {}, {'endpoint':None,'credential':'x'*48}, {'endpoint':'https://p620.ts.net','credential':None}]:
        assert outcome(validate_connection,raw)==outcome(old_url_codec,raw)
