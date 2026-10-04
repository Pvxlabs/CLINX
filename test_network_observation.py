"""隔离 fixture 回归；不代表真实 Air 或 GUI 已验收。"""
import dataclasses
import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest

from node_protocol import NodeRegistry,NodeRecord,SharingScope,NodeAuthorizationStore,NodeProtocolError,_now
from network_observation import ObservationWriter,ObservationDirectory,observation_id
from observation_source import CodexObservationSource,ObservationCollector
from test_native_interop import native,TID,hashes


def event(seq=1,tid="unmanaged",turn="turn-1",ordinal=1,**changes):
    result=dict(seq=seq,native_thread_id=tid,provider="codex_app_server",project="/approved",
        title="简短标题\n不应铺满列表",source_generation="generation-1",
        turn=dict(turn_id=turn,ordinal=ordinal,native_state="COMPLETED",summary="公开结果",
                  business_result=None))
    result.update(changes)
    return result


@pytest.fixture
def directory(tmp_path):
    registry=NodeRegistry(tmp_path/"nodes.db")
    approvals=NodeAuthorizationStore(tmp_path/"identity")
    scope=approvals.grant("air",user_scope="alice",read_sessions=True,projects=("/approved",))
    registry.register(NodeRecord("air",user_scope="alice",display_name="Air",state="ONLINE",last_seen=_now()),scope)
    peers=SimpleNamespace(all=lambda:{"air":{"trust_state":"TRUSTED","security_status":"OPAQUE_V1"}})
    writer=ObservationWriter(tmp_path/"observation.db")
    reader=ObservationDirectory(writer.path,registry,approvals,peers,"alice")
    yield writer,reader,registry,approvals,scope
    registry.close()


def test_unmanaged_nullable_identity_shared_api_and_mcp(directory):
    from observer_server import ObserverAPI
    from observation_mcp import standalone_server
    writer,reader,registry,approvals,scope=directory
    writer.accept("air",scope,dict(stream_id="stream",events=[event()]))
    api=ObserverAPI(Mock(),lambda:"x"*40,reader)
    a=api.request("GET","/v2/observations","Bearer "+"x"*40)
    server=standalone_server(reader)
    b=server._call_tool("clinx_list_observations",{})
    assert a["items"]==b["items"]
    item=a["items"][0]
    assert item["task_ref"] is None and item["turn"]["execution_ref"] is None
    assert item["turn"]["business_result"] is None
    assert item["turn"]["native_state"]=="COMPLETED"
    assert item["binding_evidence"]=="THREAD_UNBOUND"
    assert item["title"]=="简短标题"
    assert item["control"]["enabled"] is False
    assert item["liveness"]=="NOT_PROVEN"
    assert server.handle({"id":1,"method":"tools/list"})["result"]["tools"][0]["inputSchema"]["additionalProperties"] is False
    assert server.handle({"id":2,"method":"server/discover"})["result"]["resultType"]=="complete"


def test_offline_history_restart_and_read_only(directory):
    writer,reader,registry,approvals,scope=directory
    writer.accept("air",scope,dict(stream_id="stream",events=[event()]))
    oid=reader.list()["items"][0]["observation_id"]
    before=writer.path.read_bytes()
    registry.touch("air",state="OFFLINE")
    restarted=ObservationDirectory(writer.path,registry,approvals,reader.peers,"alice")
    assert restarted.detail(oid)["item"]["freshness"]=="OFFLINE"
    assert restarted.detail(oid)["turns"][0]["native_state"]=="COMPLETED"
    assert restarted.context(oid)["context_status"]=="UNCACHED_CONTENT_UNAVAILABLE"
    assert writer.path.read_bytes()==before


