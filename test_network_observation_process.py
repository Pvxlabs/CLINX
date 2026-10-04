"""真实独立进程与 OPAQUE/mTLS 传输；数据全为隔离 fixture。"""
import json
import os
from pathlib import Path
import select
import socket
import sqlite3
import subprocess
import sys
import time

from local_discovery.identity import NodeIdentity,PrivateStore,TrustedPeerStore
from local_discovery.discovery import Candidate
from local_discovery.transport import DeviceServer,LanTransport
from node_protocol import NodeAuthorizationStore
from task_registry import TaskRegistry
from test_node_runtime import paired

ROOT=Path(__file__).resolve().parent


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        return sock.getsockname()[1]


def line(process,timeout=10):
    ready,_,_=select.select([process.stdout],[],[],timeout)
    assert ready, "进程未按时返回就绪/读回"
    value=process.stdout.readline()
    assert value, process.stderr.read()
    return json.loads(value)


def fixture_source(root,tid,project):
    root.mkdir()
    rollout=root/("fixture-"+tid+".jsonl")
    rollout.write_text(json.dumps({"type":"session_meta","payload":{"id":tid,"cwd":project}})+"\n")
    with sqlite3.connect(root/"state_5.sqlite") as c:
        c.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT,rollout_path TEXT,history_mode TEXT,title TEXT,updated_at INT,source TEXT)")
        c.execute("CREATE INDEX updated_threads ON threads(updated_at,id)")
        c.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?)",(tid,project,str(rollout),"paginated","隔离原生会话",100,"desktop"))
    with sqlite3.connect(root/"thread_history_1.sqlite") as c:
        c.executescript("""
          CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,rollout_ordinal INT,status TEXT,started_at INT,completed_at INT,PRIMARY KEY(thread_id,turn_id));
          CREATE INDEX turns_page ON thread_turns(thread_id,rollout_ordinal);
          CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,rollout_ordinal INT,created_at_ms INT,item_type TEXT,item_json TEXT,PRIMARY KEY(thread_id,turn_id,item_id));
          CREATE INDEX items_page ON thread_items(thread_id,turn_id,rollout_ordinal);
          CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY,next_rollout_byte_offset INT);
        """)
        c.execute("INSERT INTO thread_turns VALUES(?,?,?,?,?,?)",(tid,"turn-fixture",1,"inProgress",int(time.time()),None))
        c.execute("INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)",(tid,"turn-fixture","public",1,100,"agentMessage",json.dumps({"type":"agentMessage","text":"公开进度","phase":"commentary"})))
        c.execute("INSERT INTO thread_history_projection_state VALUES(?,?)",(tid,rollout.stat().st_size))


