//! Helper request API and paired Node network process. Never a task-state writer.
fn main() {
    if std::env::args().nth(1).as_deref() == Some("--collector-contract") {
        println!("RUST_COLLECTOR_V1");
        return;
    }
    if std::env::args().nth(1).as_deref() == Some("--observation-contract") {
        println!("RUST_OBSERVATION_V1");
        return;
    }
    if std::env::args().nth(1).as_deref() == Some("--network-contract") {
        println!("RUST_NODE_NETWORK_V1");
        return;
    }
    if std::env::args().nth(1).as_deref() == Some("--network-worker") {
        if clinx_decision_kernel::node_network::serve().is_err() {
            eprintln!("NODE_NETWORK_UNAVAILABLE");
            std::process::exit(1);
        }
        return;
    }
    let input = std::io::stdin();
    let output = std::io::stdout();
    if clinx_decision_kernel::request_pipe::serve(input.lock(), output.lock()).is_err() {
        eprintln!("REQUEST_PIPE_UNAVAILABLE");
        std::process::exit(1);
    }
}
