"""One resource/epoch authority; runtime allocations are canonical projections.

Explicit, additive and default-off. Historical resources are inventory only.
This module is a SQL consistency boundary, not OS writable-handle revocation.
"""

import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar

from authority_transactions import borrowed
from canonical_authority import CanonicalAuthority
from runtime_control import RuntimeControlStore
from runtime_control.clock import encode_time
from task_registry import TaskRegistryError

_enrolling = ContextVar("shared_allocation_enrollment", default=None)


def allocation_epoch(conn, path, resource, attempt, worker, incarnation):
    """Called by the EXISTING allocator, inside its existing transaction."""
    row = conn.execute(
        "SELECT * FROM authority_resources WHERE resource_id=?", (resource,)
    ).fetchone()
    if row is None:
        return None
    ctx = _enrolling.get()
    if not (ctx == (resource, attempt, worker, incarnation) and borrowed(path) is not None):
        from runtime_control.errors import RuntimeConflict

        raise RuntimeConflict("CANONICAL_RESOURCE_REQUIRES_SHARED_AUTHORITY")
    return row["epoch"]


def check_shared_owner(conn, resource, epoch, assignment_id, now):
    from runtime_control.clock import decode_time
    from runtime_control.errors import LeaseExpired, StaleMutation

    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='authority_mapping_schema'"
    ).fetchone():
        return
    row = conn.execute(
        "SELECT * FROM authority_current_mapping WHERE resource_id=?", (resource,)
    ).fetchone()
    if row is None:
        return
    if row["state"] != "ACTIVE" or row["epoch"] != epoch or row["assignment_id"] != assignment_id:
        raise StaleMutation("canonical allocation is no longer current")
    if decode_time(now).timestamp() >= row["expires_at"]:
        raise LeaseExpired("canonical lease is expired")


class EnrollmentStore(RuntimeControlStore):
    @contextmanager
    def _transaction(self, *, safety_key=None):
        joined = borrowed(self.path)
        if joined is None or _enrolling.get() is None:
            with super()._transaction(safety_key=safety_key) as transaction:
                yield transaction
            return
        if safety_key is not None:
            raise TaskRegistryError("ENROLLMENT_IS_NOT_OWNER_MUTATION")
        self._require_schema(joined)
        now = self._coordinator_now(joined, encode_time(self.clock.now()))
        yield joined, now, None
        # Outer canonical transaction owns commit, receipt and crash boundaries.


