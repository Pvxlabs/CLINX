"""Real disposable Git repositories exercise task-owned Git target authority."""

import dataclasses
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from derived_git_targets import DerivedGitTargetError, inspect_worktree
from execution_policy import build_execution_policy, parse_execution_policy
from execution_semantics import build_routing_identity, parse_routing_identity
from host_executor import HostExecutor, HostExecutorConfig, HostExecutionRequest, AuthorityDenied, InvalidArguments, TargetNotRegistered
from m9_integration import ClinxIntegration, M9IntegrationError
from mcp_server import ClinxMCPServer
from task_registry import TaskRegistry, TaskRegistryError


def git(path, *args):
    return subprocess.run(('git', '-C', str(path), *args), check=True,
                          capture_output=True, text=True).stdout.strip()


def commit(path, message):
    git(path, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '--allow-empty', '-m', message)


def push_scope(target=''):
    return {'capability': 'GIT', 'operation': 'push_current_branch',
            'operation_class': 'DEVELOPMENT_MUTATION', 'target': target}


@pytest.fixture
def scene(tmp_path):
    remote = tmp_path / 'remote.git'
    parent = tmp_path / 'parent'
    derived = tmp_path / 'derived'
    remote.mkdir(); parent.mkdir()
    git(remote, 'init', '--bare', '-b', 'main')
    git(parent, 'init', '-b', 'main')
    git(parent, 'remote', 'add', 'origin', str(remote))
    commit(parent, 'base')
    git(parent, 'push', 'origin', 'main')
    git(parent, 'checkout', '-b', 'codex/parent')
    commit(parent, 'parent candidate')
    registry = TaskRegistry(tmp_path / 'tasks.sqlite3')
    config = HostExecutorConfig(enabled=True, trusted_workspace_roots=(tmp_path,))
    policy = build_execution_policy(execution_surface='HOST_EXECUTOR',
        required_capabilities=['GIT'], operation_classes=['DEVELOPMENT_MUTATION'],
        operation_scopes=[push_scope(), {'capability': 'GIT', 'operation': 'development_command',
            'operation_class': 'DEVELOPMENT_MUTATION', 'target': ''}])
    route = build_routing_identity(host='p620', surface='host_executor',
        provider='codex_app_server', transport='local_stdio', workspace_alias='p620',
        project_alias='fixture', project_identity='fixture', conversation_binding='thread',
        worktree_key=registry.worktree_key(host='p620', cwd=str(parent), repository_origin=str(remote)),
        authority_scopes=policy.authority_scopes)
    task = registry.create_task(host='p620', workspace_alias='p620', project_alias='fixture',
        project_name='Fixture', cwd=str(parent), repository_origin=str(remote),
        branch='codex/parent', title='Git fixture', execution_policy=policy, routing_identity=route)
    integration = ClinxIntegration(SimpleNamespace(host_executor=config), registry, None, None, None)
    executor = HostExecutor(config, registry)
    with registry.execution(task.task_id, execution_ref='exec_create'):
        created = executor.execute(HostExecutionRequest(task.task_id, 'exec_create', route, policy,
            'DEVELOPMENT_MUTATION', 'GIT', 'development_command',
            {'argv': ['git', 'worktree', 'add', '-b', 'codex/derived', str(derived)]}, parent))
        assert created['exit_code'] == 0
    commit(derived, 'derived candidate')
    return SimpleNamespace(tmp=tmp_path, remote=remote, parent=parent, derived=derived,
        registry=registry, config=config, policy=policy, route=route, task=task,
        integration=integration, executor=executor, creation_ref=created['host_execution_ref'])


def request(s, execution_ref, policy, route, args=None, operation='push_current_branch'):
    return HostExecutionRequest(s.task.task_id, execution_ref, route, policy,
        'DEVELOPMENT_MUTATION', 'GIT', operation, args or {}, s.parent)


def adopt(s):
    return s.integration.register_derived_git_target(approved=True, task_ref=s.task.task_id,
        path=str(s.derived), ownership_evidence='Operator reviewed the exact task Host receipt',
        creation_host_execution_ref=s.creation_ref)


