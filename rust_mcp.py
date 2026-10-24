"""Public schemas for the Rust command plane; no authority decisions here."""


def configure(tools):
    def obj(properties, required=()):
        return dict(type='object', properties=properties, required=list(required), additionalProperties=False)

    string = dict(type='string')
    nullable = dict(type=['string', 'null'])
    boolean = dict(type='boolean')
    policy = dict(type='object')
    seal = obj({k: string for k in ('cwd', 'branch', 'head')}, ('cwd', 'branch', 'head'))
    task = dict(task_ref=string, title=string, project=string, host=string, workspace_contract=seal,
                thread_id=nullable, lifecycle=string, policy_hash=string, execution_policy=policy,
                active_execution=nullable, read_only=boolean, authority=dict(const='RUST', type='string'))
    execution = {
        **{k: string for k in ('execution_ref', 'task_ref', 'prepared_execution_ref', 'status',
                              'dispatch_state', 'resource_state', 'delivery_certainty')},
        'native_turn_id': nullable, 'thread_id': nullable, 'execution_started': boolean,
        'cancel_requested': boolean, 'execution_result': dict(type=['object', 'null']),
        'business_outcome': dict(type='string', const='NOT_ASSERTED'),
        'authority': dict(type='string', const='RUST'), 'read_only': boolean,
    }
    outputs = {
        'clinx_prepare_execution': obj({**{k:string for k in ('prepared_execution_ref', 'task_ref', 'project',
            'host', 'title', 'status', 'request_hash', 'policy_hash')}, 'model':nullable, 'reasoning_effort':nullable,
            'workspace_contract':seal, 'execution_policy':policy, 'execution_started':dict(type='boolean',const=False),
            'read_only':boolean}, ('prepared_execution_ref','task_ref','request_hash','workspace_contract','execution_started')),
        'clinx_start_execution': obj(execution, ('execution_ref','task_ref','status','resource_state','authority')),
        'clinx_cancel_execution': obj(execution, ('execution_ref','task_ref','status','resource_state','authority')),
        'clinx_find_task': obj(dict(tasks=dict(type='array',items=obj(task,('task_ref',))),read_only=boolean), ('tasks','read_only')),
        'clinx_list_projects': obj(dict(projects=dict(type='array',items=obj(dict(project=string,host=string,cwd=string))),read_only=boolean), ('projects','read_only')),
        'clinx_get_capabilities': obj(dict(authority=dict(const='RUST',type='string'),capabilities=dict(type='array',items=string),
            legacy_history=boolean,host_tools=boolean,read_only=boolean), ('authority','capabilities')),
        'clinx_adopt_conversation': obj(dict(adoption_ref=string,adoption_status=string,task_ref=string,
            thread_id=string,workspace_contract=seal,read_only=boolean), ('adoption_status','task_ref','thread_id')),
    }
    for tool in tools:
        name = tool['name']
        if name in {'clinx_get_status', 'clinx_get_context'}:
            # Retain the published exact-native response contract as well as
            # Rust task/execution projections. All branches remain read-only.
            native = tool['outputSchema']
            native['properties']['thread_id'] = nullable
            native['properties']['execution_result']['anyOf'][0]['properties'].update(
                outcome=string, result_ref=string)
            if name == 'clinx_get_status':
                tool['outputSchema'] = dict(anyOf=[native,
                    obj(execution, ('execution_ref','task_ref','status','resource_state','authority')),
                    obj(task, ('task_ref','lifecycle','workspace_contract','authority'))])
            else:
                tool['outputSchema']['properties']['thread_id'] = nullable
                tool['outputSchema']['properties']['context'] = dict(type='object')
                tool['inputSchema']['anyOf'].append({'required': ['execution_ref'],
                    'not': {'anyOf': [{'required': [k]} for k in ('query', 'thread_id', 'codex_uri')]}})
        if name in outputs:
            tool['outputSchema'] = outputs[name]
        if name == 'clinx_prepare_execution':
            tool['description'] = 'Persist a reviewed Rust execution request with exact cwd, branch and HEAD. Does not dispatch. HOST_EXECUTOR requires explicit operation scopes.'
        elif name == 'clinx_adopt_conversation':
            tool['inputSchema']['properties']['exact_turn_id'] = dict(type='string', minLength=1)
            tool['inputSchema']['properties']['workspace_contract'] = seal
            tool['description'] = 'Adopt an exact idle thread visible on the confined Provider. Supply exact_turn_id for a terminal predecessor checkpoint required by Host successor. Does not transfer a Desktop writer.'
        elif name == 'clinx_start_execution':
            tool['description'] = 'Submit the prepared request to the sole Rust authority. The confined Provider receives one durable send permit. Read the same execution after an unknown response; do not prepare or dispatch another attempt.'
        elif name == 'clinx_get_prepared_request':
            tool['inputSchema']['properties']['request_ref']['pattern'] = '^(prepared|reauth)_[0-9a-f]{32}$'