def migrate_mapping(path, *, fault=None):
    """Requires explicit v1 + runtime migrations. All v2 DDL is one transaction."""
    from task_registry import TaskRegistry

    with TaskRegistry(path, initialize=False)._connect() as c:
        c.execute("BEGIN IMMEDIATE")
        for name in ("authority_schema", "runtime_allocations", "runtime_assignments"):
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone():
                raise TaskRegistryError("SHARED_SCHEMA_REQUIRED:" + name)
        c.execute("""CREATE TABLE IF NOT EXISTS authority_mapping_schema(
            version INTEGER PRIMARY KEY CHECK(version=1))""")
        versions = [r[0] for r in c.execute("SELECT version FROM authority_mapping_schema")]
        if versions not in ([], [1]):
            raise TaskRegistryError("UNSUPPORTED_MAPPING_SCHEMA")
        c.execute("""CREATE TABLE IF NOT EXISTS authority_allocation_links(
            execution_ref TEXT PRIMARY KEY, resource_id TEXT NOT NULL,
            assignment_id TEXT NOT NULL UNIQUE, allocation_id TEXT NOT NULL UNIQUE,
            attempt_id TEXT NOT NULL UNIQUE, worker_id TEXT NOT NULL, incarnation_id TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS authority_mapping_events(
            resource_id TEXT NOT NULL, epoch INTEGER NOT NULL, state TEXT NOT NULL,
            execution_ref TEXT NOT NULL, assignment_id TEXT NOT NULL,
            PRIMARY KEY(resource_id,epoch,state))""")
        # A view, never a second independently incrementable epoch field.
        c.execute("""CREATE VIEW IF NOT EXISTS authority_current_mapping AS
            SELECT r.resource_id,r.epoch,r.state,r.task_id,r.execution_ref,
                   l.assignment_id,l.allocation_id,l.attempt_id,l.worker_id,l.incarnation_id,r.expires_at
            FROM authority_resources r JOIN authority_allocation_links l
            ON l.execution_ref=r.execution_ref AND l.resource_id=r.resource_id""")
        for table in ("authority_allocation_links", "authority_mapping_events"):
            for op in ("UPDATE", "DELETE"):
                c.execute(f"""CREATE TRIGGER IF NOT EXISTS immutable_{table}_{op.lower()}
                    BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'IMMUTABLE_MAPPING_FACT'); END""")
        c.execute("""CREATE TRIGGER IF NOT EXISTS authority_link_validate BEFORE INSERT ON authority_allocation_links
            BEGIN SELECT CASE WHEN NOT EXISTS(
                SELECT 1 FROM authority_resources r
                JOIN runtime_assignments a ON a.assignment_id=NEW.assignment_id
                JOIN runtime_allocations al ON al.allocation_id=NEW.allocation_id
                JOIN runtime_attempts at ON at.attempt_id=NEW.attempt_id
                JOIN runtime_executions e ON e.execution_id=at.execution_id
                WHERE r.resource_id=NEW.resource_id AND r.execution_ref=NEW.execution_ref
                AND r.resource_id=clinx_fence_resource() AND r.epoch=clinx_fence_epoch()
                AND a.resource_key=r.resource_id AND al.resource_key=r.resource_id
                AND a.resource_epoch=r.epoch AND al.resource_epoch=r.epoch
                AND a.attempt_id=NEW.attempt_id AND al.attempt_id=NEW.attempt_id
                AND al.assignment_id=a.assignment_id
                AND a.worker_id=NEW.worker_id AND al.worker_id=NEW.worker_id
                AND a.incarnation_id=NEW.incarnation_id AND al.incarnation_id=NEW.incarnation_id
                AND e.execution_id=r.execution_ref AND e.task_id=r.task_id
                AND r.state='ACTIVE' AND a.lifecycle='ACTIVE' AND al.lifecycle='ACTIVE'
            ) THEN RAISE(ABORT,'RESOURCE_MAPPING_CONFLICT') END; END""")
        # Replace only the v1 runtime guards, preserving all historical rows.
        for table, epoch_col in (
            ("runtime_assignments", "resource_epoch"),
            ("runtime_allocations", "resource_epoch"),
            ("runtime_protected_resources", "fencing_epoch"),
        ):
            for op in ("INSERT", "UPDATE", "DELETE"):
                name = f"authority_guard_{table}_{op.lower()}"
                c.execute(f"DROP TRIGGER IF EXISTS {name}")
                row = "OLD" if op == "DELETE" else "NEW"
                # Existing unlinked resources stay rejected. Only enrollment or
                # a projection that matches the canonical tuple may write.
                identity = ""
                if table != "runtime_protected_resources":
                    identity = f"""AND {row}.assignment_id=l.assignment_id
                        AND {row}.attempt_id=l.attempt_id AND {row}.worker_id=l.worker_id
                        AND {row}.incarnation_id=l.incarnation_id
                        AND {row}.lifecycle=CASE WHEN r.state='ACTIVE' THEN 'ACTIVE' ELSE 'RELEASED' END"""
                    expiry_column = (
                        "lease_expires_at" if table == "runtime_assignments" else "expires_at"
                    )
                    identity += f" AND julianday({row}.{expiry_column}) <= julianday(r.expires_at,'unixepoch')"
                allow = f"""EXISTS(SELECT 1 FROM authority_current_mapping r
                    JOIN authority_allocation_links l ON l.execution_ref=r.execution_ref
                    WHERE r.resource_id={row}.resource_key AND r.epoch={row}.{epoch_col} {identity})"""
                # Canonical changes update projections atomically under its tuple.
                enrolled = f"""EXISTS(SELECT 1 FROM authority_resources r
                    WHERE r.resource_id={row}.resource_key AND r.epoch={row}.{epoch_col}
                    AND r.resource_id=clinx_fence_resource() AND r.epoch=clinx_fence_epoch()
                    AND r.execution_ref=clinx_fence_execution())"""
                if op == "DELETE":
                    allow = enrolled = "0"  # retain facts, including released rows
                old_check = ""
                if op == "UPDATE":
                    old_check = """SELECT CASE WHEN OLD.resource_key IS NOT NEW.resource_key
                        THEN RAISE(ABORT,'RESOURCE_MAPPING_IDENTITY_IMMUTABLE') END;"""
                    if table != "runtime_protected_resources":
                        old_check += """SELECT CASE WHEN OLD.assignment_id IS NOT NEW.assignment_id
                            OR OLD.attempt_id IS NOT NEW.attempt_id OR OLD.worker_id IS NOT NEW.worker_id
                            OR OLD.incarnation_id IS NOT NEW.incarnation_id
                            THEN RAISE(ABORT,'RESOURCE_MAPPING_IDENTITY_IMMUTABLE') END;"""
                c.execute(f"""CREATE TRIGGER {name} BEFORE {op} ON {table} BEGIN
                    {old_check}
                    SELECT CASE WHEN EXISTS(SELECT 1 FROM authority_resources WHERE resource_id={row}.resource_key)
                    AND NOT ({allow} OR {enrolled})
                    THEN RAISE(ABORT,'CANONICAL_RESOURCE_REQUIRES_SHARED_AUTHORITY') END;
                    END""")
        # Old side of an UPDATE must not be retargeted to an unrelated resource.
        # SQL counters remain canonical-token fenced by the original migration.
        c.execute('DROP TRIGGER IF EXISTS authority_project_allocation')
        c.execute("""CREATE TRIGGER authority_project_allocation
            AFTER UPDATE OF epoch,state,expires_at ON authority_resources
            WHEN EXISTS(SELECT 1 FROM authority_allocation_links WHERE execution_ref=NEW.execution_ref)
            BEGIN
                UPDATE runtime_assignments SET resource_epoch=NEW.epoch,
                    lifecycle=CASE WHEN NEW.state='ACTIVE' THEN 'ACTIVE' ELSE 'RELEASED' END,
                    lease_expires_at=strftime('%Y-%m-%dT%H:%M:%fZ',NEW.expires_at,'unixepoch'),version=version+1
                    WHERE assignment_id=(SELECT assignment_id FROM authority_allocation_links WHERE execution_ref=NEW.execution_ref)
                    AND (resource_epoch!=NEW.epoch OR lifecycle!=CASE WHEN NEW.state='ACTIVE' THEN 'ACTIVE' ELSE 'RELEASED' END
                        OR lease_expires_at!=strftime('%Y-%m-%dT%H:%M:%fZ',NEW.expires_at,'unixepoch'));
                UPDATE runtime_allocations SET resource_epoch=NEW.epoch,
                    lifecycle=CASE WHEN NEW.state='ACTIVE' THEN 'ACTIVE' ELSE 'RELEASED' END,
                    expires_at=strftime('%Y-%m-%dT%H:%M:%fZ',NEW.expires_at,'unixepoch'),version=version+1
                    WHERE allocation_id=(SELECT allocation_id FROM authority_allocation_links WHERE execution_ref=NEW.execution_ref)
                    AND (resource_epoch!=NEW.epoch OR lifecycle!=CASE WHEN NEW.state='ACTIVE' THEN 'ACTIVE' ELSE 'RELEASED' END
                        OR expires_at!=strftime('%Y-%m-%dT%H:%M:%fZ',NEW.expires_at,'unixepoch'));
                UPDATE runtime_protected_resources SET fencing_epoch=NEW.epoch
                    WHERE resource_key=NEW.resource_id AND fencing_epoch!=NEW.epoch;
                UPDATE runtime_attempts SET lifecycle='RELEASED',version=version+1
                    WHERE attempt_id=(SELECT attempt_id FROM authority_allocation_links WHERE execution_ref=NEW.execution_ref)
                    AND NEW.state='RELEASED' AND lifecycle!='RELEASED';
                INSERT OR IGNORE INTO authority_mapping_events
                    SELECT NEW.resource_id,NEW.epoch,NEW.state,NEW.execution_ref,assignment_id
                    FROM authority_allocation_links WHERE execution_ref=NEW.execution_ref;
            END""")
        if fault:
            fault("mapping_before_commit")
        c.execute("INSERT OR IGNORE INTO authority_mapping_schema VALUES(1)")
    if fault:
        fault("mapping_after_commit")