def test_duplicate_out_of_order_gap_and_terminal_monotonic(directory):
    writer,reader,_,_,scope=directory
    batch=dict(stream_id="stream",events=[event()])
    assert writer.accept("air",scope,batch)["ack_seq"]==1
    assert writer.accept("air",scope,batch)["ack_seq"]==1
    with pytest.raises(NodeProtocolError,match="different content"):
        writer.accept("air",scope,dict(stream_id="stream",events=[event(title="conflict")]))
    result=writer.accept("air",scope,dict(stream_id="stream",events=[event(3)]))
    assert result["error_code"]=="OBSERVATION_GAP" and result["expected_seq"]==2
    # Entire batch must remain uncommitted when a later sequence has a gap.
    assert not writer.accept("air",scope,dict(stream_id="stream",events=[event(2),event(4)]))["accepted"]
    with sqlite3.connect(writer.path) as c:
        assert c.execute("SELECT seq FROM streams").fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM events").fetchone()[0]==1
    stale=event(2);stale["turn"]["native_state"]="RUNNING";stale["turn"]["summary"]=None
    writer.accept("air",scope,dict(stream_id="stream",events=[stale]))
    assert reader.list()["items"][0]["turn"]["native_state"]=="COMPLETED"
    assert reader.list()["items"][0]["turn"]["summary"]=="公开结果"
    writer.accept("air",scope,dict(stream_id="stream",events=[event(3,turn="turn-0",ordinal=0)]))
    item=reader.list()["items"][0]
    assert item["turn"]["turn_id"]=="turn-1"
    assert len(reader.detail(item["observation_id"])["turns"])==2


def test_revoke_filters_and_cursor_isolation(directory):
    writer,reader,registry,approvals,scope=directory
    writer.accept("air",scope,dict(stream_id="stream",events=[event(i,tid=f"t-{i}") for i in range(1,5)]))
    page=reader.list(limit=1);cursor=page["next_cursor"];oid=page["items"][0]["observation_id"]
    assert reader.list(limit=1,cursor=cursor)["items"][0]["observation_id"]!=oid
    for kw in (dict(node="air"),dict(kind="external"),dict(project="/approved")):
        with pytest.raises(NodeProtocolError):reader.list(cursor=cursor,**kw)
    other=ObservationDirectory(writer.path,registry,approvals,reader.peers,"bob")
    assert other.list()["items"]==[]
    with pytest.raises(NodeProtocolError):other.list(cursor=cursor)
    approvals.revoke("air")
    assert reader.list()["items"]==[]
    with pytest.raises(NodeProtocolError): reader.detail(oid)
    with pytest.raises(NodeProtocolError): reader.list(cursor=cursor)


def test_auth_scope_allowlist_and_scrub(directory):
    writer,reader,_,_,scope=directory
    for change in ({"project":"/private"},{"provider":"other-agent"},{"task_ref":"task_fake"}):
        with pytest.raises(NodeProtocolError):
            writer.accept("air",scope,dict(stream_id="stream",events=[event(**change)]))
    item=event();item.update(reasoning="NEVER_REASONING",tool_output="NEVER_TOOL")
    item["turn"]["summary"]="token="+("s"*40)+" secret=fixture_secret"
    writer.accept("air",scope,dict(stream_id="stream",events=[item]))
    raw=json.dumps(reader.list())
    assert "NEVER_" not in raw and "fixture_secret" not in raw
    assert "NOT_IMPLEMENTED" in raw


def test_managed_dedup_and_rebuild_requires_cas(directory):
    writer,reader,_,_,scope=directory
    writer.accept("air",scope,dict(stream_id="stream",events=[event()]))
    managed=event(2,task_ref="task-real",binding_evidence="CANONICAL_ROUTE")
    managed["turn"].update(execution_ref="exec-real",business_result="PASS",execution_state="COMPLETED")
    writer.accept("air",scope,dict(stream_id="stream",events=[managed]))
    assert len(reader.list()["items"])==1
    assert reader.list(kind="managed")["items"][0]["task_ref"]=="task-real"
    with pytest.raises(NodeProtocolError):
        writer.accept("air",scope,dict(stream_id="rebuilt",events=[event()]))
    writer.accept("air",scope,dict(stream_id="rebuilt",previous_stream_id="stream",events=[event()]))
    oid=reader.list()["items"][0]["observation_id"]
    assert reader.detail(oid)["gap"]=="SOURCE_REBUILT"


def test_source_paginated_incremental_late_terminal_and_no_authority_writes(native):
    with sqlite3.connect(native.root/"state_5.sqlite") as c:
        c.execute("ALTER TABLE threads ADD COLUMN updated_at INTEGER DEFAULT 100")
    source=CodexObservationSource(native.root,"p620",native.registry.path)
    scope=SharingScope("alice",read_sessions=True,projects=(str(native.repo),))
    before=hashes(native)
    metadata,after,coverage=source.inventory(scope)
    assert len(metadata)==1
    events,state=source.sample(metadata[0],{})
    assert len(events)==3
    assert all(e["task_ref"] is None for e in events)
    assert not any("NEVER_" in json.dumps(e) or "LARGE_TOOL" in json.dumps(e) for e in events)
    assert hashes(native)==before
    with sqlite3.connect(native.root/"thread_history_1.sqlite") as c:
        c.execute("UPDATE thread_turns SET status='completed',completed_at=9 WHERE turn_id='turn-2'")
    events,_=source.sample(metadata[0],state)
    assert next(e["turn"] for e in events if e["turn"]["turn_id"]=="turn-2")["native_state"]=="COMPLETED"
    assert source.inventory(SharingScope("alice",read_sessions=True,projects=("/private",)))[0]==[]


