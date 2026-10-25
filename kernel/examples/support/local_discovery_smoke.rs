//! Orchestrates existing Node contracts over private anonymous pipes. Discovery
//! presence is not identity evidence; OPAQUE/TLS and persisted trust must agree.
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    ffi::OsString,
    io::{BufRead, BufReader, Read, Write},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};
use zeroize::{Zeroize, Zeroizing};

type Result<T> = std::result::Result<T, &'static str>;
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Lan,
    Loopback,
}
pub fn options(args: impl Iterator<Item = OsString>) -> Result<(Mode, PathBuf)> {
    let mut args = args;
    let mode = match args.next().as_deref().and_then(|s| s.to_str()) {
        Some("--lan") => Mode::Lan,
        Some("--loopback") => Mode::Loopback,
        _ => return Err("Usage: local_discovery_smoke --lan|--loopback [--node-binary PATH]"),
    };
    let node = match args.next() {
        None => std::env::current_exe()
            .ok()
            .and_then(|p| p.parent()?.parent().map(|p| p.join("clinx-node")))
            .ok_or("SMOKE_NODE_BINARY_REQUIRED")?,
        Some(flag) if flag == "--node-binary" => args
            .next()
            .map(PathBuf::from)
            .ok_or("SMOKE_NODE_BINARY_REQUIRED")?,
        _ => return Err("SMOKE_ARGUMENT_INVALID"),
    };
    if args.next().is_some() {
        return Err("SMOKE_ARGUMENT_INVALID");
    }
    Ok((mode, node))
}

struct Worker {
    child: Child,
    input: Option<ChildStdin>,
    replies: mpsc::Receiver<Result<Value>>,
    reader: Option<thread::JoinHandle<()>>,
    sequence: u64,
}
impl Worker {
    fn start(binary: &Path, home: &Path) -> Result<Self> {
        let mut child = Command::new(binary)
            .arg("--network-worker")
            .env_clear()
            .env("PATH", "/usr/bin:/bin")
            .env("HOME", home)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| "SMOKE_NODE_START_FAILED")?;
        let input = child.stdin.take();
        let output = child.stdout.take().ok_or("SMOKE_PIPE_UNAVAILABLE")?;
        let (tx, replies) = mpsc::channel();
        let reader = thread::spawn(move || {
            let mut output = BufReader::new(output);
            loop {
                let mut line = Zeroizing::new(String::new());
                // This is a private pipe from the explicitly selected Node,
                // never an external log or an inherited stdout stream.
                match output.by_ref().take(1024 * 1024).read_line(&mut line) {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {
                        let event =
                            serde_json::from_str(&line).map_err(|_| "SMOKE_NODE_RESPONSE_INVALID");
                        if tx.send(event).is_err() {
                            break;
                        }
                    }
                }
            }
        });
        Ok(Self {
            child,
            input,
            replies,
            reader: Some(reader),
            sequence: 0,
        })
    }
    fn call(&mut self, operation: &str, arguments: Value) -> Result<Value> {
        self.sequence += 1;
        let mut request = json!({"id":self.sequence,"operation":operation,"arguments":arguments});
        let encoded = Zeroizing::new(request.to_string());
        if let Value::String(code) = &mut request["arguments"]["code"] {
            code.zeroize();
        }
        let input = self.input.as_mut().ok_or("SMOKE_NODE_CLOSED")?;
        writeln!(input, "{}", encoded.as_str())
            .and_then(|_| input.flush())
            .map_err(|_| "SMOKE_PIPE_UNAVAILABLE")?;
        drop(encoded);
        let mut event = self
            .replies
            .recv_timeout(Duration::from_secs(30))
            .map_err(|_| "SMOKE_NODE_TIMEOUT")??;
        if event["id"] != self.sequence || event["kind"] != "result" {
            return Err("SMOKE_UNEXPECTED_CALLBACK_OR_RESPONSE");
        }
        if let Some(error) = event["error"].as_str() {
            return Err(match error {
                "PAIRING_CLOSED" => "PAIRING_CLOSED",
                "TLS_HANDSHAKE_FAILED" => "TLS_HANDSHAKE_FAILED",
                _ => "SMOKE_NODE_REQUEST_REJECTED",
            });
        }
        Ok(event["result"].take())
    }
    fn close(&mut self) -> Result<()> {
        self.input.take();
        let deadline = Instant::now() + Duration::from_secs(5);
        let status = loop {
            if let Some(status) = self
                .child
                .try_wait()
                .map_err(|_| "SMOKE_CHILD_WAIT_FAILED")?
            {
                break status;
            }
            if Instant::now() >= deadline {
                // Only this task-owned child; never a PID from shared state.
                let _ = self.child.kill();
                let _ = self.child.wait();
                return Err("SMOKE_CHILD_EXIT_TIMEOUT");
            }
            thread::sleep(Duration::from_millis(10));
        };
        if let Some(reader) = self.reader.take() {
            reader.join().map_err(|_| "SMOKE_PIPE_READER_FAILED")?;
        }
        if status.success() {
            Ok(())
        } else {
            Err("SMOKE_CHILD_EXIT_FAILED")
        }
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        let _ = self.close();
    }
}