class SharedAuthority(CanonicalAuthority):
    """Same writer and same canonical commit; no second owner/grant API."""

    shared_mapping_entry = True

    def __init__(self, path, *, runtime_store=None, **kwargs):
        super().__init__(path, **kwargs)
        self.runtime_store = runtime_store or EnrollmentStore(path)
        if self.runtime_store.path.resolve() != self.path.resolve():
            raise ValueError("mapping requires the same database")

    def _claim(self, conn, request, resource, epoch, now):
        if request["operation"] == "advance_epoch":
            linked = conn.execute(
                "SELECT 1 FROM authority_current_mapping WHERE resource_id=?", (resource,)
            ).fetchone()
            if not linked:
                raise TaskRegistryError("HISTORICAL_MAPPING_REQUIRES_EXPLICIT_HANDOFF")
            # Existing allocation is the projection being advanced, not a holder
            # competing with itself. The base class still performs every CAS.
        elif not isinstance(request.get("runtime"), dict):
            raise TaskRegistryError("RUNTIME_MAPPING_REQUIRED")
        result = super()._claim(conn, request, resource, epoch, now)
        if request["operation"] == "claim":
            args = dict(request["runtime"])
            if set(args) != {"attempt_id", "worker_id", "incarnation_id"}:
                raise TaskRegistryError("EXACT_RUNTIME_IDENTITY_REQUIRED")
            token = _enrolling.set(
                (resource, args["attempt_id"], args["worker_id"], args["incarnation_id"])
            )
            try:
                native = self.runtime_store.assign_attempt(
                    **args,
                    resource_key=resource,
                    lease_seconds=max(
                        1,
                        min(self.runtime_store.max_lease_seconds, int(request["expires_at"] - now)),
                    ),
                    command_id="shared:" + request["execution_ref"],
                    idempotency_key="shared:" + request["execution_ref"],
                )
            except Exception as exc:
                from runtime_control.errors import RuntimeControlError

                if isinstance(exc, RuntimeControlError):
                    raise TaskRegistryError(type(exc).__name__ + ":" + str(exc)) from exc
                raise
            finally:
                _enrolling.reset(token)
            self._fault("mapping_after_native_grant")
            values = native.result
            conn.execute(
                "INSERT INTO authority_allocation_links VALUES(?,?,?,?,?,?,?)",
                (
                    request["execution_ref"],
                    resource,
                    values["assignment_id"],
                    values["allocation_id"],
                    args["attempt_id"],
                    args["worker_id"],
                    args["incarnation_id"],
                ),
            )
            conn.execute(
                "INSERT INTO authority_mapping_events VALUES(?,?,?,?,?)",
                (resource, epoch, "ACTIVE", request["execution_ref"], values["assignment_id"]),
            )
            result["runtime_allocation"] = values
        self._fault("mapping_after_projection")
        return result

    def _runtime_held(self, conn, resource):
        # Exclude ONLY this same execution's exact existing projection.
        return conn.execute(
            """SELECT 1 FROM runtime_allocations a WHERE a.resource_key=?
            AND a.lifecycle IN ('ACTIVE','QUARANTINED') AND NOT EXISTS(
                SELECT 1 FROM authority_current_mapping m WHERE m.resource_id=a.resource_key
                AND m.allocation_id=a.allocation_id AND m.epoch=a.resource_epoch
                AND m.execution_ref=clinx_fence_execution())""",
            (resource,),
        ).fetchone()