def test_collector_durable_checkpoint_singleton_and_unbound(native,tmp_path):
    with sqlite3.connect(native.root/"state_5.sqlite") as c:c.execute("ALTER TABLE threads ADD COLUMN updated_at INTEGER DEFAULT 100")
    scope=SharingScope("alice",read_sessions=True,projects=(str(native.repo),))
    source=CodexObservationSource(native.root,"p620")
    identity=SimpleNamespace(public={"node_id":"p620"})
    reg=SimpleNamespace(heartbeat=lambda *a,**k:dict(registered=True,scope=scope.as_dict()),
                        upload=lambda *a,**k:dict(accepted=True,ack_seq=k["events"][-1]["seq"]))
    path=tmp_path/"spool.db"
    collector=ObservationCollector(path,source,identity,reg,lambda:scope,1234,())
    with pytest.raises(NodeProtocolError):
        ObservationCollector(path,source,identity,reg,lambda:scope,1234,())
    before=hashes(native);collector.tick()
    assert collector.conn.execute("SELECT count(*) FROM outbox").fetchone()[0]==3
    seq=collector.get("seq");collector.close()
    collector=ObservationCollector(path,source,identity,reg,lambda:scope,1234,())
    collector.tick()
    assert collector.get("seq")==seq
    assert collector.conn.execute("SELECT count(*) FROM outbox").fetchone()[0]==0
    collector.close()
    assert hashes(native)==before

@pytest.mark.parametrize("attempt", range(12))
def test_finalizer_concurrency_stability(native,tmp_path,attempt):
    from test_air_result_contract import test_terminal_concurrent_opaque_status_and_exact_context
    test_terminal_concurrent_opaque_status_and_exact_context(native,tmp_path)


def test_canonical_native_same_turn_merge_and_missing_result_marker(native,tmp_path):
    from m9_integration import ExecutionFinalizer
    from test_pvx1812_completion import RESULT
    with patch("thread_identity.ThreadIdentityReader",return_value=native.reader):
        task=native.integration.adopt_conversation(thread_id=TID)["task_ref"]
    ref="exec_network_fixture"
    with native.registry.execution(task,execution_ref=ref,retain=True):
        native.registry.set_execution_state(task,"CODEX_RUNNING",turn_id="turn-2",codex_running=True)
    ExecutionFinalizer(native.registry,None).finalize(execution_ref=ref,task_id=task,turn_id="turn-2",raw_result=RESULT)
    source=CodexObservationSource(native.root,"p620",native.registry.path)
    metadata=source.history.metadata(TID)
    before=hashes(native)
    events,state=source.sample(metadata,{})
    exact=[e for e in events if e["turn"]["turn_id"]=="turn-2"]
    assert len(exact)==1 and exact[0]["task_ref"]==task
    assert exact[0]["turn"]["business_result"]=="PASS"
    assert exact[0]["turn"]["execution_ref"]==ref
    assert hashes(native)==before


def test_legacy_allowlist_tail_resume_and_terminal(tmp_path):
    from test_network_observation_process import fixture_source
    root=tmp_path/"native";fixture_source(root,"legacy","/fixture")
    path=root/"fixture-legacy.jsonl"
    with sqlite3.connect(root/"state_5.sqlite") as c:c.execute("UPDATE threads SET history_mode='legacy'")
    rows=[
      {"type":"turn_context","timestamp":"2026-10-04T00:00:00Z","payload":{"turn_id":"legacy-turn"}},
      {"type":"response_item","payload":{"type":"reasoning","text":"NEVER_INTERNAL"}},
      {"type":"response_item","payload":{"type":"agentMessage","phase":"analysis","text":"NEVER_ANALYSIS"}},
      {"type":"response_item","payload":{"type":"agentMessage","text":"公开答复"}},
      {"type":"event_msg","timestamp":"2026-10-04T00:01:00Z","payload":{"type":"task_complete","turn_id":"legacy-turn"}},
    ]
    with path.open("a") as f:
        for row in rows:f.write(json.dumps(row)+"\n")
    source=CodexObservationSource(root,"air")
    events,state=source.sample(source.history.metadata("legacy"),{})
    assert events[0]["turn"]["native_state"]=="COMPLETED"
    assert "NEVER_" not in json.dumps(events)
    saved=state["offset"]
    _,new=source.sample(source.history.metadata("legacy"),state)
    assert new["offset"]==saved
    stable,_=source.sample(source.history.metadata("legacy"),new)
    assert stable[0]["coverage"]=="INDEXED"


