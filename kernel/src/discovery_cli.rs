//! Production devices/pair/serve orchestration over the existing Rust discovery
//! and enrollment contracts. Presence is never execution authorization.
use crate::{
    discovery_bootstrap, discovery_enrollment, discovery_identity as identity, discovery_index,
    discovery_state, mdns_runtime, node_tls::Result, runtime_config,
};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    io::{IsTerminal, Write},
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

#[derive(Debug)]
struct Options {
    config: PathBuf,
    command: String,
    state: Option<PathBuf>,
    display: Option<String>,
    wait: f64,
    json: bool,
    device: Option<String>,
    observer: Option<PathBuf>,
    port: u16,
}
fn interval(value: &str) -> Result<f64> {
    let value = value
        .trim()
        .chars()
        .map(|c| {
            crate::python_numeric::integer_text(&c.to_string())
                .ok()
                .and_then(|s| s.chars().next())
                .unwrap_or(c)
        })
        .collect::<String>();
    let bytes = value.as_bytes();
    if bytes.iter().enumerate().any(|(i, b)| {
        *b == b'_'
            && (i == 0
                || i + 1 == bytes.len()
                || !bytes[i - 1].is_ascii_digit()
                || !bytes[i + 1].is_ascii_digit())
    }) {
        return Err("INVALID_DISCOVERY_INTERVAL".into());
    }
    value
        .replace('_', "")
        .parse()
        .map_err(|_| "INVALID_DISCOVERY_INTERVAL".into())
}
impl Options {
    fn parse(args: Vec<String>) -> Result<Self> {
        let mut out = Self {
            config: "bridge.toml".into(),
            command: String::new(),
            state: None,
            display: None,
            wait: 2.,
            json: false,
            device: None,
            observer: None,
            port: 0,
        };
        let mut args = args.into_iter();
        while let Some(arg) = args.next() {
            let (flag, inline) = arg
                .split_once('=')
                .map(|(a, b)| (a, Some(b.to_owned())))
                .unwrap_or((&arg, None));
            let mut value = || {
                inline
                    .clone()
                    .or_else(|| args.next())
                    .ok_or_else(|| "CLI_ARGUMENT_REQUIRED".to_owned())
            };
            match flag {
                "--config" if out.command.is_empty() => out.config = value()?.into(),
                "devices" | "pair" | "serve" if out.command.is_empty() => out.command = flag.into(),
                "--state-dir" if !out.command.is_empty() => out.state = Some(value()?.into()),
                "--display-name" if !out.command.is_empty() => out.display = Some(value()?),
                "--wait" if !out.command.is_empty() => out.wait = interval(&value()?)?,
                "--json" if !out.command.is_empty() && inline.is_none() => out.json = true,
                "--observer-config" if matches!(out.command.as_str(), "pair" | "serve") => {
                    out.observer = Some(value()?.into())
                }
                "--port" if matches!(out.command.as_str(), "pair" | "serve") => {
                    out.port = crate::python_numeric::integer_text(&value()?)
                        .map_err(|_| "INVALID_PORT")?
                        .parse::<i64>()
                        .ok()
                        .and_then(|n| u16::try_from(n).ok())
                        .ok_or("INVALID_PORT")?
                }
                "--allow-dev-pairing" => return Err("SECURITY_BLOCKER_DEV_ONLY".into()),
                _ if out.command == "pair" && out.device.is_none() && !arg.starts_with('-') => {
                    out.device = Some(arg)
                }
                _ => return Err("CLI_ARGUMENT_INVALID".into()),
            }
        }
        if out.command.is_empty() {
            return Err("CLI_COMMAND_REQUIRED".into());
        }
        if !(0.0..=30.0).contains(&out.wait) {
            return Err("INVALID_DISCOVERY_INTERVAL".into());
        }
        Ok(out)
    }
    fn accepting(&self) -> bool {
        self.command == "pair" && self.device.as_deref() == Some("accept")
    }
}
fn absolute(path: &Path) -> Result<PathBuf> {
    let p = runtime_config::expanduser(path)?;
    if p.is_absolute() {
        Ok(p)
    } else {
        Ok(std::env::current_dir()
            .map_err(|_| "STATE_UNAVAILABLE")?
            .join(p))
    }
}
fn settings(options: &Options) -> Result<(PathBuf, String)> {
    let path = runtime_config::expanduser(&options.config)?;
    let raw: toml::Value = if path.exists() {
        toml::from_str(&std::fs::read_to_string(path).map_err(|_| "CONFIG_READ_FAILED")?)
            .map_err(|_| "TOMLDecodeError")?
    } else {
        toml::Value::Table(Default::default())
    };
    let runtime = raw.get("runtime");
    if runtime.is_some_and(|r| !r.is_table()) {
        return Err("ValueError".into());
    }
    for key in ["runtime_host", "task_db_path"] {
        if runtime
            .and_then(|r| r.get(key))
            .is_some_and(|v| !v.is_str())
        {
            return Err("ValueError".into());
        }
    }
    let host = runtime
        .and_then(|r| r.get("runtime_host"))
        .and_then(toml::Value::as_str)
        .filter(|v| !v.is_empty())
        .map(str::to_owned)
        .unwrap_or_else(runtime_config::hostname);
    let host = runtime_config::invoke("canonical_host", &json!({"value":host}), |_, _| {
        Err("CONFIG_CALLBACK_INVALID".into())
    })?
    .as_str()
    .ok_or("INVALID_NODE_ID")?
    .to_owned();
    let state = if let Some(root) = &options.state {
        root.clone()
    } else {
        let db = runtime
            .and_then(|r| r.get("task_db_path"))
            .and_then(toml::Value::as_str)
            .unwrap_or("~/.local/state/clinx/tasks.sqlite3");
        runtime_config::expanduser(Path::new(db))?
            .parent()
            .unwrap_or(Path::new(""))
            .join("devices")
    };
    Ok((absolute(&state)?, host))
}
fn peers(state: &discovery_state::Runtime, root: &Path) -> Result<Value> {
    state.invoke("discovery.peers.all", &json!({"root":root}), |_, _| {
        Err("DISCOVERY_CALLBACK_INVALID".into())
    })
}
fn call(
    enrollment: &discovery_enrollment::Runtime,
    state: Arc<discovery_state::Runtime>,
    op: &str,
    args: &Value,
) -> Result<Value> {
    enrollment.invoke(op, args, state, |_, _| {
        Err("DISCOVERY_CALLBACK_INVALID".into())
    })
}
fn reconnect(
    root: &Path,
    local: &Value,
    candidates: &Value,
    state: Arc<discovery_state::Runtime>,
    enrollment: &discovery_enrollment::Runtime,
) -> Result<BTreeMap<String, String>> {
    let trusted = peers(&state, root)?;
    let mut states = BTreeMap::new();
    for (node, candidate) in candidates.as_object().ok_or("INVALID_DISCOVERY_METADATA")? {
        if local["node_id"] == *node || trusted.get(node).is_none() {
            continue;
        }
        let result = call(
            enrollment,
            state.clone(),
            "discovery.enrollment.client.reconnect",
            &json!({"root":root,"candidate":candidate}),
        );
        let status = match result {
            Ok(_) => "AUTHENTICATED",
            Err(ref e) if e == "IDENTITY_MISMATCH" => "IDENTITY_MISMATCH",
            Err(_) => "AUTHENTICATION_FAILED",
        };
        states.insert(node.clone(), status.into());
    }
    Ok(states)
}
fn devices(
    root: &Path,
    local: &Value,
    candidates: &Value,
    state: Arc<discovery_state::Runtime>,
    enrollment: &discovery_enrollment::Runtime,
) -> Result<Value> {
    let states = reconnect(root, local, candidates, state.clone(), enrollment)?;
    let mut rows = discovery_index::invoke(
        "discovery_index.device_rows",
        &json!({"identity":identity::public_record(local)?,"trusted":peers(&state,root)?,"online":candidates}),
    )?;
    for row in rows.as_array_mut().ok_or("INVALID_DISCOVERY_METADATA")? {
        let status = states.get(row["node_id"].as_str().unwrap_or(""));
        row["authenticated"] = json!(status.is_some_and(|s| s == "AUTHENTICATED"));
        if status.is_some_and(|s| s == "IDENTITY_MISMATCH") {
            row["state"] = json!("Identity Mismatch");
        }
    }
    Ok(json!({"devices":rows,"security_status":"OPAQUE_V1"}))
}
fn choose(
    candidates: &Value,
    local: &Value,
    selector: Option<&str>,
    tty: bool,
    output: &mut impl Write,
    read: impl FnOnce() -> Result<String>,
) -> Result<Value> {
    let mut choices = candidates
        .as_object()
        .ok_or("INVALID_DISCOVERY_METADATA")?
        .values()
        .filter(|c| c["node_id"] != local["node_id"])
        .collect::<Vec<_>>();
    if let Some(selector) = selector.filter(|s| !s.is_empty()) {
        choices.retain(|c| c["node_id"] == selector || c["display_name"] == selector);
        if choices.len() != 1 {
            return Err("ValueError".into());
        }
        return Ok(choices[0].clone());
    }
    if choices.is_empty() {
        return Err("ValueError".into());
    }
    choices.sort_by_key(|c| c["node_id"].as_str().unwrap_or(""));
    for (i, candidate) in choices.iter().enumerate() {
        writeln!(
            output,
            "{}. {} ({})",
            i + 1,
            candidate["display_name"].as_str().unwrap_or(""),
            candidate["node_id"].as_str().unwrap_or("")
        )
        .map_err(|_| "DEVICE_OUTPUT_FAILED")?;
    }
    if !tty {
        return Err("ValueError".into());
    }
    write!(output, "Select device: ")
        .and_then(|_| output.flush())
        .map_err(|_| "DEVICE_OUTPUT_FAILED")?;
    let answer = read()?;
    let answer = answer.trim_end_matches(['\r', '\n']);
    if !regex::Regex::new(r"^\p{Nd}+$").unwrap().is_match(answer) {
        return Err("ValueError".into());
    }
    let index = crate::python_numeric::integer_text(answer)
        .map_err(|_| "ValueError")?
        .parse::<usize>()
        .map_err(|_| "ValueError")?;
    choices
        .get(index.wrapping_sub(1))
        .cloned()
        .cloned()
        .ok_or_else(|| "ValueError".into())
}
fn pair_candidate(
    root: &Path,
    candidate: &Value,
    state: Arc<discovery_state::Runtime>,
    enrollment: &discovery_enrollment::Runtime,
    tty: bool,
    read_code: impl FnOnce() -> Result<String>,
) -> Result<Value> {
    let trusted = peers(&state, root)?;
    let old = &trusted[candidate["node_id"].as_str().ok_or("INVALID_NODE_ID")?];
    if !old.is_null() && old["security_status"] != "DEV_ONLY" {
        return call(
            enrollment,
            state,
            "discovery.enrollment.client.reconnect",
            &json!({"root":root,"candidate":candidate}),
        );
    }
    if !tty {
        return Err("LOCAL_TTY_REQUIRED_FOR_PIN".into());
    }
    let mut code = read_code()?;
    let result = call(
        enrollment,
        state,
        "discovery.enrollment.client.pair",
        &json!({"root":root,"candidate":candidate,"code":code.trim_end_matches(['\r','\n'])}),
    );
    use zeroize::Zeroize;
    code.zeroize();
    result
}
fn due(candidates: &Value, attempted: &BTreeMap<String, f64>, now: f64) -> Value {
    Value::Object(
        candidates
            .as_object()
            .into_iter()
            .flatten()
            .filter(|(n, _)| now - attempted.get(*n).copied().unwrap_or(-60.) >= 10.)
            .map(|(n, c)| (n.clone(), c.clone()))
            .collect(),
    )
}
fn record_attempts(
    attempted: &mut BTreeMap<String, f64>,
    candidates: &Value,
    due: &Value,
    now: f64,
) {
    for n in due.as_object().into_iter().flatten().map(|(n, _)| n) {
        attempted.insert(n.clone(), now);
    }
    attempted.retain(|n, _| candidates.get(n).is_some());
}
struct Signals {
    stopped: Arc<AtomicBool>,
    ids: Vec<signal_hook::SigId>,
}
impl Signals {
    fn new() -> Result<Self> {
        let mut signals = Self {
            stopped: Arc::new(AtomicBool::new(false)),
            ids: vec![],
        };
        for sig in [signal_hook::consts::SIGINT, signal_hook::consts::SIGTERM] {
            signals.ids.push(
                signal_hook::flag::register(sig, signals.stopped.clone())
                    .map_err(|_| "SIGNAL_REGISTRATION_FAILED")?,
            );
        }
        Ok(signals)
    }
    fn check(&self) -> Result<()> {
        if self.stopped.load(Ordering::Relaxed) {
            Err("PAIRING_CANCELLED".into())
        } else {
            Ok(())
        }
    }
    fn wait(&self, duration: Duration) -> Result<()> {
        let until = Instant::now() + duration;
        loop {
            self.check()?;
            let Some(remaining) = until.checked_duration_since(Instant::now()) else {
                return Ok(());
            };
            std::thread::sleep(remaining.min(Duration::from_millis(20)));
        }
    }
    fn selection(&self) -> Result<String> {
        let input = std::io::stdin();
        loop {
            self.check()?;
            let mut fds = [rustix::event::PollFd::new(
                &input,
                rustix::event::PollFlags::IN,
            )];
            match rustix::event::poll(
                &mut fds,
                Some(&rustix::event::Timespec {
                    tv_sec: 0,
                    tv_nsec: 100_000_000,
                }),
            ) {
                Ok(n) if n > 0 => break,
                Ok(_) | Err(rustix::io::Errno::INTR) => (),
                Err(_) => return Err("DEVICE_SELECTION_FAILED".into()),
            }
        }
        let mut line = String::new();
        input
            .read_line(&mut line)
            .map_err(|_| "DEVICE_SELECTION_FAILED")?;
        Ok(line)
    }
}
impl Drop for Signals {
    fn drop(&mut self) {
        for id in &self.ids {
            signal_hook::low_level::unregister(*id);
        }
    }
}
fn start_server(
    options: &Options,
    root: &Path,
    state: Arc<discovery_state::Runtime>,
    enrollment: &discovery_enrollment::Runtime,
    address: &str,
) -> Result<Value> {
    if let Some(config) = &options.observer {
        discovery_bootstrap::connection(config)?;
    }
    let config = options.observer.clone();
    enrollment.invoke(
        "discovery.enrollment.server.start",
        &json!({"root":root,"address":address,"port":options.port,"observer_enabled":config.is_some()}),
        state,
        move |op, _| {
            if op != "discovery.enrollment.observer" {
                return Err("DISCOVERY_CALLBACK_INVALID".into());
            }
            discovery_bootstrap::connection(config.as_deref().ok_or("OBSERVER_SHARING_DISABLED")?)
        },
    )
}
fn serve(
    options: &Options,
    root: &Path,
    local: &Value,
    browser: &mdns_runtime::Browser,
    output: &mut impl Write,
    state: Arc<discovery_state::Runtime>,
    signals: &Signals,
) -> Result<()> {
    let enrollment = discovery_enrollment::Runtime::default();
    let server = start_server(options, root, state.clone(), &enrollment, "0.0.0.0")?;
    let result = (|| {
        browser.advertise(
            &json!({"identity":identity::public_record(local)?,"port":server["port"]}),
        )?;
        writeln!(
            output,
            "This Device: {} — listening for nearby devices",
            local["display_name"].as_str().unwrap_or("")
        )
        .and_then(|_| output.flush())
        .map_err(|_| "DEVICE_OUTPUT_FAILED")?;
        if options.accepting() {
            let code = state.invoke(
                "discovery.window.open",
                &json!({"window":server["window"],"lifetime":60}),
                |_, _| Err("DISCOVERY_CALLBACK_INVALID".into()),
            )?;
            writeln!(
                output,
                "Pairing code: {} (60 seconds; one use; at most 3 failures)",
                code.as_str().ok_or("PAIRING_FAILED")?
            )
            .and_then(|_| output.flush())
            .map_err(|_| "DEVICE_OUTPUT_FAILED")?;
        }
        let clock = Instant::now();
        let mut attempted = BTreeMap::new();
        while !signals.stopped.load(Ordering::Relaxed) {
            let candidates = browser.candidates()?["candidates"].clone();
            let due = due(&candidates, &attempted, clock.elapsed().as_secs_f64());
            reconnect(root, local, &due, state.clone(), &enrollment)?;
            record_attempts(
                &mut attempted,
                &candidates,
                &due,
                clock.elapsed().as_secs_f64(),
            );
            std::thread::sleep(Duration::from_millis(500));
        }
        Ok(())
    })();
    let closed = call(
        &enrollment,
        state,
        "discovery.enrollment.server.close",
        &json!({"server":server["server"]}),
    );
    result.and(closed.map(|_| ()))
}
fn execute(
    options: &Options,
    interfaces: Option<Vec<String>>,
    output: &mut impl Write,
) -> Result<()> {
    if options.accepting() && (options.json || !std::io::stdout().is_terminal()) {
        return Err("PAIRING_REQUIRES_LOCAL_TTY".into());
    }
    let signals = Signals::new()?;
    let (root, host) = settings(options)?;
    let local = identity::open(&root, &host, options.display.as_deref())?;
    let state = Arc::new(discovery_state::Runtime::default());
    let enrollment = discovery_enrollment::Runtime::default();
    let browser = mdns_runtime::Browser::start(interfaces)?;
    let result = (|| {
        if options.command == "serve" || options.accepting() {
            return serve(options, &root, &local, &browser, output, state, &signals);
        }
        signals.wait(Duration::from_secs_f64(options.wait))?;
        let candidates = browser.candidates()?["candidates"].clone();
        if options.command == "devices" {
            let result = devices(&root, &local, &candidates, state, &enrollment)?;
            signals.check()?;
            if options.json {
                writeln!(output, "{result}").map_err(|_| "DEVICE_OUTPUT_FAILED")?;
            } else {
                for row in result["devices"].as_array().unwrap() {
                    writeln!(
                        output,
                        "{}: {} ({}){}",
                        row["state"].as_str().unwrap(),
                        row["display_name"].as_str().unwrap(),
                        row["node_id"].as_str().unwrap(),
                        if row["authenticated"] == true {
                            " / authenticated"
                        } else {
                            ""
                        }
                    )
                    .map_err(|_| "DEVICE_OUTPUT_FAILED")?;
                }
            }
            return Ok(());
        }
        let tty = std::io::stdin().is_terminal();
        let selected = choose(
            &candidates,
            &local,
            options.device.as_deref(),
            tty,
            output,
            || signals.selection(),
        )?;
        let session = pair_candidate(&root, &selected, state, &enrollment, tty, || {
            crate::node_daemon::pairing_code("4-digit code shown on the other device: ")
        })?;
        signals.check()?;
        if options.json {
            writeln!(output, "{session}").map_err(|_| "DEVICE_OUTPUT_FAILED")?;
        } else {
            writeln!(
                output,
                "Trusted device connected: {}; execution authorization still required",
                selected["display_name"].as_str().unwrap_or("")
            )
            .map_err(|_| "DEVICE_OUTPUT_FAILED")?;
        }
        Ok(())
    })();
    result.and(browser.close())
}
pub fn main_cli(args: Vec<String>) -> i32 {
    if args.first().is_some_and(|s| s == "--device-cli-contract") {
        println!("RUST_DEVICE_CLI_V1");
        return 0;
    }
    if args.iter().any(|s| s == "--help" || s == "-h") {
        println!(
            "clinx [--config PATH] devices|pair [DEVICE|accept]|serve [--state-dir PATH] [--display-name NAME] [--wait SECONDS] [--json]\npair/serve: [--port PORT] [--observer-config PATH]\nDiscover and pair CLINX devices. Execution authorization is separate."
        );
        return 0;
    }
    match Options::parse(args)
        .and_then(|options| execute(&options, None, &mut std::io::stdout().lock()))
    {
        Ok(()) => 0,
        Err(e) if e == "PAIRING_CANCELLED" => 0,
        Err(e) => {
            let safe = if e.chars().all(|c| c.is_ascii_uppercase() || c == '_')
                || matches!(e.as_str(), "ValueError" | "TOMLDecodeError")
            {
                e.as_str()
            } else {
                "ValueError"
            };
            eprintln!("DEVICE_ERROR: {safe}");
            2
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn options(args: &[&str]) -> Options {
        Options::parse(args.iter().map(|s| s.to_string()).collect()).unwrap()
    }
    fn identity(root: &Path, node: &str) -> Value {
        identity::open(root, node, Some(node)).unwrap()
    }
    #[test]
    fn empty_interface_cli_keeps_json_identity_and_config_without_python() {
        let temp = tempfile::tempdir().unwrap();
        let config = temp.path().join("device.toml");
        let root = temp.path().join("devices");
        std::fs::write(
            &config,
            "[runtime]\nruntime_host='workstation-alpha'\ntask_db_path='unread.sqlite3'\n",
        )
        .unwrap();
        let opts = options(&[
            "--config",
            config.to_str().unwrap(),
            "devices",
            "--state-dir",
            root.to_str().unwrap(),
            "--wait",
            "0",
            "--json",
        ]);
        let mut output = vec![];
        execute(&opts, Some(vec![]), &mut output).unwrap();
        let reply: Value = serde_json::from_slice(&output).unwrap();
        assert_eq!(reply["security_status"], "OPAQUE_V1");
        assert_eq!(reply["devices"].as_array().unwrap().len(), 1);
        assert_eq!(reply["devices"][0]["state"], "This Device");
        assert_eq!(reply["devices"][0]["node_id"], "alpha");
        assert_eq!(reply["devices"][0]["authenticated"], false);
        assert!(!temp.path().join("unread.sqlite3").exists());
    }
    #[test]
    fn discovery_interval_and_accept_tty_rejection_precede_initialization() {
        let accepted = options(&["serve", "--wait", "١_٠", "--port", "-٠"]);
        assert_eq!(accepted.wait, 10.);
        assert_eq!(accepted.port, 0);
        for wait in ["-1", "31", "NaN", "inf"] {
            assert_eq!(
                Options::parse(vec!["devices".into(), "--wait".into(), wait.into()]).unwrap_err(),
                "INVALID_DISCOVERY_INTERVAL"
            );
        }
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("not-created");
        let opts = options(&[
            "pair",
            "accept",
            "--state-dir",
            root.to_str().unwrap(),
            "--json",
        ]);
        assert_eq!(
            execute(&opts, Some(vec![]), &mut vec![]).unwrap_err(),
            "PAIRING_REQUIRES_LOCAL_TTY"
        );
        assert!(!root.exists());
        let cancelled = Signals {
            stopped: Arc::new(AtomicBool::new(true)),
            ids: vec![],
        };
        assert_eq!(
            cancelled.wait(Duration::from_secs(30)).unwrap_err(),
            "PAIRING_CANCELLED"
        );
    }
    #[test]
    fn selection_matches_names_exactly_excludes_self_and_checks_decimal_input() {
        let candidates = json!({"self":{"node_id":"self","display_name":"Mine"},"z":{"node_id":"z","display_name":"same"},"a":{"node_id":"a","display_name":"same"}});
        let local = json!({"node_id":"self"});
        assert_eq!(
            choose(
                &candidates,
                &local,
                Some("z"),
                false,
                &mut vec![],
                || panic!()
            )
            .unwrap()["node_id"],
            "z"
        );
        assert_eq!(
            choose(
                &candidates,
                &local,
                Some("same"),
                false,
                &mut vec![],
                || panic!()
            )
            .unwrap_err(),
            "ValueError"
        );
        assert_eq!(
            choose(
                &candidates,
                &local,
                Some("self"),
                false,
                &mut vec![],
                || panic!()
            )
            .unwrap_err(),
            "ValueError"
        );
        let mut output = vec![];
        assert_eq!(
            choose(&candidates, &local, None, true, &mut output, || Ok(
                "١\n".into()
            ))
            .unwrap()["node_id"],
            "a"
        );
        assert_eq!(
            String::from_utf8(output).unwrap(),
            "1. same (a)\n2. same (z)\nSelect device: "
        );
        for input in ["+1", "-1", "1_0", "0", "3", " 1"] {
            assert_eq!(
                choose(&candidates, &local, None, true, &mut vec![], || Ok(
                    input.into()
                ))
                .unwrap_err(),
                "ValueError"
            );
        }
        assert_eq!(
            choose(&candidates, &local, None, false, &mut vec![], || panic!()).unwrap_err(),
            "ValueError"
        );
    }
    #[test]
    fn reconnect_throttle_records_completion_and_expires_departed_nodes() {
        let candidates = json!({"a":{},"b":{}});
        let mut attempts = BTreeMap::new();
        let pending = due(&candidates, &attempts, 0.);
        assert_eq!(pending, candidates);
        record_attempts(&mut attempts, &candidates, &pending, 3.);
        assert_eq!(due(&candidates, &attempts, 12.999), json!({}));
        assert_eq!(due(&candidates, &attempts, 13.), candidates);
        let current = json!({"b":{},"c":{}});
        let pending = due(&current, &attempts, 13.);
        record_attempts(&mut attempts, &current, &pending, 14.);
        assert!(!attempts.contains_key("a"));
        assert_eq!(attempts["b"], 14.);
        assert_eq!(attempts["c"], 14.);
    }
    #[test]
    fn loopback_cli_pair_reconnect_no_pin_observer_readback_and_revocation() {
        use std::os::unix::fs::PermissionsExt;
        let temp = tempfile::tempdir().unwrap();
        let client = temp.path().join("client");
        let server = temp.path().join("server");
        identity(&client, "alpha");
        let remote = identity(&server, "beta");
        let token = temp.path().join("observer.env");
        let config = temp.path().join("observer.json");
        std::fs::write(
            &token,
            format!("CLINX_OBSERVER_TOKEN={}\r\n", "x".repeat(48)),
        )
        .unwrap();
        std::fs::write(&config,json!({"share_read_only_observer":true,"endpoint":"https://beta.ts.net","credential_file":token}).to_string()).unwrap();
        for path in [&token, &config] {
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)).unwrap();
        }
        let runtime = discovery_enrollment::Runtime::default();
        let state = Arc::new(discovery_state::Runtime::default());
        let options = options(&["serve", "--observer-config", config.to_str().unwrap()]);
        let listening =
            start_server(&options, &server, state.clone(), &runtime, "127.0.0.1").unwrap();
        let code = state
            .invoke(
                "discovery.window.open",
                &json!({"window":listening["window"],"lifetime":60}),
                |_, _| panic!(),
            )
            .unwrap();
        let candidate = json!({"node_id":"beta","display_name":"beta","fingerprint":remote["fingerprint"],"endpoints":[["127.0.0.1",listening["port"]]],"mismatch":false});
        assert_eq!(
            pair_candidate(
                &client,
                &candidate,
                state.clone(),
                &runtime,
                false,
                || panic!()
            )
            .unwrap_err(),
            "LOCAL_TTY_REQUIRED_FOR_PIN"
        );
        let session = pair_candidate(&client, &candidate, state.clone(), &runtime, true, || {
            Ok(code.as_str().unwrap().to_owned() + "\n")
        })
        .unwrap();
        assert_eq!(session["authority_granted"], false);
        assert_eq!(session["capabilities"], json!([]));
        assert_eq!(
            pair_candidate(
                &client,
                &candidate,
                state.clone(),
                &runtime,
                false,
                || panic!()
            )
            .unwrap(),
            session
        );
        let args = json!({"root":client,"candidate":candidate});
        let connection = call(
            &runtime,
            state.clone(),
            "discovery.enrollment.client.observer",
            &args,
        )
        .unwrap();
        assert_eq!(
            discovery_bootstrap::validate(&connection).unwrap()["credential"],
            "x".repeat(48)
        );
        std::fs::write(&token, format!("CLINX_OBSERVER_TOKEN={}\n", "y".repeat(48))).unwrap();
        assert_eq!(
            call(
                &runtime,
                state.clone(),
                "discovery.enrollment.client.observer",
                &args
            )
            .unwrap()["credential"],
            "y".repeat(48)
        );
        state
            .invoke(
                "discovery.peers.revoke",
                &json!({"root":server,"node_id":"alpha"}),
                |_, _| panic!(),
            )
            .unwrap();
        assert!(
            pair_candidate(
                &client,
                &candidate,
                state.clone(),
                &runtime,
                false,
                || panic!()
            )
            .is_err()
        );
        call(
            &runtime,
            state,
            "discovery.enrollment.server.close",
            &json!({"server":listening["server"]}),
        )
        .unwrap();
    }
}
