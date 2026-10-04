"""Trusted workspace paths use the same config/contract as the runtime."""
import dataclasses
import json
from pathlib import Path
import subprocess

import pytest

import bridge
from execution_policy import DEVELOPMENT_MUTATION, READ_ONLY_HOST
from host_contract import executable_contract
from host_executor import AuthorityDenied, RegisteredTarget, TargetNotRegistered
import test_m13b as support


@pytest.fixture
def workspace(tmp_path):
    host = support.HostExecutorFixture()
    host.setUp()
    dev, artifacts = tmp_path / 'dev', tmp_path / 'artifacts'
    dev.mkdir()
    artifacts.mkdir()
    host.root = dev / 'clinx-pvx1807-remediation'
    host.root.mkdir()
    runtime = dev / 'clinx-thread-runtime-20261001'
    runtime.mkdir()
    contract = dev / 'clinx-host-contract-v2-20261001'
    contract.mkdir()
    for directory in (runtime, contract):
        (directory / 'file').write_text('verified\n')
        subprocess.run(['git', 'init', '-q', str(directory)], check=True)
    (artifacts / 'qualification.json').write_text('{"status":"PASS"}\n')
    host.executor.config = dataclasses.replace(host.config,
        trusted_workspace_roots=(dev, artifacts), max_output_bytes=65536)
    task, ref, route, policy, lease = host.bound(
        capabilities=('HOST_FILESYSTEM', 'LOCAL_HOST_PROCESS', 'GIT', 'SSH'),
        classes=(READ_ONLY_HOST, DEVELOPMENT_MUTATION))

    def run(operation, arguments, *, capability='HOST_FILESYSTEM', mutation=False):
        return host.executor.execute(host.request(task, ref, route, policy, capability,
            operation, arguments, operation_class=DEVELOPMENT_MUTATION if mutation else READ_ONLY_HOST))

    yield host, dev, artifacts, runtime, contract, run
    lease.__exit__(None, None, None)
    host.tearDown()


def test_cross_worktree_absolute_relative_parent_and_artifact_reads(workspace):
    host, dev, artifacts, runtime, contract, run = workspace
    (host.root / 'file').write_text('local\n')
    for path in [runtime / 'file', contract / 'file', artifacts / 'qualification.json',
                 'file', '../clinx-thread-runtime-20261001/file',
                 '../clinx-thread-runtime-20261001/../clinx-host-contract-v2-20261001/file']:
        assert run('path_read', {'path': str(path)})['exit_code'] == 0
    (host.root / 'inside').symlink_to(runtime, target_is_directory=True)
    assert run('path_read', {'path': 'inside/file'})['stdout'] == 'verified\n'


def test_cross_worktree_development_git_python_and_diff(workspace):
    host, dev, artifacts, runtime, contract, run = workspace
    (runtime / 'script.py').write_text("print('qualified')\n")
    commands = [
        ['git', '-C', str(runtime), 'status', '--short'],
        ['git', '-C', '../clinx-thread-runtime-20261001', 'status', '--short'],
        ['python3', str(runtime / 'script.py')],
        ['diff', str(runtime / 'file'), str(contract / 'file')],
        ['cat', str(artifacts / 'qualification.json')],
    ]
    for argv in commands:
        result = run('development_command', {'argv': argv},
                     capability='LOCAL_HOST_PROCESS', mutation=True)
        assert result['exit_code'] == 0, (argv, result['stderr'])


@pytest.mark.parametrize('path', ['/etc/passwd', '/root/file', '/var/lib/file',
                                  '~/.ssh/id_ed25519', '~/.aws/credentials'])
def test_system_and_credential_paths_denied_for_both_surfaces(workspace, path):
    *_, run = workspace
    with pytest.raises(TargetNotRegistered):
        run('path_read', {'path': path})
    with pytest.raises(TargetNotRegistered):
        run('development_command', {'argv': ['cat', path]}, mutation=True)


