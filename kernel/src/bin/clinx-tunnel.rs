//! Standalone Rust Tunnel lifecycle and health entrypoint.
fn main() {
    std::process::exit(clinx_decision_kernel::tunnel_runtime::main_cli(
        std::env::args().skip(1).collect(),
    ));
}
