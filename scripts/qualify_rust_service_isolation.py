#!/usr/bin/env python3
"""Probe deployed service identities; never mutate the authority database."""
import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

from rust_authority_isolation import durable, process_identity, require_root


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--release', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    require_root()
    units = {}
    for name in ('authority', 'provider', 'dispatcher'):
        unit = 'clinx-rust-' + name + '.service'
        pid = int(subprocess.check_output(['systemctl', 'show', unit, '-p', 'MainPID', '--value']))
        if not pid:
            raise RuntimeError('DEPLOYED_SERVICE_NOT_RUNNING:' + unit)
        units[name] = process_identity(pid)
    import pwd
    worker = pwd.getpwnam(Path("/var/lib/clinx-rust/config/worker-user").read_text().strip())
    code = r'''
import errno,json,os,socket,sys
from pathlib import Path
writer_pid=sys.argv[1]
result={'uid':os.getuid(),'euid':os.geteuid(),'groups':os.getgroups(),'denied':{},'fd_closed':False}
try:os.fstat(int(sys.argv[2]))
except OSError as e:result['fd_closed']=e.errno==errno.EBADF
for path in ('/var/lib/clinx-rust/authority/authority.sqlite3','/proc/'+writer_pid+'/fd','/run/docker.sock','/run/clinx-rust/authority.sock','/run/clinx-rust-api/mcp.sock'):
 try:
  if path.endswith('.sock'):
   with socket.socket(socket.AF_UNIX) as s:s.connect(path)
  elif path.endswith('/fd'):list(Path(path).iterdir())
  else:
   f=os.open(path,os.O_RDWR);os.close(f)
  result['denied'][path]=False
 except OSError as e:result['denied'][path]=e.errno in (errno.EACCES,errno.ENOENT,errno.EPERM)
link=Path('/tmp')/('clinx-authority-alias-'+str(os.getpid()))
try:
 link.symlink_to('/var/lib/clinx-rust/authority/authority.sqlite3')
 try:
  fd=os.open(link,os.O_RDWR);os.close(fd);result['path_alias_denied']=False
 except OSError as e:result['path_alias_denied']=e.errno in (errno.EACCES,errno.ENOENT,errno.EPERM)
finally:link.unlink(missing_ok=True)
print(json.dumps(result))
'''
    # A harmless inherited FD verifies close_range, not credential-based revocation.
    fd = os.open('/dev/null', os.O_RDONLY)
    command = ['nsenter', '-t', str(units['dispatcher']['pid']), '-m', '--',
               'runuser', '-u', 'clinx-transport', '--', str(args.release/'host-worker'),
               '/usr/bin/python3', '-c', code, str(units['authority']['pid']), str(fd)]
    try:
        probe = subprocess.run(command, capture_output=True, text=True, check=True, timeout=20, pass_fds=(fd,))
    finally:
        os.close(fd)
    result = json.loads(probe.stdout)
    provider_probe = subprocess.run(['nsenter', '-t', str(units['provider']['pid']), '-m', '--',
        'runuser', '-u', worker.pw_name, '--', '/usr/bin/python3', '-c', code,
        str(units['authority']['pid']), '2147483647'], capture_output=True, text=True, check=True, timeout=20)
    provider_result = json.loads(provider_probe.stdout)
    passed = (result['uid'] == result['euid'] == worker.pw_uid
              and set(result['groups']) == set(os.getgrouplist(worker.pw_name, worker.pw_gid)) and all(result['denied'].values())
              and result['fd_closed'] and result['path_alias_denied']
              and provider_result['uid'] == provider_result['euid'] == worker.pw_uid
              and all(provider_result['denied'].values()) and provider_result['path_alias_denied'])
    receipt = dict(status='PASS' if passed else 'BLOCKED', scope='DEPLOYED_NEW_SERVICE_ISOLATION',
        legacy_worker_revocation='NOT_VERIFIED', units=units, probe=result, provider_probe=provider_result,
        release_manifest_sha256=hashlib.sha256((args.release/'manifest.json').read_bytes()).hexdigest())
    durable(args.output, receipt)
    print(json.dumps(receipt))
    if not passed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