def test_symlink_escape_relative_traversal_and_option_paths_denied(workspace, tmp_path):
    host, dev, artifacts, runtime, contract, run = workspace
    outside = tmp_path / 'outside'
    outside.write_text('must remain outside')
    (host.root / 'escape').symlink_to(outside)
    for path in ['escape', '../../outside', str(host.root / 'escape')]:
        with pytest.raises(TargetNotRegistered):
            run('path_read', {'path': path})
        with pytest.raises(TargetNotRegistered):
            run('development_command', {'argv': ['cat', path]}, mutation=True)
    with pytest.raises(TargetNotRegistered):
        run('development_command', {'argv': ['diff', '--from-file=/etc/passwd', 'file']}, mutation=True)
    (host.root / 'escape-dir').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(TargetNotRegistered):
        run('development_command', {'argv': ['git', '-C', 'escape-dir', 'status']}, mutation=True)
    assert run('path_read', {'path': str(runtime / 'file')})['exit_code'] == 0


@pytest.mark.parametrize('argv', [['sudo', 'true'], ['su'], ['ssh', 'orion-core'],
    ['aws', 'configure'], ['git', 'push'], ['git', 'fetch'],
    ['git', '-c', 'core.hooksPath=hooks', 'status'], ['bash', '-c', 'true']])
def test_paths_do_not_grant_privilege_production_or_network_authority(workspace, argv):
    *_, run = workspace
    with pytest.raises(AuthorityDenied):
        run('development_command', {'argv': argv}, mutation=True)
    with pytest.raises(AuthorityDenied):
        run('remote_true', {'target': 'orion-core'}, capability='SSH')


def test_marker_namespace_remains_project_bound(workspace):
    host, dev, artifacts, runtime, contract, run = workspace
    name = '.clinx-host-executor-escape'
    (host.root / name).symlink_to(runtime / 'marker')
    with pytest.raises(TargetNotRegistered):
        run('marker_create', {'name': name}, mutation=True)


def test_trusted_path_does_not_allow_registered_production_entrypoint(workspace):
    host, dev, artifacts, runtime, contract, run = workspace
    script = runtime / 'production-deploy'
    script.write_text('#!/bin/sh\nexit 0\n')
    script.chmod(0o755)
    target = RegisteredTarget('production-deploy', ('PRODUCTION_MUTATION',),
                              commands=(('production-deploy', (str(script),)),))
    host.executor.config = dataclasses.replace(host.executor.config, local_commands=(target,))
    for argv in [[str(script)], ['sh', str(script)]]:
        with pytest.raises(AuthorityDenied, match='production command'):
            run('development_command', {'argv': argv}, mutation=True)
    assert run('path_read', {'path': str(script)})['exit_code'] == 0


def test_long_literal_is_not_misclassified_as_filesystem_access(workspace):
    *_, run = workspace
    assert run('development_command', {'argv': ['python3', '-c', 'print(' + repr('x' * 400) + ')']},
               mutation=True)['exit_code'] == 0


def test_runtime_config_discovery_and_dynamic_contract_have_one_root_source():
    cfg = bridge.BridgeConfig.load(Path(__file__).with_name('bridge.toml'))
    contract = executable_contract(cfg.host_executor)
    expected = [str(root) for root in cfg.host_executor.trusted_workspace_roots]
    assert expected == ['/home/pvxlabs/dev', '/data/artifacts']
    assert contract['trusted_workspace_roots'] == expected
    assert contract['path_authority']['production_authority_granted'] is False
    assert contract['capabilities']['HOST_FILESYSTEM']['operations']['path_read']['target_kind'] == 'TRUSTED_WORKSPACE'
    assert contract['capabilities']['GIT']['operations']['push_current_branch']['target_kind'] == 'TASK_OWNED_GIT_WORKTREE'
    spec = support.HostExecutor.dynamic_tool_spec(cfg.host_executor)
    assert json.dumps(expected) in spec['tools'][0]['description']


@pytest.mark.parametrize('roots', ['["/"]', '["relative"]', '[false]', '"/tmp"'])
def test_config_rejects_unbounded_or_malformed_roots(tmp_path, roots):
    source = Path(__file__).with_name('bridge.toml').read_text()
    source = source.replace('trusted_workspace_roots = ["/home/pvxlabs/dev", "/data/artifacts"]',
                            'trusted_workspace_roots = ' + roots)
    config = tmp_path / 'bridge.toml'
    config.write_text(source)
    with pytest.raises(bridge.BridgeError, match='trusted_workspace_roots'):
        bridge.BridgeConfig.load(config)
