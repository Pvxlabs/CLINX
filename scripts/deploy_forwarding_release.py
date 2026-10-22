"""Narrow user-service installer for the existing four-file P1 package.

Reuses the package's exact snapshots, blobs and CAS. No task database access,
global drain predicate, policy service, Owner or Provider lifecycle operation.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

if __package__:
    from .build_tunnel_release import encoded
    from .tunnel_release_cas import CASRefused, check, rooted, sha, state
else:
    from build_tunnel_release import encoded
    from tunnel_release_cas import CASRefused, check, rooted, sha, state

TUNNEL = 'clinx-tunnel.service'
HEALTH = 'clinx-tunnel-health.service'
TIMER = 'clinx-tunnel-health.timer'
PRESERVED = ('clinx-execution-owner.service', 'clinx-provider.service')


def atomic(path, data, mode):
    path = Path(path)
    if data is None:
        path.unlink()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.clinx-p1-')
        try:
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), mode)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def unpack(archive, destination, expected):
    if sha(Path(archive).read_bytes()) != expected:
        raise CASRefused('ARCHIVE_HASH_MISMATCH')
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        names = [m.name for m in members]
        if len(names) != len(set(names)) or any(
            not m.isfile() or PurePosixPath(m.name).is_absolute()
            or '..' in PurePosixPath(m.name).parts or m.mode & ~0o777
            for m in members
        ):
            raise CASRefused('REGULAR_RELATIVE_ARCHIVE_REQUIRED')
        destination.mkdir(parents=True, exist_ok=False)
        for member in members:
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(member).read())
            target.chmod(member.mode)


class Package:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.release = self.directory / 'release'
        self.rollback = self.directory / 'rollback'
        self.manifest = json.loads((self.release / 'manifest.json').read_text())
        self.plan = json.loads((self.rollback / 'plan.json').read_text())
        self.tplan = json.loads((self.rollback / 'tunnel/plan.json').read_text())
        if (self.manifest['schema'] != 'CLINX_FORWARDING_P1_RELEASE_V1'
                or self.plan['schema'] != 'CLINX_FORWARDING_P1_PACKAGE_V1'
                or sha((self.rollback / 'plan.json').read_bytes())
                != self.manifest['rollback_plan_sha256']
                or sha((self.rollback / 'tunnel/plan.json').read_bytes())
                != self.plan['tunnel_plan_sha256']):
            raise CASRefused('PACKAGE_PLAN_MISMATCH')
        home = str(Path.home())
        self.launcher = home + '/.local/lib/clinx-control/launch-mcp'
        exact = {home + '/.config/systemd/user/' + name for name in (
            TUNNEL, HEALTH, TUNNEL + '.d/99-p1-reliability.conf')}
        if (set(self.plan['changes']) != exact | {self.launcher}
                or set(self.tplan['changes']) != exact
                or self.tplan['home'] != home):
            raise CASRefused('EXACT_FOUR_FILE_SCOPE_REQUIRED')
        root = Path(home) / '.local/lib/clinx-control/releases'
        if (self.plan['installs']['control'] != str(root / self.plan['release_id'] / 'control')
                or self.plan['installs']['tunnel'] != str(root / self.tplan['release_id'])
                or not re.fullmatch(r'forwarding-p1-[0-9a-f]{20}', self.plan['release_id'])
                or not re.fullmatch(r'tunnel-reliability-p1-[0-9a-f]{20}', self.tplan['release_id'])):
            raise CASRefused('EXACT_RELEASE_TARGET_REQUIRED')
        actual_files = {str(p.relative_to(self.release)) for p in self.release.rglob('*')
                        if p.is_file()}
        if actual_files != set(self.manifest['files']) | {'manifest.json'}:
            raise CASRefused('PAYLOAD_FILE_SET_MISMATCH')
        for name, expected in self.manifest['files'].items():
            if state(rooted(self.release, '/' + name)) != expected:
                raise CASRefused('PAYLOAD_DRIFT: ' + name)
        for name, entry in self.plan['changes'].items():
            for direction in ('before', 'after'):
                data = self.blob(name, direction)
                if entry[direction] is not None and sha(data) != entry[direction]['sha256']:
                    raise CASRefused('ROLLBACK_BLOB_DRIFT: ' + name)
        if self.plan['changes'][self.launcher]['after'] != state(self.release / 'launch-mcp'):
            raise CASRefused('LOADER_PAYLOAD_MISMATCH')
        for name in exact:
            if self.plan['changes'][name] != self.tplan['changes'][name]:
                raise CASRefused('PAIRED_PLAN_MISMATCH')

    def blob(self, name, direction):
        expected = self.plan['changes'][name][direction]
        if expected is None:
            return None
        path = (self.rollback / ('loader.' + direction) if name == self.launcher
                else self.rollback / 'tunnel/blobs' / expected['sha256'])
        return path.read_bytes()

    def preflight(self, root, direction='forward', dependency_root='/'):
        expected = 'before' if direction == 'forward' else 'after'
        for name, entry in self.plan['changes'].items():
            if state(rooted(root, name)) != entry[expected]:
                raise CASRefused('STALE_PAIRED_STATE: ' + name)
        tplan = copy.deepcopy(self.tplan)
        # Loader is a managed member of this paired transaction, checked above.
        tplan['guards'].pop(self.launcher, None)
        check(tplan, self.rollback / 'tunnel', root, direction, guard_root=dependency_root)
        for name, expected_state in self.plan['control_base_guards'].items():
            path = str(Path(self.plan['base_source']) / name)
            if state(rooted(dependency_root, path)) != expected_state:
                raise CASRefused('INSTALLED_SOURCE_DRIFT: ' + name)

    def stage(self, root):
        for member, target in self.plan['installs'].items():
            source = self.release / member
            destination = rooted(root, target)
            expected = {str(p.relative_to(source)): state(p)
                        for p in source.rglob('*') if p.is_file()}
            if destination.exists():
                actual = {str(p.relative_to(destination)): state(p)
                          for p in destination.rglob('*') if p.is_file()
                          and '__pycache__' not in p.parts}
                if actual != expected:
                    raise CASRefused('EXISTING_RELEASE_DRIFT: ' + target)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(dir=destination.parent, prefix='.p1-release-'))
            try:
                for name in expected:
                    src, dst = source / name, temporary / name
                    atomic(dst, src.read_bytes(), expected[name]['mode'])
                os.rename(temporary, destination)
                fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)


class Services:
    def call(self, action, *names):
        if action != 'daemon-reload' and (action not in ('stop', 'start') or not names
                or set(names) - {TUNNEL, HEALTH, TIMER}):
            raise ValueError('UNSUPPORTED_SERVICE_OPERATION')
        if action == 'daemon-reload' and names:
            raise ValueError('RELOAD_TAKES_NO_UNITS')
        completed = subprocess.run(['systemctl', '--user', action, *names],
                                   capture_output=True, text=True, timeout=45)
        if completed.returncode:
            raise RuntimeError('systemctl ' + action + ' ' + ' '.join(names)
                               + ': ' + completed.stderr.strip())

    def snapshot(self):
        result = {}
        for name in (TUNNEL, HEALTH, TIMER, *PRESERVED):
            text = subprocess.check_output(['systemctl', '--user', 'show', name,
                '-p', 'MainPID', '-p', 'ActiveState', '-p', 'SubState', '-p', 'Result',
                '-p', 'NRestarts', '-p', 'ExecMainStartTimestampMonotonic'],
                text=True, timeout=5)
            result[name] = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
        return result


def wait_poll(state_dir, *, timeout=90, legacy=False):
    url_file = (Path.home() / '.local/state/tunnel-client/health/clinx-p620.url'
                if legacy else Path(state_dir) / 'tunnel.url')
    deadline = time.monotonic() + timeout
    samples = []
    first = None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        try:
            base = url_file.read_text().strip().rstrip('/')
            url = urllib.parse.urlsplit(base)
            if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1'):
                raise ValueError('LOOPBACK_HEALTH_URL_REQUIRED')
            with opener.open(base + '/metrics', timeout=3) as response:
                metrics = response.read(1048576).decode()
            values = [float(line.split()[-1]) for line in metrics.splitlines()
                      if re.match(r'^commands_poll_last_successful_timestamp_seconds(?:\{|\s)', line)]
            last = max(values) if values else 0
            age = time.time() - last
            if 0 <= age <= 30:
                first = last if first is None else first
                samples.append(dict(observed_at=time.time(), last_poll=last, poll_age=age))
                if last > first:
                    return dict(advancing=True, samples=samples[-2:])
        except (OSError, ValueError):
            pass
        time.sleep(2)
    raise RuntimeError('ADVANCING_FRESH_POLL_NOT_OBSERVED_WITHIN_' + str(timeout) + 'S')


class Installer:
    def __init__(self, package, receipt, services, *, root='/', health=None):
        self.package, self.path, self.services = package, Path(receipt), services
        self.root = Path(root)
        self.health = health or (lambda legacy: wait_poll(
            Path.home() / '.local/state/clinx/tunnel-reliability', legacy=legacy))
        self.record = {}

    def save(self, **fields):
        self.record.update(fields, updated_at=time.time())
        atomic(self.path, encoded(self.record), 0o600)

    def stop(self):
        self.services.call('stop', TIMER)
        self.services.call('stop', HEALTH)
        self.services.call('stop', TUNNEL)

    def start(self, legacy):
        self.services.call('daemon-reload')
        self.services.call('start', TUNNEL)
        proof = self.health(legacy)
        self.services.call('start', HEALTH)
        if self.record['before_services'][TIMER]['ActiveState'] == 'active':
            self.services.call('start', TIMER)
        current = self.services.snapshot()
        if (current[TUNNEL]['ActiveState'] != 'active'
                or current[HEALTH]['Result'] != 'success'
                or current[TIMER]['ActiveState'] != self.record['before_services'][TIMER]['ActiveState']):
            raise RuntimeError('PAIRED_SERVICE_HEALTH_FAILED')
        for name in PRESERVED:
            if current[name] != self.record['before_services'][name]:
                raise RuntimeError('PRESERVED_SERVICE_CHANGED: ' + name)
        return proof, current

    def validate_restore(self):
        attempted = set(self.record.get('attempted', []))
        for name, entry in self.package.plan['changes'].items():
            current = state(rooted(self.root, name))
            allowed = (entry['before'], entry['after']) if name in attempted else (entry['before'],)
            if current not in allowed:
                raise CASRefused('ROLLBACK_CONFLICT_NO_OVERWRITE: ' + name)

    def restore(self):
        self.validate_restore()
        for name in reversed(self.record.get('attempted', [])):
            entry = self.package.plan['changes'][name]
            target = rooted(self.root, name)
            if state(target) == entry['before']:
                continue
            if state(target) != entry['after']:
                raise CASRefused('ROLLBACK_CONFLICT_NO_OVERWRITE: ' + name)
            atomic(target, self.package.blob(name, 'before'),
                   entry['before']['mode'] if entry['before'] else 0)
        self.package.preflight(self.root)

    def activate(self):
        self.package.preflight(self.root)
        self.package.stage(self.root)
        self.save(state='PREPARED', release_id=self.package.plan['release_id'],
                  before_services=self.services.snapshot(), attempted=[])
        try:
            self.stop()
            self.package.preflight(self.root)
            for name, entry in sorted(self.package.plan['changes'].items()):
                target = rooted(self.root, name)
                if state(target) != entry['before']:
                    raise CASRefused('CONCURRENT_TARGET_CHANGE: ' + name)
                self.save(state='UPDATING', attempted=self.record['attempted'] + [name])
                atomic(target, self.package.blob(name, 'after'), entry['after']['mode'])
            self.package.preflight(self.root, 'rollback')
            self.save(state='STARTING')
            proof, current = self.start(False)
            self.save(state='DEPLOYED', health=proof, after_services=current)
        except Exception as error:
            self.save(state='ROLLBACK_REQUIRED', error=repr(error))
            try:
                self.stop()
                self.restore()
                proof, current = self.start(True)
                self.save(state='ROLLED_BACK', rollback_health=proof, after_services=current)
            except Exception as rollback_error:
                self.save(state='BLOCKED_ROLLBACK', rollback_error=repr(rollback_error))
            raise
        return self.record

    def rollback(self):
        self.record = json.loads(self.path.read_text())
        self.validate_restore()
        if self.record['state'] == 'ROLLED_BACK':
            self.package.preflight(self.root)
            return self.record
        self.stop()
        self.restore()
        proof, current = self.start(True)
        self.save(state='ROLLED_BACK', rollback_health=proof, after_services=current)
        return self.record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('activate', 'rollback'))
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--rollback', type=Path)
    parser.add_argument('--candidate-sha256')
    parser.add_argument('--rollback-sha256')
    args = parser.parse_args()
    backup = args.backup.absolute()
    owned = Path.home() / '.local/state/clinx/maintenance'
    if not backup.is_relative_to(owned) or rooted('/', str(backup)) != backup:
        raise ValueError('EXISTING_MAINTENANCE_BACKUP_ROOT_REQUIRED')
    if args.action == 'activate':
        backup.mkdir(mode=0o700, parents=True, exist_ok=False)
        for source, name in ((args.candidate, 'candidate.tar.gz'), (args.rollback, 'rollback.tar.gz')):
            shutil.copyfile(source, backup / name)
            (backup / name).chmod(0o600)
        unpack(backup / 'candidate.tar.gz', backup / 'package/release', args.candidate_sha256)
        unpack(backup / 'rollback.tar.gz', backup / 'package/rollback', args.rollback_sha256)
        for name in ('deploy_forwarding_release.py', 'build_tunnel_release.py',
                     'tunnel_release_cas.py', 'build_transport_runtime.py'):
            shutil.copyfile(Path(__file__).parent / name, backup / name)
    package = Package(backup / 'package')
    installer = Installer(package, backup / 'deployment-receipt.json', Services())
    result = installer.activate() if args.action == 'activate' else installer.rollback()
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