def inventory(path):
    """Read-only snapshot, no schema init or writes, no invented historical epoch.

    Exact canonical worktree key equality is the only automatic match. Opaque
    runtime keys have no safe alias inference. Ambiguity stays UNKNOWN.
    """
    from pathlib import Path

    c = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA query_only=ON")
        c.execute("BEGIN")
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        facts = {}
        for table, key in (
            ("worktree_leases", "worktree_key"),
            ("authority_resources", "resource_id"),
            ("runtime_allocations", "resource_key"),
            ("runtime_resource_counters", "resource_key"),
        ):
            if table in tables:
                for row in c.execute(f"SELECT * FROM {table} ORDER BY {key}"):
                    value = dict(row)
                    facts.setdefault(value[key], {}).setdefault(table, []).append(value)
        result = []
        for resource, rows in sorted(facts.items()):
            links = []
            if "authority_allocation_links" in tables:
                links = [
                    dict(r)
                    for r in c.execute(
                        "SELECT * FROM authority_current_mapping WHERE resource_id=?", (resource,)
                    )
                ]
            status = "CURRENT_SHARED" if links else "HISTORICAL_UNMAPPED"
            if "worktree_leases" in rows and "runtime_allocations" in rows and not links:
                status = "CONFLICT_REQUIRES_STOP_AND_EXPLICIT_HANDOFF"
            result.append(
                dict(
                    resource_id=resource,
                    mapping=status,
                    current_mapping=links,
                    facts=rows,
                    inferred_epoch=None,
                    rule="exact stored resource key only; never infer owner/epoch from title,path similarity or UNKNOWN",
                )
            )
        return result
    finally:
        c.close()
