//! Tunnel recovery policy and durable receipts. No task replay or execution authority.
use regex::Regex;
use serde_json::{Value, json};
use std::{fs, io::Write, path::Path};
type Result<T> = std::result::Result<T, String>;
fn number(v: &Value) -> Result<f64> {
    v.as_f64()
        .filter(|n| n.is_finite())
        .ok_or("INVALID_POLICY".into())
}
fn policy(p: &Value) -> Result<()> {
    for name in [
        "stale_after",
        "startup_grace",
        "interval",
        "probe_timeout",
        "stop_timeout",
        "cooldown",
        "failure_grace",
        "max_restarts",
        "stable_for",
    ] {
        if p[name].as_f64().is_none_or(|n| !n.is_finite() || n <= 0.) {
            return Err(format!("INVALID_POLICY_{name}"));
        }
    }
    if p["max_restarts"].as_u64().is_none() {
        return Err("INVALID_RESTART_LIMIT".into());
    }
    Ok(())
}
pub fn process_start(pid: &Value) -> Option<String> {
    let text = fs::read_to_string(format!("/proc/{}/stat", pid.as_u64()?)).ok()?;
    let fields: Vec<_> = text.rsplit_once(')')?.1.split_whitespace().collect();
    if fields.first() == Some(&"Z") {
        None
    } else {
        fields.get(19).map(|v| v.to_string())
    }
}
fn group_live(pid: u64) -> Result<bool> {
    for entry in fs::read_dir("/proc").map_err(|_| "PROCESS_STATE_UNAVAILABLE")? {
        let entry = entry.map_err(|_| "PROCESS_STATE_UNAVAILABLE")?;
        if entry.file_name().to_string_lossy().parse::<u64>().is_err() {
            continue;
        }
        let text = match fs::read_to_string(entry.path().join("stat")) {
            Ok(v) => v,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(_) => return Err("PROCESS_STATE_UNAVAILABLE".into()),
        };
        let (_, tail) = text.rsplit_once(')').ok_or("PROCESS_STATE_INVALID")?;
        let fields: Vec<_> = tail.split_whitespace().collect();
        if fields
            .get(2)
            .ok_or("PROCESS_STATE_INVALID")?
            .parse::<u64>()
            .map_err(|_| "PROCESS_STATE_INVALID")?
            == pid
            && fields.first() != Some(&"Z")
        {
            return Ok(true);
        }
    }
    Ok(false)
}
fn poll(a: &Value) -> Result<Value> {
    let metrics = a["metrics"].as_str().ok_or("INVALID_METRICS")?;
    let re = Regex::new(
        r"(?m)^commands_poll_last_successful_timestamp_seconds(?:\{[^}\n]*\})?\s+(\S+)$",
    )
    .unwrap();
    let samples: Vec<_> = re.captures_iter(metrics).collect();
    let stamp = if samples.len() == 1 {
        samples[0][1]
            .parse::<f64>()
            .ok()
            .filter(|n| n.is_finite() && *n > 0.)
    } else {
        None
    };
    let now = number(&a["now"])?;
    let max_age = number(&a["max_age"])?;
    let age = stamp.map(|v| now - v);
    let mut errors = serde_json::Map::new();
    let re = Regex::new(
        r#"(?m)^commands_poll_errors_total\{[^\n]*error_kind="([^"\n]+)"[^\n]*\}\s+(\S+)$"#,
    )
    .unwrap();
    for v in re.captures_iter(metrics) {
        if matches!(&v[1], "timeout" | "other")
            && let Ok(n) = v[2].parse::<f64>()
            && n.is_finite()
            && n >= 0.
        {
            errors.insert(v[1].to_string(), json!(n));
        }
    }
    Ok(
        json!({"last_poll":stamp,"poll_age":age,"fresh":age.is_some_and(|n|n>=0. && n<=max_age),"poll_errors":errors,"probe_error":if stamp.is_some(){Value::Null}else{json!("INVALID_POLL_SAMPLE")}}),
    )
}
fn acquire(a: &Value) -> Result<Value> {
    let file = Path::new(
        a["directory"]
            .as_str()
            .ok_or("PRIVATE_STATE_DIRECTORY_REQUIRED")?,
    )
    .join("state.json");
    if !file.exists() {
        return Ok(json!({"identity":a["identity"],"launches":0}));
    }
    let state: Value = serde_json::from_slice(&fs::read(file).map_err(|_| "STATE_UNAVAILABLE")?)
        .map_err(|_| "INVALID_STATE")?;
    if state["identity"] != a["identity"] || state["launches"].as_u64().is_none() {
        return Err("STATE_IDENTITY_OR_BUDGET_INVALID".into());
    }
    if state["reason"] == "LAUNCH_INTENT" {
        return Err("SPAWN_OUTCOME_UNKNOWN_NO_DUPLICATE_CONSUMER".into());
    }
    if !state["child_pid"].is_null() && state["child_pid"] != 0 {
        let id = state["child_pid"]
            .as_u64()
            .filter(|n| *n > 0 && *n <= i32::MAX as u64)
            .ok_or("PRIOR_CHILD_IDENTITY_INVALID")?;
        let pid =
            rustix::process::Pid::from_raw(id as i32).ok_or("PRIOR_CHILD_IDENTITY_INVALID")?;
        match rustix::process::test_kill_process(pid) {
            Err(rustix::io::Errno::SRCH) => {
                if group_live(id)? {
                    return Err("PRIOR_GROUP_MAY_BE_LIVE_NO_DUPLICATE_CONSUMER".into());
                }
            }
            _ => return Err("PRIOR_CHILD_MAY_BE_LIVE_NO_ADOPTION_OR_DUPLICATION".into()),
        }
    }
    Ok(state)
}
fn record(a: &Value) -> Result<Value> {
    let mut state = a["state"].as_object().ok_or("INVALID_STATE")?.clone();
    let changed =
        state.get("status") != Some(&a["status"]) || state.get("reason") != Some(&a["reason"]);
    for key in [
        "status",
        "reason",
        "updated_at",
        "supervisor_pid",
        "supervisor_start",
        "policy",
    ] {
        state.insert(key.into(), a[key].clone());
    }
    for (k, v) in a["fields"].as_object().ok_or("INVALID_STATE")? {
        state.insert(k.clone(), v.clone());
    }
    let dir = Path::new(
        a["directory"]
            .as_str()
            .ok_or("PRIVATE_STATE_DIRECTORY_REQUIRED")?,
    );
    let mut file = tempfile::Builder::new()
        .prefix(".state-")
        .tempfile_in(dir)
        .map_err(|_| "STATE_WRITE_FAILED")?;
    file.write_all(&serde_json::to_vec(&state).map_err(|_| "INVALID_STATE")?)
        .map_err(|_| "STATE_WRITE_FAILED")?;
    file.as_file()
        .sync_all()
        .map_err(|_| "STATE_WRITE_FAILED")?;
    file.persist(dir.join("state.json"))
        .map_err(|_| "STATE_WRITE_FAILED")?;
    fs::File::open(dir)
        .and_then(|f| f.sync_all())
        .map_err(|_| "STATE_WRITE_FAILED")?;
    Ok(json!({"state":state,"changed":changed}))
}
fn step(a: &Value) -> Result<Value> {
    let p = &a["policy"];
    policy(p)?;
    let mut state = a["state"].clone();
    let mut timing = a["timing"].clone();
    let sample = &a["sample"];
    let now = number(&a["now"])?;
    let exited = a["exited"] == true;
    let fresh =
        sample["fresh"] == true && number(&sample["last_poll"])? >= number(&a["launched_wall"])?;
    let (status, reason, action);
    if fresh && !exited {
        if timing["healthy_since"].is_null() {
            timing["healthy_since"] = json!(now);
        }
        if timing["healthy_start_poll"].is_null() {
            timing["healthy_start_poll"] = sample["last_poll"].clone();
        }
        if now - number(&timing["healthy_since"])? >= number(&p["stable_for"])?
            && number(&sample["last_poll"])? > number(&timing["healthy_start_poll"])?
        {
            state["launches"] = json!(1);
        }
        if let Some(since) = timing["failure_started"].as_f64() {
            state["recovery_seconds"] = json!(now - since);
        }
        timing["failure_started"] = Value::Null;
        status = "HEALTHY";
        reason = "FRESH_CONTROL_POLL";
        action = "wait";
    } else {
        timing["healthy_since"] = Value::Null;
        timing["healthy_start_poll"] = Value::Null;
        if timing["failure_started"].is_null() {
            timing["failure_started"] = json!(now);
        }
        reason = if exited {
            "CHILD_EXITED"
        } else {
            "POLL_STALE_OR_UNAVAILABLE"
        };
        let recover = exited
            || (now - number(&a["launched_mono"])? >= number(&p["startup_grace"])?
                && now - number(&timing["failure_started"])? >= number(&p["failure_grace"])?);
        status = if recover { "BACKOFF" } else { "DEGRADED" };
        action = if recover { "recover" } else { "wait" };
    }
    Ok(json!({"state":state,"timing":timing,"status":status,"reason":reason,"action":action}))
}
pub fn invoke(op: &str, a: &Value) -> Result<Value> {
    match op {
        "tunnel.policy" => {
            policy(&a["policy"])?;
            Ok(Value::Null)
        }
        "tunnel.poll" => poll(a),
        "tunnel.process_start" => Ok(json!(process_start(&a["pid"]))),
        "tunnel.group_live" => Ok(json!(group_live(
            a["pgid"].as_u64().ok_or("PROCESS_STATE_INVALID")?
        )?)),
        "tunnel.acquire" => acquire(a),
        "tunnel.record" => record(a),
        "tunnel.step" => step(a),
        "tunnel.launch" => {
            policy(&a["policy"])?;
            let mut state = a["state"].clone();
            let n = state["launches"]
                .as_u64()
                .ok_or("STATE_IDENTITY_OR_BUDGET_INVALID")?;
            let limit = a["policy"]["max_restarts"]
                .as_u64()
                .unwrap()
                .checked_add(1)
                .ok_or("INVALID_RESTART_LIMIT")?;
            let allowed = n < limit;
            if allowed {
                state["launches"] = json!(n + 1);
                state["launch_sequence"] = json!(
                    state["launch_sequence"]
                        .as_u64()
                        .unwrap_or(0)
                        .checked_add(1)
                        .ok_or("STATE_IDENTITY_OR_BUDGET_INVALID")?
                );
            }
            Ok(json!({"allowed":allowed,"state":state}))
        }
        "tunnel.status" => {
            let path =
                Path::new(a["directory"].as_str().ok_or("INVALID_STATE")?).join("state.json");
            let mut s: Value =
                serde_json::from_slice(&fs::read(path).map_err(|_| "STATE_UNAVAILABLE")?)
                    .map_err(|_| "INVALID_STATE")?;
            policy(&s["policy"])?;
            let now = number(&a["now"])?;
            let valid = s["status"] == "HEALTHY"
                && s["supervisor_start"]
                    .as_str()
                    .is_some_and(|v| process_start(&s["supervisor_pid"]).as_deref() == Some(v))
                && s["child_start"]
                    .as_str()
                    .is_some_and(|v| process_start(&s["child_pid"]).as_deref() == Some(v))
                && s["updated_at"].as_f64().is_some_and(|v| {
                    0. <= now - v
                        && now - v
                            <= s["policy"]["interval"].as_f64().unwrap()
                                + s["policy"]["probe_timeout"].as_f64().unwrap()
                                + 2.
                })
                && s["last_poll"].as_f64().is_some_and(|v| {
                    0. <= now - v && now - v <= s["policy"]["stale_after"].as_f64().unwrap()
                });
            s["currently_healthy"] = json!(valid);
            s["dispatch_verified"] = json!(false);
            Ok(s)
        }
        _ => Err("TUNNEL_OPERATION_UNKNOWN".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn p() -> Value {
        json!({"stale_after":120,"startup_grace":45,"interval":5,"probe_timeout":3,"stop_timeout":5,"cooldown":10,"failure_grace":15,"max_restarts":3,"stable_for":300})
    }
    #[test]
    fn invalid_and_ambiguous_metrics_never_prove_a_poll() {
        for value in ["", "NaN", "Inf", "-Inf", "nonsense", "-1", "0", "1001"] {
            assert_eq!(poll(&json!({"metrics":format!("commands_poll_last_successful_timestamp_seconds {value}"),"now":1000,"max_age":120})).unwrap()["fresh"],false);
        }
        let line = "commands_poll_last_successful_timestamp_seconds 999\n";
        assert_eq!(
            poll(&json!({"metrics":line.repeat(2),"now":1000,"max_age":120})).unwrap()["fresh"],
            false
        );
    }
    #[test]
    fn lost_spawn_receipt_cannot_reset_or_replay_budget() {
        let dir = tempfile::tempdir().unwrap();
        let a = json!({"directory":dir.path(),"identity":"same"});
        fs::write(
            dir.path().join("state.json"),
            br#"{"identity":"same","launches":1,"reason":"LAUNCH_INTENT"}"#,
        )
        .unwrap();
        assert_eq!(
            acquire(&a).unwrap_err(),
            "SPAWN_OUTCOME_UNKNOWN_NO_DUPLICATE_CONSUMER"
        );
        assert!(
            invoke(
                "tunnel.launch",
                &json!({"state":{"launches":4,"launch_sequence":4},"policy":p()})
            )
            .unwrap()["allowed"]
                == false
        );
    }
    #[test]
    fn exact_existing_live_child_is_never_adopted_or_signalled() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(
            dir.path().join("state.json"),
            serde_json::to_vec(
                &json!({"identity":"same","launches":1,"child_pid":std::process::id()}),
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(
            acquire(&json!({"directory":dir.path(),"identity":"same"})).unwrap_err(),
            "PRIOR_CHILD_MAY_BE_LIVE_NO_ADOPTION_OR_DUPLICATION"
        );
    }
    #[test]
    fn healthy_but_nonadvancing_poll_does_not_reset_budget() {
        let mut a = json!({"policy":p(),"state":{"launches":3},"timing":{"healthy_since":1,"healthy_start_poll":1000,"failure_started":null},"sample":{"fresh":true,"last_poll":1000},"now":400,"launched_wall":999,"launched_mono":0,"exited":false});
        assert_eq!(step(&a).unwrap()["state"]["launches"], 3);
        a["sample"]["last_poll"] = json!(1001);
        assert_eq!(step(&a).unwrap()["state"]["launches"], 1);
    }
    #[test]
    fn prior_generation_poll_is_not_a_fresh_recovery() {
        let a = json!({"policy":p(),"state":{"launches":1},"timing":{"healthy_since":null,"healthy_start_poll":null,"failure_started":0},"sample":{"fresh":true,"last_poll":998},"now":100,"launched_wall":999,"launched_mono":0,"exited":false});
        assert_eq!(step(&a).unwrap()["action"], "recover");
    }
    #[test]
    fn receipt_is_durable_and_unknown_state_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let a = json!({"directory":dir.path(),"state":{"identity":"same","launches":1},"status":"STARTING","reason":"LAUNCH_INTENT","updated_at":1000,"supervisor_pid":std::process::id(),"supervisor_start":process_start(&json!(std::process::id())),"policy":p(),"fields":{"child_pid":null}});
        let r = record(&a).unwrap();
        assert_eq!(
            r["state"],
            serde_json::from_slice::<Value>(&fs::read(dir.path().join("state.json")).unwrap())
                .unwrap()
        );
        assert!(acquire(&json!({"directory":dir.path(),"identity":"different"})).is_err());
    }
}
