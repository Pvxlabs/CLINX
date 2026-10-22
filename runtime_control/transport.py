"""Durable inbox adapter for the existing runtime authority transaction.

This adapter retains the store's clock/handoff protocol. It does not map a
runtime allocation to a canonical worktree lease or create a second owner for
an enrolled canonical resource. Cross-database forwarding needs an outbox pump.
"""

import json
import sqlite3
from contextvars import ContextVar
from pathlib import Path

from canonical_authority import CanonicalAuthority, canonical, digest

_active = ContextVar("runtime_transport_delivery", default=None)


class AlreadyCommitted(Exception):
    def __init__(self, receipt):
        self.receipt = receipt


def begin_delivery(conn, path):
    ctx = _active.get()
    if not ctx or ctx["path"] != Path(path).resolve():
        return
    prior = conn.execute(
        """SELECT delivery_id,receipt FROM runtime_transport_receipts
        WHERE resource_id=? AND epoch=? AND operation_id=?""",
        ctx["key"],
    ).fetchone()
    if prior:
        if prior[0] != ctx["delivery_id"]:
            raise AlreadyCommitted(
                dict(
                    commit="OPERATION_ID_CONFLICT",
                    original_receipt=json.loads(prior[1]),
                )
            )
        raise AlreadyCommitted(json.loads(prior[1]))


def finish_delivery(conn, path):
    ctx = _active.get()
    if not ctx or ctx["path"] != Path(path).resolve():
        return
    row = conn.execute(
        "SELECT receipt_id,result_json FROM runtime_command_receipts WHERE command_id=?",
        (ctx["command_id"],),
    ).fetchone()
    if row is None:
        raise ValueError("runtime command receipt missing")
    result = json.loads(row[1])
    if (
        result.get("resource_key") != ctx["key"][0]
        or result.get("resource_epoch") != ctx["key"][1]
    ):
        raise ValueError("EPOCH_CAS_CONFLICT")
    receipt = dict(
        commit="RECORDED",
        resource_id=ctx["key"][0],
        epoch=ctx["key"][1],
        operation_id=ctx["key"][2],
        delivery_id=ctx["delivery_id"],
        runtime_receipt_id=row[0],
        result=json.loads(row[1]),
    )
    conn.execute(
        "INSERT INTO runtime_transport_receipts VALUES(?,?,?,?,?)",
        (*ctx["key"], ctx["delivery_id"], canonical(receipt)),
    )
    conn.execute(
        "INSERT INTO runtime_transport_outbox VALUES(?,?)",
        (digest(ctx["key"]), canonical(receipt)),
    )
    ctx["receipt"] = receipt


def install(store):
    """Explicit additive adapter migration; no runtime schema version rewrite."""
    # Both adapters use identical opaque inbox wire semantics. Runtime installs
    # only transport tables, not canonical enrollment or TaskRegistry tables.
    from canonical_authority import DDL

    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(next(sql for sql in DDL if "authority_inbox(" in sql))
        conn.execute("""CREATE TABLE IF NOT EXISTS runtime_transport_receipts(
            resource_id TEXT NOT NULL,epoch INTEGER NOT NULL,operation_id TEXT NOT NULL,
            delivery_id TEXT NOT NULL REFERENCES authority_inbox(delivery_id),receipt TEXT NOT NULL,
            PRIMARY KEY(resource_id,epoch,operation_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS runtime_transport_outbox(
            outbox_id TEXT PRIMARY KEY,receipt TEXT NOT NULL)""")
        for table in (
            "authority_inbox",
            "runtime_transport_receipts",
            "runtime_transport_outbox",
        ):
            for action in ("UPDATE", "DELETE"):
                conn.execute(f"""CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()}
                    BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable transport fact'); END""")


class RuntimeTransport:
    def __init__(self, store):
        self.store = store
        self.inbox = CanonicalAuthority(store.path)

    def deliver(self, event):
        return self.inbox.deliver(event)

    def read_commit(self, resource_id, epoch, operation_id):
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT receipt FROM runtime_transport_receipts
                WHERE resource_id=? AND epoch=? AND operation_id=?""",
                (resource_id, epoch, operation_id),
            ).fetchone()
            return json.loads(row[0]) if row else {"commit": "NOT_FOUND"}

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
        key = (resource_id, epoch, operation_id)
        with self.store.connect() as conn:
            prior = conn.execute(
                """SELECT delivery_id,receipt FROM runtime_transport_receipts
                WHERE resource_id=? AND epoch=? AND operation_id=?""",
                key,
            ).fetchone()
            if prior:
                return (
                    json.loads(prior[1])
                    if prior[0] == delivery_id
                    else dict(
                        commit="OPERATION_ID_CONFLICT",
                        original_receipt=json.loads(prior[1]),
                    )
                )
            delivered = conn.execute(
                "SELECT envelope FROM authority_inbox WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
        if not delivered:
            return {"commit": "DELIVERY_NOT_FOUND"}
        ctx = dict(
            path=Path(self.store.path).resolve(),
            key=key,
            delivery_id=delivery_id,
            command_id="transport:" + digest(key),
        )
        token = _active.set(ctx)
        try:
            request = json.loads(delivered[0])["payload"]
            op, args = request["operation"], dict(request["args"])
            if args.get("resource_key") != resource_id:
                raise ValueError("RESOURCE_IDENTITY_CONFLICT")
            if op not in {
                "assign_attempt",
                "mutate_protected_resource",
                "release_assignment",
            }:
                raise ValueError("UNSUPPORTED_RUNTIME_OPERATION")
            if op != "assign_attempt" and args.get("resource_epoch") != epoch:
                raise ValueError("EPOCH_IDENTITY_CONFLICT")
            # assign_attempt obtains epoch from the SAME existing counter;
            # target epoch is checked in the business transaction hook below.
            args.update(command_id=ctx["command_id"], idempotency_key=ctx["command_id"])
            getattr(self.store, op)(**args)
            return ctx["receipt"]
        except AlreadyCommitted as exc:
            return exc.receipt
        except Exception as exc:
            # Operational/crash faults must propagate; do not invent business
            # rejection on an uncertain COMMIT. Caller reads the receipt.
            from runtime_control.errors import RuntimeControlError

            if not isinstance(
                exc,
                (
                    RuntimeControlError,
                    ValueError,
                    KeyError,
                    TypeError,
                    sqlite3.IntegrityError,
                ),
            ):
                raise
            receipt = dict(
                commit="REJECTED_DOWNSTREAM",
                reason=type(exc).__name__ + ":" + str(exc),
                resource_id=resource_id,
                epoch=epoch,
                operation_id=operation_id,
                delivery_id=delivery_id,
            )
            with self.store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                prior = conn.execute(
                    "SELECT delivery_id,receipt FROM runtime_transport_receipts WHERE resource_id=? AND epoch=? AND operation_id=?",
                    key,
                ).fetchone()
                if prior:
                    return (
                        json.loads(prior[1])
                        if prior[0] == delivery_id
                        else dict(
                            commit="OPERATION_ID_CONFLICT",
                            original_receipt=json.loads(prior[1]),
                        )
                    )
                conn.execute(
                    "INSERT INTO runtime_transport_receipts VALUES(?,?,?,?,?)",
                    (*key, delivery_id, canonical(receipt)),
                )
            return receipt
        finally:
            _active.reset(token)
