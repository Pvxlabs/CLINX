"""Durable correlation of Host execution and provider delivery, never a runner.

Only operation names/targets and payload hashes are stored here. Host evidence remains authoritative
for execution; provider item/completed is the separate delivery acknowledgement.
"""
from __future__ import annotations

import hashlib
import json
import time


class DeliveryReconciliationRequired(RuntimeError):
    code = "RESULT_DELIVERY_RECONCILIATION_REQUIRED"
    execution_state = "RECONCILIATION_REQUIRED"


class ToolCallReplayRejected(DeliveryReconciliationRequired):
    code = "TOOL_CALL_ALREADY_RECORDED"


def requires_reconciliation(row):
    """Only proven non-dispatch or known completion AND delivery clears uncertainty."""
    if row['execution_state'] == 'COMMAND_NOT_DISPATCHED':
        return False
    # A maintenance reconciliation may clear the uncertainty while preserving
    # the original FAILED delivery and Provider rejection.  It never rewrites
    # the delivery to DELIVERED.
    reconciliation_state = (row['reconciliation_state'] if 'reconciliation_state' in row.keys()
                            else None)
    if reconciliation_state == 'RESOLVED':
        return False
    return not (row['execution_state'] in {'COMMAND_EXECUTION_FAILED', 'COMMAND_EXECUTED_RESULT_DELIVERED'}
                and row['delivery_state'] == 'DELIVERED' and row['host_exit_code'] is not None)


def ack_in_flight(row):
    return (row['delivery_state'] == 'PENDING' and row['host_exit_code'] is not None
            and row['response_sent_at'] is not None and row['failure_code'] is None)


def delivery_summary(records, raw_result=''):
    states = [r['delivery_state'] for r in records]
    state = ('NO_HOST_CALLS' if not states else 'DELIVERED' if all(s == 'DELIVERED' for s in states)
             else 'FAILED' if 'FAILED' in states else 'PENDING')
    mentions_pending = any(token in (raw_result or '') for token in
                           ('AWAITING_PROVIDER_ACK', 'COMMAND_EXECUTED_RESULT_DELIVERY_PENDING'))
    return {'source': 'PERSISTED_DELIVERY_LEDGER', 'state': state, 'count': len(states),
            'call_states': [r.get('call_state', 'LEGACY') for r in records],
            'worker_reported_pending': mentions_pending,
            'worker_pending_is_stale_snapshot': mentions_pending and state == 'DELIVERED',
            'business_result_independent': True,
            'reconciliation_required': any(r['reconciliation_required'] for r in records)}


def certainty(row):
    state = row['execution_state']
    unresolved = requires_reconciliation(row)
    pending = (row['delivery_state'] == 'PENDING' and row['host_exit_code'] is not None
               and row['failure_code'] is None)
    side = ('NOT_EXECUTED' if state == 'COMMAND_NOT_DISPATCHED' else
            'UNKNOWN' if row['host_exit_code'] is None else 'EXECUTED')
    return {'side_effect_certainty': side, 'reconciliation_required': unresolved and not pending,
            'execution_can_continue': not unresolved, 'retry_required': False,
            'result_certainty': 'KNOWN' if row['host_exit_code'] is not None else 'UNKNOWN',
            'host_dispatched': False if state == 'COMMAND_NOT_DISPATCHED' else True if row['host_exit_code'] is not None else None,
            'continuation_state': 'WAITING_INTERNAL_ACK' if pending else 'RECONCILIATION_REQUIRED' if unresolved else 'SAFE_TO_CONTINUE',
            'retry_allowed': False,
            'failure_stage': 'PRE_DISPATCH_VALIDATION' if state == 'COMMAND_NOT_DISPATCHED' else 'RESULT_DELIVERY' if unresolved and not pending else 'HOST_EXECUTION' if row['host_exit_code'] else None}