def test_registered_and_derived_push_reauthorization_and_remote_sha(scene):
    s = scene
    with s.registry.execution(s.task.task_id, execution_ref='exec_original'):
        result = s.executor.execute(request(s, 'exec_original', s.policy, s.route))
        assert result['exit_code'] == 0
    assert git(s.remote, 'rev-parse', 'refs/heads/codex/parent') == git(s.parent, 'rev-parse', 'HEAD')
    original_policy = s.registry.get_execution_policy('exec_original')
    target = adopt(s)
    target_id = target['target_id']
    discovered = s.integration.get_effective_authority(task_ref=s.task.task_id)
    assert discovered['derived_git_targets'][0]['identity'] == target_id
    assert discovered['derived_git_targets'][0]['task_target_authorized'] is False
    with s.registry.execution(s.task.task_id, execution_ref='exec_before_grant'):
        with pytest.raises(AuthorityDenied):
            s.executor.execute(request(s, 'exec_before_grant', s.policy, s.route,
                {'target': target_id}))
    identity = s.integration.get_effective_authority(task_ref=s.task.task_id)
    new_policy = dict(execution_surface='HOST_EXECUTOR', required_capabilities=['GIT'],
        operation_classes=['DEVELOPMENT_MUTATION'],
        operation_scopes=[push_scope(), {'capability': 'GIT', 'operation': 'development_command',
            'operation_class': 'DEVELOPMENT_MUTATION', 'target': ''}, push_scope(target_id)])
    prepared = s.integration.prepare_policy_reauthorization(approved=True,
        task_ref=s.task.task_id, expected_policy_hash=identity['policy_hash'],
        target_policy=new_policy, reason='Approve this task-owned branch only')
    assert prepared['requested_scope']['derived_git_targets'][0]['target_id'] == target_id
    applied = s.integration.apply_policy_reauthorization(approved=True,
        prepared_reauthorization_ref=prepared['prepared_reauthorization_ref'])
    assert applied['policy_version'] == 1
    assert s.registry.get_execution_policy('exec_original') == original_policy
    assert not original_policy.permits_operation('GIT', 'push_current_branch',
        'DEVELOPMENT_MUTATION', target_id)
    current = s.registry.get_task(s.task.task_id)
    future_policy = parse_execution_policy(current.execution_policy_json)
    future_route = parse_routing_identity(current.routing_identity_json)
    with s.registry.execution(s.task.task_id, execution_ref='exec_future'):
        pushed = s.executor.execute(request(s, 'exec_future', future_policy, future_route,
            {'target': target_id}))
        assert pushed['exit_code'] == 0
        with pytest.raises(AuthorityDenied):
            s.executor.execute(request(s, 'exec_future', future_policy, future_route,
                operation='fetch_origin'))
    assert git(s.remote, 'rev-parse', 'refs/heads/codex/derived') == git(s.derived, 'rev-parse', 'HEAD')
    assert s.integration.get_effective_authority(task_ref=s.task.task_id)['derived_git_targets'][0]['task_target_authorized']
    with s.registry._connect() as conn:
        version = conn.execute('SELECT approved_scope_json FROM task_policy_versions WHERE task_id=? AND version=1',
                               (s.task.task_id,)).fetchone()
    assert target_id in version['approved_scope_json']
    assert s.creation_ref in target['provenance']


def test_public_mcp_registration_returns_discoverable_identity(scene):
    s = scene
    server = ClinxMCPServer(s.integration)
    names = [tool['name'] for tool in server.handle({'jsonrpc': '2.0', 'id': 1,
        'method': 'tools/list'})['result']['tools']]
    assert 'clinx_register_derived_git_target' in names
    registered = server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
        'params': {'name': 'clinx_register_derived_git_target', 'arguments': {
            'approved': True, 'task_ref': s.task.task_id, 'path': str(s.derived),
            'ownership_evidence': 'Reviewed exact task Host worktree creation receipt',
            'creation_host_execution_ref': s.creation_ref}}})['result']
    assert not registered['isError']
    target_id = registered['structuredContent']['target_id']
    authority = server.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
        'params': {'name': 'clinx_get_effective_authority',
                   'arguments': {'task_ref': s.task.task_id}}})['result']
    assert authority['structuredContent']['derived_git_targets'][0]['identity'] == target_id


def test_registration_requires_real_owned_worktree_and_exact_repo(scene):
    s = scene
    assert adopt(s)['target_id'].startswith('gitwt_')
    other = s.tmp / 'other'
    git(s.parent, 'worktree', 'add', '-b', 'codex/other', str(other))
    other_route = build_routing_identity(host='p620', surface='host_executor',
        provider='codex_app_server', transport='local_stdio', workspace_alias='p620',
        project_alias='other', project_identity='other', conversation_binding='thread-other',
        worktree_key=s.registry.worktree_key(host='p620', cwd=str(other), repository_origin=str(s.remote)),
        authority_scopes=s.policy.authority_scopes)
    other_task = s.registry.create_task(host='p620', workspace_alias='p620', project_alias='other',
        project_name='Other', cwd=str(other), repository_origin=str(s.remote),
        branch='codex/other', title='Other task', execution_policy=s.policy, routing_identity=other_route)
    with pytest.raises(TaskRegistryError, match='another task'):
        s.registry.register_derived_git_target(task_id=s.task.task_id, path=str(other),
            trusted_roots=s.config.trusted_workspace_roots, provenance='other task worktree')
    with pytest.raises(M9IntegrationError, match='INVALID_TARGET_SCOPE'):
        s.integration.prepare_policy_reauthorization(approved=True, task_ref=s.task.task_id,
            expected_policy_hash=s.registry.get_task_policy_identity(s.task.task_id)['policy_hash'],
            target_policy=dict(execution_surface='HOST_EXECUTOR', required_capabilities=['GIT'],
                operation_classes=['DEVELOPMENT_MUTATION'], operation_scopes=[push_scope(str(other))]),
            reason='invalid path scope')
    arbitrary = s.tmp / 'arbitrary'; arbitrary.mkdir()
    with pytest.raises(TaskRegistryError):
        s.registry.register_derived_git_target(task_id=s.task.task_id, path=str(arbitrary),
            trusted_roots=s.config.trusted_workspace_roots, provenance='wrong directory')
    link = s.tmp / 'linked'; link.symlink_to(other)
    with pytest.raises(TaskRegistryError, match='symlink'):
        s.registry.register_derived_git_target(task_id=s.task.task_id, path=str(link),
            trusted_roots=s.config.trusted_workspace_roots, provenance='symlink')
    alien = s.tmp / 'alien'; alien.mkdir(); git(alien, 'init', '-b', 'codex/alien')
    git(alien, 'remote', 'add', 'origin', str(s.remote))
    with pytest.raises(TaskRegistryError):
        s.registry.register_derived_git_target(task_id=s.task.task_id, path=str(alien),
            trusted_roots=s.config.trusted_workspace_roots, provenance='different repo')
    with pytest.raises(TaskRegistryError, match='another task'):
        s.registry.register_derived_git_target(task_id=other_task.task_id, path=str(s.derived),
            trusted_roots=s.config.trusted_workspace_roots, provenance='attempted cross task adoption')
    with pytest.raises(TaskRegistryError, match='creation receipt'):
        s.registry.register_derived_git_target(task_id=other_task.task_id, path=str(s.derived),
            trusted_roots=s.config.trusted_workspace_roots, provenance='wrong task receipt',
            creation_host_execution_ref=s.creation_ref)


