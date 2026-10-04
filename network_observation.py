"""Versioned durable observation projection. Never imports the execution plane.

Only the authenticated centre writer initializes this database. Readers use
mode=ro and recheck paired trust, both persisted approvals, user and project on
every response, including cached history and cursors.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
import secrets
import sqlite3
import time

from native_history import message, normalize_native_status
from node_protocol import NODE_PROTOCOL_VERSION, SUPPORTED_PROVIDER, NodeProtocolError, SharingScope

VERSION = "clinx-observation-v1"
MAX_BATCH = 32
MAX_ITEMS = 20000
MAX_TURNS = 50000
MAX_EVENTS = 50000
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT", "INTERRUPTED", "DISCONNECTED"}
COVERAGE = {"codex_desktop": "PERSISTED_INDEX", "codex_cli": "PERSISTED_INDEX",
            "clinx": "CANONICAL_READ_ONLY", "other_agents": "NOT_IMPLEMENTED",
            "ephemeral_unpublished": "UNOBSERVABLE"}


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def observation_id(node, user, provider, thread):
    return "obs_" + hashlib.sha256(compact([node, user, provider, thread]).encode()).hexdigest()[:40]


def clean(value, limit=1000):
    if value is None:
        return None
    if not isinstance(value, str):
        raise NodeProtocolError("INVALID_OBSERVATION", "Display text must be a string")
    value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)", "[REDACTED]", value)
    value = re.sub(r"(?i)\b(token|credential|password|secret)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", value)
    parsed = message({"type": "agentMessage", "text": value[:limit * 2]})
    return parsed[1][:limit] if parsed else ""


def bounded_id(value, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise NodeProtocolError("INVALID_OBSERVATION", "Invalid observation identity")
    return value


@contextlib.contextmanager
def database(path, *, write=False):
    path = Path(path).resolve()
    conn = sqlite3.connect(str(path) if write else path.as_uri() + "?mode=ro",
                           uri=not write, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        if not write:
            conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield conn
        if write:
            conn.commit()
    except Exception:
        if write:
            conn.rollback()
        raise
    finally:
        conn.close()


def validate_event(event, node, scope):
    """Copy only public fields. Arbitrary tool/reasoning payloads never persist."""
    if not isinstance(event, dict):
        raise NodeProtocolError("INVALID_OBSERVATION", "Event must be an object")
    thread = bounded_id(event.get("native_thread_id"))
    provider = event.get("provider", SUPPORTED_PROVIDER)
    project = event.get("project")
    if not isinstance(project, str) or len(project) > 1024 or not scope.allows_project(project):
        raise NodeProtocolError("PROJECT_SCOPE_DENIED", "Project is outside approved observation scope")
    if not scope.allows("observation.upload", provider):
        raise NodeProtocolError("SHARING_SCOPE_DENIED", "Observation scope denied")
    turn = event.get("turn") or {}
    if not isinstance(turn, dict):
        raise NodeProtocolError("INVALID_OBSERVATION", "Turn must be an object")
    ordinal = turn.get("ordinal", 0)
    if type(ordinal) is not int or ordinal < 0:
        raise NodeProtocolError("INVALID_OBSERVATION", "Invalid turn order")
    native_state = normalize_native_status(turn.get("native_state"))
    canonical = turn.get("execution_state")
    if canonical is not None and (not isinstance(canonical, str) or len(canonical) > 64):
        raise NodeProtocolError("INVALID_OBSERVATION", "Invalid canonical state")
    task = bounded_id(event.get("task_ref"), True)
    eref = bounded_id(turn.get("execution_ref"), True)
    if (task or eref) and event.get("binding_evidence") != "CANONICAL_ROUTE":
        raise NodeProtocolError("OBSERVATION_BINDING_UNPROVEN", "Canonical references require owning route evidence")
    result = turn.get("business_result")
    if result not in (None, "PASS", "BLOCKED"):
        raise NodeProtocolError("INVALID_OBSERVATION", "Invalid business result")
    if result is not None and not (task and eref):
        raise NodeProtocolError("OBSERVATION_BINDING_UNPROVEN", "Business result needs canonical execution")
    artifacts = turn.get("artifacts", [])
    if not isinstance(artifacts, list) or len(artifacts) > 16:
        raise NodeProtocolError("INVALID_OBSERVATION", "Artifact limit exceeded")
    return {
        "observation_id": observation_id(node, scope.user_scope, provider, thread),
        "node_id": node, "user_scope": scope.user_scope, "provider": provider,
        "native_thread_id": thread, "project": project,
        "title": clean((event.get("title") or thread).splitlines()[0], 100),
        "source": "CLINX_MANAGED" if task else "CODEX_NATIVE",
        "source_client": clean(event.get("source_client") or "UNKNOWN", 40),
        "task_ref": task, "binding_evidence": "CANONICAL_ROUTE" if task else "THREAD_UNBOUND",
        "source_generation": bounded_id(event.get("source_generation", "unknown")),
        "coverage": clean(event.get("coverage") or "PARTIAL", 80),
        "source_updated_at": clean(str(event.get("source_updated_at") or ""), 64),
        "turn": {
            "turn_id": bounded_id(turn.get("turn_id"), True), "ordinal": ordinal,
            "native_state": native_state, "execution_ref": eref,
            "execution_state": canonical, "business_result": result,
            "result_processing": clean(turn.get("result_processing") or "UNKNOWN", 80),
            "started_at": clean(str(turn.get("started_at") or ""), 64),
            "completed_at": clean(str(turn.get("completed_at") or ""), 64),
            "progress": clean(turn.get("progress"), 1500),
            "summary": clean(turn.get("summary"), 3000),
            "artifacts": [clean(v, 256) for v in artifacts],
        },
    }


class ObservationWriter:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript("""
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS streams(
                  node TEXT NOT NULL,user TEXT NOT NULL,stream TEXT NOT NULL,
                  seq INTEGER NOT NULL,source_generation TEXT,gap TEXT,received REAL NOT NULL,
                  PRIMARY KEY(node,user));
                CREATE TABLE IF NOT EXISTS items(
                  id TEXT PRIMARY KEY,node TEXT NOT NULL,user TEXT NOT NULL,project TEXT NOT NULL,
                  provider TEXT NOT NULL,thread TEXT NOT NULL,task TEXT,received REAL NOT NULL,
                  ordinal INTEGER NOT NULL,payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS observation_list ON items(user,id);
                CREATE TABLE IF NOT EXISTS turns(
                  observation TEXT NOT NULL,turn_key TEXT NOT NULL,ordinal INTEGER NOT NULL,
                  received REAL NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(observation,turn_key));
                CREATE INDEX IF NOT EXISTS observation_turns ON turns(observation,ordinal DESC,turn_key);
                CREATE TABLE IF NOT EXISTS events(
                  node TEXT NOT NULL,user TEXT NOT NULL,stream TEXT NOT NULL,seq INTEGER NOT NULL,
                  digest TEXT NOT NULL,received REAL NOT NULL,PRIMARY KEY(node,user,stream,seq));
            """)
            c.execute("INSERT OR IGNORE INTO settings VALUES ('cursor_secret',?)", (secrets.token_hex(32),))
            c.execute("INSERT OR IGNORE INTO settings VALUES ('retention_evicted','0')")
        os.chmod(self.path, 0o600)

    def accept(self, node, scope, request):
        stream = bounded_id(request.get("stream_id"))
        events = request.get("events")
        if not isinstance(events, list) or not 0 < len(events) <= MAX_BATCH:
            raise NodeProtocolError("INVALID_OBSERVATION_BATCH", "Batch must contain 1..32 events")
        if len(compact(events).encode()) > 180000:
            raise NodeProtocolError("INVALID_OBSERVATION_BATCH", "Batch exceeds byte budget")
        validated = []
        for event in events:
            seq = event.get("seq") if isinstance(event, dict) else None
            if type(seq) is not int or seq < 1:
                raise NodeProtocolError("INVALID_OBSERVATION_SEQUENCE", "Sequence must be positive")
            if event.get("scope_redacted") is True:
                if set(event) != {"seq","scope_redacted"}:
                    raise NodeProtocolError("INVALID_OBSERVATION","Redacted sequence cannot carry content")
                payload = None
            else:
                payload = validate_event(event, node, scope)
            validated.append((seq, payload, hashlib.sha256(compact(payload).encode()).hexdigest()))
        if [v[0] for v in validated] != sorted(set(v[0] for v in validated)):
            raise NodeProtocolError("OBSERVATION_SEQUENCE_ORDER", "Batch sequence must strictly increase")
        received = time.time()
        with database(self.path, write=True) as c:
            state = c.execute("SELECT * FROM streams WHERE node=? AND user=?", (node, scope.user_scope)).fetchone()
            seq = state["seq"] if state and state["stream"] == stream else 0
            gap = state["gap"] if state else None
            if state and state["stream"] != stream:
                # Explicit source rebuild requires the previous central stream CAS.
                if request.get("previous_stream_id") != state["stream"]:
                    raise NodeProtocolError("OBSERVATION_SOURCE_CONFLICT", "Stream replacement needs previous stream identity")
                gap = "SOURCE_REBUILT"
            expected = seq + 1
            for incoming, _, _ in validated:
                if incoming > seq:
                    if incoming != expected:
                        if state:
                            c.execute("UPDATE streams SET gap=? WHERE node=? AND user=?", ("SEQUENCE_GAP",node,scope.user_scope))
                        return {"accepted": False, "error_code": "OBSERVATION_GAP", "expected_seq": expected,
                                "ack_seq": seq, "stream_id": stream}
                    expected += 1
            for incoming, payload, digest in validated:
                if incoming <= seq:
                    old = c.execute("SELECT digest FROM events WHERE node=? AND user=? AND stream=? AND seq=?",
                                    (node, scope.user_scope, stream, incoming)).fetchone()
                    if old and old[0] != digest:
                        raise NodeProtocolError("OBSERVATION_SEQUENCE_CONFLICT", "Same sequence has different content")
                    continue
                if incoming != seq + 1:
                    return {"accepted": False, "error_code": "OBSERVATION_GAP", "expected_seq": seq + 1,
                            "ack_seq": seq, "stream_id": stream}
                if payload is None:
                    c.execute("INSERT INTO events VALUES (?,?,?,?,?,?)", (node,scope.user_scope,stream,incoming,digest,received))
                    seq=incoming
                    gap="SCOPE_WITHHELD"
                    continue
                oid, turn = payload["observation_id"], payload["turn"]
                previous = c.execute("SELECT * FROM items WHERE id=?", (oid,)).fetchone()
                if previous:
                    prior = json.loads(previous["payload"])
                    if prior["project"] != payload["project"] or (prior["task_ref"] and payload["task_ref"] and prior["task_ref"] != payload["task_ref"]):
                        raise NodeProtocolError("OBSERVATION_IDENTITY_CONFLICT", "Thread association changed")
                    if prior["source_generation"] != payload["source_generation"]:
                        gap = "SOURCE_REBUILT"
                    # A native-only update cannot erase a proven canonical binding.
                    if prior["task_ref"] and not payload["task_ref"]:
                        payload.update(task_ref=prior["task_ref"], source="CLINX_MANAGED", binding_evidence="CANONICAL_ROUTE")
                key = turn["turn_id"] or turn["execution_ref"] or "metadata"
                old_turn = c.execute("SELECT payload FROM turns WHERE observation=? AND turn_key=?", (oid, key)).fetchone()
                if old_turn:
                    old = json.loads(old_turn[0])
                    if old["execution_ref"] and turn["execution_ref"] and old["execution_ref"] != turn["execution_ref"]:
                        raise NodeProtocolError("OBSERVATION_IDENTITY_CONFLICT","Same native turn has conflicting canonical owners")
                    if old["native_state"] in TERMINAL and turn["native_state"] not in TERMINAL:
                        turn["native_state"] = old["native_state"]
                        turn["completed_at"] = old["completed_at"]
                    for field in ("business_result", "summary", "execution_ref", "execution_state"):
                        if old[field] and not turn[field]:
                            turn[field] = old[field]
                c.execute("INSERT OR REPLACE INTO turns VALUES (?,?,?,?,?)",
                          (oid, key, turn["ordinal"], received, compact(turn)))
                if previous and previous["ordinal"] > turn["ordinal"]:
                    payload["turn"] = json.loads(previous["payload"])["turn"]
                c.execute("INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (oid,node,scope.user_scope,payload["project"],payload["provider"],payload["native_thread_id"],
                           payload["task_ref"],received,payload["turn"]["ordinal"],compact(payload)))
                c.execute("INSERT INTO events VALUES (?,?,?,?,?,?)", (node,scope.user_scope,stream,incoming,digest,received))
                seq = incoming
            c.execute("INSERT OR REPLACE INTO streams VALUES (?,?,?,?,?,?,?)",
                      (node,scope.user_scope,stream,seq,next((p["source_generation"] for _,p,_ in reversed(validated) if p), state["source_generation"] if state else "redacted"),gap,received))
            for table, maximum, order in (("items",MAX_ITEMS,"received"),("turns",MAX_TURNS,"received"),("events",MAX_EVENTS,"received")):
                count = c.execute("SELECT count(*) FROM " + table).fetchone()[0]
                extra = max(0, count-maximum)
                if extra:
                    c.execute(f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} ORDER BY {order} LIMIT ?)", (extra,))
                    c.execute("UPDATE settings SET value=CAST(value AS INTEGER)+? WHERE key='retention_evicted'", (extra,))
            c.execute("DELETE FROM turns WHERE observation NOT IN (SELECT id FROM items)")
        return {"accepted": True,"ack_seq": seq,"stream_id": stream,"received_at": received,"gap": gap}


class ObservationDirectory:
    """Shared Observer/MCP service; no canonical status or migration calls."""
    def __init__(self, path, registry, approvals, peers, user_scope, remote_factory=None):
        self.path, self.registry, self.approvals, self.peers = Path(path), registry, approvals, peers
        self.user_scope, self.remote_factory = user_scope, remote_factory

    def _scope(self, node):
        from node_runtime import intersect_scope
        peer = self.peers.all().get(node)
        record = self.registry.get(node)
        if not peer or peer.get("trust_state") != "TRUSTED" or peer.get("security_status") != "OPAQUE_V1" or not record or not record.authorized or not record.trusted or not record.version_compatible:
            return None
        scope = intersect_scope(self.registry.scope(node,self.user_scope), self.approvals.get(node))
        return scope if scope.user_scope == self.user_scope and scope.allows("session.read") else None

    def _allowed(self, payload):
        scope = self._scope(payload["node_id"])
        return scope and scope.allows_project(payload["project"]) and scope.allows("session.read",payload["provider"])

    def _cursor(self,c,context,position=None,cursor=None):
        secret = c.execute("SELECT value FROM settings WHERE key='cursor_secret'").fetchone()[0].encode()
        # Approval fingerprints bind pagination to current grants, not only filter strings.
        approval = hashlib.sha256(compact({n.node_id: self._scope(n.node_id).as_dict() if self._scope(n.node_id) else None
                           for n in self.registry.list_nodes(user_scope=self.user_scope)}).encode()).hexdigest()
        generations=[tuple(r) for r in c.execute("SELECT node,stream,source_generation FROM streams WHERE user=? ORDER BY node",(self.user_scope,))]
        retention=c.execute("SELECT value FROM settings WHERE key='retention_evicted'").fetchone()[0]
        generation=hashlib.sha256(compact([generations,retention]).encode()).hexdigest()
        scope = [VERSION,self.user_scope,approval,generation,hashlib.sha256(compact(context).encode()).hexdigest()]
        if cursor is not None:
            try:
                if not isinstance(cursor,str) or len(cursor)>2048: raise ValueError()
                raw,signature = cursor.split(".")
                data=base64.urlsafe_b64decode(raw+"="*(-len(raw)%4))
                if not hmac.compare_digest(hmac.new(secret,data,hashlib.sha256).hexdigest(),signature):
                    raise ValueError()
                decoded=json.loads(data)
                if decoded["scope"] != scope or decoded["expires"] < time.time():
                    raise ValueError()
                return decoded["position"]
            except (ValueError,TypeError,KeyError):
                raise NodeProtocolError("INVALID_OBSERVATION_CURSOR","Cursor does not match current user, scope, filter or history") from None
        data=compact({"scope":scope,"position":position,"expires":time.time()+3600}).encode()
        return base64.urlsafe_b64encode(data).decode().rstrip("=")+"."+hmac.new(secret,data,hashlib.sha256).hexdigest()

    def _project(self,payload,received):
        record=self.registry.get(payload["node_id"])
        scope=self._scope(payload["node_id"])
        available=bool(record and not record.stale and record.state=="ONLINE")
        turn=payload["turn"]
        enabled=bool(available and scope and scope.execute_tasks and payload["task_ref"]
                     and "execution.prepare" in record.capabilities)
        reason=("NODE_OFFLINE" if not available else "READ_SESSIONS_ONLY" if not scope or not scope.execute_tasks
                else "PROVIDER_CONTROL_UNAVAILABLE" if "execution.prepare" not in record.capabilities
                else "EXPLICIT_ADOPTION_REQUIRED" if not payload["task_ref"] else "CANONICAL_AUTHORIZATION_REQUIRED")
        return dict(payload,schema_version=VERSION,received_at=received,device_name=record.display_name if record else payload["node_id"],
            freshness="RECENT" if available and time.time()-received<30 else "STALE" if available else "OFFLINE",
            liveness="NOT_PROVEN",progress_percent=None,
            control={"node_id":payload["node_id"],"native_thread_id":payload["native_thread_id"],
                "task_ref":payload["task_ref"],"execution_ref":turn["execution_ref"],
                "entrypoint":"clinx_prepare_execution" if payload["task_ref"] else "clinx_adopt_conversation",
                "enabled":enabled,
                "reason":reason,
                "allowed_actions":[]})  # Observer offers a mapping; writes remain on canonical plane.

    def list(self, *, node=None, project=None, state=None, kind=None, cursor=None, limit=50):
        if type(limit) is not int or not 1<=limit<=100 or kind not in (None,"managed","external"):
            raise NodeProtocolError("INVALID_OBSERVATION_QUERY","Invalid pagination or kind")
        filters=[node,project,state,kind]
        with database(self.path) as c:
            after=self._cursor(c,filters,cursor=cursor) if cursor else ""
            if not isinstance(after,str):
                raise NodeProtocolError("INVALID_OBSERVATION_CURSOR","Invalid position")
            result=[]; scanned=0; last=after
            # Work and response bounds remain explicit even with sparse authorized rows.
            rows=c.execute("SELECT * FROM items WHERE user=? AND id>? ORDER BY id LIMIT 501",(self.user_scope,after)).fetchall()
            for row in rows[:500]:
                if len(result)>=limit: break
                scanned+=1;last=row["id"];payload=json.loads(row["payload"])
                if not self._allowed(payload): continue
                if node and payload["node_id"]!=node: continue
                if project and payload["project"]!=project: continue
                if state and state not in (payload["turn"]["native_state"],payload["turn"]["execution_state"]): continue
                if kind and bool(payload["task_ref"])!=(kind=="managed"): continue
                result.append(self._project(payload,row["received"]))
            more=len(rows)>scanned
            evicted=int(c.execute("SELECT value FROM settings WHERE key='retention_evicted'").fetchone()[0])
            sources=[{"node_id":r["node"],"stream_id":r["stream"],"ack_seq":r["seq"],
                      "source_generation":r["source_generation"],"gap":r["gap"],"last_upload_at":r["received"]}
                     for r in c.execute("SELECT * FROM streams WHERE user=? ORDER BY node",(self.user_scope,))
                     if self._scope(r["node"])]
            return {"schema_version":VERSION,"read_only":True,"items":result,"sources":sources,
                "next_cursor":self._cursor(c,filters,last) if more else None,"has_more":more,
                "coverage":COVERAGE,"retention_evicted":evicted,"observed_at":time.time()}

    def detail(self, observation_id, *, cursor=None, limit=32):
        if type(limit) is not int or not 1<=limit<=100:
            raise NodeProtocolError("INVALID_OBSERVATION_QUERY","Invalid page size")
        with database(self.path) as c:
            row=c.execute("SELECT * FROM items WHERE id=? AND user=?",(observation_id,self.user_scope)).fetchone()
            if not row or not self._allowed(json.loads(row["payload"])):
                raise NodeProtocolError("OBSERVATION_NOT_FOUND","Observation unavailable in current approved scope")
            context=["history",observation_id]
            before=self._cursor(c,context,cursor=cursor) if cursor else [9223372036854775807,"\uffff"]
            if not isinstance(before,list) or len(before)!=2:
                raise NodeProtocolError("INVALID_OBSERVATION_CURSOR","Invalid history position")
            turns=c.execute("SELECT * FROM turns WHERE observation=? AND (ordinal,turn_key)<(?,?) ORDER BY ordinal DESC,turn_key DESC LIMIT ?",
                            (observation_id,*before,limit+1)).fetchall()
            page=turns[:limit];more=len(turns)>limit
            payload=json.loads(row["payload"])
            stream=c.execute("SELECT * FROM streams WHERE node=? AND user=?",(payload["node_id"],self.user_scope)).fetchone()
            return {"schema_version":VERSION,"item":self._project(payload,row["received"]),
                "turns":[json.loads(t["payload"]) for t in page],"has_more":more,
                "next_cursor":self._cursor(c,context,[page[-1]["ordinal"],page[-1]["turn_key"]]) if more else None,
                "gap":stream["gap"] if stream else "UNKNOWN","content_status":"CACHED_PUBLIC_SUMMARIES",
                "read_only":True}

    def context(self, observation_id, *, cursor=None):
        item=self.detail(observation_id,limit=1)["item"]
        if not self.remote_factory or item["freshness"]=="OFFLINE":
            return {"schema_version":VERSION,"context_status":"UNCACHED_CONTENT_UNAVAILABLE","read_only":True}
        record=self.registry.get(item["node_id"])
        result=self.remote_factory(record).call({"operation":"session.read","protocol_version":NODE_PROTOCOL_VERSION,
            "user_scope":self.user_scope,"provider":item["provider"],"thread_id":item["native_thread_id"],
            "cursor":cursor,"recent_turns":4,"max_bytes":16000})
        return dict(result,schema_version=VERSION)


def directory_from_env():
    """Explicit shared configuration for both formal server entrypoints."""
    from node_protocol import NodeRegistry,NodeAuthorizationStore
    from local_discovery.identity import PrivateStore,TrustedPeerStore
    from node_runtime import remote_client_factory
    path=os.environ.get("CLINX_OBSERVATION_DB")
    if not path: return None
    state=Path(os.environ["CLINX_OBSERVATION_STATE"])
    registry=NodeRegistryReadOnly(os.environ["CLINX_OBSERVATION_REGISTRY"])
    peers=TrustedPeerStore(PrivateStore(state))
    return ObservationDirectory(path,registry,NodeAuthorizationStore(state),peers,
        os.environ["CLINX_OBSERVATION_USER_SCOPE"],
        remote_client_factory(state,os.environ["CLINX_OBSERVATION_NODE_ID"],registry))


class NodeRegistryReadOnly:
    """Reuse registry decoding without invoking its initializing constructor."""
    def __init__(self,path): self.path=Path(path)
    def get(self,node):
        from node_protocol import NodeRegistry
        with database(self.path) as c:
            row=c.execute("SELECT * FROM nodes WHERE node_id=?",(node,)).fetchone()
            return NodeRegistry._record(self,row) if row else None
    def list_nodes(self,*,user_scope=None):
        with database(self.path) as c:
            ids=[r[0] for r in c.execute("SELECT node_id FROM nodes WHERE user_scope=? ORDER BY node_id",(user_scope,))]
        return [self.get(n) for n in ids]
    def scope(self,node,user):
        with database(self.path) as c:
            row=c.execute("SELECT * FROM sharing_scopes WHERE node_id=? AND user_scope=?",(node,user)).fetchone()
            if not row: return None
            projects=c.execute("SELECT projects_json FROM observation_projects WHERE node_id=? AND user_scope=?",(node,user)).fetchone()
        return SharingScope(user_scope=user,read_sessions=bool(row["read_sessions"]),execute_tasks=bool(row["execute_tasks"]),
            providers=tuple(json.loads(row["providers_json"])),projects=tuple(json.loads(projects[0])) if projects else ("*",),
            revoked=bool(row["revoked"]),updated_at=row["updated_at"])
