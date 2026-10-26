//! Collector checkpoints, spool and exact receipts on private real SQLite.
#[path = "support/observation.rs"]
mod support;
use clinx_decision_kernel::{
    observation, observation_collector::Runtime, observation_runtime::Source, observation_source,
};
use rusqlite::{Connection, params};
use serde_json::{Value, json};
use support::*;

struct Harness {
    native: Native,
    source: Source,
    path: std::path::PathBuf,
    centre: std::path::PathBuf,
    scope: Value,
    online: bool,
    uploads: Vec<Value>,
    ack: Option<Value>,
    wrong_stream: bool,
    lose_ack: bool,
    kill_at: Option<&'static str>,
    malformed: bool,
    failed_coverage: bool,
    no_inventory: bool,
    sampled: Vec<String>,
    hold_readers: bool,
    readers: Vec<Connection>,
    require_empty_before_sample: bool,
    slow_sample: bool,
}
impl Harness {
    fn new() -> Self {
        let native = Native::new();
        let source = native.source();
        let path = native.temp.path().join("spool.db");
        let centre = native.temp.path().join("centre.db");
        observation::init(&centre).unwrap();
        Self {
            native,
            source,
            path,
            centre,
            scope: scope(),
            online: true,
            uploads: vec![],
            ack: None,
            wrong_stream: false,
            lose_ack: false,
            kill_at: None,
            malformed: false,
            failed_coverage: false,
            no_inventory: false,
            sampled: vec![],
            hold_readers: false,
            readers: vec![],
            require_empty_before_sample: false,
            slow_sample: false,
        }
    }
    fn open(&self) -> Runtime {
        let r = Runtime::default();
        r.invoke("collector.init", &json!({"path":self.path}), |_, _| {
            panic!("unexpected callback")
        })
        .unwrap();
        r
    }
    fn call(&mut self, r: &Runtime, op: &str, a: Value) -> Result<Value> {
        r.invoke(&format!("collector.{op}"), &a, |op, a| {
            self.callback(op, a)
                .map(|v| v.expect("kill callback needs real worker"))
        })
    }
    fn tick(&mut self, r: &Runtime) -> Result<Value> {
        self.call(r, "tick", json!({"outbox_limit":2048}))
    }
    fn callback(&mut self, op: &str, a: Value) -> Result<Option<Value>> {
        let result = match op {
            "collector.scope" => self.scope.clone(),
            "collector.heartbeat" => {
                if self.online {
                    json!({"registered":true,"scope":self.scope})
                } else {
                    json!({"offline":true})
                }
            }
            "collector.upload" => {
                self.uploads.push(a["batch"].clone());
                if let Some(ack) = &self.ack {
                    return Ok(Some(json!({"accepted":true,"ack_seq":ack})));
                }
                if self.wrong_stream {
                    return Ok(Some(
                        json!({"accepted":true,"ack_seq":3,"stream_id":"another-stream"}),
                    ));
                }
                let receipt = observation::accept(
                    &json!({"path":self.centre,"node":"air","scope":a["scope"],"request":a["batch"]}),
                )?;
                if self.kill_at == Some("upload") {
                    self.kill_at = None;
                    return Ok(None);
                }
                if self.lose_ack {
                    self.lose_ack = false;
                    json!({"offline":true})
                } else {
                    receipt
                }
            }
            "collector.generation" => self.source.invoke("generation", &json!({}))?,
            "collector.inventory" => {
                if self.no_inventory {
                    json!([[], null, "INDEXED"])
                } else {
                    self.source.invoke("inventory", &a)?
                }
            }
            "collector.canonical_inventory" => self.source.invoke("canonical_inventory", &a)?,
            "collector.sample" => {
                if self.slow_sample {
                    std::thread::sleep(std::time::Duration::from_millis(2050));
                }
                if self.require_empty_before_sample {
                    assert_eq!(
                        self.counts().1,
                        0,
                        "history must not overtake pending receipts"
                    );
                }
                self.sampled
                    .push(a["metadata"]["id"].as_str().unwrap().into());
                if self.no_inventory {
                    let mut state = a["state"].clone();
                    state["active"] = json!(true);
                    return Ok(Some(json!([[], state])));
                }
                let mut result = self.source.invoke("sample", &a)?;
                if self.kill_at == Some("sample") {
                    self.kill_at = None;
                    return Ok(None);
                }
                if self.malformed {
                    let last = result[0].as_array_mut().unwrap().last_mut().unwrap();
                    last["turn"]["summary"] = json!({"not":"display text"});
                }
                if self.failed_coverage {
                    let mut last = result[0].as_array().unwrap().last().unwrap().clone();
                    last["coverage"] = json!("NATIVE_OBSERVER_UNAVAILABLE");
                    last["turn"] = json!({"turn_id":null,"ordinal":0,"native_state":"UNKNOWN"});
                    result[0] = json!([last]);
                }
                if self.hold_readers {
                    let c = Connection::open(&self.path).unwrap();
                    c.execute_batch("BEGIN").unwrap();
                    let _: i64 = c
                        .query_row("SELECT count(*) FROM sessions", [], |r| r.get(0))
                        .unwrap();
                    self.readers.push(c);
                }
                result
            }
            _ => return Err(format!("UNEXPECTED_CALLBACK:{op}")),
        };
        Ok(Some(result))
    }
    fn counts(&self) -> (i64, i64) {
        let c = Connection::open(&self.path).unwrap();
        (
            c.query_row("SELECT count(*) FROM sessions", [], |r| r.get(0))
                .unwrap(),
            c.query_row("SELECT count(*) FROM outbox", [], |r| r.get(0))
                .unwrap(),
        )
    }
    fn events(&self) -> i64 {
        Connection::open(&self.centre)
            .unwrap()
            .query_row("SELECT count(*) FROM events", [], |r| r.get(0))
            .unwrap()
    }
    fn payloads(&self) -> Vec<Value> {
        let c = Connection::open(&self.path).unwrap();
        let mut s = c.prepare("SELECT payload FROM outbox").unwrap();
        s.query_map([], |r| r.get::<_, String>(0))
            .unwrap()
            .map(|r| serde_json::from_str(&r.unwrap()).unwrap())
            .collect()
    }
    fn worker(&self) -> Worker {
        let mut w = Worker::new();
        w.call("collector.init", json!({"path":self.path}), |_, _| {
            panic!("unexpected callback")
        })
        .unwrap();
        w
    }
    fn worker_tick(&mut self, w: &mut Worker) -> Result<Value> {
        w.call("collector.tick", json!({"outbox_limit":2048}), |op, a| {
            self.callback(op, a)
        })
    }
}
fn get(r: &Runtime, key: &str) -> Value {
    r.invoke("collector.get", &json!({"key":key}), |_, _| {
        panic!("unexpected callback")
    })
    .unwrap()
}
#[test]
fn collector_singleton_durable_stream_and_restart() {
    let mut h = Harness::new();
    let r = h.open();
    assert_eq!(
        Runtime::default()
            .invoke("collector.init", &json!({"path":h.path}), |_, _| panic!())
            .unwrap_err(),
        "COLLECTOR_ALREADY_RUNNING"
    );
    assert_eq!(h.tick(&r).unwrap()["delay"], 2);
    assert_eq!(h.counts(), (1, 3));
    let (stream, seq) = (get(&r, "stream"), get(&r, "seq"));
    drop(r);
    let r = h.open();
    h.tick(&r).unwrap();
    assert_eq!(get(&r, "stream"), stream);
    assert_eq!(get(&r, "seq"), seq);
    assert_eq!(h.counts(), (1, 0));
    assert_eq!(h.events(), 3);
}
#[test]
fn unknown_upload_ack_replays_only_identical_observation_receipts() {
    let mut h = Harness::new();
    let r = h.open();
    h.tick(&r).unwrap();
    h.lose_ack = true;
    assert_eq!(h.tick(&r).unwrap()["status"], "OFFLINE_BUFFERING");
    assert_eq!(h.counts().1, 3);
    let seq = get(&r, "seq");
    drop(r);
    let r = h.open();
    h.tick(&r).unwrap();
    assert_eq!(h.uploads[0], h.uploads[1]);
    assert_eq!(h.counts().1, 0);
    assert_eq!(get(&r, "seq"), seq);
    assert_eq!(h.events(), 3);
}

