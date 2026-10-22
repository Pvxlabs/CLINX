"""Bounded outbox pump and append-only delivery/business ACKs.

No daemon, network, Provider or Host. Use from the existing authority process.
Cross-database commits are separate; retry is at-least-once + receipt readback.
The supplied SQLite destination commits receipt projections only. External
side effects require their own durable receipt/CAS; delivery alone stays UNKNOWN.
"""

import json
import sqlite3
from pathlib import Path

from canonical_authority import CanonicalAuthority, canonical, digest
from task_registry import TaskRegistry


def connection(path):
    return TaskRegistry(path, initialize=False)._connect()


def install(path, source_id, *, fault=None):
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("stable source_id required")
    with connection(path) as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("""CREATE TABLE IF NOT EXISTS boundary_identity(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), source_id TEXT NOT NULL UNIQUE)""")
        prior = c.execute("SELECT source_id FROM boundary_identity").fetchone()
        if prior and prior[0] != source_id:
            raise ValueError("SOURCE_IDENTITY_CONFLICT")
        c.execute("INSERT OR IGNORE INTO boundary_identity VALUES(1,?)", (source_id,))
        c.execute("""CREATE TABLE IF NOT EXISTS boundary_acks(
            outbox_id TEXT NOT NULL,destination_id TEXT NOT NULL,phase TEXT NOT NULL,
            receipt TEXT NOT NULL,PRIMARY KEY(outbox_id,destination_id,phase),
            CHECK(phase IN ('DELIVERY','COMMIT')))""")
        c.execute("""CREATE TABLE IF NOT EXISTS boundary_attempts(
            position INTEGER PRIMARY KEY,outbox_id TEXT NOT NULL,destination_id TEXT NOT NULL,
            status TEXT NOT NULL,detail TEXT NOT NULL)""")
        # A route is bound on first attempt, before any destination call.
        c.execute("""CREATE TABLE IF NOT EXISTS boundary_routes(
            outbox_id TEXT PRIMARY KEY,destination_id TEXT NOT NULL)""")
        for t in ("boundary_identity", "boundary_acks", "boundary_routes", "boundary_attempts"):
            for op in ("UPDATE", "DELETE"):
                c.execute(f"""CREATE TRIGGER IF NOT EXISTS immutable_{t}_{op.lower()}
                    BEFORE {op} ON {t} BEGIN SELECT RAISE(ABORT,'IMMUTABLE_BOUNDARY_FACT'); END""")
        if fault:
            fault("boundary_migration_before_commit")
    if fault:
        fault("boundary_migration_after_commit")


def ack(path, outbox_id, destination, phase, receipt, *, fault=None):
    raw = canonical(receipt)
    with connection(path) as c:
        c.execute("BEGIN IMMEDIATE")
        prior = c.execute(
            "SELECT receipt FROM boundary_acks WHERE outbox_id=? AND destination_id=? AND phase=?",
            (outbox_id, destination, phase),
        ).fetchone()
        if prior and prior[0] != raw:
            raise ValueError("ACK_IDENTITY_CONFLICT")
        c.execute(
            "INSERT OR IGNORE INTO boundary_acks VALUES(?,?,?,?)",
            (outbox_id, destination, phase, raw),
        )
        if fault:
            fault("ack_" + phase.lower() + "_before_commit")
    if fault:
        fault("ack_" + phase.lower() + "_after_commit")