struct Node {
    worker: Worker,
    root: PathBuf,
    identity: Value,
    server: Value,
    browser: Option<Value>,
    closed: bool,
}
impl Node {
    fn start(binary: &Path, root: PathBuf, name: &str, mode: Mode) -> Result<Self> {
        let mut worker = Worker::start(binary, &root)?;
        let identity = worker.call("discovery.identity.open", json!({"root":root,"host":name}))?;
        let server = worker.call("discovery.enrollment.server.start", json!({"root":root,"address":if mode==Mode::Lan {"0.0.0.0"} else {"127.0.0.1"},"port":0}))?;
        let browser = if mode == Mode::Lan {
            let browser = worker.call("mdns.start", json!({"interfaces":null}))?;
            if browser["interfaces"]
                .as_array()
                .is_none_or(|a| a.is_empty())
            {
                return Err("SMOKE_NO_PRIVATE_LAN_INTERFACE");
            }
            worker.call(
                "mdns.advertise",
                json!({"id":browser["id"],"identity":identity["public"],"port":server["port"]}),
            )?;
            Some(browser["id"].clone())
        } else {
            None
        };
        Ok(Self {
            worker,
            root,
            identity,
            server,
            browser,
            closed: false,
        })
    }
    fn candidate(&mut self, target: &Node) -> Result<Value> {
        let public = &target.identity["public"];
        let Some(browser) = &self.browser else {
            // Explicit isolated fixture: no LAN/mDNS claim for this mode.
            return Ok(
                json!({"node_id":public["node_id"],"display_name":public["display_name"],"fingerprint":public["fingerprint"],"endpoints":[["127.0.0.1",target.server["port"]]],"mismatch":false}),
            );
        };
        let deadline = Instant::now() + Duration::from_secs(12);
        loop {
            let records = self.worker.call("mdns.candidates", json!({"id":browser}))?;
            let candidate = &records["candidates"]
                [public["node_id"].as_str().ok_or("SMOKE_IDENTITY_INVALID")?];
            if candidate.is_object() {
                if candidate["fingerprint"] != public["fingerprint"]
                    || candidate["mismatch"] != false
                {
                    return Err("SMOKE_DISCOVERY_IDENTITY_CONFLICT");
                }
                if candidate["endpoints"]
                    .as_array()
                    .is_some_and(|rows| rows.iter().any(|e| e[1] == target.server["port"]))
                {
                    return Ok(candidate.clone());
                }
            }
            if Instant::now() >= deadline {
                return Err("SMOKE_DISCOVERY_TIMEOUT");
            }
            thread::sleep(Duration::from_millis(100));
        }
    }
    fn verify_session(&mut self, session: &Value, target: &Node) -> Result<()> {
        let public = &target.identity["public"];
        let expected = json!({"node_id":public["node_id"],"fingerprint":public["fingerprint"],"transport":"lan_tls","capabilities":[],"authority_granted":false,"authorization_required":true});
        let peers = self.worker.call(
            "discovery.store.read",
            json!({"root":self.root,"name":"peers.json"}),
        )?;
        let peer = &peers["peers"][public["node_id"].as_str().ok_or("SMOKE_IDENTITY_INVALID")?];
        if *session != expected
            || peer["security_status"] != "OPAQUE_V1"
            || peer["trust_state"] != "TRUSTED"
            || peer["fingerprint"] != public["fingerprint"]
        {
            return Err("SMOKE_SESSION_OR_TRUST_CONFLICT");
        }
        // Pairing authenticates devices and grants no execution/read scopes.
        let sharing = self.worker.call(
            "discovery.store.read",
            json!({"root":self.root,"name":"sharing.json"}),
        )?;
        if !sharing.is_null() {
            return Err("SMOKE_UNEXPECTED_AUTHORIZATION");
        }
        Ok(())
    }
    fn reconnect(&mut self, target: &Node) -> Result<()> {
        let candidate = self.candidate(target)?;
        let session = self.worker.call(
            "discovery.enrollment.client.reconnect",
            json!({"root":self.root,"candidate":candidate}),
        )?;
        self.verify_session(&session, target)
    }
    fn window_closed(&mut self) -> Result<()> {
        if self
            .worker
            .call(
                "discovery.window.reserve",
                json!({"window":self.server["window"]}),
            )
            .err()
            != Some("PAIRING_CLOSED")
        {
            return Err("SMOKE_UNEXPECTED_OPEN_PAIRING_WINDOW");
        }
        Ok(())
    }
    fn close(&mut self) -> Result<()> {
        if self.closed {
            return Ok(());
        }
        self.closed = true;
        let cleanup = (|| {
            if let Some(id) = self.browser.take() {
                self.worker.call("mdns.close", json!({"id":id}))?;
            }
            self.worker.call(
                "discovery.enrollment.server.close",
                json!({"server":self.server["server"]}),
            )?;
            Ok(())
        })();
        let exited = self.worker.close();
        cleanup.and(exited)
    }
}
impl Drop for Node {
    fn drop(&mut self) {
        let _ = self.close();
    }
}

