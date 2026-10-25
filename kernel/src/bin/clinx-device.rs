//! Native production device CLI; no Python runtime.
fn main() {
    std::process::exit(clinx_decision_kernel::discovery_cli::main_cli(
        std::env::args().skip(1).collect(),
    ));
}
