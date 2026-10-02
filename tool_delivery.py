"""Durable correlation of Host execution and provider delivery, never a runner.

No command payloads or output are stored here. Host evidence remains authoritative
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
                PRIMARY KEY(execution_ref,tool_call_id))''')

    @staticmethod
    def records(registry, execution_ref: str):
        with registry._connect() as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='host_tool_deliveries'").fetchone():
                return []
            return [{**dict(row), **certainty(row)} for row in conn.execute(
                'SELECT * FROM host_tool_deliveries WHERE execution_ref=? ORDER BY admitted_at',
                (execution_ref,))]

    def admit(self, request, identity):
        params = request['params']
        call_id = params.get('callId')
        if not isinstance(call_id, str) or not call_id.strip():
            raise DeliveryReconciliationRequired('dynamic tool callId is required before dispatch')
        digest = hashlib.sha256(json.dumps(params, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with self.registry._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = conn.execute('SELECT * FROM host_tool_deliveries WHERE execution_ref=?',
                                (self.execution_ref,)).fetchall()
            for row in rows:
                if row['tool_call_id'] == call_id:
                    error = ToolCallReplayRejected('tool call already recorded; replay never executes Host again')
                    error.execution_state = row['execution_state']
                    error.reconciliation_required = requires_reconciliation(row)
                    raise error
            if any(requires_reconciliation(row) for row in rows):
                raise DeliveryReconciliationRequired('existing invocation requires reconciliation; command retry prohibited')
            conn.execute('''INSERT INTO host_tool_deliveries
                (execution_ref,tool_call_id,request_id,identity_json,request_hash,
                 execution_state,delivery_state,admitted_at) VALUES (?,?,?,?,?,?,?,?)''',
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
                execution_state=CASE WHEN delivery_state='FAILED' AND ?='COMMAND_EXECUTED_RESULT_DELIVERY_PENDING' THEN 'COMMAND_EXECUTED_RESULT_DELIVERY_FAILED' ELSE ? END,host_completed_at=?,failure_code=CASE WHEN delivery_state='FAILED' THEN failure_code ELSE NULL END
                WHERE execution_ref=? AND tool_call_id=?''',
                (host_ref, result.get('exit_code'), state, state, time.time(), self.execution_ref, call_id))

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
                (row is not None and row['execution_state'] == 'COMMAND_NOT_DISPATCHED'))
            state = ('COMMAND_NOT_DISPATCHED' if proven_not_dispatched else
                     'COMMAND_DISPATCHED' if host is None or host['exit_code'] is None else
                     'COMMAND_EXECUTED_RESULT_DELIVERY_FAILED' if host['exit_code'] == 0 else
                     'COMMAND_EXECUTION_FAILED')
            code = ((failure_code or (row['failure_code'] if row else None) or 'COMMAND_NOT_DISPATCHED')
                    if proven_not_dispatched else
                    'RESULT_DELIVERY_FAILED_AFTER_EXECUTION' if host is not None and host['exit_code'] is not None else
                    'RESULT_DELIVERY_UNCONFIRMED_AFTER_DISPATCH')
            conn.execute('''UPDATE host_tool_deliveries SET delivery_state='FAILED',
                execution_state=?,failure_code=?,host_execution_ref=?,host_exit_code=?
                WHERE execution_ref=? AND tool_call_id=? AND delivery_state != 'DELIVERED' ''',
                (state, code, host['host_execution_ref'] if host else None,
                 host['exit_code'] if host else None, self.execution_ref, call_id))
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
            if (item.get('namespace'), item.get('tool')) != (identity['namespace'], identity['tool']):
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
            state = row['execution_state']
            if delivered and row['host_exit_code'] == 0:
                state = 'COMMAND_EXECUTED_RESULT_DELIVERED'
            elif not delivered and row['host_exit_code'] == 0:
                state = 'COMMAND_EXECUTED_RESULT_DELIVERY_FAILED'
            conn.execute('''UPDATE host_tool_deliveries SET delivery_state=?,execution_state=?,
                failure_code=?,acknowledged_at=? WHERE execution_ref=? AND tool_call_id=?''',
                ('DELIVERED' if delivered else 'FAILED', state,
                 row['failure_code'] if row['execution_state'] == 'COMMAND_NOT_DISPATCHED' else
                 None if delivered else (
                  'RESULT_DELIVERY_FAILED_AFTER_EXECUTION' if row['host_exit_code'] is not None else
                  'RESULT_DELIVERY_UNCONFIRMED_AFTER_DISPATCH'),
                 time.time(), self.execution_ref, call_id))
