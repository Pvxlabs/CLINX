"""Bounded ChatGPT consultation ledger, on the existing CLINX task database.

This is message coordination only. It never starts/resumes a Codex execution.
OP uses the same stdio MCP boundary as Codex; no HTTP listener or credentials.
"""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import time
import uuid



def identifier(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("CONSULTATION_INVALID_UUID")
    return value


def bounded(value, maximum, label):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > maximum:
        raise ValueError("CONSULTATION_INVALID_" + label)
    return value


class Consultations:
    def __init__(self, registry):
        self.registry = registry
        with closing(registry._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS chatgpt_bindings (
                    binding_id TEXT PRIMARY KEY, client_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL UNIQUE, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS chatgpt_consultations (
                    request_id TEXT PRIMARY KEY, binding_id TEXT NOT NULL,
                    task_ref TEXT NOT NULL, execution_ref TEXT, question TEXT NOT NULL,
                    context TEXT NOT NULL, payload_hash TEXT NOT NULL, prompt TEXT NOT NULL,
                    state TEXT NOT NULL, attempted INTEGER NOT NULL DEFAULT 0,
                    attempt_id TEXT, user_message_id TEXT, reply_id TEXT UNIQUE,
                    reply TEXT, reply_hash TEXT, reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    FOREIGN KEY(binding_id) REFERENCES chatgpt_bindings(binding_id),
                    FOREIGN KEY(task_ref) REFERENCES tasks(task_id));
            """)

    def bind(self, *, client_id, conversation_id, approved):
        identifier(client_id); identifier(conversation_id)
        if approved is not True:
            raise ValueError("CONSULTATION_EXPLICIT_BINDING_REQUIRED")
        with closing(self.registry._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM chatgpt_bindings WHERE conversation_id=?", (conversation_id,)).fetchone()
            if row:
                if row['client_id'] != client_id:
                    raise ValueError("CONSULTATION_CONVERSATION_ALREADY_BOUND")
                return dict(row)
            row = dict(binding_id=str(uuid.uuid4()), client_id=client_id,
                       conversation_id=conversation_id, created_at=time.time())
            db.execute("INSERT INTO chatgpt_bindings VALUES (:binding_id,:client_id,:conversation_id,:created_at)", row)
            return row

    @staticmethod
    def _binding(db, binding_id, client_id=None):
        identifier(binding_id)
        row = db.execute("SELECT * FROM chatgpt_bindings WHERE binding_id=?", (binding_id,)).fetchone()
        if not row or client_id is not None and row['client_id'] != client_id:
            raise ValueError("CONSULTATION_BINDING_MISMATCH")
        return row

    def submit(self, *, request_id, binding_id, task_ref, question, context='', execution_ref=None):
        identifier(request_id)
        bounded(question, 12000, 'QUESTION')
        if not isinstance(context, str) or len(context.encode()) > 8000:
            raise ValueError("CONSULTATION_INVALID_CONTEXT")
        self.registry.get_task(task_ref)
        if execution_ref:
            execution = self.registry.get_execution_record(execution_ref)
            if not execution or execution['task_id'] != task_ref:
                raise ValueError("CONSULTATION_EXECUTION_MISMATCH")
        payload = dict(binding_id=binding_id, task_ref=task_ref, execution_ref=execution_ref,
                       question=question, context=context)
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        marker = 'OP_CONSULT_REQUEST:' + request_id
        prompt = (f"Codex 咨询请求（只需回答问题，不派发或继续任何任务）。\n{marker}\n"
                  f"请在回复正文中原样包含上面的请求标记，便于严格关联本轮回复。\n"
                  f"task_ref: {task_ref}\nexecution_ref: {execution_ref or '(none)'}\n"
                  f"<execution_context>\n{context}\n</execution_context>\n<question>\n{question}\n</question>")
        now = time.time()
        with closing(self.registry._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._binding(db, binding_id)
            old = db.execute("SELECT * FROM chatgpt_consultations WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if old['payload_hash'] != digest:
                    raise ValueError("CONSULTATION_REQUEST_ID_CONFLICT")
            else:
                db.execute("""INSERT INTO chatgpt_consultations
                    (request_id,binding_id,task_ref,execution_ref,question,context,payload_hash,prompt,state,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?, 'PENDING',?,?)""",
                    (request_id,binding_id,task_ref,execution_ref,question,context,digest,prompt,now,now))
        return self.get(request_id=request_id)

    def get(self, *, request_id):
        identifier(request_id)
        with closing(self.registry._connect()) as db, db:
            row = db.execute("""SELECT c.*,b.conversation_id FROM chatgpt_consultations c
                JOIN chatgpt_bindings b USING(binding_id) WHERE request_id=?""", (request_id,)).fetchone()
            if not row:
                raise ValueError("CONSULTATION_NOT_FOUND")
            return dict(row)

    def pending(self, *, binding_id, client_id):
        with closing(self.registry._connect()) as db, db:
            binding = self._binding(db, binding_id, identifier(client_id))
            rows = db.execute("""SELECT * FROM chatgpt_consultations WHERE binding_id=?
                AND state NOT IN ('COMPLETED','CANCELLED') ORDER BY created_at LIMIT 20""", (binding_id,)).fetchall()
            return dict(binding=dict(binding), requests=[dict(row) for row in rows])

    def transition(self, *, request_id, binding_id, client_id, action, attempt_id=None,
                   user_message_id=None, reply_id=None, reply=None, reason=''):
        identifier(request_id)
        if not isinstance(reason, str) or len(reason.encode()) > 512:
            raise ValueError("CONSULTATION_INVALID_REASON")
        with closing(self.registry._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._binding(db, binding_id, identifier(client_id))
            row = db.execute("SELECT * FROM chatgpt_consultations WHERE request_id=? AND binding_id=?", (request_id,binding_id)).fetchone()
            if not row:
                raise ValueError("CONSULTATION_BINDING_MISMATCH")
            state = row['state']
            changes = {}
            if action == 'begin':
                identifier(attempt_id)
                if state == 'PENDING' and not row['attempted']:
                    # Only this successful CAS grants one UI write; retries receive no permission.
                    changes = dict(state='SENDING', attempted=1, attempt_id=attempt_id)
                else:
                    return dict(self.get(request_id=request_id), send_permitted=False)
            elif action == 'sent':
                bounded(user_message_id, 256, 'USER_MESSAGE_ID')
                if state not in {'SENDING', 'AWAITING_REPLY'}:
                    raise ValueError("CONSULTATION_INVALID_TRANSITION")
                if row['user_message_id'] and row['user_message_id'] != user_message_id:
                    raise ValueError("CONSULTATION_USER_MESSAGE_MISMATCH")
                changes = dict(state='AWAITING_REPLY', user_message_id=user_message_id, reason='')
            elif action == 'complete':
                identifier(reply_id); bounded(reply, 1024*1024, 'REPLY')
                if 'OP_CONSULT_REQUEST:' + request_id not in reply:
                    raise ValueError("CONSULTATION_REPLY_MARKER_MISMATCH")
                digest = hashlib.sha256(reply.encode()).hexdigest()
                if state == 'COMPLETED':
                    if row['reply_id'] != reply_id or row['reply_hash'] != digest:
                        raise ValueError("CONSULTATION_REPLY_CONFLICT")
                    return self.get(request_id=request_id)
                if state != 'AWAITING_REPLY' or not row['user_message_id']:
                    raise ValueError("CONSULTATION_INVALID_TRANSITION")
                if db.execute("SELECT 1 FROM chatgpt_consultations WHERE reply_id=?", (reply_id,)).fetchone():
                    raise ValueError("CONSULTATION_REPLY_ALREADY_USED")
                changes = dict(state='COMPLETED', reply_id=reply_id, reply=reply,
                               reply_hash=digest, reason='')
            elif action == 'pause':
                if state in {'COMPLETED','CANCELLED'}:
                    return self.get(request_id=request_id)
                changes = dict(state='PAUSED', reason=bounded(reason,512,'REASON'))
            elif action == 'cancel':
                if state in {'COMPLETED','CANCELLED'}:
                    return self.get(request_id=request_id)
                if state != 'PAUSED':
                    raise ValueError("CONSULTATION_PAUSE_BEFORE_CANCEL")
                changes = dict(state='CANCELLED', reason=bounded(reason,512,'REASON'))
            elif action == 'resume':
                if state != 'PAUSED':
                    raise ValueError("CONSULTATION_INVALID_TRANSITION")
                # A previously attempted send ALWAYS returns to read-only reconciliation.
                changes = dict(state='SENDING' if row['attempted'] else 'PENDING', reason='')
            else:
                raise ValueError("CONSULTATION_INVALID_ACTION")
            changes['updated_at'] = time.time()
            db.execute('UPDATE chatgpt_consultations SET ' + ','.join(key+'=?' for key in changes) + ' WHERE request_id=?', (*changes.values(),request_id))
        return dict(self.get(request_id=request_id), send_permitted=action == 'begin')


NAMES = ('clinx_bind_chatgpt', 'clinx_submit_consultation', 'clinx_get_consultation',
         'clinx_list_consultations', 'clinx_update_consultation')


def tools():
    string = {'type':'string'}
    uid = {'type':'string','format':'uuid'}
    common = dict(binding_id=uid, client_id=uid)
    specs = [
        (NAMES[0], 'Bind the current OP-selected real ChatGPT conversation after explicit user approval; never infer its URL.',
         dict(client_id=uid,conversation_id=uid,approved={'type':'boolean','const':True}), ['client_id','conversation_id','approved']),
        (NAMES[1], 'Persist one ChatGPT question for an existing CLINX task. Same request_id is idempotent; changed payload conflicts. Does not start Codex.',
         dict(request_id=uid,binding_id=uid,task_ref=string,question={'type':'string','maxLength':12000},context={'type':'string','maxLength':8000},execution_ref=string),
         ['request_id','binding_id','task_ref','question']),
        (NAMES[2], 'Read exact consultation status and verified reply. Query first after a timeout; never blindly resend. COMPLETED means reply captured, not Codex execution success.',
         dict(request_id=uid), ['request_id']),
        (NAMES[3], 'OP private stdio: list outstanding requests for the exact persisted conversation/client binding.',common,['binding_id','client_id']),
        (NAMES[4], 'OP private stdio: CAS before a single UI send, reconcile, return verified reply, or pause/resume. Resume of attempted writes is read-only. No Codex execution.',
         dict(common,request_id=uid,action={'type':'string','enum':['begin','sent','complete','pause','resume','cancel']},attempt_id=uid,user_message_id=string,reply_id=uid,reply=string,reason=string),
         ['request_id','binding_id','client_id','action']),
    ]
    return [dict(name=name,description=description,inputSchema=dict(type='object',properties=properties,required=required,additionalProperties=False),
                 annotations=dict(readOnlyHint=name in NAMES[2:4],destructiveHint=False,idempotentHint=True,openWorldHint=False))
            for name,description,properties,required in specs]


def call(registry, name, arguments):
    if getattr(registry, 'read_only', False):
        from execution_owner import ExecutionOwnerClient
        return ExecutionOwnerClient(registry).control('consultation', name, arguments)
    ledger = Consultations(registry)
    return dict(zip(NAMES, (ledger.bind,ledger.submit,ledger.get,ledger.pending,ledger.transition)))[name](**arguments)


def standalone_server(registry):
    from mcp_server import ClinxMCPServer
    server = ClinxMCPServer.__new__(ClinxMCPServer)
    server.integration = None
    server.consultation_registry = registry
    server.public_tools = tools()
    server.public_tool_names = set(NAMES)
    return server
