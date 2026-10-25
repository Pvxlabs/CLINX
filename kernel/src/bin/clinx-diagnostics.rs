#[path = "../devops_command.rs"]
mod command;
#[path = "../diagnostics.rs"]
mod diagnostics;
use std::{collections::BTreeMap, path::Path, time::Duration};
fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args.is_empty() || args.iter().any(|a| a == "--help") {
        println!(
            "clinx-diagnostics embedded-bridge --base-url URL --tunnel-pid PID --bridge-pid PID [--observe-seconds 10]\nclinx-diagnostics tunnel-health --url-file PATH [--max-age 120]\nRead-only. Embedded Bridge quiet history never grants recycle authority."
        );
        return;
    }
    let result = (|| -> diagnostics::Result<serde_json::Value> {
        let mut opts = BTreeMap::new();
        let mut i = 1;
        while i < args.len() {
            let key = args[i].strip_prefix("--").ok_or("INVALID_ARGUMENT")?;
            if opts
                .insert(key, args.get(i + 1).ok_or("INVALID_ARGUMENT")?.clone())
                .is_some()
            {
                return Err("DUPLICATE_ARGUMENT".into());
            }
            i += 2;
        }
        let get = |k| {
            opts.get(k)
                .cloned()
                .ok_or_else(|| format!("ARGUMENT_REQUIRED:{k}"))
        };
        match args[0].as_str() {
            "embedded-bridge" => {
                if opts.keys().any(|k| {
                    !["base-url", "tunnel-pid", "bridge-pid", "observe-seconds"].contains(k)
                }) {
                    return Err("INVALID_ARGUMENT".into());
                }
                diagnostics::collect(
                    &get("base-url")?,
                    get("tunnel-pid")?
                        .parse()
                        .map_err(|_| "EXACT_PID_REQUIRED")?,
                    get("bridge-pid")?
                        .parse()
                        .map_err(|_| "EXACT_PID_REQUIRED")?,
                    opts.get("observe-seconds")
                        .map(|s| s.parse())
                        .transpose()
                        .map_err(|_| "INVALID_DURATION")?
                        .unwrap_or(10),
                )
            }
            "tunnel-health" => {
                if opts.keys().any(|k| !["url-file", "max-age"].contains(k)) {
                    return Err("INVALID_ARGUMENT".into());
                }
                diagnostics::health(
                    Path::new(&get("url-file")?),
                    opts.get("max-age")
                        .map(|s| s.parse())
                        .transpose()
                        .map_err(|_| "INVALID_MAX_AGE")?
                        .unwrap_or(120.),
                    |a| command::run(a, Duration::from_secs(15)),
                )
            }
            _ => Err("INVALID_MODE".into()),
        }
    })();
    match result {
        Ok(v) => {
            println!("{v}");
            if v["safe_to_recycle"] == false {
                std::process::exit(2);
            }
        }
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(1);
        }
    }
}