pub fn run(binary: &Path, mode: Mode) -> Result<Value> {
    let state = tempfile::Builder::new()
        .prefix("clinx-discovery-smoke-")
        .tempdir()
        .map_err(|_| "SMOKE_PRIVATE_STATE_UNAVAILABLE")?;
    let suffix = format!(
        "{:x}",
        Sha256::digest(state.path().as_os_str().as_encoded_bytes())
    );
    let alpha = "clinx-smoke-a-".to_owned() + &suffix[..16];
    let beta = "clinx-smoke-b-".to_owned() + &suffix[..16];
    let mut a = Node::start(binary, state.path().join("a"), &alpha, mode)?;
    let mut b = Node::start(binary, state.path().join("b"), &beta, mode)?;
    let candidate = a.candidate(&b)?;
    // PIN exists only in these private pipes and RAM. No argv/env/file/stdout
    // copy and no debug formatting; the request serialization is zeroized.
    let code = b.worker.call(
        "discovery.window.open",
        json!({"window":b.server["window"],"lifetime":60}),
    )?;
    let mut arguments = json!({"root":a.root,"candidate":candidate});
    arguments["code"] = code;
    let paired = a
        .worker
        .call("discovery.enrollment.client.pair", arguments)?;
    a.verify_session(&paired, &b)?;
    b.reconnect(&a)?;
    let identities = (a.identity.clone(), b.identity.clone());
    a.close()?;
    b.close()?;
    // Waited actual process exits: new workers reload stable identity/trust.
    let mut a = Node::start(binary, state.path().join("a"), &alpha, mode)?;
    let mut b = Node::start(binary, state.path().join("b"), &beta, mode)?;
    if a.identity != identities.0 || b.identity != identities.1 {
        return Err("SMOKE_RESTART_IDENTITY_DRIFT");
    }
    a.window_closed()?;
    b.window_closed()?;
    a.reconnect(&b)?;
    b.reconnect(&a)?;
    let mut imposter = Node::start(
        binary,
        state.path().join("imposter"),
        &("clinx-smoke-imposter-".to_owned() + &suffix[..16]),
        mode,
    )?;
    let mut target = a.candidate(&b)?;
    let endpoint = a.candidate(&imposter)?["endpoints"][0].clone();
    target["endpoints"] = json!([endpoint]);
    // Keep beta's authentic fingerprint/identity, connect to an actual server
    // with another key. Require a TLS failure, not any generic network failure.
    let rejected = a.worker.call(
        "discovery.enrollment.client.reconnect",
        json!({"root":a.root,"candidate":target}),
    );
    if rejected.err() != Some("TLS_HANDSHAKE_FAILED") {
        return Err("SMOKE_WRONG_TLS_IDENTITY_NOT_REJECTED");
    }
    a.reconnect(&b)?;
    b.reconnect(&a)?;
    a.window_closed()?;
    b.window_closed()?;
    imposter.close()?;
    a.close()?;
    b.close()?;
    Ok(
        json!({"contract":"RUST_DISCOVERY_SMOKE_V1","mode":if mode==Mode::Lan {"lan"} else {"isolated_loopback"},"mdns":if mode==Mode::Lan {"PASS"} else {"NOT_RUN_SEPARATE_PRIVATE_UDP_QUALIFICATION"},"opaque_pairing":"PASS","two_process_restart":"PASS","bidirectional_mtls_without_pin":"PASS","wrong_tls_identity_rejected":"PASS","authorization_isolation":"PASS","physical_cross_device":"NOT_VERIFIED"}),
    )
}
