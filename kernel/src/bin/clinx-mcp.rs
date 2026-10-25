//! API-identity stdio MCP server. No task database or execution worker.
#![forbid(unsafe_code)]
use clinx_decision_kernel::{
    MAX_FRAME_BYTES,
    mcp::{self, Directory, NoObservations, Observations, Server},
    mcp_api::{Api, NativeReads},
    native_transport::{Authority, UnixAuthority},
    provider::Result,
};
use serde_json::{Value, json};
use std::{
    io::{BufRead, BufReader, Read, Write},
    path::PathBuf,
};
struct SelectedAuthority(Option<UnixAuthority>);
impl Authority for SelectedAuthority {
    fn call(&mut self, operation: &str, payload: Value, id: &str) -> Result<Value> {
        self.0
            .as_mut()
            .ok_or("OBSERVATION_READ_ONLY")?
            .call(operation, payload, id)
    }
}
fn run() -> Result<()> {
    let mut args = std::env::args().skip(1);
    let mut config = None;
    let mut call = None;
    let mut stdio = false;
    let mut observations_only = false;
    while let Some(a) = args.next() {
        match a.as_str() {
            "--config" => config = args.next().map(PathBuf::from),
            "--stdio" => stdio = true,
            "--observations-only" => observations_only = true,
            "--observation-runtime-contract" => {
                println!("RUST_OBSERVATION_DIRECTORY_V1");
                return Ok(());
            }
            "--call" => call = args.next(),
            "--no-recover-existing" => {}
            _ => return Err("MCP_ARGUMENT_INVALID".into()),
        }
    }
    if !stdio && call.is_none() {
        return Err("MCP_STDIO_REQUIRED".into());
    }
    if observations_only && call.is_some() {
        return Err("OBSERVATION_READ_ONLY".into());
    }
    let (api, obs): (_, Box<dyn Observations>) = if observations_only {
        // No Authority/socket/config is opened by the standalone read-only API.
        let cfg = clinx_decision_kernel::mcp_api::Config {
            runtime_host: String::new(),
            provider_endpoint: PathBuf::from("/unconfigured-provider"),
            provider_timeout_seconds: 3.,
            host_aliases: Default::default(),
            codex_host_ids: Default::default(),
        };
        (
            Api {
                authority: SelectedAuthority(None),
                reads: NativeReads(cfg.clone()),
                config: cfg,
            },
            Box::new(Directory::standalone()?),
        )
    } else {
        let path = config.ok_or("MCP_CONFIG_REQUIRED")?;
        let cfg = mcp::load_config(&path)?;
        let mut authority = UnixAuthority {
            endpoint: std::env::var_os("CLINX_RUST_SOCKET")
                .map(PathBuf::from)
                .ok_or("RUST_AUTHORITY_CONFIG_REQUIRED")?,
            uid: std::env::var("CLINX_RUST_UID")
                .ok()
                .and_then(|s| s.parse().ok())
                .ok_or("RUST_AUTHORITY_CONFIG_REQUIRED")?,
        };
        if authority.call("health", json!({}), "health")?["business_commands"] != true {
            return Err("RUST_BUSINESS_AUTHORITY_REQUIRED".into());
        }
        let obs: Box<dyn Observations> = if call.is_none()
            && std::env::var("CLINX_RUST_OBSERVATION_READS").as_deref() == Ok("1")
        {
            Box::new(Directory::new(&path)?)
        } else {
            Box::new(NoObservations)
        };
        (
            Api {
                authority: SelectedAuthority(Some(authority)),
                reads: NativeReads(cfg.clone()),
                config: cfg,
            },
            obs,
        )
    };
    let mut server = Server {
        api,
        observations: obs,
    };
    let stdin = std::io::stdin();
    let mut input = BufReader::new(stdin.lock());
    let mut output = std::io::stdout().lock();
    if let Some(name) = call {
        // Observation backend may request exact managed context only. No
        // generic subprocess business-command channel is exposed here.
        if name != "clinx_get_context" {
            return Err("OBSERVER_READ_ONLY".into());
        }
        let mut bytes = vec![];
        input
            .take((MAX_FRAME_BYTES + 1) as u64)
            .read_to_end(&mut bytes)
            .map_err(|_| "INVALID_REQUEST")?;
        if bytes.len() > MAX_FRAME_BYTES {
            return Err("REQUEST_TOO_LARGE".into());
        }
        let a: Value = serde_json::from_slice(&bytes).map_err(|_| "INVALID_REQUEST")?;
        let r = server
            .api
            .invoke(&name, &a)
            .map(|v| json!({"result":mcp::public_json(v)}))
            .unwrap_or_else(|e| json!({"error":mcp::tool_error(e,&name,&a)["error_code"]}));
        serde_json::to_writer(&mut output, &r).map_err(|_| "MCP_OUTPUT_UNAVAILABLE")?;
        output
            .write_all(b"\n")
            .map_err(|_| "MCP_OUTPUT_UNAVAILABLE")?;
        return Ok(());
    }
    loop {
        let mut bytes = vec![];
        let n = (&mut input)
            .take((MAX_FRAME_BYTES + 1) as u64)
            .read_until(b'\n', &mut bytes)
            .map_err(|_| "MCP_INPUT_UNAVAILABLE")?;
        if n == 0 {
            break;
        }
        // Do not parse the tail of an oversized frame as a fresh request.
        if bytes.len() > MAX_FRAME_BYTES {
            return Err("REQUEST_TOO_LARGE".into());
        }
        if bytes.iter().all(u8::is_ascii_whitespace) {
            continue;
        }
        let r = match serde_json::from_slice(&bytes) {
            Ok(v) => server.handle(v),
            Err(_) => Some(
                json!({"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"invalid JSON"}}),
            ),
        };
        if let Some(r) = r {
            serde_json::to_writer(&mut output, &r).map_err(|_| "MCP_OUTPUT_UNAVAILABLE")?;
            output
                .write_all(b"\n")
                .and_then(|_| output.flush())
                .map_err(|_| "MCP_OUTPUT_UNAVAILABLE")?
        }
    }
    Ok(())
}
fn main() {
    if let Err(e) = run() {
        eprintln!("MCP_SERVER=BLOCKED: {e}");
        std::process::exit(1)
    }
}
