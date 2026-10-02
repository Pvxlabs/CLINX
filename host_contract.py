"""Safe, executable Host vocabulary shared by discovery, schema and validation.

No credential, URL or registered argv is published here. Configured workspace
roots are public capability metadata, never production authority.
"""
from execution_policy import (
    DEVELOPMENT_CAPABILITIES, DEVELOPMENT_MUTATION, HOST_CAPABILITIES,
    HOST_CAPABILITY_PROBES, PRODUCTION_MUTATION, PRODUCTION_READ_ONLY, READ_ONLY_HOST,
)

READ_CLASSES = (READ_ONLY_HOST, DEVELOPMENT_MUTATION, PRODUCTION_READ_ONLY, PRODUCTION_MUTATION)


def operation_catalog(config):
    catalog = {capability: {} for capability in HOST_CAPABILITIES}

    def add(capability, names, *, classes=READ_CLASSES, required=(), optional=(),
            mutating=False, effects='NONE', targets=None):
        if targets:
            classes = tuple(c for c in classes if any(c in t['operation_classes'] for t in targets))
        for name in names:
            catalog[capability][name] = {
                'capability': capability, 'operation': name,
                'description': name.replace('_', ' ') + '; ' + effects,
                'target_kind': ('TRUSTED_WORKSPACE' if name in {'path_read', 'development_command'} else
                                'REGISTERED_TARGET' if targets is not None else
                                'REGISTERED_PROJECT' if capability in {'GIT', 'HOST_FILESYSTEM'} else 'LOCAL_HOST'),
                'argument_schema': {'type': 'object', 'required': list(required),
                    'properties': {key: ({'type': 'array', 'items': {'type': 'string'}, 'minItems': 1} if key == 'argv'
                                        else {'type': 'integer', 'minimum': 1, 'maximum': 500} if key == 'lines'
                                        else {'type': 'string', 'minLength': 1}) for key in (*required, *optional)},
                    'additionalProperties': False},
                'operation_class': classes[0], 'operation_classes': list(classes),
                'required_arguments': list(required), 'optional_arguments': list(optional),
                'mutating': mutating, 'side_effects': effects,
                **({'registered_targets': targets} if targets is not None else {}),
            }

    services = [{'identity': target.alias, 'operation_classes': list(target.operation_classes)}
                for target in config.services]
    add('LOCAL_HOST_PROCESS', ('host_identity', 'user_identity', 'working_directory', 'uptime'))
    add('LOCAL_HOST_PROCESS', ('service_process',), required=('target',), targets=services)
    add('SYSTEMD_USER', ('service_is_active', 'service_status'), required=('target',), targets=services)
    add('SYSTEMD_USER', ('service_journal',), required=('target',), optional=('lines',), targets=services)
    add('SYSTEMD_USER', ('service_restart',), required=('target',), targets=services,
        classes=(DEVELOPMENT_MUTATION, PRODUCTION_MUTATION), mutating=True, effects='SERVICE_RESTART')
    add('GIT', ('head', 'status', 'remote_main_head', 'ahead_behind'))
    add('GIT', ('fetch_origin',), mutating=True, effects='LOCAL_OBJECTS_AND_REMOTE_TRACKING_REFS_ONLY')
    add('GIT', ('push_current_branch',), classes=(DEVELOPMENT_MUTATION,), optional=('remote',),
        mutating=True, effects='REGISTERED_ORIGIN_NON_FORCE_CURRENT_BRANCH')
    add('HOST_FILESYSTEM', ('git_head', 'git_status'))
    add('HOST_FILESYSTEM', ('path_read',), required=('path',), optional=('lines',))
    add('HOST_FILESYSTEM', ('marker_create', 'marker_remove'), required=('name',),
        classes=(DEVELOPMENT_MUTATION,), mutating=True, effects='PROJECT_MARKER')
    for capability in DEVELOPMENT_CAPABILITIES:
        add(capability, ('development_command',), required=('argv',), classes=(DEVELOPMENT_MUTATION,),
            mutating=True, effects='TRUSTED_WORKSPACE_DEVELOPMENT')
    for name, attribute in (('dns_lookup', 'dns_name'), ('https_head', 'url')):
        targets = [{'identity': t.alias, 'operation_classes': list(t.operation_classes)}
                   for t in config.network_targets if getattr(t, attribute)]
        add('OUTBOUND_NETWORK', (name,), required=('target',), targets=targets)
    ssh_names = {'remote_hostname', 'remote_uptime', 'remote_true'}
    ssh_names.update(name for target in config.ssh_targets for name, _ in target.commands)
    for name in sorted(ssh_names):
        targets = [{'identity': t.alias, 'operation_classes': list(t.operation_classes)}
                   for t in config.ssh_targets
                   if name in {'remote_hostname', 'remote_uptime', 'remote_true'} or name in dict(t.commands)]
        # Configured argv can mutate; expose the maximum configured authority honestly.
        mutating = any(c in {DEVELOPMENT_MUTATION, PRODUCTION_MUTATION}
                       for t in targets for c in t['operation_classes'])
        add('SSH', (name,), required=('target',), targets=targets, mutating=mutating,
            effects='REGISTERED_REMOTE_COMMAND' if mutating else 'REMOTE_READ')
    for target in config.local_commands:
        if dict(target.commands).get(target.alias):
            mutating = any(c in {DEVELOPMENT_MUTATION, PRODUCTION_MUTATION} for c in target.operation_classes)
            add('LOCAL_HOST_PROCESS', ('registered_command:' + target.alias,),
                classes=target.operation_classes, targets=[{'identity': target.alias,
                'operation_classes': list(target.operation_classes)}], mutating=mutating,
                effects='REGISTERED_LOCAL_COMMAND' if mutating else 'LOCAL_READ')
    for workflow in config.workflows:
        operations = catalog['LOCAL_HOST_PROCESS']
        existing = operations.get(workflow.operation)
        target = {'identity': workflow.target, 'operation_classes': [workflow.operation_class],
                  'workflow': workflow.identity, 'parameter_schema': workflow.parameter_schema}
        if existing:
            if workflow.operation_class not in existing['operation_classes']:
                existing['operation_classes'].append(workflow.operation_class)
            existing['registered_targets'].append(target)
            continue
        add('LOCAL_HOST_PROCESS', (workflow.operation,), classes=(workflow.operation_class,),
            required=('target', 'parameters'), targets=[target],
            mutating=workflow.operation_class == PRODUCTION_MUTATION, effects='CONTROLLER_OWNED_WORKFLOW')
        operations[workflow.operation]['argument_schema']['properties']['parameters'] = {'type': 'object'}
    for operations in catalog.values():
        for spec in operations.values():
            spec.update(implemented=True, client_exposed='NOT_OBSERVED', runtime_health='NOT_PROBED', task_authorized=None)
            for target in spec.get('registered_targets', []):
                target['task_target_authorized'] = None
    return catalog