class SQLiteReceiptDestination:
    """Actual durable inbox + receipt projection commit in a SECOND database."""

    def __init__(self, path, destination_id, *, fault=None):
        self.path = Path(path)
        self.destination_id = destination_id
        self.fault = fault

    def install(self):
        from canonical_authority import DDL

        with sqlite3.connect(self.path, isolation_level=None) as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(next(s for s in DDL if "authority_inbox(" in s))
            c.execute("""CREATE TABLE IF NOT EXISTS boundary_destination_identity(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),destination_id TEXT NOT NULL)""")
            prior = c.execute("SELECT destination_id FROM boundary_destination_identity").fetchone()
            if prior and prior[0] != self.destination_id:
                raise ValueError("DESTINATION_IDENTITY_CONFLICT")
            c.execute(
                "INSERT OR IGNORE INTO boundary_destination_identity VALUES(1,?)",
                (self.destination_id,),
            )
            c.execute("""CREATE TABLE IF NOT EXISTS boundary_projections(
                event_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,source_receipt TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS boundary_commit_receipts(
                event_id TEXT PRIMARY KEY,delivery_id TEXT NOT NULL,receipt TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS boundary_response_outbox(
                event_id TEXT PRIMARY KEY,receipt TEXT NOT NULL)""")
            for t in (
                "authority_inbox",
                "boundary_destination_identity",
                "boundary_projections",
                "boundary_commit_receipts",
                "boundary_response_outbox",
            ):
                for op in ("UPDATE", "DELETE"):
                    c.execute(f"""CREATE TRIGGER IF NOT EXISTS immutable_{t}_{op.lower()}
                        BEFORE {op} ON {t} BEGIN SELECT RAISE(ABORT,'IMMUTABLE_DESTINATION_FACT'); END""")

    def _verify_identity(self, conn):
        row = conn.execute("SELECT destination_id FROM boundary_destination_identity").fetchone()
        if not row or row[0] != self.destination_id:
            raise ValueError("DESTINATION_IDENTITY_CONFLICT")

    def deliver(self, event):
        return CanonicalAuthority(self.path, fault=self.fault).deliver(event)

    def read_commit(self, event_id):
        with connection(self.path) as c:
            self._verify_identity(c)
            row = c.execute(
                "SELECT receipt FROM boundary_commit_receipts WHERE event_id=?", (event_id,)
            ).fetchone()
            return json.loads(row[0]) if row else {"commit": "NOT_FOUND"}

    def commit(self, delivery_id, event_id):
        with connection(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            self._verify_identity(c)
            prior = c.execute(
                "SELECT delivery_id,receipt FROM boundary_commit_receipts WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if prior:
                if prior[0] != delivery_id:
                    raise ValueError("DESTINATION_OPERATION_CONFLICT")
                return json.loads(prior[1])
            row = c.execute(
                "SELECT envelope FROM authority_inbox WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
            if not row:
                return {"commit": "DELIVERY_NOT_FOUND"}
            event = json.loads(row[0])
            if event["event_id"] != event_id:
                raise ValueError("EVENT_IDENTITY_CONFLICT")
            payload = event["payload"]
            receipt = dict(
                commit="RECORDED",
                effect="RECEIPT_PROJECTION",
                destination_id=self.destination_id,
                event_id=event_id,
                delivery_id=delivery_id,
                source_id=payload["source_id"],
                source_outbox_id=payload["outbox_id"],
                source_receipt_sha256=digest(payload["source_receipt"]),
            )
            c.execute(
                "INSERT INTO boundary_projections VALUES(?,?,?)",
                (event_id, payload["source_id"], canonical(payload["source_receipt"])),
            )
            if self.fault:
                self.fault("destination_after_mutation")
            c.execute(
                "INSERT INTO boundary_commit_receipts VALUES(?,?,?)",
                (event_id, delivery_id, canonical(receipt)),
            )
            c.execute(
                "INSERT INTO boundary_response_outbox VALUES(?,?)", (event_id, canonical(receipt))
            )
            if self.fault:
                self.fault("destination_before_commit")
        if self.fault:
            self.fault("destination_after_commit")
        return receipt


class OutboxPump:
    def __init__(self, path, destination, *, fault=None):
        self.path = Path(path)
        self.destination = destination
        self.fault = fault

    def _attempt(self, oid, status, detail=""):
        with connection(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "INSERT INTO boundary_attempts(outbox_id,destination_id,status,detail) VALUES(?,?,?,?)",
                (oid, self.destination.destination_id, status, detail),
            )
            if self.fault:
                self.fault("attempt_" + status.lower() + "_before_commit")
        if self.fault:
            self.fault("attempt_" + status.lower() + "_after_commit")

    def pending(self, limit=100):
        if not 1 <= limit <= 1000:
            raise ValueError("bounded batch required")
        with connection(self.path) as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            rows = []
            for table, col in (
                ("authority_outbox", "payload"),
                ("runtime_transport_outbox", "receipt"),
            ):
                if table in tables:
                    rows.extend(
                        (table + ":" + r[0], json.loads(r[1]))
                        for r in c.execute(
                            f"""SELECT o.outbox_id,o.{col} FROM {table} o WHERE NOT EXISTS(
                            SELECT 1 FROM boundary_acks a WHERE a.outbox_id=?||o.outbox_id
                            AND a.phase='COMMIT') ORDER BY o.outbox_id LIMIT ?""",
                            (table + ":", limit),
                        )
                    )
            return rows[:limit]

    def once(self, limit=100):
        results = []
        for oid, source_receipt in self.pending(limit):
            with connection(self.path) as c:
                c.execute("BEGIN IMMEDIATE")
                source_id = c.execute("SELECT source_id FROM boundary_identity").fetchone()[0]
                route = c.execute(
                    "SELECT destination_id FROM boundary_routes WHERE outbox_id=?", (oid,)
                ).fetchone()
                if route and route[0] != self.destination.destination_id:
                    raise ValueError("OUTBOX_ROUTE_CONFLICT")
                c.execute(
                    "INSERT OR IGNORE INTO boundary_routes VALUES(?,?)",
                    (oid, self.destination.destination_id),
                )
                if self.fault:
                    self.fault("route_before_commit")
            if self.fault:
                self.fault("route_after_commit")
            event_id = digest(
                [
                    source_id,
                    oid,
                    source_receipt["resource_id"],
                    source_receipt["epoch"],
                    source_receipt["operation_id"],
                ]
            )
            event = dict(
                stream_id="clinx-outbox:" + source_id,
                event_id=event_id,
                payload=dict(source_id=source_id, outbox_id=oid, source_receipt=source_receipt),
            )
            try:
                self._attempt(oid, "ATTEMPTED")
                delivery = self.destination.deliver(event)
                if delivery.get("delivery") != "DELIVERED" or delivery.get("delivery_id") != digest(
                    event
                ):
                    raise ValueError("DELIVERY_ACK_IDENTITY_CONFLICT")
                ack(
                    self.path,
                    oid,
                    self.destination.destination_id,
                    "DELIVERY",
                    delivery,
                    fault=self.fault,
                )
                if not hasattr(self.destination, "read_commit") or not hasattr(
                    self.destination, "commit"
                ):
                    # The pump cannot retry an unknown external effect. It only
                    # forwards immutable notifications to this delivery endpoint.
                    self._attempt(oid, "EXTERNAL_OUTCOME_UNKNOWN")
                    results.append(dict(outbox_id=oid, state="EXTERNAL_OUTCOME_UNKNOWN"))
                    continue
                committed = self.destination.read_commit(event_id)
                if committed.get("commit") == "NOT_FOUND":
                    committed = self.destination.commit(delivery["delivery_id"], event_id)
                if (
                    committed.get("commit") != "RECORDED"
                    or committed.get("delivery_id") != delivery["delivery_id"]
                    or committed.get("event_id") != event_id
                    or committed.get("source_id") != source_id
                    or committed.get("destination_id") != self.destination.destination_id
                    or committed.get("source_outbox_id") != oid
                    or committed.get("source_receipt_sha256") != digest(source_receipt)
                ):
                    raise ValueError("COMMIT_ACK_IDENTITY_CONFLICT")
                ack(
                    self.path,
                    oid,
                    self.destination.destination_id,
                    "COMMIT",
                    committed,
                    fault=self.fault,
                )
                self._attempt(oid, "COMMIT_ACKED")
                results.append(dict(outbox_id=oid, state="COMMIT_ACKED", receipt=committed))
            except Exception as exc:
                # Includes failures after remote commit: never report rollback
                # or rejected business outcome. Retry reads remote receipt first.
                self._attempt(oid, "OUTCOME_UNKNOWN", type(exc).__name__ + ":" + str(exc))
                results.append(dict(outbox_id=oid, state="OUTCOME_UNKNOWN"))
        return results
