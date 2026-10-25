"""Existing identity/trust and pairing lifecycle on disposable private stores."""
import contextlib
import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest

from clinx.discovery.identity import DeviceError, NodeIdentity, PrivateStore, TrustedPeerStore
from clinx.discovery.pairing import PairingWindow, PRODUCTION_PROTOCOL, PRODUCTION_TRUST


def identities(tmp_path):
    return [NodeIdentity(PrivateStore(tmp_path / name), name) for name in ('alpha', 'beta')]


def digest(root):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir() if p.is_file()}


def test_read_only_peer_snapshot_creates_no_write_lock_and_never_mutates(tmp_path):
    a, b = identities(tmp_path)
    peers = TrustedPeerStore(a.store)
    peers.trust(b.public, b.certificate, security_status=PRODUCTION_TRUST,
        pairing_protocol=PRODUCTION_PROTOCOL, fresh_pairing=True)
    (a.store.root / 'store.lock').unlink()

    class ReadOnly(PrivateStore):
        def __init__(self, root):
            self.root = root
        @contextlib.contextmanager
        def lock(self, *args, **kwargs):
            yield
        def write(self, *args, **kwargs):
            raise DeviceError('READ_ONLY_OBSERVATION_IDENTITY')

    read = TrustedPeerStore(ReadOnly(a.store.root))
    before = digest(a.store.root)
    assert read.all()['beta']['trust_state'] == 'TRUSTED'
    with pytest.raises(DeviceError, match='READ_ONLY_OBSERVATION_IDENTITY'):
        read.authenticate('beta', b.public['public_key'])
    assert digest(a.store.root) == before
    assert not (a.store.root / 'store.lock').exists()


def test_rust_trust_persists_conflicts_revoke_and_authentication_without_key_material(tmp_path):
    a, b = identities(tmp_path)
    peers = TrustedPeerStore(a.store)
    peers.trust(b.public, b.certificate, security_status=PRODUCTION_TRUST,
        pairing_protocol=PRODUCTION_PROTOCOL, fresh_pairing=True)
    value = peers.authenticate('beta', b.public['public_key'])
    assert value['last_authenticated_at'] is not None
    with pytest.raises(DeviceError, match='IDENTITY_MISMATCH'):
        peers.authenticate('beta', a.public['public_key'])
    with pytest.raises(DeviceError, match='TRUST_DOWNGRADE_REJECTED'):
        peers.trust(b.public, b.certificate, security_status='DEV_ONLY')
    peers.revoke('beta')
    with pytest.raises(DeviceError, match='UNTRUSTED_DEVICE'):
        peers.authenticate('beta', b.public['public_key'])
    with pytest.raises(DeviceError, match='IDENTITY_MISMATCH'):
        peers.trust(b.public, b.certificate, security_status=PRODUCTION_TRUST,
            pairing_protocol=PRODUCTION_PROTOCOL, fresh_pairing=True)
    state = a.store.root / 'peers.json'
    assert 'private_key' not in state.read_text()
    assert state.stat().st_mode & 0o777 == 0o600


def test_pairing_commit_failure_is_exact_and_consumed_before_io():
    window = PairingWindow()
    code = window.open()
    reservation, _, actual = window.reserve()
    assert code == actual
    failure = OSError('isolated fixture disk failure')
    def commit():
        raise failure
    with pytest.raises(OSError) as error:
        window.complete(reservation, success=True, commit=commit)
    assert error.value is failure
    with pytest.raises(DeviceError, match='PAIRING_CLOSED'):
        window.reserve()
    with pytest.raises(DeviceError, match='PAIRING_REPLAY'):
        window.complete(reservation, success=True)
    window.close()


def test_pairing_window_concurrency_has_one_commit_and_no_secret_restoration():
    window = PairingWindow()
    window.open()
    reservation, _, _ = window.reserve()
    commits = []
    def complete(_):
        try:
            window.complete(reservation, success=True, commit=lambda: commits.append(True))
            return 'accepted'
        except DeviceError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(complete, range(8)))
    assert results.count('accepted') == len(commits) == 1
    assert results.count('PAIRING_REPLAY') == 7
    assert window._code is None and window._id is None
    fresh = PairingWindow()
    with pytest.raises(DeviceError, match='PAIRING_CLOSED'):
        fresh.reserve()
    window.close()
    fresh.close()


@pytest.mark.parametrize('held_lock', ['worker', 'runtime'])
def test_pairing_gc_finalizer_does_not_reenter_held_runtime_locks(held_lock):
    import subprocess
    import sys
    import textwrap
    # Run the forced-GC regression in its own bounded process so a reintroduced
    # finalizer deadlock cannot stall the qualification runner itself.
    script = textwrap.dedent('''
        import gc, sys, time, weakref
        from clinx.discovery import identity
        from clinx.discovery.pairing import PairingWindow
        gc.disable()
        window = PairingWindow()
        window_id = window._window
        reference = weakref.ref(window)
        window.cycle = window
        del window
        lock = identity._runtime_worker.lock if sys.argv[1] == 'worker' else identity._runtime_lock
        with lock:
            gc.collect()
            assert reference() is None
        deadline = time.monotonic() + 3
        while True:
            try:
                identity._state_call('window.snapshot', window=window_id)
            except identity.DeviceError as exc:
                assert str(exc) == 'PAIRING_CLOSED'
                break
            assert time.monotonic() < deadline, 'queued finalization was never processed'
            time.sleep(.01)
        # Cleanup must not consume a different active reservation or replay a
        # failed business commit while deleting the unreachable old window.
        active = PairingWindow()
        active.open()
        reservation, _, _ = active.reserve()
        calls = []
        def failed_commit():
            calls.append(True)
            raise OSError('isolated unknown commit')
        try:
            active.complete(reservation, success=True, commit=failed_commit)
        except OSError:
            pass
        else:
            raise AssertionError('lost exact commit exception')
        try:
            active.complete(reservation, success=True, commit=lambda: calls.append(True))
        except identity.DeviceError as exc:
            assert str(exc) == 'PAIRING_REPLAY'
        else:
            raise AssertionError('unknown commit was replayed')
        assert calls == [True]
        active.close()
    ''')
    result = subprocess.run([sys.executable, '-c', script, held_lock],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