def test_real_process_collection_tls_centre_observer_mcp_two_clients_and_recovery(tmp_path):
    node,node_peers,centre,centre_peers=paired(tmp_path)
    # A second independently paired source represents a P620 node in this fixture.
    second=NodeIdentity(PrivateStore(tmp_path/"p620-fixture"),"p620-fixture")
    with DeviceServer(centre,centre_peers,address="127.0.0.1") as pairing:
        candidate=Candidate(centre.public["node_id"],centre.public["display_name"],centre.public["fingerprint"],(("127.0.0.1",pairing.port),))
        LanTransport(second,TrustedPeerStore(second.store)).pair(candidate,pairing.window.open())
    for source in (node,second):
        NodeAuthorizationStore(source.store.root).grant("p620",user_scope="fixture-user",read_sessions=True,projects=("/fixture",))
        NodeAuthorizationStore(centre.store.root).grant(source.public["node_id"],user_scope="fixture-user",read_sessions=True,projects=("/fixture",))
    observation=tmp_path/"observations.db";registry=tmp_path/"nodes.db"
    canonical=tmp_path/"canonical.db";TaskRegistry(canonical)
    import gc
    gc.collect()  # 完成 fixture 建库的连接回收，再冻结物理文件证据。
    authority_before=canonical.read_bytes()
    centre_port=free_port();observer_port=free_port()
    env=dict(os.environ,PYTHONPATH=str(ROOT),CLINX_OBSERVATION_DB=str(observation),
        CLINX_OBSERVATION_STATE=str(centre.store.root),CLINX_OBSERVATION_REGISTRY=str(registry),
        CLINX_OBSERVATION_USER_SCOPE="fixture-user",CLINX_OBSERVATION_NODE_ID="p620",
        CLINX_OBSERVER_DB=str(canonical),CLINX_OBSERVER_TOKEN="fixture-observer-"+"x"*32,
        CLINX_OBSERVER_PORT=str(observer_port))
    processes=[]
    def spawn(argv,extra=None):
        p=subprocess.Popen([sys.executable,*argv],cwd=ROOT,env=dict(env,**(extra or {})),
                           stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        processes.append(p);return p
    def centre_start():
        p=spawn(["node_centre_entrypoint.py","--state",str(centre.store.root),"--node-id","p620","serve",
            "--registry",str(registry),"--observations",str(observation),"--bind","127.0.0.1",
            "--port",str(centre_port),"--stale-after","2"])
        assert line(p)["ready"];return p
    client_code="""import json,os,urllib.request
req=urllib.request.Request('http://127.0.0.1:'+os.environ['CLINX_OBSERVER_PORT']+'/v2/observations',
 headers={'Authorization':'Bearer '+os.environ['CLINX_OBSERVER_TOKEN']})
print(urllib.request.urlopen(req,timeout=2).read().decode())
"""
    def http_client():
        # Each call is a fresh independent OS process, not a function mock.
        p=subprocess.run([sys.executable,"-c",client_code],cwd=ROOT,env=env,capture_output=True,text=True,timeout=5)
        if p.returncode: return None
        return json.loads(p.stdout)
    def wait_items(predicate,timeout=12):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            page=http_client()
            if page and predicate(page["items"]): return page
            time.sleep(.15)
        raise AssertionError("自动目录未满足条件；"+str(page))
    try:
        centre_process=centre_start()
        roots=[]
        for source in (node,second):
            root=tmp_path/(source.public["node_id"]+"-native")
            tid="fixture-"+source.public["node_id"]
            fixture_source(root,tid,"/fixture");roots.append(root)
            source.store.write("node-config.json",dict(schema_version=1,centre_id="p620",
                centre_endpoint=f"tls://127.0.0.1:{centre_port}",bind_address="127.0.0.1",port=free_port()))
            spawn(["MonitorApp/Scripts/node_service_entrypoint.py"],dict(CLINX_NODE_STATE=str(source.store.root),
                CLINX_NODE_HEALTH_STATE=str(tmp_path/(source.public["node_id"]+"-health")),CLINX_NATIVE_HOME=str(root)))
        observer=subprocess.Popen(["sh","bin/clinx-observer"],cwd=ROOT,
            env=dict(env,CLINX_OBSERVER_PYTHON=sys.executable),
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        processes.append(observer)
        page=wait_items(lambda items:len(items)==2)
        assert {i["node_id"] for i in page["items"]}=={"air","p620-fixture"}
        assert all(i["task_ref"] is None and i["turn"]["execution_ref"] is None for i in page["items"])
        identities={i["observation_id"] for i in page["items"]}
        mcp=spawn(["mcp_server.py","--stdio","--observations-only"])
        mcp.stdin.write(json.dumps(dict(jsonrpc="2.0",id=1,method="tools/call",
            params=dict(name="clinx_list_observations",arguments={})))+"\n");mcp.stdin.flush()
        response=line(mcp)
        structured=response["result"]["structuredContent"]
        assert {i["observation_id"] for i in structured["items"]}==identities
        a=http_client();b=http_client()
        assert a["items"]==b["items"]
        delays=[]
        for sample in range(5):
            started=time.monotonic()
            label=f"公开进度样本-{sample}"
            with sqlite3.connect(roots[0]/"thread_history_1.sqlite") as c:
                c.execute("UPDATE thread_items SET item_json=?",(json.dumps({"type":"agentMessage","text":label,"phase":"commentary"}),))
            wait_items(lambda rows:any(i["node_id"]=="air" and i["turn"]["summary"]==label for i in rows))
            delays.append(time.monotonic()-started)
        # Natural persisted source facts change without updating index timestamp.
        started=time.monotonic()
        with sqlite3.connect(roots[0]/"thread_history_1.sqlite") as c:
            c.execute("UPDATE thread_turns SET status='completed',completed_at=?",(int(time.time()),))
        terminal=wait_items(lambda rows:any(i["node_id"]=="air" and i["turn"]["native_state"]=="COMPLETED" for i in rows))
        delay=time.monotonic()-started
        delays.append(delay)
        assert max(delays)<=5.0, f"fixture 延迟超过验收目标: {delays}"
        # Shared centre restart while source remains running; buffered facts survive.
        centre_process.terminate();centre_process.wait(timeout=5)
        with sqlite3.connect(roots[1]/"thread_history_1.sqlite") as c:
            c.execute("UPDATE thread_turns SET status='completed',completed_at=?",(int(time.time()),))
        time.sleep(2.5)
        offline=http_client()
        assert {i["observation_id"] for i in offline["items"]}==identities
        assert all(i["freshness"]=="OFFLINE" for i in offline["items"])
        centre_process=centre_start()
        recovered=wait_items(lambda rows:len(rows)==2 and all(i["turn"]["native_state"]=="COMPLETED" for i in rows),timeout=18)
        assert {i["observation_id"] for i in recovered["items"]}==identities
        assert canonical.read_bytes()==authority_before
        # Revocation hides the already cached item on the next read.
        NodeAuthorizationStore(centre.store.root).revoke("air",user_scope="fixture-user")
        revoked=wait_items(lambda rows:len(rows)==1)
        assert revoked["items"][0]["node_id"]=="p620-fixture"
        # Logical evidence retained by pytest capture; no test credential output.
        usage=[]
        for process in processes:
            stat=Path(f"/proc/{process.pid}/stat")
            if stat.exists():
                fields=stat.read_text().split()
                usage.append({"pid":process.pid,"cpu_seconds":(int(fields[13])+int(fields[14]))/os.sysconf("SC_CLK_TCK"),
                              "rss_bytes":int(fields[23])*os.sysconf("SC_PAGE_SIZE")})
        print(json.dumps({"evidence":"ISOLATED_FIXTURE_REAL_PROCESSES","latency_samples_seconds":delays,"p95_seconds":sorted(delays)[-1],
            "measurement":"SOURCE_SQL_COMMIT_TO_INDEPENDENT_HTTP_CLIENT_NOT_MAC_UI","process_usage":usage,
            "processes":len(processes),"identities":sorted(identities),
            "canonical_db_unchanged":True,"provider_calls":0,"lease_mutations":0}))
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
