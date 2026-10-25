-- Independent SQL rows from original Python historical fixture.

            CREATE TABLE tasks(task_id TEXT PRIMARY KEY,project_alias TEXT,title TEXT,host TEXT,status TEXT,execution_state TEXT,current_stage TEXT,current_blocker TEXT,created_at TEXT,updated_at TEXT,last_progress_at TEXT,codex_running INT,retry_required INT,turn_id TEXT);
            CREATE TABLE executions(execution_ref TEXT,task_id TEXT,logical_model TEXT,resolved_model TEXT,stage TEXT,execution_state TEXT,turn_id TEXT,acquired_at TEXT,routing_identity_json TEXT,execution_policy_json TEXT);
            CREATE TABLE execution_history(execution_ref TEXT,task_id TEXT,logical_model TEXT,resolved_model TEXT,stage TEXT,execution_state TEXT,turn_id TEXT,acquired_at TEXT,routing_identity_json TEXT,execution_policy_json TEXT,released_at TEXT);
            CREATE TABLE execution_results(task_id TEXT,execution_ref TEXT,turn_id TEXT,status TEXT,summary TEXT,validation TEXT,blockers TEXT,next_state TEXT,received_at TEXT);
            CREATE TABLE conversation_bindings(task_id TEXT,thread_id TEXT);
            CREATE TABLE prepared_executions(resulting_execution_ref TEXT,resulting_task_id TEXT,reasoning_effort TEXT,created_at TEXT);
            CREATE TABLE host_executions(task_id TEXT,host_execution_ref TEXT,execution_ref TEXT,host TEXT,surface TEXT,operation_class TEXT,capability TEXT,operation TEXT,started_at TEXT,completed_at TEXT,duration_ms INT,exit_code INT,result_state TEXT,timed_out INT);
            CREATE TABLE v2_events(cursor INTEGER PRIMARY KEY,event_id TEXT,event_type TEXT,occurred_at TEXT,recorded_at TEXT,payload_json TEXT);

INSERT INTO tasks VALUES ('task-fixture','fixture','Title','p620','ACTIVE','COMPLETED','COMPLETE','','2026-10-25T01:00:00Z','2026-10-25T02:00:00Z','2026-10-25T02:00:00Z',0,0,'00000000-0000-0000-0000-000000000002');
INSERT INTO execution_history VALUES ('exec-fixture','task-fixture','logical','resolved','COMPLETE','COMPLETED','00000000-0000-0000-0000-000000000002','2026-10-25T01:00:00Z','{"host": {"stable_identifier": "p620"}, "provider": {"stable_identifier": "codex_app_server"}, "conversation": {"binding": "01a0fa4e-11e7-7540-91c8-1eee45d1a4d0"}}','{"operation_classes": ["DEVELOPMENT_MUTATION"], "execution_surface": "HOST_EXECUTOR"}','2026-10-25T02:00:00Z');
INSERT INTO execution_results VALUES ('task-fixture','exec-fixture','00000000-0000-0000-0000-000000000002','PASS','Done','checked','NONE','IN_REVIEW','2026-10-25T02:00:00Z');
INSERT INTO conversation_bindings VALUES ('task-fixture','01a0fa4e-11e7-7540-91c8-1eee45d1a4d0');
