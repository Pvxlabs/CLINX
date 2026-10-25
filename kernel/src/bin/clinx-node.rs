//! Helper request API and paired Node network process. Never a task-state writer.
fn main() {
    if std::env::args().nth(1).as_deref() == Some("--discovery-gui") {
        std::process::exit(clinx_decision_kernel::discovery_gui::main_cli());
    }
    if std::env::args().nth(1).as_deref() == Some("--device-runtime-contract") {
        println!("RUST_DEVICE_RUNTIME_V1");
        return;
    }
    if std::env::args().nth(1).as_deref() == Some("--native-writer-probe") {
        let result = std::env::args()
            .nth(2)
            .and_then(|s| serde_json::from_str(&s).ok())
            .ok_or_else(|| "NATIVE_PROBE_INVALID".to_string())
            .map(|v| clinx_decision_kernel::observation_runtime::inspect_writer(&v));
        match result {
            Ok(value) => println!("{value}"),
            Err(error) => {
                eprintln!("{error}");
                std::process::exit(1);
            }
        }
        return;
    }
    if std::env::args().nth(1).as_deref() == Some("--thin-runtime-contract") {
        println!("RUST_THIN_RUNTIME_V1");
        return;
    }
    if std::env::args().nth(1).as_deref() == Some("--provider-process-child") {
        let _ = clinx_decision_kernel::provider_client::exec_process_child();
        std::process::exit(127);
    }
    if std::env::args().nth(1).as_deref() == Some("--client-runtime-contract") {
        println!("RUST_CLIENT_RUNTIME_V1");
        return;
    }
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
