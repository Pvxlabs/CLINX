#!/usr/bin/env python3
"""Bounded, offline qualification of the repaired NON_AUTHORITATIVE lab runtime.

Reuses P4 identity, P3 fixtures, frozen Python oracle and bwrap peers. Never imports
production authority or opens a live database. Output must be a new directory.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from tools.qualification.runtime_lab.reference import CONTRACT, Reference  # noqa: E402
from scripts import check_execution_core_contract as v  # noqa: E402
from scripts.qualify_runtime_shadow import (  # noqa: E402
    Peer,
    distribution,
    dump,
    same,
    sandbox,
    sha,
)
from test_rust_runtime_lab import (  # noqa: E402
    ack_row,
    allocation_trace,
    event,
    record,
    req,
    trace,
)


def log(message):
    print(message, flush=True)


class Qualification:
    def __init__(self, output, prior):
        self.out = output
        self.prior = prior
        output.mkdir(parents=True, exist_ok=False)
        self.scratch = output / 'scratch'
        self.scratch.mkdir()
        (output / 'bin').mkdir()
        self.binary = output / 'bin/clinx-runtime-lab'
        self.commands = []
        self.resources = []
        self.serial = 0
        identity = json.loads((prior / 'reproduction-source-hashes.json').read_text())
        for name, expected in identity.items():
            if sha(ROOT / name) != expected:
                raise RuntimeError(f'candidate source drift: {name}')
        receipt = json.loads((prior / 'receipt.json').read_text())
        assert receipt['status'] == 'SHADOW_CONFORMANCE_PASS'
        shutil.copy2(prior / 'bin/clinx-runtime-lab', self.binary)
        assert sha(self.binary) == receipt['binary_sha256']
        # Include all transitive Rust inputs, the probe, oracle, fixtures and runner.
        for p in [*sorted((ROOT / 'kernel/src').rglob('*.rs')),
                  ROOT / 'kernel/Cargo.lock', ROOT / 'tools/qualification/runtime_lab/shadow_probe.rs',
                  Path(__file__).resolve()]:
            identity[str(p.relative_to(ROOT))] = sha(p)
        self.identity = identity
        dump(output / 'identity.json', dict(
            sources=identity, binary_sha256=sha(self.binary),
            reused_shadow_receipt_sha256=sha(prior / 'receipt.json'),
            reused_full_ledger_sha256=sha(prior / 'full-ledger-summary.json'),
            shadow=receipt['counts'], full_ledger_prefixes=receipt['full_ledger_equal_prefixes'],
            authority='NON_AUTHORITATIVE', deployment='NOT_DEPLOYED',
            platform=platform.platform(), python=sys.version,
            loadavg=os.getloadavg(), cpu_count=os.cpu_count(),
            head=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip(),
        ))
        with tarfile.open(output / 'source.tar.gz', 'w:gz') as archive:
            for name in identity:
                archive.add(ROOT / name, arcname=name)
        shutil.copy2(prior / 'receipt.json', output / 'reused-shadow-receipt.json')
        shutil.copy2(prior / 'full-ledger-summary.json', output / 'reused-full-ledger.json')

    def directory(self, label):
        self.serial += 1
        return self.scratch / f'{self.serial:04d}-{label}'

    def peer(self, directory, init=False):
        p = Peer(self.out, [self.binary, '--lab-dir', directory] + (['--init'] if init else []))
        self.commands.append(p.command)
        return p

    def close(self, peer, label):
        self.resources.append(dict(label=label, **peer.close()))

    def process(self, directory, commands, *, init=False, crash=None, inject=False):
        inner = [str(self.binary), '--lab-dir', str(directory)] + (['--init'] if init else [])
        if inject:
            inner = ['strace', '-qq', '-o', str(self.out / f'pre-sync-{self.serial}.strace'),
                     '-e', 'trace=write,fsync', '-e', 'inject=fsync:signal=SIGKILL:when=1', *inner]
        command = sandbox(self.out, inner)
        if crash:
            pos = command.index('--')
            command[pos:pos] = ['--setenv', 'CLINX_RUNTIME_LAB_CRASH', crash]
        self.commands.append(command)
        p = subprocess.run(command, input=''.join(v.canonical(r) + '\n' for r in commands),
                           text=True, capture_output=True, timeout=40)
        results = [json.loads(line)['result'] for line in p.stdout.splitlines()]
        return dict(command=command, exit_code=p.returncode, results=results, stderr=p.stderr)

    def audit(self, directory, reference):
        restored = Reference()
        previous = ''
        raw = (directory / 'events.ndjson').read_bytes()
        for index, line in enumerate(raw.splitlines(), 1):
            frame = json.loads(line)
            assert set(frame) == {'position', 'previous', 'request', 'hash'}
            assert frame['position'] == index and frame['previous'] == previous
            assert frame['hash'] == v.digest({k: frame[k] for k in ('position', 'previous', 'request')})
            previous = frame['hash']
            restored.execute(frame['request'])
            assert frame['request']['request_id'] in restored.receipts
        assert same(restored.__dict__, reference.__dict__)
        return dict(journal_sha256=sha(directory / 'events.ndjson'),
                    frames=len(raw.splitlines()), head_hash=previous,
                    full_reference_ledger_equal=True)

    def checked(self, directory, reference, command, label):
        expected = reference.execute(command)
        p = self.peer(directory)
        actual = p.call(command)
        snapshot = p.call(req('inspect', {}, 'read-prefix'))
        assert same(actual, expected) and same(snapshot['snapshot'], reference.snapshot()), label
        self.close(p, label)
        # A new process for each prefix and a durable receipt readback.
        before = sha(directory / 'events.ndjson')
        p = self.peer(directory)
        assert same(p.call(req('inspect', {}, 'reopened'))['snapshot'], reference.snapshot())
        retried = None
        if command['request_id'] in reference.receipts:
            retried = p.call(command)
            assert same(retried, reference.execute(command))
            assert retried['verdict'] == 'DUPLICATE'
        self.close(p, label + '-reopen')
        assert sha(directory / 'events.ndjson') == before
        return dict(label=label, request=command, python=expected, rust=actual,
                    snapshot=snapshot, receipt_retry=retried, **self.audit(directory, reference))

    def crash_matrix(self):
        rows = []
        es, allocations = trace(full=True, final=True), allocation_trace(release=True)
        row, ack = ack_row()
        checkpoint = copy.deepcopy(es[5]['payload'])
        checkpoint['facts'].update(finalization_phase='RESULT_COMMITTED', commit_bundle=['result'])
        checkpoint['facts'].pop('observation')
        commands = [
            req('append', {'events': [e]}, f'execution-{i}') for i, e in enumerate(es[:5])
        ]
        commands += [req('append', {'events': [e]}, f'grant-{i}') for i, e in enumerate(allocations[:2])]
        commands += [req('record_delivery', {'row': row}, 'delivery'),
                     req('append', {'events': [es[5]]}, 'missing-ack-terminal'),
                     req('late_ack', {'ack': {**ack, 'success': False}}, 'lost-ack'),
                     req('late_ack', {'ack': {**ack, 'generation': 99}}, 'stale-ack'),
                     req('checkpoint', {'record': checkpoint}, 'result'),
                     req('late_ack', {'ack': ack}, 'late-exact-ack')]
        bad = copy.deepcopy(es[5])
        bad['payload']['facts']['commit_bundle'].remove('receipt')
        bad['payload_hash'] = v.digest(bad['payload'])
        commands += [req('append', {'events': [bad]}, 'partial-terminal'),
                     req('append', {'events': [es[5]]}, 'terminal')]
        commands += [req('append', {'events': [e]}, f'release-{i}')
                     for i, e in enumerate(allocations[2:])]
        dump(self.out / 'crash-requests.json', commands)
        ref = Reference()
        directory = self.directory('all-prefixes')
        p = self.peer(directory, True)
        self.close(p, 'init-prefixes')
        for index, command in enumerate(commands):
            rows.append(self.checked(directory, ref, command, f'normal-prefix-{index + 1}'))
        assert sum(e['payload']['action'] == 'dispatch' for e in ref.events) == 1
        assert ref.snapshot()['event_count'] == 10
        # Crash around each operation that actually persists, including delivery,
        # original result, ACK repair, terminal and each release phase.
        matrix = []
        prefix = Reference()
        for index, command in enumerate(commands):
            trial = copy.deepcopy(prefix)
            trial.execute(command)
            persists = command['request_id'] in trial.receipts and command['request_id'] not in prefix.receipts
            if persists:
                for crash in ('BEFORE_APPEND', 'AFTER_SYNC', 'PRE_SYNC_SIGKILL', 'TORN_APPEND'):
                    d = self.directory(f'crash-{index}-{crash}')
                    seeded = self.process(d, commands[:index], init=True)
                    assert seeded['exit_code'] == 0
                    killed = self.process(d, [command], crash=crash if crash != 'PRE_SYNC_SIGKILL' else None,
                                          inject=crash == 'PRE_SYNC_SIGKILL')
                    assert killed['exit_code'] != 0 and killed['results'] == []
                    if crash in ('BEFORE_APPEND', 'AFTER_SYNC', 'TORN_APPEND'):
                        assert killed['exit_code'] == 86
                    if crash == 'TORN_APPEND':
                        reopened = self.process(d, [req('inspect', {}, 'no-projection')])
                        assert reopened['exit_code'] != 0 and reopened['results'] == []
                        matrix.append(dict(prefix=index + 1, crash=crash, killed=killed,
                                           reopened=reopened, status='PASS_FAIL_CLOSED',
                                           journal_sha256=sha(d / 'events.ndjson')))
                        continue
                    # A process kill before fsync can leave a complete frame in page
                    # cache. This is observed survival, never a power-loss guarantee.
                    expected = copy.deepcopy(prefix if crash == 'BEFORE_APPEND' else trial)
                    p = self.peer(d)
                    actual = p.call(req('inspect', {}, 'after-crash'))
                    assert same(actual['snapshot'], expected.snapshot())
                    self.close(p, f'crash-{index}-{crash}')
                    audit_before = self.audit(d, expected)
                    retry = self.checked(d, expected, command, f'retry-{index}-{crash}')
                    assert retry['rust']['verdict'] == ('DUPLICATE' if crash != 'BEFORE_APPEND' else trial.execute(command)['receipt']['verdict'])
                    assert sum(e['payload']['action'] == 'dispatch' for e in expected.events) <= 1
                    matrix.append(dict(prefix=index + 1, crash=crash, killed=killed,
                                       recovered=actual, retry=retry, before_retry=audit_before, status='PASS'))
            prefix.execute(command)
        dump(self.out / 'crash-matrix.json', dict(
            status='PASS', normal_prefixes=rows, crash_cases=matrix,
            logical_dispatch_count=1, external_dispatches=0,
            limits=['process exit/SIGKILL only; no machine power-loss or storage-controller test',
                    'event and receipt share one atomic lab frame; no separate production receipt table',
                    'PRE_SYNC_SIGKILL observes complete frame survival in OS page cache',
                    'torn tail is rejected, automatic tail repair NOT_COVERED']))
        log(f'crash PASS: {len(rows)} prefixes, {len(matrix)} crash cases')

    def traces(self, native):
        receipts_path = self.prior / 'qualification-final/protocol-receipts.sanitized.json'
        receipts = json.loads(receipts_path.read_text())
        for item in receipts:
            assert sha(Path(item['source'])) == item['source_sha256']
        # Bounded safe source: one existing real-native qualification receipt.
        # Export state/identity relations only; no prompt, raw_result, paths,
        # workspace_contract, policy, commands, summary or timestamps.
        data = json.loads(native.read_text())
        tokens = {}

        def anon(value):
            if value is None:
                return None
            if value not in tokens:
                tokens[value] = f'anon-native-{len(tokens) + 1}'
            return tokens[value]

        real = []
        for index, execution in enumerate(data['executions']):
            start, result = execution['start'], execution['result']
            sample = dict(kind='existing_real_native_receipt',
                          execution_started=start['execution_started'],
                          dispatch_status=start['dispatch_status'], status=result['status'],
                          start_execution=anon(start['execution_ref']),
                          result_execution=anon(result['execution_ref']),
                          task_id=anon(result['task_id']), turn_id=anon(result['turn_id']))
            real.append(dict(source=str(native), source_sha256=sha(native), record_index=index,
                             sample=sample, classification='REAL_NATIVE_RECEIPT; incomplete V1 envelope'))
        dump(self.out / 'native-receipts.sanitized.json', real)
        dump(self.out / 'protocol-receipts.sanitized.json', receipts)
        directory = self.directory('receipt-replay')
        p = self.peer(directory, True)
        reference, rows = Reference(), []
        for index, item in enumerate([*receipts, *real]):
            command = req('append', {'events': [item['sample']]}, f'receipt-{index}')
            expected, actual = reference.execute(command), p.call(command)
            assert expected == actual == {'verdict': 'REJECT'}
            rows.append(dict(source=item['source'], source_sha256=item['source_sha256'],
                             python=expected, rust=actual))
        assert p.call(req('inspect', {}, 'empty'))['snapshot']['event_count'] == 0
        self.close(p, 'receipts')
        dump(self.out / 'trace-coverage.json', dict(
            protocol_receipts=len(receipts), real_native_receipts=len(real),
            full_real_v1_envelopes=0, rejection_boundary='PASS',
            real_execution_semantic_replay='NOT_COVERED', comparisons=rows,
            discovery='bounded to prior sanitized protocol receipts and one existing real-native receipt',
            missing=['family/schema reader and complete immutable V1 Event envelope',
                     'Attempt/owner epoch/version/transaction bundle baseline'],
            limits='No synthesized envelopes or historical upgrader; no live Provider/Host or production payload query'))
        log(f'trace boundary PASS: {len(receipts)} protocol + {len(real)} real-native receipts; V1 NOT_COVERED')

    def fencing(self):
        d = self.directory('fencing')
        old = self.peer(d, True)
        events = trace(full=True, final=True) + allocation_trace(release=True)[:2]
        seed = req('append', {'events': events}, 'seed')
        assert old.call(seed)['verdict'] == 'APPLY'
        live_denied = self.process(d, [req('inspect', {}, 'new-writer')])
        assert live_denied['exit_code'] != 0 and not live_denied['results']
        # Locate exact runtime pid; bwrap PID may be a wrapper. Signal only our child.
        runtime_pid = self.exact_process(old)[0]
        os.kill(runtime_pid, signal.SIGSTOP)
        try:
            stopped_denied = self.process(d, [req('inspect', {}, 'stopped-writer')])
            assert stopped_denied['exit_code'] != 0 and not stopped_denied['results']
            original = d / 'events.ndjson'
            old_inode = original.stat().st_ino
            displaced = d / 'old-inode.ndjson'
            original.rename(displaced)
            shutil.copyfile(displaced, original)
            new_inode = original.stat().st_ino
            assert new_inode != old_inode
            new = self.peer(d)
            new_state = new.call(req('inspect', {}, 'successor'))
            # New worker/epoch writes on replacement inode; exact same resource key.
            grant = copy.deepcopy(allocation_trace(release=True)[0]['payload'])
            grant['id'] = 'new-allocation'
            for field in ('stored_owner', 'presented_owner'):
                grant['facts'][field].update(execution_id='e2', attempt_id='a2',
                                             assignment_id='as2', worker_id='w2',
                                             incarnation_id='i2', resource_epoch=3)
            grant['facts']['previous_epoch'] = 2
            grant_command = req('append', {'events': [event(grant, 1, full=True)]}, 'epoch-3')
            new_result = new.call(grant_command)
            assert new_result['verdict'] == 'APPLY'
            new_hash = sha(original)
            old_hash = sha(displaced)
            os.kill(runtime_pid, signal.SIGCONT)
            # Frozen guard facts are supplied evidence; stop1/fence1 are strings,
            # not verified OS revocation. The old worker can still commit release
            # against its old inode while the successor owns epoch 3 elsewhere.
            release = req('append', {'events': allocation_trace(release=True)[2:]}, 'old-release')
            old_result = old.call(release)
            assert old_result['verdict'] == 'APPLY'
            assert sha(displaced) != old_hash and sha(original) == new_hash
            successor_after = new.call(req('inspect', {}, 'new-after-old-release'))
            assert same(successor_after, new_result)
            self.close(new, 'fencing-successor')
            self.close(old, 'fencing-old')
        finally:
            try:
                os.kill(runtime_pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            for peer in (old, locals().get('new')):
                if peer is not None and peer.p.poll() is None:
                    peer.p.kill()
                    peer.p.wait(timeout=10)
        # Same-UID shared resource capability probe. This is an OS boundary test,
        # not an implemented Runtime storage adapter or a real provider effect.
        resource = d / 'shared-resource'
        resource.write_bytes(b'old-epoch')
        fd = os.open(resource, os.O_WRONLY)
        resource.write_bytes(b'new-epoch')
        os.pwrite(fd, b'STALE-OLD', 0)
        after_stale_write = resource.read_text()
        os.close(fd)
        resource.unlink()
        dump(self.out / 'fencing.json', dict(
            status='BLOCKED', same_inode_exclusion='PASS', stopped_old_writer_blocks_takeover='PASS',
            live_contender=live_denied, stopped_contender=stopped_denied,
            old_inode=old_inode, replacement_inode=new_inode, successor_initial=new_state,
            successor_epoch_3=new_result, delayed_old_release=old_result,
            successor_after_old_release=successor_after,
            old_inode_still_writable=True, replacement_inode_unchanged=True,
            shared_resource_capability_probe=dict(stale_write_observed=after_stale_write,
                                                  same_uid_unlink_succeeded=not resource.exists()),
            interpretation='flock fences one inode only; no physical worker/epoch capability revocation; no supported takeover adapter',
            production_storage_os_constraints='NOT_COVERED',
            boundary='Task-owned files only. Path replacement is an adversarial probe, not supported takeover. Shared-resource probe is OS-only, not runtime integration.'))
        log('fencing BLOCKED: same-inode exclusion PASS; epoch capability revocation absent')

    def exact_process(self, peer):
        pending = [peer.p.pid]
        while pending:
            pid = pending.pop()
            path = Path(f'/proc/{pid}')
            try:
                pending.extend(int(x) for x in (path / f'task/{pid}/children').read_text().split())
                if (path / 'exe').resolve() == self.binary.resolve():
                    return pid, path
            except (FileNotFoundError, ProcessLookupError):
                continue
        raise RuntimeError('exact runtime process not found')

    def observation(self, peer):
        pid, path = self.exact_process(peer)
        status = dict(line.split(':', 1) for line in (path / 'status').read_text().splitlines())
        stat = (path / 'stat').read_text().split()
        return dict(pid=pid, executable_sha256=sha(path / 'exe'),
                    rss_kib=int(status['VmRSS'].split()[0]),
                    hwm_kib=int(status['VmHWM'].split()[0]),
                    fd_count=len(list((path / 'fd').iterdir())),
                    user_ticks=int(stat[13]), system_ticks=int(stat[14]),
                    monotonic_ns=time.monotonic_ns())

    def performance(self, recovery_samples):
        assert recovery_samples >= 1000, 'p99 recovery gate requires 1000 observations'
        records = [v.materialize(CONTRACT, c['record']) for c in CONTRACT['cases']]
        dump(self.out / 'benchmark-records.json', records)
        # Build only the current library and the existing timing probe. Runtime
        # timings use the exact already-qualified candidate binary, not a rebuild.
        command = sandbox(self.out, ['/home/pvxlabs/.cargo/bin/cargo', 'build', '--release',
                                      '--locked', '--offline', '--lib', '-j', '2',
                                      '--manifest-path', str(ROOT / 'kernel/Cargo.toml')])
        pos = command.index('--')
        command[pos:pos] = ['--setenv', 'HOME', '/home/pvxlabs', '--setenv', 'PATH',
                            '/home/pvxlabs/.cargo/bin:/usr/bin:/bin', '--setenv',
                            'CARGO_TARGET_DIR', str(self.out / 'target')]
        self.commands.append(command)
        built = subprocess.run(command, text=True, capture_output=True, timeout=180)
        (self.out / 'build-probe-library.log').write_text(built.stdout + built.stderr)
        assert built.returncode == 0
        deps = self.out / 'target/release/deps'
        library = next(deps.glob('libclinx_decision_kernel-*.rlib'))
        probe = self.out / 'bin/shadow-core-probe'
        compile_cmd = ['/home/pvxlabs/.cargo/bin/rustc', '--edition=2024', '-C', 'opt-level=3',
                       str(ROOT / 'tools/qualification/runtime_lab/shadow_probe.rs'), '--extern',
                       f'clinx_decision_kernel={library}', '--extern',
                       f'serde_json={next(deps.glob("libserde_json-*.rlib"))}',
                       '-L', f'dependency={deps}', '-o', str(probe)]
        self.commands.append(compile_cmd)
        compiled = subprocess.run(compile_cmd, text=True, capture_output=True, timeout=60)
        (self.out / 'compile-probe.log').write_text(compiled.stdout + compiled.stderr)
        assert compiled.returncode == 0
        result = subprocess.run(sandbox(self.out, [probe, self.out / 'benchmark-records.json']),
                                text=True, capture_output=True, timeout=30, check=True)
        core = json.loads(result.stdout)
        dump(self.out / 'raw-core.json', core)
        assert core['verdicts'] == [v.check_transition(CONTRACT, records[i % 35]) for i in range(20000)]
        perf = dict(rust_core=distribution(core['ns']))
        directory = self.directory('protocol')
        p = self.peer(directory, True)
        for i in range(100):
            p.call(req('evaluate', {'record': records[i % 35]}, f'warm-{i}'))
        for i in range(2000):
            assert p.call(req('evaluate', {'record': records[i % 35]}, f'measure-{i}')) == {'verdict': core['verdicts'][i]}
        dump(self.out / 'raw-protocol.json', p.latencies[-2000:])
        perf['rust_protocol_rtt'] = distribution(p.latencies[-2000:])
        self.close(p, 'protocol-perf')
        heartbeats = []
        for i in range(128):
            r = record('RuntimeWorker', 'bench-worker', 'REGISTERED' if i == 0 else 'HEALTHY',
                       'heartbeat', 'HEALTHY', 'registry', i,
                       dict(current_incarnation='bench-incarnation', presented_incarnation='bench-incarnation'))
            heartbeats.append(req('append', {'events': [event(r, i + 1, full=True)]}, f'hb-{i}'))
        dump(self.out / 'benchmark-append-requests.json', heartbeats)
        rounds = []
        for round_id in range(9):
            directory = self.directory('append')
            p = self.peer(directory, True)
            reference = Reference()
            for command in heartbeats:
                assert same(p.call(command), reference.execute(command))
            rounds.append(dict(round=round_id, warmup=round_id == 0, ns=p.latencies[-128:],
                               journal_bytes=(directory / 'events.ndjson').stat().st_size))
            self.close(p, f'append-{round_id}')
        dump(self.out / 'raw-append.json', rounds)
        perf['rust_append_depth_1_128'] = distribution([n for row in rounds[1:] for n in row['ns']])
        # 32-event replay keeps 1000 startup-inclusive observations bounded.
        recovery_dir = self.directory('replay32')
        p = self.peer(recovery_dir, True)
        recovery_ref = Reference()
        for command in heartbeats[:32]:
            assert same(p.call(command), recovery_ref.execute(command))
        self.close(p, 'recovery-seed')
        recoveries = []
        for i in range(recovery_samples + 10):
            p = self.peer(recovery_dir)
            out = p.call(req('inspect', {}, 'recovered'))
            elapsed = time.perf_counter_ns() - p.started
            assert same(out['snapshot'], recovery_ref.snapshot())
            recoveries.append(dict(index=i, warmup=i < 10, ns=elapsed))
            self.close(p, 'recovery32')
            if i % 100 == 0:
                log(f'recovery samples {i}/{recovery_samples + 10}')
        dump(self.out / 'raw-recovery32.json', recoveries)
        perf['rust_recovery32_including_startup'] = distribution([x['ns'] for x in recoveries if not x['warmup']])
        # A second fixed depth establishes the scale boundary without inventing p99.
        recovery128 = []
        for i in range(105):
            p = self.peer(directory)
            assert same(p.call(req('inspect', {}, 'read'))['snapshot'], reference.snapshot())
            recovery128.append(dict(warmup=i < 5, ns=time.perf_counter_ns() - p.started))
            self.close(p, 'recovery128')
        dump(self.out / 'raw-recovery128.json', recovery128)
        perf['rust_recovery128_including_startup'] = distribution([r['ns'] for r in recovery128 if not r['warmup']])
        perf['identity'] = dict(binary_sha256=sha(self.binary), probe_sha256=sha(probe),
                                library_rlib_sha256=sha(library), records_sha256=sha(self.out / 'benchmark-records.json'),
                                append_inputs_sha256=sha(self.out / 'benchmark-append-requests.json'))
        perf['methodology'] = dict(
            core='20000 direct transition calls, pre-parsed frozen 35 records, 350 warmup, current library',
            protocol='2000 resident sequential evaluate RTT, 100 warmup; preencoded write through response decode; no startup',
            append='8 measured rounds of 128 increasing-depth fsync appends; one discarded warmup round; oracle think-time excluded',
            recovery='1000 measured fixed-depth32 fresh process replays, 10 warmup; depth128 n100 + 5 warmup. Includes bwrap/startup/inspect',
            quantiles='empirical nearest-rank; p95 n>=100; p99 n>=1000; no confidence/SLO/production capacity claim',
            environment='local shared Linux host, no CPU affinity; no comparison with old candidate; no Python equivalent durable store')
        dump(self.out / 'performance.json', perf)
        log('performance PASS: current candidate, four layers measured')

    def soak(self, seconds):
        assert 60 <= seconds <= 600
        d = self.directory('soak')
        seed = req('append', {'events': trace(full=True, final=True) + allocation_trace(release=True)}, 'soak-seed')
        reference = Reference()
        expected_seed = reference.execute(seed)
        assert expected_seed['verdict'] == 'APPLY'
        p = self.peer(d, True)
        assert same(p.call(seed), expected_seed)
        started = time.monotonic()
        deadline = started + seconds
        samples, counts, cycles, process_requests = [], 0, 1, 1
        baseline_hash = sha(d / 'events.ndjson')
        duplicate = reference.execute(seed)
        r = v.materialize(CONTRACT, CONTRACT['cases'][0]['record'])
        evaluate = req('evaluate', {'record': r}, 'soak-evaluate')
        evaluated = reference.execute(evaluate)
        next_sample = started
        latencies = []
        while time.monotonic() < deadline:
            if process_requests >= 3400:
                self.close(p, f'soak-cycle-{cycles}')
                p = self.peer(d)
                cycles += 1
                process_requests = 0
            if counts % 3 == 0:
                command, expected = seed, duplicate
            elif counts % 3 == 1:
                command, expected = evaluate, evaluated
            else:
                command, expected = req('inspect', {}, 'soak-inspect'), {'verdict': 'APPLY', 'snapshot': reference.snapshot()}
            assert same(p.call(command), expected)
            latencies.append(p.latencies[-1])
            process_requests += 1
            counts += 1
            now = time.monotonic()
            if now >= next_sample:
                samples.append(dict(elapsed_s=now - started, process_cycle=cycles,
                                    journal_bytes=(d / 'events.ndjson').stat().st_size, **self.observation(p)))
                next_sample = now + 1
            time.sleep(0.01)
        self.close(p, f'soak-cycle-{cycles}')
        assert sha(d / 'events.ndjson') == baseline_hash
        p = self.peer(d)
        assert same(p.call(req('inspect', {}, 'final-reopen'))['snapshot'], reference.snapshot())
        self.close(p, 'soak-final-recovery')
        dump(self.out / 'soak-raw.json', dict(samples=samples, latencies_ns=latencies))
        # Limits fixed in the runner before observation, not fitted to the result.
        assert max(s['rss_kib'] for s in samples) <= 64 * 1024
        assert max(s['fd_count'] for s in samples) <= 8
        assert max(s['rss_kib'] for s in samples) - min(s['rss_kib'] for s in samples) <= 8 * 1024
        dump(self.out / 'soak.json', dict(
            status='PASS', elapsed_s=time.monotonic() - started, completed_requests=counts,
            runtime_process_cycles=cycles, observation_samples=len(samples),
            rss_min_kib=min(s['rss_kib'] for s in samples), rss_max_kib=max(s['rss_kib'] for s in samples),
            hwm_max_kib=max(s['hwm_kib'] for s in samples),
            fd_min=min(s['fd_count'] for s in samples), fd_max=max(s['fd_count'] for s in samples),
            journal_bytes=(d / 'events.ndjson').stat().st_size, journal_sha256=baseline_hash,
            thresholds=dict(rss_max_kib=65536, rss_span_kib=8192, fd_max=8),
            logical_dispatches=1, external_dispatches=0, snapshot_and_ledger=self.audit(d, reference),
            latency=distribution(latencies),
            limits='bounded fixed 10-event ledger, duplicate/evaluate/inspect mix, 10ms pacing; rotates before 4096-request cap; not growing ledger, scheduler, capacity or production soak'))
        log(f'soak PASS: {counts} requests over {time.monotonic() - started:.1f}s')

    def finish(self):
        for name, expected in self.identity.items():
            assert sha(ROOT / name) == expected, f'source changed during qualification: {name}'
        dump(self.out / 'commands.json', self.commands)
        dump(self.out / 'resources.json', self.resources)
        with tarfile.open(self.out / 'journals.tar.gz', 'w:gz') as archive:
            for p in self.scratch.iterdir():
                archive.add(p, arcname=p.name)
        shutil.rmtree(self.scratch)
        if (self.out / 'target').exists():
            shutil.rmtree(self.out / 'target')
        dump(self.out / 'receipt.json', dict(
            status='BLOCKED', authority='NON_AUTHORITATIVE', deployment='NOT_DEPLOYED',
            performance='PASS', shadow='REUSED_SHADOW_CONFORMANCE_PASS', crash_recovery='PASS',
            receipt_boundary='PASS', real_v1_execution_replay='NOT_COVERED',
            physical_writer_fencing='BLOCKED', production_storage_os='NOT_COVERED', bounded_soak='PASS',
            binary_sha256=sha(self.binary), source_identity_sha256=sha(self.out / 'identity.json'),
            migration_blocker='Production authority adapter qualification is incomplete: physical worker/epoch fencing and production atomic store/replay evidence are absent.',
            P1='NOT_RUN', D001_D002_repairs='NOT_REPEATED', full_history_tests='NOT_RUN',
            controlled_cutover_recommended=False, cleanup='task-owned scratch/target removed; journals archived; no extra checkout'))
        sums = {str(p.relative_to(self.out)): sha(p) for p in sorted(self.out.rglob('*')) if p.is_file()}
        dump(self.out / 'sha256-manifest.json', sums)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prior', type=Path, required=True)
    parser.add_argument('--native-receipt', type=Path, required=True)
    parser.add_argument('--recovery-samples', type=int, default=1000)
    parser.add_argument('--soak-seconds', type=int, default=300)
    args = parser.parse_args()
    q = Qualification(args.output.resolve(), args.prior.resolve())
    q.traces(args.native_receipt.resolve())
    q.crash_matrix()
    q.fencing()
    q.performance(args.recovery_samples)
    q.soak(args.soak_seconds)
    q.finish()
    log('qualification complete: BLOCKED (see receipt.json)')


if __name__ == '__main__':
    main()
