"""Report observed failures without guessing an external approval decision."""
import re

import app_server


def safe_reason(value):
    value = re.sub(r'(?i)(authorization\s*[:=]\s*|bearer\s+)\S+', r'\1[REDACTED]', str(value))
    value = re.sub(r'(?i)((?:token|api_key|secret|password)\s*[=:]\s*)[^\s,;]+', r'\1[REDACTED]', value)
    return value[:2000]


def failure_details(name, arguments, exc, registry=None):
    reason = safe_reason(exc)
    code = getattr(exc, 'code', None)
    match = re.match(r'^([A-Z][A-Z_0-9]+)(?:[:=]|$)', str(exc))
    code = code or (match.group(1) if match else 'UNKNOWN')
    stage, source, certainty = 'UNKNOWN', 'CLINX_SERVER', 'UNKNOWN'
    recovery = 'READ_EXACT_REQUEST_AND_EXECUTION; DO_NOT_REPLAY'
    if isinstance(exc, app_server.ModelCapabilityError):
        code, stage, source, certainty = 'MODEL_CAPABILITY_UNAVAILABLE', 'MODEL_RESOLUTION', 'PROVIDER', 'NOT_EXECUTED'
    elif isinstance(exc, app_server.AppServerTransportError):
        code, stage, source = 'PROVIDER_TRANSPORT_ERROR', 'APP_SERVER_TRANSPORT', 'PROVIDER_TRANSPORT'
    elif isinstance(exc, app_server.AppServerRemoteError):
        code = getattr(exc, 'code', None) or 'PROVIDER_REQUEST_REJECTED'
        stage, source = getattr(exc, 'method', 'APP_SERVER_REQUEST'), 'PROVIDER'
        if getattr(exc, 'side_effect', None) == 'NONE' or stage == 'turn/start':
            certainty = 'NOT_EXECUTED'
    elif isinstance(exc, TypeError) and 'unexpected keyword argument' in str(exc):
        code, stage, certainty = 'CALL_ARGUMENT_INVALID', 'CALL_VALIDATION', 'NOT_EXECUTED'
        recovery = 'USE_EXPORTED_INPUT_SCHEMA'
    elif code in {'INVALID_THREAD_ID', 'INVALID_CODEX_URI', 'IDENTITY_SELECTOR_CONFLICT', 'PREPARED_REQUEST_INVALID_REF'}:
        stage, certainty, recovery = 'CALL_VALIDATION', 'NOT_EXECUTED', 'USE_EXPORTED_INPUT_SCHEMA'
    elif code.startswith(('POLICY_', 'PREPARED_REQUEST_', 'PREPARED_EXECUTION_', 'AUTHORITY_', 'PRODUCTION_SCOPE_', 'TARGET_', 'INVALID_TARGET_')):
        stage, certainty = 'AUTHORITY_VALIDATION', 'NOT_EXECUTED'
        recovery = ('READ_APPLIED_VERSION_BY_ORIGINAL_REQUEST' if name == 'clinx_apply_policy_reauthorization'
                    else 'READ_CURRENT_POLICY_AND_REQUEST; CORRECT_SCOPE_OR_BASELINE')
    elif code.startswith(('HOST_EXECUTOR_', 'CAPABILITY_', 'WORKFLOW_EXECUTABLE_', 'OPERATION_')):
        stage, certainty, recovery = 'CAPABILITY_VALIDATION', 'NOT_EXECUTED', 'CORRECT_EXACT_OPERATION_OR_CAPABILITY'
    elif code.startswith('EXECUTION_OWNER_'):
        stage, source = 'EXECUTION_OWNER', 'CLINX_EXECUTION_OWNER'
        if code in {'EXECUTION_OWNER_UNAVAILABLE', 'EXECUTION_OWNER_INVALID_REQUEST', 'EXECUTION_OWNER_DRAINING'}:
            certainty = 'NOT_EXECUTED'
    if getattr(exc, 'side_effect', None) == 'NONE':
        certainty = 'NOT_EXECUTED'
    request_ref = arguments.get('prepared_reauthorization_ref') or arguments.get('prepared_execution_ref') or arguments.get('request_ref')
    correlation = {k: arguments[k] for k in ('task_ref', 'execution_ref') if k in arguments}
    correlation.update(request_ref=request_ref, tool=name)
    evidence = None
    if registry is not None and request_ref:
        try:
            from prepared_requests import read_prepared_request
            evidence = read_prepared_request(registry, request_ref)
            correlation.update(task_ref=evidence.get('task_ref'), execution_ref=evidence.get('execution_ref'),
                               request_hash=evidence.get('request_hash'))
            if evidence.get('request_state') == 'APPLIED':
                certainty = 'APPLIED'
            elif evidence.get('request_state') in {'RUNNING', 'DISPATCHED'} or evidence.get('execution_present'):
                # Persistence establishes association, not Provider running/completion.
                certainty = 'UNKNOWN'
        except Exception:
            pass
    result = {'failure_code': code, 'failure_stage': stage, 'failure_source': source,
              'failure_reason': reason, 'server_received': True, 'caller_approval': 'NOT_OBSERVED',
              'correlation': correlation, 'side_effect_certainty': certainty,
              'outcome_certainty': 'KNOWN' if certainty in {'NOT_EXECUTED', 'APPLIED'} else 'UNKNOWN',
              'retry_allowed': False, 'reconciliation_required': certainty == 'UNKNOWN',
              'recovery_action': recovery}
    result.update(getattr(exc, 'details', {}) or {})
    return result
