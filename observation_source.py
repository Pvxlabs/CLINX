"""One bounded node collector, durable outbox and checkpoints; no execution APIs."""
from __future__ import annotations
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from native_history import NativeHistory, ThreadLookupError, message, normalize_native_status, readonly, _budget_sql
from network_observation import compact, clean, MAX_BATCH, TERMINAL
from node_protocol import NODE_PROTOCOL_VERSION, SUPPORTED_PROVIDER, NodeProtocolError

PAGE = 16
OUTBOX_LIMIT = 2048
SESSION_LIMIT = 20000


def order(value, fallback=0):
    try:
        if isinstance(value,(int,float)): return int(value * (1000 if value<100000000000 else 1))
        return int(dt.datetime.fromisoformat(str(value).replace("Z","+00:00")).timestamp()*1000)
    except (ValueError,TypeError,OverflowError):
        return fallback


class CodexObservationSource:
    provider = SUPPORTED_PROVIDER
    def __init__(self, root, node_id, canonical_db=None, owner_host=None):
        self.history=NativeHistory(root)
        self.node_id=node_id
        self.canonical_db=Path(canonical_db) if canonical_db else None
        self.owner_host=owner_host or node_id

    @property
    def generation(self):
        paths=[self.history.root/"state_5.sqlite"]
        if self.canonical_db: paths.append(self.canonical_db)
        return hashlib.sha256(compact([(str(p),p.stat().st_dev,p.stat().st_ino) for p in paths if p.exists()]).encode()).hexdigest()[:32]

    def inventory(self,scope,after=None,*,audit=False):
        path=self.history.root/"state_5.sqlite"
        if not path.exists(): return [],None,"NATIVE_INDEX_UNAVAILABLE"
        try:
            with readonly(path) as c:
                _budget_sql(c)
                columns={r[1] for r in c.execute("PRAGMA table_info(threads)")}
                if not {"id","cwd","updated_at","rollout_path"}<=columns:
                    return [],None,"NATIVE_SCHEMA_UNSUPPORTED"
                where=[];args=[]
                if "*" not in scope.projects:
                    if not scope.projects: return [],None,"SCOPE_EMPTY"
                    where.append("cwd IN ("+",".join("?" for _ in scope.projects)+")");args.extend(scope.projects)
                if after:
                    where.append("id>?" if audit else "(updated_at,id)>(?,?)")
                    args.extend([after] if audit else after)
                fields=["id","cwd","rollout_path","updated_at"]+[("substr("+k+",1,256) AS "+k if k in ("title","name") else k)
                    for k in ("history_mode","title","name","source","created_at") if k in columns]
                rows=[dict(r) for r in c.execute("SELECT "+",".join(fields)+" FROM threads"+
                    (" WHERE "+" AND ".join(where) if where else "")+
                    (" ORDER BY id" if audit else " ORDER BY updated_at,id")+" LIMIT ?",(*args,PAGE))]
                last=(rows[-1]["id"] if audit else [rows[-1]["updated_at"],rows[-1]["id"]]) if rows else None
                return rows,last,"INDEXED"
        except sqlite3.Error:
            return [],None,"NATIVE_INDEX_UNAVAILABLE"

    def canonical_inventory(self,scope,after=""):
        if not self.canonical_db or not self.canonical_db.exists(): return []
        with readonly(self.canonical_db) as c:
            _budget_sql(c)
            # Binding is discovery metadata only. Each execution is subsequently
            # checked against its immutable owning route, never the frontend host.
            rows=c.execute("SELECT b.thread_id,t.cwd,t.title,t.updated_at FROM conversation_bindings b "
                "JOIN tasks t ON t.task_id=b.task_id WHERE t.host=? AND b.thread_id>? ORDER BY b.thread_id LIMIT ?",
                (self.owner_host,after,PAGE)).fetchall()
            return [{"id":r["thread_id"],"cwd":r["cwd"],"title":r["title"],"updated_at":r["updated_at"],
                     "history_mode":"canonical","rollout_path":""} for r in rows if scope.allows_project(r["cwd"])]

    def canonical_turns(self,tid,project,state):
        if not self.canonical_db or not self.canonical_db.exists(): return []
        turns=[]
        with readonly(self.canonical_db) as c:
            _budget_sql(c)
            for table in ("executions","execution_history"):
                base = ("SELECT e.*,t.cwd,t.host FROM "+table+" e JOIN tasks t ON t.task_id=e.task_id "
                    "WHERE json_extract(e.routing_identity_json,'$.conversation.binding')=?")
                recent = c.execute(base+" ORDER BY e.acquired_at DESC,e.execution_ref DESC LIMIT 4",(tid,)).fetchall()
                before = state.get("canonical_before_"+table,["9999","~"])
                older = c.execute(base+" AND (e.acquired_at,e.execution_ref)<(?,?) "
                    "ORDER BY e.acquired_at DESC,e.execution_ref DESC LIMIT 4",(tid,*before)).fetchall()
                if older:
                    state["canonical_before_"+table]=[older[-1]["acquired_at"],older[-1]["execution_ref"]]
                state["canonical_complete_"+table]=len(older)<4
                rows={r["execution_ref"]:r for r in recent+older}.values()
                for row in rows:
                    route=json.loads(row["routing_identity_json"])
                    if row["cwd"]!=project or route.get("host",{}).get("stable_identifier")!=self.owner_host:
                        continue
                    result=c.execute("SELECT status,summary,changed_files FROM execution_results WHERE execution_ref=? AND task_id=? AND turn_id=?",
                        (row["execution_ref"],row["task_id"],row["turn_id"])).fetchone()
                    turns.append({"task_ref":row["task_id"],"turn_id":row["turn_id"],"execution_ref":row["execution_ref"],
                        "ordinal":order(row["acquired_at"]),"execution_state":row["execution_state"],
                        "native_state":"UNKNOWN","result_processing":row["failure_code"] or ("RECEIVED" if result else "PENDING"),
                        "business_result":result["status"] if result else None,
                        "summary":result["summary"] if result else None,
                        "artifacts":[v.strip() for v in result["changed_files"].split(",") if v.strip() and v.strip() != "NONE"][:16] if result and result["changed_files"] else [],
                        "started_at":row["acquired_at"],"completed_at":dict(row).get("released_at")})
        return turns

    def sample(self,metadata,state):
        tid=metadata["id"];turns=[];coverage="INDEXED"
        if metadata.get("history_mode")=="paginated":
            try:
                with readonly(self.history.root/"thread_history_1.sqlite") as c:
                    _budget_sql(c)
                    fields="turn_id,rollout_ordinal,status,started_at,completed_at"
                    recent=[dict(r) for r in c.execute("SELECT "+fields+" FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 4",(tid,))]
                    if state.get("history_complete"):
                        state["history_before"]=9223372036854775807
                    before=state.get("history_before",9223372036854775807)
                    older=[dict(r) for r in c.execute("SELECT "+fields+" FROM thread_turns WHERE thread_id=? AND rollout_ordinal<? ORDER BY rollout_ordinal DESC LIMIT 4",(tid,before))]
                    pending=state.get("active_turns",[])[:64]
                    active=[dict(r) for r in c.execute("SELECT "+fields+" FROM thread_turns WHERE thread_id=? AND turn_id IN ("+
                        ",".join("?" for _ in pending)+")",(tid,*pending))] if pending else []
                if older: state["history_before"]=older[-1]["rollout_ordinal"]
                state["history_complete"]=len(older)<4
                selected={r["turn_id"]:r for r in recent+older+active}
                state["active_turns"]=[r["turn_id"] for r in selected.values() if normalize_native_status(r["status"])=="RUNNING"][:64]
                for r in selected.values():
                    context=self.history.context(tid,self.node_id,metadata,1,4500,anchor=r["turn_id"])
                    turns.append({"turn_id":r["turn_id"],"ordinal":order(r["started_at"],r["rollout_ordinal"]),
                        "native_state":normalize_native_status(r["status"]),"started_at":r["started_at"],
                        "completed_at":r["completed_at"],"summary":context.get("last_codex_result"),
                        "progress":context.get("last_codex_result") if normalize_native_status(r["status"])=="RUNNING" else None,
                        "result_processing":"UNMANAGED"})
                if not state.get("history_complete"): coverage="HISTORY_BACKFILL"
            except (ThreadLookupError,sqlite3.Error,OSError):
                coverage="NATIVE_HISTORY_UNAVAILABLE"
        elif metadata.get("history_mode")!="canonical":
            turns,coverage=self._legacy(metadata,state)
        canonical=self.canonical_turns(tid,metadata["cwd"],state)
        native={r["turn_id"]:r for r in turns if r.get("turn_id")}
        for turn in canonical:
            if turn["turn_id"] in native:
                original=native[turn["turn_id"]]
                turn["native_state"]=original["native_state"]
                turn["ordinal"]=original["ordinal"]
                if not turn.get("summary"): turn["summary"]=original.get("summary")
                turns.remove(original)
        turns+=canonical
        if self.canonical_db and not all(state.get("canonical_complete_"+table,True) for table in ("executions","execution_history")):
            coverage="HISTORY_BACKFILL"
        if not turns: turns=[{"turn_id":None,"ordinal":0,"native_state":"UNKNOWN"}]
        state["active"]=any(t.get("native_state")=="RUNNING" or t.get("execution_state") in ("CODEX_RUNNING","FINALIZING","TURN_STARTED") for t in turns)
        task_refs={t["task_ref"] for t in canonical}
        if len(task_refs)>1:
            raise NodeProtocolError("OBSERVATION_IDENTITY_CONFLICT","Native thread has multiple canonical task owners")
        task=next(iter(task_refs),None)
        return [dict(native_thread_id=tid,provider=self.provider,project=metadata["cwd"],
            title=metadata.get("name") or metadata.get("title") or tid,source_client=metadata.get("source","UNKNOWN"),
            source_generation=self.generation,source_updated_at=metadata.get("updated_at"),
            coverage=coverage,task_ref=task,binding_evidence="CANONICAL_ROUTE" if task else "THREAD_UNBOUND",
            turn={k:v for k,v in turn.items() if k!="task_ref"}) for turn in turns],state

    def _legacy(self,metadata,state):
        """Incremental allowlist parser; never collects reasoning/tool output."""
        tid=metadata["id"];path=self.history._path(tid,metadata)
        if not path.exists(): return [],"NATIVE_HISTORY_UNAVAILABLE"
        stat=path.stat();fileid=[stat.st_dev,stat.st_ino]
        offset=state.get("offset",0)
        gap=None
        if state.get("file_id") not in (None,fileid) or offset>stat.st_size:
            offset=0;state.pop("current",None);gap="SOURCE_REBUILT"
        with path.open("rb") as f:
            f.seek(offset);data=f.read(65536)
        if not data:
            return [state["current"]] if state.get("current") else [],"INDEXED"
        end=data.rfind(b"\n")
        if end<0:
            if len(data)==65536:
                state.update(offset=offset+len(data),file_id=fileid)
                return [],"OVERSIZED_RECORD_GAP"
            return [state["current"]] if state.get("current") else [],"PARTIAL_RECORD"
        data=data[:end+1];turns={};current=state.get("current")
        for line in data.splitlines():
            try: row=json.loads(line)
            except (ValueError,UnicodeError): gap="MALFORMED_RECORD_GAP";continue
            payload=row.get("payload",{})
            if not isinstance(payload,dict): continue
            if row.get("type")=="session_meta" and (payload.get("id")!=tid or payload.get("cwd")!=metadata["cwd"]):
                raise NodeProtocolError("NATIVE_IDENTITY_CONFLICT","Native header disagrees with index")
            kind=payload.get("type")
            turn_id=payload.get("turn_id")
            if row.get("type")=="turn_context" or (row.get("type")=="event_msg" and kind=="task_started"):
                if current: turns[current["turn_id"]]=dict(current)
                current={"turn_id":turn_id,"ordinal":order(row.get("timestamp"),offset),
                         "native_state":"RUNNING","started_at":row.get("timestamp"),"result_processing":"UNMANAGED"}
            if not current or not current.get("turn_id"): continue
            if row.get("type")=="event_msg" and kind in ("task_complete","turn_aborted"):
                if turn_id and turn_id!=current["turn_id"]: continue
                current.update(native_state="COMPLETED" if kind=="task_complete" else "INTERRUPTED",completed_at=row.get("timestamp"))
                if kind=="task_complete" and isinstance(payload.get("last_agent_message"),str):
                    current["summary"]=clean(payload["last_agent_message"],3000)
            if row.get("type")=="response_item":
                parsed=message(payload)
                if parsed and parsed[0]=="assistant":
                    current["summary"]=clean(parsed[1],3000)
                    current["progress"]=clean(parsed[1],1500)
            turns[current["turn_id"]]=dict(current)
        state.update(offset=offset+len(data),file_id=fileid,current=current)
        return list(turns.values()),gap or ("HISTORY_BACKFILL" if state["offset"]<stat.st_size else "INDEXED")


