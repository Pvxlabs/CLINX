"""One-incident, compare-and-swap historical repair; no current task mutation."""
import hashlib,json,sqlite3,sys,time
from pathlib import Path
from bridge import BridgeConfig,TaskDispatcher,parse_codex_result
from native_provider import existing_client,observe_thread
from task_registry import TaskRegistry
from m9_integration import ExecutionFinalizer

REF='exec_ab8a145d457f43f7b2cafbb9e0341fa0'
TID='01a0fa9b-13ce-7fd2-84d0-82cc87e1c429'
TURN='01a0fc17-45dd-7d50-b28a-4fb4370f15cc'
TASK='task_d974fcd472034b95aaa6f8797f91e14d'
BASE=Path('/home/pvxlabs/.local/state/clinx/maintenance/completion-20261002')
CFG=BridgeConfig.load(Path('/home/pvxlabs/dev/clinx/bridge.toml'))


def one(c,table):
    r=c.execute('SELECT * FROM '+table+' WHERE execution_ref=?',(REF,)).fetchone()
    return dict(r) if r else None


def snapshot(c):
    # Verify all existing data outside the two exact historical rows remains intact.
    out={}
    for (table,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        if table=='execution_result_corrections': continue
        clause=' WHERE execution_ref != ?' if table in ('execution_history','execution_results') else ''
        rows=[tuple(r) for r in c.execute('SELECT * FROM "'+table+'"'+clause, (REF,) if clause else ())]
        out[table]=hashlib.sha256(repr(rows).encode()).hexdigest()
    return out


def repair(c,expected,result,evidence):
    c.execute('BEGIN IMMEDIATE')
    assert one(c,'execution_results')==expected['result'], 'result changed since inspection'
    assert one(c,'execution_history')==expected['history'], 'history changed since inspection'
    assert not c.execute('SELECT 1 FROM executions WHERE execution_ref=?',(REF,)).fetchone()
    assert not c.execute('SELECT 1 FROM worktree_leases WHERE execution_ref=?',(REF,)).fetchone()
    before=snapshot(c)
    c.execute('''CREATE TABLE IF NOT EXISTS execution_result_corrections (
        execution_ref TEXT PRIMARY KEY, corrected_at REAL NOT NULL,
        original_json TEXT NOT NULL, evidence_json TEXT NOT NULL, result_sha256 TEXT NOT NULL)''')
    c.execute('INSERT INTO execution_result_corrections VALUES (?,?,?,?,?)',
        (REF,time.time(),json.dumps(expected,sort_keys=True),json.dumps(evidence,sort_keys=True),
         hashlib.sha256(result.raw_result.encode()).hexdigest()))
    c.execute('''UPDATE execution_results SET status=?,summary=?,changed_files=?,validation=?,
        blockers=?,next_state=?,raw_result=?,writeback_state='PENDING',writeback_body_hash=NULL,written_at=NULL
        WHERE execution_ref=?''',
        (result.status,result.summary,result.changed_files,result.validation,result.blockers,result.next_state,result.raw_result,REF))
    c.execute("""UPDATE execution_history SET stage='COMPLETED',execution_state='COMPLETED',
        failure_stage=NULL,failure_code=NULL,failure_evidence=NULL WHERE execution_ref=?""",(REF,))
    assert before==snapshot(c), 'unrelated data changed'
    c.commit()

src=sqlite3.connect('file:'+str(CFG.task_db_path)+'?mode=ro',uri=True);src.row_factory=sqlite3.Row
expected={'result':one(src,'execution_results'),'history':one(src,'execution_history')}
r,h=expected['result'],expected['history']
assert r and h and r['task_id']==h['task_id']==TASK and r['turn_id']==h['turn_id']==TURN
assert r['status']=='BLOCKED' and h['stage']=='BLOCKED' and h['released_at']
assert r['summary']=='Provider terminal output omitted the result marker and no successful host execution evidence was recorded.'
route=json.loads(h['routing_identity_json'])
assert route['conversation']['binding']==TID
observed=observe_thread(CFG,TID)
assert observed['owner_endpoint'] and not observed['ownership_conflict']
assert all(x['state'] not in ('UNKNOWN','OFFLINE') for x in observed['observations'])
with existing_client(observed['owner_endpoint'],timeout=10) as client:
    client.initialize(client_name=CFG.app_server.client_name,client_title=CFG.app_server.client_title,client_version=CFG.app_server.client_version)
    page=client.thread_turns_list(TID,limit=20,items_view='notLoaded',sort_direction='desc')
    old=next(x for x in page['data'] if x['id']==TURN)
    assert old['status']=='completed'
    entry=client.thread_items_list(TID,turn_id=TURN,limit=1,sort_direction='desc')['data'][0]
    assert entry['turnId']==TURN and entry['item']['type']=='agentMessage' and entry['item']['phase'] in ('final','final_answer')
    normalized=TaskDispatcher._exact_final_result(entry['item']['text'])
    result=parse_codex_result(normalized)
    assert result.status=='PASS' and 'PRODUCTION_DEPLOYMENT=NOT_RUN' in result.raw_result
stamp=str(time.time_ns())
backup=BASE/('before-'+stamp+'.sqlite3'); clone=BASE/('dry-run-'+stamp+'.sqlite3')
with sqlite3.connect(backup) as out: src.backup(out)
with sqlite3.connect(clone) as out: src.backup(out)
registry=TaskRegistry(clone)
decision=ExecutionFinalizer(registry)._decision(REF,normalized,provider_outcome='PROVIDER_TERMINAL',provider_status='completed')
assert decision.terminal_state=='COMPLETED' and decision.result.status=='PASS'
evidence={'thread_id':TID,'turn_id':TURN,'exact_turn_status':'completed','phase':entry['item']['phase'],
          'provider_observation':observed,'qualification':'provider-reported, local scope only'}
with sqlite3.connect(clone) as c:
    c.row_factory=sqlite3.Row
    repair(c,expected,result,evidence)
print(json.dumps({'dry_run':'PASS','backup':str(backup),'backup_sha256':hashlib.sha256(backup.read_bytes()).hexdigest(),
                  'unrelated_tables':'unchanged','old_execution':REF,'result':'PASS','state':'COMPLETED'}))
if '--apply' in sys.argv:
    with sqlite3.connect(CFG.task_db_path,timeout=10) as c:
        c.row_factory=sqlite3.Row
        repair(c,expected,result,evidence)
    print('APPLIED_EXACT_HISTORY_ONLY; external audit left PENDING to preserve successor task state')
