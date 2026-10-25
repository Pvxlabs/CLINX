//! Opt-in local two-process Discovery qualification. Never a production entrypoint.
//! Build: cargo build --example local_discovery_smoke --bin clinx-node
//! Run: target/debug/examples/local_discovery_smoke --lan
//! --loopback exercises the same TLS/restart flow without multicast or LAN IO;
//! the mdns_runtime private UDP wire tests qualify DNS-SD separately.
#[path = "support/local_discovery_smoke.rs"]
mod smoke;

fn main() {
    match smoke::options(std::env::args_os().skip(1))
        .and_then(|(mode, node)| smoke::run(&node, mode))
    {
        Ok(receipt) => println!("{receipt}"),
        Err(code) => {
            // Only fixed local error codes. Child payloads, PINs and paths are
            // never copied into stdout/stderr, including failure reporting.
            eprintln!("{code}");
            std::process::exit(1);
        }
    }
}
