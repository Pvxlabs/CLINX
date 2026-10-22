"""Read-only writer capability inventory; never revokes or activates anything.

The existing same-UID deployment cannot securely revoke old writable DB/WAL/SHM
handles online. A zero-handle scan alone is NOT an activation certificate.
"""

import os
from pathlib import Path


def process_identity(pid):
    # comm can contain spaces or parentheses; fields after final ')' start at 3.
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return dict(pid=pid, start_ticks=int(fields[19]), state=fields[0])


def writable_handles(path, *, pids=None):
    """Inspect selected process identities and matching same-inode handles.

    Includes DB, WAL and SHM, deleted-name handles and hardlinks by dev/inode.
    Namespace-hidden / inaccessible processes are explicit completeness limits.
    This diagnostic cannot discover an FD transferred after the snapshot.
    """
    path = Path(path).resolve()
    targets = {str(path), str(path) + "-wal", str(path) + "-shm"}
    inodes = {}
    for name in targets:
        try:
            s = os.stat(name)
            inodes[(s.st_dev, s.st_ino)] = name
        except FileNotFoundError:
            pass
    if pids is None:
        pids = sorted(int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit())
    handles = []
    unreadable = []
    identities = []
    for pid in pids:
        try:
            identity = process_identity(pid)
            identities.append(identity)
            for fdpath in Path(f"/proc/{pid}/fd").iterdir():
                try:
                    s = fdpath.stat()
                    name = inodes.get((s.st_dev, s.st_ino))
                    link = os.readlink(fdpath)
                    if name is None and link.removesuffix(" (deleted)") not in targets:
                        continue
                    info = Path(f"/proc/{pid}/fdinfo/{fdpath.name}").read_text()
                    flags = int(
                        next(
                            line.split()[1]
                            for line in info.splitlines()
                            if line.startswith("flags:")
                        ),
                        8,
                    )
                    if flags & os.O_ACCMODE in (os.O_WRONLY, os.O_RDWR):
                        handles.append(
                            dict(
                                **identity,
                                fd=int(fdpath.name),
                                target=name or link,
                                device=s.st_dev,
                                inode=s.st_ino,
                                flags=flags,
                            )
                        )
                except FileNotFoundError:
                    continue
        except (PermissionError, ProcessLookupError) as exc:
            unreadable.append(dict(pid=pid, error=type(exc).__name__))
        except FileNotFoundError:
            continue
    return dict(
        handles=handles,
        unreadable=unreadable,
        processes=identities,
        completeness="selected visible processes only; not proof of global handle revocation",
    )


def handoff_preflight(path, *, old_processes, writer_uid, worker_uid, pids=None):
    """Fail closed for same UID or any surviving old identity, even SIGSTOP.

    This returns diagnostics only. Kernel isolation, privilege drop and existing
    writer-process IPC must be established in deployment before activation.
    """
    scan = writable_handles(path, pids=pids)
    surviving = []
    for old in old_processes:
        try:
            current = process_identity(old["pid"])
            if current["start_ticks"] == old["start_ticks"]:
                surviving.append(current)
        except (FileNotFoundError, ProcessLookupError):
            pass
    reasons = []
    if writer_uid == worker_uid:
        reasons.append("SAME_UID_WORKER_CAN_REOPEN_DATABASE")
    if surviving:
        reasons.append("OLD_WRITER_PROCESS_STILL_EXISTS")
    if scan["handles"]:
        reasons.append("WRITABLE_DB_WAL_SHM_HANDLES_REMAIN")
    if scan["unreadable"]:
        reasons.append("HANDLE_INVENTORY_INCOMPLETE")
    # No code path issues authorization from caller-supplied UIDs or a scan.
    return dict(
        status="BLOCKED" if reasons else "NOT_VERIFIED",
        reasons=reasons,
        surviving_old_processes=surviving,
        scan=scan,
        activation="NOT_PERFORMED",
        required="stop/reap old holders; isolate writer OS principal; worker IPC only",
    )


class WorkerAuthorityClient:
    """Worker carries a transport callback, no SQLite path/connection/raw FD.

    Supply the existing command-plane transport after deployment isolation.
    This class does not create a new service or claim OS isolation by itself.
    """

    def __init__(self, request):
        self.request = request

    @classmethod
    def for_owner(cls, database, *, timeout=90):
        """Use the existing Owner socket without constructing a TaskRegistry.

        The database string is routing identity only; this worker never opens it.
        No local fallback or automatic retry on uncertain transport outcomes.
        """
        from types import SimpleNamespace

        from execution_owner import ExecutionOwnerClient

        owner = ExecutionOwnerClient(SimpleNamespace(path=Path(database)), timeout=timeout)

        def request(method, payload):
            if method == 'deliver':
                return owner.deliver(payload)
            if method in ('commit', 'read_commit'):
                return getattr(owner, method)(**payload)
            raise ValueError('UNKNOWN_AUTHORITY_METHOD')

        return cls(request)

    def deliver(self, event):
        return self.request("deliver", event)

    def commit(self, delivery_id, resource_id, epoch, operation_id):
        return self.request(
            "commit",
            dict(
                delivery_id=delivery_id,
                resource_id=resource_id,
                epoch=epoch,
                operation_id=operation_id,
            ),
        )

    def read_commit(self, resource_id, epoch, operation_id):
        return self.request(
            "read_commit", dict(resource_id=resource_id, epoch=epoch, operation_id=operation_id)
        )


def dispatch(authority, method, payload):
    """Run in the existing authority writer process; no arbitrary SQL method."""
    if method == "deliver":
        return authority.deliver(payload)
    if method in ("commit", "read_commit"):
        return getattr(authority, method)(**payload)
    raise ValueError("UNKNOWN_AUTHORITY_METHOD")