@pytest.mark.parametrize('change', ['origin', 'pushurl', 'detach', 'main', 'master', 'protected', 'merge', 'branch'])
def test_identity_drift_fails_closed(scene, change):
    s = scene
    target = adopt(s)
    if change == 'origin':
        git(s.derived, 'remote', 'set-url', 'origin', str(s.tmp / 'wrong.git'))
    elif change == 'pushurl':
        git(s.derived, 'config', 'remote.origin.pushurl', str(s.tmp / 'wrong.git'))
    elif change == 'detach':
        git(s.derived, 'checkout', '--detach')
    elif change == 'main':
        git(s.derived, 'checkout', 'main')
    elif change == 'master':
        git(s.derived, 'checkout', '-b', 'master')
    elif change == 'protected':
        git(s.derived, 'config', '--add', 'clinx.protectedBranch', 'codex/derived')
    elif change == 'merge':
        Path(git(s.derived, 'rev-parse', '--git-path', 'MERGE_HEAD')).write_text(git(s.derived, 'rev-parse', 'HEAD') + '\n')
    else:
        git(s.derived, 'checkout', '-b', 'codex/different')
    with pytest.raises(TaskRegistryError, match='invalid|changed'):
        s.registry.resolve_derived_git_target(task_id=s.task.task_id, target_id=target['target_id'],
            trusted_roots=s.config.trusted_workspace_roots)


def test_invalid_arguments_revocation_and_cas(scene):
    s = scene
    target = adopt(s)
    for args in ({'target': target['target_id'], 'remote': 'evil'},
                 {'target': target['target_id'], 'refspec': 'codex/other:main'},
                 {'target': target['target_id'], 'force': 'true'},
                 {'target': target['target_id'], 'force-with-lease': 'true'}):
        with s.registry.execution(s.task.task_id, execution_ref='exec_args_' + str(len(args)) + str(hash(str(args)))):
            with pytest.raises((AuthorityDenied, TargetNotRegistered, InvalidArguments)):
                s.executor.execute(request(s, 'exec_args_' + str(len(args)) + str(hash(str(args))),
                    s.policy, s.route, args))
    with pytest.raises(TaskRegistryError, match='POLICY_IDENTITY_CONFLICT'):
        s.integration.prepare_policy_reauthorization(approved=True, task_ref=s.task.task_id,
            expected_policy_hash='0' * 64, target_policy=dict(execution_surface='HOST_EXECUTOR',
                required_capabilities=['GIT'], operation_classes=['DEVELOPMENT_MUTATION'],
                operation_scopes=[push_scope(target['target_id'])]), reason='stale CAS')
    s.registry.revoke_derived_git_target(task_id=s.task.task_id, target_id=target['target_id'])
    with pytest.raises(TaskRegistryError, match='not active'):
        s.registry.resolve_derived_git_target(task_id=s.task.task_id, target_id=target['target_id'],
            trusted_roots=s.config.trusted_workspace_roots)


def test_deleted_worktree_and_legacy_unscoped_policy_fail_closed(scene):
    s = scene
    target = adopt(s)
    legacy = build_execution_policy(execution_surface='HOST_EXECUTOR',
        required_capabilities=['GIT'], operation_classes=['DEVELOPMENT_MUTATION'])
    with pytest.raises(AuthorityDenied, match='explicit target scope'):
        s.executor._validate(request(s, 'exec_legacy', legacy, s.route,
            {'target': target['target_id']}))
    git(s.parent, 'worktree', 'remove', '--force', str(s.derived))
    with pytest.raises(TaskRegistryError, match='invalid'):
        s.registry.resolve_derived_git_target(task_id=s.task.task_id,
            target_id=target['target_id'], trusted_roots=s.config.trusted_workspace_roots)
