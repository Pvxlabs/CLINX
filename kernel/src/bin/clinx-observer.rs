//! Independent read-only Observer service; no Python runtime or execution APIs.
#![forbid(unsafe_code)]
fn main() {
    if std::env::args().nth(1).as_deref() == Some("--history-mount-guard") {
        if std::env::args().len() != 2 {
            eprintln!("Usage: clinx-observer --history-mount-guard");
            std::process::exit(1);
        }
        if let Err(error) = clinx_decision_kernel::observer_runtime::history_guard::run() {
            eprintln!("{error}");
            std::process::exit(1);
        }
        return;
    }
    if std::env::args().nth(1).as_deref() == Some("--observer-contract") {
        println!("RUST_OBSERVER_V1");
        return;
    }
    if std::env::args().nth(1).as_deref() == Some("--runtime-contract") {
        println!("RUST_OBSERVER_RUNTIME_V1");
        return;
    }
    if std::env::args().nth(1).as_deref() == Some("--native-writer-probe") {
        if let Some(value) = std::env::args()
            .nth(2)
            .and_then(|s| serde_json::from_str(&s).ok())
        {
            println!(
                "{}",
                clinx_decision_kernel::observation_runtime::inspect_writer(&value)
            );
            return;
        }
        eprintln!("NATIVE_PROBE_INVALID");
        std::process::exit(1);
    }
    if let Err(error) = clinx_decision_kernel::observer_runtime::run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}