#[test]
fn history_backlog_drains_in_order_before_resampling_live_evidence() {
    let mut h = Harness::new();
    let r = h.open();
    h.tick(&r).unwrap();
    let payloads = h.payloads();
    let c = Connection::open(&h.path).unwrap();
    for seq in 4..=260 {
        c.execute(
            "INSERT INTO outbox VALUES (?,?)",
            params![
                seq,
                serde_json::to_string(&payloads[(seq as usize - 1) % 3]).unwrap()
            ],
        )
        .unwrap();
    }
    c.execute("UPDATE meta SET value='260' WHERE key='seq'", [])
        .unwrap();
    drop(c);
    h.require_empty_before_sample = true;
    h.tick(&r).unwrap();
    assert_eq!(h.counts().1, 0);
    let seqs: Vec<u64> = h
        .uploads
        .iter()
        .flat_map(|b| b["events"].as_array().unwrap())
        .map(|v| v["seq"].as_u64().unwrap())
        .collect();
    assert_eq!(seqs, (1..=260).collect::<Vec<_>>());
    assert_eq!(h.uploads.len(), 9);
    assert_eq!(get(&r, "seq"), 260);
}

#[test]
fn stalled_ack_retains_spool_and_does_not_generate_more_history() {
    let mut h = Harness::new();
    let r = h.open();
    h.tick(&r).unwrap();
    let sampled = h.sampled.len();
    let original = h.payloads();
    h.ack = Some(json!(0));
    assert_eq!(h.tick(&r).unwrap()["status"], "OBSERVATION_ACK_PENDING");
    assert_eq!(h.uploads.len(), 1);
    assert_eq!(h.sampled.len(), sampled);
    assert_eq!(h.payloads(), original);
}
fn invalid_ack(ack: Value) {
    let mut h = Harness::new();
    let r = h.open();
    h.tick(&r).unwrap();
    let before = get(&r, "seq");
    h.ack = Some(ack);
    assert_eq!(h.tick(&r).unwrap_err(), "OBSERVATION_ACK_CONFLICT");
    assert_eq!(h.counts(), (1, 3));
    assert_eq!(get(&r, "seq"), before);
}
macro_rules! invalid_ack {
    ($name:ident,$ack:expr) => {
        #[test]
        fn $name() {
            invalid_ack(json!($ack));
        }
    };
}
invalid_ack!(invalid_ack_negative, -1);
invalid_ack!(invalid_ack_bool, true);
invalid_ack!(invalid_ack_ahead, 999);
invalid_ack!(invalid_ack_null, Value::Null);
invalid_ack!(invalid_ack_string, "1");
#[test]
fn ack_from_wrong_stream_cannot_delete_queued_content() {
    let mut h = Harness::new();
    let r = h.open();
    h.tick(&r).unwrap();
    h.wrong_stream = true;
    assert_eq!(h.tick(&r).unwrap_err(), "OBSERVATION_ACK_CONFLICT");
    assert_eq!(h.counts().1, 3);
}
#[test]
fn worker_death_after_centre_commit_recovers_exact_batch() {
    let mut h = Harness::new();
    let mut w = h.worker();
    h.worker_tick(&mut w).unwrap();
    h.kill_at = Some("upload");
    assert_eq!(h.worker_tick(&mut w).unwrap_err(), "WORKER_UNAVAILABLE");
    assert!(w.child.try_wait().unwrap().is_some());
    drop(w);
    assert_eq!(h.counts().1, 3);
    let mut w = h.worker();
    h.worker_tick(&mut w).unwrap();
    assert_eq!(h.uploads[0], h.uploads[1]);
    assert_eq!(h.counts().1, 0);
    assert_eq!(h.events(), 3);
}
#[test]
fn worker_death_before_sampling_commit_rolls_back_every_checkpoint() {
    let mut h = Harness::new();
    let mut w = h.worker();
    h.kill_at = Some("sample");
    assert_eq!(h.worker_tick(&mut w).unwrap_err(), "WORKER_UNAVAILABLE");
    assert!(w.child.try_wait().unwrap().is_some());
    drop(w);
    assert_eq!(h.counts(), (0, 0));
    let mut w = h.worker();
    assert_eq!(
        w.call("collector.get", json!({"key":"seq"}), |_, _| panic!())
            .unwrap(),
        0
    );
    h.worker_tick(&mut w).unwrap();
    assert_eq!(h.counts(), (1, 3));
}
#[test]
fn spool_checkpoint_and_scrubbing_are_one_transaction() {
    let mut h = Harness::new();
    let r = h.open();
    h.malformed = true;
    assert!(h.tick(&r).is_err());
    assert_eq!(h.counts(), (0, 0));
    assert_eq!(get(&r, "seq"), 0);
    assert_eq!(get(&r, "generation"), Value::Null);
    h.malformed = false;
    h.tick(&r).unwrap();
    assert_eq!(h.counts(), (1, 3));
    assert!(h.payloads().iter().all(|e| {
        e["native_thread_id"]
            .as_str()
            .is_some_and(|s| !s.is_empty())
    }));
}
#[test]
fn no_first_use_offline_grant_guess_and_no_native_writes() {
    let mut h = Harness::new();
    let before = snapshot(&h.native.root);
    h.online = false;
    let r = h.open();
    let tick = h.tick(&r).unwrap();
    assert_eq!(tick["delay"], 12);
    assert_eq!(tick["status"], "AWAITING_BILATERAL_APPROVAL");
    assert_eq!(h.counts(), (0, 0));
    assert_eq!(snapshot(&h.native.root), before);
}
#[test]
fn scope_narrowing_uploads_only_redacted_sequences() {
    let mut h = Harness::new();
    let r = h.open();
    h.tick(&r).unwrap();
    h.scope["projects"] = json!(["/other"]);
    h.tick(&r).unwrap();
    assert_eq!(
        h.uploads.last().unwrap()["events"],
        json!([{"seq":1,"scope_redacted":true},{"seq":2,"scope_redacted":true},{"seq":3,"scope_redacted":true}])
    );
    assert_eq!(h.counts().1, 0);
}
#[test]
fn offline_backpressure_keeps_source_checkpoint() {
    let mut h = Harness::new();
    let r = h.open();
    h.tick(&r).unwrap();
    let seq = get(&r, "seq");
    h.online = false;
    let tick = h.call(&r, "tick", json!({"outbox_limit":130})).unwrap();
    assert_eq!(tick["delay"], 2);
    assert_eq!(tick["status"], "OBSERVATION_BACKPRESSURE");
    assert_eq!(get(&r, "seq"), seq);
    assert_eq!(h.counts().1, 3);
}
#[test]
fn coverage_recovery_republishes_unchanged_turns() {
    let mut h = Harness::new();
    let r = h.open();
    h.tick(&r).unwrap();
    h.failed_coverage = true;
    h.tick(&r).unwrap();
    let seq = get(&r, "seq").as_i64().unwrap();
    h.failed_coverage = false;
    h.tick(&r).unwrap();
    assert!(get(&r, "seq").as_i64().unwrap() > seq);
    let events = h.payloads();
    assert!(!events.is_empty());
    assert!(events.iter().all(|e| e["coverage"] == "INDEXED"));
    assert!(events.iter().any(|e| e["turn"]["turn_id"].is_string()));
}
#[test]
fn recovery_queue_cannot_be_starved_by_active_sessions() {
    let mut h = Harness::new();
    let r = h.open();
    let generation = h.source.invoke("generation", &json!({})).unwrap();
    h.call(&r, "put", json!({"key":"generation","value":generation}))
        .unwrap();
    h.no_inventory = true;
    let c = Connection::open(&h.path).unwrap();
    for i in 0..10 {
        let id = format!("active-{i}");
        c.execute(
            "INSERT INTO sessions VALUES (?,?,?,?)",
            params![
                id,
                json!({"id":id,"cwd":"/approved"}).to_string(),
                json!({"active":true}).to_string(),
                i
            ],
        )
        .unwrap();
    }
    c.execute(
        "INSERT INTO sessions VALUES (?,?,?,?)",
        params![
            "recover",
            json!({"id":"recover","cwd":"/approved"}).to_string(),
            json!({"active":false,"hashes":{"metadata":"old-error"}}).to_string(),
            100
        ],
    )
    .unwrap();
    h.tick(&r).unwrap();
    assert!(h.sampled.iter().any(|v| v == "recover"));
    assert!(h.sampled.len() <= 16);
}
#[test]
fn native_canonical_merge_has_exact_evidence_and_no_multiple_task_owner() {
    let proof = json!({"thread_id":"t","turn_id":"turn","observed_at":10,"liveness":"LIVE"});
    let sample = |multiple| {
        observation_source::invoke(
            "source.sample",
            &json!({"metadata":{"id":"t","cwd":"/approved"},"state":{}}),
            |op, a| {
                Ok(match op {
                    "source.native_turns" => {
                        json!([[{"turn_id":"turn","ordinal":5,"native_state":"RUNNING","model":"gpt-fixture","live_observation":proof,"summary":"Native"}],"INDEXED",a["state"]])
                    }
                    "source.canonical_turns" => {
                        if multiple {
                            json!([[{"task_ref":"task-a","turn_id":"task-a","ordinal":1},{"task_ref":"task-b","turn_id":"task-b","ordinal":1}],a["state"]])
                        } else {
                            json!([[{"task_ref":"task","execution_ref":"exec","turn_id":"turn","ordinal":1,"summary":null,"native_state":"UNKNOWN"}],a["state"]])
                        }
                    }
                    "source.generation" => json!("fixture-generation"),
                    _ => panic!("unexpected callback"),
                })
            },
        )
    };
    let v = sample(false).unwrap();
    assert_eq!(v[0].as_array().unwrap().len(), 1);
    assert_eq!(v[0][0]["turn"]["ordinal"], 5);
    assert_eq!(v[0][0]["turn"]["summary"], "Native");
    assert_eq!(v[0][0]["turn"]["live_observation"], proof);
    assert_eq!(v[0][0]["turn"]["model"], "gpt-fixture");
    assert_eq!(v[0][0]["task_ref"], "task");
    assert_eq!(v[1]["active"], true);
    assert_eq!(sample(true).unwrap_err(), "OBSERVATION_IDENTITY_CONFLICT");
}
#[test]
fn completed_latest_turn_retires_historical_running_from_live_priority_only() {
    for latest in ["COMPLETED", "RUNNING"] {
        let value = observation_source::invoke("source.sample",
            &json!({"metadata":{"id":"t","cwd":"/approved"},"state":{}}), |op, args| {
            Ok(match op {
                "source.native_turns" => json!([[{"turn_id":"old","ordinal":1,"native_state":"RUNNING"},
                    {"turn_id":"latest","ordinal":2,"native_state":latest}],"INDEXED",args["state"]]),
                "source.canonical_turns" => json!([[],args["state"]]),
                "source.generation" => json!("generation"),
                _ => panic!("unexpected callback {op}"),
            })
        }).unwrap();
        assert_eq!(value[1]["active"], latest == "RUNNING");
        assert_eq!(value[0].as_array().unwrap().len(), 2);
        assert_eq!(value[0][0]["turn"]["native_state"], "RUNNING");
    }
}
#[test]
fn managed_lifecycle_prioritizes_observation_without_inventing_live_evidence() {
    for state in [
        "ADMITTED",
        "RUNNING",
        "FINALIZING",
        "SUCCEEDED",
        "BLOCKED",
        "FAILED",
        "CANCELLED",
    ] {
        for native in ["UNKNOWN", "RUNNING"] {
            let value = observation_source::invoke("source.sample",
                &json!({"metadata":{"id":"t","cwd":"/approved"},"state":{},"live_enabled":true}), |op, args| {
                Ok(match op {
                    "source.native_turns" => json!([[{"turn_id":"turn","ordinal":2,"native_state":native}],"INDEXED",args["state"]]),
                    "source.canonical_turns" => json!([[{"task_ref":"task","execution_ref":"exec","turn_id":"turn","ordinal":1,"execution_state":state,"native_state":"UNKNOWN"}],args["state"]]),
                    "source.observe_live" => json!({"thread_id":"t","turn_id":"turn","liveness":"UNKNOWN"}),
                    "source.generation" => json!("generation"),
                    _ => panic!("unexpected {op}"),
                })
            }).unwrap();
            assert_eq!(
                value[1]["active"],
                native == "RUNNING" || matches!(state, "ADMITTED" | "RUNNING" | "FINALIZING")
            );
            assert_eq!(value[0][0]["turn"]["native_state"], native);
        }
    }
}

