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
            return [dict(row) for row in conn.execute(
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
            prior = conn.execute('SELECT 1 FROM host_tool_deliveries WHERE execution_ref=? AND '
                                 '(tool_call_id=? OR delivery_state != ?)',
                                 (self.execution_ref, call_id, 'DELIVERED')).fetchone()
            if prior:
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
        state = ('COMMAND_EXECUTED_RESULT_DELIVERY_FAILED' if result.get('exit_code') == 0
                 else 'COMMAND_EXECUTION_FAILED')
        with self.registry._connect() as conn:
            host = conn.execute('SELECT execution_ref,tool_call_id FROM host_executions '
                                'WHERE host_execution_ref=?', (host_ref,)).fetchone()
            if host is None or (host['execution_ref'], host['tool_call_id']) != (self.execution_ref, call_id):
                raise DeliveryReconciliationRequired('Host evidence does not own this tool call')
            conn.execute('''UPDATE host_tool_deliveries SET host_execution_ref=?,host_exit_code=?,
                execution_state=?,host_completed_at=?,failure_code=?
                WHERE execution_ref=? AND tool_call_id=?''',
                (host_ref, result.get('exit_code'), state, time.time(),
                 'RESULT_DELIVERY_FAILED_AFTER_EXECUTION', self.execution_ref, call_id))

    def sent(self, call_id, request_id):
        with self.registry._connect() as conn:
            conn.execute('''UPDATE host_tool_deliveries SET response_id=?,response_sent_at=?
                WHERE execution_ref=? AND tool_call_id=?''',
                (json.dumps(request_id), time.time(), self.execution_ref, call_id))

    def failed(self, call_id):
        with self.registry._connect() as conn:
            host = conn.execute('SELECT host_execution_ref,exit_code FROM host_executions '
                                'WHERE execution_ref=? AND tool_call_id=?',
                                (self.execution_ref, call_id)).fetchone()
            state = ('COMMAND_NOT_DISPATCHED' if host is None else
                     'COMMAND_DISPATCHED' if host['exit_code'] is None else
                     'COMMAND_EXECUTED_RESULT_DELIVERY_FAILED' if host['exit_code'] == 0 else
                     'COMMAND_EXECUTION_FAILED')
            code = ('COMMAND_NOT_DISPATCHED' if host is None else
                    'RESULT_DELIVERY_FAILED_AFTER_EXECUTION' if host['exit_code'] is not None else
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

    def observe(self, message):
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
            delivered = False
            for part in item.get('contentItems', item.get('content_items', [])) or []:
                if not isinstance(part, dict):
                    continue
                try:
                    body = json.loads(part.get('text', ''))
                except (ValueError, TypeError):
                    continue
                if isinstance(body, dict) and row['host_execution_ref']:
                    delivered |= (body.get('host_execution_ref') == row['host_execution_ref']
                                  and body.get('execution_ref') == self.execution_ref)
            delivered = delivered and item.get('success') is True and row['response_sent_at'] is not None
            state = row['execution_state']
            if delivered and row['host_exit_code'] == 0:
                state = 'COMMAND_EXECUTED_RESULT_DELIVERED'
            conn.execute('''UPDATE host_tool_deliveries SET delivery_state=?,execution_state=?,
                failure_code=?,acknowledged_at=? WHERE execution_ref=? AND tool_call_id=?''',
                ('DELIVERED' if delivered else 'FAILED', state,
                 None if delivered else 'RESULT_DELIVERY_FAILED_AFTER_EXECUTION',
                 time.time(), self.execution_ref, call_id))
