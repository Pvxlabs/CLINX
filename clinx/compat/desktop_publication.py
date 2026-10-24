"""Bounded metadata delivery in the existing event outbox, never an executor.

Only authorized thread creation enrolls an intent. Reads never enroll/retry it.
Delivery is at-least-once: an ack lost after name/set may repeat the same title.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

from clinx.compat.completion_runtime import finalization_lock
from clinx.compat.domain import EventActor
from clinx.compat.shadow_ledger import AppendRequest, EventStore, OutboxRequest

DESTINATION = 'codex_desktop_metadata_v1'
MAX_ATTEMPTS = 8
_LOG = logging.getLogger(__name__)


def _stamp(value):
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()


def _due(row):
    if row['state'] == 'PENDING':
        return 0
    return dt.datetime.fromisoformat(row['updated_at']).timestamp() + min(300, 5 * 2 ** min(row['attempts'] - 1, 6))


def configured_endpoint(value):
    # AppServer may expose a stable symlink to a per-process socket. Preserve
    # the configured locator across daemon restarts, not its resolved inode path.
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / path
    return str(path.absolute())


def publication_key(host, thread):
    return hashlib.sha256(json.dumps([host, thread], separators=(',', ':')).encode()).hexdigest()


def inspect_publication(path, *, task_id=None, key=None):
    """Read-only status, including exhausted or superseded delivery; no migration."""
    if not Path(path).exists():
        return None
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as c:
        c.row_factory = sqlite3.Row
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_event_outbox'").fetchone():
            return None
        if key is None and task_id:
            binding = c.execute('SELECT t.host,b.thread_id FROM conversation_bindings b JOIN tasks t USING(task_id) WHERE b.task_id=?', (task_id,)).fetchone()
            if binding:
                key = publication_key(binding['host'], binding['thread_id'])
        if key is None:
            return None
        row = c.execute('SELECT * FROM v2_event_outbox WHERE destination=? AND idempotency_key=?', (DESTINATION, key)).fetchone()
    if not row:
        return None
    return {k: row[k] for k in ('state', 'attempts', 'last_error', 'updated_at')} | {
        'retry_at': _stamp(_due(row)) if row['state'] in ('PENDING', 'RETRY') else None,
        'delivery_only': True,
    }


class DesktopPublicationOutbox:
    def __init__(self, registry, cfg, *, clock=time.time):
        self.registry, self.cfg, self.clock = registry, cfg, clock
        self.store = EventStore(registry.path)

    def enqueue(self, target, title):
        self.registry.require_local_writer()
        from clinx.contracts.runtime_config import canonical_host
        native = self.cfg.app_server.native_socket
        if (not native or not target.target_host or not self.cfg.runtime_host
                or canonical_host(target.target_host) != canonical_host(self.cfg.runtime_host)
                or target.transport not in ('', 'local', 'local_stdio')):
            return None
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ValueError('bounded nonempty publication title required')
        host = canonical_host(target.target_host)
        endpoint = configured_endpoint(native)
        binding = self.registry.get_binding_by_thread(target.thread_id, host=host)
        payload = {'host': host, 'thread_id': target.thread_id, 'cwd': target.cwd,
                   'task_id': binding.task_id if binding else None, 'title': title,
                   'originator': self.cfg.app_server.client_name, 'endpoint': endpoint}
        key = publication_key(host, target.thread_id)
        self.store.initialize()
        with self.registry._connect() as c:
            c.execute("CREATE INDEX IF NOT EXISTS v2_outbox_destination_delivery ON v2_event_outbox(destination,state,updated_at,cursor)")
        self.store.append(AppendRequest(
            aggregate_type='desktop_publication', aggregate_id=key, expected_version=0,
            event_type='V1DesktopPublicationRequestedObserved', actor=EventActor('service', payload['originator']),
            idempotency_scope=DESTINATION, idempotency_key=key, payload=payload,
            outbox=(OutboxRequest(DESTINATION, key, payload),),
        ))
        return key

    def _deliver(self, payload):
        from clinx.contracts.runtime_config import canonical_host
        from clinx.provider.native_provider import existing_client
        cfg = self.cfg
        if not cfg.app_server.native_socket:
            return 'CONFIGURATION_CHANGED'
        endpoint = configured_endpoint(cfg.app_server.native_socket)
        if (payload['host'] != canonical_host(cfg.runtime_host) or payload['endpoint'] != endpoint
                or payload['originator'] != cfg.app_server.client_name):
            return 'CONFIGURATION_CHANGED'
        if payload['task_id']:
            binding = self.registry.get_binding(payload['task_id'])
            task = self.registry.get_task(payload['task_id'])
            if (not binding or binding.thread_id != payload['thread_id'] or task.cwd != payload['cwd']
                    or task.host != payload['host'] or task.title != payload['title']):
                return 'TASK_UPDATED'
        with existing_client(endpoint, timeout=min(cfg.app_server.request_timeout_seconds, 3), read_only=False) as client:
            client.initialize(client_name=cfg.app_server.client_name, client_title=cfg.app_server.client_title,
                              client_version=cfg.app_server.client_version)
            thread = client.thread_read(payload['thread_id'])
            if (thread.get('id') != payload['thread_id'] or thread.get('cwd') != payload['cwd']
                    or thread.get('originator') != payload['originator'] or thread.get('threadSource') != 'user'):
                return 'IDENTITY_MISMATCH'
            if thread.get('archived') or thread.get('name') not in (None, payload['title']):
                return 'THREAD_UPDATED'
            client.thread_name_set(payload['thread_id'], payload['title'])
        return None

    def process(self, key):
        self.registry.require_local_writer()
        # Existing cross-process flock is a delivery lock, not a Codex writer lock.
        with finalization_lock(self.registry, 'desktop-publication:' + key):
            with self.registry._connect() as c:
                row = c.execute('SELECT * FROM v2_event_outbox WHERE destination=? AND idempotency_key=?', (DESTINATION, key)).fetchone()
                if not row or row['state'] not in ('PENDING', 'RETRY') or _due(row) > self.clock():
                    return inspect_publication(self.registry.path, key=key)
                if row['attempts'] >= MAX_ATTEMPTS:
                    c.execute("UPDATE v2_event_outbox SET state='FAILED',last_error='RETRY_EXHAUSTED',updated_at=? WHERE outbox_id=?", (_stamp(self.clock()), row['outbox_id']))
                    c.commit()
                    return inspect_publication(self.registry.path, key=key)
                # Durable before RPC: a crash cannot silently reset the retry budget.
                c.execute("UPDATE v2_event_outbox SET state='RETRY',attempts=attempts+1,last_error='DELIVERY_UNCERTAIN',updated_at=? WHERE outbox_id=?", (_stamp(self.clock()), row['outbox_id']))
            try:
                error = self._deliver(json.loads(row['payload_json']))
                state = 'FAILED' if error else 'APPLIED'
            except Exception as exc:  # noqa: BLE001 - persist unexpected delivery failures without replaying execution
                state = 'FAILED' if row['attempts'] + 1 >= MAX_ATTEMPTS else 'RETRY'
                error = ('RETRY_EXHAUSTED:' if state == 'FAILED' else 'UNAVAILABLE:') + type(exc).__name__
            with self.registry._connect() as c:
                c.execute('UPDATE v2_event_outbox SET state=?,last_error=?,updated_at=? WHERE outbox_id=?', (state, error, _stamp(self.clock()), row['outbox_id']))
            if error:
                _LOG.warning('Desktop publication %s: %s', key, error)
            return inspect_publication(self.registry.path, key=key)

    def run_once(self, *, limit=8):
        self.registry.require_local_writer()
        if not 1 <= limit <= 16:
            raise ValueError('publication batch must be 1..16')
        with self.registry._connect() as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_event_outbox'").fetchone():
                return 0
            rows = c.execute("""SELECT idempotency_key FROM v2_event_outbox
                WHERE destination=? AND state IN ('PENDING','RETRY')
                AND (state='PENDING' OR julianday(updated_at) + MIN(300,5*(1 << MIN(MAX(attempts-1,0),6)))/86400.0 <= julianday(?))
                ORDER BY updated_at,cursor LIMIT ?""", (DESTINATION, _stamp(self.clock()), limit)).fetchall()
        for row in rows:
            self.process(row['idempotency_key'])
        return len(rows)