#[test]
fn missing_managed_history_can_only_use_an_exact_codex_turn_status() {
    for (identity, status) in [
        ("turn", json!("RUNNING")),
        ("different-turn", json!("RUNNING")),
        ("turn", Value::Null),
    ] {
        let value = observation_source::invoke("source.sample",
            &json!({"metadata":{"id":"t","cwd":"/approved"},"state":{},"live_enabled":true}), |op, args| {
            Ok(match op {
                "source.native_turns" => json!([[],"INDEXED",args["state"]]),
                "source.canonical_turns" => json!([[{"task_ref":"task","execution_ref":"exec","turn_id":"turn","ordinal":1,"execution_state":"RUNNING","native_state":"UNKNOWN"}],args["state"]]),
                "source.observe_live" => json!({"thread_id":"t","turn_id":identity,"liveness":"LIVE","native_state":status}),
                "source.generation" => json!("generation"),
                _ => panic!("unexpected {op}"),
            })
        }).unwrap();
        assert_eq!(
            value[0][0]["turn"]["native_state"],
            if identity == "turn" && status == "RUNNING" {
                "RUNNING"
            } else {
                "UNKNOWN"
            }
        );
        assert_eq!(value[1]["active"], true);
    }
}

#[test]
fn slow_sample_yields_for_delivery_without_losing_discovered_sessions() {
    let mut h = Harness::new();
    let r = h.open();
    let generation = h.source.invoke("generation", &json!({})).unwrap();
    h.call(&r, "put", json!({"key":"generation","value":generation}))
        .unwrap();
    h.no_inventory = true;
    h.slow_sample = true;
    let c = Connection::open(&h.path).unwrap();
    for (id, active, checked) in [("live", true, 2), ("history", false, 1)] {
        c.execute(
            "INSERT INTO sessions VALUES (?,?,?,?)",
            params![
                id,
                json!({"id":id,"cwd":"/approved"}).to_string(),
                json!({"active":active}).to_string(),
                checked
            ],
        )
        .unwrap();
    }
    h.tick(&r).unwrap();
    assert_eq!(h.sampled, vec!["live"]);
    h.tick(&r).unwrap();
    assert_eq!(h.sampled, vec!["live", "history"]);
    assert_eq!(h.counts().0, 2);
}
#[test]
fn commit_busy_rolls_back_before_the_next_tick() {
    let mut h = Harness::new();
    let r = h.open();
    h.hold_readers = true;
    assert_eq!(h.tick(&r).unwrap_err(), "COLLECTOR_STORE_UNAVAILABLE");
    assert_eq!(get(&r, "seq"), 0);
    assert_eq!(h.counts(), (0, 0));
    for c in h.readers.drain(..) {
        c.execute_batch("ROLLBACK").unwrap();
    }
    h.hold_readers = false;
    h.tick(&r).unwrap();
    assert_eq!(h.counts(), (1, 3));
}