def test_offline_buffering_and_retention_backpressure(native,tmp_path,monkeypatch):
    with sqlite3.connect(native.root/"state_5.sqlite") as c:c.execute("ALTER TABLE threads ADD COLUMN updated_at INTEGER DEFAULT 100")
    scope=SharingScope("alice",read_sessions=True,projects=(str(native.repo),))
    registry=Mock()
    registry.heartbeat.return_value=dict(registered=True,scope=scope.as_dict())
    registry.upload.return_value=dict(accepted=True,ack_seq=3)
    collector=ObservationCollector(tmp_path/"spool.db",CodexObservationSource(native.root,"p620"),
        SimpleNamespace(public={"node_id":"p620"}),registry,lambda:scope,1234,())
    collector.tick()
    registry.heartbeat.side_effect=ConnectionError()
    with sqlite3.connect(native.root/"thread_history_1.sqlite") as c:c.execute("UPDATE thread_turns SET status='completed' WHERE turn_id='turn-2'")
    collector.tick()
    assert collector.status=="OFFLINE_BUFFERING"
    assert collector.get("seq")==4
    monkeypatch.setattr("observation_source.OUTBOX_LIMIT",130)
    prior=collector.get("seq");collector.tick()
    assert collector.status=="OBSERVATION_BACKPRESSURE" and collector.get("seq")==prior
    collector.close()


def test_timestamp_ties_audit_and_source_rebuild(tmp_path):
    from test_network_observation_process import fixture_source
    root=tmp_path/"native";fixture_source(root,"first","/fixture")
    source=CodexObservationSource(root,"air")
    scope=SharingScope("user",read_sessions=True,projects=("/fixture",))
    with sqlite3.connect(root/"state_5.sqlite") as c:
        template=c.execute("SELECT * FROM threads LIMIT 1").fetchone()
        for index in range(40):
            row=list(template);row[0]=f"tie-{index:02d}"
            c.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?)",row)
    seen=[];cursor=None
    while True:
        rows,cursor,_=source.inventory(scope,cursor)
        if not rows:break
        seen.extend(r["id"] for r in rows)
    assert len(seen)==41 and len(set(seen))==41
    generation=source.generation
    # Source inode replacement is an observable rebuild, identity stays node/user/provider/thread.
    old=root/"state_5.sqlite";old.rename(root/"state-old.sqlite")
    with sqlite3.connect(root/"state-old.sqlite") as src,sqlite3.connect(old) as dst:src.backup(dst)
    assert source.generation!=generation

def test_scope_narrowing_acknowledges_withheld_sequence_without_content(directory):
    writer,reader,registry,approvals,scope=directory
    writer.accept("air",scope,dict(stream_id="stream",events=[event()]))
    result=writer.accept("air",scope,dict(stream_id="stream",events=[{"seq":2,"scope_redacted":True}]))
    assert result["ack_seq"]==2 and result["gap"]=="SCOPE_WITHHELD"
    writer.accept("air",scope,dict(stream_id="stream",events=[event(3,tid="permitted")]))
    assert len(reader.list()["items"])==2
    with sqlite3.connect(writer.path) as c:
        assert c.execute("SELECT count(*) FROM items").fetchone()[0]==2
    with pytest.raises(NodeProtocolError):
        writer.accept("air",scope,dict(stream_id="stream",events=[{"seq":4,"scope_redacted":True,"summary":"PRIVATE"}]))


@pytest.mark.parametrize("field,value", [("read_sessions","false"),("execute_tasks",1),("projects","/private"),("providers","codex_app_server")])
def test_approval_schema_fails_closed(field,value):
    with pytest.raises(ValueError):
        SharingScope("user",**{field:value})
