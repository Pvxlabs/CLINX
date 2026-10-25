"""Rust first-pairing, identity and durable trust on disposable loopback nodes."""
import importlib.abc
import json
import ssl
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from clinx.discovery.discovery import Candidate
from clinx.discovery.identity import DeviceError,NodeIdentity,PrivateStore,TrustedPeerStore
from clinx.discovery.transport import DeviceServer,LanTransport,send,receive


def backend_probe(tmp_path, binary, expected):
    # Each fresh child selects its own binary. Never reuse the parent's worker
    # or inherit runtime credentials while testing dependency failure.
    source = '''
from clinx.discovery.identity import DeviceError
from clinx.discovery.pairing import backend_status, production_backend
import sys
expected = sys.argv[1]
if expected == 'available':
    assert backend_status() == 'OPAQUE_V1'
    assert production_backend().OPAQUE_KE_VERSION == '4.0.1'
else:
    assert backend_status() == 'BACKEND_UNAVAILABLE'
    try:
        production_backend()
    except DeviceError as exc:
        assert str(exc) == expected, str(exc)
    else:
        raise AssertionError('unavailable Rust backend accepted')
'''
    result = subprocess.run([sys.executable, '-B', '-c', source, expected],
        env={'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin',
             'PYTHONPATH': str(Path(__file__).resolve().parents[1]),
             'CLINX_NODE_BINARY': str(binary)},
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_rust_backend_binary_missing_fails_closed(tmp_path):
    from clinx.authority.request_pipe import binary
    backend_probe(tmp_path, binary(), 'available')
    backend_probe(tmp_path, tmp_path/'absent-clinx-node', 'PRODUCTION_PAKE_UNAVAILABLE')


@pytest.mark.parametrize('marker', [None, {},
    {'BACKEND_ID': 'unsupported', 'OPAQUE_KE_VERSION': '4.0.1'},
    {'BACKEND_ID': 'OPAQUE-3DH-RISTRETTO255-SHA512-ARGON2I-v1', 'OPAQUE_KE_VERSION': 'unknown'},
])
def test_rust_backend_marker_fails_closed_before_any_network_request(tmp_path, marker):
    worker = tmp_path/'invalid-backend'
    worker.write_text(f'''#!{sys.executable}
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    assert request['operation'] == 'discovery.opaque.status'
    print(json.dumps({{'id': request['id'], 'result': json.loads({json.dumps(marker)!r})}}), flush=True)
''')
    worker.chmod(0o700)
    backend_probe(tmp_path, worker, 'PRODUCTION_PAKE_VERSION_MISMATCH')


def nodes(root):
    identities=[NodeIdentity(PrivateStore(root/name),name) for name in ('alpha','beta')]
    return [(node,TrustedPeerStore(node.store)) for node in identities]


def candidate(identity,port):
    return Candidate(identity.public['node_id'],identity.public['display_name'],identity.public['fingerprint'],(('127.0.0.1',port),))


def test_normal_pairing_needs_no_python_crypto_ssl_or_extension_backend(tmp_path,monkeypatch):
    class NoPythonCrypto(importlib.abc.MetaPathFinder):
        def find_spec(self,fullname,*args):
            if fullname=='_clinx_opaque' or fullname.split('.')[0] in {'cryptography','spake2'}:
                raise AssertionError('production Python crypto import: '+fullname)
    monkeypatch.setattr(sys,'meta_path',[NoPythonCrypto(),*sys.meta_path])
    monkeypatch.setattr(ssl,'SSLContext',lambda *args,**kw:(_ for _ in ()).throw(AssertionError('production Python TLS')))
    (a,pa),(b,pb)=nodes(tmp_path)
    with DeviceServer(b,pb,address='127.0.0.1') as server:
        target=candidate(b,server.port)
        session=LanTransport(a,pa).pair(target,server.window.open())
        assert not session.authority_granted and session.authorization_required
        assert session.capabilities==()
        assert pa.all()['beta']['public_key']==b.public['public_key']
        assert pb.all()['alpha']['public_key']==a.public['public_key']
        assert not any(key in pa.all()['beta'] for key in ('private_key','code','pin','session_key'))
        for identity in (a,b):
            assert identity.store.root.stat().st_mode&0o777==0o700
            assert (identity.store.root/'identity.json').stat().st_mode&0o777==0o600
        with pytest.raises(DeviceError):
            LanTransport(a,pa).pair(target,'0000')
        restarted=NodeIdentity(PrivateStore(a.store.root),'alpha')
        assert LanTransport(restarted,TrustedPeerStore(restarted.store)).reconnect(target)==session
        with LanTransport(a,pa)._connect(target,pairing=False) as sock:
            send(sock,{'op':'host_execute'})
            with pytest.raises(DeviceError): receive(sock)


def test_concurrent_network_confirmation_has_one_durable_pair(tmp_path):
    (a,pa),(b,pb)=nodes(tmp_path)
    with DeviceServer(b,pb,address='127.0.0.1') as server:
        target=candidate(b,server.port);code=server.window.open()
        def attempt(_):
            try: LanTransport(a,pa).pair(target,code);return True
            except (DeviceError,ssl.SSLError):return False
        with ThreadPoolExecutor(4) as pool:
            assert sum(pool.map(attempt,range(4)))==1
        assert len(pa.all())==len(pb.all())==1


def test_tls_substitution_and_revocation_never_repair_or_repairing_implicitly(tmp_path):
    (a,pa),(b,pb)=nodes(tmp_path)
    with DeviceServer(b,pb,address='127.0.0.1') as server:
        target=candidate(b,server.port);client=LanTransport(a,pa)
        client.pair(target,server.window.open())
        wrong=NodeIdentity(PrivateStore(tmp_path/'other'),'other')
        with DeviceServer(wrong,TrustedPeerStore(wrong.store),address='127.0.0.1') as imposter:
            with pytest.raises(ssl.SSLError):
                client.reconnect(replace(target,endpoints=(('127.0.0.1',imposter.port),)))
        pb.revoke('alpha')
        with pytest.raises((DeviceError,ssl.SSLError)):
            client.reconnect(target)
        assert pb.all()['alpha']['trust_state']=='REVOKED'