class ObservationCollector:
    """Own one node-wide file lock; UI clients only read the centre."""
    def __init__(self,path,source,identity,registration,scope,port,capabilities,previous_stream_id=None):
        self.path=Path(path);self.source=source;self.identity=identity;self.registration=registration
        self.scope=scope;self.port=port;self.capabilities=capabilities
        self.previous_stream_id=previous_stream_id
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=self.path.with_suffix(".lock").open("a")
        try: fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise NodeProtocolError("COLLECTOR_ALREADY_RUNNING","Only one collector may read this node source") from None
        self.conn=sqlite3.connect(self.path)
        self.conn.row_factory=sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,metadata TEXT NOT NULL,state TEXT NOT NULL,checked REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox(seq INTEGER PRIMARY KEY,payload TEXT NOT NULL);
        """)
        self.conn.execute("INSERT OR IGNORE INTO meta VALUES ('stream',?)",(str(uuid.uuid4()),))
        self.conn.execute("INSERT OR IGNORE INTO meta VALUES ('seq','0')")
        self.conn.commit();os.chmod(self.path,0o600)
        self.status="BOOTSTRAP";self.sampled=0

    def get(self,key,default=None):
        row=self.conn.execute("SELECT value FROM meta WHERE key=?",(key,)).fetchone()
        return json.loads(row[0]) if row and key not in ("stream",) else row[0] if row else default

    def put(self,key,value):
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",(key,compact(value)))

    def tick(self):
        scope=self.scope()
        if scope is None or not scope.allows("observation.upload"):
            stored=self.get("effective_scope")
            denied=scope
            if denied is None and stored:
                from node_protocol import SharingScope
                denied=SharingScope(user_scope=stored["user_scope"],revoked=True)
            if denied is not None:
                try: self.registration.heartbeat(self.identity,denied,port=self.port,capabilities=tuple(self.capabilities))
                except (OSError,TimeoutError): pass
            self.status="SHARING_SCOPE_DENIED";return 2
        # Record while disconnected only within the last persisted bilateral
        # approval, intersected with the current local grant. No first-use guess.
        from node_runtime import intersect_scope
        from node_protocol import SharingScope
        online = False
        try:
            reply=self.registration.heartbeat(self.identity,scope,port=self.port,capabilities=tuple(self.capabilities))
            if not reply.get("registered"):
                with self.conn: self.put("effective_scope",None)
                self.status=reply.get("error_code","OFFLINE");return 12
            effective=SharingScope(**reply["scope"])
            with self.conn: self.put("effective_scope",effective.as_dict())
            online=True
        except (OSError,TimeoutError):
            stored=self.get("effective_scope")
            if not stored: self.status="AWAITING_BILATERAL_APPROVAL";return 12
            effective=SharingScope(**stored)
        scope=intersect_scope(scope,effective)
        if not scope.allows("observation.upload"): self.status="SHARING_SCOPE_DENIED";return 12
        candidates_outbox=self.conn.execute("SELECT * FROM outbox ORDER BY seq LIMIT ?",(MAX_BATCH,)).fetchall()
        queued=[]; batch_bytes=0
        for row in candidates_outbox:
            size=len(row["payload"].encode())+64
            if queued and batch_bytes+size>150000: break
            queued.append(row);batch_bytes+=size
        if queued and online:
            upload_events=[]
            for row in queued:
                payload=json.loads(row["payload"])
                upload_events.append(dict(payload,seq=row["seq"]) if scope.allows_project(payload["project"])
                                     else {"seq":row["seq"],"scope_redacted":True})
            try:
                response=self.registration.upload(self.identity,scope,
                    stream_id=self.get("stream"),events=upload_events,previous_stream_id=self.previous_stream_id)
                if not response.get("accepted"):
                    self.status=response.get("error_code","UPLOAD_UNAVAILABLE");return 2
                with self.conn:
                    self.conn.execute("DELETE FROM outbox WHERE seq<=?",(response["ack_seq"],))
            except (OSError,TimeoutError):
                online=False
        count=self.conn.execute("SELECT count(*) FROM outbox").fetchone()[0]
        if count>=OUTBOX_LIMIT-128:
            self.status="OBSERVATION_BACKPRESSURE";return 2
        generation=self.source.generation
        with self.conn:
            if self.get("generation")!=generation:
                self.put("index_after",None);self.put("audit_after",None);self.put("generation",generation)
                self.conn.execute("UPDATE sessions SET state='{}'")
                self.status="SOURCE_REBUILT"
            metadata,after,coverage=self.source.inventory(scope,self.get("index_after"))
            if after: self.put("index_after",after)
            else:
                previous=self.get("index_after")
                # Overlap catches timestamp ties/late updates; rotating audit covers older ones.
                self.put("index_after",[max(0,previous[0]-60),""] if previous and isinstance(previous[0],(int,float)) else None)
            audit,audit_after,_=self.source.inventory(scope,self.get("audit_after"),audit=True)
            self.put("audit_after",audit_after)
            managed=self.source.canonical_inventory(scope,self.get("canonical_after",""))
            self.put("canonical_after",managed[-1]["id"] if managed else "")
            candidates={m["id"]:m for m in managed+audit+metadata}
            # Round-robin ongoing/backfill reads are independent of index timestamp.
            for r in self.conn.execute("SELECT metadata FROM sessions ORDER BY coalesce(json_extract(state,\'$.active\'),0) DESC,checked LIMIT 8"):
                m=json.loads(r[0])
                if scope.allows_project(m["cwd"]): candidates.setdefault(m["id"],m)
            for tid,m in list(candidates.items())[:PAGE*3+8]:
                if self.conn.execute("SELECT count(*) FROM outbox").fetchone()[0]>=OUTBOX_LIMIT-128:
                    self.status="OBSERVATION_BACKPRESSURE"
                    break
                if not scope.allows_project(m["cwd"]): continue
                existing=self.conn.execute("SELECT state FROM sessions WHERE id=?",(tid,)).fetchone()
                if not existing and self.conn.execute("SELECT count(*) FROM sessions").fetchone()[0]>=SESSION_LIMIT:
                    self.status="SESSION_RETENTION_LIMIT";return 2
                state=json.loads(existing[0]) if existing else {}
                events,state=self.source.sample(m,state)
                hashes=state.get("hashes",{})
                for event in events:
                    # title and summaries are scrubbed before entering the durable outbox.
                    event["title"]=clean(event.get("title"),100) or tid
                    for field in ("summary","progress"):
                        if event["turn"].get(field): event["turn"][field]=clean(event["turn"][field],3000 if field=="summary" else 1500)
                    key=event["turn"].get("execution_ref") or event["turn"].get("turn_id") or "metadata"
                    digest=hashlib.sha256(compact(event).encode()).hexdigest()
                    if hashes.get(key)==digest: continue
                    if self.conn.execute("SELECT count(*) FROM outbox").fetchone()[0] >= OUTBOX_LIMIT:
                        raise NodeProtocolError("OBSERVATION_BACKPRESSURE","Outbox full; source checkpoints remain uncommitted")
                    seq=self.get("seq",0)+1
                    self.conn.execute("INSERT INTO outbox VALUES (?,?)",(seq,compact(event)))
                    self.put("seq",seq);hashes[key]=digest
                # bounded dedup state; old historical rounds are still centrally idempotent
                state["hashes"]=dict(list(hashes.items())[-128:])
                self.conn.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?)",(tid,compact(m),compact(state),time.time()))
                self.sampled+=1
        self.status=coverage if online else "OFFLINE_BUFFERING"
        changed=self.get("seq",0)>self.get("previous_tick_seq",0)
        with self.conn: self.put("previous_tick_seq",self.get("seq",0))
        running=any(json.loads(r[0]).get("active") for r in self.conn.execute("SELECT state FROM sessions ORDER BY checked DESC LIMIT 56"))
        return 2 if changed or queued or running or len(metadata)==PAGE else 12

    def close(self):
        self.conn.close();self.lock.close()

class LocalObservationRegistration:
    """Authenticated in-process source on the centre's own stable NodeIdentity.

    The local read_sessions grant is explicit and persisted. No self-pair,
    alternate identity, canonical task, execution or Provider is created.
    """
    def __init__(self,identity,registry,approvals,writer,stale_after=45):
        self.identity,self.registry,self.approvals,self.writer=identity,registry,approvals,writer
        self.stale_after=stale_after

    def heartbeat(self,identity,scope,*,port,capabilities):
        from node_protocol import NodeRecord,_now
        approved=self.approvals.get(self.identity.public["node_id"])
        if identity is not self.identity or approved is None or not approved.allows("observation.upload"):
            old=self.registry.get(self.identity.public["node_id"])
            if old: self.registry.revoke(old.node_id,old.user_scope)
            return {"registered":False,"error_code":"LOCAL_OBSERVATION_SCOPE_DENIED"}
        record=NodeRecord(node_id=identity.public["node_id"],user_scope=approved.user_scope,
            public_key_fingerprint=identity.public["fingerprint"],display_name=identity.public["display_name"],
            capabilities=("session.read","session.status"),state="ONLINE",last_seen=_now(),stale_after_seconds=self.stale_after)
        self.registry.register(record,approved)
        return {"registered":True,"scope":approved.as_dict()}

    def upload(self,identity,scope,**batch):
        approved=self.approvals.get(self.identity.public["node_id"])
        if identity is not self.identity or approved is None or not approved.allows("observation.upload"):
            raise NodeProtocolError("LOCAL_OBSERVATION_SCOPE_DENIED","Local read_sessions approval is required")
        return self.writer.accept(identity.public["node_id"],approved,batch)
