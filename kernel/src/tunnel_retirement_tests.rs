//! Original deterministic-clock obligations, exercised directly against Engine.
use super::*;
use std::{cell::RefCell, rc::Rc};
struct Clock {
    now: f64,
    first: Option<f64>,
    stopped: bool,
    restored: bool,
    continuous: bool,
    unconfirmed: bool,
    signals: Vec<Value>,
}
struct Fake(Rc<RefCell<Clock>>);
impl Io for Fake {
    fn call(&mut self, op: &str, a: Value, s: &Value) -> Result<Value> {
        let mut c = self.0.borrow_mut();
        match op {
            "lock" | "unlock" | "event" | "child_clear" => Ok(Value::Null),
            "time" | "monotonic" => Ok(json!(c.now)),
            "spawn" => Ok(json!(999999999)),
            "poll" => Ok(json!(false)),
            "sample" => {
                if c.continuous {
                    if s["state"]["launches"] == 1
                        && c.now - s["launched_mono"].as_f64().unwrap() > 0.4
                    {
                        c.stopped = true;
                    }
                    return Ok(json!({"fresh":true,"last_poll":c.now,"poll_age":0}));
                }
                c.now += 3.;
                if c.first.is_none() {
                    c.first = Some(c.now);
                    return Ok(json!({"fresh":true,"last_poll":c.now,"poll_age":0}));
                }
                if s["state"]["launch_sequence"].as_u64().unwrap() > 1 && c.restored {
                    c.stopped = true;
                    return Ok(json!({"fresh":true,"last_poll":c.now,"poll_age":0}));
                }
                invoke(
                    "tunnel.poll",
                    &json!({"metrics":format!("commands_poll_last_successful_timestamp_seconds {}",c.first.unwrap()),"now":c.now,"max_age":120}),
                )
            }
            "terminate_override" => {
                c.now += 5.;
                Ok(json!({"child_pid":null}))
            }
            "signal" => {
                c.signals.push(a);
                Ok(Value::Null)
            }
            "child_wait" => {
                c.now += a["timeout"].as_f64().unwrap();
                Ok(json!(!c.unconfirmed))
            }
            "group_live" => Ok(json!(false)),
            "stopped" => Ok(json!(c.stopped)),
            "wait" | "sleep" => {
                c.now += a.as_f64().unwrap();
                Ok(json!(c.stopped))
            }
            _ => panic!("unexpected {op}"),
        }
    }
}
fn fixture(dir: &Path, policy: Value) -> (Engine<Fake>, Rc<RefCell<Clock>>) {
    let c = Rc::new(RefCell::new(Clock {
        now: 1000.,
        first: None,
        stopped: false,
        restored: false,
        continuous: false,
        unconfirmed: false,
        signals: vec![],
    }));
    let config = json!({"directory":dir,"command":["/bin/true"],"identity":"private-clock","policy":policy,"supervisor_pid":std::process::id(),"supervisor_start":process_start(&json!(std::process::id()))});
    (Engine::new(Fake(c.clone()), config).unwrap(), c)
}
#[test]
fn default_policy_deadlines_preserve_both_original_fault_clock_bounds() {
    for restored in [true, false] {
        let t = tempfile::tempdir().unwrap();
        let (mut engine, c) = fixture(t.path(), default_policy());
        c.borrow_mut().restored = restored;
        assert_eq!(engine.run().unwrap(), if restored { 0 } else { 78 });
        let c = c.borrow();
        assert!(c.now - c.first.unwrap() <= if restored { 219. } else { 370. });
        assert_eq!(
            engine.state["launch_sequence"],
            if restored { 2 } else { 4 }
        );
    }
}
#[test]
fn continuous_fresh_polls_are_required_to_reset_existing_budget() {
    let t = tempfile::tempdir().unwrap();
    let policy = json!({"stale_after":0.3,"startup_grace":0.5,"interval":0.05,"probe_timeout":0.3,"stop_timeout":0.2,"cooldown":0.1,"failure_grace":0.1,"max_restarts":2,"stable_for":0.25});
    let (mut engine, c) = fixture(t.path(), policy);
    c.borrow_mut().continuous = true;
    engine.acquire().unwrap();
    engine.state["launches"] = json!(2);
    engine.record("STOPPED", "FIXTURE", json!({})).unwrap();
    assert_eq!(engine.run().unwrap(), 0);
    assert_eq!(engine.state["launches"], 1);
}
#[test]
fn unconfirmed_termination_retains_exact_child_and_stops_recovery() {
    let t = tempfile::tempdir().unwrap();
    let (mut engine, c) = fixture(t.path(), default_policy());
    c.borrow_mut().unconfirmed = true;
    engine.acquire().unwrap();
    engine.child = Some(999999999);
    assert_eq!(
        engine.terminate(false).unwrap_err(),
        "CHILD_TERMINATION_UNCONFIRMED"
    );
    assert_eq!(engine.state["status"], "HOLD");
    assert_eq!(engine.state["child_pid"], 999999999);
    assert!(engine.stop_unconfirmed);
    assert_eq!(
        c.borrow().signals,
        json!([{"pid":999999999,"signal":15},{"pid":999999999,"signal":9}])
            .as_array()
            .unwrap()
            .to_vec()
    );
}