def executable_contract(config, probes=None, policy=None):
    catalog = operation_catalog(config)
    for cap, operations in catalog.items():
        for name, spec in operations.items():
            targets = spec.get('registered_targets', [])
            spec['implemented'] = True
            spec['client_exposed'] = 'NOT_OBSERVED'
            spec['runtime_health'] = 'NOT_PROBED'
            spec['task_authorized'] = None if policy is None else any(
                policy.permits_operation(cap, name, cls, target.get('identity', ''))
                and (not name.startswith('workflow:') or policy.operation_scopes is not None)
                for target in (targets or [{}]) for cls in spec['operation_classes'])
            for target in targets:
                target['task_target_authorized'] = None if policy is None else any(
                    policy.permits_operation(cap, name, cls, target['identity'])
                    and (not name.startswith('workflow:') or policy.operation_scopes is not None)
                    for cls in target['operation_classes'])
    return {
        'contract': 'CLINX_HOST_EXECUTION_CONTRACT_V2',
        'authority_granted': False,
        'authority_granted_meaning': 'DISCOVERY_DOES_NOT_GRANT_AUTHORITY; legacy field, not task authorization',
        'effective_authority': {'source': 'SEALED_EXECUTION_POLICY' if policy else 'NO_TASK_SELECTED',
            'task_authorized': None if policy is None else True,
            'production_mutation_authorized': None if policy is None else
                PRODUCTION_MUTATION in policy.operation_classes and policy.production_mutation_intent,
            'operation_scopes': None if policy is None else policy.as_dict().get('operation_scopes'),
            'target_authorization': 'CHECK_EXACT_OPERATION_SCOPE_AND_REGISTERED_TARGET',
            'runtime_health': 'NOT_PROBED', 'client_exposure': 'NOT_OBSERVED'},
        'trusted_workspace_roots': [str(root) for root in config.trusted_workspace_roots],
        'path_authority': {
            'resolution': 'Path.resolve',
            'relative_base': 'registered_task_cwd',
            'unconfigured_roots': 'REGISTERED_PROJECT_ONLY',
            'production_authority_granted': False,
            'production_authority_granted_meaning': 'PATH_ACCESS_ALONE_GRANTS_NO_PRODUCTION_AUTHORITY',
            'git_write_authority': 'REGISTERED_TASK_ORIGIN_AND_BRANCH',
        },
        'constraints': 'Probe availability does not grant authority or prove execution health. '
                       'Use exact operations and safe target identities below. Policy and target classes intersect. '
                       'Git canonical branch/origin come from the registered task, never caller refspecs. '
                       'Trusted workspace path access does not grant production or network authority. '
                       'fetch_origin writes local objects/remote-tracking refs and requires the existing lease; '
                       'it never changes index/worktree or merges/rebases. '
                       'No automatic command retry. Proven pre-dispatch rejection permits a new legal call.',
        'capabilities': {
            cap: {'request_capability': cap,
                  'probe_key': HOST_CAPABILITY_PROBES[cap],
                  'probe': (probes or {}).get(HOST_CAPABILITY_PROBES[cap], 'NOT_PROBED'),
                  'operations': {name: spec for name, spec in operations.items()
                                 if policy is None or (cap in policy.required_capabilities and
                                     set(spec['operation_classes']).intersection(policy.operation_classes))}}
            for cap, operations in catalog.items()
            if policy is None or cap in policy.required_capabilities
        },
    }
