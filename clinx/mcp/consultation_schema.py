"""Historical consultation tool schema; contains no storage or execution."""

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
