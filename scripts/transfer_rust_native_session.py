#!/usr/bin/env python3
"""Maintenance transfer of one explicitly selected, terminal native session.

No CLINX task/Lease/database migration. Uses the existing Provider and Rust
adoption transaction. Source archive, exact inode holders, copied rollout identity
and exact destination turn are recorded; this is not old-Worker OS revocation.
"""
import argparse
import hashlib
import json
import os
import pwd
import subprocess
import time
from pathlib import Path

from rust_authority_isolation import writable_holders


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-receipt', type=Path, required=True)
    parser.add_argument('--source-endpoint', required=True)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or args.output.exists():
        raise RuntimeError('ROOT_AND_UNIQUE_RECEIPT_REQUIRED')
    manifest = json.loads((args.release / 'manifest.json').read_text())
    if manifest.get('source_layout') != 'qualified-clinx-v1':
        raise RuntimeError('QUALIFIED_PACKAGE_LAYOUT_REQUIRED')
    original = json.loads(args.source_receipt.read_text())['status']
    tid, turn_id = original['thread_id'], original['native_turn_id']
    if original['status'] != 'SUCCEEDED' or original['resource_state'] != 'RELEASED':
        raise RuntimeError('EXACT_TERMINAL_SOURCE_RECEIPT_REQUIRED')
    # Only use a bounded exact native id as a filename, never a source path.
    import re
    if not re.fullmatch(r'[0-9a-f-]{36}', tid):
        raise RuntimeError('INVALID_THREAD_ID')
    record = dict(status='STARTED', scope='EXACT_NATIVE_SESSION_TRANSFER', thread_id=tid,
                  exact_turn_id=turn_id, old_worker_revocation='NOT_VERIFIED',
                  source_receipt_sha256=hashlib.sha256(args.source_receipt.read_bytes()).hexdigest())

    def save():
        temporary = args.output.with_suffix('.tmp')
        with temporary.open('w') as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, args.output)
        fd = os.open(args.output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    env = dict(os.environ, PYTHONPATH=str(args.release/'control')+':'+str(args.release/'deps'),
               PYTHONDONTWRITEBYTECODE='1', CLINX_AUTHORITY='rust',
               CLINX_RUST_SOCKET='/run/clinx-rust/authority.sock', CLINX_RUST_UID='993')

    def python(user, code, *argv, namespace=False):
        command = ['runuser', '-u', user, '--', '/usr/bin/python3', '-c', code, *argv]
        if namespace:
            pid = subprocess.check_output(['systemctl', 'show', 'clinx-rust-dispatcher.service',
                                           '-p', 'MainPID', '--value'], text=True).strip()
            command = ['nsenter', '-t', pid, '-m', '--', *command]
        return json.loads(subprocess.check_output(command, env=env, text=True, timeout=45))

    source_code = '''import json,sys
from clinx.provider.native_provider import existing_client
from clinx.provider.native_provider import exact_turn
with existing_client(sys.argv[1],20,read_only=False) as c:
 c.initialize(client_name='clinx-exact-native-transfer',client_title='Clinx',client_version='1')
 t=c.thread_read(sys.argv[2]);turn=exact_turn(c,sys.argv[2],sys.argv[3])
 assert t['id']==sys.argv[2] and t['status']['type']=='idle' and turn['status']=='completed'
 result=dict(thread=t,turn=turn)
 if sys.argv[4]=='archive':
  result['archive_ack']=c._request('thread/archive',dict(threadId=sys.argv[2]))
 print(json.dumps(result))
'''
    try:
        source = python('pvxlabs', source_code, args.source_endpoint, tid, turn_id, 'read')
        path = Path(source['thread']['path'])
        if path.is_symlink() or not path.is_relative_to(Path('/home/pvxlabs/.codex/sessions')):
            raise RuntimeError('SOURCE_ROLLOUT_PATH_CONFLICT')
        before = path.stat()
        record.update(source=source, source_inode=dict(device=before.st_dev, inode=before.st_ino),
                      source_endpoint=args.source_endpoint)
        save()
        archived = python('pvxlabs', source_code, args.source_endpoint, tid, turn_id, 'archive')
        if archived['thread']['path'] != str(path) or archived['turn'] != source['turn']:
            raise RuntimeError('SOURCE_CHANGED_DURING_ARCHIVE')
        record['archive_ack'] = archived['archive_ack']
        save()
        archived_path = Path('/home/pvxlabs/.codex/archived_sessions')/path.name
        current = archived_path.stat()
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError('ARCHIVED_INODE_CONFLICT')
        deadline = time.monotonic()+10
        while writable_holders([archived_path]):
            if time.monotonic() >= deadline:
                raise RuntimeError('SOURCE_WRITABLE_FD_STILL_OPEN')
            time.sleep(.1)
        data = archived_path.read_bytes()
        metadata = json.loads(data.splitlines()[0])
        if metadata['type'] != 'session_meta' or metadata['payload']['id'] != tid:
            raise RuntimeError('ROLLOUT_THREAD_ID_CONFLICT')
        if metadata['payload']['cwd'] != source['thread']['cwd']:
            raise RuntimeError('ROLLOUT_WORKSPACE_CONFLICT')
        imports = Path('/var/lib/clinx-rust/worker/codex160/sessions') / path.relative_to('/home/pvxlabs/.codex/sessions').parent
        worker = pwd.getpwnam(Path('/var/lib/clinx-rust/config/worker-user').read_text().strip())
        imports.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(imports, worker.pw_uid, worker.pw_gid)
        target = imports/path.name
        with target.open('xb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        target.chmod(0o600)
        os.chown(target, worker.pw_uid, worker.pw_gid)
        if archived_path.read_bytes() != data or writable_holders([archived_path]):
            raise RuntimeError('SOURCE_CHANGED_DURING_COPY')
        record.update(archived_path=str(archived_path), source_writable_holders=[],
                      source_sha256=hashlib.sha256(data).hexdigest(), target_path=str(target))
        save()
        destination_code = '''import json,sys
from clinx.provider.native_provider import existing_client
from clinx.provider.native_provider import exact_turn
with existing_client('/run/clinx-rust-provider/provider.sock',20,read_only=False) as c:
 c.initialize(client_name='clinx-exact-native-import',client_title='Clinx',client_version='1')
 r=c._request('thread/resume',dict(threadId=sys.argv[1],excludeTurns=True))
 t=r['thread']
 assert t['id']==sys.argv[1] and t['cwd']==sys.argv[4]
 turn=exact_turn(c,sys.argv[1],sys.argv[2])
 print(json.dumps(dict(thread=t,turn=turn)))
'''
        destination = python('clinx-transport', destination_code, tid, turn_id, str(target),
                             source['thread']['cwd'], namespace=True)
        if destination['turn'] != source['turn'] or destination['thread']['status']['type'] != 'idle':
            raise RuntimeError('DESTINATION_EXACT_TURN_CONFLICT')
        record['destination'] = destination
        cwd = source['thread']['cwd']
        seal = dict(cwd=cwd, branch=subprocess.check_output(['git','-c','safe.directory='+cwd,'-C',cwd,'branch','--show-current'],text=True).strip(),
                    head=subprocess.check_output(['git','-c','safe.directory='+cwd,'-C',cwd,'rev-parse','HEAD'],text=True).strip())
        adoption_code = '''import json,sys
from pathlib import Path
from clinx.contracts.runtime_config import BridgeConfig
from clinx.authority.rust_integration import RustIntegration
api=RustIntegration(BridgeConfig.load(Path('/var/lib/clinx-rust/config/bridge.toml')))
print(json.dumps(api.adopt_conversation(host='p620',project='clinx',thread_id=sys.argv[1],exact_turn_id=sys.argv[2],workspace_contract=json.loads(sys.argv[3]),title='Transferred exact native acceptance session')))
'''
        record['adoption'] = python('clinx-api', adoption_code, tid, turn_id, json.dumps(seal))
        record['status'] = 'TRANSFERRED_PENDING_RUST_ADOPTION'
        save()
        print(json.dumps({k:record[k] for k in ('status','thread_id','exact_turn_id','adoption')}))
    except Exception as exc:
        record.update(status='BLOCKED', error=str(exc))
        save()
        raise


if __name__ == '__main__':
    main()
