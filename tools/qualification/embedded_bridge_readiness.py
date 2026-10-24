"""Passive observation of the pinned Tunnel Bridge; never infer recycle safety.

The installed Bridge has no public pending/uncertain-request or admission-fence
state. Even a complete quiet event history cannot authorize group termination.
Do not use /api/codex/status: its GET handler calls Warmup / EnsureStarted.
"""
import hashlib
import json
import os
import re
import time
import urllib.request
from pathlib import Path

PINNED_VERSION = '0.0.14+0f870e50a973fa820d4c409000059e181e8d242b'
PINNED_BINARY_SHA256 = '472eb9dd9dd625b4e6023c3b4a5736b3a2e5a1b6dbe9338e001887a64ec992a6'
PASSIVE_PATHS = ('/api/status', '/api/codex/events?limit=1000')


class ProbeRefused(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProbeRefused('REDIRECT_REFUSED')


def read_json(base, path):
    if not re.fullmatch(r'http://127\.0\.0\.1:[1-9][0-9]{0,4}', base) or path not in PASSIVE_PATHS:
        raise ProbeRefused('PASSIVE_LOOPBACK_ENDPOINT_REQUIRED')
    # Ignore ambient HTTP proxies and never follow a redirect off the owned port.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(base + path, timeout=3) as response:
        body = response.read(1048577)
    if len(body) > 1048576:
        raise ProbeRefused('RESPONSE_LIMIT')
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ProbeRefused('RESPONSE_SHAPE')
    return data


def process(pid):
    if type(pid) is not int or pid <= 1:
        raise ProbeRefused('EXACT_PID_REQUIRED')
    p = Path('/proc') / str(pid)
    fields = (p / 'stat').read_text().rsplit(')', 1)[1].split()
    pipes = {}
    for fd in (p / 'fd').iterdir():
        try:
            target = os.readlink(fd)
        except FileNotFoundError:
            continue
        if target.startswith(('pipe:', 'socket:')):
            pipes[fd.name] = target
    return dict(pid=pid, ppid=int(fields[1]), pgrp=int(fields[2]),
                session=int(fields[3]), start_ticks=fields[19],
                cgroup=(p / 'cgroup').read_text(), descriptors=pipes)


def owns_listener(base, tunnel):
    port = int(base.rsplit(':', 1)[1])
    # Same network namespace; require the listening socket to be held by Tunnel.
    owned = set(tunnel['descriptors'].values())
    for line in Path('/proc/net/tcp').read_text().splitlines()[1:]:
        fields = line.split()
        if (fields[1] == f'0100007F:{port:04X}' and fields[3] == '0A'
                and 'socket:[' + fields[9] + ']' in owned):
            return True
    return False


def history(events):
    if not isinstance(events, list) or not events or len(events) > 512:
        return False
    # A ring beginning later than seq=1 cannot prove absence of earlier work.
    if [e.get('seq') for e in events if isinstance(e, dict)] != list(range(1, len(events) + 1)):
        return False
    for event in events:
        if event.get('source') not in {'notification', 'lifecycle'}:
            return False
        method = event.get('method')
        if method == 'remoteControl/status/changed':
            payload = event.get('payload')
            params = payload.get('params') if isinstance(payload, dict) else None
            if not isinstance(params, dict) or params.get('status') != 'disabled':
                return False
        elif method not in {'account/updated', 'process/ready'}:
            return False
    return (sum(e.get('method') == 'process/ready' for e in events) == 1
            and any(e.get('method') == 'remoteControl/status/changed' for e in events))


def assess(samples, *, identity_stable, binary_sha256):
    known = (binary_sha256 == PINNED_BINARY_SHA256 and len(samples) >= 2
             and all(s['status'].get('version') == PINNED_VERSION for s in samples)
             and len({s['status'].get('client_instance_id') for s in samples}) == 1
             and bool(samples[0]['status'].get('client_instance_id')))
    quiet = (known and identity_stable and all(history(s['events']) for s in samples)
             and all(s['events'] == samples[0]['events'] for s in samples))
    return dict(
        activity_observation='NO_WORK_OBSERVED' if quiet else 'UNKNOWN_OR_ACTIVITY_OBSERVED',
        pinned_contract_verified=known, process_identity_stable=identity_stable,
        complete_quiet_event_history=bool(quiet),
        safe_idle_gate='BLOCKED', safe_to_recycle=False,
        blockers=['PENDING_RPC_NOT_EXPOSED', 'UNCERTAIN_ACK_NOT_EXPOSED',
                  'MUTATION_ADMISSION_NOT_FENCED'],
        status_get_excluded='GET /api/codex/status invokes Warmup / EnsureStarted',
        safe_idle_requires='Atomic passive snapshot of pending operations, unresolved ACKs, '
                           'all loaded/active turns and a generation-bound mutation admission fence',
        production_mutations=0, provider_rpc_calls=0, second_consumers_started=0,
    )


def collect(base, *, tunnel_pid, bridge_pid, observe_seconds=10):
    if not 1 <= observe_seconds <= 30:
        raise ProbeRefused('OBSERVATION_MUST_BE_BOUNDED_1_TO_30_SECONDS')
    if not re.fullmatch(r'http://127\.0\.0\.1:[1-9][0-9]{0,4}', base):
        raise ProbeRefused('PASSIVE_LOOPBACK_ENDPOINT_REQUIRED')
    before = [process(pid) for pid in (tunnel_pid, bridge_pid)]
    tunnel, bridge = before
    if (bridge['ppid'] != tunnel_pid or bridge['cgroup'] != tunnel['cgroup']
            or bridge['pgrp'] != tunnel['pgrp'] or not owns_listener(base, tunnel)
            or any(bridge['descriptors'].get(fd) not in tunnel['descriptors'].values()
                   for fd in ('0', '1', '2'))):
        raise ProbeRefused('TUNNEL_BRIDGE_ENDPOINT_IDENTITY_CONFLICT')
    binary = hashlib.sha256(Path(f'/proc/{tunnel_pid}/exe').read_bytes()).hexdigest()
    if binary != PINNED_BINARY_SHA256:
        raise ProbeRefused('UNQUALIFIED_TUNNEL_BINARY')
    samples = []
    for i in range(2):
        status = read_json(base, PASSIVE_PATHS[0])
        events = read_json(base, PASSIVE_PATHS[1]).get('events')
        samples.append(dict(observed_at=time.time(), status=status, events=events))
        if i == 0:
            time.sleep(observe_seconds)
    after = [process(pid) for pid in (tunnel_pid, bridge_pid)]
    # Descriptor churn in Tunnel HTTP activity is expected; process identity isn't.
    stable = all(all(a[k] == b[k] for k in ('pid', 'ppid', 'pgrp', 'session', 'start_ticks', 'cgroup'))
                 for a, b in zip(before, after))
    report = assess(samples, identity_stable=stable, binary_sha256=binary)
    report.update(samples=samples, processes_before=before, processes_after=after,
                  binary_sha256=binary, observation_seconds=observe_seconds,
                  collected_paths=list(PASSIVE_PATHS))
    return report