class ToolDeliveryLedger:
    def __init__(self, registry, execution_ref: str, *, context=None):
        self.registry = registry
        self.execution_ref = execution_ref
        self.context = context or {}
        with registry._connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS host_tool_deliveries (
                execution_ref TEXT NOT NULL, tool_call_id TEXT NOT NULL,
                request_id TEXT NOT NULL, response_id TEXT,
                identity_json TEXT NOT NULL, request_hash TEXT NOT NULL,
                host_execution_ref TEXT, host_exit_code INTEGER,
                execution_state TEXT NOT NULL, delivery_state TEXT NOT NULL,
                failure_code TEXT, admitted_at REAL NOT NULL,
                host_completed_at REAL, response_sent_at REAL, acknowledged_at REAL,
                provider_item_json TEXT, provider_failure_sha256 TEXT,
                provider_observed_at REAL,
                reconciliation_state TEXT NOT NULL DEFAULT 'OPEN',
                reconciled_at REAL, reconciliation_json TEXT,
                PRIMARY KEY(execution_ref,tool_call_id))''')
            columns = {row[1] for row in conn.execute('PRAGMA table_info(host_tool_deliveries)')}
            migrations = {
                'call_state': "ALTER TABLE host_tool_deliveries ADD COLUMN call_state TEXT NOT NULL DEFAULT 'LEGACY'",
                'operation_json': "ALTER TABLE host_tool_deliveries ADD COLUMN operation_json TEXT",
                'validated_at': 'ALTER TABLE host_tool_deliveries ADD COLUMN validated_at REAL',
                'dispatched_at': 'ALTER TABLE host_tool_deliveries ADD COLUMN dispatched_at REAL',
                'provider_item_json': 'ALTER TABLE host_tool_deliveries ADD COLUMN provider_item_json TEXT',
                'provider_failure_sha256': 'ALTER TABLE host_tool_deliveries ADD COLUMN provider_failure_sha256 TEXT',
                'provider_observed_at': 'ALTER TABLE host_tool_deliveries ADD COLUMN provider_observed_at REAL',
                'reconciliation_state': "ALTER TABLE host_tool_deliveries ADD COLUMN reconciliation_state TEXT NOT NULL DEFAULT 'OPEN'",
                'reconciled_at': 'ALTER TABLE host_tool_deliveries ADD COLUMN reconciled_at REAL',
                'reconciliation_json': 'ALTER TABLE host_tool_deliveries ADD COLUMN reconciliation_json TEXT',
            }
            for name, statement in migrations.items():
                if name not in columns:
                    conn.execute(statement)

    @staticmethod
    def records(registry, execution_ref: str):
        with registry._connect() as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='host_tool_deliveries'").fetchone():
                return []
            return [{**dict(row), **certainty(row)} for row in conn.execute(
                'SELECT * FROM host_tool_deliveries WHERE execution_ref=? ORDER BY admitted_at',
                (execution_ref,))]

    @staticmethod
    def _digest(params):
        semantic = {k: params.get(k) for k in ('threadId', 'turnId', 'callId', 'namespace', 'tool', 'arguments')}
        return hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    @staticmethod
    def ingress_id(request):
        call_id = request['params'].get('callId')
        if isinstance(call_id, str) and call_id.strip():
            return call_id
        # A malformed owned request still needs a durable rejection identity.
        # This local identity is never passed to the Host executor as a valid call.
        payload = json.dumps(request, sort_keys=True, separators=(',', ':')).encode()
        return 'invalid-' + hashlib.sha256(payload).hexdigest()

    def reject_received(self, request, failure_code):
        call_id = self.ingress_id(request)
        with self.registry._connect() as conn:
            row = conn.execute('SELECT call_state FROM host_tool_deliveries WHERE execution_ref=? AND tool_call_id=?',
                               (self.execution_ref, call_id)).fetchone()
        if row is None or row['call_state'] != 'RECEIVED':
            return None  # Never rewrite a duplicate's completed/uncertain Host evidence.
        self.failed(call_id, not_dispatched=True, failure_code=failure_code)
        return call_id

    @staticmethod
    def validated(registry, execution_ref, call_id):
        with registry._connect() as conn:
            columns = {r[1] for r in conn.execute('PRAGMA table_info(host_tool_deliveries)')}
            if 'call_state' in columns:
                conn.execute("UPDATE host_tool_deliveries SET call_state='VALIDATED',validated_at=? WHERE execution_ref=? AND tool_call_id=? AND call_state='DISPATCHED'",
                             (time.time(), execution_ref, call_id))

    @staticmethod
    def running(registry, execution_ref, call_id):
        with registry._connect() as conn:
            if 'call_state' in {r[1] for r in conn.execute('PRAGMA table_info(host_tool_deliveries)')}:
                conn.execute("UPDATE host_tool_deliveries SET call_state='RUNNING' WHERE execution_ref=? AND tool_call_id=?",
                             (execution_ref, call_id))

    def receive(self, request, identity):
        """Commit ingress before schema, policy, queueing, or Host side effects."""
        params = request['params']
        call_id = self.ingress_id(request)
        digest = self._digest(params)
        values = params.get('arguments')
        operation = ({k: values.get(k) for k in ('operation_class', 'capability', 'operation')}
                     if isinstance(values, dict) else {})
        arguments = values.get('arguments') if isinstance(values, dict) else None
        operation['target'] = arguments.get('target') if isinstance(arguments, dict) else None
        with self.registry._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM host_tool_deliveries WHERE execution_ref=? AND tool_call_id=?',
                               (self.execution_ref, call_id)).fetchone()
            if row is not None:
                if row['call_state'] == 'RECEIVED' and row['request_hash'] != digest:
                    raise DeliveryReconciliationRequired('tool call identity reused with changed semantics')
                return call_id
            conn.execute('''INSERT INTO host_tool_deliveries
                (execution_ref,tool_call_id,request_id,identity_json,request_hash,
                 execution_state,delivery_state,admitted_at,call_state,operation_json)
                 VALUES (?,?,?,?,?,?,'PENDING',?,'RECEIVED',?)''',
                (self.execution_ref, call_id, json.dumps(request.get('id')),
                 json.dumps({**self.context, **identity}, sort_keys=True), digest,
                 'REQUEST_RECEIVED', time.time(), json.dumps(operation, sort_keys=True)))
        return call_id

    def admit(self, request, identity):
        params = request['params']
        call_id = params.get('callId')
        if not isinstance(call_id, str) or not call_id.strip():
            raise DeliveryReconciliationRequired('dynamic tool callId is required before dispatch')
        digest = self._digest(params)
        with self.registry._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = conn.execute('SELECT * FROM host_tool_deliveries WHERE execution_ref=?',
                                (self.execution_ref,)).fetchall()
            if any(row['reconciliation_state'] == 'RESOLVED' for row in rows):
                raise DeliveryReconciliationRequired(
                    'execution delivery was reconciled; a new execution is required'
                )
            for row in rows:
                if row['tool_call_id'] == call_id and row['call_state'] != 'RECEIVED':
                    error = ToolCallReplayRejected('tool call already recorded; replay never executes Host again')
                    error.execution_state = row['execution_state']
                    error.reconciliation_required = requires_reconciliation(row)
                    raise error
            if any(requires_reconciliation(row) for row in rows if row['call_state'] != 'RECEIVED'):
                raise DeliveryReconciliationRequired('existing invocation requires reconciliation; command retry prohibited')
            existing = next((r for r in rows if r['tool_call_id'] == call_id), None)
            if existing is not None:
                if existing['request_hash'] != self._digest(params):
                    raise DeliveryReconciliationRequired('tool call identity reused with changed semantics')
                conn.execute('''UPDATE host_tool_deliveries SET request_id=?,identity_json=?,
                    execution_state='COMMAND_DISPATCHED',call_state='VALIDATING'
                    WHERE execution_ref=? AND tool_call_id=? AND call_state='RECEIVED' ''',
                    (json.dumps(request['id']), json.dumps({**self.context, **identity}, sort_keys=True),
                     self.execution_ref, call_id))
                return call_id
            conn.execute('''INSERT INTO host_tool_deliveries
                (execution_ref,tool_call_id,request_id,identity_json,request_hash,
                 execution_state,delivery_state,admitted_at,call_state) VALUES (?,?,?,?,?,?,?,?,'VALIDATING')''',
                (self.execution_ref, call_id, json.dumps(request['id']), json.dumps({**self.context, **identity}, sort_keys=True),
                 digest, 'COMMAND_DISPATCHED', 'PENDING', time.time()))
        return call_id

    def host_result(self, call_id, result):
        host_ref = result.get('host_execution_ref')
        if not host_ref or result.get('execution_ref') != self.execution_ref:
            raise DeliveryReconciliationRequired('Host result correlation is missing or changed')
        state = ('COMMAND_DISPATCHED' if result.get('exit_code') is None else
                 'COMMAND_EXECUTED_RESULT_DELIVERY_PENDING' if result.get('exit_code') == 0
                 else 'COMMAND_EXECUTION_FAILED')
        with self.registry._connect() as conn:
            host = conn.execute('SELECT execution_ref,tool_call_id,exit_code FROM host_executions '
                                'WHERE host_execution_ref=?', (host_ref,)).fetchone()
            if host is None or (host['execution_ref'], host['tool_call_id']) != (self.execution_ref, call_id):
                raise DeliveryReconciliationRequired('Host evidence does not own this tool call')
            if host['exit_code'] != result.get('exit_code'):
                raise DeliveryReconciliationRequired('Host result does not match durable completion evidence')
            conn.execute('''UPDATE host_tool_deliveries SET host_execution_ref=?,host_exit_code=?,
                execution_state=CASE WHEN delivery_state='FAILED' AND ?='COMMAND_EXECUTED_RESULT_DELIVERY_PENDING' THEN 'COMMAND_EXECUTED_RESULT_DELIVERY_FAILED' ELSE ? END,host_completed_at=?,failure_code=CASE WHEN delivery_state='FAILED' THEN failure_code ELSE NULL END,
                call_state=CASE WHEN ? IS NULL THEN 'UNKNOWN' WHEN ?=0 THEN 'SUCCEEDED' ELSE 'FAILED' END
                WHERE execution_ref=? AND tool_call_id=?''',
                (host_ref, result.get('exit_code'), state, state, time.time(), result.get('exit_code'), result.get('exit_code'), self.execution_ref, call_id))

    def sent(self, call_id, request_id):
        with self.registry._connect() as conn:
            conn.execute('''UPDATE host_tool_deliveries SET response_id=?,response_sent_at=?
                WHERE execution_ref=? AND tool_call_id=?''',
                (json.dumps(request_id), time.time(), self.execution_ref, call_id))

    def failed(self, call_id, *, not_dispatched=False, failure_code=None):
        with self.registry._connect() as conn:
            host = conn.execute('SELECT host_execution_ref,exit_code FROM host_executions '
                                'WHERE execution_ref=? AND tool_call_id=?',
                                (self.execution_ref, call_id)).fetchone()
            row = conn.execute('SELECT * FROM host_tool_deliveries WHERE execution_ref=? AND tool_call_id=?',
                               (self.execution_ref, call_id)).fetchone()
            proven_not_dispatched = host is None and (not_dispatched or
                (row is not None and (row['execution_state'] == 'COMMAND_NOT_DISPATCHED' or row['call_state'] == 'RECEIVED')))
            state = ('COMMAND_NOT_DISPATCHED' if proven_not_dispatched else
                     'COMMAND_DISPATCHED' if host is None or host['exit_code'] is None else
                     'COMMAND_EXECUTED_RESULT_DELIVERY_FAILED' if host['exit_code'] == 0 else
                     'COMMAND_EXECUTION_FAILED')
            code = ((failure_code or (row['failure_code'] if row else None) or 'COMMAND_NOT_DISPATCHED')
                    if proven_not_dispatched else
                    'RESULT_DELIVERY_FAILED_AFTER_EXECUTION' if host is not None and host['exit_code'] is not None else
                    'RESULT_DELIVERY_UNCONFIRMED_AFTER_DISPATCH')
            conn.execute('''UPDATE host_tool_deliveries SET delivery_state='FAILED',
                execution_state=?,failure_code=?,host_execution_ref=?,host_exit_code=?,call_state=?
                WHERE execution_ref=? AND tool_call_id=? AND delivery_state != 'DELIVERED' ''',
                (state, code, host['host_execution_ref'] if host else None,
                 host['exit_code'] if host else None,
                 'REJECTED' if proven_not_dispatched else 'UNKNOWN' if host is None or host['exit_code'] is None else 'SUCCEEDED' if host['exit_code'] == 0 else 'FAILED',
                 self.execution_ref, call_id))
            return state

    def disconnected(self):
        for row in self.records(self.registry, self.execution_ref):
            if row['delivery_state'] == 'PENDING':
                self.failed(row['tool_call_id'])

    def observe(self, message, *, owner=None):
        if message.get('method') != 'item/completed':
            return
        params = message.get('params', {})
        item = params.get('item', {})
        if item.get('type') not in {'dynamicToolCall', 'DynamicToolCall'}:
            return
        call_id = item.get('id')
        with self.registry._connect() as conn:
            row = conn.execute('SELECT * FROM host_tool_deliveries WHERE execution_ref=? AND tool_call_id=?',
                               (self.execution_ref, call_id)).fetchone()
            if row is None or row['delivery_state'] == 'DELIVERED':
                return
            identity = json.loads(row['identity_json'])
            if (params.get('threadId'), params.get('turnId')) != (identity['thread_id'], identity['turn_id']):
                return
            item_identity = (item.get('namespace'), item.get('tool'))
            if item_identity != (identity['namespace'], identity['tool']) and not (
                    item.get('success') is False and item_identity == (None, None)):
                return
            if owner is not None and any(identity.get(k) != v for k, v in owner.items()):
                return
            delivered = False
            rejection_delivered = False
            for part in item.get('contentItems', item.get('content_items', [])) or []:
                if not isinstance(part, dict):
                    continue
                try:
                    body = json.loads(part.get('text', ''))
                except (ValueError, TypeError):
                    continue
                if isinstance(body, dict) and row['execution_state'] == 'COMMAND_NOT_DISPATCHED':
                    rejection_delivered |= (body.get('execution_state') == 'COMMAND_NOT_DISPATCHED'
                                            and body.get('result_state') == row['failure_code'])
                if isinstance(body, dict) and row['host_execution_ref']:
                    delivered |= (body.get('host_execution_ref') == row['host_execution_ref']
                                  and body.get('execution_ref') == self.execution_ref)
            delivered = ((delivered and item.get('success') is True) or
                         (rejection_delivered and item.get('success') is False)) and row['response_sent_at'] is not None
            provider_json = json.dumps(item, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
            provider_hash = hashlib.sha256(provider_json.encode('utf-8')).hexdigest()
            state = row['execution_state']
            if delivered and row['host_exit_code'] == 0:
                state = 'COMMAND_EXECUTED_RESULT_DELIVERED'
            elif not delivered and row['host_exit_code'] == 0:
                state = 'COMMAND_EXECUTED_RESULT_DELIVERY_FAILED'
            failure_code = (row['failure_code'] if row['execution_state'] == 'COMMAND_NOT_DISPATCHED' else
                None if delivered else ('RESULT_DELIVERY_FAILED_AFTER_EXECUTION'
                    if row['host_exit_code'] is not None else 'RESULT_DELIVERY_UNCONFIRMED_AFTER_DISPATCH'))
            conn.execute('''UPDATE host_tool_deliveries SET delivery_state=?,execution_state=?,
                failure_code=?,acknowledged_at=?,provider_item_json=?,provider_failure_sha256=?,provider_observed_at=?
                WHERE execution_ref=? AND tool_call_id=?''',
                 ('DELIVERED' if delivered else 'FAILED', state, failure_code, time.time(),
                  provider_json if not delivered else row['provider_item_json'],
                  provider_hash if not delivered else row['provider_failure_sha256'],
                  time.time() if not delivered else row['provider_observed_at'],
                  self.execution_ref, call_id))

    def reconcile_failed(self, call_id: str, *, proof: dict) -> dict:
        """Resolve one known Host-success/provider-delivery failure without replay.

        The proof is an identity/summary assertion.  Host and Provider evidence
        is read from the durable ledger and every field is compared.  Delivery
        remains FAILED so the original Provider rejection is never rewritten.
        """
        if not isinstance(proof, dict):
            raise DeliveryReconciliationRequired('delivery reconciliation proof must be an object')
        required = {'task_id', 'thread_id', 'turn_id', 'namespace', 'tool',
                    'host_execution_ref', 'operation_class', 'capability',
                    'operation', 'argv', 'mutating', 'provider_failure_sha256'}
        if not required.issubset(proof):
            raise DeliveryReconciliationRequired('delivery reconciliation requires exact identity and operation summary')
        with self.registry._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM host_tool_deliveries WHERE execution_ref=? AND tool_call_id=?',
                               (self.execution_ref, call_id)).fetchone()
            if row is None:
                raise DeliveryReconciliationRequired('unknown Host delivery call')
            if row['reconciliation_state'] == 'RESOLVED':
                return {'state': 'ALREADY_APPLIED', 'execution_ref': self.execution_ref,
                        'tool_call_id': call_id, 'delivery_state': row['delivery_state'],
                        'reconciliation_state': 'RESOLVED'}
            if row['delivery_state'] != 'FAILED' or row['execution_state'] != 'COMMAND_EXECUTED_RESULT_DELIVERY_FAILED':
                raise DeliveryReconciliationRequired('delivery is not a known executed Provider failure')
            if not row['provider_item_json'] or not row['provider_failure_sha256']:
                raise DeliveryReconciliationRequired('Provider failure item evidence is missing')
            if proof['provider_failure_sha256'] != row['provider_failure_sha256']:
                raise DeliveryReconciliationRequired('Provider failure evidence hash mismatch')
            identity = json.loads(row['identity_json'])
            expected_identity = {
                'task_id': identity.get('task_ref'), 'thread_id': identity.get('thread_id'),
                'turn_id': identity.get('turn_id'), 'namespace': identity.get('namespace'),
                'tool': identity.get('tool'),
            }
            for key, value in expected_identity.items():
                if value is not None and proof[key] != value:
                    raise DeliveryReconciliationRequired('delivery identity mismatch: ' + key)
            host = conn.execute('SELECT * FROM host_executions WHERE host_execution_ref=?',
                                (proof['host_execution_ref'],)).fetchone()
            if host is None or host['execution_ref'] != self.execution_ref or host['tool_call_id'] != call_id:
                raise DeliveryReconciliationRequired('Host receipt does not own this delivery')
            if proof['task_id'] != host['task_id']:
                raise DeliveryReconciliationRequired('delivery task identity does not match Host receipt')
            if (host['completed_at'] is None or host['exit_code'] != 0
                    or host['timed_out'] or host['result_state'] != 'SUCCEEDED'):
                raise DeliveryReconciliationRequired('Host receipt is not a definitive successful completion')
            if host['mutating'] is None:
                raise DeliveryReconciliationRequired('Host mutating property is unknown')
            if bool(host['mutating']):
                raise DeliveryReconciliationRequired('mutating Host delivery requires the stricter reconciliation path')
            for key, value in {'operation_class': host['operation_class'],
                               'capability': host['capability'], 'operation': host['operation'],
                               'mutating': bool(host['mutating'])}.items():
                if proof[key] != value:
                    raise DeliveryReconciliationRequired('Host operation summary mismatch: ' + key)
            try:
                argv = json.loads(host['argv_json'])
            except (TypeError, ValueError):
                raise DeliveryReconciliationRequired('Host argv evidence is malformed')
            if proof['argv'] != argv:
                raise DeliveryReconciliationRequired('Host argv summary mismatch')
            provider = json.loads(row['provider_item_json'])
            if provider.get('success') is not False:
                raise DeliveryReconciliationRequired('Provider failure item identity is not exact')
            if ((provider.get('namespace') not in (None, identity.get('namespace'))) or
                    (provider.get('tool') not in (None, identity.get('tool')))):
                raise DeliveryReconciliationRequired('Provider failure item identity is not exact')
            stamp = time.time()
            evidence = {'proof': proof, 'host_receipt': dict(host), 'provider_item': provider,
                        'original_delivery_state': row['delivery_state'],
                        'original_execution_state': row['execution_state']}
            updated = conn.execute('''UPDATE host_tool_deliveries SET reconciliation_state='RESOLVED',
                reconciled_at=?,reconciliation_json=? WHERE execution_ref=? AND tool_call_id=?
                AND reconciliation_state='OPEN' ''',
                (stamp, json.dumps(evidence, sort_keys=True, ensure_ascii=False),
                 self.execution_ref, call_id)).rowcount
            if updated != 1:
                raise DeliveryReconciliationRequired('delivery reconciliation CAS lost')
            conn.execute('''CREATE TABLE IF NOT EXISTS host_delivery_reconciliation_audit (
                execution_ref TEXT NOT NULL, tool_call_id TEXT NOT NULL,
                proof_json TEXT NOT NULL, host_receipt_json TEXT NOT NULL,
                provider_item_json TEXT NOT NULL, reconciled_at REAL NOT NULL,
                PRIMARY KEY(execution_ref,tool_call_id))''')
            conn.execute('''INSERT INTO host_delivery_reconciliation_audit VALUES (?,?,?,?,?,?)''',
                (self.execution_ref, call_id, json.dumps(proof, sort_keys=True),
                 json.dumps(dict(host), sort_keys=True), row['provider_item_json'], stamp))
            return {'state': 'RESOLVED', 'execution_ref': self.execution_ref,
                    'tool_call_id': call_id, 'delivery_state': 'FAILED',
                    'reconciliation_state': 'RESOLVED',
                    'provider_failure_sha256': row['provider_failure_sha256'],
                    'host_execution_ref': proof['host_execution_ref']}
