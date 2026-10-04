"""Explicit real-worker qualification on a disposable repo and isolated task DB.

Run with the release's Python. Uses the existing authenticated native Provider;
does not change its config, credentials, services, or any existing CLINX task.
"""
import argparse
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from mcp_server import build_server
from execution_owner import socket_path
from tool_delivery import ToolDeliveryLedger, ToolCallReplayRejected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    private = Path.home()/'.local/state/clinx/qualification/host-delivery-20261004'/str(time.time_ns())
    private.mkdir(parents=True, mode=0o700)
    project = Path.home()/'dev'/('clinx-host-owner-fixture-' + private.name)
    project.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'codex/owner-fixture', str(project)], check=True)
    subprocess.run(['git', '-C', str(project), '-c', 'user.name=CLINX fixture', '-c',
                    'user.email=fixture@example.invalid', 'commit', '-q', '--allow-empty', '-m', 'Fixture'], check=True)
    config = private/'bridge.toml'
    original = args.config.read_text()
    original = original.replace('task_db_path = "~/.local/state/clinx/tasks.sqlite3"',
                                'task_db_path = ' + json.dumps(str(private/'tasks.sqlite3')))
    config.write_text(original)
    config.chmod(0o600)
    env = os.environ.copy()
    env.pop('CLINX_ENABLE_NODE_ROUTER', None)
    commands = [sys.executable, str(SOURCE/'mcp_server.py'), '--config', str(config)]
    observer = build_server(config).integration
    assert observer.registry.path == private/'tasks.sqlite3'
    owner = subprocess.Popen(commands + ['--execution-owner'], env=env,
                             stdout=(private/'owner.stdout').open('w'), stderr=(private/'owner.stderr').open('w'))
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence = {'source': str(SOURCE), 'private_state': str(private), 'project': str(project),
                'owner_pid': owner.pid, 'executions': []}

    def save():
        args.evidence.write_text(json.dumps(evidence, indent=2))

    for _ in range(100):
        if socket_path(observer.registry).exists():
            break
        assert owner.poll() is None, 'owner startup failed; see private stderr'
        time.sleep(0.1)
    else:
        raise RuntimeError('owner socket not ready')
    for mutation in (False, True):
        process = subprocess.Popen(commands + ['--stdio', '--no-recover-existing'], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=(private/'requester.stderr').open('a'), text=True)
        def tool(name, arguments):
            process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': name, 'method': 'tools/call',
                                           'params': {'name': name, 'arguments': arguments}}) + '\n')
            process.stdin.flush()
            response = json.loads(process.stdout.readline())['result']
            assert not response.get('isError'), response
            return response['structuredContent']
        request = ({'capability': 'LOCAL_HOST_PROCESS', 'operation': 'development_command',
                    'operation_class': 'DEVELOPMENT_MUTATION', 'arguments': {'argv': ['python3', '-c',
                    "from pathlib import Path; p=Path('counter'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); print('COUNTER='+p.read_text())"]}}
                   if mutation else {'capability': 'GIT', 'operation': 'status',
                                     'operation_class': 'READ_ONLY_HOST', 'arguments': {}})
        prepared = tool('clinx_prepare_execution', dict(approved=True, task_action='create', host='p620',
            project=project.name, title='Persistent owner requester-exit ' + request['operation_class'],
            summary='Isolated Host delivery qualification', model='gpt-6-luna', reasoning_effort='low',
            execution_surface='HOST_EXECUTOR', required_capabilities=[request['capability']],
            operation_classes=[request['operation_class']], requested_operations=[{
                **{k: request[k] for k in ('capability', 'operation', 'operation_class')}, 'target': ''}],
            prompt='Call clinx.clinx_host_operation exactly once with '+json.dumps(request)+
            '. Await the result. No other commands/tools and no retry. Return CLINX_EXECUTION_RESULT with '
            'STATUS=PASS only on exit_code=0, SUMMARY=actual host_execution_ref and stdout, '
            'CHANGED_FILES='+('counter' if mutation else 'NONE')+', VALIDATION=real Host output, '
            'BLOCKERS=NONE, NEXT_STATE=COMPLETED. If no result or uncertain return BLOCKED.'))
        start = tool('clinx_start_execution', dict(approved=True, prepared_execution_ref=prepared['prepared_execution_ref']))
        assert start['execution_started']
        process.terminate()
        process.wait(5)
        item = {'start': start, 'requester_exit': process.returncode, 'requester_pid': process.pid,
                'operation': request, 'active': observer.get_status(execution_ref=start['execution_ref'])}
        evidence['executions'].append(item)
        save()
        print('REQUESTER_EXIT', start['execution_ref'], process.returncode, 'OWNER', owner.pid, flush=True)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            result = observer.registry.get_execution_result(start['execution_ref'])
            if result:
                break
            assert owner.poll() is None, 'owner died'
            time.sleep(1)
        status = observer.get_status(execution_ref=start['execution_ref'])
        item['terminal'] = status
        save()
        assert status['execution_state'] == 'COMPLETED', status['execution_state']
        rows = ToolDeliveryLedger.records(observer.registry, start['execution_ref'])
        assert status['dynamic_tool_deliveries'] == rows
        assert len(rows) == 1 and rows[0]['call_state'] == 'SUCCEEDED'
        assert rows[0]['delivery_state'] == 'DELIVERED' and rows[0]['host_execution_ref']
        assert json.loads(rows[0]['identity_json'])['client_pid'] == owner.pid
        # Exact duplicate start returns the stored dispatch without a second turn.
        replay = observer.start_execution(approved=True, prepared_execution_ref=prepared['prepared_execution_ref'])
        assert replay['execution_ref'] == start['execution_ref']
        # Replay the real call identity at the durable admission boundary.
        ledger = ToolDeliveryLedger(observer.registry, start['execution_ref'])
        identity = json.loads(rows[0]['identity_json'])
        params = {'callId': rows[0]['tool_call_id'], 'threadId': identity['thread_id'],
                  'turnId': identity['turn_id'], 'namespace': identity['namespace'],
                  'tool': identity['tool'], 'arguments': request}
        try:
            ledger.admit({'id': 'qualification-duplicate', 'params': params}, identity)
        except ToolCallReplayRejected:
            item['duplicate_call'] = 'REJECTED_WITHOUT_DISPATCH'
        else:
            raise AssertionError('duplicate call admitted')
        if mutation:
            assert (project/'counter').read_text() == '1'
            item['counter'] = 1
        save()
        print('PASS', request['operation_class'], start['execution_ref'], flush=True)
    owner.terminate()
    owner.wait(10)
    assert owner.returncode == 0
    evidence['owner_drained_exit'] = owner.returncode
    evidence['result'] = 'PASS'
    save()


if __name__ == '__main__':
    main()
