//! Independent Rust Helper/Centre service entrypoint.
fn main() {
    std::process::exit(clinx_decision_kernel::node_daemon::main_cli(
        std::env::args().skip(1).collect(),
    ));
}
