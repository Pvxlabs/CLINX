"""Observation-only admission into the existing task registry; never dispatches.

The authenticated observation writer is the only automatic caller. Node IDs are
paired durable identities, not display aliases. History stays in observations;
this table is an identity index with exact immutable first-admission provenance.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import sqlite3
import time
import uuid
from pathlib import Path

from network_observation import compact, database
from node_protocol import SUPPORTED_PROVIDER, NodeProtocolError, SessionIdentity
from task_registry import _now

THREAD = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
CLIENTS = {"cli", "vscode", "desktop", "codex_desktop", "codex_cli"}


def canonical_native_id(node, provider, thread):
    return "task_" + uuid.uuid5(uuid.NAMESPACE_URL, compact([node, provider, thread])).hex


def handoff(registry, association):
    """Identity only. This envelope is usable only inside an authorized node RPC."""
    task = registry.get_task(association["task_id"])
    return {key: association[key] for key in (
        "node_id", "provider", "thread_id", "user_scope", "task_id", "host",
        "origin", "first_provenance_json",
    )} | {"title": task.title, "cwd": association['project']}


def qualify_handoff(registry, envelope, *, owner, user, fingerprint, project, identity):
    """Install an exact canonical identity at its execution owner, without a grant.

    Caller has checked bilateral execute scope and local target allowlists. All
    workspace metadata comes from local configuration and native JSONL, never
    from the centre. No binding/policy/lease/execution is installed by handoff.
    """
    node, provider, tid = (envelope.get(k) for k in ("node_id", "provider", "thread_id"))
    proof = json.loads(envelope.get("first_provenance_json", "{}"))
    if (node != owner or envelope.get("host") != owner or provider != SUPPORTED_PROVIDER
            or envelope.get("origin") != "CODEX_NATIVE" or envelope.get("user_scope") != user
            or not isinstance(tid, str) or not THREAD.fullmatch(tid)
            or envelope.get("task_id") != canonical_native_id(node, provider, tid)
            or proof.get("node_fingerprint") != fingerprint or proof.get("node") != node
            or not proof.get("digest") or not proof.get("observation_id")
            or identity.get("id") != tid or identity.get("cwd") != envelope.get("cwd")
            or str(project.repo) != envelope.get("cwd")):
        raise NodeProtocolError("CANONICAL_HANDOFF_IDENTITY_CONFLICT", "Exact native identity, owner or provenance disagrees")
    task_id = envelope["task_id"]
    title = registry._validate_metadata_value('title', identity.get('name') or envelope.get('title') or 'Native Codex task', 200)
    with registry._connect() as c:
        c.execute("BEGIN IMMEDIATE")
        old = c.execute("SELECT * FROM native_task_bindings WHERE node_id=? AND provider=? AND thread_id=?",
                        (node, provider, tid)).fetchone()
        task = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        owners = c.execute("SELECT b.task_id FROM conversation_bindings b JOIN tasks t USING(task_id) WHERE t.host=? AND b.thread_id=? LIMIT 2",
                           (owner, tid)).fetchall()
        if (any(r[0] != task_id for r in owners)
                or (old and (old["task_id"] != task_id or old["user_scope"] != user
                             or json.loads(old["first_provenance_json"]).get("node_fingerprint") != fingerprint))
                or (task and (not old or task["host"] != owner or task["cwd"] != envelope["cwd"]
                              or task["project_alias"] != project.project_alias))):
            raise NodeProtocolError("CANONICAL_HANDOFF_OWNER_CONFLICT", "Existing canonical owner cannot be replaced or merged")
        stamp = _now()
        if not task:
            c.execute("""INSERT INTO tasks(task_id,host,workspace_alias,project_alias,project_name,cwd,
                title,status,created_at,updated_at,execution_state,current_stage,last_progress_at)
                VALUES (?,?,?,?,?,?,?,'ACTIVE',?,?,'UNKNOWN','native_handoff',?)""",
                (task_id, owner, project.workspace_alias, project.project_alias, project.linear_name,
                 envelope["cwd"], title, stamp, stamp, stamp))
        if not old:
            # Preserve the immutable originating observation. Handoff is not a
            # new native event and must not advance observed state/freshness.
            c.execute("INSERT INTO native_task_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (node, provider, tid, user, task_id, owner, envelope["cwd"], "CODEX_NATIVE",
                 compact(proof), compact(proof), 0, proof.get("received", stamp)))
    return task_id


def seconds(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else 0
    except (TypeError, ValueError):
        try:
            return dt.datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            ).timestamp()
        except (ValueError, TypeError, OverflowError):
            return 0


def binding(
    path,
    *,
    node=None,
    host=None,
    provider=SUPPORTED_PROVIDER,
    thread=None,
    task=None,
    user=None,
):
    """Read exact associations without schema migration or write locks."""
    if not path or not Path(path).exists():
        return None
    with database(path) as c:
        if not c.execute(
            "SELECT 1 FROM sqlite_master WHERE name='native_task_bindings'"
        ).fetchone():
            return None
        predicates, args = [], []
        for column, value in (
            ("node_id", node),
            ("host", host),
            ("provider", provider),
            ("thread_id", thread),
            ("task_id", task),
            ("user_scope", user),
        ):
            if value is not None:
                predicates.append(column + "=?")
                args.append(value)
        rows = c.execute(
            "SELECT * FROM native_task_bindings WHERE "
            + " AND ".join(predicates)
            + " LIMIT 2",
            args,
        ).fetchall()
        if len(rows) > 1:
            raise NodeProtocolError(
                "NATIVE_IDENTITY_AMBIGUOUS", "Use an exact node and thread"
            )
        return dict(rows[0]) if rows else None


class NativeTaskLifecycle:
    def __init__(self, registry, directory, routes, *, since, auto_budget=1000):
        self.registry, self.directory, self.routes = registry, directory, routes
        from execution_semantics import normalize_host

        hosts = set()
        for route in routes.values():
            host = normalize_host(route["host"]).stable_identifier
            if host in hosts or host != route["host"] or not route.get("fingerprint"):
                raise ValueError(
                    "Node mappings need distinct canonical hosts and pinned fingerprints"
                )
            hosts.add(host)
        if type(auto_budget) is not int or not 0 <= auto_budget <= 10000:
            raise ValueError("Automatic admission budget must be 0..10000")
        if seconds(since) <= 0:
            raise ValueError("Explicit automatic-admission lower bound is required")
        with registry._connect() as c:
            c.execute(
                "INSERT OR IGNORE INTO native_lifecycle_control(id,since,auto_remaining) VALUES (1,?,?)",
                (seconds(since), auto_budget),
            )

    def control(self, *, paused=None, backfill_remaining=None, auto_remaining=None):
        """Explicit local operator action; no observation payload controls budgets."""
        if paused is not None and type(paused) is not bool:
            raise ValueError("paused must be boolean")
        if backfill_remaining is not None and (
            type(backfill_remaining) is not int or not 0 <= backfill_remaining <= 1000
        ):
            raise ValueError("Backfill budget must be 0..1000")
        if auto_remaining is not None and (
            type(auto_remaining) is not int or not 0 <= auto_remaining <= 10000
        ):
            raise ValueError("Automatic admission budget must be 0..10000")
        with self.registry._connect() as c:
            for column, value in (
                ("paused", paused),
                ("backfill_remaining", backfill_remaining),
                ("auto_remaining", auto_remaining),
            ):
                if value is not None:
                    c.execute(
                        "UPDATE native_lifecycle_control SET "
                        + column
                        + "=? WHERE id=1",
                        (value,),
                    )
            return dict(
                c.execute(
                    "SELECT * FROM native_lifecycle_control WHERE id=1"
                ).fetchone()
            )

    def reconcile(self, observation, *, backfill=False):
        # detail rechecks current bilateral sharing, trust, user and project.
        item = self.directory.detail(observation, limit=1)["item"]
        node, provider, tid = (
            item["node_id"],
            item["provider"],
            item["native_thread_id"],
        )
        project = self.routes.get(node, {}).get("projects", {}).get(item["project"])
        route = self.routes.get(node, {})
        record = self.directory.registry.get(node)
        if (
            not project
            or provider != SUPPORTED_PROVIDER
            or not THREAD.fullmatch(tid)
            or not record
            or route.get("fingerprint") != record.public_key_fingerprint
        ):
            return {"status": "UNMAPPED_SCOPE"}
        # Re-read stored sanitized payload and immutable latest source event.
        with database(self.directory.path) as c:
            row = c.execute(
                "SELECT * FROM items WHERE id=? AND user=?",
                (observation, self.directory.user_scope),
            ).fetchone()
            payload = json.loads(row["payload"])
            event = c.execute(
                "SELECT node,stream,seq,digest,received FROM events WHERE observation=? AND user=? AND json_extract(payload,'$.turn.ordinal')=? AND json_extract(payload,'$.turn.turn_id') IS ? ORDER BY received DESC,seq DESC LIMIT 1",
                (
                    observation,
                    self.directory.user_scope,
                    payload["turn"]["ordinal"],
                    payload["turn"]["turn_id"],
                ),
            ).fetchone()
        if not event:
            return {"status": "PROVENANCE_UNAVAILABLE"}
        proof = dict(
            event,
            observation_id=observation,
            source_generation=payload["source_generation"],
            node_fingerprint=record.public_key_fingerprint,
        )
        with self.registry._connect() as c:
            deadline = time.monotonic() + 0.5
            c.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            c.execute("BEGIN IMMEDIATE")
            control = c.execute(
                "SELECT * FROM native_lifecycle_control WHERE id=1"
            ).fetchone()
            if control["paused"]:
                return {"status": "PAUSED"}
            old = c.execute(
                "SELECT * FROM native_task_bindings WHERE node_id=? AND provider=? AND thread_id=?",
                (node, provider, tid),
            ).fetchone()
            if (
                old
                and json.loads(old["first_provenance_json"]).get("node_fingerprint")
                != record.public_key_fingerprint
            ):
                raise NodeProtocolError(
                    "NATIVE_IDENTITY_CONFLICT",
                    "Physical node identity changed; explicit migration required",
                )
            if old and old["user_scope"] != item["user_scope"]:
                raise NodeProtocolError(
                    "NATIVE_IDENTITY_CONFLICT",
                    "Node thread belongs to another user scope",
                )
            # Existing routes/bindings are evidence; title/cwd similarity never merges tasks.
            owners = {
                r[0]
                for r in c.execute(
                    "SELECT DISTINCT b.task_id FROM conversation_bindings b JOIN tasks t USING(task_id) WHERE b.thread_id=? AND t.host=? LIMIT 2",
                    (tid, route["host"]),
                )
            }
            for table in ("executions", "execution_history"):
                owners.update(
                    r[0]
                    for r in c.execute(
                        "SELECT DISTINCT e.task_id FROM "
                        + table
                        + " e JOIN tasks t USING(task_id) WHERE json_extract(e.routing_identity_json,'$.conversation.binding')=? AND t.host=? LIMIT 2",
                        (tid, route["host"]),
                    )
                )
            for table in (
                "conversation_binding_lineage",
                "conversation_policy_lineage",
            ):
                owners.update(
                    r[0]
                    for r in c.execute(
                        "SELECT DISTINCT b.task_id FROM "
                        + table
                        + " b JOIN tasks t USING(task_id) WHERE (b.predecessor_thread=? OR b.successor_thread=?) AND t.host=? LIMIT 2",
                        (tid, tid, route["host"]),
                    )
                )
            if payload["task_ref"] and payload["task_ref"] not in owners:
                raise NodeProtocolError(
                    "CANONICAL_ROUTE_UNPROVEN",
                    "Source task claim requires a registry route",
                )
            if old:
                owners.add(old["task_id"])
            if len(owners) > 1:
                raise NodeProtocolError(
                    "NATIVE_IDENTITY_CONFLICT",
                    "Conflicting canonical owners; no automatic merge",
                )
            task_id = next(iter(owners), None)
            managed = (
                c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if task_id
                else None
            )
            if task_id and (
                not managed
                or managed["host"] != route["host"]
                or managed["cwd"] != item["project"]
            ):
                raise NodeProtocolError(
                    "NATIVE_IDENTITY_CONFLICT",
                    "Canonical owner and project route disagree",
                )
            if not task_id:
                admission = payload.get("native_admission", {})
                if (
                    item["source_client"] not in CLIENTS
                    or not (
                        admission.get("has_user_event") is True
                        or admission.get("user_message_observed") is True
                    )
                    or not payload["turn"]["turn_id"]
                ):
                    return {"status": "NOT_TASK_WORTHY"}
                historical = seconds(admission.get("created_at")) < control["since"]
                if historical and (not backfill or control["backfill_remaining"] <= 0):
                    return {"status": "HISTORY_REQUIRES_BACKFILL"}
                if not historical and control["auto_remaining"] <= 0:
                    return {"status": "AUTO_REGISTRATION_LIMIT"}
                if item["freshness"] == "OFFLINE":
                    return {"status": "NODE_OFFLINE"}
                task_id = canonical_native_id(node, provider, tid)
                stamp = _now()
                c.execute(
                    """INSERT INTO tasks(task_id,host,workspace_alias,project_alias,project_name,cwd,
                    title,status,created_at,updated_at,execution_state,current_stage,last_progress_at)
                    VALUES (?,?,?,?,?,?,?,'ACTIVE',?,?,'UNKNOWN','native_observed',?)""",
                    (
                        task_id,
                        route["host"],
                        project["workspace_alias"],
                        project["project_alias"],
                        project.get("project_name", project["project_alias"]),
                        item["project"],
                        item["title"],
                        stamp,
                        stamp,
                        stamp,
                    ),
                )
                if historical:
                    c.execute(
                        "UPDATE native_lifecycle_control SET backfill_remaining=backfill_remaining-1 WHERE id=1"
                    )
                else:
                    c.execute(
                        "UPDATE native_lifecycle_control SET auto_remaining=auto_remaining-1 WHERE id=1"
                    )
            origin = (
                old["origin"] if old else "CLINX_MANAGED" if managed else "CODEX_NATIVE"
            )
            ordinal = payload["turn"]["ordinal"]
            if not old:
                c.execute(
                    "INSERT INTO native_task_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        node,
                        provider,
                        tid,
                        item["user_scope"],
                        task_id,
                        route["host"],
                        item["project"],
                        origin,
                        compact(proof),
                        compact(proof),
                        ordinal,
                        row["received"],
                    ),
                )
            elif ordinal >= old["latest_ordinal"]:
                c.execute(
                    "UPDATE native_task_bindings SET latest_provenance_json=?,latest_ordinal=?,observed_at=? WHERE node_id=? AND provider=? AND thread_id=?",
                    (compact(proof), ordinal, row["received"], node, provider, tid),
                )
            # Observation never changes managed state, policy, binding or lease.
            has_managed_binding = c.execute(
                "SELECT 1 FROM conversation_bindings WHERE task_id=?", (task_id,)
            ).fetchone()
            if (
                origin == "CODEX_NATIVE"
                and not has_managed_binding
                and (not old or ordinal >= old["latest_ordinal"])
            ):
                state = payload["turn"]["native_state"]
                # Persisted RUNNING has no live authority. Directory projects fresh proof.
                state = "UNKNOWN" if state == "RUNNING" else state
                archived = payload.get("native_admission", {}).get("archived") is True
                status = (
                    "ARCHIVED"
                    if archived
                    else "COMPLETED"
                    if state == "COMPLETED"
                    else "ACTIVE"
                )
                c.execute(
                    "UPDATE tasks SET status=?,execution_state=?,current_stage='native_observed',turn_id=?,updated_at=?,codex_running=0 WHERE task_id=?",
                    (
                        status,
                        state,
                        payload["turn"]["turn_id"],
                        dt.datetime.fromtimestamp(
                            row["received"], dt.timezone.utc
                        ).isoformat(),
                        task_id,
                    ),
                )
            c.commit()
        identity = SessionIdentity(
            node_id=node,
            user_scope=item["user_scope"],
            provider=provider,
            native_thread_id=tid,
            source_version=payload["source_generation"],
        )
        self.directory.registry.update_thread(
            identity,
            source="canonical_lifecycle_v1",
            status={
                "task_ref": task_id,
                "source": origin,
                "observation_id": observation,
            },
        )
        return {
            "status": "REGISTERED",
            "task_ref": task_id,
            "source": origin,
            "execution_started": False,
        }

    def accepted(self, node, scope, events):
        from network_observation import observation_id

        results = []
        for tid in dict.fromkeys(
            e.get("native_thread_id") for e in events if e.get("native_thread_id")
        ):
            try:
                results.append(
                    self.reconcile(
                        observation_id(node, scope.user_scope, SUPPORTED_PROVIDER, tid)
                    )
                )
            except NodeProtocolError as exc:
                results.append(exc.as_dict())
            except sqlite3.OperationalError:
                results.append(
                    {
                        "error_code": "CANONICAL_RECONCILE_UNAVAILABLE",
                        "retry": "explicit bounded backfill or upload replay",
                    }
                )
        return results

    def backfill(self, *, limit=16):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Backfill page must be 1..100")
        control = self.control()
        if control["paused"] or control["backfill_remaining"] <= 0:
            return {"status": "PAUSED", "items": []}
        page = self.directory.list(
            cursor=control["backfill_cursor"],
            limit=min(limit, control["backfill_remaining"]),
        )
        results = [
            self.reconcile(item["observation_id"], backfill=True)
            for item in page["items"]
        ]
        with self.registry._connect() as c:
            c.execute(
                "UPDATE native_lifecycle_control SET backfill_cursor=? WHERE id=1",
                (page["next_cursor"],),
            )
            if not page["next_cursor"]:
                c.execute(
                    "UPDATE native_lifecycle_control SET backfill_remaining=0 WHERE id=1"
                )
        return {
            "status": "BOUNDED_PAGE",
            "items": results,
            "next_cursor": page["next_cursor"],
        }
