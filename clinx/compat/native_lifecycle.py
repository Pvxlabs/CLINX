"""Local device identity handoff and read-only legacy native associations.

Automatic Python Centre admission is retired. The configured local node adapter
still validates exact native identities before continuing its compatibility path.
"""

from __future__ import annotations

import json
import re
import uuid

from clinx.monitor.network_observation import compact
from clinx.nodes.node_protocol import SUPPORTED_PROVIDER, NodeProtocolError
from clinx.nodes.node_protocol import _now

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


from clinx.monitor.native_associations import binding as binding
