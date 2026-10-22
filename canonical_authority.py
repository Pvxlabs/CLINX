"""Explicit, default-off SQLite integration of the minimal takeover contract.

Transport accepts opaque JSON into a durable inbox. Canonical commits borrow
ONE connection through existing TaskRegistry/Finalizer/repair code. This is a
logical database fence, not revocation of an OS-level writable DB capability.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from authority_transactions import bind
from task_registry import TaskRegistry, TaskRegistryError


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


DDL = (
    """CREATE TABLE IF NOT EXISTS authority_schema(version INTEGER PRIMARY KEY CHECK(version=1))""",
    """CREATE TABLE IF NOT EXISTS authority_resources(
        resource_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL CHECK(epoch>0),
        execution_ref TEXT NOT NULL, task_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('ACTIVE','RELEASED')),
        expires_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS authority_bindings(
        execution_ref TEXT PRIMARY KEY, resource_id TEXT NOT NULL, task_id TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS authority_inbox(
        position INTEGER PRIMARY KEY, delivery_id TEXT NOT NULL UNIQUE,
        stream_id TEXT NOT NULL,event_id TEXT NOT NULL,envelope TEXT NOT NULL,receipt TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS authority_receipts(
        resource_id TEXT NOT NULL,epoch INTEGER NOT NULL,operation_id TEXT NOT NULL,
        delivery_id TEXT NOT NULL REFERENCES authority_inbox(delivery_id),receipt TEXT NOT NULL,
        PRIMARY KEY(resource_id,epoch,operation_id))""",
    """CREATE TABLE IF NOT EXISTS authority_outbox(
        outbox_id TEXT PRIMARY KEY, resource_id TEXT NOT NULL,epoch INTEGER NOT NULL,
        operation_id TEXT NOT NULL, destination TEXT NOT NULL, payload TEXT NOT NULL)""",
    # Reuse the existing runtime counter; never reset it on release/recovery.
    """CREATE TABLE IF NOT EXISTS runtime_resource_counters(
        resource_key TEXT PRIMARY KEY,current_epoch INTEGER NOT NULL DEFAULT 0 CHECK(current_epoch>=0))""",
)


def migrate(path):
    """Explicit additive v1 migration. Never called by registry construction.

    Validate a production-compatible schema and refuse enrollment of runtime
    allocations; existing historical rows are not backfilled with invented epochs.
    All DDL, including triggers, is committed in one transaction (no executescript).
    """
    registry = TaskRegistry(path, initialize=False)
    with registry._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for table, columns in {
            "tasks": {"task_id", "execution_state", "turn_id"},
            "executions": {
                "execution_ref",
                "task_id",
                "worktree_key",
                "execution_state",
            },
            "execution_history": {"execution_ref", "task_id", "worktree_key"},
            "execution_results": {"execution_ref", "task_id", "raw_result"},
            "worktree_leases": {"worktree_key", "task_id", "execution_ref"},
            "result_reconciliation_audit": {"execution_ref", "expected_result_sha256"},
        }.items():
            actual = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if not columns <= actual:
                raise TaskRegistryError("AUTHORITY_SCHEMA_INCOMPATIBLE:" + table)
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='authority_schema'"
        ).fetchone():
            if [r[0] for r in conn.execute("SELECT version FROM authority_schema")] != [
                1
            ]:
                raise TaskRegistryError("UNSUPPORTED_AUTHORITY_SCHEMA")
        for statement in DDL:
            conn.execute(statement)
        # Apply guards only to explicitly enrolled objects. Unenrolled legacy
        # rows retain their schema/values and do not gain an invented epoch.
        tables = {
            "tasks": ("task", "task_id"),
            "executions": ("execution", "execution_ref"),
            "execution_history": ("execution", "execution_ref"),
            "execution_results": ("execution", "execution_ref"),
            "result_ingestions": ("execution", "execution_ref"),
            "result_reconciliation_audit": ("execution", "execution_ref"),
            "execution_liveness": ("execution", "execution_ref"),
            "host_executions": ("execution", "execution_ref"),
            "production_resource_leases": ("execution", "execution_ref"),
            "worktree_leases": ("resource", "worktree_key"),
        }
        for table, (kind, column) in tables.items():
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name=?", (table,)
            ).fetchone():
                continue
            for action in ("INSERT", "UPDATE", "DELETE"):
                checks = (
                    ("NEW",)
                    if action == "INSERT"
                    else ("OLD",)
                    if action == "DELETE"
                    else ("OLD", "NEW")
                )
                body = []
                for row in checks:
                    if kind == "task":
                        enrolled = f"b.task_id={row}.{column}"
                        exact = f"r.task_id={row}.{column}"
                    elif kind == "resource":
                        enrolled = f"b.resource_id={row}.{column}"
                        exact = (
                            f"r.resource_id={row}.{column} AND r.task_id={row}.task_id "
                            f"AND r.execution_ref={row}.execution_ref"
                        )
                    else:
                        enrolled = f"b.execution_ref={row}.{column}"
                        exact = f"r.execution_ref={row}.{column} AND r.task_id={row}.task_id"
                        if table in {"executions", "execution_history"}:
                            enrolled += f" OR b.resource_id={row}.worktree_key"
                            exact += f" AND r.resource_id={row}.worktree_key"
                        if table in {
                            "result_ingestions",
                            "result_reconciliation_audit",
                            "execution_liveness",
                        }:
                            exact = f"r.execution_ref={row}.{column}"
                    body.append(f"""SELECT CASE WHEN EXISTS(
                        SELECT 1 FROM authority_bindings b WHERE {enrolled})
                        AND NOT EXISTS(SELECT 1 FROM authority_resources r
                            WHERE {exact} AND r.resource_id=clinx_fence_resource()
                            AND r.epoch=clinx_fence_epoch()
                            AND r.execution_ref=clinx_fence_execution()
                            AND (r.state='ACTIVE' OR (r.state='RELEASED'
                                AND clinx_fence_operation() IN ('repair','owner_projection'))))
                        THEN RAISE(ABORT,'AUTHORITY_FENCE_REQUIRED') END;""")
                conn.execute(f"DROP TRIGGER IF EXISTS authority_guard_{table}_{action.lower()}")
                conn.execute(f"""CREATE TRIGGER authority_guard_{table}_{action.lower()}
                    BEFORE {action} ON {table} BEGIN {"".join(body)} END""")
        # A second runtime allocator must not advance the same enrolled resource.
        # It needs an explicit mapping/adapter rather than independent ownership.
        for table in (
            "runtime_assignments",
            "runtime_allocations",
            "runtime_protected_resources",
        ):
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name=?", (table,)
            ).fetchone():
                for action in ("INSERT", "UPDATE", "DELETE"):
                    rows = (
                        ("NEW",)
                        if action == "INSERT"
                        else ("OLD",)
                        if action == "DELETE"
                        else ("OLD", "NEW")
                    )
                    body = "".join(
                        f"""SELECT CASE WHEN EXISTS(SELECT 1 FROM authority_resources
                        WHERE resource_id={row}.resource_key) THEN
                        RAISE(ABORT,'CANONICAL_RESOURCE_REQUIRES_SHARED_AUTHORITY') END;"""
                        for row in rows
                    )
                    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS authority_guard_{table}_{action.lower()}
                        BEFORE {action} ON {table} BEGIN {body} END""")
        for table in (
            "authority_inbox",
            "authority_receipts",
            "authority_outbox",
            "authority_bindings",
        ):
            for action in ("UPDATE", "DELETE"):
                conn.execute(f"""CREATE TRIGGER IF NOT EXISTS authority_immutable_{table}_{action.lower()}
                    BEFORE {action} ON {table} BEGIN
                    SELECT RAISE(ABORT,'immutable authority fact'); END""")
        for table, resource_column, epoch_column in (
            ("authority_resources", "resource_id", "epoch"),
            ("runtime_resource_counters", "resource_key", "current_epoch"),
            ("authority_receipts", "resource_id", "epoch"),
            ("authority_outbox", "resource_id", "epoch"),
        ):
            for action in ("INSERT", "UPDATE", "DELETE"):
                row = "OLD" if action == "DELETE" else "NEW"
                enrolled = (
                    "1"
                    if table != "runtime_resource_counters"
                    else f"EXISTS(SELECT 1 FROM authority_resources WHERE resource_id={row}.{resource_column})"
                )
                conn.execute(f"""CREATE TRIGGER IF NOT EXISTS authority_token_{table}_{action.lower()}
                    BEFORE {action} ON {table} WHEN {enrolled} BEGIN
                    SELECT CASE WHEN {row}.{resource_column} IS NOT clinx_fence_resource()
                        OR {row}.{epoch_column} IS NOT clinx_fence_epoch()
                        THEN RAISE(ABORT,'AUTHORITY_TRANSACTION_REQUIRED') END; END""")
        conn.execute("INSERT OR IGNORE INTO authority_schema VALUES(1)")


class CanonicalAuthority:
    def __init__(self, path, *, clock=time.time, fault=None, registry=None):
        self.path = Path(path)
        if registry is not None and registry.path.resolve() != self.path.resolve():
            raise ValueError("registry must use the same authority database")
        self.registry = (
            registry if registry is not None else TaskRegistry(path, initialize=False)
        )
        self.clock, self.fault = clock, fault

    def _fault(self, point):
        if self.fault:
            self.fault(point)

    def deliver(self, event):
        if not isinstance(event, dict) or any(
            not isinstance(event.get(k), str) or not event[k]
            for k in ("stream_id", "event_id")
        ):
            return {"delivery": "INVALID_ENVELOPE"}
        raw, delivery = canonical(event), digest(event)
        with self.registry._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                "SELECT envelope,receipt FROM authority_inbox WHERE delivery_id=?",
                (delivery,),
            ).fetchone()
            if prior:
                if prior["envelope"] != raw:
                    raise ValueError("digest collision")
                return json.loads(prior["receipt"])
            conflict = bool(
                conn.execute(
                    "SELECT 1 FROM authority_inbox WHERE stream_id=? AND event_id=?",
                    (event["stream_id"], event["event_id"]),
                ).fetchone()
            )
            position = conn.execute(
                "SELECT COALESCE(MAX(position),0)+1 FROM authority_inbox"
            ).fetchone()[0]
            receipt = dict(
                delivery="DELIVERED",
                destination="sqlite_downstream_inbox",
                delivery_id=delivery,
                position=position,
                stream_id=event["stream_id"],
                event_id=event["event_id"],
                identity_conflict=conflict,
            )
            conn.execute(
                "INSERT INTO authority_inbox VALUES(?,?,?,?,?,?)",
                (
                    position,
                    delivery,
                    event["stream_id"],
                    event["event_id"],
                    raw,
                    canonical(receipt),
                ),
            )
            self._fault("delivery_before_commit")
        self._fault("delivery_after_commit")
        return receipt

    def read_delivery(self, delivery_id):
        with self.registry._connect() as conn:
            row = conn.execute(
                "SELECT receipt FROM authority_inbox WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            return json.loads(row[0]) if row else {"delivery": "NOT_FOUND"}

    def read_commit(self, resource_id, epoch, operation_id):
        with self.registry._connect() as conn:
            row = conn.execute(
                "SELECT receipt FROM authority_receipts WHERE resource_id=? AND epoch=? AND operation_id=?",
                (resource_id, epoch, operation_id),
            ).fetchone()
            return json.loads(row[0]) if row else {"commit": "NOT_FOUND"}

    def _claim(self, conn, request, resource, epoch, now):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='authority_mapping_schema'").fetchone():
            if not getattr(self, "shared_mapping_entry", False):
                raise TaskRegistryError("SHARED_AUTHORITY_ENTRY_REQUIRED")
        task_id, execution = request["task_id"], request["execution_ref"]
        task = self.registry.get_task(task_id)
        expected_key = self.registry.worktree_key(
            host=task.host, cwd=task.cwd, repository_origin=task.repository_origin
        )
        if resource != expected_key:
            raise TaskRegistryError("RESOURCE_IDENTITY_CONFLICT")
        prior = conn.execute(
            "SELECT * FROM authority_resources WHERE resource_id=?", (resource,)
        ).fetchone()
        counter = conn.execute(
            "SELECT current_epoch FROM runtime_resource_counters WHERE resource_key=?",
            (resource,),
        ).fetchone()
        previous_epoch = counter[0] if counter else 0
        if epoch != previous_epoch + 1 or request["expected_epoch"] != previous_epoch:
            raise TaskRegistryError("EPOCH_CAS_CONFLICT")
        if self._runtime_held(conn, resource):
            raise TaskRegistryError("RUNTIME_RESOURCE_MAPPING_REQUIRED")
        expires = request["expires_at"]
        if type(expires) not in (int, float) or not now < expires < float("inf"):
            raise TaskRegistryError("INVALID_LEASE_EXPIRY")
        if request["operation"] == "advance_epoch":
            if (
                not prior
                or prior["state"] != "ACTIVE"
                or prior["execution_ref"] != execution
                or prior["task_id"] != task_id
            ):
                raise TaskRegistryError("EPOCH_OWNER_CONFLICT")
            # Same execution/lease; no new attempt, provider call or replay.
        else:
            if prior and prior["state"] != "RELEASED":
                raise TaskRegistryError("RESOURCE_STILL_HELD")
            if (
                conn.execute(
                    "SELECT 1 FROM executions WHERE task_id=?", (task_id,)
                ).fetchone()
                or conn.execute(
                    "SELECT 1 FROM worktree_leases WHERE worktree_key=?", (resource,)
                ).fetchone()
            ):
                raise TaskRegistryError("LEGACY_OWNER_MIGRATION_REQUIRED")
            if (
                conn.execute(
                    "SELECT 1 FROM authority_bindings WHERE execution_ref=?",
                    (execution,),
                ).fetchone()
                or conn.execute(
                    "SELECT 1 FROM execution_history WHERE execution_ref=?",
                    (execution,),
                ).fetchone()
            ):
                raise TaskRegistryError("HISTORICAL_EXECUTION_IMMUTABLE")
            conn.execute(
                "INSERT INTO authority_bindings VALUES(?,?,?)",
                (execution, resource, task_id),
            )
        conn.execute(
            "INSERT INTO runtime_resource_counters SELECT ?,0 WHERE NOT EXISTS(SELECT 1 FROM runtime_resource_counters WHERE resource_key=?)",
            (resource, resource),
        )
        changed = conn.execute(
            "UPDATE runtime_resource_counters SET current_epoch=? WHERE resource_key=? AND current_epoch=?",
            (epoch, resource, previous_epoch),
        ).rowcount
        if changed != 1:
            raise TaskRegistryError("EPOCH_CAS_CONFLICT")
        conn.execute(
            """INSERT INTO authority_resources VALUES(?,?,?,?, 'ACTIVE',?)
            ON CONFLICT(resource_id) DO UPDATE SET epoch=excluded.epoch,execution_ref=excluded.execution_ref,
            task_id=excluded.task_id,state='ACTIVE',expires_at=excluded.expires_at""",
            (resource, epoch, execution, task_id, expires),
        )
        if request["operation"] == "claim":
            if task.status != "ACTIVE":
                raise TaskRegistryError("TASK_NOT_ACTIVE")
            with self.registry.execution(task_id, request.get('issue_id'),
                    execution_ref=execution, retain=True,
                    expected_policy_json=request.get('expected_policy_json'),
                    expected_route_json=request.get('expected_route_json')):
                self.registry.set_execution_state(
                    task_id,
                    "CLAIMED",
                    current_stage="CLAIMED",
                    turn_id=None,
                    _shadow_execution_ref=execution,
                )
        return dict(execution_ref=execution, resource_epoch=epoch)

    @staticmethod
    def _runtime_held(conn, resource):
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='runtime_allocations'"
            ).fetchone()
            and conn.execute(
                "SELECT 1 FROM runtime_allocations WHERE resource_key=? AND lifecycle IN ('ACTIVE','QUARANTINED')",
                (resource,),
            ).fetchone()
        )

    def _apply(self, conn, request, resource, epoch):
        row = conn.execute(
            "SELECT * FROM authority_resources WHERE resource_id=?", (resource,)
        ).fetchone()
        execution, task = row["execution_ref"], row["task_id"]
        args = request.get("args", {})
        if any(
            args.get(k, value) != value
            for k, value in (("execution_ref", execution), ("task_id", task))
        ):
            raise TaskRegistryError("EXECUTION_IDENTITY_CONFLICT")
        op = request["operation"]
        if op in {'owner_update', 'owner_projection'}:
            owner = getattr(self, 'owner', None)
            if owner is None:
                raise TaskRegistryError('EXECUTION_OWNER_NOT_CONFIGURED')
            result = owner.apply_registry_mutation(conn, execution, task, op, args)
        elif op == 'owner_liveness':
            owner = getattr(self, 'owner', None)
            if owner is None:
                raise TaskRegistryError('EXECUTION_OWNER_NOT_CONFIGURED')
            result = owner.apply_liveness_mutation(execution, task, args)
        elif op == 'native_input':
            from native_input_recovery import commit_receipt
            if (args['execution_ref'] != execution
                    or args['task_snapshot']['task_id'] != task
                    or args['binding_snapshot']['task_id'] != task):
                raise TaskRegistryError('EXECUTION_IDENTITY_CONFLICT')
            result = commit_receipt(self.registry, **args)
        elif op == 'renew':
            expires = args['expires_at']
            now = self.clock()
            if (not getattr(self, 'shared_mapping_entry', False)
                    or args.get('execution_ref') != execution
                    or type(expires) not in (int, float)
                    or not now < expires <= now + self.runtime_store.max_lease_seconds):
                raise TaskRegistryError('INVALID_SHARED_RENEWAL')
            conn.execute('UPDATE authority_resources SET expires_at=? WHERE resource_id=? AND epoch=?',
                (expires, resource, epoch))
            result = dict(execution_ref=execution, resource_epoch=epoch, expires_at=expires)
        elif op == "state":
            if args.get("state") in {
                "COMPLETED",
                "FAILED",
                "BLOCKED",
                "CANCELLED",
                "STOPPED",
                "IN_REVIEW",
            }:
                raise TaskRegistryError("TERMINAL_REQUIRES_FINALIZER_BUNDLE")
            if "_shadow_execution_ref" in args:
                raise TaskRegistryError("EXECUTION_IDENTITY_CONFLICT")
            result = self.registry.set_execution_state(
                task,
                _shadow_execution_ref=execution,
                **{k: v for k, v in args.items() if k != "task_id"},
            )
        elif op == "result":
            result = self.registry.record_execution_result(
                execution_ref=execution,
                task_id=task,
                **{
                    k: v
                    for k, v in args.items()
                    if k not in ("execution_ref", "task_id")
                },
            )
        elif op == "finalize":
            from m9_integration import ExecutionFinalizer

            # No external projection/client in the authoritative transaction.
            class TransactionFinalizer(ExecutionFinalizer):
                def _decision(self, *values, **options):
                    # Host delivery reconciliation has its own transaction and
                    # remains pending; never open a nested transaction here.
                    options["reconcile_delivery"] = False
                    return super()._decision(*values, **options)

            # SQLite already serializes this exact bundle. Do not invert the
            # legacy file-lock -> SQLite ordering by taking flock under SQLite.
            result = ExecutionFinalizer.finalize.__wrapped__(
                TransactionFinalizer(self.registry),
                execution_ref=execution,
                task_id=task,
                **{
                    k: v
                    for k, v in args.items()
                    if k not in ("execution_ref", "task_id")
                },
            )
        elif op == "release":
            # Never expose legacy bypass flags to event payloads.
            if args:
                raise TaskRegistryError("INVALID_RELEASE_ARGUMENTS")
            if not conn.execute(
                "SELECT 1 FROM execution_results WHERE execution_ref=?", (execution,)
            ).fetchone():
                raise TaskRegistryError("TERMINAL_BUNDLE_NOT_COMMITTED")
            active = conn.execute(
                "SELECT stage FROM executions WHERE execution_ref=?", (execution,)
            ).fetchone()
            if active is None or active[0] not in {
                "COMPLETED",
                "FAILED",
                "BLOCKED",
                "CANCELLED",
                "STOPPED",
            }:
                raise TaskRegistryError("TERMINAL_BUNDLE_NOT_COMMITTED")
            self.registry.release_execution(task, execution, retain_history=True)
            result = None
        elif op == "observe":
            from execution_liveness import record

            result = record(self.registry, execution, **args)
        elif op == "repair":
            from m9_integration import ExecutionFinalizer
            from result_ingestion import repair_result

            if (
                args.get("identity", {}).get("execution_ref") != execution
                or args["identity"].get("task_id") != task
            ):
                raise TaskRegistryError("EXECUTION_IDENTITY_CONFLICT")
            result = repair_result(ExecutionFinalizer(self.registry), **args)
        else:
            raise TaskRegistryError("UNSUPPORTED_CANONICAL_OPERATION")
        self._fault("after_mutation")
        # Finalizer may have released the exact canonical lease. Record this
        # resource release in the SAME transaction as history/result/receipt.
        if (
            op in {"finalize", "release", "owner_update"}
            and not conn.execute(
                "SELECT 1 FROM executions WHERE execution_ref=?", (execution,)
            ).fetchone()
        ):
            if conn.execute(
                "SELECT 1 FROM worktree_leases WHERE worktree_key=?", (resource,)
            ).fetchone():
                raise TaskRegistryError("RELEASE_IDENTITY_CONFLICT")
            changed = conn.execute(
                "UPDATE authority_resources SET state='RELEASED' WHERE resource_id=? AND epoch=? AND state='ACTIVE'",
                (resource, epoch),
            ).rowcount
            if changed != 1:
                raise TaskRegistryError("EPOCH_CAS_CONFLICT")
        return (
            dataclasses.asdict(result) if dataclasses.is_dataclass(result) else result
        )

    def commit(self, delivery_id, resource_id, epoch, operation_id):
        if (
            type(epoch) is not int
            or epoch < 1
            or any(
                not isinstance(v, str) or not v
                for v in (delivery_id, resource_id, operation_id)
            )
        ):
            return {"commit": "INVALID_ENVELOPE"}
        self._fault("before_transaction")
        with self.registry._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                "SELECT delivery_id,receipt FROM authority_receipts WHERE resource_id=? AND epoch=? AND operation_id=?",
                (resource_id, epoch, operation_id),
            ).fetchone()
            if prior:
                if prior[0] != delivery_id:
                    return dict(
                        commit="OPERATION_ID_CONFLICT",
                        original_receipt=json.loads(prior[1]),
                    )
                return json.loads(prior[1])
            delivered = conn.execute(
                "SELECT envelope FROM authority_inbox WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            if not delivered:
                return {"commit": "DELIVERY_NOT_FOUND"}
            request = json.loads(delivered[0]).get("payload")
            receipt = dict(
                resource_id=resource_id,
                epoch=epoch,
                operation_id=operation_id,
                delivery_id=delivery_id,
            )
            # Business failures roll back ONLY the attempted bundle; durable
            # transport acceptance and explicit rejection remain available.
            conn.execute("SAVEPOINT mutation")
            try:
                now = self.clock()  # trusted authority clock, sampled under lock
                if not isinstance(request, dict):
                    raise TaskRegistryError("INVALID_CANONICAL_REQUEST")
                claiming = request.get("operation") in {"claim", "advance_epoch"}
                if claiming:
                    execution = request["execution_ref"]
                else:
                    with bind(self.path, conn, resource_id, epoch, ""):
                        matched = conn.execute(
                            """UPDATE authority_resources SET epoch=epoch WHERE resource_id=? AND epoch=?
                            AND ((state='ACTIVE' AND expires_at>?) OR
                                (state='RELEASED' AND (?='owner_projection' OR (?='repair' AND expires_at>?))))""",
                            (resource_id, epoch, now, request.get("operation"), request.get("operation"), now),
                        ).rowcount
                    if matched != 1:
                        raise TaskRegistryError("REJECTED_STALE_OR_INACTIVE_EPOCH")
                    execution = conn.execute(
                        "SELECT execution_ref FROM authority_resources WHERE resource_id=?",
                        (resource_id,),
                    ).fetchone()[0]
                with bind(
                    self.path,
                    conn,
                    resource_id,
                    epoch,
                    execution,
                    request.get("operation"),
                ):
                    self._fault("after_fence")
                    result = (
                        self._claim(conn, request, resource_id, epoch, now)
                        if claiming
                        else self._apply(conn, request, resource_id, epoch)
                    )
                receipt.update(commit="RECORDED", result=result)
                # Durable projection intent only, no external call under lock.
                outbox_id = digest([resource_id, epoch, operation_id])
                with bind(self.path, conn, resource_id, epoch, execution):
                    conn.execute(
                        "INSERT INTO authority_outbox VALUES(?,?,?,?,?,?)",
                        (
                            outbox_id,
                            resource_id,
                            epoch,
                            operation_id,
                            "canonical_commit_notification",
                            canonical(receipt),
                        ),
                    )
                conn.execute("RELEASE mutation")
            except (
                TaskRegistryError,
                sqlite3.IntegrityError,
                KeyError,
                TypeError,
                ValueError,
            ) as exc:
                conn.execute("ROLLBACK TO mutation")
                conn.execute("RELEASE mutation")
                receipt.update(commit="REJECTED_DOWNSTREAM", reason=str(exc))
                if str(exc) == "REJECTED_STALE_OR_INACTIVE_EPOCH":
                    receipt["commit"] = str(exc)
            with bind(self.path, conn, resource_id, epoch, ""):
                conn.execute(
                    "INSERT INTO authority_receipts VALUES(?,?,?,?,?)",
                    (resource_id, epoch, operation_id, delivery_id, canonical(receipt)),
                )
            self._fault("state_before_commit")
        self._fault("state_after_commit")
        return receipt
